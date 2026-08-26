"""
Regressions from the four-photo Jetta run on 26 Aug 2026.

The album debounce worked - one reply for four photos - but the same transcript showed
four separate defects, all reproduced here:

  1. "uygundur yayınla" and "evet onaylıyorum" were not recognised as confirmations, so
     the seller had to confirm three times and an extra LLM pass rewrote the description.
  2. The car published under "Diğer" even though its title and description classify
     cleanly as "Otomotiv".
  3. A photo from an hour-old abandoned draft rode along into the new listing.
  4. One photo 404'd at Twilio and was dropped without telling anyone.

(4) lives in the bridge and is covered by pazarglobal-whatsapp-bridge/test_album_debounce.py.
"""
import os
import re

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from routers.gateway_v3 import FSMEngine, _match_fsm_command, _parse_edit_updates  # noqa: E402


def _norm(message: str) -> str:
    """Same normalisation handle_message applies before matching a command."""
    return re.sub(r"[^\wşğıöçü]+", " ", (message or "").lower().strip(), flags=re.UNICODE).strip()


# ── 1. Confirmations ─────────────────────────────────────────────────────────

def test_natural_confirmations_publish():
    """The exact phrases in FSM_COMMANDS never covered how people actually type."""
    for message in (
        "uygundur yayınla",
        "evet onaylıyorum",
        "tamamdır yayınlayalım",
        "olur hadi",
        "evet lütfen yayınla",
        "kabul ediyorum",
        "aynen onayla",
    ):
        assert _match_fsm_command(_norm(message)) == "CONFIRM", message


def test_edit_requests_never_publish():
    """The whole point of whole-message matching: a request to change something is not a yes."""
    for message in (
        "evet ama fiyatı değiştir",
        "evet fiyat 500 olsun",
        "başlığı değiştir",
        "daha güzel yaz",
        "konum Antep",
        "evet iptal",
    ):
        assert _match_fsm_command(_norm(message)) is None, message


def test_negations_never_publish():
    """Turkish negates with a suffix, so prefix matching would turn a refusal into a yes."""
    for message in ("yayınlama", "onaylamıyorum", "onaylamayalım"):
        assert _match_fsm_command(_norm(message)) != "CONFIRM", message


def test_cancellations_still_cancel():
    for message in ("iptal", "vazgeçtim", "boş ver", "şimdilik olmasın"):
        assert _match_fsm_command(_norm(message)) == "CANCEL", message


# ── 2. Category ──────────────────────────────────────────────────────────────

JETTA = {
    "title": "2011 Volkswagen Jetta 1.6 Dizel | Primeline Paket, 324.000 km",
    "description": (
        "2011 model Volkswagen Jetta, 1.6 dizel motoru ile ekonomik bir seçenek sunuyor. "
        "Araçta 6 boya mevcut; ön 3 ve sol 3 kapıda boya bulunuyor. Antep'te teslim."
    ),
    "price": 425000.0,
    "location": "Antep",
    "condition": "2. El",
}


def test_diger_is_reclassified_not_kept():
    """'Diğer' is this code's own "nothing matched" fallback, not a decision anyone made."""
    listing = dict(JETTA, category="Diğer")
    FSMEngine.validate(listing)
    assert listing["category"] == "Otomotiv", listing["category"]


def test_missing_category_is_classified():
    listing = dict(JETTA)
    FSMEngine.validate(listing)
    assert listing["category"] == "Otomotiv", listing["category"]


def test_seller_chosen_category_is_respected():
    """Someone who typed the category themselves means it - including 'Diğer'."""
    listing = dict(JETTA, category="Diğer", category_locked=True)
    FSMEngine.validate(listing)
    assert listing["category"] == "Diğer", listing["category"]


def test_editing_the_category_locks_it():
    updates, errors = _parse_edit_updates("kategori: Diğer")
    assert not errors, errors
    assert updates.get("category")
    assert updates.get("category_locked") is True


def test_category_is_never_left_empty():
    """Even a locked nonsense value must not reach the insert."""
    listing = dict(JETTA, category="Kripto Madencilik", category_locked=True)
    FSMEngine.validate(listing)
    assert listing["category"] == "Diğer", listing["category"]


def test_unclassifiable_item_falls_back_to_diger():
    listing = {
        "title": "Zxqv",
        "description": "Zxqv qwerty",
        "price": 100.0,
        "location": "Antep",
        "condition": "2. El",
    }
    FSMEngine.validate(listing)
    assert listing["category"] == "Diğer", listing["category"]


# ── 3. Draft expiry ordering ─────────────────────────────────────────────────

def test_ttl_check_runs_before_prefill_and_media():
    """An abandoned draft must not be revived by the message starting a new listing.

    The prefill and media steps both stamp draft_updated_at with the current time. While
    the staleness check sat after them, an arriving photo refreshed the very timestamp it
    was about to be judged by, so the old draft's images and category came along.

    Asserted on source order because the ordering IS the fix.
    """
    import inspect
    from routers import gateway_v3

    source = inspect.getsource(gateway_v3.handle_message)
    ttl = source.index("Draft TTL check")
    prefill = source.index("Structured prefill injection")
    media = source.index("Attach WhatsApp media paths")

    assert ttl < prefill, "TTL check must run before the prefill refreshes the timestamp"
    assert ttl < media, "TTL check must run before media attach refreshes the timestamp"
