
---

## Features

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

### Build EXE (PyInstaller)

```bash
# Scripts are available (Windows):
cd app
rebuild.bat

# Or run it manually:
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

### Pack release ZIP

# 1. setup env
cd yt-dlp-gui/app
pip install -r ../requirements.txt
pip install pyinstaller

# 2. Build  script
.\rebuild.bat