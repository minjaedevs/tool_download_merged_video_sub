"""M3U8 Tab data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class M3U8Item:
    """Single M3U8 download item."""
    id: int
    url: str
    name: str
    save_dir: Path
    fmt: str = "mp4"          # "mp4" | "m3u8"
    status: str = "pending"   # pending | downloading | done | error | stopped
    error_msg: str = ""
    progress: float = 0.0     # 0.0 - 100.0
    speed: str = ""           # e.g. "1.2 MiB/s"
    eta: str = ""             # e.g. "00:30"
    output_path: Optional[Path] = None
    instance_id: int = 0      # for ignoring stale worker signals

    @property
    def is_running(self) -> bool:
        return self.status == "downloading"

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    @property
    def is_error(self) -> bool:
        return self.status == "error"
