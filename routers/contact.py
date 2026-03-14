from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from services.jwt_auth import verify_supabase_token
from services.supabase_client import supabase_client

router = APIRouter(prefix="/api/v3/contact", tags=["contact"])


class ContactSendRequest(BaseModel):
    token: str = Field(..., min_length=10)
    message: str = Field(..., min_length=1, max_length=3000)
    sender_name: Optional[str] = Field(default=None, max_length=120)
    sender_session_id: Optional[str] = Field(default=None, max_length=120)
    sender_user_id: Optional[str] = Field(default=None, max_length=120)
    channel: str = Field(default="web")


class OwnerReplyRequest(BaseModel):
    conversation_id: str = Field(..., min_length=10)
    message: str = Field(..., min_length=1, max_length=3000)


async def _require_auth_user_id(authorization: Optional[str]) -> str:
    valid, user_id, err = await verify_supabase_token(authorization or "")
    if not valid or not user_id:
        raise HTTPException(status_code=401, detail=err or "unauthorized")
    return user_id


@router.get("/public-link/{listing_id}")
async def get_public_contact_link(listing_id: str):
    """Get or create listing-specific contact token link for public listing pages."""
    row = await supabase_client.ensure_contact_token_for_listing(listing_id)
    if not row:
        raise HTTPException(status_code=404, detail="listing_not_contactable")

    token = str(row.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="token_generation_failed")

    return {
        "success": True,
        "data": {
            "listing_id": listing_id,
            "token": token,
            "contact_path": f"/contact/{token}",
        },
    }


@router.get("/resolve/{token}")
async def resolve_contact_link(token: str):
    """Resolve token for contact page context (listing title, visibility, owner labels)."""
    resolved = await supabase_client.resolve_contact_token(token)
    if not resolved:
        raise HTTPException(status_code=404, detail="invalid_or_expired_token")

    listing = resolved.get("listing") or {}

    phone_visibility = str(listing.get("phone_visibility") or "public").strip().lower()
    name_visibility = str(listing.get("name_visibility") or "public").strip().lower()

    owner_name = str(listing.get("user_name") or "Satıcı")
    owner_phone = str(listing.get("user_phone") or "")

    if name_visibility == "hidden":
        owner_name = "İlan Sahibi"

    if phone_visibility == "hidden":
        owner_phone = ""

    return {
        "success": True,
        "data": {
            "listing": {
                "id": listing.get("id"),
                "title": listing.get("title"),
                "status": listing.get("status"),
                "expires_at": listing.get("expires_at"),
                "owner_name": owner_name,
                "owner_phone": owner_phone,
                "phone_visibility": phone_visibility,
                "name_visibility": name_visibility,
            }
        },
    }


@router.post("/send")
async def send_message_via_contact_link(payload: ContactSendRequest):
    """Public endpoint: send site-internal message to listing owner via token."""
    resolved = await supabase_client.resolve_contact_token(payload.token)
    if not resolved:
        raise HTTPException(status_code=404, detail="invalid_or_expired_token")

    listing = resolved.get("listing") or {}
    token_row = resolved.get("token") or {}

    listing_id = str(listing.get("id") or "")
    owner_user_id = str(listing.get("user_id") or "")
    if not listing_id or not owner_user_id:
        raise HTTPException(status_code=500, detail="contact_resolution_failed")

    sender_name = (payload.sender_name or "").strip() or "İsimsiz Alıcı"

    conv = await supabase_client.find_or_create_conversation(
        listing_id=listing_id,
        owner_user_id=owner_user_id,
        contact_token_id=str(token_row.get("id") or "") or None,
        sender_user_id=(payload.sender_user_id or "").strip() or None,
        sender_session_id=(payload.sender_session_id or "").strip() or None,
        sender_name=sender_name,
        source_channel=(payload.channel or "web").strip().lower(),
        first_message_preview=payload.message,
    )
    if not conv:
        raise HTTPException(status_code=500, detail="conversation_create_failed")

    msg = await supabase_client.add_message_to_conversation(
        conversation_id=str(conv.get("id") or ""),
        listing_id=listing_id,
        sender_role="buyer",
        body=payload.message,
    )
    if not msg:
        raise HTTPException(status_code=500, detail="message_send_failed")

    return {
        "success": True,
        "data": {
            "conversation_id": conv.get("id"),
            "message_id": msg.get("id"),
            "listing_id": listing_id,
        },
    }


@router.get("/inbox")
async def get_owner_inbox(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    limit: int = 50,
):
    """Owner inbox list (auth required)."""
    owner_user_id = await _require_auth_user_id(authorization)
    rows = await supabase_client.get_owner_inbox(owner_user_id, limit=limit)

    return {
        "success": True,
        "data": rows,
    }


@router.get("/inbox/{conversation_id}")
async def get_owner_conversation_messages(
    conversation_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    limit: int = 100,
):
    """Owner reads conversation messages (auth + ownership check)."""
    owner_user_id = await _require_auth_user_id(authorization)

    can_access = await supabase_client.owner_can_access_conversation(owner_user_id, conversation_id)
    if not can_access:
        raise HTTPException(status_code=403, detail="forbidden")

    rows = await supabase_client.get_conversation_messages(conversation_id, limit=limit)
    await supabase_client.mark_conversation_read_for_user(owner_user_id, conversation_id)

    return {
        "success": True,
        "data": rows,
    }


@router.post("/inbox/reply")
async def owner_reply(
    payload: OwnerReplyRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
):
    """Owner sends reply to a conversation (auth + ownership check)."""
    owner_user_id = await _require_auth_user_id(authorization)

    can_access = await supabase_client.owner_can_access_conversation(owner_user_id, payload.conversation_id)
    if not can_access:
        raise HTTPException(status_code=403, detail="forbidden")

    participant_role = await supabase_client.get_conversation_participant_role(owner_user_id, payload.conversation_id)
    if participant_role not in {"owner", "buyer"}:
        raise HTTPException(status_code=403, detail="forbidden")

    conv = (
        supabase_client.client
        .table("listing_conversations")
        .select("listing_id")
        .eq("id", payload.conversation_id)
        .limit(1)
        .execute()
    )
    conv_row = conv.data[0] if conv.data else None
    listing_id = str(conv_row.get("listing_id") or "") if isinstance(conv_row, dict) else ""

    if not listing_id:
        raise HTTPException(status_code=404, detail="conversation_not_found")

    msg = await supabase_client.add_message_to_conversation(
        conversation_id=payload.conversation_id,
        listing_id=listing_id,
        sender_role="owner" if participant_role == "owner" else "buyer",
        body=payload.message,
    )
    if not msg:
        raise HTTPException(status_code=500, detail="reply_send_failed")

    return {
        "success": True,
        "data": {
            "conversation_id": payload.conversation_id,
            "message_id": msg.get("id"),
        },
    }
