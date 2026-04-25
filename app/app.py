"""
NetShort Downloader GUI - Integrated into yt-dlp-gui.
Supports:
  - yt-dlp mode: download single URLs with optional subtitles (existing flow)
  - NetShort mode: fetch episodes by movie_id, parallel download, hardcode sub + crop overlay
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess as sp
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import qtawesome as qta
import requests
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings

from dep_dl import DepWorker
from ui.main_window import Ui_MainWindow
from utils import BIN_DIR, ROOT, ItemRoles, TreeColumn, load_toml, save_toml
from worker import DownloadWorker
from update_version import Updater

try:
    from _version import __version__
except ImportError:
    __version__ = "1.0.0"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s (%(module)s:%(lineno)d) %(message)s",
    handlers=[
        logging.FileHandler(BIN_DIR / "debug.log", encoding="utf-8", delay=True),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================================
# NETSHORT CONSTANTS
# ============================================================================

NETSHORT_APP_NAME = "NetShort GUI"
NETSHORT_CONFIG_KEY = "NetShortGUI"

DEFAULT_API_URL = "https://api.xemshort.top/allepisode?shortPlayId={movie_id}"

NETSHORT_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-AU,en;q=0.9,vi;q=0.8",
    "Origin": "https://xemshort.top",
    "Referer": "https://xemshort.top/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "short-source": "web",
}

NETSHORT_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Referer": "https://xemshort.top/",
    "Origin": "https://xemshort.top",
}


# ============================================================================
# SUB VIEWER DIALOG  (existing)
# ============================================================================

class SubViewerDialog(QtWidgets.QDialog):
    """Subtitle viewer dialog with search support."""

    def __init__(self, srt_content: str, parent=None):
        """Build dialog UI and render the SRT content as coloured HTML."""
        super().__init__(parent)
        self.setWindowTitle("Sub Title")
        self.setMinimumSize(700, 500)
        self.resize(750, 550)
        self.setModal(False)

        layout = QtWidgets.QVBoxLayout(self)

        toolbar = QtWidgets.QHBoxLayout()
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Tim kiem...")
        self.search_input.textChanged.connect(self._do_search)
        self.btn_prev = QtWidgets.QPushButton("<")
        self.btn_prev.setFixedWidth(30)
        self.btn_prev.clicked.connect(self._prev_match)
        self.btn_next = QtWidgets.QPushButton(">")
        self.btn_next.setFixedWidth(30)
        self.btn_next.clicked.connect(self._next_match)
        self.match_label = QtWidgets.QLabel("")
        toolbar.addWidget(QtWidgets.QLabel("Tim:"))
        toolbar.addWidget(self.search_input)
        toolbar.addWidget(self.btn_prev)
        toolbar.addWidget(self.btn_next)
        toolbar.addWidget(self.match_label)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.text_edit = QtWidgets.QTextEdit()
        self.text_edit.setReadOnly(True)
        font = QtGui.QFont("Consolas", 9)
        self.text_edit.setFont(font)
        layout.addWidget(self.text_edit)

        footer = QtWidgets.QHBoxLayout()
        self.line_count_label = QtWidgets.QLabel("")
        self.line_count_label.setStyleSheet("color: #888; font-size: 11px")
        footer.addWidget(self.line_count_label)
        btn_close = QtWidgets.QPushButton("Dong")
        btn_close.clicked.connect(self.close)
        footer.addStretch()
        footer.addWidget(btn_close)
        layout.addLayout(footer)

        self._srt_content = srt_content
        self._all_highlights = []
        self._cur_match = -1
        self._render_srt()

    def _render_srt(self):
        """Render raw SRT text as coloured HTML and display in the text widget."""
        lines = self._srt_content.split("\n")
        html_lines = []
        entry_count = 0

        for line in lines:
            stripped = line.strip()
            if stripped.isdigit():
                entry_count += 1
                html_lines.append(
                    f'<div class="entry" style="color:#60a5fa;font-weight:bold;">{line}</div>'
                )
            elif "-->" in line:
                html_lines.append(f'<div class="time" style="color:#f97316;">{line}</div>')
            elif stripped:
                html_lines.append(f'<div class="text">{line}</div>')
            else:
                html_lines.append('<div class="gap">&nbsp;</div>')

        html = (
            '<html><head><style>'
            'body{background:#0f1117;color:#e2e8f0;padding:8px;font-family:Consolas,monospace;font-size:13px}'
            '.entry{font-size:11px;margin-top:8px}'
            '.time{font-size:11px;margin-bottom:2px}'
            '.text{margin-bottom:6px;line-height:1.5}'
            '.gap{height:4px}'
            '.hl{background:#fbbf24;color:#0f1117;padding:1px 2px;border-radius:2px}'
            '</style></head><body>' + "".join(html_lines) + '</body></html>'
        )
        self.text_edit.setHtml(html)
        self.line_count_label.setText(f"{entry_count} entries")

    def _do_search(self, text):
        """Highlight all matches of the search term and update the match counter."""
        if not text:
            self._cur_match = -1
            self._all_highlights = []
            self.match_label.setText("")
            self._render_srt()
            return
        self._cur_match = -1
        self._all_highlights = []
        pattern = re.compile(re.escape(text), re.IGNORECASE)
        html_lines = []
        entry_count = 0
        for line in self._srt_content.split("\n"):
            stripped = line.strip()
            if stripped.isdigit():
                entry_count += 1
                safe = QtCore.Q.escape(line)
                html_lines.append(f'<div class="entry">{safe}</div>')
            elif "-->" in line:
                safe = QtCore.Q.escape(line)
                html_lines.append(f'<div class="time">{safe}</div>')
            elif stripped:
                safe = QtCore.Q.escape(line)
                highlighted = pattern.sub(lambda m: f'<span class="hl">{m.group()}</span>', safe)
                count = len(pattern.findall(line))
                self._all_highlights.extend([True] * count)
                html_lines.append(f'<div class="text">{highlighted}</div>')
            else:
                html_lines.append('<div class="gap">&nbsp;</div>')
        html = (
            '<html><head><style>'
            'body{background:#0f1117;color:#e2e8f0;padding:8px;font-family:Consolas,monospace;font-size:13px}'
            '.entry{font-size:11px;margin-top:8px}'
            '.time{font-size:11px;margin-bottom:2px}'
            '.text{margin-bottom:6px;line-height:1.5}'
            '.gap{height:4px}'
            '.hl{background:#fbbf24;color:#0f1117;padding:1px 2px;border-radius:2px}'
            '</style></head><body>' + "".join(html_lines) + '</body></html>'
        )
        self.text_edit.setHtml(html)
        self.match_label.setText(f"0/{len(self._all_highlights)}")

    def _prev_match(self):
        """Navigate to the previous search match."""
        if not self._all_highlights:
            return
        self._cur_match = (self._cur_match - 1) % len(self._all_highlights)
        self.match_label.setText(f"{self._cur_match + 1}/{len(self._all_highlights)}")

    def _next_match(self):
        """Navigate to the next search match."""
        if not self._all_highlights:
            return
        self._cur_match = (self._cur_match + 1) % len(self._all_highlights)
        self.match_label.setText(f"{self._cur_match + 1}/{len(self._all_highlights)}")


# ============================================================================
# NETSHORT DATA CLASSES
# ============================================================================


@dataclass
class NSEpisode:
    """Single episode data: URLs, local file paths, and download status."""
    id: str
    name: str
    episode: int
    play: str
    subtitle_url: Optional[str] = None
    selected: bool = True

    video_path: Optional[Path] = None
    sub_path: Optional[Path] = None
    merged_path: Optional[Path] = None
    status: str = "pending"
    error_msg: str = ""


@dataclass
class NSMovie:
    """Movie container: holds all episodes and the target save directory."""
    name: str
    episodes: list[NSEpisode] = field(default_factory=list)
    save_dir: Path = field(default_factory=lambda: Path("."))

    @property
    def selected_count(self) -> int:
        """Number of episodes marked as selected."""
        return sum(1 for e in self.episodes if e.selected)

    @property
    def total(self) -> int:
        """Total number of episodes in this movie."""
        return len(self.episodes)

    @property
    def folder_name(self) -> str:
        """Sanitized movie name safe for use as a directory name."""
        return _ns_sanitize_filename(self.name)


# ============================================================================
# NETSHORT HELPERS
# ============================================================================


def _ns_sanitize_filename(name: str) -> str:
    """Strip characters illegal on Windows/Linux filesystems and trim to 200 chars."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:200] if name else "Untitled"


def _ns_parse_episodes(data: dict | list, movie_name: str = "") -> list[NSEpisode]:
    """Parse API response (dict or list) into a sorted list of NSEpisode objects."""
    if isinstance(data, dict):
        # Try curl format: shortPlayEpisodeInfos
        items = data.get("shortPlayEpisodeInfos")
        # Try requests format: data
        if not items:
            items = data.get("data", [])
        # Lưu shortPlayName nếu chưa có
        if not movie_name:
            movie_name = data.get("shortPlayName", "")
    else:
        items = data

    episodes: list[NSEpisode] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        # curl format: episodeNo, episodeName, playVoucher, subtitleList
        episode = item.get("episodeNo") or item.get("episode") or 0

        # episodeName riêng -> dùng; không có thì dùng movie_name
        ep_name = item.get("episodeName")
        name = ep_name if ep_name else movie_name or item.get("name") or "Untitled"

        play = item.get("playVoucher") or item.get("play") or ""

        # subtitle: format cũ = {subtitle:[{url}]} vs format mới = {subtitleList:[{url}]}
        subtitle_list = item.get("subtitleList") or item.get("subtitle") or []
        sub_url = None
        if subtitle_list and isinstance(subtitle_list, list) and len(subtitle_list) > 0:
            # format cũ: subtitle[0].url, format mới: subtitleList[0].url
            sub_url = subtitle_list[0].get("url")

        episodes.append(NSEpisode(
            id=str(item.get("episodeId") or item.get("id", "")),
            name=name,
            episode=int(episode),
            play=play,
            subtitle_url=sub_url,
        ))
    episodes.sort(key=lambda e: e.episode)
    return episodes


def _ns_detect_sub_ext(content: bytes) -> str:
    """Detect subtitle format from raw bytes; returns 'vtt', 'srt', or 'txt'."""
    text = content[:500].decode("utf-8", errors="ignore")
    if "WEBVTT" in text:
        return "vtt"
    if "-->" in text:
        return "srt"
    return "txt"


def _ns_check_ffmpeg() -> bool:
    """Return True if ffmpeg is available (bundled next to EXE or on system PATH)."""
    # Check bundled ffmpeg next to the executable first
    bundled = Path(sys.executable).parent / "ffmpeg.exe"
    if bundled.exists():
        try:
            sp.run([str(bundled), "-version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, sp.CalledProcessError):
            pass
    # Fall back to system PATH
    try:
        sp.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, sp.CalledProcessError):
        return False


def _ns_escape_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter string (backslash and colon)."""
    s = str(path).replace("\\", "/").replace(":", r"\:")
    return s.replace("'", r"\'")


def _ns_convert_sub_to_ass(sub_path: Path, font_name: str, font_size: int,
                            outline: float = 1.0) -> Path:
    """Convert .srt/.vtt to .ass with full style control embedded in the file."""
    import re as _re

    ass_path = sub_path.with_suffix('.ass')

    # Full ASS style: Bold=-1 (bold), Outline in style header, Shadow=0
    header = (
        "[Script Info]\r\n"
        "ScriptType: v4.00+\r\nCollisions: Normal\r\n"
        "PlayResX: 384\r\nPlayResY: 288\r\n\r\n"
        "[V4+ Styles]\r\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
        f"Style: Default,{font_name},{font_size},"
        "&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,2,10,10,30,1\r\n\r\n"
        "[Events]\r\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    )

    try:
        content = sub_path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return sub_path

    is_vtt = content.strip().upper().startswith('WEBVTT')
    events = []

    def _to_ass_time(h, m, s, ms):
        return f"{int(h)}:{m}:{s}.{str(ms)[:2]}"

    if is_vtt:
        blocks = _re.split(r'\n{2,}', content.strip())
        for block in blocks:
            block = block.strip()
            if not block or block.upper().startswith('WEBVTT') or block.startswith('NOTE'):
                continue
            lines = block.split('\n')
            t_idx = next((i for i, l in enumerate(lines) if '-->' in l), None)
            if t_idx is None:
                continue
            t_line = lines[t_idx].split('-->')[0].strip() + ' --> ' + lines[t_idx].split('-->')[1].strip().split()[0]
            mt = _re.match(
                r'(?:(\d+):)?(\d+):(\d+)\.(\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)\.(\d+)', t_line
            )
            if not mt:
                continue
            start = _to_ass_time(mt.group(1) or 0, mt.group(2), mt.group(3), mt.group(4))
            end = _to_ass_time(mt.group(5) or 0, mt.group(6), mt.group(7), mt.group(8))
            text_lines = [l.strip() for l in lines[t_idx+1:] if l.strip()]
            if not text_lines:
                continue
            text = r'\N'.join(_re.sub(r'<[^>]+>', '', l) for l in text_lines)
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    else:
        blocks = _re.split(r'\n\s*\n', content.strip())
        for block in blocks:
            lines = block.strip().split('\n')
            t_idx = next((i for i, l in enumerate(lines) if '-->' in l), None)
            if t_idx is None:
                continue
            mt = _re.match(
                r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)',
                lines[t_idx]
            )
            if not mt:
                continue
            h1, m1, s1, ms1, h2, m2, s2, ms2 = mt.groups()
            start = _to_ass_time(h1, m1, s1, ms1)
            end = _to_ass_time(h2, m2, s2, ms2)
            text_lines = [l.strip() for l in lines[t_idx+1:] if l.strip()]
            if not text_lines:
                continue
            text = r'\N'.join(_re.sub(r'<[^>]+>', '', l) for l in text_lines)
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    if not events:
        return sub_path

    try:
        ass_path.write_text(header + '\r\n'.join(events) + '\r\n', encoding='utf-8-sig')
        return ass_path
    except Exception:
        return sub_path


def _ns_install_fonts(fonts_dir: Path, log_fn=None) -> None:
    """Install TTF fonts from fonts_dir into user's local fonts dir (no admin needed, Win10+)."""
    if sys.platform != "win32":
        return
    import winreg
    import shutil as _sh

    local_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    reg_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

    for font_file in list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf")):
        dest = local_fonts / font_file.name
        try:
            local_fonts.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                _sh.copy2(str(font_file), str(dest))
                if log_fn:
                    log_fn(f"Font installed: {font_file.name}")
            reg_name = font_file.stem.replace("-", " ") + " (TrueType)"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, reg_path, 0,
                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
            ) as key:
                try:
                    winreg.QueryValueEx(key, reg_name)
                except FileNotFoundError:
                    winreg.SetValueEx(key, reg_name, 0, winreg.REG_SZ, str(dest))
                    if log_fn:
                        log_fn(f"Font registered: {reg_name}")
        except Exception as e:
            if log_fn:
                log_fn(f"Font install warn ({font_file.name}): {e}")


def _ns_load_bundled_fonts(fonts_dir: Path) -> list[str]:
    """Register TTF/OTF files from fonts_dir with Qt and return their family names."""
    families = []
    for f in sorted(fonts_dir.glob("*.ttf")) + sorted(fonts_dir.glob("*.otf")):
        fid = QtGui.QFontDatabase.addApplicationFont(str(f))
        if fid >= 0:
            families.extend(QtGui.QFontDatabase.applicationFontFamilies(fid))
    return families


# ============================================================================
# NETSHORT WORKER THREADS
# ============================================================================


class NSFetchWorker(QtCore.QThread):
    """Background thread that fetches episode list from the API."""

    success = QtCore.Signal(list, str)  # episodes, movie_name
    error = QtCore.Signal(str)

    def __init__(self, api_url: str, movie_id: str):
        """Store API URL and movie ID for the fetch request."""
        super().__init__()
        self.api_url = api_url
        self.movie_id = movie_id

    def run(self):
        """Call the API via curl OPTIONS, parse JSON, emit success or error signal."""
        import subprocess
        import json

        if "{movie_id}" in self.api_url:
            url = self.api_url.format(movie_id=self.movie_id)
        elif self.movie_id and self.movie_id not in self.api_url:
            sep = "&" if "?" in self.api_url else "?"
            url = f"{self.api_url}{sep}shortPlayId={self.movie_id}"
        else:
            url = self.api_url

        ua = (
            "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Mobile Safari/537.36"
        )

        # Dùng OPTIONS request - server trả data JSON rõ ràng (GET trả encoded)
        cmd = (
            f'curl -s -L -X OPTIONS --url "{url}" '
            f'-H "accept: */*" '
            f'-H "accept-language: en-AU,en;q=0.9,vi;q=0.8" '
            f'-H "access-control-request-headers: short-source" '
            f'-H "access-control-request-method: GET" '
            f'-H "origin: https://xemshort.top" '
            f'-H "referer: https://xemshort.top/" '
            f'-H "sec-fetch-dest: empty" '
            f'-H "sec-fetch-mode: cors" '
            f'-H "sec-fetch-site: same-site" '
            f'-H "user-agent: {ua}"'
        )

        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, timeout=60
            )
            raw = result.stdout
        except subprocess.TimeoutExpired:
            self.error.emit("curl timed out after 60s.")
            return

        if not raw or raw.strip() == b"":
            self.error.emit(f"curl returned empty response.\nURL: {url}")
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            preview = raw[:200].decode("utf-8", errors="replace")
            self.error.emit(
                f"JSON khong hop le: {e}\n"
                f"Preview: {preview}"
            )
            return

        # Check for obfuscated data
        api_data = data.get("data") if isinstance(data, dict) else None
        if isinstance(api_data, str) and len(api_data) > 100:
            self.error.emit(
                "API tra ve du lieu bi ma hoa.\n"
                "Thu lai hoac dung 'Load JSON File' / 'Paste JSON'."
            )
            return

        if isinstance(data, dict) and not data.get("success", True):
            self.error.emit(f"API loi: {data.get('message', 'unknown')}")
            return

        episodes = _ns_parse_episodes(data)
        if not episodes:
            self.error.emit("API tra ve danh sach rong.")
            return

        movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
        self.success.emit(episodes, movie_name)


class NSDownloadMergeWorker(QtCore.QThread):
    """Background thread: downloads video + subtitle then optionally hardcodes sub via ffmpeg."""

    log_msg = QtCore.Signal(str)
    episode_status = QtCore.Signal(int, str)
    progress = QtCore.Signal(int, int)
    finished_all = QtCore.Signal()

    def __init__(self, movie: NSMovie, concurrency: int, download_sub: bool,
                 do_merge: bool, crf: int, preset: str,
                 sub_font: str = "UTM Alter Gothic", sub_size: int = 20,
                 sub_margin_v: int = 30):
        """Configure worker with movie data, thread count, and ffmpeg encode settings."""
        super().__init__()
        self.movie = movie
        self.concurrency = concurrency
        self.download_sub = download_sub
        self.do_merge = do_merge
        self.crf = crf
        self.ffpreset = preset
        self.sub_font = sub_font
        self.sub_size = sub_size
        self.sub_margin_v = sub_margin_v
        import threading
        self._stop = threading.Event()

    def stop(self):
        """Signal the worker to stop after the current episode finishes."""
        self._stop.set()

    def log(self, msg: str):
        """Emit a timestamped log message to the UI log panel."""
        ts = time.strftime("%H:%M:%S")
        self.log_msg.emit(f"[{ts}] {msg}")

    def _get_ffmpeg_path(self) -> Path | None:
        """Locate ffmpeg: check bundled copy next to EXE first, then system PATH."""
        import sys as _sys
        for name in ("ffmpeg", "ffmpeg.exe"):
            base = str(Path(_sys.executable).parent)
            candidate = Path(base) / name
            if candidate.exists():
                return candidate
        import shutil as sh
        path = sh.which("ffmpeg")
        if path:
            return Path(path)
        return None

    def _download_file(self, url: str, output: Path, desc: str, retries: int = 3) -> bool:
        """Download a URL to a file with retry logic; skip if file already exists."""
        if output.exists() and output.stat().st_size > 1024:
            self.log(f"SKIP {desc} (da ton tai)")
            return True

        for attempt in range(1, retries + 1):
            if self._stop.is_set():
                tmp.unlink(missing_ok=True)
                return False
            try:
                with requests.get(url, headers=NETSHORT_DOWNLOAD_HEADERS,
                                  stream=True, timeout=15) as r:
                    r.raise_for_status()
                    tmp = output.with_suffix(output.suffix + ".part")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            if self._stop.is_set():
                                tmp.unlink(missing_ok=True)
                                return False
                            if chunk:
                                f.write(chunk)
                    tmp.rename(output)
                return True
            except requests.exceptions.Timeout:
                self.log(f"TIMEOUT {desc} (thu {attempt}/{retries})")
                if self._stop.is_set():
                    tmp.unlink(missing_ok=True)
                    return False
                time.sleep(2 * attempt)
                continue
            except requests.exceptions.ConnectionError as e:
                self.log(f"ERR {desc} (thu {attempt}/{retries}): {e}")
                if self._stop.is_set():
                    tmp.unlink(missing_ok=True)
                    return False
                time.sleep(2 * attempt)
                continue
            except Exception as e:
                self.log(f"ERR {desc} (thu {attempt}/{retries}): {e}")
                if self._stop.is_set():
                    tmp.unlink(missing_ok=True)
                    return False
                time.sleep(2 * attempt)
        tmp.unlink(missing_ok=True)
        return False

    def _download_episode(self, ep: NSEpisode) -> bool:
        """Download video and subtitle for one episode; skip sub if local file exists."""
        if self._stop.is_set() or not ep.selected:
            return False

        folder = self.movie.save_dir / self.movie.folder_name
        folder.mkdir(parents=True, exist_ok=True)

        padding = len(str(self.movie.total))
        base = f"ep{str(ep.episode).zfill(padding)}"

        self.episode_status.emit(ep.episode, "downloading")

        video_path = folder / f"{base}.mp4"
        dl_ok = self._download_file(ep.play, video_path, f"video tap {ep.episode}")

        # If file already exists (skipped), still mark as downloaded so merge can run
        if video_path.exists() and video_path.stat().st_size > 1024 and ep.status != "error":
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded")
            self.log(f"SKIP video tap {ep.episode} (da ton tai)")
        elif not dl_ok:
            ep.status = "error"
            ep.error_msg = "download video failed"
            self.episode_status.emit(ep.episode, "error")
            return False
        else:
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded")

        if self.download_sub and ep.subtitle_url:
            # Check if sub already exists (skip download to preserve user edits)
            existing_sub = next(
                (folder / f"{base}.{ext}" for ext in ("srt", "vtt", "txt")
                 if (folder / f"{base}.{ext}").exists()
                 and (folder / f"{base}.{ext}").stat().st_size > 0),
                None
            )
            if existing_sub:
                ep.sub_path = existing_sub
                self.log(f"SKIP sub tap {ep.episode} (da ton tai: {existing_sub.name})")
            else:
                try:
                    r = requests.get(ep.subtitle_url,
                                     headers=NETSHORT_DOWNLOAD_HEADERS, timeout=30)
                    r.raise_for_status()
                    ext = _ns_detect_sub_ext(r.content)
                    sub_path = folder / f"{base}.{ext}"
                    with open(sub_path, "wb") as f:
                        f.write(r.content)
                    ep.sub_path = sub_path
                    self.log(f"sub tap {ep.episode} OK ({ext}, {len(r.content)} bytes)")
                except Exception as e:
                    self.log(f"sub tap {ep.episode} loi: {e}")

        ep.status = "downloaded"
        self.episode_status.emit(ep.episode, "downloaded")
        return True

    def _merge_episode(self, ep: NSEpisode) -> bool:
        """Burn subtitle into video with ffmpeg; re-merge only if sub is newer than output."""
        if self._stop.is_set() or not ep.video_path or not ep.video_path.exists():
            return False

        merge_dir = self.movie.save_dir / self.movie.folder_name / "merged"
        merge_dir.mkdir(parents=True, exist_ok=True)

        padding = len(str(self.movie.total))
        base = f"ep{str(ep.episode).zfill(padding)}"
        out_path = merge_dir / f"{base}_merged.mp4"

        if out_path.exists() and out_path.stat().st_size > 1024:
            # Re-merge if sub was modified after last merge
            sub_mtime = ep.sub_path.stat().st_mtime if ep.sub_path and ep.sub_path.exists() else 0
            if sub_mtime <= out_path.stat().st_mtime:
                ep.merged_path = out_path
                ep.status = "done"
                self.episode_status.emit(ep.episode, "done")
                self.log(f"merge tap {ep.episode} SKIP (da ton tai)")
                return True
            self.log(f"tap {ep.episode}: sub moi hon merged -- re-merge...")

        if not ep.sub_path or not ep.sub_path.exists():
            shutil.copy2(ep.video_path, out_path)
            ep.merged_path = out_path
            ep.status = "done"
            self.episode_status.emit(ep.episode, "done")
            self.log(f"tap {ep.episode} khong co sub -- copy video vao merged/")
            return True

        self.episode_status.emit(ep.episode, "merging")
        self.log(f"merge tap {ep.episode}...")

        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            self.log("ffmpeg not found!")
            ep.status = "error"
            ep.error_msg = "ffmpeg not found"
            self.episode_status.emit(ep.episode, "error")
            return False

        sub_filter = _ns_escape_path(ep.sub_path)

        # Locate bundled fonts directory (next to EXE or next to script)
        fonts_dir = Path(sys.executable).parent / "fonts"
        if not fonts_dir.exists():
            fonts_dir = Path(__file__).parent / "fonts"

        if fonts_dir.exists():
            _ns_install_fonts(fonts_dir, self.log)
            fonts_dir_escaped = _ns_escape_path(fonts_dir)
            vf_filter = (
                f"subtitles='{sub_filter}'"
                f":fontsdir='{fonts_dir_escaped}'"
                f":force_style='FontName={self.sub_font},FontSize={self.sub_size},"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                f"BorderStyle=1,Outline=1,Shadow=0,Bold=1,Alignment=2,MarginV={self.sub_margin_v}'"
            )
        else:
            self.log("WARN: fonts/ dir not found, fallback to system fonts")
            vf_filter = (
                f"subtitles='{sub_filter}':force_style="
                f"'FontName={self.sub_font},FontSize={self.sub_size},"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                f"BorderStyle=1,Outline=1,Shadow=0,Bold=1,Alignment=2,MarginV={self.sub_margin_v}'"
            )

        cmd = [
            str(ffmpeg_path), "-y",
            "-i", str(ep.video_path),
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-preset", self.ffpreset,
            "-crf", str(self.crf),
            "-c:a", "copy",
            "-loglevel", "error",
            str(out_path),
        ]

        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                self.log(f"ffmpeg loi tap {ep.episode}: {result.stderr[:500]}")
                ep.status = "error"
                ep.error_msg = "ffmpeg failed"
                self.episode_status.emit(ep.episode, "error")
                return False

            ep.merged_path = out_path
            ep.status = "done"
            self.episode_status.emit(ep.episode, "done")
            self.log(f"merge tap {ep.episode} OK -> {out_path.name}")
            return True

        except sp.TimeoutExpired:
            self.log(f"merge tap {ep.episode} TIMEOUT")
            ep.status = "error"
            ep.error_msg = "merge timeout"
            self.episode_status.emit(ep.episode, "error")
            return False
        except Exception as e:
            self.log(f"merge tap {ep.episode} exception: {e}")
            ep.status = "error"
            ep.error_msg = str(e)
            self.episode_status.emit(ep.episode, "error")
            return False

    def run(self):
        """Entry point: download all selected episodes in parallel, then merge sequentially."""
        selected = [e for e in self.movie.episodes if e.selected]
        total = len(selected)
        if total == 0:
            self.log("Khong co tap nao duoc chon.")
            self.finished_all.emit()
            return

        self.log(f"=== Bat dau tai & merge '{self.movie.name}' ({total} tap) ===")
        self.log(f"Thu muc: {self.movie.save_dir / self.movie.folder_name}")
        done = 0

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._download_episode, e): e for e in selected}
            for _ in as_completed(futures):
                if self._stop.is_set():
                    for f in futures:
                        f.cancel()
                    break
                done += 1
                self.progress.emit(done, total * (2 if self.do_merge else 1))

        if self._stop.is_set():
            self.log("Da dung.")
            self.finished_all.emit()
            return

        if self.do_merge:
            if not _ns_check_ffmpeg():
                self.log("CANH BAO: khong tim thay ffmpeg -- bo qua merge.")
            else:
                for ep in selected:
                    if self._stop.is_set():
                        break
                    # Merge if downloaded OR if video already exists (re-merge case)
                    if ep.status in ("downloaded", "pending") and ep.video_path and ep.video_path.exists():
                        self._merge_episode(ep)
                        done += 1
                        self.progress.emit(done, total * 2)

        self.log(f"=== Hoan tat '{self.movie.name}' ===")
        self.finished_all.emit()


# ============================================================================
# EPISODE PICKER DIALOG
# ============================================================================


class NSEpisodePickerDialog(QtWidgets.QDialog):
    """Dialog for selecting which episodes to add to the download queue."""

    def __init__(self, movie_name: str, episodes: list[NSEpisode], parent=None):
        """Build the episode checklist UI."""
        super().__init__(parent)
        self.episodes = episodes
        self.setWindowTitle(f"Chon tap - {movie_name}")
        self.resize(500, 600)

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            f"<b>{movie_name}</b> - tong {len(episodes)} tap. Tick de chon:"
        )
        layout.addWidget(info)

        btn_row = QtWidgets.QHBoxLayout()
        self.select_all_btn = QtWidgets.QPushButton("Chon tat ca")
        self.deselect_all_btn = QtWidgets.QPushButton("Bo chon tat ca")
        self.select_all_btn.clicked.connect(lambda: self._toggle_all(True))
        self.deselect_all_btn.clicked.connect(lambda: self._toggle_all(False))
        btn_row.addWidget(self.select_all_btn)
        btn_row.addWidget(self.deselect_all_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search tap (VD: 10-20, 5, 15)...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list_widget = QtWidgets.QListWidget()
        for ep in episodes:
            label = f"Tap {ep.episode}"
            if ep.name and ep.name != movie_name:
                label += f" - {ep.name}"
            item = QtWidgets.QListWidgetItem(label)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, ep)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, stretch=1)

        self.count_label = QtWidgets.QLabel()
        self._update_count()
        self.list_widget.itemChanged.connect(self._update_count)
        layout.addWidget(self.count_label)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Add")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _toggle_all(self, check: bool):
        """Check or uncheck all visible episodes in the list."""
        state = (QtCore.Qt.CheckState.Checked if check
                 else QtCore.Qt.CheckState.Unchecked)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    def _filter(self, text: str):
        """Filter visible episodes by number or range (e.g. '5', '10-20')."""
        text = text.strip().lower()
        ranges = []
        for part in re.split(r"[,\s]+", text):
            if not part:
                continue
            m = re.match(r"^(\d+)-(\d+)$", part)
            if m:
                ranges.append((int(m.group(1)), int(m.group(2))))
            elif part.isdigit():
                ranges.append((int(part), int(part)))

        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            ep: NSEpisode = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not text:
                item.setHidden(False)
            else:
                visible = any(lo <= ep.episode <= hi for lo, hi in ranges)
                item.setHidden(not visible)

    def _update_count(self):
        """Refresh the 'selected N/total' label below the list."""
        n = sum(
            1 for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == QtCore.Qt.CheckState.Checked
        )
        self.count_label.setText(f"Da chon: {n}/{self.list_widget.count()}")

    def get_selected_episodes(self) -> list[NSEpisode]:
        """Sync checkbox states back to NSEpisode.selected and return the full list."""
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            ep: NSEpisode = item.data(QtCore.Qt.ItemDataRole.UserRole)
            ep.selected = (
                item.checkState() == QtCore.Qt.CheckState.Checked
            )
        return self.episodes


# ============================================================================
# PASTE JSON DIALOG
# ============================================================================


class NSPasteJsonDialog(QtWidgets.QDialog):
    """Dialog for pasting raw JSON API response text."""

    def __init__(self, parent=None):
        """Build a simple text-area dialog for JSON input."""
        super().__init__(parent)
        self.setWindowTitle("Paste JSON")
        self.resize(700, 500)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Dan JSON response tu API (hoac object {success, data: [...]}):"
        ))
        self.text = QtWidgets.QTextEdit()
        self.text.setFont(QtGui.QFont("Consolas", 10))
        layout.addWidget(self.text)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_json(self) -> Optional[dict | list]:
        """Parse and return the typed JSON; show a warning dialog on decode error."""
        try:
            return json.loads(self.text.toPlainText())
        except json.JSONDecodeError as e:
            QtWidgets.QMessageBox.warning(self, "Loi",
                                         f"JSON khong hop le: {e}")
            return None


# ============================================================================
# MAIN WINDOW  (merged yt-dlp + NetShort)
# ============================================================================


class MainWindow(QtWidgets.QMainWindow, Ui_MainWindow):
    """Main application window combining the yt-dlp tab and NetShort mode."""

    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.setWindowIcon(QtGui.QIcon(str(ROOT / "assets" / "yt-dlp-gui.ico")))
        self.setMinimumWidth(1100)
        self.load_config()
        self.connect_ui()

        # --- Build NetShort UI ---
        ns_widget = self._build_netshort_ui()
        self.setCentralWidget(ns_widget)

        self._load_netshort_settings()
        self._check_netshort_ffmpeg()
        self.show()

        self.dep_worker = DepWorker(self.config["general"]["update_ytdlp"])
        self.dep_worker.finished.connect(self.on_dep_finished)
        self.dep_worker.progress.connect(self.on_dep_progress)
        self.dep_worker.start()

        self.to_dl = {}
        self.workers = {}
        self.nsmovies: list[NSMovie] = []
        self.nsworker: Optional[NSDownloadMergeWorker] = None
        self.nsfetch_worker: Optional[NSFetchWorker] = None
        self.index = 0
        self._ns_iterator = None
        self._sub_dialogs = {}

        self.updater = Updater(self)
        self.updater.check(silent=True)

    # -------------------------------------------------------------------------
    # NetShort UI setup
    # -------------------------------------------------------------------------

    def _build_netshort_ui(self) -> QtWidgets.QWidget:
        """Create and return the complete NetShort tab widget with all controls."""
        self.ns_tab = QtWidgets.QWidget()
        ns_layout = QtWidgets.QVBoxLayout(self.ns_tab)

        cfg = QtWidgets.QGroupBox("Cau hinh")
        cfg_layout = QtWidgets.QFormLayout(cfg)

        save_row = QtWidgets.QHBoxLayout()
        self.ns_save_dir_edit = QtWidgets.QLineEdit()
        self.ns_save_dir_edit.setPlaceholderText("Chon thu muc luu...")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._ns_browse_save_dir)
        save_row.addWidget(self.ns_save_dir_edit)
        save_row.addWidget(browse_btn)
        cfg_layout.addRow("Thu muc luu:", save_row)

        self.ns_api_url_edit = QtWidgets.QLineEdit(DEFAULT_API_URL)
        self.ns_api_url_edit.setPlaceholderText(
            "https://api.xemshort.top/allepisode?shortPlayId={movie_id}"
        )
        cfg_layout.addRow("API endpoint:", self.ns_api_url_edit)

        options_row = QtWidgets.QHBoxLayout()
        self.ns_concurrency_spin = QtWidgets.QSpinBox()
        self.ns_concurrency_spin.setRange(1, 16)
        self.ns_concurrency_spin.setValue(4)
        options_row.addWidget(QtWidgets.QLabel("Luong:"))
        options_row.addWidget(self.ns_concurrency_spin)
        options_row.addSpacing(10)
        self.ns_sub_checkbox = QtWidgets.QCheckBox("Tai phu de")
        self.ns_sub_checkbox.setChecked(True)
        options_row.addWidget(self.ns_sub_checkbox)
        self.ns_merge_checkbox = QtWidgets.QCheckBox("Hardcode sub (merge)")
        self.ns_merge_checkbox.setChecked(True)
        options_row.addWidget(self.ns_merge_checkbox)
        options_row.addSpacing(10)
        self.ns_crf_spin = QtWidgets.QSpinBox()
        self.ns_crf_spin.setRange(18, 28)
        self.ns_crf_spin.setValue(22)
        self.ns_crf_spin.setToolTip("CRF: 18=chat luong cao, 28=nho hon")
        options_row.addWidget(QtWidgets.QLabel("CRF:"))
        options_row.addWidget(self.ns_crf_spin)
        options_row.addStretch()
        cfg_layout.addRow("Tuy chon:", options_row)

        # Sub style row
        sub_style_row = QtWidgets.QHBoxLayout()
        sub_style_row.addWidget(QtWidgets.QLabel("Font:"))
        self.ns_sub_font_combo = QtWidgets.QComboBox()
        self.ns_sub_font_combo.setEditable(True)
        _fonts_dir = Path(__file__).parent / "fonts"
        _bundled = _ns_load_bundled_fonts(_fonts_dir) if _fonts_dir.exists() else []
        _system = []
        for _f in _bundled + [f for f in _system if f not in _bundled]:
            self.ns_sub_font_combo.addItem(_f)
        self.ns_sub_font_combo.setCurrentText(_bundled[0] if _bundled else "Arial")
        self.ns_sub_font_combo.setMinimumWidth(180)
        self.ns_sub_font_combo.setToolTip(
            "Font chu cho phu de.\n"
            "Font trong thu muc fonts/ se tu dong cai khi merge.\n"
            "Co the go ten font bat ky hoac chon tu danh sach."
        )
        sub_style_row.addWidget(self.ns_sub_font_combo)
        sub_style_row.addSpacing(16)
        sub_style_row.addWidget(QtWidgets.QLabel("Size:"))
        self.ns_sub_size_spin = QtWidgets.QSpinBox()
        self.ns_sub_size_spin.setRange(12, 80)
        self.ns_sub_size_spin.setValue(20)
        self.ns_sub_size_spin.setToolTip("Co chu phu de (khuyen nghi: 18-28)")
        sub_style_row.addWidget(self.ns_sub_size_spin)
        sub_style_row.addSpacing(16)
        sub_style_row.addWidget(QtWidgets.QLabel("MarginV:"))
        self.ns_sub_margin_v_spin = QtWidgets.QSpinBox()
        self.ns_sub_margin_v_spin.setRange(0, 300)
        self.ns_sub_margin_v_spin.setValue(30)
        self.ns_sub_margin_v_spin.setToolTip(
            "Vi tri sub theo chieu doc (MarginV).\n"
            "0 = sat mep duoi, tang de day sub len cao hon.\n"
            "Mac dinh: 30"
        )
        sub_style_row.addWidget(self.ns_sub_margin_v_spin)
        sub_style_row.addStretch()
        cfg_layout.addRow("Sub style:", sub_style_row)

        ns_layout.addWidget(cfg)

        inp = QtWidgets.QGroupBox("Them phim")
        inp_layout = QtWidgets.QHBoxLayout(inp)

        inp_layout.addWidget(QtWidgets.QLabel("Movie ID:"))
        self.ns_movie_id_edit = QtWidgets.QLineEdit()
        self.ns_movie_id_edit.setPlaceholderText("VD: 2041732413888921612")
        inp_layout.addWidget(self.ns_movie_id_edit, stretch=1)

        self.ns_fetch_btn = QtWidgets.QPushButton("Fetch Data")
        self.ns_fetch_btn.clicked.connect(self._ns_on_fetch)
        inp_layout.addWidget(self.ns_fetch_btn)

        self.ns_paste_btn = QtWidgets.QPushButton("Paste JSON")
        self.ns_paste_btn.clicked.connect(self._ns_on_paste_json)
        inp_layout.addWidget(self.ns_paste_btn)

        self.ns_load_btn = QtWidgets.QPushButton("Load JSON File")
        self.ns_load_btn.clicked.connect(self._ns_on_load_json)
        inp_layout.addWidget(self.ns_load_btn)

        ns_layout.addWidget(inp)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        ns_table_widget = QtWidgets.QWidget()
        ns_table_layout = QtWidgets.QVBoxLayout(ns_table_widget)
        ns_table_layout.setContentsMargins(0, 0, 0, 0)

        ns_table_header = QtWidgets.QHBoxLayout()
        ns_table_header.addWidget(QtWidgets.QLabel("<b>Danh sach phim da them</b>"))
        ns_table_header.addStretch()
        self.ns_start_btn = QtWidgets.QPushButton("Start Download & Merge")
        self.ns_start_btn.clicked.connect(self._ns_on_start)
        self.ns_start_btn.setEnabled(False)
        self.ns_start_btn.setStyleSheet(
            "QPushButton { background-color: #22c55e; color: white; font-weight: bold; padding: 6px 12px; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #9ca3af; }"
        )
        ns_table_header.addWidget(self.ns_start_btn)
        self.ns_stop_btn = QtWidgets.QPushButton("Stop")
        self.ns_stop_btn.clicked.connect(self._ns_on_stop)
        self.ns_stop_btn.setEnabled(False)
        self.ns_stop_btn.setStyleSheet(
            "QPushButton { background-color: #ef4444; color: white; font-weight: bold; padding: 6px 12px; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #9ca3af; }"
        )
        ns_table_header.addWidget(self.ns_stop_btn)
        ns_table_layout.addLayout(ns_table_header)

        self.ns_table = QtWidgets.QTableWidget(0, 5)
        self.ns_table.setHorizontalHeaderLabels(
            ["Name", "Tap", "Chon", "Trang thai", "Actions"]
        )
        self.ns_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.ns_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.ns_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.ns_table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.ns_table.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.ns_table.verticalHeader().setVisible(False)
        ns_table_layout.addWidget(self.ns_table)

        splitter.addWidget(ns_table_widget)

        log_widget = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_widget)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QtWidgets.QLabel("<b>Log</b>"))
        self.ns_log_text = QtWidgets.QTextEdit()
        self.ns_log_text.setReadOnly(True)
        self.ns_log_text.setFont(QtGui.QFont("Consolas", 9))
        self.ns_log_text.setStyleSheet("background:#1a1a2e;color:#00ff00;")
        log_layout.addWidget(self.ns_log_text)
        splitter.addWidget(log_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        ns_layout.addWidget(splitter, stretch=1)

        ns_progress_row = QtWidgets.QHBoxLayout()
        self.ns_progress_bar = QtWidgets.QProgressBar()
        self.ns_progress_bar.setTextVisible(True)
        ns_progress_row.addWidget(self.ns_progress_bar)
        ns_layout.addLayout(ns_progress_row)

        self.ns_status = QtWidgets.QLabel("San sang.")
        self.ns_status.setStyleSheet("color: #888; font-size: 11px; padding-left: 4px;")
        ns_layout.addWidget(self.ns_status)

        return self.ns_tab

    def _check_netshort_ffmpeg(self):
        """Warn user and disable the merge checkbox if ffmpeg is not found."""
        if not _ns_check_ffmpeg():
            QtWidgets.QMessageBox.warning(
                self, "ffmpeg",
                "Khong tim thay ffmpeg trong PATH.\n"
                "Chuc nang merge se bi disable. "
                "Tai tai https://ffmpeg.org/download.html."
            )
            self.ns_merge_checkbox.setChecked(False)
            self.ns_merge_checkbox.setEnabled(False)

    def _ns_browse_save_dir(self):
        """Open a folder picker and populate the save directory field."""
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Chon thu muc luu",
            self.ns_save_dir_edit.text() or str(Path.home())
        )
        if d:
            self.ns_save_dir_edit.setText(d)

    # -------------------------------------------------------------------------
    # yt-dlp UI handlers (existing)
    # -------------------------------------------------------------------------

    def connect_ui(self):
        """Wire all yt-dlp tab buttons and menu actions to their handler slots."""
        self.pb_path.clicked.connect(self.button_path)
        self.pb_add.clicked.connect(self.button_add)
        self.pb_clear.clicked.connect(self.button_clear)
        self.pb_download.clicked.connect(self.button_download)

        self.action_open_bin_folder.triggered.connect(
            lambda: self.open_folder(BIN_DIR)
        )
        self.action_open_log_folder.triggered.connect(
            lambda: self.open_folder(ROOT)
        )
        self.action_exit.triggered.connect(self.close)
        self.action_about.triggered.connect(self.show_about)
        self.action_help.triggered.connect(self.show_help)
        self.action_clear_url_list.triggered.connect(self.te_link.clear)
        self.action_load_txt.triggered.connect(self.button_load_txt)
        self.action_check_update.triggered.connect(self._on_check_update)

    def on_dep_progress(self, status):
        """Show dependency download status in the status bar."""
        self.statusBar.showMessage(status, 10000)

    def on_dep_finished(self):
        """Clean up the dep worker and re-enable the download button."""
        self.dep_worker.deleteLater()
        try:
            self.pb_download.setEnabled(True)
        except RuntimeError:
            pass

    def open_folder(self, path):
        """Open a directory in the OS file explorer."""
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def show_about(self):
        """Display the About dialog with version and project info."""
        QtWidgets.QMessageBox.about(
            self,
            "About Tool Download Movie Pro",
            f'<a href="https://github.com/dsymbol/yt-dlp-gui">Tool Download Movie Pro</a> {__version__}<br><br>'
            "Phan mem tai phim, video tu nhieu nguon.<br>"
            "NetShort mode: tai phim tu xemshort.top.",
        )

    def show_help(self):
        """Display the usage guide dialog."""
        help_text = (
            "<b>Huong dan su dung Tool Download Movie Pro</b><br><br>"
            "<b>1. Tim Movie ID:</b><br>"
            "- Truy cap <a href='https://xemshort.top'>https://xemshort.top</a><br>"
            "- Tim phim muon tai, mo trang phim<br>"
            "- Copy Movie ID tu URL (vi du: xemshort.top/phim/ten-phim-<b>2043519588162863105</b>.html)<br><br>"
            "<b>2. Tai phim:</b><br>"
            "- Dan Movie ID vao o \"Movie ID\"<br>"
            "- Nhan \"Fetch\" de lay danh sach tap<br>"
            "- Chon preset (mp4/webm/best) va thu muc luu<br>"
            "- Nhan \"Start Download\" de tai<br><br>"
            "<b>3. Tai phu de & Auto Merged Sub:</b><br>"
            "- <b>Tai phu de:</b> Tai file phu de (vn/en)<br>"
            "- <b>Auto Merged Sub:</b> Mac dinh luon bat - khi chon \"Tai phu de\", "
            "ban <b>phai tick</b> checkbox \"Auto Merged Sub\" truoc khi nhan Start.<br>"
            "- Neu khong tick \"Auto Merged Sub\" khi tai phu de, phim se khong duoc ghep phu de.<br>"
            "- <b>Hardcode sub (merge):</b> Burn phu de vao video (tat ca trong 1 file)<br><br>"
            "<b>4. Cac tinh nang khac:</b><br>"
            "- <b>Load JSON File:</b> Tai file JSON da luu truoc do<br>"
            "- <b>Paste JSON:</b> Dan truc tiep noi dung JSON<br><br>"
            "<b>5. Meo:</b><br>"
            "- Chon preset \"mp4\" de tuong thich tot nhat<br>"
            "- Neu phim co phu de, can tick ca \"Tai phu de\" va \"Auto Merged Sub\"<br>"
            "- Tick \"Hardcode sub (merge)\" de burn phu de vao video<br><br>"
            f"<b>Version:</b> {__version__}"
        )

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Huong dan su dung")
        msg.setText(help_text)
        msg.setTextFormat(QtCore.Qt.RichText)
        msg.setStandardButtons(QtWidgets.QMessageBox.Ok)
        msg.exec()

    def _on_check_update(self):
        """Called when the user selects 'Kiem tra cap nhat' from the Help menu."""
        self.updater.check(silent=False)

    def open_menu(self, position):
        """Show right-click context menu on the download tree (Delete / Copy URL / Open Folder)."""
        menu = QtWidgets.QMenu()

        delete_action = menu.addAction(qta.icon("mdi6.trash-can"), "Delete")
        copy_url_action = menu.addAction(qta.icon("mdi6.content-copy"), "Copy URL")
        open_folder_action = menu.addAction(
            qta.icon("mdi6.folder-open"), "Open Folder"
        )

        item = self.tw.itemAt(position)

        if item:
            item_path = item.data(0, ItemRoles.PathRole)
            item_link = item.data(0, ItemRoles.LinkRole)
            action = menu.exec(self.tw.viewport().mapToGlobal(position))

            if action == delete_action:
                self.remove_item(item, 0)
            elif action == copy_url_action:
                QtWidgets.QApplication.clipboard().setText(item_link)
                logger.info(f"Copied URL to clipboard: {item_link}")
            elif action == open_folder_action:
                self.open_folder(item_path)
                logger.info(f"Opened folder: {item_path}")

    def remove_item(self, item, column):
        """Stop the worker for an item (if running) and remove it from the tree."""
        item_id = item.data(0, ItemRoles.IdRole)
        item_text = item.text(0)

        logger.debug(f"Removing download ({item_id}): {item_text}")

        if worker := self.workers.get(item_id):
            worker.stop()

        self.to_dl.pop(item_id, None)
        self.tw.takeTopLevelItem(
            self.tw.indexOfTopLevelItem(item)
        )

    def button_path(self):
        """Open a folder picker and set the output path field."""
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select a folder",
            self.le_path.text() or QtCore.QDir.homePath(),
            QtWidgets.QFileDialog.Option.ShowDirsOnly,
        )

        if path:
            self.le_path.setText(path)

    def button_add(self):
        """Validate inputs, create a DownloadWorker per URL, and add each to the queue."""
        missing = []
        preset = self.dd_preset.currentText()
        links = self.te_link.toPlainText()
        path = self.le_path.text()
        sub_url = self.le_sub_url.text().strip()

        if not links:
            missing.append("Video URL")
        if not path:
            missing.append("Save to")

        if missing:
            missing_fields = ", ".join(missing)
            return QtWidgets.QMessageBox.information(
                self,
                "Application Message",
                f"Required field{'s' if len(missing) > 1 else ''} ({missing_fields}) missing.",
            )

        self.te_link.clear()
        self.le_sub_url.clear()

        for link in links.split("\n"):
            link = link.strip()
            item = QtWidgets.QTreeWidgetItem(
                self.tw, [link, preset, "-", "", "Queued", "-", "-"]
            )
            pb = QtWidgets.QProgressBar()
            pb.setStyleSheet("QProgressBar { margin-bottom: 3px; }")
            pb.setTextVisible(False)
            self.tw.setItemWidget(item, 3, pb)
            [
                item.setTextAlignment(i, QtCore.Qt.AlignmentFlag.AlignCenter)
                for i in range(1, 6)
            ]
            item.setData(0, ItemRoles.IdRole, self.index)
            item.setData(0, ItemRoles.LinkRole, link)
            item.setData(0, ItemRoles.PathRole, path)

            worker = DownloadWorker(
                item, self.config, link, path, preset, sub_url
            )
            self.to_dl[self.index] = worker
            logger.info(f"Queued download ({self.index}) added {link}")
            self.index += 1

    def button_load_txt(self):
        """Load a batch .txt file (VIDEO=/SUB=/--- format) and queue all entries."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open .txt file",
            "",
            "Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error",
                                           f"Cannot read file:\n{e}")
            return

        items = []
        current_video = None
        current_sub = ""
        path_dir = self.le_path.text()

        for line in lines:
            line = line.strip()
            if line.startswith("VIDEO="):
                current_video = line[6:].strip()
            elif line.startswith("SUB="):
                current_sub = line[4:].strip()
            elif line == "---":
                if current_video:
                    items.append((current_video, current_sub))
                current_video = None
                current_sub = ""

        if current_video:
            items.append((current_video, current_sub))

        if not items:
            QtWidgets.QMessageBox.information(
                self, "Info", "No valid VIDEO= entries found in the file."
            )
            return

        preset = self.dd_preset.currentText()
        added = 0

        for video_url, sub_url in items:
            item = QtWidgets.QTreeWidgetItem(
                self.tw, [video_url, preset, "-", "", "Queued", "-", "-"]
            )
            pb = QtWidgets.QProgressBar()
            pb.setStyleSheet("QProgressBar { margin-bottom: 3px; }")
            pb.setTextVisible(False)
            self.tw.setItemWidget(item, 3, pb)
            [
                item.setTextAlignment(i, QtCore.Qt.AlignmentFlag.AlignCenter)
                for i in range(1, 6)
            ]
            item.setData(0, ItemRoles.IdRole, self.index)
            item.setData(0, ItemRoles.LinkRole, video_url)
            item.setData(0, ItemRoles.PathRole, path_dir)

            worker = DownloadWorker(
                item, self.config, video_url, path_dir, preset, sub_url
            )
            self.to_dl[self.index] = worker
            logger.info(f"Batch queued ({self.index}): {video_url}")
            self.index += 1
            added += 1

        self.statusBar.showMessage(f"Da them {added} tap vao queue", 5000)

    def button_clear(self):
        """Clear the download queue and tree; blocked if downloads are in progress."""
        if self.workers:
            return QtWidgets.QMessageBox.critical(
                self,
                "Application Message",
                "Unable to clear list because there are active downloads in progress.\n"
                "Remove a download by right clicking on it and selecting delete.",
            )

        self.workers = {}
        self.to_dl = {}
        self.tw.clear()

    def button_download(self):
        """Auto-add any pending URL text, then start all queued DownloadWorkers."""
        if self.te_link.toPlainText().strip():
            self.button_add()

        if not self.to_dl:
            return QtWidgets.QMessageBox.information(
                self,
                "Application Message",
                "Unable to download because there are no links in the list.",
            )

        for idx, worker in self.to_dl.items():
            self.workers[idx] = worker
            worker.finished.connect(worker.deleteLater)
            worker.finished.connect(lambda x=idx: self.workers.pop(x))
            worker.progress.connect(self.on_dl_progress)
            worker.start()

        self.to_dl = {}

    def load_config(self):
        """Load config.toml from user data dir (or copy default), populate presets dropdown."""
        bin_dir = BIN_DIR
        bin_dir.mkdir(parents=True, exist_ok=True)
        config_path = bin_dir / "config.toml"

        default_config = {
            "general": {"update_ytdlp": True, "current_preset": 0, "path": ""},
            "presets": {
                "best": "-f bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "mp4": "-f bv*[vcodec^=avc]+ba[ext=m4a]/b",
                "mp3": "--extract-audio --audio-format mp3 --audio-quality 0",
                "xemshort": "-f bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            },
            "update": {
                "github_repo": "minjaedevs/tool_download_merged_video_sub",
                "enabled": True,
            },
        }

        self.config = default_config.copy()

        if config_path.exists():
            try:
                loaded = load_toml(config_path)
                if "general" in loaded and "presets" in loaded:
                    self.config = loaded
                else:
                    logger.warning("Config missing keys, using defaults + loaded values.")
                    self.config["general"].update(loaded.get("general", {}))
                    self.config["presets"].update(loaded.get("presets", {}))
            except Exception:
                logger.warning("Config load failed, using defaults.")
        elif (ROOT / "root" / "config.toml").exists():
            try:
                self.config = load_toml(ROOT / "root" / "config.toml")
                save_toml(config_path, self.config)
                logger.info("Copied default config to user data dir.")
            except Exception:
                logger.warning("Failed to copy default config.")

        update_ytdlp = self.config["general"].get("update_ytdlp")
        self.config["general"]["update_ytdlp"] = (
            update_ytdlp if update_ytdlp else True
        )
        self.dd_preset.addItems(self.config["presets"].keys())
        self.dd_preset.setCurrentIndex(
            self.config["general"].get("current_preset", 0)
        )
        self.le_path.setText(self.config["general"].get("path", ""))

    def on_dl_progress(self, item: QtWidgets.QTreeWidgetItem, emit_data):
        """Receive progress updates from DownloadWorker and update the tree row."""
        try:
            for data in emit_data:
                index, update = data
                logger.debug(
                    f"on_dl_progress: item={item.data(0,ItemRoles.IdRole)} "
                    f"index={index} update={repr(str(update)[:50])}"
                )
                if index == 3:
                    pb = self.tw.itemWidget(item, index)
                    if pb:
                        pb.setValue(round(float(update.replace("%", ""))))
                elif index == 999:
                    item.setData(0, ItemRoles.SubSrtRole, update)
                    logger.debug(f"  -> Stored SRT content len={len(update)}")
                elif index == TreeColumn.SUB:
                    item.setText(index, update)
                    if update:
                        brush = QtGui.QBrush(QtGui.QColor("#60a5fa"))
                        item.setForeground(index, brush)
                    logger.debug(f"  -> Set Sub column to: {update}")
                elif index != 3:
                    item.setText(index, update)
        except AttributeError:
            logger.info(
                f"Download ({item.data(0, ItemRoles.IdRole)}) no longer exists"
            )
        except Exception as e:
            logger.error(f"on_dl_progress error: {e}")

    def _on_tw_item_clicked(self, item, col):
        """Open (or focus) the subtitle viewer dialog when the Sub column is clicked."""
        logger.debug(
            f"itemClicked: col={col}, SUB={TreeColumn.SUB}, "
            f"match={col == TreeColumn.SUB}"
        )
        if col != TreeColumn.SUB:
            return
        item_id = item.data(0, ItemRoles.IdRole)
        srt_content = item.data(0, ItemRoles.SubSrtRole)
        logger.debug(
            f"  item_id={item_id}, "
            f"srt_content={type(srt_content).__name__}("
            f"{len(srt_content) if srt_content else 0})"
        )
        if not srt_content:
            return
        if item_id in self._sub_dialogs and self._sub_dialogs[item_id] is not None:
            dlg = self._sub_dialogs[item_id]
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        else:
            dlg = SubViewerDialog(srt_content, self)
            self._sub_dialogs[item_id] = dlg
            dlg.show()

    def closeEvent(self, event):
        """Persist preset selection, output path, and NetShort settings on window close."""
        self.config["general"]["current_preset"] = self.dd_preset.currentIndex()
        self.config["general"]["path"] = self.le_path.text()
        bin_dir = BIN_DIR
        bin_dir.mkdir(parents=True, exist_ok=True)
        save_toml(bin_dir / "config.toml", self.config)
        self._save_netshort_settings()
        event.accept()

    # -------------------------------------------------------------------------
    # NetShort settings persistence
    # -------------------------------------------------------------------------

    def _ns_settings(self) -> QSettings:
        """Return the QSettings instance scoped to the NetShort configuration key."""
        return QSettings(NETSHORT_APP_NAME, NETSHORT_CONFIG_KEY)

    def _load_netshort_settings(self):
        """Restore NetShort UI controls from persisted QSettings."""
        s = self._ns_settings()
        self.ns_save_dir_edit.setText(
            s.value("save_dir", str(Path.home() / "Downloads" / "NetShort"))
        )
        self.ns_api_url_edit.setText(
            s.value("api_url", DEFAULT_API_URL)
        )
        self.ns_concurrency_spin.setValue(
            int(s.value("concurrency", 4))
        )
        self.ns_sub_checkbox.setChecked(
            s.value("download_sub", True, type=bool)
        )
        self.ns_merge_checkbox.setChecked(
            s.value("do_merge", True, type=bool)
        )
        self.ns_crf_spin.setValue(
            int(s.value("crf", 22))
        )
        self.ns_sub_font_combo.setCurrentText(
            s.value("sub_font", "UTM Alter Gothic")
        )
        self.ns_sub_size_spin.setValue(
            int(s.value("sub_size", 20))
        )
        self.ns_sub_margin_v_spin.setValue(
            int(s.value("sub_margin_v", 30))
        )

    def _save_netshort_settings(self):
        """Persist current NetShort UI control values to QSettings."""
        s = self._ns_settings()
        s.setValue("save_dir", self.ns_save_dir_edit.text())
        s.setValue("api_url", self.ns_api_url_edit.text())
        s.setValue("concurrency", self.ns_concurrency_spin.value())
        s.setValue("download_sub", self.ns_sub_checkbox.isChecked())
        s.setValue("do_merge", self.ns_merge_checkbox.isChecked())
        s.setValue("crf", self.ns_crf_spin.value())
        s.setValue("sub_font", self.ns_sub_font_combo.currentText())
        s.setValue("sub_size", self.ns_sub_size_spin.value())
        s.setValue("sub_margin_v", self.ns_sub_margin_v_spin.value())

    # -------------------------------------------------------------------------
    # NetShort UI handlers
    # -------------------------------------------------------------------------

    def _ns_log(self, msg: str):
        """Append a message to the NetShort log panel and auto-scroll to bottom."""
        self.ns_log_text.append(msg)
        self.ns_log_text.verticalScrollBar().setValue(
            self.ns_log_text.verticalScrollBar().maximum()
        )

    def _ns_on_fetch(self):
        """Validate Movie ID and API URL then start NSFetchWorker."""
        movie_id = self.ns_movie_id_edit.text().strip()
        if not movie_id:
            QtWidgets.QMessageBox.warning(self, "Thieu input",
                                          "Vui long nhap Movie ID.")
            return
        api_url = self.ns_api_url_edit.text().strip()
        if not api_url.startswith(("http://", "https://")):
            QtWidgets.QMessageBox.warning(self, "API URL",
                                          "API URL phai bat dau bang http:// hoac https://.")
            return

        self.ns_fetch_btn.setEnabled(False)
        self.ns_status.setText(f"Dang fetch {movie_id}...")
        self._ns_log(f"Fetching {movie_id}...")

        self.nsfetch_worker = NSFetchWorker(api_url, movie_id)
        self.nsfetch_worker.success.connect(self._ns_on_fetch_success)
        self.nsfetch_worker.error.connect(self._ns_on_fetch_error)
        self.nsfetch_worker.finished.connect(
            lambda: self.ns_fetch_btn.setEnabled(True)
        )
        self.nsfetch_worker.start()

    def _ns_on_fetch_success(self, episodes: list[NSEpisode], movie_name: str = ""):
        """Handle successful fetch: update status and open the episode picker."""
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        self.ns_status.setText(f"Fetched {len(episodes)} tap.")
        self._ns_log(f"Fetched {len(episodes)} tap.")
        self._ns_show_picker(episodes, name)

    def _ns_on_fetch_error(self, msg: str):
        """Handle fetch failure: log the error and show a critical message box."""
        self.ns_status.setText("Fetch loi.")
        self._ns_log(f"ERROR: {msg}")
        QtWidgets.QMessageBox.critical(self, "Fetch loi", msg)

    def _ns_on_paste_json(self):
        """Open the Paste JSON dialog and load episodes from the typed JSON."""
        dlg = NSPasteJsonDialog(self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            data = dlg.get_json()
            if data is None:
                return
            try:
                movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
                episodes = _ns_parse_episodes(data, movie_name)
                if not episodes:
                    QtWidgets.QMessageBox.warning(self, "Rong",
                                                  "JSON khong chua episode nao.")
                    return
                self._ns_show_picker(episodes, movie_name)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Parse loi",
                                               f"{type(e).__name__}: {e}")

    def _ns_on_load_json(self):
        """Open a file picker, read a saved JSON file, and load its episodes."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Chon file JSON", "", "JSON (*.json);;All (*)"
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
            episodes = _ns_parse_episodes(data, movie_name)
            if not episodes:
                QtWidgets.QMessageBox.warning(self, "Rong",
                                              "File khong co episode.")
                return
            self._ns_show_picker(episodes, movie_name)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Load loi",
                                           f"{type(e).__name__}: {e}")

    def _ns_show_picker(self, episodes: list[NSEpisode], movie_name: str = ""):
        """Show episode picker dialog; on accept, create an NSMovie and add to table."""
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        dlg = NSEpisodePickerDialog(name, episodes, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            selected = dlg.get_selected_episodes()
            save_dir = Path(
                self.ns_save_dir_edit.text() or "."
            )
            save_dir.mkdir(parents=True, exist_ok=True)
            movie = NSMovie(
                name=name,
                episodes=selected,
                save_dir=save_dir,
            )
            self.nsmovies.append(movie)
            self._ns_add_movie_to_table(movie)
            self.ns_start_btn.setEnabled(True)
            self._ns_log(
                f"Them '{name}' - {movie.selected_count}/{movie.total} tap."
            )

    def _ns_add_movie_to_table(self, movie: NSMovie):
        """Insert a new row for the movie into the queue table with action buttons."""
        row = self.ns_table.rowCount()
        self.ns_table.insertRow(row)

        self.ns_table.setItem(row, 0, QtWidgets.QTableWidgetItem(movie.name))
        self.ns_table.setItem(row, 1,
                              QtWidgets.QTableWidgetItem(str(movie.total)))
        self.ns_table.setItem(row, 2, QtWidgets.QTableWidgetItem(
            str(movie.selected_count)))
        self.ns_table.setItem(row, 3, QtWidgets.QTableWidgetItem("Ready"))
        self._ns_set_status(row, "Ready")

        btn_widget = QtWidgets.QWidget()
        btn_layout = QtWidgets.QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(2, 2, 2, 2)
        btn_layout.setSpacing(4)

        open_btn = QtWidgets.QPushButton("Mo thu muc")
        open_btn.setToolTip("Mo thu muc chua video")
        open_btn.clicked.connect(lambda _, m=movie: self._ns_open_movie_folder(m))
        btn_layout.addWidget(open_btn)

        open_merged_btn = QtWidgets.QPushButton("Mo merged")
        open_merged_btn.setToolTip("Mo thu muc merged/")
        open_merged_btn.clicked.connect(lambda _, m=movie: self._ns_open_merged_folder(m))
        btn_layout.addWidget(open_merged_btn)

        remerge_btn = QtWidgets.QPushButton("Merge lai")
        remerge_btn.setToolTip("Xoa file merged cu va hardcode sub lai")
        remerge_btn.clicked.connect(lambda _, m=movie: self._ns_remerge_movie(m))
        btn_layout.addWidget(remerge_btn)

        movie.remerge_btn = remerge_btn
        self.ns_table.setCellWidget(row, 4, btn_widget)
        self._ns_update_row_btns(movie)

    def _ns_set_status(self, row: int, text: str):
        """Set status cell text and background color based on state."""
        item = self.ns_table.item(row, 3)
        if item is None:
            return
        item.setText(text)
        tl = text.lower()
        if tl.startswith("done"):
            bg = QtGui.QColor("#d4edda")
            fg = QtGui.QColor("#155724")
        elif "error" in tl:
            bg = QtGui.QColor("#f8d7da")
            fg = QtGui.QColor("#721c24")
        elif tl == "ready":
            bg = QtGui.QColor("#fff3cd")
            fg = QtGui.QColor("#856404")
        else:
            bg = QtGui.QColor("#d1ecf1")
            fg = QtGui.QColor("#0c5460")
        item.setBackground(QtGui.QBrush(bg))
        item.setForeground(QtGui.QBrush(fg))

    def _ns_update_row_btns(self, movie: NSMovie):
        """Update visibility of per-row action buttons."""
        worker_running = self.nsworker and self.nsworker.isRunning()
        has_done = any(e.selected and e.status == "done" for e in movie.episodes)
        if hasattr(movie, "remerge_btn"):
            movie.remerge_btn.setVisible(not worker_running and has_done)

    def _ns_remove_movie(self, movie: NSMovie):
        """Remove a movie from the queue list and its corresponding table row."""
        if movie in self.nsmovies:
            idx = self.nsmovies.index(movie)
            self.nsmovies.remove(movie)
            if hasattr(movie, "remerge_btn"):
                del movie.remerge_btn
            self.ns_table.removeRow(idx)
            if not self.nsmovies:
                self.ns_start_btn.setEnabled(False)

    def _ns_open_movie_folder(self, movie: NSMovie):
        """Open the movie's save folder in the OS file explorer."""
        folder = movie.save_dir / movie.folder_name
        folder.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    def _ns_open_merged_folder(self, movie: NSMovie):
        """Open the merged/ sub-folder in the OS file explorer."""
        folder = movie.save_dir / movie.folder_name / "merged"
        folder.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    def _ns_remerge_movie(self, movie: NSMovie):
        """Delete merged files for done episodes and re-run the merge phase."""
        if self.nsworker and self.nsworker.isRunning():
            QtWidgets.QMessageBox.warning(
                self, "Dang chay",
                "Vui long doi tien trinh hien tai hoan tat truoc khi re-merge."
            )
            return

        reset_count = 0
        for ep in movie.episodes:
            if ep.selected and ep.status == "done":
                if ep.merged_path and ep.merged_path.exists():
                    try:
                        ep.merged_path.unlink()
                    except Exception:
                        pass
                ep.merged_path = None
                ep.status = "pending"
                reset_count += 1

        if reset_count == 0:
            QtWidgets.QMessageBox.information(
                self, "Re-merge",
                "Khong co tap nao trang thai 'done' de re-merge.\n"
                "Chi re-merge duoc khi tap da 'done'."
            )
            return

        if movie in self.nsmovies:
            row = self.nsmovies.index(movie)
            self._ns_set_status(row, "Ready")

        self._ns_log(f"Re-merge '{movie.name}': reset {reset_count} tap, bat dau lai...")
        self._ns_on_start()

    def _ns_on_start(self):
        """Collect movies with pending/error episodes and kick off sequential processing."""
        pending = [
            m for m in self.nsmovies
            if any(e.status in ("pending", "error")
                   for e in m.episodes if e.selected)
        ]
        if not pending:
            for m in self.nsmovies:
                self._ns_update_row_btns(m)
            QtWidgets.QMessageBox.information(
                self, "Hoan tat",
                "Khong co phim nao can tai."
            )
            return

        self.ns_start_btn.setEnabled(False)
        self.ns_stop_btn.setEnabled(True)
        self.ns_fetch_btn.setEnabled(False)
        self.ns_progress_bar.setValue(0)

        for m in self.nsmovies:
            self._ns_update_row_btns(m)

        self._ns_run_next_movie(iter(pending))

    def _ns_run_next_movie(self, iterator):
        """Advance to the next movie in the iterator and start its worker; show Done on finish."""
        try:
            movie = next(iterator)
        except StopIteration:
            self._ns_log("=== TAT CA HOAN TAT ===")
            self.ns_status.setText("Hoan tat.")
            self.ns_start_btn.setEnabled(True)
            self.ns_stop_btn.setEnabled(False)
            self.ns_fetch_btn.setEnabled(True)
            self.ns_progress_bar.setValue(100)
            QtWidgets.QMessageBox.information(
                self, "Done",
                "Da hoan thanh tat ca phim trong bang."
            )
            return

        row = self.nsmovies.index(movie)
        self._ns_set_status(row, "Running...")

        self.nsworker = NSDownloadMergeWorker(
            movie,
            concurrency=self.ns_concurrency_spin.value(),
            download_sub=self.ns_sub_checkbox.isChecked(),
            do_merge=self.ns_merge_checkbox.isChecked(),
            crf=self.ns_crf_spin.value(),
            preset="fast",
            sub_font=self.ns_sub_font_combo.currentText(),
            sub_size=self.ns_sub_size_spin.value(),
            sub_margin_v=self.ns_sub_margin_v_spin.value(),
        )
        self.nsworker.log_msg.connect(self._ns_log)
        self.nsworker.progress.connect(self._ns_on_progress)
        self.nsworker.episode_status.connect(
            lambda ep_num, st, m=movie, r=row: self._ns_on_episode_status(m, r, ep_num, st)
        )
        self.nsworker.finished_all.connect(
            lambda m=movie: self._ns_on_movie_done(m)
        )
        self._ns_iterator = iterator
        self.nsworker.start()

    def _ns_on_progress(self, done: int, total: int):
        """Update the progress bar with current done/total step count."""
        pct = int(done / total * 100) if total else 0
        self.ns_progress_bar.setValue(pct)
        self.ns_progress_bar.setFormat(f"{done}/{total} ({pct}%)")

    def _ns_on_episode_status(self, movie: NSMovie, row: int,
                               ep_num: int, status: str):
        """Update the movie row status cell with the latest episode state."""
        done_count = sum(
            1 for e in movie.episodes
            if e.selected and e.status == "done"
        )
        total_sel = movie.selected_count
        self._ns_set_status(row, f"{status} ({done_count}/{total_sel})")

    def _ns_on_movie_done(self, movie: NSMovie):
        """Mark movie row as Done and advance the iterator to the next movie."""
        if movie not in self.nsmovies:
            return
        row = self.nsmovies.index(movie)
        ok = sum(
            1 for e in movie.episodes
            if e.selected and e.status == "done"
        )
        total = movie.selected_count
        self._ns_set_status(row, f"Done {ok}/{total}")
        self._ns_update_row_btns(movie)

        if self._ns_iterator is None:
            return
        iterator = self._ns_iterator
        self._ns_iterator = None
        self._ns_run_next_movie(iterator)

    def _ns_on_stop(self):
        """Signal the active worker to stop and reset the UI button states."""
        self._ns_log("Dang dung...")
        self._ns_iterator = None
        if self.nsworker and self.nsworker.isRunning():
            self.nsworker.stop()
        self.ns_start_btn.setEnabled(True)
        self.ns_stop_btn.setEnabled(False)
        self.ns_fetch_btn.setEnabled(True)


# ============================================================================
# ENTRY
# ============================================================================


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Tool Download Movie Pro")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
