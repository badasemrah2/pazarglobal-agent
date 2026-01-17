"""Session + cache storage helpers for WebChat.

Goal: keep `webchat.py` focused on request/flow logic by extracting storage concerns.
This module provides a Redis-backed store with an in-memory fallback when Redis is disabled.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services import redis_client


# In-memory cache for last search results (when Redis is disabled)
LAST_SEARCH_CACHE: Dict[str, List[Any]] = {}

# Local session cache fallback when Redis is disabled
IN_MEMORY_SESSION_CACHE: Dict[str, Dict[str, Any]] = {}


def redis_is_disabled() -> bool:
    """Centralize redis enabled/disabled checks."""

    return bool(getattr(redis_client, "disabled", False))


async def load_session_state(session_id: str) -> Optional[Dict[str, Any]]:
    """Load session either from Redis or in-memory fallback."""

    if redis_is_disabled():
        return IN_MEMORY_SESSION_CACHE.get(session_id)
    return await redis_client.get_session(session_id)


async def persist_session_state(session_id: str, session: Dict[str, Any]) -> None:
    """Persist session state regardless of backend availability."""

    if redis_is_disabled():
        IN_MEMORY_SESSION_CACHE[session_id] = session
        return
    await redis_client.set_session(session_id, session)


def remove_session_state(session_id: str) -> None:
    """Remove session from fallback cache when Redis is disabled."""

    if redis_is_disabled():
        IN_MEMORY_SESSION_CACHE.pop(session_id, None)
