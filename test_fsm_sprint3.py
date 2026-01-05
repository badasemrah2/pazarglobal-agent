"""
Test FSM parked/timeout/hesitation behaviors (Sprint 3)
"""
from __future__ import annotations

import importlib
import types
from typing import Any
from datetime import datetime, timezone, timedelta

import pytest
from _pytest.monkeypatch import MonkeyPatch


def import_webchat(monkeypatch: MonkeyPatch) -> types.ModuleType:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("SUPABASE_URL", "http://localhost")
    monkeypatch.setenv("SUPABASE_KEY", "test")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test")

    import api.webchat as webchat

    return importlib.reload(webchat)


@pytest.mark.asyncio
async def test_parked_flow_after_inactivity(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        def __init__(self):
            self.log_calls: list[dict[str, Any]] = []

        async def log_action(self, action: str, metadata: dict[str, Any], **kwargs) -> bool:
            self.log_calls.append({"action": action, "metadata": metadata})
            return True

    fake_sb = FakeSupabase()
    monkeypatch.setattr(webchat, "supabase_client", fake_sb)

    webchat.IN_MEMORY_SESSION_CACHE.clear()
    session_id = "parked_test"

    # Simulate last_user_at was 11 minutes ago (exceeds FSM_PARK_TIMEOUT_SECONDS=600)
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=11 * 60)).isoformat()
    webchat.IN_MEMORY_SESSION_CACHE[session_id] = {
        "user_id": "u1",
        "intent": "create_listing",
        "locked_intent": "create_listing",
        "active_draft_id": None,
        "pending_media_urls": [],
        "pending_media_analysis": [],
        "fsm_state": "active",
        "fsm_state_reason": None,
        "fsm_state_updated_at": old_ts,
        "fsm_state_intent": None,
        "parked_intent": None,
        "last_user_at": old_ts,
        "last_bot_at": None,
    }

    # User sends a message after long silence → auto-park
    resp = await webchat.process_webchat_message(
        message_body="hello",
        session_id=session_id,
        user_id="u1",
        media_urls=None,
    )

    assert resp["success"] is True
    assert resp["data"]["type"] == "parked"
    assert "park ettim" in resp["message"].lower()

    updated_session = webchat.IN_MEMORY_SESSION_CACHE[session_id]
    assert updated_session["fsm_state"] == "parked"
    assert updated_session["parked_intent"] == "create_listing"
    assert updated_session["locked_intent"] is None

    parked_events = [c for c in fake_sb.log_calls if c["metadata"].get("event") == "parked"]
    assert len(parked_events) == 1, "Should emit one parked event"


@pytest.mark.asyncio
async def test_resume_from_parked(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        async def log_action(self, **kwargs) -> bool:
            return True

    monkeypatch.setattr(webchat, "supabase_client", FakeSupabase())

    webchat.IN_MEMORY_SESSION_CACHE.clear()
    session_id = "resume_test"

    now_iso = datetime.now(timezone.utc).isoformat()
    webchat.IN_MEMORY_SESSION_CACHE[session_id] = {
        "user_id": "u2",
        "intent": None,
        "locked_intent": None,
        "active_draft_id": None,
        "pending_media_urls": [],
        "pending_media_analysis": [],
        "fsm_state": "parked",
        "fsm_state_reason": "inactivity",
        "fsm_state_updated_at": now_iso,
        "fsm_state_intent": "create_listing",
        "parked_intent": "create_listing",
        "last_user_at": now_iso,
        "last_bot_at": None,
    }

    # User says "devam" → resume
    resp = await webchat.process_webchat_message(
        message_body="devam",
        session_id=session_id,
        user_id="u2",
        media_urls=None,
    )

    # Should not return parked prompt; flow unlocked
    updated = webchat.IN_MEMORY_SESSION_CACHE[session_id]
    assert updated["fsm_state"] == "active"
    assert updated["locked_intent"] == "create_listing"
    assert updated["parked_intent"] is None


@pytest.mark.asyncio
async def test_cancel_from_parked(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        async def log_action(self, **kwargs) -> bool:
            return True

    monkeypatch.setattr(webchat, "supabase_client", FakeSupabase())

    webchat.IN_MEMORY_SESSION_CACHE.clear()
    session_id = "cancel_parked"

    now_iso = datetime.now(timezone.utc).isoformat()
    webchat.IN_MEMORY_SESSION_CACHE[session_id] = {
        "user_id": "u3",
        "intent": None,
        "locked_intent": None,
        "active_draft_id": None,
        "pending_media_urls": [],
        "pending_media_analysis": [],
        "fsm_state": "parked",
        "fsm_state_reason": "inactivity",
        "fsm_state_updated_at": now_iso,
        "fsm_state_intent": "search_listings",
        "parked_intent": "search_listings",
        "last_user_at": now_iso,
        "last_bot_at": None,
    }

    resp = await webchat.process_webchat_message(
        message_body="iptal",
        session_id=session_id,
        user_id="u3",
        media_urls=None,
    )

    assert resp["success"] is True
    assert resp["data"]["type"] == "parked_cancel"
    assert "iptal ettim" in resp["message"].lower()

    updated = webchat.IN_MEMORY_SESSION_CACHE[session_id]
    assert updated["fsm_state"] == "active"
    assert updated["parked_intent"] is None
    assert updated["locked_intent"] is None


@pytest.mark.asyncio
async def test_hesitation_marks_fsm_state(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        def __init__(self):
            self.log_calls: list[dict[str, Any]] = []

        async def log_action(self, action: str, metadata: dict[str, Any], **kwargs) -> bool:
            self.log_calls.append({"action": action, "metadata": metadata})
            return True

    fake_sb = FakeSupabase()
    monkeypatch.setattr(webchat, "supabase_client", fake_sb)

    webchat.IN_MEMORY_SESSION_CACHE.clear()
    session_id = "hesitation"

    now_iso = datetime.now(timezone.utc).isoformat()
    webchat.IN_MEMORY_SESSION_CACHE[session_id] = {
        "user_id": "u4",
        "intent": "create_listing",
        "locked_intent": "create_listing",
        "active_draft_id": None,
        "pending_media_urls": [],
        "pending_media_analysis": [],
        "fsm_state": "active",
        "fsm_state_reason": None,
        "fsm_state_updated_at": now_iso,
        "fsm_state_intent": None,
        "parked_intent": None,
        "last_user_at": now_iso,
        "last_bot_at": None,
    }

    resp = await webchat.process_webchat_message(
        message_body="dur bi bakayım",
        session_id=session_id,
        user_id="u4",
        media_urls=None,
    )

    assert resp["success"] is True
    assert resp["data"]["type"] == "hesitation_exit"
    assert "acele yok" in resp["message"].lower()

    updated = webchat.IN_MEMORY_SESSION_CACHE[session_id]
    assert updated["fsm_state"] == "hesitation_exit"
    assert updated["locked_intent"] is None

    hesitation_events = [c for c in fake_sb.log_calls if c["metadata"].get("event") == "hesitation_exit"]
    assert len(hesitation_events) == 1, "Should emit hesitation_exit event"


@pytest.mark.asyncio
async def test_composer_timeout_parks_flow(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        def __init__(self):
            self.log_calls: list[dict[str, Any]] = []

        async def get_latest_draft_for_user(self, user_id: str) -> None:
            return None

        async def log_action(self, action: str, metadata: dict[str, Any], **kwargs) -> bool:
            self.log_calls.append({"action": action, "metadata": metadata})
            return True

    fake_sb = FakeSupabase()
    monkeypatch.setattr(webchat, "supabase_client", fake_sb)

    # Simulate a ComposerAgent that hangs forever
    class SlowComposer:
        async def orchestrate_listing_creation(self, **kwargs):
            import asyncio
            await asyncio.sleep(9999)
            return {"success": True, "draft_id": "never", "draft": {}}

    monkeypatch.setattr(webchat, "ComposerAgent", lambda: SlowComposer())

    webchat.IN_MEMORY_SESSION_CACHE.clear()
    session_id = "timeout_test"

    now_iso = datetime.now(timezone.utc).isoformat()
    webchat.IN_MEMORY_SESSION_CACHE[session_id] = {
        "user_id": "u5",
        "intent": "create_listing",
        "locked_intent": "create_listing",
        "active_draft_id": None,
        "pending_media_urls": [],
        "pending_media_analysis": [],
        "fsm_state": "active",
        "fsm_state_reason": None,
        "fsm_state_updated_at": now_iso,
        "fsm_state_intent": None,
        "parked_intent": None,
        "last_user_at": now_iso,
        "last_bot_at": None,
    }

    resp = await webchat.process_webchat_message(
        message_body="create listing",
        session_id=session_id,
        user_id="u5",
        media_urls=None,
    )

    assert resp["success"] is False
    assert resp["data"]["type"] == "timeout"
    assert "beklemeye aldım" in resp["message"].lower()

    updated = webchat.IN_MEMORY_SESSION_CACHE[session_id]
    assert updated["fsm_state"] == "timeout"
    assert updated["parked_intent"] == "create_listing"
    assert updated["locked_intent"] is None

    timeout_events = [c for c in fake_sb.log_calls if c["metadata"].get("event") == "timeout"]
    assert len(timeout_events) == 1, "Should emit timeout event"
