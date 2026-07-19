"""Sync Xshort movie catalog to Supabase.

Usage:
    python app/xshort/sync_xshort_movies.py --dry-run --max-pages 2
    python app/xshort/sync_xshort_movies.py --max-pages 5000

Create the table first with app/xshort/supabase_schema.sql.
The script reads SUPABASE_URL and SUPABASE_KEY from the project .env file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import requests


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
HOME_URL = "https://api.xemshort.top/api/home"
LOADMORE_URL = "https://api.xemshort.top/api/loadmore"
TABLE_NAME = "xshort_movies"

XSHORT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://xemshort.top",
    "Referer": "https://xemshort.top/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "short-source": "xshort",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
}


def load_env_file(path: Path = ENV_PATH) -> None:
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def plain_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


def ascii_text(value: Any) -> str:
    text = plain_text(value).replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return plain_text(text).lower()


def parse_episode_count(value: Any) -> int | None:
    digits = re.findall(r"\d+", ascii_text(value))
    if not digits:
        return None
    try:
        return int(digits[0])
    except ValueError:
        return None


def cursor_int(cursor: str | None, key: str) -> int | None:
    if not cursor:
        return None
    try:
        values = parse_qs(cursor, keep_blank_values=True).get(key)
        return int(values[0]) if values else None
    except (TypeError, ValueError):
        return None


def movie_key(item: dict[str, Any]) -> str:
    return plain_text(item.get("playId") or item.get("play_id") or item.get("id"))


def normalize_movie(
    item: dict[str, Any],
    source_api: str,
    source_cursor: str | None,
    is_featured: bool,
) -> dict[str, Any] | None:
    play_id = movie_key(item)
    name = plain_text(item.get("name"))
    if not play_id or not name or name.upper() == "TOP":
        return None

    intro = plain_text(item.get("intro"))
    label_list = plain_text(item.get("labelList") or item.get("label_list"))
    search_text = plain_text(" ".join(p for p in (name, intro, label_list) if p))
    timestamp = now_iso()
    return {
        "source_id": plain_text(item.get("id")) or play_id,
        "play_id": play_id,
        "name": name,
        "thumbnail": plain_text(item.get("thumbnail")) or None,
        "intro": intro or None,
        "label_list": label_list or None,
        "episode_count": parse_episode_count(label_list),
        "search_text": search_text,
        "search_text_ascii": ascii_text(search_text),
        "source_api": source_api,
        "source_cursor": source_cursor,
        "source_offset": cursor_int(source_cursor, "offset"),
        "position_index": cursor_int(source_cursor, "position_index"),
        "is_featured": is_featured,
        "raw": item,
        "last_seen_at": timestamp,
        "synced_at": timestamp,
        "updated_at": timestamp,
    }


def merge_movie(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if not old:
        return new
    merged = old.copy()
    for key, value in new.items():
        if value is None:
            continue
        if key in {"intro", "label_list", "episode_count"} and not value:
            continue
        if key == "is_featured":
            merged[key] = bool(merged.get(key)) or bool(value)
            continue
        merged[key] = value
    merged["search_text"] = plain_text(
        " ".join(
            p
            for p in (merged.get("name"), merged.get("intro"), merged.get("label_list"))
            if p
        )
    )
    merged["search_text_ascii"] = ascii_text(merged["search_text"])
    return merged


def raise_for_status(response: requests.Response, label: str) -> None:
    if response.status_code < 400:
        return
    body = response.text[:2000] if response.text else ""
    raise requests.HTTPError(f"{label} HTTP {response.status_code}: {body}", response=response)


def fetch_json(session: requests.Session, url: str, **kwargs: Any) -> dict[str, Any]:
    response = session.get(url, headers=XSHORT_HEADERS, timeout=30, **kwargs)
    raise_for_status(response, f"GET {response.url}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(f"Invalid JSON from {response.url}: {response.text[:1000]}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected response type: {type(data).__name__}")
    if data.get("success") is False:
        raise ValueError(f"API success=false: {data.get('message') or data}")
    return data


def fetch_all_movies(max_pages: int, pause: float, log_fn=print) -> list[dict[str, Any]]:
    session = requests.Session()
    by_play_id: dict[str, dict[str, Any]] = {}

    home = fetch_json(session, HOME_URL)
    featured_items = home.get("data") or []
    all_block = home.get("all") if isinstance(home.get("all"), dict) else {}
    all_items = all_block.get("data") or []
    cursor = plain_text(all_block.get("url") if all_block else "")
    next_page = bool(all_block.get("nextPage")) if all_block else False

    for item in featured_items:
        if isinstance(item, dict):
            movie = normalize_movie(item, "home.data", None, True)
            if movie:
                by_play_id[movie["play_id"]] = merge_movie(by_play_id.get(movie["play_id"]), movie)

    for item in all_items:
        if isinstance(item, dict):
            movie = normalize_movie(item, "home.all", cursor, False)
            if movie:
                by_play_id[movie["play_id"]] = merge_movie(by_play_id.get(movie["play_id"]), movie)

    log_fn(
        f"Home: featured={len(featured_items)}, all={len(all_items)}, "
        f"unique={len(by_play_id)}, nextPage={next_page}, cursor={cursor or '-'}"
    )

    page = 0
    seen_cursors: set[str] = set()
    while next_page and cursor and page < max_pages:
        if cursor in seen_cursors:
            log_fn(f"Stop: repeated cursor {cursor}")
            break
        seen_cursors.add(cursor)
        page += 1

        data = fetch_json(session, LOADMORE_URL, params={"url": cursor})
        items = data.get("data") or []
        before = len(by_play_id)
        for item in items:
            if isinstance(item, dict):
                movie = normalize_movie(item, "loadmore", cursor, False)
                if movie:
                    by_play_id[movie["play_id"]] = merge_movie(by_play_id.get(movie["play_id"]), movie)

        next_page = bool(data.get("nextPage"))
        cursor = plain_text(data.get("url"))
        log_fn(
            f"Loadmore {page}: items={len(items)}, new={len(by_play_id) - before}, "
            f"total={len(by_play_id)}, nextPage={next_page}, cursor={cursor or '-'}"
        )
        if not items:
            break
        if pause > 0:
            time.sleep(pause)

    return list(by_play_id.values())


def supabase_headers(supabase_key: str, prefer: str = "return=minimal") -> dict[str, str]:
    return {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def upsert_movies(
    movies: list[dict[str, Any]],
    supabase_url: str,
    supabase_key: str,
    batch_size: int,
    log_fn=print,
) -> int:
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{TABLE_NAME}"
    headers = supabase_headers(supabase_key, prefer="resolution=merge-duplicates,return=minimal")
    total = 0
    for offset in range(0, len(movies), batch_size):
        batch = movies[offset:offset + batch_size]
        response = requests.post(
            endpoint,
            headers=headers,
            params={"on_conflict": "play_id"},
            data=json.dumps(batch, ensure_ascii=False),
            timeout=60,
        )
        raise_for_status(response, f"Supabase upsert {TABLE_NAME}")
        total += len(batch)
        log_fn(f"Supabase upsert: {total}/{len(movies)}")
    return total


def add_items(
    items: list[Any],
    *,
    source_api: str,
    source_cursor: str | None,
    is_featured: bool,
    by_play_id: dict[str, dict[str, Any]],
    pending: dict[str, dict[str, Any]],
) -> int:
    before = len(by_play_id)
    for item in items:
        if not isinstance(item, dict):
            continue
        movie = normalize_movie(item, source_api, source_cursor, is_featured)
        if not movie:
            continue
        play_id = movie["play_id"]
        existing = by_play_id.get(play_id)
        merged = merge_movie(existing, movie)
        by_play_id[play_id] = merged
        if existing is None:
            pending[play_id] = merged
    return len(by_play_id) - before


def sync_all_movies_streaming(
    *,
    max_pages: int,
    pause: float,
    batch_size: int,
    supabase_url: str,
    supabase_key: str,
    flush_pages: int,
    start_cursor: str | None = None,
    stop_after_no_new_pages: int = 20,
    log_fn=print,
) -> dict[str, int]:
    session = requests.Session()
    by_play_id: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    upserted = 0

    def flush_pending(force: bool = False) -> None:
        nonlocal upserted
        if not pending:
            return
        if not force and len(pending) < batch_size:
            return
        rows = list(pending.values())
        pending.clear()
        upserted += upsert_movies(
            movies=rows,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            batch_size=batch_size,
            log_fn=log_fn,
        )

    if start_cursor:
        cursor = plain_text(start_cursor)
        next_page = True
        log_fn(f"Resume from cursor={cursor}")
    else:
        home = fetch_json(session, HOME_URL)
        featured_items = home.get("data") or []
        all_block = home.get("all") if isinstance(home.get("all"), dict) else {}
        all_items = all_block.get("data") or []
        cursor = plain_text(all_block.get("url") if all_block else "")
        next_page = bool(all_block.get("nextPage")) if all_block else False

        add_items(
            featured_items,
            source_api="home.data",
            source_cursor=None,
            is_featured=True,
            by_play_id=by_play_id,
            pending=pending,
        )
        add_items(
            all_items,
            source_api="home.all",
            source_cursor=cursor,
            is_featured=False,
            by_play_id=by_play_id,
            pending=pending,
        )
        log_fn(
            f"Home: featured={len(featured_items)}, all={len(all_items)}, "
            f"unique={len(by_play_id)}, pending={len(pending)}, nextPage={next_page}, cursor={cursor or '-'}"
        )
        flush_pending(force=len(pending) >= batch_size)

    page = 0
    no_new_pages = 0
    seen_cursors: set[str] = set()
    pages_since_flush = 0
    while next_page and cursor and page < max_pages:
        if cursor in seen_cursors:
            log_fn(f"Stop: repeated cursor {cursor}")
            break
        seen_cursors.add(cursor)
        page += 1
        pages_since_flush += 1

        data = fetch_json(session, LOADMORE_URL, params={"url": cursor})
        items = data.get("data") or []
        new_count = add_items(
            items,
            source_api="loadmore",
            source_cursor=cursor,
            is_featured=False,
            by_play_id=by_play_id,
            pending=pending,
        )
        no_new_pages = no_new_pages + 1 if new_count == 0 else 0
        next_page = bool(data.get("nextPage"))
        cursor = plain_text(data.get("url"))
        log_fn(
            f"Loadmore {page}: items={len(items)}, new={new_count}, total={len(by_play_id)}, "
            f"pending={len(pending)}, noNewPages={no_new_pages}, nextPage={next_page}, cursor={cursor or '-'}"
        )

        if pages_since_flush >= flush_pages or len(pending) >= batch_size:
            flush_pending(force=True)
            pages_since_flush = 0
        if not items:
            break
        if no_new_pages >= stop_after_no_new_pages:
            log_fn(f"Stop: {no_new_pages} pages liên tiếp không có movie mới.")
            break
        if pause > 0:
            time.sleep(pause)

    flush_pending(force=True)
    return {
        "fetched": len(by_play_id),
        "upserted": upserted,
        "loadmore_pages": page,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    load_env_file()
    parser = argparse.ArgumentParser(description="Sync Xshort all movies to Supabase.")
    parser.add_argument("--max-pages", type=int, default=5000, help="Max loadmore pages after /api/home.")
    parser.add_argument("--pause", type=float, default=0.25, help="Seconds between loadmore requests.")
    parser.add_argument("--batch-size", type=int, default=300, help="Supabase upsert batch size.")
    parser.add_argument("--flush-pages", type=int, default=10, help="Upsert pending rows after this many pages.")
    parser.add_argument("--start-cursor", help="Resume streaming sync from a loadmore cursor.")
    parser.add_argument(
        "--stop-after-no-new-pages",
        type=int,
        default=20,
        help="Stop when this many consecutive loadmore pages add no new play_id.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch API only, do not write Supabase.")
    parser.add_argument("--dump-json", type=Path, help="Write normalized movies to a JSON file.")
    args = parser.parse_args()

    def log(message: str) -> None:
        print(message, flush=True)

    if args.dry_run or args.dump_json:
        movies = fetch_all_movies(max_pages=args.max_pages, pause=args.pause, log_fn=log)
        print(f"Fetched {len(movies)} unique Xshort movies.", flush=True)
        if args.dump_json:
            args.dump_json.parent.mkdir(parents=True, exist_ok=True)
            args.dump_json.write_text(json.dumps(movies, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Wrote {args.dump_json}", flush=True)
    else:
        movies = []

    if args.dry_run:
        for movie in movies[:10]:
            print(f"- {movie['play_id']} | {movie['name']} | {movie.get('label_list') or '-'}", flush=True)
        return 0

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("Missing SUPABASE_URL or SUPABASE_KEY. Add them to .env or use --dry-run.", file=sys.stderr)
        return 2

    if args.dump_json:
        upserted = upsert_movies(
            movies=movies,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            batch_size=args.batch_size,
            log_fn=log,
        )
        print(f"Synced Xshort movies to {TABLE_NAME}: fetched={len(movies)}, upserted={upserted}.", flush=True)
        return 0

    result = sync_all_movies_streaming(
        max_pages=args.max_pages,
        pause=args.pause,
        batch_size=args.batch_size,
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        flush_pages=max(args.flush_pages, 1),
        start_cursor=args.start_cursor,
        stop_after_no_new_pages=max(args.stop_after_no_new_pages, 1),
        log_fn=log,
    )
    print(
        f"Synced Xshort movies to {TABLE_NAME}: fetched={result['fetched']}, "
        f"upserted={result['upserted']}, loadmore_pages={result['loadmore_pages']}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
