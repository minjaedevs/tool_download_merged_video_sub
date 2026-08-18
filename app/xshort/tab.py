"""Xshort downloader tab.

The UI intentionally mirrors the NetShort downloader tab. The only source
difference is that search and episode detail are read from Supabase
`xshort_movies`, where `detail_raw` stores the /allepisode response.
"""
from __future__ import annotations

import json as _json
import os
import uuid
from pathlib import Path

import requests
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings

from xemshort.dialogs import XSEpisodePickerDialog
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

    def _movie_info_text(self, movie: dict) -> str:
        """Basic fields + full raw JSON (includes banner/thumbnail links)."""
        fields = [
            ("name", movie.get("name")),
            ("play_id", movie.get("play_id")),
            ("thumbnail", movie.get("thumbnail")),
            ("label_list", movie.get("label_list")),
            ("episode_count", movie.get("episode_count")),
            ("source_api", movie.get("source_api")),
        ]
        basic = "\n".join(f"{k}: {v or ''}" for k, v in fields)
        raw = movie.get("raw")
        if raw:
            raw_str = _json.dumps(raw, ensure_ascii=False, indent=2)
            return basic + "\n\n--- raw ---\n" + raw_str
        return basic

    def _show_movie_info(self) -> None:
        if not self._selected_movie:
            return
        movie = self._selected_movie
        raw = movie.get("raw") or {}
        raw_text = _json.dumps(raw, ensure_ascii=False, indent=2)
        full_text = self._movie_info_text(movie)

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Thông tin Xshort: {movie.get('name') or ''}")
        dlg.resize(700, 520)
        layout = QtWidgets.QVBoxLayout(dlg)

        title = QtWidgets.QLabel(str(movie.get("name") or "Untitled"))
        title.setWordWrap(True)
        title.setStyleSheet("font-size:16px;font-weight:800;color:#111827;")
        layout.addWidget(title)

        info = QtWidgets.QPlainTextEdit()
        info.setReadOnly(True)
        info.setPlainText(full_text)
        info.setStyleSheet("font-family:Consolas,monospace;font-size:11px;")
        layout.addWidget(info, stretch=1)

        row = QtWidgets.QHBoxLayout()

        copy_raw_btn = QtWidgets.QPushButton("Copy Raw JSON")
        copy_raw_btn.setStyleSheet(
            "QPushButton { background:#7c3aed;color:white;font-weight:700;"
            "padding:6px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#6d28d9; }"
        )
        copy_raw_btn.clicked.connect(lambda: self._copy_and_notify(raw_text, "Đã copy raw JSON"))
        row.addWidget(copy_raw_btn)

        copy_info_btn = QtWidgets.QPushButton("Copy Info")
        copy_info_btn.setStyleSheet(
            "QPushButton { background:#2563eb;color:white;font-weight:700;"
            "padding:6px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#1d4ed8; }"
        )
        copy_info_btn.clicked.connect(lambda: self._copy_and_notify(full_text, "Đã copy info"))
        row.addWidget(copy_info_btn)

        row.addStretch()
        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.setStyleSheet(
            "QPushButton { background:#374151;color:white;font-weight:700;"
            "padding:6px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#4b5563; }"
        )
        close_btn.clicked.connect(dlg.close)
        row.addWidget(close_btn)
        layout.addLayout(row)
        dlg.exec()

    def _copy_and_notify(self, text: str, msg: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)
        QtWidgets.QToolTip.showText(
            QtGui.QCursor.pos(), msg, None, QtCore.QRect(), 1500
        )


class XshortFetchFromSupabaseWorker(QtCore.QThread):
    """Fetch Xshort episodes from xshort_movies.detail_raw."""

    success = QtCore.Signal(list, str, str, int)
    error = QtCore.Signal(str, int)
    log_msg = QtCore.Signal(str, int)
    movie_raw = QtCore.Signal(object, int)  # (raw dict, instance_id)

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
                    "select": "play_id,name,episode_count,raw,detail_raw,detail_synced_at",
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
            movie_raw_data = row.get("raw") or {}
            self.movie_raw.emit(movie_raw_data, self.instance_id)

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
                # Missing subtitles should not block video downloads.
                ep.is_locked = not ep.play

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


class XshortEpisodePickerDialog(XSEpisodePickerDialog):
    """Episode picker with movie raw info panel at the top for Xshort."""

    def __init__(
        self,
        movie_name: str,
        episodes: list,
        parent=None,
        source: str = "",
        raw: dict | None = None,
    ):
        self._movie_raw = raw or {}
        super().__init__(movie_name, episodes, parent, source)
        if self._movie_raw:
            self._insert_raw_panel()

    def _insert_raw_panel(self) -> None:
        raw_text = _json.dumps(self._movie_raw, ensure_ascii=False, indent=2)
        layout = self.layout()

        # Header toggle button
        toggle_btn = QtWidgets.QPushButton("▶ Thông tin phim (raw) — nhấn để mở/đóng")
        toggle_btn.setCheckable(True)
        toggle_btn.setChecked(False)
        toggle_btn.setStyleSheet(
            "QPushButton { background:#1f2937;color:#d1d5db;font-weight:700;"
            "padding:5px 10px;border-radius:4px;text-align:left; }"
            "QPushButton:checked { background:#111827;color:#f9fafb; }"
            "QPushButton:hover { background:#374151; }"
        )

        # Panel container (hidden by default)
        panel = QtWidgets.QWidget()
        panel.setVisible(False)
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 4, 0, 4)
        panel_layout.setSpacing(4)

        text_edit = QtWidgets.QPlainTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setPlainText(raw_text)
        text_edit.setStyleSheet(
            "QPlainTextEdit { font-family:Consolas,monospace;font-size:10px;"
            "background:#0f172a;color:#e2e8f0;border:1px solid #334155;"
            "border-radius:4px;padding:4px; }"
        )
        text_edit.setFixedHeight(160)
        panel_layout.addWidget(text_edit)

        btn_row = QtWidgets.QHBoxLayout()
        copy_raw_btn = QtWidgets.QPushButton("Copy Raw JSON")
        copy_raw_btn.setStyleSheet(
            "QPushButton { background:#7c3aed;color:white;font-weight:700;"
            "padding:4px 12px;border-radius:4px; }"
            "QPushButton:hover { background:#6d28d9; }"
        )
        copy_raw_btn.clicked.connect(lambda: self._copy_raw(raw_text))
        btn_row.addWidget(copy_raw_btn)
        btn_row.addStretch()
        panel_layout.addLayout(btn_row)

        toggle_btn.toggled.connect(panel.setVisible)
        toggle_btn.toggled.connect(
            lambda checked: toggle_btn.setText(
                ("▼" if checked else "▶") + " Thông tin phim (raw) — nhấn để mở/đóng"
            )
        )

        layout.insertWidget(0, toggle_btn)
        layout.insertWidget(1, panel)

    def _copy_raw(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)
        QtWidgets.QToolTip.showText(
            QtGui.QCursor.pos(), "Đã copy raw JSON", None, QtCore.QRect(), 1500
        )


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
        worker.movie_raw.connect(self._on_movie_raw)
        worker.finished.connect(lambda: self.ns_fetch_db_btn.setEnabled(True))
        worker.finished.connect(lambda: self.ns_fetch_btn.setEnabled(True))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(
            lambda w=worker: self._fetch_workers.remove(w) if w in self._fetch_workers else None
        )
        worker.start()

    def _on_movie_raw(self, raw: dict, instance_id: int) -> None:
        if instance_id == self._fetch_instance_id:
            self._current_raw = raw

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

    def _ns_show_picker(self, episodes: list[XSEpisode], movie_name: str = "", movie_id: str = ""):
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        raw = getattr(self, "_current_raw", {})
        dlg = XshortEpisodePickerDialog(name, episodes, self, source=self._cache_source(), raw=raw)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        selected = dlg.get_selected_episodes()
        save_dir = Path(self.ns_save_dir_edit.text() or ".")
        save_dir.mkdir(parents=True, exist_ok=True)

        # Merge into existing movie row if same movie_id found
        if movie_id:
            for m in self.movies:
                if m.movie_id == movie_id:
                    new_eps: list[int] = []
                    reset_eps: list[int] = []
                    pending_eps: list[int] = []
                    for ep in selected:
                        existing = next((e for e in m.episodes if e.id == ep.id), None)
                        if existing is None:
                            ep.status = "pending"
                            ep.merge_note = ""
                            m.episodes.append(ep)
                            new_eps.append(ep.episode)
                        elif existing.status == "done":
                            existing.status = "pending"
                            reset_eps.append(existing.episode)
                        else:
                            pending_eps.append(existing.episode)
                    if new_eps or reset_eps:
                        self._ns_refresh_movie_row(m)
                    parts = []
                    if new_eps:
                        eps_str = ", ".join(f"T{n}" for n in sorted(new_eps))
                        parts.append(f"thêm mới {len(new_eps)} tập: {eps_str}")
                    if reset_eps:
                        eps_str = ", ".join(f"T{n}" for n in sorted(reset_eps))
                        parts.append(f"reset {len(reset_eps)} tập đã done → pending: {eps_str}")
                    if pending_eps and not new_eps and not reset_eps:
                        eps_str = ", ".join(f"T{n}" for n in sorted(pending_eps))
                        parts.append(f"{len(pending_eps)} tập đang xử lý: {eps_str}")
                    self.ns_start_btn.setEnabled(True)
                    self._log(f"'{m.name}': " + (" | ".join(parts) if parts else "không có thay đổi"))
                    return

        # New movie
        for ep in selected:
            ep.status = "pending"
            ep.merge_note = ""
        movie = XSMovie(name=name, episodes=selected, save_dir=save_dir, movie_id=movie_id)
        self.movies.append(movie)
        self._ns_add_movie_to_table(movie)
        self.ns_start_btn.setEnabled(True)
        self._log(f"Thêm '{name}' - {movie.selected_count}/{movie.total} tập.")

    def _create_download_worker(self, movie: XSMovie, **kwargs) -> XshortDownloadMergeWorker:
        return XshortDownloadMergeWorker(movie, **kwargs)
