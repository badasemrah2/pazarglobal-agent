from __future__ import annotations

import importlib
import types

import pytest
from _pytest.monkeypatch import MonkeyPatch


def import_supabase_client(monkeypatch: MonkeyPatch) -> types.ModuleType:
    # Ensure required env vars exist before Settings() is instantiated at import time.
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test")

    module = importlib.import_module("services.supabase_client")
    module = importlib.reload(module)

    # Ensure deterministic base URL regardless of previously-imported Settings() state.
    monkeypatch.setattr(module.settings, "supabase_url", "https://example.supabase.co", raising=False)
    return module


def test_extracts_url_from_nested_json_string(monkeypatch: MonkeyPatch) -> None:
    supabase_client = import_supabase_client(monkeypatch)
    client = supabase_client.SupabaseClient()

    nested = (
        '{"image_url":"{\\"image_url\\":\\"https://snovwbffwvmkgjulrtsm.supabase.co/storage/v1/object/public/product-images/905412879705/webchat_x/abc.jpg\\",'
        '\\"metadata\\":{\\"analysis\\":{\\"product\\":\\"Dell Dizüstü Bilgisayar\\"}}}","metadata":{}}'
    )

    norm = client._normalize_image_entry(nested)
    assert norm is not None
    assert norm["image_url"].startswith("https://")
    assert norm["image_url"].endswith(".jpg")


def test_converts_storage_path_to_public_url(monkeypatch: MonkeyPatch) -> None:
    supabase_client = import_supabase_client(monkeypatch)
    client = supabase_client.SupabaseClient()

    path = "905412879705/temp_1766742178613/1766742178613_arv4r0dxi.jpg"
    norm = client._normalize_image_entry(path)
    assert norm is not None
    assert norm["image_url"].startswith("https://example.supabase.co/storage/v1/object/public/product-images/")


def test_extracts_url_from_markdown_image(monkeypatch: MonkeyPatch) -> None:
    supabase_client = import_supabase_client(monkeypatch)
    client = supabase_client.SupabaseClient()

    md = "![Dell](https://example.com/a.jpg)"
    norm = client._normalize_image_entry(md)
    assert norm is not None
    assert norm["image_url"] == "https://example.com/a.jpg"
