"""
Tool Download Movie Pro - XemShort downloader.

Supports:
  - XemShort mode: fetch episodes by movie_id, parallel download, hardcode sub + crop overlay
  - M3U8 mode: download M3U8 playlists with concurrent workers
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QSettings

from dep_dl import DepWorker
from ui.main_window import Ui_MainWindow
from utils import BIN_DIR, ROOT, load_toml, save_toml
from update_version import Updater
from xemshort import XemShortTab
from m3utab import M3U8Tab

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
# MAIN WINDOW
# ============================================================================

class MainWindow(QtWidgets.QMainWindow, Ui_MainWindow):
    """Main application window with XemShort and M3U8 tabs."""

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

        # Tab 2: M3U8 Downloader
        m3u8_tab = M3U8Tab()
        tab_widget.addTab(m3u8_tab, "M3U8")

        tab_widget.setCurrentIndex(0)  # default to XemShort
        self.setCentralWidget(tab_widget)

        self.show()

        self._check_first_launch()
        self.dep_worker = DepWorker()
        self.dep_worker.finished.connect(self.on_dep_finished)
        self.dep_worker.progress.connect(self.on_dep_progress)
        self.dep_worker.start()

        self.updater = Updater(self)
        self.updater.check(silent=True)

    # -------------------------------------------------------------------------
    # Menu / UI handlers
    # -------------------------------------------------------------------------

    def connect_ui(self):
        """Wire menu actions to their handler slots."""
        self.action_open_bin_folder.triggered.connect(
            lambda: self.open_folder(BIN_DIR)
        )
        self.action_open_log_folder.triggered.connect(
            lambda: self.open_folder(ROOT)
        )
        self.action_exit.triggered.connect(self.close)
        self.action_about.triggered.connect(self.show_about)
        self.action_help.triggered.connect(self.show_help)
        self.action_check_update.triggered.connect(self._on_check_update)

    def on_dep_progress(self, status):
        """Show dependency download status in the status bar."""
        self.statusBar.showMessage(status, 10000)

    def on_dep_finished(self):
        """Clean up the dep worker."""
        self.dep_worker.deleteLater()

    def open_folder(self, path):
        """Open a directory in the OS file explorer."""
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def show_about(self):
        """Display the About dialog with version and project info."""
        QtWidgets.QMessageBox.about(
            self,
            "About Tool Download Movie Pro",
            f"Tool Download Movie Pro {__version__}<br><br>"
            "Phần mềm tải phim, video từ nhiều nguồn.<br>"
            "Hỗ trợ XemShort mode (tải phim từ xemshort.top) và M3U8 mode.",
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
            "<b>2. Chế độ M3U8:</b><br>"
            "- Nhập URL video M3U8, đặt tên (tùy chọn)<br>"
            "- Chọn thư mục lưu, cấu hình số luồng<br>"
            "- Nhấn Thêm để thêm vào danh sách<br>"
            "- Nhấn Start All để tải nhiều video cùng lúc<br>"
            "- Mỗi hàng có nút mở thư mục và xóa riêng<br><br>"
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

    def load_config(self):
        """Load config.toml from user data dir (or copy default)."""
        bin_dir = BIN_DIR
        bin_dir.mkdir(parents=True, exist_ok=True)
        config_path = bin_dir / "config.toml"

        default_config = {
            "update": {
                "github_repo": "minjaedevs/tool_download_merged_video_sub",
                "enabled": True,
            },
        }

        self.config = default_config.copy()

        if config_path.exists():
            try:
                loaded = load_toml(config_path)
                if "update" in loaded:
                    self.config = loaded
                else:
                    self.config["update"].update(loaded.get("update", {}))
            except Exception:
                logger.warning("Config load failed, using defaults.")
        elif (ROOT / "root" / "config.toml").exists():
            try:
                loaded = load_toml(ROOT / "root" / "config.toml")
                if "update" in loaded:
                    self.config["update"].update(loaded.get("update", {}))
                save_toml(config_path, self.config)
                logger.info("Copied default config to user data dir.")
            except Exception:
                logger.warning("Failed to copy default config.")

    def closeEvent(self, event):
        """Persist config on window close."""
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
