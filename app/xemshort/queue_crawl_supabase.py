"""Supabase helpers for queue_request_movie_aller and nestShort_crawl tables.

Used by:
  - request_queue_tab.py  (UI tab for submitting crawl requests)
  - crawl_service.py      (standalone service at Xemshort_tool)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import requests

TABLE_QUEUE = "queue_request_movie_aller"
TABLE_CRAWL = "nestShort_crawl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supabase_headers(key: str, prefer: str = "return=representation") -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def _base(url: str) -> str:
    return url.rstrip("/") + "/rest/v1"


# ── Queue table (queue_request_movie_aller) ───────────────────────────────────

def submit_queue_request(
    supabase_url: str,
    supabase_key: str,
    author: str,
    short_play_id: str,
    provider: str = "netshort",
) -> dict:
    """
    Upsert a crawl request into the queue.

    - New row               → create with status='pending'
    - Exists + completed    → return with _already_completed=True  (notify user)
    - Exists + error/failed → PATCH reset to pending, return with _reset_from_error=True
    - Exists + other status → return with _already_exists=True     (pending / processing)

    Returns the resulting row dict.
    """
    base = _base(supabase_url)
    endpoint = f"{base}/{TABLE_QUEUE}"
    headers = _supabase_headers(supabase_key, "return=representation")

    # Check existence (scoped to provider so same ID can exist for different providers)
    resp = requests.get(
        endpoint,
        headers=headers,
        params={
            "shortPlayId": f"eq.{short_play_id}",
            "provider": f"eq.{provider}",
            "select": "*",
        },
        timeout=30,
    )
    resp.raise_for_status()
    rows: list[dict] = resp.json() if isinstance(resp.json(), list) else []

    if rows:
        row = rows[0]
        status = str(row.get("status") or "pending")

        if status == "completed":
            return {**row, "_already_completed": True}

        if status in ("error", "failed"):
            # Reset to pending so the crawler will retry
            patch = requests.patch(
                endpoint,
                headers=_supabase_headers(supabase_key, "return=representation"),
                params={
                    "shortPlayId": f"eq.{short_play_id}",
                    "provider": f"eq.{provider}",
                },
                data=json.dumps({
                    "status": "pending",
                    "notes": None,
                    "updated_at": _now_iso(),
                }),
                timeout=30,
            )
            patch.raise_for_status()
            updated = patch.json()
            updated_row = (
                updated[0] if isinstance(updated, list) and updated
                else {**row, "status": "pending", "notes": None}
            )
            return {**updated_row, "_reset_from_error": True}

        return {**row, "_already_exists": True}

    post = requests.post(
        endpoint,
        headers=headers,
        data=json.dumps({
            "shortPlayId": short_play_id,
            "author": [author],
            "status": "pending",
            "provider": provider,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }),
        timeout=30,
    )
    post.raise_for_status()
    result = post.json() if isinstance(post.json(), list) else []
    return result[0] if result else {}


def fetch_queue_requests(
    supabase_url: str,
    supabase_key: str,
    status: str | None = None,
    limit: int | None = 100,
    provider: str | None = None,
) -> list[dict]:
    """Return queue rows ordered newest-first, optionally filtered by status and/or provider."""
    base = _base(supabase_url)
    endpoint = f"{base}/{TABLE_QUEUE}"
    headers = _supabase_headers(supabase_key, "return=representation")
    page_size = min(limit, 1000) if limit is not None else 1000
    params: dict[str, str] = {"select": "*", "order": "created_at.desc"}
    if status:
        params["status"] = f"eq.{status}"
    if provider:
        params["provider"] = f"eq.{provider}"

    rows: list[dict] = []
    offset = 0
    while limit is None or len(rows) < limit:
        request_limit = page_size if limit is None else min(page_size, limit - len(rows))
        page_params = {**params, "limit": str(request_limit), "offset": str(offset)}
        resp = requests.get(endpoint, headers=headers, params=page_params, timeout=30)
        resp.raise_for_status()
        page = resp.json() if isinstance(resp.json(), list) else []
        rows.extend(page)
        if len(page) < request_limit:
            break
        offset += len(page)
    return rows


def fetch_oldest_pending(supabase_url: str, supabase_key: str) -> dict | None:
    """Return the oldest queue row with status='pending', or None if queue empty."""
    base = _base(supabase_url)
    endpoint = f"{base}/{TABLE_QUEUE}"
    headers = _supabase_headers(supabase_key, "return=representation")
    resp = requests.get(
        endpoint,
        headers=headers,
        params={
            "select": "*",
            "status": "eq.pending",
            "order": "created_at.asc",
            "limit": "1",
        },
        timeout=30,
    )
    resp.raise_for_status()
    rows: list[dict] = resp.json() if isinstance(resp.json(), list) else []
    return rows[0] if rows else None


def update_queue_status(
    supabase_url: str,
    supabase_key: str,
    short_play_id: str,
    status: str,
    notes: str | None = None,
    name: str | None = None,
) -> None:
    """PATCH status (and optionally notes, name) on a queue row."""
    base = _base(supabase_url)
    endpoint = f"{base}/{TABLE_QUEUE}"
    headers = _supabase_headers(supabase_key, "return=minimal")
    payload: dict[str, Any] = {"status": status, "updated_at": _now_iso()}
    if notes is not None:
        payload["notes"] = notes[:500]
    if name:
        payload["name"] = name
    if status == "completed":
        payload["completed_at"] = _now_iso()
    resp = requests.patch(
        endpoint,
        headers=headers,
        params={"shortPlayId": f"eq.{short_play_id}"},
        data=json.dumps(payload),
        timeout=30,
    )
    resp.raise_for_status()


# ── Crawl table (nestShort_crawl) ─────────────────────────────────────────────

def upsert_crawl_result(
    supabase_url: str,
    supabase_key: str,
    capture_data: dict,
) -> None:
    """
    Upsert a row in nestShort_crawl from run_capture.py output JSON.

    capture_data must contain 'shortPlayId'.
    When 'episodes' key is present, full dict is stored in 'raw' column.
    """
    base = _base(supabase_url)
    endpoint = f"{base}/{TABLE_CRAWL}"
    headers = _supabase_headers(
        supabase_key,
        "resolution=merge-duplicates,return=minimal",
    )

    short_play_id = capture_data.get("shortPlayId") or capture_data.get("short_play_id", "")
    payload: dict[str, Any] = {
        "shortPlayId": short_play_id,
        "updated_at": _now_iso(),
    }

    for field in ("showName", "total", "captured", "missing_episodes", "status"):
        if field in capture_data:
            payload[field] = capture_data[field]

    if "episodes" in capture_data:
        payload["raw"] = capture_data  # store full JSON in raw column

    resp = requests.post(
        endpoint,
        headers=headers,
        params={"on_conflict": "shortPlayId"},
        data=json.dumps(payload, ensure_ascii=False),
        timeout=30,
    )
    resp.raise_for_status()
