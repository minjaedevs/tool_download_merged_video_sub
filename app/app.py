"""
Tool Download Movie Pro - yt-dlp GUI with XemShort downloader.

Supports:
  - XemShort mode: fetch episodes by movie_id, parallel download, hardcode sub + crop overlay
  - yt-dlp mode: download single URLs with optional subtitles
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess as sp
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import qtawesome as qta
import requests
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings

from dep_dl import DepWorker
from ui.main_window import Ui_MainWindow
from utils import BIN_DIR, ROOT, ItemRoles, TreeColumn, load_toml, save_toml
from worker import DownloadWorker
from update_version import Updater
from xemshort import XemShortTab

try:
    from _version import __version__
except ImportError:
    __version__ = "1.0.0"

# NetShort constants moved to xemshort/.


def _get_time_greeting() -> str:
    """Return a Vietnamese greeting based on the current hour of day."""
    h = time.localtime().tm_hour
    if 5 <= h < 12:
        return "Chào buổi sáng"
    if 12 <= h < 18:
        return "Chào buổi chiều"
    return "Chào buổi tối"


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s (%(module)s:%(lineno)d) %(message)s",
    handlers=[
        logging.FileHandler(BIN_DIR / "debug.log", encoding="utf-8", delay=True),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================================
# SUB VIEWER DIALOG  (existing)
# ============================================================================

class SubViewerDialog(QtWidgets.QDialog):
    """Subtitle viewer dialog with search support."""

    def __init__(self, srt_content: str, parent=None):
        """Build dialog UI and render the SRT content as coloured HTML."""
        super().__init__(parent)
        self.setWindowTitle("Phụ Đề")
        self.setMinimumSize(700, 500)
        self.resize(750, 550)
        self.setModal(False)

        layout = QtWidgets.QVBoxLayout(self)

        toolbar = QtWidgets.QHBoxLayout()
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Tìm kiếm...")
        self.search_input.textChanged.connect(self._do_search)
        _nav_style = (
            "QPushButton { background-color: #374151; color: #d1d5db; padding: 2px 6px; "
            "border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #4b5563; }"
            "QPushButton:disabled { background-color: #1f2937; color: #6b7280; }"
        )
        self.btn_prev = QtWidgets.QPushButton("<")
        self.btn_prev.setFixedWidth(30)
        self.btn_prev.setStyleSheet(_nav_style)
        self.btn_prev.clicked.connect(self._prev_match)
        self.btn_next = QtWidgets.QPushButton(">")
        self.btn_next.setFixedWidth(30)
        self.btn_next.setStyleSheet(_nav_style)
        self.btn_next.clicked.connect(self._next_match)
        self.match_label = QtWidgets.QLabel("")
        toolbar.addWidget(QtWidgets.QLabel("Tìm:"))
        toolbar.addWidget(self.search_input)
        toolbar.addWidget(self.btn_prev)
        toolbar.addWidget(self.btn_next)
        toolbar.addWidget(self.match_label)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.text_edit = QtWidgets.QTextEdit()
        self.text_edit.setReadOnly(True)
        font = QtGui.QFont("Consolas", 9)
        self.text_edit.setFont(font)
        layout.addWidget(self.text_edit)

        footer = QtWidgets.QHBoxLayout()
        self.line_count_label = QtWidgets.QLabel("")
        self.line_count_label.setStyleSheet("color: #888; font-size: 11px")
        footer.addWidget(self.line_count_label)
        btn_close = QtWidgets.QPushButton("Đóng")
        btn_close.setStyleSheet(
            "QPushButton { background-color: #4b5563; color: white; padding: 4px 14px; "
            "border-radius: 4px; }"
            "QPushButton:hover { background-color: #374151; }"
        )
        btn_close.clicked.connect(self.close)
        footer.addStretch()
        footer.addWidget(btn_close)
        layout.addLayout(footer)

        self._srt_content = srt_content
        self._all_highlights = []
        self._cur_match = -1
        self._render_srt()

    def _render_srt(self):
        """Render raw SRT text as coloured HTML and display in the text widget."""
        lines = self._srt_content.split("\n")
        html_lines = []
        entry_count = 0

        for line in lines:
            stripped = line.strip()
            if stripped.isdigit():
                entry_count += 1
                html_lines.append(
                    f'<div class="entry" style="color:#60a5fa;font-weight:bold;">{line}</div>'
                )
            elif "-->" in line:
                html_lines.append(f'<div class="time" style="color:#f97316;">{line}</div>')
            elif stripped:
                html_lines.append(f'<div class="text">{line}</div>')
            else:
                html_lines.append('<div class="gap">&nbsp;</div>')

        html = (
            '<html><head><style>'
            'body{background:#0f1117;color:#e2e8f0;padding:8px;font-family:Consolas,monospace;font-size:13px}'
            '.entry{font-size:11px;margin-top:8px}'
            '.time{font-size:11px;margin-bottom:2px}'
            '.text{margin-bottom:6px;line-height:1.5}'
            '.gap{height:4px}'
            '.hl{background:#fbbf24;color:#0f1117;padding:1px 2px;border-radius:2px}'
            '</style></head><body>' + "".join(html_lines) + '</body></html>'
        )
        self.text_edit.setHtml(html)
        self.line_count_label.setText(f"{entry_count} entries")

    def _do_search(self, text):
        """Highlight all matches of the search term and update the match counter."""
        if not text:
            self._cur_match = -1
            self._all_highlights = []
            self.match_label.setText("")
            self._render_srt()
            return
        self._cur_match = -1
        self._all_highlights = []
        pattern = re.compile(re.escape(text), re.IGNORECASE)
        html_lines = []
        entry_count = 0
        for line in self._srt_content.split("\n"):
            stripped = line.strip()
            if stripped.isdigit():
                entry_count += 1
                safe = QtCore.Q.escape(line)
                html_lines.append(f'<div class="entry">{safe}</div>')
            elif "-->" in line:
                safe = QtCore.Q.escape(line)
                html_lines.append(f'<div class="time">{safe}</div>')
            elif stripped:
                safe = QtCore.Q.escape(line)
                highlighted = pattern.sub(lambda m: f'<span class="hl">{m.group()}</span>', safe)
                count = len(pattern.findall(line))
                self._all_highlights.extend([True] * count)
                html_lines.append(f'<div class="text">{highlighted}</div>')
            else:
                html_lines.append('<div class="gap">&nbsp;</div>')
        html = (
            '<html><head><style>'
            'body{background:#0f1117;color:#e2e8f0;padding:8px;font-family:Consolas,monospace;font-size:13px}'
            '.entry{font-size:11px;margin-top:8px}'
            '.time{font-size:11px;margin-bottom:2px}'
            '.text{margin-bottom:6px;line-height:1.5}'
            '.gap{height:4px}'
            '.hl{background:#fbbf24;color:#0f1117;padding:1px 2px;border-radius:2px}'
            '</style></head><body>' + "".join(html_lines) + '</body></html>'
        )
        self.text_edit.setHtml(html)
        self.match_label.setText(f"0/{len(self._all_highlights)}")

    def _prev_match(self):
        """Navigate to the previous search match."""
        if not self._all_highlights:
            return
        self._cur_match = (self._cur_match - 1) % len(self._all_highlights)
        self.match_label.setText(f"{self._cur_match + 1}/{len(self._all_highlights)}")

    def _next_match(self):
        """Navigate to the next search match."""
        if not self._all_highlights:
            return
        self._cur_match = (self._cur_match + 1) % len(self._all_highlights)
        self.match_label.setText(f"{self._cur_match + 1}/{len(self._all_highlights)}")


# ============================================================================
# MAIN WINDOW
# ============================================================================

class MainWindow(QtWidgets.QMainWindow, Ui_MainWindow):
    """Main application window with XemShort and yt-dlp tabs."""

    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.setWindowIcon(QtGui.QIcon(str(ROOT / "assets" / "yt-dlp-gui.ico")))
        self.setMinimumWidth(1100)
        self.load_config()
        self.connect_ui()

        # Build our own tab widget (before centralwidget is set)
        tab_widget = QtWidgets.QTabWidget()
        tab_widget.setDocumentMode(True)

        # Tab 1: XemShort
        xs_tab = XemShortTab()
        tab_widget.addTab(xs_tab, "XemShort")

        # Tab 2: yt-dlp (original UI from setupUi)
        yt_dlp_idx = tab_widget.addTab(self.centralwidget, "yt-dlp")
        tab_widget.tabBar().setTabVisible(yt_dlp_idx, False)  # hide yt-dlp tab

        tab_widget.setCurrentIndex(0)  # default to XemShort
        self.setCentralWidget(tab_widget)

        self.show()

        self._check_first_launch()
        self.dep_worker = DepWorker(self.config["general"]["update_ytdlp"])
        self.dep_worker.finished.connect(self.on_dep_finished)
        self.dep_worker.progress.connect(self.on_dep_progress)
        self.dep_worker.start()

        self.to_dl = {}
        self.workers = {}
        self.index = 0
        self._sub_dialogs = {}

        self.updater = Updater(self)
        self.updater.check(silent=True)

    # -------------------------------------------------------------------------
    # yt-dlp UI handlers
    # -------------------------------------------------------------------------

    def connect_ui(self):
        """Wire all yt-dlp tab buttons and menu actions to their handler slots."""
        self.pb_path.clicked.connect(self.button_path)
        self.pb_add.clicked.connect(self.button_add)
        self.pb_clear.clicked.connect(self.button_clear)
        self.pb_download.clicked.connect(self.button_download)

        self.action_open_bin_folder.triggered.connect(
            lambda: self.open_folder(BIN_DIR)
        )
        self.action_open_log_folder.triggered.connect(
            lambda: self.open_folder(ROOT)
        )
        self.action_exit.triggered.connect(self.close)
        self.action_about.triggered.connect(self.show_about)
        self.action_help.triggered.connect(self.show_help)
        self.action_clear_url_list.triggered.connect(self.te_link.clear)
        self.action_load_txt.triggered.connect(self.button_load_txt)
        self.action_check_update.triggered.connect(self._on_check_update)

    def on_dep_progress(self, status):
        """Show dependency download status in the status bar."""
        self.statusBar.showMessage(status, 10000)

    def on_dep_finished(self):
        """Clean up the dep worker and re-enable the download button."""
        self.dep_worker.deleteLater()
        try:
            self.pb_download.setEnabled(True)
        except RuntimeError:
            pass

    def open_folder(self, path):
        """Open a directory in the OS file explorer."""
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def show_about(self):
        """Display the About dialog with version and project info."""
        QtWidgets.QMessageBox.about(
            self,
            "About Tool Download Movie Pro",
            f'<a href="https://github.com/dsymbol/yt-dlp-gui">Tool Download Movie Pro</a> {__version__}<br><br>'
            "Phần mềm tải phim, video từ nhiều nguồn.<br>"
            "Hỗ trợ XemShort mode (tải phim từ xemshort.top) và yt-dlp mode.",
        )

    def show_help(self):
        """Display the usage guide dialog."""
        help_text = (
            "<b>Hướng dẫn sử dụng Tool Download Movie Pro</b><br><br>"
            "<b>1. Chế độ XemShort (mặc định):</b><br>"
            "- Truy cập <a href='https://xemshort.top'>https://xemshort.top</a><br>"
            "- Tìm phim muốn tải, mở trang phim<br>"
            "- Copy Movie ID từ URL<br>"
            "- Dán Movie ID vào ô, nhấn Fetch Data<br>"
            "- Chọn tập, cấu hình merge phụ đề, nhấn Start<br><br>"
            "<b>2. Chế độ yt-dlp:</b><br>"
            "- Nhập URL video vào ô trên<br>"
            "- Chọn preset (best/mp4/mp3) và thư mục lưu<br>"
            "- Nhấn Add để thêm vào queue, nhấn Download để tải<br><br>"
            "<b>3. Batch download (yt-dlp):</b><br>"
            "- Vào menu Tools > Load txt file<br>"
            "- Định dạng: VIDEO=url / SUB=subtitle_url / ---<br><br>"
            f"<b>Version:</b> {__version__}"
        )

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Hướng dẫn sử dụng")
        msg.setText(help_text)
        msg.setTextFormat(QtCore.Qt.RichText)
        msg.setStandardButtons(QtWidgets.QMessageBox.Ok)
        msg.exec()

    def _on_check_update(self):
        """Called when the user selects 'Kiem tra cap nhat' from the Help menu."""
        self.updater.check(silent=False)

    def open_menu(self, position):
        """Show right-click context menu on the download tree (Delete / Copy URL / Open Folder)."""
        menu = QtWidgets.QMenu()

        delete_action = menu.addAction(qta.icon("mdi6.trash-can"), "Delete")
        copy_url_action = menu.addAction(qta.icon("mdi6.content-copy"), "Copy URL")
        open_folder_action = menu.addAction(
            qta.icon("mdi6.folder-open"), "Open Folder"
        )

        item = self.tw.itemAt(position)

        if item:
            item_path = item.data(0, ItemRoles.PathRole)
            item_link = item.data(0, ItemRoles.LinkRole)
            action = menu.exec(self.tw.viewport().mapToGlobal(position))

            if action == delete_action:
                self.remove_item(item, 0)
            elif action == copy_url_action:
                QtWidgets.QApplication.clipboard().setText(item_link)
                logger.info(f"Copied URL to clipboard: {item_link}")
            elif action == open_folder_action:
                self.open_folder(item_path)
                logger.info(f"Opened folder: {item_path}")

    def remove_item(self, item, column):
        """Stop the worker for an item (if running) and remove it from the tree."""
        item_id = item.data(0, ItemRoles.IdRole)
        item_text = item.text(0)

        logger.debug(f"Removing download ({item_id}): {item_text}")

        if worker := self.workers.get(item_id):
            worker.stop()

        self.to_dl.pop(item_id, None)
        self.tw.takeTopLevelItem(
            self.tw.indexOfTopLevelItem(item)
        )

    def button_path(self):
        """Open a folder picker and set the output path field."""
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select a folder",
            self.le_path.text() or QtCore.QDir.homePath(),
            QtWidgets.QFileDialog.Option.ShowDirsOnly,
        )

        if path:
            self.le_path.setText(path)

    def button_add(self):
        """Validate inputs, create a DownloadWorker per URL, and add each to the queue."""
        missing = []
        preset = self.dd_preset.currentText()
        links = self.te_link.toPlainText()
        path = self.le_path.text()
        sub_url = self.le_sub_url.text().strip()

        if not links:
            missing.append("Video URL")
        if not path:
            missing.append("Save to")

        if missing:
            missing_fields = ", ".join(missing)
            return QtWidgets.QMessageBox.information(
                self,
                "Application Message",
                f"Required field{'s' if len(missing) > 1 else ''} ({missing_fields}) missing.",
            )

        self.te_link.clear()
        self.le_sub_url.clear()

        for link in links.split("\n"):
            link = link.strip()
            item = QtWidgets.QTreeWidgetItem(
                self.tw, [link, preset, "-", "", "Queued", "-", "-"]
            )
            pb = QtWidgets.QProgressBar()
            pb.setStyleSheet("QProgressBar { margin-bottom: 3px; }")
            pb.setTextVisible(False)
            self.tw.setItemWidget(item, 3, pb)
            [
                item.setTextAlignment(i, QtCore.Qt.AlignmentFlag.AlignCenter)
                for i in range(1, 6)
            ]
            item.setData(0, ItemRoles.IdRole, self.index)
            item.setData(0, ItemRoles.LinkRole, link)
            item.setData(0, ItemRoles.PathRole, path)

            worker = DownloadWorker(
                item, self.config, link, path, preset, sub_url
            )
            self.to_dl[self.index] = worker
            logger.info(f"Queued download ({self.index}) added {link}")
            self.index += 1

    def button_load_txt(self):
        """Load a batch .txt file (VIDEO=/SUB=/--- format) and queue all entries."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open .txt file",
            "",
            "Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error",
                                          f"Cannot read file:\n{e}")
            return

        items = []
        current_video = None
        current_sub = ""
        path_dir = self.le_path.text()

        for line in lines:
            line = line.strip()
            if line.startswith("VIDEO="):
                current_video = line[6:].strip()
            elif line.startswith("SUB="):
                current_sub = line[4:].strip()
            elif line == "---":
                if current_video:
                    items.append((current_video, current_sub))
                current_video = None
                current_sub = ""

        if current_video:
            items.append((current_video, current_sub))

        if not items:
            QtWidgets.QMessageBox.information(
                self, "Info", "No valid VIDEO= entries found in the file."
            )
            return

        preset = self.dd_preset.currentText()
        added = 0

        for video_url, sub_url in items:
            item = QtWidgets.QTreeWidgetItem(
                self.tw, [video_url, preset, "-", "", "Queued", "-", "-"]
            )
            pb = QtWidgets.QProgressBar()
            pb.setStyleSheet("QProgressBar { margin-bottom: 3px; }")
            pb.setTextVisible(False)
            self.tw.setItemWidget(item, 3, pb)
            [
                item.setTextAlignment(i, QtCore.Qt.AlignmentFlag.AlignCenter)
                for i in range(1, 6)
            ]
            item.setData(0, ItemRoles.IdRole, self.index)
            item.setData(0, ItemRoles.LinkRole, video_url)
            item.setData(0, ItemRoles.PathRole, path_dir)

            worker = DownloadWorker(
                item, self.config, video_url, path_dir, preset, sub_url
            )
            self.to_dl[self.index] = worker
            logger.info(f"Batch queued ({self.index}): {video_url}")
            self.index += 1
            added += 1

        self.statusBar.showMessage(f"Đã thêm {added} tập vào queue", 5000)

    def button_clear(self):
        """Clear the download queue and tree; blocked if downloads are in progress."""
        if self.workers:
            return QtWidgets.QMessageBox.critical(
                self,
                "Application Message",
                "Unable to clear list because there are active downloads in progress.\n"
                "Remove a download by right clicking on it and selecting delete.",
            )

        self.workers = {}
        self.to_dl = {}
        self.tw.clear()

    def button_download(self):
        """Auto-add any pending URL text, then start all queued DownloadWorkers."""
        if self.te_link.toPlainText().strip():
            self.button_add()

        if not self.to_dl:
            return QtWidgets.QMessageBox.information(
                self,
                "Application Message",
                "Unable to download because there are no links in the list.",
            )

        for idx, worker in self.to_dl.items():
            self.workers[idx] = worker
            worker.finished.connect(worker.deleteLater)
            worker.finished.connect(lambda x=idx: self.workers.pop(x))
            worker.progress.connect(self.on_dl_progress)
            worker.start()

        self.to_dl = {}

    def load_config(self):
        """Load config.toml from user data dir (or copy default), populate presets dropdown."""
        bin_dir = BIN_DIR
        bin_dir.mkdir(parents=True, exist_ok=True)
        config_path = bin_dir / "config.toml"

        default_config = {
            "general": {"update_ytdlp": True, "current_preset": 0, "path": ""},
            "presets": {
                "best": "-f bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "mp4": "-f bv*[vcodec^=avc]+ba[ext=m4a]/b",
                "mp3": "--extract-audio --audio-format mp3 --audio-quality 0",
                "xemshort": "-f bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            },
            "update": {
                "github_repo": "minjaedevs/tool_download_merged_video_sub",
                "enabled": True,
            },
        }

        self.config = default_config.copy()

        if config_path.exists():
            try:
                loaded = load_toml(config_path)
                if "general" in loaded and "presets" in loaded:
                    self.config = loaded
                else:
                    logger.warning("Config missing keys, using defaults + loaded values.")
                    self.config["general"].update(loaded.get("general", {}))
                    self.config["presets"].update(loaded.get("presets", {}))
            except Exception:
                logger.warning("Config load failed, using defaults.")
        elif (ROOT / "root" / "config.toml").exists():
            try:
                self.config = load_toml(ROOT / "root" / "config.toml")
                save_toml(config_path, self.config)
                logger.info("Copied default config to user data dir.")
            except Exception:
                logger.warning("Failed to copy default config.")

        update_ytdlp = self.config["general"].get("update_ytdlp")
        self.config["general"]["update_ytdlp"] = (
            update_ytdlp if update_ytdlp else True
        )
        self.dd_preset.addItems(self.config["presets"].keys())
        self.dd_preset.setCurrentIndex(
            self.config["general"].get("current_preset", 0)
        )
        self.le_path.setText(self.config["general"].get("path", ""))

    def on_dl_progress(self, item: QtWidgets.QTreeWidgetItem, emit_data):
        """Receive progress updates from DownloadWorker and update the tree row."""
        try:
            for data in emit_data:
                index, update = data
                logger.debug(
                    f"on_dl_progress: item={item.data(0,ItemRoles.IdRole)} "
                    f"index={index} update={repr(str(update)[:50])}"
                )
                if index == 3:
                    pb = self.tw.itemWidget(item, index)
                    if pb:
                        pb.setValue(round(float(update.replace("%", ""))))
                elif index == 999:
                    item.setData(0, ItemRoles.SubSrtRole, update)
                    logger.debug(f"  -> Stored SRT content len={len(update)}")
                elif index == TreeColumn.SUB:
                    item.setText(index, update)
                    if update:
                        brush = QtGui.QBrush(QtGui.QColor("#60a5fa"))
                        item.setForeground(index, brush)
                    logger.debug(f"  -> Set Sub column to: {update}")
                elif index != 3:
                    item.setText(index, update)
        except AttributeError:
            logger.info(
                f"Download ({item.data(0, ItemRoles.IdRole)}) no longer exists"
            )
        except Exception as e:
            logger.error(f"on_dl_progress error: {e}")

    def _on_tw_item_clicked(self, item, col):
        """Open (or focus) the subtitle viewer dialog when the Sub column is clicked."""
        logger.debug(
            f"itemClicked: col={col}, SUB={TreeColumn.SUB}, "
            f"match={col == TreeColumn.SUB}"
        )
        if col != TreeColumn.SUB:
            return
        item_id = item.data(0, ItemRoles.IdRole)
        srt_content = item.data(0, ItemRoles.SubSrtRole)
        logger.debug(
            f"  item_id={item_id}, "
            f"srt_content={type(srt_content).__name__}("
            f"{len(srt_content) if srt_content else 0})"
        )
        if not srt_content:
            return
        if item_id in self._sub_dialogs and self._sub_dialogs[item_id] is not None:
            dlg = self._sub_dialogs[item_id]
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        else:
            dlg = SubViewerDialog(srt_content, self)
            self._sub_dialogs[item_id] = dlg
            dlg.show()

    def closeEvent(self, event):
        """Persist preset selection and output path on window close."""
        self.config["general"]["current_preset"] = self.dd_preset.currentIndex()
        self.config["general"]["path"] = self.le_path.text()
        bin_dir = BIN_DIR
        bin_dir.mkdir(parents=True, exist_ok=True)
        save_toml(bin_dir / "config.toml", self.config)
        event.accept()

    # -------------------------------------------------------------------------
    # First launch welcome
    # -------------------------------------------------------------------------

    def _check_first_launch(self):
        """Show a welcome dialog on first launch only (once per installation)."""
        s = QSettings("Tool Download Movie Pro", "AppSettings")
        if s.value("first_launch_done", False, type=bool):
            return
        s.setValue("first_launch_done", True)
        greeting = _get_time_greeting()
        QtWidgets.QMessageBox.information(
            self,
            "Tool Download Movie Pro",
            f"Chào bạn! Đã quay trở lại với Tool Download Movie Pro.\n\n"
            f"Bạn đang sử dụng phiên bản {__version__}.\n"
            f"Chúc bạn một ngày làm việc hiệu quả!",
        )


# ============================================================================
# ENTRY
# ============================================================================


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Tool Download Movie Pro")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
