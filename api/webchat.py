"""
WebChat API endpoints for frontend integration
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from agents import (
    ComposerAgent,
    DescriptionAgent,
    IntentRouterAgent,
    PublishDeleteAgent,
    SearchComposerAgent,
    SmallTalkAgent,
    TitleAgent,
)
from agents.vision_safety_gate import vision_safety_gate
from config import settings
from services import openai_client, redis_client, supabase_client
from services.logger import (
    bind_session_logger,
    ensure_session_trace,
    get_logger,
    log_fsm_event,
)
from services.text_normalization import (
    canonicalize_condition,
    looks_like_image_action_command,
    normalize_for_match,
    violates_listing_content_guard,
)
from tools import delete_listing_tool, get_wallet_balance_tool, publish_listing_tool


# In-memory cache for last search results (when Redis is disabled)
LAST_SEARCH_CACHE: Dict[str, List[Any]] = {}

# Local session cache fallback when Redis is disabled
IN_MEMORY_SESSION_CACHE: Dict[str, Dict[str, Any]] = {}

MEDIA_ANALYSIS_SYSTEM_PROMPT = (
    "You are a marketplace vision assistant that returns concise Turkish JSON. "
    "Always respond with a single JSON object containing these keys: product (string), "
    "category (string), condition (string), features (array of up to 5 short strings), "
    "description (string), safety_flags (array of short warning strings, empty array when no issues). "
    "IMPORTANT: The 'condition' field is a VISUAL IMPRESSION from the photo (e.g. 'temiz', 'yıpranmış', 'çok iyi görünüyor'). "
    "Do NOT infer or state marketplace condition like 'Sıfır'/'2. El'. If unsure, use empty string. "
    "If you are unsure, set the field to an empty string or empty array."
)

MEDIA_ANALYSIS_USER_PROMPT = (
    "Lütfen görseldeki ürünü analiz et ve yukarıdaki JSON şemasını doldur. "
    "Ürünün türünü, olası kullanım alanını, durumunu ve dikkat çeken özelliklerini belirt. "
    "Durum alanını 'görsel izlenim' olarak yaz (örn: 'temiz', 'yıpranmış', 'çok iyi görünüyor'). "
    "'Sıfır/2. El' gibi kesin çıkarım yapma."
)

FSM_PARK_TIMEOUT_SECONDS = 10 * 60  # 10 minutes of user silence → parked
FSM_COMPOSER_TIMEOUT_SECONDS = 45   # ComposerAgent hard timeout
RESUME_KEYWORDS = {"devam", "kaldığımız yerden", "kaldigimiz yerden", "resume", "continue"}


logger = get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_since(ts: Optional[str], now_iso: Optional[str] = None) -> Optional[float]:
    if not ts:
        return None
    try:
        base = datetime.fromisoformat(ts)
        now_dt = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
        return max(0.0, (now_dt - base).total_seconds())
    except Exception:
        try:
            return max(0.0, time.time() - float(ts))
        except Exception:
            return None


def _set_fsm_state(session: Dict[str, Any], state: str, intent: Optional[str] = None, reason: Optional[str] = None) -> None:
    prev_state = session.get("fsm_state")
    session["fsm_state"] = state
    session["fsm_state_reason"] = reason
    session["fsm_state_updated_at"] = _utc_now_iso()
    if intent:
        session["fsm_state_intent"] = intent
    logger.info(f"FSM state transition: {prev_state} → {state} (reason={reason}, intent={intent})")


async def _record_fsm_event(event: str, session_id: str, session: Dict[str, Any], detail: Dict[str, Any] | None = None) -> None:
    detail = detail or {}
    log_fsm_event(event, session_id, session, **detail)

    try:
        if not hasattr(supabase_client, "log_action"):
            bind_session_logger(session_id, session, event=event).debug(
                "Supabase client lacks log_action, skipping telemetry persist",
            )
            return
        meta = {
            "event": event,
            "session_id": session_id,
            "intent": session.get("intent"),
            "locked_intent": session.get("locked_intent"),
            "fsm_state": session.get("fsm_state"),
            "fsm_state_reason": session.get("fsm_state_reason"),
        }
        meta.update(detail)

        await supabase_client.log_action(
            action="fsm_event",
            metadata=meta,
            resource_type="session",
            resource_id=session_id,
            user_id=session.get("user_id"),
        )
        bind_session_logger(session_id, session, event=event).debug("FSM telemetry persisted to audit_logs")
    except Exception as e:
        # Telemetry must never break the flow
        bind_session_logger(session_id, session, event=event).warning(
            f"FSM telemetry emit failed: {e}"
        )


def is_resume_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    return any(token in msg for token in RESUME_KEYWORDS)


def redis_is_disabled() -> bool:
    """Centralize redis enabled/disabled checks."""

    return bool(getattr(redis_client, "disabled", False))


async def load_session_state(session_id: str) -> Optional[Dict[str, Any]]:
    """Load session either from Redis or in-memory fallback."""

    if redis_is_disabled():
        return IN_MEMORY_SESSION_CACHE.get(session_id)
    return await redis_client.get_session(session_id)


async def persist_session_state(session_id: str, session: Dict[str, Any]) -> None:
    """Persist session state regardless of backend availability."""

    if redis_is_disabled():
        IN_MEMORY_SESSION_CACHE[session_id] = session
        return
    await redis_client.set_session(session_id, session)


def remove_session_state(session_id: str) -> None:
    """Remove session from fallback cache when Redis is disabled."""
    if redis_is_disabled():
        IN_MEMORY_SESSION_CACHE.pop(session_id, None)


SEARCH_CONTEXT_RESULT_LIMIT = 6
_SEARCH_CONTEXT_KEEP_FIELDS = {
    "id",
    "listing_id",
    "title",
    "description",
    "price",
    "category",
    "location",
    "user_location",
    "user_id",
    "user_name",
    "user_phone",
    "contact_phone",
    "metadata",
    "image_url",
    "images",
}
_DETAIL_KEYWORDS = {"goster", "göster", "detay", "incele", "bak", "gösterebilir", "gosterir", "detayini"}
_THIS_LISTING_KEYWORDS = {
    "bu ilan",
    "bu ilani",
    "bu ilanin",
    "bu urun",
    "bu ürün",
    "az onceki ilan",
    "az önceki ilan",
}
_FOLLOWUP_OWNER_KEYWORDS = {"kime ait", "sahibi", "kimin", "kim satiyor", "kim satıyor"}
_FOLLOWUP_CONTACT_KEYWORDS = {"telefon", "numara", "iletisim", "iletişim", "ulaş", "ulas"}


def _trim_listing_for_context(listing: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(listing, dict):
        return {}
    trimmed: Dict[str, Any] = {}
    for key in _SEARCH_CONTEXT_KEEP_FIELDS:
        if key in listing:
            trimmed[key] = listing[key]
    return trimmed


def _get_search_context_results(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    ctx = session.get("search_context")
    results = (ctx or {}).get("results") if isinstance(ctx, dict) else None
    if isinstance(results, list):
        return results
    return []


def _store_search_context(session: Dict[str, Any], query: str, listings: Optional[List[Dict[str, Any]]]) -> None:
    listings = listings or []
    trimmed = [_trim_listing_for_context(item) for item in listings[:SEARCH_CONTEXT_RESULT_LIMIT]]
    session["search_context"] = {
        "search_id": str(uuid.uuid4()),
        "query": (query or "").strip(),
        "results": trimmed,
        "stored_at": _utc_now_iso(),
    }
    session["context_mode"] = "search"


def _store_active_listing(session: Dict[str, Any], listing: Dict[str, Any], source: str = "search") -> None:
    trimmed = _trim_listing_for_context(listing)
    if not trimmed:
        return
    session["active_listing_context"] = {
        "listing": trimmed,
        "listing_id": str(trimmed.get("id") or trimmed.get("listing_id") or ""),
        "source": source,
        "stored_at": _utc_now_iso(),
    }
    session["context_mode"] = "view_listing"


def _get_active_listing(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ctx = session.get("active_listing_context")
    if isinstance(ctx, dict) and isinstance(ctx.get("listing"), dict):
        return ctx.get("listing")
    return None


def _clear_active_listing_if_matches(session: Dict[str, Any], listing_id: Optional[str]) -> None:
    if not listing_id:
        return
    ctx = session.get("active_listing_context")
    if not isinstance(ctx, dict):
        return
    ctx_id = str(ctx.get("listing_id") or "").strip()
    if ctx_id and ctx_id == str(listing_id).strip():
        session.pop("active_listing_context", None)


def _extract_listing_index(message: str) -> Optional[int]:
    msg = normalize_for_match(message)
    if not msg:
        return None
    match = re.search(r"(\d{1,3})\s*(?:nolu|no\'lu|no|numarali|numaral[ıi]|\.?)\s*(?:ilan|liste|sirasi|sira)?", msg)
    if match:
        idx = int(match.group(1)) - 1
        if idx >= 0:
            return idx
    return None


def _references_current_listing(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    return any(token in msg for token in _THIS_LISTING_KEYWORDS)


def _looks_like_listing_detail_request(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    if any(token in msg for token in _DETAIL_KEYWORDS):
        return True
    return _extract_listing_index(message) is not None


def _looks_like_search_query(message: str) -> bool:
    """Check if message is a search query (var mı, ara, bul)."""
    msg = normalize_for_match(message)
    if not msg:
        return False
    search_keywords = {
        "var mi", "var mı", "varmi", "varmı",
        "mevcut mu", "bulunur mu", "var misin",
        "ara", "arama", "bul", "ariyorum", "arıyorum",
        "ihtiyacim var", "ihtiyacım var", "lazim", "lazım"
    }
    return any(kw in msg for kw in search_keywords)


def _classify_listing_followup_question(message: str) -> Optional[str]:
    msg = normalize_for_match(message)
    if not msg:
        return None
    if any(token in msg for token in _FOLLOWUP_OWNER_KEYWORDS):
        return "owner"
    if any(token in msg for token in _FOLLOWUP_CONTACT_KEYWORDS):
        return "contact"
    return None


def _is_interrupt_signal(message: str) -> bool:
    """Detect interrupt signals: 'bişey sorabilir miyim', 'dur bi', 'merak ettim'."""
    msg = normalize_for_match(message)
    if not msg:
        return False
    interrupt_patterns = [
        r"\bbis[eş]ey\s+sorabilir\s*mi",
        r"\bsor(abilir|abilmek)\s*mi",
        r"\bdur\s*(bi|bir)\b",
        r"\bbekle\b",
        r"\bmerak\s+ettim\b",
        r"\bbir\s+dakika\b",
        r"\b[şs]unu\s+merak\b",
    ]
    return any(re.search(pat, msg) for pat in interrupt_patterns)


def _is_meta_question(message: str) -> bool:
    """Detect meta questions about listing: 'kime ait', 'ne zaman', 'nerede'."""
    msg = normalize_for_match(message)
    if not msg:
        return False
    meta_patterns = [
        r"\bkime\s+ait\b",
        r"\bkimin\b",
        r"\bsahibi\b",
        r"\bkim\s+sat[ıi]yor\b",
        r"\bne\s+zaman\b",
        r"\bnerede\b",
        r"\bhangi\s+(s[eş]hir|b[oö]lge)\b",
    ]
    return any(re.search(pat, msg) for pat in meta_patterns)


def _search_context_is_stale(session: Dict[str, Any]) -> bool:
    """Check if search context is stale (no recent search results)."""
    ctx = session.get("search_context")
    if not isinstance(ctx, dict):
        return True
    results = ctx.get("results")
    if not isinstance(results, list) or len(results) == 0:
        return True
    # Check if stored_at is older than 5 minutes
    stored_at = ctx.get("stored_at")
    if not stored_at:
        return True
    try:
        from datetime import datetime, timezone, timedelta
        stored_time = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if now - stored_time > timedelta(minutes=5):
            return True
    except Exception:
        return True
    return False


def _listing_contact_text(listing: Dict[str, Any]) -> tuple[str, str]:
    owner = str(listing.get("user_name") or "Satıcı bilgisi yok").strip()
    phone = str(listing.get("user_phone") or listing.get("contact_phone") or "Telefon paylaşılmamış").strip()
    return owner, phone


def _listing_belongs_to_user(listing: Dict[str, Any], user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    owner = str(listing.get("user_id") or "").strip()
    return bool(owner) and owner == str(user_id).strip()


def _resolve_listing_reference(
    session: Dict[str, Any],
    message: str,
    session_id: str,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    listings_ctx = _get_search_context_results(session)
    idx = _extract_listing_index(message)
    if idx is not None:
        if 0 <= idx < len(listings_ctx):
            return listings_ctx[idx], "search_context"
        cached = LAST_SEARCH_CACHE.get(session_id) or []
        if 0 <= idx < len(cached):
            return _trim_listing_for_context(cached[idx]), "legacy_cache"

    if _references_current_listing(message):
        active = _get_active_listing(session)
        if active:
            return active, "active_listing"

    msg_norm = normalize_for_match(message)
    if msg_norm and listings_ctx:
        for entry in listings_ctx:
            title_norm = normalize_for_match(entry.get("title") or "")
            if title_norm and title_norm in msg_norm:
                return entry, "title_match"

    cached = LAST_SEARCH_CACHE.get(session_id) or []
    if msg_norm and cached:
        for entry in cached:
            title_norm = normalize_for_match(entry.get("title") or "")
            if title_norm and title_norm in msg_norm:
                return _trim_listing_for_context(entry), "legacy_title_match"

    return None, None


def _format_listing_detail_message(listing: Dict[str, Any]) -> str:
    title = listing.get("title") or "Başlıksız"
    price = listing.get("price")
    price_txt = f"{price} ₺" if price is not None else "Fiyat belirtilmemiş"
    category = listing.get("category") or "Kategori yok"
    location = listing.get("location") or listing.get("user_location") or "Konum belirtilmemiş"
    owner = listing.get("user_name") or "Satıcı bilgisi yok"
    phone = listing.get("user_phone") or listing.get("contact_phone") or "Telefon yok"
    description = listing.get("description") or "Açıklama yok"
    if len(description) > 600:
        description = description[:600] + "..."

    image_url = listing.get("image_url")
    images = listing.get("images") if isinstance(listing.get("images"), list) else []
    if not image_url and images:
        first = images[0]
        if isinstance(first, dict):
            image_url = first.get("image_url") or first.get("public_url")
        elif isinstance(first, str):
            image_url = first

    extra_images: List[str] = []
    if images:
        for img in images[1:]:
            if isinstance(img, dict):
                url = img.get("image_url") or img.get("public_url")
            elif isinstance(img, str):
                url = img
            else:
                url = None
            if url:
                extra_images.append(url)

    detail_msg = f"![{title}]({image_url})\n" if image_url else ""
    detail_msg += (
        f"**{title}**\n{price_txt} | {location} | {category}\n"
        f"Satıcı: {owner} | Telefon: {phone}\n\nAçıklama:\n{description}"
    )
    if extra_images:
        links = "\n".join([f"[Foto {i + 2}]({url})" for i, url in enumerate(extra_images)])
        if links:
            detail_msg += f"\n\nEk görseller:\n{links}"
    return detail_msg


def merge_unique_urls(existing: List[str], new_urls: List[str]) -> List[str]:
    """Merge new media URLs while preserving order and removing duplicates."""
    seen: set[str] = set()
    merged: List[str] = []
    for url in (existing or []) + (new_urls or []):
        if url and url not in seen:
            merged.append(url)
            seen.add(url)
    return merged


def is_publish_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    return any(
        (t := normalize_for_match(token)) and t in msg
        for token in [
            "yayınla",
            "yayınla!",
            "yayinla",
            "yayina",
            "yayınlamak",
            "yayinlamak",
            "publish",
        ]
    )


def is_delete_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    return any(
        (t := normalize_for_match(token)) and t in msg
        for token in ["sil", "ilanı sil", "ilani sil", "kaldır", "kaldir", "delete"]
    )


def is_create_listing_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False

    # Common typo tolerance
    if "ialn" in msg and "vermek istiyorum" in msg:
        return True

    # Explicit create/sell commands
    if msg in {
        "ilan olustur",
        "ilan ver",
        "ilan vermek istiyorum",
        "ilan koymak istiyorum",
        "ilan girmek istiyorum",
        "sat",
        "satiyorum",
        "satmak istiyorum",
    }:
        return True

    return any(
        phrase in msg
        for phrase in [
            "ilan olustur",
            "ilan ver",
            "ilan vermek istiyorum",
            "ilan koymak istiyorum",
            "ilan girmek istiyorum",
            "satmak istiyorum",
            "satiyorum",
            "satacagim",
            "satisa koy",
        ]
    )


def is_show_draft_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    if "taslak" not in msg and "taslağ" not in msg and "taslag" not in msg:
        return False
    return any(
        (t := normalize_for_match(token)) and t in msg
        for token in [
            "göster",
            "goster",
            "durum",
            "status",
            "güncel",
            "guncel",
            "güncelle",
            "guncelle",
            "bak",
            "görüntüle",
            "goruntule",
        ]
    )


def is_help_or_next_step_query(message: str) -> bool:
    """Return True when the user asks what to do next (meta/help), not slot content.

    This prevents messages like "şimdi ne yapmalıyım" from being accidentally persisted
    as title/description/location during deterministic slot filling.
    """

    msg = normalize_for_match(message)
    if not msg:
        return False

    # Keep this intentionally conservative: match explicit "what next" phrases.
    triggers = [
        "şimdi ne yapmalıyım",
        "simdi ne yapmaliyim",
        "ne yapmalıyım",
        "ne yapmaliyim",
        "ne yapacağım",
        "ne yapacagim",
        "ne yapayım",
        "ne yapayim",
        "ne yapmam lazım",
        "ne yapmam lazim",
        "bundan sonra",
        "sonra ne",
        "sonraki adım",
        "sonraki adim",
        "nasıl devam",
        "nasil devam",
        "nasıl ilerleyelim",
        "nasil ilerleyelim",
        "ne yapacağız",
        "ne yapacagiz",
        "yardım",
        "yardim",
        "help",
    ]
    if any(t in msg for t in triggers):
        return True

    # Common short form: "ne yapayım?" / "ne yapcam" etc.
    if bool(re.search(r"\b(ne\s+yap(ay[ıi]m|maliyim|acag[ıi]m|mam\s+lazim))\b", msg)):
        return True

    # If the user explicitly asks a question about next steps.
    if ("ne yap" in msg or "next" in msg) and "?" in msg:
        return True

    return False


def user_refuses_images(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    if any((t := normalize_for_match(token)) and t in msg for token in [
        "resimsiz",
        "fotoğrafsız",
        "fotografsiz",
        "resim yok",
        "fotoğraf yok",
        "fotograf yok",
        "fotoğraf eklemeyeceğim",
        "fotograf eklemeyecegim",
        "resim eklemeyeceğim",
        "resim eklemeyecegim",
        "resim yüklemek istemiyorum",
        "resim yuklemek istemiyorum",
        "fotoğraf yüklemek istemiyorum",
        "fotograf yuklemek istemiyorum",
        "fotoğraf eklemek istemiyorum",
        "fotograf eklemek istemiyorum",
    ]):
        return True

    # Fallback: handle unicode/typo variations by intent-based matching.
    mentions_image = any((t := normalize_for_match(tok)) and t in msg for tok in ["resim", "foto", "fotoğraf", "fotograf", "görsel", "gorsel"])
    refuses = any((t := normalize_for_match(tok)) and t in msg for tok in [
        "istemiyorum",
        "yüklemek istemiyorum",
        "yuklemek istemiyorum",
        "eklemek istemiyorum",
        "eklemeyeceğim",
        "eklemeyecegim",
    ])
    return bool(mentions_image and refuses)


def is_search_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False

    # Draft/status queries should never be treated as marketplace search.
    if "taslak" in msg or "taslağ" in msg or "taslag" in msg:
        return False

    # Availability-style queries (very common in Turkish): "bilgisayar var mı?".
    # These should be treated as search/browse intent even if the user doesn't say "ara".
    if bool(re.search(r"\bvar\s*m[ıi]\b", msg)) or any(token in msg for token in ["varmı", "varmi", "var mı", "var mi"]):
        return True
    # Common Turkish search/browse phrases
    if any(phrase in msg for phrase in [
        "arıyorum",
        "ariyorum",
        "benzer ara",
        "benzerini ara",
        "benzer",
        "ilan listele",
        "ilanları listele",
        "ilanlari listele",
        "ilanlar",
        "ilanları",
        "ilanlari",
        "listele",
        "search",
        "find",
    ]):
        return True

    # Word-boundary guard for short verbs like "ara" and "bul" to avoid matching inside other words.
    # IMPORTANT: do NOT treat bare "göster" as search unless the user mentions listings/products.
    if bool(re.search(r"\b(goster|göster)\b", msg)) and not ("ilan" in msg or "ürün" in msg or "urun" in msg):
        return False
    return bool(re.search(r"\b(ara|bul|listele|goster|göster)\b", msg))


def is_browse_all_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    return msg in {
        "ilan listele",
        "ilanları listele",
        "ilanlari listele",
        "ilanlar",
        "ilanları",
        "ilanlari",
        "listele",
        "ilanlari goster",
        "ilanları göster",
        "ilanları goster",
        "ilanlari göster",
        "ilanlari göster",
        "ilanları goster",
    }


def is_confirm_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    # Common confirmations + typos
    return any((t := normalize_for_match(token)) and t in msg for token in [
        "onayla",
        "onaylıyorum",
        "onayliyorum",
        "onay",
        "evet",
        "tamam",
        "olur",
        "ok",
        "okay",
        "onyalıyorum",
        "onyaliyorum",
    ])


def is_cancel_command(message: str) -> bool:
    msg = normalize_for_match(message)
    if not msg:
        return False
    # Treat "istemiyorum"-style refusals as a cancel as well to prevent users getting
    # stuck in a flow (especially create_listing) when they don't know the keyword.
    return any((t := normalize_for_match(token)) and t in msg for token in [
        "iptal",
        "vazgeç",
        "vazgec",
        "vazgeçtim",
        "vazgectim",
        "hayır",
        "hayir",
        "boşver",
        "bosver",
        "istemiyorum",
        "istemiyom",
        "satmak istemiyorum",
        "ilan oluşturmak istemiyorum",
        "ilan olusturmak istemiyorum",
        "gerek yok",
        "bırak",
        "birak",
    ])


def is_hesitation_signal(message: str) -> bool:
    """
    Detect user hesitation or uncertainty signals.
    These indicate the user is NOT ready to provide information.
    This prevents FSM loop trap where system keeps asking same question.
    """
    msg = normalize_for_match(message)
    if not msg:
        return False
    
    # Exclude false positives: user says "bilmiyorum" but with instruction
    if "otomatik" in msg or "sen belirle" in msg or "sen seç" in msg or "sen sec" in msg:
        return False
    
    # Short messages showing hesitation/pause
    if msg in ["dur", "dur bi", "dur bir", "bekle", "durur", "dursun"]:
        return True
    # Uncertainty patterns
    hesitation_patterns = [
        "aslında bakayım",
        "aslinda bakayim",
        "bakayım",
        "bakayim",
        "bakalım",
        "bakalim",
        "belki",
        "emin değilim",
        "emin degilim",
        "düşüneyim",
        "dusuneyim",
        "satmayabilirim",
        "vermeyebilirim",
        "karar vermedim",
    ]
    return any((t := normalize_for_match(token)) and t in msg for token in hesitation_patterns)


def is_wallet_balance_command(message: str) -> bool:
    """Return True when the user asks about wallet balance/remaining credits."""

    msg = normalize_for_match(message)
    if not msg:
        return False

    mentions_balance = any(tok in msg for tok in [
        "kredim",
        "kredi",
        "bakiye",
        "bakiyem",
        "cuzdan",
        "cüzdan",
        "wallet",
        "balance",
    ])
    if not mentions_balance:
        return False

    # Avoid accidental matches on troubleshooting statements like "kredi düşmüyor".
    asks_amount = any(tok in msg for tok in ["ne kadar", "kac", "kaç", "goster", "göster", "ogren", "öğren", "soyle", "söyle"])
    return bool(asks_amount or "kalan" in msg)


def sanitize_classified_intent(message: str, classified_intent: str | None) -> str | None:
    # Post-process router output to avoid accidental lock-in and wrong flows.
    #
    # The router can occasionally return intents that require state (e.g. publish/delete)
    # even when the user did not ask for them. This function constrains those cases.
    if not classified_intent:
        return classified_intent

    msg = (message or "").strip().lower()

    # Never enter publish/delete unless the user explicitly requested it.
    if classified_intent == "publish_or_delete" and not (is_publish_command(msg) or is_delete_command(msg)):
        # If it looks like a product query, prefer search.
        if is_search_command(msg):
            return "search_listings"
        return "small_talk"

    return classified_intent


def draft_is_publishable(draft: Dict[str, Any]) -> bool:
    listing = (draft or {}).get("listing_data") or {}
    images = (draft or {}).get("images") or []
    if not (listing.get("title") and str(listing.get("title")).strip()):
        return False
    if not (listing.get("description") and str(listing.get("description")).strip()):
        return False
    if listing.get("price") is None:
        return False
    if not (listing.get("condition") and str(listing.get("condition")).strip()):
        return False
    if not (listing.get("category") and str(listing.get("category")).strip()):
        return False
    allow_no_images = bool(isinstance(listing, dict) and listing.get("allow_no_images"))
    if not allow_no_images and not images:
        return False
    return True


def draft_has_any_content(draft: Dict[str, Any]) -> bool:
    """Return True if draft has any meaningful user-provided content."""
    listing = (draft or {}).get("listing_data") or {}
    images = (draft or {}).get("images") or []
    if images:
        return True
    for key in ["title", "description", "category"]:
        val = listing.get(key)
        if isinstance(val, str) and val.strip():
            return True
    if isinstance(listing.get("condition"), str) and str(listing.get("condition") or "").strip():
        return True
    if listing.get("price") is not None:
        return True
    return False


def draft_has_non_media_content(draft: Dict[str, Any]) -> bool:
    """Return True if draft has listing fields filled (excluding images).

    This is used to decide whether an existing draft likely contains an older item's data.
    Images alone are common in the "upload photos first" flow and should not trigger a reset.
    """
    listing = (draft or {}).get("listing_data") or {}
    for key in ["title", "description", "category"]:
        val = listing.get(key)
        if isinstance(val, str) and val.strip():
            return True
    if isinstance(listing.get("condition"), str) and str(listing.get("condition") or "").strip():
        return True
    if listing.get("price") is not None:
        return True
    return False


def should_reset_draft_for_new_listing(message: str, draft: Dict[str, Any]) -> bool:
    """Heuristic: if user explicitly starts a new listing, reset the single in-progress draft.

    This avoids mixing data when the platform enforces one active draft per user.
    Keep conservative: only reset on explicit create/sell phrases, not on 'devam'.
    """
    msg = normalize_for_match(message)
    if not msg:
        return False
    # Explicit switch to a different/new listing should reset the single draft.
    if any(phrase in msg for phrase in ["baska ilan", "yeni ilan", "farkli ilan"]):
        return draft_has_any_content(draft)
    if not is_create_listing_command(msg):
        return False
    # Don't reset when user says "devam"; they likely want to continue the current draft.
    if msg in {"devam", "devam et"}:
        return False
    # Reset only when we have non-media listing fields that indicate an older draft.
    # Do NOT reset drafts that only have images; otherwise we wipe newly uploaded photos and loop.
    return draft_has_non_media_content(draft)


async def handle_publish_or_delete_flow(
    message_body: str,
    session_id: str,
    session: Dict[str, Any],
    user_id: str,
    redis_disabled: bool,
    session_dirty: bool
) -> Dict[str, Any]:
    """Deterministic publish flow (no LLM): avoids looping confirmations and fake costs."""

    # Allow users to exit the publish/delete flow with a general cancel phrase.
    if is_cancel_command(message_body) and not user_refuses_images(message_body):
        try:
            draft_id = session.get("active_draft_id")
            if not draft_id and user_id:
                latest = await supabase_client.get_latest_draft_for_user(user_id)
                draft_id = (latest or {}).get("id")
            if isinstance(draft_id, str) and draft_id:
                await supabase_client.clear_pending_publish_state(draft_id)
        except Exception:
            pass

        session.pop("locked_intent", None)
        session["intent"] = None
        session["pending_publish"] = None
        session_dirty = True
        return {
            "success": True,
            "message": "Tamam. Yayınlama işlemini iptal ettim. İstersen ürün arayabilir ya da ilan oluşturmaya başlayabilirsin.",
            "data": {"type": "conversation", "intent": "small_talk"},
            "intent": "small_talk",
            "_session_dirty": session_dirty,
        }

    # Only support publish for now (delete can be added similarly)
    draft_id = session.get("active_draft_id")
    if not draft_id:
        # Gracefully exit: unlock intent and inform user
        session.pop("locked_intent", None)
        session["intent"] = None
        session_dirty = True
        return {
            "success": False,
            "message": "Henüz bir ilan taslağınız yok. Yayınlamak için önce 'ilan oluştur' yazıp yeni bir ilan başlatabilirsiniz. Ya da ürün aramak isterseniz 'iphone varmı' gibi arama yapabilirsiniz.",
            "data": {"type": "conversation"},
            "intent": "small_talk",
            "_session_dirty": session_dirty
        }

    # Read draft
    draft = await supabase_client.get_draft(draft_id)
    if not draft:
        return {
            "success": False,
            "message": "Taslak bulunamadı. Lütfen yeniden deneyin.",
            "data": {"type": "publish_delete"},
            "intent": "publish_or_delete"
        }

    listing_data = draft.get("listing_data") or {}
    if not isinstance(listing_data, dict):
        listing_data = {}

    # If the user explicitly wants to publish without photos, persist that preference.
    if user_refuses_images(message_body):
        try:
            await supabase_client.update_draft_allow_no_images(draft_id, True)
            draft = await supabase_client.get_draft(draft_id) or draft
            listing_data = (draft or {}).get("listing_data") or listing_data
            if not isinstance(listing_data, dict):
                listing_data = {}
        except Exception:
            pass

    session_pending = session.get("pending_publish")
    db_pending = listing_data.get("_pending_publish") if isinstance(listing_data, dict) else None
    if (
        (not isinstance(session_pending, dict) or session_pending.get("draft_id") != draft_id)
        and isinstance(db_pending, dict)
        and db_pending.get("draft_id") == draft_id
    ):
        session["pending_publish"] = db_pending
        session_pending = db_pending
        session_dirty = True

    pending = session_pending if isinstance(session_pending, dict) and session_pending.get("draft_id") == draft_id else None

    if pending:
        edit_request = extract_preview_edit(message_body)
        if edit_request:
            edit_result = await apply_preview_edit(draft_id, edit_request["field"], edit_request["value"])
            if not edit_result.get("success"):
                return {
                    "success": False,
                    "message": edit_result.get("message") or "Değişiklik kaydedilemedi.",
                    "data": {"type": "publish_preview", "draft_id": draft_id},
                    "intent": "publish_or_delete",
                    "_session_dirty": session_dirty
                }

            updated_draft = edit_result.get("draft") or draft
            preview_data = build_draft_preview_payload(updated_draft)
            pending["preview"] = preview_data
            session["pending_publish"] = pending
            session_dirty = True
            await supabase_client.set_pending_publish_state(draft_id, pending)

            cost = int(pending.get("cost") or settings.listing_credit_cost)
            balance = pending.get("balance")
            message_text = format_preview_message(
                preview_data,
                cost,
                balance,
                highlight=edit_result.get("message"),
                include_vision=not bool(session.get("vision_explained"))
            )
            return {
                "success": True,
                "message": message_text,
                "data": {
                    "type": "publish_preview",
                    "draft_id": draft_id,
                    "preview": preview_data,
                    "credit_cost": cost
                },
                "intent": "publish_or_delete",
                "_session_dirty": session_dirty
            }

        if is_cancel_command(message_body):
            session.pop("pending_publish", None)
            await supabase_client.clear_pending_publish_state(draft_id)
            session_dirty = True
            return {
                "success": True,
                "message": "Yayınlama işlemi iptal edildi.",
                "data": {"type": "publish_delete"},
                "intent": "publish_or_delete",
                "_session_dirty": session_dirty
            }

        if is_confirm_command(message_body):
            cost = int(pending.get("cost") or settings.listing_credit_cost)
            result = await publish_listing_tool.execute(draft_id=draft_id, user_id=user_id, credit_cost=cost)
            if result.get("success"):
                await supabase_client.clear_pending_publish_state(draft_id)
                session.pop("pending_publish", None)
                session["active_draft_id"] = None
                session["intent"] = None
                session.pop("locked_intent", None)
                session_dirty = True
                listing_id = (result.get("data") or {}).get("listing_id")
                return {
                    "success": True,
                    "message": f"İlan yayınlandı. İlan ID: {listing_id}" if listing_id else "İlan yayınlandı.",
                    "data": {"type": "publish_delete", "listing_id": listing_id},
                    "intent": "small_talk",
                    "_session_dirty": session_dirty
                }
            return {
                "success": False,
                "message": result.get("error") or "Yayınlama başarısız oldu.",
                "data": {"type": "publish_delete"},
                "intent": "publish_or_delete",
                "_session_dirty": session_dirty
            }

        cost = int(pending.get("cost") or settings.listing_credit_cost)
        preview_data = pending.get("preview") or build_draft_preview_payload(draft)
        pending["preview"] = preview_data
        session["pending_publish"] = pending
        session_dirty = True
        await supabase_client.set_pending_publish_state(draft_id, pending)
        message_text = format_preview_message(preview_data, cost, pending.get("balance"))
        if bool(session.get("vision_explained")):
            message_text = format_preview_message(preview_data, cost, pending.get("balance"), include_vision=False)
        return {
            "success": True,
            "message": message_text,
            "data": {
                "type": "publish_preview",
                "draft_id": draft_id,
                "preview": preview_data,
                "credit_cost": cost
            },
            "intent": "publish_or_delete",
            "_session_dirty": session_dirty
        }

    # Not pending: if draft incomplete, show what is missing
    if not draft_is_publishable(draft):
        return {
            "success": True,
            "message": build_draft_status_message(draft, include_vision=not bool(session.get("vision_explained"))),
            "data": {"type": "draft_update"},
            "intent": "create_listing",
            "_session_dirty": session_dirty
        }

    balance_result = await get_wallet_balance_tool.execute(user_id=user_id)
    balance = None
    if balance_result.get("success"):
        balance = (balance_result.get("data") or {}).get("balance")
    cost = int(settings.listing_credit_cost)
    preview_data = build_draft_preview_payload(draft)

    pending_payload = {
        "draft_id": draft_id,
        "cost": cost,
        "balance": balance,
        "preview": preview_data
    }

    session["pending_publish"] = pending_payload
    session_dirty = True
    await supabase_client.set_pending_publish_state(draft_id, pending_payload)

    return {
        "success": True,
        "message": format_preview_message(preview_data, cost, balance, include_vision=not bool(session.get("vision_explained"))),
        "data": {
            "type": "publish_preview",
            "draft_id": draft_id,
            "preview": preview_data,
            "credit_cost": cost
        },
        "intent": "publish_or_delete",
        "_session_dirty": session_dirty
    }


async def analyze_media_with_vision(media_urls: List[str]) -> List[Dict[str, Any]]:
    """Run OpenAI vision analysis for each media URL."""
    analyses: List[Dict[str, Any]] = []
    for url in media_urls:
        try:
            messages = [
                {
                    "role": "system",
                    "content": MEDIA_ANALYSIS_SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": MEDIA_ANALYSIS_USER_PROMPT},
                        {"type": "image_url", "image_url": {"url": url}}
                    ]
                }
            ]
            response = await openai_client.create_vision_completion(
                messages,
                max_tokens=600,
                response_format={"type": "json_object"}
            )
            raw = response.choices[0].message.content or "{}"
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"summary": raw}
            analyses.append({"image_url": url, "analysis": parsed})
        except Exception as exc:
            analyses.append({"image_url": url, "analysis": {"error": str(exc)}})
    return analyses


def format_media_analysis_message(analyses: List[Dict[str, Any]]) -> str:
    """Create a user-facing message summarizing media analyses."""
    summary_lines: List[str] = []
    for idx, entry in enumerate(analyses, 1):
        analysis = entry.get("analysis") or {}
        if not isinstance(analysis, dict):
            analysis = {"summary": analysis}
        parts: List[str] = []
        product = analysis.get("product") or analysis.get("category")
        if product:
            parts.append(f"ürün: {product}")
        condition = analysis.get("condition")
        if condition:
            parts.append(f"görsel izlenim: {condition}")
        features = analysis.get("features")
        if isinstance(features, list) and features:
            parts.append("özellikler: " + ", ".join(features[:3]))
        elif isinstance(features, str) and features:
            parts.append(f"özellikler: {features}")
        safety = analysis.get("safety_flags")
        if safety:
            if isinstance(safety, list):
                parts.append("uyarılar: " + ", ".join(safety))
            else:
                parts.append(f"uyarılar: {safety}")
        if not parts:
            fallback = analysis.get("summary") or analysis.get("description") or "Detay bulunamadı"
            parts.append(str(fallback))
        summary_lines.append(f"Fotoğraf {idx}: " + "; ".join(parts))

    if not summary_lines:
        summary_lines.append("Görseller analiz edilemedi.")

    prompt_line = "Bu görsel ile ne yapmak istiyorsunuz?"
    options = [
        "📦 İlan vermek",
        "🔍 Benzer ilanları aramak",
        "💰 Fiyat araştırması yapmak",
    ]

    return "\n\n".join([
        "📷 Görseli aldım ve analiz ettim.",
        "\n".join(summary_lines),
        prompt_line,
        "\n".join(options),
        "(Cevap olarak: 'ilan', 'ara' veya 'fiyat' yazabilirsiniz.)",
    ])


def classify_media_action_choice(message: str) -> Optional[str]:
    """Classify the mandatory choice after image-first flow.

    Returns one of: 'create_listing', 'search_listings', 'price_research', or None.
    """
    msg = (message or "").strip().lower()
    if not msg:
        return None

    # Strong signals
    if is_create_listing_command(msg) or "ilan" in msg or "sat" in msg:
        return "create_listing"
    if is_search_command(msg) or "ara" in msg or "benzer" in msg:
        return "search_listings"
    if user_asks_market_price(msg) or any(tok in msg for tok in ["fiyat", "kaç para", "kac para", "piyasa", "araştır"]):
        return "price_research"
    return None
def format_create_listing_intro_message(draft: Optional[Dict[str, Any]] = None) -> str:
    """Hybrid UX: let user answer in one message, but we can still ask missing parts."""

    example_title = ""
    example_desc = ""
    try:
        vision = _unwrap_vision_product((draft or {}).get("vision_product")) if isinstance(draft, dict) else {}
        if isinstance(vision, dict) and vision:
            example_title = generate_title_from_vision(vision)
            example_desc = generate_description_from_vision(vision)
    except Exception:
        example_title = ""
        example_desc = ""

    example_block = ""
    if example_title or example_desc:
        ex_lines: List[str] = ["Örnek (istersen tek mesajda böyle yazabilirsin):"]
        if example_title:
            ex_lines.append(f"• Ürün adı: {example_title}")
        if example_desc:
            ex_lines.append(f"• Kısa açıklama: {example_desc}")
        ex_lines.append("• Lokasyon: Bursa / İstanbul")
        ex_lines.append("• Durum: sıfır / 2. el")
        ex_lines.append("• Fiyat: 18.000 TL")
        example_block = "\n".join(ex_lines)

    base = (
        "Anladım. İlanını hazırlamak için aşağıdaki bilgileri yazabilirsin.\n\n"
        "• Ürün adı\n"
        "• Kısa açıklama\n"
        "• Lokasyon\n"
        "• Durum (Sıfır / 2. El / Az Kullanılmış - opsiyonel)\n"
        "• Fiyat\n\n"
        "İstersen hepsini tek mesajda yaz; eksik olursa ben sorarım.\n"
        "Dilersen ilanına eklemek için daha sonra da resim gönderebilirsin."
    )

    if example_block:
        return base + "\n\n" + example_block
    return base


def parse_condition_input(message: str) -> Optional[str]:
    # Use canonical labels users are familiar with in marketplace UIs.
    # Map common informal phrases ("iyi durumda", "temiz", "orta"...) into these.
    return canonicalize_condition(message)


def extract_listing_fields_from_freeform(message: str) -> Dict[str, Any]:
    """Best-effort extraction from a single freeform user message.

    Goal: support power-user one-shot messages like:
    "iphone 14 2.el bursa fiyat 18000 tl temiz"
    Without relying on LLM.
    """

    text = (message or "").strip()
    if not text:
        return {}

    lowered = text.lower()

    price = parse_price_input(text)
    location = parse_location_input(text)
    condition = parse_condition_input(text)

    # Remove obvious tokens for title extraction
    title_candidate = text
    # Strip price-like parts
    title_candidate = re.sub(r"\b(fiyat|price)\b\s*[:=]?\s*\d+[\d\.\,\s]*\s*(tl|₺)?", " ", title_candidate, flags=re.IGNORECASE)
    title_candidate = re.sub(r"\b\d{1,3}(?:[\.,]\d{3})+\b", " ", title_candidate)
    title_candidate = re.sub(r"\b\d{3,6}\b\s*(tl|₺)\b", " ", title_candidate, flags=re.IGNORECASE)
    title_candidate = re.sub(r"\b(tl|₺)\b", " ", title_candidate, flags=re.IGNORECASE)
    # Strip condition tokens
    title_candidate = re.sub(
        r"\b(2\s*\.?\s*el|ikinci\s*el|sıfır|sifir|yeni|az\s*kullanılmış|az\s*kullanilmis|yeni\s*gibi|like\s*new)\b",
        " ",
        title_candidate,
        flags=re.IGNORECASE,
    )
    # Strip common filler words
    title_candidate = re.sub(r"\b(acil|satılık|satilik)\b", " ", title_candidate, flags=re.IGNORECASE)

    if location:
        # remove location occurrence (best effort)
        title_candidate = re.sub(re.escape(location), " ", title_candidate, flags=re.IGNORECASE)

    title_candidate = " ".join(title_candidate.split()).strip()

    # Description: keep the original message as fallback short description
    description = ""
    # If user explicitly wrote a long-ish sentence, preserve it.
    if len(text) >= 10:
        # Avoid using the whole string if it is mostly title+numbers.
        description = text

    # If user asks to publish/cancel etc, do not treat as listing content.
    if is_publish_command(lowered) or is_cancel_command(lowered) or is_show_draft_command(lowered):
        return {}

    fields: Dict[str, Any] = {}
    if title_candidate and len(title_candidate) >= 3:
        fields["title"] = title_candidate
    if description and len(description.strip()) >= 6:
        fields["description"] = description.strip()
    if price is not None:
        fields["price"] = float(price)
    if location:
        fields["location"] = location
    if condition:
        fields["condition"] = condition
    return fields


def draft_ready_for_preview(draft: Dict[str, Any]) -> bool:
    listing = (draft or {}).get("listing_data") or {}
    if not isinstance(listing, dict):
        return False
    title = str(listing.get("title") or "").strip()
    location = str(listing.get("location") or "").strip()
    price = listing.get("price")
    condition = str(listing.get("condition") or "").strip()
    return bool(title and location and price is not None and condition)


def format_ready_preview_message(draft: Dict[str, Any]) -> str:
    preview = build_draft_preview_payload(draft)
    price = preview.get("price")
    if isinstance(price, (int, float)):
        price_text = f"{int(price):,} ₺".replace(",", ".")
    else:
        price_text = str(price) if price else "—"

    image_count = int(preview.get("image_count") or 0)
    lines: List[str] = [
        "✨ Harika! İlanını vitrinlik hale getirdim. Önizleme:",
        f"• Başlık: {preview.get('title') or '—'}",
        f"• Açıklama: {preview.get('description') or '—'}",
        f"• Fiyat: {price_text}",
        f"• Durum: {preview.get('condition') or '—'}",
        f"• Kategori: {preview.get('category') or '—'}",
        f"• Lokasyon: {preview.get('location') or '—'}",
        f"• Fotoğraflar: {image_count} / 5",
        "",
        "Yayınlamak için 'yayınla' yazabilirsin.",
        "Düzenlemek için: 'başlık: ...', 'açıklama: ...', 'fiyat: ...', 'lokasyon: ...' yazabilirsin.",
        "Daha fazla fotoğraf eklemek için yeni fotoğraf gönderebilirsin.",
    ]
    return "\n".join(lines)


async def maybe_enrich_title_description(
    draft_id: str,
    draft: Dict[str, Any],
    user_message: str,
) -> Optional[Dict[str, str]]:
    """Improve title/description using TitleAgent + DescriptionAgent.

    Uses user beyanı + optional vision 'görsel izlenim'.
    Disabled automatically for tests (OPENAI_API_KEY='test') and on any failure.
    """

    try:
        if not draft_id:
            return None
        if str(getattr(settings, "openai_api_key", "") or "").strip().lower() in {"test", ""}:
            return None

        listing = (draft or {}).get("listing_data") or {}
        if not isinstance(listing, dict):
            listing = {}

        title = str(listing.get("title") or "").strip()
        description = str(listing.get("description") or "").strip()

        # If either field is missing, we can't improve safely.
        if not title or not description:
            return None

        vision = _unwrap_vision_product((draft or {}).get("vision_product"))
        images = (draft or {}).get("images") or []
        image_count = len(images) if isinstance(images, list) else 0

        known = {
            "user_message": user_message,
            "user_beyani_title": title,
            "user_beyani_description": description,
            "price": listing.get("price"),
            "location": listing.get("location"),
            "category": listing.get("category"),
            "user_condition": listing.get("condition"),
            "vision": vision if isinstance(vision, dict) else {},
            "image_count": image_count,
        }

        # TitleAgent: minimal improvement
        title_agent = TitleAgent()
        title_msg = (
            "Aşağıdaki bilgilerle TASLAK başlığını minimal şekilde iyileştir.\n"
            "Kullanıcı beyanını koru; sadece netleştir ve vitrinde güçlü hale getir.\n\n"
            + json.dumps(known, ensure_ascii=False)
        )
        title_result = await title_agent.run(title_msg, context={"draft_id": draft_id})

        out_title = ""
        for call in (title_result or {}).get("tool_calls") or []:
            if call.get("tool") == "update_title":
                data = (call.get("result") or {}).get("data") or {}
                out_title = str(data.get("title") or "").strip()

        # DescriptionAgent: always improve
        desc_agent = DescriptionAgent()
        desc_msg = (
            "Aşağıdaki bilgilerle TASLAK açıklamasını mutlaka iyileştir.\n"
            "Kullanıcı beyanına sadık kal; varsa görsel izlenimi temkinli şekilde ekle.\n\n"
            + json.dumps(known, ensure_ascii=False)
        )
        desc_result = await desc_agent.run(desc_msg, context={"draft_id": draft_id})

        out_desc = ""
        for call in (desc_result or {}).get("tool_calls") or []:
            if call.get("tool") == "update_description":
                data = (call.get("result") or {}).get("data") or {}
                out_desc = str(data.get("description") or "").strip()

        if not out_title and not out_desc:
            return None
        return {"title": out_title, "description": out_desc}
    except Exception:
        return None


def _extract_buffered_media_from_draft(draft: Optional[Dict[str, Any]]) -> tuple[list[str], list[Dict[str, Any]]]:
    listing = (draft or {}).get("listing_data") or {}
    if not isinstance(listing, dict):
        return [], []
    urls = listing.get("_buffered_media_urls")
    analyses = listing.get("_buffered_media_analysis")
    if not isinstance(urls, list):
        urls = []
    urls = [str(u) for u in urls if isinstance(u, str) and u]
    if not isinstance(analyses, list):
        analyses = []
    cleaned_analyses: list[Dict[str, Any]] = []
    for a in analyses:
        if isinstance(a, dict) and a.get("image_url"):
            cleaned_analyses.append(a)
    return urls, cleaned_analyses

# UUID helper for anonymous web users
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def normalize_user_id(raw_id: Optional[str]) -> str:
    """Ensure we always operate with a valid UUID (required by Supabase)."""
    if raw_id:
        try:
            return str(uuid.UUID(str(raw_id)))
        except (ValueError, AttributeError, TypeError):
            # Deterministically hash non-UUID identifiers (e.g., web_user_x) to stable UUIDs
            return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw_id)))
    return str(uuid.uuid4())


def _unwrap_vision_product(vision: Any) -> Dict[str, Any]:
    """Return the inner vision dict.

    Some flows may store vision_product directly as the analysis dict, while others
    may store a wrapper like {"image_url": ..., "analysis": {...}}.
    """
    if isinstance(vision, dict) and isinstance(vision.get("analysis"), dict):
        return vision.get("analysis") or {}
    return vision if isinstance(vision, dict) else {}


def generate_title_from_vision(vision: Any) -> str:
    v = _unwrap_vision_product(vision)
    product = str(v.get("product") or v.get("category") or "").strip()
    condition = str(v.get("condition") or "").strip()
    features = v.get("features")

    feature_txt = ""
    if isinstance(features, list) and features:
        feature_txt = ", ".join([str(f).strip() for f in features[:2] if str(f).strip()])
    elif isinstance(features, str) and features.strip():
        feature_txt = features.strip()

    base = product or "Ürün"
    parts: List[str] = [base]
    if feature_txt:
        parts.append(feature_txt)

    # Never put visual impression into the title; title should be product + key features.

    title = " - ".join([p for p in parts if p])
    title = " ".join(title.split())
    return title[:100].rstrip(" -")


def generate_description_from_vision(vision: Any) -> str:
    v = _unwrap_vision_product(vision)
    if not v:
        return ""
    product = str(v.get("product") or v.get("category") or "Ürün").strip()
    condition = str(v.get("condition") or "").strip()
    features = v.get("features")

    feature_txt = ""
    if isinstance(features, list) and features:
        feature_txt = ", ".join([str(f).strip() for f in features[:4] if str(f).strip()])
    elif isinstance(features, str) and features.strip():
        feature_txt = features.strip()

    sentences: List[str] = []
    if product:
        sentences.append(f"{product} satışa hazır.")
    if condition:
        sentences.append(f"Görsel izlenim: {condition}.")
    if feature_txt:
        sentences.append(f"Öne çıkan özellikler: {feature_txt}.")
    sentences.append("Detay için mesaj atabilirsiniz.")
    return " ".join(" ".join(sentences).split())


def build_draft_status_message(draft: Dict[str, Any], include_vision: bool = True) -> str:
    """Generate a friendly status message about the current draft state.

    include_vision controls whether we print the vision summary block.
    """
    listing = draft.get("listing_data") or {}
    images = draft.get("images") or []
    summary_lines: List[str] = []
    missing: List[str] = []
    vision_lines: List[str] = []

    def add_line(label: str, value: str):
        if value:
            summary_lines.append(f"• {label}: {value}")

    title = listing.get("title")
    if title:
        add_line("Başlık", title)
    else:
        missing.append("ürünün adı (başlık)")

    description = listing.get("description")
    if description:
        preview = description if len(description) <= 160 else description[:157] + "..."
        add_line("Açıklama", preview)
    else:
        missing.append("detaylı açıklama")

    price = listing.get("price")
    if price is not None:
        price_value = f"{price} ₺" if isinstance(price, (int, float)) else str(price)
        add_line("Fiyat", price_value)
    else:
        missing.append("fiyat")

    condition = listing.get("condition")
    if condition:
        add_line("Durum", str(condition))
    else:
        missing.append("durum (Sıfır / 2. El)")

    category = listing.get("category")
    if category:
        add_line("Kategori", category)
    else:
        missing.append("kategori")

    allow_no_images = bool(isinstance(listing, dict) and listing.get("allow_no_images"))
    add_line("Fotoğraflar", f"{len(images)} adet" if images else "henüz eklenmedi")
    if not images and not allow_no_images:
        missing.append("ürün fotoğrafları")

    vision = draft.get("vision_product")
    if isinstance(vision, dict) and isinstance(vision.get("analysis"), dict):
        vision = vision.get("analysis")

    if include_vision and isinstance(vision, dict):
        vision_category = vision.get("category") or vision.get("product")
        vision_condition = vision.get("condition")
        features = vision.get("features")
        if vision_category and not category:
            add_line("Kategori", str(vision_category))
        if vision_category:
            vision_lines.append(f"Ürün türü: {vision_category}")
        if vision_condition:
            vision_lines.append(f"Görsel izlenim: {vision_condition}")
        if isinstance(features, list) and features:
            top_features = ", ".join([str(f) for f in features[:3] if f])
            if top_features:
                vision_lines.append(f"Öne çıkan özellikler: {top_features}")
        elif isinstance(features, str) and features:
            vision_lines.append(f"Öne çıkan özellikler: {features}")

    message_parts = ["📋 Taslak durumu güncellendi."]
    if summary_lines:
        message_parts.append("\n".join(summary_lines))

        if vision_lines:
            message_parts.append("🔎 Görsel analizi:\n" + "\n".join(f"• {line}" for line in vision_lines))

    if missing:
        message_parts.append(
            "Birkaç şey eksik kalmış: " + ", ".join(missing) + ". Bunları yazabilir ya da fotoğraf gönderebilirsin 📸"
        )
    else:
        message_parts.append("Tüm temel bilgiler tamam. Hazırsanız 'yayınla' yazarak ilanı yayınlayabilirsiniz.")

    return "\n\n".join(part.strip() for part in message_parts if part.strip())


def _extract_preview_image_url(entry: Any) -> Optional[str]:
    if isinstance(entry, dict):
        for key in ["image_url", "public_url", "url", "path"]:
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    elif isinstance(entry, str) and entry.strip():
        return entry.strip()
    return None


def build_draft_preview_payload(draft: Dict[str, Any]) -> Dict[str, Any]:
    listing = (draft or {}).get("listing_data") or {}
    description = str(listing.get("description") or "").strip()
    if len(description) > 280:
        description_preview = description[:277] + "..."
    else:
        description_preview = description

    images: List[str] = []
    for entry in (draft or {}).get("images") or []:
        url = _extract_preview_image_url(entry)
        if url:
            images.append(url)

    return {
        "draft_id": draft.get("id"),
        "title": str(listing.get("title") or "").strip(),
        "description": description_preview,
        "full_description": description,
        "price": listing.get("price"),
        "condition": str(listing.get("condition") or "").strip(),
        "category": str(listing.get("category") or "").strip(),
        "location": str(listing.get("location") or "").strip(),
        "images": images,
        "image_count": len(images),
        "vision": draft.get("vision_product") if isinstance(draft.get("vision_product"), dict) else None,
    }


def format_preview_message(
    preview: Dict[str, Any],
    cost: int,
    balance: Optional[float] = None,
    highlight: Optional[str] = None,
    include_vision: bool = True
) -> str:
    def _needs_vehicle_detail_prompt(desc: str, category: str) -> bool:
        cat_l = (category or "").lower()
        if not any(token in cat_l for token in ["oto", "otomotiv", "araç", "arac", "araba", "otomobil", "vasıta", "vasita", "moto", "motor"]):
            return False
        desc_l = (desc or "").lower()
        keywords = ["km", "kilometre", "tramer", "hasar", "kaza", "boya", "değişen", "degisen"]
        return not any(k in desc_l for k in keywords)

    lines: List[str] = ["� YAYIN ÖNCESİ KONTROL"]
    lines.append("")

    title = preview.get("title") or "—"
    description = preview.get("description") or "—"
    price = preview.get("price")
    if isinstance(price, (int, float)):
        price_text = f"{int(price):,} ₺".replace(",", ".")
    else:
        price_text = str(price) if price else "—"
    category = preview.get("category") or "—"
    condition = preview.get("condition") or "—"
    location = preview.get("location") or "—"
    image_count = preview.get("image_count") or 0
    full_desc = preview.get("full_description") or description

    lines.append("BAŞLIK:")
    lines.append(title)
    lines.append("")
    lines.append("AÇIKLAMA:")
    lines.append(description)
    lines.append("")
    lines.append("FİYAT:")
    lines.append(price_text)
    lines.append("")
    lines.append("DURUM:")
    lines.append(condition)
    lines.append("")
    lines.append("KATEGORİ:")
    lines.append(category)
    lines.append("")
    lines.append("LOKASYON:")
    lines.append(location)
    lines.append("")
    lines.append("FOTOĞRAFLAR:")
    lines.append(f"{image_count} adet")

    vision = preview.get("vision")
    if include_vision and isinstance(vision, dict):
        vision_lines: List[str] = []
        if vision.get("condition"):
            vision_lines.append(f"Görsel izlenim: {vision['condition']}")
        features = vision.get("features")
        if isinstance(features, list) and features:
            feature_txt = ", ".join([str(f) for f in features[:3] if f])
            if feature_txt:
                vision_lines.append(f"Özellikler: {feature_txt}")
        vision_desc = vision.get("description")
        if vision_desc:
            vision_lines.append(f"Not: {vision_desc}")
        if vision_lines:
            lines.append("")
            lines.append("🔎 GÖRSEL ANALİZİ:")
            lines.extend(f"• {entry}" for entry in vision_lines)

    if highlight:
        lines.append("")
        lines.append(highlight)

    # Optional reminder for automotive listings when key details are missing.
    if _needs_vehicle_detail_prompt(full_desc, category):
        lines.append("")
        lines.append("🚗 OTOMOTİV HATIRLATMA (isteğe bağlı):")
        lines.append("• Km, boya/değişen ve tramer/hasar durumunu eklersen alıcılar için net olur. İstersen açıklamaya ekleyebilirim.")
        lines.append("")

    lines.append("")
    lines.append("─────────────────────────")
    if balance is not None:
        lines.append(f"Mevcut bakiyeniz: {int(balance)} kredi")
    lines.append(f"Yayın ücreti: {cost} kredi")
    lines.append("")
    lines.append("🛠️ KOMUTLAR")
    lines.append("👉 Onayla: onayla")
    lines.append("👉 Düzenle: başlık: ..., açıklama: ..., fiyat: ...")
    lines.append("👉 İptal: iptal")
    lines.append("")
    lines.append("İlanınızda değişiklik yapmak veya yayınlamak için yukarıdaki komutları kullanın.")

    return "\n".join(lines)


_PREVIEW_EDIT_KEYWORDS = {
    "title": ["başlık", "baslik", "başlığı", "basligi", "title"],
    "description": ["açıklama", "aciklama", "açıklamayı", "aciklamayi", "description"],
    "price": ["fiyat", "price"],
    "category": ["kategori", "category"],
    "location": ["lokasyon", "konum", "location"],
    "condition": ["durum", "kondisyon", "condition"],
}

_PREVIEW_EDIT_KEYWORDS_NORM = {
    field: {normalize_for_match(k) for k in keywords}
    for field, keywords in _PREVIEW_EDIT_KEYWORDS.items()
}


def extract_preview_edit(message: str) -> Optional[Dict[str, str]]:
    if not message:
        return None
    text = message.strip()
    if not text:
        return None

    # Parse deterministic edits like: "AÇIKLAMA: ..." / "Başlık = ...".
    # We avoid relying on re.IGNORECASE for Turkish I/İ/ı edge-cases by normalizing.
    for match in re.finditer(r"([^\n:=]{2,40})\s*[:=]\s*([^\n]+)", text):
        raw_key = (match.group(1) or "").strip()
        raw_value = (match.group(2) or "").strip()
        if not raw_key or not raw_value:
            continue

        key_norm = normalize_for_match(raw_key)
        for field, keywords_norm in _PREVIEW_EDIT_KEYWORDS_NORM.items():
            if key_norm in keywords_norm:
                return {"field": field, "value": raw_value}

    return None


async def apply_preview_edit(draft_id: str, field: str, value: str) -> Dict[str, Any]:
    if not draft_id:
        return {"success": False, "message": "Henüz başlattığın bir ilan yok 🤷‍♂️"}
    clean_value = (value or "").strip()
    if not clean_value:
        return {"success": False, "message": "Yeni değeri anlayamadım."}

    success = False
    feedback = ""

    if field == "title":
        if len(clean_value) < 3:
            return {"success": False, "message": "Başlık en az 3 karakter olmalı."}
        success = await supabase_client.update_draft_title(draft_id, clean_value)
        feedback = "Başlık güncellendi."
    elif field == "description":
        if len(clean_value) < 10:
            return {"success": False, "message": "Açıklama biraz daha detaylı olmalı (en az 10 karakter)."}
        success = await supabase_client.update_draft_description(draft_id, clean_value)
        feedback = "Açıklama güncellendi."
    elif field == "price":
        parsed = parse_price_input(clean_value)
        if parsed is None:
            return {"success": False, "message": "Fiyatı sayısal olarak yazın (örn: 12500)."}
        success = await supabase_client.update_draft_price(draft_id, float(parsed))
        feedback = "Fiyat güncellendi."
    elif field == "category":
        normalized = normalize_category_input(clean_value) or clean_value.title()
        success = await supabase_client.update_draft_category(draft_id, normalized)
        feedback = f"Kategori '{normalized}' olarak güncellendi."
    elif field == "location":
        if len(clean_value) < 2:
            return {"success": False, "message": "Lokasyon en az 2 karakter olmalı."}
        success = await supabase_client.update_draft_location(draft_id, clean_value)
        feedback = "Lokasyon güncellendi."
    elif field == "condition":
        parsed = parse_condition_input(clean_value)
        if not parsed:
            return {"success": False, "message": "Durum için 'Sıfır', '2. El' veya 'Az Kullanılmış' yazın."}
        success = await supabase_client.update_draft_condition(draft_id, parsed)
        feedback = f"Durum güncellendi: {parsed}"
    else:
        return {"success": False, "message": "Bu alanı düzenleyemiyorum."}

    if not success:
        return {"success": False, "message": "Değişikliği kaydedemedim. Bir daha dener misin? 😅"}

    updated = await supabase_client.get_draft(draft_id)
    return {"success": True, "message": feedback, "draft": updated}


_GREETING_TOKENS = {
    "selam",
    "selamlar",
    "merhaba",
    "mrb",
    "hey",
    "hi",
    "hello",
    "günaydın",
    "gunaydin",
    "iyi akşamlar",
    "iyi aksamlar",
    "iyi geceler",
}


def looks_like_greeting(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    if msg in _GREETING_TOKENS:
        return True
    # very short social pings
    if len(msg) <= 6 and any(tok in msg for tok in ["selam", "mrb", "hi", "hey"]):
        return True
    return False


_COMMAND_ONLY_TOKENS = {
    "ilan oluştur",
    "ilan olustur",
    "ilan",
    "başlat",
    "baslat",
    "devam",
    "devam et",
}


def is_improve_command(message: str) -> bool:
    """User requests deterministic title/description improvement in preview.

    We keep this narrow to avoid accidentally treating normal sentences as a command.
    """
    msg = (message or "").strip().lower()
    if not msg:
        return False

    # Normalize Turkish chars (ş/ı/ğ/ç/ö/ü) and punctuation.
    transl = str.maketrans({
        "ş": "s",
        "ı": "i",
        "ğ": "g",
        "ç": "c",
        "ö": "o",
        "ü": "u",
    })
    msg_n = msg.translate(transl)
    msg_n = re.sub(r"[^a-z0-9\s]", " ", msg_n)
    msg_n = re.sub(r"\s+", " ", msg_n).strip()

    # Accept: "iyileştir", "iyilestir", "iyilestir lutfen".
    if msg_n == "iyilestir":
        return True
    if msg_n.startswith("iyilestir "):
        # Only allow small polite suffixes to stay deterministic.
        rest = msg_n[len("iyilestir "):].strip()
        return rest in {"lutfen", "lutfenn", "pls", "please"}
    return False


_FLOW_CONTROL_PATTERNS: list[re.Pattern[str]] = [
    # Generic listing intent messages that should not become title/description.
    re.compile(r"\bilan\b\s*(oluştur|olustur|aç|ac|başlat|baslat|ver|yayınla|yayinla)\b", re.IGNORECASE),
    re.compile(r"\bilan\b.*\b(verm(e|ek)|satmak|satış|satis|vermek\s+istiyorum|istiyorum)\b", re.IGNORECASE),
    re.compile(r"\btasla(?:k|ğ)\b.*\b(oluştur|olustur|aç|ac|başlat|baslat|kullanmak\s+istiyorum|istiyorum)\b", re.IGNORECASE),
    # Category uncertainty / delegation
    re.compile(r"\bkategori\b.*\b(bilmiyorum|emin\s+değilim|emin\s+degilim|otomatik|sen\s+seç|sen\s+sec)\b", re.IGNORECASE),
]


def is_command_only_message(message: str) -> bool:
    msg = (message or "").strip().lower()
    if msg in _COMMAND_ONLY_TOKENS:
        return True

    # Treat short, flow-control-like meta sentences as commands too.
    # This prevents inputs like "ilan vermek istiyorum" from being saved as title/description.
    if not msg:
        return False
    if len(msg) > 80:
        return False
    word_count = len([w for w in re.split(r"\s+", msg) if w])
    if word_count > 10:
        return False
    for pat in _FLOW_CONTROL_PATTERNS:
        if pat.search(msg):
            return True
    return False


def user_requests_auto_category(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    if msg in {"otomatik", "otomatik belirle"}:
        return True
    if "otomatik" in msg and "kategori" in msg:
        return True
    if "kategori" in msg and any(phrase in msg for phrase in [
        "sen belirle",
        "sen seç",
        "sen sec",
        "bilmiyorum",
        "emin değilim",
        "emin degilim",
    ]):
        return True
    return False


def infer_category_from_draft(draft: Dict[str, Any]) -> Optional[str]:
    listing = (draft or {}).get("listing_data") or {}
    vision = _unwrap_vision_product((draft or {}).get("vision_product"))

    candidates: list[str] = []
    if isinstance(vision, dict):
        candidates.append(str(vision.get("category") or "").strip())
        candidates.append(str(vision.get("product") or "").strip())
    candidates.append(str(listing.get("title") or "").strip())
    candidates.append(str(listing.get("description") or "").strip())

    for cand in candidates:
        if cand:
            normalized = normalize_category_input(cand)
            if normalized:
                return normalized

    combined = " ".join([c for c in candidates if c]).strip()
    if combined:
        normalized = normalize_category_input(combined)
        if normalized:
            return normalized
    return None


def next_missing_slot(draft: Dict[str, Any]) -> Optional[str]:
    listing = (draft or {}).get("listing_data") or {}
    images = (draft or {}).get("images") or []
    # DEBUG: log draft state to diagnose photo-loss loop
    logger.debug(f"next_missing_slot: draft_id={draft.get('id')}, images_count={len(images)}, listing_keys={list(listing.keys())}")
    allow_no_images = bool(isinstance(listing, dict) and listing.get("allow_no_images"))
    if not (listing.get("title") or "").strip():
        return "title"
    if not (listing.get("description") or "").strip():
        return "description"
    if listing.get("price") is None:
        return "price"
    if not (str(listing.get("condition") or "").strip()):
        return "condition"
    if not (str(listing.get("location") or "").strip()):
        return "location"
    if not (listing.get("category") or "").strip():
        return "category"
    if not images and not allow_no_images:
        return "images"
    return None


def build_next_step_message(draft: Dict[str, Any]) -> str:
    slot = next_missing_slot(draft)
    vision = (draft or {}).get("vision_product") or {}
    suggested_category = ""
    if isinstance(vision, dict):
        suggested_category = str(vision.get("category") or vision.get("product") or "").strip()

    if slot == "images":
        return "Fotoğraf eklemek ister misiniz? İsterseniz fotoğraf gönderebilir veya 'resimsiz' yazarak resimsiz devam edebilirsiniz."
    if slot == "title":
        return "Ürünün adı nedir? (Örn: 'iPhone 14 128GB siyah')"
    if slot == "description":
        return "Kısa bir açıklama yazar mısınız? (durum, çizik/hasar, kutu/fatura, takas vb.)"
    if slot == "price":
        return "Fiyat nedir? İsterseniz 'kaç para eder' yazın, piyasa verisine göre tahmin söyleyeyim."
    if slot == "condition":
        return "Durum nedir? (Sıfır / 2. El / Az Kullanılmış)"
    if slot == "location":
        return "Lokasyon nedir? (Örn: 'İstanbul' veya 'Ankara Çankaya')"
    if slot == "category":
        if suggested_category:
            return f"Kategori nedir? (İsterseniz önerim: {suggested_category}; bilmiyorsanız 'otomatik' yazın)"
        return "Kategori nedir? (Örn: Elektronik, Otomotiv...; bilmiyorsanız 'otomatik' yazın)"

    # Completed
    return "Tüm temel bilgiler tamam. Hazırsanız 'yayınla' yazarak ilanı yayınlayabilirsiniz."


def user_asks_market_price(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return any(phrase in msg for phrase in [
        "kaç para eder",
        "kac para eder",
        "ne kadar eder",
        "ne kadara gider",
        "piyasa",
        "fiyat öner",
        "fiyat oner",
    ])


def normalize_category_input(message: str) -> Optional[str]:
    """Normalize common category inputs to canonical labels.

    Returns None if the message does not look like a category.
    """
    msg = (message or "").strip().lower()
    if not msg:
        return None

    # Keep this intentionally small and conservative to avoid misclassifying insults/random text as a category.
    mapping = {
        "otomotiv": "Otomotiv",
        "oto": "Otomotiv",
        "vasita": "Otomotiv",
        "taşıt": "Otomotiv",
        "tasit": "Otomotiv",
        "araba": "Otomotiv",
        "araç": "Otomotiv",
        "arac": "Otomotiv",
        "otomobil": "Otomotiv",
        "v a s i t a": "Otomotiv",
        "vasıta": "Otomotiv",
        "vasita": "Otomotiv",
        "elektronik": "Elektronik",
        "telefon": "Elektronik",
        "bilgisayar": "Elektronik",
        "ev": "Ev & Yaşam",
        "ev yaşam": "Ev & Yaşam",
        "ev & yaşam": "Ev & Yaşam",
        "ev ve yaşam": "Ev & Yaşam",
        "mobilya": "Ev & Yaşam",
        "dekorasyon": "Ev & Yaşam",
        "beyaz esya": "Ev & Yaşam",
        "beyaz eşya": "Ev & Yaşam",
        "moda": "Moda & Aksesuar",
        "aksesuar": "Moda & Aksesuar",
        "giyim": "Moda & Aksesuar",
        "spor": "Spor & Outdoor",
        "outdoor": "Spor & Outdoor",
        "hobi": "Hobi, Koleksiyon & Sanat",
        "koleksiyon": "Hobi, Koleksiyon & Sanat",
        "sanat": "Hobi, Koleksiyon & Sanat",
        "emlak": "Emlak",
        "hizmet": "Hizmetler",
        "hizmetler": "Hizmetler",
        "ustalar": "Hizmetler",
        "usta": "Hizmetler",
        "özel ders": "Eğitim & Kurs",
        "ozel ders": "Eğitim & Kurs",
        "egitim": "Eğitim & Kurs",
        "eğitim": "Eğitim & Kurs",
        "is ilanlari": "İş İlanları",
        "iş ilanları": "İş İlanları",
        "is ilani": "İş İlanları",
        "iş ilanı": "İş İlanları",
        "dijital": "Dijital Ürün & Hizmetler",
        "abonelik": "Dijital Ürün & Hizmetler",
        "yazilim": "Dijital Ürün & Hizmetler",
        "yazılım": "Dijital Ürün & Hizmetler",
        "yedek parca": "Yedek Parça & Aksesuar",
        "yedek parça": "Yedek Parça & Aksesuar",
        "diger": "Diğer",
        "diğer": "Diğer",
        "genel": "Diğer",
    }

    if msg in mapping:
        return mapping[msg]

    # Deterministic library-based classification (brands + product keywords)
    try:
        from services.category_library import classify_category
        lib_cat = classify_category(msg)
        if lib_cat:
            return lib_cat
    except Exception:
        pass

    # Handle forms like "kategori: otomotiv" or "kategorisi otomotiv" or "kategori otomotiv olsun"
    m = re.search(r"\bkategori(?:si)?\b\s*[:\-]?\s*(.+)$", msg)
    if m:
        rest = (m.group(1) or "").strip()
        # remove common trailing verbs
        rest = re.sub(r"\b(olsun|yap|yapalım|yapalim|seç|sec|seçelim|secelim|olarak|diye|lütfen|lutfen)\b", " ", rest)
        rest = re.sub(r"[^0-9a-zA-ZçğıöşüÇĞİÖŞÜ& ]+", " ", rest).strip()
        tokens = [t for t in rest.split() if t]
        if tokens:
            # Try 2-token phrase first (e.g., 'ev yaşam'), then first token.
            cand2 = " ".join(tokens[:2]).lower()
            if cand2 in mapping:
                return mapping[cand2]
            cand1 = tokens[0].lower()
            if cand1 in mapping:
                return mapping[cand1]

            # Library-based classification on the extracted segment
            try:
                from services.category_library import classify_category
                lib_cat = classify_category(rest)
                if lib_cat:
                    return lib_cat
            except Exception:
                pass
            # As a last resort, accept Title-case for short clean values
            if len(tokens) <= 2:
                return " ".join([t.title() for t in tokens]).strip() or None

    # For single-token inputs, accept Title-case as a last resort only if it looks like a known category word.
    tokens = [t for t in msg.replace("/", " ").replace(",", " ").split() if t]
    if len(tokens) == 1 and tokens[0] in mapping:
        return mapping[tokens[0]]

    return None


def parse_price_input(message: str) -> Optional[float]:
    """Best-effort price parser for direct user input (e.g. '250000', '250.000', '250k')."""
    msg = (message or "").strip().lower()
    if not msg:
        return None
    # Don't treat market-price questions as numeric input.
    if user_asks_market_price(msg):
        return None

    # Normalize thousands separators
    cleaned = msg.replace("₺", "").replace("tl", "").replace("try", "").strip()
    multiplier = 1.0
    if cleaned.endswith("k"):
        multiplier = 1000.0
        cleaned = cleaned[:-1].strip()
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", "")
    if not cleaned.isdigit():
        return None
    try:
        return float(int(cleaned) * multiplier)
    except Exception:
        return None


_KNOWN_CITIES = {
    "istanbul",
    "ankara",
    "izmir",
    "bursa",
    "antalya",
    "adana",
    "konya",
    "gaziantep",
    "kayseri",
    "mersin",
    "kocaeli",
    "sakarya",
    "trabzon",
    "samsun",
    "eskişehir",
    "eskisehir",
    "diyarbakır",
    "diyarbakir",
    "şanlıurfa",
    "sanliurfa",
    "tekirdağ",
    "tekirdag",
}


def parse_location_input(message: str) -> Optional[str]:
    """Best-effort location parser for mixed messages.

    Intended for patterns like: "..., 18.000 TL, İstanbul".
    Returns None if it cannot confidently extract a location.
    """
    msg = (message or "").strip()
    if not msg:
        return None

    # Prefer last comma-separated segment (common in single-message listing summaries)
    if "," in msg:
        last = msg.split(",")[-1].strip()
        if last:
            last_norm = re.sub(r"[^0-9a-zA-ZçğıöşüÇĞİÖŞÜ ]+", " ", last).strip().lower()
            if last_norm in _KNOWN_CITIES:
                return last.title() if last_norm not in {"istanbul", "ankara", "izmir"} else last.capitalize()
            # Accept "City District" if the first token is a known city
            toks = [t for t in last_norm.split() if t]
            if toks and toks[0] in _KNOWN_CITIES and len(last) <= 40:
                return last.strip()

    # Otherwise, look for any standalone known city token
    lowered = msg.lower()
    for city in _KNOWN_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return city.capitalize() if city in {"istanbul", "ankara", "izmir"} else city.title()
    return None


def extract_vision_search_query(analyses: List[Dict[str, Any]]) -> str:
    """Convert cached vision JSON into a simple Turkish keyword query."""
    tokens: List[str] = []
    for entry in analyses or []:
        analysis = (entry or {}).get("analysis")
        if not isinstance(analysis, dict):
            continue
        for key in ["product", "category", "condition"]:
            val = str(analysis.get(key) or "").strip()
            if val and val.lower() not in {"", "unknown", "bilinmiyor"}:
                tokens.append(val)
        feats = analysis.get("features")
        if isinstance(feats, list):
            for f in feats[:3]:
                f_txt = str(f or "").strip()
                if f_txt:
                    tokens.append(f_txt)
    # de-dup while preserving order
    seen = set()
    uniq: List[str] = []
    for t in tokens:
        k = t.lower()
        if k not in seen:
            uniq.append(t)
            seen.add(k)
    return " ".join(uniq[:10]).strip()

router = APIRouter(prefix="/webchat", tags=["webchat"])


@router.get("/categories")
async def get_categories() -> Dict[str, Any]:
    """Return supported categories for frontend dropdown/filter consistency."""
    try:
        from services.category_library import get_supported_categories, get_category_options
        return {
            "categories": get_supported_categories(),
            "options": get_category_options(),
        }
    except Exception as e:
        logger.error(f"Failed to load categories: {e}")
        # Fail safe: return a minimal set
        return {
            "categories": ["Elektronik", "Otomotiv", "Diğer"],
            "options": [
                {"id": "Elektronik", "label": "Elektronik"},
                {"id": "Otomotiv", "label": "Otomotiv"},
                {"id": "Diğer", "label": "Genel / Diğer"},
            ],
        }


class ChatMessage(BaseModel):
    """Chat message model"""
    session_id: str
    message: str
    user_id: Optional[str] = None
    media_url: Optional[str] = None
    media_urls: Optional[List[str]] = None


class MediaAnalysisRequest(BaseModel):
    """Media analysis request model"""
    session_id: str
    user_id: Optional[str] = None
    media_urls: List[str]


class ChatResponse(BaseModel):
    """Chat response model"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    intent: Optional[str] = None


class WalletBalanceResponse(BaseModel):
    """Wallet balance API response"""
    success: bool
    balance: Optional[int] = None
    currency: str = "credits"
    message: Optional[str] = None


class WalletTransactionsResponse(BaseModel):
    """Wallet history API response"""
    success: bool
    transactions: List[Dict[str, Any]]
    message: Optional[str] = None


@router.get("/wallet/balance", response_model=WalletBalanceResponse)
async def get_wallet_balance(session_id: Optional[str] = None, user_id: Optional[str] = None):
    """Wallet balance endpoint (Sprint 2)."""
    session: Dict[str, Any] | None = None
    if not user_id and session_id:
        session = await load_session_state(session_id)
        if isinstance(session, dict):
            user_id = session.get("user_id")

    normalized_user = normalize_user_id(user_id or session_id)
    if not normalized_user:
        raise HTTPException(status_code=400, detail="user_id or session_id is required")

    try:
        balance = await supabase_client.get_wallet_balance(normalized_user)
    except Exception as exc:
        logger.error(f"Wallet balance fetch failed: {exc}")
        raise HTTPException(status_code=500, detail="Bakiye şu anda alınamıyor")

    if balance is None:
        return WalletBalanceResponse(success=False, balance=None, message="Bakiye bulunamadı")

    return WalletBalanceResponse(success=True, balance=int(balance))


@router.get("/wallet/history", response_model=WalletTransactionsResponse)
async def get_wallet_history(limit: int = 20, session_id: Optional[str] = None, user_id: Optional[str] = None):
    """Wallet transaction history endpoint (Sprint 2)."""
    session: Dict[str, Any] | None = None
    if not user_id and session_id:
        session = await load_session_state(session_id)
        if isinstance(session, dict):
            user_id = session.get("user_id")

    normalized_user = normalize_user_id(user_id or session_id)
    if not normalized_user:
        raise HTTPException(status_code=400, detail="user_id or session_id is required")

    capped_limit = max(1, min(limit, 50))
    try:
        txs = await supabase_client.get_wallet_transactions(normalized_user, limit=capped_limit)
    except Exception as exc:
        logger.error(f"Wallet history fetch failed: {exc}")
        raise HTTPException(status_code=500, detail="İşlem geçmişi şu anda alınamıyor")

    return WalletTransactionsResponse(success=True, transactions=txs or [])


class ConnectionManager:
    """WebSocket connection manager"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        logger.info(f"WebSocket connected: {session_id}")
    
    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]
            logger.info(f"WebSocket disconnected: {session_id}")
    
    async def send_message(self, session_id: str, message: dict):
        if session_id in self.active_connections:
            await self.active_connections[session_id].send_json(message)


manager = ConnectionManager()


async def process_webchat_message(
    message_body: str,
    session_id: str,
    user_id: Optional[str] = None,
    media_url: Optional[str] = None,
    media_urls: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Process webchat message and route to appropriate agent
    
    Args:
        message_body: Message text
        session_id: Session identifier
        user_id: User ID (optional)
        media_url: Optional single media URL (legacy)
        media_urls: Optional list of media URLs
    
    Returns:
        Response dict
    """
    async def _default_finalize(payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload

    finalize_response = _default_finalize

    try:
        # Support both single and multiple media URLs
        all_media_urls = media_urls or ([media_url] if media_url else [])
        incoming_media_urls = list(all_media_urls)
        redis_disabled = redis_is_disabled()
        session_dirty = False

        # Get or create session regardless of Redis availability
        session = await load_session_state(session_id)
        if session is None or not isinstance(session, dict):
            session = {
                "user_id": user_id,
                "intent": None,
                "locked_intent": None,
                "active_draft_id": None,
                "pending_media_urls": [],
                "pending_media_analysis": [],
                "search_context": {},
                "active_listing_context": None,
                "pending_listing_delete": None,
                "context_mode": None,
                "fsm_state": "active",
                "fsm_state_reason": None,
                "fsm_state_updated_at": _utc_now_iso(),
                "fsm_state_intent": None,
                "parked_intent": None,
                "last_user_at": _utc_now_iso(),
                "last_bot_at": None,
            }
            session_dirty = True
        else:
            # Make a shallow copy so we can mutate safely
            session = dict(session)
            if "pending_media_urls" not in session:
                session["pending_media_urls"] = []
                session_dirty = True
            if "pending_media_analysis" not in session:
                session["pending_media_analysis"] = []
                session_dirty = True
            if "search_context" not in session:
                session["search_context"] = {}
                session_dirty = True
            if "active_listing_context" not in session:
                session["active_listing_context"] = None
                session_dirty = True
            if "pending_listing_delete" not in session:
                session["pending_listing_delete"] = None
                session_dirty = True
            if "context_mode" not in session:
                session["context_mode"] = None
                session_dirty = True
            if "locked_intent" not in session:
                session["locked_intent"] = None
                session_dirty = True
            if "fsm_state" not in session:
                session["fsm_state"] = "active"
                session_dirty = True
            if "fsm_state_reason" not in session:
                session["fsm_state_reason"] = None
                session_dirty = True
            if "fsm_state_updated_at" not in session:
                session["fsm_state_updated_at"] = _utc_now_iso()
                session_dirty = True
            if "fsm_state_intent" not in session:
                session["fsm_state_intent"] = None
                session_dirty = True
            if "parked_intent" not in session:
                session["parked_intent"] = None
                session_dirty = True
            if "last_user_at" not in session:
                session["last_user_at"] = _utc_now_iso()
                session_dirty = True
            if "last_bot_at" not in session:
                session["last_bot_at"] = None
                session_dirty = True

        trace_id, trace_added = ensure_session_trace(session)
        if trace_added:
            session_dirty = True

        message_preview = (message_body or "").strip()
        log_fsm_event(
            "message_received",
            session_id,
            session,
            message_preview=message_preview[:160],
            has_media=bool(incoming_media_urls),
            media_count=len(incoming_media_urls),
            redis_disabled=redis_disabled,
        )

        async def _finalize_response(payload: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal session_dirty
            session["last_bot_at"] = _utc_now_iso()
            if not session_dirty:
                session_dirty = True
            if session_dirty:
                await persist_session_state(session_id, session)
            response_type = None
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                response_type = data.get("type")
            log_fsm_event(
                "response_ready",
                session_id,
                session,
                response_type=response_type,
                intent=payload.get("intent"),
            )
            return payload
        finalize_response = _finalize_response

        now_iso = _utc_now_iso()
        inactivity_seconds = _seconds_since(session.get("last_user_at"), now_iso)
        session["last_user_at"] = now_iso
        session_dirty = True

        bind_session_logger(
            session_id,
            session,
            inactivity_seconds=inactivity_seconds,
        ).debug(
            "Session heartbeat"
        )

        # Auto-park flows after prolonged silence to avoid stale prompts.
        locked_for_timeout = session.get("locked_intent")
        if (
            locked_for_timeout
            and inactivity_seconds is not None
            and inactivity_seconds > FSM_PARK_TIMEOUT_SECONDS
            and session.get("fsm_state") != "parked"
        ):
            session["parked_intent"] = locked_for_timeout
            session["locked_intent"] = None
            session["intent"] = None
            _set_fsm_state(session, "parked", intent=locked_for_timeout, reason="inactivity")
            session_dirty = True
            await _record_fsm_event(
                "parked",
                session_id,
                session,
                {"inactivity_seconds": inactivity_seconds, "parked_intent": locked_for_timeout},
            )
            return await finalize_response({
                "success": True,
                "message": "Bir süredir ses çıkmadı, beklemede kaldım 😴 Devam etmek için 'devam' yaz, ya da yeni bir şey söyle."
                "data": {"type": "parked"},
                "intent": None,
            })

        # If already parked/timeout, require explicit resume keyword to continue.
        if session.get("fsm_state") in {"parked", "timeout"}:
            logger.info(f"Session {session_id[:8]}... in {session.get('fsm_state')} state, awaiting resume/cancel")
            if is_resume_command(message_body):
                restored_intent = session.get("parked_intent") or session.get("fsm_state_intent") or session.get("intent") or session.get("locked_intent") or "create_listing"
                session["locked_intent"] = restored_intent
                session["intent"] = restored_intent
                session["parked_intent"] = None
                _set_fsm_state(session, "active", intent=restored_intent, reason="resume_command")
                session_dirty = True
                if not redis_disabled:
                    await redis_client.set_intent(session_id, restored_intent)
                await _record_fsm_event("resumed", session_id, session, {"restored_intent": restored_intent})
            elif is_cancel_command(message_body):
                session["locked_intent"] = None
                session["intent"] = None
                session["parked_intent"] = None
                _set_fsm_state(session, "active", intent=None, reason="cancel_from_parked")
                session_dirty = True
                await _record_fsm_event("parked_cancel", session_id, session, {})
                return await finalize_response({
                    "success": True,
                    "message": "Tamam, iptal ettim. Ne yapmak istersin? 😊"
                    "data": {"type": "parked_cancel"},
                    "intent": "small_talk",
                })
            else:
                return await finalize_response({
                    "success": True,
                    "message": "Beklemedeydin. 'Devam' yazabilir ya da 'iptal' diyerek baştan başlayabilirsin 🔄"
                    "data": {"type": session.get("fsm_state")},
                    "intent": None,
                })

        # IMPORTANT: frontend may omit user_id for some calls.
        # If we normalize None -> uuid4(), we get a different user per request,
        # causing drafts/images to appear "lost" and the flow to loop asking for photos.
        raw_user_id = session.get("user_id") or user_id or session_id
        normalized_user_id = normalize_user_id(raw_user_id)
        if session.get("user_id") != normalized_user_id:
            session["user_id"] = normalized_user_id
            session_dirty = True
        user_id = normalized_user_id

        # Deterministic acceptance of a previously suggested price.
        # This must NOT rely on in-memory session state because Railway may route
        # consecutive requests to different instances when Redis is disabled.
        if user_id and (is_confirm_command(message_body) or is_cancel_command(message_body)):
            try:
                latest = await supabase_client.get_latest_draft_for_user(user_id)
                listing = (latest or {}).get("listing_data") or {}
                if isinstance(listing, dict):
                    pending_suggested = listing.get("_pending_price_suggestion")
                else:
                    pending_suggested = None

                if latest and listing and listing.get("price") is None and pending_suggested is not None:
                    draft_id = latest.get("id")
                    if is_confirm_command(message_body):
                        suggested_int = int(float(pending_suggested))
                        ok = await supabase_client.update_draft_price(draft_id, float(suggested_int))
                        # Clear the pending marker regardless of update return value; then verify.
                        await supabase_client.clear_pending_price_suggestion(draft_id)
                        updated = await supabase_client.get_draft(draft_id)
                        updated_listing = (updated or {}).get("listing_data") or {}
                        if ok or (isinstance(updated_listing, dict) and updated_listing.get("price") is not None):
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or {}),
                                "data": {
                                    "intent": "create_listing",
                                    "draft_id": draft_id,
                                    "draft": updated,
                                    "type": "draft_update",
                                },
                                "intent": "create_listing",
                            })
                        return await finalize_response({
                            "success": True,
                            "message": "Fiyatı anlayamadım. Kaç liradan satacaksın? 💰"
                            "data": {"type": "slot_prompt", "slot": "price", "draft_id": draft_id},
                            "intent": "create_listing",
                        })

                    # Cancel: user rejected the suggestion
                    await supabase_client.clear_pending_price_suggestion(draft_id)
                    return await finalize_response({
                        "success": True,
                        "message": "Peki. Fiyatı siz yazar mısınız?",
                        "data": {"type": "slot_prompt", "slot": "price", "draft_id": draft_id},
                        "intent": "create_listing",
                    })
            except Exception:
                # Fall through to normal handling
                pass

        delete_listing_request: Optional[Dict[str, Any]] = None
        delete_listing_source: Optional[str] = None
        if is_delete_command(message_body):
            listing_candidate, listing_source = _resolve_listing_reference(session, message_body, session_id)
            if listing_candidate:
                delete_listing_request = {"listing": listing_candidate}
                delete_listing_source = listing_source or "context"

        # If user issues a publish/delete/create command, override any sticky intent.
        # This prevents getting stuck in a previous flow (e.g., search_listings) when the user explicitly
        # changes their mind and wants to sell or publish.
        wants_publish_delete_intent = is_publish_command(message_body) or (
            is_delete_command(message_body) and delete_listing_request is None
        )
        if wants_publish_delete_intent:
            prev_locked_pub = session.get("locked_intent")
            session["intent"] = "publish_or_delete"
            session["locked_intent"] = "publish_or_delete"
            intent = "publish_or_delete"
            intent_reason = "publish_delete_command"
            session_dirty = True
            if not redis_disabled:
                await redis_client.set_intent(session_id, "publish_or_delete")
            if prev_locked_pub != "publish_or_delete":
                await _record_fsm_event("intent_lock", session_id, session, {"new_intent": "publish_or_delete", "prev_locked": prev_locked_pub})

        if is_create_listing_command(message_body) and not (is_publish_command(message_body) or is_delete_command(message_body)):
            prev_locked = session.get("locked_intent")
            session["intent"] = "create_listing"
            session["locked_intent"] = "create_listing"
            intent = "create_listing"
            intent_reason = "create_command"
            session_dirty = True
            if not redis_disabled:
                await redis_client.set_intent(session_id, "create_listing")
            if prev_locked != "create_listing":
                await _record_fsm_event("intent_lock", session_id, session, {"new_intent": "create_listing", "prev_locked": prev_locked})
        
        # Store message in history
        if not redis_disabled:
            await redis_client.add_message(session_id, {
                "role": "user",
                "content": message_body,
                "timestamp": str(uuid.uuid1().time)
            })

        if delete_listing_request:
            listing = delete_listing_request.get("listing") if isinstance(delete_listing_request, dict) else None
            listing_id = str((listing or {}).get("id") or (listing or {}).get("listing_id") or "").strip()
            if not listing or not listing_id:
                log_fsm_event(
                    "delete_listing_missing_reference",
                    session_id,
                    session,
                    listing_source=delete_listing_source,
                )
                return await finalize_response({
                    "success": False,
                    "message": "Hangi ilanı demek istediğini anlayamadım. Önce arama yap, sonra '1 numaralı ilanı göster' diyebilirsin 🔎"
                    "data": {"type": "listing_action_needed"},
                    "intent": session.get("intent") or "search_listings",
                })

            if not _listing_belongs_to_user(listing, user_id):
                log_fsm_event(
                    "delete_listing_denied",
                    session_id,
                    session,
                    listing_id=listing_id,
                    listing_source=delete_listing_source,
                )
                return await finalize_response({
                    "success": False,
                    "message": "Bu ilan sana ait değil, silemem 🚫 Sadece kendi ilanlarını silebilirsin."
                    "data": {"type": "listing_action_denied", "listing_id": listing_id},
                    "intent": "search_listings",
                })

            _store_active_listing(session, listing, source=delete_listing_source or "context")
            price = listing.get("price")
            price_txt = f"{price} ₺" if price is not None else "fiyat belirtilmemiş"
            title = listing.get("title") or "Bu ilan"
            session["pending_listing_delete"] = {
                "listing_id": listing_id,
                "title": title,
                "price": price,
                "prompted_at": _utc_now_iso(),
            }
            session_dirty = True
            prompt = f"{title} ({price_txt}) ilanını silmek istediğine emin misin? (evet/hayır)"
            log_fsm_event(
                "delete_listing_confirm",
                session_id,
                session,
                listing_id=listing_id,
                listing_source=delete_listing_source,
            )
            return await finalize_response({
                "success": True,
                "message": prompt,
                "data": {
                    "type": "listing_delete_confirm",
                    "listing_id": listing_id,
                    "title": title,
                },
                "intent": "search_listings",
            })

        # Merge any newly provided media into session-level context
        pending_media_urls = session.get("pending_media_urls") or []
        if not isinstance(pending_media_urls, list):
            pending_media_urls = []
        if all_media_urls:
            merged = merge_unique_urls(pending_media_urls, all_media_urls)
            if merged != pending_media_urls:
                session["pending_media_urls"] = merged
                pending_media_urls = merged
                session_dirty = True
        all_media_urls = pending_media_urls
        has_media_context = bool(all_media_urls)

        # NON-STICKY RECOVERY (MEDIA):
        # If Redis is disabled / sessions are not sticky, a user may be in the middle of a
        # create_listing flow (draft already has user-provided fields), but the in-memory
        # session loses locked_intent. In that case, treat incoming media as "add photos" to
        # the latest in-progress draft instead of asking the image-first intent gate.
        if incoming_media_urls and not session.get("locked_intent") and user_id:
            try:
                if not is_create_listing_command(message_body) and not is_search_command(message_body) and not user_asks_market_price(message_body):
                    latest = await supabase_client.get_latest_draft_for_user(user_id)
                    if latest and draft_has_non_media_content(latest):
                        session["intent"] = "create_listing"
                        session["locked_intent"] = "create_listing"
                        session["active_draft_id"] = str(latest.get("id") or "")
                        session_dirty = True
                        if not redis_disabled:
                            await redis_client.set_intent(session_id, "create_listing")
            except Exception:
                pass

        # IMAGE ADD (POST-INTENT):
        # When locked in create_listing, any incoming images are treated as "add images" only.
        # Do NOT re-route intent, do NOT change title/description/category; only safety check + append + counter.
        if incoming_media_urls and session.get("locked_intent") == "create_listing":
            try:
                draft = None
                draft_id = session.get("active_draft_id")
                if isinstance(draft_id, str) and draft_id:
                    draft = await supabase_client.get_draft(draft_id)
                if not draft and user_id:
                    draft = await supabase_client.get_latest_draft_for_user(user_id)
                    draft_id = (draft or {}).get("id")
                if not draft and user_id:
                    draft = await supabase_client.create_draft(user_id=user_id, phone_number=session_id)
                    draft_id = (draft or {}).get("id")

                if not draft_id:
                    return await finalize_response({
                        "success": True,
                        "message": "Henüz başlattığın bir ilan yok. 'İlan oluştur' yazarak başlayabilirsin 🆕",
                        "data": {"type": "slot_prompt"},
                        "intent": "create_listing",
                    })

                # Analyze only new incoming URLs
                analyses = await analyze_media_with_vision(incoming_media_urls)
                analysis_by_url: Dict[str, Any] = {}
                for entry in analyses or []:
                    if isinstance(entry, dict) and entry.get("image_url"):
                        analysis_by_url[str(entry["image_url"])] = entry.get("analysis")

                updated_draft = await supabase_client.get_draft(draft_id)
                existing_images = (updated_draft or {}).get("images") or []
                current_count = len(existing_images) if isinstance(existing_images, list) else 0
                existing_urls: set[str] = set()
                if isinstance(existing_images, list):
                    for img in existing_images:
                        if isinstance(img, dict):
                            u = img.get("image_url")
                            if isinstance(u, str) and u:
                                existing_urls.add(u)

                added = 0
                blocked = 0
                ignored = 0
                duplicates = 0

                for url in incoming_media_urls:
                    if not url:
                        continue
                    if url in existing_urls:
                        duplicates += 1
                        continue
                    if current_count >= 5:
                        ignored += 1
                        continue
                    analysis = analysis_by_url.get(url)
                    safety_flags = []
                    if isinstance(analysis, dict):
                        sf = analysis.get("safety_flags")
                        if isinstance(sf, list):
                            safety_flags = [str(x) for x in sf if str(x).strip()]
                        elif isinstance(sf, str) and sf.strip():
                            safety_flags = [sf.strip()]

                    if safety_flags:
                        blocked += 1
                        continue

                    meta = {"analysis": analysis} if analysis is not None else None
                    ok = await supabase_client.add_listing_image(draft_id, url, metadata=meta)
                    if ok:
                        added += 1
                        current_count += 1
                        existing_urls.add(url)

                # Clear session pending media so we don't re-process in later turns
                session["pending_media_urls"] = []
                session["pending_media_analysis"] = []
                session_dirty = True

                msg_parts = []
                if added:
                    msg_parts.append(f"Resim eklendi. Şu an {current_count} / 5 resim eklediniz.")
                elif duplicates and not (blocked or ignored):
                    msg_parts.append("Bu görsel zaten ekli.")
                if blocked:
                    msg_parts.append("Bazı görsellerde uyarı tespit ettim; onları eklemedim.")
                if ignored:
                    msg_parts.append("Maksimum 5 resim eklenebilir; fazlasını eklemedim.")
                if not msg_parts:
                    msg_parts.append("Bu görselleri ilana ekleyemedim.")

                return await finalize_response({
                    "success": True,
                    "message": " ".join(msg_parts),
                    "data": {
                        "type": "image_added",
                        "draft_id": draft_id,
                        "added": added,
                        "blocked": blocked,
                        "ignored": ignored,
                        "image_count": current_count,
                        "max_images": 5,
                    },
                    "intent": "create_listing",
                })
            except Exception:
                # Fall through to normal handling
                pass

        # IMAGE-FIRST MANDATORY CHOICE:
        # If we previously received images without a locked intent, we must ask what the user
        # wants to do, and then branch based on that choice.
        if session.get("awaiting_media_action"):
            choice = classify_media_action_choice(message_body)
            # If we can't classify yet, re-ask (do not route intent).
            if not choice:
                return await finalize_response({
                    "success": True,
                    "message": format_media_analysis_message(session.get("pending_media_analysis") or []),
                    "data": {
                        "type": "media_action_required",
                        "media_urls": session.get("pending_media_urls") or [],
                        "media_analysis": session.get("pending_media_analysis") or [],
                    },
                    "intent": None,
                })

            # Clear the flag now; the next handler will set appropriate locked intent.
            session["awaiting_media_action"] = False
            session_dirty = True

            if choice == "create_listing":
                # Lock create_listing intent.
                session["intent"] = "create_listing"
                session["locked_intent"] = "create_listing"
                session_dirty = True

                # Ensure we have a draft and consume buffered media into draft.images.
                draft = None
                draft_id = session.get("active_draft_id")
                if isinstance(draft_id, str) and draft_id:
                    draft = await supabase_client.get_draft(draft_id)
                if not draft:
                    draft = await supabase_client.get_latest_draft_for_user(user_id)
                    draft_id = (draft or {}).get("id")
                if not draft:
                    draft = await supabase_client.create_draft(user_id=user_id, phone_number=session_id)
                    draft_id = (draft or {}).get("id")

                if draft_id:
                    session["active_draft_id"] = draft_id
                    session_dirty = True

                    # Attach images (max 5) with cached analysis metadata.
                    existing = await supabase_client.get_draft(draft_id)
                    existing_images = (existing or {}).get("images") or []
                    current_count = len(existing_images) if isinstance(existing_images, list) else 0

                    analyses = session.get("pending_media_analysis") or []
                    analysis_by_url: Dict[str, Any] = {}
                    if isinstance(analyses, list):
                        for entry in analyses:
                            if isinstance(entry, dict) and entry.get("image_url"):
                                analysis_by_url[str(entry["image_url"])] = entry.get("analysis")

                    for url in session.get("pending_media_urls") or []:
                        if not url:
                            continue
                        if current_count >= 5:
                            break
                        meta = {}
                        analysis = analysis_by_url.get(url)
                        if analysis is not None:
                            meta = {"analysis": analysis}
                        ok = await supabase_client.add_listing_image(draft_id, url, metadata=meta or None)
                        if ok:
                            current_count += 1

                    # Persist a single vision_product snapshot and auto-category suggestion (best-effort)
                    first_analysis = None
                    for entry in analyses or []:
                        a = (entry or {}).get("analysis") if isinstance(entry, dict) else None
                        if isinstance(a, dict) and a:
                            first_analysis = a
                            break
                    if isinstance(first_analysis, dict) and first_analysis:
                        try:
                            await supabase_client.update_draft_vision_product(draft_id, first_analysis)
                        except Exception:
                            pass
                        try:
                            cat_candidate = str(first_analysis.get("category") or "").strip()
                            normalized_cat = normalize_category_input(cat_candidate) if cat_candidate else None
                            if normalized_cat:
                                await supabase_client.update_draft_category(draft_id, normalized_cat)
                        except Exception:
                            pass

                    # Clear buffered media in both session and draft listing_data (best-effort).
                    try:
                        await supabase_client.clear_buffered_media(draft_id)
                    except Exception:
                        pass
                    session["pending_media_urls"] = []
                    session["pending_media_analysis"] = []
                    session_dirty = True

                intro_draft = None
                try:
                    intro_draft = await supabase_client.get_draft(draft_id) if draft_id else draft
                except Exception:
                    intro_draft = draft

                return await finalize_response({
                    "success": True,
                    "message": format_create_listing_intro_message(intro_draft),
                    "data": {"type": "create_listing_intro", "draft_id": draft_id},
                    "intent": "create_listing",
                })

            if choice == "search_listings":
                session["intent"] = "search_listings"
                session["locked_intent"] = "search_listings"
                session_dirty = True
                return await finalize_response({
                    "success": True,
                    "message": "Anladım. Benzer ilanları aramak için ne aradığınızı kısaca yazar mısınız?",
                    "data": {"type": "search_intro"},
                    "intent": "search_listings",
                })

            # price_research
            return await finalize_response({
                "success": True,
                "message": "Anladım. Fiyat araştırması yapmak için 'kaç para eder' yazabilir veya ürünle ilgili kısa bir bilgi ekleyebilirsiniz.",
                "data": {"type": "price_research_intro"},
                "intent": None,
            })

        # PRE-INTENT BUFFER RULE:
        # Images are data, not intent. If user only sent media (or media + neutral text)
        # and we have no locked intent yet, do not force create_listing.
        locked_intent = session.get("locked_intent")
        if has_media_context and not locked_intent:
            # If user already expressed an explicit intent in text, we'll continue below.
            explicit_create = is_create_listing_command(message_body)
            explicit_search = is_search_command(message_body)

            if not explicit_create and not explicit_search:
                # Ensure we have vision analysis cached for the buffered media (best-effort)
                cached_analyses = session.get("pending_media_analysis") or []
                if not isinstance(cached_analyses, list):
                    cached_analyses = []
                # Only analyze URLs we haven't analyzed yet
                analyzed_urls = {str(a.get("image_url")) for a in cached_analyses if isinstance(a, dict) and a.get("image_url")}
                new_urls = [u for u in all_media_urls if u and u not in analyzed_urls]
                if new_urls:
                    new_analyses = await analyze_media_with_vision(new_urls)
                    cached_analyses = cached_analyses + new_analyses
                    session["pending_media_analysis"] = cached_analyses
                    session_dirty = True

                # Store buffered media in DB (draft listing_data) for non-sticky sessions.
                # Do NOT write to draft.images until the user chooses an intent.
                if user_id and all_media_urls:
                    try:
                        draft = await supabase_client.get_latest_draft_for_user(user_id)
                        draft_id = (draft or {}).get("id")
                        if not draft:
                            draft = await supabase_client.create_draft(user_id=user_id, phone_number=session_id)
                            draft_id = (draft or {}).get("id")
                        if draft_id:
                            session["active_draft_id"] = draft_id
                            session_dirty = True
                            await supabase_client.set_buffered_media(draft_id, all_media_urls, session.get("pending_media_analysis") or [])
                    except Exception:
                        pass

                message_text = format_media_analysis_message(session.get("pending_media_analysis") or [])
                # Mandatory choice after image-first.
                session["awaiting_media_action"] = True
                session["vision_explained"] = True
                session_dirty = True
                return await finalize_response({
                    "success": True,
                    "message": message_text,
                    "data": {
                        "type": "media_action_required",
                        "media_urls": all_media_urls,
                        "media_analysis": session.get("pending_media_analysis") or []
                    },
                    "intent": None
                })

        # PRE-INTENT DRAFT SLOT RECOVERY:
        # With Redis disabled and requests potentially landing on different instances,
        # the intent router may misclassify short slot answers like "Otomotiv".
        # If the user has an in-progress draft missing category/price, accept those
        # answers deterministically before intent routing.
        if user_id:
            try:
                latest = await supabase_client.get_latest_draft_for_user(user_id)
                if latest and latest.get("id"):
                    missing = next_missing_slot(latest)
                    if missing == "category":
                        normalized = normalize_category_input(message_body)
                        if normalized:
                            draft_id = latest.get("id")
                            ok = await supabase_client.update_draft_category(draft_id, normalized)
                            updated = await supabase_client.get_draft(draft_id)
                            # Pin session to create_listing for subsequent turns
                            session["intent"] = "create_listing"
                            session["locked_intent"] = "create_listing"
                            session["active_draft_id"] = draft_id
                            session_dirty = True
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or latest),
                                "data": {
                                    "intent": "create_listing",
                                    "draft_id": draft_id,
                                    "draft": updated or latest,
                                    "type": "draft_update",
                                    "category": normalized,
                                    "applied": bool(ok),
                                },
                                "intent": "create_listing",
                            })

                    if missing == "price":
                        draft_id = latest.get("id")
                        # If user asked for market price, pin intent so the create_listing flow
                        # can handle the suggestion logic.
                        if user_asks_market_price(message_body):
                            session["intent"] = "create_listing"
                            session["locked_intent"] = "create_listing"
                            session["active_draft_id"] = draft_id
                            session_dirty = True
                        else:
                            price_val = parse_price_input(message_body)
                            if price_val is not None:
                                ok = await supabase_client.update_draft_price(draft_id, float(price_val))
                                updated = await supabase_client.get_draft(draft_id)
                                # Pin session to create_listing for subsequent turns
                                session["intent"] = "create_listing"
                                session["locked_intent"] = "create_listing"
                                session["active_draft_id"] = draft_id
                                session_dirty = True
                                return await finalize_response({
                                    "success": True,
                                    "message": build_next_step_message(updated or latest),
                                    "data": {
                                        "intent": "create_listing",
                                        "draft_id": draft_id,
                                        "draft": updated or latest,
                                        "type": "draft_update",
                                        "price": float(price_val),
                                        "applied": bool(ok),
                                    },
                                    "intent": "create_listing",
                                })

                    if missing == "location":
                        draft_id = latest.get("id")
                        loc = parse_location_input(message_body) or (message_body or "").strip()
                        if len(loc) >= 2:
                            ok = await supabase_client.update_draft_location(draft_id, loc)
                            updated = await supabase_client.get_draft(draft_id)
                            session["intent"] = "create_listing"
                            session["locked_intent"] = "create_listing"
                            session["active_draft_id"] = draft_id
                            session_dirty = True
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or latest),
                                "data": {
                                    "intent": "create_listing",
                                    "draft_id": draft_id,
                                    "draft": updated or latest,
                                    "type": "draft_update",
                                    "location": loc,
                                    "applied": bool(ok),
                                },
                                "intent": "create_listing",
                            })
            except Exception:
                pass

        # PURE GREETING OVERRIDE:
        # If the user only greets ("selam", "merhaba"...), do not advance task flows
        # (create_listing / publish_or_delete / search_listings). This avoids confusing
        # draft status prompts when the user is just saying hi.
        if (
            looks_like_greeting(message_body)
            and not is_publish_command(message_body)
            and not is_delete_command(message_body)
            and not is_create_listing_command(message_body)
            and not is_search_command(message_body)
        ):
            display_name = None
            try:
                display_name = await supabase_client.get_user_display_name(user_id)
            except Exception:
                display_name = None

            name_txt = f" {display_name}" if display_name else ""
            welcome = (
                f"Selam{name_txt}! PazarGlobal'e hoş geldin!\n\n"
                "🛒 Ürün satmak istersen: Satmak istediğin ürünün adını ve özelliklerini yazabilirsin.\n\n"
                "🔍 Ürün aramak istersen: Ne tür bir ürün arıyorsun?\n\n"
                "Bugün PazarGlobal'de ne yapmak istersin, ürün mü satacaksın yoksa bir şey mi arıyorsun?"
            )

            hint = ""
            if session.get("active_draft_id") or session.get("pending_media_urls") or session.get("pending_media_analysis"):
                hint = "\n\nİstersen ilan taslağına kaldığımız yerden devam edebiliriz. Ürünün adını (başlık) yazman yeterli."
            return await finalize_response({
                "success": True,
                "message": welcome + hint,
                "data": {"type": "conversation", "intent": "small_talk"},
                "intent": "small_talk",
            })

        # DRAFT STATUS OVERRIDE:
        # Allow users to ask for the draft status at any time without forcing a cancel or switching intents.
        if is_show_draft_command(message_body):
            try:
                draft = None
                draft_id = session.get("active_draft_id")
                if isinstance(draft_id, str) and draft_id:
                    draft = await supabase_client.get_draft(draft_id)
                if not draft and user_id:
                    draft = await supabase_client.get_latest_draft_for_user(user_id)
                if draft:
                    return await finalize_response({
                        "success": True,
                        "message": build_draft_status_message(draft, include_vision=True),
                        "data": {"type": "draft_status", "draft_id": draft.get("id"), "draft": draft},
                        "intent": session.get("intent") or "create_listing",
                    })
            except Exception:
                pass
            return await finalize_response({
                "success": False,
                "message": "Aktif bir taslak bulunamadı. Önce 'ilan oluştur' ile taslak başlatın.",
                "data": {"type": "draft_status"},
                "intent": session.get("intent") or "small_talk",
            })

        # HESITATION DETECTION (FSM loop preventer):
        # If user shows uncertainty/hesitation while in create_listing flow,
        # acknowledge it and exit gracefully to prevent repeated prompts.
        if is_hesitation_signal(message_body) and session.get("locked_intent") == "create_listing":
            # Clear the locked state to allow user to restart fresh
            session["locked_intent"] = None
            session["intent"] = "small_talk"
            session["parked_intent"] = None
            _set_fsm_state(session, "hesitation_exit", intent="create_listing", reason="user_hesitation")
            session_dirty = True
            if not redis_disabled:
                await redis_client.set_intent(session_id, "small_talk")

            await _record_fsm_event("hesitation_exit", session_id, session, {"message": message_body})
            
            return await finalize_response({
                "success": True,
                "message": "Tamam, acele yok. Karar verdiğinde söylersin, birlikte ilan oluştururuz. 😊",
                "data": {"type": "hesitation_exit"},
                "intent": "small_talk",
            })

        # WALLET BALANCE OVERRIDE:
        # Allow checking remaining credits at any time without getting stuck in a flow.
        if is_wallet_balance_command(message_body):
            try:
                balance_result = await get_wallet_balance_tool.execute(user_id=user_id)
                if balance_result.get("success"):
                    balance = (balance_result.get("data") or {}).get("balance")
                    if balance is not None:
                        msg = f"Kalan krediniz: {int(balance)} kredi."
                    else:
                        msg = "Kalan kredinizi şu an göremiyorum. Lütfen daha sonra tekrar deneyin."
                else:
                    msg = "Kalan kredinizi şu an göremiyorum. Lütfen daha sonra tekrar deneyin."
            except Exception:
                msg = "Kalan kredinizi şu an göremiyorum. Lütfen daha sonra tekrar deneyin."

            current_intent = session.get("intent") or session.get("locked_intent") or "small_talk"
            return await finalize_response({
                "success": True,
                "message": msg,
                "data": {"type": "wallet_balance"},
                "intent": current_intent,
            })

        # GLOBAL CANCEL OVERRIDE:
        # Users may say "satmaktan vazgeçtim" / "iptal" while in any flow.
        # Clear the locked intent so routing can start fresh. Do not interfere with
        # publish/delete deterministic flow, which already has its own cancel semantics.
        if is_cancel_command(message_body) and not user_refuses_images(message_body) and session.get("locked_intent") != "publish_or_delete":
            # Best-effort: reset the underlying draft in DB so old fields don't leak
            # into the next listing flow (single-draft-per-user model).
            try:
                draft_id = session.get("active_draft_id")
                if not draft_id and user_id:
                    latest = await supabase_client.get_latest_draft_for_user(user_id)
                    draft_id = (latest or {}).get("id")
                if isinstance(draft_id, str) and draft_id:
                    await supabase_client.clear_pending_publish_state(draft_id)
                    await supabase_client.reset_draft(draft_id, phone_number=session_id)
            except Exception:
                pass

            session.pop("locked_intent", None)
            session["intent"] = None
            session["active_draft_id"] = None
            session["pending_media_urls"] = []
            session["pending_media_analysis"] = []
            session_dirty = True
            return await finalize_response({
                "success": True,
                "message": "Tamam. Bu işlemi iptal ettim. İstersen ürün arayabilir ya da yeni bir ilan oluşturmaya başlayabilirsin.",
                "data": {"type": "conversation", "intent": "small_talk"},
                "intent": "small_talk",
            })

        # Get or determine intent
        intent = session.get("intent")
        locked_intent = session.get("locked_intent")
        intent_reason = "session_cache" if intent else None

        # If we somehow ended up in publish/delete without an explicit user request and without
        # a locked publish/delete flow, drop it so we can route normally.
        if intent == "publish_or_delete" and locked_intent != "publish_or_delete":
            if not (is_publish_command(message_body) or is_delete_command(message_body)):
                session["intent"] = None
                intent = None
                session_dirty = True

        # SOFT OVERRIDE: Interrupt signals and meta questions can break through locked intents
        # This MUST come before intent switch ergonomics to allow breaking out
        is_interrupt = _is_interrupt_signal(message_body)
        is_meta = _is_meta_question(message_body)
        
        if locked_intent and (is_interrupt or is_meta):
            # Interrupt detected - unlock intent and route to small_talk
            prev_locked = locked_intent
            session.pop("locked_intent", None)
            session["intent"] = "small_talk"
            intent = "small_talk"
            locked_intent = None
            intent_reason = "soft_override_interrupt" if is_interrupt else "soft_override_meta"
            session_dirty = True
            if not redis_disabled:
                await redis_client.set_intent(session_id, intent)
            await _record_fsm_event("intent_unlock", session_id, session, {
                "prev_locked": prev_locked,
                "trigger": "interrupt" if is_interrupt else "meta_question",
                "message_preview": message_body[:50]
            })
        
        # SEARCH EXIT: If locked in search but context is stale and no new search query, unlock
        if locked_intent == "search_listings" and not _looks_like_search_query(message_body):
            if _search_context_is_stale(session) and not _looks_like_listing_detail_request(message_body):
                # Search context expired, unlock
                session.pop("locked_intent", None)
                locked_intent = None
                session["intent"] = None
                intent = None
                session_dirty = True
                await _record_fsm_event("search_exit", session_id, session, {
                    "reason": "context_stale",
                    "message_preview": message_body[:50]
                })

        # INTENT SWITCH ERGONOMICS:
        # If the user is locked in create_listing but says a clear search command (e.g. "benzer ara"),
        # don't silently ignore it. Guide them to the explicit cancel keyword.
        # HOWEVER: if locked in publish_or_delete and user tries to search, allow override.
        if locked_intent == "create_listing" and is_search_command(message_body):
            return await finalize_response({
                "success": True,
                "message": (
                    "Şu an ilan oluşturma akışındasın. Arama moduna geçmek için önce 'iptal' (veya 'vazgeç') yaz. "
                    "Sonra 'benzer ara' ya da 'telefon ara' gibi arama isteğini yazabilirsin."
                ),
                "data": {
                    "type": "conversation",
                    "intent": "create_listing",
                    "hint": {"cancel": "iptal", "then": "benzer ara"},
                },
                "intent": "create_listing",
            })
        
        # SEARCH OVERRIDE: Allow search queries to override publish_or_delete lock
        # when no publish confirmation is pending.
        if locked_intent == "publish_or_delete" and _looks_like_search_query(message_body):
            pending_publish = session.get("pending_publish")
            if not pending_publish or not isinstance(pending_publish, dict):
                # No pending confirmation; allow search
                session.pop("locked_intent", None)
                session["intent"] = "search_listings"
                session["locked_intent"] = "search_listings"
                locked_intent = "search_listings"
                intent = "search_listings"
                intent_reason = "search_override"
                session_dirty = True
                if not redis_disabled:
                    await redis_client.set_intent(session_id, intent)
                await _record_fsm_event("intent_lock", session_id, session, {"new_intent": intent, "prev_locked": "publish_or_delete", "trigger": "search_query_override"})

        # Sticky intent: once locked_intent is set, do not re-run global routing.
        # Publish/delete can still temporarily override.
        if locked_intent and intent != "publish_or_delete":
            intent = locked_intent
            if intent_reason not in {"publish_delete_command", "create_command"}:
                intent_reason = "locked_intent"
            if session.get("intent") != intent:
                session["intent"] = intent
                session_dirty = True

        # If no locked intent, deterministic override for clear user commands.
        if not locked_intent and intent != "publish_or_delete":
            override_intent = None
            if is_create_listing_command(message_body):
                override_intent = "create_listing"
            elif is_search_command(message_body):
                override_intent = "search_listings"
            if override_intent and override_intent != intent:
                prev_locked = session.get("locked_intent")
                intent = override_intent
                session["intent"] = intent
                session["locked_intent"] = intent
                locked_intent = intent
                intent_reason = "command_override"
                session_dirty = True
                if not redis_disabled:
                    await redis_client.set_intent(session_id, intent)
                await _record_fsm_event("intent_lock", session_id, session, {"new_intent": override_intent, "prev_locked": prev_locked, "trigger": "command_override"})

        if not intent:
            router_agent = IntentRouterAgent()
            intent = sanitize_classified_intent(message_body, await router_agent.classify_intent(message_body))
            session["intent"] = intent
            intent_reason = "router"
            session_dirty = True
            if not redis_disabled:
                await redis_client.set_intent(session_id, intent)
            log_fsm_event(
                "intent_classified",
                session_id,
                session,
                classified_intent=intent,
            )
            logger.info(f"WebChat intent for {session_id}: {intent}")

            # Only lock "task" intents; keep small_talk unlocked.
            if intent in {"create_listing", "search_listings"}:
                prev_locked_router = session.get("locked_intent")
                session["locked_intent"] = intent
                locked_intent = intent
                session_dirty = True
                await _record_fsm_event("intent_lock", session_id, session, {"new_intent": intent, "prev_locked": prev_locked_router, "trigger": "router"})
        
        log_fsm_event(
            "intent_selected",
            session_id,
            session,
            selected_intent=intent,
            intent_source=intent_reason,
        )

        response_data = {"intent": intent}
        
        # Route to appropriate agent
        if intent == "create_listing":
            log_fsm_event(
                "flow_enter_create_listing",
                session_id,
                session,
                draft_id=session.get("active_draft_id"),
            )
            # If user asks for market price while we are missing price, answer deterministically.
            # Uses cached Perplexity pipeline on Supabase Edge (market_price_snapshots).
            draft_id = session.get("active_draft_id")

            # If we have pre-intent buffered media, consume it into the draft once intent is locked.
            # Important: do NOT re-run vision in process_image_tool; reuse cached analysis.
            if session.get("pending_media_urls") and not draft_id:
                # Create a draft first
                draft_created = await supabase_client.create_draft(user_id=user_id, phone_number=session_id)
                draft_id = (draft_created or {}).get("id")
                if draft_id:
                    session["active_draft_id"] = draft_id
                    session_dirty = True

            if session.get("pending_media_urls") and draft_id:
                analyses = session.get("pending_media_analysis") or []
                analysis_by_url: Dict[str, Any] = {}
                if isinstance(analyses, list):
                    for entry in analyses:
                        if isinstance(entry, dict) and entry.get("image_url"):
                            analysis_by_url[str(entry["image_url"])] = entry.get("analysis")

                # Attach images + metadata
                for url in session.get("pending_media_urls") or []:
                    if not url:
                        continue
                    meta = {}
                    if url in analysis_by_url:
                        meta = {"analysis": analysis_by_url[url]}
                    await supabase_client.add_listing_image(draft_id, url, metadata=meta)

                # Best-effort: store vision_product, but do NOT auto-write category from vision here.
                # Otherwise, the subsequent explicit create command (e.g. "ilan oluştur") can trigger
                # the "start new listing" reset heuristic (draft_has_non_media_content), wiping newly
                # uploaded photos and causing a photo-request loop.
                first_analysis = None
                if isinstance(analyses, list) and analyses:
                    first = analyses[0]
                    if isinstance(first, dict):
                        first_analysis = first.get("analysis")
                if isinstance(first_analysis, dict) and first_analysis:
                    try:
                        await supabase_client.update_draft_vision_product(draft_id, first_analysis)
                    except Exception:
                        pass

                # Clear pre-intent buffer after consumption
                session["pending_media_urls"] = []
                session["pending_media_analysis"] = []
                session_dirty = True

            draft_id = session.get("active_draft_id")
            existing_draft = await supabase_client.get_draft(draft_id) if draft_id else None

            # With Redis disabled (and Railway load-balancing), a new request may land on a different instance.
            # Recover the active draft deterministically from the DB.
            if not existing_draft and user_id:
                existing_draft = await supabase_client.get_latest_draft_for_user(user_id)
                if existing_draft and existing_draft.get("id"):
                    draft_id = existing_draft.get("id")
                    session["active_draft_id"] = draft_id
                    session_dirty = True
                    # DEBUG: log recovered draft state
                    logger.info(f"Recovered draft {draft_id} for user {user_id}: images={len(existing_draft.get('images') or [])}")

            # If the user explicitly starts a new listing, reset the single in-progress draft
            # to prevent reusing an old item's data (common with non-sticky sessions).
            if existing_draft and draft_id and should_reset_draft_for_new_listing(message_body, existing_draft):
                try:
                    ok = await supabase_client.reset_draft(draft_id, phone_number=session_id)
                    if ok:
                        existing_draft = await supabase_client.get_draft(draft_id)
                except Exception:
                    pass

            # If the user refuses to upload images, allow a no-photo listing.
            if existing_draft and draft_id and user_refuses_images(message_body):
                try:
                    await supabase_client.update_draft_allow_no_images(draft_id, True)
                    existing_draft = await supabase_client.get_draft(draft_id) or existing_draft
                except Exception:
                    pass
                response_data.update({
                    "draft_id": draft_id,
                    "draft": existing_draft,
                    "type": "draft_update",
                })
                return await finalize_response({
                    "success": True,
                    "message": "Tamam, resimsiz devam edelim. " + build_next_step_message(existing_draft),
                    "data": response_data,
                    "intent": intent,
                })

            # Deterministic slot filling: if the draft is missing exactly one next slot,
            # treat the user's next message as that slot input (avoid depending on sticky session state).
            if existing_draft and draft_id and is_cancel_command(message_body) and not user_refuses_images(message_body):
                # User wants to stop this flow.
                try:
                    session.pop("locked_intent", None)
                    session.pop("intent", None)
                    session.pop("pending_price_suggestion", None)
                    session_dirty = True
                except Exception:
                    pass
                return await finalize_response({
                    "success": True,
                    "message": "Tamam. İlan oluşturmayı iptal ettim. İstersen yeni bir ürün satabilir ya da ürün arayabilirsin.",
                    "data": {"type": "conversation", "intent": "small_talk"},
                    "intent": "small_talk",
                })

            if existing_draft and draft_id:
                # AUTO CATEGORY (NO PROMPT):
                # Avoid asking the user "Kategori nedir?" which causes hesitation.
                # If category is missing, infer it deterministically from vision/title/description and persist it.
                try:
                    listing_auto = (existing_draft or {}).get("listing_data") or {}
                    if isinstance(listing_auto, dict) and not str(listing_auto.get("category") or "").strip():
                        inferred = infer_category_from_draft(existing_draft)
                        category_to_set = inferred or "Diğer"
                        ok = await supabase_client.update_draft_category(draft_id, category_to_set)
                        if ok:
                            refreshed = await supabase_client.get_draft(draft_id)
                            if refreshed:
                                existing_draft = refreshed
                except Exception:
                    pass

                slot = next_missing_slot(existing_draft)

                # Dynamic fallback: user asks what to do next while we're waiting for slot content.
                # Do NOT persist this meta/help message into listing fields.
                if is_help_or_next_step_query(message_body):
                    data_type: str
                    data_payload: Dict[str, Any]
                    if slot:
                        data_type = "slot_prompt"
                        data_payload = {"type": data_type, "slot": slot, "draft_id": draft_id}
                    else:
                        data_type = "conversation"
                        data_payload = {"type": data_type, "intent": intent, "draft_id": draft_id}

                    return await finalize_response({
                        "success": True,
                        "message": build_next_step_message(existing_draft),
                        "data": data_payload,
                        "intent": intent,
                    })

                # If the draft is already ready (minimum set), allow inline edits and show a preview
                # without forcing the publish flow.
                if draft_ready_for_preview(existing_draft):
                    edit_request = extract_preview_edit(message_body)
                    if edit_request:
                        edit_result = await apply_preview_edit(draft_id, edit_request["field"], edit_request["value"])
                        if edit_result.get("success"):
                            updated_draft = edit_result.get("draft") or await supabase_client.get_draft(draft_id) or existing_draft
                            return await finalize_response({
                                "success": True,
                                "message": format_ready_preview_message(updated_draft),
                                "data": {"type": "draft_ready", "draft_id": draft_id, "preview": build_draft_preview_payload(updated_draft)},
                                "intent": intent,
                            })
                        return await finalize_response({
                            "success": False,
                            "message": edit_result.get("message") or "Değişiklik kaydedilemedi.",
                            "data": {"type": "draft_ready", "draft_id": draft_id},
                            "intent": intent,
                        })

                # Hybrid: try extracting multiple fields from one freeform message.
                if not is_command_only_message(message_body) and not looks_like_greeting(message_body):
                    extracted = extract_listing_fields_from_freeform(message_body)
                    if extracted:
                        listing_now = (existing_draft or {}).get("listing_data") or {}
                        if not isinstance(listing_now, dict):
                            listing_now = {}

                        try:
                            if extracted.get("price") is not None and listing_now.get("price") is None:
                                await supabase_client.update_draft_price(draft_id, float(extracted["price"]))
                        except Exception:
                            pass
                        try:
                            if extracted.get("location") and not str(listing_now.get("location") or "").strip():
                                await supabase_client.update_draft_location(draft_id, str(extracted["location"]))
                        except Exception:
                            pass
                        try:
                            if extracted.get("title") and not str(listing_now.get("title") or "").strip():
                                await supabase_client.update_draft_title(draft_id, str(extracted["title"]))
                        except Exception:
                            pass
                        try:
                            current_desc = str(listing_now.get("description") or "").strip()
                            if extracted.get("description") and (not current_desc or len(current_desc) < 20):
                                await supabase_client.update_draft_description(draft_id, str(extracted["description"]))
                        except Exception:
                            pass
                        try:
                            if extracted.get("condition") and hasattr(supabase_client, "update_draft_condition"):
                                if not str(listing_now.get("condition") or "").strip():
                                    await supabase_client.update_draft_condition(draft_id, str(extracted["condition"]))
                        except Exception:
                            pass

                        refreshed = await supabase_client.get_draft(draft_id)
                        if refreshed:
                            existing_draft = refreshed
                            slot = next_missing_slot(existing_draft)

                # If we have the minimum set, optionally enrich and show a "wow" preview.
                if draft_ready_for_preview(existing_draft):
                    force_enrich = is_improve_command(message_body)

                    listing_ready = (existing_draft or {}).get("listing_data") or {}
                    if not isinstance(listing_ready, dict):
                        listing_ready = {}

                    enrich_attempted = bool(listing_ready.get("_copy_enriched_attempted"))
                    enrich_done = bool(listing_ready.get("_copy_enriched"))

                    # Deterministic behavior:
                    # - Auto enrich only once (when draft first becomes preview-ready)
                    # - After that, only re-run when the user explicitly says "iyileştir"
                    should_enrich = force_enrich or (not enrich_attempted and not enrich_done)

                    if should_enrich and not force_enrich:
                        # Mark attempted before running to avoid repeated auto attempts.
                        try:
                            if hasattr(supabase_client, "set_draft_listing_data_flag"):
                                await supabase_client.set_draft_listing_data_flag(draft_id, "_copy_enriched_attempted", True)
                                refreshed = await supabase_client.get_draft(draft_id)
                                if refreshed:
                                    existing_draft = refreshed
                                    listing_ready = (existing_draft or {}).get("listing_data") or listing_ready
                        except Exception:
                            pass

                    if should_enrich:
                        enriched = await maybe_enrich_title_description(draft_id, existing_draft, message_body)
                        if enriched:
                            try:
                                if enriched.get("title"):
                                    await supabase_client.update_draft_title(draft_id, enriched["title"])
                                if enriched.get("description"):
                                    await supabase_client.update_draft_description(draft_id, enriched["description"])
                                if hasattr(supabase_client, "set_draft_listing_data_flag"):
                                    await supabase_client.set_draft_listing_data_flag(draft_id, "_copy_enriched", True)
                                updated = await supabase_client.get_draft(draft_id)
                                if updated:
                                    existing_draft = updated
                            except Exception:
                                pass

                    return await finalize_response({
                        "success": True,
                        "message": format_ready_preview_message(existing_draft),
                        "data": {"type": "draft_ready", "draft_id": draft_id, "preview": build_draft_preview_payload(existing_draft)},
                        "intent": intent,
                    })

                # Allow category auto-selection command even if the next missing slot is not
                # category (e.g. after adding `location` slot). This prevents the flow from
                # misclassifying the user's message as location/title/etc.
                listing_for_auto = (existing_draft or {}).get("listing_data") or {}
                if (
                    isinstance(listing_for_auto, dict)
                    and not str(listing_for_auto.get("category") or "").strip()
                    and user_requests_auto_category(message_body)
                ):
                    inferred = infer_category_from_draft(existing_draft)
                    if inferred:
                        ok = await supabase_client.update_draft_category(draft_id, inferred)
                        updated = await supabase_client.get_draft(draft_id)
                        if ok or updated:
                            response_data.update({
                                "draft_id": draft_id,
                                "draft": updated,
                                "type": "draft_update",
                            })
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or existing_draft),
                                "data": response_data,
                                "intent": intent,
                            })

                # Category
                if slot == "category":
                    # If the user delegates category selection, infer deterministically from the draft (vision/title/desc).
                    if user_requests_auto_category(message_body) or is_command_only_message(message_body):
                        inferred = infer_category_from_draft(existing_draft)
                        if inferred:
                            ok = await supabase_client.update_draft_category(draft_id, inferred)
                            updated = await supabase_client.get_draft(draft_id)
                            if ok or updated:
                                response_data.update({
                                    "draft_id": draft_id,
                                    "draft": updated,
                                    "type": "draft_update",
                                })
                                return await finalize_response({
                                    "success": True,
                                    "message": build_next_step_message(updated or existing_draft),
                                    "data": response_data,
                                    "intent": intent,
                                })
                        # Could not infer — keep prompting for category.
                        return await finalize_response({
                            "success": True,
                            "message": build_next_step_message(existing_draft),
                            "data": {"type": "slot_prompt", "slot": "category", "draft_id": draft_id},
                            "intent": intent,
                        })

                    normalized = normalize_category_input(message_body)
                    if normalized:
                        ok = await supabase_client.update_draft_category(draft_id, normalized)
                        updated = await supabase_client.get_draft(draft_id)
                        if ok or updated:
                            response_data.update({
                                "draft_id": draft_id,
                                "draft": updated,
                                "type": "draft_update",
                            })
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or existing_draft),
                                "data": response_data,
                                "intent": intent,
                            })

                # Title
                if slot == "title":
                    if is_command_only_message(message_body) or looks_like_image_action_command(message_body):
                        # Photo-first flow: if we have images + vision, auto-seed title/description
                        # and continue instead of saving the command as a title.
                        try:
                            listing = (existing_draft or {}).get("listing_data") or {}
                            images = (existing_draft or {}).get("images") or []
                            vision = _unwrap_vision_product((existing_draft or {}).get("vision_product"))
                            has_vision_signal = False
                            if isinstance(vision, dict):
                                if str(vision.get("product") or "").strip():
                                    has_vision_signal = True
                                if str(vision.get("category") or "").strip():
                                    has_vision_signal = True
                                if str(vision.get("condition") or "").strip():
                                    has_vision_signal = True
                                if isinstance(vision.get("features"), list) and vision.get("features"):
                                    has_vision_signal = True
                                if isinstance(vision.get("features"), str) and vision.get("features").strip():
                                    has_vision_signal = True

                            if images and has_vision_signal:
                                if not (str(listing.get("title") or "").strip()):
                                    seeded_title = generate_title_from_vision(vision)
                                    if seeded_title:
                                        await supabase_client.update_draft_title(draft_id, seeded_title)
                                if not (str(listing.get("description") or "").strip()):
                                    seeded_desc = generate_description_from_vision(vision)
                                    if seeded_desc:
                                        await supabase_client.update_draft_description(draft_id, seeded_desc)
                                updated = await supabase_client.get_draft(draft_id)
                                response_data.update({
                                    "draft_id": draft_id,
                                    "draft": updated,
                                    "type": "draft_update",
                                    "slot": next_missing_slot(updated or existing_draft),
                                })
                                return await finalize_response({
                                    "success": True,
                                    "message": build_next_step_message(updated or existing_draft),
                                    "data": response_data,
                                    "intent": intent,
                                })
                        except Exception:
                            pass

                        return await finalize_response({
                            "success": True,
                            "message": build_next_step_message(existing_draft),
                            "data": {"type": "slot_prompt", "slot": "title", "draft_id": draft_id},
                            "intent": intent,
                        })
                    if looks_like_greeting(message_body):
                        return await finalize_response({
                            "success": True,
                            "message": build_next_step_message(existing_draft),
                            "data": {"type": "slot_prompt", "slot": "title", "draft_id": draft_id},
                            "intent": intent,
                        })
                    trimmed_title = (message_body or "").strip()
                    if len(trimmed_title) >= 3:
                        if violates_listing_content_guard(trimmed_title):
                            logger.info("Title guard suppressed action command payload for draft %s", draft_id)
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(existing_draft),
                                "data": {"type": "slot_prompt", "slot": "title", "draft_id": draft_id},
                                "intent": intent,
                            })
                        ok = await supabase_client.update_draft_title(draft_id, trimmed_title)
                        updated = await supabase_client.get_draft(draft_id)
                        if ok or updated:
                            response_data.update({
                                "draft_id": draft_id,
                                "draft": updated,
                                "type": "draft_update",
                            })
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or existing_draft),
                                "data": response_data,
                                "intent": intent,
                            })

                # Description
                if slot == "description":
                    if is_command_only_message(message_body) or looks_like_image_action_command(message_body):
                        return await finalize_response({
                            "success": True,
                            "message": build_next_step_message(existing_draft),
                            "data": {"type": "slot_prompt", "slot": "description", "draft_id": draft_id},
                            "intent": intent,
                        })
                    if looks_like_greeting(message_body):
                        return await finalize_response({
                            "success": True,
                            "message": build_next_step_message(existing_draft),
                            "data": {"type": "slot_prompt", "slot": "description", "draft_id": draft_id},
                            "intent": intent,
                        })
                    trimmed_description = (message_body or "").strip()
                    if len(trimmed_description) >= 6:
                        if violates_listing_content_guard(trimmed_description):
                            logger.info("Description guard suppressed action command payload for draft %s", draft_id)
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(existing_draft),
                                "data": {"type": "slot_prompt", "slot": "description", "draft_id": draft_id},
                                "intent": intent,
                            })
                        ok = await supabase_client.update_draft_description(draft_id, trimmed_description)
                        updated = await supabase_client.get_draft(draft_id)
                        if ok or updated:
                            response_data.update({
                                "draft_id": draft_id,
                                "draft": updated,
                                "type": "draft_update",
                            })
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or existing_draft),
                                "data": response_data,
                                "intent": intent,
                            })

                # Price (only if user typed a numeric price)
                if slot == "price":
                    price_val = parse_price_input(message_body)
                    if price_val is not None:
                        ok = await supabase_client.update_draft_price(draft_id, float(price_val))
                        updated = await supabase_client.get_draft(draft_id)
                        if ok or updated:
                            response_data.update({
                                "draft_id": draft_id,
                                "draft": updated,
                                "type": "draft_update",
                            })
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or existing_draft),
                                "data": response_data,
                                "intent": intent,
                            })

                # Condition
                if slot == "condition":
                    parsed = parse_condition_input(message_body)
                    if parsed and hasattr(supabase_client, "update_draft_condition"):
                        ok = await supabase_client.update_draft_condition(draft_id, parsed)
                        updated = await supabase_client.get_draft(draft_id)
                        if ok or updated:
                            response_data.update({
                                "draft_id": draft_id,
                                "draft": updated,
                                "type": "draft_update",
                            })
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or existing_draft),
                                "data": response_data,
                                "intent": intent,
                            })

                    return await finalize_response({
                        "success": True,
                        "message": "Durumu anlayamadım. Lütfen 'Sıfır', '2. El' veya 'Az Kullanılmış' yazın.",
                        "data": {"type": "slot_prompt", "slot": "condition", "draft_id": draft_id},
                        "intent": intent,
                    })

                # Location
                if slot == "location":
                    loc = parse_location_input(message_body)
                    if loc:
                        ok = await supabase_client.update_draft_location(draft_id, loc)
                        updated = await supabase_client.get_draft(draft_id)
                        if ok or updated:
                            response_data.update({
                                "draft_id": draft_id,
                                "draft": updated,
                                "type": "draft_update",
                            })
                            return await finalize_response({
                                "success": True,
                                "message": build_next_step_message(updated or existing_draft),
                                "data": response_data,
                                "intent": intent,
                            })
                    return await finalize_response({
                        "success": True,
                        "message": build_next_step_message(existing_draft),
                        "data": {"type": "slot_prompt", "slot": "location", "draft_id": draft_id},
                        "intent": intent,
                    })

            # If we previously suggested a price, allow a natural confirmation response.
            pending_price = session.get("pending_price_suggestion")
            if (
                existing_draft
                and next_missing_slot(existing_draft) == "price"
                and isinstance(pending_price, dict)
                and pending_price.get("draft_id") == draft_id
            ):
                if is_confirm_command(message_body):
                    try:
                        suggested_price = pending_price.get("suggested_price")
                        if suggested_price is not None:
                            ok = await supabase_client.update_draft_price(draft_id, float(suggested_price))
                            session.pop("pending_price_suggestion", None)
                            session_dirty = True
                            if ok:
                                updated = await supabase_client.get_draft(draft_id)
                                response_data.update({
                                    "draft_id": draft_id,
                                    "draft": updated,
                                    "type": "draft_update",
                                })
                                return await finalize_response({
                                    "success": True,
                                    "message": build_next_step_message(updated or {}),
                                    "data": response_data,
                                    "intent": intent,
                                })
                    except Exception:
                        # Fall through to normal handling
                        pass
                elif is_cancel_command(message_body):
                    session.pop("pending_price_suggestion", None)
                    session_dirty = True
                    return await finalize_response({
                        "success": True,
                        "message": "Peki. Fiyatı siz yazar mısınız?",
                        "data": {"type": "slot_prompt", "slot": "price", "draft_id": draft_id},
                        "intent": intent,
                    })

            if existing_draft and next_missing_slot(existing_draft) == "price" and user_asks_market_price(message_body):
                listing = (existing_draft or {}).get("listing_data") or {}
                vision = (existing_draft or {}).get("vision_product") or {}

                title = (listing.get("title") or "").strip()
                description = (listing.get("description") or "").strip()
                category = (listing.get("category") or "").strip()
                user_condition = str(listing.get("condition") or "").strip()
                condition_for_price = canonicalize_condition(user_condition) or "2. El"

                # If we don't have a title yet, fall back to vision product/category
                if not title and isinstance(vision, dict):
                    title = str(vision.get("product") or vision.get("category") or "").strip()

                # If we don't have a useful category yet, best-effort map vision category to library.
                # This improves cache hit rate (product_key includes category) and Perplexity query relevance.
                category_for_price = category
                if not category_for_price and isinstance(vision, dict):
                    vision_cat = str(vision.get("category") or "").strip()
                    if vision_cat:
                        mapped = normalize_category_input(vision_cat)
                        if mapped:
                            category_for_price = mapped
                if category_for_price:
                    mapped = normalize_category_input(category_for_price)
                    if mapped:
                        category_for_price = mapped

                # If we don't have a category yet, let edge function handle defaulting.
                price_resp = await supabase_client.suggest_price_cached(
                    title=title or "Ürün",
                    category=category_for_price or "Diğer",
                    description=description or "",
                    condition=condition_for_price,
                    vision=vision if isinstance(vision, dict) else None,
                    user_claim=(message_body or "").strip(),
                )

                price_value = price_resp.get("price")
                if price_resp.get("success") and price_value is not None:
                    suggested = int(price_value)
                    cached = bool(price_resp.get("cached"))
                    confidence = price_resp.get("confidence")
                    cached_txt = "(önbellekten)" if cached else "(webden güncel)"
                    conf_txt = f" Güven: %{int(float(confidence) * 100)}." if confidence is not None else ""

                    session["pending_price_suggestion"] = {
                        "draft_id": draft_id,
                        "suggested_price": suggested,
                    }
                    session_dirty = True

                    # Persist suggestion into the draft so confirm/cancel works without session stickiness.
                    try:
                        await supabase_client.set_pending_price_suggestion(draft_id, suggested)
                    except Exception:
                        pass

                    return await finalize_response({
                        "success": True,
                        "message": (
                            f"Önerilen satış fiyatı: {suggested} ₺ {cached_txt}.{conf_txt} "
                            "Fiyatı bu şekilde yazayım mı? (evet/hayır ya da kendi fiyatınızı yazın)"
                        ),
                        "data": {
                            "type": "price_suggestion",
                            "suggested_price": suggested,
                            "draft_id": draft_id,
                            "cached": cached,
                            "confidence": confidence,
                            "details": price_resp.get("result"),
                        },
                        "intent": intent
                    })

                # If edge function fails, fall back to direct ask
                return await finalize_response({
                    "success": True,
                    "message": "Şu an piyasa verisine erişemedim. Fiyatı siz yazar mısınız?",
                    "data": {"type": "slot_prompt", "slot": "price"},
                    "intent": intent
                })

            composer = ComposerAgent()

            # Reduce unnecessary LLM load: don't run composer on pure greetings.
            run_composer = True
            composer_skip_reason = None
            if looks_like_greeting(message_body):
                run_composer = False
                composer_skip_reason = "greeting"

            # Also don't run composer on pure flow commands like "ilan oluştur" when we already
            # have media in the draft; otherwise title/description agents may hallucinate from
            # an empty/command-only message.
            if run_composer and is_command_only_message(message_body):
                active_draft_id = session.get("active_draft_id")
                if not existing_draft and isinstance(active_draft_id, str) and active_draft_id:
                    existing_draft = await supabase_client.get_draft(active_draft_id)
                if existing_draft and (existing_draft.get("images") or []):
                    run_composer = False
                    composer_skip_reason = "command_only_with_media"

            # Pass no media URLs here because we already consumed pre-intent buffer into the draft.
            # If you later want to support post-lock image uploads in this endpoint, they will still
            # come through as media_urls and can be attached before calling composer.
            result = None
            if run_composer:
                active_draft_id = session.get("active_draft_id")
                composer_draft_id = active_draft_id if isinstance(active_draft_id, str) and active_draft_id else None
                try:
                    log_fsm_event(
                        "agent_invocation",
                        session_id,
                        session,
                        agent="ComposerAgent",
                        draft_id=composer_draft_id,
                    )
                    result = await asyncio.wait_for(
                        composer.orchestrate_listing_creation(
                            user_message=message_body,
                            user_id=user_id,
                            phone_number=session_id,  # Use session_id as identifier
                            draft_id=composer_draft_id,
                            media_urls=[],
                        ),
                        timeout=FSM_COMPOSER_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"ComposerAgent timeout after {FSM_COMPOSER_TIMEOUT_SECONDS}s for session {session_id[:8]}...")
                    session["parked_intent"] = intent
                    session["locked_intent"] = None
                    session["intent"] = None
                    _set_fsm_state(session, "timeout", intent=intent, reason="composer_timeout")
                    session_dirty = True
                    await _record_fsm_event("timeout", session_id, session, {"stage": "composer"})
                    return await finalize_response({
                        "success": False,
                        "message": "Şu an yanıt veremedim, akışı beklemeye aldım. Devam etmek için 'devam' yazabilir ya da yeniden başlatabilirsin.",
                        "data": {"type": "timeout"},
                        "intent": None,
                    })
            else:
                log_fsm_event(
                    "agent_skipped",
                    session_id,
                    session,
                    agent="ComposerAgent",
                    reason=composer_skip_reason or "noop",
                )

            if run_composer:
                log_fsm_event(
                    "agent_result",
                    session_id,
                    session,
                    agent="ComposerAgent",
                    success=bool(isinstance(result, dict) and result.get("success")),
                )

            # If we skipped composer (or composer failed), just read current draft
            if not result:
                draft_id = session.get("active_draft_id")
                draft = await supabase_client.get_draft(draft_id) if draft_id else None
                if not draft:
                    return await finalize_response({
                        "success": True,
                        "message": "İlan taslağı için bir şeyler yazın veya fotoğraf yükleyin.",
                        "data": {"type": "slot_prompt"},
                        "intent": intent
                    })

                prompt = build_next_step_message(draft)
                slot = next_missing_slot(draft)
                return await finalize_response({
                    "success": True,
                    "message": prompt,
                    "data": {"type": "slot_prompt", "slot": slot, "draft_id": draft_id},
                    "intent": intent
                })
            # Guard against unexpected None/invalid result
            if not result or not isinstance(result, dict):
                return await finalize_response({
                    "success": False,
                    "message": "Internal error: listing creation failed",
                    "data": None,
                    "intent": intent
                })

            if result.get("success"):
                if session.get("active_draft_id") != result["draft_id"]:
                    session["active_draft_id"] = result["draft_id"]
                    session_dirty = True
                if not redis_disabled:
                    await redis_client.set_active_draft(session_id, result["draft_id"])
                if session.get("pending_media_urls"):
                    session["pending_media_urls"] = []
                    session_dirty = True
                
                draft = result["draft"]

                # Step-by-step UX: ask only the next missing slot.
                # (Full summary is still available via build_draft_status_message if needed.)
                slot = next_missing_slot(draft)
                if slot is None:
                    response_text = build_draft_status_message(draft, include_vision=not bool(session.get("vision_explained")))
                else:
                    response_text = build_next_step_message(draft)
                
                response_data.update({
                    "draft_id": result["draft_id"],
                    "draft": draft,
                    "type": "draft_update"
                })
                
                return await finalize_response({
                    "success": True,
                    "message": response_text,
                    "data": response_data,
                    "intent": intent
                })
            else:
                return await finalize_response({
                    "success": False,
                    "message": (result.get("error") if isinstance(result, dict) else "Failed to create listing"),
                    "data": None,
                    "intent": intent
                })
        
        elif intent == "publish_or_delete":
            # Deterministic publish/delete flow to avoid looping confirmations and hallucinated fees.
            log_fsm_event(
                "agent_invocation",
                session_id,
                session,
                agent="PublishDeleteFlow",
            )
            publish_payload = await handle_publish_or_delete_flow(
                message_body=message_body,
                session_id=session_id,
                session=session,
                user_id=user_id,
                redis_disabled=redis_disabled,
                session_dirty=session_dirty
            )

            log_fsm_event(
                "agent_result",
                session_id,
                session,
                agent="PublishDeleteFlow",
                success=bool(publish_payload.get("success")),
            )

            # propagate session_dirty back to outer finalize
            if publish_payload.pop("_session_dirty", False):
                session_dirty = True

            response_data["type"] = "publish_delete"
            if isinstance(publish_payload.get("data"), dict):
                response_data.update(publish_payload["data"])
            return await finalize_response({
                "success": publish_payload.get("success", False),
                "message": publish_payload.get("message", ""),
                "data": response_data,
                "intent": publish_payload.get("intent")
            })
        
        elif intent == "search_listings":
            log_fsm_event(
                "flow_enter_search",
                session_id,
                session,
                search_context_size=len(_get_search_context_results(session)),
            )
            # If we have pre-intent buffered media analysis, enrich the search query with it.
            if session.get("pending_media_urls") and session.get("pending_media_analysis"):
                vision_query = extract_vision_search_query(session.get("pending_media_analysis") or [])
                if vision_query:
                    message_body = (message_body + " " + vision_query).strip()
                session["pending_media_urls"] = []
                session["pending_media_analysis"] = []
                session_dirty = True

            # Handle simple "ilan listele" style requests deterministically.
            if is_browse_all_command(message_body):
                listings = await supabase_client.search_listings(limit=5)
                LAST_SEARCH_CACHE[session_id] = listings
                if not listings:
                    return await finalize_response({
                        "success": True,
                        "message": "Şu anda listelenecek aktif ilan bulunamadı.",
                        "data": {"type": "search_results", "listings": [], "count": 0},
                        "intent": intent
                    })

                msg_lines = [f"🔍 Son {len(listings)} ilan:"]
                for idx, listing in enumerate(listings, 1):
                    title = listing.get("title") or "Başlıksız"
                    price = listing.get("price")
                    price_txt = f"{price} ₺" if price is not None else "Fiyat belirtilmemiş"
                    category = listing.get("category") or "Kategori yok"
                    msg_lines.append(f"{idx}. {title} - {price_txt} - {category}")

                msg_lines.append("Detay için: '1 nolu ilanın detayını göster' yazabilirsiniz.")
                return await finalize_response({
                    "success": True,
                    "message": "\n".join(msg_lines),
                    "data": {"type": "search_results", "listings": listings, "count": len(listings)},
                    "intent": intent
                })

            # If user asks to show previous search results, reuse cache
            lower_msg = message_body.lower()
            if any(k in lower_msg for k in ["göster", "detay", "ilanı", "ilanin"]) and LAST_SEARCH_CACHE.get(session_id):
                listings = LAST_SEARCH_CACHE.get(session_id, [])
                idx_match = re.search(r"(\d+)", lower_msg)
                idx = int(idx_match.group(1)) - 1 if idx_match else 0
                if 0 <= idx < len(listings):
                    listing = listings[idx]
                    title = listing.get("title") or "Başlıksız"
                    price = listing.get("price")
                    price_txt = f"{price} ₺" if price is not None else "Fiyat belirtilmemiş"
                    category = listing.get("category") or "Kategori yok"
                    location = listing.get("location") or listing.get("user_location") or "Konum belirtilmemiş"
                    description = listing.get("description") or "Açıklama yok"
                    # Trim uzun açıklama
                    if len(description) > 600:
                        description = description[:600] + "..."
                    owner = listing.get("user_name") or "Satıcı bilgisi yok"
                    phone = listing.get("user_phone") or "Telefon yok"
                    # Görsel seçimi
                    image_url = listing.get("image_url")
                    extra_images = []
                    if not image_url and listing.get("images") and isinstance(listing["images"], list):
                        first_img = listing["images"][0]
                        if isinstance(first_img, dict):
                            image_url = first_img.get("image_url") or first_img.get("public_url")
                        elif isinstance(first_img, str):
                            image_url = first_img
                        extra_images = []
                        for img in listing["images"][1:]:
                            if isinstance(img, dict):
                                url = img.get("image_url") or img.get("public_url")
                            elif isinstance(img, str):
                                url = img
                            else:
                                url = None
                            if url:
                                extra_images.append(url)
                    detail_msg = f"![{title}]({image_url})\n" if image_url else ""
                    detail_msg += f"**{title}**\n{price_txt} | {location} | {category}\nSatıcı: {owner} | Telefon: {phone}\n\nAçıklama:\n{description}"
                    if extra_images:
                        links = "\n".join([f"[Foto {i+2}]({url})" for i, url in enumerate(extra_images) if url])
                        if links:
                            detail_msg += f"\n\nEk görseller:\n{links}"
                    return await finalize_response({
                        "success": True,
                        "message": detail_msg,
                        "data": {"listing": listing, "type": "search_results"},
                        "intent": intent
                    })
                else:
                    return await finalize_response({
                        "success": False,
                        "message": "Önce bir arama yapın ya da geçerli bir ilan numarası belirtin (örn: '1 nolu ilanın detayını göster').",
                        "data": None,
                        "intent": intent
                    })

            composer = SearchComposerAgent()
            log_fsm_event(
                "agent_invocation",
                session_id,
                session,
                agent="SearchComposerAgent",
            )
            result = await composer.orchestrate_search(message_body)

            log_fsm_event(
                "agent_result",
                session_id,
                session,
                agent="SearchComposerAgent",
                success=bool(result and result.get("success")),
            )

            if not result or not isinstance(result, dict):
                return await finalize_response({
                    "success": False,
                    "message": "Internal error: search failed",
                    "data": None,
                    "intent": intent
                })

            response_data.update({
                "listings": result.get("listings", []),
                "count": result.get("count", 0),
                "type": "search_results"
            })

            # Cache full results for follow-up detail requests
            if result.get("listings_full") is not None:
                LAST_SEARCH_CACHE[session_id] = result["listings_full"]

            return await finalize_response({
                "success": result.get("success", False),
                "message": result.get("message", "Search completed"),
                "data": response_data,
                "intent": intent
            })
        
        else:  # small_talk
            log_fsm_event(
                "agent_invocation",
                session_id,
                session,
                agent="SmallTalkAgent",
            )
            agent = SmallTalkAgent()
            response = await agent.run_simple(message_body)
            log_fsm_event(
                "agent_result",
                session_id,
                session,
                agent="SmallTalkAgent",
                success=bool(response),
            )

            response_data["type"] = "conversation"
            return await finalize_response({
                "success": True,
                "message": response or "",
                "data": response_data,
                "intent": intent
            })
    
    except Exception as e:
        logger.error(f"WebChat message processing error: {e}")
        return await finalize_response({
            "success": False,
            "message": "An error occurred. Please try again.",
            "data": None,
            "intent": None
        })


@router.post("/message", response_model=ChatResponse)
async def send_message(chat_message: ChatMessage):
    """
    Send a chat message (REST endpoint)
    
    Used for simple request-response interactions
    """
    result = await process_webchat_message(
        message_body=chat_message.message,
        session_id=chat_message.session_id,
        user_id=chat_message.user_id,
        media_url=chat_message.media_url,
        media_urls=chat_message.media_urls
    )
    
    # Store response in history
    await redis_client.add_message(chat_message.session_id, {
        "role": "assistant",
        "content": result["message"],
        "timestamp": str(uuid.uuid1().time)
    })
    
    return ChatResponse(**result)


@router.post("/media/analyze", response_model=ChatResponse)
async def analyze_media(chat_message: MediaAnalysisRequest):
    """Run vision analysis on uploaded media and prompt user for next action."""
    if not chat_message.media_urls:
        raise HTTPException(status_code=400, detail="media_urls is required")

    # 🔴 STEP 0: PRE-ROUTING VISION SAFETY CHECK (Sprint 1)
    # Block unsafe content BEFORE it reaches FSM/Router/Vision Analysis
    safety_check = await vision_safety_gate.check_media(chat_message.media_urls)
    if not safety_check.get("safe", True):
        block_reason = safety_check.get("block_reason", "İçerik güvenlik politikalarımıza uygun değil.")
        logger.warning(
            f"Vision safety gate blocked media upload. Session: {chat_message.session_id}, "
            f"Categories: {safety_check.get('flagged_categories', [])}"
        )
        return ChatResponse(
            success=False,
            message=block_reason,
            data={
                "type": "safety_blocked",
                "flagged_categories": safety_check.get("flagged_categories", [])
            },
            intent="blocked"
        )

    session = await load_session_state(chat_message.session_id)
    if session is None or not isinstance(session, dict):
        session = {
            "user_id": chat_message.user_id,
            "intent": None,
            "active_draft_id": None,
            "pending_media_urls": []
        }
    else:
        session = dict(session)
        if "pending_media_urls" not in session:
            session["pending_media_urls"] = []

    ensure_session_trace(session)
    log_fsm_event(
        "media_analyze_request",
        chat_message.session_id,
        session,
        media_count=len(chat_message.media_urls),
    )

    # Keep user identity stable even if the frontend omits user_id.
    # Falling back to session_id prevents creating a new anonymous UUID per request.
    raw_user_id = session.get("user_id") or chat_message.user_id or chat_message.session_id
    normalized_user_id = normalize_user_id(raw_user_id)
    session["user_id"] = normalized_user_id

    merged_urls = merge_unique_urls(session.get("pending_media_urls") or [], chat_message.media_urls)
    session["pending_media_urls"] = merged_urls

    # Mark this session as starting a fresh listing draft if user proceeds to "ilan oluştur".
    # This prevents older draft fields (like a cached price) from leaking into a new flow.
    if not session.get("active_draft_id"):
        session["start_fresh_draft"] = True

    analyses = await analyze_media_with_vision(chat_message.media_urls)
    session["pending_media_analysis"] = analyses
    message_text = format_media_analysis_message(analyses)
    # This message is the one-time user-facing vision explanation for this upload.
    session["vision_explained"] = True

    # IMPORTANT (non-sticky sessions): persist uploaded media into the active draft.
    # The frontend uses /media/analyze, and the follow-up "ilan oluştur" message may
    # land on a different instance where in-memory session state is missing.
    if normalized_user_id and merged_urls:
        try:
            draft = None
            draft_id = session.get("active_draft_id")
            if isinstance(draft_id, str) and draft_id:
                draft = await supabase_client.get_draft(draft_id)

            if not draft:
                draft = await supabase_client.get_latest_draft_for_user(normalized_user_id)
                draft_id = (draft or {}).get("id")

            # If we're starting fresh and the existing draft has non-media fields, reset it
            # (avoid leaking old title/price/category into the new photo-first flow).
            if draft and draft_id and session.get("start_fresh_draft") and draft_has_non_media_content(draft):
                ok = await supabase_client.reset_draft(draft_id, phone_number=chat_message.session_id)
                if ok:
                    draft = await supabase_client.get_draft(draft_id)

            if not draft:
                draft = await supabase_client.create_draft(user_id=normalized_user_id, phone_number=chat_message.session_id)
                draft_id = (draft or {}).get("id")

            if draft_id:
                session["active_draft_id"] = draft_id
                session.pop("start_fresh_draft", None)

                analysis_by_url: Dict[str, Any] = {}
                for entry in analyses or []:
                    if isinstance(entry, dict) and entry.get("image_url"):
                        analysis_by_url[str(entry["image_url"])] = entry.get("analysis")

                for url in merged_urls:
                    if not url:
                        continue
                    analysis = analysis_by_url.get(url)
                    meta = {"analysis": analysis} if isinstance(analysis, dict) and analysis else None
                    await supabase_client.add_listing_image(draft_id, url, metadata=meta)

                # Best-effort: store the first analysis as draft.vision_product
                first_analysis = None
                for entry in analyses or []:
                    a = (entry or {}).get("analysis") if isinstance(entry, dict) else None
                    if isinstance(a, dict) and a:
                        first_analysis = a
                        break
                if isinstance(first_analysis, dict) and first_analysis:
                    await supabase_client.update_draft_vision_product(draft_id, first_analysis)
        except Exception:
            # Never fail the media analysis response because of draft persistence
            pass

    await persist_session_state(chat_message.session_id, session)

    if not redis_is_disabled():
        await redis_client.add_message(chat_message.session_id, {
            "role": "assistant",
            "content": message_text,
            "timestamp": str(uuid.uuid1().time)
        })

    return ChatResponse(
        success=True,
        message=message_text,
        data={
            "type": "media_analysis",
            "analyses": analyses,
            "pending_media_urls": merged_urls
        },
        intent=session.get("intent")
    )


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for real-time chat
    
    Provides real-time bidirectional communication
    """
    await manager.connect(websocket, session_id)
    
    try:
        # Send connection confirmation
        await manager.send_message(session_id, {
            "type": "connection",
            "message": "Connected to PazarGlobal AI Assistant",
            "session_id": session_id
        })
        
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            
            message = data.get("message")
            user_id = data.get("user_id")
            
            if not message:
                continue
            
            # Process message
            result = await process_webchat_message(
                message_body=message,
                session_id=session_id,
                user_id=user_id
            )
            
            # Store response
            await redis_client.add_message(session_id, {
                "role": "assistant",
                "content": result["message"],
                "timestamp": str(uuid.uuid1().time)
            })
            
            # Send response
            await manager.send_message(session_id, {
                "type": "message",
                **result
            })
    
    except WebSocketDisconnect:
        manager.disconnect(session_id)
        logger.info(f"Client disconnected: {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(session_id)


@router.get("/session/{session_id}")
async def get_session(session_id: str):
    """Get session information"""
    session = await load_session_state(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return {
        "session_id": session_id,
        "session": session
    }


@router.get("/history/{session_id}")
async def get_history(session_id: str, limit: int = 20):
    """Get chat history for session"""
    messages = await redis_client.get_messages(session_id, limit)
    return {
        "session_id": session_id,
        "messages": messages,
        "count": len(messages)
    }


@router.post("/session/new")
async def create_session(user_id: Optional[str] = None):
    """Create a new chat session"""
    session_id = f"web_{uuid.uuid4()}"
    
    await persist_session_state(session_id, {
        "user_id": user_id or str(uuid.uuid4()),
        "intent": None,
        "active_draft_id": None,
        "pending_media_urls": []
    })
    
    return {
        "session_id": session_id,
        "message": "Session created successfully"
    }


@router.delete("/session/{session_id}")
async def delete_session(session_id: str):
    """Delete a chat session"""
    existing = await load_session_state(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found")
    remove_session_state(session_id)
    if not redis_is_disabled():
        await redis_client.delete_session(session_id)
    return {"message": "Session deleted successfully"}
