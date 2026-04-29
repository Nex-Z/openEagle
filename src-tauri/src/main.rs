#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs,
    io::Write,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use chrono::Utc;
use enigo::{Enigo, Key, KeyboardControllable, MouseButton, MouseControllable};
use regex::Regex;
use screenshots::Screen;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, RunEvent, State, WebviewUrl,
    WebviewWindowBuilder,
};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

const BACKEND_EVENT: &str = "backend://status";
const READY_PATTERN: &str = r"\[AGENT_READY\]\s+WS_PORT:\s+(\d+)";
const SIDECAR_NAME: &str = "binaries/open-eagle-agent";
const SOLO_OVERLAY_LABEL: &str = "solo_overlay";
const SOLO_OVERLAY_WIDTH: f64 = 400.0;
const SOLO_OVERLAY_HEIGHT: f64 = 240.0;
const SOLO_OVERLAY_MARGIN: i32 = 18;
const SOLO_OVERLAY_AUTO_HIDE_MS: u64 = 4000;
const CONVERSATION_INDEX_FILE: &str = "conversation-index.json";
const CONVERSATION_DIR: &str = "conversations";

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendStatePayload {
    phase: String,
    port: Option<u16>,
    message: String,
}

impl BackendStatePayload {
    fn starting(message: impl Into<String>) -> Self {
        Self {
            phase: "starting".into(),
            port: None,
            message: message.into(),
        }
    }

    fn ready(port: u16) -> Self {
        Self {
            phase: "ready".into(),
            port: Some(port),
            message: format!("Backend is ready on port {port}"),
        }
    }

    fn error(message: impl Into<String>) -> Self {
        Self {
            phase: "error".into(),
            port: None,
            message: message.into(),
        }
    }

    fn disconnected(message: impl Into<String>) -> Self {
        Self {
            phase: "disconnected".into(),
            port: None,
            message: message.into(),
        }
    }
}

impl Default for BackendStatePayload {
    fn default() -> Self {
        Self::starting("Desktop shell is booting the backend")
    }
}

#[derive(Default)]
struct BackendRuntime {
    state: Mutex<BackendStatePayload>,
    child: Mutex<Option<CommandChild>>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ScreenshotResult {
    path: String,
    width: u32,
    height: u32,
    captured_at: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ActionPayload {
    action: String,
    #[serde(default)]
    x: Option<f64>,
    #[serde(default)]
    y: Option<f64>,
    #[serde(default)]
    delta: Option<i32>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    keys: Option<Vec<String>>,
    #[serde(default)]
    screen_width: Option<f64>,
    #[serde(default)]
    screen_height: Option<f64>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct OverlayPayload {
    title: Option<String>,
    detail: Option<String>,
    step_text: Option<String>,
    history_text: Option<String>,
    state: Option<String>,
    step_count: Option<u32>,
    max_steps: Option<u32>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SoloResultNotificationPayload {
    request_id: Option<String>,
    state: String,
    detail: Option<String>,
}

#[tauri::command]
fn get_backend_state(state: State<'_, Arc<BackendRuntime>>) -> BackendStatePayload {
    state.state.lock().unwrap().clone()
}

fn conversation_store_root(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map_err(|err| format!("failed to resolve app data dir: {err}"))
}

fn conversation_index_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(conversation_store_root(app)?.join(CONVERSATION_INDEX_FILE))
}

fn conversations_dir(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(conversation_store_root(app)?.join(CONVERSATION_DIR))
}

fn validate_conversation_id(conversation_id: &str) -> Result<(), String> {
    if conversation_id.is_empty()
        || !conversation_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Err("conversationId may only contain A-Z, a-z, 0-9, _ and -".to_string());
    }
    Ok(())
}

fn conversation_file_path(app: &AppHandle, conversation_id: &str) -> Result<PathBuf, String> {
    validate_conversation_id(conversation_id)?;
    Ok(conversations_dir(app)?.join(format!("{conversation_id}.json")))
}

fn read_json_file(path: &PathBuf) -> Result<Value, String> {
    let text = fs::read_to_string(path)
        .map_err(|err| format!("failed to read {}: {err}", path.display()))?;
    serde_json::from_str(&text)
        .map_err(|err| format!("failed to parse {}: {err}", path.display()))
}

fn atomic_write_json(path: &PathBuf, value: &Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create {}: {err}", parent.display()))?;
    }

    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| format!("invalid target file path: {}", path.display()))?;
    let tmp_path = path.with_file_name(format!("{file_name}.tmp"));
    let data = serde_json::to_vec_pretty(value)
        .map_err(|err| format!("failed to serialize {}: {err}", path.display()))?;

    {
        let mut file = fs::File::create(&tmp_path)
            .map_err(|err| format!("failed to create {}: {err}", tmp_path.display()))?;
        file.write_all(&data)
            .map_err(|err| format!("failed to write {}: {err}", tmp_path.display()))?;
        file.write_all(b"\n")
            .map_err(|err| format!("failed to finish {}: {err}", tmp_path.display()))?;
        file.sync_all()
            .map_err(|err| format!("failed to sync {}: {err}", tmp_path.display()))?;
    }

    if path.exists() {
        fs::remove_file(path)
            .map_err(|err| format!("failed to replace {}: {err}", path.display()))?;
    }
    fs::rename(&tmp_path, path).map_err(|err| {
        format!(
            "failed to rename {} to {}: {err}",
            tmp_path.display(),
            path.display()
        )
    })?;
    Ok(())
}

fn normalize_conversation_index(value: Value) -> Result<Value, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "conversation index must be an object".to_string())?;
    let version = object
        .get("version")
        .and_then(|value| value.as_u64())
        .unwrap_or(1);
    let conversations = object
        .get("conversations")
        .filter(|value| value.is_array())
        .cloned()
        .unwrap_or_else(|| json!([]));
    Ok(json!({
        "version": version,
        "conversations": conversations,
    }))
}

fn normalize_conversation_file(value: Value) -> Result<Value, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "conversation file must be an object".to_string())?;
    let summary = object
        .get("summary")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or_else(|| "conversation file requires summary object".to_string())?;
    let conversation_id = summary
        .get("id")
        .and_then(|value| value.as_str())
        .ok_or_else(|| "conversation summary requires id".to_string())?;
    validate_conversation_id(conversation_id)?;
    let messages = object
        .get("messages")
        .filter(|value| value.is_array())
        .cloned()
        .unwrap_or_else(|| json!([]));
    let saved_at = object
        .get("savedAt")
        .and_then(|value| value.as_str())
        .map(|value| value.to_string())
        .unwrap_or_else(|| Utc::now().to_rfc3339());

    Ok(json!({
        "version": object.get("version").and_then(|value| value.as_u64()).unwrap_or(1),
        "summary": summary,
        "messages": messages,
        "savedAt": saved_at,
    }))
}

#[tauri::command]
fn load_conversation_index(app: AppHandle) -> Result<Value, String> {
    let path = conversation_index_path(&app)?;
    if !path.exists() {
        return Ok(json!({
            "version": 1,
            "conversations": [],
        }));
    }
    normalize_conversation_index(read_json_file(&path)?)
}

#[tauri::command]
fn save_conversation_index(app: AppHandle, index: Value) -> Result<Value, String> {
    let normalized = normalize_conversation_index(index)?;
    let path = conversation_index_path(&app)?;
    atomic_write_json(&path, &normalized)?;
    Ok(json!({"ok": true}))
}

#[tauri::command]
fn load_conversation_file(app: AppHandle, conversation_id: String) -> Result<Value, String> {
    let path = conversation_file_path(&app, &conversation_id)?;
    if !path.exists() {
        return Err(format!("conversation file does not exist: {conversation_id}"));
    }
    normalize_conversation_file(read_json_file(&path)?)
}

#[tauri::command]
fn save_conversation_file(app: AppHandle, conversation: Value) -> Result<Value, String> {
    let normalized = normalize_conversation_file(conversation)?;
    let conversation_id = normalized
        .get("summary")
        .and_then(|summary| summary.get("id"))
        .and_then(|value| value.as_str())
        .ok_or_else(|| "conversation summary requires id".to_string())?;
    let path = conversation_file_path(&app, conversation_id)?;
    atomic_write_json(&path, &normalized)?;
    Ok(json!({"ok": true}))
}

#[tauri::command]
fn delete_conversation_file(app: AppHandle, conversation_id: String) -> Result<Value, String> {
    let path = conversation_file_path(&app, &conversation_id)?;
    if path.exists() {
        fs::remove_file(&path)
            .map_err(|err| format!("failed to delete {}: {err}", path.display()))?;
    }
    Ok(json!({"ok": true}))
}

#[tauri::command]
fn capture_screenshot() -> Result<ScreenshotResult, String> {
    println!("[SOLO/RUST] capture_screenshot",);
    let screens = Screen::all().map_err(|err| format!("failed to enumerate screens: {err}"))?;
    let screen = screens
        .first()
        .ok_or_else(|| "no screen found".to_string())?;
    let image = screen
        .capture()
        .map_err(|err| format!("failed to capture screenshot: {err}"))?;
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|err| format!("system time error: {err}"))?
        .as_millis();
    let target = std::env::temp_dir().join(format!("open_eagle_solo_{timestamp}.png"));
    image
        .save(&target)
        .map_err(|err| format!("failed to save screenshot: {err}"))?;
    Ok(ScreenshotResult {
        path: target.to_string_lossy().to_string(),
        width: image.width(),
        height: image.height(),
        captured_at: Utc::now().to_rfc3339(),
    })
}

#[tauri::command]
fn read_image_data_url(path: String) -> Result<String, String> {
    // Normalize POSIX paths (from Python) to OS-native paths.
    let normalized = path.replace('/', std::path::MAIN_SEPARATOR_STR);
    let target = PathBuf::from(&normalized);
    if !target.exists() {
        return Err(format!("image file does not exist: {path}"));
    }
    if !target.is_file() {
        return Err(format!("image path is not a file: {path}"));
    }
    let bytes = fs::read(&target).map_err(|err| format!("failed to read image: {err}"))?;
    let ext = target
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let mime = match ext.as_str() {
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "gif" => "image/gif",
        "bmp" => "image/bmp",
        _ => "image/png",
    };
    let encoded = STANDARD.encode(bytes);
    Ok(format!("data:{mime};base64,{encoded}"))
}

fn parse_key(token: &str) -> Option<Key> {
    match token.to_lowercase().as_str() {
        "ctrl" | "control" => Some(Key::Control),
        "alt" => Some(Key::Alt),
        "shift" => Some(Key::Shift),
        "meta" | "win" | "cmd" => Some(Key::Meta),
        "enter" => Some(Key::Return),
        "tab" => Some(Key::Tab),
        "esc" | "escape" => Some(Key::Escape),
        "delete" => Some(Key::Delete),
        "backspace" => Some(Key::Backspace),
        "f4" => Some(Key::F4),
        value if value.len() == 1 => value.chars().next().map(Key::Layout),
        _ => None,
    }
}

fn normalize_point(value: f64, max: Option<f64>) -> i32 {
    if let Some(bound) = max {
        if value >= 0.0 && value <= 1.0 {
            return (value * bound) as i32;
        }
    }
    value as i32
}

#[tauri::command]
fn perform_mouse_action(payload: Value) -> Result<Value, String> {
    let args: ActionPayload =
        serde_json::from_value(payload).map_err(|err| format!("invalid payload: {err}"))?;
    let mut enigo = Enigo::new();
    println!("[SOLO/RUST] perform_mouse_action action={}", args.action);
    match args.action.as_str() {
        "click" => {
            if let (Some(x), Some(y)) = (args.x, args.y) {
                let nx = normalize_point(x, args.screen_width);
                let ny = normalize_point(y, args.screen_height);
                enigo.mouse_move_to(nx, ny);
            }
            enigo.mouse_click(MouseButton::Left);
        }
        "double_click" => {
            if let (Some(x), Some(y)) = (args.x, args.y) {
                let nx = normalize_point(x, args.screen_width);
                let ny = normalize_point(y, args.screen_height);
                enigo.mouse_move_to(nx, ny);
            }
            enigo.mouse_click(MouseButton::Left);
            std::thread::sleep(Duration::from_millis(80));
            enigo.mouse_click(MouseButton::Left);
        }
        "right_click" => {
            if let (Some(x), Some(y)) = (args.x, args.y) {
                let nx = normalize_point(x, args.screen_width);
                let ny = normalize_point(y, args.screen_height);
                enigo.mouse_move_to(nx, ny);
            }
            enigo.mouse_click(MouseButton::Right);
        }
        "move_mouse" => {
            let x = args.x.ok_or_else(|| "move_mouse requires x".to_string())?;
            let y = args.y.ok_or_else(|| "move_mouse requires y".to_string())?;
            let nx = normalize_point(x, args.screen_width);
            let ny = normalize_point(y, args.screen_height);
            enigo.mouse_move_to(nx, ny);
        }
        "scroll" => {
            let delta = args.delta.unwrap_or(0);
            enigo.mouse_scroll_y(delta);
        }
        other => {
            return Err(format!("unsupported mouse action: {other}"));
        }
    }
    Ok(json!({
        "ok": true,
        "action": args.action,
    }))
}

#[tauri::command]
fn perform_keyboard_action(payload: Value) -> Result<Value, String> {
    let args: ActionPayload =
        serde_json::from_value(payload).map_err(|err| format!("invalid payload: {err}"))?;
    let mut enigo = Enigo::new();
    println!("[SOLO/RUST] perform_keyboard_action action={}", args.action);
    match args.action.as_str() {
        "type_text" => {
            let text = args
                .text
                .ok_or_else(|| "type_text requires text".to_string())?;
            enigo.key_sequence(&text);
        }
        "press_keys" => {
            let keys = args
                .keys
                .ok_or_else(|| "press_keys requires keys".to_string())?;
            let parsed: Vec<Key> = keys.iter().filter_map(|item| parse_key(item)).collect();
            if parsed.is_empty() {
                return Err("no valid keys to press".to_string());
            }
            for key in parsed.iter().take(parsed.len().saturating_sub(1)) {
                enigo.key_down(*key);
            }
            if let Some(last) = parsed.last() {
                enigo.key_click(*last);
            }
            for key in parsed.iter().take(parsed.len().saturating_sub(1)).rev() {
                enigo.key_up(*key);
            }
        }
        other => {
            return Err(format!("unsupported keyboard action: {other}"));
        }
    }
    Ok(json!({
        "ok": true,
        "action": args.action,
    }))
}

fn position_overlay(window: &tauri::WebviewWindow) {
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten());
    if let Some(monitor) = monitor {
        let work_area = monitor.work_area();
        let width = SOLO_OVERLAY_WIDTH.round() as i32;
        let height = SOLO_OVERLAY_HEIGHT.round() as i32;
        let x = work_area.position.x + work_area.size.width as i32 - width - SOLO_OVERLAY_MARGIN;
        let y = work_area.position.y + work_area.size.height as i32 - height - SOLO_OVERLAY_MARGIN;
        let _ = window.set_position(PhysicalPosition::new(
            x.max(work_area.position.x),
            y.max(work_area.position.y),
        ));
    }
}

fn normalize_overlay_payload(payload: OverlayPayload) -> OverlayPayload {
    OverlayPayload {
        title: Some(
            payload
                .title
                .unwrap_or_else(|| "SOLO 正在执行桌面操作".to_string()),
        ),
        detail: Some(
            payload
                .detail
                .unwrap_or_else(|| "请保持桌面可见，可随时暂停或结束。".to_string()),
        ),
        step_text: Some(payload.step_text.unwrap_or_default()),
        history_text: Some(payload.history_text.unwrap_or_default()),
        state: Some(payload.state.unwrap_or_else(|| "running".to_string())),
        step_count: Some(payload.step_count.unwrap_or(0)),
        max_steps: Some(payload.max_steps.unwrap_or(100)),
    }
}

#[tauri::command]
fn show_solo_overlay(app: AppHandle, payload: OverlayPayload) -> Result<Value, String> {
    let payload = normalize_overlay_payload(payload);
    println!(
        "[SOLO/RUST] show_solo_overlay state={}",
        payload.state.clone().unwrap_or_default()
    );

    if let Some(window) = app.get_webview_window(SOLO_OVERLAY_LABEL) {
        position_overlay(&window);
        let _ = window.show();
        let _ = window.set_focus();
        let serialized = serde_json::to_string(&payload).map_err(|err| err.to_string())?;
        let _ = window.eval(&format!("window.__SOLO_OVERLAY__={serialized};"));
        let _ = window.emit("solo://overlay_state", payload);
        return Ok(json!({"ok": true}));
    }

    let serialized = serde_json::to_string(&payload).map_err(|err| err.to_string())?;
    let init_script = format!(
        "window.__OPEN_EAGLE_SOLO_OVERLAY__=true;window.__SOLO_OVERLAY__={serialized};"
    );

    let window = WebviewWindowBuilder::new(
        &app,
        SOLO_OVERLAY_LABEL,
        WebviewUrl::App("solo-overlay.html".into()),
    )
    .title("SOLO Overlay")
    .always_on_top(true)
    .decorations(false)
    .skip_taskbar(true)
    .resizable(false)
    .focused(false)
    .inner_size(SOLO_OVERLAY_WIDTH, SOLO_OVERLAY_HEIGHT)
    .initialization_script(&init_script)
    .build()
    .map_err(|err| format!("failed to create overlay window: {err}"))?;

    position_overlay(&window);
    let _ = window.set_ignore_cursor_events(true);

    Ok(json!({"ok": true}))
}

#[tauri::command]
fn update_solo_overlay(app: AppHandle, payload: OverlayPayload) -> Result<Value, String> {
    let payload = normalize_overlay_payload(payload);
    if let Some(window) = app.get_webview_window(SOLO_OVERLAY_LABEL) {
        println!(
            "[SOLO/RUST] update_solo_overlay state={}",
            payload.state.clone().unwrap_or_default()
        );
        position_overlay(&window);
        let _ = window.emit("solo://overlay_state", payload.clone());
        let _ = window.set_ignore_cursor_events(true);

        if matches!(
            payload.state.as_deref(),
            Some("completed" | "aborted" | "error")
        ) {
            let app_clone = app.clone();
            let window_label = SOLO_OVERLAY_LABEL.to_string();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(Duration::from_millis(SOLO_OVERLAY_AUTO_HIDE_MS)).await;
                if let Some(w) = app_clone.get_webview_window(&window_label) {
                    let _ = w.hide();
                    let _ = w.close();
                }
            });
        }
    }
    Ok(json!({"ok": true}))
}

#[tauri::command]
fn hide_solo_overlay(app: AppHandle) -> Result<Value, String> {
    println!("[SOLO/RUST] hide_solo_overlay");
    if let Some(window) = app.get_webview_window(SOLO_OVERLAY_LABEL) {
        let _ = window.hide();
        let _ = window.close();
    }
    Ok(json!({"ok": true}))
}

fn solo_notification_title(state: &str) -> &str {
    match state {
        "completed" => "SOLO 已完成",
        "aborted" => "SOLO 已结束",
        "error" => "SOLO 执行失败",
        _ => "SOLO 状态更新",
    }
}

fn sanitize_notification_body(detail: Option<String>) -> String {
    let raw = detail
        .map(|value| value.replace('\r', " ").replace('\n', " "))
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "返回 openEagle 查看执行结果。".to_string());
    let path_pattern = Regex::new(r"([A-Za-z]:\\[^\s]+|/[^\s]+)").ok();
    let sanitized = path_pattern
        .map(|pattern| pattern.replace_all(&raw, "[路径]").to_string())
        .unwrap_or(raw);
    let trimmed = sanitized.trim();
    if trimmed.chars().count() <= 180 {
        return trimmed.to_string();
    }
    let mut body = trimmed.chars().take(180).collect::<String>();
    body.push('…');
    body
}

#[tauri::command]
fn notify_solo_result(
    app: AppHandle,
    payload: SoloResultNotificationPayload,
) -> Result<Value, String> {
    let title = solo_notification_title(&payload.state);
    let body = sanitize_notification_body(payload.detail);
    println!(
        "[SOLO/RUST] notify_solo_result state={} body={}",
        payload.state, body
    );
    app.notification()
        .builder()
        .title(title)
        .body(&body)
        .show()
        .map_err(|err| format!("failed to show notification: {err}"))?;

    Ok(json!({
        "ok": true,
        "notified": true,
        "requestId": payload.request_id,
    }))
}

fn set_state(app: &AppHandle, runtime: &Arc<BackendRuntime>, next: BackendStatePayload) {
    {
        let mut state = runtime.state.lock().unwrap();
        *state = next.clone();
    }

    let _ = app.emit(BACKEND_EVENT, next);
}

fn project_root() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .expect("src-tauri should live under the repo root")
        .to_path_buf()
}

fn backend_root() -> PathBuf {
    project_root().join("backend")
}

fn backend_python() -> PathBuf {
    backend_root()
        .join(".venv")
        .join("Scripts")
        .join("python.exe")
}

fn spawn_backend(app: AppHandle) {
    let runtime = app.state::<Arc<BackendRuntime>>().inner().clone();
    set_state(
        &app,
        &runtime,
        BackendStatePayload::starting("Starting Python backend"),
    );

    tauri::async_runtime::spawn(async move {
        let command = if cfg!(debug_assertions) {
            let python = backend_python();
            if python.exists() {
                app.shell()
                    .command(python.to_string_lossy().to_string())
                    .current_dir(backend_root())
                    .args(["-m", "app.main", "--host", "127.0.0.1", "--port", "0"])
                    .env("PYTHONUTF8", "1")
                    .env("PYTHONUNBUFFERED", "1")
            } else {
                app.shell()
                    .command("uv")
                    .current_dir(backend_root())
                    .args([
                        "run",
                        "python",
                        "-m",
                        "app.main",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "0",
                    ])
                    .env("PYTHONUTF8", "1")
                    .env("PYTHONUNBUFFERED", "1")
            }
        } else {
            match app.shell().sidecar(SIDECAR_NAME) {
                Ok(command) => command.args(["--host", "127.0.0.1", "--port", "0"]),
                Err(error) => {
                    set_state(
                        &app,
                        &runtime,
                        BackendStatePayload::error(format!(
                            "Failed to create sidecar command: {error}"
                        )),
                    );
                    return;
                }
            }
        };

        let (mut receiver, child) = match command.spawn() {
            Ok(result) => result,
            Err(error) => {
                set_state(
                    &app,
                    &runtime,
                    BackendStatePayload::error(format!("Backend failed to start: {error}")),
                );
                return;
            }
        };

        {
            let mut child_slot = runtime.child.lock().unwrap();
            *child_slot = Some(child);
        }

        let ready_regex = Regex::new(READY_PATTERN).expect("valid handshake regex");
        let timeout_app = app.clone();
        let timeout_runtime = runtime.clone();

        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(Duration::from_secs(12)).await;
            let state = timeout_runtime.state.lock().unwrap().clone();
            if state.phase == "starting" && state.port.is_none() {
                set_state(
                    &timeout_app,
                    &timeout_runtime,
                    BackendStatePayload::error(
                        "Backend handshake timed out before a port was reported",
                    ),
                );
            }
        });

        while let Some(event) = receiver.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let text = String::from_utf8_lossy(&line).trim().to_string();
                    if let Some(captures) = ready_regex.captures(&text) {
                        let port = captures
                            .get(1)
                            .and_then(|value| value.as_str().parse::<u16>().ok());

                        if let Some(port) = port {
                            set_state(&app, &runtime, BackendStatePayload::ready(port));
                        }
                    }
                }
                CommandEvent::Stderr(line) => {
                    let text = String::from_utf8_lossy(&line).trim().to_string();
                    if !text.is_empty() {
                        let current = runtime.state.lock().unwrap().clone();
                        if current.phase != "ready" {
                            set_state(
                                &app,
                                &runtime,
                                BackendStatePayload::starting(format!(
                                    "Backend boot output: {text}"
                                )),
                            );
                        }
                    }
                }
                CommandEvent::Terminated(payload) => {
                    {
                        let mut child_slot = runtime.child.lock().unwrap();
                        *child_slot = None;
                    }

                    let message = if payload.code == Some(0) {
                        "Backend process exited".to_string()
                    } else {
                        format!("Backend process exited unexpectedly: {:?}", payload.code)
                    };
                    set_state(&app, &runtime, BackendStatePayload::disconnected(message));
                }
                _ => {}
            }
        }
    });
}

fn kill_backend(app: &AppHandle) {
    let runtime = app.state::<Arc<BackendRuntime>>().inner().clone();
    let maybe_child = {
        let mut child_slot = runtime.child.lock().unwrap();
        child_slot.take()
    };

    if let Some(child) = maybe_child {
        let _ = child.kill();
    }
}

fn main() {
    // Set AppUserModelId for Windows notification toasts (required by WinRT).
    #[cfg(target_os = "windows")]
    unsafe {
        use std::ffi::CString;
        let id = CString::new("com.openeagle.desktop").unwrap();
        extern "system" {
            fn SetCurrentProcessExplicitAppUserModelID(app_id: *const i8) -> i32;
        }
        let _ = SetCurrentProcessExplicitAppUserModelID(id.as_ptr());
    }

    let runtime = Arc::new(BackendRuntime::default());

    let app = tauri::Builder::default()
        .manage(runtime)
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            get_backend_state,
            load_conversation_index,
            load_conversation_file,
            save_conversation_file,
            save_conversation_index,
            delete_conversation_file,
            capture_screenshot,
            read_image_data_url,
            perform_mouse_action,
            perform_keyboard_action,
            show_solo_overlay,
            update_solo_overlay,
            hide_solo_overlay,
            notify_solo_result
        ])
        .setup(|app| {
            spawn_backend(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build tauri application");

    app.run(|app, event| {
        if matches!(event, RunEvent::Exit) {
            let _ = hide_solo_overlay(app.clone());
            kill_backend(app);
        }
    });
}
