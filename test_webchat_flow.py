from __future__ import annotations

import importlib
import types
from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch


def import_webchat(monkeypatch: MonkeyPatch) -> types.ModuleType:
    # Ensure required env vars exist before Settings() is instantiated at import time.
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("SUPABASE_URL", "http://localhost")
    monkeypatch.setenv("SUPABASE_KEY", "test")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test")

    import api.webchat as webchat

    # Reload to ensure it picks up env vars if another test imported it earlier.
    return importlib.reload(webchat)


@pytest.mark.asyncio
async def test_pre_intent_media_buffer_then_create_listing_prompts_next_slot(monkeypatch: MonkeyPatch) -> None:
    # Import here so monkeypatch can replace module globals
    webchat = import_webchat(monkeypatch)

    # --- Fake Supabase client ---
    class FakeSupabase:
        def __init__(self):
            self.drafts: dict[str, dict[str, Any]] = {}
            self._id = 0
            self.reset_calls: list[str] = []

        async def create_draft(self, user_id: str, phone_number: str) -> dict[str, Any]:
            self._id += 1
            draft_id = f"draft_{self._id}"
            self.drafts[draft_id] = {
                "id": draft_id,
                "listing_data": {"title": None, "description": None, "price": None, "category": None},
                "images": [],
                "vision_product": {},
            }
            return self.drafts[draft_id]

        async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
            return self.drafts.get(draft_id)

        async def add_listing_image(self, listing_id: str, image_url: str, metadata: dict[str, Any] | None = None) -> bool:
            d = self.drafts[listing_id]
            d.setdefault("images", []).append({"image_url": image_url, "metadata": metadata or {}})
            return True

        async def update_draft_category(self, draft_id: str, category: str, vision_product: dict[str, Any] | None = None) -> bool:
            d = self.drafts[draft_id]
            d["listing_data"]["category"] = category
            if vision_product is not None:
                d["vision_product"] = vision_product
            return True

        async def update_draft_vision_product(self, draft_id: str, vision_product: dict[str, Any]) -> bool:
            d = self.drafts[draft_id]
            d["vision_product"] = vision_product
            return True

        async def reset_draft(self, draft_id: str, phone_number: str | None = None) -> bool:
            # Mimic production behavior: reset wipes images + listing fields.
            self.reset_calls.append(draft_id)
            d = self.drafts[draft_id]
            d["listing_data"] = {"title": None, "description": None, "price": None, "category": None}
            d["images"] = []
            return True

    fake_supabase = FakeSupabase()
    monkeypatch.setattr(webchat, "supabase_client", fake_supabase)

    # Avoid any real OpenAI call
    async def fake_analyze_media(media_urls: list[str]) -> list[dict[str, Any]]:
        return [{"image_url": media_urls[0], "analysis": {"product": "iPhone 14", "category": "Elektronik", "condition": "İyi Durumda", "features": ["128GB"]}}]

    monkeypatch.setattr(webchat, "analyze_media_with_vision", fake_analyze_media)

    # Ensure clean session cache
    webchat.IN_MEMORY_SESSION_CACHE.clear()

    session_id = "s1"

    # 1) User sends a photo first: should NOT lock intent, should return media analysis prompt.
    r1 = await webchat.process_webchat_message(
        message_body="",
        session_id=session_id,
        user_id="u1",
        media_urls=["https://example.com/img1.jpg"],
    )

    assert r1["success"] is True
    assert r1["data"]["type"] == "media_analysis"
    assert r1["intent"] is None

    # 2) User says 'ilan oluştur': should consume buffered media into a draft and ask next slot.
    r2 = await webchat.process_webchat_message(
        message_body="ilan oluştur",
        session_id=session_id,
        user_id="u1",
        media_urls=None,
    )

    assert r2["success"] is True
    assert r2["intent"] == "create_listing"
    assert r2["data"]["type"] in {"slot_prompt", "draft_update"}

    # Should ask for title next (since we only attached images)
    assert "Ürünün adı" in r2["message"]

    # Regression: should NOT have reset the draft just because vision included a category.
    assert fake_supabase.reset_calls == []


@pytest.mark.asyncio
async def test_command_only_does_not_trigger_hallucinated_title_when_images_exist(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        def __init__(self):
            self.drafts: dict[str, dict[str, Any]] = {
                "d1": {
                    "id": "d1",
                    "listing_data": {"title": None, "description": None, "price": None, "category": None},
                    "images": [{"image_url": "https://example.com/x.jpg", "metadata": {}}],
                    "vision_product": {},
                }
            }

        async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
            return self.drafts.get(draft_id)

        async def create_draft(self, user_id: str, phone_number: str) -> dict[str, Any]:
            return self.drafts["d1"]

        async def add_listing_image(self, listing_id: str, image_url: str, metadata: dict[str, Any] | None = None) -> bool:
            return True

        async def update_draft_category(self, draft_id: str, category: str, vision_product: dict[str, Any] | None = None) -> bool:
            return True

    fake_supabase = FakeSupabase()
    monkeypatch.setattr(webchat, "supabase_client", fake_supabase)

    # Ensure clean session cache and set an active draft with images
    webchat.IN_MEMORY_SESSION_CACHE.clear()
    webchat.IN_MEMORY_SESSION_CACHE["s2"] = {
        "user_id": "u2",
        "intent": "create_listing",
        "locked_intent": "create_listing",
        "active_draft_id": "d1",
        "pending_media_urls": [],
        "pending_media_analysis": [],
    }

    # If Composer is called here, we want the test to fail (this is the regression we fixed).
    class BoomComposer:
        async def orchestrate_listing_creation(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("ComposerAgent should not run on command-only when images exist")

    monkeypatch.setattr(webchat, "ComposerAgent", lambda: BoomComposer())

    r = await webchat.process_webchat_message(
        message_body="ilan oluştur",
        session_id="s2",
        user_id="u2",
        media_urls=None,
    )

    assert r["success"] is True
    assert "Ürünün adı" in r["message"]


@pytest.mark.asyncio
async def test_missing_user_id_uses_session_id_stable_identity(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        def __init__(self):
            self.drafts: dict[str, dict[str, Any]] = {}
            self._id = 0
            self.created_user_ids: list[str] = []
            self.reset_calls: list[str] = []

        async def create_draft(self, user_id: str, phone_number: str) -> dict[str, Any]:
            self.created_user_ids.append(user_id)
            self._id += 1
            draft_id = f"draft_{self._id}"
            self.drafts[draft_id] = {
                "id": draft_id,
                "listing_data": {"title": None, "description": None, "price": None, "category": None},
                "images": [],
                "vision_product": {},
            }
            return self.drafts[draft_id]

        async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
            return self.drafts.get(draft_id)

        async def get_latest_draft_for_user(self, user_id: str) -> dict[str, Any] | None:
            # Return the latest draft regardless; test only needs stable flow.
            if not self.drafts:
                return None
            # Draft ids are monotonic in this fake.
            latest_id = sorted(self.drafts.keys())[-1]
            return self.drafts[latest_id]

        async def add_listing_image(self, listing_id: str, image_url: str, metadata: dict[str, Any] | None = None) -> bool:
            d = self.drafts[listing_id]
            d.setdefault("images", []).append({"image_url": image_url, "metadata": metadata or {}})
            return True

        async def update_draft_category(self, draft_id: str, category: str, vision_product: dict[str, Any] | None = None) -> bool:
            d = self.drafts[draft_id]
            d["listing_data"]["category"] = category
            if vision_product is not None:
                d["vision_product"] = vision_product
            return True

        async def update_draft_vision_product(self, draft_id: str, vision_product: dict[str, Any]) -> bool:
            d = self.drafts[draft_id]
            d["vision_product"] = vision_product
            return True

        async def reset_draft(self, draft_id: str, phone_number: str | None = None) -> bool:
            # If production code incorrectly calls reset on 'ilan oluştur', we'd lose images and loop.
            self.reset_calls.append(draft_id)
            d = self.drafts[draft_id]
            d["listing_data"] = {"title": None, "description": None, "price": None, "category": None}
            d["images"] = []
            return True

    fake_supabase = FakeSupabase()
    monkeypatch.setattr(webchat, "supabase_client", fake_supabase)

    async def fake_analyze_media(media_urls: list[str]) -> list[dict[str, Any]]:
        return [{"image_url": media_urls[0], "analysis": {"product": "Citroen", "category": "Otomotiv"}}]

    monkeypatch.setattr(webchat, "analyze_media_with_vision", fake_analyze_media)

    webchat.IN_MEMORY_SESSION_CACHE.clear()

    session_id = "web_session_abc"

    # Media arrives first, but the caller omits user_id.
    r1 = await webchat.process_webchat_message(
        message_body="",
        session_id=session_id,
        user_id=None,
        media_urls=["https://example.com/img1.jpg"],
    )
    assert r1["success"] is True
    assert r1["data"]["type"] == "media_analysis"

    # Then the user says 'ilan oluştur' again without user_id.
    r2 = await webchat.process_webchat_message(
        message_body="ilan oluştur",
        session_id=session_id,
        user_id=None,
        media_urls=None,
    )

    assert r2["success"] is True
    assert r2["intent"] == "create_listing"
    # Should *not* loop back to requesting photos again.
    assert "fotoğraf" not in r2["message"].lower()
    assert "Ürünün adı" in r2["message"]
    assert fake_supabase.reset_calls == []


@pytest.mark.asyncio
async def test_global_cancel_resets_locked_intent_and_draft(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        def __init__(self):
            self.drafts: dict[str, dict[str, Any]] = {
                "d1": {
                    "id": "d1",
                    "listing_data": {"title": "Eski", "description": "Eski açıklama", "price": 10, "category": "Elektronik"},
                    "images": [{"image_url": "https://example.com/x.jpg", "metadata": {}}],
                    "vision_product": {"product": "Old"},
                }
            }
            self.reset_called_with: list[tuple[str, str]] = []
            self.cleared_pending_publish: list[str] = []

        async def get_latest_draft_for_user(self, user_id: str) -> dict[str, Any] | None:
            return self.drafts.get("d1")

        async def reset_draft(self, draft_id: str, phone_number: str | None = None) -> bool:
            self.reset_called_with.append((draft_id, phone_number or ""))
            d = self.drafts[draft_id]
            d["listing_data"] = {"title": None, "description": None, "price": None, "category": None}
            d["images"] = []
            d["vision_product"] = {}
            return True

        async def clear_pending_publish_state(self, draft_id: str) -> bool:
            self.cleared_pending_publish.append(draft_id)
            return True

    fake_supabase = FakeSupabase()
    monkeypatch.setattr(webchat, "supabase_client", fake_supabase)

    webchat.IN_MEMORY_SESSION_CACHE.clear()
    webchat.IN_MEMORY_SESSION_CACHE["s_cancel"] = {
        "user_id": "u_cancel",
        "intent": "create_listing",
        "locked_intent": "create_listing",
        "active_draft_id": "d1",
        "pending_media_urls": ["https://example.com/x.jpg"],
        "pending_media_analysis": [{"image_url": "https://example.com/x.jpg", "analysis": {"product": "x"}}],
    }

    r = await webchat.process_webchat_message(
        message_body="satmaktan vazgeçtim",
        session_id="s_cancel",
        user_id="u_cancel",
        media_urls=None,
    )

    assert r["success"] is True
    assert r["intent"] == "small_talk"
    assert fake_supabase.reset_called_with, "Draft should be reset on global cancel"
    assert fake_supabase.cleared_pending_publish == ["d1"]


@pytest.mark.asyncio
async def test_locked_create_listing_search_command_prompts_cancel_hint(monkeypatch: MonkeyPatch) -> None:
    webchat = import_webchat(monkeypatch)

    class FakeSupabase:
        async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
            return {
                "id": draft_id,
                "listing_data": {"title": None, "description": None, "price": None, "category": None},
                "images": [{"image_url": "https://example.com/x.jpg", "metadata": {}}],
                "vision_product": {},
            }

        async def get_latest_draft_for_user(self, user_id: str) -> dict[str, Any] | None:
            return None

    monkeypatch.setattr(webchat, "supabase_client", FakeSupabase())

    webchat.IN_MEMORY_SESSION_CACHE.clear()
    webchat.IN_MEMORY_SESSION_CACHE["s_locked"] = {
        "user_id": "u_locked",
        "intent": "create_listing",
        "locked_intent": "create_listing",
        "active_draft_id": "d_locked",
        "pending_media_urls": [],
        "pending_media_analysis": [],
    }

    r = await webchat.process_webchat_message(
        message_body="benzer ara",
        session_id="s_locked",
        user_id="u_locked",
        media_urls=None,
    )

    assert r["success"] is True
    assert r["intent"] == "create_listing"
    assert "iptal" in r["message"].lower()
