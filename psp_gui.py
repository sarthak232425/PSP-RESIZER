import os
import sys
import subprocess
import threading
import queue
import shutil
import tkinter as tk
from tkinter import filedialog, scrolledtext
from tkinter import ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
except Exception:  # pragma: no cover
    DND_FILES = None  # type: ignore
    TkinterDnD = None  # type: ignore

# --- PSP Optimization Settings ---
PSP_WIDTH = 480
PSP_HEIGHT = 272
VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"

# Compression defaults chosen to avoid output size blow-ups.
DEFAULT_CRF = 26
DEFAULT_MAXRATE = "600k"
DEFAULT_BUFSIZE = "1200k"
DEFAULT_AUDIO_BITRATE = "96k"


def get_base_dir() -> str:
    """Returns a folder next to the app/script where input/output should live."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_ffmpeg_path() -> str:
    """Find FFmpeg whether running as script or bundled .exe."""
    if getattr(sys, "frozen", False):
        # When bundled by PyInstaller, we include ffmpeg.exe as a binary.
        bundled = os.path.join(sys._MEIPASS, "ffmpeg.exe")  # type: ignore[attr-defined]
        if os.path.exists(bundled):
            return bundled

    # Prefer a local ffmpeg.exe next to the script (common for portable setups)
    local = os.path.join(get_base_dir(), "ffmpeg.exe")
    if os.path.exists(local):
        return local

    # Fall back to PATH
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    return found or "ffmpeg.exe"


class PSPConverterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PSP Video Converter")
        self.root.geometry("900x650")
        self.root.minsize(780, 560)
        self.root.resizable(True, True)

        base_dir = get_base_dir()
        self.output_dir = tk.StringVar(value=os.path.join(base_dir, "output"))

        self._queued_files: list[str] = []
        self._queued_set: set[str] = set()
        self._queue_lock = threading.Lock()

        self._cancel_current = threading.Event()
        self._current_process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._progress_queue: "queue.Queue[float]" = queue.Queue()
        self._worker_thread: threading.Thread | None = None

        self._setup_ui()
        self._pump_ui_queues()

    def _setup_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Use a single root grid so the queue + log can expand.
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        frame_top = ttk.LabelFrame(self.root, text="Settings", padding=10)
        frame_top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        frame_top.columnconfigure(1, weight=1)

        ttk.Label(frame_top, text="Queue:").grid(row=0, column=0, sticky=tk.NW, pady=5)

        queue_frame = ttk.Frame(frame_top)
        queue_frame.grid(row=0, column=1, sticky="nsew", padx=8)
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(0, weight=1)

        self.queue_list = tk.Listbox(queue_frame, height=8, exportselection=False)
        self.queue_list.grid(row=0, column=0, sticky="nsew")
        queue_scroll = ttk.Scrollbar(queue_frame, orient=tk.VERTICAL, command=self.queue_list.yview)
        queue_scroll.grid(row=0, column=1, sticky="ns")
        self.queue_list.configure(yscrollcommand=queue_scroll.set)

        queue_buttons = ttk.Frame(frame_top)
        queue_buttons.grid(row=0, column=2, sticky="ne")
        self.add_files_btn = ttk.Button(queue_buttons, text="Add Files", command=self._browse_input)
        self.add_files_btn.pack(fill=tk.X)
        self.remove_selected_btn = ttk.Button(queue_buttons, text="Remove Selected", command=self._remove_selected)
        self.remove_selected_btn.pack(fill=tk.X, pady=(6, 0))
        self.clear_queue_btn = ttk.Button(queue_buttons, text="Clear Queue", command=self._clear_queue)
        self.clear_queue_btn.pack(fill=tk.X, pady=(6, 0))

        ttk.Label(frame_top, text="Output Folder:").grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
        ttk.Entry(frame_top, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", padx=8, pady=(10, 0))
        ttk.Button(frame_top, text="Browse", command=self._browse_output).grid(row=1, column=2, sticky="e", pady=(10, 0))

        frame_mid = ttk.Frame(self.root)
        frame_mid.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        frame_mid.columnconfigure(0, weight=1)

        buttons_row = ttk.Frame(frame_mid)
        buttons_row.grid(row=0, column=0, sticky="ew")
        buttons_row.columnconfigure(0, weight=1)

        self.convert_btn = ttk.Button(buttons_row, text="START", command=self._start_conversion)
        self.convert_btn.grid(row=0, column=0, sticky="ew", ipady=6)
        self.cancel_btn = ttk.Button(buttons_row, text="CANCEL CURRENT", command=self._cancel_current_file, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=(10, 0), sticky="e", ipady=6)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(frame_mid, mode="determinate", maximum=100.0, variable=self.progress_var)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.progress_text = ttk.Label(frame_mid, text="0%")
        self.progress_text.grid(row=2, column=0, sticky="w")

        frame_bot = ttk.LabelFrame(self.root, text="Status Log", padding=8)
        frame_bot.grid(row=2, column=0, sticky="nsew", padx=12, pady=(6, 12))
        frame_bot.columnconfigure(0, weight=1)
        frame_bot.rowconfigure(1, weight=1)

        self.log_area = scrolledtext.ScrolledText(frame_bot, state="disabled")
        self.log_area.grid(row=1, column=0, sticky="nsew")

        self.drop_hint = ttk.Label(
            frame_bot,
            text=(
                "Tip: Drag & drop videos into this window to queue them"
                if TkinterDnD is not None
                else "Tip: Use 'Add Files' to queue videos"
            ),
        )
        self.drop_hint.grid(row=0, column=0, sticky="w", pady=(0, 8))

        if TkinterDnD is not None and DND_FILES is not None:
            try:
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        self._log("Welcome to PSP Video Converter! Queue files, then start.")
        self._refresh_queue_listbox()

    def _browse_input(self) -> None:
        filenames = filedialog.askopenfilenames(
            title="Select video file(s)",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv"),
                ("MP4", "*.mp4"),
                ("All files", "*.*"),
            ],
        )
        if filenames:
            self._queue_files(list(filenames))

    def _on_drop(self, event: object) -> None:
        data = getattr(event, "data", None)
        if not data:
            return

        try:
            paths = list(self.root.tk.splitlist(data))
        except Exception:
            paths = [str(data)]

        expanded: list[str] = []
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for child in os.listdir(p):
                    expanded.append(os.path.join(p, child))
            else:
                expanded.append(p)

        self._queue_files(expanded)

    def _queue_files(self, paths: list[str]) -> None:
        valid_exts = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"}
        added = 0
        with self._queue_lock:
            for p in paths:
                if not p:
                    continue
                if not os.path.exists(p):
                    continue
                if os.path.isdir(p):
                    continue
                ext = os.path.splitext(p)[1].lower()
                if ext not in valid_exts:
                    continue
                if p in self._queued_set:
                    continue
                self._queued_set.add(p)
                self._queued_files.append(p)
                added += 1

        if added:
            self._log(f"Queued {added} file(s).")
        self.root.after(0, self._refresh_queue_listbox)

    def _refresh_queue_listbox(self) -> None:
        with self._queue_lock:
            items = list(self._queued_files)
        self.queue_list.delete(0, tk.END)
        for p in items:
            self.queue_list.insert(tk.END, os.path.basename(p))

    def _remove_selected(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._log("[INFO] Can't remove while converting. Cancel current or wait.")
            return

        selection = list(self.queue_list.curselection())
        if not selection:
            return

        with self._queue_lock:
            # Remove highest index first
            for idx in sorted(selection, reverse=True):
                if 0 <= idx < len(self._queued_files):
                    p = self._queued_files.pop(idx)
                    self._queued_set.discard(p)
        self._refresh_queue_listbox()

    def _clear_queue(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._log("[INFO] Can't clear while converting. Cancel current or wait.")
            return

        with self._queue_lock:
            self._queued_files.clear()
            self._queued_set.clear()
        self._refresh_queue_listbox()

    def _browse_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir.get())
        if folder:
            self.output_dir.set(folder)

    def _log(self, message: str) -> None:
        """Thread-safe: queues log messages for the UI thread."""
        self._log_queue.put(message)

    def _queue_progress(self, percent: float) -> None:
        try:
            percent = max(0.0, min(100.0, float(percent)))
        except Exception:
            return
        self._progress_queue.put(percent)

    def _pump_ui_queues(self) -> None:
        """Runs on the UI thread; flushes queued UI events."""
        try:
            while True:
                message = self._log_queue.get_nowait()
                self.log_area.configure(state="normal")
                self.log_area.insert(tk.END, message + "\n")
                self.log_area.see(tk.END)
                self.log_area.configure(state="disabled")
        except queue.Empty:
            pass

        try:
            last = None
            while True:
                last = self._progress_queue.get_nowait()
        except queue.Empty:
            if last is not None:
                self.progress_var.set(last)
                self.progress_text.configure(text=f"{int(last)}%")

        self.root.after(100, self._pump_ui_queues)

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.convert_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")
            self.add_files_btn.configure(state="disabled")
            self.remove_selected_btn.configure(state="disabled")
            self.clear_queue_btn.configure(state="disabled")
            self.progress_var.set(0.0)
            self.progress_text.configure(text="0%")
        else:
            self.progress_var.set(0.0)
            self.progress_text.configure(text="0%")
            self.convert_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self.add_files_btn.configure(state="normal")
            self.remove_selected_btn.configure(state="normal")
            self.clear_queue_btn.configure(state="normal")

    def _start_conversion(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return

        with self._queue_lock:
            has_files = bool(self._queued_files)

        if not has_files:
            self._log("[ERROR] No files queued. Use 'Add Files' or drag & drop.")
            return

        self._set_busy(True)
        self._cancel_current.clear()
        self._log("")
        self._log("--- Starting Batch Conversion ---")

        self._worker_thread = threading.Thread(target=self._run_conversion_process, daemon=True)
        self._worker_thread.start()

    def _cancel_current_file(self) -> None:
        self._cancel_current.set()
        with self._process_lock:
            p = self._current_process
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        self._log("[INFO] Cancel requested for current file...")

    def _run_conversion_process(self) -> None:
        try:
            out = self.output_dir.get()

            os.makedirs(out, exist_ok=True)

            ffmpeg_bin = get_ffmpeg_path()
            if not ffmpeg_bin or (os.path.isabs(ffmpeg_bin) and not os.path.exists(ffmpeg_bin)):
                self._log("[ERROR] ffmpeg.exe not found. Put it next to the app or add it to PATH.")
                return

            index = 0
            while True:
                with self._queue_lock:
                    if not self._queued_files:
                        break
                    inp_file = self._queued_files[0]

                index += 1
                with self._queue_lock:
                    remaining = len(self._queued_files)

                if not os.path.exists(inp_file):
                    self._log(f"[{index}/?] [SKIP] Missing: {inp_file}")
                    with self._queue_lock:
                        if self._queued_files and self._queued_files[0] == inp_file:
                            self._queued_files.pop(0)
                            self._queued_set.discard(inp_file)
                    self.root.after(0, self._refresh_queue_listbox)
                    continue

                filename = os.path.basename(inp_file)
                clean_name = os.path.splitext(filename)[0]
                output_path = os.path.join(out, f"PSP_{clean_name}.mp4")

                self._cancel_current.clear()
                self._log(f"[{index}/{index + remaining - 1}] Converting: {filename}")

                duration = _get_duration_seconds(ffmpeg_bin, inp_file)
                if duration and duration > 0:
                    self._log(f"    Duration: {duration:.1f}s")

                ok = _run_ffmpeg_with_progress(
                    ffmpeg_bin=ffmpeg_bin,
                    input_path=inp_file,
                    output_path=output_path,
                    duration_seconds=duration,
                    on_progress=self._queue_progress,
                    log=self._log,
                    cancel_event=self._cancel_current,
                    set_process=self._set_current_process,
                )

                if ok:
                    self._log("    -> Success!")
                    self._log(f"    Saved: {output_path}")
                else:
                    if self._cancel_current.is_set():
                        self._log("    -> Canceled.")
                    else:
                        self._log("    -> [ERROR] Conversion failed.")

                # Remove finished item from queue
                with self._queue_lock:
                    if self._queued_files and self._queued_files[0] == inp_file:
                        self._queued_files.pop(0)
                        self._queued_set.discard(inp_file)
                self.root.after(0, self._refresh_queue_listbox)

            with self._process_lock:
                self._current_process = None

            self._log("--- All Done! ---")
        finally:
            self.root.after(0, lambda: self._set_busy(False))

    def _set_current_process(self, p: subprocess.Popen[str] | None) -> None:
        with self._process_lock:
            self._current_process = p


def _get_duration_seconds(ffmpeg_bin: str, input_path: str) -> float | None:
    """Extract duration via `ffmpeg -i` output (no ffprobe dependency)."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", input_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )
        text = p.stderr or ""
        # Example: Duration: 00:01:23.45,
        for line in text.splitlines():
            if "Duration:" in line:
                part = line.split("Duration:", 1)[1].strip()
                timecode = part.split(",", 1)[0].strip()
                h, m, s = timecode.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None
    return None


def _run_ffmpeg_with_progress(
    *,
    ffmpeg_bin: str,
    input_path: str,
    output_path: str,
    duration_seconds: float | None,
    on_progress: "callable[[float], None]",
    log: "callable[[str], None]",
    cancel_event: threading.Event,
    set_process: "callable[[subprocess.Popen[str] | None], None]",
) -> bool:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-i",
        input_path,
        "-vf",
        (
            f"scale={PSP_WIDTH}:{PSP_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={PSP_WIDTH}:{PSP_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
        ),
        "-c:v",
        VIDEO_CODEC,
        "-profile:v",
        "baseline",
        "-level",
        "3.0",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        str(DEFAULT_CRF),
        "-maxrate",
        DEFAULT_MAXRATE,
        "-bufsize",
        DEFAULT_BUFSIZE,
        "-c:a",
        AUDIO_CODEC,
        "-b:a",
        DEFAULT_AUDIO_BITRATE,
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        output_path,
    ]

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )
        set_process(p)
    except Exception as e:
        log(f"[ERROR] Failed to start FFmpeg: {e}")
        return False

    last_reported = -1
    stderr_tail: list[str] = []
    try:
        assert p.stdout is not None
        assert p.stderr is not None

        # Drain stderr in a lightweight way (capture tail for errors)
        def _drain_stderr() -> None:
            for line in p.stderr:  # type: ignore[assignment]
                line = line.rstrip("\n")
                if line:
                    stderr_tail.append(line)
                    if len(stderr_tail) > 20:
                        stderr_tail.pop(0)

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        for raw in p.stdout:
            if cancel_event.is_set():
                try:
                    if p.poll() is None:
                        p.terminate()
                except Exception:
                    pass
                break

            line = raw.strip()
            if not line:
                continue

            if line.startswith("out_time_ms=") and duration_seconds and duration_seconds > 0:
                try:
                    out_time_ms = int(line.split("=", 1)[1])
                    pct = (out_time_ms / 1_000_000.0) / duration_seconds * 100.0
                    pct_int = int(pct)
                    if pct_int != last_reported:
                        last_reported = pct_int
                        on_progress(pct)
                except Exception:
                    pass
            elif line.startswith("progress="):
                if line.endswith("end"):
                    on_progress(100.0)

        rc = p.wait()
        if rc == 0:
            on_progress(100.0)
            return True

        if cancel_event.is_set():
            return False

        log("[ERROR] FFmpeg error output (tail):")
        for l in stderr_tail[-12:]:
            log(l)
        return False
    finally:
        set_process(None)
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


if __name__ == "__main__":
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    app = PSPConverterApp(root)
    root.mainloop()
