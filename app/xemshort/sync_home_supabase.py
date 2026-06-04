"""Sync only NetShort /api/home movies to Supabase.

Usage:
    python app/xemshort/sync_home_supabase.py

The script reads SUPABASE_URL and SUPABASE_KEY from .env or environment.
"""
from __future__ import annotations

import os
import sys

from sync_movies_supabase import (
    NETSHORT_SOURCE,
    load_env_file,
    upsert_home_supabase,
)


def main() -> int:
    load_env_file()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("Missing SUPABASE_URL or SUPABASE_KEY.", file=sys.stderr)
        return 2

    result = upsert_home_supabase(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        source=NETSHORT_SOURCE,
    )
    print(
        f"Home sync {result['source_name']} ({result['table']}): "
        f"fetched={result['fetched']}, upserted={result['upserted']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
