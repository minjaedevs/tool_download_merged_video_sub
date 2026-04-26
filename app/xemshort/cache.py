"""XemShort fetch cache (30-min TTL, in-memory)."""
from __future__ import annotations

import time

# key: cache_key(str)  →  value: (episodes, movie_name, timestamp_float)
_XS_FETCH_CACHE: dict[str, tuple[list, str, float]] = {}
_XS_FETCH_CACHE_TTL = 30 * 60   # 30 minutes in seconds

# Backward-compat aliases (same dict object — mutations are shared)
_NS_FETCH_CACHE = _XS_FETCH_CACHE
_NS_FETCH_CACHE_TTL = _XS_FETCH_CACHE_TTL


def _ns_cache_key(api_url: str, movie_id: str) -> str:
    """Stable cache key from api_url + movie_id."""
    return f"{api_url.rstrip('/')}|{movie_id}"


def _ns_cache_get(key: str) -> tuple[list, str] | None:
    """Return (episodes, movie_name) if cache hit and not expired, else None."""
    entry = _XS_FETCH_CACHE.get(key)
    if entry is None:
        return None
    episodes, movie_name, ts = entry
    if time.time() - ts > _XS_FETCH_CACHE_TTL:
        del _XS_FETCH_CACHE[key]
        return None
    return episodes, movie_name


def _ns_cache_set(key: str, episodes: list, movie_name: str) -> None:
    """Store result in cache with current timestamp."""
    _XS_FETCH_CACHE[key] = (episodes, movie_name, time.time())


def _ns_cache_evict_expired() -> int:
    """Remove all expired entries. Returns number of entries removed."""
    now = time.time()
    expired = [k for k, (_, _, ts) in _XS_FETCH_CACHE.items()
               if now - ts > _XS_FETCH_CACHE_TTL]
    for k in expired:
        del _XS_FETCH_CACHE[k]
    return len(expired)


def _ns_cache_clear() -> int:
    """Clear all cache entries. Returns number of entries removed."""
    count = len(_XS_FETCH_CACHE)
    _XS_FETCH_CACHE.clear()
    return count
