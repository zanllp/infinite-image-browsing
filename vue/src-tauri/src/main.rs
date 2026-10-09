// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Deserialize;
use std::fs::File;
use std::fs::OpenOptions;
use std::io::prelude::*;
use std::io::Error;
use std::io::Write;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tauri::api::process::Command;
use tauri::api::process::CommandEvent;
use tauri::WindowEvent;
use chrono::Local;
use chrono::format::{DelayedFormat, StrftimeItems};

#[cfg(target_os = "windows")]
use winapi::shared::ntdef::HANDLE;
#[cfg(target_os = "windows")]
use winapi::um::handleapi::CloseHandle;
#[cfg(target_os = "windows")]
use winapi::um::jobapi2::{AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject};
#[cfg(target_os = "windows")]
use winapi::um::processthreadsapi::OpenProcess;
#[cfg(target_os = "windows")]
use winapi::um::winnt::{
    JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
};

/// 最多自动重启 sidecar 的次数
const MAX_SIDECAR_RESTARTS: u32 = 3;
/// 关闭时等待 sidecar 优雅退出的时间
const GRACEFUL_EXIT_WAIT: Duration = Duration::from_secs(3);

// Learn more about Tauri commands at https://tauri.app/v1/guides/features/command
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}
struct AppState {
    port: u16,
}
#[derive(serde::Serialize)]
struct AppConf {
    port: u16,
}
#[tauri::command]
fn get_tauri_conf(state: tauri::State<'_, AppState>) -> AppConf {
    AppConf { port: state.port }
}

#[derive(Deserialize, Default, Debug)]
struct Config {
    sdwebui_dir: String,
}

fn read_config_file(path: &str) -> Result<String, Error> {
    let mut file = File::open(path)?;
    let mut contents = String::new();
    file.read_to_string(&mut contents)?;
    Ok(contents)
}

/// 统一写日志：带毫秒时间戳，同时打到 stdout 和 iib_api_server.log
fn log_line(log_file: &File, level: &str, message: &str) {
    let timestamp: DelayedFormat<StrftimeItems<'_>> =
        Local::now().format("[%Y-%m-%d %H:%M:%S%.3f]");
    let line = format!("{} {} {}", level, timestamp, message);
    println!("{}", line);
    let mut file: &File = log_file;
    let _ = writeln!(file, "{}", line);
}

/// 请求 sidecar 优雅退出（它要收尾数据库、写日志、清理解压目录）
fn request_shutdown(port: u16) {
    let url = format!("http://127.0.0.1:{}/infinite_image_browsing/shutdown", port);
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(3))
        .build();
    if let Ok(client) = client {
        if let Err(e) = client.post(&url).send() {
            eprintln!("HTTP shutdown request failed: {}", e);
        }
    }
}

/// 兜底：等不及了就强杀（正常情况下 Job Object / 优雅退出就够了）
fn kill_process_by_pid(pid: u32) {
    if pid == 0 {
        return;
    }
    #[cfg(target_os = "windows")]
    {
        let _ = std::process::Command::new("taskkill")
            .args(&["/F", "/T", "/PID", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
    }

    #[cfg(not(target_os = "windows"))]
    {
        let _ = std::process::Command::new("kill")
            .args(&["-9", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
    }
}

/// 先优雅退出，最多等 GRACEFUL_EXIT_WAIT；没退再强杀
fn shutdown_sidecar(port: u16, child_pid: u32, exited: &AtomicBool, log_file: &File) {
    request_shutdown(port);
    let deadline = Instant::now() + GRACEFUL_EXIT_WAIT;
    while !exited.load(Ordering::SeqCst) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(100));
    }
    if exited.load(Ordering::SeqCst) {
        log_line(log_file, "INFO", "[TIMELINE] event=sidecar_exited_gracefully");
    } else {
        log_line(log_file, "INFO", "[TIMELINE] event=sidecar_force_killed");
        kill_process_by_pid(child_pid);
    }
}

#[cfg(target_os = "windows")]
#[derive(Clone, Copy)]
struct JobHandle(HANDLE);
// Job Object 句柄：裸指针本身不是 Send，但句柄在整个进程里跨线程使用是安全的
#[cfg(target_os = "windows")]
unsafe impl Send for JobHandle {}
#[cfg(target_os = "windows")]
unsafe impl Sync for JobHandle {}

#[cfg(not(target_os = "windows"))]
#[derive(Clone, Copy)]
struct JobHandle;

/// 建一个 "最后一个句柄关闭时杀掉所有成员" 的 Job Object。
/// 本进程正常退出、崩溃、被任务管理器强杀，Windows 都会把 sidecar 一起干掉，杜绝孤儿。
#[cfg(target_os = "windows")]
fn create_kill_on_close_job() -> Option<JobHandle> {
    unsafe {
        let job = CreateJobObjectW(std::ptr::null_mut(), std::ptr::null());
        if job.is_null() {
            return None;
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &mut info as *mut _ as *mut _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if ok == 0 {
            CloseHandle(job);
            return None;
        }
        Some(JobHandle(job))
    }
}

#[cfg(not(target_os = "windows"))]
fn create_kill_on_close_job() -> Option<JobHandle> {
    None
}

#[cfg(target_os = "windows")]
fn assign_process_to_job(job: JobHandle, pid: u32) -> bool {
    unsafe {
        let handle = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
        if handle.is_null() {
            return false;
        }
        let ok = AssignProcessToJobObject(job.0, handle);
        CloseHandle(handle);
        ok != 0
    }
}

#[cfg(not(target_os = "windows"))]
fn assign_process_to_job(_job: JobHandle, _pid: u32) -> bool {
    false
}

fn sidecar_args(port: u16) -> Vec<String> {
    let mut args = vec![
        "--port".to_string(),
        port.to_string(),
        "--allow_cors".to_string(),
        "--enable_shutdown".to_string(),
        // 让 sidecar 盯着我们：本进程一消失就自己退出（Job Object 之外的第二道保险）
        "--parent_pid".to_string(),
        std::process::id().to_string(),
    ];
    let contents = read_config_file("app.conf.json").unwrap_or_default();
    let conf = serde_json::from_str::<Config>(&contents).unwrap_or_default();
    if !conf.sdwebui_dir.is_empty() {
        args.push("--sd_webui_dir".to_string());
        args.push(conf.sdwebui_dir);
    }
    args
}

#[tauri::command]
fn shutdown_api_server_command(state: tauri::State<'_, AppState>) {
    request_shutdown(state.port);
}

fn main() {
    let log_file = Arc::new(
        OpenOptions::new()
            .create(true)
            .write(true)
            .append(true)
            .open("iib_api_server.log")
            .expect("Failed to open log file"),
    );
    // 启动时间线基准点：应用进程启动。后面 sidecar 的每一行都能对着它算相对耗时。
    log_line(&log_file, "INFO", "[TIMELINE] event=app_start");

    let job = create_kill_on_close_job();
    if job.is_some() {
        log_line(&log_file, "INFO", "[TIMELINE] event=job_object_created");
    } else {
        log_line(&log_file, "INFO", "[TIMELINE] event=job_object_unavailable");
    }

    let listener = std::net::TcpListener::bind("localhost:0").expect("无法绑定到任何可用端口");
    let port = listener.local_addr().unwrap().port();
    drop(listener);

    let (mut rx, child) = Command::new_sidecar("iib_api_server")
        .expect("failed to create `iib_api_server` binary command")
        .args(sidecar_args(port))
        .spawn()
        .expect("Failed to spawn sidecar");
    let child_pid = child.pid();
    // child handle is intentionally dropped here; we track the PID to stop the process on close
    drop(child);
    log_line(
        &log_file,
        "INFO",
        &format!("[TIMELINE] event=sidecar_spawned pid={}", child_pid),
    );
    if let Some(job) = job {
        if assign_process_to_job(job, child_pid) {
            log_line(&log_file, "INFO", "[TIMELINE] event=job_object_assigned");
        } else {
            log_line(&log_file, "INFO", "[TIMELINE] event=job_object_assign_failed");
        }
    }

    let child_pid_state = Arc::new(AtomicU32::new(child_pid));
    let exited = Arc::new(AtomicBool::new(false));
    let shutting_down = Arc::new(AtomicBool::new(false));

    {
        let log_file = log_file.clone();
        let child_pid_state = child_pid_state.clone();
        let exited = exited.clone();
        let shutting_down = shutting_down.clone();
        // 阻塞式 recv 跑在普通线程里：sidecar 意外退出时能按退避重启
        std::thread::spawn(move || {
            let mut attempts = 0u32;
            loop {
                while let Some(event) = rx.blocking_recv() {
                    match event {
                        CommandEvent::Stdout(line) => log_line(&log_file, "INFO", &line),
                        CommandEvent::Stderr(line) => log_line(&log_file, "ERR", &line),
                        CommandEvent::Terminated(payload) => {
                            exited.store(true, Ordering::SeqCst);
                            log_line(
                                &log_file,
                                "INFO",
                                &format!(
                                    "[TIMELINE] event=sidecar_terminated code={:?}",
                                    payload.code
                                ),
                            );
                        }
                        _ => (),
                    }
                }

                if shutting_down.load(Ordering::SeqCst) {
                    break;
                }
                if attempts >= MAX_SIDECAR_RESTARTS {
                    log_line(&log_file, "INFO", "[TIMELINE] event=sidecar_restart_giveup");
                    break;
                }
                attempts += 1;
                let wait = Duration::from_secs(1u64 << (attempts - 1).min(3));
                log_line(
                    &log_file,
                    "INFO",
                    &format!(
                        "[TIMELINE] event=sidecar_restarting attempt={} wait={}s",
                        attempts,
                        wait.as_secs()
                    ),
                );
                std::thread::sleep(wait);

                match Command::new_sidecar("iib_api_server") {
                    Ok(command) => match command.args(sidecar_args(port)).spawn() {
                        Ok((new_rx, new_child)) => {
                            let new_pid = new_child.pid();
                            drop(new_child);
                            child_pid_state.store(new_pid, Ordering::SeqCst);
                            exited.store(false, Ordering::SeqCst);
                            if let Some(job) = job {
                                assign_process_to_job(job, new_pid);
                            }
                            log_line(
                                &log_file,
                                "INFO",
                                &format!(
                                    "[TIMELINE] event=sidecar_restarted attempt={} pid={}",
                                    attempts, new_pid
                                ),
                            );
                            rx = new_rx;
                        }
                        Err(e) => {
                            log_line(
                                &log_file,
                                "ERR",
                                &format!("failed to restart sidecar: {}", e),
                            );
                            break;
                        }
                    },
                    Err(e) => {
                        log_line(
                            &log_file,
                            "ERR",
                            &format!("failed to create sidecar command: {}", e),
                        );
                        break;
                    }
                }
            }
        });
    }

    tauri::Builder::default()
        .manage(AppState { port })
        .invoke_handler(tauri::generate_handler![
            greet,
            get_tauri_conf,
            shutdown_api_server_command
        ])
        .on_window_event(move |event| match event.event() {
            WindowEvent::CloseRequested { .. } => {
                log_line(&log_file, "INFO", "[TIMELINE] event=window_close_requested");
                shutting_down.store(true, Ordering::SeqCst);
                shutdown_sidecar(
                    port,
                    child_pid_state.load(Ordering::SeqCst),
                    &exited,
                    &log_file,
                );
            }
            _ => (),
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
