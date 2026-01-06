"""Centralized logging utilities for structured FSM/agent telemetry."""
from __future__ import annotations

import os
import sys
import uuid
from typing import Any, Dict, Optional, Tuple

from loguru import logger as _logger

from config import settings

_LOGGER_CONFIGURED = False
_DEFAULT_FORMAT = os.getenv("LOG_FORMAT", "json").lower()
_FSM_DEBUG = os.getenv("FSM_DEBUG", "0").lower() in {"1", "true", "yes", "on"}


def configure_logging() -> None:
    """Configure loguru sink once with JSON-friendly defaults for Railway."""

    global _LOGGER_CONFIGURED
    if _LOGGER_CONFIGURED:
        return

    _logger.remove()
    level = (settings.log_level or "INFO").upper()

    if _DEFAULT_FORMAT == "json":
        _logger.add(
            sys.stdout,
            level=level,
            serialize=True,
            backtrace=False,
            diagnose=False,
            enqueue=True,
        )
    else:
        fmt = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        _logger.add(
            sys.stdout,
            level=level,
            format=fmt,
            colorize=settings.api_env != "production",
            backtrace=False,
            diagnose=False,
        )

    _LOGGER_CONFIGURED = True


configure_logging()


def get_logger(module: Optional[str] = None, **bound: Any):
    """Return a logger bound with optional module/context fields."""

    configure_logging()
    logger = _logger
    if module:
        logger = logger.bind(module=module)
    if bound:
        logger = logger.bind(**bound)
    return logger


def ensure_session_trace(session: Optional[Dict[str, Any]]) -> Tuple[str, bool]:
    """Ensure the session dict includes a stable trace_id used across events."""

    if isinstance(session, dict):
        trace = session.get("trace_id")
        if trace:
            return str(trace), False
        trace = uuid.uuid4().hex
        session["trace_id"] = trace
        return trace, True
    return uuid.uuid4().hex, False


def _session_snapshot(session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(session, dict):
        return {}
    active_listing_ctx = session.get("active_listing_context") or {}
    active_listing_id = None
    if isinstance(active_listing_ctx, dict):
        active_listing_id = active_listing_ctx.get("listing_id")
        if not active_listing_id:
            listing = active_listing_ctx.get("listing")
            if isinstance(listing, dict):
                active_listing_id = listing.get("id") or listing.get("listing_id")
    snapshot = {
        "intent": session.get("intent"),
        "locked_intent": session.get("locked_intent"),
        "fsm_state": session.get("fsm_state"),
        "fsm_state_reason": session.get("fsm_state_reason"),
        "context_mode": session.get("context_mode"),
        "draft_id": session.get("active_draft_id"),
        "active_listing_id": active_listing_id,
        "user_id": session.get("user_id"),
    }
    return {k: v for k, v in snapshot.items() if v not in (None, "")}


def bind_session_logger(
    session_id: Optional[str],
    session: Optional[Dict[str, Any]],
    **extra: Any,
):
    """Bind a logger with session + trace context for ad-hoc debug lines."""

    trace_id, _ = ensure_session_trace(session)
    context = {
        "trace_id": trace_id,
        "session_id": session_id,
        **_session_snapshot(session),
    }
    context.update({k: v for k, v in extra.items() if v is not None})
    return get_logger("session").bind(**context)


def log_fsm_event(
    event: str,
    session_id: Optional[str],
    session: Optional[Dict[str, Any]],
    *,
    level: str = "INFO",
    debug_only: bool = False,
    **fields: Any,
) -> None:
    """Emit a structured FSM/agent telemetry event."""

    if debug_only and not _FSM_DEBUG:
        return

    trace_id, _ = ensure_session_trace(session)
    payload = {
        "event": event,
        "trace_id": trace_id,
        "session_id": session_id,
        **_session_snapshot(session),
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    lvl = (level or "INFO").upper()
    get_logger("fsm").bind(**payload).log(lvl, "fsm_event")


__all__ = [
    "bind_session_logger",
    "configure_logging",
    "ensure_session_trace",
    "get_logger",
    "log_fsm_event",
]
