"""XemShort dialog widgets: detail, VTT editor, video popup, episode picker, paste JSON, phone mockup."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess as sp
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
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
        """Build report from cached episode metadata — no blocking ffprobe calls.

        Duration info is read from ep.merge_note which is set during the merge
        worker run, avoiding per-episode subprocess invocations on the UI thread.
        Use the 'Kiểm tra' button for an on-demand ffprobe comparison.
        """
        note = ep.merge_note or ""
        if note in ("ok",) or note.startswith("skip:"):
            dur_label, dur_detail = "OK", ""
        elif note.startswith("dur:"):
            dur_label, dur_detail = "⚠ Chênh lệch", f" | {note[4:]}"
        elif note == "no_sub":
            dur_label, dur_detail = "⚠ Thiếu sub", ""
        elif note == "error":
            msg = ep.error_msg[:60] if ep.error_msg else ""
            dur_label, dur_detail = "⚠ Lỗi", (f" | {msg}" if msg else "")
        elif ep.status == "done":
            dur_label, dur_detail = "OK", ""
        elif ep.status == "error":
            msg = ep.error_msg[:60] if ep.error_msg else ""
            dur_label, dur_detail = "⚠ Lỗi", (f" | {msg}" if msg else "")
        elif ep.status in ("pending", "downloading", "downloaded", "merging"):
            dur_label, dur_detail = ep.status, ""
        else:
            dur_label, dur_detail = "—", ""

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


class _EpisodePreviewDownloadWorker(QtCore.QThread):
    """Download an episode video URL to a temp file for local preview playback."""

    success = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, url: str, episode: int, source: str = "", parent=None):
        super().__init__(parent)
        self.url = url
        self.episode = episode
        self.source = source

    @staticmethod
    def _ffmpeg_path() -> Path | None:
        for name in ("ffmpeg", "ffmpeg.exe"):
            candidate = Path(sys.executable).parent / name
            if candidate.exists():
                return candidate
        path = shutil.which("ffmpeg")
        return Path(path) if path else None

    def _headers_for_url(self) -> dict[str, str]:
        from .workers import NETSHORT_DOWNLOAD_HEADERS

        headers = dict(NETSHORT_DOWNLOAD_HEADERS)
        if self._is_phimngan_preview():
            headers.update({
                "Referer": "https://phimngan.tv/",
                "Origin": "https://phimngan.tv",
            })
        return headers

    def _is_phimngan_preview(self) -> bool:
        return self.source == "phimngan"

    @staticmethod
    def _ffmpeg_headers(headers: dict[str, str]) -> str:
        return "".join(f"{key}: {value}\r\n" for key, value in headers.items())

    def _download_hls_preview(self, out_path: Path, headers: dict[str, str]) -> None:
        ffmpeg_path = self._ffmpeg_path()
        if not ffmpeg_path:
            raise RuntimeError("Can ffmpeg de preview HLS/m3u8.")

        part_path = out_path.with_suffix(out_path.suffix + ".part")
        part_path.unlink(missing_ok=True)
        cmd = [
            str(ffmpeg_path),
            "-y",
            "-loglevel", "warning",
            "-headers", self._ffmpeg_headers(headers),
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-allowed_extensions", "ALL",
            "-allowed_segment_extensions", "ALL",
            "-extension_picky", "0",
            "-i", self.url,
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-t", "120",
            str(part_path),
        ]
        flags = {"creationflags": sp.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}
        err_path = part_path.with_suffix(part_path.suffix + ".stderr.txt")
        err_path.unlink(missing_ok=True)
        err_file = err_path.open("w", encoding="utf-8", errors="replace")
        process = sp.Popen(
            cmd,
            stdout=sp.DEVNULL,
            stderr=err_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            **flags,
        )
        try:
            started_at = time.monotonic()
            while process.poll() is None:
                if self.isInterruptionRequested():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except sp.TimeoutExpired:
                        process.kill()
                        process.wait()
                    part_path.unlink(missing_ok=True)
                    raise RuntimeError("Da huy preview HLS.")
                if time.monotonic() - started_at > 180:
                    process.kill()
                    process.wait()
                    part_path.unlink(missing_ok=True)
                    raise RuntimeError("TIMEOUT preview HLS.")
                self.msleep(100)
        finally:
            err_file.close()
        stderr = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
        err_path.unlink(missing_ok=True)
        result_code = process.returncode
        if result_code != 0:
            part_path.unlink(missing_ok=True)
            raise RuntimeError(stderr[:500] or "ffmpeg preview HLS failed")
        if not part_path.exists() or part_path.stat().st_size <= 1024:
            part_path.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg preview HLS output rong")
        part_path.replace(out_path)

    def run(self):
        try:
            tmp_dir = Path(tempfile.gettempdir()) / "yt_dlp_gui_xemshort_preview"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out_path = tmp_dir / f"preview_ep_{self.episode}_{abs(hash(self.url))}.mp4"
            if out_path.exists() and out_path.stat().st_size > 0:
                if not self._is_phimngan_preview() or _ns_get_video_duration_secs(out_path):
                    self.success.emit(str(out_path))
                    return
                out_path.unlink(missing_ok=True)

            headers = self._headers_for_url()
            with requests.get(
                self.url,
                headers=headers,
                stream=True,
                timeout=(15, 120),
            ) as response:
                response.raise_for_status()
                part_path = out_path.with_suffix(out_path.suffix + ".part")
                first_chunk = b""
                if self._is_phimngan_preview():
                    content_type = response.headers.get("Content-Type", "").lower()
                    first_chunk = next(response.iter_content(chunk_size=1024 * 64), b"")
                    looks_like_hls = (
                        b"#EXTM3U" in first_chunk[:2048]
                        or "mpegurl" in content_type
                        or "vnd.apple.mpegurl" in content_type
                        or ".m3u8" in self.url.lower()
                        or ".m3u" in self.url.lower()
                    )
                    if looks_like_hls:
                        response.close()
                        part_path.unlink(missing_ok=True)
                        self._download_hls_preview(out_path, headers)
                        self.success.emit(str(out_path))
                        return

                with part_path.open("wb") as fh:
                    if first_chunk:
                        fh.write(first_chunk)
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        if self.isInterruptionRequested():
                            return
                        if chunk:
                            fh.write(chunk)
                if self.isInterruptionRequested():
                    return
                part_path.replace(out_path)
            self.success.emit(str(out_path))
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}")


class XSEpisodePickerDialog(QtWidgets.QDialog):
    """Dialog for selecting which episodes to add to the download queue."""

    def __init__(self, movie_name: str, episodes: list[XSEpisode], parent=None, source: str = ""):
        super().__init__(parent)
        self.episodes = episodes
        self.source = source
        self.setWindowTitle(f"Chọn tập - {movie_name}")
        self.resize(820, 650)

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
        self.paste_errors_btn = QtWidgets.QPushButton("Dán tập lỗi")
        self.paste_errors_btn.setStyleSheet(
            "QPushButton { background-color: #f97316; color: white; padding: 4px 12px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #ea580c; }"
        )
        self.paste_errors_btn.setToolTip(
            "Đọc danh sách số tập từ clipboard (copy bằng nút 'Sao chép tập lỗi'),\n"
            "bỏ chọn tất cả, xóa filter rồi tích đúng các tập đó."
        )
        self.select_all_btn.clicked.connect(lambda: self._toggle_all(True))
        self.deselect_all_btn.clicked.connect(lambda: self._toggle_all(False))
        self.paste_errors_btn.clicked.connect(self._paste_error_episodes)
        btn_row.addWidget(self.select_all_btn)
        btn_row.addWidget(self.deselect_all_btn)
        btn_row.addWidget(self.paste_errors_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Tìm tập (VD: 10-20, 5, 15)...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list_widget = QtWidgets.QListWidget()
        for ep in episodes:
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.ItemDataRole.UserRole, ep)
            item.setSizeHint(QtCore.QSize(720, 42))
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, self._build_episode_row(ep, movie_name))
        layout.addWidget(self.list_widget, stretch=1)

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

    def _build_episode_row(self, ep: XSEpisode, movie_name: str) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(8)

        checkbox = QtWidgets.QCheckBox()
        # Locked episodes: uncheck by default and disable to prevent accidental selection
        # Lock khi thiếu video URL hoặc sub URL (cần cả 2 để merge).
        is_locked = not ep.play or not ep.subtitle_url
        checkbox.setChecked(not is_locked)
        checkbox.setEnabled(not is_locked)
        if is_locked:
            checkbox.setToolTip("Tập này bị khóa — thiếu URL video hoặc URL phụ đề")
        checkbox.toggled.connect(lambda *_: self._update_count())
        row._episode_checkbox = checkbox
        row.setProperty("episode_checkbox", checkbox)
        layout.addWidget(checkbox)

        label_text = f"Tap {ep.episode}"
        if ep.name and ep.name != movie_name:
            label_text += f" - {ep.name}"
        if is_locked:
            label_text += "  🔒"
        label = QtWidgets.QLabel(label_text)
        label.setMinimumWidth(220)
        label.setStyleSheet(
            "font-weight: 600; color: #6b7280;" if is_locked else "font-weight: 600;"
        )
        layout.addWidget(label, stretch=1)

        if is_locked:
            video_label = QtWidgets.QLabel("🔒 LOCKED")
            video_label.setStyleSheet("color:#ef4444; font-weight:bold;")
        elif ep.play:
            video_label = QtWidgets.QLabel("Video: yes")
            video_label.setStyleSheet("color:#16a34a;")
        else:
            video_label = QtWidgets.QLabel("Video: no")
            video_label.setStyleSheet("color:#9ca3af;")
        layout.addWidget(video_label)

        sub_label = QtWidgets.QLabel("Sub: yes" if ep.subtitle_url else "Sub: no")
        sub_label.setStyleSheet("color:#16a34a;" if ep.subtitle_url else "color:#9ca3af;")
        layout.addWidget(sub_label)

        play_btn = QtWidgets.QPushButton("Play")
        play_btn.setEnabled(bool(ep.play))
        play_btn.setToolTip(ep.play or "Tap nay khong co video URL")
        play_btn.setStyleSheet(
            "QPushButton { background-color:#2563eb;color:white;padding:3px 10px;border-radius:4px; }"
            "QPushButton:disabled { background-color:#9ca3af; }"
        )
        play_btn.clicked.connect(lambda *_args, e=ep: self._preview_episode(e))
        layout.addWidget(play_btn)

        sub_btn = QtWidgets.QPushButton("Sub")
        sub_btn.setEnabled(bool(ep.subtitle_url))
        sub_btn.setToolTip(ep.subtitle_url or "Tap nay khong co subtitle URL")
        sub_btn.setStyleSheet(
            "QPushButton { background-color:#059669;color:white;padding:3px 10px;border-radius:4px; }"
            "QPushButton:disabled { background-color:#9ca3af; }"
        )
        sub_btn.clicked.connect(lambda *_args, e=ep: self._open_subtitle_url(e))
        layout.addWidget(sub_btn)
        return row

    def _row_checkbox(self, item: QtWidgets.QListWidgetItem) -> QtWidgets.QCheckBox | None:
        row = self.list_widget.itemWidget(item)
        if row is None:
            return None
        direct_checkbox = getattr(row, "_episode_checkbox", None)
        if isinstance(direct_checkbox, QtWidgets.QCheckBox):
            return direct_checkbox
        checkbox = row.property("episode_checkbox")
        return checkbox if isinstance(checkbox, QtWidgets.QCheckBox) else None

    def _preview_episode(self, ep: XSEpisode):
        if not ep.play:
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Preview - Tap {ep.episode}")
        dlg.resize(900, 560)
        layout = QtWidgets.QVBoxLayout(dlg)

        status = QtWidgets.QLabel("Dang tai video preview...")
        status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        status.setStyleSheet("font-size: 14px; color: #6b7280;")
        layout.addWidget(status, stretch=1)

        btn_row = QtWidgets.QHBoxLayout()
        open_url_btn = QtWidgets.QPushButton("Mo URL goc")
        open_url_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(ep.play)))
        btn_row.addWidget(open_url_btn)
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("Dong")
        close_btn.clicked.connect(dlg.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        worker = _EpisodePreviewDownloadWorker(ep.play, ep.episode, source=self.source, parent=dlg)
        dlg._preview_worker = worker
        dlg._preview_player = None
        dlg._preview_audio = None

        def on_success(path: str):
            status.setText("Dang mo video...")
            file_url = QtCore.QUrl.fromLocalFile(path)
            open_file_btn = QtWidgets.QPushButton("Mo file")
            open_file_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(file_url))
            btn_row.insertWidget(1, open_file_btn)
            try:
                from PySide6 import QtMultimedia, QtMultimediaWidgets  # type: ignore

                video_widget = QtMultimediaWidgets.QVideoWidget()
                video_widget.setMinimumHeight(430)
                layout.replaceWidget(status, video_widget)
                status.deleteLater()

                player = QtMultimedia.QMediaPlayer(dlg)
                audio = QtMultimedia.QAudioOutput(dlg)
                player.setAudioOutput(audio)
                player.setVideoOutput(video_widget)
                player.setSource(file_url)
                audio.setVolume(0.8)
                dlg._preview_player = player
                dlg._preview_audio = audio

                play_btn = QtWidgets.QPushButton("Play/Pause")
                play_btn.clicked.connect(
                    lambda: player.pause()
                    if player.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState
                    else player.play()
                )
                btn_row.insertWidget(0, play_btn)
                player.play()
            except Exception as exc:
                status.setText(f"Da tai video nhung khong play duoc trong app: {exc}")
                QtGui.QDesktopServices.openUrl(file_url)

        def on_error(message: str):
            status.setText(f"Khong tai duoc preview: {message}")

        worker.success.connect(on_success)
        worker.error.connect(on_error)
        worker.finished.connect(worker.deleteLater)
        worker.start()

        dlg.finished.connect(
            lambda *_: dlg._preview_player.stop()
            if getattr(dlg, "_preview_player", None) is not None
            else None
        )
        dlg.exec()
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(3000)
        except RuntimeError:
            pass

    def _open_subtitle_url(self, ep: XSEpisode):
        if not ep.subtitle_url:
            return
        QtWidgets.QApplication.clipboard().setText(ep.subtitle_url)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(ep.subtitle_url))
        QtWidgets.QToolTip.showText(
            QtGui.QCursor.pos(),
            "Da copy link sub vao clipboard",
            None,
            QtCore.QRect(),
            1500,
        )

    def _toggle_all(self, check: bool):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if not item.isHidden():
                checkbox = self._row_checkbox(item)
                if checkbox:
                    checkbox.setChecked(check)

    def _paste_error_episodes(self):
        """Đọc danh sách số tập từ clipboard, bỏ chọn tất cả rồi tích đúng các tập đó."""
        text = QtWidgets.QApplication.clipboard().text().strip()
        if not text:
            QtWidgets.QToolTip.showText(
                QtGui.QCursor.pos(),
                "Clipboard trống — hãy nhấn 'Sao chép tập lỗi' trước.",
                None, QtCore.QRect(), 2000,
            )
            return

        # Parse JSON array [1, 2, 5, ...] từ nút "Sao chép tập lỗi"
        ep_nums: set[int] = set()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                ep_nums = {int(x) for x in parsed if isinstance(x, (int, str)) and str(x).strip().isdigit()}
        except Exception:
            pass

        # Fallback: comma/space separated "1,2,5 10"
        if not ep_nums:
            for part in re.split(r"[,\s]+", text):
                part = part.strip()
                if part.isdigit():
                    ep_nums.add(int(part))

        if not ep_nums:
            QtWidgets.QToolTip.showText(
                QtGui.QCursor.pos(),
                "Clipboard không chứa danh sách số tập hợp lệ.",
                None, QtCore.QRect(), 2000,
            )
            return

        # Bỏ chọn tất cả + xóa filter
        self._toggle_all(False)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        # Hiện lại tất cả item (reset filter)
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setHidden(False)

        # Tích các tập có số trong ep_nums
        matched = 0
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            ep: XSEpisode = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if ep.episode in ep_nums:
                checkbox = self._row_checkbox(item)
                if checkbox:
                    checkbox.setChecked(True)
                    matched += 1

        self._update_count()
        not_found = len(ep_nums) - matched
        msg = f"Đã chọn {matched}/{len(ep_nums)} tập từ clipboard"
        if not_found:
            msg += f" ({not_found} tập không tìm thấy)"
        QtWidgets.QToolTip.showText(
            QtGui.QCursor.pos(), msg, None, QtCore.QRect(), 2000,
        )

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
        n = 0
        for i in range(self.list_widget.count()):
            checkbox = self._row_checkbox(self.list_widget.item(i))
            if checkbox and checkbox.isChecked():
                n += 1
        self.count_label.setText(f"Đã chọn: {n}/{self.list_widget.count()}")

    def get_selected_episodes(self) -> list[XSEpisode]:
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            ep: XSEpisode = item.data(QtCore.Qt.ItemDataRole.UserRole)
            checkbox = self._row_checkbox(item)
            ep.selected = bool(checkbox and checkbox.isChecked())
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
