"""Nguonc tab built from the kkphim1 M3U8 download flow."""
from __future__ import annotations

import base64
import json
import queue
import re
import shutil
import subprocess as sp
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from PySide6 import QtCore, QtWidgets

from kkphim1.kkphimtab import (
    KkPhimEpisodeDialog,
    KkPhimTab,
    _episode_output_name,
    _kkphim_slug_from_input,
)
from m3u8.m3utab import (
    _APP_NAME,
    _dark_btn,
    _dark_input,
    _sanitize_filename,
    M3U8DownloadWorker,
    M3U8Item,
)
from m3u8.m3utab_workers import DOWNLOAD_HEADERS
from m3u8pro.m3utab_pro_workers import YtDlpM3U8DownloadWorker

_NGUONC_CONFIG_KEY = "nguonc"
_NGUONC_API_BASE = "https://phim.nguonc.com/api/film"


def _normalize_nguonc_payload(data: dict) -> dict:
    """Normalize nguonc response to the shared kkphim episode shape."""
    payload = data
    nested = data.get("data")
    if isinstance(nested, dict) and (
        isinstance(nested.get("movie"), dict) or "episodes" in nested
    ):
        payload = nested

    movie = payload.get("movie")
    if not isinstance(movie, dict):
        movie = payload

    episodes = payload.get("episodes")
    if episodes is None and isinstance(movie, dict):
        episodes = movie.get("episodes")
    if not isinstance(episodes, list):
        episodes = []

    normalized_episodes = []
    for group in episodes:
        if not isinstance(group, dict):
            continue
        server_data = group.get("server_data")
        if server_data is None:
            server_data = group.get("items")
        if not isinstance(server_data, list):
            server_data = []

        normalized_items = []
        for item in server_data:
            if not isinstance(item, dict):
                continue
            normalized_item = dict(item)
            m3u8_url = str(
                normalized_item.get("link_m3u8") or normalized_item.get("m3u8") or ""
            ).strip()
            embed_url = str(
                normalized_item.get("link_embed") or normalized_item.get("embed") or ""
            ).strip()
            if m3u8_url:
                normalized_item["link_m3u8"] = m3u8_url
            if embed_url:
                normalized_item["link_embed"] = embed_url
            normalized_items.append(normalized_item)

        normalized_group = dict(group)
        normalized_group["server_data"] = normalized_items
        normalized_episodes.append(normalized_group)

    return {
        "movie": movie if isinstance(movie, dict) else {},
        "episodes": normalized_episodes,
    }


class NguoncFetchWorker(QtCore.QThread):
    finished_ok = QtCore.Signal(dict)
    failed = QtCore.Signal(str)

    def __init__(self, slug: str):
        super().__init__()
        self.slug = _kkphim_slug_from_input(slug)

    def run(self):
        if not self.slug:
            self.failed.emit("Vui long nhap slug phim.")
            return
        try:
            response = requests.get(f"{_NGUONC_API_BASE}/{self.slug}", timeout=20)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            self.failed.emit(f"Khong fetch duoc API: {e}")
            return

        status = data.get("status")
        if status is False or str(status).lower() in {"false", "error", "failed"}:
            self.failed.emit(str(data.get("msg") or data.get("message") or "API tra ve loi"))
            return

        self.finished_ok.emit(_normalize_nguonc_payload(data))


class NguoncEmbedDownloadWorker(YtDlpM3U8DownloadWorker):
    """Resolve nguonc embed pages, then download the HLS manifest with yt-dlp."""

    def __init__(
        self,
        url: str,
        save_dir: Path,
        name: str,
        fmt: str = "m3u8",
        fragments: int = 8,
        container_mode: str = "mp4",
    ):
        super().__init__(
            url=url,
            save_dir=save_dir,
            name=name,
            fmt=fmt,
            fragments=fragments,
            container_mode=container_mode,
        )
        self._embed_referer = ""
        self._manifest_duration_ms = 0
        self._manifest_text = ""
        self._temp_manifest_path: Path | None = None

    @staticmethod
    def _decode_b64_json(value: str) -> dict:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _decode_b64_bytes(value: str) -> bytes:
        padded = value.strip() + "=" * (-len(value.strip()) % 4)
        return base64.b64decode(padded)

    @staticmethod
    def is_streamc_embed(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = parsed.netloc.lower()
        return host.endswith(".streamc.xyz") and parsed.path.endswith("/embed.php")

    @staticmethod
    def _duration_ms_from_manifest(manifest: str) -> int:
        total = 0.0
        for match in re.finditer(r"#EXTINF:\s*([0-9.]+)", manifest):
            try:
                total += float(match.group(1))
            except ValueError:
                continue
        return int(total * 1000)

    @classmethod
    def _decrypt_streamc_manifest(cls, manifest: str, key_text: str) -> str:
        if "#ENC-AESGCM" not in manifest:
            return manifest
        try:
            from Crypto.Cipher import AES
        except Exception as e:
            raise RuntimeError(
                "Manifest streamc bi ma hoa AES-GCM. Cai pycryptodome de giai ma."
            ) from e

        iv_match = re.search(r"#ENC-AESGCM;iv=([a-fA-F0-9]+)", manifest)
        payload = ""
        for raw_line in manifest.splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                payload = line
                break
        if not iv_match or not payload:
            raise ValueError("Manifest streamc ma hoa thieu iv hoac payload")

        encrypted = cls._decode_b64_bytes(payload)
        if len(encrypted) <= 16:
            raise ValueError("Payload streamc qua ngan")
        ciphertext = encrypted[:-16]
        tag = encrypted[-16:]
        key = key_text.encode("utf-8")
        cipher = AES.new(key, AES.MODE_GCM, nonce=bytes.fromhex(iv_match.group(1)))
        decrypted = cipher.decrypt_and_verify(ciphertext, tag)
        return decrypted.decode("utf-8")

    @staticmethod
    def _absolutize_manifest_urls(manifest: str, base_url: str) -> str:
        lines = []
        for raw_line in manifest.splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and not urlparse(line).scheme:
                lines.append(urljoin(base_url, line))
            else:
                lines.append(raw_line)
        return "\n".join(lines) + "\n"

    def _write_temp_manifest(self, manifest_url: str, manifest_text: str) -> str:
        text = self._absolutize_manifest_urls(manifest_text, manifest_url)
        self._manifest_text = text
        tmp = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".m3u8",
            prefix=f"nguonc_{self.instance_id}_",
            dir=self.save_dir,
            delete=False,
        )
        try:
            tmp.write(text)
        finally:
            tmp.close()
        self._temp_manifest_path = Path(tmp.name)
        return str(self._temp_manifest_path)

    def _episode_folder_base(self) -> str:
        match = re.match(r"\s*(\d+)", self.name)
        if match:
            return f"ep{int(match.group(1)):02d}"
        safe = _sanitize_filename(self.name).strip()
        return safe or "ep01"

    @staticmethod
    def _is_valid_hls_dir(path: Path) -> bool:
        playlist = path / "index.m3u8"
        return playlist.exists() and playlist.stat().st_size > 128 and any(path.glob("seg_*.ts"))

    def _prepare_episode_hls_dir(self) -> Path:
        root = self.save_dir / "m3u8"
        root.mkdir(parents=True, exist_ok=True)
        base = self._episode_folder_base()
        n = 0
        while True:
            suffix = "" if n == 0 else f" {n}"
            candidate = root / f"{base}{suffix}"
            if not candidate.exists():
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            if candidate.is_dir() and not self._is_valid_hls_dir(candidate):
                shutil.rmtree(candidate, ignore_errors=True)
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            if candidate.is_dir() and not any(candidate.iterdir()):
                candidate.rmdir()
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            if self._is_valid_hls_dir(candidate):
                n += 1
                continue
            n += 1

    def _cleanup_hls_source_temps(self):
        for path in self.save_dir.glob(f".nguonc_hls_source_{self.instance_id}*"):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except Exception:
                pass

    def _download_source_video_for_hls(self) -> tuple[bool, str, Path | None]:
        out_path = self.save_dir / f".nguonc_hls_source_{self.instance_id}.ts"
        old_mode = self.container_mode
        try:
            self.container_mode = "ts"
            self.log_msg.emit(self.instance_id, "Nguonc HLS: tai source TS bang yt-dlp truoc...")
            return self._download_ytdlp(out_path)
        finally:
            self.container_mode = old_mode

    def _download_hls_package(self, input_path: Path | None = None) -> tuple[bool, str, Path | None]:
        iid = self.instance_id
        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            return False, "Khong tim thay ffmpeg de tao HLS local", None
        if input_path is None:
            if self._temp_manifest_path is None or not self._temp_manifest_path.exists():
                return False, "Khong co manifest tam de tao HLS local", None
            input_path = self._temp_manifest_path
        if not input_path.exists():
            return False, f"Khong tim thay input de tao HLS: {input_path}", None

        out_dir = self._prepare_episode_hls_dir()
        playlist_path = out_dir / "index.m3u8"
        segment_pattern = out_dir / "seg_%05d.ts"
        cflags = {"creationflags": sp.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

        input_opts = []
        if input_path.suffix.lower() == ".m3u8":
            input_opts = [
                "-user_agent", DOWNLOAD_HEADERS["User-Agent"],
                "-headers", self._ffmpeg_headers(self._embed_referer or self._origin_referer()),
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_delay_max", "5",
                "-rw_timeout", "15000000",
                "-http_persistent", "1",
                "-http_multiple", "1",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
                "-allowed_extensions", "ALL",
                "-allowed_segment_extensions", "ALL",
                "-extension_picky", "0",
            ]

        cmd = [
            str(ffmpeg_path), "-y",
            "-hide_banner",
            "-loglevel", "warning",
            "-progress", "pipe:1",
            *input_opts,
            "-i", str(input_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-f", "hls",
            "-hls_time", "6",
            "-hls_playlist_type", "vod",
            "-hls_flags", "temp_file",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(segment_pattern),
            str(playlist_path),
        ]

        self.log_msg.emit(iid, f"Nguonc HLS local: {out_dir.name}/index.m3u8")
        self.log_msg.emit(iid, f"ffmpeg HLS input: {input_path.name}")
        try:
            self._proc = sp.Popen(
                cmd,
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **cflags,
            )
            proc = self._proc

            import threading
            progress_queue: queue.Queue[str] = queue.Queue()
            stderr_lines: list[str] = []
            stderr_lock = threading.Lock()

            def drain_stdout():
                assert proc is not None and proc.stdout is not None
                for line in proc.stdout:
                    progress_queue.put(line)

            def drain_stderr():
                assert proc is not None and proc.stderr is not None
                for line in proc.stderr:
                    with stderr_lock:
                        stderr_lines.append(line.rstrip())

            stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            last_progress = time.monotonic()
            max_idle_seconds = 90
            while proc.poll() is None:
                if self._is_aborted():
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except sp.TimeoutExpired:
                        proc.kill()
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    shutil.rmtree(out_dir, ignore_errors=True)
                    return False, "Stopped by user", None

                try:
                    raw = progress_queue.get(timeout=1)
                except queue.Empty:
                    if time.monotonic() - last_progress > max_idle_seconds:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except sp.TimeoutExpired:
                            proc.kill()
                        stdout_thread.join(timeout=1)
                        stderr_thread.join(timeout=1)
                        shutil.rmtree(out_dir, ignore_errors=True)
                        return False, "ffmpeg HLS khong co progress trong 90s", None
                    continue

                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == "progress":
                    last_progress = time.monotonic()
                    continue
                if key.strip() != "out_time_ms":
                    continue
                try:
                    out_time_ms = int(val.strip()) // 1000
                except ValueError:
                    continue
                last_progress = time.monotonic()
                pct = self._ffmpeg_progress_percent(out_time_ms)
                elapsed_s = out_time_ms // 1000
                elapsed_str = f"{elapsed_s // 60}:{elapsed_s % 60:02d}"
                self.progress.emit(iid, "downloading", pct, "", elapsed_str, "")

            ret = proc.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=2)
            with stderr_lock:
                stderr_out = "\n".join(stderr_lines).strip()

            if ret != 0:
                if stderr_out:
                    self.log_msg.emit(iid, "ffmpeg HLS error:\n" + "\n".join(stderr_out.splitlines()[-8:]))
                shutil.rmtree(out_dir, ignore_errors=True)
                return False, f"ffmpeg HLS exit code: {ret}", None
            if not playlist_path.exists() or playlist_path.stat().st_size <= 128:
                shutil.rmtree(out_dir, ignore_errors=True)
                return False, "ffmpeg khong tao duoc index.m3u8", None
            if not any(out_dir.glob("seg_*.ts")):
                shutil.rmtree(out_dir, ignore_errors=True)
                return False, "ffmpeg khong tao duoc segment HLS", None
            self.log_msg.emit(iid, f"Nguonc HLS done: {playlist_path}")
            return True, "", playlist_path
        except FileNotFoundError:
            shutil.rmtree(out_dir, ignore_errors=True)
            return False, "ffmpeg khong tim thay", None
        except Exception as e:
            shutil.rmtree(out_dir, ignore_errors=True)
            return False, str(e), None
        finally:
            self._proc = None

    @classmethod
    def resolve_embed_url(cls, embed_url: str) -> tuple[str, int, str]:
        response = requests.get(
            embed_url,
            headers={
                **DOWNLOAD_HEADERS,
                "Referer": "https://phim.nguonc.com/",
            },
            timeout=20,
        )
        response.raise_for_status()
        match = re.search(r'data-obf=["\']([^"\']+)["\']', response.text)
        if not match:
            raise ValueError("Khong tim thay data-obf trong embed page")

        stream_data = cls._decode_b64_json(match.group(1))
        sub = str(stream_data.get("sUb") or "").strip()
        if not sub:
            raise ValueError("Khong tim thay sUb trong data-obf")
        sub_data = {}
        try:
            sub_data = cls._decode_b64_json(sub)
        except Exception:
            pass
        key = str(stream_data.get("kX") or sub_data.get("t") or "").strip()
        manifest_url = urljoin(embed_url, f"{sub}.m3u8")
        manifest_response = requests.get(
            manifest_url,
            headers={
                **DOWNLOAD_HEADERS,
                "Referer": embed_url,
            },
            timeout=20,
        )
        manifest_response.raise_for_status()
        manifest_text = manifest_response.text
        if "#ENC-AESGCM" in manifest_text:
            if not key:
                raise ValueError("Manifest streamc ma hoa nhung thieu kX")
            manifest_text = cls._decrypt_streamc_manifest(manifest_text, key)
        return manifest_url, cls._duration_ms_from_manifest(manifest_text), manifest_text

    def _origin_referer(self) -> str:
        return self._embed_referer or super()._origin_referer()

    def _ffmpeg_input_options(self) -> list[str]:
        return [
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-allowed_extensions", "ALL",
            "-allowed_segment_extensions", "ALL",
            "-extension_picky", "0",
        ]

    def _ffmpeg_progress_percent(self, out_time_ms: int) -> float:
        if self._manifest_duration_ms <= 0:
            return 0.0
        return max(0.0, min(99.9, out_time_ms / self._manifest_duration_ms * 100.0))

    def _save_manifest(self):
        iid = self.instance_id
        if not self._manifest_text:
            response = requests.get(
                self.url,
                headers={
                    **DOWNLOAD_HEADERS,
                    "Referer": self._embed_referer or self._origin_referer(),
                },
                timeout=30,
            )
            response.raise_for_status()
            self._manifest_text = response.text
        if "#EXTM3U" not in self._manifest_text[:200]:
            raise ValueError("URL khong tra ve playlist M3U8")
        self.save_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._unique_output_path(".m3u8")
        out_path.write_text(self._manifest_text, encoding="utf-8")
        self.output_ready.emit(iid, str(out_path))
        self.progress.emit(iid, "Done", 100.0, "", "", "")
        self.finished.emit(iid, True, "")

    def run(self):
        if self.is_streamc_embed(self.url):
            embed_url = self.url
            self._embed_referer = embed_url
            try:
                self.log_msg.emit(self.instance_id, "Resolve nguonc embed -> m3u8...")
                manifest_url, self._manifest_duration_ms, self._manifest_text = self.resolve_embed_url(
                    embed_url
                )
                temp_manifest = Path(self._write_temp_manifest(manifest_url, self._manifest_text))
                self.url = temp_manifest.as_uri()
                self.log_msg.emit(self.instance_id, f"Nguonc embed: {embed_url[:80]}...")
                self.log_msg.emit(self.instance_id, f"Nguonc manifest da resolve: {temp_manifest.name}")
            except Exception as e:
                self.log_msg.emit(self.instance_id, f"Loi resolve embed nguonc: {e}")
                self.progress.emit(self.instance_id, "Error", 0.0, "", "", "")
                self.finished.emit(self.instance_id, False, f"Khong resolve duoc embed: {e}")
                return
        if self.container_mode == "m3u8":
            try:
                self.progress.emit(self.instance_id, "downloading", 0.0, "", "", "")
                source_path: Path | None = None
                ok, err, source_path = self._download_source_video_for_hls()
                if ok and source_path is not None:
                    ok, err, final_path = self._download_hls_package(source_path)
                else:
                    final_path = None
                if ok and final_path is not None:
                    self.output_ready.emit(self.instance_id, str(final_path))
                    self.progress.emit(self.instance_id, "Done", 100.0, "", "", "")
                    self.finished.emit(self.instance_id, True, "")
                else:
                    self.log_msg.emit(self.instance_id, f"Loi tao HLS nguonc: {err}")
                    self.progress.emit(self.instance_id, "Error", 0.0, "", "", "")
                    self.finished.emit(self.instance_id, False, err)
            except Exception as e:
                self.log_msg.emit(self.instance_id, f"Loi tao HLS nguonc: {e}")
                self.progress.emit(self.instance_id, "Error", 0.0, "", "", "")
                self.finished.emit(self.instance_id, False, f"Khong tao duoc HLS: {e}")
            finally:
                self._cleanup_hls_source_temps()
                if self._temp_manifest_path is not None:
                    try:
                        self._temp_manifest_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            return
        try:
            super().run()
        finally:
            if self._temp_manifest_path is not None:
                try:
                    self._temp_manifest_path.unlink(missing_ok=True)
                except Exception:
                    pass


class NguoncTab(KkPhimTab):
    """Fetch Nguonc episodes and download embed-backed streams."""

    def __init__(self):
        super().__init__()
        self._configure_container_options()

    def _configure_container_options(self):
        current = str(self._cfg_container.currentData() or "mp4")
        if current not in ("mp4", "m3u8"):
            current = "mp4"
        self._cfg_container.blockSignals(True)
        try:
            self._cfg_container.clear()
            self._cfg_container.addItem("MP4 (video)", "mp4")
            self._cfg_container.addItem("M3U8 (HLS folder)", "m3u8")
            idx = self._cfg_container.findData(current)
            self._cfg_container.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._cfg_container.blockSignals(False)
        self._cfg_container.setToolTip(
            "MP4: tai video bang yt-dlp tu embed\n"
            "M3U8: tao thu muc m3u8/epXX/index.m3u8 va segment .ts local"
        )
        self._save_settings()

    def settings(self) -> QtCore.QSettings:
        return QtCore.QSettings(_APP_NAME, _NGUONC_CONFIG_KEY)

    def _load_settings(self):
        super()._load_settings()
        if self._cfg_container.currentData() not in ("mp4", "m3u8"):
            idx = self._cfg_container.findData("mp4")
            self._cfg_container.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_container_mode_changed(self):
        self._save_settings()

    def _build_add_bar(self) -> QtWidgets.QWidget:
        grp = QtWidgets.QGroupBox("nguonc")
        lay = QtWidgets.QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        lay.addWidget(QtWidgets.QLabel("Slug phim:"))
        self._kk_slug = QtWidgets.QLineEdit()
        self._kk_slug.setPlaceholderText(
            "hoa-thien-cot hoac https://phim.nguonc.com/api/film/..."
        )
        self._kk_slug.setStyleSheet(_dark_input())
        self._kk_slug.returnPressed.connect(self._on_fetch_clicked)
        lay.addWidget(self._kk_slug, stretch=1)

        self._btn_fetch = QtWidgets.QPushButton("Fetch")
        self._btn_fetch.setStyleSheet(_dark_btn("#2563eb", "#1d4ed8", padding="5px 16px"))
        self._btn_fetch.clicked.connect(self._on_fetch_clicked)
        lay.addWidget(self._btn_fetch)
        return grp

    def _on_fetch_clicked(self):
        slug = _kkphim_slug_from_input(self._kk_slug.text())
        if not slug:
            QtWidgets.QMessageBox.information(self, "Thieu slug", "Vui long nhap slug phim.")
            return
        save_dir = self._validated_save_dir()
        if save_dir is None:
            QtWidgets.QMessageBox.information(self, "Thieu thu muc", "Vui long chon thu muc luu truoc.")
            return
        if self._fetch_worker and self._fetch_worker.isRunning():
            return
        self._save_settings()
        self._kk_slug.setText(slug)
        self._btn_fetch.setEnabled(False)
        self._btn_fetch.setText("Dang fetch...")
        self._log(f"Fetch nguonc API: {slug}")
        self._fetch_worker = NguoncFetchWorker(slug)
        self._fetch_worker.finished_ok.connect(self._on_fetch_ok)
        self._fetch_worker.failed.connect(self._on_fetch_failed)
        self._fetch_worker.finished.connect(self._on_fetch_done)
        self._fetch_worker.start()
        self._update_action_buttons()

    def _resolve_episode_url(self, ep: dict) -> str:
        m3u8_url = str(ep.get("link_m3u8") or ep.get("m3u8") or "").strip()
        embed_url = str(ep.get("link_embed") or ep.get("embed") or "").strip()
        return M3U8DownloadWorker.normalize_media_url(embed_url or m3u8_url)

    def _add_selected_episodes(self, movie_name: str, selected: list[tuple[str, dict]]) -> int:
        movie_dir = self._movie_save_dir(movie_name)
        if movie_dir is None:
            return 0
        added = 0
        errors = []
        for server_name, ep in selected:
            try:
                url = self._resolve_episode_url(ep)
            except Exception as e:
                ep_name = str(ep.get("name") or ep.get("slug") or "Tap").strip()
                errors.append(f"{server_name} {ep_name}: {e}")
                continue
            if not url:
                continue

            ep_name = str(ep.get("name") or ep.get("slug") or "Tap").strip()
            base_name = _episode_output_name(server_name, ep_name)
            name = self._unique_queue_name(base_name, movie_dir)
            item = M3U8Item(
                id=self._next_id,
                url=url,
                name=name,
                save_dir=movie_dir,
                fmt="m3u8",
                status="pending",
            )
            self._next_id += 1
            self.items.append(item)
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._row_for_id[item.id] = row
            self._fill_row(row, item)
            added += 1

        self._update_count_label()
        self._save_settings()
        if errors:
            self._log("Khong resolve duoc mot so tap nguonc:")
            for msg in errors[:10]:
                self._log(f"  - {msg}")
            if len(errors) > 10:
                self._log(f"  ... va {len(errors) - 10} tap khac")
        return added

    def _start_item(self, item: M3U8Item):
        if item.status == "downloading":
            return
        save_dir = item.save_dir or self._movie_save_dir(self._current_movie_name or "nguonc")
        if save_dir is None:
            return
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self,
                "Thu muc khong hop le",
                f"Khong the tao hoac truy cap thu muc luu:\n{save_dir}\n\n{e}",
            )
            return

        item.status = "downloading"
        item.progress = 0.0
        item.speed = ""
        item.eta = ""
        item.error_msg = ""
        item.save_dir = save_dir
        self._fill_row(self._row_for_id[item.id], item)
        self._update_action_buttons()

        worker = NguoncEmbedDownloadWorker(
            url=item.url,
            save_dir=item.save_dir,
            name=item.name,
            fmt=item.fmt,
            fragments=int(self._cfg_fragments.currentText()),
            container_mode=str(self._cfg_container.currentData()),
        )
        item.instance_id = worker.instance_id
        self.workers[item.id] = worker
        worker.log_msg.connect(self._on_worker_log)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.output_ready.connect(self._on_worker_output_ready)
        worker.start()
        self._update_action_buttons()
        self._log(
            f"[{item.name}] Bat dau tai nguonc bang yt-dlp -N {self._cfg_fragments.currentText()} tu embed..."
        )
