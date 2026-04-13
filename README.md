# PSP RESIZER (GUI)

A tiny Tkinter GUI that converts a selected video into a PSP-friendly MP4 using FFmpeg.

## Run from source

1. Put `ffmpeg.exe` next to `psp_gui.py` (recommended for portability), **or** install FFmpeg and ensure it’s on your PATH.
2. Run:

```powershell
py psp_gui.py
```

It will create `input/` and `output/` next to the script (or next to the built `.exe`).

## Build a windowed EXE (no console)

1. Install PyInstaller:

```powershell
py -m pip install pyinstaller tkinterdnd2
```

2. Ensure `ffmpeg.exe` is next to `psp_gui.py`.

3. Build:

```powershell
pyinstaller --onefile --windowed --add-binary "ffmpeg.exe;." --collect-all tkinterdnd2 psp_gui.py
```

The resulting executable will be in `dist/`.
