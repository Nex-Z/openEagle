#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};

use regex::Regex;
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, RunEvent, State};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

const BACKEND_EVENT: &str = "backend://status";
const READY_PATTERN: &str = r"\[AGENT_READY\]\s+WS_PORT:\s+(\d+)";
const SIDECAR_NAME: &str = "binaries/open-eagle-agent";

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

#[tauri::command]
fn get_backend_state(state: State<'_, Arc<BackendRuntime>>) -> BackendStatePayload {
    state.state.lock().unwrap().clone()
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
        .invoke_handler(tauri::generate_handler![get_backend_state])
        .setup(|app| {
            spawn_backend(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build tauri application");

    app.run(|app, event| {
        if matches!(event, RunEvent::Exit) {
            kill_backend(app);
        }
    });
}
