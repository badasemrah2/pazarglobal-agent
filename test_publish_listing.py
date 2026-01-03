from __future__ import annotations

from typing import Any, Dict, Optional

import importlib
import pytest

from services.supabase_client import SupabaseClient


class _FakeResult:
    def __init__(self, data: Optional[list[dict[str, Any]]] = None):
        self.data = data or []


class _FakeTable:
    def __init__(self, name: str, recorder: dict[str, Any]):
        self.name = name
        self.recorder = recorder
        self._payload: dict[str, Any] | None = None

    def insert(self, payload: dict[str, Any]):
        self._payload = payload
        if self.name == "listings":
            self.recorder["listings_insert"] = payload
        return self

    def delete(self):
        return self

    def update(self, payload: dict[str, Any]):
        # not needed in this test
        self._payload = payload
        return self

    def select(self, *_args: Any, **_kwargs: Any):
        return self

    def eq(self, *_args: Any, **_kwargs: Any):
        return self

    def order(self, *_args: Any, **_kwargs: Any):
        return self

    def limit(self, *_args: Any, **_kwargs: Any):
        return self

    def execute(self):
        if self.name == "listings" and self._payload is not None:
            return _FakeResult([{**self._payload, "id": "listing_1"}])
        return _FakeResult([{"ok": True}])


class _FakeSupabase:
    def __init__(self, recorder: dict[str, Any]):
        self.recorder = recorder

    def table(self, name: str):
        return _FakeTable(name, self.recorder)

    def rpc(self, *_args: Any, **_kwargs: Any):
        raise AssertionError("RPC should not be used in publish_listing")


@pytest.mark.asyncio
async def test_publish_listing_populates_user_fields_and_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: dict[str, Any] = {}

    client = SupabaseClient()
    client._client = _FakeSupabase(recorder)  # type: ignore[attr-defined]

    async def fake_get_draft(_draft_id: str) -> Dict[str, Any] | None:
        return {
            "id": _draft_id,
            "listing_data": {
                "title": "iPhone 14 128GB",
                "description": "Temiz, kutulu.",
                "price": 20000,
                "category": "Elektronik",
                "contact_phone": "+905551234567",
            },
            "images": [
                {"image_url": "https://example.com/a.jpg", "metadata": {}},
            ],
            "vision_product": {"product": "iPhone 14", "category": "Elektronik"},
        }

    async def fake_get_user_display_name(_user_id: str) -> str | None:
        return "Emrah"

    async def fake_get_user_phone(_user_id: str) -> str | None:
        return None

    async def fake_log_action(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(client, "get_draft", fake_get_draft)
    monkeypatch.setattr(client, "get_user_display_name", fake_get_user_display_name)
    monkeypatch.setattr(client, "get_user_phone", fake_get_user_phone)
    monkeypatch.setattr(client, "log_action", fake_log_action)

    # Force keyword generator to return empty so deterministic fallback must kick in.
    async def fake_generate_listing_keywords(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return {"keywords": [], "keywords_text": ""}

    mod = importlib.import_module("services.supabase_client")
    monkeypatch.setattr(mod, "generate_listing_keywords", fake_generate_listing_keywords)

    out = await client.publish_listing("draft_1", "user_1")
    assert out is not None

    payload = recorder.get("listings_insert")
    assert isinstance(payload, dict)

    assert payload.get("user_name") == "Emrah"
    # phone should fall back to contact_phone from draft
    assert payload.get("user_phone") == "+905551234567"

    metadata = payload.get("metadata")
    assert isinstance(metadata, dict)
    assert metadata.get("source") == "agent"
    assert metadata.get("created_via") == "webchat"

    # Must have deterministic keywords
    assert isinstance(metadata.get("keywords"), list)
    assert len(metadata.get("keywords")) > 0
    assert isinstance(metadata.get("keywords_text"), str)
    assert len(metadata.get("keywords_text")) > 0
