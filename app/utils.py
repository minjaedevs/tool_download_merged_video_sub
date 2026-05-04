import sys
from pathlib import Path

import tomlkit
from platformdirs import user_data_dir

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    ROOT = Path(sys._MEIPASS)
else:
    ROOT = Path(__file__).parent

# BIN_DIR: persistent user data (config, logs) - works in both dev and frozen modes
BIN_DIR = Path(user_data_dir("yt-dlp-gui"))


def load_toml(path):
    with open(path, "r", encoding="utf-8") as file:
        return tomlkit.parse(file.read())


def save_toml(path, data):
    with open(path, "w", encoding="utf-8") as file:
        file.write(tomlkit.dumps(data))
