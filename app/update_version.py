"""
Auto-update module: checks GitHub Releases for a newer version and
installs it by replacing the running executable.

Config (bin_dir/config.toml):
  [update]
  enabled = true
  github_repo = "owner/repo"   # e.g. "minjaedevs/tool_download_merged_video_sub"
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import requests
import tomlkit
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog
from utils import BIN_DIR, ROOT

# ── Helpers ──────────────────────────────────────────────────────────────────


def _v(s: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple."""
    return tuple(int(x) for x in s.lstrip("v").split(".") if x.isdigit())


def _get_version() -> str:
    """Return the current app version.

    Uses a direct import so it works in both dev mode and PyInstaller frozen
    bundles (where _version.py is compiled into the exe, not copied as a text
    file next to it).
    """
    try:
        from _version import __version__
        return __version__
    except ImportError:
        return "0.0.0"


def _get_github_repo() -> str | None:
    """Read github_repo from config.toml, return None if absent."""
    try:
        for path in (ROOT / "root", BIN_DIR):
            config_path = path / "config.toml"
            if config_path.exists():
                cfg = tomlkit.parse(config_path.read_text(encoding="utf-8"))
                repo = cfg.get("update", {}).get("github_repo") or cfg.get("general", {}).get("github_repo")
                if repo:
                    return repo
        return None
    except Exception:
        return None


def _get_api_url() -> str | None:
    """Build the GitHub releases API URL from the configured repo."""
    repo = _get_github_repo()
    if not repo:
        return None
    return f"https://api.github.com/repos/{repo}/releases/latest"


def _get_current_exe() -> Path:
    """Return the path to the currently running executable."""
    return Path(sys.executable)


def _get_exe_name() -> str:
    """Return the .exe filename used in release assets."""
    return _get_current_exe().name


# ── Workers ──────────────────────────────────────────────────────────────────


class CheckWorker(QThread):
    """Background thread that queries the GitHub releases API (non-blocking)."""

    result = Signal(dict)
    failed = Signal(str)

    def __init__(self, api_url: str):
        super().__init__()
        self.api_url = api_url

    def run(self):
        try:
            r = requests.get(self.api_url, timeout=15)
            r.raise_for_status()
            self.result.emit(r.json())
        except Exception as e:
            self.failed.emit(str(e))


class DownloadWorker(QThread):
    """Background thread that streams the installer .exe from GitHub."""

    progress = Signal(int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            r = requests.get(self.url, stream=True, timeout=60,
                             headers={"Accept": "application/octet-stream"})
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))

            tmp = Path(tempfile.gettempdir()) / "tool-download-movie-update.exe"
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.progress.emit(int(done * 100 / total))

            self.done.emit(str(tmp))
        except Exception as e:
            self.failed.emit(str(e))


# ── Updater ───────────────────────────────────────────────────────────────────


class Updater(QObject):
    """
    Coordinates version checking, download, and installation of a new release.

    Usage::

        from update_version import Updater
        updater = Updater(parent_window)
        updater.check(silent=True)   # check on startup (no dialog if up-to-date)
        updater.check(silent=False)  # called from menu: shows "up-to-date" msg
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self._progress_dlg: QProgressDialog | None = None
        self._check_worker: CheckWorker | None = None
        self._latest_tag: str = ""

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, silent: bool = False) -> None:
        """
        Check GitHub for a newer release (non-blocking).

        Args:
            silent: If True, suppress the "already up-to-date" dialog.
                    Use True when checking automatically at startup.
        """
        api_url = _get_api_url()
        if not api_url:
            if not silent:
                QMessageBox.warning(
                    self.parent, "Lỗi cấu hình",
                    "Không tìm thấy 'github_repo' trong config.toml.\n"
                    "Vui lòng thêm vào phần [update]."
                )
            return

        self._check_worker = CheckWorker(api_url)
        self._check_worker.result.connect(lambda data: self._on_check_result(data, silent))
        self._check_worker.failed.connect(lambda msg: self._on_check_failed(msg, silent))
        self._check_worker.start()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_check_result(self, data: dict, silent: bool) -> None:
        latest_tag: str = data.get("tag_name", "")
        if not latest_tag:
            return

        current_ver = _get_version()
        if _v(latest_tag) <= _v(current_ver):
            if not silent:
                QMessageBox.information(
                    self.parent, "Đã là bản mới nhất",
                    f"Bạn đang dùng phiên bản mới nhất ({current_ver})."
                )
            return

        # Find the matching .exe asset
        exe_name = _get_exe_name()
        asset = next(
            (a for a in data.get("assets", []) if a["name"] == exe_name),
            None,
        )
        if not asset:
            # Fallback: take the first .exe asset
            asset = next(
                (a for a in data.get("assets", []) if a["name"].endswith(".exe")),
                None,
            )
        if not asset:
            return

        notes = data.get("body", "").strip()
        ret = QMessageBox.question(
            self.parent,
            "Có bản cập nhật mới",
            f"Phiên bản hiện tại: {current_ver}\n"
            f"Phiên bản mới: {latest_tag}\n\n"
            f"{notes[:500]}\n\nTải và cập nhật ngay?",
        )
        self._latest_tag = latest_tag
        if ret == QMessageBox.Yes:
            self._download(asset["browser_download_url"])

    def _on_check_failed(self, msg: str, silent: bool) -> None:
        if not silent:
            QMessageBox.warning(
                self.parent, "Lỗi",
                f"Không kiểm tra được bản cập nhật:\n{msg}"
            )

    def _download(self, url: str) -> None:
        self._progress_dlg = QProgressDialog(
            "Đang tải bản cập nhật…", "Huỷ", 0, 100, self.parent
        )
        self._progress_dlg.setWindowTitle("Đang cập nhật")
        self._progress_dlg.setMinimumDuration(0)
        self._progress_dlg.canceled.connect(self._on_cancel)

        self._worker = DownloadWorker(url)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_downloaded)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, pct: int) -> None:
        if self._progress_dlg:
            self._progress_dlg.setValue(pct)

    def _on_cancel(self) -> None:
        if hasattr(self, "_worker") and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)
        if self._progress_dlg:
            self._progress_dlg.close()

    def _on_failed(self, msg: str) -> None:
        if self._progress_dlg:
            self._progress_dlg.close()
        QMessageBox.critical(self.parent, "Lỗi tải", f"Không tải được bản cập nhật:\n{msg}")

    def _on_downloaded(self, new_exe_path: str) -> None:
        if self._progress_dlg:
            self._progress_dlg.setValue(100)
            self._progress_dlg.close()
            self._progress_dlg = None

        self._install(new_exe_path)

    def _install(self, new_exe_path: str) -> None:
        """
        Replace the running executable with the downloaded one.

        The trick:
          1. Write a .bat script to a temp directory.
          2. The bat waits 2 s then retries moving the new exe over the current
             one (up to 30 attempts, 1 s apart) until the file lock is released.
          3. Show a notification dialog asking the user to reopen the app manually.
          4. User clicks OK → app closes, bat runs in background and replaces the exe.
        """
        current_exe = _get_current_exe()
        bat_path = Path(tempfile.gettempdir()) / "tool-download-movie-update.bat"
        new_exe = Path(new_exe_path)

        bat_content = (
            '@echo off\n'
            'timeout /t 2 /nobreak >nul\n'
            # Retry loop: wait until the running exe is released (max 30 attempts)
            'set /a retries=0\n'
            ':retry\n'
            f'move /y "{new_exe}" "{current_exe}" >nul 2>&1\n'
            'if errorlevel 1 (\n'
            '    set /a retries+=1\n'
            '    if %retries% geq 10 goto failed\n'
            '    timeout /t 1 /nobreak >nul\n'
            '    goto retry\n'
            ')\n'
            'goto cleanup\n'
            ':failed\n'
            f'echo Failed to replace executable after 10 retries. New version is at: "{new_exe}"\n'
            'pause\n'
            ':cleanup\n'
            'del "%~f0"\n'
        )

        try:
            bat_path.write_text(bat_content, encoding="utf-8")
        except Exception as e:
            QMessageBox.critical(
                self.parent, "Lỗi",
                f"Không tạo được script cài đặt:\n{e}"
            )
            return

        try:
            subprocess.Popen(
                ["cmd", "/c", str(bat_path)],
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                ),
                cwd=str(current_exe.parent),
            )
        except Exception as e:
            QMessageBox.critical(
                self.parent, "Lỗi",
                f"Không chạy được script cài đặt:\n{e}"
            )
            return

        QMessageBox.information(
            self.parent,
            "Cài đặt cập nhật",
            f"Bản cập nhật đã được tải về và đang được cài đặt.\n\n"
            f"Nhấn OK để đóng ứng dụng.\n"
            f"Vui lòng mở lại ứng dụng sau khi cài đặt hoàn tất."
        )
        self.parent.close()
