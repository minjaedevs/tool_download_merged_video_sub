"""Sync XemShort/NetShort movie catalog to Supabase.

Usage:
    set SUPABASE_URL=https://...
    set SUPABASE_KEY=<service-role-key-for-server-sync>
    python app/xemshort/sync_movies_supabase.py --dry-run
    python app/xemshort/sync_movies_supabase.py

Create the table first with supabase_movies_schema.sql.
Keep the service-role key on the Python/Flask server only. Next.js should use
the publishable key for read/search, not for syncing writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
HOME_URL = "https://api.xemshort.top/api/home"
LOADMORE_URL = (
    "https://api.xemshort.top/api/loadmore"
    "?page={page}&url=https:%2F%2Fnetshort.com%2Fvi%2Fdrama%2Fall-plots%2Fpage%2F"
)
NETSHORT_SOURCE = "netshort"
MOVIE_SOURCES = {
    "netshort": {"name": "NetShort", "table": "netshort_movies", "sync_enabled": True},
    "reelshort": {"name": "ReelShort", "table": "reelshort_movies", "sync_enabled": False},
    "shortmax": {"name": "ShortMax", "table": "shortmax_movies", "sync_enabled": False},
    "dramawave": {"name": "DramaWave", "table": "dramawave_movies", "sync_enabled": False},
    "dramabox": {"name": "DramaBox", "table": "dramabox_movies", "sync_enabled": False},
}

NETSHORT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://xemshort.top",
    "Referer": "https://xemshort.top/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "short-source": "netshort",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file(path: Path = ENV_PATH) -> None:
    """Load simple KEY=VALUE lines into os.environ without adding a dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _plain_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


def _ascii_text(value: Any) -> str:
    text = _plain_text(value).replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return _plain_text(text).lower()


def _movie_key(item: dict[str, Any]) -> str:
    return _plain_text(item.get("playId") or item.get("play_id") or item.get("id"))


def source_table(source: str) -> str:
    if source not in MOVIE_SOURCES:
        raise ValueError(f"Unknown movie source: {source}")
    return str(MOVIE_SOURCES[source]["table"])


def _normalize_movie(item: dict[str, Any], source_api: str, source_page: int | None) -> dict[str, Any] | None:
    play_id = _movie_key(item)
    name = _plain_text(item.get("name"))
    if not play_id or not name:
        return None

    intro = _plain_text(item.get("intro"))
    label_list = _plain_text(item.get("labelList") or item.get("label_list"))
    search_text = _plain_text(" ".join(part for part in (name, intro, label_list) if part))
    search_text_ascii = _ascii_text(search_text)

    return {
        "source_id": _plain_text(item.get("id")) or play_id,
        "play_id": play_id,
        "name": name,
        "thumbnail": _plain_text(item.get("thumbnail")) or None,
        "intro": intro or None,
        "label_list": label_list or None,
        "search_text": search_text,
        "search_text_ascii": search_text_ascii,
        "source_page": source_page,
        "source_api": source_api,
        "raw": item,
        "synced_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _fetch_json(session: requests.Session, url: str, timeout: int = 20) -> dict[str, Any]:
    response = session.get(url, headers=NETSHORT_HEADERS, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected response type: {type(data).__name__}")
    if data.get("success") is False:
        raise ValueError(f"API returned success=false: {data.get('message') or data}")
    return data


def _is_no_more_movies_response(data: dict[str, Any]) -> bool:
    if data.get("success") is not False:
        return False
    message = _ascii_text(data.get("message") or data.get("msg") or "")
    return "khong tim thay phim" in message or "not found" in message or "no movie" in message


def fetch_home_movies() -> list[dict[str, Any]]:
    """Fetch and normalize only /api/home movies."""
    session = requests.Session()
    home = _fetch_json(session, HOME_URL)
    movies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in home.get("data") or []:
        if not isinstance(item, dict):
            continue
        movie = _normalize_movie(item, source_api="home", source_page=None)
        if not movie or movie["play_id"] in seen:
            continue
        movies.append(movie)
        seen.add(movie["play_id"])
    return movies


def fetch_movies(start_page: int = 2, max_pages: int = 500, pause: float = 0.15,
                 stop_after_no_new_pages: int = 8) -> list[dict[str, Any]]:
    """Fetch /api/home then loadmore until empty or repeated pages add no new movies."""
    by_play_id: dict[str, dict[str, Any]] = {}

    for movie in fetch_home_movies():
        by_play_id[movie["play_id"]] = movie

    empty_seen = False
    no_new_pages = 0
    session = requests.Session()
    for page in range(start_page, start_page + max_pages):
        try:
            data = _fetch_json(session, LOADMORE_URL.format(page=page))
        except ValueError as exc:
            message = str(exc)
            if "khong tim thay phim" in _ascii_text(message):
                empty_seen = True
                break
            raise
        items = data.get("data") or []
        if not items:
            empty_seen = True
            break
        before_count = len(by_play_id)
        for item in items:
            if not isinstance(item, dict):
                continue
            movie = _normalize_movie(item, source_api="loadmore", source_page=page)
            if not movie:
                continue
            old = by_play_id.get(movie["play_id"])
            if old:
                # Preserve richer intro text from /api/home when loadmore has only labels.
                if old.get("intro") and not movie.get("intro"):
                    movie["intro"] = old["intro"]
                movie["search_text"] = _plain_text(
                    " ".join(part for part in (movie["name"], movie.get("intro"), movie.get("label_list")) if part)
                )
                movie["search_text_ascii"] = _ascii_text(movie["search_text"])
            by_play_id[movie["play_id"]] = movie
        if len(by_play_id) == before_count:
            no_new_pages += 1
            if no_new_pages >= stop_after_no_new_pages:
                break
        else:
            no_new_pages = 0
        time.sleep(pause)

    if not empty_seen:
        print(
            f"Warning: stopped before receiving an empty page "
            f"(max_pages={max_pages}, no_new_pages={no_new_pages}).",
            file=sys.stderr,
        )
    return list(by_play_id.values())


def _supabase_headers(supabase_key: str, prefer: str = "return=minimal") -> dict[str, str]:
    return {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def supabase_count(supabase_url: str, supabase_key: str, source: str = NETSHORT_SOURCE,
                   synced_since: str | None = None) -> int:
    """Return exact movie count from the source-specific Supabase table."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/{source_table(source)}"
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "count=exact"
    params = {"select": "id", "limit": "1"}
    if synced_since:
        params["synced_at"] = f"gte.{synced_since}"
    response = requests.get(endpoint, headers=headers, params=params, timeout=60)
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


def search_movies_supabase(supabase_url: str, supabase_key: str, query: str = "",
                           page: int = 1, page_size: int = 24,
                           source: str = NETSHORT_SOURCE) -> tuple[list[dict[str, Any]], int]:
    """Search a source-specific movie table by search_text with simple pagination."""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/{source_table(source)}"
    headers = _supabase_headers(supabase_key)
    headers["Prefer"] = "count=exact"
    offset = (page - 1) * page_size
    params = {
        "select": "play_id,name,thumbnail,intro,label_list,search_text,search_text_ascii,created_at,synced_at",
        "order": "created_at.desc",
        "limit": str(page_size),
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
        raise ValueError(f"Unexpected Supabase search response: {type(rows).__name__}")
    total = 0
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        count_text = content_range.rsplit("/", 1)[-1]
        if count_text.isdigit():
            total = int(count_text)
    if not total:
        total = len(rows)
    return rows, total


def supabase_source_stats(supabase_url: str, supabase_key: str,
                          synced_since: str | None = None) -> dict[str, dict[str, int | str]]:
    """Return movie counts for every configured source."""
    stats: dict[str, dict[str, int | str]] = {}
    for source, meta in MOVIE_SOURCES.items():
        total = supabase_count(supabase_url, supabase_key, source=source)
        new_since = (
            supabase_count(supabase_url, supabase_key, source=source, synced_since=synced_since)
            if synced_since else 0
        )
        stats[source] = {
            "name": str(meta["name"]),
            "table": str(meta["table"]),
            "total": total,
            "new_since": new_since,
            "sync_enabled": int(bool(meta["sync_enabled"])),
        }
    return stats


def test_insert_supabase(supabase_url: str, supabase_key: str, source: str = NETSHORT_SOURCE) -> str:
    """Insert and delete one temporary row to verify write permissions."""
    table = source_table(source)
    play_id = f"test-sync-{int(time.time() * 1000)}"
    now = _now_iso()
    payload = [{
        "source_id": play_id,
        "play_id": play_id,
        "name": "Test Sync Movie",
        "thumbnail": None,
        "intro": "temporary insert test",
        "label_list": "test",
        "search_text": "Test Sync Movie temporary insert test",
        "search_text_ascii": "test sync movie temporary insert test",
        "source_page": None,
        "source_api": "test",
        "raw": {},
        "synced_at": now,
        "updated_at": now,
    }]
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/{table}"
    headers = _supabase_headers(supabase_key)
    response = requests.post(endpoint, headers=headers, data=json.dumps(payload, ensure_ascii=False), timeout=60)
    response.raise_for_status()

    delete_headers = _supabase_headers(supabase_key)
    delete_headers.pop("Content-Type", None)
    response = requests.delete(
        endpoint,
        headers=delete_headers,
        params={"play_id": f"eq.{play_id}"},
        timeout=60,
    )
    response.raise_for_status()
    return play_id


def fetch_existing_play_ids(supabase_url: str, supabase_key: str, source: str = NETSHORT_SOURCE,
                            page_size: int = 1000) -> set[str]:
    """Read existing play_id values so reruns can skip already-synced movies."""
    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/{source_table(source)}"
    headers = _supabase_headers(supabase_key)
    existing: set[str] = set()
    offset = 0
    while True:
        params = {
            "select": "play_id",
            "order": "id.asc",
            "limit": str(page_size),
            "offset": str(offset),
        }
        response = requests.get(endpoint, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError(f"Unexpected Supabase select response: {type(rows).__name__}")
        for row in rows:
            if isinstance(row, dict) and row.get("play_id"):
                existing.add(str(row["play_id"]))
        if len(rows) < page_size:
            break
        offset += page_size
    return existing


def insert_new_supabase(movies: list[dict[str, Any]], supabase_url: str, supabase_key: str,
                        source: str = NETSHORT_SOURCE, batch_size: int = 500,
                        log_fn=None) -> tuple[int, int]:
    """Insert only movies whose play_id does not already exist in Supabase."""
    existing = fetch_existing_play_ids(supabase_url, supabase_key, source=source)
    new_movies = [movie for movie in movies if movie["play_id"] not in existing]
    if not new_movies:
        return 0, len(movies)

    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/{source_table(source)}"
    headers = _supabase_headers(supabase_key)
    total = 0
    for offset in range(0, len(new_movies), batch_size):
        batch = new_movies[offset:offset + batch_size]
        response = requests.post(endpoint, headers=headers, data=json.dumps(batch, ensure_ascii=False), timeout=60)
        response.raise_for_status()
        total += len(batch)
        if log_fn:
            log_fn(f"Inserted batch: {total}/{len(new_movies)}")
    return total, len(movies) - len(new_movies)


def upsert_home_supabase(supabase_url: str, supabase_key: str,
                         source: str = NETSHORT_SOURCE) -> dict[str, int | str]:
    """Fetch /api/home and upsert rows so home metadata stays current."""
    if source != NETSHORT_SOURCE:
        source_name = str(MOVIE_SOURCES.get(source, {}).get("name", source))
        raise ValueError(f"Source {source_name} chua ho tro sync home.")

    movies = fetch_home_movies()
    if not movies:
        return {
            "source": source,
            "source_name": str(MOVIE_SOURCES[source]["name"]),
            "table": source_table(source),
            "fetched": 0,
            "upserted": 0,
        }

    base = supabase_url.rstrip("/")
    endpoint = f"{base}/rest/v1/{source_table(source)}"
    headers = _supabase_headers(supabase_key, prefer="resolution=merge-duplicates,return=minimal")
    response = requests.post(
        endpoint,
        headers=headers,
        params={"on_conflict": "play_id"},
        data=json.dumps(movies, ensure_ascii=False),
        timeout=60,
    )
    response.raise_for_status()
    return {
        "source": source,
        "source_name": str(MOVIE_SOURCES[source]["name"]),
        "table": source_table(source),
        "fetched": len(movies),
        "upserted": len(movies),
    }


def sync_to_supabase(supabase_url: str, supabase_key: str, start_page: int = 2,
                     max_pages: int = 500, pause: float = 0.15,
                     stop_after_no_new_pages: int = 8, source: str = NETSHORT_SOURCE,
                     log_fn=None) -> dict[str, int | str | dict]:
    """Fetch API data and insert only new movies into Supabase."""
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    if source != NETSHORT_SOURCE:
        source_name = str(MOVIE_SOURCES.get(source, {}).get("name", source))
        raise ValueError(f"Nguồn {source_name} chưa hỗ trợ sync.")

    sync_started_at = _now_iso()
    source_name = str(MOVIE_SOURCES[source]["name"])
    table = source_table(source)
    before = supabase_count(supabase_url, supabase_key, source=source)
    log(f"{source_name} ({table}) hien co {before} phim.")
    movies = fetch_movies(
        start_page=start_page,
        max_pages=max_pages,
        pause=pause,
        stop_after_no_new_pages=stop_after_no_new_pages,
    )
    log(f"Fetch API xong: {len(movies)} phim unique.")
    inserted, skipped = insert_new_supabase(movies, supabase_url, supabase_key, source=source, log_fn=log)
    after = supabase_count(supabase_url, supabase_key, source=source)
    new_by_synced_at = supabase_count(
        supabase_url,
        supabase_key,
        source=source,
        synced_since=sync_started_at,
    )
    stats = supabase_source_stats(supabase_url, supabase_key, synced_since=sync_started_at)
    log(f"Sync xong: inserted={inserted}, skipped={skipped}, total={after}.")
    return {
        "source": source,
        "source_name": source_name,
        "table": table,
        "before": before,
        "fetched": len(movies),
        "inserted": inserted,
        "new_by_synced_at": new_by_synced_at,
        "skipped": skipped,
        "after": after,
        "sync_started_at": sync_started_at,
        "stats": stats,
    }


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Sync XemShort movies to Supabase.")
    parser.add_argument("--start-page", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--pause", type=float, default=0.15)
    parser.add_argument("--stop-after-no-new-pages", type=int, default=8)
    parser.add_argument("--source", choices=sorted(MOVIE_SOURCES), default=NETSHORT_SOURCE)
    parser.add_argument("--home-only", action="store_true", help="Fetch /api/home and upsert only home movies.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print summary without Supabase insert.")
    parser.add_argument("--dump-json", type=Path, help="Write normalized movies to a JSON file.")
    parser.add_argument("--test-insert", action="store_true", help="Insert and delete one temporary row, then exit.")
    args = parser.parse_args()

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()

    if args.source != NETSHORT_SOURCE:
        print(f"Source {MOVIE_SOURCES[args.source]['name']} chưa hỗ trợ sync.", file=sys.stderr)
        return 2

    if args.test_insert:
        if not supabase_url or not supabase_key:
            print("Missing SUPABASE_URL or SUPABASE_KEY.", file=sys.stderr)
            return 2
        play_id = test_insert_supabase(supabase_url=supabase_url, supabase_key=supabase_key)
        print(f"Test insert OK: {play_id}")
        return 0

    if args.home_only:
        if not supabase_url or not supabase_key:
            print("Missing SUPABASE_URL or SUPABASE_KEY.", file=sys.stderr)
            return 2
        result = upsert_home_supabase(supabase_url=supabase_url, supabase_key=supabase_key, source=args.source)
        print(
            f"Home sync {result['source_name']} ({result['table']}): "
            f"fetched={result['fetched']}, upserted={result['upserted']}."
        )
        return 0

    movies: list[dict[str, Any]] | None = None
    if args.dry_run or args.dump_json:
        movies = fetch_movies(
            start_page=args.start_page,
            max_pages=args.max_pages,
            pause=args.pause,
            stop_after_no_new_pages=args.stop_after_no_new_pages,
        )
        print(f"Fetched {len(movies)} unique movies.")

    if args.dump_json and movies is not None:
        args.dump_json.parent.mkdir(parents=True, exist_ok=True)
        args.dump_json.write_text(json.dumps(movies, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.dump_json}")

    if args.dry_run and movies is not None:
        for movie in movies[:5]:
            print(f"- {movie['play_id']} | {movie['name']}")
        return 0

    if not supabase_url or not supabase_key:
        print("Missing SUPABASE_URL or SUPABASE_KEY. Use --dry-run to fetch without syncing.", file=sys.stderr)
        return 2

    result = sync_to_supabase(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        start_page=args.start_page,
        max_pages=args.max_pages,
        pause=args.pause,
        stop_after_no_new_pages=args.stop_after_no_new_pages,
        source=args.source,
        log_fn=print,
    )
    print(
        f"Synced {result['source_name']} ({result['table']}): fetched={result['fetched']}, "
        f"inserted={result['inserted']}, skipped={result['skipped']}, "
        f"new_by_synced_at={result['new_by_synced_at']}, total={result['after']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
