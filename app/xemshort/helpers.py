"""XemShort pure helper functions (no workers, no dialogs)."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess as sp
import sys
from pathlib import Path
from typing import Optional

from PySide6 import QtGui

from .models import XSEpisode

# ── Platform-specific imports ──────────────────────────────────────────────────
_is_windows = platform.system() == "Windows"
if _is_windows:
    import winreg


# ── Color helpers ────────────────────────────────────────────────────────────

_COLOR_TO_HEX = {
    "Trắng":       "#FFFFFF",
    "Vàng":        "#FFD700",
    "Xanh dương":  "#00BFFF",
    "Đỏ":          "#FF6B6B",
    "Xanh lá":     "#00FF7F",
    "Cam":         "#FFA500",
    "Hồng":        "#FF69B4",
    "Tím":         "#DA70D6",
    "Lục":         "#90EE90",
    "Xám sáng":    "#D3D3D3",
}


def _ns_color_to_ass(color_str: str) -> str:
    """Convert a color name or hex string to ASS &HAABBGGRR format."""
    hex_val = _COLOR_TO_HEX.get(color_str, color_str) if color_str else "#FFFFFF"
    hex_val = hex_val.lstrip("#")
    if len(hex_val) == 6:
        r, g, b = hex_val[0:2], hex_val[2:4], hex_val[4:6]
        return f"&H00{b.upper()}{g.upper()}{r.upper()}"
    return "&H00FFFFFF"


# ── API / JSON helpers ────────────────────────────────────────────────────────

def _ns_parse_episodes(data: dict | list, movie_name: str = "") -> list[XSEpisode]:
    """Parse API response (dict or list) into a sorted list of XSEpisode objects."""
    if isinstance(data, dict):
        items = data.get("shortPlayEpisodeInfos")
        if not items:
            items = data.get("data", [])
        if not movie_name:
            movie_name = data.get("shortPlayName", "")
    else:
        items = data

    episodes: list[XSEpisode] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        _ep = item.get("episodeNo")
        episode = int(_ep) if _ep is not None else int(item.get("episode", 0))

        ep_name = item.get("episodeName")
        name = ep_name if ep_name else movie_name or item.get("name") or "Untitled"

        play = item.get("playVoucher") or item.get("play") or ""

        subtitle_list = item.get("subtitleList") or item.get("subtitle") or []
        sub_url = None
        if subtitle_list and isinstance(subtitle_list, list) and len(subtitle_list) > 0:
            sub_url = subtitle_list[0].get("url")

        episodes.append(XSEpisode(
            id=str(item.get("episodeId") or item.get("id", "")),
            name=name,
            episode=int(episode),
            play=play,
            subtitle_url=sub_url,
        ))
    episodes.sort(key=lambda e: e.episode)
    return episodes


# ── Subtitle helpers ──────────────────────────────────────────────────────────

def _ns_detect_sub_ext(content: bytes) -> str:
    """Detect subtitle format from raw bytes; returns 'vtt', 'srt', or 'txt'."""
    text = content[:500].decode("utf-8", errors="ignore")
    if "WEBVTT" in text:
        return "vtt"
    if "-->" in text:
        return "srt"
    return "txt"


def _ns_escape_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter string (backslash and colon)."""
    s = str(path).replace("\\", "/").replace(":", r"\:")
    return s.replace("'", r"\'")


def _ns_convert_sub_to_ass(sub_path: Path, font_name: str, font_size: int,
                            outline: float = 1.0) -> Path:
    """Convert .srt/.vtt to .ass with full style control embedded in the file."""
    import re as _re

    ass_path = sub_path.with_suffix('.ass')

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
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}.{int(ms):02d}"

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
            t_line = (lines[t_idx].split('-->')[0].strip() + ' --> '
                      + lines[t_idx].split('-->')[1].strip().split()[0])
            mt = _re.match(
                r'(?:(\d+):)?(\d+):(\d+)\.(\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)\.(\d+)', t_line
            )
            if not mt:
                continue
            start = _to_ass_time(mt.group(1) or 0, mt.group(2), mt.group(3), mt.group(4))
            end = _to_ass_time(mt.group(5) or 0, mt.group(6), mt.group(7), mt.group(8))
            text_lines = [l.strip() for l in lines[t_idx + 1:] if l.strip()]
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
            text_lines = [l.strip() for l in lines[t_idx + 1:] if l.strip()]
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


# ── Font helpers ──────────────────────────────────────────────────────────────

def _ns_install_fonts(fonts_dir: Path, log_fn=None) -> None:
    """Install TTF fonts from fonts_dir into user's local fonts dir (no admin, Win10+)."""
    if not _is_windows:
        return

    local_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    reg_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

    for font_file in list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf")):
        dest = local_fonts / font_file.name
        try:
            local_fonts.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(str(font_file), str(dest))
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


# ── ffmpeg / ffprobe helpers ──────────────────────────────────────────────────

def _ns_check_ffmpeg() -> bool:
    """Return True if ffmpeg is available (bundled next to EXE or on system PATH)."""
    bundled = Path(sys.executable).parent / "ffmpeg.exe"
    if bundled.exists():
        try:
            sp.run([str(bundled), "-version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, sp.CalledProcessError):
            pass
    try:
        sp.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, sp.CalledProcessError):
        return False


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
    """Get video duration using ffprobe. Returns HH:MM:SS string or None."""
    secs = _ns_get_video_duration_secs(path)
    if secs is None:
        return None
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── VTT analysis ──────────────────────────────────────────────────────────────

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
