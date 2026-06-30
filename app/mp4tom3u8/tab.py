"""Tab convert MP4 sang M3U8 dùng mp42hls.exe (Bento4)."""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings, QThread, Signal

from utils import BIN_DIR, ROOT


def _short_path(path: Path) -> str:
    """Trả về Windows 8.3 short path để tránh lỗi ANSI/Unicode trong Bento4.
    Nếu không lấy được (không phải Windows hoặc lỗi), trả về str gốc."""
    if sys.platform != "win32":
        return str(path)
    try:
        k32 = ctypes.windll.kernel32
        buf_len = k32.GetShortPathNameW(str(path), None, 0)
        if buf_len == 0:
            return str(path)
        buf = ctypes.create_unicode_buffer(buf_len)
        k32.GetShortPathNameW(str(path), buf, buf_len)
        return buf.value
    except Exception:
        return str(path)

_SETTINGS_FILE = BIN_DIR / "mp4tom3u8.ini"

MP42HLS = ROOT / "assets" / "mp42hls.exe"

# Column indices
COL_INDEX = 0
COL_NAME = 1
COL_PATH = 2
COL_STATUS = 3
COL_OUTPUT = 4
COL_ACTION = 5


# ============================================================================
# WORKER
# ============================================================================


class ConvertWorker(QThread):
    """Chạy mp42hls.exe trong thread riêng để convert 1 file MP4 → M3U8."""

    log = Signal(str)
    status_changed = Signal(int, str)   # row, status text
    output_changed = Signal(int, str)   # row, output dir path
    finished = Signal(int, bool)        # row, success

    def __init__(
        self,
        row: int,
        mp4_path: Path,
        output_base: Path,
        segment_duration: int,
        parent=None,
    ):
        super().__init__(parent)
        self.row = row
        self.mp4_path = mp4_path
        self.output_base = output_base
        self.segment_duration = segment_duration
        self._proc: subprocess.Popen | None = None
        self._stopped = False

    def run(self):
        try:
            base_name = self.mp4_path.stem
            output_dir = self.output_base / base_name
            if output_dir.exists():
                counter = 1
                while (self.output_base / f"{base_name} ({counter})").exists():
                    counter += 1
                output_dir = self.output_base / f"{base_name} ({counter})"
            output_dir.mkdir(parents=True, exist_ok=True)

            self.output_changed.emit(self.row, str(output_dir))
            self.status_changed.emit(self.row, "Đang convert...")
            self.log.emit(f"[{self.mp4_path.name}] → {output_dir}")

            cmd = [
                _short_path(MP42HLS),
                "--segment-duration", str(self.segment_duration),
                _short_path(self.mp4_path),
            ]

            self._proc = subprocess.Popen(
                cmd,
                cwd=_short_path(output_dir),   # mp42hls xuất file vào cwd
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            for line in self._proc.stdout:
                if self._stopped:
                    break
                stripped = line.strip()
                if stripped:
                    self.log.emit(f"  {stripped}")

            self._proc.wait()

            if self._stopped:
                self.status_changed.emit(self.row, "Đã dừng")
                self.finished.emit(self.row, False)
            elif self._proc.returncode == 0:
                self.status_changed.emit(self.row, "✓ Xong")
                self.log.emit(f"[{self.mp4_path.name}] Hoàn tất!")
                self.finished.emit(self.row, True)
            else:
                self.status_changed.emit(self.row, f"✗ Lỗi ({self._proc.returncode})")
                self.log.emit(f"[{self.mp4_path.name}] Lỗi! exit={self._proc.returncode}")
                self.finished.emit(self.row, False)

        except Exception as exc:
            self.status_changed.emit(self.row, "✗ Lỗi")
            self.log.emit(f"[{self.mp4_path.name}] Exception: {exc}")
            self.finished.emit(self.row, False)

    def stop(self):
        self._stopped = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


# ============================================================================
# TAB
# ============================================================================


class Mp4ToM3U8Tab(QtWidgets.QWidget):
    """Tab hàng đợi convert MP4 → M3U8 (HLS) dùng mp42hls.exe."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mp4_paths: list[Path] = []
        self._workers: list[ConvertWorker] = []
        self._pending_rows: list[int] = []
        self._completed_count = 0
        self._success_count = 0
        self._is_running = False
        self._stop_requested = False
        self._reset_after_stop = False
        self._preview_btns: dict[Path, QtWidgets.QPushButton] = {}
        self._play_btns: dict[Path, QtWidgets.QPushButton] = {}
        self._output_dirs: dict[Path, Path] = {}
        self._build_ui()
        self._load_settings()

    # -------------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------------

    def _settings(self) -> QSettings:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        return QSettings(str(_SETTINGS_FILE), QSettings.IniFormat)

    def _load_settings(self):
        s = self._settings()
        default = r"C:\Users\Pc\Downloads\M3u8Output"
        self.output_dir_edit.setText(s.value("output_dir", default))
        try:
            segment_duration = int(s.value("segment_duration", 6))
        except (TypeError, ValueError):
            segment_duration = 6
        self.segment_spin.setValue(segment_duration)

    def _save_settings(self):
        s = self._settings()
        s.setValue("output_dir", self.output_dir_edit.text())
        s.setValue("segment_duration", self.segment_spin.value())
        s.sync()

    # -------------------------------------------------------------------------
    # Build UI
    # -------------------------------------------------------------------------

    def _build_ui(self):
        main = QtWidgets.QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(6)

        # ── Config ──────────────────────────────────────────────────────────
        cfg_box = QtWidgets.QGroupBox("Cấu hình")
        cfg_form = QtWidgets.QFormLayout(cfg_box)
        cfg_form.setContentsMargins(10, 14, 10, 10)
        cfg_form.setSpacing(8)

        # Output directory
        dir_row = QtWidgets.QHBoxLayout()
        self.output_dir_edit = QtWidgets.QLineEdit()
        self.output_dir_edit.setPlaceholderText("Chọn thư mục lưu kết quả M3U8...")
        dir_browse = QtWidgets.QPushButton("Browse…")
        dir_browse.setFixedWidth(80)
        dir_browse.clicked.connect(self._browse_output_dir)
        self.output_dir_edit.textChanged.connect(lambda: self._save_settings())
        dir_row.addWidget(self.output_dir_edit)
        dir_row.addWidget(dir_browse)
        cfg_form.addRow("Thư mục lưu mặc định:", dir_row)

        # Segment duration
        seg_row = QtWidgets.QHBoxLayout()
        self.segment_spin = QtWidgets.QSpinBox()
        self.segment_spin.setRange(1, 60)
        self.segment_spin.setValue(6)
        self.segment_spin.setSuffix(" giây")
        self.segment_spin.setFixedWidth(100)
        self.segment_spin.valueChanged.connect(lambda: self._save_settings())
        seg_row.addWidget(self.segment_spin)
        seg_row.addStretch()
        cfg_form.addRow("Thời gian mỗi segment:", seg_row)

        main.addWidget(cfg_box)

        # ── Toolbar ─────────────────────────────────────────────────────────
        toolbar = QtWidgets.QHBoxLayout()

        add_btn = QtWidgets.QPushButton("＋ Thêm file MP4")
        add_btn.setFixedHeight(30)
        add_btn.clicked.connect(self._add_mp4_files)
        toolbar.addWidget(add_btn)
        toolbar.addStretch()

        help_btn = QtWidgets.QPushButton("Hướng dẫn")
        help_btn.setFixedHeight(30)
        help_btn.setToolTip("Xem hướng dẫn sử dụng tab MP4 → M3U8")
        help_btn.clicked.connect(self._show_usage_help)
        toolbar.addWidget(help_btn)

        main.addLayout(toolbar)

        # ── Queue table ─────────────────────────────────────────────────────
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["#", "Tên file", "Đường dẫn", "Trạng thái", "Thư mục output", ""]
        )
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_INDEX, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_NAME, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_PATH, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(COL_STATUS, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_OUTPUT, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(COL_ACTION, QtWidgets.QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(200)
        main.addWidget(self.table, stretch=1)

        # ── Action buttons ───────────────────────────────────────────────────
        act_row = QtWidgets.QHBoxLayout()
        act_row.setSpacing(8)

        _btn_h = 38
        _btn_font = QtGui.QFont()
        _btn_font.setPointSize(10)
        _btn_font.setBold(True)

        self.convert_btn = QtWidgets.QPushButton("▶  Convert")
        self.convert_btn.setFixedHeight(_btn_h)
        self.convert_btn.setMinimumWidth(120)
        self.convert_btn.setFont(_btn_font)
        self.convert_btn.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.convert_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #43a047, stop:1 #2e7d32);
                color: white;
                border: none;
                border-radius: 6px;
                padding: 0 18px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #66bb6a, stop:1 #388e3c);
            }
            QPushButton:pressed { background: #1b5e20; }
            QPushButton:disabled {
                background: #b0bec5; color: #eceff1;
            }
        """)
        self.convert_btn.clicked.connect(self._on_convert)

        self.stop_btn = QtWidgets.QPushButton("⏹  Dừng")
        self.stop_btn.setFixedHeight(_btn_h)
        self.stop_btn.setMinimumWidth(110)
        self.stop_btn.setFont(_btn_font)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #ef5350, stop:1 #c62828);
                color: white;
                border: none;
                border-radius: 6px;
                padding: 0 18px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #ff7043, stop:1 #e53935);
            }
            QPushButton:pressed { background: #b71c1c; }
            QPushButton:disabled {
                background: #b0bec5; color: #eceff1;
            }
        """)
        self.stop_btn.clicked.connect(self._on_stop)

        self.reset_btn = QtWidgets.QPushButton("↺  Reset")
        self.reset_btn.setFixedHeight(_btn_h)
        self.reset_btn.setMinimumWidth(100)
        self.reset_btn.setFont(_btn_font)
        self.reset_btn.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.reset_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #78909c, stop:1 #546e7a);
                color: white;
                border: none;
                border-radius: 6px;
                padding: 0 18px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #90a4ae, stop:1 #607d8b);
            }
            QPushButton:pressed { background: #37474f; }
        """)
        self.reset_btn.clicked.connect(self._on_reset)

        act_row.addWidget(self.convert_btn)
        act_row.addWidget(self.stop_btn)
        act_row.addStretch()
        act_row.addWidget(self.reset_btn)
        main.addLayout(act_row)

        # ── Progress bar ─────────────────────────────────────────────────────
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        main.addWidget(self.progress_bar)

        # ── Log ──────────────────────────────────────────────────────────────
        log_box = QtWidgets.QGroupBox("Log")
        log_vbox = QtWidgets.QVBoxLayout(log_box)
        log_vbox.setContentsMargins(4, 8, 4, 4)
        self.log_text = QtWidgets.QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(160)
        self.log_text.setFont(QtGui.QFont("Consolas", 9))
        log_vbox.addWidget(self.log_text)
        main.addWidget(log_box)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _log(self, msg: str):
        self.log_text.append(msg)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _set_row_status(self, row: int, text: str, color: str | None = None):
        item = self.table.item(row, COL_STATUS)
        if item:
            item.setText(text)
            if color:
                item.setForeground(QtGui.QColor(color))

    def _show_usage_help(self):
        settings_path = str(_SETTINGS_FILE)
        help_text = f"""
        <h3>Hướng dẫn MP4 → M3U8</h3>
        <ol>
            <li>Chọn <b>Thư mục lưu mặc định</b> để lưu kết quả HLS.</li>
            <li>Chọn <b>Thời gian mỗi segment</b>, thường dùng 4-10 giây.</li>
            <li>Bấm <b>Thêm file MP4</b> và chọn một hoặc nhiều file.</li>
            <li>Bấm <b>Convert</b> để chuyển lần lượt từng file sang M3U8.</li>
            <li>Sau khi xong, dùng <b>Play</b> để mở stream.m3u8 hoặc <b>Xem</b> để mở thư mục output.</li>
            <li>Nếu muốn chạy lượt convert mới và bảng còn dữ liệu cũ, bấm <b>Reset</b> trước rồi thêm file lại.</li>
        </ol>
        <p><b>Kết quả:</b> mỗi file sẽ tạo một thư mục riêng dạng <code>Tên file m3u8</code>.</p>
        """
        QtWidgets.QMessageBox.information(
            self,
            "Hướng dẫn sử dụng",
            help_text.strip(),
        )

    # -------------------------------------------------------------------------
    # File picker
    # -------------------------------------------------------------------------

    def _browse_output_dir(self):
        current = self.output_dir_edit.text() or str(Path.home())
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Chọn thư mục lưu M3U8", current
        )
        if d:
            self.output_dir_edit.setText(d)

    def _add_mp4_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Chọn file MP4",
            str(Path.home()),
            "MP4 Files (*.mp4 *.MP4);;All Files (*)",
        )
        for f in files:
            p = Path(f)
            if p not in self._mp4_paths:
                self._mp4_paths.append(p)
                self._append_table_row(p)

    def _append_table_row(self, path: Path):
        row = self.table.rowCount()
        self.table.insertRow(row)

        idx_item = QtWidgets.QTableWidgetItem(str(row + 1))
        idx_item.setTextAlignment(QtCore.Qt.AlignCenter)
        self.table.setItem(row, COL_INDEX, idx_item)
        self.table.setItem(row, COL_NAME, QtWidgets.QTableWidgetItem(path.name))
        self.table.setItem(row, COL_PATH, QtWidgets.QTableWidgetItem(str(path)))
        self.table.setItem(row, COL_STATUS, QtWidgets.QTableWidgetItem("Chờ"))
        self.table.setItem(row, COL_OUTPUT, QtWidgets.QTableWidgetItem(""))

        # Container widget gồm 2 nút: Preview + Xóa
        action_widget = QtWidgets.QWidget()
        action_layout = QtWidgets.QHBoxLayout(action_widget)
        action_layout.setContentsMargins(2, 1, 2, 1)
        action_layout.setSpacing(4)

        play_btn = QtWidgets.QPushButton("▶ Play")
        play_btn.setFixedHeight(24)
        play_btn.setEnabled(False)
        play_btn.setToolTip("Phát file stream.m3u8 bằng trình phát mặc định")
        play_btn.setStyleSheet(
            "QPushButton{color:#e65100;font-weight:bold;}"
            "QPushButton:disabled{color:#aaa;}"
        )
        play_btn.clicked.connect(lambda _checked, p=path: self._play_m3u8(p))
        self._play_btns[path] = play_btn

        preview_btn = QtWidgets.QPushButton("📂 Xem")
        preview_btn.setFixedHeight(24)
        preview_btn.setEnabled(False)
        preview_btn.setToolTip("Mở thư mục chứa file M3U8")
        preview_btn.setStyleSheet(
            "QPushButton{color:#1565c0;}"
            "QPushButton:disabled{color:#aaa;}"
        )
        preview_btn.clicked.connect(lambda _checked, p=path: self._open_output_folder(p))
        self._preview_btns[path] = preview_btn

        del_btn = QtWidgets.QPushButton("Xóa")
        del_btn.setFixedHeight(24)
        del_btn.clicked.connect(lambda _checked, p=path: self._remove_by_path(p))

        action_layout.addWidget(play_btn)
        action_layout.addWidget(preview_btn)
        action_layout.addWidget(del_btn)
        self.table.setCellWidget(row, COL_ACTION, action_widget)

    def _remove_by_path(self, path: Path):
        if self._is_running:
            return
        try:
            idx = self._mp4_paths.index(path)
        except ValueError:
            return
        self._mp4_paths.pop(idx)
        self._preview_btns.pop(path, None)
        self._play_btns.pop(path, None)
        self._output_dirs.pop(path, None)
        self.table.removeRow(idx)
        # Re-number
        for i in range(self.table.rowCount()):
            item = self.table.item(i, COL_INDEX)
            if item:
                item.setText(str(i + 1))

    # -------------------------------------------------------------------------
    # Convert logic
    # -------------------------------------------------------------------------

    def _on_convert(self):
        if any(w.isRunning() for w in self._workers):
            QtWidgets.QMessageBox.warning(
                self,
                "Cảnh báo",
                "Đang dừng worker cũ, vui lòng chờ một chút.",
            )
            return

        if self.table.rowCount() == 0:
            QtWidgets.QMessageBox.warning(
                self, "Cảnh báo", "Chưa có file MP4 nào trong hàng đợi!"
            )
            return

        output_base_str = self.output_dir_edit.text().strip()
        if not output_base_str:
            QtWidgets.QMessageBox.warning(
                self, "Cảnh báo", "Vui lòng chọn thư mục lưu!"
            )
            return

        if not MP42HLS.exists():
            QtWidgets.QMessageBox.critical(
                self,
                "Lỗi",
                f"Không tìm thấy mp42hls.exe tại:\n{MP42HLS}\n\n"
                "Hãy đặt file mp42hls.exe vào thư mục assets/.",
            )
            return

        self._save_settings()
        self._is_running = True
        self._stop_requested = False
        self._reset_after_stop = False
        self._completed_count = 0
        self._success_count = 0
        self._workers.clear()
        self.convert_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setRange(0, self.table.rowCount())
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self._pending_rows = list(range(len(self._mp4_paths)))
        self._log(f"=== Bắt đầu convert {len(self._mp4_paths)} file(s) ===")
        self._run_next()

    def _run_next(self):
        if not self._pending_rows or not self._is_running:
            self._finish_all()
            return

        row = self._pending_rows.pop(0)
        mp4_path = self._mp4_paths[row]
        output_base = Path(self.output_dir_edit.text().strip())

        worker = ConvertWorker(
            row, mp4_path, output_base, self.segment_spin.value(), self
        )
        worker.log.connect(self._log)
        worker.status_changed.connect(self._on_status_changed)
        worker.output_changed.connect(self._on_output_changed)
        worker.finished.connect(self._on_worker_finished)
        self._workers.append(worker)
        worker.start()

    def _on_status_changed(self, row: int, text: str):
        item = self.table.item(row, COL_STATUS)
        if not item:
            return
        item.setText(text)
        if "Xong" in text:
            item.setForeground(QtGui.QColor("#2e7d32"))
        elif "Lỗi" in text or "dừng" in text.lower():
            item.setForeground(QtGui.QColor("#c62828"))
        elif "Đang" in text:
            item.setForeground(QtGui.QColor("#1565c0"))

    def _on_output_changed(self, row: int, output_dir: str):
        item = self.table.item(row, COL_OUTPUT)
        if item:
            item.setText(output_dir)
        if row < len(self._mp4_paths):
            self._output_dirs[self._mp4_paths[row]] = Path(output_dir)

    def _on_worker_finished(self, row: int, success: bool):
        worker = self.sender()
        if isinstance(worker, ConvertWorker) and worker in self._workers:
            self._workers.remove(worker)
            worker.deleteLater()

        self._completed_count += 1
        if success:
            self._success_count += 1
        self.progress_bar.setValue(self._completed_count)

        if success and row < len(self._mp4_paths):
            path = self._mp4_paths[row]
            for btn in (self._preview_btns.get(path), self._play_btns.get(path)):
                if btn:
                    btn.setEnabled(True)

        if self._reset_after_stop and not any(w.isRunning() for w in self._workers):
            self._clear_all()
            return

        if self._is_running:
            self._run_next()
        elif not any(w.isRunning() for w in self._workers):
            self._finish_all()

    def _play_m3u8(self, mp4_path: Path):
        out_dir = self._output_dirs.get(mp4_path)
        if not out_dir:
            return
        m3u8_file = out_dir / "stream.m3u8"
        if m3u8_file.exists():
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(m3u8_file))
            )
        else:
            QtWidgets.QMessageBox.warning(
                self, "Không tìm thấy", f"File không tồn tại:\n{m3u8_file}"
            )

    def _open_output_folder(self, mp4_path: Path):
        out_dir = self._output_dirs.get(mp4_path)
        if out_dir and out_dir.exists():
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(out_dir))
            )

    def _finish_all(self):
        if any(w.isRunning() for w in self._workers):
            return

        self._is_running = False
        self.convert_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        total = self.table.rowCount()
        if self._stop_requested:
            self._log(
                f"=== Đã dừng. Thành công {self._success_count}/{total} file ==="
            )
        else:
            self._log(
                f"=== Hoàn tất! Thành công {self._success_count}/{total} file ==="
            )
        self._stop_requested = False

    def _on_stop(self):
        if not self._is_running and not any(w.isRunning() for w in self._workers):
            return

        self._is_running = False
        self._stop_requested = True
        self._pending_rows.clear()
        for w in self._workers:
            if w.isRunning():
                w.stop()
        self._log("=== Đang dừng... ===")
        self.stop_btn.setEnabled(False)
        self.convert_btn.setEnabled(False)

    def _on_reset(self):
        if self._is_running or any(w.isRunning() for w in self._workers):
            self._reset_after_stop = True
            self._on_stop()
            return
        self._clear_all()

    def _clear_all(self):
        self._is_running = False
        self._stop_requested = False
        self._reset_after_stop = False
        self._mp4_paths.clear()
        self._workers.clear()
        self._pending_rows.clear()
        self._preview_btns.clear()
        self._play_btns.clear()
        self._output_dirs.clear()
        self._completed_count = 0
        self._success_count = 0
        self.table.setRowCount(0)
        self.log_text.clear()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.convert_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._log("=== Đã reset bảng ===")
