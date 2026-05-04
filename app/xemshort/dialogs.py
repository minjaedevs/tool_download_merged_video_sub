"""XemShort dialog widgets: detail, VTT editor, video popup, episode picker, paste JSON, phone mockup."""
from __future__ import annotations

import json
import os
import re
import subprocess as sp
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .models import XSEpisode, XSMovie
from .helpers import (
    _ns_analyze_vtt,
    _ns_get_video_duration,
    _ns_get_video_duration_secs,
)


# ============================================================================
# PHONE MOCKUP WIDGET (used by subtitle preview)
# ============================================================================


class _NSPhoneMockup(QtWidgets.QWidget):
    """Draws a rounded phone bezel around a screen pixmap."""

    BEZEL  = 18   # bezel thickness in px
    RADIUS = 28   # outer corner radius

    def __init__(self, screen_pixmap: QtGui.QPixmap, screen_w: int, screen_h: int, parent=None):
        super().__init__(parent)
        self._pix = screen_pixmap
        self._sw  = screen_w
        self._sh  = screen_h
        total_w   = screen_w + self.BEZEL * 2
        total_h   = screen_h + self.BEZEL * 2 + 32  # +32 for home button area
        self.setFixedSize(total_w, total_h)

    def paintEvent(self, event):  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        bz = self.BEZEL
        total_w = self._sw + bz * 2
        total_h = self._sh + bz * 2 + 32

        # Phone body
        body_rect = QtCore.QRectF(0, 0, total_w, total_h)
        painter.setPen(QtGui.QPen(QtGui.QColor("#555"), 1.5))
        painter.setBrush(QtGui.QColor("#222"))
        painter.drawRoundedRect(body_rect, self.RADIUS, self.RADIUS)

        # Screen
        screen_rect = QtCore.QRect(bz, bz, self._sw, self._sh)
        painter.drawPixmap(screen_rect, self._pix)

        # Screen inner border
        painter.setPen(QtGui.QPen(QtGui.QColor("#000"), 1))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(screen_rect)

        # Home button
        cx  = total_w // 2
        cy  = self._sh + bz + 16
        painter.setPen(QtGui.QPen(QtGui.QColor("#666"), 1.5))
        painter.setBrush(QtGui.QColor("#333"))
        painter.drawEllipse(cx - 11, cy - 11, 22, 22)

        # Small notch at top
        notch_w, notch_h = 60, 10
        notch_x = (total_w - notch_w) // 2
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#333"))
        painter.drawRoundedRect(notch_x, 4, notch_w, notch_h, 5, 5)

        painter.end()


# ============================================================================
# NS VIDEO POPUP
# ============================================================================


class XSVideoPopup(QtWidgets.QDialog):
    """Simple popup showing video file info and open button."""

    def __init__(self, video_path: Path, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        size = video_path.stat().st_size
        size_str = f"{size / (1024 * 1024):.1f} MB"
        duration = _ns_get_video_duration(video_path) or "N/A"

        self.setWindowTitle(f"Video - {video_path.name}")
        self.setMinimumWidth(400)
        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            f"<b>File:</b> {video_path.name}<br>"
            f"<b>Path:</b> {video_path}<br>"
            f"<b>Size:</b> {size_str}<br>"
            f"<b>Duration:</b> {duration}"
        )
        info.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse)
        layout.addWidget(info)

        btn_row = QtWidgets.QHBoxLayout()
        open_btn = QtWidgets.QPushButton("Mở file")
        open_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; padding: 5px 14px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1d4ed8; }"
        )
        open_btn.clicked.connect(self._open_file)
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.setStyleSheet(
            "QPushButton { background-color: #4b5563; color: white; padding: 5px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #374151; }"
        )
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _open_file(self):
        import platform
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(self.video_path)
            elif system == "Darwin":
                sp.Popen(["open", str(self.video_path)])
            else:
                sp.Popen(["xdg-open", str(self.video_path)])
        except Exception:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Không thể mở file.")


# ============================================================================
# DURATION WORKER (background ffprobe sum for merged files)
# ============================================================================


class _DurationWorker(QtCore.QThread):
    """Sum duration of merged video files in background; emit total seconds."""
    result = QtCore.Signal(float)

    def __init__(self, paths: list, parent=None):
        super().__init__(parent)
        self._paths = paths

    def run(self):
        total = 0.0
        for p in self._paths:
            secs = _ns_get_video_duration_secs(p)
            if secs:
                total += secs
        self.result.emit(total)


# ============================================================================
# NS DETAIL DIALOG
# ============================================================================


class XSDetailDialog(QtWidgets.QDialog):
    """Dialog showing per-episode details: tập phim, video gốc, VTT, video merged, báo cáo."""

    def __init__(self, movie: XSMovie, parent=None):
        super().__init__(parent)
        self.movie = movie
        self.setWindowTitle(f"Chi tiết - {movie.name}")
        self.resize(900, 600)

        layout = QtWidgets.QVBoxLayout(self)

        self.header = QtWidgets.QLabel(
            f"<b>{movie.name}</b> — {movie.selected_count}/{movie.total} tập được chọn"
        )
        layout.addWidget(self.header)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Tập", "Video gốc", "VTT", "Video Merged", "Action", "Báo cáo"]
        )
        self.table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            5, QtWidgets.QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        layout.addWidget(self.table)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.setStyleSheet(
            "QPushButton { background-color: #4b5563; color: white; padding: 5px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #374151; }"
        )
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._populate()
        QtCore.QTimer.singleShot(0, self._resize_rows)

    def _populate(self):
        """Fill table rows for each episode, update header with status, then load duration async."""
        done_eps = []
        fail_eps = []
        merged_paths = []
        for ep in self.movie.episodes:
            if not ep.selected:
                continue
            self._add_episode_row(ep)
            if ep.merged_path and ep.merged_path.exists():
                done_eps.append(ep.episode)
                merged_paths.append(ep.merged_path)
            elif ep.status == "error":
                fail_eps.append(ep.episode)

        total = self.movie.selected_count
        if fail_eps:
            fail_str = ", ".join(f"Tập {n}" for n in fail_eps)
            self._header_base = (
                f"<b>{self.movie.name}</b> — {len(done_eps)}/{total} tập"
                f" &nbsp;|&nbsp; <span style='color:#ef4444'>⚠ Lỗi: {fail_str}</span>"
            )
        elif done_eps and len(done_eps) == total:
            self._header_base = (
                f"<b>{self.movie.name}</b> — {total}/{total} tập"
                f" &nbsp;|&nbsp; <span style='color:#16a34a'>✅ Hoàn tất</span>"
            )
        else:
            self._header_base = (
                f"<b>{self.movie.name}</b> — {self.movie.selected_count}/{self.movie.total} tập được chọn"
            )
        self.header.setText(self._header_base)

        # Start background duration calculation for merged files
        if merged_paths:
            self._dur_worker = _DurationWorker(merged_paths, parent=self)
            self._dur_worker.result.connect(self._on_duration_ready)
            self._dur_worker.start()

    def _on_duration_ready(self, total_secs: float):
        """Append total merged duration to header once background calc finishes."""
        if total_secs <= 0:
            return
        h = int(total_secs // 3600)
        m = int((total_secs % 3600) // 60)
        s = int(total_secs % 60)
        dur_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self.header.setText(
            self._header_base
            + f" &nbsp;|&nbsp; Tổng: <b>{dur_str}</b>"
        )

    def _resize_rows(self):
        """Resize table rows to fit wrapped content after the table is shown."""
        self.table.resizeRowsToContents()

    def _add_episode_row(self, ep: XSEpisode):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # Col 0: Tập
        label = f"Tập {ep.episode}"
        if ep.name and ep.name != self.movie.name:
            label += f" - {ep.name}"
        self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(label))

        # Col 1: Video gốc
        video_item = QtWidgets.QTableWidgetItem("")
        if ep.video_path and ep.video_path.exists():
            video_item.setText(ep.video_path.name)
            video_item.setData(QtCore.Qt.ItemDataRole.UserRole, str(ep.video_path))
            video_item.setForeground(QtGui.QBrush(QtGui.QColor("#16a34a")))
        self.table.setItem(row, 1, video_item)

        # Col 2: VTT
        vtt_item = QtWidgets.QTableWidgetItem("")
        if ep.sub_path and ep.sub_path.exists():
            vtt_item.setText(ep.sub_path.name)
            vtt_item.setData(QtCore.Qt.ItemDataRole.UserRole, str(ep.sub_path))
            video_ok = ep.video_path and ep.video_path.exists()
            color = "#16a34a" if video_ok else "#d97706"
            vtt_item.setForeground(QtGui.QBrush(QtGui.QColor(color)))
        self.table.setItem(row, 2, vtt_item)

        # Col 3: Video Merged
        merged_item = QtWidgets.QTableWidgetItem("")
        if ep.merged_path and ep.merged_path.exists():
            merged_item.setText(ep.merged_path.name)
            merged_item.setData(QtCore.Qt.ItemDataRole.UserRole, str(ep.merged_path))
            merged_item.setForeground(QtGui.QBrush(QtGui.QColor("#16a34a")))
        self.table.setItem(row, 3, merged_item)

        # Col 4: Action buttons (visible only when merged exists)
        has_merged = bool(ep.merged_path and ep.merged_path.exists())

        copy_btn = QtWidgets.QPushButton("Copy path")
        copy_btn.setToolTip("Copy đường dẫn file merged")
        copy_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white; padding: 2px 6px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #2563eb; }"
        )
        copy_btn.setVisible(has_merged)
        copy_btn.clicked.connect(lambda _, e=ep: self._copy_merged_path(e))

        del_btn = QtWidgets.QPushButton("Xóa")
        del_btn.setToolTip("Xóa file merged")
        del_btn.setStyleSheet(
            "QPushButton { background-color: #ef4444; color: white; padding: 2px 6px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #dc2626; }"
        )
        del_btn.setVisible(has_merged)
        del_btn.clicked.connect(lambda _, e=ep, r=row: self._delete_merged_file(e, r))

        check_btn = QtWidgets.QPushButton("Kiểm tra")
        check_btn.setToolTip("So sánh thời lượng video merged với video gốc")
        check_btn.setStyleSheet(
            "QPushButton { background-color: #f59e0b; color: white; padding: 2px 6px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #d97706; }"
        )
        check_btn.setVisible(has_merged)
        check_btn.clicked.connect(lambda _, e=ep: self._check_merged_vs_original(e))

        cell_widget = QtWidgets.QWidget()
        cell_layout = QtWidgets.QHBoxLayout(cell_widget)
        cell_layout.setContentsMargins(2, 2, 2, 2)
        cell_layout.setSpacing(3)
        cell_layout.addWidget(copy_btn)
        cell_layout.addWidget(del_btn)
        cell_layout.addWidget(check_btn)
        self.table.setCellWidget(row, 4, cell_widget)

        # Col 5: Báo cáo
        report = self._build_report(ep)
        self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(report))

    def _delete_merged_file(self, ep: XSEpisode, row: int):
        """Xóa file merged và cập nhật lại hàng."""
        reply = QtWidgets.QMessageBox.question(
            self, "Xác nhận xóa",
            f"Xóa file merged:\n{ep.merged_path.name}?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        try:
            ep.merged_path.unlink()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Lỗi", f"Không thể xóa file:\n{e}")
            return
        ep.merged_path = None
        self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(""))
        cell_w = self.table.cellWidget(row, 4)
        if cell_w:
            for btn in cell_w.findChildren(QtWidgets.QPushButton):
                btn.setVisible(False)
        report = self._build_report(ep)
        self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(report))

    def _copy_merged_path(self, ep: XSEpisode):
        """Copy đường dẫn file merged vào clipboard."""
        if ep.merged_path and ep.merged_path.exists():
            QtWidgets.QApplication.clipboard().setText(str(ep.merged_path))
            QtWidgets.QToolTip.showText(
                QtGui.QCursor.pos(),
                f"Copied: {ep.merged_path.name}",
                None, QtCore.QRect(), 1500,
            )

    def _check_merged_vs_original(self, ep: XSEpisode):
        """So sánh thời lượng video merged với video gốc."""
        orig_dur = merged_dur = orig_secs = merged_secs = None

        if ep.video_path and ep.video_path.exists():
            orig_dur = _ns_get_video_duration(ep.video_path)
        if ep.merged_path and ep.merged_path.exists():
            merged_dur = _ns_get_video_duration(ep.merged_path)

        def to_secs(t):
            try:
                return sum(int(x) * 60 ** i for i, x in enumerate(reversed(t.split(":"))))
            except Exception:
                return None

        if orig_dur:
            orig_secs = to_secs(orig_dur)
        if merged_dur:
            merged_secs = to_secs(merged_dur)

        lines = [
            f"Video gốc   : {orig_dur or '—'}",
            f"Video merged: {merged_dur or '—'}",
        ]

        if orig_secs is not None and merged_secs is not None:
            diff = merged_secs - orig_secs
            sign = "+" if diff >= 0 else ""
            lines.append(f"Chênh lệch  : {sign}{diff}s")
            if abs(diff) <= 2:
                lines.append("✅ OK — thời lượng khớp (<=2s)")
                icon = QtWidgets.QMessageBox.Information
            else:
                lines.append(f"⚠ Chênh lệch {abs(diff)}s — kiểm tra lại!")
                icon = QtWidgets.QMessageBox.Warning
        elif not orig_dur:
            lines.append("⚠ Không đọc được video gốc")
            icon = QtWidgets.QMessageBox.Warning
        elif not merged_dur:
            lines.append("⚠ Không đọc được video merged")
            icon = QtWidgets.QMessageBox.Warning
        else:
            icon = QtWidgets.QMessageBox.Question

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle(f"Kiểm tra - Tập {ep.episode}")
        msg.setIcon(icon)
        msg.setText("\n".join(lines))
        msg.exec()

    def _build_report(self, ep: XSEpisode) -> str:
        """Build a report string comparing video durations and VTT subtitle analysis."""
        orig_dur = merged_dur = None
        if ep.video_path and ep.video_path.exists():
            orig_dur = _ns_get_video_duration(ep.video_path)
        if ep.merged_path and ep.merged_path.exists():
            merged_dur = _ns_get_video_duration(ep.merged_path)

        dur_label = "—"
        dur_detail = ""
        if orig_dur and merged_dur:
            try:
                orig_secs = sum(
                    int(x) * 60 ** i for i, x in enumerate(reversed(orig_dur.split(":"))))
                merged_secs = sum(
                    int(x) * 60 ** i for i, x in enumerate(reversed(merged_dur.split(":"))))
                diff = abs(merged_secs - orig_secs)
                if diff <= 2:
                    dur_label = "OK"
                else:
                    dur_label = "⚠ Chênh lệch"
                dur_detail = f" | Gốc: {orig_dur} | Merge: {merged_dur}"
            except Exception:
                dur_label = "?"
                dur_detail = f" | Gốc: {orig_dur} | Merge: {merged_dur}"
        elif merged_dur:
            dur_label = "?"
            dur_detail = f" | Merge: {merged_dur}"
        elif orig_dur:
            dur_label = "⚠ Chưa merge"
            dur_detail = f" | Gốc: {orig_dur}"

        vtt_label = ""
        if ep.sub_path and ep.sub_path.exists():
            analysis = _ns_analyze_vtt(ep.sub_path)
            if analysis["total"] > 0:
                vtt_label = f" | VTT: {analysis['total']} mốc"
                if analysis["short"] > 0:
                    vtt_label += f", ⚠ {analysis['short']} ngắn"

        return f"{dur_label}{dur_detail}{vtt_label}"


# ============================================================================
# NS VTT EDITOR DIALOG
# ============================================================================


class XSVttEditorDialog(QtWidgets.QDialog):
    """Dialog for editing a VTT subtitle file with search and analysis."""

    def __init__(self, vtt_path: Path, parent=None):
        super().__init__(parent)
        self.vtt_path = vtt_path
        self.setWindowTitle(f"Sửa VTT - {vtt_path.name}")
        self.resize(800, 600)

        layout = QtWidgets.QVBoxLayout(self)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("Tìm:"))
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Tìm kiếm...")
        self.search_input.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self.search_input)
        toolbar.addStretch()

        self.analyze_btn = QtWidgets.QPushButton("Phân tích")
        self.analyze_btn.setStyleSheet(
            "QPushButton { background-color: #6366f1; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #4f46e5; }"
        )
        self.analyze_btn.clicked.connect(self._analyze)
        toolbar.addWidget(self.analyze_btn)
        layout.addLayout(toolbar)

        self.text_edit = QtWidgets.QTextEdit()
        self.text_edit.setFont(QtGui.QFont("Consolas", 10))
        try:
            content = vtt_path.read_text(encoding="utf-8", errors="replace")
            self.text_edit.setPlainText(content)
        except Exception as e:
            self.text_edit.setPlainText(f"# Không thể đọc file: {e}")
        layout.addWidget(self.text_edit)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        save_btn = QtWidgets.QPushButton("Lưu")
        save_btn.setStyleSheet(
            "QPushButton { background-color: #10b981; color: white; padding: 6px 16px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #059669; }"
        )
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        cancel_btn = QtWidgets.QPushButton("Hủy")
        cancel_btn.setStyleSheet(
            "QPushButton { background-color: #4b5563; color: white; padding: 6px 16px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #374151; }"
        )
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _on_search_changed(self, text: str):
        self._clear_highlight()
        if not text:
            return
        self._do_highlight(text)

    def _clear_highlight(self):
        cursor = QtGui.QTextCursor(self.text_edit.document())
        cursor.select(QtGui.QTextCursor.SelectionType.Document)
        fmt = QtGui.QTextCharFormat()
        fmt.setBackground(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
        fmt.setForeground(QtGui.QBrush(QtCore.Qt.GlobalColor.black))
        cursor.setCharFormat(fmt)
        cursor = QtGui.QTextCursor(self.text_edit.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
        self.text_edit.setTextCursor(cursor)

    def _do_highlight(self, text: str):
        doc = self.text_edit.document()
        cursor = QtGui.QTextCursor(doc)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)

        highlight_fmt = QtGui.QTextCharFormat()
        highlight_fmt.setBackground(QtGui.QBrush(QtGui.QColor("#fbbf24")))

        while True:
            finder = QtGui.QTextCursor(cursor)
            finder = doc.find(text, finder)
            if finder.isNull():
                break
            finder.setCharFormat(highlight_fmt)
            if finder.position() == cursor.position():
                cursor.setPosition(cursor.position() + 1)
            else:
                cursor = finder

    def _analyze(self):
        """Check all timestamps and show results in a dialog."""
        content = self.text_edit.toPlainText()
        QtWidgets.QApplication.processEvents()

        found = []
        cue_blocks = re.split(r"\n\n+", content)
        for idx, block in enumerate(cue_blocks):
            if idx % 200 == 0:
                QtWidgets.QApplication.processEvents()
            lines = block.strip().splitlines()
            if len(lines) < 2:
                continue
            ts_line = lines[0]
            match = re.search(
                r"(\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3})",
                ts_line,
            )
            if not match:
                continue
            ts_full = match.group(1)
            sub_lines = [
                l.strip()
                for l in lines[1:]
                if l.strip()
                and not l.strip().startswith(("WEBVTT", "NOTE", "STYLE"))
            ]
            if len(sub_lines) > 1 and any(1 <= len(l.split()) <= 5 for l in sub_lines):
                found.append(
                    f"⏱ {ts_full}\n   Sub: {' | '.join(sub_lines)}"
                )

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Kết quả phân tích VTT")
        dlg.resize(650, 450)
        dlg_layout = QtWidgets.QVBoxLayout(dlg)

        if found:
            header = QtWidgets.QLabel(
                f"⚠ Tìm thấy {len(found)} mốc có sub ngắn (>1 hàng, có hàng 1-5 từ):"
            )
            header.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 13px;")
            dlg_layout.addWidget(header)
            text_edit = QtWidgets.QTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setFont(QtGui.QFont("Consolas", 10))
            text_edit.setPlainText("\n\n".join(found))
            dlg_layout.addWidget(text_edit)
            if len(found) > 20:
                more_lbl = QtWidgets.QLabel(f"... và {len(found) - 20} mốc khác")
                more_lbl.setStyleSheet("color: #6b7280; font-style: italic;")
                dlg_layout.addWidget(more_lbl)
        else:
            ok_lbl = QtWidgets.QLabel("✅ Không tìm thấy mốc nào cần tách.")
            ok_lbl.setStyleSheet("color: #10b981; font-weight: bold; font-size: 14px;")
            dlg_layout.addWidget(ok_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.setStyleSheet(
            "QPushButton { background-color: #4b5563; color: white; padding: 5px 16px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #374151; }"
        )
        close_btn.clicked.connect(dlg.close)
        btn_row.addWidget(close_btn)
        dlg_layout.addLayout(btn_row)
        dlg.exec()

    def _save(self):
        """Save content back to the VTT file."""
        try:
            self.vtt_path.write_text(self.text_edit.toPlainText(), encoding="utf-8")
            QtWidgets.QMessageBox.information(
                self, "Đã lưu", f"Đã lưu file:\n{self.vtt_path.name}"
            )
            self.close()
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Lỗi lưu file", f"Không thể lưu file:\n{e}"
            )


# ============================================================================
# EPISODE PICKER DIALOG
# ============================================================================


class XSEpisodePickerDialog(QtWidgets.QDialog):
    """Dialog for selecting which episodes to add to the download queue."""

    def __init__(self, movie_name: str, episodes: list[XSEpisode], parent=None):
        super().__init__(parent)
        self.episodes = episodes
        self.setWindowTitle(f"Chọn tập - {movie_name}")
        self.resize(500, 600)

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            f"<b>{movie_name}</b> - tổng {len(episodes)} tập. Tick để chọn:"
        )
        layout.addWidget(info)

        btn_row = QtWidgets.QHBoxLayout()
        self.select_all_btn = QtWidgets.QPushButton("Chọn tất cả")
        self.select_all_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; padding: 4px 12px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #1d4ed8; }"
        )
        self.deselect_all_btn = QtWidgets.QPushButton("Bỏ chọn tất cả")
        self.deselect_all_btn.setStyleSheet(
            "QPushButton { background-color: #6b7280; color: white; padding: 4px 12px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #4b5563; }"
        )
        self.select_all_btn.clicked.connect(lambda: self._toggle_all(True))
        self.deselect_all_btn.clicked.connect(lambda: self._toggle_all(False))
        btn_row.addWidget(self.select_all_btn)
        btn_row.addWidget(self.deselect_all_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Tìm tập (VD: 10-20, 5, 15)...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list_widget = QtWidgets.QListWidget()
        for ep in episodes:
            label = f"Tập {ep.episode}"
            if ep.name and ep.name != movie_name:
                label += f" - {ep.name}"
            item = QtWidgets.QListWidgetItem(label)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, ep)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, stretch=1)

        self.list_widget.itemChanged.connect(self._update_count)
        self.count_label = QtWidgets.QLabel()
        self._update_count()
        layout.addWidget(self.count_label)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Thêm")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _toggle_all(self, check: bool):
        state = (QtCore.Qt.CheckState.Checked if check
                 else QtCore.Qt.CheckState.Unchecked)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    def _filter(self, text: str):
        text = text.strip().lower()
        ranges = []
        for part in re.split(r"[,\s]+", text):
            if not part:
                continue
            m = re.match(r"^(\d+)-(\d+)$", part)
            if m:
                ranges.append((int(m.group(1)), int(m.group(2))))
            elif part.isdigit():
                ranges.append((int(part), int(part)))

        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            ep: XSEpisode = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not text:
                item.setHidden(False)
            else:
                visible = any(lo <= ep.episode <= hi for lo, hi in ranges)
                item.setHidden(not visible)

    def _update_count(self):
        n = sum(
            1 for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == QtCore.Qt.CheckState.Checked
        )
        self.count_label.setText(f"Đã chọn: {n}/{self.list_widget.count()}")

    def get_selected_episodes(self) -> list[XSEpisode]:
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            ep: XSEpisode = item.data(QtCore.Qt.ItemDataRole.UserRole)
            ep.selected = (item.checkState() == QtCore.Qt.CheckState.Checked)
        return self.episodes


# ============================================================================
# PASTE JSON DIALOG
# ============================================================================


class XSPasteJsonDialog(QtWidgets.QDialog):
    """Dialog for pasting raw JSON API response text."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dán JSON")
        self.resize(700, 500)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Dán JSON response từ API (hoặc object {success, data: [...]}):"
        ))
        self.text = QtWidgets.QTextEdit()
        self.text.setFont(QtGui.QFont("Consolas", 10))
        layout.addWidget(self.text)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_json(self) -> Optional[dict | list]:
        try:
            return json.loads(self.text.toPlainText())
        except json.JSONDecodeError as e:
            QtWidgets.QMessageBox.warning(self, "Lỗi", f"JSON không hợp lệ: {e}")
            return None


# Backward-compat aliases
NSVideoPopup = XSVideoPopup
NSDetailDialog = XSDetailDialog
NSEpisodePickerDialog = XSEpisodePickerDialog
NSPasteJsonDialog = XSPasteJsonDialog
NSVttEditorDialog = XSVttEditorDialog
