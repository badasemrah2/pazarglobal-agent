"""Regression tests for the publish credit path.

Covers migrations/005_atomic_listing_credit.sql wiring in FSMEngine.publish:
  - the atomic reservation path,
  - the fallback used while the migration is not applied,
  - and the refund rules, including the promo case that used to mint credits.
"""
import uuid

import pytest

from routers.gateway_v3 import FSMEngine
from services.supabase_client import supabase_client


VALID_LISTING = {
    "title": "Test Ilani Baslik",
    "description": "Bu bir test ilani aciklamasidir, yeterince uzun.",
    "price": 1000,
    "location": "Istanbul",
    "condition": "2. El",
}


class _FakeExecute:
    def __init__(self, data):
        self.data = data


class _FakeInsert:
    """Mirrors the real client's .insert(...).execute() chain."""

    def __init__(self, error, data):
        self._error = error
        self._data = data

    def execute(self):
        if self._error:
            raise self._error
        return _FakeExecute(self._data)


class _FakeTable:
    """Minimal Supabase table stub; records inserts and can be told to fail."""

    def __init__(self, recorder, insert_error=None, insert_data=None):
        self._recorder = recorder
        self._insert_error = insert_error
        self._insert_data = insert_data

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, payload):
        self._recorder.append(payload)
        return _FakeInsert(self._insert_error, self._insert_data)

    def execute(self):
        return _FakeExecute([])


@pytest.fixture
def publish_env(monkeypatch):
    """Stub out everything publish() touches except the credit logic under test."""
    state = {"inserts": [], "reserve_calls": [], "refund_calls": [], "deduct_calls": []}

    async def fake_ensure_profile(_user_id):
        return True

    monkeypatch.setattr(FSMEngine, "ensure_profile_exists", classmethod(lambda cls, u: fake_ensure_profile(u)))

    async def fake_name(_u):
        return "Test User"

    async def fake_phone(_u):
        return "+905550000000"

    monkeypatch.setattr(supabase_client, "get_user_display_name", fake_name)
    monkeypatch.setattr(supabase_client, "get_user_phone", fake_phone)

    async def fake_refund(user_id, reference):
        state["refund_calls"].append((user_id, reference))
        return {"success": True, "refunded": True}

    monkeypatch.setattr(supabase_client, "refund_listing_credit", fake_refund)

    async def fake_deduct(cls, user_id, amount=55.0):
        state["deduct_calls"].append((user_id, amount))
        return True

    monkeypatch.setattr(FSMEngine, "deduct_credit", classmethod(fake_deduct))

    from services.redis_client import redis_client

    async def fake_clear():
        return 0

    monkeypatch.setattr(redis_client, "clear_search_cache", fake_clear)

    def install_client(insert_error=None, insert_data=None):
        table = _FakeTable(state["inserts"], insert_error, insert_data)

        class _Client:
            def table(self, _name):
                return table

        monkeypatch.setattr(type(supabase_client), "client", property(lambda self: _Client()))

    state["install_client"] = install_client
    state["monkeypatch"] = monkeypatch
    return state


def _install_reserve(state, result):
    async def fake_reserve(user_id, cost, reference):
        state["reserve_calls"].append((user_id, cost, reference))
        return result

    state["monkeypatch"].setattr(supabase_client, "reserve_listing_credit", fake_reserve)


@pytest.mark.asyncio
async def test_publish_uses_atomic_reservation_keyed_by_listing_id(publish_env):
    _install_reserve(publish_env, {"success": True, "charged": True, "balance": 945})
    publish_env["install_client"](insert_data=[{"id": "x"}])

    ok, message, listing_id = await FSMEngine.publish(str(uuid.uuid4()), dict(VALID_LISTING))

    assert ok, message
    assert len(publish_env["reserve_calls"]) == 1
    # The reservation reference must be the listing id, so a retry is idempotent.
    assert publish_env["reserve_calls"][0][2] == listing_id
    assert publish_env["inserts"][0]["id"] == listing_id
    # The legacy non-atomic path must not run when the RPC is available.
    assert publish_env["deduct_calls"] == []


@pytest.mark.asyncio
async def test_publish_reports_insufficient_balance_from_rpc(publish_env):
    _install_reserve(publish_env, {"success": False, "error": "insufficient_balance", "balance": 10})
    publish_env["install_client"](insert_data=[{"id": "x"}])

    ok, message, listing_id = await FSMEngine.publish(str(uuid.uuid4()), dict(VALID_LISTING))

    assert not ok
    assert "yetersiz" in message.lower()
    assert listing_id is None
    assert publish_env["inserts"] == []


@pytest.mark.asyncio
async def test_failed_insert_refunds_via_rpc_not_negative_deduct(publish_env):
    _install_reserve(publish_env, {"success": True, "charged": True, "balance": 945})
    publish_env["install_client"](insert_error=RuntimeError("db down"))

    user_id = str(uuid.uuid4())
    ok, _message, _lid = await FSMEngine.publish(user_id, dict(VALID_LISTING))

    assert not ok
    assert len(publish_env["refund_calls"]) == 1
    assert publish_env["refund_calls"][0][0] == user_id
    # The old code refunded with deduct_credit(user, -55); that path must be gone.
    assert publish_env["deduct_calls"] == []


@pytest.mark.asyncio
async def test_falls_back_to_legacy_path_when_migration_not_applied(publish_env, monkeypatch):
    _install_reserve(publish_env, {"success": False, "error": "rpc_unavailable"})
    publish_env["install_client"](insert_data=[{"id": "x"}])

    async def fake_check_wallet(cls, user_id, required_amount=55.0):
        return True, 500.0

    monkeypatch.setattr(FSMEngine, "check_wallet", classmethod(fake_check_wallet))

    ok, message, _lid = await FSMEngine.publish(str(uuid.uuid4()), dict(VALID_LISTING))

    assert ok, message
    # Legacy path charges through deduct_credit.
    assert len(publish_env["deduct_calls"]) == 1
    assert publish_env["deduct_calls"][0][1] > 0


@pytest.mark.asyncio
async def test_promo_user_is_not_refunded_on_failed_insert(publish_env, monkeypatch):
    """The old bug: a promo user was never charged but still got +55 back on failure."""
    _install_reserve(publish_env, {"success": False, "error": "rpc_unavailable"})
    publish_env["install_client"](insert_error=RuntimeError("db down"))

    async def fake_check_wallet(cls, user_id, required_amount=55.0):
        # check_wallet returns this sentinel balance while the promo window is active.
        return True, float(10**12)

    monkeypatch.setattr(FSMEngine, "check_wallet", classmethod(fake_check_wallet))

    ok, _message, _lid = await FSMEngine.publish(str(uuid.uuid4()), dict(VALID_LISTING))

    assert not ok
    refunds = [amount for _u, amount in publish_env["deduct_calls"] if amount < 0]
    assert refunds == [], f"promo kullanicisina kredi iade edildi: {refunds}"
    assert publish_env["refund_calls"] == []
