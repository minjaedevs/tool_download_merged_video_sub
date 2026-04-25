import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess as sp
import sys
import tempfile
import urllib.request
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from utils import ItemRoles, TreeColumn

logger = logging.getLogger(__name__)

SUB_HEADERS = {
    "user-agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
    "referer": "https://xemshort.top/",
}


class DownloadWorker(QtCore.QThread):
    progress = QtCore.Signal(object, list)

    def __init__(self, item, config, link, path, preset, sub_url: str = ""):
        """Initialize the download worker thread.

        Args:
            item: The QTreeWidgetItem associated with this download.
            config: Application configuration dict containing presets and global args.
            link: URL of the media to download.
            path: Destination directory for the downloaded file.
            preset: Name of the yt-dlp preset to use from config.
            sub_url: Optional URL of an external subtitle file to merge.
        """
        super().__init__()
        self.item: QtWidgets.QTreeWidgetItem = item
        self.link = link
        self.path = path
        self.preset = preset
        self.sub_url = sub_url
        self.id = self.item.data(0, ItemRoles.IdRole)
        self._link_hash = hashlib.md5(link.encode()).hexdigest()[:12]
        self.title = ""
        self._actual_filename = ""
        self.command = self.build_command(config)
        self._mutex = QtCore.QMutex()
        self._stop = False

    def build_command(self, config):
        """Build the yt-dlp command argument list.

        Assembles the full command with output template, preset flags, and global args.

        Args:
            config: Application configuration dict.

        Returns:
            list[str]: The complete command ready to pass to subprocess.
        """
        out_path = Path(self.path)
        out_path.mkdir(parents=True, exist_ok=True)
        out_template = f"video_{self.id}.%(ext)s"

        args = [
            "yt-dlp",
            "--newline",
            "--no-simulate",
            "--progress",
            "--restrict-filenames",
            "--progress-template",
            "%(progress.status)s__SEP__%(progress._total_bytes_estimate_str)s__SEP__%(progress._percent_str)s__SEP__%(progress._speed_str)s__SEP__%(progress._eta_str)s__SEP__%(info.title)s",
            "-o", out_template,
        ]
        p_args = config["presets"][self.preset]
        g_args = config["general"].get("global_args")

        args += ["-P", self.path]
        args += p_args if isinstance(p_args, list) else shlex.split(p_args)
        args += g_args if isinstance(g_args, list) else shlex.split(g_args)
        args += ["--", self.link]
        return args

    def stop(self):
        """Request the download to stop gracefully.

        Sets the internal stop flag; the running thread will terminate the
        subprocess on the next iteration.
        """
        with QtCore.QMutexLocker(self._mutex):
            self._stop = True

    def merge_subtitle(self):
        """Download an external subtitle and merge it into the video via FFmpeg.

        Downloads the SRT/WEBVTT subtitle from ``self.sub_url``, converts it to
        SRT format, then remuxes the video into an MKV container with the subtitle
        track embedded as the default stream.  Emits progress signals throughout.

        Does nothing when ``self.sub_url`` is empty.
        """
        if not self.sub_url:
            return

        self.progress.emit(self.item, [(TreeColumn.STATUS, "Merging sub")])

        # Ensure output folder exists
        out_path = Path(self.path)
        out_path.mkdir(parents=True, exist_ok=True)

        sub_fd, sub_path = tempfile.mkstemp(suffix=".srt")
        os.close(sub_fd)

        try:
            logger.info(f"Downloading subtitle from {self.sub_url}")
            req = urllib.request.Request(self.sub_url, headers=SUB_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                sub_data = resp.read()
            Path(sub_path).write_bytes(sub_data)
            logger.info(f"Sub downloaded to {sub_path} ({len(sub_data)} bytes)")

        except Exception as e:
            logger.error(f"Failed to download sub: {e}")
            Path(sub_path).unlink(missing_ok=True)
            self.progress.emit(self.item, [(TreeColumn.STATUS, "Sub dl failed")])
            return

        video_path = self._find_video_in_path(Path(self.path))
        if not video_path:
            logger.error("Could not find video file to merge subtitle")
            Path(sub_path).unlink(missing_ok=True)
            self.progress.emit(self.item, [(TreeColumn.STATUS, "Video not found")])
            return

        logger.info(f"Selected video: {video_path} (exists={video_path.exists()})")

        # Probe streams for debug
        ffprobe_path = self._get_ffprobe_path()
        if ffprobe_path:
            probe_cmd = [str(ffprobe_path), "-v", "quiet", "-show_streams", "-show_format", str(video_path)]
            try:
                cp = sp.run(probe_cmd, capture_output=True, text=True, timeout=15)
                if cp.returncode == 0:
                    for line in cp.stdout.splitlines():
                        if any(k in line for k in ("codec_type=", "codec_name=", "TAG:title=", "filename=")):
                            logger.debug(f"  ffprobe: {line.strip()}")
            except Exception as e:
                logger.debug(f"ffprobe error: {e}")

        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            logger.error("FFmpeg not found in PATH")
            Path(sub_path).unlink(missing_ok=True)
            self.progress.emit(self.item, [(TreeColumn.STATUS, "FFmpeg missing")])
            return

        # Convert WEBVTT to SRT via Python (handles UTF-8 correctly)
        webvtt_text = sub_data.decode("utf-8", errors="replace")
        srt_content = self._convert_webvtt_to_srt(webvtt_text)
        Path(sub_path).write_text(srt_content, encoding="utf-8")
        logger.info(f"Converted WEBVTT to SRT ({len(srt_content)} chars)")

        # Output to .mkv first (MP4 can't hold SRT), then rename back to original
        tmp_path = video_path.parent / (video_path.name + ".tmp.mkv")

        cmd = [
            str(ffmpeg_path),
            "-y",
            "-itsoffset", "0.046",
            "-i", str(video_path),
            "-i", sub_path,
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "srt",
            "-map_metadata", "-1",
            "-map", "0:v",
            "-map", "0:a",
            "-map", "1:s",
            "-disposition:s", "default",
            "-f", "matroska",
            str(tmp_path),
        ]

        logger.info(f"Merging subtitle: {' '.join(cmd)}")
        try:
            ffmpeg_bin_dir = str(ffmpeg_path.parent)
            cp = sp.run(cmd, capture_output=True, text=True, cwd=ffmpeg_bin_dir)
            if cp.returncode == 0:
                video_path.unlink()
                mkv_path = video_path.with_suffix(".mkv")
                tmp_path.replace(mkv_path)

                # Rename to sanitized title if available
                final_path = mkv_path
                if self.title:
                    safe_title = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", self.title)
                    safe_title = safe_title.strip()
                    if safe_title:
                        named_path = mkv_path.parent / (safe_title + ".mkv")
                        if named_path != mkv_path:
                            if named_path.exists():
                                named_path.unlink()
                            shutil.move(str(mkv_path), str(named_path))
                            logger.info(f"Renamed to: {named_path.name}")
                            final_path = named_path

                Path(sub_path).unlink(missing_ok=True)
                logger.info(f"Merged subtitle into {final_path}")
                srt_len = len(srt_content)
                logger.info(f"EMIT merge success: sub_col='Xem sub', srt_len={srt_len}")
                self.progress.emit(self.item, [
                    (TreeColumn.STATUS, "Merged sub"),
                    (TreeColumn.SUB, "Xem sub"),
                ])
                logger.info(f"EMIT srt content: index=999, len={srt_len}")
                self.progress.emit(self.item, [
                    (999, srt_content),
                ])
            else:
                stderr = cp.stderr.strip()
                logger.error(f"FFmpeg merge failed (code {cp.returncode}): {stderr}")
                tmp_path.unlink(missing_ok=True)
                Path(sub_path).unlink(missing_ok=True)
                self.progress.emit(self.item, [(TreeColumn.STATUS, "Merge failed")])

        except Exception as e:
            logger.error(f"FFmpeg merge error: {e}")
            tmp_path.unlink(missing_ok=True)
            Path(sub_path).unlink(missing_ok=True)
            self.progress.emit(self.item, [(TreeColumn.STATUS, "Merge error")])

    def _find_video_in_path(self, path: Path) -> Path | None:
        """Locate the downloaded video file inside the given directory.

        Search priority:
        1. Files matching the ``video_{id}.*`` output template.
        2. The exact filename reported by yt-dlp (``_actual_filename``).
        3. Files whose name starts with a sanitized version of ``self.title``.
        4. Fallback: the most recently modified ``.mp4``/``.mkv``/``.webm`` file.

        Args:
            path: Directory to search.

        Returns:
            The most recently modified candidate ``Path``, or ``None`` if no
            video file is found.
        """
        if not path.exists():
            return None

        candidates = []

        # 0. Prioritize video_{id}.* (matches yt-dlp output template)
        for p in path.glob(f"video_{self.id}.*"):
            if not p.name.endswith(".tmp.mkv"):
                candidates.append(p)

        # 1. Exact filename from yt-dlp output
        if self._actual_filename:
            actual = Path(self._actual_filename)
            if actual.exists():
                return actual
            glob_pattern = actual.name
            candidates += [p for p in path.glob(glob_pattern) if p not in candidates]

        # 2. By title prefix
        if self.title:
            safe_title = re.sub(r"[^\w]", "_", self.title)
            for ext in (".mp4", ".mkv", ".webm"):
                candidates += list(path.glob(f"{safe_title}{ext}"))
                candidates += list(path.glob(f"{safe_title}_*{ext}"))
                candidates += list(path.glob(f"{safe_title} - *{ext}"))

        # 3. Fallback: newest file
        if not candidates:
            candidates = list(path.glob("*.mp4")) + list(path.glob("*.mkv")) + list(path.glob("*.webm"))

        # Filter out .tmp.mkv leftovers and deduplicate
        seen = set()
        unique = []
        for p in candidates:
            if p.name.endswith(".tmp.mkv"):
                continue
            if p not in seen:
                seen.add(p)
                unique.append(p)

        if unique:
            unique.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            logger.info(f"Video candidates (sorted by mtime): {[p.name for p in unique[:3]]}")
            return unique[0]
        return None

    @staticmethod
    def _convert_webvtt_to_srt(webvtt_content: str) -> str:
        """Convert a WebVTT subtitle string to SRT format.

        Strips the ``WEBVTT`` header and ``NOTE`` blocks, renumbers cues
        sequentially from 1, and replaces ``.`` timestamp separators with
        ``,`` to comply with SRT spec.

        Args:
            webvtt_content: Raw WebVTT subtitle text (UTF-8 decoded).

        Returns:
            SRT-formatted subtitle string.
        """
        lines = webvtt_content.strip().split("\n")
        srt_lines = []
        srt_index = 1
        i = 0

        while i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and "WEBVTT" in lines[i]:
            i += 1

        while i < len(lines):
            line = lines[i].strip()
            ts_match = re.match(
                r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})",
                line,
            )
            if ts_match:
                start = f"{ts_match.group(1)}:{ts_match.group(2)}:{ts_match.group(3)},{ts_match.group(4)}"
                end = f"{ts_match.group(5)}:{ts_match.group(6)}:{ts_match.group(7)},{ts_match.group(8)}"
                srt_lines.append(f"{srt_index}")
                srt_lines.append(f"{start} --> {end}")
                srt_index += 1
            elif line and not line.startswith("NOTE"):
                srt_lines.append(line)
            i += 1

        return "\n".join(srt_lines)

    def _get_ffprobe_path(self) -> Path | None:
        """Locate the ffprobe executable.

        Checks alongside the Python interpreter first, then falls back to
        the system ``PATH``.

        Returns:
            ``Path`` to ffprobe if found, otherwise ``None``.
        """
        for name in ("ffprobe", "ffprobe.exe"):
            for base in ("", str(Path(sys.executable).parent) + os.sep):
                candidate = Path(base) / name
                if candidate.exists():
                    return candidate
        import shutil as sh
        path = sh.which("ffprobe")
        if path:
            return Path(path)
        return None

    def _get_ffmpeg_path(self) -> Path | None:
        """Locate the ffmpeg executable.

        Checks alongside the Python interpreter first, then falls back to
        the system ``PATH``.

        Returns:
            ``Path`` to ffmpeg if found, otherwise ``None``.
        """
        for name in ("ffmpeg", "ffmpeg.exe"):
            for base in ("", str(Path(sys.executable).parent) + os.sep):
                candidate = Path(base) / name
                if candidate.exists():
                    return candidate
        import shutil as sh
        path = sh.which("ffmpeg")
        if path:
            return Path(path)
        return None

    def run(self):
        """Execute the download in a background thread (QThread entry point).

        Spawns yt-dlp as a subprocess, streams its stdout line-by-line, and
        emits ``progress`` signals to update the UI.  When yt-dlp finishes
        successfully, calls :meth:`merge_subtitle` if a subtitle URL was
        provided.  Terminates the subprocess cleanly if :meth:`stop` is called.
        """
        create_window = sp.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        output = ""
        logger.info(f"Download ({self.id}) starting with cmd: {self.command}")

        self.progress.emit(self.item, [(TreeColumn.STATUS, "Processing")])

        # Clean up old files for this ID to avoid "already been downloaded" + postprocess failure
        out_path = Path(self.path)
        for old in out_path.glob(f"video_{self.id}.*"):
            old.unlink()
            logger.info(f"Cleaned up old file: {old}")

        with sp.Popen(
            self.command,
            stdout=sp.PIPE,
            stderr=sp.STDOUT,
            text=True,
            universal_newlines=True,
            creationflags=create_window,
        ) as p:
            for line in p.stdout:
                output += line
                with QtCore.QMutexLocker(self._mutex):
                    if self._stop:
                        p.terminate()
                        p.wait()
                        logger.info(f"Download ({self.id}) stopped.")
                        break

                line = line.strip()
                if "__SEP__" in line:
                    parts = [i.strip() for i in line.split("__SEP__")]
                    status, total_bytes, percent, speed, eta, title = parts
                    self.title = title
                    self.progress.emit(
                        self.item,
                        [
                            (TreeColumn.TITLE, title),
                            (TreeColumn.SIZE, total_bytes),
                            (TreeColumn.PROGRESS, percent),
                            (TreeColumn.SPEED, speed),
                            (TreeColumn.ETA, eta),
                            (TreeColumn.STATUS, "Downloading"),
                        ],
                    )
                elif "[download] Destination:" in line:
                    self._actual_filename = line.split("Destination:", 1)[1].strip()
                elif line.startswith(("[Merger]", "[ExtractAudio]")):
                    self.progress.emit(self.item, [(TreeColumn.STATUS, "Converting")])
                elif line.startswith("WARNING:"):
                    logger.warning(f"Download ({self.id}) {line}")

        if p.returncode != 0:
            logger.error(f"Download ({self.id}) returncode: {p.returncode}\n{output}")
            self.progress.emit(self.item, [(TreeColumn.STATUS, "ERROR")])
        else:
            logger.info(f"Download ({self.id}) finished.")
            self.merge_subtitle()
            self.progress.emit(
                self.item,
                [
                    (TreeColumn.PROGRESS, "100%"),
                    (TreeColumn.STATUS, "Merged sub" if self.sub_url else "Finished"),
                ],
            )
