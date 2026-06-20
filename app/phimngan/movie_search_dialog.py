"""PhimNgan movie search dialog backed by Supabase."""
from __future__ import annotations

import math
import os
from typing import Any

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

from xemshort.movie_search_dialog import MovieCard


DEFAULT_SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
DEFAULT_SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJtc3huYWpjdWRram10cXNmaG90Iiwicm9sZSI6"
    "InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDUyNDM5NSwiZXhwIjoyMDk2MTAwMzk1fQ.CvLi4fkjjSMbRaeKi85xC_d5MDCCkv2tcz4iuKinOgU"
)
SUPABASE_SOURCE = "phimngan"


class PhimNganSearchWorker(QtCore.QThread):
    """Search Supabase for phimngan movies off the UI thread."""

    success = QtCore.Signal(list, int, int, str)
    error = QtCore.Signal(str)

    def __init__(self, supabase_url: str, supabase_key: str, query: str, page: int):
        super().__init__()
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.query = query
        self.page = page

    def run(self) -> None:
        try:
            from .phimngan_db import search_movies_db
            rows, total = search_movies_db(
                self.supabase_url,
                self.supabase_key,
                query=self.query,
                page=self.page,
            )
            self.success.emit(rows, total, self.page, self.query)
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}")


PAGE_SIZE = 24


class PhimNganMovieSearchDialog(QtWidgets.QDialog):
    """Dialog for picking a phimngan.tv movie from the synced Supabase catalog."""

    selected = QtCore.Signal(str, str)  # (slug, name)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tim kiem phim phimngan.tv")
        self.resize(760, 760)
        self._worker: PhimNganSearchWorker | None = None
        self._selected: dict[str, Any] | None = None
        self._cards: list[MovieCard] = []
        self._page = 1
        self._total = 0
        self._query = ""
        self._pending_search = False
        self._net = QtNetwork.QNetworkAccessManager(self)
        self._image_cards: dict[QtNetwork.QNetworkReply, MovieCard] = {}
        self._net.finished.connect(self._on_image_loaded)
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(450)
        self._debounce.timeout.connect(self._search_first_page)

        from xemshort.movie_search_dialog import load_env_file
        load_env_file()

        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip() or DEFAULT_SUPABASE_URL
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or DEFAULT_SUPABASE_KEY

        self._build_ui()
        QtCore.QTimer.singleShot(100, self._search_first_page)

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        self.setStyleSheet("QDialog { background:#0b0f17; color:#f9fafb; }")

        # Search bar
        top = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Tim theo ten phim, the loai...")
        self.search_edit.setStyleSheet(
            "QLineEdit { background:#111827;color:#f9fafb;border:1px solid #374151;"
            "border-radius:6px;padding:8px; }"
        )
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.search_edit.returnPressed.connect(self._search_now)
        top.addWidget(self.search_edit, stretch=1)
        root.addLayout(top)

        # Status
        self.status_label = QtWidgets.QLabel("Dang tai...")
        self.status_label.setStyleSheet("color:#9ca3af;")
        root.addWidget(self.status_label)

        # Movie grid
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border:0;background:#0b0f17; }")
        self.grid_host = QtWidgets.QWidget()
        self.grid_host.setStyleSheet("background:#0b0f17;")
        self.grid = QtWidgets.QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        self.scroll.setWidget(self.grid_host)
        root.addWidget(self.scroll, stretch=1)

        # Pager
        pager = QtWidgets.QHBoxLayout()
        self.prev_btn = QtWidgets.QPushButton("Trang truoc")
        self.prev_btn.setStyleSheet(
            "QPushButton { background:#334155;color:white;font-weight:700;padding:7px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#475569; }"
            "QPushButton:disabled { background:#1f2937;color:#64748b; }"
        )
        self.prev_btn.clicked.connect(self._prev_page)
        pager.addWidget(self.prev_btn)
        self.page_label = QtWidgets.QLabel("Trang 1/1")
        self.page_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.page_label.setStyleSheet("color:#d1d5db;font-weight:bold;")
        pager.addWidget(self.page_label, stretch=1)
        self.next_btn = QtWidgets.QPushButton("Trang sau")
        self.next_btn.setStyleSheet(
            "QPushButton { background:#2563eb;color:white;font-weight:700;padding:7px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#1d4ed8; }"
            "QPushButton:disabled { background:#1f2937;color:#64748b; }"
        )
        self.next_btn.clicked.connect(self._next_page)
        pager.addWidget(self.next_btn)
        root.addLayout(pager)

        # Actions
        actions = QtWidgets.QHBoxLayout()
        actions.addStretch()
        self.info_btn = QtWidgets.QPushButton("Info")
        self.info_btn.setEnabled(False)
        self.info_btn.setStyleSheet(
            "QPushButton { background:#4f46e5;color:white;font-weight:700;padding:7px 16px;border-radius:6px; }"
            "QPushButton:hover { background:#4338ca; }"
            "QPushButton:disabled { background:#6b7280;color:#d1d5db; }"
        )
        self.info_btn.clicked.connect(self._show_movie_info)
        actions.addWidget(self.info_btn)

        self.add_btn = QtWidgets.QPushButton("Chon phim nay")
        self.add_btn.setEnabled(False)
        self.add_btn.setStyleSheet(
            "QPushButton { background:#16a34a;color:white;font-weight:700;padding:7px 16px;border-radius:6px; }"
            "QPushButton:hover { background:#15803d; }"
            "QPushButton:disabled { background:#6b7280;color:#d1d5db; }"
        )
        self.add_btn.clicked.connect(self._on_accept)
        actions.addWidget(self.add_btn)

        cancel_btn = QtWidgets.QPushButton("Huy")
        cancel_btn.setStyleSheet(
            "QPushButton { background:#374151;color:white;font-weight:700;padding:7px 16px;border-radius:6px; }"
            "QPushButton:hover { background:#4b5563; }"
        )
        cancel_btn.clicked.connect(self.reject)
        actions.addWidget(cancel_btn)
        root.addLayout(actions)

    # ── Search ────────────────────────────────────────────────────────

    def _search_first_page(self) -> None:
        self._page = 1
        self._query = self.search_edit.text().strip()
        self._search()

    def _on_search_text_changed(self) -> None:
        self._debounce.start()

    def _search_now(self) -> None:
        self._debounce.stop()
        self._search_first_page()

    def _prev_page(self) -> None:
        if self._page > 1:
            self._page -= 1
            self._search()

    def _next_page(self) -> None:
        max_page = max(1, math.ceil(self._total / PAGE_SIZE))
        if self._page < max_page:
            self._page += 1
            self._search()

    def _search(self) -> None:
        if self._worker_running():
            self._pending_search = True
            return
        self._pending_search = False
        if not self.supabase_url or not self.supabase_key:
            self.status_label.setText("Thieu cau hinh Supabase")
            return
        self._selected = None
        self.add_btn.setEnabled(False)
        self.info_btn.setEnabled(False)
        self.status_label.setText("Dang tim...")
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)

        worker = PhimNganSearchWorker(
            self.supabase_url, self.supabase_key, self._query, self._page
        )
        self._worker = worker
        worker.success.connect(self._on_search_success)
        worker.error.connect(self._on_search_error)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _worker_running(self) -> bool:
        if self._worker is None:
            return False
        try:
            return self._worker.isRunning()
        except RuntimeError:
            self._worker = None
            return False

    def _on_worker_finished(self, worker: PhimNganSearchWorker) -> None:
        if self._worker is worker:
            self._worker = None
        self._update_pager()
        if self._pending_search:
            QtCore.QTimer.singleShot(0, self._search_first_page)

    def _on_search_success(self, rows: list, total: int, page: int, query: str) -> None:
        self._total = total
        self._page = page
        self._query = query
        self._render_movies(rows)
        self.status_label.setText(f"{total} ket qua - hien {len(rows)} phim")
        self._update_pager()

    def _on_search_error(self, message: str) -> None:
        self.status_label.setText(f"Loi tim kiem: {message}")
        self._clear_grid()

    def _clear_grid(self) -> None:
        for reply in list(self._image_cards):
            self._image_cards.pop(reply, None)
            reply.abort()
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()

    def _render_movies(self, rows: list[dict[str, Any]]) -> None:
        self._clear_grid()
        self.info_btn.setEnabled(False)
        self.add_btn.setEnabled(False)
        cols = 4
        for idx, movie in enumerate(rows):
            card = MovieCard(movie)
            card.clicked.connect(self._select_movie)
            card.double_clicked.connect(self._select_and_accept)
            self.grid.addWidget(card, idx // cols, idx % cols)
            self._cards.append(card)
            thumbnail = str(movie.get("thumbnail") or "").strip()
            if thumbnail:
                self._load_image(thumbnail, card)
        self.grid.setRowStretch(max(0, math.ceil(len(rows) / cols)), 1)

    def _select_movie(self, movie: dict[str, Any]) -> None:
        self._selected = movie
        selected_slug = str(movie.get("slug") or "")
        for card in self._cards:
            card_slug = str(card.movie.get("slug") or "")
            card.set_selected(card_slug == selected_slug)
        self.add_btn.setEnabled(bool(selected_slug))
        self.info_btn.setEnabled(bool(self._selected))

    def _select_and_accept(self, movie: dict[str, Any]) -> None:
        self._select_movie(movie)
        if self._selected:
            self.accept()

    def _on_accept(self) -> None:
        if self._selected:
            self.accept()

    # ── Info ─────────────────────────────────────────────────────────

    def _show_movie_info(self) -> None:
        if not self._selected:
            return
        m = self._selected
        fields = [
            ("slug", m.get("slug")),
            ("name", m.get("name")),
            ("thumbnail", m.get("thumbnail")),
            ("episode_count", m.get("episode_count")),
            ("total_episode", m.get("total_episode")),
            ("views", m.get("views")),
            ("is_hot", m.get("is_hot")),
            ("is_featured", m.get("is_featured")),
            ("is_voice", m.get("is_voice")),
            ("category", m.get("category")),
            ("synced_at", m.get("synced_at")),
        ]
        text = "\n".join(f"{k}: {v or ''}" for k, v in fields)
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Thong tin phim")
        dlg.resize(620, 380)
        lay = QtWidgets.QVBoxLayout(dlg)
        title = QtWidgets.QLabel(str(m.get("name") or ""))
        title.setWordWrap(True)
        title.setStyleSheet("font-size:16px;font-weight:800;color:#111827;")
        lay.addWidget(title)
        info = QtWidgets.QPlainTextEdit()
        info.setReadOnly(True)
        info.setPlainText(text)
        info.setStyleSheet("font-family:Consolas,monospace;font-size:11px;")
        lay.addWidget(info, stretch=1)
        row = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy info")
        copy_btn.setStyleSheet(
            "QPushButton { background:#2563eb;color:white;font-weight:700;padding:6px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#1d4ed8; }"
        )
        copy_btn.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(text))
        row.addWidget(copy_btn)
        row.addStretch()
        close_btn = QtWidgets.QPushButton("Dong")
        close_btn.clicked.connect(dlg.close)
        row.addWidget(close_btn)
        lay.addLayout(row)
        dlg.exec()

    # ── Image loading ────────────────────────────────────────────────

    def _load_image(self, url: str, card: MovieCard) -> None:
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
        reply = self._net.get(request)
        self._image_cards[reply] = card

    def _on_image_loaded(self, reply: QtNetwork.QNetworkReply) -> None:
        card = self._image_cards.pop(reply, None)
        if isinstance(card, MovieCard) and reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError:
            pixmap = QtGui.QPixmap()
            pixmap.loadFromData(reply.readAll())
            card.set_pixmap(pixmap)
        elif isinstance(card, MovieCard):
            card.poster.setText("No image")
        reply.deleteLater()

    def _update_pager(self) -> None:
        max_page = max(1, math.ceil(self._total / PAGE_SIZE))
        self.page_label.setText(f"Trang {self._page}/{max_page}")
        self.prev_btn.setEnabled(self._page > 1 and not self._worker_running())
        self.next_btn.setEnabled(self._page < max_page and not self._worker_running())

    # ── Result ───────────────────────────────────────────────────────

    def get_result(self) -> tuple[str, str]:
        """Return (slug, name) of selected movie."""
        if self._selected:
            return str(self._selected.get("slug") or ""), str(self._selected.get("name") or "")
        return "", ""
