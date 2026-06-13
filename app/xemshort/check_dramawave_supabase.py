"""Check DramaWave Supabase table visibility and writes.

Usage:
    python app/xemshort/check_dramawave_supabase.py
    python app/xemshort/check_dramawave_supabase.py --keep-row
    python app/xemshort/check_dramawave_supabase.py --url https://... --key ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
TABLE = "dramawave_movies"


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def supabase_headers(key: str, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def print_response(label: str, response: requests.Response) -> None:
    print(f"\n[{label}]")
    print(f"status={response.status_code}")
    for name in ("content-type", "content-range", "preference-applied"):
        value = response.headers.get(name)
        if value:
            print(f"{name}={value}")
    text = response.text.strip()
    if text:
        print(text[:1200])


def request_json(response: requests.Response) -> Any:
    if not response.text.strip():
        return None
    return response.json()


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Check Supabase dramawave_movies table.")
    parser.add_argument("--url", default=os.environ.get("SUPABASE_URL", "").strip())
    parser.add_argument("--key", default=os.environ.get("SUPABASE_KEY", "").strip())
    parser.add_argument("--table", default=TABLE)
    parser.add_argument("--keep-row", action="store_true", help="Keep probe row after insert.")
    args = parser.parse_args()

    if not args.url or not args.key:
        print("Missing SUPABASE_URL/SUPABASE_KEY. Add them to .env or pass --url/--key.", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    endpoint = f"{base}/rest/v1/{args.table}"
    print(f"Supabase URL: {base}")
    print(f"Table: public.{args.table}")
    print(f"Key prefix: {args.key[:12]}... len={len(args.key)}")

    count_headers = supabase_headers(args.key, prefer="count=exact")
    count_params = {"select": "id", "limit": "1"}
    response = requests.get(endpoint, headers=count_headers, params=count_params, timeout=30)
    print_response("count before", response)
    if response.status_code == 404:
        print("Table not found through PostgREST. Check table name and Supabase project URL.", file=sys.stderr)
        return 1
    response.raise_for_status()

    play_id = f"db-check-{int(time.time() * 1000)}"
    payload = [{
        "source_id": play_id,
        "play_id": play_id,
        "name": "DB Check DramaWave",
        "thumbnail": None,
        "intro": "temporary row created by check_dramawave_supabase.py",
        "label_list": "1 tap",
        "episode_count": 1,
        "search_text": "DB Check DramaWave temporary row 1 tap",
        "search_text_ascii": "db check dramawave temporary row 1 tap",
        "source_api": "db-check",
        "source_cursor": "offset=0&position_index=10001",
        "source_offset": 0,
        "position_index": 10001,
        "is_featured": False,
        "raw": {"check": True},
    }]

    insert_headers = supabase_headers(
        args.key,
        prefer="resolution=merge-duplicates,return=representation",
    )
    response = requests.post(
        endpoint,
        headers=insert_headers,
        params={"on_conflict": "play_id"},
        data=json.dumps(payload, ensure_ascii=False),
        timeout=30,
    )
    print_response("insert probe", response)
    response.raise_for_status()

    select_headers = supabase_headers(args.key)
    select_headers.pop("Content-Type", None)
    response = requests.get(
        endpoint,
        headers=select_headers,
        params={"select": "id,play_id,name,created_at,synced_at", "play_id": f"eq.{play_id}"},
        timeout=30,
    )
    print_response("select probe", response)
    response.raise_for_status()
    rows = request_json(response)
    if not rows:
        print("Inserted probe was not visible on select. Check RLS/policies/project/key.", file=sys.stderr)
        return 1

    response = requests.get(endpoint, headers=count_headers, params=count_params, timeout=30)
    print_response("count after", response)
    response.raise_for_status()

    if args.keep_row:
        print(f"\nKept probe row: {play_id}")
        return 0

    response = requests.delete(
        endpoint,
        headers=select_headers,
        params={"play_id": f"eq.{play_id}"},
        timeout=30,
    )
    print_response("delete probe", response)
    response.raise_for_status()
    print(f"\nDeleted probe row: {play_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
