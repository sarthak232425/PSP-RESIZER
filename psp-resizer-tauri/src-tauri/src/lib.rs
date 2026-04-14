use serde::Serialize;
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
};

use tauri::{AppHandle, Emitter, Manager, State, Window};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Clone, Default)]
struct AppState(Arc<InnerState>);

#[derive(Default)]
struct InnerState {
    cancel_current: AtomicBool,
    child: Mutex<Option<Child>>,
    removed_paths: Mutex<HashSet<String>>,
}

#[derive(Clone, Copy)]
struct Preset {
    prefix: &'static str,
    width: u32,
    height: u32,
    profile: &'static str,
    level: &'static str,
    crf: u8,
    maxrate: &'static str,
    bufsize: &'static str,
    audio_bitrate: &'static str,
}

fn preset_by_name(name: &str) -> Preset {
    match name {
        "PS3" => Preset {
            prefix: "PS3_",
            width: 1280,
            height: 720,
            profile: "high",
            level: "4.1",
            crf: 23,
            maxrate: "3500k",
            bufsize: "7000k",
            audio_bitrate: "160k",
        },
        "PS Vita" => Preset {
            prefix: "VITA_",
            width: 960,
            height: 544,
            profile: "main",
            level: "4.0",
            crf: 24,
            maxrate: "1500k",
            bufsize: "3000k",
            audio_bitrate: "128k",
        },
        _ => Preset {
            prefix: "PSP_",
            width: 480,
            height: 272,
            profile: "baseline",
            level: "3.0",
            crf: 26,
            maxrate: "600k",
            bufsize: "1200k",
            audio_bitrate: "96k",
        },
    }
}

#[derive(Clone, Serialize)]
struct ProgressPayload {
    input_path: String,
    percent: f64,
}

#[derive(Clone, Serialize)]
struct FileDonePayload {
    input_path: String,
    output_path: Option<String>,
    status: String,
}

fn emit_log(window: &Window, message: impl Into<String>) {
    let _ = window.emit("log", message.into());
}

fn resolve_ffmpeg(app: &AppHandle) -> Option<PathBuf> {
    // 1) Bundled resource (release builds)
    if let Ok(resource_dir) = app.path().resource_dir() {
        let p = resource_dir.join("ffmpeg.exe");
        if p.exists() {
            return Some(p);
        }
    }

    // 2) Dev fallback: repo root ffmpeg.exe (two levels up from src-tauri)
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // Try to locate ffmpeg.exe by walking up a few levels.
            let mut cur: Option<&Path> = Some(dir);
            for _ in 0..6 {
                if let Some(c) = cur {
                    let candidate = c.join("ffmpeg.exe");
                    if candidate.exists() {
                        return Some(candidate);
                    }
                    cur = c.parent();
                }
            }
        }
    }

    // 3) PATH
    Some(PathBuf::from("ffmpeg.exe"))
}

fn duration_seconds(ffmpeg: &Path, input_path: &Path) -> Option<f64> {
    let mut cmd = Command::new(ffmpeg);
    cmd.args(["-hide_banner", "-i"])
        .arg(input_path)
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    #[cfg(windows)]
    {
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let output = cmd.output().ok()?;

    let text = String::from_utf8_lossy(&output.stderr);
    for line in text.lines() {
        if let Some(idx) = line.find("Duration:") {
            let rest = line[idx + "Duration:".len()..].trim();
            let timecode = rest.split(',').next()?.trim();
            let parts: Vec<&str> = timecode.split(':').collect();
            if parts.len() != 3 {
                continue;
            }
            let h: f64 = parts[0].trim().parse().ok()?;
            let m: f64 = parts[1].trim().parse().ok()?;
            let s: f64 = parts[2].trim().parse().ok()?;
            return Some(h * 3600.0 + m * 60.0 + s);
        }
    }
    None
}

fn sanitize_filename(s: &str) -> String {
    // Keep it simple and filesystem-safe.
    s.chars()
        .map(|c| match c {
            '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*' => '_',
            _ => c,
        })
        .collect()
}

fn run_one(
    window: &Window,
    state: &InnerState,
    ffmpeg: &Path,
    input: &Path,
    output: &Path,
    preset: Preset,
) -> (bool, bool) {
    // returns (ok, canceled)
    state.cancel_current.store(false, Ordering::SeqCst);

    let duration = duration_seconds(ffmpeg, input);
    if let Some(d) = duration {
        emit_log(window, format!("    Duration: {:.1}s", d));
    }

    let filter = format!(
        "scale={}:{}:force_original_aspect_ratio=decrease,pad={}:{}:(ow-iw)/2:(oh-ih)/2",
        preset.width, preset.height, preset.width, preset.height
    );

    let mut cmd = Command::new(ffmpeg);

    #[cfg(windows)]
    {
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    cmd.args([
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-i",
    ])
    .arg(input)
    .args(["-vf", &filter])
    .args(["-c:v", "libx264"]) 
    .args(["-profile:v", preset.profile])
    .args(["-level", preset.level])
    .args(["-pix_fmt", "yuv420p"])
    .args(["-preset", "veryfast"])
    .args(["-crf", &preset.crf.to_string()])
    .args(["-maxrate", preset.maxrate])
    .args(["-bufsize", preset.bufsize])
    .args(["-c:a", "aac"])
    .args(["-b:a", preset.audio_bitrate])
    .args(["-ar", "44100"])
    .args(["-ac", "2"])
    .args(["-movflags", "+faststart"])
    .args(["-progress", "pipe:1"])
    .arg(output)
    .stdout(Stdio::piped())
    .stderr(Stdio::piped());

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            emit_log(window, format!("[ERROR] Failed to start FFmpeg: {e}"));
            return (false, false);
        }
    };

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    {
        let mut guard = state.child.lock().unwrap();
        *guard = Some(child);
    }

    // Drain stderr tail in another thread (for error context)
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    if let Some(err) = stderr {
        thread::spawn(move || {
            use std::io::{BufRead, BufReader};
            let reader = BufReader::new(err);
            let mut tail: Vec<String> = Vec::new();
            for line in reader.lines().flatten() {
                if !line.trim().is_empty() {
                    tail.push(line);
                    if tail.len() > 20 {
                        tail.remove(0);
                    }
                }
            }
            let _ = tx.send(tail.join("\n"));
        });
    }

    // Read progress
    if let Some(out) = stdout {
        use std::io::{BufRead, BufReader};
        let reader = BufReader::new(out);
        for line in reader.lines().flatten() {
            if state.cancel_current.load(Ordering::SeqCst) {
                if let Ok(mut guard) = state.child.lock() {
                    if let Some(child) = guard.as_mut() {
                        let _ = child.kill();
                    }
                }
                break;
            }
            let line = line.trim().to_string();
            if line.starts_with("out_time_ms=") {
                if let (Some(d), Ok(ms)) = (duration, line["out_time_ms=".len()..].parse::<f64>()) {
                    let sec = ms / 1_000_000.0;
                    let pct = (sec / d) * 100.0;
                    let payload = ProgressPayload {
                        input_path: input.to_string_lossy().to_string(),
                        percent: pct.max(0.0).min(100.0),
                    };
                    let _ = window.emit("progress", payload);
                }
            } else if line == "progress=end" {
                let payload = ProgressPayload {
                    input_path: input.to_string_lossy().to_string(),
                    percent: 100.0,
                };
                let _ = window.emit("progress", payload);
            }
        }
    }

    let status = {
        let mut guard = state.child.lock().unwrap();
        let status = guard.as_mut().and_then(|c| c.wait().ok());
        *guard = None;
        status
    };

    let canceled = state.cancel_current.load(Ordering::SeqCst);
    if canceled {
        return (false, true);
    }

    if status.as_ref().is_some_and(|s| s.success()) {
        (true, false)
    } else {
            if let Ok(tail) = rx.try_recv() {
                if !tail.trim().is_empty() {
                    emit_log(window, "[ERROR] FFmpeg error output (tail):");
                    for l in tail.lines().rev().take(12).collect::<Vec<_>>().into_iter().rev() {
                        emit_log(window, l);
                    }
                }
            }
            (false, false)
    }
}

#[tauri::command]
fn cancel_current(state: State<'_, AppState>) {
    state.0.cancel_current.store(true, Ordering::SeqCst);
    if let Ok(mut guard) = state.0.child.lock() {
        if let Some(child) = guard.as_mut() {
            let _ = child.kill();
        }
    }
}

#[tauri::command]
fn remove_from_queue(state: State<'_, AppState>, path: String) {
    if let Ok(mut guard) = state.0.removed_paths.lock() {
        guard.insert(path);
    }
}

#[tauri::command]
fn start_batch(
    window: Window,
    app: AppHandle,
    state: State<'_, AppState>,
    files: Vec<String>,
    output_dir: String,
    preset: String,
) {
    let preset = preset_by_name(&preset);
    let state = state.inner().clone();

    thread::spawn(move || {
        emit_log(&window, format!("Target preset: {}x{} ({})", preset.width, preset.height, preset.prefix));

        if let Ok(mut guard) = state.0.removed_paths.lock() {
            guard.clear();
        }

        let out_dir = PathBuf::from(output_dir);
        if let Err(e) = fs::create_dir_all(&out_dir) {
            emit_log(&window, format!("[ERROR] Failed to create output folder: {e}"));
            let _ = window.emit("batch_done", ());
            return;
        }

        let ffmpeg = match resolve_ffmpeg(&app) {
            Some(p) => p,
            None => {
                emit_log(&window, "[ERROR] ffmpeg.exe not found.");
                let _ = window.emit("batch_done", ());
                return;
            }
        };

        for (i, input_str) in files.iter().enumerate() {
            let is_removed = state
                .0
                .removed_paths
                .lock()
                .map(|s| s.contains(input_str))
                .unwrap_or(false);
            if is_removed {
                let payload = FileDonePayload {
                    input_path: input_str.clone(),
                    output_path: None,
                    status: "canceled".to_string(),
                };
                let _ = window.emit("file_done", payload);
                continue;
            }

            let input = PathBuf::from(input_str);
            if !input.exists() {
                emit_log(&window, format!("[{}/{}] [SKIP] Missing: {}", i + 1, files.len(), input_str));
                let payload = FileDonePayload {
                    input_path: input_str.clone(),
                    output_path: None,
                    status: "failed".to_string(),
                };
                let _ = window.emit("file_done", payload);
                continue;
            }

            let file_name = input
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("output");
            let file_name = sanitize_filename(file_name);
            let output_path = out_dir.join(format!("{}{}.mp4", preset.prefix, file_name));

            let _ = window.emit("file_started", input_str.clone());
            emit_log(&window, format!("[{}/{}] Converting: {}", i + 1, files.len(), input.file_name().and_then(|s| s.to_str()).unwrap_or(input_str)));

            let (ok, canceled) = run_one(&window, &state.0, &ffmpeg, &input, &output_path, preset);
            let payload = FileDonePayload {
                input_path: input_str.clone(),
                output_path: Some(output_path.to_string_lossy().to_string()),
                status: if ok {
                    "success".to_string()
                } else if canceled {
                    "canceled".to_string()
                } else {
                    "failed".to_string()
                },
            };
            let _ = window.emit("file_done", payload);

            if canceled {
                emit_log(&window, "[INFO] Canceled current file.");
            }
        }

        emit_log(&window, "--- All Done! ---");
        let _ = window.emit("batch_done", ());
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
    .manage(AppState::default())
    .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
    .invoke_handler(tauri::generate_handler![start_batch, cancel_current, remove_from_queue])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
