"""Xshort downloader tab.

The UI intentionally mirrors the NetShort downloader tab. The only source
difference is that search and episode detail are read from Supabase
`xshort_movies`, where `detail_raw` stores the /allepisode response.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import requests
from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import QSettings

from xemshort.helpers import _ns_parse_episodes
from xemshort.models import XSEpisode, XSMovie
from xemshort.movie_search_dialog import NetShortMovieSearchDialog
from xemshort.sync_movies_supabase import (
    DEFAULT_SUPABASE_KEY,
    DEFAULT_SUPABASE_URL,
    XSHORT_SOURCE,
    load_env_file,
)
from xemshort.tab import XemShortTab
from xemshort.workers import DramaWaveDownloadMergeWorker


_XSHORT_APP_NAME = "XemShort GUI"
_XSHORT_CONFIG_KEY = "Xshort"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_TABLE = "xshort_movies"
_API_URL = "DB:xshort_movies.detail_raw"


class XshortMovieSearchDialog(NetShortMovieSearchDialog):
    """Dialog for picking a movie from xshort_movies."""

    def __init__(self, parent=None):
        super().__init__(parent, source=XSHORT_SOURCE, source_name="Xshort")


class XshortFetchFromSupabaseWorker(QtCore.QThread):
    """Fetch Xshort episodes from xshort_movies.detail_raw."""

    success = QtCore.Signal(list, str, str, int)
    error = QtCore.Signal(str, int)
    log_msg = QtCore.Signal(str, int)

    def __init__(self, movie_id: str):
        super().__init__()
        self.movie_id = movie_id
        self.instance_id: int = uuid.uuid4().int & 0x7FFFFFFF

    def run(self) -> None:
        load_env_file(_ENV_PATH)
        supabase_url = os.environ.get("SUPABASE_URL", "").strip() or DEFAULT_SUPABASE_URL
        supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or DEFAULT_SUPABASE_KEY
        if not supabase_url or not supabase_key:
            self.error.emit("SUPABASE_URL / SUPABASE_KEY chưa cấu hình trong .env", self.instance_id)
            return

        try:
            endpoint = supabase_url.rstrip("/") + f"/rest/v1/{_TABLE}"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            }
            response = requests.get(
                endpoint,
                headers=headers,
                params={
                    "play_id": f"eq.{self.movie_id}",
                    "select": "play_id,name,episode_count,detail_raw,detail_synced_at",
                    "limit": "1",
                },
                timeout=30,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                self.error.emit(f"play_id={self.movie_id} chưa có trong {_TABLE}.", self.instance_id)
                return

            row = rows[0]
            raw = row.get("detail_raw") or {}
            if not raw:
                self.error.emit(
                    f"detail_raw rỗng cho play_id={self.movie_id}. "
                    "Hãy chạy sync_xshort_details.py trước.",
                    self.instance_id,
                )
                return

            movie_name = str(row.get("name") or "")
            episodes = _ns_parse_episodes(raw, movie_name)
            for ep in episodes:
                # Same lock rule as NetShort: missing video or subtitle means
                # the episode cannot be selected/downloaded.
                ep.is_locked = not ep.play or not ep.subtitle_url

            if not episodes:
                self.error.emit(f"Không parse được tập nào từ {_TABLE}.detail_raw.", self.instance_id)
                return

            self.log_msg.emit(
                f"DB {_TABLE}: {len(episodes)} tập, detail_synced_at={row.get('detail_synced_at') or '-'}",
                self.instance_id,
            )
            self.success.emit(episodes, movie_name, self.movie_id, self.instance_id)
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}", self.instance_id)


class XshortDownloadMergeWorker(DramaWaveDownloadMergeWorker):
    """Xshort uses HLS episode URLs like DramaWave, with Xshort error source."""

    subtitle_error_source = "xshort"


class XshortTab(XemShortTab):
    """Xshort variant of the NetShort downloader UI."""

    def settings(self) -> QSettings:
        return QSettings(_XSHORT_APP_NAME, _XSHORT_CONFIG_KEY)

    def _cache_source(self) -> str:
        return XSHORT_SOURCE

    def _cache_source_name(self) -> str:
        return "Xshort"

    def _load_settings(self):
        s = self.settings()
        self.ns_save_dir_edit.setText(
            s.value("save_dir", str(Path.home() / "Downloads" / "Xshort"))
        )
        self.ns_api_url_edit.setText(s.value("api_url", _API_URL))
        self.ns_api_url_edit.setPlaceholderText(_API_URL)
        self.ns_api_url_edit.setReadOnly(True)
        self.ns_concurrency_spin.setValue(int(s.value("concurrency", 4)))
        self.ns_sub_checkbox.setChecked(self._setting_bool(s, "download_sub", True))
        self.ns_merge_checkbox.setChecked(self._setting_bool(s, "do_merge", True))
        self.ns_m3u8_checkbox.setChecked(False)
        self.ns_m3u8_reencode_checkbox.setChecked(False)
        self.ns_crf_spin.setValue(int(s.value("crf", 20)))
        self.ns_merge_threads_spin.setValue(int(s.value("merge_threads", 1)))
        self.ns_encode_threads_spin.setValue(int(s.value("encode_threads", 3)))
        self.ns_sub_font_combo.setCurrentText(s.value("sub_font", "UTM Alter Gothic"))
        self.ns_sub_size_spin.setValue(int(s.value("sub_size", 15)))
        self.ns_sub_margin_v_spin.setValue(int(s.value("sub_margin_v", 70)))
        default_color = self.ns_sub_color_combo.itemText(0) or "White"
        self.ns_sub_color_combo.setCurrentText(s.value("sub_color", default_color))
        self.ns_sub_bold_cb.setChecked(self._setting_bool(s, "sub_bold", True))
        self.ns_sub_italic_cb.setChecked(self._setting_bool(s, "sub_italic", False))
        self.ns_sub_outline_spin.setValue(float(s.value("sub_outline", 2.0)))

    def _ns_on_search_movie(self):
        dlg = XshortMovieSearchDialog(self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            play_id = dlg.selected_play_id()
            if play_id:
                self.ns_movie_id_edit.setText(play_id)
                self.ns_movie_id_edit.setFocus()
                self.ns_status.setText(f"Đã chọn Xshort movie id: {play_id}")
                self._log(f"[xshort search] selected play_id={play_id}")

    def _ns_on_fetch(self):
        # Xshort production path uses local DB raw detail, not live API.
        self._ns_on_fetch_from_db()

    def _ns_on_fetch_from_db(self) -> None:
        movie_id = self.ns_movie_id_edit.text().strip()
        if not movie_id:
            QtWidgets.QMessageBox.warning(self, "Thiếu input", "Vui lòng nhập Movie ID.")
            return
        self.ns_fetch_db_btn.setEnabled(False)
        self.ns_fetch_btn.setEnabled(False)
        self.ns_status.setText(f"Đang lấy Xshort từ DB: {movie_id}...")
        self._log(f"[DB] Fetching từ {_TABLE}.detail_raw: {movie_id}...")

        worker = XshortFetchFromSupabaseWorker(movie_id)
        self._fetch_instance_id = worker.instance_id
        self._fetch_workers.append(worker)

        worker.success.connect(self._ns_on_fetch_success)
        worker.error.connect(self._ns_on_fetch_error)
        worker.log_msg.connect(lambda msg, _: self._log(f"[DB] {msg}"))
        worker.finished.connect(lambda: self.ns_fetch_db_btn.setEnabled(True))
        worker.finished.connect(lambda: self.ns_fetch_btn.setEnabled(True))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(
            lambda w=worker: self._fetch_workers.remove(w) if w in self._fetch_workers else None
        )
        worker.start()

    def _ns_on_fetch_success(
        self,
        episodes: list[XSEpisode],
        movie_name: str,
        movie_id: str,
        instance_id: int,
    ):
        if instance_id != self._fetch_instance_id:
            return
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        self.ns_status.setText(f"Fetched Xshort {len(episodes)} tập.")
        self._log(f"Fetched Xshort {len(episodes)} tập.")
        self._ns_show_picker(episodes, name, movie_id)

    def _create_download_worker(self, movie: XSMovie, **kwargs) -> XshortDownloadMergeWorker:
        return XshortDownloadMergeWorker(movie, **kwargs)
