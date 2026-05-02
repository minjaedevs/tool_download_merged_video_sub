"""M3U8 Tab worker: downloads a single URL via requests (direct) or ffmpeg (HLS)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess as sp
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from PySide6 import QtCore

logger = logging.getLogger(__name__)

DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Referer": "https://xemshort.top/",
}


class M3U8DownloadWorker(QtCore.QThread):
    """Background thread that downloads a single URL.

    fmt == "mp4"  → requests streaming download (direct video URL).
    fmt == "m3u8" → ffmpeg HLS download  (-i url -c copy output.mp4).

    Signals (all carry instance_id as first arg for routing):
        progress(instance_id, status, pct, speed, eta, title)
        finished(instance_id, success, error_msg)
        log_msg(instance_id, msg)
        output_ready(instance_id, abs_path)
    """

    progress     = QtCore.Signal(int, str, float, str, str, str)
    finished     = QtCore.Signal(int, bool, str)
    log_msg      = QtCore.Signal(int, str)
    output_ready = QtCore.Signal(int, str)

    def __init__(
        self,
        url: str,
        save_dir: Path,
        name: str,
        fmt: str = "mp4",
        ytdlp_args: str = "",   # kept for API compatibility, unused
    ):
        super().__init__()
        self.url = url
        self.save_dir = Path(save_dir)
        self.name = name
        self.fmt = fmt

        self.instance_id: int = uuid.uuid4().int & 0x7FFFFFFF
        self._mutex = QtCore.QMutex()
        self._aborted = False
        # ytdlp_args kept in signature for API compatibility, not used in this flow

    # ------------------------------------------------------------------ stop
    def stop(self):
        """Request graceful abort."""
        with QtCore.QMutexLocker(self._mutex):
            self._aborted = True

    def _is_aborted(self) -> bool:
        with QtCore.QMutexLocker(self._mutex):
            return self._aborted

    # ------------------------------------------------------------------ helpers
    def _safe_name(self) -> str:
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]]+', "_", self.name).strip("._ ")
        return safe or "video"

    def _unique_output_path(self, ext: str = ".mp4") -> Path:
        """Return save_dir/{safe_name}.mp4, incrementing _N if already exists."""
        safe = self._safe_name()
        out = self.save_dir / f"{safe}{ext}"
        n = 1
        while out.exists():
            out = self.save_dir / f"{safe}_{n}{ext}"
            n += 1
        return out

    def _get_ffmpeg_path(self) -> Optional[Path]:
        """Locate ffmpeg: bundled next to EXE first, then system PATH."""
        for fname in ("ffmpeg", "ffmpeg.exe"):
            candidate = Path(sys.executable).parent / fname
            if candidate.exists():
                return candidate
        found = shutil.which("ffmpeg")
        return Path(found) if found else None

    def _get_ffprobe_path(self) -> Optional[Path]:
        """Locate ffprobe alongside ffmpeg."""
        for fname in ("ffprobe", "ffprobe.exe"):
            candidate = Path(sys.executable).parent / fname
            if candidate.exists():
                return candidate
        found = shutil.which("ffprobe")
        return Path(found) if found else None

    def _probe_duration_ms(self) -> int:
        """Return total duration in ms via ffprobe; 0 if unknown or on error.

        Used to calculate percentage progress for HLS/M3U8 downloads.
        ffprobe fetches the playlist and sums segment durations, so this works
        for both VOD and (partially) for live streams.
        """
        ffprobe = self._get_ffprobe_path()
        if not ffprobe:
            return 0
        _cflags = {"creationflags": sp.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        try:
            result = sp.run(
                [
                    str(ffprobe),
                    "-v", "quiet",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    self.url,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                **_cflags,
            )
            if result.returncode == 0:
                raw = result.stdout.strip()
                if raw and raw != "N/A":
                    return int(float(raw) * 1000)
        except Exception:
            pass
        return 0

    @staticmethod
    def _find_output_video(base_dir: Path, instance_id: int, title: str = "") -> Optional[Path]:
        """Fallback glob search — used by m3utab.py if output_ready was not emitted."""
        if not base_dir.exists():
            return None

        candidates: list[Path] = []

        if title:
            safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]]+', "_", title)
            for ext in (".mp4", ".mkv", ".webm", ".ts"):
                candidates += list(base_dir.glob(f"{safe_title}{ext}"))
                candidates += list(base_dir.glob(f"{safe_title}_*{ext}"))

        if not candidates:
            candidates = (
                list(base_dir.glob("*.mp4"))
                + list(base_dir.glob("*.mkv"))
                + list(base_dir.glob("*.webm"))
                + list(base_dir.glob("*.ts"))
            )

        seen: set[Path] = set()
        unique: list[Path] = []
        for p in candidates:
            if p.name.endswith((".tmp", ".part", ".ytdl")):
                continue
            if p not in seen:
                seen.add(p)
                unique.append(p)

        if unique:
            unique.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return unique[0]
        return None

    # Sentinel returned by _download_direct when it detects an HLS URL
    _HLS_DETECTED = "__HLS_DETECTED__"

    # ------------------------------------------------------------------ download methods
    # Minimum file size: reject anything that looks like an error page or redirect
    _MIN_VIDEO_SIZE = 10 * 1024   # 10 KiB

    def _download_direct(self, out_path: Path) -> tuple[bool, str]:
        """Download direct video URL via requests (3 retries, .part atomicity).

        Validates Content-Type and minimum file size before accepting the download.
        If the server responds with an HLS playlist (application/vnd.apple.mpegurl etc.)
        returns _HLS_DETECTED so run() can switch to ffmpeg.
        """
        iid = self.instance_id
        tmp = out_path.with_suffix(out_path.suffix + ".part")

        for attempt in range(1, 4):
            if self._is_aborted():
                tmp.unlink(missing_ok=True)
                return False, "Stopped by user"
            try:
                with requests.get(
                    self.url,
                    headers=DOWNLOAD_HEADERS,
                    stream=True,
                    timeout=30,
                    allow_redirects=True,
                ) as r:
                    r.raise_for_status()

                    ct = r.headers.get("Content-Type", "").lower()
                    hls_mimes = (
                        "application/vnd.apple.mpegurl",
                        "application/x-mpegurl",
                        "application/m3u8",
                    )
                    if ct in hls_mimes or "m3u8" in ct:
                        r.close()
                        self.log_msg.emit(iid, f"Phát hiện M3U8 stream — cần ffmpeg")
                        tmp.unlink(missing_ok=True)
                        return False, self._HLS_DETECTED

                    if ct and not any(t in ct for t in ("video", "octet-stream", "application/octet")):
                        r.close()
                        self.log_msg.emit(
                            iid,
                            f"Sai Content-Type: {ct} (thử lại {attempt}/3)",
                        )
                        tmp.unlink(missing_ok=True)
                        if attempt < 3:
                            time.sleep(2 * attempt)
                        continue

                    total = int(r.headers.get("Content-Length", 0))
                    downloaded = 0
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            if self._is_aborted():
                                tmp.unlink(missing_ok=True)
                                return False, "Stopped by user"
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total > 0:
                                    pct = downloaded / total * 100
                                    size_mb = f"{downloaded / 1_048_576:.1f} MB"
                                    self.progress.emit(iid, "downloading", pct, size_mb, "", "")

                tmp_size = tmp.stat().st_size
                if tmp_size < self._MIN_VIDEO_SIZE:
                    tmp.unlink(missing_ok=True)
                    self.log_msg.emit(iid, f"Kích thước quá nhỏ ({tmp_size} bytes) — thử lại {attempt}/3")
                    if attempt < 3:
                        time.sleep(2 * attempt)
                    continue

                tmp.rename(out_path)
                return True, ""

            except requests.exceptions.Timeout:
                self.log_msg.emit(iid, f"Timeout (lần {attempt}/3)")
            except requests.exceptions.ConnectionError as e:
                self.log_msg.emit(iid, f"Connection error (lần {attempt}/3): {e}")
            except Exception as e:
                self.log_msg.emit(iid, f"Lỗi tải (lần {attempt}/3): {e}")

            tmp.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(2 * attempt)

        return False, "Download thất bại sau 3 lần thử"

    def _download_ffmpeg(self, out_path: Path) -> tuple[bool, str]:
        """Download HLS/M3U8 stream via ffmpeg -i url -c copy with progress.

        Parses duration to calculate percentage, adds referer for anti-hotlink sites,
        and verifies output size before returning success.
        """
        iid = self.instance_id
        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            return False, "Không tìm thấy ffmpeg. Cài ffmpeg và thêm vào PATH."

        out_path.parent.mkdir(parents=True, exist_ok=True)
        _cflags = {"creationflags": sp.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

        # Extract base URL for referer from the M3U8 URL
        from urllib.parse import urlparse
        referer = ""
        try:
            parsed = urlparse(self.url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"
        except Exception:
            pass

        cmd = [
            str(ffmpeg_path), "-y",
            "-loglevel", "warning",        # global — before first -i
            "-progress", "pipe:1",         # global — write progress to stdout
            "-headers", f"Referer: {referer}",
            "-i", self.url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",     # fix AAC-ADTS → MP4 AAC (correct audio duration)
            "-avoid_negative_ts", "make_zero",  # normalise HLS PTS to start at 0
            "-movflags", "+faststart",     # write moov at start → seekable output
            "-f", "mp4",
            str(out_path),
        ]
        self.log_msg.emit(iid, f"ffmpeg: {ffmpeg_path.name}  {self.url[:60]}...")

        # Probe duration BEFORE starting ffmpeg so the stdout pipe is never left
        # unread while we block — avoids the 4 KiB Windows pipe-buffer deadlock.
        self.log_msg.emit(iid, "Đang lấy thông tin video...")
        total_duration_ms = self._probe_duration_ms()
        if total_duration_ms > 0:
            total_s = total_duration_ms // 1000
            self.log_msg.emit(iid, f"Thời lượng: {total_s // 60}:{total_s % 60:02d}")
        else:
            self.log_msg.emit(iid, "Không xác định thời lượng — hiển thị thời gian thực")

        if self._is_aborted():
            return False, "Stopped by user"

        proc: Optional[sp.Popen] = None
        try:
            proc = sp.Popen(
                cmd,
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                text=True,
                **_cflags,
            )

            out_time_ms = 0
            last_size_kb = 0
            last_speed_x = 1.0    # realtime multiplier from ffmpeg  speed=Nx

            # Drain stderr in a separate thread to avoid deadlock
            import threading
            stderr_lines: list[str] = []
            stderr_lock = threading.Lock()

            def drain_stderr():
                assert proc is not None and proc.stderr is not None
                for line in proc.stderr:
                    with stderr_lock:
                        stderr_lines.append(line.rstrip())

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()

            for line in proc.stdout or []:
                if self._is_aborted():
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except sp.TimeoutExpired:
                        proc.kill()
                    stderr_thread.join(timeout=1)
                    out_path.unlink(missing_ok=True)
                    return False, "Stopped by user"

                line = line.strip()
                if not line or "=" not in line:
                    continue

                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()

                if key == "out_time_ms":
                    # NOTE: ffmpeg names this out_time_ms but the unit is microseconds
                    try:
                        out_time_ms = int(val) // 1000   # µs → ms
                    except ValueError:
                        pass
                elif key == "total_size":
                    try:
                        last_size_kb = int(val) // 1024
                    except ValueError:
                        pass
                elif key == "speed":
                    # "2.00x" → 2.0
                    try:
                        last_speed_x = float(val.rstrip("x")) if val not in ("N/A", "0x") else 1.0
                    except ValueError:
                        pass

                if out_time_ms > 0:
                    size_str = f"{last_size_kb} KiB"
                    if total_duration_ms > 0:
                        pct = min(out_time_ms / total_duration_ms * 100, 99.9)
                        remaining_ms = total_duration_ms - out_time_ms
                        eta_s = int(remaining_ms / max(last_speed_x, 0.01) / 1000)
                        eta_str = f"{eta_s // 60}:{eta_s % 60:02d}"
                        self.progress.emit(iid, "downloading", pct, size_str, eta_str, "")
                    else:
                        # Unknown total — show elapsed in ETA column
                        elapsed_s = out_time_ms // 1000
                        eta_str = f"{elapsed_s // 60}:{elapsed_s % 60:02d}"
                        self.progress.emit(iid, "downloading", 0.0, size_str, eta_str, "")

            ret = proc.wait()

            # Drain remaining stderr
            stderr_thread.join(timeout=2)
            with stderr_lock:
                stderr_out = "\n".join(stderr_lines).strip()

            if self._is_aborted():
                out_path.unlink(missing_ok=True)
                return False, "Stopped by user"

            if ret != 0:
                if stderr_out:
                    preview = stderr_out.splitlines()[:5]
                    self.log_msg.emit(iid, "ffmpeg error:\n" + "\n".join(preview))
                return False, f"ffmpeg exit code: {ret}"

            # Verify output file
            if not out_path.exists():
                return False, "ffmpeg tạo file thất bại"
            size = out_path.stat().st_size
            if size < self._MIN_VIDEO_SIZE * 1024:
                out_path.unlink(missing_ok=True)
                return False, f"File quá nhỏ ({size} bytes) — có thể URL không hợp lệ"

            self.log_msg.emit(iid, f"ffmpeg done: {size // 1024} KiB")
            return True, ""

        except FileNotFoundError:
            return False, "ffmpeg không tìm thấy"
        except Exception as e:
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            return False, str(e)
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ------------------------------------------------------------------ run
    def run(self):
        """Entry point: choose download method based on fmt, emit signals.

        Auto-detects M3U8 URLs (by extension or Content-Type) and uses ffmpeg.
        If a direct-URL download detects an HLS playlist it returns _HLS_DETECTED
        so we retry the same URL with ffmpeg.
        """
        iid = self.instance_id
        self.save_dir.mkdir(parents=True, exist_ok=True)

        out_path = self._unique_output_path()
        self.log_msg.emit(iid, f"Starting: {self.name}  →  {out_path.name}")
        self.progress.emit(iid, "downloading", 0.0, "", "", "")

        # Auto-detect M3U8: by extension in URL or by Content-Type sentinel from direct path
        use_ffmpeg = (
            self.fmt == "m3u8"
            or ".m3u8" in self.url.lower()
            or ".m3u" in self.url.lower()
        )

        if use_ffmpeg:
            ok, err = self._download_ffmpeg(out_path)
        else:
            ok, err = self._download_direct(out_path)
            # If direct path detected an HLS stream, retry with ffmpeg
            if err == self._HLS_DETECTED:
                self.log_msg.emit(iid, "Đang chuyển sang ffmpeg cho M3U8 stream...")
                ok, err = self._download_ffmpeg(out_path)

        if self._is_aborted():
            self.log_msg.emit(iid, "Đã dừng.")
            self.progress.emit(iid, "Stopped", 0.0, "", "", "")
            self.finished.emit(iid, False, "Stopped by user")
            return

        if ok and out_path.exists():
            size = out_path.stat().st_size
            if size < self._MIN_VIDEO_SIZE * 1024:
                out_path.unlink(missing_ok=True)
                self.log_msg.emit(iid, f"Kích thước quá nhỏ ({size} bytes) — download thất bại")
                self.progress.emit(iid, "Error", 0.0, "", "", "")
                self.finished.emit(iid, False, "File quá nhỏ")
                return
            size_kb = size // 1024
            self.log_msg.emit(iid, f"Done: {out_path.name} ({size_kb} KiB)")
            self.output_ready.emit(iid, str(out_path))
            self.progress.emit(iid, "Done", 100.0, "", "", "")
            self.finished.emit(iid, True, "")
        else:
            self.log_msg.emit(iid, f"Lỗi: {err}")
            self.progress.emit(iid, "Error", 0.0, "", "", "")
            self.finished.emit(iid, False, err)
