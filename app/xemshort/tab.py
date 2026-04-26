"""
XemShort tab: full UI widget extracted from app.py/MainWindow.

Self-contained QWidget that owns all controls, table, log, and handlers.
MainWindow just does: self.tabs.addTab(XemShortTab(), "XemShort").
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings

from .cache import _NS_FETCH_CACHE_TTL, _ns_cache_clear, _ns_cache_evict_expired
from .dialogs import (
    XSDetailDialog,
    XSEpisodePickerDialog,
    XSPasteJsonDialog,
    XSVideoPopup,
    _NSPhoneMockup,
)
from .helpers import _COLOR_TO_HEX, _ns_check_ffmpeg, _ns_load_bundled_fonts, _ns_color_to_ass
from .models import XSMovie, XSEpisode
from .workers import XSDownloadMergeWorker, XSFetchWorker


# ── Constants ─────────────────────────────────────────────────────────────────

_XS_APP_NAME   = "XemShort GUI"
_XS_CONFIG_KEY = "XemShort"
DEFAULT_API_URL = "https://api.xemshort.top/allepisode?shortPlayId={movie_id}"


# ── Subtitle preview helpers (stay here to keep tab self-contained) ───────────

# ── XemShortTab ───────────────────────────────────────────────────────────────


class XemShortTab(QtWidgets.QWidget):
    """Self-contained tab widget for the XemShort downloader mode."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.movies: list[XSMovie] = []
        self.nsworker: XSDownloadMergeWorker | None = None
        self._ns_iterator = None
        self._build_ui()

    # ── Settings ────────────────────────────────────────────────────────────

    def settings(self) -> QSettings:
        return QSettings(_XS_APP_NAME, _XS_CONFIG_KEY)

    def _load_settings(self):
        s = self.settings()
        self.ns_save_dir_edit.setText(
            s.value("save_dir", str(Path.home() / "Downloads" / "XemShort")))
        self.ns_api_url_edit.setText(
            s.value("api_url", DEFAULT_API_URL))
        self.ns_concurrency_spin.setValue(int(s.value("concurrency", 4)))
        self.ns_sub_checkbox.setChecked(s.value("download_sub", True, type=bool))
        self.ns_merge_checkbox.setChecked(s.value("do_merge", True, type=bool))
        self.ns_crf_spin.setValue(int(s.value("crf", 22)))
        self.ns_sub_font_combo.setCurrentText(s.value("sub_font", "UTM Alter Gothic"))
        self.ns_sub_size_spin.setValue(int(s.value("sub_size", 20)))
        self.ns_sub_margin_v_spin.setValue(int(s.value("sub_margin_v", 30)))
        self.ns_sub_color_combo.setCurrentText(s.value("sub_color", "Trắng"))
        self.ns_sub_bold_cb.setChecked(s.value("sub_bold", True, type=bool))
        self.ns_sub_italic_cb.setChecked(s.value("sub_italic", False, type=bool))

    def _save_settings(self):
        s = self.settings()
        s.setValue("save_dir", self.ns_save_dir_edit.text())
        s.setValue("api_url", self.ns_api_url_edit.text())
        s.setValue("concurrency", self.ns_concurrency_spin.value())
        s.setValue("download_sub", self.ns_sub_checkbox.isChecked())
        s.setValue("do_merge", self.ns_merge_checkbox.isChecked())
        s.setValue("crf", self.ns_crf_spin.value())
        s.setValue("sub_font", self.ns_sub_font_combo.currentText())
        s.setValue("sub_size", self.ns_sub_size_spin.value())
        s.setValue("sub_margin_v", self.ns_sub_margin_v_spin.value())
        s.setValue("sub_color", self.ns_sub_color_combo.currentText())
        s.setValue("sub_bold", self.ns_sub_bold_cb.isChecked())
        s.setValue("sub_italic", self.ns_sub_italic_cb.isChecked())

    # ── UI builder ──────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # ── Config ──────────────────────────────────────────────────────────
        cfg = QtWidgets.QGroupBox("Cấu hình")
        cfg_layout = QtWidgets.QFormLayout(cfg)

        save_row = QtWidgets.QHBoxLayout()
        self.ns_save_dir_edit = QtWidgets.QLineEdit()
        self.ns_save_dir_edit.setPlaceholderText("Chọn thư mục lưu...")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.setStyleSheet(
            "QPushButton { background-color: #374151; color: #d1d5db; padding: 4px 10px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #4b5563; }")
        browse_btn.clicked.connect(self._ns_browse_save_dir)
        save_row.addWidget(self.ns_save_dir_edit)
        save_row.addWidget(browse_btn)
        cfg_layout.addRow("Thư mục lưu:", save_row)

        self.ns_api_url_edit = QtWidgets.QLineEdit(DEFAULT_API_URL)
        self.ns_api_url_edit.setPlaceholderText(
            "https://api.xemshort.top/allepisode?shortPlayId={movie_id}")
        cfg_layout.addRow("API endpoint:", self.ns_api_url_edit)

        opts = QtWidgets.QHBoxLayout()
        opts.addWidget(QtWidgets.QLabel("Luồng:"))
        self.ns_concurrency_spin = QtWidgets.QSpinBox()
        self.ns_concurrency_spin.setRange(1, 16)
        self.ns_concurrency_spin.setValue(4)
        opts.addWidget(self.ns_concurrency_spin)
        opts.addSpacing(10)
        self.ns_sub_checkbox = QtWidgets.QCheckBox("Tải phụ đề")
        self.ns_sub_checkbox.setChecked(True)
        opts.addWidget(self.ns_sub_checkbox)
        self.ns_merge_checkbox = QtWidgets.QCheckBox("Hardcode sub (merge)")
        self.ns_merge_checkbox.setChecked(True)
        opts.addWidget(self.ns_merge_checkbox)
        opts.addSpacing(10)
        opts.addWidget(QtWidgets.QLabel("CRF:"))
        self.ns_crf_spin = QtWidgets.QSpinBox()
        self.ns_crf_spin.setRange(18, 28)
        self.ns_crf_spin.setValue(22)
        self.ns_crf_spin.setToolTip("CRF: 18=chất lượng cao, 28=nhỏ hơn")
        opts.addWidget(self.ns_crf_spin)
        opts.addStretch()
        cfg_layout.addRow("Tùy chọn:", opts)

        # ── Sub style row ───────────────────────────────────────────────────
        sub_style_row = QtWidgets.QHBoxLayout()
        sub_style_row.addWidget(QtWidgets.QLabel("Font:"))
        self.ns_sub_font_combo = QtWidgets.QComboBox()
        self.ns_sub_font_combo.setEditable(True)
        _fonts_dir = Path(__file__).parent.parent / "fonts"
        _bundled = _ns_load_bundled_fonts(_fonts_dir) if _fonts_dir.exists() else []
        for _f in _bundled:
            self.ns_sub_font_combo.addItem(_f)
        self.ns_sub_font_combo.setCurrentText(_bundled[0] if _bundled else "Arial")
        self.ns_sub_font_combo.setMinimumWidth(180)
        self.ns_sub_font_combo.setToolTip(
            "Font chữ cho phụ đề.\n"
            "Font trong thư mục fonts/ sẽ tự động cài khi merge.\n"
            "Có thể gõ tên font bất kỳ hoặc chọn từ danh sách.")
        sub_style_row.addWidget(self.ns_sub_font_combo)
        sub_style_row.addSpacing(12)
        sub_style_row.addWidget(QtWidgets.QLabel("Size:"))
        self.ns_sub_size_spin = QtWidgets.QSpinBox()
        self.ns_sub_size_spin.setRange(12, 80)
        self.ns_sub_size_spin.setValue(20)
        sub_style_row.addWidget(self.ns_sub_size_spin)
        sub_style_row.addSpacing(12)
        sub_style_row.addWidget(QtWidgets.QLabel("MarginV:"))
        self.ns_sub_margin_v_spin = QtWidgets.QSpinBox()
        self.ns_sub_margin_v_spin.setRange(0, 300)
        self.ns_sub_margin_v_spin.setValue(30)
        self.ns_sub_margin_v_spin.setToolTip(
            "Vị trí sub theo chiều dọc (MarginV).\n"
            "0 = sát mép dưới, tăng để đẩy sub lên cao hơn.\n"
            "Mặc định: 30")
        sub_style_row.addWidget(self.ns_sub_margin_v_spin)
        sub_style_row.addSpacing(12)
        sub_style_row.addWidget(QtWidgets.QLabel("Màu:"))
        self.ns_sub_color_combo = QtWidgets.QComboBox()
        self.ns_sub_color_combo.setEditable(True)
        self.ns_sub_color_combo.setMinimumWidth(90)
        _color_presets = [
            ("Trắng", "#FFFFFF"), ("Vàng", "#FFD700"), ("Xanh dương", "#00BFFF"),
            ("Đỏ", "#FF6B6B"), ("Xanh lá", "#00FF7F"), ("Cam", "#FFA500"),
            ("Hồng", "#FF69B4"), ("Tím", "#DA70D6"), ("Lục", "#90EE90"),
            ("Xám sáng", "#D3D3D3"),
        ]
        for _lbl, _hex in _color_presets:
            self.ns_sub_color_combo.addItem(_lbl, _hex)
        self.ns_sub_color_combo.setCurrentIndex(0)  # default: Trắng
        self.ns_sub_color_combo.setToolTip(
            "Màu chữ phụ đề.\nCó thể gõ mã hex bất kỳ (VD: #FF0000)")
        sub_style_row.addWidget(self.ns_sub_color_combo)
        sub_style_row.addSpacing(8)
        sub_style_row.addWidget(QtWidgets.QLabel("Nền:"))
        sub_style_row.addSpacing(8)
        self.ns_sub_bold_cb = QtWidgets.QCheckBox("Bold")
        self.ns_sub_bold_cb.setChecked(True)
        self.ns_sub_bold_cb.setToolTip("In đậm phụ đề")
        sub_style_row.addWidget(self.ns_sub_bold_cb)
        self.ns_sub_italic_cb = QtWidgets.QCheckBox("Italic")
        self.ns_sub_italic_cb.setChecked(False)
        self.ns_sub_italic_cb.setToolTip("In nghiêng phụ đề")
        sub_style_row.addWidget(self.ns_sub_italic_cb)
        sub_style_row.addStretch()
        cfg_layout.addRow("Sub style:", sub_style_row)

        # ── Preview row ─────────────────────────────────────────────────────
        preview_row = QtWidgets.QHBoxLayout()
        preview_row.addWidget(QtWidgets.QLabel("Xem trước:"))
        _btn_style = (
            "QPushButton { background-color: #5b21b6; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #4c1d95; }")
        btn_full = QtWidgets.QPushButton("Full màn hình phone")
        btn_full.setStyleSheet(_btn_style)
        btn_full.clicked.connect(self._ns_preview_full)
        btn_full.setToolTip("Video 9:16 lấp đầy màn hình điện thoại")
        preview_row.addWidget(btn_full)
        btn_169 = QtWidgets.QPushButton("Video 16:9 trên phone")
        btn_169.setStyleSheet(_btn_style)
        btn_169.clicked.connect(self._ns_preview_169)
        btn_169.setToolTip("Video 16:9 nằm giữa màn hình điện thoại, đen trên/dưới")
        preview_row.addWidget(btn_169)
        preview_row.addStretch()
        cfg_layout.addRow("", preview_row)
        layout.addWidget(cfg)

        # ── Input group ──────────────────────────────────────────────────────
        inp = QtWidgets.QGroupBox("Thêm phim")
        inp_layout = QtWidgets.QHBoxLayout(inp)
        inp_layout.addWidget(QtWidgets.QLabel("Movie ID:"))
        self.ns_movie_id_edit = QtWidgets.QLineEdit()
        self.ns_movie_id_edit.setPlaceholderText("VD: 2041732413888921612")
        inp_layout.addWidget(self.ns_movie_id_edit, stretch=1)

        self.ns_fetch_btn = QtWidgets.QPushButton("Fetch Data")
        self.ns_fetch_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; padding: 5px 14px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1d4ed8; }"
            "QPushButton:disabled { background-color: #93c5fd; color: #fff; }")
        self.ns_fetch_btn.clicked.connect(self._ns_on_fetch)
        inp_layout.addWidget(self.ns_fetch_btn)

        self.ns_clear_cache_btn = QtWidgets.QPushButton("Xóa cache")
        self.ns_clear_cache_btn.setStyleSheet(
            "QPushButton { background-color: #374151; color: #9ca3af; padding: 5px 10px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #7f1d1d; color: #fca5a5; }")
        self.ns_clear_cache_btn.setToolTip(
            f"Xóa cache fetch. Cache tự hết hạn sau {_NS_FETCH_CACHE_TTL // 60} phút.")
        self.ns_clear_cache_btn.clicked.connect(self._ns_on_clear_cache)
        inp_layout.addWidget(self.ns_clear_cache_btn)

        for _lbl, _meth in [
            ("Paste JSON", self._ns_on_paste_json),
            ("Load JSON", self._ns_on_load_json),
        ]:
            btn = QtWidgets.QPushButton(_lbl)
            btn.setStyleSheet(
                "QPushButton { background-color: #6b7280; color: white; padding: 5px 12px; "
                "border-radius: 4px; }"
                "QPushButton:hover { background-color: #4b5563; }")
            btn.clicked.connect(_meth)
            inp_layout.addWidget(btn)

        layout.addWidget(inp)

        # ── Table + Log splitter ─────────────────────────────────────────────
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # Table section
        tbl_w = QtWidgets.QWidget()
        tbl_lay = QtWidgets.QVBoxLayout(tbl_w)
        tbl_lay.setContentsMargins(0, 0, 0, 0)
        hdr = QtWidgets.QHBoxLayout()
        hdr.addWidget(QtWidgets.QLabel("<b>Danh sách phim đã thêm</b>"))
        hdr.addStretch()
        self.ns_start_btn = QtWidgets.QPushButton("Start Download & Merge")
        self.ns_start_btn.setEnabled(False)
        self.ns_start_btn.setStyleSheet(
            "QPushButton { background-color: #22c55e; color: white; font-weight: bold; "
            "padding: 6px 12px; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #9ca3af; }")
        self.ns_start_btn.clicked.connect(self._ns_on_start)
        hdr.addWidget(self.ns_start_btn)
        self.ns_stop_btn = QtWidgets.QPushButton("Stop")
        self.ns_stop_btn.setEnabled(False)
        self.ns_stop_btn.setStyleSheet(
            "QPushButton { background-color: #ef4444; color: white; font-weight: bold; "
            "padding: 6px 12px; border-radius: 4px; }"
            "QPushButton:disabled { background-color: #9ca3af; }")
        self.ns_stop_btn.clicked.connect(self._ns_on_stop)
        hdr.addWidget(self.ns_stop_btn)
        tbl_lay.addLayout(hdr)

        self.ns_table = QtWidgets.QTableWidget(0, 7)
        self.ns_table.setHorizontalHeaderLabels(
            ["Tên phim", "Tập", "Chọn", "Trạng thái", "Kết quả", "Time", "Actions"])
        self.ns_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch)
        self.ns_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents)
        self.ns_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeToContents)
        self.ns_table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeToContents)
        self.ns_table.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.Stretch)
        self.ns_table.horizontalHeader().setSectionResizeMode(
            5, QtWidgets.QHeaderView.ResizeToContents)
        self.ns_table.horizontalHeader().setSectionResizeMode(
            6, QtWidgets.QHeaderView.ResizeToContents)
        self.ns_table.verticalHeader().setVisible(False)
        self.ns_table.setWordWrap(True)
        tbl_lay.addWidget(self.ns_table)
        splitter.addWidget(tbl_w)

        # Log section
        log_w = QtWidgets.QWidget()
        log_lay = QtWidgets.QVBoxLayout(log_w)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.addWidget(QtWidgets.QLabel("<b>Log</b>"))
        self.ns_log_text = QtWidgets.QTextEdit()
        self.ns_log_text.setReadOnly(True)
        self.ns_log_text.setFont(QtGui.QFont("Consolas", 9))
        self.ns_log_text.setStyleSheet("background:#1a1a2e;color:#00ff00;")
        log_lay.addWidget(self.ns_log_text)
        splitter.addWidget(log_w)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

        # ── Progress + status bar ───────────────────────────────────────────
        self.ns_progress_bar = QtWidgets.QProgressBar()
        self.ns_progress_bar.setTextVisible(True)
        layout.addWidget(self.ns_progress_bar)
        self.ns_status = QtWidgets.QLabel("Sẵn sàng.")
        self.ns_status.setStyleSheet("color: #888; font-size: 11px; padding-left: 4px;")
        layout.addWidget(self.ns_status)

        # ── Finalise ─────────────────────────────────────────────────────────
        self._load_settings()
        self._check_ffmpeg()

    def _check_ffmpeg(self):
        if not _ns_check_ffmpeg():
            QtWidgets.QMessageBox.warning(
                self, "ffmpeg",
                "Không tìm thấy ffmpeg trong PATH.\n"
                "Chức năng merge sẽ bị disable.\n"
                "Tải tại https://ffmpeg.org/download.html.")
            self.ns_merge_checkbox.setChecked(False)
            self.ns_merge_checkbox.setEnabled(False)

    # ── Log ─────────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        self.ns_log_text.append(msg)
        self.ns_log_text.verticalScrollBar().setValue(
            self.ns_log_text.verticalScrollBar().maximum())

    # ── Fetch ───────────────────────────────────────────────────────────────

    def _ns_on_fetch(self):
        movie_id = self.ns_movie_id_edit.text().strip()
        if not movie_id:
            QtWidgets.QMessageBox.warning(self, "Thiếu input",
                                          "Vui lòng nhập Movie ID.")
            return
        api_url = self.ns_api_url_edit.text().strip()
        if not api_url.startswith(("http://", "https://")):
            QtWidgets.QMessageBox.warning(self, "API URL",
                                          "API URL phải bắt đầu bằng http:// hoặc https://.")
            return
        self.ns_fetch_btn.setEnabled(False)
        self.ns_status.setText(f"Đang fetch {movie_id}...")
        self._log(f"Fetching {movie_id}...")

        self.nsfetch_worker = XSFetchWorker(api_url, movie_id)
        self.nsfetch_worker.success.connect(self._ns_on_fetch_success)
        self.nsfetch_worker.cache_hit.connect(self._ns_on_fetch_cache_hit)
        self.nsfetch_worker.error.connect(self._ns_on_fetch_error)
        self.nsfetch_worker.finished.connect(
            lambda: self.ns_fetch_btn.setEnabled(True))
        self.nsfetch_worker.start()

    def _ns_on_fetch_success(self, episodes: list[XSEpisode], movie_name: str = ""):
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        self.ns_status.setText(f"Fetched {len(episodes)} tập.")
        self._log(f"Fetched {len(episodes)} tập.")
        self._ns_show_picker(episodes, name)

    def _ns_on_fetch_cache_hit(self, episodes: list[XSEpisode], movie_name: str = ""):
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        self.ns_status.setText(f"Cache hit — {len(episodes)} tập.")
        self._log(f"[cache] {len(episodes)} tập (cache hit)")
        self._ns_show_picker(episodes, name)

    def _ns_on_fetch_error(self, msg: str):
        self.ns_status.setText("Fetch lỗi.")
        self._log(f"Lỗi: {msg}")
        QtWidgets.QMessageBox.critical(self, "Fetch lỗi", msg)

    def _ns_on_clear_cache(self):
        count = _ns_cache_clear()
        self.ns_status.setText(f"Đã xóa cache ({count} mục).")
        self._log(f"[cache] Đã xóa {count} mục.")

    def _ns_on_paste_json(self):
        from .helpers import _ns_parse_episodes
        dlg = XSPasteJsonDialog(self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            data = dlg.get_json()
            if data is None:
                return
            try:
                movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
                episodes = _ns_parse_episodes(data, movie_name)
                if not episodes:
                    QtWidgets.QMessageBox.warning(self, "Rỗng", "JSON không chứa episode nào.")
                    return
                self._ns_show_picker(episodes, movie_name)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Parse lỗi", f"{type(e).__name__}: {e}")

    def _ns_on_load_json(self):
        from .helpers import _ns_parse_episodes
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Chọn file JSON", "", "JSON (*.json);;All (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            movie_name = data.get("shortPlayName", "") if isinstance(data, dict) else ""
            episodes = _ns_parse_episodes(data, movie_name)
            if not episodes:
                QtWidgets.QMessageBox.warning(self, "Rỗng", "File không có episode.")
                return
            self._ns_show_picker(episodes, movie_name)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Load lỗi", f"{type(e).__name__}: {e}")

    # ── Picker ─────────────────────────────────────────────────────────────

    def _ns_show_picker(self, episodes: list[XSEpisode], movie_name: str = ""):
        name = movie_name or (episodes[0].name if episodes else "Unknown")
        dlg = XSEpisodePickerDialog(name, episodes, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            selected = dlg.get_selected_episodes()
            save_dir = Path(self.ns_save_dir_edit.text() or ".")
            save_dir.mkdir(parents=True, exist_ok=True)
            movie = XSMovie(name=name, episodes=selected, save_dir=save_dir)
            self.movies.append(movie)
            self._ns_add_movie_to_table(movie)
            self.ns_start_btn.setEnabled(True)
            self._log(f"Thêm '{name}' - {movie.selected_count}/{movie.total} tập.")

    def _ns_browse_save_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Chọn thư mục lưu",
            self.ns_save_dir_edit.text() or str(Path.home()))
        if d:
            self.ns_save_dir_edit.setText(d)

    # ── Table management ────────────────────────────────────────────────────

    def _ns_add_movie_to_table(self, movie: XSMovie):
        row = self.ns_table.rowCount()
        self.ns_table.insertRow(row)
        self.ns_table.setItem(row, 0, QtWidgets.QTableWidgetItem(movie.name))
        self.ns_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(movie.total)))
        self.ns_table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(movie.selected_count)))
        self.ns_table.setItem(row, 3, QtWidgets.QTableWidgetItem("Ready"))
        self._ns_set_status(row, "Ready")
        self.ns_table.setItem(row, 4, QtWidgets.QTableWidgetItem("—"))
        self.ns_table.setItem(row, 5, QtWidgets.QTableWidgetItem("—"))

        btn_w = QtWidgets.QWidget()
        btn_l = QtWidgets.QHBoxLayout(btn_w)
        btn_l.setContentsMargins(2, 2, 2, 2)
        btn_l.setSpacing(4)

        def _btn(lbl, style, tip, slot):
            b = QtWidgets.QPushButton(lbl)
            b.setStyleSheet(style)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        _style = "QPushButton { background-color: #3b82f6; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; } QPushButton:hover { background-color: #2563eb; }"
        _style2 = "QPushButton { background-color: #8b5cf6; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; } QPushButton:hover { background-color: #7c3aed; }"
        _style3 = "QPushButton { background-color: #f59e0b; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; } QPushButton:hover { background-color: #d97706; }"
        _style4 = "QPushButton { background-color: #10b981; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; } QPushButton:hover { background-color: #059669; }"
        _style5 = "QPushButton { background-color: #ef4444; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; } QPushButton:hover { background-color: #dc2626; }"

        movie.open_btn    = _btn("Mở thư mục", _style,  "Mở thư mục chứa video",
                                 lambda *_: self._ns_open_movie_folder(movie))
        movie.openMerged_btn = _btn("Mở merged", _style2, "Mở thư mục merged/",
                                    lambda *_: self._ns_open_merged_folder(movie))
        movie.remerge_btn = _btn("Merge lại", _style3, "Xóa merged cũ và burn lại",
                                lambda *_: self._ns_remerge_movie(movie))
        movie.detail_btn  = _btn("Chi tiết",  _style4, "Xem chi tiết từng tập",
                                lambda *_: self._ns_show_detail(movie))
        movie.delete_btn  = _btn("Xóa",       _style5, "Xóa phim khỏi danh sách",
                                lambda *_: self._ns_remove_movie(movie))

        for _b in [movie.open_btn, movie.openMerged_btn, movie.remerge_btn,
                   movie.detail_btn, movie.delete_btn]:
            btn_l.addWidget(_b)

        self.ns_table.setCellWidget(row, 6, btn_w)
        self._ns_update_row_btns(movie)

    def _ns_set_status(self, row: int, text: str):
        item = self.ns_table.item(row, 3)
        if item is None:
            return
        item.setText(text)
        tl = text.lower()
        if tl.startswith("done"):
            bg, fg = QtGui.QColor("#d4edda"), QtGui.QColor("#155724")
        elif "error" in tl:
            bg, fg = QtGui.QColor("#f8d7da"), QtGui.QColor("#721c24")
        elif tl == "ready":
            bg, fg = QtGui.QColor("#fff3cd"), QtGui.QColor("#856404")
        else:
            bg, fg = QtGui.QColor("#d1ecf1"), QtGui.QColor("#0c5460")
        item.setBackground(QtGui.QBrush(bg))
        item.setForeground(QtGui.QBrush(fg))

    def _ns_update_row_btns(self, movie: XSMovie):
        running = bool(self.nsworker and self.nsworker.isRunning())
        has_done = any(e.selected and e.status == "done" for e in movie.episodes)
        if hasattr(movie, "remerge_btn"):
            movie.remerge_btn.setVisible(not running and has_done)
        if hasattr(movie, "delete_btn"):
            movie.delete_btn.setVisible(not running)
        if hasattr(movie, "openMerged_btn"):
            movie.openMerged_btn.setVisible(True)

    def _ns_remove_movie(self, movie: XSMovie):
        if movie in self.movies:
            idx = self.movies.index(movie)
            self.movies.remove(movie)
            self.ns_table.removeRow(idx)
            if not self.movies:
                self.ns_start_btn.setEnabled(False)

    def _ns_open_movie_folder(self, movie: XSMovie):
        folder = movie.save_dir / movie.folder_name
        folder.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    def _ns_open_merged_folder(self, movie: XSMovie):
        folder = movie.save_dir / movie.folder_name / "merged"
        folder.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    def _ns_show_detail(self, movie: XSMovie):
        dlg = XSDetailDialog(movie, self)
        dlg.table.cellDoubleClicked.connect(
            lambda row, col: self._ns_detail_cell_clicked(movie, row, col, dlg))
        dlg.exec()

    def _ns_detail_cell_clicked(self, movie: XSMovie, row: int, col: int,
                                dlg: XSDetailDialog):
        item = dlg.table.item(row, col)
        if item is None:
            return
        path_str = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not path_str:
            return
        path = Path(path_str)
        if not path.exists():
            return
        if col == 1 or col == 3:
            XSVideoPopup(path, dlg).exec()
        elif col == 2:
            from .dialogs import XSVttEditorDialog
            XSVttEditorDialog(path, dlg).exec()

    # ── Start / Stop / Run ─────────────────────────────────────────────────

    def _ns_on_start(self):
        pending = [
            m for m in self.movies
            if any(e.status in ("pending", "error") for e in m.episodes if e.selected)]
        if not pending:
            for m in self.movies:
                self._ns_update_row_btns(m)
            QtWidgets.QMessageBox.information(self, "Hoàn tất",
                                            "Không có phim nào cần tải.")
            return

        self.ns_start_btn.setEnabled(False)
        self.ns_stop_btn.setEnabled(True)
        self.ns_fetch_btn.setEnabled(False)
        self.ns_progress_bar.setValue(0)
        for m in self.movies:
            self._ns_update_row_btns(m)
        self._ns_run_next_movie(iter(pending))

    def _ns_run_next_movie(self, iterator):
        try:
            movie = next(iterator)
        except StopIteration:
            self._log("=== TẤT CẢ HOÀN TẤT ===")
            self.ns_status.setText("Hoàn tất.")
            self.ns_start_btn.setEnabled(True)
            self.ns_stop_btn.setEnabled(False)
            self.ns_fetch_btn.setEnabled(True)
            self.ns_progress_bar.setValue(100)
            QtWidgets.QMessageBox.information(self, "Hoàn tất",
                                             "Đã hoàn thành tất cả phim trong bảng.")
            return

        row = self.movies.index(movie)
        self._ns_set_status(row, "Running...")

        self.nsworker = XSDownloadMergeWorker(
            movie,
            concurrency=self.ns_concurrency_spin.value(),
            download_sub=self.ns_sub_checkbox.isChecked(),
            do_merge=self.ns_merge_checkbox.isChecked(),
            crf=self.ns_crf_spin.value(),
            preset="fast",
            sub_font=self.ns_sub_font_combo.currentText(),
            sub_size=self.ns_sub_size_spin.value(),
            sub_margin_v=self.ns_sub_margin_v_spin.value(),
            sub_color=self.ns_sub_color_combo.currentText(),
            sub_bold=self.ns_sub_bold_cb.isChecked(),
            sub_italic=self.ns_sub_italic_cb.isChecked(),
        )
        self.nsworker.log_msg.connect(self._log)
        self.nsworker.progress.connect(self._ns_on_progress)
        self.nsworker.episode_status.connect(
            lambda ep_num, st, m=movie, r=row: self._ns_on_episode_status(m, r, ep_num, st))
        self.nsworker.finished_all.connect(
            lambda m=movie: self._ns_on_movie_done(m))
        self._ns_iterator = iterator
        self.nsworker.start()

    def _ns_on_progress(self, done: int, total: int):
        pct = int(done / total * 100) if total else 0
        self.ns_progress_bar.setValue(pct)
        self.ns_progress_bar.setFormat(f"{done}/{total} ({pct}%)")

    def _ns_on_episode_status(self, movie: XSMovie, row: int, ep_num: int, status: str):
        done_count = sum(1 for e in movie.episodes if e.selected and e.status == "done")
        total_sel = movie.selected_count
        self._ns_set_status(row, f"{status} ({done_count}/{total_sel})")

    def _ns_on_stop(self):
        self._log("Đang dừng...")
        self._ns_iterator = None
        if self.nsworker and self.nsworker.isRunning():
            self.nsworker.stop()
        self.ns_start_btn.setEnabled(True)
        self.ns_stop_btn.setEnabled(False)
        self.ns_fetch_btn.setEnabled(True)

    def _ns_remerge_movie(self, movie: XSMovie):
        if self.nsworker and self.nsworker.isRunning():
            QtWidgets.QMessageBox.warning(self, "Đang chạy",
                                         "Vui lòng đợi tiến trình hiện tại hoàn tất.")
            return
        reset = 0
        for ep in movie.episodes:
            if ep.selected and ep.status == "done":
                if ep.merged_path and ep.merged_path.exists():
                    try:
                        ep.merged_path.unlink()
                    except Exception:
                        pass
                ep.merged_path = None
                ep.status = "pending"
                reset += 1
        if reset == 0:
            QtWidgets.QMessageBox.information(self, "Re-merge",
                                              "Không có tập nào trạng thái 'done' để re-merge.")
            return
        if movie in self.movies:
            row = self.movies.index(movie)
            self._ns_set_status(row, "Ready")
        self._log(f"Re-merge '{movie.name}': reset {reset} tập, bắt đầu lại...")
        self._ns_on_start()

    def _ns_on_movie_done(self, movie: XSMovie):
        if movie not in self.movies:
            return
        row = self.movies.index(movie)
        ok = sum(1 for e in movie.episodes if e.selected and e.status == "done")
        total = movie.selected_count
        self._ns_set_status(row, f"Done {ok}/{total}")

        result_item = QtWidgets.QTableWidgetItem(self._ns_build_result_summary(movie))
        self.ns_table.setItem(row, 4, result_item)
        time_item = QtWidgets.QTableWidgetItem(self._ns_format_time_info(movie))
        self.ns_table.setItem(row, 5, time_item)
        self.ns_table.resizeRowsToContents()
        self._ns_update_row_btns(movie)

        if self._ns_iterator is None:
            return
        iterator = self._ns_iterator
        self._ns_iterator = None
        self._ns_run_next_movie(iterator)

    def _ns_build_result_summary(self, movie: XSMovie) -> str:
        sel = [e for e in movie.episodes if e.selected]
        ok = sum(1 for e in sel if e.status == "done" and e.merge_note == "ok")
        no_sub = sum(1 for e in sel if e.status == "done" and e.merge_note == "no_sub")
        dur = sum(1 for e in sel if e.status == "done" and e.merge_note.startswith("dur:"))
        err = sum(1 for e in sel if e.status == "error" or e.merge_note == "error")
        parts = []
        if ok:
            parts.append(f"✅ {ok} OK")
        if no_sub:
            parts.append(f"⚠ {no_sub} thiếu sub")
        if dur:
            diffs = [e.merge_note for e in sel if e.merge_note.startswith("dur:")]
            parts.append(f"⏱ {dur} lệch thời gian ({', '.join(diffs)})")
        if err:
            parts.append(f"❌ {err} lỗi")
        return "\n".join(parts) if parts else "—"

    def _ns_format_time_info(self, movie: XSMovie) -> str:
        if not movie.start_time:
            return "—"
        start_str = time.strftime("%H:%M:%S", time.localtime(movie.start_time))
        if movie.end_time:
            elapsed = int(movie.end_time - movie.start_time)
            m_, s = divmod(elapsed, 60)
            h, m_ = divmod(m_, 60)
            total_str = f"{h}h {m_}m {s}s" if h else f"{m_}m {s}s"
            return f"Bắt đầu: {start_str}\nTổng: {total_str}"
        return f"Bắt đầu: {start_str}"

    # ── Preview ────────────────────────────────────────────────────────────

    def _ns_preview_169(self):
        self._ns_show_sub_preview(aspect="16:9")

    def _ns_preview_full(self):
        self._ns_show_sub_preview(aspect="full")

    def _ns_show_sub_preview(self, aspect: str):
        """Phone-mockup subtitle preview using current sub style settings."""
        font_name  = self.ns_sub_font_combo.currentText().strip() or "Arial"
        font_size  = self.ns_sub_size_spin.value()
        margin_v   = self.ns_sub_margin_v_spin.value()
        color_name = self.ns_sub_color_combo.currentText()
        color_hex  = _COLOR_TO_HEX.get(color_name, color_name) or "#FFFFFF"

        PHONE_W, PHONE_H = 720, 1280

        if aspect == "full":
            vid_x, vid_y, vid_w, vid_h = 0, 0, PHONE_W, PHONE_H
            aspect_label = "Full màn hình phone (9:16)"
        else:
            vid_w = PHONE_W
            vid_h = PHONE_W * 9 // 16
            vid_x = 0
            vid_y = (PHONE_H - vid_h) // 2
            aspect_label = "Video 16:9 trên phone"

        sample_lines = [
            "Phụ đề mẫu  /  Sample subtitle",
            "行  高棉  เชงเม้ง",
        ]

        pixmap = QtGui.QPixmap(PHONE_W, PHONE_H)
        pixmap.setDevicePixelRatio(1.0)
        pixmap.fill(QtGui.QColor("#111111"))

        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        if aspect == "16:9":
            painter.fillRect(0, 0, PHONE_W, vid_y, QtGui.QColor("#000000"))
            painter.fillRect(0, vid_y + vid_h, PHONE_W,
                             PHONE_H - vid_y - vid_h, QtGui.QColor("#000000"))

        painter.fillRect(vid_x, vid_y, vid_w, vid_h, QtGui.QColor("#1a1a2e"))
        grad = QtGui.QLinearGradient(vid_x, vid_y, vid_x, vid_y + vid_h)
        grad.setColorAt(0.0, QtGui.QColor(40, 40, 80, 100))
        grad.setColorAt(1.0, QtGui.QColor(0, 0, 0, 200))
        painter.fillRect(vid_x, vid_y, vid_w, vid_h, grad)

        font = QtGui.QFont(font_name, font_size)
        font.setBold(self.ns_sub_bold_cb.isChecked())
        font.setItalic(self.ns_sub_italic_cb.isChecked())
        painter.setFont(font)

        text_color    = QtGui.QColor(color_hex)
        outline_color = QtGui.QColor(0, 0, 0, 255)
        outline_size  = max(2, int(font_size * 0.13))

        fm           = QtGui.QFontMetrics(font)
        line_spacing = int(font_size * 0.3)
        total_th     = len(sample_lines) * (fm.height() + line_spacing) - line_spacing

        vid_bottom  = vid_y + vid_h
        text_y_base = vid_bottom - margin_v - total_th
        max_tw      = max(fm.horizontalAdvance(line) for line in sample_lines)
        text_x      = (PHONE_W - max_tw) // 2

        y = text_y_base + fm.ascent()
        for line in sample_lines:
            x = text_x
            for dx in range(-outline_size, outline_size + 1):
                for dy in range(-outline_size, outline_size + 1):
                    if dx == 0 and dy == 0:
                        continue
                    painter.setPen(outline_color)
                    painter.drawText(int(x + dx), int(y + dy), line)
            painter.setPen(text_color)
            painter.drawText(int(x), int(y), line)
            y += fm.height() + line_spacing

        painter.end()

        scaled = pixmap.scaled(
            360, 640,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(
            f"Preview  |  {aspect_label}  |  {font_name}  {font_size}px  |  {color_name}")
        dlg.setStyleSheet("QDialog { background: #1a1a1a; }")

        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 14)
        lay.setSpacing(10)

        phone_w = _NSPhoneMockup(scaled, scaled.width(), scaled.height(), dlg)
        lay.addWidget(phone_w, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        bold_label = "On" if self.ns_sub_bold_cb.isChecked() else "Off"
        italic_label = "On" if self.ns_sub_italic_cb.isChecked() else "Off"
        info = (
            f"Font: {font_name}  |  Size: {font_size}  |  MarginV: {margin_v}"
            f"  |  Color: {color_hex}  |  Bold: {bold_label}  |  Italic: {italic_label}")
        info_lbl = QtWidgets.QLabel(info)
        info_lbl.setStyleSheet(
            "QLabel { color: #aaa; background: #0d0d0d; font-size: 11px; "
            "padding: 5px 10px; border-radius: 4px; }")
        info_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(info_lbl)

        dlg.adjustSize()
        dlg.exec()
