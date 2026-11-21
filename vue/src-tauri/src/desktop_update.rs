// Only release builds with a configured signing key enable these commands.
use super::{request_shutdown, wait_for_exit, AppState};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::updater::UpdateResponse;
use tauri::{AppHandle, Manager, Wry};

#[derive(Default)]
pub struct PendingUpdate {
    response: Mutex<Option<UpdateResponse<Wry>>>,
    pub installing: AtomicBool,
}

#[tauri::command]
pub async fn check_desktop_update(app: AppHandle) -> Result<Option<String>, String> {
    let update = app
        .updater()
        .skip_events()
        .timeout(Duration::from_secs(15 * 60))
        .check()
        .await
        .map_err(|e| e.to_string())?;
    if !update.is_update_available() {
        return Ok(None);
    }
    let version = update.latest_version().to_owned();
    *app.state::<PendingUpdate>().response.lock().unwrap() = Some(update);
    Ok(Some(version))
}

#[tauri::command]
pub async fn install_desktop_update(app: AppHandle) -> Result<(), String> {
    let pending = app.state::<PendingUpdate>();
    let update = pending
        .response
        .lock()
        .unwrap()
        .take()
        .ok_or_else(|| "No pending desktop update".to_string())?;
    pending.installing.store(true, Ordering::SeqCst);
    let handle = app.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let state = handle.state::<AppState>();
        // Hold off respawns until NSIS takes over / macOS relaunches. On an error,
        // dropping the guard releases the supervisor to start the old sidecar again.
        let mut restart = state.restart_gate.lock().unwrap();
        *restart = true;
        if !state.exited.load(Ordering::SeqCst) {
            request_shutdown(state.port);
            if !wait_for_exit(&state.exited, Duration::from_secs(30)) {
                return Err("Local service did not finish shutting down".to_string());
            }
        }
        tauri::async_runtime::block_on(update.download_and_install()).map_err(|e| e.to_string())?;
        // The Windows updater has already exited and handed relaunch to NSIS.
        handle.restart();
        Ok(())
    })
    .await;
    pending.installing.store(false, Ordering::SeqCst);
    result.map_err(|e| e.to_string())?
}
