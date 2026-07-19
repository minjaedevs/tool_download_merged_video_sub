"""Fetch Xshort movie detail raw data and update xshort_movies.detail_raw.

Usage:
    python app/xshort/sync_xshort_details.py --limit 10
    python app/xshort/sync_xshort_details.py --all

Run app/xshort/sync_xshort_movies.py first so xshort_movies has play_id rows.
The detail API intentionally uses short-source=freereel.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DETAIL_URL = "https://api.xemshort.top/allepisode"
TABLE_NAME = "xshort_movies"

DETAIL_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://xemshort.top",
    "Referer": "https://xemshort.top/",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "short-source": "freereel",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
}


def configure_console() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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


def raise_for_status(response: requests.Response, label: str) -> None:
    if response.status_code < 400:
        return
    body = response.text[:2000] if response.text else ""
    raise requests.HTTPError(f"{label} HTTP {response.status_code}: {body}", response=response)


def supabase_headers(supabase_key: str, prefer: str = "return=minimal") -> dict[str, str]:
    return {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def supabase_base() -> tuple[str, str]:
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env")
    return supabase_url.rstrip("/"), supabase_key


def fetch_movie_rows(
    *,
    supabase_url: str,
    supabase_key: str,
    limit: int,
    force: bool,
    offset: int = 0,
) -> list[dict[str, Any]]:
    endpoint = f"{supabase_url}/rest/v1/{TABLE_NAME}"
    headers = supabase_headers(supabase_key)
    params: dict[str, str] = {
        "select": "play_id,name,detail_synced_at",
        "order": "detail_synced_at.asc.nullsfirst,source_offset.asc.nullsfirst,play_id.asc",
        "offset": str(offset),
        "limit": str(limit),
    }
    if not force:
        params["detail_raw"] = "eq.{}"

    response = requests.get(endpoint, headers=headers, params=params, timeout=60)
    raise_for_status(response, f"Supabase fetch {TABLE_NAME}")
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected Supabase response: {data}")
    return [row for row in data if isinstance(row, dict) and row.get("play_id")]


def fetch_detail_raw(session: requests.Session, play_id: str) -> dict[str, Any]:
    response = session.get(
        DETAIL_URL,
        headers=DETAIL_HEADERS,
        params={"shortPlayId": play_id},
        timeout=45,
    )
    raise_for_status(response, f"GET {response.url}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(f"Invalid JSON from {response.url}: {response.text[:1000]}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected detail response type: {type(data).__name__}")
    return data


def fetch_detail_raw_once(play_id: str) -> dict[str, Any]:
    response = requests.get(
        DETAIL_URL,
        headers=DETAIL_HEADERS,
        params={"shortPlayId": play_id},
        timeout=45,
    )
    raise_for_status(response, f"GET {response.url}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(f"Invalid JSON from {response.url}: {response.text[:1000]}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected detail response type: {type(data).__name__}")
    return data


def update_detail_raw(
    *,
    supabase_url: str,
    supabase_key: str,
    play_id: str,
    detail_raw: dict[str, Any],
) -> None:
    endpoint = f"{supabase_url}/rest/v1/{TABLE_NAME}"
    headers = supabase_headers(supabase_key)
    payload = {
        "detail_raw": detail_raw,
        "detail_synced_at": now_iso(),
        "updated_at": now_iso(),
    }
    response = requests.patch(
        endpoint,
        headers=headers,
        params={"play_id": f"eq.{play_id}"},
        data=json.dumps(payload, ensure_ascii=False),
        timeout=60,
    )
    raise_for_status(response, f"Supabase update detail_raw play_id={play_id}")


def upsert_detail_rows(
    *,
    supabase_url: str,
    supabase_key: str,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    endpoint = f"{supabase_url}/rest/v1/{TABLE_NAME}"
    headers = supabase_headers(supabase_key, prefer="resolution=merge-duplicates,return=minimal")
    response = requests.post(
        endpoint,
        headers=headers,
        params={"on_conflict": "play_id"},
        data=json.dumps(rows, ensure_ascii=False),
        timeout=90,
    )
    raise_for_status(response, f"Supabase upsert detail_raw batch size={len(rows)}")
    return len(rows)


def sync_details(
    *,
    supabase_url: str,
    supabase_key: str,
    limit: int,
    page_size: int,
    pause: float,
    force: bool,
    dry_run: bool,
    batch_size: int,
    workers: int,
) -> dict[str, int]:
    session = requests.Session()
    scanned = 0
    updated = 0
    failed = 0
    offset = 0
    pending_updates: list[dict[str, Any]] = []

    def flush_updates(force_flush: bool = False) -> None:
        nonlocal updated
        if dry_run or not pending_updates:
            return
        if not force_flush and len(pending_updates) < batch_size:
            return
        count = upsert_detail_rows(
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            rows=pending_updates,
        )
        updated += count
        print(f"Supabase detail upsert: +{count}, updated={updated}", flush=True)
        pending_updates.clear()

    while scanned < limit:
        batch_limit = min(page_size, limit - scanned)
        rows = fetch_movie_rows(
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            limit=batch_limit,
            force=force,
            offset=offset if force else 0,
        )
        if not rows:
            break
        if force:
            offset += len(rows)

        def handle_success(row: dict[str, Any], detail: dict[str, Any]) -> None:
            nonlocal scanned
            play_id = str(row["play_id"])
            name = str(row.get("name") or "")
            raw_len = len(str(detail.get("data", ""))) if "data" in detail else len(json.dumps(detail))
            scanned += 1
            if dry_run:
                print(
                    f"[dry-run] {scanned}/{limit} {play_id} | {name} | "
                    f"keys={','.join(detail)} raw_len={raw_len}",
                    flush=True,
                )
                return
            timestamp = now_iso()
            pending_updates.append({
                "play_id": play_id,
                "name": name or play_id,
                "detail_raw": detail,
                "detail_synced_at": timestamp,
                "updated_at": timestamp,
            })
            print(f"Fetched detail {scanned}/{limit} | {play_id} | {name} | raw_len={raw_len}", flush=True)
            flush_updates()

        def handle_failure(row: dict[str, Any], exc: Exception) -> None:
            nonlocal failed, scanned
            play_id = str(row["play_id"])
            name = str(row.get("name") or "")
            scanned += 1
            failed += 1
            print(f"FAILED {play_id} | {name} | {type(exc).__name__}: {exc}", flush=True)

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_row = {
                    executor.submit(fetch_detail_raw_once, str(row["play_id"])): row
                    for row in rows
                }
                for future in as_completed(future_to_row):
                    row = future_to_row[future]
                    try:
                        handle_success(row, future.result())
                    except Exception as exc:
                        handle_failure(row, exc)
                    if scanned >= limit:
                        break
        else:
            for row in rows:
                try:
                    detail = fetch_detail_raw(session, str(row["play_id"]))
                    handle_success(row, detail)
                except Exception as exc:
                    handle_failure(row, exc)
                if scanned >= limit:
                    break
                if pause > 0:
                    time.sleep(pause)

        if workers > 1 and pause > 0:
            time.sleep(pause)

        if scanned >= limit:
            break

        # Non-force mode always asks for the first page of rows still missing
        # detail_raw. Once a page is flushed, those rows disappear from the
        # missing set, so offset must stay at zero.
        flush_updates(force_flush=True)

    flush_updates(force_flush=True)
    return {"scanned": scanned, "updated": updated, "failed": failed}

    flush_updates(force_flush=True)
    return {"scanned": scanned, "updated": updated, "failed": failed}


def main() -> int:
    configure_console()
    load_env_file()
    parser = argparse.ArgumentParser(description="Sync Xshort movie detail_raw to Supabase.")
    parser.add_argument("--limit", type=int, default=50, help="Max movies to process.")
    parser.add_argument("--all", action="store_true", help="Process a very large batch of missing details.")
    parser.add_argument("--page-size", type=int, default=100, help="Supabase fetch page size.")
    parser.add_argument("--batch-size", type=int, default=50, help="Supabase detail upsert batch size.")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent detail API workers.")
    parser.add_argument("--pause", type=float, default=0.15, help="Seconds between detail API calls.")
    parser.add_argument("--force", action="store_true", help="Refresh detail_raw even if it already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Call detail API but do not update Supabase.")
    args = parser.parse_args()

    supabase_url, supabase_key = supabase_base()
    limit = 1_000_000 if args.all else max(args.limit, 1)
    result = sync_details(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        limit=limit,
        page_size=max(args.page_size, 1),
        pause=max(args.pause, 0.0),
        force=args.force,
        dry_run=args.dry_run,
        batch_size=max(args.batch_size, 1),
        workers=max(args.workers, 1),
    )
    print(
        f"Detail sync done: scanned={result['scanned']}, "
        f"updated={result['updated']}, failed={result['failed']}.",
        flush=True,
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
