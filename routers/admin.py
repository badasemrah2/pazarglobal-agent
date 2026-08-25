from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException

from config.settings import settings
from services.jwt_auth import verify_supabase_token
from services.redis_client import redis_client
from services.supabase_client import supabase_client
from services.logger import get_logger


logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _require_admin_or_support(authorization: Optional[str]) -> Dict[str, Any]:
    is_valid, user_id, err = await verify_supabase_token(authorization or "")
    if not is_valid or not user_id:
        raise HTTPException(status_code=401, detail=err or "unauthorized")

    try:
        res = (
            supabase_client.client
            .table("profiles")
            .select("id, role, is_active")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data[0] if res.data else None) if hasattr(res, "data") else None
    except Exception as e:
        logger.error(f"Admin auth profile lookup failed: {e}")
        raise HTTPException(status_code=500, detail="profile_lookup_failed")

    if not row or row.get("is_active") is not True:
        raise HTTPException(status_code=403, detail="inactive_user")

    role = (row.get("role") or "").strip().lower()
    if role not in {"admin", "assist"}:
        raise HTTPException(status_code=403, detail="forbidden")

    return {"user_id": user_id, "role": role}


async def _require_admin(authorization: Optional[str]) -> Dict[str, Any]:
    user = await _require_admin_or_support(authorization)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user


@router.get("/health")
async def admin_health(authorization: Optional[str] = Header(default=None, alias="Authorization")):
    """Basic service health checks for admin panel."""
    user = await _require_admin_or_support(authorization)

    started = time.perf_counter()
    payload: Dict[str, Any] = {
        "success": True,
        "data": {
            "viewer": user,
            "redis": {"enabled": not redis_client.disabled},
            "supabase": {},
        },
    }

    # Redis
    try:
        if redis_client.disabled:
            payload["data"]["redis"].update({"status": "disabled"})
        else:
            t0 = time.perf_counter()
            client = await redis_client.get_client()
            pong = await client.ping()
            latency_ms = (time.perf_counter() - t0) * 1000
            dbsize = await client.dbsize()
            payload["data"]["redis"].update({
                "status": "ok" if pong else "error",
                "ping_ms": round(latency_ms, 2),
                "dbsize": int(dbsize),
            })
    except Exception as e:
        payload["data"]["redis"].update({"status": "error", "error": str(e)})

    # Supabase (quick query)
    try:
        t0 = time.perf_counter()
        res = supabase_client.client.table("profiles").select("id").limit(1).execute()
        latency_ms = (time.perf_counter() - t0) * 1000
        payload["data"]["supabase"].update({
            "status": "ok" if getattr(res, "data", None) is not None else "unknown",
            "query_ms": round(latency_ms, 2),
        })
    except Exception as e:
        payload["data"]["supabase"].update({"status": "error", "error": str(e)})

    # Publishing capability.
    # The 90-day launch promo expired on 2026-05-15 and nobody noticed for three months;
    # every user was silently unable to publish. Surface it here so it cannot repeat.
    try:
        from datetime import datetime, timezone

        cost = int(getattr(settings, "listing_credit_cost", 55) or 55)
        res = (
            supabase_client.client
            .table("wallets")
            .select("user_id, balance_bigint, free_unlimited_until")
            .limit(5000)
            .execute()
        )
        wallets = getattr(res, "data", None) or []
        now = datetime.now(timezone.utc)

        promo_active = 0
        blocked = 0
        soonest_expiry = None

        for wallet in wallets:
            raw = wallet.get("free_unlimited_until")
            expiry = None
            if raw:
                try:
                    expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                except Exception:
                    expiry = None

            if expiry and expiry > now:
                promo_active += 1
                if soonest_expiry is None or expiry < soonest_expiry:
                    soonest_expiry = expiry
            elif float(wallet.get("balance_bigint") or 0) < cost:
                # No promo and not enough credit: this user cannot publish at all.
                blocked += 1

        days_left = int((soonest_expiry - now).days) if soonest_expiry else None

        if blocked:
            status = "error"
        elif days_left is not None and days_left <= 30:
            status = "warning"
        else:
            status = "ok"

        payload["data"]["publishing"] = {
            "status": status,
            "wallets": len(wallets),
            "promo_active": promo_active,
            "blocked_users": blocked,
            "promo_days_left": days_left,
            "listing_cost": cost,
        }
    except Exception as e:
        payload["data"]["publishing"] = {"status": "error", "error": str(e)}

    payload["data"]["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return payload


async def _delete_keys_by_pattern(pattern: str) -> Dict[str, Any]:
    if redis_client.disabled:
        # Best-effort clear in-memory fallback based on pattern prefix.
        # Keep it intentionally minimal.
        return {"deleted": 0, "note": "redis_disabled_in_memory_fallback"}

    client = await redis_client.get_client()
    deleted = 0
    pipe = client.pipeline()
    batch = 0
    async for key in client.scan_iter(match=pattern, count=500):
        pipe.delete(key)
        batch += 1
        if batch >= 500:
            results = await pipe.execute()
            deleted += sum(int(r) for r in results if isinstance(r, int))
            pipe = client.pipeline()
            batch = 0
    if batch:
        results = await pipe.execute()
        deleted += sum(int(r) for r in results if isinstance(r, int))

    return {"deleted": int(deleted), "pattern": pattern}


@router.post("/redis/clear")
async def admin_redis_clear(
    body: Dict[str, Any],
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
):
    """Controlled Redis clearing (no arbitrary commands). Admin-only."""
    await _require_admin(authorization)

    scope = str(body.get("scope") or "").strip().lower()
    scopes = {
        "sessions": "session:*",
        "wa_auth": "wa_auth:*",
        "cache": "cache:*",
        "rate": "rate:*",
    }
    if scope not in scopes:
        raise HTTPException(status_code=400, detail="invalid_scope")

    started = time.perf_counter()
    out = await _delete_keys_by_pattern(scopes[scope])
    out["scope"] = scope
    out["took_ms"] = round((time.perf_counter() - started) * 1000, 2)

    # best-effort audit
    try:
        supabase_client.client.table("audit_logs").insert({
            "user_id": body.get("admin_user_id"),
            "action": "admin_redis_clear",
            "resource_type": "redis",
            "response_status": "success",
            "request_data": {"scope": scope},
            "metadata": out,
        }).execute()
    except Exception:
        pass

    return {"success": True, "data": out}


@router.get("/audit/recent")
async def admin_audit_recent(
    limit: int = 50,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
):
    """Fetch recent audit_logs entries (admin/assist)."""
    await _require_admin_or_support(authorization)
    safe_limit = max(1, min(int(limit), 200))
    try:
        res = (
            supabase_client.client
            .table("audit_logs")
            .select("id, user_id, action, resource_type, response_status, error_message, created_at")
            .order("created_at", desc=True)
            .limit(safe_limit)
            .execute()
        )
        return {"success": True, "data": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
