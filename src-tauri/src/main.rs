#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs,
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
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

const BACKEND_EVENT: &str = "backend://status";
const READY_PATTERN: &str = r"\[AGENT_READY\]\s+WS_PORT:\s+(\d+)";
const SIDECAR_NAME: &str = "binaries/open-eagle-agent";
const SOLO_OVERLAY_LABEL: &str = "solo_overlay";

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

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct OverlayPayload {
    title: Option<String>,
    detail: Option<String>,
    step_text: Option<String>,
    history_text: Option<String>,
    state: Option<String>,
}

#[tauri::command]
fn get_backend_state(state: State<'_, Arc<BackendRuntime>>) -> BackendStatePayload {
    state.state.lock().unwrap().clone()
}

#[tauri::command]
fn capture_screenshot() -> Result<ScreenshotResult, String> {
    println!("[SOLO/RUST] capture_screenshot",);
    let screens = Screen::all().map_err(|err| format!("failed to enumerate screens: {err}"))?;
    let screen = screens.first().ok_or_else(|| "no screen found".to_string())?;
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
    let target = PathBuf::from(path);
    if !target.exists() {
        return Err("image file does not exist".to_string());
    }
    if !target.is_file() {
        return Err("image path is not a file".to_string());
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

#[tauri::command]
fn show_solo_overlay(app: AppHandle, payload: OverlayPayload) -> Result<Value, String> {
    let title = payload
        .title
        .unwrap_or_else(|| "SOLO 正在执行桌面操作".to_string());
    let detail = payload
        .detail
        .unwrap_or_else(|| "请保持桌面可见，可随时暂停或结束".to_string());
    let step_text = payload
        .step_text
        .unwrap_or_else(|| "等待步骤更新".to_string());
    let history_text = payload.history_text.unwrap_or_default();
    let state = payload.state.unwrap_or_else(|| "running".to_string());
    println!("[SOLO/RUST] show_solo_overlay state={state}");

    if app.get_webview_window(SOLO_OVERLAY_LABEL).is_none() {
        let init_script = format!(
            r#"window.__OPEN_EAGLE_SOLO_OVERLAY__=true;window.__SOLO_OVERLAY__={{title:{},detail:{},step:{},history:{},state:{}}};"#,
            serde_json::to_string(&title).map_err(|err| err.to_string())?,
            serde_json::to_string(&detail).map_err(|err| err.to_string())?,
            serde_json::to_string(&step_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&history_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&state).map_err(|err| err.to_string())?,
        );
        let window = WebviewWindowBuilder::new(
            &app,
            SOLO_OVERLAY_LABEL,
            WebviewUrl::App("index.html?overlay=solo".into()),
        )
        .title("SOLO Overlay")
        .always_on_top(true)
        .decorations(false)
        .transparent(true)
        .skip_taskbar(true)
        .resizable(false)
        .focused(false)
        .inner_size(420.0, 280.0)
        .initialization_script(&init_script)
        .build()
        .map_err(|err| format!("failed to create overlay window: {err}"))?;
        let script = format!(
            r#"window.__SOLO_OVERLAY__={{title:{},detail:{},step:{},history:{},state:{}}};"#,
            serde_json::to_string(&title).map_err(|err| err.to_string())?,
            serde_json::to_string(&detail).map_err(|err| err.to_string())?,
            serde_json::to_string(&step_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&history_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&state).map_err(|err| err.to_string())?,
        );
        let _ = window.eval(&script);
        let _ = window.emit("solo://overlay_state", json!({"title": title, "detail": detail, "step": step_text, "history": history_text, "state": state}));
        return Ok(json!({"ok": true}));
    }

    if let Some(window) = app.get_webview_window(SOLO_OVERLAY_LABEL) {
        let script = format!(
            r#"window.__SOLO_OVERLAY__={{title:{},detail:{},step:{},history:{},state:{}}};"#,
            serde_json::to_string(&title).map_err(|err| err.to_string())?,
            serde_json::to_string(&detail).map_err(|err| err.to_string())?,
            serde_json::to_string(&step_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&history_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&state).map_err(|err| err.to_string())?,
        );
        let _ = window.eval(&script);
        let _ = window.show();
        let _ = window.emit("solo://overlay_state", json!({"title": title, "detail": detail, "step": step_text, "history": history_text, "state": state}));
    }
    Ok(json!({"ok": true}))
}

#[tauri::command]
fn update_solo_overlay(app: AppHandle, payload: OverlayPayload) -> Result<Value, String> {
    if let Some(window) = app.get_webview_window(SOLO_OVERLAY_LABEL) {
        println!(
            "[SOLO/RUST] update_solo_overlay state={}",
            payload.state.clone().unwrap_or_else(|| "unknown".to_string())
        );
        let script = format!(
            r#"window.__SOLO_OVERLAY__={{title:{},detail:{},step:{},history:{},state:{}}};"#,
            serde_json::to_string(&payload.title).map_err(|err| err.to_string())?,
            serde_json::to_string(&payload.detail).map_err(|err| err.to_string())?,
            serde_json::to_string(&payload.step_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&payload.history_text).map_err(|err| err.to_string())?,
            serde_json::to_string(&payload.state).map_err(|err| err.to_string())?,
        );
        let _ = window.eval(&script);
        let _ = window.emit(
            "solo://overlay_state",
            json!({
                "title": payload.title,
                "detail": payload.detail,
                "step": payload.step_text,
                "history": payload.history_text,
                "state": payload.state,
            }),
        );
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
    backend_root().join(".venv").join("Scripts").join("python.exe")
}

fn spawn_backend(app: AppHandle) {
    let runtime = app.state::<Arc<BackendRuntime>>().inner().clone();
    set_state(&app, &runtime, BackendStatePayload::starting("Starting Python backend"));

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
                        BackendStatePayload::error(format!("Failed to create sidecar command: {error}")),
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
                    BackendStatePayload::error("Backend handshake timed out before a port was reported"),
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
                                BackendStatePayload::starting(format!("Backend boot output: {text}")),
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
    let runtime = Arc::new(BackendRuntime::default());

    let app = tauri::Builder::default()
        .manage(runtime)
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            get_backend_state,
            capture_screenshot,
            read_image_data_url,
            perform_mouse_action,
            perform_keyboard_action,
            show_solo_overlay,
            update_solo_overlay,
            hide_solo_overlay
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
