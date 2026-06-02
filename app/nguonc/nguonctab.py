"""Nguonc tab built from the kkphim1 M3U8 download flow."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from PySide6 import QtCore, QtWidgets

from kkphim1.kkphimtab import KkPhimTab, _episode_output_name, _kkphim_slug_from_input
from m3u8.m3utab import (
    _APP_NAME,
    _dark_btn,
    _dark_input,
    M3U8DownloadWorker,
    M3U8Item,
)
from m3u8.m3utab_workers import DOWNLOAD_HEADERS

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
            embed_url = str(
                normalized_item.get("link_embed") or normalized_item.get("embed") or ""
            ).strip()
            if not normalized_item.get("link_m3u8") and normalized_item.get("m3u8"):
                normalized_item["link_m3u8"] = normalized_item.get("m3u8")
            if not normalized_item.get("link_embed") and normalized_item.get("embed"):
                normalized_item["link_embed"] = normalized_item.get("embed")
            if embed_url:
                normalized_item["link_embed"] = embed_url
                normalized_item["link_m3u8"] = embed_url
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


class NguoncFFmpegDownloadWorker(M3U8DownloadWorker):
    """Resolve nguonc embed pages, then download the HLS manifest with ffmpeg."""

    def __init__(self, url: str, save_dir: Path, name: str, fmt: str = "m3u8"):
        super().__init__(url=url, save_dir=save_dir, name=name, fmt=fmt)
        self._embed_referer = ""
        self._manifest_duration_ms = 0

    @staticmethod
    def _decode_b64_json(value: str) -> dict:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return json.loads(raw.decode("utf-8"))

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
    def resolve_embed_url(cls, embed_url: str) -> tuple[str, int]:
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
        return manifest_url, cls._duration_ms_from_manifest(manifest_response.text)

    def _origin_referer(self) -> str:
        return self._embed_referer or super()._origin_referer()

    def _ffmpeg_input_options(self) -> list[str]:
        return [
            "-allowed_extensions", "ALL",
            "-allowed_segment_extensions", "ALL",
            "-extension_picky", "0",
        ]

    def _ffmpeg_progress_percent(self, out_time_ms: int) -> float:
        if self._manifest_duration_ms <= 0:
            return 0.0
        return max(0.0, min(99.9, out_time_ms / self._manifest_duration_ms * 100.0))

    def run(self):
        if self.is_streamc_embed(self.url):
            embed_url = self.url
            self._embed_referer = embed_url
            try:
                self.log_msg.emit(self.instance_id, "Resolve nguonc embed -> m3u8...")
                self.url, self._manifest_duration_ms = self.resolve_embed_url(embed_url)
                self.log_msg.emit(self.instance_id, f"Nguonc m3u8: {self.url[:80]}...")
            except Exception as e:
                self.log_msg.emit(self.instance_id, f"Loi resolve embed nguonc: {e}")
                self.progress.emit(self.instance_id, "Error", 0.0, "", "", "")
                self.finished.emit(self.instance_id, False, f"Khong resolve duoc embed: {e}")
                return
        super().run()


class NguoncTab(KkPhimTab):
    """Fetch Nguonc episodes and download embed-backed streams with ffmpeg."""

    def settings(self) -> QtCore.QSettings:
        return QtCore.QSettings(_APP_NAME, _NGUONC_CONFIG_KEY)

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
        embed_url = str(ep.get("link_embed") or ep.get("embed") or "").strip()
        m3u8_url = str(ep.get("link_m3u8") or ep.get("m3u8") or "").strip()
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

        worker = NguoncFFmpegDownloadWorker(
            url=item.url,
            save_dir=item.save_dir,
            name=item.name,
            fmt=item.fmt,
        )
        item.instance_id = worker.instance_id
        self.workers[item.id] = worker
        worker.log_msg.connect(self._on_worker_log)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.output_ready.connect(self._on_worker_output_ready)
        worker.start()
        self._update_action_buttons()
        self._log(f"[{item.name}] Bat dau tai nguonc bang ffmpeg tu embed...")
