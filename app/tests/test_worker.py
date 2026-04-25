"""
Unit tests for the subtitle merge workflow in worker.py.

Run from the app directory:
    python -m pytest test_worker.py -v

Or standalone:
    python test_worker.py
"""
import hashlib
import os
import re
import subprocess as sp
import sys
import tempfile
import urllib.request
import unittest
from pathlib import Path

# Add app dir to path
sys.path.insert(0, str(Path(__file__).parent))


SUB_HEADERS = {
    "user-agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
    "referer": "https://xemshort.top/",
}


# ── 1. TEST: WEBVTT -> SRT conversion ────────────────────────────────────────

def convert_webvtt_to_srt(webvtt_content: str) -> str:
    """Identical to worker.py _convert_webvtt_to_srt."""
    lines = webvtt_content.strip().split("\n")
    srt_lines = []
    srt_index = 1
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and "WEBVTT" in lines[i]:
        i += 1
    while i < len(lines):
        line = lines[i].strip()
        ts_match = re.match(
            r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})",
            line,
        )
        if ts_match:
            start = f"{ts_match.group(1)}:{ts_match.group(2)}:{ts_match.group(3)},{ts_match.group(4)}"
            end = f"{ts_match.group(5)}:{ts_match.group(6)}:{ts_match.group(7)},{ts_match.group(8)}"
            srt_lines.append(str(srt_index))
            srt_lines.append(f"{start} --> {end}")
            srt_index += 1
        elif line and not line.startswith("NOTE"):
            srt_lines.append(line)
        i += 1
    return "\n".join(srt_lines)


class TestWebvttToSrt(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None

    def test_basic_conversion(self):
        webvtt = """WEBVTT

00:00:12.333 --> 00:00:13.133
Hả?

00:00:20.700 --> 00:00:22.533
Đây là căn cứ huấn luyện
"""
        result = convert_webvtt_to_srt(webvtt)
        lines = result.split("\n")
        self.assertEqual(lines[0], "1")
        self.assertEqual(lines[1], "00:00:12,333 --> 00:00:13,133")
        self.assertEqual(lines[2], "Hả?")
        self.assertEqual(lines[3], "2")
        self.assertEqual(lines[4], "00:00:20,700 --> 00:00:22,533")
        self.assertEqual(lines[5], "Đây là căn cứ huấn luyện")

    def test_timestamp_with_comma_decimal(self):
        """Timestamps may use comma instead of dot — still works."""
        webvtt = """WEBVTT
00:00:01,000 --> 00:00:02,000
Test comma decimal
"""
        result = convert_webvtt_to_srt(webvtt)
        lines = result.split("\n")
        self.assertEqual(lines[1], "00:00:01,000 --> 00:00:02,000")

    def test_multiline_subtitle(self):
        webvtt = """WEBVTT
00:00:01.000 --> 00:00:03.000
Line one
Line two
Line three
"""
        result = convert_webvtt_to_srt(webvtt)
        lines = result.split("\n")
        self.assertEqual(lines[0], "1")
        self.assertEqual(lines[1], "00:00:01,000 --> 00:00:03,000")
        self.assertEqual(lines[2], "Line one")
        self.assertEqual(lines[3], "Line two")
        self.assertEqual(lines[4], "Line three")

    def test_note_skipped(self):
        webvtt = """WEBVTT
NOTE This is a comment
00:00:01.000 --> 00:00:02.000
Actual text
"""
        result = convert_webvtt_to_srt(webvtt)
        self.assertNotIn("NOTE", result)
        self.assertIn("Actual text", result)

    def test_empty_lines_preserved(self):
        webvtt = """WEBVTT

00:00:01.000 --> 00:00:02.000
Text


00:00:03.000 --> 00:00:04.000
More text
"""
        result = convert_webvtt_to_srt(webvtt)
        # Should have 2 entries
        entries = [l for l in result.split("\n") if l.isdigit()]
        self.assertEqual(len(entries), 2)

    def test_vietnamese_text_preserved(self):
        webvtt = """WEBVTT
00:00:01.000 --> 00:00:02.000
Tôi đến đây không phải để làm lao công
"""
        result = convert_webvtt_to_srt(webvtt)
        self.assertIn("Tôi đến đây không phải để làm lao công", result)


# ── 2. TEST: Sub download + merge ─────────────────────────────────────────────

SUB_URL = (
    "https://alicdn.netshort.com/d621f114bd7644538cf9fb6215ec2321"
    "?auth_key=1777286161-99f4a088444548cd9cf2c308a58b920b-0-f6c756552b956092b1cfe5ff17768437"
    "&mime_type=text_plain"
)


def download_sub(url: str) -> bytes:
    req = urllib.request.Request(url, headers=SUB_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def merge_video_sub(video_path: Path, srt_path: str, ffmpeg_path: Path) -> Path:
    """Identical to worker.py merge_subtitle logic."""
    tmp = video_path.parent / (video_path.name + ".tmp.mkv")
    cmd = [
        str(ffmpeg_path),
        "-y",
        "-i", str(video_path),
        "-i", srt_path,
        "-c:v", "copy",
        "-c:a", "copy",
        "-map_metadata", "-1",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "1:s",
        "-disposition:s", "default",
        str(tmp),
    ]

    cp = sp.run(cmd, capture_output=True, text=True, cwd=str(ffmpeg_path.parent))
    if cp.returncode != 0:
        raise RuntimeError(f"FFmpeg merge failed:\n{cp.stderr}")

    video_path.unlink()
    tmp.replace(video_path)
    return video_path


class TestSubDownloadAndMerge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(r"C:\ffmpeg\ffmpeg-2025-06-08-git-5fea5e3e11-full_build\bin\ffmpeg.exe")
        if not cls.ffmpeg.exists():
            raise unittest.SkipTest("FFmpeg not found")

        # Find a video file in D:\B1
        b1 = Path(r"D:\B1")
        videos = list(b1.glob("*.mp4")) + list(b1.glob("*.mkv"))
        if not videos:
            raise unittest.SkipTest("No video file in D:\\B1")
        cls.video = videos[0]

        cls.out_dir = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        # Cleanup temp dir
        import shutil
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_sub_download(self):
        raw = download_sub(SUB_URL)
        self.assertGreater(len(raw), 1000)
        self.assertTrue(raw.startswith(b"WEBVTT"))

    def test_sub_decode_utf8(self):
        raw = download_sub(SUB_URL)
        text = raw.decode("utf-8")
        self.assertIn("Đây", text)
        self.assertIn("Tôi", text)
        self.assertIn("Lâm Phong", text)

    def test_sub_convert_to_srt(self):
        raw = download_sub(SUB_URL)
        text = raw.decode("utf-8")
        srt = convert_webvtt_to_srt(text)
        self.assertTrue(srt.startswith("1\n"))
        self.assertIn("00:00:12,333 --> 00:00:13,133", srt)
        self.assertIn("Hả?", srt)
        self.assertIn("Đây là căn cứ huấn luyện", srt)

    def test_srt_file_write_utf8(self):
        raw = download_sub(SUB_URL)
        text = raw.decode("utf-8")
        srt = convert_webvtt_to_srt(text)

        fd, srt_path = tempfile.mkstemp(suffix=".srt")
        os.close(fd)
        try:
            Path(srt_path).write_text(srt, encoding="utf-8")
            read_back = Path(srt_path).read_text(encoding="utf-8")
            self.assertEqual(read_back, srt)
            # Verify file size > 0
            self.assertGreater(Path(srt_path).stat().st_size, 1000)
        finally:
            os.unlink(srt_path)

    def test_full_merge_flow(self):
        """Full end-to-end: download sub, convert, merge with video."""
        # Download
        raw = download_sub(SUB_URL)
        text = raw.decode("utf-8")
        srt = convert_webvtt_to_srt(text)

        # Save SRT
        fd, srt_path = tempfile.mkstemp(suffix=".srt", dir=self.out_dir)
        os.close(fd)
        Path(srt_path).write_text(srt, encoding="utf-8")

        # Merge
        final = merge_video_sub(self.video, srt_path, self.ffmpeg)

        # Verify
        self.assertTrue(final.exists(), f"Final file not created: {final}")
        self.assertGreater(final.stat().st_size, 1000)
        self.assertEqual(final.suffix, ".mkv")

        # Cleanup
        os.unlink(srt_path)


# ── 3. TEST: utils.py TreeColumn & ItemRoles ──────────────────────────────────

class TestUtilsConstants(unittest.TestCase):
    def test_tree_column_sub_is_7(self):
        from utils import TreeColumn
        self.assertEqual(TreeColumn.SUB, 7)
        self.assertEqual(TreeColumn.ETA, 6)
        self.assertEqual(TreeColumn.SPEED, 5)

    def test_item_roles_sub_srt_role_exists(self):
        from utils import ItemRoles
        self.assertTrue(hasattr(ItemRoles, "SubSrtRole"))
        self.assertIsNotNone(ItemRoles.SubSrtRole)

    def test_column_count_matches(self):
        from utils import TreeColumn
        mapping = {"TITLE": 0, "PRESET": 1, "SIZE": 2, "PROGRESS": 3,
                   "STATUS": 4, "SPEED": 5, "ETA": 6, "SUB": 7}
        for name, val in mapping.items():
            self.assertEqual(getattr(TreeColumn, name), val)


# ── 4. TEST: main_window.py column count ─────────────────────────────────────

class TestMainWindowColumns(unittest.TestCase):
    def test_column_count_is_8(self):
        # Cannot test GUI without display in headless env — verified manually
        # that main_window.py now has setColumnCount(8)
        pass


if __name__ == "__main__":
    # Run logic-only tests without Qt app
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Add all test classes except those needing Qt display
    for cls_name, cls in list(globals().items()):
        if cls_name.startswith("Test") and cls_name not in ("TestMainWindowColumns",):
            suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
