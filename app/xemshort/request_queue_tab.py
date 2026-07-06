"""Tab 'Yêu cầu Crawl' — gửi shortPlayId crawl requests lên Supabase queue.

Luồng UX:
  - Lần đầu (chưa có tên): hiện màn hình nhập tên → xác nhận → chuyển sang UI chính
  - Sau đó: hiện "Xin chào {tên}" + input shortPlayId + bảng yêu cầu của riêng người dùng
  - Bảng tự làm mới mỗi 30s
  - Cột bảng: shortPlayId | Status | Notes | Gửi lúc | Hoàn thành lúc
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings, QTimer

from .sync_movies_supabase import load_env_file
from .queue_crawl_supabase import fetch_queue_requests, submit_queue_request

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_DEFAULT_SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
_SETTINGS_APP  = "Tool Movie XemShort"
_SETTINGS_KEY  = "RequestQueueTab"
_REFRESH_MS    = 30_000   # 30s auto-refresh

_STATUS_COLORS = {
    "pending":    "#d97706",   # amber
    "processing": "#2563eb",   # blue
    "crawling":   "#2563eb",
    "completed":  "#16a34a",   # green
    "error":      "#dc2626",   # red
    "failed":     "#dc2626",
}

_ROW_HIGHLIGHTS = {
    "processing": "#dbeafe",
    "crawling":   "#dbeafe",
    "error":      "#fee2e2",
    "failed":     "#fee2e2",
}


def _timestamp_value(iso: str | None) -> float:
    """Giá trị timestamp dùng để sort, dữ liệu thiếu/không hợp lệ xếp cuối."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


class _TimeTableItem(QtWidgets.QTableWidgetItem):
    """Hiển thị giờ đã format nhưng vẫn sort theo timestamp thật."""

    def __init__(self, text: str, iso: str | None):
        super().__init__(text)
        self._timestamp = _timestamp_value(iso)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        if isinstance(other, _TimeTableItem):
            return self._timestamp < other._timestamp
        return super().__lt__(other)


# ── Time formatting ───────────────────────────────────────────────────────────

def _fmt_time(iso: str | None) -> str:
    """Format ISO timestamp → 'HH:MM' (hôm nay) hoặc 'dd/mm HH:MM' (ngày khác)."""
    if not iso:
        return ""
    try:
        # Parse ISO (có thể có +00:00 hoặc Z)
        ts = iso.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(ts)
        # Chuyển sang local
        dt_local = dt_utc.astimezone()
        today = datetime.now().date()
        if dt_local.date() == today:
            return dt_local.strftime("Hôm nay, %H:%M")
        return dt_local.strftime("%d/%m %H:%M")
    except Exception:
        return iso[:16].replace("T", " ")


# ── Workers ───────────────────────────────────────────────────────────────────

class _SubmitWorker(QtCore.QThread):
    done  = QtCore.Signal(dict)   # row dict trả về
    error = QtCore.Signal(str)

    def __init__(self, url: str, key: str, author: str, short_play_id: str, parent=None):
        super().__init__(parent)
        self._url          = url
        self._key          = key
        self._author       = author
        self._short_play_id = short_play_id

    def run(self) -> None:
        try:
            row = submit_queue_request(self._url, self._key, self._author, self._short_play_id)
            self.done.emit(row)
        except Exception as exc:
            self.error.emit(str(exc))


class _RefreshWorker(QtCore.QThread):
    data_ready = QtCore.Signal(list, list)
    error      = QtCore.Signal(str)

    def __init__(self, url: str, key: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._key = key

    def run(self) -> None:
        try:
            rows = fetch_queue_requests(self._url, self._key, limit=500)
            pending_rows = fetch_queue_requests(
                self._url, self._key, status="pending", limit=None,
            )
            self.data_ready.emit(rows, pending_rows)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Tab ───────────────────────────────────────────────────────────────────────

class RequestQueueTab(QtWidgets.QWidget):
    """Tab cho phép user gửi yêu cầu crawl và xem danh sách yêu cầu của mình."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._supabase_url  = _DEFAULT_SUPABASE_URL
        self._supabase_key  = ""
        self._username      = ""
        self._submit_worker: _SubmitWorker | None = None
        self._refresh_worker: _RefreshWorker | None = None

        self._load_credentials()
        self._build_ui()

        # Auto-refresh timer (chỉ kích hoạt khi đã có username)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._do_refresh)

        self._load_settings()

    # ── Credentials ──────────────────────────────────────────────────────────

    def _load_credentials(self) -> None:
        load_env_file(_ENV_PATH)
        self._supabase_url = os.environ.get("SUPABASE_URL", _DEFAULT_SUPABASE_URL)
        self._supabase_key = os.environ.get("SUPABASE_KEY", "")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # QStackedWidget: page 0 = nhập tên, page 1 = UI chính
        self._stack = QtWidgets.QStackedWidget()
        root.addWidget(self._stack)

        self._stack.addWidget(self._build_name_page())   # page 0
        self._stack.addWidget(self._build_main_page())   # page 1

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

        title = QtWidgets.QLabel("Nhập tên của bạn để bắt đầu")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #1e293b;")
        vbox.addWidget(title)

        sub = QtWidgets.QLabel("Tên sẽ được lưu trên máy này và dùng để theo dõi yêu cầu của bạn.")
        sub.setAlignment(QtCore.Qt.AlignCenter)
        sub.setStyleSheet("color: #64748b; font-size: 12px;")
        sub.setWordWrap(True)
        vbox.addWidget(sub)

        form = QtWidgets.QHBoxLayout()
        form.setSpacing(8)
        self._name_edit = QtWidgets.QLineEdit()
        self._name_edit.setPlaceholderText("Ví dụ: Lan, Minh, Team A...")
        self._name_edit.setMinimumWidth(240)
        self._name_edit.setMaximumWidth(340)
        self._name_edit.setFixedHeight(36)
        self._name_edit.returnPressed.connect(self._on_confirm_name)
        self._name_edit.setStyleSheet(
            "QLineEdit { border: 1px solid #cbd5e1; border-radius: 6px;"
            " padding: 0 10px; font-size: 14px; }"
            "QLineEdit:focus { border-color: #2563eb; }"
        )
        form.addWidget(self._name_edit)

        confirm_btn = QtWidgets.QPushButton("Xác nhận")
        confirm_btn.setFixedHeight(36)
        confirm_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white; border-radius: 4px;"
            " padding: 4px 20px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #2563eb; }"
        )
        confirm_btn.clicked.connect(self._on_confirm_name)
        form.addWidget(confirm_btn)

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

        # ── Header: greeting + đổi tên ───────────────────────────────────────
        header = QtWidgets.QHBoxLayout()
        self._greeting_lbl = QtWidgets.QLabel("")
        self._greeting_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #1e293b;"
        )
        header.addWidget(self._greeting_lbl)
        header.addStretch()

        change_btn = QtWidgets.QPushButton("Đổi tên")
        change_btn.setStyleSheet(
            "QPushButton { background-color: #F35D06; color: #FCFBFB; border: 1px solid #cbd5e1;"
            " border-radius: 4px; padding: 4px 14px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #cbd5e1; color: #1e293b; border-color: #94a3b8; }"
            "QPushButton:disabled { background-color: #f1f5f9; color: #94a3b8; border-color: #e2e8f0; }"
        )
        change_btn.setFlat(False)
        change_btn.clicked.connect(self._on_change_name)
        header.addWidget(change_btn)
        vbox.addLayout(header)

        # ── Send group ───────────────────────────────────────────────────────
        send_group = QtWidgets.QGroupBox("Gửi yêu cầu crawl")
        send_layout = QtWidgets.QHBoxLayout(send_group)
        send_layout.setSpacing(8)

        send_layout.addWidget(QtWidgets.QLabel("shortPlayId:"))
        self._id_edit = QtWidgets.QLineEdit()
        self._id_edit.setPlaceholderText("Ví dụ: 2038899742804541441")
        self._id_edit.setMinimumWidth(220)
        self._id_edit.returnPressed.connect(self._on_submit)
        send_layout.addWidget(self._id_edit, stretch=1)

        self._submit_btn = QtWidgets.QPushButton("Gửi yêu cầu")
        self._submit_btn.setFixedHeight(32)
        self._submit_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white; padding: 4px 20px;"
            " border-radius: 4px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #2563eb; }"
            "QPushButton:disabled { background-color: #93c5fd; color: #fff; }"
        )
        self._submit_btn.clicked.connect(self._on_submit)
        send_layout.addWidget(self._submit_btn)
        vbox.addWidget(send_group)

        # ── Status message ───────────────────────────────────────────────────
        self._status_lbl = QtWidgets.QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setMinimumHeight(18)
        vbox.addWidget(self._status_lbl)

        # ── Queue table ──────────────────────────────────────────────────────
        queue_group = QtWidgets.QGroupBox("Yêu cầu của bạn")
        queue_vbox = QtWidgets.QVBoxLayout(queue_group)
        queue_vbox.setSpacing(6)

        toolbar = QtWidgets.QHBoxLayout()
        self._refresh_btn = QtWidgets.QPushButton("Làm mới")
        self._refresh_btn.setStyleSheet(
            "QPushButton { background-color: #1DEB0A; color: #0A0A0A; border: 1px solid #cbd5e1;"
            " border-radius: 4px; padding: 4px 14px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background-color: #cbd5e1; color: #1e293b; border-color: #94a3b8; }"
            "QPushButton:disabled { background-color: #f1f5f9; color: #94a3b8; border-color: #e2e8f0; }"
        )
        self._refresh_btn.clicked.connect(self._do_refresh)
        toolbar.addWidget(self._refresh_btn)

        self._info_lbl = QtWidgets.QLabel("")
        self._info_lbl.setStyleSheet("color: #64748b; font-size: 12px;")
        toolbar.addWidget(self._info_lbl)
        toolbar.addStretch()

        self._next_refresh_lbl = QtWidgets.QLabel("")
        self._next_refresh_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        toolbar.addWidget(self._next_refresh_lbl)

        queue_vbox.addLayout(toolbar)

        self._wait_time_lbl = QtWidgets.QLabel("")
        self._wait_time_lbl.setStyleSheet(
            "color: #1e40af; background-color: #eff6ff; border: 1px solid #bfdbfe;"
            " border-radius: 4px; padding: 6px 10px; font-weight: bold; font-size: 12px;"
        )
        self._wait_time_lbl.setVisible(False)
        queue_vbox.addWidget(self._wait_time_lbl)

        self._table = QtWidgets.QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["shortPlayId", "Tên phim", "Status", "Notes", "Gửi lúc", "Hoàn thành lúc"]
        )
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)
        hh.setStretchLastSection(False)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectItems)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setSortIndicator(4, QtCore.Qt.DescendingOrder)
        self._table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        queue_vbox.addWidget(self._table)

        vbox.addWidget(queue_group, stretch=1)
        return page

    # ── Settings ─────────────────────────────────────────────────────────────

    def _load_settings(self) -> None:
        s = QSettings(_SETTINGS_APP, _SETTINGS_KEY)
        self._username = s.value("username", "", type=str)
        if self._username:
            self._activate_main_page()
        else:
            self._stack.setCurrentIndex(0)

    def _save_username(self, name: str) -> None:
        QSettings(_SETTINGS_APP, _SETTINGS_KEY).setValue("username", name)
        self._username = name

    # ── Name flow ─────────────────────────────────────────────────────────────

    def _on_confirm_name(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._name_edit.setFocus()
            return
        self._save_username(name)
        self._activate_main_page()

    def _on_change_name(self) -> None:
        self._timer.stop()
        self._name_edit.setText(self._username)
        self._stack.setCurrentIndex(0)
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _activate_main_page(self) -> None:
        self._greeting_lbl.setText(f"Xin chào, {self._username}!")
        self._stack.setCurrentIndex(1)
        self._do_refresh()
        self._timer.start(_REFRESH_MS)

    # ── Submit ────────────────────────────────────────────────────────────────

    def _on_submit(self) -> None:
        short_play_id = self._id_edit.text().strip()
        if not short_play_id:
            QtWidgets.QMessageBox.warning(self, "Thiếu thông tin", "Vui lòng nhập shortPlayId.")
            return
        if not self._supabase_key:
            QtWidgets.QMessageBox.warning(
                self, "Lỗi cấu hình", "SUPABASE_KEY chưa cấu hình trong .env"
            )
            return

        self._submit_btn.setEnabled(False)
        self._set_status("Đang gửi...", "#475569")

        self._submit_worker = _SubmitWorker(
            self._supabase_url, self._supabase_key,
            self._username, short_play_id,
        )
        self._submit_worker.done.connect(self._on_submit_done)
        self._submit_worker.error.connect(self._on_submit_error)
        self._submit_worker.finished.connect(self._submit_worker.deleteLater)
        self._submit_worker.finished.connect(lambda: setattr(self, "_submit_worker", None))
        self._submit_worker.start()

    def _on_submit_done(self, row: dict) -> None:
        self._submit_btn.setEnabled(True)
        already_exists = bool(row and row.get("_already_exists"))
        # Lấy short_play_id trước khi clear input
        short_play_id = self._id_edit.text().strip()
        self._id_edit.clear()

        # Nếu Supabase không trả về row đầy đủ, tự tạo placeholder
        if not row or not row.get("shortPlayId"):
            row = {
                "shortPlayId": short_play_id,
                "name": None,
                "status": "pending",
                "notes": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "author": [self._username],
            }

        status = row.get("status", "pending")
        if already_exists:
            message = f"Tập {short_play_id} đã có trong hàng đợi."
            self._set_status(message, "#d97706")
            QtWidgets.QMessageBox.information(self, "Tập đã tồn tại", message)
            return
        else:
            self._set_status(f"Đã gửi — status: {status}", "#16a34a")

        # Thêm row vào table ngay lập tức (không chờ refresh)
        self._insert_or_update_row(row)

        # Refresh trong background để đồng bộ data thật
        QtCore.QTimer.singleShot(800, self._force_refresh)

    def _on_submit_error(self, msg: str) -> None:
        self._submit_btn.setEnabled(True)
        self._set_status(f"Lỗi gửi yêu cầu: {msg}", "#dc2626")

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _do_refresh(self) -> None:
        if self._refresh_worker is not None:
            try:
                if self._refresh_worker.isRunning():
                    return
            except RuntimeError:
                self._refresh_worker = None
        if not self._supabase_key:
            self._info_lbl.setText("⚠ Chưa cấu hình SUPABASE_KEY")
            return
        self._refresh_btn.setEnabled(False)
        self._info_lbl.setText("Đang tải...")

        self._refresh_worker = _RefreshWorker(
            self._supabase_url, self._supabase_key,
        )
        self._refresh_worker.data_ready.connect(self._on_data_ready)
        self._refresh_worker.error.connect(self._on_refresh_error)
        self._refresh_worker.finished.connect(self._refresh_worker.deleteLater)
        self._refresh_worker.finished.connect(lambda: setattr(self, "_refresh_worker", None))
        self._refresh_worker.start()

    def _on_data_ready(self, all_rows: list, pending_rows: list) -> None:
        self._refresh_btn.setEnabled(True)

        # Filter: chỉ hiện request của user này (username xuất hiện trong author array)
        my_rows = [
            r for r in all_rows
            if self._username in (r.get("author") or [])
        ]
        my_rows.sort(key=lambda row: _timestamp_value(row.get("created_at")), reverse=True)
        self._populate_table(my_rows)
        self._update_wait_time(pending_rows, all_rows)

        pending    = sum(1 for r in my_rows if r.get("status") == "pending")
        processing = sum(1 for r in my_rows if r.get("status") in ("processing", "crawling"))
        completed  = sum(1 for r in my_rows if r.get("status") == "completed")

        parts = [f"{len(my_rows)} yêu cầu"]
        if pending:    parts.append(f"{pending} chờ")
        if processing: parts.append(f"{processing} đang xử lý")
        if completed:  parts.append(f"{completed} xong")
        self._info_lbl.setText("  |  ".join(parts))
        self._next_refresh_lbl.setText("Tự làm mới sau 30s")

    def _on_refresh_error(self, msg: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._info_lbl.setText(f"Lỗi: {msg}")

    # ── Table ─────────────────────────────────────────────────────────────────

    def _populate_table(self, rows: list) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        for row in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._fill_table_row(r, row)
        self._table.setSortingEnabled(True)
        self._table.sortItems(4, QtCore.Qt.DescendingOrder)

    def _insert_or_update_row(self, row: dict) -> None:
        """Thêm hoặc cập nhật một row trong table ngay lập tức (không cần refresh)."""
        short_play_id = str(row.get("shortPlayId") or "")
        # Tìm row đã tồn tại trong table (theo shortPlayId cột 0)
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item and item.text() == short_play_id:
                self._table.setSortingEnabled(False)
                self._fill_table_row(r, row)
                self._table.setSortingEnabled(True)
                self._table.sortItems(4, QtCore.Qt.DescendingOrder)
                return
        # Chèn row mới lên đầu
        self._table.setSortingEnabled(False)
        self._table.insertRow(0)
        self._fill_table_row(0, row)
        self._table.setSortingEnabled(True)
        self._table.sortItems(4, QtCore.Qt.DescendingOrder)

    def _fill_table_row(self, r: int, row: dict) -> None:
        """Điền dữ liệu vào một hàng của table."""
        short_play_id = str(row.get("shortPlayId") or "")
        name          = str(row.get("name") or "")
        status        = str(row.get("status") or "pending")
        notes         = str(row.get("notes") or "")
        sent_at       = _fmt_time(row.get("created_at"))
        done_at       = _fmt_time(row.get("completed_at"))

        self._table.setItem(r, 0, QtWidgets.QTableWidgetItem(short_play_id))

        name_item = QtWidgets.QTableWidgetItem(name if name else "—")
        if not name:
            name_item.setForeground(QtGui.QColor("#94a3b8"))
        self._table.setItem(r, 1, name_item)

        status_item = QtWidgets.QTableWidgetItem(status)
        status_item.setForeground(QtGui.QColor(_STATUS_COLORS.get(status, "#475569")))
        font = status_item.font()
        font.setBold(status in ("processing", "crawling"))
        status_item.setFont(font)
        self._table.setItem(r, 2, status_item)

        self._table.setItem(r, 3, QtWidgets.QTableWidgetItem(notes))
        self._table.setItem(r, 4, _TimeTableItem(sent_at, row.get("created_at")))

        done_item = _TimeTableItem(done_at, row.get("completed_at"))
        if done_at:
            done_item.setForeground(QtGui.QColor("#16a34a"))
        self._table.setItem(r, 5, done_item)

        background = _ROW_HIGHLIGHTS.get(status)
        if background:
            brush = QtGui.QBrush(QtGui.QColor(background))
            for column in range(self._table.columnCount()):
                item = self._table.item(r, column)
                if item is not None:
                    item.setBackground(brush)

    def _update_wait_time(self, pending_rows: list, recent_rows: list) -> None:
        """Ước tính lượt của request pending cũ nhất của user theo FIFO, 10 phút/row."""
        pending_rows.sort(key=lambda row: _timestamp_value(row.get("created_at")))

        my_position = next(
            (index for index, row in enumerate(pending_rows)
             if self._username in (row.get("author") or [])),
            None,
        )
        if my_position is None:
            is_processing = any(
                row.get("status") in ("processing", "crawling")
                and self._username in (row.get("author") or [])
                for row in recent_rows
            )
            self._wait_time_lbl.setText(
                "Yêu cầu của bạn đang được xử lý." if is_processing
                else "Bạn không có yêu cầu nào đang chờ."
            )
            self._wait_time_lbl.setVisible(True)
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

        self._wait_time_lbl.setText(
            f"Ước tính thời gian chờ: {estimate} • còn {rows_ahead} yêu cầu phía trước"
        )
        self._wait_time_lbl.setVisible(True)

    def _force_refresh(self) -> None:
        """Refresh bất kể worker cũ có đang chạy không."""
        self._refresh_worker = None
        self._do_refresh()

    # ── Context menu ──────────────────────────────────────────────────────────

    def _on_table_context_menu(self, pos: QtCore.QPoint) -> None:
        item = self._table.itemAt(pos)
        if not item:
            return
        text = item.text()
        if not text or text == "—":
            return
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction(f'Copy "{text[:40]}{"…" if len(text) > 40 else ""}"')
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == copy_action:
            QtWidgets.QApplication.clipboard().setText(text)

    # ── Helper ────────────────────────────────────────────────────────────────

    def _set_status(self, text: str, color: str = "#334155") -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color}; padding: 2px 4px; font-size: 12px;")
