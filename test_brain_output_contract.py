"""Regression tests for what the Brain is allowed to put into a draft.

The model used to own more of the listing than it could actually know:

  - `condition` was written on every turn, falling back to "2. El" whenever the model
    omitted it. Since _handle_create merges every non-None field, a seller who had picked
    "Sıfır" had it silently reset the next time they typed anything.
  - `category` came from an 8-entry set inside brain.py while the real taxonomy in
    category_library has 17 entries, so valid categories were dropped on the floor.
  - `images` were accepted from the model, which has no way to know real storage URLs.

The Brain now emits only what the seller actually told it; the server owns the rest.
"""
import pytest

from core.brain import Guardrails, brain


def _sanitize(listing_data, message=""):
    return Guardrails.sanitize(
        {"intent": "CREATE", "response_text": "tamam", "listing_data": listing_data},
        message,
    )


def test_condition_is_not_invented_when_the_model_omits_it():
    out = _sanitize({"title": "Test Urun", "price": 100})
    assert "condition" not in out.listing_data


def test_condition_is_kept_when_the_model_returns_a_valid_one():
    out = _sanitize({"title": "Test Urun", "condition": "Sıfır"})
    assert out.listing_data["condition"] == "Sıfır"


def test_invalid_condition_is_dropped_rather_than_defaulted():
    """Dropping it lets the previously stored value survive the merge in _handle_create."""
    out = _sanitize({"title": "Test Urun", "condition": "yeni gibi"})
    assert "condition" not in out.listing_data


def test_category_is_never_taken_from_the_model():
    out = _sanitize({"title": "Test Urun", "category": "Elektronik"})
    assert "category" not in out.listing_data


def test_images_are_never_taken_from_the_model():
    out = _sanitize({"title": "Test Urun", "images": ["URL", "http://fake/x.jpg"]})
    assert "images" not in out.listing_data


def test_price_outside_the_allowed_range_is_dropped():
    assert "price" not in _sanitize({"title": "X", "price": 0}).listing_data
    assert "price" not in _sanitize({"title": "X", "price": 10**12}).listing_data
    assert _sanitize({"title": "X", "price": "45000"}).listing_data["price"] == 45000.0


@pytest.mark.parametrize(
    "title,expected_group",
    [
        ("BMW F30 316i 2012", "otomotiv"),
        ("iPhone 14 Pro 256GB", "elektronik"),
        ("3+1 Daire Kadıköy", "emlak"),
    ],
)
def test_context_injects_the_matching_category_profile(title, expected_group):
    context = brain._build_context(
        current_listing={"title": title},
        fsm_state="DRAFTING",
        missing_fields=["price"],
        last_intent="CREATE",
    )
    assert f"YAZIM PROFİLİ ({expected_group})" in context


def test_context_no_longer_tells_the_model_to_print_a_preview():
    """The single biggest source of the robotic tone: a preview table on every turn."""
    context = brain._build_context(
        current_listing={"title": "Bir urun", "price": 100},
        fsm_state="DRAFTING",
        missing_fields=[],
        last_intent="CREATE",
    )
    assert "preview göster" not in context
    assert "önizleme basma" in context


def test_context_is_machine_readable_state_not_prose():
    """The draft lives on the server; each turn re-anchors the model to it."""
    context = brain._build_context(
        current_listing={"title": "Bir urun", "price": 100, "location": "İzmir"},
        fsm_state="READY",
        missing_fields=[],
        last_intent="CREATE",
    )
    assert '"phase": "READY"' in context
    assert '"location": "İzmir"' in context
