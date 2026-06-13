"""Test one DramaWave episode download/remux/merge pipeline.

Writes outputs to C:\\tmp\\dramawave_episode_test by default.
"""
from __future__ import annotations

import argparse
import subprocess as sp
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests


PLAY_URL = "https://api.xemshort.top/api/dramawave/play.m3u8?episode=hN6UwM9tYGyt6HJ00yhEMQ~~"
SUBTITLE_URL = (
    "https://api.xemshort.top/dramawave/subtitle?"
    "f=https%3A%2F%2Fvideo-v6.mydramawave.com%2Fvt%2Fresource%2F11273%2F"
    "89c8ca4c-d155-494f-86b7-d3285f19ce62.srt"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://xemshort.top",
    "Referer": "https://xemshort.top/",
    "short-source": "dramawave",
}


def run_cmd(cmd: list[str], timeout: int = 120) -> sp.CompletedProcess[str]:
    print("\n$ " + " ".join(f'"{part}"' if " " in part else part for part in cmd))
    return sp.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def print_result(label: str, result: sp.CompletedProcess[str]) -> None:
    print(f"\n[{label}] returncode={result.returncode}")
    if result.stdout.strip():
        print("stdout:")
        print(result.stdout[-2000:])
    if result.stderr.strip():
        print("stderr:")
        print(result.stderr[-4000:])


def fetch_preview(url: str) -> None:
    print(f"\n[http preview] {url}")
    response = requests.get(url, headers=HEADERS, timeout=30)
    print(f"status={response.status_code}")
    print(f"content-type={response.headers.get('content-type')}")
    print(f"final-url={response.url}")
    print(f"bytes={len(response.content)}")
    preview = response.content[:500].decode("utf-8", errors="replace")
    print(preview)
    response.raise_for_status()


def download_playlist(url: str, out_path: Path) -> None:
    print(f"\n[playlist] {url}")
    response = requests.get(url, headers=HEADERS, timeout=30)
    print(f"status={response.status_code}")
    print(f"content-type={response.headers.get('content-type')}")
    print(f"final-url={response.url}")
    print(f"bytes={len(response.content)}")
    response.raise_for_status()
    out_path.write_bytes(response.content)
    print(f"wrote={out_path}")


def inspect_playlist_assets(playlist_path: Path, base_url: str) -> None:
    text = playlist_path.read_text(encoding="utf-8", errors="replace")
    urls: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MAP:") and "URI=" in stripped:
            uri = stripped.split("URI=", 1)[1].strip().strip('"')
            if '",' in uri:
                uri = uri.split('",', 1)[0]
            urls.append(("map", urljoin(base_url, uri)))
        elif stripped and not stripped.startswith("#"):
            urls.append(("segment", urljoin(base_url, stripped)))
        if len(urls) >= 2:
            break

    for label, url in urls:
        print(f"\n[{label} preview] {url}")
        response = requests.get(url, headers=HEADERS, timeout=30)
        print(f"status={response.status_code}")
        print(f"content-type={response.headers.get('content-type')}")
        print(f"bytes={len(response.content)}")
        print(f"head16={response.content[:16].hex(' ')}")
        print(response.content[:120].decode("utf-8", errors="replace"))
        response.raise_for_status()


def download_subtitle(url: str, out_path: Path) -> None:
    print(f"\n[subtitle] {url}")
    response = requests.get(url, headers=HEADERS, timeout=30)
    print(f"status={response.status_code}")
    print(f"content-type={response.headers.get('content-type')}")
    print(f"bytes={len(response.content)}")
    response.raise_for_status()
    out_path.write_bytes(response.content)
    print(f"wrote={out_path}")
    preview = response.content[:240].decode("utf-8", errors="replace")
    print(preview)
    lowered = preview.lower()
    if "<html" in lowered or "<!doctype" in lowered:
        raise ValueError("Subtitle endpoint returned HTML, not an SRT/VTT subtitle.")


def escape_subtitle_filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test DramaWave episode HLS/remux/merge.")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--out-dir", default=r"C:\tmp\dramawave_episode_test")
    parser.add_argument("--play-url", default=PLAY_URL)
    parser.add_argument("--subtitle-url", default=SUBTITLE_URL)
    args = parser.parse_args()

    play_url = args.play_url
    subtitle_url = args.subtitle_url

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = out_dir / "ep01.m3u8"
    sub_path = out_dir / "ep01.srt"
    video_path = out_dir / "ep01.mp4"
    merged_path = out_dir / "ep01_merged.mp4"

    fetch_preview(play_url)
    download_playlist(play_url, playlist_path)
    inspect_playlist_assets(playlist_path, play_url)
    download_subtitle(subtitle_url, sub_path)

    remux_cmd = [
        args.ffmpeg,
        "-y",
        "-loglevel", "verbose",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
        "-allowed_extensions", "ALL",
        "-allowed_segment_extensions", "ALL",
        "-extension_picky", "0",
        "-i", str(playlist_path),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(video_path),
    ]
    result = run_cmd(remux_cmd, timeout=300)
    print_result("ffmpeg remux", result)
    if result.returncode != 0:
        return result.returncode

    probe_cmd = [
        args.ffprobe,
        "-v", "error",
        "-show_entries", "format=duration,size",
        "-of", "default=noprint_wrappers=1",
        str(video_path),
    ]
    result = run_cmd(probe_cmd, timeout=60)
    print_result("ffprobe mp4", result)
    if result.returncode != 0:
        return result.returncode

    print(f"\n[merge] start burning subtitle: {sub_path} -> {merged_path}")
    merge_cmd = [
        args.ffmpeg,
        "-y",
        "-loglevel", "warning",
        "-i", str(video_path),
        "-vf", f"subtitles='{escape_subtitle_filter_path(sub_path)}'",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "copy",
        str(merged_path),
    ]
    result = run_cmd(merge_cmd, timeout=600)
    print_result("ffmpeg merge sub", result)
    if result.returncode != 0:
        return result.returncode
    if not merged_path.exists() or merged_path.stat().st_size <= 1024:
        print(f"\n[merge] output missing or too small: {merged_path}")
        return 1

    print(f"\nOK video={video_path}")
    print(f"OK merged={merged_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
