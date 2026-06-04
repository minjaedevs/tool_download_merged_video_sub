"""NetShort movie search dialog backed by Supabase."""
from __future__ import annotations

import math
import os
from typing import Any

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

from .sync_movies_supabase import (
    NETSHORT_SOURCE,
    load_env_file,
    search_movies_supabase,
)


PAGE_SIZE = 24
DEFAULT_SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
DEFAULT_SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJtc3huYWpjdWRram10cXNmaG90Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDUyNDM5NSwiZXhwIjoyMDk2MTAwMzk1fQ.CvLi4fkjjSMbRaeKi85xC_d5MDCCkv2tcz4iuKinOgU"


class NetShortSearchWorker(QtCore.QThread):
    """Search Supabase off the UI thread."""

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
            rows, total = search_movies_supabase(
                self.supabase_url,
                self.supabase_key,
                query=self.query,
                page=self.page,
                page_size=PAGE_SIZE,
                source=NETSHORT_SOURCE,
            )
            self.success.emit(rows, total, self.page, self.query)
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}")


class MovieCard(QtWidgets.QFrame):
    clicked = QtCore.Signal(dict)
    double_clicked = QtCore.Signal(dict)

    def __init__(self, movie: dict[str, Any], parent=None):
        super().__init__(parent)
        self.movie = movie
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(145)
        self.setObjectName("movieCard")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.poster = QtWidgets.QLabel()
        self.poster.setFixedSize(132, 176)
        self.poster.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.poster.setStyleSheet("background:#1f2937;color:#9ca3af;border-radius:8px;")
        self.poster.setText("Loading")
        layout.addWidget(self.poster)

        self.selected_badge = QtWidgets.QLabel("DA CHON", self.poster)
        self.selected_badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.selected_badge.setGeometry(8, 8, 64, 22)
        self.selected_badge.setStyleSheet(
            "background:#f43f5e;color:white;border-radius:11px;font-size:10px;font-weight:800;"
        )
        self.selected_badge.hide()

        self.title = QtWidgets.QLabel(str(movie.get("name") or "Untitled"))
        self.title.setWordWrap(True)
        self.title.setFixedHeight(42)
        self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.title.setStyleSheet("font-weight:700;color:#f9fafb;font-size:12px;")
        layout.addWidget(self.title)

        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.setStyleSheet(
                "#movieCard { background:#4a1428; border:3px solid #fb7185; border-radius:10px; }"
            )
            self.title.setStyleSheet("font-weight:800;color:#ffffff;font-size:12px;")
            self.selected_badge.show()
            self.selected_badge.raise_()
        else:
            self.setStyleSheet(
                "#movieCard { background:#111827; border:1px solid #374151; border-radius:10px; }"
                "#movieCard:hover { border:1px solid #6b7280; }"
            )
            self.title.setStyleSheet("font-weight:700;color:#f9fafb;font-size:12px;")
            self.selected_badge.hide()

    def set_pixmap(self, pixmap: QtGui.QPixmap) -> None:
        if pixmap.isNull():
            self.poster.setText("No image")
            return
        scaled = pixmap.scaled(
            self.poster.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self.poster.setPixmap(scaled)
        self.selected_badge.raise_()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit(self.movie)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.movie)
        super().mouseDoubleClickEvent(event)


class NetShortMovieSearchDialog(QtWidgets.QDialog):
    """Dialog for picking a NetShort movie from the synced catalog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tim kiem NetShort")
        self.resize(760, 760)
        self._worker: NetShortSearchWorker | None = None
        self._selected_movie: dict[str, Any] | None = None
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

        load_env_file()
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip() or DEFAULT_SUPABASE_URL
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or DEFAULT_SUPABASE_KEY

        self._build_ui()
        QtCore.QTimer.singleShot(100, self._search_first_page)

    def selected_play_id(self) -> str:
        if not self._selected_movie:
            return ""
        return str(self._selected_movie.get("play_id") or "").strip()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        self.setStyleSheet("QDialog { background:#0b0f17; color:#f9fafb; }")

        top = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Tim theo ten phim, label, intro...")
        self.search_edit.setStyleSheet(
            "QLineEdit { background:#111827;color:#f9fafb;border:1px solid #374151;"
            "border-radius:6px;padding:8px; }"
        )
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.search_edit.returnPressed.connect(self._search_now)
        top.addWidget(self.search_edit, stretch=1)
        root.addLayout(top)

        self.status_label = QtWidgets.QLabel("Dang tai...")
        self.status_label.setStyleSheet("color:#9ca3af;")
        root.addWidget(self.status_label)

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
        self.add_btn = QtWidgets.QPushButton("Add phim")
        self.add_btn.setEnabled(False)
        self.add_btn.setStyleSheet(
            "QPushButton { background:#16a34a;color:white;font-weight:700;padding:7px 16px;border-radius:6px; }"
            "QPushButton:hover { background:#15803d; }"
            "QPushButton:disabled { background:#6b7280;color:#d1d5db; }"
        )
        self.add_btn.clicked.connect(self.accept)
        actions.addWidget(self.add_btn)
        cancel_btn = QtWidgets.QPushButton("Huy")
        cancel_btn.setStyleSheet(
            "QPushButton { background:#374151;color:white;font-weight:700;padding:7px 16px;border-radius:6px; }"
            "QPushButton:hover { background:#4b5563; }"
        )
        cancel_btn.clicked.connect(self.reject)
        actions.addWidget(cancel_btn)
        root.addLayout(actions)

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
        self._selected_movie = None
        self.add_btn.setEnabled(False)
        self.info_btn.setEnabled(False)
        self.status_label.setText("Dang tim...")
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)

        worker = NetShortSearchWorker(self.supabase_url, self.supabase_key, self._query, self._page)
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

    def _on_worker_finished(self, worker: NetShortSearchWorker) -> None:
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
        shown = len(rows)
        self.status_label.setText(f"{total} ket qua - dang hien {shown} phim")
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
            widget = item.widget()
            if widget:
                widget.deleteLater()
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
        self._selected_movie = movie
        selected_play_id = str(movie.get("play_id") or "")
        for card in self._cards:
            card_play_id = str(card.movie.get("play_id") or "")
            card.set_selected(card_play_id == selected_play_id)
        self.add_btn.setEnabled(bool(self.selected_play_id()))
        self.info_btn.setEnabled(bool(self._selected_movie))

    def _select_and_accept(self, movie: dict[str, Any]) -> None:
        self._select_movie(movie)
        if self.selected_play_id():
            self.accept()

    def _movie_info_text(self, movie: dict[str, Any]) -> str:
        fields = [
            ("name", movie.get("name")),
            ("play_id", movie.get("play_id")),
            ("thumbnail", movie.get("thumbnail")),
            ("label_list", movie.get("label_list")),
            ("search_text", movie.get("search_text")),
        ]
        return "\n".join(f"{key}: {value or ''}" for key, value in fields)

    def _show_movie_info(self) -> None:
        if not self._selected_movie:
            return
        text = self._movie_info_text(self._selected_movie)
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Thong tin phim")
        dlg.resize(620, 380)
        layout = QtWidgets.QVBoxLayout(dlg)

        title = QtWidgets.QLabel(str(self._selected_movie.get("name") or "Untitled"))
        title.setWordWrap(True)
        title.setStyleSheet("font-size:16px;font-weight:800;color:#111827;")
        layout.addWidget(title)

        info = QtWidgets.QPlainTextEdit()
        info.setReadOnly(True)
        info.setPlainText(text)
        info.setStyleSheet("font-family:Consolas,monospace;font-size:11px;")
        layout.addWidget(info, stretch=1)

        row = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy info")
        copy_btn.setStyleSheet(
            "QPushButton { background:#2563eb;color:white;font-weight:700;padding:6px 14px;border-radius:6px; }"
            "QPushButton:hover { background:#1d4ed8; }"
        )
        copy_btn.clicked.connect(lambda: self._copy_movie_info(text))
        row.addWidget(copy_btn)
        row.addStretch()
        close_btn = QtWidgets.QPushButton("Dong")
        close_btn.clicked.connect(dlg.close)
        row.addWidget(close_btn)
        layout.addLayout(row)
        dlg.exec()

    def _copy_movie_info(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)
        QtWidgets.QToolTip.showText(
            QtGui.QCursor.pos(),
            "Da copy thong tin phim",
            None,
            QtCore.QRect(),
            1500,
        )

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
