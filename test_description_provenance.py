"""Regression tests for the description guard's provenance archive.

The guard strips verifiable claims (year, kutu, sertifika, usage counts, condition
wording) from generated copy unless the user actually made them. Its record of what the
user said used to be rebuilt from a single turn, and `load_session` dropped the stored
copy, so a detail the seller gave in turn 1 counted as invented by turn 2 and was deleted
out of their own listing.

These tests pin the fixed behaviour: the archive survives turns, still blocks claims the
user never made, and does not leak across drafts.
"""
import pytest

from routers.gateway_v3 import (
    _SESSION_USER_STATEMENTS_KEY,
    _apply_ai_description_guard,
    _collect_confirmed_description_claims,
    _remember_user_statement,
    load_session,
    save_session,
)

USER = "provenance-test-user"
CHANNEL = "webchat"


async def _fresh_session():
    from services.redis_client import _IN_MEMORY_SESSIONS

    _IN_MEMORY_SESSIONS.clear()
    return await load_session(USER, CHANNEL)


@pytest.mark.asyncio
async def test_user_statement_survives_a_session_roundtrip():
    """The archive must come back out of Redis; this is the bug that broke everything."""
    session = await _fresh_session()
    _remember_user_statement(session, "2012 model, kutulu satıyorum")
    await save_session(USER, CHANNEL, session)

    reloaded = await load_session(USER, CHANNEL)

    assert reloaded[_SESSION_USER_STATEMENTS_KEY] == ["2012 model, kutulu satıyorum"]
    claims = _collect_confirmed_description_claims(reloaded)
    assert "kutu" in claims
    assert "year:2012" in claims


@pytest.mark.asyncio
async def test_detail_given_in_turn_one_is_not_stripped_in_turn_three():
    """The seller's own words must not be deleted from their listing three turns later."""
    session = await _fresh_session()

    # Turn 1: the seller states the facts.
    _remember_user_statement(session, "2012 model bmw f30, kutulu ve sorunsuz")
    await save_session(USER, CHANNEL, session)

    # Turn 2: something unrelated.
    session = await load_session(USER, CHANNEL)
    _remember_user_statement(session, "fiyatı 800000")
    await save_session(USER, CHANNEL, session)

    # Turn 3: the assistant rewrites the copy, reusing those facts.
    session = await load_session(USER, CHANNEL)
    listing = {
        "title": "BMW F30 316i",
        "description": "2012 model BMW F30, kutulu olarak teslim edilir. Sorunsuz durumda.",
    }
    claims = _collect_confirmed_description_claims(session, listing, "")
    _apply_ai_description_guard(listing, previous_listing=None, confirmed_claims=claims)

    assert "2012" in listing["description"], listing["description"]
    assert "kutulu" in listing["description"].lower(), listing["description"]


@pytest.mark.asyncio
async def test_claim_the_user_never_made_is_still_stripped():
    """The archive must not turn the guard off - invented claims still have to go."""
    session = await _fresh_session()
    _remember_user_statement(session, "bmw satıyorum, fiyat 800000")
    await save_session(USER, CHANNEL, session)

    session = await load_session(USER, CHANNEL)
    listing = {
        "title": "BMW F30",
        "description": "Aracın sertifikası mevcuttur ve 2019 model olarak kayıtlıdır.",
    }
    claims = _collect_confirmed_description_claims(session, listing, "")
    _apply_ai_description_guard(listing, previous_listing=None, confirmed_claims=claims)

    assert "sertifika" not in listing["description"].lower(), listing["description"]
    assert "2019" not in listing["description"], listing["description"]


@pytest.mark.asyncio
async def test_a_new_draft_does_not_inherit_the_previous_product_claims():
    """Claims are scoped to one draft; a phone's 'kutulu' must not authorise a car's."""
    session = await _fresh_session()
    _remember_user_statement(session, "iphone satıyorum, kutulu")
    await save_session(USER, CHANNEL, session)

    # Draft abandoned: same reset the TTL path performs.
    session = await load_session(USER, CHANNEL)
    session[_SESSION_USER_STATEMENTS_KEY] = []
    _remember_user_statement(session, "şimdi arabamı satmak istiyorum")
    await save_session(USER, CHANNEL, session)

    session = await load_session(USER, CHANNEL)
    claims = _collect_confirmed_description_claims(session)
    assert "kutu" not in claims


@pytest.mark.asyncio
async def test_repeated_identical_message_is_not_archived_twice():
    """Twilio retries deliver the same body twice; the archive should not grow for it."""
    session = await _fresh_session()
    _remember_user_statement(session, "kutulu")
    _remember_user_statement(session, "kutulu")

    assert session[_SESSION_USER_STATEMENTS_KEY] == ["kutulu"]


@pytest.mark.asyncio
async def test_archive_is_capped():
    """A long conversation must not grow the session payload without bound."""
    from routers.gateway_v3 import _MAX_USER_STATEMENTS

    session = await _fresh_session()
    for i in range(_MAX_USER_STATEMENTS + 15):
        _remember_user_statement(session, f"mesaj {i}")

    stored = session[_SESSION_USER_STATEMENTS_KEY]
    assert len(stored) == _MAX_USER_STATEMENTS
    assert stored[-1] == f"mesaj {_MAX_USER_STATEMENTS + 14}"
