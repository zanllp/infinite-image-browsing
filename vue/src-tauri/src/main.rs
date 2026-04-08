// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Deserialize;
use std::fs::File;
use std::fs::OpenOptions;
use std::io::prelude::*;
use std::io::Error;
use std::io::Write;
use std::process::Stdio;
use std::time::Duration;
use tauri::api::process::Command;
use tauri::api::process::CommandEvent;
use tauri::WindowEvent;
use chrono::Local;
use chrono::format::{DelayedFormat, StrftimeItems};

// Learn more about Tauri commands at https://tauri.app/v1/guides/features/command
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}
struct AppState {
    port: u16,
    child_pid: u32,
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

fn shutdown_api_server(port: u16, child_pid: u32) {
    let url = format!("http://127.0.0.1:{}/infinite_image_browsing/shutdown", port);
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(3))
        .build();
    if let Ok(client) = client {
        let res = client.post(&url).send();
        if let Err(e) = res {
            eprintln!("HTTP shutdown request failed: {}", e);
        }
    }
    // Fallback: force kill the process by PID to prevent orphaned processes
    kill_process_by_pid(child_pid);
}

fn kill_process_by_pid(pid: u32) {
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

#[tauri::command]
fn shutdown_api_server_command(state: tauri::State<'_, AppState>) {
    shutdown_api_server(state.port, state.child_pid);
}

fn main() {
    let listener = std::net::TcpListener::bind("localhost:0").expect("无法绑定到任何可用端口");
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let port_str = port.to_string();
    let mut args = vec!["--port", &port_str, "--allow_cors", "--enable_shutdown"];
    let contents = read_config_file("app.conf.json").unwrap_or_default();
    let conf = serde_json::from_str::<Config>(&contents).unwrap_or_default();
    if !conf.sdwebui_dir.is_empty() {
        args.push("--sd_webui_dir");
        args.push(&conf.sdwebui_dir);
    }
    let (mut rx, child) = Command::new_sidecar("iib_api_server")
        .expect("failed to create `iib_api_server` binary command")
        .args(args)
        .spawn()
        .expect("Failed to spawn sidecar");
    let child_pid = child.pid();
    // child handle is intentionally dropped here; we use the PID to kill the process on shutdown
    drop(child);
    let log_file = OpenOptions::new()
        .create(true)
        .write(true)
        .append(true)
        .open("iib_api_server.log")
        .expect("Failed to open log file");
    tauri::async_runtime::spawn(async move {
        // read events such as stdout
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let timestamp: DelayedFormat<StrftimeItems<'_>> =
                        Local::now().format("[%Y-%m-%d %H:%M:%S]");
                    let log_line = format!("INFO {} {}", timestamp, line);
                    println!("{}", log_line);
                    writeln!(&log_file, "{}", log_line).expect("Failed to write to log file");
                },
                CommandEvent::Stderr(line) => {
                    let timestamp: DelayedFormat<StrftimeItems<'_>> =
                        Local::now().format("[%Y-%m-%d %H:%M:%S]");
                    let log_line = format!("ERR {} {}", timestamp, line);
                    println!("{}", log_line);
                    writeln!(&log_file, "{}", log_line).expect("Failed to write to log file");
                }
                _ => (),
            };
        }
    });
    tauri::Builder::default()
        .manage(AppState { port, child_pid })
        .invoke_handler(tauri::generate_handler![
            greet,
            get_tauri_conf,
            shutdown_api_server_command
        ])
        .on_window_event(move |event| match event.event() {
            WindowEvent::CloseRequested { .. } => shutdown_api_server(port, child_pid),
            _ => (),
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
