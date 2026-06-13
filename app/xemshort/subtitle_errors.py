"""Supabase logging for subtitle download/merge problems."""
from __future__ import annotations

import json
import os
from typing import Any

import requests

from .models import XSEpisode, XSMovie
from .sync_movies_supabase import _supabase_headers, load_env_file


def _episode_display_name(movie: XSMovie, episode: XSEpisode) -> str:
    """Return a stable episode label for Supabase summary columns."""
    return f"T\u1eadp {episode.episode}" if episode.episode else (episode.name or movie.name)


def subtitle_error_connection() -> tuple[str, str] | None:
    """Return Supabase connection from .env/environment, or None when disabled."""
    load_env_file()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        return None
    return supabase_url.rstrip("/"), supabase_key


def upsert_subtitle_error(
    source: str,
    movie: XSMovie,
    episode: XSEpisode,
    note: str,
    raw: dict[str, Any] | None = None,
    timeout: int = 15,
) -> bool:
    """Upsert one subtitle error into Supabase. Returns False when disabled/failed."""
    if not movie.movie_id:
        return False
    conn = subtitle_error_connection()
    if conn is None:
        return False

    supabase_url, supabase_key = conn
    endpoint = f"{supabase_url}/rest/v1/rpc/upsert_subtitle_error_movie"
    headers = _supabase_headers(supabase_key, prefer="return=minimal")
    raw_payload = dict(raw or {})
    if episode.name and episode.name != movie.name:
        raw_payload.setdefault("api_episode_name", episode.name)
    payload = {
        "p_source": source,
        "p_play_id": movie.movie_id,
        "p_movie_name": movie.name,
        "p_episode_id": episode.id,
        "p_episode_number": episode.episode,
        "p_episode_name": _episode_display_name(movie, episode),
        "p_error_note": note,
        "p_raw": raw_payload,
    }
    response = requests.post(
        endpoint,
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=timeout,
    )
    response.raise_for_status()
    return True
