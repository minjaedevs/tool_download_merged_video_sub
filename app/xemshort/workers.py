"""XemShort background worker threads: XSFetchWorker, XSDownloadMergeWorker."""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import sys
import tempfile
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
import urllib3
from PySide6 import QtCore

# awscdn.netshort.com has an untrusted chain on Windows — suppress the warning globally
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .models import XSEpisode, XSMovie
from .helpers import (
    _ns_b64_decode_safe,
    _ns_color_to_ass,
    _ns_convert_sub_to_ass,
    _ns_detect_sub_ext,
    _ns_escape_path,
    _ns_get_video_duration,
    _ns_get_video_duration_secs,
    _ns_install_fonts,
    _ns_parse_episodes,
    _ns_try_decrypt,
)
from .cache import _ns_cache_get, _ns_cache_key, _ns_cache_set
from .subtitle_errors import upsert_subtitle_error

# API headers used for all XemShort HTTP requests
NETSHORT_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://xemshort.top",
    "Referer": "https://xemshort.top/",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "short-source": "netshort",
}

DRAMAWAVE_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://xemshort.top",
    "Referer": "https://xemshort.top/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "short-source": "dramawave",
}

_MERGE_SIDECAR_FILE = ".merge_settings.json"


def _rewrite_hls_playlist_urls(content: bytes, base_url: str) -> bytes:
    """Make relative HLS playlist URIs absolute before saving to a local file."""
    text = content.decode("utf-8-sig", errors="replace")
    out_lines: list[str] = []

    def _rewrite_uri_attr(match: re.Match[str]) -> str:
        quote = match.group(1)
        uri = match.group(2)
        if uri.startswith(("http://", "https://", "data:")):
            return match.group(0)
        return f"URI={quote}{urljoin(base_url, uri)}{quote}"

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            out_lines.append(
                re.sub(r'URI=(["\'])([^"\']+)\1', _rewrite_uri_attr, line)
            )
            continue
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        if stripped.startswith(("http://", "https://", "data:")):
            out_lines.append(line)
            continue
        prefix_len = len(line) - len(line.lstrip())
        out_lines.append(line[:prefix_len] + urljoin(base_url, stripped))
    return ("\n".join(out_lines) + "\n").encode("utf-8")

# ── GPU encoder detection ─────────────────────────────────────────────────────

_GPU_ENCODER_CANDIDATES = [
    # (encoder_name, keyword_in_ffmpeg_encoders_output, quality_param_fn(crf))
    ("h264_nvenc", "nvenc", lambda crf: ["-preset", "p4", "-rc", "vbr", "-cq", str(crf)]),
    ("h264_amf",   "amf",   lambda crf: ["-quality", "balanced", "-qp_i", str(crf), "-qp_p", str(min(crf + 2, 51))]),
    ("h264_qsv",   "qsv",   lambda crf: ["-preset", "fast", "-global_quality", str(crf)]),
]


@functools.lru_cache(maxsize=1)
def _detect_video_encoder(ffmpeg_path: str) -> str:
    """
    Probe available H.264 encoders; return the first working GPU encoder.
    Cached with lru_cache — runs only once per process lifetime.
    Falls back to 'libx264' if no GPU encoder is available or functional.
    """
    import subprocess as _sp
    _cflags = {"creationflags": _sp.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        enc_list = _sp.run(
            [ffmpeg_path, "-encoders", "-v", "quiet"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, **_cflags,
        )
        for enc_name, keyword, _ in _GPU_ENCODER_CANDIDATES:
            if keyword not in enc_list.stdout:
                continue
            # Sanity-check: actually encode 1 frame to /dev/null
            probe = _sp.run(
                [ffmpeg_path,
                 "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04:r=1",
                 "-c:v", enc_name, "-frames:v", "1",
                 "-f", "null", "-"],
                capture_output=True, timeout=10, **_cflags,
            )
            if probe.returncode == 0:
                return enc_name
    except Exception:
        pass
    return "libx264"


def _encoder_quality_params(encoder_name: str, crf: int, cpu_preset: str) -> list[str]:
    """Return encoder-specific quality/speed parameters."""
    for name, _, param_fn in _GPU_ENCODER_CANDIDATES:
        if name == encoder_name:
            return param_fn(crf)
    # libx264 CPU fallback
    return ["-preset", cpu_preset, "-crf", str(crf)]


def _load_merge_sidecar(merge_dir: Path) -> dict:
    """Load merge settings fingerprint from sidecar; return {} if missing or unreadable."""
    try:
        return json.loads((merge_dir / _MERGE_SIDECAR_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_merge_sidecar(merge_dir: Path, settings: dict) -> None:
    """Persist merge settings fingerprint to sidecar file."""
    try:
        (merge_dir / _MERGE_SIDECAR_FILE).write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


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


class XSFetchWorker(QtCore.QThread):
    """Background thread that fetches episode list from the API."""

    success   = QtCore.Signal(list, str, str, int)   # episodes, movie_name, movie_id, instance_id
    cache_hit = QtCore.Signal(list, str, str, int)   # episodes, movie_name, movie_id, instance_id
    error     = QtCore.Signal(str, int)              # msg, instance_id
    log_msg   = QtCore.Signal(str, int)              # debug/warning, instance_id

    def __init__(self, api_url: str, movie_id: str, headers: dict[str, str] | None = None):
        """Store API URL and movie ID for the fetch request."""
        super().__init__()
        self.api_url = api_url
        self.movie_id = movie_id
        self.headers = headers or NETSHORT_API_HEADERS
        self.instance_id: int = uuid.uuid4().int & 0x7FFFFFFF

    def run(self):
        """Fetch episodes, checking in-memory cache first (TTL=30 min)."""
        key = _ns_cache_key(f"{self.headers.get('short-source', '')}:{self.api_url}", self.movie_id)
        cached = _ns_cache_get(key)
        if cached is not None:
            episodes, movie_name = cached
            self.cache_hit.emit(episodes, movie_name, self.movie_id, self.instance_id)
            return

        url = self.api_url.replace("{movie_id}", self.movie_id)
        try:
            import subprocess as sp, json as _json, platform as _platform
            _cflags = {"creationflags": sp.CREATE_NO_WINDOW} if _platform.system() == "Windows" else {}
            _hdr_args: list[str] = []
            for _hk, _hv in self.headers.items():
                _hdr_args += ["-H", f"{_hk}: {_hv}"]
            result = sp.run(
                ["curl", "-s", "--max-time", "10", *_hdr_args, url],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, **_cflags
            )
            if result.returncode != 0 or not result.stdout.strip():
                # Fall back to requests with (connect=10s, read=30s) timeout
                r = requests.get(url, headers=self.headers, timeout=(10, 30), verify=False)
                r.raise_for_status()
                data = r.json()
            else:
                data = _json.loads(result.stdout)

            # Xử lý encrypted data field (data["data"] là chuỗi base64/AES)
            if isinstance(data, dict) and isinstance(data.get("data"), str) and len(data.get("data", "")) > 100:
                raw = _ns_b64_decode_safe(data["data"])
                decrypted = _ns_try_decrypt(raw)
                if decrypted is not None:
                    data = decrypted

            movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
            episodes = _ns_parse_episodes(data, movie_name)
            # Format mới: tên phim nằm trong từng episode item thay vì root shortPlayName
            if not movie_name and episodes:
                movie_name = episodes[0].name
            if not episodes:
                if isinstance(data, dict):
                    keys = list(data.keys())
                    preview = str(data)[:400]
                    self.error.emit(
                        f"Không tìm thấy tập nào trong API response.\n"
                        f"Keys: {keys}\n"
                        f"Preview: {preview}",
                        self.instance_id,
                    )
                else:
                    self.error.emit(
                        f"Không tìm thấy tập nào trong API response.\n"
                        f"Type: {type(data).__name__}, Preview: {str(data)[:400]}",
                        self.instance_id,
                    )
                return

            # Debug: log raw episode keys when play URL is missing
            empty_play = [e for e in episodes if not e.play]
            if empty_play:
                raw_items = (
                    data if isinstance(data, list) else (
                        data.get("shortPlayEpisodeInfos")
                        or data.get("episodeList")
                        or data.get("episodes")
                        or data.get("data")
                        or data.get("list")
                        or data.get("result")
                        or []
                    )
                )
                first_item = raw_items[0] if raw_items and isinstance(raw_items[0], dict) else {}
                self.log_msg.emit(
                    f"⚠ {len(empty_play)}/{len(episodes)} tập thiếu URL video.\n"
                    f"Keys trong item[0]: {list(first_item.keys())}\n"
                    f"Preview item[0]: {str(first_item)[:400]}",
                    self.instance_id,
                )

            _ns_cache_set(key, episodes, movie_name)
            self.success.emit(episodes, movie_name, self.movie_id, self.instance_id)

        except Exception as e:
            self.error.emit(str(e), self.instance_id)


# Backward-compat alias
NSFetchWorker = XSFetchWorker


class DramaWaveFetchWorker(XSFetchWorker):
    """Fetch DramaWave episode list with the required source header."""

    def __init__(self, api_url: str, movie_id: str):
        super().__init__(api_url, movie_id, headers=DRAMAWAVE_API_HEADERS)


class XSDownloadMergeWorker(QtCore.QThread):
    """Background thread: downloads video + subtitle then optionally hardcodes sub via ffmpeg."""

    subtitle_error_source = "netshort"

    log_msg        = QtCore.Signal(str)
    episode_status = QtCore.Signal(int, str, int)   # ep_num, status, instance_id
    progress       = QtCore.Signal(int, int, int)    # done, total, instance_id
    finished_all   = QtCore.Signal(int)              # instance_id

    def __init__(self, movie: XSMovie, concurrency: int, download_sub: bool,
                 do_merge: bool, crf: int, preset: str, encode_threads: int = 4,
                 merge_concurrency: int = 1,
                 sub_font: str = "UTM Alter Gothic", sub_size: int = 20,
                 sub_margin_v: int = 30, sub_color: str = "Trắng",
                 sub_bold: bool = True, sub_italic: bool = False,
                 sub_outline: float = 1.0,
                 convert_m3u8: bool = False,
                 m3u8_reencode: bool = False):
        """Configure worker with movie data, thread count, and ffmpeg encode settings."""
        super().__init__()
        self.movie = movie
        self.concurrency = concurrency
        self.download_sub = download_sub
        self.do_merge = do_merge
        self.convert_m3u8 = convert_m3u8
        self.m3u8_reencode = m3u8_reencode
        self.crf = crf
        self.ffpreset = preset
        self.merge_concurrency = max(1, min(2, merge_concurrency))
        self.encode_threads = max(1, encode_threads)
        self.sub_font = sub_font
        self.sub_size = sub_size
        self.sub_margin_v = sub_margin_v
        self.sub_color = sub_color
        self.sub_bold = sub_bold
        self.sub_italic = sub_italic
        self.sub_outline = sub_outline
        self._stop = threading.Event()
        # Unique ID so stale signals from a previous worker are ignored
        import uuid
        self.instance_id = uuid.uuid4().int & 0x7FFFFFFF

    def _record_subtitle_error(self, ep: XSEpisode, note: str, **raw) -> None:
        """Best-effort Supabase upsert for subtitle failures."""
        try:
            ok = upsert_subtitle_error(
                self.subtitle_error_source,
                self.movie,
                ep,
                note,
                raw={k: v for k, v in raw.items() if v is not None},
            )
            if ok:
                self.log(f"subtitle error saved: {self.subtitle_error_source} T{ep.episode}")
        except Exception as exc:
            self.log(f"subtitle error save failed T{ep.episode}: {exc}")

    @staticmethod
    def _is_subtitle_download_error(note: str) -> bool:
        return note == "empty subtitle download" or note.startswith("subtitle download failed:")

    def stop(self):
        """Signal the worker to stop after the current episode finishes."""
        self._stop.set()

    def log(self, msg: str):
        """Emit a timestamped log message to the UI log panel."""
        ts = time.strftime("%H:%M:%S")
        self.log_msg.emit(f"[{ts}] {msg}")

    def _get_ffmpeg_path(self) -> Optional[Path]:
        """Locate ffmpeg: check bundled copy next to EXE first, then system PATH."""
        for name in ("ffmpeg", "ffmpeg.exe"):
            candidate = Path(sys.executable).parent / name
            if candidate.exists():
                return candidate
        path = shutil.which("ffmpeg")
        if path:
            return Path(path)
        return None

    def _settings_fingerprint(self) -> dict:
        """Return a dict representing the current merge/subtitle settings used for cache invalidation."""
        return {
            "font":     self.sub_font,
            "size":     self.sub_size,
            "color":    self.sub_color,
            "bold":     self.sub_bold,
            "italic":   self.sub_italic,
            "margin_v": self.sub_margin_v,
            "crf":      self.crf,
            "preset":   self.ffpreset,
        }

    def _subtitle_outline(self) -> float:
        return self.sub_outline

    def _subtitle_shadow(self) -> float:
        return 0.0

    def _merge_video_filter(self, vf_filter: str, ep: XSEpisode) -> str:
        return vf_filter

    def _merge_output_args(self, ep: XSEpisode) -> list[str]:
        return []

    def _episode_base_name(self, ep: XSEpisode) -> str:
        padding = len(str(self.movie.total))
        return f"ep{str(ep.episode).zfill(padding)}"

    def _episode_m3u8_dir_name(self, ep: XSEpisode) -> str:
        return self._episode_base_name(ep)

    @staticmethod
    def _is_valid_hls_dir(out_dir: Path) -> bool:
        playlist = out_dir / "index.m3u8"
        if not playlist.exists() or playlist.stat().st_size <= 128:
            return False
        try:
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return False
        segment_names = [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        if not segment_names:
            return False
        for name in segment_names:
            if "://" in name or name.startswith("/"):
                continue
            if not (out_dir / name).exists():
                return False
        return any(out_dir.glob("seg_*.ts"))

    def _prepare_m3u8_dir(self, ep: XSEpisode) -> tuple[Path, bool]:
        root = self.movie.save_dir / self.movie.folder_name / "m3u8"
        root.mkdir(parents=True, exist_ok=True)
        base = self._episode_m3u8_dir_name(ep)
        index = 0
        while True:
            suffix = "" if index == 0 else f" {index}"
            out_dir = root / f"{base}{suffix}"
            if not out_dir.exists():
                out_dir.mkdir(parents=True, exist_ok=False)
                return out_dir, False
            if out_dir.is_dir() and not self._is_valid_hls_dir(out_dir):
                shutil.rmtree(out_dir, ignore_errors=True)
                out_dir.mkdir(parents=True, exist_ok=False)
                return out_dir, False
            if out_dir.is_dir() and self._is_valid_hls_dir(out_dir):
                index += 1
                continue
            index += 1

    def _download_file(self, url: str, output: Path, desc: str, retries: int = 3) -> bool:
        """Download a URL to a file with retry logic; skip if file already exists."""
        if output.exists() and output.stat().st_size > 1024:
            self.log(f"SKIP {desc} (đã tồn tại)")
            return True

        tmp = output.with_suffix(output.suffix + ".part")
        for attempt in range(1, retries + 1):
            if self._stop.is_set():
                tmp.unlink(missing_ok=True)
                return False
            try:
                with requests.get(url, headers=NETSHORT_DOWNLOAD_HEADERS,
                                  stream=True, timeout=15, verify=False) as r:
                    r.raise_for_status()
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
            except requests.exceptions.ConnectionError as e:
                self.log(f"LỖI {desc} (thử {attempt}/{retries}): {e}")
            except Exception as e:
                self.log(f"LỖI {desc} (thử {attempt}/{retries}): {e}")
            if self._stop.is_set():
                tmp.unlink(missing_ok=True)
                return False
            time.sleep(2 * attempt)
        tmp.unlink(missing_ok=True)
        return False

    def _download_episode(self, ep: XSEpisode) -> bool:
        """Download video and subtitle for one episode; skip sub if local file exists."""
        if self._stop.is_set() or not ep.selected:
            return False

        # Guard: ep.play trống → không có URL để tải (thường do isLock=True)
        if not ep.play:
            ep.status = "error"
            ep.merge_note = "error"
            if getattr(ep, "is_locked", False):
                ep.error_msg = "episode is locked (isLock=True)"
                self.log(f"tập {ep.episode}: bị khóa (isLock=True) — bỏ qua")
            else:
                ep.error_msg = "missing video URL"
                self.log(f"tập {ep.episode}: không có URL video (thiếu field play/playVoucher/...)")
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            return False

        folder = self.movie.save_dir / self.movie.folder_name
        folder.mkdir(parents=True, exist_ok=True)

        base = self._episode_base_name(ep)

        video_path = folder / f"{base}.mp4"

        # ── Video ────────────────────────────────────────────────────────────
        if video_path.exists() and video_path.stat().st_size > 1024 and ep.status == "done":
            # Video exists AND was previously merged — skip download, merge will handle it
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded", self.instance_id)
            self.log(f"SKIP video tập {ep.episode} (đã tồn tại)")
        else:
            self.episode_status.emit(ep.episode, "downloading", self.instance_id)
            dl_ok = self._download_file(ep.play, video_path, f"video tập {ep.episode}")
            if not dl_ok:
                ep.status = "error"
                ep.error_msg = "download video failed"
                self.episode_status.emit(ep.episode, "error", self.instance_id)
                return False
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded", self.instance_id)

        # ── Subtitle ────────────────────────────────────────────────────────
        if self.download_sub and ep.subtitle_url:
            ep.sub_path = None
            for ext in ("srt", "vtt", "txt"):
                old_sub = folder / f"{base}.{ext}"
                if old_sub.exists():
                    try:
                        old_sub.unlink()
                        self.log(f"xóa sub cũ tập {ep.episode}: {old_sub.name}")
                    except Exception as e:
                        self.log(f"không xóa được sub cũ tập {ep.episode} ({old_sub.name}): {e}")
            try:
                r = requests.get(ep.subtitle_url,
                                 headers=NETSHORT_DOWNLOAD_HEADERS, timeout=30, verify=False)
                ct = r.headers.get("Content-Type", "?")
                self.log(
                    f"sub tập {ep.episode}: HTTP {r.status_code} "
                    f"{len(r.content)}B Content-Type={ct}"
                )
                r.raise_for_status()

                # Detect HTML error page (CDN/403 returns HTML instead of subtitle)
                raw_peek = r.content.lstrip(b'\xef\xbb\xbf')[:15]
                if raw_peek.lstrip().startswith(b'<'):
                    preview = r.content[:300].decode("utf-8", errors="replace")
                    self.log(f"sub tập {ep.episode}: server trả về HTML thay vì subtitle\n{preview}")
                    ep.error_msg = "subtitle response is HTML (CDN error/403)"
                    self._record_subtitle_error(
                        ep,
                        "subtitle response is HTML",
                        subtitle_url=ep.subtitle_url,
                    )
                    return True

                if not r.content or not r.content.strip():
                    ep.error_msg = "empty subtitle download"
                    self._record_subtitle_error(
                        ep,
                        "empty subtitle download",
                        subtitle_url=ep.subtitle_url,
                        bytes=0,
                    )
                    self.log(f"sub tập {ep.episode} empty")
                    return True
                ext = _ns_detect_sub_ext(r.content)
                sub_path = folder / f"{base}.{ext}"
                with open(sub_path, "wb") as f:
                    f.write(r.content)
                ep.sub_path = sub_path
                self.log(f"sub tập {ep.episode} OK ({ext}, {len(r.content)} bytes)")
            except Exception as e:
                ep.error_msg = f"subtitle download failed: {e}"
                self._record_subtitle_error(
                    ep,
                    "subtitle download failed",
                    subtitle_url=ep.subtitle_url,
                    error=str(e),
                )
                self.log(f"sub tập {ep.episode} lỗi: {e}")

        return True

    def _merge_episode(self, ep: XSEpisode) -> bool:
        """Burn subtitle into video with ffmpeg; re-merge only if sub is newer than output."""
        if self._stop.is_set() or not ep.video_path or not ep.video_path.exists():
            return False

        merge_dir = self.movie.save_dir / self.movie.folder_name / "merged"
        merge_dir.mkdir(parents=True, exist_ok=True)

        base = self._episode_base_name(ep)
        out_path = merge_dir / f"{base}_merged.mp4"

        if not ep.sub_path or not ep.sub_path.exists():
            previous_error = ep.error_msg
            ep.merge_note = "no_sub"
            ep.status = "error"
            ep.error_msg = "missing subtitle"
            if previous_error and self._is_subtitle_download_error(previous_error):
                self.log(f"subtitle error already recorded at download step T{ep.episode}")
            else:
                self._record_subtitle_error(
                    ep,
                    previous_error or "missing subtitle",
                    subtitle_url=ep.subtitle_url,
                )
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            self.log(
                f"tập {ep.episode}: thiếu sub, bỏ qua merge. "
                "Hãy fetch data lại rồi Start Download & Merge."
            )
            return False

        if out_path.exists() and out_path.stat().st_size > 1024:
            stored = _load_merge_sidecar(merge_dir)
            if stored and stored != self._settings_fingerprint():
                self.log(f"tập {ep.episode}: settings thay đổi -- re-merge...")
            else:
                sub_mtime = ep.sub_path.stat().st_mtime if ep.sub_path and ep.sub_path.exists() else 0
                if sub_mtime <= out_path.stat().st_mtime:
                    ep.merged_path = out_path
                    ep.status = "done"
                    ep.merge_note = "skip:existing"
                    self.episode_status.emit(ep.episode, "done", self.instance_id)
                    self.log(f"merge tập {ep.episode} SKIP (đã tồn tại)")
                    return True
                self.log(f"tập {ep.episode}: sub mới hơn merged -- re-merge...")

        self.episode_status.emit(ep.episode, "merging", self.instance_id)
        self.log(f"merge tập {ep.episode}...")

        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            self.log("ffmpeg not found!")
            ep.status = "error"
            ep.error_msg = "ffmpeg not found"
            ep.merge_note = "error"
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            return False

        tmp_sub_dir = Path(tempfile.gettempdir()) / "yt_dlp_gui_xemshort_subs" / str(self.instance_id)
        tmp_sub_dir.mkdir(parents=True, exist_ok=True)
        tmp_ass_path = tmp_sub_dir / f"{base}.ass"
        sub_outline = self._subtitle_outline()
        sub_shadow = self._subtitle_shadow()
        sub_for_ffmpeg = _ns_convert_sub_to_ass(
            ep.sub_path, self.sub_font, self.sub_size,
            outline=sub_outline, ass_path=tmp_ass_path
        )
        if sub_for_ffmpeg == ep.sub_path:
            ep.merge_note = "no_sub"
            ep.status = "error"
            ep.error_msg = "invalid or empty subtitle"
            self._record_subtitle_error(
                ep,
                "invalid or empty subtitle",
                subtitle_url=ep.subtitle_url,
                sub_path=str(ep.sub_path),
            )
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            self.log(
                f"tập {ep.episode}: subtitle không hợp lệ hoặc rỗng, không merge. "
                "Hãy fetch/download lại subtitle rồi Start Download & Merge."
            )
            return False

        sub_filter = _ns_escape_path(sub_for_ffmpeg)

        # Locate bundled fonts directory:
        # 1) next to EXE (user-placed)
        # 2) app/fonts/ (dev mode)
        # 3) app/root/fonts/ or _MEIPASS/root/fonts/ (PyInstaller bundle via --add-data=root;root)
        fonts_dir = Path(sys.executable).parent / "fonts"
        if not fonts_dir.exists():
            fonts_dir = Path(__file__).parent.parent / "fonts"
        if not fonts_dir.exists():
            fonts_dir = Path(__file__).parent.parent / "root" / "fonts"

        if fonts_dir.exists():
            _ns_install_fonts(fonts_dir, self.log)
            fonts_dir_escaped = _ns_escape_path(fonts_dir)
            vf_filter = (
                f"subtitles='{sub_filter}'"
                f":fontsdir='{fonts_dir_escaped}'"
                f":force_style='FontName={self.sub_font},FontSize={self.sub_size},"
                f"PrimaryColour={_ns_color_to_ass(self.sub_color)},"
                f"OutlineColour=&H00000000,"
                f"BorderStyle=1,Outline={sub_outline:g},Shadow={sub_shadow:g},"
                f"Bold={-1 if self.sub_bold else 0},"
                f"Italic={1 if self.sub_italic else 0},"
                f"Alignment=2,MarginV={self.sub_margin_v}'"
            )
        else:
            self.log("CẢNH BÁO: không tìm thấy thư mục fonts/ -- dùng font hệ thống")
            vf_filter = (
                f"subtitles='{sub_filter}':force_style="
                f"'FontName={self.sub_font},FontSize={self.sub_size},"
                f"PrimaryColour={_ns_color_to_ass(self.sub_color)},"
                f"OutlineColour=&H00000000,"
                f"BorderStyle=1,Outline={sub_outline:g},Shadow={sub_shadow:g},"
                f"Bold={-1 if self.sub_bold else 0},"
                f"Italic={1 if self.sub_italic else 0},"
                f"Alignment=2,MarginV={self.sub_margin_v}'"
            )

        vf_filter = self._merge_video_filter(vf_filter, ep)

        # Get exact source duration to pin output length
        orig_secs = _ns_get_video_duration_secs(ep.video_path)

        import subprocess as sp
        import platform as _platform
        # Detect best encoder once (cached); use configured encode_threads
        encoder_name = _detect_video_encoder(str(ffmpeg_path))
        encoder_params = _encoder_quality_params(encoder_name, self.crf, self.ffpreset)
        _cpu_threads = self.encode_threads
        self.log(f"tập {ep.episode}: encoder={encoder_name}  threads={_cpu_threads}  crf={self.crf}")

        cmd = [
            str(ffmpeg_path), "-y",
            "-threads", str(_cpu_threads),
            "-i", str(ep.video_path),
            "-vf", vf_filter,
            "-c:v", encoder_name,
            *encoder_params,
            "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        cmd += self._merge_output_args(ep)
        # For libx264: also cap encoder-internal thread count explicitly
        if encoder_name == "libx264":
            cmd += ["-x264-params", f"threads={_cpu_threads}"]
        if orig_secs is not None:
            cmd += ["-t", f"{orig_secs:.6f}"]
        cmd += ["-loglevel", "warning", str(out_path)]

        # Run ffmpeg at below-normal priority so it yields to foreground apps
        if _platform.system() == "Windows":
            _BELOW_NORMAL = 0x00004000   # BELOW_NORMAL_PRIORITY_CLASS
            _cflags = {"creationflags": sp.CREATE_NO_WINDOW | _BELOW_NORMAL}
        else:
            _cflags = {}
        try:
            result = sp.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=3600, **_cflags
            )
            if result.stderr.strip():
                self.log(f"ffmpeg warning tập {ep.episode}: {result.stderr[:300]}")
            if result.returncode != 0:
                self.log(f"ffmpeg lỗi tập {ep.episode}: {result.stderr[:500]}")
                ep.status = "error"
                ep.merge_note = "error"
                ep.error_msg = "ffmpeg failed"
                self.episode_status.emit(ep.episode, "error", self.instance_id)
                return False

            ep.merged_path = out_path
            ep.status = "done"
            _save_merge_sidecar(merge_dir, self._settings_fingerprint())

            # Duration check — set merge_note BEFORE emitting done
            try:
                orig_dur = _ns_get_video_duration(ep.video_path)
                merged_dur = _ns_get_video_duration(out_path)
                if orig_dur and merged_dur:
                    def _to_secs(t):
                        return sum(float(x) * 60 ** i for i, x in enumerate(reversed(t.split(":"))))
                    diff = _to_secs(merged_dur) - _to_secs(orig_dur)
                    sign = "+" if diff >= 0 else ""
                    if abs(diff) <= 2:
                        ep.merge_note = "ok"
                        self.log(f"  duration OK: goc={orig_dur} merged={merged_dur}")
                    else:
                        ep.merge_note = f"dur:{sign}{diff}s"
                        self.log(
                            f"  CANH BAO duration: goc={orig_dur} merged={merged_dur} "
                            f"chenh={sign}{diff}s"
                        )
                else:
                    ep.merge_note = "ok"
            except Exception:
                ep.merge_note = "ok"

            self.episode_status.emit(ep.episode, "done", self.instance_id)
            self.log(f"merge tập {ep.episode} OK -> {out_path.name}")
            return True

        except sp.TimeoutExpired:
            self.log(f"merge tập {ep.episode} TIMEOUT")
            ep.status = "error"
            ep.merge_note = "error"
            ep.error_msg = "merge timeout"
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            return False
        except Exception as e:
            self.log(f"merge tập {ep.episode} exception: {e}")
            ep.status = "error"
            ep.merge_note = "error"
            ep.error_msg = str(e)
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            return False

    def _convert_episode_to_m3u8(self, ep: XSEpisode) -> bool:
        """Package a finished episode as VOD HLS under movie/m3u8/{episode}/."""
        if self._stop.is_set():
            return False

        input_path = ep.merged_path if self.do_merge and ep.merged_path else ep.video_path
        if not input_path or not input_path.exists():
            ep.error_msg = "missing input for m3u8"
            ep.status = "error"
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            self.log(f"m3u8 tap {ep.episode}: khong tim thay file input")
            return False

        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            ep.error_msg = "ffmpeg not found"
            ep.status = "error"
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            self.log("ffmpeg not found!")
            return False

        out_dir, _ = self._prepare_m3u8_dir(ep)
        playlist_path = out_dir / "index.m3u8"
        segment_pattern = out_dir / "seg_%05d.ts"

        setattr(ep, "_m3u8_skipped", False)
        if self._is_valid_hls_dir(out_dir):
            ep.m3u8_path = playlist_path
            ep.status = "done"
            setattr(ep, "_m3u8_skipped", True)
            self.episode_status.emit(ep.episode, "done", self.instance_id)
            self.log(f"m3u8 tap {ep.episode} SKIP (da ton tai)")
            return True

        self.episode_status.emit(ep.episode, "m3u8", self.instance_id)
        self.log(f"m3u8 tap {ep.episode}: {input_path.name} -> {playlist_path}")

        import subprocess as sp
        import platform as _platform

        cmd = [
            str(ffmpeg_path), "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
        ]
        if self.m3u8_reencode:
            cmd += [
                "-c:v", "libx264",
                "-preset", self.ffpreset,
                "-crf", str(self.crf),
                "-force_key_frames", "expr:gte(t,n_forced*6)",
                "-sc_threshold", "0",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "48000",
                "-ac", "2",
            ]
        else:
            cmd += [
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "48000",
                "-ac", "2",
            ]
        hls_flags = "independent_segments+temp_file" if self.m3u8_reencode else "temp_file"
        cmd += [
            "-f", "hls",
            "-hls_time", "6",
            "-hls_playlist_type", "vod",
            "-hls_flags", hls_flags,
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(segment_pattern),
            "-loglevel", "warning",
            str(playlist_path),
        ]

        if _platform.system() == "Windows":
            _BELOW_NORMAL = 0x00004000
            _cflags = {"creationflags": sp.CREATE_NO_WINDOW | _BELOW_NORMAL}
        else:
            _cflags = {}

        try:
            result = sp.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=3600, **_cflags
            )
            if result.stderr.strip():
                self.log(f"ffmpeg m3u8 warning tap {ep.episode}: {result.stderr[:300]}")
            if result.returncode != 0:
                self.log(f"ffmpeg m3u8 loi tap {ep.episode}: {result.stderr[:500]}")
                ep.error_msg = "ffmpeg m3u8 failed"
                ep.status = "error"
                self.episode_status.emit(ep.episode, "error", self.instance_id)
                shutil.rmtree(out_dir, ignore_errors=True)
                return False
            if not self._is_valid_hls_dir(out_dir):
                ep.error_msg = "m3u8 output missing"
                ep.status = "error"
                self.episode_status.emit(ep.episode, "error", self.instance_id)
                shutil.rmtree(out_dir, ignore_errors=True)
                return False
            ep.m3u8_path = playlist_path
            ep.status = "done"
            self.episode_status.emit(ep.episode, "done", self.instance_id)
            self.log(f"m3u8 tap {ep.episode} OK -> {playlist_path}")
            return True
        except sp.TimeoutExpired:
            ep.error_msg = "m3u8 timeout"
            ep.status = "error"
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            self.log(f"m3u8 tap {ep.episode} TIMEOUT")
            shutil.rmtree(out_dir, ignore_errors=True)
            return False
        except Exception as e:
            ep.error_msg = str(e)
            ep.status = "error"
            self.episode_status.emit(ep.episode, "error", self.instance_id)
            self.log(f"m3u8 tap {ep.episode} exception: {e}")
            shutil.rmtree(out_dir, ignore_errors=True)
            return False

    def run(self):
        """Entry point: download all selected episodes in parallel, then merge sequentially."""
        selected = [e for e in self.movie.episodes if e.selected]
        total = len(selected)
        if total == 0:
            self.log("Không có tập nào được chọn.")
            self.finished_all.emit(self.instance_id)
            return

        self.movie.start_time = time.time()
        self.log(f"=== Bắt đầu tải & merge '{self.movie.name}' ({total} tập) ===")
        self.log(f"Thư mục: {self.movie.save_dir / self.movie.folder_name}")
        phase_count = 1 + int(self.do_merge) + int(self.convert_m3u8)
        grand_total = total * phase_count
        done = 0
        dl_ok = 0
        dl_fail = 0

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._download_episode, e): e for e in selected}
            for future in as_completed(futures):
                if self._stop.is_set():
                    for f in futures:
                        f.cancel()
                    break
                try:
                    ok = future.result()
                except Exception:
                    ok = False
                if ok:
                    done += 1
                    dl_ok += 1
                else:
                    dl_fail += 1
                self.progress.emit(done, grand_total, self.instance_id)

        if self._stop.is_set():
            self.log("Đã dừng.")
            self.finished_all.emit(self.instance_id)
            return

        merge_ok = 0
        merge_skip = 0
        merge_fail = 0

        if self.do_merge:
            if not self._get_ffmpeg_path():
                self.log("CẢNH BÁO: không tìm thấy ffmpeg -- bỏ qua merge.")
            else:
                merge_items = [
                    ep for ep in selected
                    if ep.status == "downloaded" and ep.video_path and ep.video_path.exists()
                ]
                if merge_items:
                    self.log(f"Luồng merge: {self.merge_concurrency}")
                with ThreadPoolExecutor(max_workers=self.merge_concurrency) as pool:
                    futures = {pool.submit(self._merge_episode, ep): ep for ep in merge_items}
                    for future in as_completed(futures):
                        if self._stop.is_set():
                            for f in futures:
                                f.cancel()
                            break
                        ep = futures[future]
                        try:
                            ok = future.result()
                        except Exception as e:
                            ok = False
                            ep.status = "error"
                            ep.merge_note = "error"
                            ep.error_msg = str(e)
                            self.episode_status.emit(ep.episode, "error", self.instance_id)
                        if ok:
                            done += 1
                            if ep.merge_note.startswith("skip:"):
                                merge_skip += 1
                            else:
                                merge_ok += 1
                        else:
                            merge_fail += 1
                        self.progress.emit(done, grand_total, self.instance_id)

        # Summary log — actual new downloads vs already-present skips
        m3u8_ok = 0
        m3u8_skip = 0
        m3u8_fail = 0

        if self.convert_m3u8:
            if not self._get_ffmpeg_path():
                self.log("CANH BAO: khong tim thay ffmpeg -- bo qua convert m3u8.")
            else:
                if self.do_merge:
                    m3u8_items = [
                        ep for ep in selected
                        if ep.status == "done" and ep.merged_path and ep.merged_path.exists()
                    ]
                else:
                    m3u8_items = [
                        ep for ep in selected
                        if ep.status == "downloaded" and ep.video_path and ep.video_path.exists()
                    ]
                if m3u8_items:
                    self.log("Bat dau convert M3U8 production...")
                for ep in m3u8_items:
                    if self._stop.is_set():
                        break
                    ok = self._convert_episode_to_m3u8(ep)
                    if ok:
                        done += 1
                        if getattr(ep, "_m3u8_skipped", False):
                            m3u8_skip += 1
                        else:
                            m3u8_ok += 1
                    else:
                        m3u8_fail += 1
                    self.progress.emit(done, grand_total, self.instance_id)

        actual_dl = dl_ok - merge_skip  # episodes actually downloaded (not already-done skips)
        dl_summary = f"Tải: {actual_dl} mới" + (f", {dl_fail} lỗi" if dl_fail else "")
        if self.convert_m3u8:
            m3u8_parts = []
            if m3u8_ok:
                m3u8_parts.append(f"{m3u8_ok} moi")
            if m3u8_skip:
                m3u8_parts.append(f"{m3u8_skip} da co")
            if m3u8_fail:
                m3u8_parts.append(f"{m3u8_fail} loi")
            self.log("[Ket qua M3U8] " + (", ".join(m3u8_parts) if m3u8_parts else "0"))
        if self.do_merge:
            merge_parts = []
            if merge_ok:
                merge_parts.append(f"{merge_ok} mới")
            if merge_skip:
                merge_parts.append(f"{merge_skip} đã có")
            if merge_fail:
                merge_parts.append(f"{merge_fail} lỗi")
            merge_summary = "Merge: " + ", ".join(merge_parts) if merge_parts else "Merge: 0"
            self.log(f"[Kết quả] {dl_summary} | {merge_summary}")
        else:
            self.log(f"[Kết quả] {dl_summary}")

        # Always finish at 100%
        self.progress.emit(grand_total, grand_total, self.instance_id)

        self.movie.end_time = time.time()
        self.log(f"=== Hoàn tất '{self.movie.name}' ===")
        self.finished_all.emit(self.instance_id)


# Backward-compat alias
NSDownloadMergeWorker = XSDownloadMergeWorker


class DramaWaveDownloadMergeWorker(XSDownloadMergeWorker):
    """Download worker for DramaWave HLS episode URLs."""

    subtitle_error_source = "dramawave"

    def _is_hls_url(self, url: str) -> bool:
        """Return True for HLS playlist URLs, including non-standard .m3u links."""
        lower = (url or "").lower()
        return ".m3u8" in lower or ".m3u" in lower or "play.m3u" in lower

    def _download_hls_video(self, url: str, output: Path, desc: str) -> bool:
        """Use ffmpeg to download/remux an HLS playlist into a valid MP4 file."""
        if output.exists() and output.stat().st_size > 1024:
            try:
                if _ns_get_video_duration_secs(output) and _ns_get_video_duration_secs(output) > 0:
                    self.log(f"SKIP {desc} (da ton tai)")
                    return True
            except Exception:
                pass
            try:
                output.unlink()
                self.log(f"xoa file video hong: {output.name}")
            except Exception as exc:
                self.log(f"khong xoa duoc file video hong {output.name}: {exc}")
                return False

        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            self.log(f"{desc}: can ffmpeg de tai HLS/m3u")
            return False

        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_suffix(output.suffix + ".part.mp4")
        playlist_tmp = output.with_suffix(output.suffix + ".playlist.m3u8")
        tmp.unlink(missing_ok=True)
        playlist_tmp.unlink(missing_ok=True)

        try:
            response = requests.get(url, headers=NETSHORT_DOWNLOAD_HEADERS, timeout=30, verify=False)
            response.raise_for_status()
            if b"#EXTM3U" not in response.content[:1024]:
                preview = response.content[:300].decode("utf-8", errors="replace")
                self.log(f"{desc}: playlist khong hop le: {preview}")
                return False
            playlist_content = _rewrite_hls_playlist_urls(response.content, response.url)
            if playlist_content != response.content:
                self.log(f"{desc}: rewrite HLS relative URLs -> absolute")
            playlist_tmp.write_bytes(playlist_content)
        except Exception as exc:
            self.log(f"LOI {desc} khi tai playlist HLS/m3u: {exc}")
            return False

        cmd = [
            str(ffmpeg_path),
            "-y",
            "-loglevel", "warning",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-allowed_extensions", "ALL",
            "-allowed_segment_extensions", "ALL",
            "-extension_picky", "0",
            "-i", str(playlist_tmp),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(tmp),
        ]
        try:
            import subprocess as sp
            import platform as _platform
            _cflags = {"creationflags": sp.CREATE_NO_WINDOW} if _platform.system() == "Windows" else {}
            result = sp.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                **_cflags,
            )
            if result.returncode != 0:
                self.log(f"ffmpeg tai HLS loi {desc}: {result.stderr[:500]}")
                tmp.unlink(missing_ok=True)
                playlist_tmp.unlink(missing_ok=True)
                return False
            if not tmp.exists() or tmp.stat().st_size <= 1024:
                self.log(f"ffmpeg tai HLS loi {desc}: output rong")
                tmp.unlink(missing_ok=True)
                playlist_tmp.unlink(missing_ok=True)
                return False
            tmp.replace(output)
            playlist_tmp.unlink(missing_ok=True)
            self.log(f"{desc} OK (HLS/m3u -> MP4, {output.stat().st_size} bytes)")
            return True
        except sp.TimeoutExpired:
            self.log(f"TIMEOUT {desc} (HLS/m3u)")
        except Exception as exc:
            self.log(f"LOI {desc} (HLS/m3u): {exc}")
        tmp.unlink(missing_ok=True)
        playlist_tmp.unlink(missing_ok=True)
        return False

    def _download_episode(self, ep: XSEpisode) -> bool:
        """Download DramaWave HLS video and subtitle for one episode."""
        if self._stop.is_set() or not ep.selected:
            return False

        folder = self.movie.save_dir / self.movie.folder_name
        folder.mkdir(parents=True, exist_ok=True)

        base = self._episode_base_name(ep)
        video_path = folder / f"{base}.mp4"

        if video_path.exists() and video_path.stat().st_size > 1024 and ep.status == "done":
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded", self.instance_id)
            self.log(f"SKIP video tap {ep.episode} (da ton tai)")
        else:
            self.episode_status.emit(ep.episode, "downloading", self.instance_id)
            if self._is_hls_url(ep.play):
                dl_ok = self._download_hls_video(ep.play, video_path, f"video tap {ep.episode}")
            else:
                dl_ok = self._download_file(ep.play, video_path, f"video tap {ep.episode}")
            if not dl_ok:
                ep.status = "error"
                ep.error_msg = "download video failed"
                self.episode_status.emit(ep.episode, "error", self.instance_id)
                return False
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded", self.instance_id)

        if self.download_sub and ep.subtitle_url:
            ep.sub_path = None
            for ext in ("srt", "vtt", "txt"):
                old_sub = folder / f"{base}.{ext}"
                if old_sub.exists():
                    try:
                        old_sub.unlink()
                        self.log(f"xoa sub cu tap {ep.episode}: {old_sub.name}")
                    except Exception as exc:
                        self.log(f"khong xoa duoc sub cu tap {ep.episode} ({old_sub.name}): {exc}")
            try:
                response = requests.get(ep.subtitle_url, headers=NETSHORT_DOWNLOAD_HEADERS, timeout=30, verify=False)
                response.raise_for_status()
                if not response.content or not response.content.strip():
                    ep.error_msg = "empty subtitle download"
                    self._record_subtitle_error(
                        ep,
                        "empty subtitle download",
                        subtitle_url=ep.subtitle_url,
                        bytes=0,
                    )
                    self.log(f"sub tap {ep.episode} empty")
                    return True
                ext = _ns_detect_sub_ext(response.content)
                sub_path = folder / f"{base}.{ext}"
                with open(sub_path, "wb") as f:
                    f.write(response.content)
                ep.sub_path = sub_path
                self.log(f"sub tap {ep.episode} OK ({ext}, {len(response.content)} bytes)")
            except Exception as exc:
                ep.error_msg = f"subtitle download failed: {exc}"
                self._record_subtitle_error(
                    ep,
                    "subtitle download failed",
                    subtitle_url=ep.subtitle_url,
                    error=str(exc),
                )
                self.log(f"sub tap {ep.episode} loi: {exc}")


# ── XSFetchFromSupabaseWorker ─────────────────────────────────────────────────

class XSFetchFromSupabaseWorker(QtCore.QThread):
    """Fetch episodes from Supabase nestShort_crawl instead of direct API call.

    Emits the same signals as XSFetchWorker so the tab can reuse existing handlers.
    Credentials (SUPABASE_URL, SUPABASE_KEY) are loaded from the project .env file.
    """

    success = QtCore.Signal(list, str, str, int)   # episodes, movie_name, movie_id, instance_id
    error   = QtCore.Signal(str, int)              # msg, instance_id
    log_msg = QtCore.Signal(str, int)              # msg, instance_id

    _ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
    _TABLE    = "nestShort_crawl"

    def __init__(self, movie_id: str):
        super().__init__()
        self.movie_id    = movie_id
        self.instance_id: int = uuid.uuid4().int & 0x7FFFFFFF

    def run(self) -> None:
        import os as _os
        from .sync_movies_supabase import (
            DEFAULT_SUPABASE_KEY,
            DEFAULT_SUPABASE_URL,
            load_env_file,
        )

        load_env_file(self._ENV_PATH)
        supabase_url: str = _os.environ.get("SUPABASE_URL", "").strip() or DEFAULT_SUPABASE_URL
        supabase_key: str = _os.environ.get("SUPABASE_KEY", "").strip() or DEFAULT_SUPABASE_KEY

        if not supabase_url or not supabase_key:
            self.error.emit(
                "SUPABASE_URL / SUPABASE_KEY chưa cấu hình trong .env",
                self.instance_id,
            )
            return

        try:
            endpoint = supabase_url.rstrip("/") + f"/rest/v1/{self._TABLE}"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            }
            resp = requests.get(
                endpoint,
                headers=headers,
                params={
                    "shortPlayId": f"eq.{self.movie_id}",
                    "select": "shortPlayId,showName,total,captured,status,raw",
                    "limit": "1",
                },
                timeout=30,
                verify=False,
            )
            resp.raise_for_status()
            rows: list = resp.json() if isinstance(resp.json(), list) else []

            if not rows:
                self.error.emit(
                    f"shortPlayId={self.movie_id} chưa có trong DB.\n"
                    "Vui lòng gửi yêu cầu crawl qua tab 'Yêu cầu Crawl' trước.",
                    self.instance_id,
                )
                return

            row = rows[0]
            crawl_status: str = row.get("status", "unknown")
            if crawl_status != "completed":
                captured = row.get("captured", 0)
                total    = row.get("total", 0)
                self.error.emit(
                    f"Dữ liệu chưa sẵn sàng: status={crawl_status} "
                    f"({captured}/{total} tập).\nVui lòng đợi crawl hoàn thành.",
                    self.instance_id,
                )
                return

            raw: dict = row.get("raw") or {}
            if not raw:
                self.error.emit(
                    f"raw JSON rỗng trong {self._TABLE}.", self.instance_id
                )
                return

            show_name: str = row.get("showName") or raw.get("showName", "")
            episodes = self._parse_raw_episodes(raw, show_name)

            if not episodes:
                self.error.emit(
                    f"Không parse được tập nào từ raw JSON "
                    f"(total={row.get('total')}, captured={row.get('captured')}).",
                    self.instance_id,
                )
                return

            self.log_msg.emit(
                f"DB: {len(episodes)} tập (status={crawl_status}, show={show_name})",
                self.instance_id,
            )
            self.success.emit(episodes, show_name, self.movie_id, self.instance_id)

        except Exception as exc:
            self.error.emit(str(exc), self.instance_id)

    def _parse_raw_episodes(self, raw: dict, show_name: str) -> list:
        """Parse raw DB JSON into XSEpisode list. Override for provider-specific lock logic."""
        return _ns_parse_episodes(raw, show_name)


class DramaWaveFetchFromSupabaseWorker(XSFetchFromSupabaseWorker):
    """Fetch episodes from dramawave_crawl instead of nestShort_crawl."""

    _TABLE = "dramawave_crawl"

    def _parse_raw_episodes(self, raw: dict, show_name: str) -> list:
        """DramaWave: lock only when play URL is missing (subtitle not required)."""
        episodes = _ns_parse_episodes(raw, show_name)
        for ep in episodes:
            ep.is_locked = not ep.play
        return episodes
