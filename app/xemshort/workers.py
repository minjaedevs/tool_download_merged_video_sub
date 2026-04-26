"""XemShort background worker threads: XSFetchWorker, XSDownloadMergeWorker."""
from __future__ import annotations

import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
import threading

import requests
from PySide6 import QtCore

from .models import XSEpisode, XSMovie
from .helpers import (
    _ns_color_to_ass,
    _ns_detect_sub_ext,
    _ns_escape_path,
    _ns_get_video_duration,
    _ns_get_video_duration_secs,
    _ns_install_fonts,
    _ns_parse_episodes,
)
from .cache import _ns_cache_get, _ns_cache_key, _ns_cache_set

# API headers used for all XemShort HTTP requests
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

_MERGE_SIDECAR_FILE = ".merge_settings.json"


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

    success   = QtCore.Signal(list, str)   # episodes, movie_name
    cache_hit = QtCore.Signal(list, str)   # episodes, movie_name (served from cache)
    error     = QtCore.Signal(str)

    def __init__(self, api_url: str, movie_id: str):
        """Store API URL and movie ID for the fetch request."""
        super().__init__()
        self.api_url = api_url
        self.movie_id = movie_id

    def run(self):
        """Fetch episodes, checking in-memory cache first (TTL=30 min)."""
        key = _ns_cache_key(self.api_url, self.movie_id)
        cached = _ns_cache_get(key)
        if cached is not None:
            episodes, movie_name = cached
            self.cache_hit.emit(episodes, movie_name)
            return

        url = self.api_url.replace("{movie_id}", self.movie_id)
        try:
            import subprocess as sp, json as _json
            result = sp.run(
                ["curl", "-s", "--max-time", "15",
                 "-H", f"User-Agent: {NETSHORT_API_HEADERS['User-Agent']}",
                 "-H", "Accept: */*",
                 "-H", f"Origin: {NETSHORT_API_HEADERS['Origin']}",
                 "-H", f"Referer: {NETSHORT_API_HEADERS['Referer']}",
                 "-H", "short-source: web",
                 url],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode != 0 or not result.stdout.strip():
                # Fall back to requests
                r = requests.get(url, headers=NETSHORT_API_HEADERS, timeout=15)
                r.raise_for_status()
                data = r.json()
            else:
                data = _json.loads(result.stdout)

            movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
            episodes = _ns_parse_episodes(data, movie_name)
            if not episodes:
                self.error.emit("Không tìm thấy tập nào trong API response.")
                return

            _ns_cache_set(key, episodes, movie_name)
            self.success.emit(episodes, movie_name)

        except Exception as e:
            self.error.emit(str(e))


# Backward-compat alias
NSFetchWorker = XSFetchWorker


class XSDownloadMergeWorker(QtCore.QThread):
    """Background thread: downloads video + subtitle then optionally hardcodes sub via ffmpeg."""

    log_msg        = QtCore.Signal(str)
    episode_status = QtCore.Signal(int, str)
    progress       = QtCore.Signal(int, int)
    finished_all   = QtCore.Signal()

    def __init__(self, movie: XSMovie, concurrency: int, download_sub: bool,
                 do_merge: bool, crf: int, preset: str,
                 sub_font: str = "UTM Alter Gothic", sub_size: int = 20,
                 sub_margin_v: int = 30, sub_color: str = "Trắng",
                 sub_bold: bool = True, sub_italic: bool = False):
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
        self.sub_bold = sub_bold
        self.sub_italic = sub_italic
        self._stop = threading.Event()

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
                                  stream=True, timeout=15) as r:
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

        folder = self.movie.save_dir / self.movie.folder_name
        folder.mkdir(parents=True, exist_ok=True)

        padding = len(str(self.movie.total))
        base = f"ep{str(ep.episode).zfill(padding)}"

        video_path = folder / f"{base}.mp4"

        # ── Video ────────────────────────────────────────────────────────────
        if video_path.exists() and video_path.stat().st_size > 1024 and ep.status != "error":
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded")
            self.log(f"SKIP video tập {ep.episode} (đã tồn tại)")
        else:
            self.episode_status.emit(ep.episode, "downloading")
            dl_ok = self._download_file(ep.play, video_path, f"video tập {ep.episode}")
            if not dl_ok:
                ep.status = "error"
                ep.error_msg = "download video failed"
                self.episode_status.emit(ep.episode, "error")
                return False
            ep.video_path = video_path
            ep.status = "downloaded"
            self.episode_status.emit(ep.episode, "downloaded")

        # ── Subtitle ────────────────────────────────────────────────────────
        if self.download_sub and ep.subtitle_url:
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

        return True

    def _merge_episode(self, ep: XSEpisode) -> bool:
        """Burn subtitle into video with ffmpeg; re-merge only if sub is newer than output."""
        if self._stop.is_set() or not ep.video_path or not ep.video_path.exists():
            return False

        merge_dir = self.movie.save_dir / self.movie.folder_name / "merged"
        merge_dir.mkdir(parents=True, exist_ok=True)

        padding = len(str(self.movie.total))
        base = f"ep{str(ep.episode).zfill(padding)}"
        out_path = merge_dir / f"{base}_merged.mp4"

        if out_path.exists() and out_path.stat().st_size > 1024:
            stored = _load_merge_sidecar(merge_dir)
            if stored and stored != self._settings_fingerprint():
                self.log(f"tập {ep.episode}: settings thay đổi -- re-merge...")
            else:
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
            ep.merge_note = "error"
            self.episode_status.emit(ep.episode, "error")
            return False

        sub_filter = _ns_escape_path(ep.sub_path)

        # Locate bundled fonts directory (next to EXE first, then next to app/ dir)
        fonts_dir = Path(sys.executable).parent / "fonts"
        if not fonts_dir.exists():
            # __file__ = app/xemshort/workers.py  →  .parent.parent = app/
            fonts_dir = Path(__file__).parent.parent / "fonts"

        if fonts_dir.exists():
            _ns_install_fonts(fonts_dir, self.log)
            fonts_dir_escaped = _ns_escape_path(fonts_dir)
            vf_filter = (
                f"subtitles='{sub_filter}'"
                f":fontsdir='{fonts_dir_escaped}'"
                f":force_style='FontName={self.sub_font},FontSize={self.sub_size},"
                f"PrimaryColour={_ns_color_to_ass(self.sub_color)},"
                f"OutlineColour=&H00000000,"
                f"BorderStyle=1,Outline=1,Shadow=0,"
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
                f"BorderStyle=1,Outline=1,Shadow=0,"
                f"Bold={-1 if self.sub_bold else 0},"
                f"Italic={1 if self.sub_italic else 0},"
                f"Alignment=2,MarginV={self.sub_margin_v}'"
            )

        # Get exact source duration to pin output length
        orig_secs = _ns_get_video_duration_secs(ep.video_path)

        import subprocess as sp
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
            _save_merge_sidecar(merge_dir, self._settings_fingerprint())

            # Duration check
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
                self.progress.emit(done, total * (2 if self.do_merge else 1))

        if self._stop.is_set():
            self.log("Đã dừng.")
            self.finished_all.emit()
            return

        merge_ok = 0
        merge_fail = 0

        if self.do_merge:
            if not self._get_ffmpeg_path():
                self.log("CẢNH BÁO: không tìm thấy ffmpeg -- bỏ qua merge.")
            else:
                for ep in selected:
                    if self._stop.is_set():
                        break
                    if ep.status == "downloaded" and ep.video_path and ep.video_path.exists():
                        ok = self._merge_episode(ep)
                        if ok:
                            done += 1
                            merge_ok += 1
                        else:
                            merge_fail += 1
                        self.progress.emit(done, total * 2)

        # Summary log
        dl_summary = f"Tải: {dl_ok}/{total} thành công" + (f", {dl_fail} lỗi" if dl_fail else "")
        if self.do_merge:
            merge_summary = f"Merge: {merge_ok}/{dl_ok} thành công" + (f", {merge_fail} lỗi" if merge_fail else "")
            self.log(f"[Kết quả] {dl_summary} | {merge_summary}")
        else:
            self.log(f"[Kết quả] {dl_summary}")

        # Always finish at 100%
        grand_total = total * (2 if self.do_merge else 1)
        self.progress.emit(grand_total, grand_total)

        self.movie.end_time = time.time()
        self.log(f"=== Hoàn tất '{self.movie.name}' ===")
        self.finished_all.emit()


# Backward-compat alias
NSDownloadMergeWorker = XSDownloadMergeWorker
