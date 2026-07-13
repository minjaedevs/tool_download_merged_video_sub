"""Tab 'Yêu cầu Crawl' — gửi shortPlayId crawl requests lên Supabase queue.

Luồng UX:
  - Lần đầu (chưa có tên): hiện màn hình nhập tên → xác nhận → chuyển sang UI chính
  - Sau đó: hiện "Xin chào {tên}" + sub-tabs NetShort / Dramaware
  - Mỗi sub-tab: input ID + nút Gửi (có confirmation dialog) + bảng yêu cầu riêng
  - Mỗi sub-tab tự làm mới mỗi 30s (per-tab refresh)
  - Cột bảng: shortPlayId | Tên phim | Status | Notes | Gửi lúc | Hoàn thành lúc | Đã tải
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings, QTimer

from .sync_movies_supabase import DEFAULT_SUPABASE_KEY, DEFAULT_SUPABASE_URL, load_env_file
from .queue_crawl_supabase import fetch_queue_requests, submit_queue_request

_ENV_PATH     = Path(__file__).resolve().parents[2] / ".env"
_SETTINGS_APP = "Tool Movie XemShort"
_SETTINGS_KEY = "RequestQueueTab"
_REFRESH_MS   = 30_000   # 30s auto-refresh

# provider key → display label
PROVIDERS: dict[str, str] = {
    "netshort":  "NetShort",
    "dramawave": "Dramaware",
}


# ── Theme ─────────────────────────────────────────────────────────────────────

def _is_dark() -> bool:
    app = QtWidgets.QApplication.instance()
    return bool(app and app.palette().color(QtGui.QPalette.Window).lightness() < 128)


class _Tk:
    """Color tokens — one instance per theme (light / dark)."""

    def __init__(self, dark: bool) -> None:
        self.dark = dark

        if dark:
            # backgrounds
            self.bg_input     = "#1e293b"
            self.bg_alt       = "#162032"
            self.bg_header    = "#0f172a"
            self.bg_selected  = "#1e3a5f"
            # text
            self.text         = "#e2e8f0"
            self.text_muted   = "#94a3b8"
            self.text_hint    = "#475569"
            self.name_missing = "#475569"
            # borders
            self.border       = "#334155"
            self.border_focus = "#60a5fa"
            # status text (brighter for dark bg)
            self.status = {
                "pending":    "#fbbf24",
                "processing": "#60a5fa",
                "crawling":   "#60a5fa",
                "completed":  "#4ade80",
                "error":      "#f87171",
                "failed":     "#f87171",
            }
            # row highlight backgrounds (dark, subtle)
            self.hl = {
                "processing": "#0f2d52",
                "crawling":   "#0f2d52",
                "error":      "#3b0f0f",
                "failed":     "#3b0f0f",
            }
            # semantic
            self.success      = "#4ade80"
            self.warning      = "#fbbf24"
            self.err          = "#f87171"
            # wait-time banner
            self.wait_text    = "#93c5fd"
            self.wait_bg      = "#0c1a3d"
            self.wait_border  = "#1d4ed8"
            # default status label text
            self.status_default = "#94a3b8"
            # neutral/cancel button
            self.btn_n_bg     = "#1e293b"
            self.btn_n_border = "#475569"
            self.btn_n_hover  = "#334155"
            self.btn_n_text   = "#e2e8f0"
            # disabled state
            self.btn_dis_bg   = "#1e293b"
            self.btn_dis_text = "#475569"
            self.btn_dis_bdr  = "#334155"
            # dl count label
            self.dl_count     = "#a78bfa"
        else:
            # backgrounds
            self.bg_input     = "#ffffff"
            self.bg_alt       = "#f1f5f9"
            self.bg_header    = "#f1f5f9"
            self.bg_selected  = "#dbeafe"
            # text
            self.text         = "#1e293b"
            self.text_muted   = "#64748b"
            self.text_hint    = "#94a3b8"
            self.name_missing = "#94a3b8"
            # borders
            self.border       = "#cbd5e1"
            self.border_focus = "#2563eb"
            # status text
            self.status = {
                "pending":    "#d97706",
                "processing": "#2563eb",
                "crawling":   "#2563eb",
                "completed":  "#16a34a",
                "error":      "#dc2626",
                "failed":     "#dc2626",
            }
            # row highlight backgrounds
            self.hl = {
                "processing": "#dbeafe",
                "crawling":   "#dbeafe",
                "error":      "#fee2e2",
                "failed":     "#fee2e2",
            }
            # semantic
            self.success      = "#16a34a"
            self.warning      = "#d97706"
            self.err          = "#dc2626"
            # wait-time banner
            self.wait_text    = "#1e40af"
            self.wait_bg      = "#eff6ff"
            self.wait_border  = "#bfdbfe"
            # default status label text
            self.status_default = "#334155"
            # neutral/cancel button
            self.btn_n_bg     = "#ffffff"
            self.btn_n_border = "#cbd5e1"
            self.btn_n_hover  = "#f1f5f9"
            self.btn_n_text   = "#334155"
            # disabled state
            self.btn_dis_bg   = "#f1f5f9"
            self.btn_dis_text = "#94a3b8"
            self.btn_dis_bdr  = "#e2e8f0"
            # dl count label
            self.dl_count     = "#7c3aed"


# ── Utilities ─────────────────────────────────────────────────────────────────

def _timestamp_value(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        ts = iso.replace("Z", "+00:00")
        dt_local = datetime.fromisoformat(ts).astimezone()
        today = datetime.now().date()
        if dt_local.date() == today:
            return dt_local.strftime("Hôm nay, %H:%M")
        return dt_local.strftime("%d/%m %H:%M")
    except Exception:
        return iso[:16].replace("T", " ")


class _TimeTableItem(QtWidgets.QTableWidgetItem):
    def __init__(self, text: str, iso: str | None):
        super().__init__(text)
        self._timestamp = _timestamp_value(iso)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        if isinstance(other, _TimeTableItem):
            return self._timestamp < other._timestamp
        return super().__lt__(other)


# ── Add Titles Dialog ─────────────────────────────────────────────────────────

class _AddTitlesDialog(QtWidgets.QDialog):
    """Dialog nhập danh sách tên phim đã tải — mỗi dòng một tên."""

    def __init__(self, existing: set, parent=None):
        super().__init__(parent)
        tk = _Tk(_is_dark())

        self.setWindowTitle("Tên phim đã tải")
        self.setMinimumWidth(520)
        self.setMinimumHeight(420)
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowContextHelpButtonHint)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        lbl = QtWidgets.QLabel("Nhập tên phim đã tải <b>(mỗi dòng một tên)</b>:")
        lbl.setStyleSheet(f"font-size: 13px; color: {tk.text};")
        layout.addWidget(lbl)

        self._edit = QtWidgets.QPlainTextEdit()
        self._edit.setPlaceholderText(
            "Ví dụ:\nBước Qua Bóng Tối\nChạy Trốn Tình Yêu\nCô Ta Quý Giá\n..."
        )
        self._edit.setFont(QtGui.QFont("Segoe UI", 10))
        self._edit.setStyleSheet(
            f"QPlainTextEdit {{ border: 1px solid {tk.border}; border-radius: 6px;"
            f" padding: 6px; background-color: {tk.bg_input}; color: {tk.text}; }}"
            f"QPlainTextEdit:focus {{ border-color: {tk.border_focus}; }}"
        )
        if existing:
            self._edit.setPlainText("\n".join(sorted(existing)))
        layout.addWidget(self._edit, stretch=1)

        self._count_lbl = QtWidgets.QLabel("")
        self._count_lbl.setStyleSheet(
            f"color: {tk.dl_count}; font-size: 11px; font-weight: bold;"
        )
        layout.addWidget(self._count_lbl)
        self._edit.textChanged.connect(self._update_count)
        self._update_count()

        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QtWidgets.QPushButton("Hủy")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {tk.btn_n_border}; border-radius: 4px;"
            f" padding: 4px 20px; font-size: 12px; background-color: {tk.btn_n_bg};"
            f" color: {tk.btn_n_text}; }}"
            f"QPushButton:hover {{ background-color: {tk.btn_n_hover}; }}"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QtWidgets.QPushButton("Thêm")
        ok_btn.setFixedHeight(34)
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            "QPushButton { background-color: #7c3aed; color: white; padding: 4px 28px;"
            " border-radius: 4px; font-weight: bold; font-size: 12px; border: none; }"
            "QPushButton:hover { background-color: #6d28d9; }"
        )
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

    def _update_count(self) -> None:
        n = len(self.get_titles())
        self._count_lbl.setText(f"{n} tên phim" if n else "")

    def get_titles(self) -> set:
        lines = self._edit.toPlainText().splitlines()
        return {line.strip() for line in lines if line.strip()}


# ── Confirm Submit Dialog ─────────────────────────────────────────────────────

class _ConfirmSubmitDialog(QtWidgets.QDialog):
    """Dialog xác nhận trước khi gửi yêu cầu crawl."""

    def __init__(self, provider_label: str, short_play_id: str, parent=None):
        super().__init__(parent)
        tk = _Tk(_is_dark())

        self.setWindowTitle("Xác nhận yêu cầu crawl")
        self.setMinimumWidth(420)
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowContextHelpButtonHint)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 16)

        # Icon + message
        msg_lbl = QtWidgets.QLabel(
            f"Bạn có muốn thêm yêu cầu crawl <b>{provider_label}</b> này không?"
        )
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"font-size: 13px; color: {tk.text};")
        layout.addWidget(msg_lbl)

        # ID preview
        id_frame = QtWidgets.QFrame()
        id_frame.setStyleSheet(
            f"QFrame {{ background-color: {tk.bg_alt}; border: 1px solid {tk.border};"
            " border-radius: 4px; padding: 6px; }}"
        )
        id_layout = QtWidgets.QHBoxLayout(id_frame)
        id_layout.setContentsMargins(8, 6, 8, 6)

        id_label = QtWidgets.QLabel("ID:")
        id_label.setStyleSheet(f"color: {tk.text_muted}; font-size: 12px;")
        id_layout.addWidget(id_label)

        id_value = QtWidgets.QLabel(short_play_id)
        id_value.setStyleSheet(
            f"color: {tk.text}; font-size: 12px; font-weight: bold; font-family: monospace;"
        )
        id_value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        id_layout.addWidget(id_value)
        id_layout.addStretch()
        layout.addWidget(id_frame)

        # Buttons
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QtWidgets.QPushButton("Hủy")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {tk.btn_n_border}; border-radius: 4px;"
            f" padding: 4px 20px; font-size: 12px; background-color: {tk.btn_n_bg};"
            f" color: {tk.btn_n_text}; }}"
            f"QPushButton:hover {{ background-color: {tk.btn_n_hover}; }}"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QtWidgets.QPushButton("Có, thêm yêu cầu")
        ok_btn.setFixedHeight(34)
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white; padding: 4px 20px;"
            " border-radius: 4px; font-weight: bold; font-size: 12px; border: none; }"
            "QPushButton:hover { background-color: #2563eb; }"
        )
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)


# ── Workers ───────────────────────────────────────────────────────────────────

class _SubmitWorker(QtCore.QThread):
    done  = QtCore.Signal(dict)
    error = QtCore.Signal(str)

    def __init__(
        self,
        url: str,
        key: str,
        author: str,
        short_play_id: str,
        provider: str,
        parent=None,
    ):
        super().__init__(parent)
        self._url           = url
        self._key           = key
        self._author        = author
        self._short_play_id = short_play_id
        self._provider      = provider

    def run(self) -> None:
        try:
            row = submit_queue_request(
                self._url, self._key,
                self._author, self._short_play_id,
                self._provider,
            )
            self.done.emit(row)
        except Exception as exc:
            self.error.emit(str(exc))


class _RefreshWorker(QtCore.QThread):
    data_ready = QtCore.Signal(list, list)
    error      = QtCore.Signal(str)

    def __init__(self, url: str, key: str, provider: str, parent=None):
        super().__init__(parent)
        self._url      = url
        self._key      = key
        self._provider = provider

    def run(self) -> None:
        try:
            rows = fetch_queue_requests(
                self._url, self._key,
                provider=self._provider, limit=500,
            )
            pending_rows = fetch_queue_requests(
                self._url, self._key,
                provider=self._provider, status="pending", limit=None,
            )
            self.data_ready.emit(rows, pending_rows)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Tab ───────────────────────────────────────────────────────────────────────

class RequestQueueTab(QtWidgets.QWidget):
    """Tab cho phép user gửi yêu cầu crawl và xem danh sách yêu cầu của mình.

    Hỗ trợ nhiều provider (NetShort, Dramaware) qua sub-tabs.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._supabase_url = DEFAULT_SUPABASE_URL
        self._supabase_key = ""
        self._username     = ""
        self._downloaded_titles: set = set()
        self._tk = _Tk(_is_dark())

        # Per-provider panel state — populated in _build_provider_panel()
        self._panels: dict[str, dict] = {}

        self._load_credentials()
        self._build_ui()
        self._apply_theme()
        self._load_settings()

    # ── Credentials ──────────────────────────────────────────────────────────

    def _load_credentials(self) -> None:
        load_env_file(_ENV_PATH)
        self._supabase_url = os.environ.get("SUPABASE_URL", "").strip() or DEFAULT_SUPABASE_URL
        self._supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or DEFAULT_SUPABASE_KEY

    # ── Theme ─────────────────────────────────────────────────────────────────

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.PaletteChange:
            new_dark = _is_dark()
            if new_dark != self._tk.dark:
                self._tk = _Tk(new_dark)
                self._apply_theme()
                for provider, panel in self._panels.items():
                    if panel["last_rows"]:
                        self._populate_table(panel["last_rows"], provider)

    def _apply_theme(self) -> None:
        """Áp dụng toàn bộ màu sắc theo theme hiện tại."""
        tk = self._tk

        # Widget-level QSS (cascade xuống QGroupBox, QTableWidget, ...)
        self.setStyleSheet(f"""
            QGroupBox {{
                color: {tk.text};
                border: 1px solid {tk.border};
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 14px;
                font-size: 12px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
                color: {tk.text_muted};
            }}
            QTableWidget {{
                gridline-color: {tk.border};
                color: {tk.text};
                border: 1px solid {tk.border};
                outline: none;
                selection-background-color: {tk.bg_selected};
                selection-color: {tk.text};
            }}
            QHeaderView::section {{
                background-color: {tk.bg_header};
                color: {tk.text};
                border: none;
                border-bottom: 2px solid {tk.border};
                border-right: 1px solid {tk.border};
                padding: 5px 8px;
                font-weight: bold;
                font-size: 12px;
            }}
            QHeaderView::section:last-child {{
                border-right: none;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {tk.border};
                border-radius: 4px;
                min-height: 30px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 8px;
                margin: 0;
            }}
            QScrollBar::handle:horizontal {{
                background: {tk.border};
                border-radius: 4px;
                min-width: 30px;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
            QLabel {{
                color: {tk.text};
            }}
        """)

        # Name page widgets
        if hasattr(self, "_name_page_title"):
            self._name_page_title.setStyleSheet(
                f"font-size: 16px; font-weight: bold; color: {tk.text};"
            )
        if hasattr(self, "_name_page_sub"):
            self._name_page_sub.setStyleSheet(
                f"color: {tk.text_muted}; font-size: 12px;"
            )
        if hasattr(self, "_name_edit"):
            self._name_edit.setStyleSheet(
                f"QLineEdit {{ border: 1px solid {tk.border}; border-radius: 6px;"
                f" padding: 0 10px; font-size: 14px;"
                f" background-color: {tk.bg_input}; color: {tk.text}; }}"
                f"QLineEdit:focus {{ border-color: {tk.border_focus}; }}"
            )
        if hasattr(self, "_confirm_btn"):
            self._confirm_btn.setStyleSheet(
                "QPushButton { background-color: #3b82f6; color: white; border-radius: 4px;"
                " padding: 4px 20px; font-weight: bold; font-size: 12px; }"
                "QPushButton:hover { background-color: #2563eb; }"
            )

        # Main page: header
        if hasattr(self, "_greeting_lbl"):
            self._greeting_lbl.setStyleSheet(
                f"font-size: 14px; font-weight: bold; color: {tk.text};"
            )
        if hasattr(self, "_change_btn"):
            self._change_btn.setStyleSheet(
                f"QPushButton {{ background-color: #F35D06; color: #FCFBFB;"
                f" border: 1px solid {tk.border}; border-radius: 4px;"
                f" padding: 4px 14px; font-weight: bold; font-size: 12px; }}"
                f"QPushButton:hover {{ background-color: {tk.btn_n_hover};"
                f" color: {tk.text}; border-color: {tk.border}; }}"
                f"QPushButton:disabled {{ background-color: {tk.btn_dis_bg};"
                f" color: {tk.btn_dis_text}; border-color: {tk.btn_dis_bdr}; }}"
            )

        # Main page: shared dl toolbar
        if hasattr(self, "_add_titles_btn"):
            self._add_titles_btn.setStyleSheet(
                "QPushButton { background-color: #7c3aed; color: white; border: none;"
                " border-radius: 4px; padding: 4px 14px; font-weight: bold; font-size: 12px; }"
                "QPushButton:hover { background-color: #6d28d9; }"
            )
        if hasattr(self, "_pick_folder_btn"):
            self._pick_folder_btn.setStyleSheet(
                "QPushButton { background-color: #0891b2; color: white; border: none;"
                " border-radius: 4px; padding: 4px 14px; font-weight: bold; font-size: 12px; }"
                "QPushButton:hover { background-color: #0e7490; }"
            )
        if hasattr(self, "_dl_count_lbl"):
            self._dl_count_lbl.setStyleSheet(
                f"color: {tk.dl_count}; font-size: 11px; font-weight: bold;"
            )

        # Per-provider panel widgets
        for provider in PROVIDERS:
            p = self._panels.get(provider)
            if not p:
                continue

            # Table palette
            table = p.get("table")
            if table:
                pal = table.palette()
                pal.setColor(QtGui.QPalette.Base, QtGui.QColor(tk.bg_input))
                pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(tk.bg_alt))
                pal.setColor(QtGui.QPalette.Text, QtGui.QColor(tk.text))
                pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(tk.text))
                pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(tk.bg_selected))
                table.setPalette(pal)

            if "id_edit" in p:
                p["id_edit"].setStyleSheet(
                    f"QLineEdit {{ border: 1px solid {tk.border}; border-radius: 4px;"
                    f" padding: 0 8px; font-size: 13px;"
                    f" background-color: {tk.bg_input}; color: {tk.text}; }}"
                    f"QLineEdit:focus {{ border-color: {tk.border_focus}; }}"
                )
            if "submit_btn" in p:
                p["submit_btn"].setStyleSheet(
                    "QPushButton { background-color: #3b82f6; color: white; padding: 4px 20px;"
                    " border-radius: 4px; font-weight: bold; font-size: 12px; border: none; }"
                    "QPushButton:hover { background-color: #2563eb; }"
                    f"QPushButton:disabled {{ background-color: {tk.btn_dis_bg};"
                    f" color: {tk.btn_dis_text}; border: 1px solid {tk.btn_dis_bdr}; }}"
                )
            if "refresh_btn" in p:
                p["refresh_btn"].setStyleSheet(
                    "QPushButton { background-color: #16a34a; color: #ffffff;"
                    f" border: 1px solid {tk.border}; border-radius: 4px;"
                    " padding: 4px 14px; font-weight: bold; font-size: 12px; }"
                    "QPushButton:hover { background-color: #15803d; }"
                    f"QPushButton:disabled {{ background-color: {tk.btn_dis_bg};"
                    f" color: {tk.btn_dis_text}; border-color: {tk.btn_dis_bdr}; }}"
                )
            if "info_lbl" in p:
                p["info_lbl"].setStyleSheet(f"color: {tk.text_muted}; font-size: 12px;")
            if "next_refresh_lbl" in p:
                p["next_refresh_lbl"].setStyleSheet(
                    f"color: {tk.text_hint}; font-size: 11px;"
                )
            if "wait_time_lbl" in p:
                p["wait_time_lbl"].setStyleSheet(
                    f"color: {tk.wait_text}; background-color: {tk.wait_bg};"
                    f" border: 1px solid {tk.wait_border}; border-radius: 4px;"
                    " padding: 6px 10px; font-weight: bold; font-size: 12px;"
                )
            if "status_lbl" in p:
                current_css = p["status_lbl"].styleSheet()
                if (
                    "color:" not in current_css
                    or "#334155" in current_css
                    or "#94a3b8" in current_css
                ):
                    p["status_lbl"].setStyleSheet(
                        f"color: {tk.status_default}; padding: 2px 4px; font-size: 12px;"
                    )

    # ── UI builder ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QtWidgets.QStackedWidget()
        root.addWidget(self._stack)

        self._stack.addWidget(self._build_name_page())
        self._stack.addWidget(self._build_main_page())

    # ── Page 0: Name setup ───────────────────────────────────────────────────

    def _build_name_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(page)
        vbox.setAlignment(QtCore.Qt.AlignCenter)
        vbox.setSpacing(16)

        icon_lbl = QtWidgets.QLabel("👤")
        icon_lbl.setAlignment(QtCore.Qt.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 48px;")
        vbox.addWidget(icon_lbl)

        self._name_page_title = QtWidgets.QLabel("Nhập tên của bạn để bắt đầu")
        self._name_page_title.setAlignment(QtCore.Qt.AlignCenter)
        vbox.addWidget(self._name_page_title)

        self._name_page_sub = QtWidgets.QLabel(
            "Tên sẽ được lưu trên máy này và dùng để theo dõi yêu cầu của bạn."
        )
        self._name_page_sub.setAlignment(QtCore.Qt.AlignCenter)
        self._name_page_sub.setWordWrap(True)
        vbox.addWidget(self._name_page_sub)

        form = QtWidgets.QHBoxLayout()
        form.setSpacing(8)

        self._name_edit = QtWidgets.QLineEdit()
        self._name_edit.setPlaceholderText("Ví dụ: Lan, Minh, Team A...")
        self._name_edit.setMinimumWidth(240)
        self._name_edit.setMaximumWidth(340)
        self._name_edit.setFixedHeight(36)
        self._name_edit.returnPressed.connect(self._on_confirm_name)
        form.addWidget(self._name_edit)

        self._confirm_btn = QtWidgets.QPushButton("Xác nhận")
        self._confirm_btn.setFixedHeight(36)
        self._confirm_btn.clicked.connect(self._on_confirm_name)
        form.addWidget(self._confirm_btn)

        form_wrapper = QtWidgets.QWidget()
        form_wrapper.setLayout(form)
        vbox.addWidget(form_wrapper, alignment=QtCore.Qt.AlignCenter)

        return page

    # ── Page 1: Main UI ──────────────────────────────────────────────────────

    def _build_main_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(page)
        vbox.setContentsMargins(14, 12, 14, 12)
        vbox.setSpacing(10)

        # ── Header ───────────────────────────────────────────────────────────
        header = QtWidgets.QHBoxLayout()
        self._greeting_lbl = QtWidgets.QLabel("")
        header.addWidget(self._greeting_lbl)
        header.addStretch()

        self._change_btn = QtWidgets.QPushButton("Đổi tên")
        self._change_btn.setFlat(False)
        self._change_btn.clicked.connect(self._on_change_name)
        header.addWidget(self._change_btn)
        vbox.addLayout(header)

        # ── Shared DL toolbar ─────────────────────────────────────────────
        dl_toolbar = QtWidgets.QHBoxLayout()
        dl_toolbar.setSpacing(6)

        self._add_titles_btn = QtWidgets.QPushButton("+ Tên phim đã tải")
        self._add_titles_btn.setToolTip("Nhập thủ công danh sách tên phim đã tải")
        self._add_titles_btn.clicked.connect(self._on_add_titles)
        dl_toolbar.addWidget(self._add_titles_btn)

        self._pick_folder_btn = QtWidgets.QPushButton("Chọn thư mục phim đã tải")
        self._pick_folder_btn.setToolTip(
            "Quét tên các thư mục con trong folder để tự động thêm danh sách phim đã tải"
        )
        self._pick_folder_btn.clicked.connect(self._on_pick_folder)
        dl_toolbar.addWidget(self._pick_folder_btn)

        self._dl_count_lbl = QtWidgets.QLabel("")
        dl_toolbar.addWidget(self._dl_count_lbl)
        dl_toolbar.addStretch()
        vbox.addLayout(dl_toolbar)

        # ── Provider sub-tabs ────────────────────────────────────────────────
        self._provider_tabs = QtWidgets.QTabWidget()
        for provider, label in PROVIDERS.items():
            panel_widget = self._build_provider_panel(provider)
            self._provider_tabs.addTab(panel_widget, label)
        vbox.addWidget(self._provider_tabs, stretch=1)

        return page

    # ── Provider panel ────────────────────────────────────────────────────────

    def _build_provider_panel(self, provider: str) -> QtWidgets.QWidget:
        """Build UI panel for one crawl provider and register it in self._panels."""
        panel_widget = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(panel_widget)
        vbox.setContentsMargins(10, 10, 10, 10)
        vbox.setSpacing(8)

        label = PROVIDERS[provider]

        # ── Send group ───────────────────────────────────────────────────────
        send_group = QtWidgets.QGroupBox(f"Gửi yêu cầu crawl {label}")
        send_layout = QtWidgets.QHBoxLayout(send_group)
        send_layout.setSpacing(8)

        id_label_text = "shortPlayId:" if provider == "netshort" else "ID:"
        send_layout.addWidget(QtWidgets.QLabel(id_label_text))

        id_edit = QtWidgets.QLineEdit()
        placeholder = (
            "Ví dụ: 2038899742804541441" if provider == "netshort"
            else "Ví dụ: zntfl372BV"
        )
        id_edit.setPlaceholderText(placeholder)
        id_edit.setMinimumWidth(220)
        id_edit.setFixedHeight(32)
        id_edit.returnPressed.connect(lambda p=provider: self._on_submit(p))
        send_layout.addWidget(id_edit, stretch=1)

        submit_btn = QtWidgets.QPushButton("Gửi yêu cầu")
        submit_btn.setFixedHeight(32)
        submit_btn.clicked.connect(lambda _=False, p=provider: self._on_submit(p))
        send_layout.addWidget(submit_btn)
        vbox.addWidget(send_group)

        # ── Status label ─────────────────────────────────────────────────────
        status_lbl = QtWidgets.QLabel("")
        status_lbl.setWordWrap(True)
        status_lbl.setMinimumHeight(18)
        vbox.addWidget(status_lbl)

        # ── Queue group ──────────────────────────────────────────────────────
        queue_group = QtWidgets.QGroupBox("Yêu cầu của bạn")
        queue_vbox = QtWidgets.QVBoxLayout(queue_group)
        queue_vbox.setSpacing(6)

        # Refresh toolbar
        toolbar = QtWidgets.QHBoxLayout()
        refresh_btn = QtWidgets.QPushButton("Làm mới")
        refresh_btn.clicked.connect(lambda _=False, p=provider: self._do_refresh(p))
        toolbar.addWidget(refresh_btn)

        info_lbl = QtWidgets.QLabel("")
        toolbar.addWidget(info_lbl)
        toolbar.addStretch()

        next_refresh_lbl = QtWidgets.QLabel("")
        toolbar.addWidget(next_refresh_lbl)
        queue_vbox.addLayout(toolbar)

        # Wait time banner
        wait_time_lbl = QtWidgets.QLabel("")
        wait_time_lbl.setVisible(False)
        queue_vbox.addWidget(wait_time_lbl)

        # Table
        table = QtWidgets.QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(
            ["shortPlayId", "Tên phim", "Status", "Notes", "Gửi lúc", "Hoàn thành lúc", "Đã tải"]
        )
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QtWidgets.QHeaderView.ResizeToContents)
        hh.setStretchLastSection(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectItems)
        table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setSortingEnabled(True)
        table.horizontalHeader().setSortIndicator(4, QtCore.Qt.DescendingOrder)
        table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table: self._on_table_context_menu(pos, t)
        )
        queue_vbox.addWidget(table)

        vbox.addWidget(queue_group, stretch=1)

        # ── Timer (per-provider) ─────────────────────────────────────────────
        timer = QTimer(self)
        timer.timeout.connect(lambda p=provider: self._do_refresh(p))

        # ── Register panel ───────────────────────────────────────────────────
        self._panels[provider] = {
            "id_edit":          id_edit,
            "submit_btn":       submit_btn,
            "status_lbl":       status_lbl,
            "wait_time_lbl":    wait_time_lbl,
            "table":            table,
            "refresh_btn":      refresh_btn,
            "info_lbl":         info_lbl,
            "next_refresh_lbl": next_refresh_lbl,
            "timer":            timer,
            "submit_worker":    None,
            "refresh_worker":   None,
            "last_rows":        [],
        }

        return panel_widget

    # ── Settings ─────────────────────────────────────────────────────────────

    def _load_settings(self) -> None:
        s = QSettings(_SETTINGS_APP, _SETTINGS_KEY)
        self._username = s.value("username", "", type=str)
        raw = s.value("downloaded_titles", "[]", type=str)
        try:
            self._downloaded_titles = set(json.loads(raw))
        except Exception:
            self._downloaded_titles = set()
        self._update_dl_count_label()
        if self._username:
            self._activate_main_page()
        else:
            self._stack.setCurrentIndex(0)

    def _save_username(self, name: str) -> None:
        QSettings(_SETTINGS_APP, _SETTINGS_KEY).setValue("username", name)
        self._username = name

    def _save_downloaded(self) -> None:
        QSettings(_SETTINGS_APP, _SETTINGS_KEY).setValue(
            "downloaded_titles", json.dumps(sorted(self._downloaded_titles))
        )

    # ── Downloaded titles ─────────────────────────────────────────────────────

    def _is_downloaded(self, name: str) -> bool:
        if not name:
            return False
        name_norm = name.strip().lower()
        return any(t.strip().lower() == name_norm for t in self._downloaded_titles)

    def _update_dl_count_label(self) -> None:
        n = len(self._downloaded_titles)
        self._dl_count_lbl.setText(f"({n} phim đã tải)" if n else "")

    def _on_add_titles(self) -> None:
        dlg = _AddTitlesDialog(self._downloaded_titles, parent=self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self._downloaded_titles = dlg.get_titles()
            self._save_downloaded()
            self._update_dl_count_label()
            self._refresh_downloaded_column()

    def _on_pick_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Chọn thư mục chứa phim đã tải", ""
        )
        if not folder:
            return
        path = Path(folder)
        sub_names = {d.name for d in path.iterdir() if d.is_dir()}
        if not sub_names:
            QtWidgets.QMessageBox.information(
                self, "Không tìm thấy",
                f"Không có thư mục phim nào trong:\n{folder}"
            )
            return
        added = sub_names - self._downloaded_titles
        self._downloaded_titles |= sub_names
        self._save_downloaded()
        self._update_dl_count_label()
        self._refresh_downloaded_column()
        QtWidgets.QMessageBox.information(
            self, "Đã thêm",
            f"Thêm {len(added)} tên phim mới từ thư mục.\n"
            f"Tổng cộng: {len(self._downloaded_titles)} phim đã tải."
        )

    def _refresh_downloaded_column(self) -> None:
        for provider, panel in self._panels.items():
            table = panel["table"]
            for r in range(table.rowCount()):
                name_item = table.item(r, 1)
                name = name_item.text() if name_item else ""
                if name == "—":
                    name = ""
                table.setItem(r, 6, self._make_dl_item(name))

    def _make_dl_item(self, name: str) -> QtWidgets.QTableWidgetItem:
        downloaded = self._is_downloaded(name)
        item = QtWidgets.QTableWidgetItem("✓" if downloaded else "")
        item.setTextAlignment(QtCore.Qt.AlignCenter)
        if downloaded:
            item.setForeground(QtGui.QColor(self._tk.success))
            font = item.font()
            font.setBold(True)
            font.setPointSize(font.pointSize() + 2)
            item.setFont(font)
        return item

    # ── Name flow ─────────────────────────────────────────────────────────────

    def _on_confirm_name(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._name_edit.setFocus()
            return
        self._save_username(name)
        self._activate_main_page()

    def _on_change_name(self) -> None:
        for panel in self._panels.values():
            panel["timer"].stop()
        self._name_edit.setText(self._username)
        self._stack.setCurrentIndex(0)
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _activate_main_page(self) -> None:
        self._greeting_lbl.setText(f"Xin chào, {self._username}!")
        self._stack.setCurrentIndex(1)
        for provider in PROVIDERS:
            self._do_refresh(provider)
            self._panels[provider]["timer"].start(_REFRESH_MS)

    # ── Submit ────────────────────────────────────────────────────────────────

    def _on_submit(self, provider: str) -> None:
        panel = self._panels[provider]
        short_play_id = panel["id_edit"].text().strip()

        if not short_play_id:
            QtWidgets.QMessageBox.warning(self, "Thiếu thông tin", "Vui lòng nhập ID.")
            return
        if not self._supabase_key:
            QtWidgets.QMessageBox.warning(
                self, "Lỗi cấu hình", "SUPABASE_KEY chưa cấu hình trong .env"
            )
            return

        # Confirmation dialog
        dlg = _ConfirmSubmitDialog(PROVIDERS[provider], short_play_id, parent=self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return

        panel["submit_btn"].setEnabled(False)
        self._set_status(provider, "Đang gửi...", self._tk.text_muted)

        worker = _SubmitWorker(
            self._supabase_url, self._supabase_key,
            self._username, short_play_id, provider,
        )
        worker.done.connect(lambda row, p=provider: self._on_submit_done(row, p))
        worker.error.connect(lambda msg, p=provider: self._on_submit_error(msg, p))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda p=provider: self._clear_submit_worker(p))
        panel["submit_worker"] = worker
        worker.start()

    def _clear_submit_worker(self, provider: str) -> None:
        self._panels[provider]["submit_worker"] = None

    def _on_submit_done(self, row: dict, provider: str) -> None:
        panel = self._panels[provider]
        panel["submit_btn"].setEnabled(True)
        already_exists = bool(row and row.get("_already_exists"))
        short_play_id = panel["id_edit"].text().strip()
        panel["id_edit"].clear()

        if not row or not row.get("shortPlayId"):
            row = {
                "shortPlayId": short_play_id,
                "name": None,
                "status": "pending",
                "notes": None,
                "provider": provider,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "author": [self._username],
            }

        status = row.get("status", "pending")
        if already_exists:
            message = f"ID {short_play_id} đã có trong hàng đợi {PROVIDERS[provider]}."
            self._set_status(provider, message, self._tk.warning)
            QtWidgets.QMessageBox.information(self, "ID đã tồn tại", message)
            return
        self._set_status(provider, f"Đã gửi — status: {status}", self._tk.success)

        self._insert_or_update_row(row, provider)
        QtCore.QTimer.singleShot(800, lambda p=provider: self._force_refresh(p))

    def _on_submit_error(self, msg: str, provider: str) -> None:
        self._panels[provider]["submit_btn"].setEnabled(True)
        self._set_status(provider, f"Lỗi gửi yêu cầu: {msg}", self._tk.err)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _do_refresh(self, provider: str) -> None:
        panel = self._panels[provider]
        worker = panel.get("refresh_worker")
        if worker is not None:
            try:
                if worker.isRunning():
                    return
            except RuntimeError:
                panel["refresh_worker"] = None
        if not self._supabase_key:
            panel["info_lbl"].setText("⚠ Chưa cấu hình SUPABASE_KEY")
            return
        panel["refresh_btn"].setEnabled(False)
        panel["info_lbl"].setText("Đang tải...")

        new_worker = _RefreshWorker(self._supabase_url, self._supabase_key, provider)
        new_worker.data_ready.connect(
            lambda rows, pending, p=provider: self._on_data_ready(rows, pending, p)
        )
        new_worker.error.connect(
            lambda msg, p=provider: self._on_refresh_error(msg, p)
        )
        new_worker.finished.connect(new_worker.deleteLater)
        new_worker.finished.connect(lambda p=provider: self._clear_refresh_worker(p))
        panel["refresh_worker"] = new_worker
        new_worker.start()

    def _clear_refresh_worker(self, provider: str) -> None:
        self._panels[provider]["refresh_worker"] = None

    def _on_data_ready(self, all_rows: list, pending_rows: list, provider: str) -> None:
        panel = self._panels[provider]
        panel["refresh_btn"].setEnabled(True)

        my_rows = [
            r for r in all_rows
            if self._username in (r.get("author") or [])
        ]
        my_rows.sort(key=lambda r: _timestamp_value(r.get("created_at")), reverse=True)

        panel["last_rows"] = my_rows
        self._populate_table(my_rows, provider)
        self._update_wait_time(pending_rows, all_rows, provider)

        pending    = sum(1 for r in my_rows if r.get("status") == "pending")
        processing = sum(1 for r in my_rows if r.get("status") in ("processing", "crawling"))
        completed  = sum(1 for r in my_rows if r.get("status") == "completed")

        parts = [f"{len(my_rows)} yêu cầu"]
        if pending:    parts.append(f"{pending} chờ")
        if processing: parts.append(f"{processing} đang xử lý")
        if completed:  parts.append(f"{completed} xong")
        panel["info_lbl"].setText("  |  ".join(parts))
        panel["next_refresh_lbl"].setText("Tự làm mới sau 30s")

    def _on_refresh_error(self, msg: str, provider: str) -> None:
        panel = self._panels[provider]
        panel["refresh_btn"].setEnabled(True)
        panel["info_lbl"].setText(f"Lỗi: {msg}")

    # ── Table ─────────────────────────────────────────────────────────────────

    def _populate_table(self, rows: list, provider: str) -> None:
        table = self._panels[provider]["table"]
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for row in rows:
            r = table.rowCount()
            table.insertRow(r)
            self._fill_table_row(r, row, provider)
        table.setSortingEnabled(True)
        table.sortItems(4, QtCore.Qt.DescendingOrder)

    def _insert_or_update_row(self, row: dict, provider: str) -> None:
        table = self._panels[provider]["table"]
        short_play_id = str(row.get("shortPlayId") or "")
        for r in range(table.rowCount()):
            item = table.item(r, 0)
            if item and item.text() == short_play_id:
                table.setSortingEnabled(False)
                self._fill_table_row(r, row, provider)
                table.setSortingEnabled(True)
                table.sortItems(4, QtCore.Qt.DescendingOrder)
                return
        table.setSortingEnabled(False)
        table.insertRow(0)
        self._fill_table_row(0, row, provider)
        table.setSortingEnabled(True)
        table.sortItems(4, QtCore.Qt.DescendingOrder)

    def _fill_table_row(self, r: int, row: dict, provider: str) -> None:
        tk = self._tk
        table = self._panels[provider]["table"]

        short_play_id = str(row.get("shortPlayId") or "")
        name          = str(row.get("name") or "")
        status        = str(row.get("status") or "pending")
        notes         = str(row.get("notes") or "")
        sent_at       = _fmt_time(row.get("created_at"))
        done_at       = _fmt_time(row.get("completed_at"))

        table.setItem(r, 0, QtWidgets.QTableWidgetItem(short_play_id))

        name_item = QtWidgets.QTableWidgetItem(name if name else "—")
        if not name:
            name_item.setForeground(QtGui.QColor(tk.name_missing))
        table.setItem(r, 1, name_item)

        status_item = QtWidgets.QTableWidgetItem(status)
        status_item.setForeground(QtGui.QColor(tk.status.get(status, tk.text_muted)))
        font = status_item.font()
        font.setBold(status in ("processing", "crawling"))
        status_item.setFont(font)
        table.setItem(r, 2, status_item)

        table.setItem(r, 3, QtWidgets.QTableWidgetItem(notes))
        table.setItem(r, 4, _TimeTableItem(sent_at, row.get("created_at")))

        done_item = _TimeTableItem(done_at, row.get("completed_at"))
        if done_at:
            done_item.setForeground(QtGui.QColor(tk.success))
        table.setItem(r, 5, done_item)

        table.setItem(r, 6, self._make_dl_item(name))

        # Row highlight background
        hl_color = tk.hl.get(status)
        if hl_color:
            brush = QtGui.QBrush(QtGui.QColor(hl_color))
            for col in range(table.columnCount()):
                item = table.item(r, col)
                if item is not None:
                    item.setBackground(brush)

    def _update_wait_time(
        self, pending_rows: list, recent_rows: list, provider: str
    ) -> None:
        panel = self._panels[provider]
        pending_rows.sort(key=lambda row: _timestamp_value(row.get("created_at")))

        my_position = next(
            (i for i, row in enumerate(pending_rows)
             if self._username in (row.get("author") or [])),
            None,
        )
        if my_position is None:
            is_processing = any(
                row.get("status") in ("processing", "crawling")
                and self._username in (row.get("author") or [])
                for row in recent_rows
            )
            panel["wait_time_lbl"].setText(
                "Yêu cầu của bạn đang được xử lý." if is_processing
                else "Bạn không có yêu cầu nào đang chờ."
            )
            panel["wait_time_lbl"].setVisible(True)
            return

        rows_ahead = my_position
        total_minutes = rows_ahead * 10
        if total_minutes == 0:
            estimate = "sắp tới lượt"
        elif total_minutes < 60:
            estimate = f"khoảng {total_minutes} phút"
        else:
            hours, minutes = divmod(total_minutes, 60)
            estimate = f"khoảng {hours} giờ" + (f" {minutes} phút" if minutes else "")

        panel["wait_time_lbl"].setText(
            f"Ước tính thời gian chờ: {estimate} • còn {rows_ahead} yêu cầu phía trước"
        )
        panel["wait_time_lbl"].setVisible(True)

    def _force_refresh(self, provider: str) -> None:
        panel = self._panels[provider]
        panel["refresh_worker"] = None
        self._do_refresh(provider)

    # ── Context menu ──────────────────────────────────────────────────────────

    def _on_table_context_menu(
        self, pos: QtCore.QPoint, table: QtWidgets.QTableWidget
    ) -> None:
        item = table.itemAt(pos)
        if not item:
            return
        text = item.text()
        if not text or text == "—":
            return
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction(f'Copy "{text[:40]}{"…" if len(text) > 40 else ""}"')
        action = menu.exec(table.viewport().mapToGlobal(pos))
        if action == copy_action:
            QtWidgets.QApplication.clipboard().setText(text)

    # ── Helper ────────────────────────────────────────────────────────────────

    def _set_status(self, provider: str, text: str, color: str = "") -> None:
        clr = color or self._tk.status_default
        lbl = self._panels[provider]["status_lbl"]
        lbl.setText(text)
        lbl.setStyleSheet(f"color: {clr}; padding: 2px 4px; font-size: 12px;")
