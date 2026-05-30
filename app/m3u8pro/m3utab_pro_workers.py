"""M3U8 Pro worker using yt-dlp concurrent fragments."""
from __future__ import annotations

import re
import shutil
import subprocess as sp
import sys
from pathlib import Path
from typing import Optional

from m3u8.m3utab_workers import DOWNLOAD_HEADERS, M3U8DownloadWorker

try:
    from utils import BIN_DIR, ROOT
except Exception:
    BIN_DIR = None
    ROOT = Path(__file__).parent


class YtDlpM3U8DownloadWorker(M3U8DownloadWorker):
    """Download a single M3U8 URL with yt-dlp -N fragment concurrency."""

    def __init__(
        self,
        url: str,
        save_dir: Path,
        name: str,
        fmt: str = "m3u8",
        fragments: int = 16,
        container_mode: str = "mp4",
    ):
        super().__init__(url=url, save_dir=save_dir, name=name, fmt=fmt)
        self.fragments = max(1, int(fragments))
        self.container_mode = container_mode
        self._proc: Optional[sp.Popen] = None

    def stop(self):
        super().stop()

    def _unique_temp_prefix(self, out_path: Path) -> Path:
        return out_path.with_name(f"{out_path.stem}__{self.instance_id}")

    @staticmethod
    def _is_fixup_or_merge_error(lines: list[str]) -> bool:
        text = "\n".join(lines[-40:]).lower()
        return any(token in text for token in ("fixup", "fixing", "merger", "merge", "ffmpeg"))

    @staticmethod
    def _rename_unique(src: Path, dst: Path) -> Path:
        if src == dst:
            return src
        candidate = dst
        n = 1
        while candidate.exists():
            candidate = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
            n += 1
        src.rename(candidate)
        return candidate

    def _find_temp_output(self, prefix: Path) -> Optional[Path]:
        candidates = [
            p for p in prefix.parent.glob(f"{prefix.name}*")
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp"))
        ]
        candidates = [
            p for p in candidates
            if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".ts")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def _final_output_path(self, out_path: Path, found: Path) -> Path:
        """Return the visible final file path for the selected container mode."""
        if self.container_mode == "ts":
            return out_path.with_suffix(".ts")
        return out_path.with_suffix(found.suffix)

    def _get_yt_dlp_path(self) -> Optional[Path]:
        names = ("yt-dlp.exe", "yt-dlp") if sys.platform == "win32" else ("yt-dlp", "yt-dlp.exe")
        dirs = [
            Path(sys.executable).parent,
            Path(__file__).parent,
            Path(__file__).parent / "bin",
            ROOT / "bin",
        ]
        if BIN_DIR is not None:
            dirs.append(BIN_DIR)
        for directory in dirs:
            for fname in names:
                candidate = directory / fname
                if candidate.exists():
                    return candidate
        found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        return Path(found) if found else None

    @staticmethod
    def _parse_ytdlp_progress(line: str) -> tuple[float | None, str, str]:
        pct = None
        speed = ""
        eta = ""

        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m:
            try:
                pct = float(m.group(1))
            except ValueError:
                pct = None

        m = re.search(r"\bat\s+([0-9.]+(?:KiB|MiB|GiB|B)/s)", line)
        if m:
            speed = m.group(1)

        m = re.search(r"\bETA\s+([^\s]+)", line)
        if m:
            eta = m.group(1)

        return pct, speed, eta

    def _download_ytdlp(self, out_path: Path) -> tuple[bool, str, Path | None]:
        iid = self.instance_id
        yt_dlp_path = self._get_yt_dlp_path()
        if not yt_dlp_path:
            return False, "Không tìm thấy yt-dlp.exe. Cài yt-dlp hoặc đặt yt-dlp.exe cạnh app/bin.", None

        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp_prefix = self._unique_temp_prefix(out_path)
        template = str(temp_prefix) + ".%(ext)s"
        referer = self._origin_referer()
        ffmpeg_path = self._get_ffmpeg_path()
        cflags = {"creationflags": sp.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

        cmd = [
            str(yt_dlp_path),
            "--newline",
            "--no-warnings",
            "--retries", "10",
            "--fragment-retries", "10",
            "--concurrent-fragments", str(self.fragments),
            "--add-header", f"User-Agent: {DOWNLOAD_HEADERS['User-Agent']}",
            "--add-header", f"Accept: {DOWNLOAD_HEADERS['Accept']}",
            "--add-header", f"Accept-Language: {DOWNLOAD_HEADERS['Accept-Language']}",
        ]
        if self.container_mode == "ts":
            cmd += ["--hls-use-mpegts"]
        else:
            cmd += ["--no-hls-use-mpegts", "--merge-output-format", "mp4"]
        if ffmpeg_path:
            cmd += ["--ffmpeg-location", str(ffmpeg_path.parent)]
        if referer:
            cmd += ["--add-header", f"Referer: {referer}"]
        cmd += ["-o", template, self.url]

        if ffmpeg_path:
            self.log_msg.emit(
                iid,
                f"yt-dlp: {yt_dlp_path.name} -N {self.fragments}, "
                f"mode={self.container_mode}, ffmpeg={ffmpeg_path.name}",
            )
        else:
            self.log_msg.emit(
                iid,
                f"yt-dlp: mode={self.container_mode}, không tìm thấy ffmpeg cho bước fixup/merge",
            )
        self.log_msg.emit(iid, f"yt-dlp URL: {self.url[:80]}...")

        lines: list[str] = []
        try:
            self._proc = sp.Popen(
                cmd,
                stdout=sp.PIPE,
                stderr=sp.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **cflags,
            )
            assert self._proc.stdout is not None

            for raw in self._proc.stdout:
                if self._is_aborted():
                    self.stop()
                    try:
                        self._proc.wait(timeout=3)
                    except sp.TimeoutExpired:
                        self._proc.kill()
                    return False, "Stopped by user", None

                line = raw.strip()
                if not line:
                    continue
                lines.append(line)

                pct, speed, eta = self._parse_ytdlp_progress(line)
                if pct is not None:
                    self.progress.emit(iid, "downloading", min(pct, 99.9), speed, eta, "")
                elif line.startswith("[Merger]") or line.startswith("[Fixup]"):
                    self.log_msg.emit(iid, line)

            ret = self._proc.wait()
            if ret != 0:
                found = self._find_temp_output(temp_prefix)
                if (
                    self._is_fixup_or_merge_error(lines)
                    and found
                    and found.exists()
                    and found.stat().st_size >= self._MIN_VIDEO_SIZE * 1024
                ):
                    size = found.stat().st_size
                    final_path = self._rename_unique(found, self._final_output_path(out_path, found))
                    self.log_msg.emit(
                        iid,
                        f"yt-dlp trả exit code {ret} sau bước fixup nhưng file đã tồn tại: "
                        f"{final_path.name} ({size // 1024} KiB)",
                    )
                    return True, "", final_path

                preview = "\n".join(lines[-20:])
                if preview:
                    self.log_msg.emit(iid, "yt-dlp error:\n" + preview)
                return False, f"yt-dlp exit code: {ret}", None

            found = self._find_temp_output(temp_prefix)
            if not found or not found.exists():
                return False, "yt-dlp tải xong nhưng không tìm thấy file output", None
            size = found.stat().st_size
            if size < self._MIN_VIDEO_SIZE * 1024:
                found.unlink(missing_ok=True)
                return False, f"File quá nhỏ ({size} bytes)", None
            final_path = self._rename_unique(found, self._final_output_path(out_path, found))
            self.log_msg.emit(iid, f"yt-dlp done: {final_path.name} ({size // 1024} KiB)")
            return True, "", final_path

        except FileNotFoundError:
            return False, "yt-dlp không tìm thấy", None
        except Exception as e:
            return False, str(e), None
        finally:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def run(self):
        iid = self.instance_id
        self.save_dir.mkdir(parents=True, exist_ok=True)

        out_path = self._unique_output_path(".ts" if self.container_mode == "ts" else ".mp4")
        self.log_msg.emit(iid, f"Starting Pro: {self.name} -> {out_path.stem}.%(ext)s")
        self.progress.emit(iid, "downloading", 0.0, "", "", "")

        ok, err, final_path = self._download_ytdlp(out_path)

        if self._is_aborted():
            self.log_msg.emit(iid, "Đã dừng.")
            self.progress.emit(iid, "Stopped", 0.0, "", "", "")
            self.finished.emit(iid, False, "Stopped by user")
            return

        if ok and final_path and final_path.exists():
            self.output_ready.emit(iid, str(final_path))
            self.progress.emit(iid, "Done", 100.0, "", "", "")
            self.finished.emit(iid, True, "")
        else:
            self.log_msg.emit(iid, f"Lỗi: {err}")
            self.progress.emit(iid, "Error", 0.0, "", "", "")
            self.finished.emit(iid, False, err)
