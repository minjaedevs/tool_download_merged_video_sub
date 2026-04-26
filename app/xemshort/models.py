"""XemShort data models: XSEpisode, XSMovie."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _ns_sanitize_filename(name: str) -> str:
    """Strip characters illegal on Windows/Linux filesystems and trim to 200 chars."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:200] if name else "Untitled"


@dataclass
class XSEpisode:
    """Single episode data: URLs, local file paths, and download status."""
    id: str
    name: str
    episode: int
    play: str
    subtitle_url: Optional[str] = None
    selected: bool = True

    video_path: Optional[Path] = None
    sub_path: Optional[Path] = None
    merged_path: Optional[Path] = None
    status: str = "pending"
    error_msg: str = ""
    merge_note: str = ""   # "ok" | "no_sub" | "dur:+Xs" | "error"


@dataclass
class XSMovie:
    """Movie container: holds all episodes and the target save directory."""
    name: str
    episodes: list[XSEpisode] = field(default_factory=list)
    save_dir: Path = field(default_factory=lambda: Path("."))
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    @property
    def selected_count(self) -> int:
        """Number of episodes marked as selected."""
        return sum(1 for e in self.episodes if e.selected)

    @property
    def total(self) -> int:
        """Total number of episodes in this movie."""
        return len(self.episodes)

    @property
    def folder_name(self) -> str:
        """Sanitized movie name safe for use as a directory name."""
        return _ns_sanitize_filename(self.name)


# Backward-compat aliases
NSEpisode = XSEpisode
NSMovie = XSMovie
