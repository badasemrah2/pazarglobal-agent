import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from routers.gateway_v3 import (  # noqa: E402
    _apply_description_removal_request,
    _apply_drafting_edit_request,
    _classify_draft_message_intent,
    _collect_description_validation_errors,
    _detect_enrichment_action,
    _detect_prohibited_listing_term,
    _format_preview,
    _format_search_continuation_page,
    _handle_enrichment_action,
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


def test_parse_edit_with_leading_context_prefix_routes_to_field_update():
    updates, errors = _parse_edit_updates("Açıklamayı düzelt fiyatı 750 TL yap")

    assert updates == {"price": 750}
    assert errors == []
    assert _classify_draft_message_intent("Açıklamayı düzelt fiyatı 750 TL yap") == "field_update"


def test_title_refinement_phrase_stays_out_of_direct_edit_path():
    updates, errors = _parse_edit_updates("başlığı daha güzel yap")

    assert updates == {}
    assert errors == []
    assert _should_try_direct_edit("başlığı daha güzel yap") is False


def test_parse_condition_edit_accepts_compact_second_hand_alias():
    updates, errors = _parse_edit_updates("durum: 2.el")

    assert updates == {"condition": "2. El"}
    assert errors == []
    assert _should_try_direct_edit("durum: 2.el") is True


async def test_apply_drafting_edit_request_accepts_multi_line_bundle_with_second_hand_alias(monkeypatch):
    async def fake_save_session(*_: object, **__: object):
        return None

    monkeypatch.setattr("routers.gateway_v3.save_session", fake_save_session)

    session = {
        "listing_data": {
            "title": "Citroen c3 benzinli otomatik 2020",
            "price": 1150000,
            "category": "Otomotiv",
        },
        "state": "DRAFTING",
        "fsm_state": "DRAFTING",
    }

    response = await _apply_drafting_edit_request(
        "user-1",
        "webchat",
        session,
        (
            "Açıklama: Citroen c3 benzinli otomatik 2020 model hatasız boyasız tramer yok.\n"
            "Durum: 2.el\n"
            "Konum: Ankara"
        ),
    )

    assert response is not None
    assert response.success is True
    assert response.metadata["ready_for_publish"] is True
    assert response.listing_preview is not None
    assert response.listing_preview["condition"] == "2. El"
    assert response.listing_preview["location"] == "Ankara"


def test_detect_enrichment_action_handles_inflected_title_and_description():
    assert _detect_enrichment_action("başlığı daha güzel yap") == "suggest_title"
    assert _detect_enrichment_action("başlığı yeniden yaz") == "suggest_title"
    assert _detect_enrichment_action("açıklamayı daha profesyonel yaz") == "improve_text"


def test_classify_description_removal_intent():
    assert _classify_draft_message_intent("açıklamadan hasarlı kelimesini sil") == "remove_description_text"


def test_description_validator_flags_price_and_unconfirmed_claims():
    errors = _collect_description_validation_errors(
        {
            "title": "iPhone 13",
            "description": "Temiz cihaz. 500 TL. Kutulu, sertifikalı ve 2021 sınırlı üretim.",
            "condition": "2. El",
        },
        confirmed_claims=set(),
    )

    assert "Açıklamada fiyat yazılamaz." in errors
    assert "Doğrulanmamış bilgi: kutu" in errors
    assert "Doğrulanmamış bilgi: sertifika" in errors
    assert "Doğrulanmamış bilgi: 2021" in errors


def test_format_preview_can_show_full_description_when_requested():
    description = (
        "Satılık citroen c3 benzinli otomatik - 2020 model\n\n"
        "Hatasız, boyasız ve tramer kaydı yok. Tüm periyodik bakımları düzenli olarak yapıldı. "
        "Aracın 4 lastiği yeni değiştirildi."
    )

    preview = _format_preview(
        {
            "title": "Citroen c3 benzinli otomatik 2020 model",
            "price": 1200000,
            "category": "Otomotiv",
            "description": description,
            "condition": "2. El",
            "location": "Bursa",
            "images": ["one", "two", "three"],
        },
        show_full_description=True,
    )

    assert "✅ Açıklama:" in preview
    assert description in preview
    assert "Tüm periyodik bakımları düzenli olarak yapıldı." in preview


async def test_handle_enrichment_action_returns_full_improved_description(monkeypatch):
    improved_description = (
        "Satılık citroen c3 benzinli otomatik - 2020 model\n\n"
        "Hatasız, boyasız ve tramer kaydı yok. Tüm periyodik bakımları düzenli olarak yapıldı.\n\n"
        "Detaylar ve görüşme için lütfen mesaj atın."
    )

    async def fake_edge_call(*_: object, **__: object):
        return {"success": True, "result": improved_description}

    async def fake_save_session(*_: object, **__: object):
        return None

    monkeypatch.setattr("routers.gateway_v3.supabase_client._call_edge_function", fake_edge_call)
    monkeypatch.setattr("routers.gateway_v3.save_session", fake_save_session)

    session = {
        "listing_data": {
            "title": "Citroen c3 benzinli otomatik 2020 model",
            "description": "Kısa açıklama",
            "price": 1200000,
            "category": "Otomotiv",
            "condition": "2. El",
            "location": "Bursa",
            "images": ["one", "two", "three"],
        },
        "state": "READY",
        "fsm_state": "DRAFTING",
    }

    response = await _handle_enrichment_action("user-1", "webchat", session, "improve_text")

    assert response is not None
    assert response.success is True
    assert "Tüm periyodik bakımları düzenli olarak yapıldı." in response.text
    assert "Detaylar ve görüşme için lütfen mesaj atın." in response.text
    assert response.listing_preview is not None
    assert response.listing_preview["description"] == improved_description.replace("\n\n", "\n")


async def test_handle_enrichment_action_strips_unconfirmed_claims(monkeypatch):
    async def fake_edge_call(*_: object, **__: object):
        return {
            "success": True,
            "result": "Temiz cihaz. Uzun süre sorunsuz kullanıldı. 500 TL. Kutulu. Sertifikalı. 2021 sınırlı üretim.",
        }

    async def fake_save_session(*_: object, **__: object):
        return None

    monkeypatch.setattr("routers.gateway_v3.supabase_client._call_edge_function", fake_edge_call)
    monkeypatch.setattr("routers.gateway_v3.save_session", fake_save_session)

    session = {
        "listing_data": {
            "title": "iPhone 13",
            "description": "Temiz cihaz ve sorunsuz kullanım.",
            "price": 18000,
            "category": "Elektronik",
            "condition": "2. El",
            "location": "Bursa",
        },
        "state": "READY",
        "fsm_state": "DRAFTING",
    }

    response = await _handle_enrichment_action("user-1", "webchat", session, "improve_text")

    assert response is not None
    assert response.listing_preview is not None
    assert "500" not in response.listing_preview["description"]
    assert "Kutulu" not in response.listing_preview["description"]
    assert "Sertifikalı" not in response.listing_preview["description"]
    assert "2021" not in response.listing_preview["description"]
    assert "Temiz cihaz" in response.listing_preview["description"]


async def test_apply_description_removal_request_removes_phrase(monkeypatch):
    async def fake_save_session(*_: object, **__: object):
        return None

    monkeypatch.setattr("routers.gateway_v3.save_session", fake_save_session)

    session = {
        "listing_data": {
            "title": "iPhone 13",
            "description": "Temiz cihaz, hasarlı değil, kutusuz gönderilecek.",
            "price": 18000,
            "category": "Elektronik",
            "condition": "2. El",
            "location": "Bursa",
        },
        "state": "READY",
        "fsm_state": "DRAFTING",
    }

    response = await _apply_description_removal_request(
        "user-1",
        "webchat",
        session,
        "açıklamadan hasarlı değil ifadesini sil",
    )

    assert response is not None
    assert response.listing_preview is not None
    assert "hasarlı değil" not in response.listing_preview["description"].lower()


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