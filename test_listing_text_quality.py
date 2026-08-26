"""Regression tests for listing copy quality and the conversational gates.

Two classes of defect are pinned here.

Text: normalize_keyboard_text lowercased every letter it touched, and it runs over titles
and descriptions on the display path. Every listing published through the agent came out
with its brands, acronyms and units flattened - production rows read "Bmw 2012 f30" and
"Apple iphone 14 pro 128gb".

Gates: several pre-LLM shortcuts matched on substrings, so ordinary drafting messages were
answered by the wrong handler - most damagingly a bare "yok" (a normal answer to "kutusu
var mı?") destroyed the whole draft.
"""
import re

import pytest

from core.brain import Guardrails
from routers.gateway_v3 import (
    FSM_COMMANDS,
    FSMEngine,
    _apply_structured_prefill_to_listing,
    _is_show_more_command,
    _looks_like_price_research_request,
)
from services.text_normalization import normalize_for_match, normalize_keyboard_text


# ── Text quality ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "2012 model BMW F30 316i, NBT var. 375.000 KM.",
        "Apple iPhone 15 Pro 256GB",
        "İstanbul Kadıköy",
        "ÇAĞRI ŞÜKRÜ ÖZGÜR",
        "IKEA BILLY Kitaplık",
    ],
)
def test_keyboard_normalisation_preserves_case(text):
    assert normalize_keyboard_text(text) == text


def test_keyboard_normalisation_still_strips_diacritics():
    assert normalize_keyboard_text("Café Crème naïve") == "Cafe Creme naive"


def test_matching_is_still_case_insensitive():
    """Search callers lowercase themselves, so preserving case must not break matching."""
    assert normalize_keyboard_text("BMW").lower() == "bmw"
    assert normalize_for_match("BMW") == "bmw"


def test_validate_does_not_recase_copy_that_is_already_cased():
    listing = {
        "title": "Apple iPhone 15 Pro 256GB",
        "description": "NBT sistemi mevcuttur ve 375.000 KM'dedir. Kutusu ile birlikte.",
        "price": 1000,
        "location": "İstanbul",
    }
    FSMEngine.validate(listing)

    assert listing["title"] == "Apple iPhone 15 Pro 256GB"
    assert "NBT" in listing["description"]
    assert "KM" in listing["description"]
    assert listing["location"] == "İstanbul"


def test_validate_still_sentence_cases_all_lowercase_input():
    """The cleanup this was written for - a seller typing everything in lowercase."""
    listing = {
        "title": "satılık bisiklet çantası",
        "description": "su geçirmez, 20 litre. sırt askısı var.",
        "price": 1000,
        "location": "izmir",
    }
    FSMEngine.validate(listing)

    assert listing["title"].startswith("S")
    assert listing["location"] == "İzmir"


# ── Conversational gates ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message",
    ["yok", "hayır", "olmaz", "kutusu yok", "sunroof yok", "bu rengi istemiyorum"],
)
def test_ordinary_negative_answers_do_not_cancel_the_draft(message):
    """detect_cancel leads to clear_session, so a plain "no" used to wipe the listing."""
    assert Guardrails.detect_cancel(message) is False


@pytest.mark.parametrize(
    "message",
    ["iptal", "iptal et", "vazgeçtim", "ilanı iptal et", "ilan vermek istemiyorum"],
)
def test_real_cancel_requests_still_cancel(message):
    assert Guardrails.detect_cancel(message) is True


@pytest.mark.parametrize("message", ["devam", "daha fazla", "devamını göster", "sonraki"])
def test_pagination_command_is_recognised(message):
    assert _is_show_more_command(message) is True


@pytest.mark.parametrize(
    "message",
    ["devam edelim, konum İstanbul", "devamlı kullandım", "devam ediyorum ama fiyat 500"],
)
def test_drafting_message_is_not_mistaken_for_pagination(message):
    assert _is_show_more_command(message) is False


@pytest.mark.parametrize(
    "message",
    ["bu kaç para eder", "fiyatı ne kadar", "piyasa değeri nedir", "fiyat araştır"],
)
def test_price_research_request_is_recognised(message):
    assert _looks_like_price_research_request(message) is True


@pytest.mark.parametrize(
    "message",
    ["fiyat yeni: 25000", "fiyat 800000", "genel fiyat listesi guncel", "sonra fiyat veririm"],
)
def test_plain_price_statement_is_not_a_research_request(message):
    """"ne" as a substring also lives inside "yeni", "genel" and "sonra"."""
    assert _looks_like_price_research_request(message) is False


# ── Publish confirmation vocabulary ───────────────────────────────────────────
# WhatsApp cannot render buttons (its Business API only supports pre-registered Content
# Templates), so the seller always types the answer to "Onaylıyor musunuz?". A reply the
# map does not recognise falls through to the LLM, drops back to DRAFTING, and the listing
# silently fails to publish.

def _fsm_command(message: str):
    normalized = re.sub(r"[^\wşğıöçü]+", " ", message.lower().strip(), flags=re.UNICODE).strip()
    return FSM_COMMANDS.get(normalized)


@pytest.mark.parametrize(
    "message",
    ["evet", "Tamam", "tamamdır", "olur", "olsun", "peki", "hadi", "onayla", "Yayınla", "EVET"],
)
def test_natural_yes_answers_confirm_the_publish(message):
    assert _fsm_command(message) == "CONFIRM"


@pytest.mark.parametrize(
    "message", ["hayır", "yok", "olmaz", "iptal", "vazgeçtim", "boş ver"]
)
def test_natural_no_answers_cancel_the_publish(message):
    assert _fsm_command(message) == "CANCEL"


@pytest.mark.parametrize(
    "message",
    [
        "evet ama fiyatı değiştir",
        "tamam da başlığı düzelt",
        "yayınla ama önce açıklamayı değiştir",
    ],
)
def test_qualified_answers_go_to_the_llm_instead_of_publishing(message):
    """Money moves here, so a "yes, but..." must never match as a plain yes."""
    assert _fsm_command(message) is None


# ── Keyword index ─────────────────────────────────────────────────────────────
# metadata.keywords_text is the recall booster search leans on. Three defects made it
# index the wrong things, and all three were only visible against real rows.

def _keywords(listing):
    return FSMEngine.generate_keywords(listing).split()


def test_location_is_indexed():
    """Search never queries the location column, so a city missing here is unfindable."""
    kw = _keywords({"title": "Bmw 2012 f30", "description": "375.000 km", "location": "İstanbul"})
    assert "istanbul" in kw


@pytest.mark.parametrize(
    "text,expected,mangled",
    [
        ("İstanbul", "istanbul", "stanbul"),
        ("İzmir", "izmir", "zmir"),
        ("İphone 17 promax", "iphone", "phone"),
    ],
)
def test_turkish_dotted_capital_is_not_split(text, expected, mangled):
    """str.lower() turns "İ" into "i" + U+0307; the tokenizer then drops the leading "i"."""
    kw = _keywords({"title": text, "description": "aciklama metni burada", "location": text})
    assert expected in kw
    assert mangled not in kw


def test_recall_helpers_key_off_category_and_title_only():
    """A flat whose description mentions a phone must not be indexed as a phone."""
    kw = _keywords({
        "title": "Mudanya Burgaz 2+1 deniz manzaralı daire",
        "description": "Detaylı bilgi için telefon ile ulaşabilirsiniz.",
        "category": "Emlak",
    })
    assert "emlak" in kw
    assert "cep telefonu" not in kw
    assert "akilli telefon" not in kw


def test_contact_numbers_are_not_indexed():
    """Publishing deliberately keeps the seller's number server-side; so does the index."""
    kw = _keywords({
        "title": "Bmw 2012 f30",
        "description": "İletişim: +90 545 836 8779 arayın",
        "location": "İstanbul",
    })
    assert not [t for t in kw if t.isdigit() and len(t) >= 7]
    assert "8779" not in kw


# ── Vision prefill vs. what the seller actually wrote ─────────────────────────
# A four-photo VW Jetta, sent with the text "2011 model Jetta 1.6 dizel manuel...",
# came back titled "Toyota Corolla": vision misread the first photo, and because the
# prefill is applied before the Brain reads the message, the guess became the title and
# every later turn treated it as fact.

def test_photo_guess_does_not_override_what_the_seller_wrote():
    listing = {}
    changed = _apply_structured_prefill_to_listing(
        listing,
        {"title": "Toyota Corolla"},
        user_text="2011 model Jetta 1.6 dizel manuel, 324 bin km, Antep teslim",
    )
    assert changed is False
    assert "title" not in listing


def test_photo_guess_still_fills_an_empty_draft():
    """Photo-only messages have nothing else to go on, so the guess is welcome there."""
    listing = {}
    changed = _apply_structured_prefill_to_listing(
        listing, {"title": "Volkswagen Jetta"}, user_text="Fotoğraf gönderdim"
    )
    assert changed is True
    assert listing["title"] == "Volkswagen Jetta"


def test_photo_guess_never_replaces_an_existing_title():
    listing = {"title": "2011 Volkswagen Jetta 1.6 TDI"}
    _apply_structured_prefill_to_listing(
        listing, {"title": "Toyota Corolla"}, user_text=""
    )
    assert listing["title"] == "2011 Volkswagen Jetta 1.6 TDI"
