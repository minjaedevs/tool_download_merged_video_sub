"""phimngan.tv source-specific fetch and HLS download workers."""
from __future__ import annotations

import json
import platform
import re
import subprocess as sp
from pathlib import Path
from urllib.parse import quote

import requests

from xemshort.cache import _ns_cache_get, _ns_cache_key, _ns_cache_set
from xemshort.helpers import _ns_get_video_duration_secs
from xemshort.models import XSEpisode
from xemshort.workers import DramaWaveDownloadMergeWorker, XSFetchWorker


PHIMNGAN_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "RSC": "1",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _phimngan_slug_from_input(value: str) -> str:
    """Extract phimngan.tv movie slug from a slug or movie URL."""
    value = (value or "").strip()
    if not value:
        return ""
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    match = re.search(r"/movies/([^/]+)", value)
    if match:
        return match.group(1).strip()
    return value.rsplit("/", 1)[-1].strip()


def _phimngan_rsc_state(slug: str) -> str:
    """Build the encoded Next Router state tree for /movies/{slug}."""
    path = f"/movies/{slug}"
    state = [
        "",
        {
            "children": [
                "(root)",
                {
                    "children": [
                        "movies",
                        {
                            "children": [
                                ["id", slug, "c"],
                                {"children": ["__PAGE__", {}, path, "refresh"]},
                                None,
                                "refetch",
                            ]
                        },
                    ]
                },
            ]
        },
    ]
    return quote(json.dumps(state, separators=(",", ":")), safe="")


class PhimNganFetchWorker(XSFetchWorker):
    """Fetch phimngan.tv movie RSC payload and parse episodes for the downloader UI."""

    def __init__(self, api_url: str, movie_id: str):
        super().__init__(api_url, movie_id, headers=PHIMNGAN_API_HEADERS)

    def run(self):
        slug = _phimngan_slug_from_input(self.movie_id)
        if not slug:
            self.error.emit("Vui long nhap slug phimngan.tv.", self.instance_id)
            return

        key = _ns_cache_key("phimngan:rsc", slug)
        cached = _ns_cache_get(key)
        if cached is not None:
            episodes, movie_name = cached
            self.cache_hit.emit(episodes, movie_name, slug, self.instance_id)
            return

        api_url = (self.api_url or "").strip() or "https://phimngan.tv/movies/{movie_id}?_rsc=h1khq"
        url = api_url.replace("{movie_id}", slug).replace("{slug}", slug)
        headers = dict(PHIMNGAN_API_HEADERS)
        headers["Referer"] = f"https://phimngan.tv/movies/{slug}"
        headers["Next-Router-State-Tree"] = _phimngan_rsc_state(slug)

        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            text = response.content.decode("utf-8", errors="replace")

            movie_match = re.search(
                r'"movie":\{"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]+)",'
                r'"slug":"(?P<slug>[^"]+)".*?"cover":"(?P<cover>[^"]+)",'
                r'"episodeCount":(?P<episode_count>\d+),"totalEpisode":(?P<total_episode>\d+)',
                text,
                re.DOTALL,
            )
            if not movie_match:
                self.error.emit("Khong tim thay movie object trong response phimngan.tv.", self.instance_id)
                return

            movie_id = movie_match.group("id")
            movie_name = movie_match.group("title")
            episode_pattern = re.compile(
                r'"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]*)","order":(?P<order>\d+),'
                r'"description":(?:null|"(?:\\.|[^"])*"),"videoUrl":"(?P<video>[^"]+)",'
                r'"cover":"(?P<cover>[^"]+)","duration":(?P<duration>\d+),'
                r'"subtitleList":(?P<subtitle>\[[\s\S]*?\]|"\$[^"]+")',
                re.DOTALL,
            )

            seen: set[int] = set()
            episodes: list[XSEpisode] = []
            for match in episode_pattern.finditer(text):
                order = int(match.group("order"))
                if order in seen:
                    continue
                seen.add(order)
                ep_id = match.group("id")
                title = match.group("title") or movie_name
                vi_sub = f"https://cdn.phimngan.xyz/subtitles/{movie_id}/{ep_id}/vi-VN.vtt"
                episodes.append(
                    XSEpisode(
                        id=ep_id,
                        name=title,
                        episode=order,
                        play=match.group("video"),
                        subtitle_url=vi_sub,
                    )
                )

            episodes.sort(key=lambda ep: ep.episode)
            if not episodes:
                preview = text[:500]
                self.error.emit(f"Khong tim thay tap nao trong response phimngan.tv.\nPreview: {preview}", self.instance_id)
                return

            _ns_cache_set(key, episodes, movie_name)
            self.success.emit(episodes, movie_name, slug, self.instance_id)
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}", self.instance_id)


class PhimNganDownloadMergeWorker(DramaWaveDownloadMergeWorker):
    """Download worker for phimngan.tv HLS URLs."""

    subtitle_error_source = "phimngan"

    def _download_hls_video(self, url: str, output: Path, desc: str) -> bool:
        """Let ffmpeg read the remote playlist so absolute-relative CDN paths resolve."""
        if output.exists() and output.stat().st_size > 1024:
            try:
                if _ns_get_video_duration_secs(output) and _ns_get_video_duration_secs(output) > 0:
                    self.log(f"SKIP {desc} (da ton tai)")
                    return True
            except Exception:
                pass
            try:
                output.unlink()
                self.log(f"xoa file video hong: {output.name}")
            except Exception as exc:
                self.log(f"khong xoa duoc file video hong {output.name}: {exc}")
                return False

        ffmpeg_path = self._get_ffmpeg_path()
        if not ffmpeg_path:
            self.log(f"{desc}: can ffmpeg de tai HLS/m3u")
            return False

        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_suffix(output.suffix + ".part.mp4")
        tmp.unlink(missing_ok=True)

        headers = (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36\r\n"
            "Referer: https://phimngan.tv/\r\n"
            "Origin: https://phimngan.tv\r\n"
        )
        cmd = [
            str(ffmpeg_path),
            "-y",
            "-loglevel", "warning",
            "-headers", headers,
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-allowed_extensions", "ALL",
            "-allowed_segment_extensions", "ALL",
            "-extension_picky", "0",
            "-i", url,
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(tmp),
        ]
        try:
            _cflags = {"creationflags": sp.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}
            result = sp.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                **_cflags,
            )
            if result.returncode != 0:
                self.log(f"ffmpeg tai HLS loi {desc}: {result.stderr[:500]}")
                tmp.unlink(missing_ok=True)
                return False
            if not tmp.exists() or tmp.stat().st_size <= 1024:
                self.log(f"ffmpeg tai HLS loi {desc}: output rong")
                tmp.unlink(missing_ok=True)
                return False
            tmp.replace(output)
            self.log(f"{desc} OK (HLS -> MP4, {output.stat().st_size} bytes)")
            return True
        except sp.TimeoutExpired:
            self.log(f"TIMEOUT {desc} (HLS)")
        except Exception as exc:
            self.log(f"LOI {desc} (HLS): {exc}")
        tmp.unlink(missing_ok=True)
        return False
