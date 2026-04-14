import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";

type FileDonePayload = {
  input_path: string;
  output_path?: string;
  status: "success" | "failed" | "canceled";
};

type ProgressPayload = {
  input_path: string;
  percent: number;
};

type QueueStatus = "queued" | "running" | "done" | "failed" | "canceled";

type QueueItem = {
  path: string;
  name: string;
  progress: number;
  status: QueueStatus;
  startedAtMs?: number;
  etaSeconds?: number;
};

const TARGETS = ["PSP", "PS Vita", "PS3"] as const;

function basename(p: string): string {
  const normalized = p.replace(/\\/g, "/");
  const parts = normalized.split("/");
  return parts[parts.length - 1] || p;
}

function clampPercent(n: number): number {
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, n));
}

function formatEta(seconds: number | undefined): string {
  if (seconds === undefined) return "";
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
  return `${m}:${String(ss).padStart(2, "0")}`;
}

// --- Icons (Inline SVGs for a zero-dependency clean look) ---
const Icons = {
  Folder: () => <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>,
  Trash: () => <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>,
  Play: () => <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="6 3 20 12 6 21 6 3"/></svg>,
  Plus: () => <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" x2="12" y1="5" y2="19"/><line x1="5" x2="19" y1="12" y2="12"/></svg>,
  Upload: () => <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-neutral-500 mb-2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/></svg>,
};

function App() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [outputDir, setOutputDir] = useState<string>("");
  const [target, setTarget] = useState<(typeof TARGETS)[number]>("PSP");
  const [isRunning, setIsRunning] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string>("Ready.");

  const pendingToRun = useMemo(
    () => items.filter((i) => i.status === "queued").map((i) => i.path),
    [items]
  );

  function addToQueue(paths: string[]) {
    setItems((prev) => {
      const existing = new Set(prev.map((p) => p.path));
      const next = [...prev];
      for (const path of paths) {
        if (existing.has(path)) continue;
        next.push({
          path,
          name: basename(path),
          progress: 0,
          status: "queued",
        });
      }
      return next;
    });
  }

  function clearQueue() {
    setItems([]);
  }

  async function removeItem(path: string) {
    const item = items.find((i) => i.path === path);

    if (isRunning) {
      try {
        await invoke("remove_from_queue", { path });
      } catch (e) {
        setStatusMessage(`Remove failed: ${String(e)}`);
      }
    }

    if (item?.status === "running") {
      try {
        await invoke("cancel_current");
      } catch (e) {
        setStatusMessage(`Cancel failed: ${String(e)}`);
      }
    }

    setItems((prev) => prev.filter((i) => i.path !== path));
  }

  async function openConnectLink() {
    try {
      await openUrl("https://sarthakgawari.in");
    } catch (e) {
      setStatusMessage(`Open link failed: ${String(e)}`);
    }
  }

  async function addFilesViaDialog() {
    try {
      const result = await openDialog({
        multiple: true,
        filters: [{ name: "Video", extensions: ["mp4", "mkv", "avi", "mov", "flv", "wmv"] }],
      });

      if (!result) return;
      const paths = Array.isArray(result) ? result : [result];
      addToQueue(paths);
    } catch (e) {
      setStatusMessage(`Add Files failed: ${String(e)}`);
    }
  }

  async function chooseOutputDir() {
    try {
      const result = await openDialog({ directory: true, multiple: false });
      if (!result || Array.isArray(result)) return;
      setOutputDir(result);
    } catch (e) {
      setStatusMessage(`Output folder dialog failed: ${String(e)}`);
    }
  }

  async function start() {
    if (isRunning) return;
    if (pendingToRun.length === 0) {
      setStatusMessage("No queued files to convert.");
      return;
    }
    if (!outputDir) {
      setStatusMessage("Please choose an output folder first.");
      return;
    }

    setIsRunning(true);
    setStatusMessage("Converting...");
    setItems((prev) =>
      prev.map((i) => (i.status === "queued" ? { ...i, progress: 0 } : i))
    );

    try {
      await invoke("start_batch", {
        files: pendingToRun,
        outputDir,
        preset: target,
      });
    } catch (e) {
      setStatusMessage(`Start failed: ${String(e)}`);
      setIsRunning(false);
    }
  }

  // Event Listeners (Kept exactly as your original logic)
  useEffect(() => {
    let unlistenDrop: (() => void) | null = null;
    let unlistenProgress: (() => void) | null = null;
    let unlistenFileStarted: (() => void) | null = null;
    let unlistenFileDone: (() => void) | null = null;
    let unlistenBatchDone: (() => void) | null = null;

    (async () => {
      try {
        unlistenDrop = await getCurrentWindow().onDragDropEvent((event) => {
          if (event.payload.type === "drop") {
            addToQueue(event.payload.paths);
          }
        });
      } catch {}

      unlistenProgress = await listen<ProgressPayload>("progress", (e) => {
        const p = clampPercent(e.payload.percent);
        const now = Date.now();
        setItems((prev) =>
          prev.map((i) =>
            i.path === e.payload.input_path
              ? (() => {
                  const startedAtMs = i.startedAtMs;
                  let etaSeconds: number | undefined = i.etaSeconds;

                  if (startedAtMs !== undefined && p >= 1) {
                    const elapsedSeconds = (now - startedAtMs) / 1000;
                    const totalSeconds = (elapsedSeconds * 100) / p;
                    const remainingSeconds = totalSeconds - elapsedSeconds;
                    if (Number.isFinite(remainingSeconds) && remainingSeconds >= 0) {
                      etaSeconds = remainingSeconds;
                    }
                  }

                  return {
                    ...i,
                    progress: p,
                    etaSeconds,
                    status: i.status === "queued" ? "running" : i.status,
                  };
                })()
              : i
          )
        );
      });

      unlistenFileStarted = await listen<string>("file_started", (e) => {
        const now = Date.now();
        setItems((prev) =>
          prev.map((i) =>
            i.path === e.payload
              ? { ...i, status: "running", progress: 0, startedAtMs: now, etaSeconds: undefined }
              : i
          )
        );
      });

      unlistenFileDone = await listen<FileDonePayload>("file_done", (e) => {
        setItems((prev) =>
          prev.map((i) => {
            if (i.path !== e.payload.input_path) return i;
            if (e.payload.status === "success") {
              return { ...i, status: "done", progress: 100, etaSeconds: 0 };
            }
            if (e.payload.status === "canceled") {
              return { ...i, status: "canceled", etaSeconds: undefined };
            }
            return { ...i, status: "failed", etaSeconds: undefined };
          })
        );
      });

      unlistenBatchDone = await listen<void>("batch_done", () => {
        setIsRunning(false);
        setStatusMessage("Batch complete.");
      });
    })();

    return () => {
      unlistenDrop?.();
      unlistenProgress?.();
      unlistenFileStarted?.();
      unlistenFileDone?.();
      unlistenBatchDone?.();
    };
  }, []);

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-200 selection:bg-indigo-500/30 font-sans">
      <div className="mx-auto flex h-screen max-w-5xl flex-col p-6">
        
        {/* Header Section */}
        <header className="mb-6 flex items-end justify-between border-b border-neutral-800 pb-4">
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-white">PS Resizer</h1>
            <p className="text-sm text-neutral-500 mt-1">
              High-performance batch video converter.
            </p>
          </div>
          <div className="text-right flex items-center gap-4">
            <div className="flex flex-col items-end">
              <span className="text-xs font-semibold text-neutral-500 uppercase tracking-wider">Status</span>
              <span className={`text-sm font-medium ${isRunning ? "text-indigo-400 animate-pulse" : "text-neutral-300"}`}>
                {statusMessage}
              </span>
            </div>
          </div>
        </header>

        {/* Control Panel */}
        <div className="grid grid-cols-1 md:grid-cols-12 gap-4 mb-6">
          <div className="md:col-span-3">
            <label className="block text-xs font-semibold uppercase tracking-wider text-neutral-500 mb-1.5">Target</label>
            <select
              className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2.5 text-sm text-white focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 transition-all appearance-none"
              value={target}
              disabled={isRunning}
              onChange={(e) => setTarget(e.target.value as (typeof TARGETS)[number])}
            >
              {TARGETS.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>

          <div className="md:col-span-7">
            <label className="block text-xs font-semibold uppercase tracking-wider text-neutral-500 mb-1.5">Output Directory</label>
            <div className="flex gap-2">
              <input
                className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2.5 text-sm text-neutral-300 placeholder-neutral-600 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 transition-all cursor-default"
                value={outputDir}
                readOnly
                placeholder="Select destination..."
                onClick={chooseOutputDir}
              />
              <button
                className="flex items-center gap-2 rounded-lg border border-neutral-800 bg-neutral-800 px-4 py-2.5 text-sm font-medium hover:bg-neutral-700 hover:border-neutral-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                onClick={chooseOutputDir}
                disabled={isRunning}
              >
                <Icons.Folder />
              </button>
            </div>
          </div>

          <div className="md:col-span-2 flex items-end">
            <button
              className="w-full flex items-center justify-center gap-2 rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-bold text-white hover:bg-indigo-500 hover:shadow-[0_0_15px_rgba(99,102,241,0.4)] transition-all disabled:opacity-50 disabled:hover:shadow-none disabled:cursor-not-allowed"
              onClick={start}
              disabled={isRunning || pendingToRun.length === 0}
            >
              {isRunning ? (
                <>Converting...</>
              ) : (
                <><Icons.Play /> Start</>
              )}
            </button>
          </div>
        </div>

        {/* Toolbar */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <button
              className="flex items-center gap-2 rounded-md border border-neutral-800 bg-neutral-900/50 px-3 py-1.5 text-xs font-medium text-neutral-300 hover:bg-neutral-800 hover:text-white transition-colors disabled:opacity-50"
              onClick={addFilesViaDialog}
              disabled={isRunning}
            >
              <Icons.Plus /> Add Files
            </button>
            <button
              className="flex items-center gap-2 rounded-md border border-transparent px-3 py-1.5 text-xs font-medium text-neutral-500 hover:bg-red-500/10 hover:text-red-400 transition-colors disabled:opacity-50"
              onClick={clearQueue}
              disabled={isRunning || items.length === 0}
            >
              <Icons.Trash /> Clear Queue
            </button>
          </div>
        </div>

        {/* Main Queue Area */}
        <main className="flex-1 overflow-y-auto rounded-xl border border-neutral-800 bg-neutral-900/50 relative custom-scrollbar">
          {items.length === 0 ? (
            <div 
              className="absolute inset-0 flex flex-col items-center justify-center border-2 border-dashed border-neutral-800 rounded-xl m-2 hover:border-indigo-500/50 hover:bg-indigo-500/5 transition-all cursor-pointer"
              onClick={addFilesViaDialog}
            >
              <Icons.Upload />
              <p className="text-neutral-400 font-medium text-sm">Drag & drop video files here</p>
              <p className="text-neutral-600 text-xs mt-1">or click to browse</p>
            </div>
          ) : (
            <div className="p-2 flex flex-col gap-2">
              {items.map((item) => {
                const isRunning = item.status === "running";
                const isDone = item.status === "done";
                const isFailed = item.status === "failed" || item.status === "canceled";
                
                let statusColor = "text-neutral-500";
                if (isRunning) statusColor = "text-indigo-400";
                if (isDone) statusColor = "text-emerald-400";
                if (isFailed) statusColor = "text-red-400";

                const etaText = isRunning ? formatEta(item.etaSeconds) : "";

                return (
                  <div
                    key={item.path}
                    className="group flex flex-col gap-2 rounded-lg border border-neutral-800 bg-neutral-900 p-3 hover:border-neutral-700 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium text-neutral-200" title={item.name}>
                          {item.name}
                        </div>
                        <div className="truncate text-xs text-neutral-600 mt-0.5" title={item.path}>
                          {item.path}
                        </div>
                      </div>
                      <div className="flex items-center gap-3 shrink-0">
                        <div className="text-right">
                          <div className={`text-xs font-semibold capitalize ${statusColor}`}>
                            {item.status} {isRunning && `${Math.round(item.progress)}%`}
                          </div>
                          {etaText && <div className="text-[10px] text-neutral-500 mt-0.5">ETA: {etaText}</div>}
                        </div>
                        <button
                          className="h-8 w-8 rounded-md flex items-center justify-center text-neutral-500 hover:bg-red-500/10 hover:text-red-400 transition-colors disabled:opacity-30"
                          onClick={() => void removeItem(item.path)}
                          title="Remove item"
                        >
                          <Icons.Trash />
                        </button>
                      </div>
                    </div>

                    {/* Sleek Progress Bar */}
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-neutral-950 border border-neutral-800/50">
                      <div
                        className={`h-full rounded-full transition-all duration-300 ease-out ${
                          isDone ? "bg-emerald-500" : isFailed ? "bg-red-500" : "bg-indigo-500"
                        } ${isRunning ? "relative overflow-hidden" : ""}`}
                        style={{ width: `${Math.round(item.progress)}%` }}
                      >
                        {/* Shimmer effect for running state */}
                        {isRunning && (
                          <div className="absolute top-0 left-0 bottom-0 right-0 animate-[shimmer_2s_infinite] bg-gradient-to-r from-transparent via-white/20 to-transparent translate-x-[-100%]" />
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </main>

        <div className="mt-3 text-center">
          <button
            type="button"
            onClick={() => void openConnectLink()}
            className="text-xs text-neutral-500 underline hover:text-neutral-200"
          >
            Click here to connect with me!
          </button>
        </div>
      </div>

      {/* Global styles for animations & scrollbars */}
      <style>{`
        @keyframes shimmer {
          100% { transform: translateX(100%); }
        }
        .custom-scrollbar::-webkit-scrollbar {
          width: 8px;
        }
        .custom-scrollbar::-webkit-scrollbar-track {
          background: transparent;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
          background-color: #262626; /* neutral-800 */
          border-radius: 20px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover {
          background-color: #404040; /* neutral-700 */
        }
      `}</style>
    </div>
  );
}

export default App;