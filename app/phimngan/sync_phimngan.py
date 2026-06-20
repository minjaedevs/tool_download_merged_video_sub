"""Sync phimngan.tv movies & episodes to Supabase.

Usage:
    python app/phimngan/sync_phimngan.py                   # skip existing, add new only
    python app/phimngan/sync_phimngan.py --force           # re-fetch ALL data
    python app/phimngan/sync_phimngan.py --movies-only     # chi sync movies
    python app/phimngan/sync_phimngan.py --episodes-only   # chi sync episodes
    python app/phimngan/sync_phimngan.py --force --movies-only   # re-fetch movies
    python app/phimngan/sync_phimngan.py --force --episodes-only # re-fetch ALL episodes
    python app/phimngan/sync_phimngan.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

# Resolve app/phimngan_db.py
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

import requests

from phimngan_db import (
    SUPABASE_KEY as _DEFAULT_KEY,
    SUPABASE_URL as _DEFAULT_URL,
    count_movies,
    count_episodes,
    delete_episodes_for_slug,
    fetch_episodes_from_db,
    fetch_existing_slugs,
    fetch_movie_from_db,
    fetch_movies_page,
    load_env_file,
    parse_episodes_from_html,
    upsert_episodes,
    upsert_movies,
    LIST_HEADERS,
    DETAIL_API_URL,
)


_MOVIE_ID_RE = re.compile(
    r'"movie":\{"id":"(?P<id>[^"]+)","title":"(?P<title>[^"]+)",'
    r'"slug":"(?P<slug>[^"]+)".*?"cover":"(?P<cover>[^"]+)",'
    r'"episodeCount":(?P<episode_count>\d+),"totalEpisode":(?P<total_episode>\d+)',
    re.DOTALL,
)


def _load_conf() -> tuple[str, str]:
    load_env_file()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip() or _DEFAULT_URL
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip() or _DEFAULT_KEY
    return supabase_url, supabase_key


# ─────────────────────────────────────────────────────────────────
# Step 1: Sync movies (list pages)
# ─────────────────────────────────────────────────────────────────

def sync_movies(
    supabase_url: str,
    supabase_key: str,
    max_pages: int = 100,
    pause: float = 0.5,
    force: bool = False,
    log_fn=None,
) -> dict:
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    session = requests.Session()

    before = count_movies(supabase_url, supabase_key)
    log(f"phimngan_movies hien co: {before} phim.")

    total_new = 0
    total_upserted = 0
    no_new_pages = 0
    STOP_NO_NEW = 3

    for page in range(1, max_pages + 1):
        try:
            movies, _has_more = fetch_movies_page(session, page)
        except Exception as exc:
            log(f"[page {page}] Loi fetch: {exc}")
            break

        if not movies:
            log(f"[page {page}] Khong co phim nao. Dung lai.")
            break

        if force:
            # Upsert ALL movies (overwrite existing with fresh data)
            upsert_movies(movies, supabase_url, supabase_key, log_fn=log)
            total_upserted += len(movies)
            log(f"[page {page}] Force upsert {len(movies)} phim. Tong: {total_upserted}.")
        else:
            # Filter out already-synced slugs
            existing = fetch_existing_slugs(supabase_url, supabase_key)
            new_movies = [m for m in movies if m["slug"] not in existing]

            if new_movies:
                upsert_movies(new_movies, supabase_url, supabase_key, log_fn=log)
                total_new += len(new_movies)
                no_new_pages = 0
                log(f"[page {page}] Upsert {len(new_movies)}/{len(movies)} phim moi. Tong: {total_new}.")
            else:
                no_new_pages += 1
                log(f"[page {page}] Khong co phim moi ({no_new_pages}/{STOP_NO_NEW}).")
                if no_new_pages >= STOP_NO_NEW:
                    log("Dung sync: da qua nhieu trang khong co phim moi.")
                    break

        time.sleep(pause)

    after = count_movies(supabase_url, supabase_key)
    return {
        "step": "movies",
        "before": before,
        "after": after,
        "total_new": total_new,
        "total_upserted": total_upserted if force else total_new,
    }


# ─────────────────────────────────────────────────────────────────
# Step 2: Sync episodes (detail pages)
# ─────────────────────────────────────────────────────────────────

def _fetch_detail_page(slug: str, timeout: int = 30) -> str:
    """Fetch /movies/{slug} and return raw text. Uses its own session."""
    import requests as _req
    url = DETAIL_API_URL.replace("{slug}", slug)
    headers = dict(LIST_HEADERS)
    headers["Referer"] = f"https://phimngan.tv/movies/{slug}"
    response = _req.get(url, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        import logging as _logging
        _logging.warning(
            f"Detail fetch {slug} -> HTTP {response.status_code} "
            f"(len={len(response.content)}): {response.text[:200]}"
        )
    response.raise_for_status()
    return response.content.decode("utf-8", errors="replace")


def sync_episodes(
    supabase_url: str,
    supabase_key: str,
    max_movies: int = 100,
    pause: float = 0.5,
    force: bool = False,
    log_fn=None,
) -> dict:
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    all_slugs = list(fetch_existing_slugs(supabase_url, supabase_key))
    log(f"Total movies in DB: {len(all_slugs)}")

    if force:
        log("[force] Se xoa episodes cu va fetch lai cho tat ca phim.")

    processed = 0
    total_eps = 0
    errors = 0
    skipped = 0

    for slug in all_slugs:
        # Skip if already has episodes (unless force)
        existing_eps = fetch_episodes_from_db(supabase_url, supabase_key, slug)
        if existing_eps and not force:
            skipped += 1
            continue
        if existing_eps and force:
            # Delete old episodes first
            try:
                delete_episodes_for_slug(supabase_url, supabase_key, slug)
                log(f"[{slug}] Da xoa {len(existing_eps)} episodes cu (force mode).")
            except Exception as exc:
                log(f"[{slug}] Loi xoa episodes cu: {exc}")
                errors += 1
                continue

        movie = fetch_movie_from_db(supabase_url, supabase_key, slug)
        if not movie:
            continue

        # Fetch detail HTML
        try:
            html = _fetch_detail_page(slug)
        except Exception as exc:
            log(f"[{slug}] Loi fetch detail: {exc}")
            errors += 1
            continue

        # Extract movie ID from HTML (needed for CDN sub URL)
        movie_id = slug
        m_match = _MOVIE_ID_RE.search(html)
        if m_match:
            movie_id = m_match.group("id")

        # Parse episodes
        episodes = parse_episodes_from_html(html, slug, movie.get("name", slug), movie_id)
        if not episodes:
            log(f"[{slug}] Khong tim thay episode nao trong detail page.")
            errors += 1
            continue

        # Upsert
        upserted, _ = upsert_episodes(episodes, supabase_url, supabase_key, log_fn=log)
        total_eps += upserted
        processed += 1
        log(f"[{slug}] Upsert {upserted} episodes. Tong: {processed} phim, {total_eps} eps.")

        if processed >= max_movies:
            log(f"Da xu ly {max_movies} phim. Dung lai.")
            break

        time.sleep(pause)

    return {
        "step": "episodes",
        "processed": processed,
        "total_episodes": total_eps,
        "errors": errors,
        "skipped": skipped,
    }


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Sync phimngan.tv to Supabase.")
    parser.add_argument("--max-pages", type=int, default=100,
                        help="Max list pages to crawl (default: 100)")
    parser.add_argument("--max-movies", type=int, default=10000,
                        help="Max movies to fetch episodes for (default: 10000)")
    parser.add_argument("--pause", type=float, default=0.5,
                        help="Seconds between requests (default: 0.5)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-fetch: xoa data cu, lay data moi")
    parser.add_argument("--movies-only", action="store_true",
                        help="Chi sync movies, khong sync episodes")
    parser.add_argument("--episodes-only", action="store_true",
                        help="Chi sync episodes cho movies da co trong DB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch without writing to DB")
    args = parser.parse_args()

    supabase_url, supabase_key = _load_conf()
    if not supabase_url or not supabase_key:
        print("Missing SUPABASE_URL or SUPABASE_KEY.", file=sys.stderr)
        return 2

    # Dry-run
    if args.dry_run:
        session = requests.Session()
        movies, has_more = fetch_movies_page(session, 1)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"[dry-run] Page 1: {len(movies)} movies, has_more={has_more}")
        for m in movies[:5]:
            print(f"  - {m['slug']} | {m['name']} | ep={m.get('episode_count')}/{m.get('total_episode')}")
        return 0

    result_movies: dict = {}
    result_episodes: dict = {}

    if not args.episodes_only:
        result_movies = sync_movies(
            supabase_url, supabase_key,
            max_pages=args.max_pages,
            pause=args.pause,
            force=args.force,
        )
        print(f"[movies] before={result_movies['before']}, "
              f"after={result_movies['after']}, "
              f"new_or_updated={result_movies['total_upserted']}")

    if not args.movies_only:
        result_episodes = sync_episodes(
            supabase_url, supabase_key,
            max_movies=args.max_movies,
            pause=args.pause,
            force=args.force,
        )
        print(f"[episodes] processed={result_episodes['processed']}, "
              f"total_eps={result_episodes['total_episodes']}, "
              f"skipped={result_episodes['skipped']}, "
              f"errors={result_episodes['errors']}")

    print("Sync hoan tat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
