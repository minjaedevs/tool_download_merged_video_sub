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


# Supabase credentials (fallback if env not set)
_DEFAULT_SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
_DEFAULT_SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJtc3huYWpjdWRram10cXNmaG90Iiwicm9sZSI6"
    "InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDUyNDM5NSwiZXhwIjoyMDk2MTAwMzk1fQ.CvLi4fkjjSMbRaeKi85xC_d5MDCCkv2tcz4iuKinOgU"
)


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


def _db_supabase_headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _fetch_episodes_from_db(supabase_url: str, supabase_key: str,
                            slug: str) -> tuple[list[XSEpisode], str] | None:
    """Query Supabase for episodes. Returns (episodes, movie_name) or None if not found."""
    import os
    supabase_key_env = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_key_env:
        return None

    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_episodes"
    headers = _db_supabase_headers(supabase_key_env)
    params = {
        "select": "ep_id,ep_order,ep_title,video_url,sub_url_vi",
        "movie_slug": f"eq.{slug}",
        "order": "ep_order.asc",
    }
    try:
        resp = requests.get(endpoint, headers=headers, params=params, timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return None

        # Get movie name from phimngan_movies
        movie_endpoint = f"{base}/rest/v1/phimngan_movies"
        movie_params = {"select": "name", "slug": f"eq.{slug}", "limit": "1"}
        movie_resp = requests.get(movie_endpoint, headers=headers, params=movie_params, timeout=20)
        movie_name = slug
        if movie_resp.ok:
            movie_rows = movie_resp.json()
            if movie_rows:
                movie_name = movie_rows[0].get("name", slug)

        episodes = [
            XSEpisode(
                id=row["ep_id"],
                name=row.get("ep_title") or movie_name,
                episode=row["ep_order"],
                play=row.get("video_url") or "",
                subtitle_url=row.get("sub_url_vi"),
            )
            for row in rows
        ]
        episodes.sort(key=lambda ep: ep.episode)
        return episodes, movie_name
    except Exception:
        return None


def _upsert_episodes_to_db(supabase_url: str, supabase_key: str,
                           episodes: list, slug: str, movie_name: str) -> None:
    """Upsert episodes to Supabase after a successful API fetch."""
    import os
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not key:
        return
    from datetime import datetime, timezone
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_episodes"
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "movie_slug": slug,
            "movie_name": movie_name,
            "ep_id": ep.id,
            "ep_order": ep.episode,
            "ep_title": ep.name,
            "video_url": ep.play,
            "sub_url_vi": ep.subtitle_url,
            "synced_at": now,
        }
        for ep in episodes
    ]
    try:
        headers = _db_supabase_headers(key)
        resp = requests.post(
            endpoint,
            headers=headers,
            params={"on_conflict": "movie_slug,ep_order"},
            data=json.dumps(rows, ensure_ascii=False),
            timeout=30,
        )
        if not resp.ok:
            import logging
            logging.getLogger(__name__).warning(
                f"Upsert episodes failed for {slug}: {resp.status_code} {resp.text[:100]}"
            )
    except Exception:
        pass


def _fetch_and_parse_from_api(api_url: str, slug: str) -> tuple[list[XSEpisode], str, str]:
    """Call phimngan.tv API and parse episodes. Returns (episodes, movie_name, movie_id)."""
    url = api_url.replace("{movie_id}", slug).replace("{slug}", slug)
    headers = dict(PHIMNGAN_API_HEADERS)
    headers["Referer"] = f"https://phimngan.tv/movies/{slug}"
    headers["Next-Router-State-Tree"] = _phimngan_rsc_state(slug)

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
        raise ValueError("Khong tim thay movie object trong response phimngan.tv.")

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
        raise ValueError(f"Khong tim thay tap nao trong response.\nPreview: {text[:500]}")
    return episodes, movie_name, movie_id


class PhimNganFetchWorker(XSFetchWorker):
    """Fetch phimngan.tv movie — tries Supabase first, falls back to API."""

    def __init__(self, api_url: str, movie_id: str,
                 supabase_url: str = _DEFAULT_SUPABASE_URL,
                 force_api: bool = False):
        super().__init__(api_url, movie_id, headers=PHIMNGAN_API_HEADERS)
        self._supabase_url = supabase_url
        self._force_api = force_api

    def run(self):
        import os
        slug = _phimngan_slug_from_input(self.movie_id)
        if not slug:
            self.error.emit("Vui long nhap slug phimngan.tv.", self.instance_id)
            return

        # 1. In-memory cache — ONLY used when force_api is OFF
        if not self._force_api:
            key = _ns_cache_key("phimngan:rsc", slug)
            cached = _ns_cache_get(key)
            if cached is not None:
                episodes, movie_name = cached
                self.cache_hit.emit(episodes, movie_name, slug, self.instance_id)
                return

        # 2. Supabase DB lookup (skip if force_api is checked)
        if not self._force_api:
            supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or _DEFAULT_SUPABASE_KEY
            if supabase_key:
                db_result = _fetch_episodes_from_db(
                    self._supabase_url, supabase_key, slug
                )
                if db_result is not None:
                    episodes, movie_name = db_result
                    # Cache DB result for next time
                    key = _ns_cache_key("phimngan:rsc", slug)
                    _ns_cache_set(key, episodes, movie_name)
                    self.cache_hit.emit(episodes, movie_name, slug, self.instance_id)
                    return

        # 3. Call API directly (always when force_api=True, or when DB miss)
        api_url = (self.api_url or "").strip() or "https://phimngan.tv/movies/{movie_id}?_rsc=h1khq"
        try:
            episodes, movie_name, _ = _fetch_and_parse_from_api(api_url, slug)
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}", self.instance_id)
            return

        # 4. Save to Supabase for future use (fire-and-forget)
        _ns_cache_set(_ns_cache_key("phimngan:rsc", slug), episodes, movie_name)
        supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or _DEFAULT_SUPABASE_KEY
        if supabase_key:
            _upsert_episodes_to_db(self._supabase_url, supabase_key, episodes, slug, movie_name)

        self.success.emit(episodes, movie_name, slug, self.instance_id)


class PhimNganDownloadMergeWorker(DramaWaveDownloadMergeWorker):
    """Download worker for phimngan.tv HLS URLs."""

    subtitle_error_source = "phimngan"

    def _subtitle_outline(self) -> float:
        # PhimNgan videos are usually vertical HLS encodes; the NetShort outline
        # can render too thin after scaling, so make the burn-in border explicit.
        return 3.8

    def _settings_fingerprint(self) -> dict:
        settings = super()._settings_fingerprint()
        settings["outline"] = self._subtitle_outline()
        settings["style_version"] = 3
        return settings

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

        def _run_ffmpeg(cmd: list[str]) -> bool:
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
                    stderr = result.stderr or ""
                    if "Unrecognized option" in stderr or "Option not found" in stderr:
                        return None  # signal: retry without advanced options
                    self.log(f"ffmpeg tai HLS loi {desc}: {stderr[:500]}")
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
                tmp.unlink(missing_ok=True)
                return False
            except Exception as exc:
                self.log(f"LOI {desc} (HLS): {exc}")
                tmp.unlink(missing_ok=True)
                return False

        cmd_full = [
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

        # Fallback: strip unsupported options for older ffmpeg
        cmd_basic = [
            str(ffmpeg_path),
            "-y",
            "-loglevel", "warning",
            "-headers", headers,
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-i", url,
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(tmp),
        ]

        # First attempt with advanced options
        result = _run_ffmpeg(cmd_full)
        if result is None:
            # Old ffmpeg: retry without -allowed_extensions / -allowed_segment_extensions / -extension_picky
            self.log(f"ffmpeg cu — thu lai khong options HLS nang cao...")
            result = _run_ffmpeg(cmd_basic)

        return bool(result)
