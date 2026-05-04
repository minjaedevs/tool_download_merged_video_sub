import os
import platform
import shutil
import stat
import sys
import zipfile
from typing import List, Tuple
import logging


import requests
from PySide6.QtCore import QThread, Signal

from utils import ROOT, BIN_DIR
import subprocess as sp

logger = logging.getLogger(__name__)

os.environ["PATH"] += os.pathsep + str(BIN_DIR)
os.environ["PATH"] += os.pathsep + str(ROOT / "bin")  # old version compatibility

BINARIES = {
    "Linux": {
        "ffmpeg": "ffmpeg-linux64-v4.1",
        "ffprobe": "ffprobe-linux64-v4.1",
        "deno": "deno-x86_64-unknown-linux-gnu.zip",
    },
    "Darwin": {
        "ffmpeg": "ffmpeg-osx64-v4.1",
        "ffprobe": "ffprobe-osx64-v4.1",
        "deno": "deno-x86_64-apple-darwin.zip",
    },
    "Windows": {
        "ffmpeg": "ffmpeg-win64-v4.1.exe",
        "ffprobe": "ffprobe-win64-v4.1.exe",
        "deno": "deno-x86_64-pc-windows-msvc.zip",
    },
}

FFMPEG_BASE_URL = "https://github.com/imageio/imageio-binaries/raw/183aef992339cc5a463528c75dd298db15fd346f/ffmpeg/"
DENO_BASE_URL = "https://github.com/denoland/deno/releases/latest/download/"


class DepWorker(QThread):
    progress = Signal(str)

    def __init__(self):
        super().__init__()
        self.missing: List[Tuple[str, str]] = []

    def _check_missing_dependencies(self):
        system_os = platform.system()
        if system_os not in BINARIES:
            return

        required_binaries = ["ffmpeg", "ffprobe", "deno"]
        missing_exes = [
            exe
            for exe in required_binaries
            if not shutil.which(exe)
            and not (
                BIN_DIR / (exe + (".exe" if system_os == "Windows" else ""))
            ).exists()
        ]

        if not missing_exes:
            return

        BIN_DIR.mkdir(parents=True, exist_ok=True)

        for exe in missing_exes:
            binary_name = BINARIES[system_os][exe]

            if exe == "deno":
                url = DENO_BASE_URL + binary_name
            else:
                url = FFMPEG_BASE_URL + binary_name

            target_name = f"{exe}.exe" if system_os == "Windows" else exe
            target_path = str(BIN_DIR / target_name)

            self.missing.append((url, target_path))

    def chmod(self):
        try:
            st = os.stat(self.filename)
            os.chmod(self.filename, st.st_mode | stat.S_IEXEC)
        except OSError:
            pass

    def run(self):
        self._check_missing_dependencies()

        if self.missing:
            for missingexe in self.missing:
                self.url, self.filename = missingexe
                self._download()
                self.chmod()

        self.finished.emit()

    def _download(self):
        response = requests.get(self.url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        block_size = 8192
        downloaded = 0

        display_name = os.path.basename(self.filename)
        temp_filename = self.filename + ".part"

        with open(temp_filename, "wb") as f:
            for chunk in response.iter_content(chunk_size=block_size):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                if total_size > 0:
                    percentage = int((downloaded / total_size) * 100)
                    dl_mb = downloaded / (1024 * 1024)
                    tot_mb = total_size / (1024 * 1024)
                    status = f"Downloading dependency: ({display_name}) {dl_mb:.2f} / {tot_mb:.2f} MB {percentage}%"
                else:
                    percentage = 0
                    dl_mb = downloaded / (1024 * 1024)
                    status = f"{display_name}: {dl_mb:.2f} MB"

                self.progress.emit(status)

        if self.url.endswith(".zip"):
            try:
                zip_filename = temp_filename + ".zip"
                shutil.move(temp_filename, zip_filename)

                with zipfile.ZipFile(zip_filename, "r") as zf:
                    target_filename = zf.namelist()[0]
                    with zf.open(target_filename) as source, open(
                        self.filename, "wb"
                    ) as target:
                        shutil.copyfileobj(source, target)

                if os.path.exists(zip_filename):
                    os.remove(zip_filename)
                return
            except Exception as e:
                if os.path.exists(zip_filename):
                    os.remove(zip_filename)
                raise e

        if os.path.exists(self.filename):
            os.remove(self.filename)
        os.rename(temp_filename, self.filename)
