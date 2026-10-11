use std::fs::{create_dir_all, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Manager, Runtime};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

struct BackendSidecar(Mutex<Option<CommandChild>>);

impl Drop for BackendSidecar {
    fn drop(&mut self) {
        if let Ok(mut child) = self.0.lock() {
            if let Some(child) = child.take() {
                append_backend_log("Stopping backend sidecar from state drop.");
                let _ = child.kill();
            }
        }
    }
}

fn backend_log_path() -> PathBuf {
    let base = std::env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir());
    base.join("SpotifyRoomLighting").join("backend-sidecar.log")
}

fn append_backend_log(message: &str) {
    let path = backend_log_path();
    if let Some(parent) = path.parent() {
        let _ = create_dir_all(parent);
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{message}");
    }
}

#[cfg(windows)]
fn stop_existing_backend_processes() {
    use std::os::windows::process::CommandExt;
    use std::time::{Duration, Instant};

    const CREATE_NO_WINDOW: u32 = 0x08000000;
    // taskkill talks to WMI, which can hang indefinitely on a broken machine.
    const TASKKILL_TIMEOUT: Duration = Duration::from_secs(3);

    for image_name in [
        "huephony-backend.exe",
        "huephony-backend-x86_64-pc-windows-msvc.exe",
        "huephoria-backend.exe",
        "huephoria-backend-x86_64-pc-windows-msvc.exe",
        "spotify-lights-backend.exe",
        "spotify-lights-backend-x86_64-pc-windows-msvc.exe",
    ] {
        let mut child = match std::process::Command::new("taskkill")
            .args(["/IM", image_name, "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                append_backend_log(&format!(
                    "Could not check existing backend process {image_name}: {error}"
                ));
                continue;
            }
        };

        let started = Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    if status.success() {
                        append_backend_log(&format!(
                            "Stopped existing backend process: {image_name}."
                        ));
                    }
                    break;
                }
                Ok(None) if started.elapsed() >= TASKKILL_TIMEOUT => {
                    let _ = child.kill();
                    append_backend_log(&format!(
                        "taskkill for {image_name} timed out; continuing without it."
                    ));
                    break;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(error) => {
                    append_backend_log(&format!(
                        "Could not check existing backend process {image_name}: {error}"
                    ));
                    break;
                }
            }
        }
    }
}

#[cfg(not(windows))]
fn stop_existing_backend_processes() {}

fn stop_backend_sidecar<R: Runtime, M: Manager<R>>(manager: &M, reason: &str) {
    let child = {
        let sidecar = manager.state::<BackendSidecar>();
        sidecar.0.lock().ok().and_then(|mut child| child.take())
    };
    if let Some(child) = child {
        append_backend_log(&format!("Stopping backend sidecar: {reason}."));
        let _ = child.kill();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            #[cfg(desktop)]
            {
                use tauri_plugin_shell::ShellExt;

                // Paint the window dark before the webview renders, so launch
                // shows the app's own background instead of a grey/white flash.
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.set_background_color(Some(tauri::window::Color(9, 7, 10, 255)));
                }

                // Start the backend off the main thread so a slow or hung
                // taskkill can never freeze the window.
                app.manage(BackendSidecar(Mutex::new(None)));
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    stop_existing_backend_processes();
                    append_backend_log("Starting backend sidecar.");
                    let spawned = handle
                        .shell()
                        .sidecar("huephony-backend")
                        .map(|sidecar| {
                            sidecar.env(
                                "SPOTIFY_LIGHTING_PARENT_PID",
                                std::process::id().to_string(),
                            )
                        })
                        .and_then(|sidecar| sidecar.spawn());
                    let (mut receiver, child): (_, CommandChild) = match spawned {
                        Ok(spawned) => spawned,
                        Err(error) => {
                            append_backend_log(&format!(
                                "Could not start backend sidecar: {error}"
                            ));
                            return;
                        }
                    };
                    if let Ok(mut slot) = handle.state::<BackendSidecar>().0.lock() {
                        *slot = Some(child);
                    }
                    tauri::async_runtime::spawn(async move {
                        while let Some(event) = receiver.recv().await {
                            match event {
                                CommandEvent::Stdout(bytes) => {
                                    append_backend_log(&format!(
                                        "stdout: {}",
                                        String::from_utf8_lossy(&bytes)
                                    ));
                                }
                                CommandEvent::Stderr(bytes) => {
                                    append_backend_log(&format!(
                                        "stderr: {}",
                                        String::from_utf8_lossy(&bytes)
                                    ));
                                }
                                CommandEvent::Terminated(payload) => {
                                    append_backend_log(&format!(
                                        "terminated: code={:?}, signal={:?}",
                                        payload.code, payload.signal
                                    ));
                                }
                                other => {
                                    append_backend_log(&format!("event: {other:?}"));
                                }
                            }
                        }
                    });
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                stop_backend_sidecar(window, "window close requested");
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Huephony");

    app.run(|app_handle, event| match event {
        tauri::RunEvent::ExitRequested { .. } => {
            stop_backend_sidecar(app_handle, "app exit requested");
        }
        tauri::RunEvent::Exit => {
            stop_backend_sidecar(app_handle, "app exiting");
        }
        _ => {}
    });
}
