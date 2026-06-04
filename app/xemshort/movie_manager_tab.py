"""Movie catalog management tab for XemShort sources."""
from __future__ import annotations

import os
import time
from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

from .sync_movies_supabase import (
    MOVIE_SOURCES,
    NETSHORT_SOURCE,
    load_env_file,
    sync_to_supabase,
    supabase_source_stats,
)


_MOVIE_MGR_APP = "Tool Movie XemShort"
_MOVIE_MGR_KEY = "MovieManager"
_DEFAULT_SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"


def _today_start_iso() -> str:
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return today.isoformat()


class MovieManagerWorker(QtCore.QThread):
    """Run Supabase movie tasks off the UI thread."""

    log_msg = QtCore.Signal(str)
    stats_ready = QtCore.Signal(dict)
    sync_ready = QtCore.Signal(dict)
    error = QtCore.Signal(str)

    def __init__(
        self,
        action: str,
        supabase_url: str,
        supabase_key: str,
        source: str,
        synced_since: str | None = None,
    ):
        super().__init__()
        self.action = action
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.source = source
        self.synced_since = synced_since

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_msg.emit(f"[{ts}] {message}")

    def run(self) -> None:
        try:
            if self.action == "stats":
                stats = supabase_source_stats(
                    self.supabase_url,
                    self.supabase_key,
                    synced_since=self.synced_since,
                )
                self.stats_ready.emit(stats)
                return

            if self.action == "sync":
                result = sync_to_supabase(
                    self.supabase_url,
                    self.supabase_key,
                    source=self.source,
                    log_fn=self.log,
                )
                result["today_stats"] = supabase_source_stats(
                    self.supabase_url,
                    self.supabase_key,
                    synced_since=self.synced_since,
                )
                self.sync_ready.emit(result)
                return

            raise ValueError(f"Unknown action: {self.action}")
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}")


class MovieManagerTab(QtWidgets.QWidget):
    """Operations panel for source-specific movie catalog tables."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: MovieManagerWorker | None = None
        self._selected_source = NETSHORT_SOURCE
        self._panels: dict[str, dict[str, QtWidgets.QWidget]] = {}
        self._build_ui()
        self._load_settings()
        QtCore.QTimer.singleShot(250, lambda: self._start_worker("stats", quiet_missing=True))

    def settings(self) -> QtCore.QSettings:
        return QtCore.QSettings(_MOVIE_MGR_APP, _MOVIE_MGR_KEY)

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.connection_group = QtWidgets.QGroupBox("Ket noi Supabase")
        form = QtWidgets.QFormLayout(self.connection_group)

        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("https://...supabase.co")
        form.addRow("Supabase URL:", self.url_edit)

        key_row = QtWidgets.QHBoxLayout()
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setPlaceholderText("Service role key hoac SUPABASE_KEY env")
        key_row.addWidget(self.key_edit, stretch=1)
        self.show_key_cb = QtWidgets.QCheckBox("Hien key")
        self.show_key_cb.toggled.connect(self._toggle_key_visible)
        key_row.addWidget(self.show_key_cb)
        form.addRow("Service key:", key_row)

        layout.addWidget(self.connection_group)
        self.connection_group.setVisible(False)

        self.source_tabs = QtWidgets.QTabWidget()
        self.source_tabs.setDocumentMode(True)
        self.source_tabs.currentChanged.connect(self._on_source_tab_changed)
        for source, meta in MOVIE_SOURCES.items():
            tab = self._build_source_tab(source, meta)
            self.source_tabs.addTab(tab, str(meta["name"]))
        layout.addWidget(self.source_tabs, stretch=1)

    def _build_source_tab(self, source: str, meta: dict) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(tab)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        header = QtWidgets.QGroupBox(str(meta["name"]))
        header_lay = QtWidgets.QVBoxLayout(header)
        title = QtWidgets.QLabel(f"<b>{meta['name']}</b> - table: <code>{meta['table']}</code>")
        title.setTextFormat(QtCore.Qt.TextFormat.RichText)
        header_lay.addWidget(title)

        if not meta["sync_enabled"]:
            locked = QtWidgets.QLabel("Nguon nay chua ho tro sync trong phien ban hien tai.")
            locked.setStyleSheet(
                "background:#fff7ed;color:#9a3412;border:1px solid #fed7aa;"
                "border-radius:6px;padding:8px;font-weight:700;"
            )
            header_lay.addWidget(locked)
        root.addWidget(header)

        stats = QtWidgets.QGroupBox("Thong ke")
        stats_lay = QtWidgets.QGridLayout(stats)

        total_title = QtWidgets.QLabel("Tong phim")
        total_title.setStyleSheet("color:#6b7280;font-size:12px;")
        total_value = QtWidgets.QLabel("-")
        total_value.setStyleSheet("font-size:24px;font-weight:800;color:#111827;")
        stats_lay.addWidget(total_title, 0, 0)
        stats_lay.addWidget(total_value, 1, 0)

        today_title = QtWidgets.QLabel("Sync moi hom nay")
        today_title.setStyleSheet("color:#6b7280;font-size:12px;")
        today_value = QtWidgets.QLabel("-")
        today_value.setStyleSheet("font-size:24px;font-weight:800;color:#16a34a;")
        stats_lay.addWidget(today_title, 0, 1)
        stats_lay.addWidget(today_value, 1, 1)

        status_title = QtWidgets.QLabel("Trang thai")
        status_title.setStyleSheet("color:#6b7280;font-size:12px;")
        status_value = QtWidgets.QLabel("San sang" if meta["sync_enabled"] else "Bi khoa")
        status_value.setStyleSheet(
            "font-size:16px;font-weight:800;color:#2563eb;"
            if meta["sync_enabled"] else
            "font-size:16px;font-weight:800;color:#9ca3af;"
        )
        stats_lay.addWidget(status_title, 0, 2)
        stats_lay.addWidget(status_value, 1, 2)
        stats_lay.setColumnStretch(3, 1)
        root.addWidget(stats)

        actions = QtWidgets.QHBoxLayout()
        refresh_btn = QtWidgets.QPushButton("Refresh thong ke")
        refresh_btn.setStyleSheet(
            "QPushButton { background:#374151;color:white;font-weight:700;padding:7px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#4b5563; }"
            "QPushButton:disabled { background:#9ca3af; }"
        )
        refresh_btn.clicked.connect(lambda *_args, s=source: self._start_worker("stats", source=s))
        actions.addWidget(refresh_btn)

        sync_btn = QtWidgets.QPushButton("Sync data")
        sync_btn.setEnabled(bool(meta["sync_enabled"]))
        sync_btn.setStyleSheet(
            "QPushButton { background:#16a34a;color:white;font-weight:800;padding:7px 16px;border-radius:6px; }"
            "QPushButton:hover { background:#15803d; }"
            "QPushButton:disabled { background:#9ca3af;color:#f3f4f6; }"
        )
        sync_btn.clicked.connect(lambda *_args, s=source: self._start_worker("sync", source=s))
        actions.addWidget(sync_btn)
        actions.addStretch()
        root.addLayout(actions)

        log = QtWidgets.QTextEdit()
        log.setReadOnly(True)
        log.setFont(QtGui.QFont("Consolas", 9))
        log.setStyleSheet("background:#f8fafc;color:#111827;")
        root.addWidget(QtWidgets.QLabel("<b>Log sync</b>"))
        root.addWidget(log, stretch=1)

        self._panels[source] = {
            "total": total_value,
            "today": today_value,
            "status": status_value,
            "refresh_btn": refresh_btn,
            "sync_btn": sync_btn,
            "log": log,
        }
        return tab

    def _load_settings(self) -> None:
        load_env_file()
        settings = self.settings()
        env_url = os.environ.get("SUPABASE_URL", "").strip()
        saved_url = str(settings.value("supabase_url", _DEFAULT_SUPABASE_URL))
        self.url_edit.setText(env_url or saved_url)
        self.key_edit.setText(os.environ.get("SUPABASE_KEY", ""))
        self.url_edit.textChanged.connect(lambda *_: self.settings().setValue("supabase_url", self.url_edit.text()))
        self._select_source(NETSHORT_SOURCE)

    def _toggle_key_visible(self, visible: bool) -> None:
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password)

    def _on_source_tab_changed(self, index: int) -> None:
        sources = list(MOVIE_SOURCES)
        if 0 <= index < len(sources):
            self._select_source(sources[index])

    def _select_source(self, source: str) -> None:
        self._selected_source = source
        self.settings().setValue("movie_source", source)
        sources = list(MOVIE_SOURCES)
        if source in sources and self.source_tabs.currentIndex() != sources.index(source):
            self.source_tabs.setCurrentIndex(sources.index(source))
        meta = MOVIE_SOURCES[source]
        self._log(source, f"[{time.strftime('%H:%M:%S')}] Da chon nguon: {meta['name']} ({meta['table']})")

    def _connection(self, quiet_missing: bool = False) -> tuple[str, str] | None:
        supabase_url = self.url_edit.text().strip()
        supabase_key = self.key_edit.text().strip()
        if not supabase_url or not supabase_key:
            if quiet_missing:
                self._log(self._selected_source, f"[{time.strftime('%H:%M:%S')}] Missing Supabase config in .env.")
                return None
            QtWidgets.QMessageBox.warning(
                self,
                "Thieu ket noi",
                "Vui long nhap Supabase URL va service role key.",
            )
            return None
        return supabase_url, supabase_key

    def _set_busy(self, busy: bool) -> None:
        for source, meta in MOVIE_SOURCES.items():
            panel = self._panels[source]
            refresh_btn = panel["refresh_btn"]
            sync_btn = panel["sync_btn"]
            if isinstance(refresh_btn, QtWidgets.QPushButton):
                refresh_btn.setEnabled(not busy)
            if isinstance(sync_btn, QtWidgets.QPushButton):
                sync_btn.setEnabled((not busy) and bool(meta["sync_enabled"]))

    def _log(self, source: str, message: str) -> None:
        log = self._panels.get(source, {}).get("log")
        if isinstance(log, QtWidgets.QTextEdit):
            log.append(message)
            log.verticalScrollBar().setValue(log.verticalScrollBar().maximum())

    def _worker_running(self) -> bool:
        if self._worker is None:
            return False
        try:
            return self._worker.isRunning()
        except RuntimeError:
            self._worker = None
            return False

    def _start_worker(self, action: str, source: str | None = None, quiet_missing: bool = False) -> None:
        if self._worker_running():
            return
        source = source or self._selected_source
        self._select_source(source)
        conn = self._connection(quiet_missing=quiet_missing)
        if conn is None:
            return
        if action == "sync" and not MOVIE_SOURCES[source]["sync_enabled"]:
            self._log(source, f"[{time.strftime('%H:%M:%S')}] Nguon nay dang bi khoa, chua ho tro sync.")
            return

        self._set_busy(True)
        self._log(source, f"[{time.strftime('%H:%M:%S')}] Start {action}...")
        worker = MovieManagerWorker(action, *conn, source, synced_since=_today_start_iso())
        self._worker = worker
        worker.log_msg.connect(lambda msg, s=source: self._log(s, msg))
        worker.stats_ready.connect(self._on_stats_ready)
        worker.sync_ready.connect(self._on_sync_ready)
        worker.error.connect(lambda msg, s=source: self._on_error(s, msg))
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_worker_finished(self, worker: MovieManagerWorker) -> None:
        if self._worker is worker:
            self._worker = None
        self._set_busy(False)

    def _on_stats_ready(self, stats: dict) -> None:
        self._apply_stats(stats)
        self._log(self._selected_source, f"[{time.strftime('%H:%M:%S')}] Da refresh thong ke theo ngay hien tai.")

    def _on_sync_ready(self, result: dict) -> None:
        source = str(result.get("source") or self._selected_source)
        today_stats = result.get("today_stats")
        if isinstance(today_stats, dict):
            self._apply_stats(today_stats)
        else:
            stats = result.get("stats")
            if isinstance(stats, dict):
                self._apply_stats(stats)
        self._log(
            source,
            f"[{time.strftime('%H:%M:%S')}] Done: fetched={result.get('fetched')} "
            f"new={result.get('new_by_synced_at')} skipped={result.get('skipped')}",
        )

    def _apply_stats(self, stats: dict) -> None:
        for source, panel in self._panels.items():
            item = stats.get(source, {})
            total = int(item.get("total", 0) or 0)
            today_new = int(item.get("new_since", 0) or 0)
            total_label = panel["total"]
            today_label = panel["today"]
            if isinstance(total_label, QtWidgets.QLabel):
                total_label.setText(str(total))
            if isinstance(today_label, QtWidgets.QLabel):
                today_label.setText(str(today_new))

    def _on_error(self, source: str, message: str) -> None:
        self._log(source, f"[{time.strftime('%H:%M:%S')}] ERROR: {message}")
        QtWidgets.QMessageBox.critical(self, "Movie Manager", message)
