"""Search context + listing follow-up helpers for WebChat.

Goal: keep `webchat.py` smaller by extracting pure helper logic around:
- storing/retrieving recent search results
- resolving "bu ilan / 2 nolu ilan" references
- formatting listing detail responses
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from api.webchat_store import LAST_SEARCH_CACHE
from services.text_normalization import normalize_for_match


SEARCH_CONTEXT_RESULT_LIMIT = 6

# NOTE: This is a chat/session context whitelist, not the canonical `listings` table schema.
# It may include derived/legacy keys (e.g. `listing_id`, `contact_phone`, `user_location`) that
# can appear in RPC/join payloads and help follow-up questions work reliably.
SEARCH_CONTEXT_KEEP_FIELDS = {
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trim_listing_for_context(listing: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(listing, dict):
        return {}
    trimmed: Dict[str, Any] = {}
    for key in SEARCH_CONTEXT_KEEP_FIELDS:
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
    match = re.search(r"(\d{1,3})\s*(?:nolu|no'lu|no|numarali|numaral[ıi]|\.?)\s*(?:ilan|liste|sirasi|sira)?", msg)
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
        "var mi",
        "var mı",
        "varmi",
        "varmı",
        "mevcut mu",
        "bulunur mu",
        "var misin",
        "ara",
        "arama",
        "bul",
        "ariyorum",
        "arıyorum",
        "ihtiyacim var",
        "ihtiyacım var",
        "lazim",
        "lazım",
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
    """Detect interrupt signals: 'bir şey sorabilir miyim', 'dur bi', 'merak ettim'."""

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

    stored_at = ctx.get("stored_at")
    if not stored_at:
        return True

    try:
        stored_time = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return bool(now - stored_time > timedelta(minutes=5))
    except Exception:
        return True


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
