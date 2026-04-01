import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from routers.gateway_v3 import (  # noqa: E402
    _detect_enrichment_action,
    _detect_prohibited_listing_term,
    _format_search_continuation_page,
    _parse_edit_updates,
    _should_try_direct_edit,
)
from agents.search_agents import SearchComposerAgent  # noqa: E402
from services.example_listings import EXAMPLE_LISTING_OWNER_ID  # noqa: E402
from services.vision_service import vision_service  # noqa: E402


def test_parse_natural_price_edit_phrase():
    updates, errors = _parse_edit_updates("fiyatı 12500 yap")

    assert updates == {"price": 12500}
    assert errors == []
    assert _should_try_direct_edit("fiyatı 12500 yap") is True


def test_parse_natural_location_edit_phrase():
    updates, errors = _parse_edit_updates("lokasyonu Kadıköy olarak değiştir")

    assert updates == {"location": "Kadıköy"}
    assert errors == []
    assert _should_try_direct_edit("lokasyonu Kadıköy olarak değiştir") is True


def test_title_refinement_phrase_stays_out_of_direct_edit_path():
    updates, errors = _parse_edit_updates("başlığı daha güzel yap")

    assert updates == {}
    assert errors == []
    assert _should_try_direct_edit("başlığı daha güzel yap") is False


def test_detect_enrichment_action_handles_inflected_title_and_description():
    assert _detect_enrichment_action("başlığı daha güzel yap") == "suggest_title"
    assert _detect_enrichment_action("başlığı yeniden yaz") == "suggest_title"
    assert _detect_enrichment_action("açıklamayı daha profesyonel yaz") == "improve_text"


def test_publish_text_guard_does_not_flag_turkish_gun_word():
    listing = {
        "title": "2 gün kullanıldı iPhone 13",
        "description": "Çok temiz, sadece 3 gün kullanıldı.",
    }

    assert _detect_prohibited_listing_term(listing) is None


def test_vision_prohibited_product_does_not_flag_turkish_gun_word():
    analysis = {"product": "2 gün kullanılmış telefon", "category": "Elektronik"}

    assert vision_service.detect_prohibited_product(analysis) is None


def test_vision_prohibited_product_still_flags_real_weapon_terms():
    analysis = {"product": "Tabanca seti", "category": "Diğer"}

    assert vision_service.detect_prohibited_product(analysis) == "tabanca"


async def test_format_search_continuation_page_marks_example_listing(monkeypatch):
    async def fake_ensure_contact_token_for_listing(_: str):
        return None

    monkeypatch.setattr(
        "routers.gateway_v3.supabase_client.ensure_contact_token_for_listing",
        fake_ensure_contact_token_for_listing,
    )

    text = await _format_search_continuation_page(
        [
            {
                "id": "example-1",
                "title": "Demo Telefon",
                "price": 18000,
                "category": "Elektronik",
                "description": "Temiz cihaz",
                "user_id": EXAMPLE_LISTING_OWNER_ID,
            },
            {
                "id": "normal-1",
                "title": "Normal Telefon",
                "price": 17500,
                "category": "Elektronik",
                "description": "Normal cihaz",
                "user_id": "11111111-1111-1111-1111-111111111111",
            },
        ],
        start_idx=0,
        page_size=2,
    )

    assert "1. [Örnek İlan] Demo Telefon - 18000 TL - Elektronik" in text
    assert "2. Normal Telefon - 17500 TL - Elektronik" in text


async def test_search_agent_message_marks_example_listing(monkeypatch):
    async def fake_search_execute(**_: object):
        return {
            "success": True,
            "data": {
                "listings": [
                    {
                        "id": "example-1",
                        "title": "Demo Telefon",
                        "price": 18000,
                        "category": "Elektronik",
                        "description": "Temiz cihaz",
                        "user_id": EXAMPLE_LISTING_OWNER_ID,
                    },
                    {
                        "id": "normal-1",
                        "title": "Normal Telefon",
                        "price": 17500,
                        "category": "Elektronik",
                        "description": "Normal cihaz",
                        "user_id": "11111111-1111-1111-1111-111111111111",
                    },
                ]
            },
        }

    async def fake_market_execute(**_: object):
        return {"success": False, "data": {}}

    async def fake_ensure_contact_token_for_listing(_: str):
        return None

    monkeypatch.setattr("agents.search_agents.search_listings_tool.execute", fake_search_execute)
    monkeypatch.setattr("agents.search_agents.market_price_tool.execute", fake_market_execute)
    monkeypatch.setattr(
        "agents.search_agents.supabase_client.ensure_contact_token_for_listing",
        fake_ensure_contact_token_for_listing,
    )

    agent = SearchComposerAgent()
    result = await agent.orchestrate_search("iphone")

    assert result["success"] is True
    assert "1️⃣ [Örnek İlan] Demo Telefon - 18000 TL - Elektronik" in result["message"]
    assert "2️⃣ Normal Telefon - 17500 TL - Elektronik" in result["message"]