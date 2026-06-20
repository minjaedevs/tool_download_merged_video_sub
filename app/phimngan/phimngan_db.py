"""PhimNgan DB operations: Supabase read/write cho phimngan_movies & phimngan_episodes."""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
SUPABASE_URL = "https://rmsxnajcudkjmtqsfhot.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJtc3huYWpjdWRram10cXNmaG90Iiwicm9sZSI6"
    "InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDUyNDM5NSwiZXhwIjoyMDk2MTAwMzk1fQ.CvLi4fkjjSMbRaeKi85xC_d5MDCCkv2tcz4iuKinOgU"
)

LIST_API_URL = "https://phimngan.tv/movies"
DETAIL_API_URL = "https://phimngan.tv/movies/{slug}?_rsc=h1khq"

LIST_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "RSC": "1",
    "Referer": "https://phimngan.tv/movies",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
}

# RSC state tree cho list page
_LIST_RSC_STATE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(root)%22%2C%7B%22children%22%3A%5B%22movies%22%2C"
    "%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fmovies%22%2C%22refresh%22%5D%7D%"
    "2Cnull%2C%22refetch%22%5D%7D%5D%7D%5D"
)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _plain_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


def _ascii_text(value: Any) -> str:
    text = _plain_text(value).replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _parse_view_count(text: str) -> int | None:
    """Parse '1.3m', '912.8k', '864.2k' → int."""
    text = _ascii_text(text).strip()
    m = re.search(r"([\d.]+)\s*(m|k|M|K)?", text)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("m", "M"):
        return int(num * 1_000_000)
    if unit in ("k", "K"):
        return int(num * 1_000)
    return int(num)


def _parse_date(text: str) -> datetime.date | None:
    """Parse '04/07/2025' → date."""
    text = _plain_text(text).strip()
    for fmt in ("%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _parse_episode_count(text: str) -> tuple[int, int]:
    """Parse '80/80 tập' → (80, 80)."""
    text = _ascii_text(text)
    digits = re.findall(r"\d+", text)
    if len(digits) >= 2:
        return int(digits[0]), int(digits[1])
    if len(digits) == 1:
        return int(digits[0]), int(digits[0])
    return 0, 0


def _supabase_headers(supabase_key: str, prefer: str = "return=minimal") -> dict[str, str]:
    return {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


# ─────────────────────────────────────────────────────────────────
# Normalize movie from list page HTML
# ─────────────────────────────────────────────────────────────────

_MOVIE_RSC_RE = re.compile(
    r'\{"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]*)","slug":"(?P<slug>[^"]+)",'
    r'"description":"(?P<description>[^"]*)","tags":"(?P<tags>[^"]*)","cover":"(?P<cover>[^"]+)",'
    r'"episodeCount":(?P<episode_count>\d+),'
    r'"totalEpisode":(?P<total_episode>\d+),'
    r'"viewCount":(?P<view_count>\d+),"favoriteCount":(?P<favorite_count>\d+),'
    r'"isHot":(?P<is_hot>true|false),"isFeatured":(?P<is_featured>true|false)\}',
    re.DOTALL
)


def _normalize_movie(item: dict[str, Any], source_page: int | None) -> dict[str, Any] | None:
    """Normalize a movie dict parsed from the HTML JSON into a DB row."""
    # slug must be kept RAW — it's used as a URL path and must not be normalized
    slug = str(item.get("slug") or item.get("id") or "").strip()
    # name gets normalized for display/search
    name = _plain_text(item.get("title"))
    if not slug or not name:
        return None

    episode_count = int(item.get("episodeCount") or 0)
    total_episode = int(item.get("totalEpisode") or 0)

    view_count = int(item.get("viewCount") or 0)
    favorite_count = int(item.get("favoriteCount") or 0)
    comment_count = int(item.get("commentCount") or 0)

    is_hot = bool(item.get("isHot"))
    is_featured = bool(item.get("isFeatured"))

    is_voice = False  # not in list page

    updated_raw = _plain_text(item.get("updatedAt") or "")
    updated_date = _parse_date(updated_raw)

    thumbnail = _plain_text(item.get("cover") or "")
    description = _plain_text(item.get("description") or "")
    tags = _plain_text(item.get("tags") or "")

    search_text = _plain_text(" ".join(p for p in [name, description, tags] if p))
    search_text_ascii = _ascii_text(search_text)

    ep_text = f"{episode_count}/{total_episode} tap" if (episode_count or total_episode) else ""

    return {
        "slug": slug,
        "name": name,
        "thumbnail": thumbnail or None,
        "episode_count": episode_count or None,
        "total_episode": total_episode or None,
        "episode_count_text": ep_text or None,
        "views": view_count or None,
        "likes": favorite_count or None,
        "comments": comment_count or None,
        "is_hot": is_hot,
        "is_featured": is_featured,
        "is_voice": is_voice,
        "category": tags or None,
        "updated_date": updated_date.isoformat() if updated_date else None,
        "source_page": source_page,
        "search_text": search_text,
        "search_text_ascii": search_text_ascii,
        "list_page_raw": item,
        "last_seen_at": _now_iso(),
        "synced_at": _now_iso(),
        "updated_at_ts": _now_iso(),
    }


# ─────────────────────────────────────────────────────────────────
# Normalize episode from detail page HTML
# ─────────────────────────────────────────────────────────────────

_EPISODE_RSC_RE = re.compile(
    r'"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]*)","order":(?P<order>\d+),'
    r'"description":(?:null|"(?:\\.|[^"])*"),"videoUrl":"(?P<video>[^"]+)",'
    r'"cover":"(?P<cover>[^"]+)","duration":(?P<duration>\d+),'
    r'"subtitleList":(?P<subtitle>\[[\s\S]*?\][^,}]*|"\$[^"]+")',
    re.DOTALL,
)


def _normalize_episode(
    item: dict[str, Any],
    movie_slug: str,
    movie_name: str,
) -> dict[str, Any] | None:
    """Normalize an episode dict from detail page RSC into a DB row."""
    ep_id = _plain_text(item.get("id"))
    ep_order = int(item.get("order") or 0)
    ep_title = _plain_text(item.get("title") or "")
    video_url = _plain_text(item.get("videoUrl") or item.get("video_url") or "")
    cover_url = _plain_text(item.get("cover") or "")
    duration = int(item.get("duration") or 0)

    # Hard-code Vietnamese subtitle URL
    sub_url_vi = f"https://cdn.phimngan.xyz/subtitles/{movie_slug}/{ep_id}/vi-VN.vtt"

    return {
        "movie_slug": movie_slug,
        "movie_name": movie_name,
        "ep_id": ep_id,
        "ep_order": ep_order,
        "ep_title": ep_title or None,
        "video_url": video_url or None,
        "sub_url_vi": sub_url_vi,
        "cover_url": cover_url or None,
        "duration_secs": duration or None,
        "raw": item,
        "synced_at": _now_iso(),
    }


# ─────────────────────────────────────────────────────────────────
# Fetch list page HTML & parse movies
# ─────────────────────────────────────────────────────────────────

def _build_list_rsc_state(page: int) -> str:
    """Build Next-Router-State-Tree for /movies?page=N."""
    if page == 1:
        return _LIST_RSC_STATE
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
                                f"__PAGE__?{{\\\"page\\\":\\\"{page}\\\"}}",
                                {},
                                f"/movies?page={page}",
                                "refetch",
                            ]
                        },
                    ]
                },
            ]
        },
    ]
    import json as _json
    from urllib.parse import quote
    return quote(_json.dumps(state, separators=(",", ":")), safe="")


def _fetch_list_page(session: requests.Session, page: int, timeout: int = 30) -> str:
    """Fetch /movies?page=N and return raw text."""
    params = {"_rsc": "elvm9"} if page > 1 else {"_rsc": "f0wvx"}
    if page > 1:
        params["page"] = str(page)
    headers = dict(LIST_HEADERS)
    headers["Referer"] = "https://phimngan.tv/movies"
    headers["Next-Router-State-Tree"] = _build_list_rsc_state(page)
    response = session.get(LIST_API_URL, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    return response.content.decode("utf-8", errors="replace")


def _parse_movies_from_html(html: str) -> list[dict[str, Any]]:
    """Parse all movie objects from HTML.

    Each movie is a flat JSON object extracted by brace-counting,
    then parsed with json.loads() to handle escaped quotes in description.
    """
    movies: list[dict[str, Any]] = []
    seen: set[str] = set()

    i = 0
    while i < len(html):
        # Find start of a movie object: {"id":"
        start = html.find('{"id":"', i)
        if start < 0:
            break

        # Count braces to find the matching closing }
        depth = 0
        obj_end = -1
        for j in range(start, len(html)):
            ch = html[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_end = j
                    break

        if obj_end < 0:
            break

        raw_str = html[start:obj_end + 1]
        try:
            raw = json.loads(raw_str)
        except (json.JSONDecodeError, ValueError):
            i = obj_end + 1
            continue

        if not isinstance(raw, dict):
            i = obj_end + 1
            continue

        normalized = _normalize_movie(raw, source_page=None)
        if not normalized:
            i = obj_end + 1
            continue

        slug = normalized["slug"]
        if slug in seen:
            i = obj_end + 1
            continue
        seen.add(slug)
        movies.append(normalized)
        i = obj_end + 1

    return movies


def fetch_movies_page(session: requests.Session | None, page: int,
                      timeout: int = 30) -> tuple[list[dict[str, Any]], bool]:
    """Fetch one page of movies. Returns (movies, has_more)."""
    if session is None:
        session = requests.Session()
    html = _fetch_list_page(session, page, timeout)
    movies = _parse_movies_from_html(html)
    # has_more is always True when movies exist — caller stops on empty page
    return movies, bool(movies)


# ─────────────────────────────────────────────────────────────────
# Fetch detail page HTML & parse episodes
# ─────────────────────────────────────────────────────────────────

_DETAIL_RSC_STATE_FMT = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%7B%22children%22%3A%5B%22(root)%22%2C"
    "%7B%22children%22%3A%5B%22movies%22%2C%7B%22children%22%3A%5B"
    "%5B%22id%22%2C%22{SLUG}%22%2C%22c%22%5D%2C"
    "%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fmovies%2F{SLUG}%22%2C%22refresh%22%5D%7D%2C"
    "null%2C%22refetch%22%5D%7D%5D%7D%5D%5D"
)


def _build_detail_rsc_state(slug: str) -> str:
    slug_enc = slug.replace("\\", "\\\\").replace('"', '\\"')
    return _DETAIL_RSC_STATE_FMT.replace("{SLUG}", slug_enc)


def _fetch_detail_page(session: requests.Session, slug: str, timeout: int = 30) -> str:
    """Fetch /movies/{slug}?_rsc=xxx and return raw text."""
    url = DETAIL_API_URL.replace("{slug}", slug)
    headers = dict(LIST_HEADERS)
    headers["Referer"] = f"https://phimngan.tv/movies/{slug}"
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.content.decode("utf-8", errors="replace")


def parse_episodes_from_html(html: str, movie_slug: str, movie_name: str,
                            movie_id: str) -> list[dict[str, Any]]:
    """Parse all episode objects from detail page RSC HTML."""
    episodes: list[dict[str, Any]] = []
    seen: set[int] = set()
    for m in _EPISODE_RSC_RE.finditer(html):
        order = int(m.group("order"))
        if order in seen:
            continue
        seen.add(order)
        item = {
            "id": m.group("id"),
            "title": m.group("title"),
            "order": order,
            "videoUrl": m.group("video"),
            "cover": m.group("cover"),
            "duration": int(m.group("duration")),
            "subtitleList": m.group("subtitle"),
        }
        normalized = _normalize_episode(item, movie_slug, movie_name)
        if normalized:
            normalized["sub_url_vi"] = (
                f"https://cdn.phimngan.xyz/subtitles/{movie_id}/{normalized['ep_id']}/vi-VN.vtt"
            )
            episodes.append(normalized)
    episodes.sort(key=lambda e: e["ep_order"])
    return episodes


# Alias for backward compat (used by sync script)
parse_episodes_from_html_public = parse_episodes_from_html


# ─────────────────────────────────────────────────────────────────
# Supabase read/write
# ─────────────────────────────────────────────────────────────────

def upsert_movies(
    movies: list[dict[str, Any]],
    supabase_url: str,
    supabase_key: str,
    batch_size: int = 500,
    log_fn=None,
) -> tuple[int, int]:
    """Upsert movies into phimngan_movies. Returns (upserted, total)."""
    if not movies:
        return 0, 0
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_movies"
    headers = _supabase_headers(supabase_key, prefer="resolution=merge-duplicates,return=minimal")

    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    total = 0
    for offset in range(0, len(movies), batch_size):
        batch = movies[offset:offset + batch_size]
        response = requests.post(
            endpoint,
            headers=headers,
            params={"on_conflict": "slug"},
            data=json.dumps(batch, ensure_ascii=False),
            timeout=60,
        )
        if response.status_code >= 400:
            _log(f"Upsert movies batch error {response.status_code}: {response.text[:200]}")
        else:
            total += len(batch)
        _log(f"Upserted movies batch: {total}/{len(movies)}")
    return total, len(movies)


def upsert_episodes(
    episodes: list[dict[str, Any]],
    supabase_url: str,
    supabase_key: str,
    batch_size: int = 500,
    log_fn=None,
) -> tuple[int, int]:
    """Upsert episodes into phimngan_episodes. Returns (upserted, total)."""
    if not episodes:
        return 0, 0
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_episodes"
    headers = _supabase_headers(supabase_key, prefer="resolution=merge-duplicates,return=minimal")

    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    total = 0
    for offset in range(0, len(episodes), batch_size):
        batch = episodes[offset:offset + batch_size]
        response = requests.post(
            endpoint,
            headers=headers,
            params={"on_conflict": "movie_slug,ep_order"},
            data=json.dumps(batch, ensure_ascii=False),
            timeout=60,
        )
        if response.status_code >= 400:
            _log(f"Upsert episodes batch error {response.status_code}: {response.text[:200]}")
        else:
            total += len(batch)
        _log(f"Upserted episodes batch: {total}/{len(episodes)}")
    return total, len(episodes)


def count_movies(supabase_url: str, supabase_key: str) -> int:
    """Return total movie count."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_movies"
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "count=exact"
    response = requests.get(endpoint, headers=headers, params={"select": "id", "limit": "1"}, timeout=60)
    if response.status_code == 404:
        return 0
    response.raise_for_status()
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1]
        if total.isdigit():
            return int(total)
    rows = response.json()
    return len(rows) if isinstance(rows, list) else 0


def count_episodes(supabase_url: str, supabase_key: str) -> int:
    """Return total episode count across all movies."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_episodes"
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "count=exact"
    response = requests.get(endpoint, headers=headers, params={"select": "id", "limit": "1"}, timeout=60)
    if response.status_code == 404:
        return 0
    response.raise_for_status()
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1]
        if total.isdigit():
            return int(total)
    rows = response.json()
    return len(rows) if isinstance(rows, list) else 0


def delete_episodes_for_slug(
    supabase_url: str,
    supabase_key: str,
    slug: str,
) -> int:
    """Delete all episodes for a movie slug. Returns deleted count."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_episodes"
    headers = _supabase_headers(supabase_key, prefer="return=representation")
    params = {"movie_slug": f"eq.{slug}"}
    response = requests.delete(endpoint, headers=headers, params=params, timeout=30)
    if response.status_code == 404:
        return 0
    response.raise_for_status()
    if response.text and response.text != "[]":
        try:
            deleted = response.json()
            return len(deleted) if isinstance(deleted, list) else 0
        except Exception:
            pass
    return 0


def fetch_existing_slugs(
    supabase_url: str,
    supabase_key: str,
    page_size: int = 1000,
) -> set[str]:
    """Read all existing slugs to skip already-synced movies."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_movies"
    headers = _supabase_headers(supabase_key)
    existing: set[str] = set()
    offset = 0
    while True:
        params = {
            "select": "slug",
            "order": "id.asc",
            "limit": str(page_size),
            "offset": str(offset),
        }
        response = requests.get(endpoint, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            break
        for row in rows:
            if isinstance(row, dict) and row.get("slug"):
                existing.add(str(row["slug"]))
        if len(rows) < page_size:
            break
        offset += page_size
    return existing


def get_episode_count(
    supabase_url: str,
    supabase_key: str,
    movie_slug: str,
) -> int:
    """Return episode count for a movie from DB."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_episodes"
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "count=exact"
    params = {
        "select": "id",
        "movie_slug": f"eq.{movie_slug}",
        "limit": "1",
    }
    response = requests.get(endpoint, headers=headers, params=params, timeout=30)
    if response.status_code == 404:
        return 0
    response.raise_for_status()
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1]
        if total.isdigit():
            return int(total)
    return len(response.json())


def fetch_episodes_from_db(
    supabase_url: str,
    supabase_key: str,
    movie_slug: str,
) -> list[dict[str, Any]]:
    """Fetch all episodes for a movie from DB, ordered by ep_order."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_episodes"
    headers = _supabase_headers(supabase_key)
    params = {
        "select": "ep_id,ep_order,ep_title,video_url,sub_url_vi,cover_url,duration_secs",
        "movie_slug": f"eq.{movie_slug}",
        "order": "ep_order.asc",
    }
    response = requests.get(endpoint, headers=headers, params=params, timeout=30)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def fetch_movie_from_db(
    supabase_url: str,
    supabase_key: str,
    slug: str,
) -> dict[str, Any] | None:
    """Fetch a single movie by slug from DB."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_movies"
    headers = _supabase_headers(supabase_key)
    params = {"select": "*", "slug": f"eq.{slug}", "limit": "1"}
    response = requests.get(endpoint, headers=headers, params=params, timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    return data[0] if isinstance(data, list) and data else None


def search_movies_db(
    supabase_url: str,
    supabase_key: str,
    query: str = "",
    page: int = 1,
    page_size: int = 24,
) -> tuple[list[dict[str, Any]], int]:
    """Search movies in phimngan_movies with pagination."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/phimngan_movies"
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "count=exact"
    offset = (max(1, page) - 1) * max(1, min(100, page_size))
    params = {
        "select": (
            "slug,name,thumbnail,episode_count,total_episode,"
            "is_hot,is_featured,is_voice,category,views,updated_date,"
            "search_text,search_text_ascii,synced_at"
        ),
        "order": "views.desc",
        "limit": str(max(1, min(100, page_size))),
        "offset": str(offset),
    }
    text = _ascii_text(query)
    if text:
        params["search_text_ascii"] = f"ilike.*{text}*"
    response = requests.get(endpoint, headers=headers, params=params, timeout=60)
    if response.status_code == 400 and "search_text_ascii" in response.text:
        params.pop("search_text_ascii", None)
        if text:
            params["search_text"] = f"ilike.*{_plain_text(query)}*"
        response = requests.get(endpoint, headers=headers, params=params, timeout=60)
    if response.status_code == 404:
        return [], 0
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected search response: {type(rows).__name__}")
    total = 0
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        count_text = content_range.rsplit("/", 1)[-1]
        if count_text.isdigit():
            total = int(count_text)
    if not total:
        total = len(rows)
    return rows, total
