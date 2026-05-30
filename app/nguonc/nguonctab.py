"""Nguonc tab built from the kkphim1 M3U8 download flow."""
from __future__ import annotations

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

_NGUONC_CONFIG_KEY = "nguonc"
_NGUONC_API_BASE = "https://phimapi.com/phim"


def _normalize_nguonc_payload(data: dict) -> dict:
    """Normalize phimapi response and make link_embed the primary media source."""
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
                # The shared episode dialog reads link_m3u8 before link_embed.
                # Mirror the embed wrapper into both fields so preview/download start there.
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


class NguoncTab(KkPhimTab):
    """Fetch Nguonc episodes and download with yt-dlp."""

    def settings(self) -> QtCore.QSettings:
        return QtCore.QSettings(_APP_NAME, _NGUONC_CONFIG_KEY)

    def _build_add_bar(self) -> QtWidgets.QWidget:
        grp = QtWidgets.QGroupBox("nguonc")
        lay = QtWidgets.QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        lay.addWidget(QtWidgets.QLabel("Slug phim:"))
        self._kk_slug = QtWidgets.QLineEdit()
        self._kk_slug.setPlaceholderText("hoa-thien-cot hoac https://phimapi.com/phim/...")
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
        self._log(f"Fetch nguonc: {slug}")
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
