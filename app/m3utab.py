"""M3U8 Download Tab — pure-M3U8 downloader using yt-dlp, running multiple concurrent workers."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
import subprocess as sp
import uuid
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from m3utab_models import M3U8Item
from m3utab_workers import M3U8DownloadWorker

logger = logging.getLogger(__name__)

_APP_NAME = "M3U8-GUI"
_CONFIG_KEY = "m3utab"


# -------------------------------------------------------------------------- #
# Helpers
# -------------------------------------------------------------------------- #

def _dark_btn(
    color: str = "#2563eb",
    hover: str = "#1d4ed8",
    text: str = "white",
    padding: str = "5px 14px",
) -> str:
    """Return a CSS stylesheet string for a dark-themed QPushButton."""
    return (
        f"QPushButton {{ background-color: {color}; color: {text}; "
        f"padding: {padding}; border-radius: 5px; font-weight: bold; border: none; }}"
        f"QPushButton:hover {{ background-color: {hover}; }}"
        f"QPushButton:disabled {{ background-color: #374151; color: #6b7280; }}"
    )


def _dark_input() -> str:
    return (
        "QLineEdit { background-color: #ffffff; color: #111827; "
        "border: 1px solid #d1d5db; border-radius: 4px; padding: 4px 8px; }"
        "QLineEdit:focus { border-color: #3b82f6; }"
    )


def _sanitize_filename(name: str) -> str:
    """Strip unsafe filesystem characters and truncate to 200 chars."""
    import re
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:200] or "Untitled"


# -------------------------------------------------------------------------- #
# Column layout for the table
# -------------------------------------------------------------------------- #

_COL_NAME = 0
_COL_URL = 1
_COL_FMT = 2
_COL_STATUS = 3
_COL_PROGRESS = 4
_COL_SPEED = 5
_COL_ETA = 6
_COL_ACTIONS = 7
_NUM_COLS = 8


# -------------------------------------------------------------------------- #
# M3U8Tab
# -------------------------------------------------------------------------- #

class M3U8Tab(QtWidgets.QWidget):
    """
    Standalone tab widget for downloading M3U8 (and any yt-dlp-supported) URLs.

    User flow
    ─────────
    1. Pick save directory (top config bar).
    2. Enter URL + optional custom name → click Add.
       The item lands in the table with status "Pending".
    3. Click "Start All" → every "Pending" item launches a background worker.
       Multiple workers run concurrently (controlled by Concurrency spinbox).
    4. Each row updates in real time: progress bar, speed, ETA, status text.
    5. Click "Stop All" to gracefully cancel running workers.
    6. Click the folder icon on any row to open that item's save directory.
    7. Completed rows show a green "Done" badge; error rows show red "Error".
    """

    def __init__(self):
        super().__init__()
        # ── State ──────────────────────────────────────────────────────────
        self.items: list[M3U8Item] = []          # model data
        self.workers: dict[int, M3U8DownloadWorker] = {}  # item_id → worker
        self._row_for_id: dict[int, int] = {}      # item_id → table row
        self._next_id: int = 1
        self._log_lines: list[str] = []
        self._log_max = 2000
        self._batch_total = 0      # tổng số cần hoàn thành trong batch Start All
        self._batch_remaining = 0  # số còn lại chưa xong

        self._build_ui()
        self._load_settings()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        """Assemble the tab layout: config bar → add bar → table → action bar → log panel."""
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Config bar ────────────────────────────────────────────────────
        root.addWidget(self._build_config_bar())

        # ── Add bar ──────────────────────────────────────────────────────
        root.addWidget(self._build_add_bar())

        # ── Table ────────────────────────────────────────────────────────
        root.addWidget(self._build_table(), stretch=1)

        # ── Action bar ────────────────────────────────────────────────────
        root.addWidget(self._build_action_bar())

        # ── Log panel ────────────────────────────────────────────────────
        root.addWidget(self._build_log_panel(), stretch=0)

    def _build_config_bar(self) -> QtWidgets.QWidget:
        """Save directory, concurrency, and yt-dlp args inputs."""
        grp = QtWidgets.QGroupBox("Cấu hình")
        lay = QtWidgets.QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)

        lay.addWidget(QtWidgets.QLabel("Thư mục lưu:"))
        self._cfg_save_dir = QtWidgets.QLineEdit()
        self._cfg_save_dir.setPlaceholderText("Chọn thư mục lưu video...")
        self._cfg_save_dir.setStyleSheet(_dark_input())
        self._cfg_save_dir.setMinimumWidth(350)
        lay.addWidget(self._cfg_save_dir, stretch=1)

        btn_browse = QtWidgets.QPushButton("📁")
        btn_browse.setToolTip("Chọn thư mục")
        btn_browse.setStyleSheet(_dark_btn("#374151", "#4b5563", padding="4px 10px"))
        btn_browse.clicked.connect(self._on_browse_save_dir)
        lay.addWidget(btn_browse)

        lay.addSpacing(10)

        lay.addWidget(QtWidgets.QLabel("Luồng:"))
        self._cfg_concurrency = QtWidgets.QSpinBox()
        self._cfg_concurrency.setRange(1, 10)
        self._cfg_concurrency.setValue(3)
        self._cfg_concurrency.setFixedWidth(60)
        self._cfg_concurrency.setStyleSheet(
            "QSpinBox { background: #ffffff; color: #111827; border: 1px solid #d1d5db; "
            "border-radius: 4px; padding: 4px; }"
        )
        self._cfg_concurrency.setToolTip("Số lượng video tải đồng thời")
        lay.addWidget(self._cfg_concurrency)

        lay.addSpacing(10)

        lay.addWidget(QtWidgets.QLabel("yt-dlp args:"))
        self._cfg_ytdlp_args = QtWidgets.QLineEdit()
        self._cfg_ytdlp_args.setPlaceholderText("--no-playlist -f best")
        self._cfg_ytdlp_args.setStyleSheet(_dark_input())
        self._cfg_ytdlp_args.setMinimumWidth(200)
        self._cfg_ytdlp_args.setToolTip("Thêm tham số yt-dlp tùy chỉnh (ví dụ: -f best, --no-playlist)")
        lay.addWidget(self._cfg_ytdlp_args, stretch=0)

        return grp

    def _build_add_bar(self) -> QtWidgets.QWidget:
        """Add-bar: name input, URL input, format selector, and Add button."""
        grp = QtWidgets.QGroupBox("Thêm video")
        lay = QtWidgets.QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        lay.addWidget(QtWidgets.QLabel("Tên:"))
        self._add_name = QtWidgets.QLineEdit()
        self._add_name.setPlaceholderText("Tên video (tùy chọn)")
        self._add_name.setStyleSheet(_dark_input())
        self._add_name.setMinimumWidth(150)
        self._add_name.returnPressed.connect(self._on_add_clicked)
        lay.addWidget(self._add_name, stretch=0)

        lay.addWidget(QtWidgets.QLabel("URL:"))
        self._add_url = QtWidgets.QLineEdit()
        self._add_url.setPlaceholderText("https://example.com/video.m3u8 ...")
        self._add_url.setStyleSheet(_dark_input())
        self._add_url.returnPressed.connect(self._on_add_clicked)
        lay.addWidget(self._add_url, stretch=1)

        lay.addWidget(QtWidgets.QLabel("F:"))
        self._add_fmt = QtWidgets.QComboBox()
        self._add_fmt.addItems(["mp4", "m3u8"])
        self._add_fmt.setStyleSheet("""
            QComboBox {
                background-color: #ffffff;
                color: #111827;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                padding: 3px 6px;
                min-width: 60px;
            }
            QComboBox::drop-down { border: none; width: 18px; }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #6b7280;
                margin-right: 4px;
            }
        """)
        self._add_fmt.setToolTip("Chọn định dạng: mp4 hoặc m3u8")
        lay.addWidget(self._add_fmt)

        btn = QtWidgets.QPushButton("Thêm")
        btn.setStyleSheet(_dark_btn())
        btn.clicked.connect(self._on_add_clicked)
        lay.addWidget(btn)

        return grp

    def _build_table(self) -> QtWidgets.QTableWidget:
        """Build and configure the download queue table."""
        self._table = QtWidgets.QTableWidget()
        self._table.setColumnCount(_NUM_COLS)
        self._table.setHorizontalHeaderLabels(
            ["Tên", "URL", "F", "Trạng thái", "Tiến độ", "Tốc độ", "ETA", "Hành động"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setStyleSheet(self._table_style())
        self._table.horizontalHeader().setStretchLastSection(True)

        # Column widths
        col_widths = {
            _COL_NAME: 160,
            _COL_URL: 260,
            _COL_FMT: 50,
            _COL_STATUS: 110,
            _COL_PROGRESS: 130,
            _COL_SPEED: 80,
            _COL_ETA: 65,
            _COL_ACTIONS: 150,
        }
        for col, w in col_widths.items():
            self._table.setColumnWidth(col, w)

        self._table.cellClicked.connect(self._on_table_click)
        return self._table

    @staticmethod
    def _table_style() -> str:
        """Return the complete dark theme stylesheet for the download queue table."""
        return """
            QTableWidget {
                background-color: #111827;
                color: #f3f4f6;
                gridline-color: #1f2937;
                border: 1px solid #1f2937;
                border-radius: 6px;
                font-size: 12px;
            }
            QTableWidget::item {
                padding: 4px 6px;
            }
            QTableWidget::item:selected {
                background-color: #1e3a5f;
                color: #f3f4f6;
            }
            QHeaderView::section {
                background-color: #1f2937;
                color: #d1d5db;
                padding: 5px 8px;
                border: none;
                border-right: 1px solid #374151;
                border-bottom: 1px solid #374151;
                font-weight: bold;
                font-size: 12px;
            }
            QScrollBar:vertical {
                background: #111827;
                width: 8px;
            }
            QScrollBar::handle:vertical { background: #374151; border-radius: 4px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal { background: #111827; height: 8px; }
            QScrollBar::handle:horizontal { background: #374151; border-radius: 4px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        """

    def _build_action_bar(self) -> QtWidgets.QWidget:
        """Start All, Stop All, Clear Done, Reset, overall progress bar, and item count."""
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 4)

        self._btn_start_all = QtWidgets.QPushButton("▶ Start All")
        self._btn_start_all.setStyleSheet(_dark_btn("#16a34a", "#15803d", padding="6px 18px"))
        self._btn_start_all.clicked.connect(self._on_start_all)
        lay.addWidget(self._btn_start_all)

        self._btn_stop_all = QtWidgets.QPushButton("⏹ Stop All")
        self._btn_stop_all.setStyleSheet(_dark_btn("#dc2626", "#b91c1c", padding="6px 18px"))
        self._btn_stop_all.setEnabled(False)
        self._btn_stop_all.clicked.connect(self._on_stop_all)
        lay.addWidget(self._btn_stop_all)

        self._btn_clear_done = QtWidgets.QPushButton("🗑 Xóa hoàn thành")
        self._btn_clear_done.setStyleSheet(_dark_btn("#6b7280", "#4b5563", padding="6px 14px"))
        self._btn_clear_done.clicked.connect(self._on_clear_done)
        lay.addWidget(self._btn_clear_done)

        self._btn_reset = QtWidgets.QPushButton("🔄 Reset")
        self._btn_reset.setStyleSheet(_dark_btn("#6b7280", "#4b5563", padding="6px 14px"))
        self._btn_reset.setToolTip("Xóa toàn bộ danh sách và reset tiến độ")
        self._btn_reset.clicked.connect(self._on_reset_all)
        lay.addWidget(self._btn_reset)

        self._overall_pb = QtWidgets.QProgressBar()
        self._overall_pb.setFixedHeight(16)
        self._overall_pb.setFormat("Tổng: %p%")
        self._overall_pb.setStyleSheet("""
            QProgressBar {
                background-color: #1f2937;
                border: none;
                border-radius: 8px;
                text-align: center;
                color: #f3f4f6;
                font-size: 11px;
            }
            QProgressBar::chunk {
                background-color: #3b82f6;
                border-radius: 8px;
            }
        """)
        lay.addWidget(self._overall_pb, stretch=1)

        self._lbl_count = QtWidgets.QLabel("0 video")
        self._lbl_count.setStyleSheet("color: #9ca3af; font-size: 12px;")
        lay.addWidget(self._lbl_count)

        return w

    def _build_log_panel(self) -> QtWidgets.QWidget:
        """Read-only log panel showing timestamped download messages."""
        grp = QtWidgets.QGroupBox("Nhật ký")
        lay = QtWidgets.QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 6)

        self._log_widget = QtWidgets.QTextEdit()
        self._log_widget.setReadOnly(True)
        self._log_widget.setMaximumHeight(130)
        self._log_widget.setStyleSheet("""
            QTextEdit {
                background-color: #0f1117;
                color: #9ca3af;
                border: 1px solid #1f2937;
                border-radius: 4px;
                font-family: Consolas, monospace;
                font-size: 11px;
                padding: 4px;
            }
        """)
        lay.addWidget(self._log_widget)

        return grp

    # ------------------------------------------------------------------ Settings
    def settings(self) -> QtCore.QSettings:
        """Return a QSettings instance scoped to M3U8-GUI / m3utab."""
        return QtCore.QSettings(_APP_NAME, _CONFIG_KEY)

    def _load_settings(self):
        """Load persisted save_dir, concurrency, and yt-dlp args from QSettings."""
        s = self.settings()
        self._cfg_save_dir.setText(s.value("save_dir", ""))
        self._cfg_concurrency.setValue(int(s.value("concurrency", 3)))
        self._cfg_ytdlp_args.setText(s.value("ytdlp_args", ""))

    def _save_settings(self):
        """Persist save_dir, concurrency, and yt-dlp args to QSettings."""
        s = self.settings()
        s.setValue("save_dir", self._cfg_save_dir.text())
        s.setValue("concurrency", self._cfg_concurrency.value())
        s.setValue("ytdlp_args", self._cfg_ytdlp_args.text())

    # ------------------------------------------------------------------ Actions
    def _on_browse_save_dir(self):
        """Open a native file dialog to select the save directory."""
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Chọn thư mục lưu video",
            self._cfg_save_dir.text() or str(Path.home()),
            QtWidgets.QFileDialog.Option.ShowDirsOnly,
        )
        if path:
            self._cfg_save_dir.setText(path)
            self._save_settings()

    def _on_add_clicked(self):
        """Validate inputs, create an M3U8Item, append it to the table and model."""
        url = self._add_url.text().strip()
        if not url:
            QtWidgets.QMessageBox.information(self, "Thiếu URL", "Vui lòng nhập URL video.")
            return

        name = self._add_name.text().strip()
        if not name:
            name = self._derive_name_from_url(url)

        # Validate: reject duplicate name
        for existing in self.items:
            if existing.name.strip().lower() == name.strip().lower():
                QtWidgets.QMessageBox.information(
                    self, "Trùng tên",
                    f'Tên "{name}" đã tồn tại. Vui lòng đổi tên khác.'
                )
                self._add_name.selectAll()
                self._add_name.setFocus()
                return

        save_dir = Path(self._cfg_save_dir.text().strip())
        if not save_dir.name:
            QtWidgets.QMessageBox.information(self, "Thiếu thư mục", "Vui lòng chọn thư mục lưu.")
            return
        save_dir.mkdir(parents=True, exist_ok=True)

        # Derive name from URL if not provided
        if not name:
            name = self._derive_name_from_url(url)

        item = M3U8Item(
            id=self._next_id,
            url=url,
            name=name,
            save_dir=save_dir,
            fmt=self._add_fmt.currentText(),
            status="pending",
        )
        self._next_id += 1

        self.items.append(item)
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._row_for_id[item.id] = row
        self._fill_row(row, item)
        self._update_count_label()
        self._log(f"Đã thêm: {name} ({url[:60]}...)")

        self._add_url.clear()
        self._add_name.clear()
        self._add_url.setFocus()
        self._save_settings()

    def _derive_name_from_url(self, url: str) -> str:
        """Extract a human-readable name from the last path segment of a URL."""
        import re
        try:
            path = re.sub(r"^\w+://", "", url).split("?")[0].split("/")
            name = path[-1] if path else "video"
            name = re.sub(r"\.(m3u8|mp4|mkv|webm)(\?.*)?$", "", name, flags=re.IGNORECASE)
            name = re.sub(r"[-_]", " ", name)
            return name.strip()[:100] or "video"
        except Exception:
            return "video"

    def _fill_row(self, row: int, item: M3U8Item):
        """Populate (or refresh) all cells for a given row: name, URL, format, status, progress bar, speed, ETA, actions."""
        # Name
        name_item = QtWidgets.QTableWidgetItem(item.name)
        name_item.setForeground(QtGui.QBrush(QtGui.QColor("#f3f4f6")))
        self._table.setItem(row, _COL_NAME, name_item)

        # URL
        url_item = QtWidgets.QTableWidgetItem(item.url)
        url_item.setForeground(QtGui.QBrush(QtGui.QColor("#9ca3af")))
        url_item.setToolTip(item.url)
        self._table.setItem(row, _COL_URL, url_item)

        # Format badge
        fmt_color = "#22c55e" if item.fmt == "m3u8" else "#3b82f6"
        fmt_item = QtWidgets.QTableWidgetItem(item.fmt.upper())
        fmt_item.setForeground(QtGui.QBrush(QtGui.QColor(fmt_color)))
        fmt_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, _COL_FMT, fmt_item)

        # Status
        self._set_status_cell(row, item.status, item.error_msg)

        # Progress bar (col 3)
        pb = QtWidgets.QProgressBar()
        pb.setStyleSheet(self._pb_style(item.status))
        pb.setValue(int(item.progress))
        pb.setTextVisible(False)
        pb.setFixedHeight(10)
        self._table.setCellWidget(row, _COL_PROGRESS, pb)

        # Speed
        sp_item = QtWidgets.QTableWidgetItem(item.speed)
        sp_item.setForeground(QtGui.QBrush(QtGui.QColor("#f97316")))
        self._table.setItem(row, _COL_SPEED, sp_item)

        # ETA
        eta_item = QtWidgets.QTableWidgetItem(item.eta)
        eta_item.setForeground(QtGui.QBrush(QtGui.QColor("#9ca3af")))
        self._table.setItem(row, _COL_ETA, eta_item)

        # Actions
        self._set_actions_cell(row, item)

    def _set_status_cell(self, row: int, status: str, error_msg: str = ""):
        """Write a colored status badge into the status column (e.g. "Đang tải...", "Hoàn thành", "Lỗi")."""
        colors = {
            "pending":   ("#6b7280",  "Chờ"),
            "downloading": ("#3b82f6", "Đang tải..."),
            "done":      ("#16a34a",  "✅ Hoàn thành"),
            "error":     ("#dc2626",  "❌ Lỗi"),
            "stopped":   ("#d97706",  "⏹ Đã dừng"),
        }
        color, label = colors.get(status, ("#9ca3af", status))
        item = QtWidgets.QTableWidgetItem(label)
        item.setForeground(QtGui.QBrush(QtGui.QColor(color)))
        item.setToolTip(error_msg)
        self._table.setItem(row, _COL_STATUS, item)

    def _set_actions_cell(self, row: int, item: M3U8Item):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)

        # Folder button (always visible)
        btn_folder = QtWidgets.QPushButton("📁")
        btn_folder.setFixedSize(28, 24)
        btn_folder.setToolTip("Mở thư mục")
        btn_folder.setStyleSheet(_dark_btn("#374151", "#4b5563", padding="2px"))
        btn_folder.clicked.connect(lambda *_, it=item: self._open_item_folder(it))
        lay.addWidget(btn_folder)

        if item.status == "pending":
            # Start individual button
            btn_start = QtWidgets.QPushButton("▶")
            btn_start.setFixedSize(28, 24)
            btn_start.setToolTip("Bắt đầu tải")
            btn_start.setStyleSheet(_dark_btn("#16a34a", "#15803d", padding="2px"))
            btn_start.clicked.connect(lambda *_, it=item: self._start_item(it))
            lay.addWidget(btn_start)

            # Delete button
            btn_del = QtWidgets.QPushButton("🗑")
            btn_del.setFixedSize(28, 24)
            btn_del.setToolTip("Xóa")
            btn_del.setStyleSheet(_dark_btn("#374151", "#7f1d1d", padding="2px"))
            btn_del.clicked.connect(lambda *_, it=item: self._delete_item(it))
            lay.addWidget(btn_del)

        elif item.status == "downloading":
            # Stop button
            btn_stop = QtWidgets.QPushButton("⏹")
            btn_stop.setFixedSize(28, 24)
            btn_stop.setToolTip("Dừng")
            btn_stop.setStyleSheet(_dark_btn("#dc2626", "#b91c1c", padding="2px"))
            btn_stop.clicked.connect(lambda *_, it=item: self._stop_item(it))
            lay.addWidget(btn_stop)

            # Delete button
            btn_del = QtWidgets.QPushButton("🗑")
            btn_del.setFixedSize(28, 24)
            btn_del.setToolTip("Xóa")
            btn_del.setStyleSheet(_dark_btn("#374151", "#7f1d1d", padding="2px"))
            btn_del.clicked.connect(lambda *_, it=item: self._delete_item(it))
            lay.addWidget(btn_del)

        elif item.status in ("done", "error", "stopped"):
            # Copy path button
            if item.status == "done":
                btn_copy = QtWidgets.QPushButton("📋")
                btn_copy.setFixedSize(28, 24)
                btn_copy.setToolTip("Copy path")
                btn_copy.setStyleSheet(_dark_btn("#374151", "#4b5563", padding="2px"))
                btn_copy.clicked.connect(lambda *_, it=item: self._copy_item_path(it))
                lay.addWidget(btn_copy)

                # Play button
                btn_play = QtWidgets.QPushButton("▶")
                btn_play.setFixedSize(28, 24)
                btn_play.setToolTip("Phát video")
                btn_play.setStyleSheet(_dark_btn("#16a34a", "#15803d", padding="2px"))
                btn_play.clicked.connect(lambda *_, it=item: self._play_item(it))
                lay.addWidget(btn_play)

            # Delete button
            btn_del = QtWidgets.QPushButton("🗑")
            btn_del.setFixedSize(28, 24)
            btn_del.setToolTip("Xóa")
            btn_del.setStyleSheet(_dark_btn("#374151", "#7f1d1d", padding="2px"))
            btn_del.clicked.connect(lambda *_, it=item: self._delete_item(it))
            lay.addWidget(btn_del)

            # Retry button if error
            if item.status == "error":
                btn_retry = QtWidgets.QPushButton("↻")
                btn_retry.setFixedSize(28, 24)
                btn_retry.setToolTip("Thử lại")
                btn_retry.setStyleSheet(_dark_btn("#d97706", "#b45309", padding="2px"))
                btn_retry.clicked.connect(lambda *_, it=item: self._start_item(it))
                lay.addWidget(btn_retry)

        self._table.setCellWidget(row, _COL_ACTIONS, w)

    @staticmethod
    def _pb_style(status: str) -> str:
        """Return a per-status colored stylesheet for a download progress bar."""
        base = (
            "QProgressBar { background-color: #1f2937; border: none; border-radius: 5px; "
            "text-align: center; }"
        )
        colors = {
            "pending":     "#374151",
            "downloading": "#3b82f6",
            "done":        "#16a34a",
            "error":       "#dc2626",
            "stopped":     "#d97706",
        }
        color = colors.get(status, "#374151")
        return base + f" QProgressBar::chunk {{ background-color: {color}; border-radius: 5px; }}"

    # ------------------------------------------------------------------ Worker control
    def _start_item(self, item: M3U8Item):
        """Launch a background worker to download `item`."""
        if item.status == "downloading":
            return

        save_dir = Path(self._cfg_save_dir.text().strip())
        if not save_dir.name:
            QtWidgets.QMessageBox.information(self, "Thiếu thư mục", "Vui lòng chọn thư mục lưu.")
            return

        item.status = "downloading"
        item.progress = 0.0
        item.speed = ""
        item.eta = ""
        item.error_msg = ""
        item.save_dir = save_dir
        self._fill_row(self._row_for_id[item.id], item)
        self._update_action_buttons()

        worker = M3U8DownloadWorker(
            url=item.url,
            save_dir=item.save_dir,
            name=item.name,
            fmt=item.fmt,
            ytdlp_args=self._cfg_ytdlp_args.text(),
        )
        item.instance_id = worker.instance_id
        self.workers[item.id] = worker

        worker.log_msg.connect(self._on_worker_log)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.output_ready.connect(self._on_worker_output_ready)
        worker.start()
        self._log(f"[{item.name}] Bắt đầu tải...")

    def _stop_item(self, item: M3U8Item):
        """Send stop signal to the worker's thread and mark item as stopped."""
        if item.id in self.workers:
            self.workers[item.id].stop()
        item.status = "stopped"
        item.progress = 0.0
        row = self._row_for_id.get(item.id, -1)
        if row >= 0:
            self._fill_row(row, item)
        self._log(f"[{item.name}] Đã dừng.")
        self._update_action_buttons()

    def _delete_item(self, item: M3U8Item):
        """Stop the associated worker (blocking), remove the item from model and table."""
        # Stop worker if running and wait for thread to finish
        if item.id in self.workers:
            w = self.workers[item.id]
            w.stop()
            w.quit()
            w.quit()
            w.wait(5000)  # max 5s
            del self.workers[item.id]
        # Remove from model
        if item in self.items:
            self.items.remove(item)
        # Remove from table
        row = self._row_for_id.pop(item.id, -1)
        if row >= 0:
            self._table.removeRow(row)
            # Fix row map after removal
            for rid, r in list(self._row_for_id.items()):
                if r > row:
                    self._row_for_id[rid] = r - 1
        self._update_count_label()
        self._log(f"Đã xóa: {item.name}")
        self._update_action_buttons()

    def _open_item_folder(self, item: M3U8Item):
        """Open the item's save directory in the OS file explorer."""
        path = item.save_dir if item.save_dir and item.save_dir.exists() else self._cfg_save_dir.text()
        if path:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def _copy_item_path(self, item: M3U8Item):
        """Copy the item's output path (or save dir) to the system clipboard."""
        path = item.output_path or item.save_dir
        if path:
            QtWidgets.QApplication.clipboard().setText(str(path))
            self._log(f"Đã copy path: {path}")

    def _play_item(self, item: M3U8Item):
        """Launch the downloaded file with the OS default video player."""
        path = item.output_path
        if path and path.exists():
            try:
                sp.Popen(["start", "", str(path)], shell=True)
            except Exception as e:
                self._log(f"Lỗi khi mở video: {e}")
        else:
            self._log("File không tồn tại hoặc chưa có output path.")

    def _on_start_all(self):
        """Start all pending items up to the configured concurrency limit."""
        save_dir = Path(self._cfg_save_dir.text().strip())
        if not save_dir.name:
            QtWidgets.QMessageBox.information(self, "Thiếu thư mục", "Vui lòng chọn thư mục lưu.")
            return

        pending = [it for it in self.items if it.status == "pending"]
        if not pending:
            self._log("Không có video nào ở trạng thái chờ.")
            return

        self._batch_total = 0
        self._batch_remaining = 0

        concurrency = self._cfg_concurrency.value()
        to_start = pending[:concurrency]
        self._batch_total = len(to_start)
        self._batch_remaining = self._batch_total
        self._log(f"Start All: {self._batch_total} video (concurrency={concurrency})")

        for item in to_start:
            self._start_item(item)

        self._update_action_buttons()

    def _on_stop_all(self):
        """Send stop signal to all running workers."""
        count = 0
        for item in self.items:
            if item.status == "downloading":
                if item.id in self.workers:
                    self.workers[item.id].stop()
                count += 1
        self._log(f"Stop All: đã gửi tín hiệu dừng cho {count} worker.")
        self._update_action_buttons()

    def _on_clear_done(self):
        """Remove all items with status done or stopped from the table and model."""
        to_remove = [it for it in self.items if it.status in ("done", "stopped")]
        if not to_remove:
            self._log("Không có mục nào hoàn thành hoặc đã dừng để xóa.")
            return
        for item in to_remove:
            self._delete_item(item)
        self._log(f"Đã xóa {len(to_remove)} mục đã hoàn thành.")

    def _on_reset_all(self):
        """Stop all workers, clear the entire table, and reset overall progress — same as fresh start."""
        # Stop all running workers and wait for threads to finish
        for item in list(self.items):
            if item.id in self.workers:
                w = self.workers[item.id]
                w.stop()
                w.quit()
                w.wait(5000)  # max 5s per worker
                del self.workers[item.id]

        # Clear model and table
        self.items.clear()
        self.workers.clear()
        self._row_for_id.clear()
        self._table.setRowCount(0)
        self._next_id = 1

        # Reset overall progress bar
        self._overall_pb.setValue(0)
        self._overall_pb.setFormat("Tổng: 0%")

        self._log("Đã reset toàn bộ danh sách.")
        self._update_action_buttons()

    # ------------------------------------------------------------------ Worker signals
    def _on_worker_log(self, instance_id: int, msg: str):
        """Append a worker log message to the log panel."""
        # Guard: ignore logs from workers whose items have been deleted
        item = self._find_item_by_instance(instance_id)
        if item is None or item not in self.items:
            return
        self._log(msg)

    def _on_worker_progress(
        self,
        instance_id: int,
        status: str,
        pct: float,
        speed: str,
        eta: str,
        title: str,
    ):
        """Handle per-item progress update: refresh progress bar, speed, and ETA cells."""
        # Find item by instance_id
        item = self._find_item_by_instance(instance_id)
        # Guard: ignore progress from workers whose items have been deleted
        if item is None or item not in self.items:
            return

        item.progress = pct
        item.speed = speed
        item.eta = eta
        if title and item.name in ("video", ""):
            item.name = title

        row = self._row_for_id.get(item.id, -1)
        if row < 0:
            return

        # Update progress bar
        pb = self._table.cellWidget(row, _COL_PROGRESS)
        if pb:
            pb.setValue(int(pct))

        # Update speed
        sp_item = self._table.item(row, _COL_SPEED)
        if sp_item:
            sp_item.setText(speed)

        # Update ETA
        eta_item = self._table.item(row, _COL_ETA)
        if eta_item:
            eta_item.setText(eta)

        self._update_overall_progress()

    def _on_worker_finished(self, instance_id: int, success: bool, error_msg: str):
        """Handle worker completion: update status, auto-detect output path, refresh actions cell, start next pending item."""
        item = self._find_item_by_instance(instance_id)
        # Guard: item may have been deleted (e.g. user clicked Reset) while worker ran
        if item is None or item not in self.items:
            logger.debug(f"Worker {instance_id} finished but item is already gone, ignoring.")
            return

        # Clean up worker reference
        if item.id in self.workers:
            del self.workers[item.id]

        item.status = "done" if success else "error"
        item.error_msg = error_msg
        item.progress = 100.0 if success else 0.0

        # Fallback: if output_path not set yet, search for the downloaded file
        if success and not item.output_path:
            found = M3U8DownloadWorker._find_output_video(
                item.save_dir, instance_id, item.name
            )
            if found and found.exists():
                item.output_path = found
                self._log(f"[{item.name}] Tìm thấy file: {found.name}")
            else:
                self._log(f"[{item.name}] Không tìm thấy file output — path chưa được xác định")

        row = self._row_for_id.get(item.id, -1)
        if row >= 0:
            self._fill_row(row, item)
            self._set_status_cell(row, item.status, item.error_msg)
            self._set_actions_cell(row, item)

        self._log(
            f"[{item.name}] {'Hoàn thành ✅' if success else f'Lỗi ❌ — {error_msg}'}"
        )
        self._update_action_buttons()
        self._update_overall_progress()

        # Auto-start next pending item up to concurrency limit
        if self._batch_total > 0:
            self._batch_remaining -= 1
            running = sum(1 for it in self.items if it.status == "downloading")
            concurrency = self._cfg_concurrency.value()
            if running < concurrency:
                next_pending = [
                    it for it in self.items
                    if it.status == "pending" and it.id not in self.workers
                ]
                if next_pending:
                    self._start_item(next_pending[0])
                    self._batch_remaining += 1

        self._maybe_show_done_dialog()

    def _on_worker_output_ready(self, instance_id: int, output_path: str):
        """Store the resolved output file path and refresh the actions cell."""
        item = self._find_item_by_instance(instance_id)
        if item is None or item not in self.items:
            return
        item.output_path = Path(output_path)
        row = self._row_for_id.get(item.id, -1)
        if row >= 0:
            self._set_actions_cell(row, item)

    def _find_item_by_instance(self, instance_id: int) -> M3U8Item | None:
        """Find the M3U8Item that owns the given worker instance_id."""
        for item in self.items:
            if item.instance_id == instance_id:
                return item
        return None

    def _maybe_show_done_dialog(self):
        """Show a dialog when all items from the current Start-All batch are finished."""
        if self._batch_total <= 0:
            return

        # Auto-start fills _batch_remaining as new items are started
        running = sum(1 for it in self.items if it.status == "downloading")
        if running > 0:
            return  # still downloading

        # All workers done — check if any pending items were started by this batch
        if self._batch_remaining > 0:
            return  # auto-start still pending, wait

        success_count = sum(1 for it in self.items if it.status == "done")
        fail_count = len(self.items) - success_count
        if fail_count == 0:
            msg = f"Hoàn thành! {success_count} video đã tải xong."
        else:
            msg = f"Tải xong: {success_count} thành công, {fail_count} thất bại."

        self._log(f"[Batch] Hoàn thành — {msg}")

        # Reset batch state BEFORE showing dialog
        self._batch_total = 0
        self._batch_remaining = 0

        # Defer dialog so table finishes updating first
        QtCore.QTimer.singleShot(0, lambda: QtWidgets.QMessageBox.information(
            self, "Download hoàn tất", msg, QtWidgets.QMessageBox.Ok
        ))

    # ------------------------------------------------------------------ UI updates
    def _update_action_buttons(self):
        """Enable/disable action bar buttons based on current item states."""
        running = sum(1 for it in self.items if it.status == "downloading")
        pending = sum(1 for it in self.items if it.status == "pending")

        self._btn_start_all.setEnabled(pending > 0)
        self._btn_stop_all.setEnabled(running > 0)
        self._btn_clear_done.setEnabled(any(it.status in ("done", "stopped") for it in self.items))

        count_text = f"{running} đang chạy / {pending} chờ / {len(self.items)} tổng"
        self._lbl_count.setText(count_text)

    def _update_count_label(self):
        """Shorthand to refresh both action buttons and the item count label."""
        self._update_action_buttons()

    def _update_overall_progress(self):
        """Recalculate and display the aggregate progress across all items."""
        if not self.items:
            self._overall_pb.setValue(0)
            self._overall_pb.setFormat("Tổng: 0%")
            return

        total_progress = sum(it.progress for it in self.items) / len(self.items)
        self._overall_pb.setValue(int(total_progress))
        done = sum(1 for it in self.items if it.status == "done")
        running = sum(1 for it in self.items if it.status == "downloading")
        self._overall_pb.setFormat(f"Tổng: {int(total_progress)}% ({done} done, {running} running)")

    def _log(self, msg: str):
        """Append a timestamped line to the log panel and cap at _log_max lines."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_lines.append(line)
        if len(self._log_lines) > self._log_max:
            self._log_lines = self._log_lines[-self._log_max :]
        self._log_widget.append(line)
        # Auto-scroll to bottom
        scroll = self._log_widget.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    # ------------------------------------------------------------------ Table interaction
    def _on_table_click(self, row: int, col: int):
        """Handle table cell click (reserved for future use, e.g. copy-on-doubleclick)."""
        pass

    # ------------------------------------------------------------------ Close
    def closeEvent(self, event):
        """Persist settings, stop all workers, then close."""
        self._save_settings()
        for item in self.items:
            if item.id in self.workers:
                self.workers[item.id].stop()
        event.accept()
