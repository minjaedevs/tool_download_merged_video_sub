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

def _get_time_greeting() -> str:
    """Return a Vietnamese greeting based on the current hour of day."""
    h = time.localtime().tm_hour
    if 5 <= h < 12:
        return "Chào buổi sáng"
    if 12 <= h < 18:
        return "Chào buổi chiều"
    return "Chào buổi tối"


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
        self.setWindowTitle("Phụ Đề")
        self.setMinimumSize(700, 500)
        self.resize(750, 550)
        self.setModal(False)

        layout = QtWidgets.QVBoxLayout(self)

        toolbar = QtWidgets.QHBoxLayout()
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Tìm kiếm...")
        self.search_input.textChanged.connect(self._do_search)
        self.btn_prev = QtWidgets.QPushButton("<")
        self.btn_prev.setFixedWidth(30)
        self.btn_prev.clicked.connect(self._prev_match)
        self.btn_next = QtWidgets.QPushButton(">")
        self.btn_next.setFixedWidth(30)
        self.btn_next.clicked.connect(self._next_match)
        self.match_label = QtWidgets.QLabel("")
        toolbar.addWidget(QtWidgets.QLabel("Tìm:"))
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
        btn_close = QtWidgets.QPushButton("Đóng")
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
    merge_note: str = ""   # "ok" | "no_sub" | "dur:+Xs" | "error"


@dataclass
class NSMovie:
    """Movie container: holds all episodes and the target save directory."""
    name: str
    episodes: list[NSEpisode] = field(default_factory=list)
    save_dir: Path = field(default_factory=lambda: Path("."))
    start_time: Optional[float] = None
    end_time: Optional[float] = None

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


_COLOR_TO_HEX = {
    "Trắng": "#FFFFFF", "Vàng": "#FFD700", "Xanh dương": "#00BFFF",
    "Đỏ": "#FF6B6B", "Xanh lá": "#00FF7F", "Cam": "#FFA500",
    "Hồng": "#FF69B4", "Tím": "#DA70D6", "Lục": "#90EE90",
    "Xám sáng": "#D3D3D3",
}

def _ns_color_to_ass(color_str: str) -> str:
    """Convert a color name or hex string to ASS &HAABBGGRR format."""
    hex_val = _COLOR_TO_HEX.get(color_str, color_str) if color_str else "#FFFFFF"
    hex_val = hex_val.lstrip("#")
    if len(hex_val) == 6:
        r, g, b = hex_val[0:2], hex_val[2:4], hex_val[4:6]
        return f"&H00{b.upper()}{g.upper()}{r.upper()}"
    return "&H00FFFFFF"


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


def _ns_analyze_vtt(vtt_path: Path) -> dict:
    """Parse a VTT file and return analysis: total cues and short-cue candidates.

    A "short cue" is a cue block with more than 1 subtitle line where at
    least one line has 1-5 words — indicates fragmented/split subtitle
    segments that likely need merging.

    Returns:
        dict with keys: total (int), short (int), total_subs (int)
    """
    try:
        content = vtt_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"total": 0, "short": 0, "total_subs": 0}

    cue_blocks = re.split(r"\n\n+", content.strip())
    total = 0
    short = 0
    total_subs = 0

    for block in cue_blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        ts_match = re.search(
            r"(\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3})",
            lines[0],
        )
        if not ts_match:
            continue
        total += 1
        sub_lines = [
            l.strip()
            for l in lines[1:]
            if l.strip()
            and not l.strip().startswith(("WEBVTT", "NOTE", "STYLE"))
        ]
        for l in sub_lines:
            total_subs += 1
        if len(sub_lines) > 1 and any(1 <= len(l.split()) <= 5 for l in sub_lines):
            short += 1

    return {"total": total, "short": short, "total_subs": total_subs}


# ============================================================================
# NS DETAIL DIALOG
# ============================================================================


def _ns_get_video_duration_secs(path: Path) -> Optional[float]:
    """Get exact video duration in seconds (float) using ffprobe."""
    try:
        result = sp.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _ns_get_video_duration(path: Path) -> Optional[str]:
    """Get video duration using ffprobe. Returns HH:MM:SS string or None on failure."""
    secs = _ns_get_video_duration_secs(path)
    if secs is None:
        return None
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ============================================================================
# NS DETAIL DIALOG
# ============================================================================

class NSDetailDialog(QtWidgets.QDialog):
    """Dialog showing per-episode details: tập phim, video gốc, VTT, video merged, báo cáo."""

    def __init__(self, movie: NSMovie, parent=None):
        super().__init__(parent)
        self.movie = movie
        self.setWindowTitle(f"Chi tiết - {movie.name}")
        self.resize(900, 600)

        layout = QtWidgets.QVBoxLayout(self)

        # Header
        header = QtWidgets.QLabel(
            f"<b>{movie.name}</b> — {movie.selected_count}/{movie.total} tập được chọn"
        )
        layout.addWidget(header)

        # Table: 6 columns
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Tập", "Video gốc", "VTT", "Video Merged", "Action", "Báo cáo"]
        )
        self.table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QtWidgets.QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        layout.addWidget(self.table)

        # Button row
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._populate()
        QtCore.QTimer.singleShot(0, self._resize_rows)

    def _populate(self):
        """Fill table rows for each episode."""
        for ep in self.movie.episodes:
            if not ep.selected:
                continue
            self._add_episode_row(ep)

    def _resize_rows(self):
        """Resize table rows to fit wrapped content after the table is shown."""
        self.table.resizeRowsToContents()

    def _add_episode_row(self, ep: NSEpisode):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # Col 0: Tập
        label = f"Tập {ep.episode}"
        if ep.name and ep.name != self.movie.name:
            label += f" - {ep.name}"
        self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(label))

        # Col 1: Video gốc
        video_item = QtWidgets.QTableWidgetItem("")
        if ep.video_path and ep.video_path.exists():
            video_item.setText(ep.video_path.name)
            video_item.setData(QtCore.Qt.ItemDataRole.UserRole, str(ep.video_path))
            video_item.setForeground(QtGui.QBrush(QtGui.QColor("#16a34a")))
        self.table.setItem(row, 1, video_item)

        # Col 2: VTT
        vtt_item = QtWidgets.QTableWidgetItem("")
        if ep.sub_path and ep.sub_path.exists():
            vtt_item.setText(ep.sub_path.name)
            vtt_item.setData(QtCore.Qt.ItemDataRole.UserRole, str(ep.sub_path))
            video_ok = ep.video_path and ep.video_path.exists()
            color = "#16a34a" if video_ok else "#d97706"
            vtt_item.setForeground(QtGui.QBrush(QtGui.QColor(color)))
        self.table.setItem(row, 2, vtt_item)

        # Col 3: Video Merged
        merged_item = QtWidgets.QTableWidgetItem("")
        if ep.merged_path and ep.merged_path.exists():
            merged_item.setText(ep.merged_path.name)
            merged_item.setData(QtCore.Qt.ItemDataRole.UserRole, str(ep.merged_path))
            merged_item.setForeground(QtGui.QBrush(QtGui.QColor("#16a34a")))
        self.table.setItem(row, 3, merged_item)

        # Col 4: Action (copy / xóa / kiểm tra — visible only when merged exists)
        has_merged = bool(ep.merged_path and ep.merged_path.exists())

        copy_btn = QtWidgets.QPushButton("Copy path")
        copy_btn.setToolTip("Copy đường dẫn file merged")
        copy_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white; padding: 2px 6px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #2563eb; }"
        )
        copy_btn.setVisible(has_merged)
        copy_btn.clicked.connect(lambda _, e=ep: self._copy_merged_path(e))

        del_btn = QtWidgets.QPushButton("Xóa")
        del_btn.setToolTip("Xóa file merged")
        del_btn.setStyleSheet(
            "QPushButton { background-color: #ef4444; color: white; padding: 2px 6px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #dc2626; }"
        )
        del_btn.setVisible(has_merged)
        del_btn.clicked.connect(lambda _, e=ep, r=row: self._delete_merged_file(e, r))

        check_btn = QtWidgets.QPushButton("Kiểm tra")
        check_btn.setToolTip("So sánh thời lượng video merged với video gốc")
        check_btn.setStyleSheet(
            "QPushButton { background-color: #f59e0b; color: white; padding: 2px 6px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #d97706; }"
        )
        check_btn.setVisible(has_merged)
        check_btn.clicked.connect(lambda _, e=ep: self._check_merged_vs_original(e))

        cell_widget = QtWidgets.QWidget()
        cell_layout = QtWidgets.QHBoxLayout(cell_widget)
        cell_layout.setContentsMargins(2, 2, 2, 2)
        cell_layout.setSpacing(3)
        cell_layout.addWidget(copy_btn)
        cell_layout.addWidget(del_btn)
        cell_layout.addWidget(check_btn)
        self.table.setCellWidget(row, 4, cell_widget)

        # Col 5: Báo cáo
        report = self._build_report(ep)
        self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(report))

    def _delete_merged_file(self, ep: NSEpisode, row: int):
        """Xóa file merged và cập nhật lại hàng."""
        reply = QtWidgets.QMessageBox.question(
            self, "Xác nhận xóa",
            f"Xóa file merged:\n{ep.merged_path.name}?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        try:
            ep.merged_path.unlink()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Lỗi", f"Không thể xóa file:\n{e}")
            return
        ep.merged_path = None
        self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(""))
        cell_w = self.table.cellWidget(row, 4)
        if cell_w:
            for btn in cell_w.findChildren(QtWidgets.QPushButton):
                btn.setVisible(False)
        report = self._build_report(ep)
        self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(report))

    def _copy_merged_path(self, ep: NSEpisode):
        """Copy duong dan file merged vao clipboard."""
        if ep.merged_path and ep.merged_path.exists():
            QtWidgets.QApplication.clipboard().setText(str(ep.merged_path))
            QtWidgets.QToolTip.showText(
                QtGui.QCursor.pos(),
                f"Copied: {ep.merged_path.name}",
                None, QtCore.QRect(), 1500,
            )

    def _check_merged_vs_original(self, ep: NSEpisode):
        """So sanh thoi luong video merged voi video goc."""
        orig_dur = None
        merged_dur = None
        orig_secs = None
        merged_secs = None

        if ep.video_path and ep.video_path.exists():
            orig_dur = _ns_get_video_duration(ep.video_path)
        if ep.merged_path and ep.merged_path.exists():
            merged_dur = _ns_get_video_duration(ep.merged_path)

        def to_secs(t):
            try:
                return sum(int(x) * 60 ** i for i, x in enumerate(reversed(t.split(":"))))
            except Exception:
                return None

        if orig_dur:
            orig_secs = to_secs(orig_dur)
        if merged_dur:
            merged_secs = to_secs(merged_dur)

        lines = []
        lines.append(f"Video goc : {orig_dur or '—'}")
        lines.append(f"Video merged: {merged_dur or '—'}")

        if orig_secs is not None and merged_secs is not None:
            diff = merged_secs - orig_secs
            sign = "+" if diff >= 0 else ""
            lines.append(f"Chenh lech : {sign}{diff}s")
            if abs(diff) <= 2:
                lines.append("✅ OK — thoi luong khop (<=2s)")
                icon = QtWidgets.QMessageBox.Information
            else:
                lines.append(f"⚠ Chenh lech {abs(diff)}s — kiem tra lai!")
                icon = QtWidgets.QMessageBox.Warning
        elif not orig_dur:
            lines.append("⚠ Khong doc duoc video goc")
            icon = QtWidgets.QMessageBox.Warning
        elif not merged_dur:
            lines.append("⚠ Khong doc duoc video merged")
            icon = QtWidgets.QMessageBox.Warning
        else:
            icon = QtWidgets.QMessageBox.Question

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle(f"Kiem tra - Tap {ep.episode}")
        msg.setIcon(icon)
        msg.setText("\n".join(lines))
        msg.exec()

    def _build_report(self, ep: NSEpisode) -> str:
        """Build a report string comparing video durations and VTT subtitle analysis."""
        orig_dur = None
        merged_dur = None
        if ep.video_path and ep.video_path.exists():
            orig_dur = _ns_get_video_duration(ep.video_path)
        if ep.merged_path and ep.merged_path.exists():
            merged_dur = _ns_get_video_duration(ep.merged_path)

        # Duration comparison
        dur_label = "—"
        dur_detail = ""
        if orig_dur and merged_dur:
            try:
                orig_secs = sum(int(x) * 60**i for i, x in enumerate(reversed(orig_dur.split(":"))))
                merged_secs = sum(int(x) * 60**i for i, x in enumerate(reversed(merged_dur.split(":"))))
                diff = abs(merged_secs - orig_secs)
                if diff <= 2:
                    dur_label = "OK"
                else:
                    dur_label = "⚠ Chênh lệch"
                dur_detail = f" | Gốc: {orig_dur} | Merge: {merged_dur}"
            except Exception:
                dur_label = "?"
                dur_detail = f" | Gốc: {orig_dur} | Merge: {merged_dur}"
        elif merged_dur:
            dur_label = "?"
            dur_detail = f" | Merge: {merged_dur}"
        elif orig_dur:
            dur_label = "⚠ Chưa merge"
            dur_detail = f" | Gốc: {orig_dur}"

        # VTT analysis
        vtt_label = ""
        if ep.sub_path and ep.sub_path.exists():
            analysis = _ns_analyze_vtt(ep.sub_path)
            if analysis["total"] > 0:
                vtt_label = f" | VTT: {analysis['total']} mốc"
                if analysis["short"] > 0:
                    vtt_label += f", ⚠ {analysis['short']} ngắn"

        return f"{dur_label}{dur_detail}{vtt_label}"

    def _get_parent_window(self) -> Optional[QtWidgets.QWidget]:
        """Return the parent MainWindow for spawning sub-dialogs."""
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, QtWidgets.QMainWindow):
                return p
            p = p.parentWidget()
        return None


# ============================================================================
# NS VTT EDITOR DIALOG
# ============================================================================

class NSVttEditorDialog(QtWidgets.QDialog):
    """Dialog for editing a VTT subtitle file with search and analysis."""

    def __init__(self, vtt_path: Path, parent=None):
        super().__init__(parent)
        self.vtt_path = vtt_path
        self.setWindowTitle(f"Sửa VTT - {vtt_path.name}")
        self.resize(800, 600)

        layout = QtWidgets.QVBoxLayout(self)

        # Toolbar row
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("Tìm:"))
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Tìm kiếm...")
        self.search_input.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self.search_input)
        toolbar.addStretch()

        self.analyze_btn = QtWidgets.QPushButton("Phân tích")
        self.analyze_btn.setStyleSheet(
            "QPushButton { background-color: #6366f1; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #4f46e5; }"
        )
        self.analyze_btn.clicked.connect(self._analyze)
        toolbar.addWidget(self.analyze_btn)
        layout.addLayout(toolbar)

        # Text editor
        self.text_edit = QtWidgets.QTextEdit()
        self.text_edit.setFont(QtGui.QFont("Consolas", 10))
        try:
            content = vtt_path.read_text(encoding="utf-8", errors="replace")
            self.text_edit.setPlainText(content)
        except Exception as e:
            self.text_edit.setPlainText(f"# Không thể đọc file: {e}")
        layout.addWidget(self.text_edit)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        save_btn = QtWidgets.QPushButton("Lưu")
        save_btn.setStyleSheet(
            "QPushButton { background-color: #10b981; color: white; padding: 6px 16px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #059669; }"
        )
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        cancel_btn = QtWidgets.QPushButton("Hủy")
        cancel_btn.setStyleSheet(
            "QPushButton { padding: 6px 16px; border-radius: 4px; font-weight: bold; }"
        )
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _on_search_changed(self, text: str):
        """Clear old highlights first, then apply new ones immediately on each keystroke."""
        self._clear_highlight()
        if not text:
            return
        self._do_highlight(text)

    def _clear_highlight(self):
        """Reset all character formats to default."""
        cursor = QtGui.QTextCursor(self.text_edit.document())
        cursor.select(QtGui.QTextCursor.SelectionType.Document)
        fmt = QtGui.QTextCharFormat()
        fmt.setBackground(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
        fmt.setForeground(QtGui.QBrush(QtCore.Qt.GlobalColor.black))
        cursor.setCharFormat(fmt)
        # Move cursor back to start so user sees top of document
        cursor = QtGui.QTextCursor(self.text_edit.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
        self.text_edit.setTextCursor(cursor)

    def _do_highlight(self, text: str):
        """Highlight all occurrences of text."""
        doc = self.text_edit.document()
        cursor = QtGui.QTextCursor(doc)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)

        highlight_fmt = QtGui.QTextCharFormat()
        highlight_fmt.setBackground(QtGui.QBrush(QtGui.QColor("#fbbf24")))

        while True:
            finder = QtGui.QTextCursor(cursor)
            finder = doc.find(text, finder)
            if finder.isNull():
                break
            finder.setCharFormat(highlight_fmt)
            if finder.position() == cursor.position():
                cursor.setPosition(cursor.position() + 1)
            else:
                cursor = finder

    def _analyze(self):
        """Check all timestamps and show results in a dialog."""
        content = self.text_edit.toPlainText()
        QtWidgets.QApplication.processEvents()

        found = []
        cue_blocks = re.split(r"\n\n+", content)
        for idx, block in enumerate(cue_blocks):
            if idx % 200 == 0:
                QtWidgets.QApplication.processEvents()
            lines = block.strip().splitlines()
            if len(lines) < 2:
                continue
            ts_line = lines[0]
            match = re.search(
                r"(\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3})",
                ts_line,
            )
            if not match:
                continue
            ts_full = match.group(1)
            sub_lines = [
                l.strip()
                for l in lines[1:]
                if l.strip()
                and not l.strip().startswith(("WEBVTT", "NOTE", "STYLE"))
            ]
            # Flag cue: >1 sub line AND at least one line has 1-5 words
            if len(sub_lines) > 1 and any(1 <= len(l.split()) <= 5 for l in sub_lines):
                found.append(
                    f"⏱ {ts_full}\n   Sub: {' | '.join(sub_lines)}"
                )

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Kết quả phân tích VTT")
        dlg.resize(650, 450)
        dlg_layout = QtWidgets.QVBoxLayout(dlg)

        if found:
            header = QtWidgets.QLabel(f"⚠ Tìm thấy {len(found)} mốc có sub ngắn (>1 hàng, có hàng 1-5 từ):")
            header.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 13px;")
            dlg_layout.addWidget(header)
            text_edit = QtWidgets.QTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setFont(QtGui.QFont("Consolas", 10))
            text_edit.setPlainText("\n\n".join(found))
            dlg_layout.addWidget(text_edit)
            if len(found) > 20:
                more_lbl = QtWidgets.QLabel(f"... và {len(found) - 20} mốc khác")
                more_lbl.setStyleSheet("color: #6b7280; font-style: italic;")
                dlg_layout.addWidget(more_lbl)
        else:
            ok_lbl = QtWidgets.QLabel("✅ Không tìm thấy mốc nào cần tách.")
            ok_lbl.setStyleSheet("color: #10b981; font-weight: bold; font-size: 14px;")
            dlg_layout.addWidget(ok_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.clicked.connect(dlg.close)
        btn_row.addWidget(close_btn)
        dlg_layout.addLayout(btn_row)
        dlg.exec()

    def _save(self):
        """Save content back to the VTT file."""
        try:
            self.vtt_path.write_text(
                self.text_edit.toPlainText(), encoding="utf-8"
            )
            QtWidgets.QMessageBox.information(
                self, "Đã lưu", f"Đã lưu file:\n{self.vtt_path.name}"
            )
            self.close()
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Lỗi lưu file", f"Không thể lưu file:\n{e}"
            )


# ============================================================================
# PHONE MOCKUP WIDGET (used by subtitle preview)
# ============================================================================

class _NSPhoneMockup(QtWidgets.QWidget):
    """Draws a rounded phone bezel around a screen pixmap."""

    BEZEL  = 18   # bezel thickness in px
    RADIUS = 28   # outer corner radius

    def __init__(self, screen_pixmap: QtGui.QPixmap, screen_w: int, screen_h: int, parent=None):
        super().__init__(parent)
        self._pix = screen_pixmap
        self._sw  = screen_w
        self._sh  = screen_h
        total_w   = screen_w + self.BEZEL * 2
        total_h   = screen_h + self.BEZEL * 2 + 32  # +32 for home button area
        self.setFixedSize(total_w, total_h)

    def paintEvent(self, event):  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        bz = self.BEZEL
        total_w = self._sw + bz * 2
        total_h = self._sh + bz * 2 + 32

        # Phone body
        body_rect = QtCore.QRectF(0, 0, total_w, total_h)
        painter.setPen(QtGui.QPen(QtGui.QColor("#555"), 1.5))
        painter.setBrush(QtGui.QColor("#222"))
        painter.drawRoundedRect(body_rect, self.RADIUS, self.RADIUS)

        # Screen
        screen_rect = QtCore.QRect(bz, bz, self._sw, self._sh)
        painter.drawPixmap(screen_rect, self._pix)

        # Screen inner border
        painter.setPen(QtGui.QPen(QtGui.QColor("#000"), 1))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(screen_rect)

        # Home button
        cx = total_w // 2
        cy  = self._sh + bz + 16
        painter.setPen(QtGui.QPen(QtGui.QColor("#666"), 1.5))
        painter.setBrush(QtGui.QColor("#333"))
        painter.drawEllipse(cx - 11, cy - 11, 22, 22)

        # Small notch at top
        notch_w, notch_h = 60, 10
        notch_x = (total_w - notch_w) // 2
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#333"))
        painter.drawRoundedRect(notch_x, 4, notch_w, notch_h, 5, 5)

        painter.end()


# ============================================================================
# NS VIDEO POPUP (simple info popup)
# ============================================================================

class NSVideoPopup(QtWidgets.QDialog):
    """Simple popup showing video file info and open button."""

    def __init__(self, video_path: Path, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        size = video_path.stat().st_size
        size_str = f"{size / (1024*1024):.1f} MB"
        duration = _ns_get_video_duration(video_path) or "N/A"

        self.setWindowTitle(f"Video - {video_path.name}")
        self.setMinimumWidth(400)
        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel()
        info.setText(
            f"<b>File:</b> {video_path.name}<br>"
            f"<b>Path:</b> {video_path}<br>"
            f"<b>Size:</b> {size_str}<br>"
            f"<b>Duration:</b> {duration}"
        )
        info.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse)
        layout.addWidget(info)

        btn_row = QtWidgets.QHBoxLayout()
        open_btn = QtWidgets.QPushButton("Mở file")
        open_btn.clicked.connect(self._open_file)
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _open_file(self):
        try:
            os.startfile(self.video_path)
        except Exception:
            try:
                sp.Popen(["xdg-open", str(self.video_path)])
            except Exception:
                QtWidgets.QMessageBox.warning(
                    self, "Lỗi", "Không thể mở file."
                )


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
                f"JSON không hợp lệ: {e}\n"
                f"Preview: {preview}"
            )
            return

        # Check for obfuscated data
        api_data = data.get("data") if isinstance(data, dict) else None
        if isinstance(api_data, str) and len(api_data) > 100:
            self.error.emit(
                "API trả về dữ liệu bị mã hóa.\n"
                "Thử lại hoặc dùng 'Load JSON File' / 'Paste JSON'."
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
                 sub_margin_v: int = 30, sub_color: str = "Trắng"):
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
        self.sub_color = sub_color
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
            self.log(f"SKIP {desc} (đã tồn tại)")
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
                self.log(f"TIMEOUT {desc} (thử {attempt}/{retries})")
                if self._stop.is_set():
                    tmp.unlink(missing_ok=True)
                    return False
                time.sleep(2 * attempt)
                continue
            except requests.exceptions.ConnectionError as e:
                self.log(f"LỖI {desc} (thử {attempt}/{retries}): {e}")
                if self._stop.is_set():
                    tmp.unlink(missing_ok=True)
                    return False
                time.sleep(2 * attempt)
                continue
            except Exception as e:
                self.log(f"LỖI {desc} (thử {attempt}/{retries}): {e}")
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
        dl_ok = self._download_file(ep.play, video_path, f"video tập {ep.episode}")

        # If file already exists (skipped), still mark as downloaded so merge can run
        if video_path.exists() and video_path.stat().st_size > 1024 and ep.status != "error":
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded")
            self.log(f"SKIP video tập {ep.episode} (đã tồn tại)")
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
                self.log(f"SKIP sub tập {ep.episode} (đã tồn tại: {existing_sub.name})")
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
                    self.log(f"sub tập {ep.episode} OK ({ext}, {len(r.content)} bytes)")
                except Exception as e:
                    self.log(f"sub tập {ep.episode} lỗi: {e}")

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
                self.log(f"merge tập {ep.episode} SKIP (đã tồn tại)")
                return True
            self.log(f"tập {ep.episode}: sub mới hơn merged -- re-merge...")

        if not ep.sub_path or not ep.sub_path.exists():
            shutil.copy2(ep.video_path, out_path)
            ep.merged_path = out_path
            ep.merge_note = "no_sub"
            ep.status = "done"
            self.episode_status.emit(ep.episode, "done")
            self.log(f"tập {ep.episode} không có sub -- copy video vào merged/")
            return True

        self.episode_status.emit(ep.episode, "merging")
        self.log(f"merge tập {ep.episode}...")

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
                f"PrimaryColour={_ns_color_to_ass(self.sub_color)},"
                f"OutlineColour=&H00000000,"
                f"BorderStyle=1,Outline=1,Shadow=0,Bold=-1,"
                f"Alignment=2,MarginV={self.sub_margin_v}'"
            )
        else:
            self.log("CẢNH BÁO: không tìm thấy thư mục fonts/ -- dùng font hệ thống")
            vf_filter = (
                f"subtitles='{sub_filter}':force_style="
                f"'FontName={self.sub_font},FontSize={self.sub_size},"
                f"PrimaryColour={_ns_color_to_ass(self.sub_color)},"
                f"OutlineColour=&H00000000,"
                f"BorderStyle=1,Outline=1,Shadow=0,Bold=-1,"
                f"Alignment=2,MarginV={self.sub_margin_v}'"
            )

        # Get exact source duration to pin output length
        orig_secs = _ns_get_video_duration_secs(ep.video_path)

        cmd = [
            str(ffmpeg_path), "-y",
            "-i", str(ep.video_path),
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-preset", self.ffpreset,
            "-crf", str(self.crf),
            "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        if orig_secs is not None:
            cmd += ["-t", f"{orig_secs:.6f}"]
        cmd += ["-loglevel", "warning", str(out_path)]

        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.stderr.strip():
                self.log(f"ffmpeg warning tập {ep.episode}: {result.stderr[:300]}")
            if result.returncode != 0:
                self.log(f"ffmpeg lỗi tập {ep.episode}: {result.stderr[:500]}")
                ep.status = "error"
                ep.merge_note = "error"
                ep.error_msg = "ffmpeg failed"
                self.episode_status.emit(ep.episode, "error")
                return False

            ep.merged_path = out_path
            ep.status = "done"
            self.episode_status.emit(ep.episode, "done")
            self.log(f"merge tập {ep.episode} OK -> {out_path.name}")
            # Duration check
            try:
                orig_dur = _ns_get_video_duration(ep.video_path)
                merged_dur = _ns_get_video_duration(out_path)
                if orig_dur and merged_dur:
                    def _to_secs(t):
                        return sum(int(x) * 60**i for i, x in enumerate(reversed(t.split(":"))))
                    diff = _to_secs(merged_dur) - _to_secs(orig_dur)
                    sign = "+" if diff >= 0 else ""
                    if abs(diff) <= 2:
                        ep.merge_note = "ok"
                        self.log(f"  duration OK: goc={orig_dur} merged={merged_dur}")
                    else:
                        ep.merge_note = f"dur:{sign}{diff}s"
                        self.log(f"  CANH BAO duration: goc={orig_dur} merged={merged_dur} chenh={sign}{diff}s")
                else:
                    ep.merge_note = "ok"
            except Exception:
                ep.merge_note = "ok"
            return True

        except sp.TimeoutExpired:
            self.log(f"merge tập {ep.episode} TIMEOUT")
            ep.status = "error"
            ep.merge_note = "error"
            ep.error_msg = "merge timeout"
            self.episode_status.emit(ep.episode, "error")
            return False
        except Exception as e:
            self.log(f"merge tập {ep.episode} exception: {e}")
            ep.status = "error"
            ep.merge_note = "error"
            ep.error_msg = str(e)
            self.episode_status.emit(ep.episode, "error")
            return False

    def run(self):
        """Entry point: download all selected episodes in parallel, then merge sequentially."""
        selected = [e for e in self.movie.episodes if e.selected]
        total = len(selected)
        if total == 0:
            self.log("Không có tập nào được chọn.")
            self.finished_all.emit()
            return

        self.movie.start_time = time.time()
        self.log(f"=== Bắt đầu tải & merge '{self.movie.name}' ({total} tập) ===")
        self.log(f"Thư mục: {self.movie.save_dir / self.movie.folder_name}")
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
            self.log("Đã dừng.")
            self.finished_all.emit()
            return

        if self.do_merge:
            if not _ns_check_ffmpeg():
                self.log("CẢNH BÁO: không tìm thấy ffmpeg -- bỏ qua merge.")
            else:
                for ep in selected:
                    if self._stop.is_set():
                        break
                    # Merge if downloaded OR if video already exists (re-merge case)
                    if ep.status in ("downloaded", "pending") and ep.video_path and ep.video_path.exists():
                        self._merge_episode(ep)
                        done += 1
                        self.progress.emit(done, total * 2)

        self.movie.end_time = time.time()
        self.log(f"=== Hoàn tất '{self.movie.name}' ===")
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
        self.setWindowTitle(f"Chọn tập - {movie_name}")
        self.resize(500, 600)

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            f"<b>{movie_name}</b> - tổng {len(episodes)} tập. Tick để chọn:"
        )
        layout.addWidget(info)

        btn_row = QtWidgets.QHBoxLayout()
        self.select_all_btn = QtWidgets.QPushButton("Chọn tất cả")
        self.deselect_all_btn = QtWidgets.QPushButton("Bỏ chọn tất cả")
        self.select_all_btn.clicked.connect(lambda: self._toggle_all(True))
        self.deselect_all_btn.clicked.connect(lambda: self._toggle_all(False))
        btn_row.addWidget(self.select_all_btn)
        btn_row.addWidget(self.deselect_all_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Tìm tập (VD: 10-20, 5, 15)...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list_widget = QtWidgets.QListWidget()
        for ep in episodes:
            label = f"Tập {ep.episode}"
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
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Thêm")
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
        self.count_label.setText(f"Đã chọn: {n}/{self.list_widget.count()}")

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
        self.setWindowTitle("Dán JSON")
        self.resize(700, 500)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Dán JSON response từ API (hoặc object {success, data: [...]}):"
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
            QtWidgets.QMessageBox.warning(self, "Lỗi",
                                         "JSON không hợp lệ: {e}")
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

        # --- Build tabs: XemShort (first) + yt-dlp ---
        tab_widget = QtWidgets.QTabWidget()
        tab_widget.setDocumentMode(True)

        # Tab 1: XemShort (NetShort)
        ns_widget = self._build_netshort_ui()
        tab_widget.addTab(ns_widget, "XemShort")

        # Tab 2: yt-dlp (original UI, already built by setupUi)
        yt_dlp_idx = tab_widget.addTab(self.centralwidget, "yt-dlp")
        tab_widget.tabBar().setTabVisible(yt_dlp_idx, False)

        # XemShort is the first tab (index 0) — default on startup
        tab_widget.setCurrentIndex(0)

        self.setCentralWidget(tab_widget)

        self._load_netshort_settings()
        self._check_netshort_ffmpeg()
        self.show()

        self._check_first_launch()
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

        cfg = QtWidgets.QGroupBox("Cấu hình")
        cfg_layout = QtWidgets.QFormLayout(cfg)

        save_row = QtWidgets.QHBoxLayout()
        self.ns_save_dir_edit = QtWidgets.QLineEdit()
        self.ns_save_dir_edit.setPlaceholderText("Chọn thư mục lưu...")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._ns_browse_save_dir)
        save_row.addWidget(self.ns_save_dir_edit)
        save_row.addWidget(browse_btn)
        cfg_layout.addRow("Thư mục lưu:", save_row)

        self.ns_api_url_edit = QtWidgets.QLineEdit(DEFAULT_API_URL)
        self.ns_api_url_edit.setPlaceholderText(
            "https://api.xemshort.top/allepisode?shortPlayId={movie_id}"
        )
        cfg_layout.addRow("API endpoint:", self.ns_api_url_edit)

        options_row = QtWidgets.QHBoxLayout()
        self.ns_concurrency_spin = QtWidgets.QSpinBox()
        self.ns_concurrency_spin.setRange(1, 16)
        self.ns_concurrency_spin.setValue(4)
        options_row.addWidget(QtWidgets.QLabel("Luồng:"))
        options_row.addWidget(self.ns_concurrency_spin)
        options_row.addSpacing(10)
        self.ns_sub_checkbox = QtWidgets.QCheckBox("Tải phụ đề")
        self.ns_sub_checkbox.setChecked(True)
        options_row.addWidget(self.ns_sub_checkbox)
        self.ns_merge_checkbox = QtWidgets.QCheckBox("Hardcode sub (merge)")
        self.ns_merge_checkbox.setChecked(True)
        options_row.addWidget(self.ns_merge_checkbox)
        options_row.addSpacing(10)
        self.ns_crf_spin = QtWidgets.QSpinBox()
        self.ns_crf_spin.setRange(18, 28)
        self.ns_crf_spin.setValue(22)
        self.ns_crf_spin.setToolTip("CRF: 18=chất lượng cao, 28=nhỏ hơn")
        options_row.addWidget(QtWidgets.QLabel("CRF:"))
        options_row.addWidget(self.ns_crf_spin)
        options_row.addStretch()
        cfg_layout.addRow("Tùy chọn:", options_row)

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
            "Font chữ cho phụ đề.\n"
            "Font trong thư mục fonts/ sẽ tự động cài khi merge.\n"
            "Có thể gõ tên font bất kỳ hoặc chọn từ danh sách."
        )
        sub_style_row.addWidget(self.ns_sub_font_combo)
        sub_style_row.addSpacing(16)
        sub_style_row.addWidget(QtWidgets.QLabel("Size:"))
        self.ns_sub_size_spin = QtWidgets.QSpinBox()
        self.ns_sub_size_spin.setRange(12, 80)
        self.ns_sub_size_spin.setValue(20)
        self.ns_sub_size_spin.setToolTip("Cỡ chữ phụ đề (khuyến nghị: 18-28)")
        sub_style_row.addWidget(self.ns_sub_size_spin)
        sub_style_row.addSpacing(16)
        sub_style_row.addWidget(QtWidgets.QLabel("MarginV:"))
        self.ns_sub_margin_v_spin = QtWidgets.QSpinBox()
        self.ns_sub_margin_v_spin.setRange(0, 300)
        self.ns_sub_margin_v_spin.setValue(30)
        self.ns_sub_margin_v_spin.setToolTip(
            "Vị trí sub theo chiều dọc (MarginV).\n"
            "0 = sát mép dưới, tăng để đẩy sub lên cao hơn.\n"
            "Mặc định: 30"
        )
        sub_style_row.addWidget(self.ns_sub_margin_v_spin)
        sub_style_row.addSpacing(16)
        sub_style_row.addWidget(QtWidgets.QLabel("Màu:"))
        self.ns_sub_color_combo = QtWidgets.QComboBox()
        self.ns_sub_color_combo.setEditable(True)
        self.ns_sub_color_combo.setMinimumWidth(90)
        _color_presets = [
            ("Trắng",         "#FFFFFF"),
            ("Vàng",          "#FFD700"),
            ("Xanh dương",    "#00BFFF"),
            ("Đỏ",            "#FF6B6B"),
            ("Xanh lá",       "#00FF7F"),
            ("Cam",           "#FFA500"),
            ("Hồng",          "#FF69B4"),
            ("Tím",           "#DA70D6"),
            ("Lục",           "#90EE90"),
            ("Xám sáng",      "#D3D3D3"),
        ]
        for _label, _hex in _color_presets:
            self.ns_sub_color_combo.addItem(_label, _hex)
        self.ns_sub_color_combo.setCurrentIndex(0)  # default: Trắng
        self.ns_sub_color_combo.setToolTip(
            "Màu chữ phụ đề.\n"
            "Có thể gõ mã hex bất kỳ (VD: #FF0000)"
        )
        sub_style_row.addWidget(self.ns_sub_color_combo)
        sub_style_row.addStretch()
        cfg_layout.addRow("Sub style:", sub_style_row)

        # Preview buttons row
        preview_row = QtWidgets.QHBoxLayout()
        preview_row.addWidget(QtWidgets.QLabel("Xem trước:"))
        btn_full = QtWidgets.QPushButton("Full màn hình phone")
        btn_full.clicked.connect(self._ns_preview_full)
        btn_full.setToolTip("Video 9:16 lấp đầy màn hình điện thoại")
        preview_row.addWidget(btn_full)
        btn_169 = QtWidgets.QPushButton("Video 16:9 trên phone")
        btn_169.clicked.connect(self._ns_preview_169)
        btn_169.setToolTip("Video 16:9 nằm giữa màn hình điện thoại, đen trên/dưới")
        preview_row.addWidget(btn_169)
        preview_row.addStretch()
        cfg_layout.addRow("", preview_row)

        ns_layout.addWidget(cfg)

        inp = QtWidgets.QGroupBox("Thêm phim")
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
        ns_table_header.addWidget(QtWidgets.QLabel("<b>Danh sách phim đã thêm</b>"))
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

        self.ns_table = QtWidgets.QTableWidget(0, 7)
        self.ns_table.setHorizontalHeaderLabels(
            ["Tên phim", "Tập", "Chọn", "Trạng thái", "Kết quả", "Time", "Actions"]
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
            4, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.ns_table.horizontalHeader().setSectionResizeMode(
            5, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.ns_table.horizontalHeader().setSectionResizeMode(
            6, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.ns_table.verticalHeader().setVisible(False)
        self.ns_table.setWordWrap(True)
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

        self.ns_status = QtWidgets.QLabel("Sẵn sàng.")
        self.ns_status.setStyleSheet("color: #888; font-size: 11px; padding-left: 4px;")
        ns_layout.addWidget(self.ns_status)

        return self.ns_tab

    def _check_netshort_ffmpeg(self):
        """Warn user and disable the merge checkbox if ffmpeg is not found."""
        if not _ns_check_ffmpeg():
            QtWidgets.QMessageBox.warning(
                self, "ffmpeg",
                "Không tìm thấy ffmpeg trong PATH.\n"
                "Chức năng merge sẽ bị disable. "
                "Tải tại https://ffmpeg.org/download.html."
            )
            self.ns_merge_checkbox.setChecked(False)
            self.ns_merge_checkbox.setEnabled(False)

    def _ns_browse_save_dir(self):
        """Open a folder picker and populate the save directory field."""
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Chọn thư mục lưu",
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
            "Phần mềm tải phim, video từ nhiều nguồn.<br>"
            "NetShort mode: tải phim từ xemshort.top.",
        )

    def show_help(self):
        """Display the usage guide dialog."""
        help_text = (
            "<b>Hướng dẫn sử dụng Tool Download Movie Pro</b><br><br>"
            "<b>1. Tìm Movie ID:</b><br>"
            "- Truy cập <a href='https://xemshort.top'>https://xemshort.top</a><br>"
            "- Tìm phim muốn tải, mở trang phim<br>"
            "- Copy Movie ID từ URL (ví dụ: xemshort.top/phim/ten-phim-<b>2043519588162863105</b>.html)<br><br>"
            "<b>2. Tải phim:</b><br>"
            "- Dán Movie ID vào ô \"Movie ID\"<br>"
            "- Nhấn \"Fetch\" để lấy danh sách tập<br>"
            "- Chọn preset (mp4/webm/best) và thư mục lưu<br>"
            "- Nhấn \"Start Download\" để tải<br><br>"
            "<b>3. Tải phụ đề & Auto Merged Sub:</b><br>"
            "- <b>Tải phụ đề:</b> Tải file phụ đề (vn/en)<br>"
            "- <b>Auto Merged Sub:</b> Mặc định luôn bật - khi chọn \"Tải phụ đề\", "
            "bạn <b>phải tick</b> checkbox \"Auto Merged Sub\" trước khi nhấn Start.<br>"
            "- Nếu không tick \"Auto Merged Sub\" khi tải phụ đề, phim sẽ không được ghép phụ đề.<br>"
            "- <b>Hardcode sub (merge):</b> Burn phụ đề vào video (tất cả trong 1 file)<br><br>"
            "<b>4. Các tính năng khác:</b><br>"
            "- <b>Load JSON File:</b> Tải file JSON đã lưu trước đó<br>"
            "- <b>Paste JSON:</b> Dán trực tiếp nội dung JSON<br><br>"
            "<b>5. Mẹo:</b><br>"
            "- Chọn preset \"mp4\" để tương thích tốt nhất<br>"
            "- Nếu phim có phụ đề, cần tick cả \"Tải phụ đề\" và \"Auto Merged Sub\"<br>"
            "- Tick \"Hardcode sub (merge)\" để burn phụ đề vào video<br><br>"
            f"<b>Version:</b> {__version__}"
        )

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Hướng dẫn sử dụng")
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

        self.statusBar.showMessage(f"Đã thêm {added} tập vào queue", 5000)

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
    # First launch welcome
    # -------------------------------------------------------------------------

    def _check_first_launch(self):
        """Show a welcome dialog on first launch only (once per installation)."""
        s = self._ns_settings()
        if s.value("first_launch_done", False, type=bool):
            return
        s.setValue("first_launch_done", True)
        greeting = _get_time_greeting()
        QtWidgets.QMessageBox.information(
            self,
            "Tool Download Movie Pro",
            f"Chào bạn! Đã quay trở lại với Tool Download Movie Pro.\n\n"
            f"Bạn đang sử dụng phiên bản {__version__}.\n"
            f"Chúc bạn một ngày làm việc hiệu quả!",
        )

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
        self.ns_sub_color_combo.setCurrentText(
            s.value("sub_color", "Trắng")
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
        s.setValue("sub_color", self.ns_sub_color_combo.currentText())

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
            QtWidgets.QMessageBox.warning(self, "Thiếu input",
                                          "Vui lòng nhập Movie ID.")
            return
        api_url = self.ns_api_url_edit.text().strip()
        if not api_url.startswith(("http://", "https://")):
            QtWidgets.QMessageBox.warning(self, "API URL",
                                          "API URL phải bắt đầu bằng http:// hoặc https://.")
            return

        self.ns_fetch_btn.setEnabled(False)
        self.ns_status.setText(f"Đang fetch {movie_id}...")
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
        self.ns_status.setText(f"Fetched {len(episodes)} tập.")
        self._ns_log(f"Fetched {len(episodes)} tập.")
        self._ns_show_picker(episodes, name)

    def _ns_on_fetch_error(self, msg: str):
        """Handle fetch failure: log the error and show a critical message box."""
        self.ns_status.setText("Fetch lỗi.")
        self._ns_log(f"Lỗi: {msg}")
        QtWidgets.QMessageBox.critical(self, "Fetch lỗi", msg)

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
                    QtWidgets.QMessageBox.warning(self, "Rỗng",
                                                  "JSON không chứa episode nào.")
                    return
                self._ns_show_picker(episodes, movie_name)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Parse lỗi",
                                               f"{type(e).__name__}: {e}")

    def _ns_on_load_json(self):
        """Open a file picker, read a saved JSON file, and load its episodes."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Chọn file JSON", "", "JSON (*.json);;All (*)"
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
            episodes = _ns_parse_episodes(data, movie_name)
            if not episodes:
                QtWidgets.QMessageBox.warning(self, "Rỗng",
                                              "File không có episode.")
                return
            self._ns_show_picker(episodes, movie_name)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Load lỗi",
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
                f"Thêm '{name}' - {movie.selected_count}/{movie.total} tập."
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
        self.ns_table.setItem(row, 4, QtWidgets.QTableWidgetItem("—"))
        self.ns_table.setItem(row, 5, QtWidgets.QTableWidgetItem("—"))

        btn_widget = QtWidgets.QWidget()
        btn_layout = QtWidgets.QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(2, 2, 2, 2)
        btn_layout.setSpacing(4)

        open_btn = QtWidgets.QPushButton("Mở thư mục")
        open_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white; padding: 4px 8px; "
            "border-radius: 4px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #2563eb; }"
        )
        open_btn.setToolTip("Mở thư mục chứa video")
        open_btn.clicked.connect(lambda _, m=movie: self._ns_open_movie_folder(m))
        btn_layout.addWidget(open_btn)

        open_merged_btn = QtWidgets.QPushButton("Mở merged")
        open_merged_btn.setStyleSheet(
            "QPushButton { background-color: #8b5cf6; color: white; padding: 4px 8px; "
            "border-radius: 4px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #7c3aed; }"
        )
        open_merged_btn.setToolTip("Mở thư mục merged/")
        open_merged_btn.clicked.connect(lambda _, m=movie: self._ns_open_merged_folder(m))
        btn_layout.addWidget(open_merged_btn)

        remerge_btn = QtWidgets.QPushButton("Merge lại")
        remerge_btn.setStyleSheet(
            "QPushButton { background-color: #f59e0b; color: white; padding: 4px 8px; "
            "border-radius: 4px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #d97706; }"
        )
        remerge_btn.setToolTip("Xóa file merged cũ và hardcode sub lại")
        remerge_btn.clicked.connect(lambda _, m=movie: self._ns_remerge_movie(m))
        btn_layout.addWidget(remerge_btn)

        detail_btn = QtWidgets.QPushButton("Chi tiết")
        detail_btn.setStyleSheet(
            "QPushButton { background-color: #10b981; color: white; padding: 4px 8px; "
            "border-radius: 4px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #059669; }"
        )
        detail_btn.setToolTip("Xem chi tiết từng tập")
        detail_btn.clicked.connect(lambda _, m=movie: self._ns_show_detail(m))
        btn_layout.addWidget(detail_btn)

        delete_btn = QtWidgets.QPushButton("Xóa")
        delete_btn.setStyleSheet(
            "QPushButton { background-color: #ef4444; color: white; padding: 4px 8px; "
            "border-radius: 4px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #dc2626; }"
            "QPushButton:disabled { background-color: #9ca3af; }"
        )
        delete_btn.setToolTip("Xóa phim khỏi danh sách")
        delete_btn.clicked.connect(lambda _, m=movie: self._ns_remove_movie(m))
        btn_layout.addWidget(delete_btn)

        movie.remerge_btn = remerge_btn
        movie.delete_btn = delete_btn
        self.ns_table.setCellWidget(row, 6, btn_widget)
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
        if hasattr(movie, "delete_btn"):
            movie.delete_btn.setVisible(not worker_running)

    def _ns_remove_movie(self, movie: NSMovie):
        """Remove a movie from the queue list and its corresponding table row."""
        if movie in self.nsmovies:
            idx = self.nsmovies.index(movie)
            self.nsmovies.remove(movie)
            if hasattr(movie, "remerge_btn"):
                del movie.remerge_btn
            if hasattr(movie, "delete_btn"):
                del movie.delete_btn
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
                self, "Đang chạy",
                "Vui lòng đợi tiến trình hiện tại hoàn tất trước khi re-merge."
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
                "Không có tập nào trạng thái 'done' để re-merge.\n"
                "Chỉ re-merge được khi tập đã 'done'."
            )
            return

        if movie in self.nsmovies:
            row = self.nsmovies.index(movie)
            self._ns_set_status(row, "Ready")

        self._ns_log(f"Re-merge '{movie.name}': reset {reset_count} tập, bắt đầu lại...")
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
                self, "Hoàn tất",
                "Không có phim nào cần tải."
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
            self._ns_log("=== TẤT CẢ HOÀN TẤT ===")
            self.ns_status.setText("Hoàn tất.")
            self.ns_start_btn.setEnabled(True)
            self.ns_stop_btn.setEnabled(False)
            self.ns_fetch_btn.setEnabled(True)
            self.ns_progress_bar.setValue(100)
            QtWidgets.QMessageBox.information(
                self, "Hoàn tất",
                "Đã hoàn thành tất cả phim trong bảng."
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
            sub_color=self.ns_sub_color_combo.currentText(),
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

    def _ns_build_result_summary(self, movie: NSMovie) -> str:
        """Build a merge-result summary string from episode merge_note fields."""
        sel = [e for e in movie.episodes if e.selected]
        ok_count      = sum(1 for e in sel if e.status == "done" and e.merge_note == "ok")
        no_sub_count  = sum(1 for e in sel if e.status == "done" and e.merge_note == "no_sub")
        dur_warn_count = sum(1 for e in sel if e.status == "done" and e.merge_note.startswith("dur:"))
        error_count   = sum(1 for e in sel if e.status == "error" or e.merge_note == "error")
        parts = []
        if ok_count:
            parts.append(f"✅ {ok_count} OK")
        if no_sub_count:
            parts.append(f"⚠ {no_sub_count} thiếu sub")
        if dur_warn_count:
            # Collect the actual diff strings for display
            diffs = [e.merge_note for e in sel if e.merge_note.startswith("dur:")]
            parts.append(f"⏱ {dur_warn_count} lệch thời gian ({', '.join(diffs)})")
        if error_count:
            parts.append(f"❌ {error_count} lỗi")
        return "\n".join(parts) if parts else "—"

    def _ns_format_time_info(self, movie: NSMovie) -> str:
        """Format start time and total elapsed time for the movie."""
        if not movie.start_time:
            return "—"
        start_str = time.strftime("%H:%M:%S", time.localtime(movie.start_time))
        if movie.end_time:
            elapsed = int(movie.end_time - movie.start_time)
            mins, secs = divmod(elapsed, 60)
            hrs, mins = divmod(mins, 60)
            if hrs:
                total_str = f"{hrs}h {mins}m {secs}s"
            elif mins:
                total_str = f"{mins}m {secs}s"
            else:
                total_str = f"{secs}s"
            return f"Bắt đầu: {start_str}\nTổng: {total_str}"
        return f"Bắt đầu: {start_str}"

    def _ns_on_movie_done(self, movie: NSMovie):
        """Mark movie row as Done and advance the iterator to the next movie."""
        if movie not in self.nsmovies:
            return
        row = self.nsmovies.index(movie)
        ok = sum(1 for e in movie.episodes if e.selected and e.status == "done")
        total = movie.selected_count
        self._ns_set_status(row, f"Done {ok}/{total}")

        # Update Kết quả (col 4) and Time (col 5)
        result_item = QtWidgets.QTableWidgetItem(self._ns_build_result_summary(movie))
        self.ns_table.setItem(row, 4, result_item)
        time_item = QtWidgets.QTableWidgetItem(self._ns_format_time_info(movie))
        self.ns_table.setItem(row, 5, time_item)
        self.ns_table.resizeRowsToContents()

        self._ns_update_row_btns(movie)

        if self._ns_iterator is None:
            return
        iterator = self._ns_iterator
        self._ns_iterator = None
        self._ns_run_next_movie(iterator)

    def _ns_show_detail(self, movie: NSMovie):
        """Open the NSDetailDialog for the given movie."""
        dlg = NSDetailDialog(movie, self)
        dlg.table.cellDoubleClicked.connect(
            lambda row, col: self._ns_detail_cell_clicked(movie, row, col, dlg)
        )
        dlg.exec()

    def _ns_detail_cell_clicked(self, movie: NSMovie, row: int, col: int, dlg: NSDetailDialog):
        """Handle double-click on a detail table cell."""
        item = dlg.table.item(row, col)
        if item is None:
            return
        path_str = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not path_str:
            return
        path = Path(path_str)
        if not path.exists():
            return

        if col == 1 or col == 3:
            # Video gốc hoặc Video Merged — mở popup info
            NSVideoPopup(path, dlg).exec()
        elif col == 2:
            # VTT — mở editor
            NSVttEditorDialog(path, dlg).exec()

    def _ns_preview_169(self):
        """Show a 16:9 subtitle preview (1280x720, real video resolution)."""
        self._ns_show_sub_preview(aspect="16:9")

    def _ns_preview_full(self):
        """Show a full-screen (2.35:1) subtitle preview."""
        self._ns_show_sub_preview(aspect="full")

    def _ns_show_sub_preview(self, aspect: str):
        """Phone-mockup subtitle preview.

        aspect="full"  → 9:16 vertical video fills phone screen entirely.
        aspect="16:9"  → 16:9 horizontal video centred on phone, black bars top/bottom.
        """
        font_name  = self.ns_sub_font_combo.currentText().strip() or "Arial"
        font_size  = self.ns_sub_size_spin.value()
        margin_v   = self.ns_sub_margin_v_spin.value()
        color_name = self.ns_sub_color_combo.currentText()
        color_hex  = _COLOR_TO_HEX.get(color_name, color_name) if color_name else "#FFFFFF"

        # ── Phone canvas (full render resolution) ───────────────────────────
        PHONE_W, PHONE_H = 720, 1280

        if aspect == "full":
            # 9:16 vertical video — fills entire phone screen
            vid_x, vid_y = 0, 0
            vid_w, vid_h = PHONE_W, PHONE_H
            aspect_label = "Full màn hình phone (9:16)"
        else:
            # 16:9 horizontal video centred on phone
            vid_w = PHONE_W
            vid_h = PHONE_W * 9 // 16      # 405 px
            vid_x = 0
            vid_y = (PHONE_H - vid_h) // 2  # 437 px
            aspect_label = "Video 16:9 trên phone"

        sample_lines = [
            "Phụ đề mẫu  /  Sample subtitle",
            "行  高棉  เชงเม้ง",
        ]

        # ── Draw phone canvas ────────────────────────────────────────────────
        pixmap = QtGui.QPixmap(PHONE_W, PHONE_H)
        pixmap.setDevicePixelRatio(1.0)
        pixmap.fill(QtGui.QColor("#111111"))

        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Black bars outside video region (16:9 mode)
        if aspect == "16:9":
            painter.fillRect(0, 0, PHONE_W, vid_y, QtGui.QColor("#000000"))
            painter.fillRect(0, vid_y + vid_h, PHONE_W,
                             PHONE_H - vid_y - vid_h, QtGui.QColor("#000000"))

        # Video scene background
        painter.fillRect(vid_x, vid_y, vid_w, vid_h, QtGui.QColor("#1a1a2e"))
        grad = QtGui.QLinearGradient(vid_x, vid_y, vid_x, vid_y + vid_h)
        grad.setColorAt(0.0, QtGui.QColor(40, 40, 80, 100))
        grad.setColorAt(1.0, QtGui.QColor(0, 0, 0, 200))
        painter.fillRect(vid_x, vid_y, vid_w, vid_h, grad)

        # ── Subtitle text ────────────────────────────────────────────────────
        font = QtGui.QFont(font_name, font_size)
        font.setBold(True)
        painter.setFont(font)

        text_color    = QtGui.QColor(color_hex)
        outline_color = QtGui.QColor(0, 0, 0, 255)
        outline_size  = max(2, int(font_size * 0.13))

        fm           = QtGui.QFontMetrics(font)
        line_spacing = int(font_size * 0.3)
        total_th     = len(sample_lines) * (fm.height() + line_spacing) - line_spacing

        # Bottom of video region minus MarginV (mirrors ASS Alignment=2 MarginV)
        vid_bottom  = vid_y + vid_h
        text_y_base = vid_bottom - margin_v - total_th

        max_tw  = max(fm.horizontalAdvance(line) for line in sample_lines)
        text_x  = (PHONE_W - max_tw) // 2

        y = text_y_base + fm.ascent()
        for line in sample_lines:
            x = text_x
            for dx in range(-outline_size, outline_size + 1):
                for dy in range(-outline_size, outline_size + 1):
                    if dx == 0 and dy == 0:
                        continue
                    painter.setPen(outline_color)
                    painter.drawText(int(x + dx), int(y + dy), line)
            painter.setPen(text_color)
            painter.drawText(int(x), int(y), line)
            y += fm.height() + line_spacing

        painter.end()

        # ── Scale to display size (~360×640) ─────────────────────────────────
        scaled = pixmap.scaled(
            360, 640,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )

        # ── Build dialog ─────────────────────────────────────────────────────
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(
            f"Preview  |  {aspect_label}  |  {font_name}  {font_size}px  |  {color_name}"
        )
        dlg.setStyleSheet("QDialog { background: #1a1a1a; }")

        main_layout = QtWidgets.QVBoxLayout(dlg)
        main_layout.setContentsMargins(24, 20, 24, 14)
        main_layout.setSpacing(10)

        # Phone mockup
        phone_widget = _NSPhoneMockup(scaled, scaled.width(), scaled.height(), dlg)
        main_layout.addWidget(phone_widget, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        # Info bar
        info = (
            f"Font: {font_name}  |  Size: {font_size}  |  MarginV: {margin_v}"
            f"  |  Color: {color_hex}  |  Bold: On"
        )
        info_lbl = QtWidgets.QLabel(info)
        info_lbl.setStyleSheet(
            "QLabel { color: #aaa; background: #0d0d0d; font-size: 11px;"
            " padding: 5px 10px; border-radius: 4px; }"
        )
        info_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(info_lbl)

        dlg.adjustSize()
        dlg.exec()


    def _ns_on_stop(self):
        """Signal the active worker to stop and reset the UI button states."""
        self._ns_log("Đang dừng...")
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
