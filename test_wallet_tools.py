"""Tests for wallet tool helpers (balance/history)"""
import pytest

from tools import get_wallet_balance_tool, get_wallet_transactions_tool
from services import supabase_client


@pytest.mark.asyncio
async def test_get_wallet_balance_tool(monkeypatch):
    async def fake_balance(user_id: str):
        assert user_id == "user-1"
        return 150

    monkeypatch.setattr(supabase_client, "get_wallet_balance", fake_balance)

    result = await get_wallet_balance_tool.execute(user_id="user-1")
    assert result["success"] is True
    assert result["data"]["balance"] == 150


@pytest.mark.asyncio
async def test_get_wallet_transactions_tool(monkeypatch):
    fake_rows = [
        {"amount_bigint": -50, "reference": "publish", "kind": "debit", "created_at": "2024-01-01"},
        {"amount_bigint": 200, "reference": "topup", "kind": "credit", "created_at": "2023-12-31"},
    ]

    async def fake_tx(user_id: str, limit: int = 20):
        assert user_id == "user-2"
        assert limit == 5
        return fake_rows

    monkeypatch.setattr(supabase_client, "get_wallet_transactions", fake_tx)

    result = await get_wallet_transactions_tool.execute(user_id="user-2", limit=5)
    assert result["success"] is True
    assert result["data"]["transactions"] == fake_rows


@pytest.mark.asyncio
async def test_get_wallet_transactions_tool_handles_error(monkeypatch):
    async def raise_err(*args, **kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(supabase_client, "get_wallet_transactions", raise_err)

    result = await get_wallet_transactions_tool.execute(user_id="user-3", limit=5)
    assert result["success"] is False
    assert "alınamadı" in result["error"].lower()
