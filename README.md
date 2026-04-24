# yt-dlp-gui

Simplistic graphical interface for the command line tool [yt-dlp](https://github.com/yt-dlp/yt-dlp), extended with a **NetShort mode** for batch episode downloading from xemshort.top with automatic subtitle hardcoding.

---

## Features

### Standard Mode (yt-dlp)
- Download any URL supported by yt-dlp
- Select quality preset (`best`, `mp4`, `mp3`, or custom)
- Configure output directory
- Real-time download log
- Auto-update yt-dlp on startup

### NetShort Mode
- **Fetch episodes** from `xemshort.top` by Movie ID or API URL
- **Episode picker** — select which episodes to include
- **Parallel downloads** — video + subtitle downloaded concurrently
- **Subtitle skip** — if subtitle file already exists locally, skip re-download (preserves manual edits)
- **Hardcode subtitles** — burn-in subtitles onto video using ffmpeg with custom font, size, and outline
- **Auto re-merge** — if subtitle file is newer than the merged output, re-merge automatically
- **Crop overlay** — remove black bars / watermark area via ffmpeg crop filter
- **Progress tracking** — per-episode and per-movie status table
- **Save/load API response** as JSON for offline use

---

## Project Structure

```
yt-dlp-gui/
├── app/
│   ├── app.py              # Main application — UI logic + NetShort mode
│   ├── worker.py           # yt-dlp download worker thread
│   ├── dep_dl.py           # Dependency downloader (yt-dlp, ffmpeg)
│   ├── utils.py            # Shared utilities, constants
│   ├── config.toml         # User configuration (presets, settings)
│   ├── ui/
│   │   ├── main_window.py  # Auto-generated PySide6 UI code
│   │   └── main_window.ui  # Qt Designer UI file
│   ├── assets/             # Icons and static resources
│   ├── fonts/              # Bundled fonts (UTM Alter Gothic, etc.)
│   ├── release/            # Built release packages
│   ├── tests/              # Test and debug scripts
│   ├── scratch/            # Temporary / debug files (not part of build)
│   ├── rebuild.bat         # PyInstaller build script
│   └── yt-dlp-gui.spec     # PyInstaller spec file
├── tests/                  # Root-level test scripts
├── requirements.txt
└── README.md
```

---

## NetShort Mode — Workflow

```
1. Enter Movie ID  →  [Fetch]
         │
         ▼
2. API returns episode list  →  Episode Picker dialog
   (or load from saved JSON)      (select episodes to process)
         │
         ▼
3. Add to queue  →  [Start]
         │
         ├─ For each movie (parallel per episode):
         │
         │   ┌─────────────────────────────────┐
         │   │  Download Video (.mp4)           │
         │   │  Download Subtitle (.srt/.vtt)   │  ← skipped if file exists
         │   └─────────────────────────────────┘
         │              │
         │   [Option: Hardcode Sub (merge)]
         │              │
         │   ffmpeg burn-in subtitles
         │   - Font: UTM Alter Gothic (installed per-user)
         │   - FontSize, Outline, Alignment via force_style
         │   - Crop overlay removed via crop filter
         │              │
         │   Output: {episode}_merged.mp4
         │
         └─ Movie status → Done / Error
```

### Subtitle Re-merge Logic

| Condition | Action |
|-----------|--------|
| Merged file does not exist | Run merge |
| Merged file exists, sub not newer | Skip (already up to date) |
| Merged file exists, sub is newer | Re-merge automatically |

---

## Usage

### Portable (Windows)

Download the latest release ZIP from the [releases page](https://github.com/dsymbol/yt-dlp-gui/releases). Extract and run `yt-dlp-gui.exe`. No installation required — ffmpeg and yt-dlp are bundled.

### Manual

Requires [Python](https://www.python.org/downloads/) 3.9+.

```bash
git clone https://github.com/dsymbol/yt-dlp-gui
cd yt-dlp-gui
pip install -r requirements.txt
cd app
python app.py
```

---

## Configuration

Edit `app/config.toml` to customize presets and general settings.

```toml
[general]
path = "D:/Downloads"           # default save directory
update_ytdlp = true             # auto-update yt-dlp on startup
global_args = ""                # extra args added to every download

[presets]
best  = "-f bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
mp4   = "-f bv*[vcodec^=avc]+ba[ext=m4a]/b"
mp3   = "--extract-audio --audio-format mp3 --audio-quality 0"
```

Custom presets can be strings or lists:

```toml
[presets]
mp4_thumbnail = ["-f", "bv*[vcodec^=avc]+ba[ext=m4a]/b", "--embed-thumbnail"]
```

---

## Subtitle Style (NetShort Mode)

Configured in the NetShort tab UI and applied via ffmpeg `force_style`:

| Setting | Default |
|---------|---------|
| Font | UTM Alter Gothic |
| Font Size | 20 |
| Outline | 1 |
| Shadow | 0 |
| Bold | Yes |
| Alignment | Bottom center (2) |
| MarginV | 30px |

The font is installed automatically to the Windows per-user font directory on first run.

---

## CLI Commands

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run (development)

```bash
cd app
python app.py
```

### Regenerate UI code from Qt Designer file

Sau khi chỉnh sửa `app/ui/main_window.ui` trong Qt Designer, chạy lệnh này để cập nhật file Python:

```bash
pyside6-uic app/ui/main_window.ui -o app/ui/main_window.py
```

### Build EXE (PyInstaller)

```bash
# Dùng script có sẵn (Windows):
cd app
rebuild.bat

# Hoặc chạy thủ công:
cd app
pyinstaller --name=yt-dlp-gui --onefile --windowed \
  --icon=assets/yt-dlp-gui.ico \
  --add-data=assets;assets \
  --hidden-import=httpx \
  --hidden-import=anyio \
  --hidden-import=charset_normalizer \
  --hidden-import=certifi \
  --hidden-import=platformdirs \
  --hidden-import=httpx_sse \
  app.py --distpath=dist --noconfirm
```

Output EXE: `app/dist/yt-dlp-gui.exe`

### Đóng gói release ZIP

```bash
# Copy EXE vào thư mục release rồi zip:
cp app/dist/yt-dlp-gui.exe app/release/yt-dlp-gui-vX.Y.Z/
cd app/release
zip -r yt-dlp-gui-vX.Y.Z.zip yt-dlp-gui-vX.Y.Z/
```

### Kiểm tra setup (Windows)

```bat
app\setup_check.bat
```

---

## Building

> Output EXE là `app/dist/yt-dlp-gui.exe`. Copy vào `app/release/yt-dlp-gui-vX.Y.Z/` rồi ZIP để phân phối.

> Nếu có lỗi khi chạy, kiểm tra file `debug.log` trong thư mục ứng dụng.
