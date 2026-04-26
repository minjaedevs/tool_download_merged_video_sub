"""
Integration test: _merge_episode with real files.

Input :  ep01.mp4 + ep01.vtt
Output:  merged/ep01_merged.mp4

Checks after merge:
  - Merged file exists and is non-trivial size
  - ep.status == "done", ep.merged_path set
  - Duration of merged == duration of original  (tolerance 0.5 s)
  - ep.merge_note is "ok" or "dur:..." (not "error")
  - ffmpeg stderr warnings are printed for visibility
"""

import sys
import unittest
from pathlib import Path

from PySide6.QtCore import QCoreApplication
_qt_app = QCoreApplication.instance() or QCoreApplication(sys.argv)

# ── add app/ to path ───────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from xemshort import NSEpisode, NSMovie, NSDownloadMergeWorker  # noqa: E402
from xemshort.helpers import _ns_get_video_duration_secs  # noqa: E402

# ── test data paths ────────────────────────────────────────────────────────
_BASE = Path(r"C:\Users\Pc\Downloads\NetShort\Khoa Học Viễn Tưởng VS Kiếm Hiệp Tu Tiên")
VIDEO_PATH  = _BASE / "ep01.mp4"
VTT_PATH    = _BASE / "ep01.vtt"
SAVE_DIR    = _BASE.parent


# ── helpers ────────────────────────────────────────────────────────────────

def _build_worker(movie: NSMovie) -> NSDownloadMergeWorker:
    return NSDownloadMergeWorker(
        movie=movie,
        concurrency=1,
        download_sub=False,
        do_merge=True,
        crf=23,
        preset="fast",
        sub_font="UTM Alter Gothic",
        sub_size=20,
        sub_margin_v=30,
    )


# ── test class ─────────────────────────────────────────────────────────────

class TestMergeEpisode(unittest.TestCase):
    """Integration tests for NSDownloadMergeWorker._merge_episode."""

    # Results stored here so tests are independent of each other
    _merge_ok: bool = False
    _ep: NSEpisode = None
    _orig_secs: float = None
    _merged_secs: float = None
    _stderr: str = ""

    @classmethod
    def setUpClass(cls):
        # ── require input files ──
        if not VIDEO_PATH.exists():
            raise unittest.SkipTest(f"Video not found: {VIDEO_PATH}")
        if not VTT_PATH.exists():
            raise unittest.SkipTest(f"VTT not found: {VTT_PATH}")

        # ── build objects ──
        movie = NSMovie(
            name="Khoa Học Viễn Tưởng VS Kiếm Hiệp Tu Tiên",
            save_dir=SAVE_DIR,
        )
        ep = NSEpisode(
            id="ep01",
            name="Tập 1",
            episode=1,
            play="",
            subtitle_url=None,
            selected=True,
        )
        ep.video_path = VIDEO_PATH
        ep.sub_path   = VTT_PATH
        movie.episodes.append(ep)

        # ── delete existing merged so we always do a fresh merge ──
        merge_dir = SAVE_DIR / movie.folder_name / "merged"
        padding   = len(str(movie.total))
        out_path  = merge_dir / f"ep{str(ep.episode).zfill(padding)}_merged.mp4"
        if out_path.exists():
            out_path.unlink()
            print(f"\n[setup] deleted old merged: {out_path.name}")

        # ── record original duration before merge ──
        cls._orig_secs = _ns_get_video_duration_secs(VIDEO_PATH)
        print(f"[setup] original duration : {cls._orig_secs:.3f}s")

        # ── run merge ──
        worker = _build_worker(movie)

        # capture log messages to stdout
        worker.log_msg.connect(lambda msg: print(f"  [worker] {msg}"))

        cls._merge_ok = worker._merge_episode(ep)
        cls._ep       = ep

        if ep.merged_path and ep.merged_path.exists():
            cls._merged_secs = _ns_get_video_duration_secs(ep.merged_path)
            print(f"[setup] merged  duration : {cls._merged_secs:.3f}s")

        print(f"[setup] merge returned   : {cls._merge_ok}")
        print(f"[setup] ep.status        : {ep.status}")
        print(f"[setup] ep.merge_note    : {ep.merge_note!r}")

    # ── individual checks ──────────────────────────────────────────────────

    def test_01_merge_returns_true(self):
        """_merge_episode must return True."""
        self.assertTrue(self._merge_ok,
                        "merge returned False — check [worker] log above")

    def test_02_merged_path_set(self):
        """ep.merged_path must be set to an existing file."""
        self.assertIsNotNone(self._ep.merged_path,
                             "ep.merged_path is None after merge")
        self.assertTrue(self._ep.merged_path.exists(),
                        f"Merged file missing: {self._ep.merged_path}")

    def test_03_status_done(self):
        """ep.status must be 'done'."""
        self.assertEqual(self._ep.status, "done",
                         f"Got status={self._ep.status!r}, expected 'done'")

    def test_04_merged_file_not_empty(self):
        """Merged file must be > 100 KB."""
        if not (self._ep.merged_path and self._ep.merged_path.exists()):
            self.skipTest("merged_path not set")
        size_kb = self._ep.merged_path.stat().st_size / 1024
        self.assertGreater(size_kb, 100,
                           f"Merged file suspiciously small: {size_kb:.1f} KB")

    def test_05_duration_match(self):
        """Merged duration must equal original within 0.5 s tolerance."""
        self.assertIsNotNone(self._orig_secs,
                             "ffprobe could not read original duration")
        self.assertIsNotNone(self._merged_secs,
                             "ffprobe could not read merged duration")
        diff = abs(self._merged_secs - self._orig_secs)
        self.assertLessEqual(
            diff, 0.5,
            f"Duration mismatch!\n"
            f"  Original : {self._orig_secs:.3f}s\n"
            f"  Merged   : {self._merged_secs:.3f}s\n"
            f"  Diff     : {diff:.3f}s  (max allowed: 0.5 s)"
        )

    def test_06_merge_note_not_error(self):
        """ep.merge_note must not be 'error'."""
        self.assertNotEqual(self._ep.merge_note, "error",
                            "merge_note='error' — merge may have failed silently")

    def test_07_merge_note_duration_warning(self):
        """If merge_note starts with 'dur:' print detail but do NOT fail."""
        note = self._ep.merge_note
        if note.startswith("dur:"):
            print(f"\n  [WARN] duration note: {note} — investigate source file")
        # warn only, not a hard failure (the -t flag should prevent actual truncation)
        self.assertNotEqual(note, "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
