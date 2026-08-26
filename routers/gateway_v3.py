"""
PazarGlobal Agent V3 - Gateway + FSM Engine

Tek giriş noktası: POST /api/v3/message

Mimari:
- LLM Brain: Serbest konuşma, intent belirleme, JSON üretme
- FSM Engine: JSON validasyon, keywords üretme, wallet, publish

Flow:
1. Message → Brain (LLM)
2. Brain → JSON + Intent
3. Intent CANCEL → Reset (LLM override)
4. Intent SEARCH → SearchComposerAgent
5. Intent CREATE → FSM Engine
   - FSM validates JSON
   - Missing fields? → Brain'e geri döndür
   - Ready + Confirmed? → Publish
"""
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
import re
import uuid
from datetime import datetime

from config.settings import settings
from services.jwt_auth import get_user_id_from_request
from core.brain import brain, BrainOutput, Intent
from services.redis_client import redis_client
from services.supabase_client import supabase_client
from services.example_listings import prefix_example_listing_title
from agents.vision_safety_gate import vision_safety_gate
from services.vision_service import vision_service
from services.text_normalization import canonicalize_condition, normalize_for_match
from services.logger import get_logger

logger = get_logger(__name__)


def _safe_exception_text(exc: Exception) -> str:
    """Return exception text safely; some SDK exceptions can fail during str()."""
    try:
        return str(exc)
    except Exception:
        try:
            return repr(exc)
        except Exception:
            return f"<{type(exc).__name__}>"

router = APIRouter(prefix="/api/v3", tags=["gateway-v3"])


# ═══════════════════════════════════════════════════════════════════
# FSM STATE CONSTANTS (defined early for use in load_session)
# ═══════════════════════════════════════════════════════════════════
FSM_STATE_IDLE = "IDLE"
FSM_STATE_DRAFTING = "DRAFTING"
FSM_STATE_PENDING_CONFIRMATION = "PENDING_CONFIRMATION"  # Waiting for "onayla"


# ═══════════════════════════════════════════════════════════════════
# REQUEST/RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════

class MessageRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    message: str = Field("", description="User message")
    media_urls: Optional[List[str]] = Field(default=None)
    channel: str = Field("webchat")
    prefill_listing_data: Optional[Dict[str, Any]] = Field(default=None)


class ButtonResponse(BaseModel):
    text: str
    payload: str


class MessageResponse(BaseModel):
    success: bool = True
    text: str
    # WEBCHAT-ONLY HINT. `text` must always stand on its own.
    #
    # WhatsApp cannot show these. Its Business API only renders interactive replies through
    # pre-registered Content Templates (max 3 quick replies, ~20 char labels, created
    # ahead of time), which cannot carry the per-message labels generated here - and the
    # bridge sends plain `body=` text anyway. So on WhatsApp the seller always types their
    # answer, which is why FSM_COMMANDS accepts a wide range of natural confirmations.
    #
    # As of today the web ChatBox does not render them either, so nothing displays these
    # at all; that they were never missed is itself evidence that `text` carries the flow.
    buttons: List[ButtonResponse] = []
    listing_preview: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = {}
    error: Optional[str] = None


def _normalize_media_url(candidate: str) -> str:
    """Convert storage paths to public Supabase URLs when possible."""
    c = (candidate or "").strip()
    if not c:
        return ""
    if c.startswith(("http://", "https://")):
        return c
    base = (settings.supabase_url or "").strip().rstrip("/")
    if c.startswith("/storage/"):
        return f"{base}{c}" if base else c
    if base:
        path = c.lstrip("/")
        return f"{base}/storage/v1/object/public/product-images/{path}"
    return c


def _normalize_media_urls(media_urls: Optional[List[str]]) -> List[str]:
    if not media_urls:
        return []
    normalized: List[str] = []
    for url in media_urls:
        norm = _normalize_media_url(url)
        if norm and norm not in normalized:
            normalized.append(norm)
    return normalized


def _filter_valid_images(images: Optional[List[Any]]) -> List[str]:
    if not images:
        return []
    valid: List[str] = []
    for entry in images:
        if not entry:
            continue
        if isinstance(entry, dict):
            candidate = (
                entry.get("image_url")
                or entry.get("public_url")
                or entry.get("url")
                or entry.get("path")
            )
            if not isinstance(candidate, str):
                continue
            value = candidate.strip()
        elif isinstance(entry, str):
            value = entry.strip()
        else:
            continue

        if not value or value.upper() == "URL":
            continue
        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", value, re.IGNORECASE):
            continue

        norm = _normalize_media_url(value)
        if norm and norm not in valid:
            valid.append(norm)

    return valid


PROHIBITED_LISTING_TERMS = {
    # Do not include plain English "gun" here.
    # Turkish "gün" normalizes to "gun" and caused false-positive publish blocks.
    "silah", "tabanca", "tufek", "tüfek", "pistol", "firearm", "revolver", "shotgun",
    "mermi", "cephane", "bomba", "patlayici", "patlayıcı", "explosive", "uyusturucu", "uyuşturucu",
    "kokain", "eroin", "esrar", "meth", "amfetamin", "cocaine", "heroin",
}


def _contains_prohibited_term(normalized_text: str, term: str) -> bool:
    from services.text_normalization import normalize_for_match

    term_norm = normalize_for_match(term)
    if not normalized_text or not term_norm:
        return False

    # Multi-word phrase match with token boundaries
    if " " in term_norm:
        padded = f" {normalized_text} "
        return f" {term_norm} " in padded

    # Single word match with regex word boundaries to avoid false positives
    # Example false-positive prevented: "gun" matching "uygun"
    return re.search(rf"\b{re.escape(term_norm)}\b", normalized_text) is not None


def _detect_prohibited_listing_term(listing_data: Dict[str, Any]) -> Optional[str]:
    title = str(listing_data.get("title") or "")
    description = str(listing_data.get("description") or "")
    normalized = normalize_for_match(f"{title} {description}")
    if not normalized:
        return None
    for term in PROHIBITED_LISTING_TERMS:
        if _contains_prohibited_term(normalized, term):
            return term
    return None


# ═══════════════════════════════════════════════════════════════════
# SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

def _session_key(user_id: str, channel: str) -> str:
    """Scope session by channel to avoid WhatsApp/Webchat overlap."""
    safe_channel = (channel or "webchat").strip().lower()
    return f"{user_id}:{safe_channel}"


async def load_session(user_id: str, channel: str) -> Dict[str, Any]:
    """Load session from Redis (scoped by channel).

    NOTE: this rebuilds a fixed dict, so any key not listed here is dropped on every turn
    even though it was written to Redis. `description_confirmed_claims` and
    `user_statements` used to be exactly that: the description guard re-derived the user's
    confirmed claims from scratch each turn, so a detail the user gave in turn 1 counted as
    unverified by turn 2 and was stripped back out of their own listing.
    """
    session_id = _session_key(user_id, channel)
    session = await redis_client.get_session(session_id) or {}
    return {
        "listing_data": session.get("listing_data", {}),
        "state": session.get("state", "IDLE"),  # IDLE, DRAFTING, READY
        "conversation_history": session.get("conversation_history", []),
        "last_intent": session.get("last_intent"),
        "draft_updated_at": session.get("draft_updated_at"),
        "search_cache": session.get("search_cache", []),
        "search_cursor": session.get("search_cursor", 0),
        # FSM 2-step confirmation state
        "fsm_state": session.get("fsm_state", FSM_STATE_IDLE),
        "pending_publish_balance": session.get("pending_publish_balance"),
        "pending_publish_cost": session.get("pending_publish_cost"),
        # Ground truth for the description guard: what the user actually said, verbatim.
        _SESSION_DESCRIPTION_CLAIMS_KEY: session.get(_SESSION_DESCRIPTION_CLAIMS_KEY, []),
        _SESSION_USER_STATEMENTS_KEY: session.get(_SESSION_USER_STATEMENTS_KEY, []),
        # Price research context, also previously lost between turns.
        "last_price_query": session.get("last_price_query"),
        "last_suggested_price": session.get("last_suggested_price"),
        # Fingerprint of the last preview shown, so an unchanged draft is not re-printed.
        "last_preview_signature": session.get("last_preview_signature"),
    }


async def save_session(user_id: str, channel: str, session: Dict[str, Any]):
    """Save session to Redis (scoped by channel)"""
    session_id = _session_key(user_id, channel)
    history = session.get("conversation_history", [])
    if len(history) > 20:
        history = history[-20:]
    session["conversation_history"] = history
    await redis_client.set_session(session_id, session)


async def clear_session(user_id: str, channel: str):
    """Clear session - reset (scoped by channel)"""
    session_id = _session_key(user_id, channel)
    await redis_client.delete_session(session_id)


def _draft_signature(listing: Optional[Dict[str, Any]]) -> str:
    """Stable fingerprint of the fields a preview actually shows.

    Used to decide whether a preview would tell the user anything new. Re-printing an
    identical preview turn after turn is what made the flow feel like a form.
    """
    data = listing if isinstance(listing, dict) else {}
    parts = [
        str(data.get("title") or ""),
        str(data.get("description") or ""),
        str(data.get("price") or ""),
        str(data.get("location") or ""),
        str(data.get("condition") or ""),
        str(len(_filter_valid_images(data.get("images")))),
    ]
    return "|".join(parts)


def _is_show_more_command(lower_msg: str) -> bool:
    """Is this message *only* a request for the next page of search results?

    This used to be a substring test, so "devam edelim, konum İstanbul" - a perfectly
    normal drafting message - matched "devam" and got answered with search pagination
    whenever a stale search cache happened to exist. It now has to be the whole message.
    """
    msg = re.sub(r"[.!?\s]+$", "", (lower_msg or "").strip().lower())
    exact = {
        "daha fazla göster", "daha fazla goster", "daha fazla",
        "devamını göster", "devamini goster", "devamını", "devamini",
        "devam", "devam et", "sonraki", "sonraki sayfa", "diğerleri", "digerleri",
    }
    return msg in exact


def _normalize_intent_text(text: str) -> str:
    value = (text or "").lower().strip()
    tr_map = str.maketrans({
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
    })
    value = value.translate(tr_map)
    value = re.sub(r"\s+", " ", value)
    return value


def _looks_like_price_research_request(message: str) -> bool:
    """Soft fallback for flexible price-research utterances.

    This is intentionally permissive and only used when LLM returned CHAT
    without a tool_call. It should not override CREATE/SEARCH/REPORT flows.
    """
    msg = _normalize_intent_text(message)
    if not msg:
        return False

    strong_phrases = [
        "kac para", "kac lira", "ne kadar", "ne kadara", "fiyati ne", "fiyati nedir",
        "fiyat arast", "piyasa arast", "piyasa degeri", "ortalama fiyat", "ederi ne",
        "neye gider", "kaca gider", "ne kadara satilir", "fiyatini ogren",
    ]
    typo_phrases = [
        "fiyta", "fiytai", "fiyati nekadar", "fiyat nekadar", "fiyat nekdr",
    ]

    if any(p in msg for p in strong_phrases):
        return True
    if any(p in msg for p in typo_phrases):
        return True

    # Fallback token logic for colloquial variants.
    # These are matched on word boundaries: "ne" as a substring also lives inside "yeni",
    # "genel" and "sonra", so any sentence mentioning a price used to be routed to
    # Perplexity - "fiyat yeni: 25000" was a price-research request as far as this was
    # concerned.
    has_price_signal = bool(re.search(r"\b(fiyat\w*|piyasa\w*|deger\w*|eder|ortalama)\b", msg))
    has_question_signal = bool(
        "?" in msg or re.search(r"\b(nedir|ne|kac|kaca|nekadar|arastir\w*|soyler)\b", msg)
    )
    return has_price_signal and has_question_signal




_DESCRIPTION_PRICE_PATTERN = re.compile(
    r"\b(?:fiyat[ıi]?\s*[:=-]?\s*)?\d{1,3}(?:[.\s]\d{3})*(?:[.,]\d+)?\s*(?:tl|₺|lira)\b",
    flags=re.IGNORECASE,
)
_DESCRIPTION_PRICE_WORD_PATTERN = re.compile(r"\bfiyat[ıi]?\b", flags=re.IGNORECASE)
_DESCRIPTION_DAMAGE_PATTERN = re.compile(r"\bhasarl[ıi]\b", flags=re.IGNORECASE)
_DESCRIPTION_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
# Turkish is agglutinative: "kutu" surfaces as kutulu / kutusu / kutusuz / kutuda.
# The original patterns only allowed -lu/-suz, so "kutusu mevcuttur" and "sertifikası
# vardır" walked straight past the guard. These must stay symmetric with the extraction
# patterns in _extract_confirmed_description_claims: if the guard can recognise a surface
# form the extractor cannot, it deletes wording the seller actually used.
_DESCRIPTION_CONFIRMABLE_CLAIM_PATTERNS: Dict[str, re.Pattern] = {
    # Enumerated rather than \w* so the unrelated word "kutup" is not swept up.
    "kutu": re.compile(r"\bkutu(?:lu|luk|su|suz|sunda|sundan|yla|yu|da|dan)?\b", flags=re.IGNORECASE),
    "sertifika": re.compile(r"\bsertifika\w*", flags=re.IGNORECASE),
    "sinirli": re.compile(r"\bs[ıi]n[ıi]rl[ıi]\w*(?:\s+[üu]retim\w*)?", flags=re.IGNORECASE),
}
_DESCRIPTION_CONFIRMABLE_CLAIM_LABELS = {
    "kutu": "kutu",
    "sertifika": "sertifika",
    "sinirli": "sınırlı",
}
_DESCRIPTION_DELIVERY_CLAIM_PATTERNS: Dict[str, re.Pattern] = {
    "delivery:kargo": re.compile(r"\bkargo(?:yla|yle|ile)?\b", flags=re.IGNORECASE),
    "delivery:teslimat": re.compile(r"\bteslimat\b", flags=re.IGNORECASE),
    "delivery:elden_teslim": re.compile(r"\belden\s+teslim\b", flags=re.IGNORECASE),
    "delivery:kapida_odeme": re.compile(r"\bkap[ıi]da\s+[öo]deme\b", flags=re.IGNORECASE),
}
_DESCRIPTION_DELIVERY_CLAIM_LABELS = {
    "delivery:kargo": "kargo",
    "delivery:teslimat": "teslimat",
    "delivery:elden_teslim": "elden teslim",
    "delivery:kapida_odeme": "kapıda ödeme",
}
_DESCRIPTION_USAGE_COUNT_PATTERN = re.compile(
    r"\b(?:sadece\s+)?(?P<count>\d+)\s*kez\s+(?P<verb>kullan(?:ı|i)ld(?:ı|i)|giyildi|takıldı|takildi)\b",
    flags=re.IGNORECASE,
)
_DESCRIPTION_UNUSED_PATTERN = re.compile(r"\bhi[cç]\s+kullan(?:ı|i)lmad(?:ı|i)\b", flags=re.IGNORECASE)
_DESCRIPTION_QUALITY_CLAIM_PATTERNS: Dict[str, re.Pattern] = {
    "condition:cok_iyi_durumda": re.compile(r"\bçok\s+iyi\s+durum(?:da)?\b", flags=re.IGNORECASE),
    "condition:mukemmel_durumda": re.compile(r"\bm[üu]kemmel\s+durum(?:da)?\b", flags=re.IGNORECASE),
    "condition:kusursuz": re.compile(r"\bkusursuz\b", flags=re.IGNORECASE),
}
_DESCRIPTION_QUALITY_CLAIM_LABELS = {
    "condition:cok_iyi_durumda": "çok iyi durumda",
    "condition:mukemmel_durumda": "mükemmel durumda",
    "condition:kusursuz": "kusursuz",
}
_DESCRIPTION_SEED_PREFIX_PATTERNS = [
    re.compile(r"^(?:📷\s*)?(?:g[oö]rsel)\s*\d+\s*[:\-.]?\s*", flags=re.IGNORECASE),
    re.compile(r"^durum\s*[:\-.]?\s*", flags=re.IGNORECASE),
    re.compile(
        r"^(?:(?:öne|one)\s+(?:çıkan|cikan)\s+(?:özellikler|ozellikler)|(?:özellikler|ozellikler)|detaylar)\s*[:\-.]\s*",
        flags=re.IGNORECASE,
    ),
]
_SESSION_DESCRIPTION_CLAIMS_KEY = "description_confirmed_claims"
# Append-only archive of what the user actually typed this draft. It is the provenance
# source the description guard checks against, and it is never published - it only decides
# whether a verifiable claim in the generated copy is allowed to stay.
_SESSION_USER_STATEMENTS_KEY = "user_statements"
_MAX_USER_STATEMENTS = 30
_DESCRIPTION_REMOVAL_PATTERNS = [
    re.compile(
        r"(?:açıklamadan|aciklamadan|metinden|ilan metninden)\s+[\"'“”]?(?P<phrase>.+?)[\"'“”]?\s+(?:ifadesini|kelimesini|bilgisini)?\s*(?:sil|kaldır|kaldir|çıkar|cikar)(?:\s+gitsin)?\s*$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"[\"'“”]?(?P<phrase>.+?)[\"'“”]?\s+(?:ifadesini|kelimesini|bilgisini)\s+(?:açıklamadan|aciklamadan|metinden)\s+(?:sil|kaldır|kaldir|çıkar|cikar)\s*$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:açıklamadaki|aciklamadaki|metindeki)\s+[\"'“”]?(?P<phrase>.+?)[\"'“”]?\s+(?:ifadesini|kelimesini|bilgisini)?\s*(?:sil|kaldır|kaldir|çıkar|cikar)\s*$",
        flags=re.IGNORECASE,
    ),
]


def _build_usage_claim_key(count: str, verb: str) -> str:
    normalized_verb = normalize_for_match(verb).replace(" ", "")
    return f"usage:{count}:{normalized_verb or 'unknown'}"


def _extract_confirmed_description_claims(text: str) -> set[str]:
    claims: set[str] = set()
    if not text:
        return claims

    normalized = normalize_for_match(text)
    # Kept deliberately in step with _DESCRIPTION_CONFIRMABLE_CLAIM_PATTERNS above.
    if re.search(r"\bkutu(?:lu|luk|su|suz|sunda|sundan|yla|yu|da|dan)?\b", normalized, flags=re.IGNORECASE):
        claims.add("kutu")
    if re.search(r"\bsertifika\w*", normalized, flags=re.IGNORECASE):
        claims.add("sertifika")
    if re.search(r"\bsinirli\w*", normalized, flags=re.IGNORECASE):
        claims.add("sinirli")
    for claim_key, pattern in _DESCRIPTION_DELIVERY_CLAIM_PATTERNS.items():
        if pattern.search(text):
            claims.add(claim_key)
    for match in _DESCRIPTION_USAGE_COUNT_PATTERN.finditer(text):
        claims.add(_build_usage_claim_key(str(match.group("count") or ""), str(match.group("verb") or "")))
    if _DESCRIPTION_UNUSED_PATTERN.search(text):
        claims.add("usage:none")
    for claim_key, pattern in _DESCRIPTION_QUALITY_CLAIM_PATTERNS.items():
        if pattern.search(text):
            claims.add(claim_key)

    for year in _DESCRIPTION_YEAR_PATTERN.findall(text):
        claims.add(f"year:{year}")

    return claims


def _get_session_description_claims(session: Dict[str, Any]) -> set[str]:
    raw_claims = session.get(_SESSION_DESCRIPTION_CLAIMS_KEY) or []
    if not isinstance(raw_claims, list):
        return set()
    return {str(claim).strip() for claim in raw_claims if str(claim).strip()}


def _remember_description_claims(session: Dict[str, Any], *texts: str) -> set[str]:
    claims = _get_session_description_claims(session)
    for text in texts:
        claims.update(_extract_confirmed_description_claims(text))
    if claims:
        session[_SESSION_DESCRIPTION_CLAIMS_KEY] = sorted(claims)
    return claims


def _remove_description_claims(session: Dict[str, Any], *texts: str) -> None:
    claims = _get_session_description_claims(session)
    if not claims:
        return

    to_remove: set[str] = set()
    for text in texts:
        to_remove.update(_extract_confirmed_description_claims(text))

    claims.difference_update(to_remove)
    if claims:
        session[_SESSION_DESCRIPTION_CLAIMS_KEY] = sorted(claims)
    else:
        session.pop(_SESSION_DESCRIPTION_CLAIMS_KEY, None)


def _get_user_statements(session: Dict[str, Any]) -> List[str]:
    raw = session.get(_SESSION_USER_STATEMENTS_KEY) or []
    if not isinstance(raw, list):
        return []
    return [str(entry) for entry in raw if str(entry).strip()]


def _remember_user_statement(session: Dict[str, Any], message: str) -> None:
    """Append a raw user message to the provenance archive for this draft."""
    text = str(message or "").strip()
    if not text:
        return

    statements = _get_user_statements(session)
    if statements and statements[-1] == text:
        return  # ignore an exact repeat (retries, duplicate webhooks)

    statements.append(text)
    session[_SESSION_USER_STATEMENTS_KEY] = statements[-_MAX_USER_STATEMENTS:]


def _collect_confirmed_description_claims(
    session: Dict[str, Any],
    listing: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> set[str]:
    """Everything the user has verifiably told us across the whole draft.

    Reads the full statement archive, not just the current turn: a detail given three
    messages ago is still something the user said, and the generated copy is allowed to
    keep it.
    """
    claims = _get_session_description_claims(session)

    for statement in _get_user_statements(session):
        claims.update(_extract_confirmed_description_claims(statement))

    if listing and isinstance(listing, dict):
        claims.update(_extract_confirmed_description_claims(str(listing.get("title") or "")))
    if message:
        claims.update(_extract_confirmed_description_claims(message))
    return claims


def _cleanup_description_text(text: str) -> str:
    if not text:
        return ""

    cleaned_lines: List[str] = []
    for raw_line in str(text).splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        line = re.sub(r"\s+([,.;:!?])", r"\1", line)
        line = re.sub(r"([,.;:!?])(?:\s*[,.!?:;])+", r"\1", line)
        line = re.sub(r",\s*,+", ", ", line)
        line = line.strip(" ,;:-")
        if line:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def _strip_description_seed_prefixes(text: str) -> str:
    cleaned = str(text or "").strip()
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        for pattern in _DESCRIPTION_SEED_PREFIX_PATTERNS:
            cleaned = pattern.sub("", cleaned).strip()
    return cleaned.strip(" ,;:-")


def _looks_like_description_seed(text: str) -> bool:
    if not text:
        return False

    raw_text = str(text)
    if re.search(
        r"(?:g[oö]rsel\s*\d+\s*[:\-.]|durum\s*[:\-.]|(?:öne|one)\s+(?:çıkan|cikan)\s+(?:özellikler|ozellikler)\s*[:\-.])",
        raw_text,
        flags=re.IGNORECASE,
    ):
        return True

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in _DESCRIPTION_SEED_PREFIX_PATTERNS):
            return True

    return False


def _normalize_description_seed(text: str) -> str:
    if not text:
        return ""

    if not _looks_like_description_seed(text):
        return _cleanup_description_text(text)

    fragments: List[str] = []
    seen: set[str] = set()
    for raw_part in re.split(r"[\r\n]+|(?<=[.!?])\s+", str(text)):
        part = _strip_description_seed_prefixes(raw_part)
        part = re.sub(r"\s+", " ", part).strip(" ,;:-")
        normalized_part = normalize_for_match(part)
        if not normalized_part or normalized_part in seen:
            continue
        seen.add(normalized_part)
        if len(part) > 1:
            part = part[0].upper() + part[1:]
        if part[-1] not in ".!?":
            part += "."
        fragments.append(part)

    return _cleanup_description_text(" ".join(fragments))


def _collect_description_validation_errors(
    listing: Dict[str, Any],
    confirmed_claims: Optional[set[str]] = None,
) -> List[str]:
    description = str(listing.get("description") or "").strip()
    if not description:
        return []

    claims = confirmed_claims or set()
    errors: List[str] = []
    normalized = normalize_for_match(description)

    if _DESCRIPTION_PRICE_PATTERN.search(description) or _DESCRIPTION_PRICE_WORD_PATTERN.search(description):
        errors.append("Açıklamada fiyat yazılamaz.")

    if re.search(r"\bhasarli\b", normalized, flags=re.IGNORECASE):
        errors.append("Açıklamada hasarlı bilgisi kullanılamaz; durum alanı bunu doğrulamıyor.")

    for claim_key, label in _DESCRIPTION_CONFIRMABLE_CLAIM_LABELS.items():
        if claim_key in claims:
            continue
        if _DESCRIPTION_CONFIRMABLE_CLAIM_PATTERNS[claim_key].search(description):
            errors.append(f"Doğrulanmamış bilgi: {label}")

    for claim_key, label in _DESCRIPTION_DELIVERY_CLAIM_LABELS.items():
        if claim_key in claims:
            continue
        if _DESCRIPTION_DELIVERY_CLAIM_PATTERNS[claim_key].search(description):
            errors.append(f"Doğrulanmamış bilgi: {label}")

    for match in _DESCRIPTION_USAGE_COUNT_PATTERN.finditer(description):
        claim_key = _build_usage_claim_key(str(match.group("count") or ""), str(match.group("verb") or ""))
        if claim_key not in claims:
            errors.append(f"Doğrulanmamış bilgi: {match.group(0).strip()}")

    if _DESCRIPTION_UNUSED_PATTERN.search(description) and "usage:none" not in claims:
        errors.append("Doğrulanmamış bilgi: hiç kullanılmadı")

    for claim_key, label in _DESCRIPTION_QUALITY_CLAIM_LABELS.items():
        if claim_key in claims:
            continue
        if _DESCRIPTION_QUALITY_CLAIM_PATTERNS[claim_key].search(description):
            errors.append(f"Doğrulanmamış bilgi: {label}")

    for year in sorted(set(_DESCRIPTION_YEAR_PATTERN.findall(description))):
        if f"year:{year}" not in claims:
            errors.append(f"Doğrulanmamış bilgi: {year}")

    deduped: List[str] = []
    seen: set[str] = set()
    for error in errors:
        if error not in seen:
            seen.add(error)
            deduped.append(error)
    return deduped


def _sanitize_ai_generated_description(
    description: str,
    confirmed_claims: Optional[set[str]] = None,
) -> tuple[str, List[str]]:
    cleaned = _normalize_description_seed(str(description or ""))
    claims = confirmed_claims or set()
    removed: List[str] = []

    for pattern in (_DESCRIPTION_PRICE_PATTERN, _DESCRIPTION_PRICE_WORD_PATTERN):
        cleaned, count = pattern.subn(" ", cleaned)
        if count:
            removed.append("price")

    cleaned, count = _DESCRIPTION_DAMAGE_PATTERN.subn(" ", cleaned)
    if count:
        removed.append("hasarli")

    for claim_key, pattern in _DESCRIPTION_CONFIRMABLE_CLAIM_PATTERNS.items():
        if claim_key in claims:
            continue
        cleaned, count = pattern.subn(" ", cleaned)
        if count:
            removed.append(claim_key)

    for claim_key, pattern in _DESCRIPTION_DELIVERY_CLAIM_PATTERNS.items():
        if claim_key in claims:
            continue
        cleaned, count = pattern.subn(" ", cleaned)
        if count:
            removed.append(claim_key)

    def _strip_unconfirmed_usage(match: re.Match[str]) -> str:
        claim_key = _build_usage_claim_key(str(match.group("count") or ""), str(match.group("verb") or ""))
        if claim_key in claims:
            return match.group(0)
        removed.append(claim_key)
        return " "

    cleaned = _DESCRIPTION_USAGE_COUNT_PATTERN.sub(_strip_unconfirmed_usage, cleaned)

    if "usage:none" not in claims:
        cleaned, count = _DESCRIPTION_UNUSED_PATTERN.subn(" ", cleaned)
        if count:
            removed.append("usage:none")

    for claim_key, pattern in _DESCRIPTION_QUALITY_CLAIM_PATTERNS.items():
        if claim_key in claims:
            continue
        cleaned, count = pattern.subn(" ", cleaned)
        if count:
            removed.append(claim_key)

    for year in sorted(set(_DESCRIPTION_YEAR_PATTERN.findall(cleaned))):
        if f"year:{year}" in claims:
            continue
        cleaned, count = re.subn(rf"\b{re.escape(year)}\b", " ", cleaned)
        if count:
            removed.append(f"year:{year}")

    return _cleanup_description_text(cleaned), removed


def _apply_ai_description_guard(
    listing: Dict[str, Any],
    previous_listing: Optional[Dict[str, Any]],
    confirmed_claims: Optional[set[str]] = None,
) -> List[str]:
    description = str(listing.get("description") or "").strip()
    if not description:
        return []

    cleaned_description, removed_tokens = _sanitize_ai_generated_description(description, confirmed_claims)
    if cleaned_description:
        listing["description"] = cleaned_description[:2000]
    else:
        previous_description = str((previous_listing or {}).get("description") or "").strip()
        listing["description"] = previous_description[:2000] if previous_description else ""

    errors = _collect_description_validation_errors(listing, confirmed_claims)
    if errors:
        previous_description = str((previous_listing or {}).get("description") or "").strip()
        if previous_description:
            fallback = dict(listing)
            fallback["description"] = previous_description
            if not _collect_description_validation_errors(fallback, confirmed_claims):
                listing["description"] = previous_description[:2000]
                errors = []

    if removed_tokens:
        logger.info(f"Description guard removed generated claims: {sorted(set(removed_tokens))}")

    return errors


def _extract_description_removal_phrase(message: str) -> Optional[str]:
    if not message:
        return None

    for pattern in _DESCRIPTION_REMOVAL_PATTERNS:
        match = pattern.search(message.strip())
        if not match:
            continue
        phrase = str(match.group("phrase") or "").strip().strip("`'\"“”")
        if phrase:
            return phrase
    return None


def _split_description_removal_targets(phrase: str) -> List[str]:
    if not phrase:
        return []

    raw_targets = re.split(r"\s+(?:ve|ile|ya da|yada)\s+|[,/;]", phrase, flags=re.IGNORECASE)
    targets: List[str] = []
    seen: set[str] = set()
    for raw_target in raw_targets:
        target = str(raw_target or "").strip().strip("`'\"“”")
        target = re.sub(r"\b(?:yaz[ıi]s[ıi]n[ıi]|kelimesini|ifadesini|bilgisini)\b", " ", target, flags=re.IGNORECASE)
        target = re.sub(r"\s+", " ", target).strip(" ,;:-")
        normalized_target = normalize_for_match(target)
        if not normalized_target or normalized_target in seen:
            continue
        seen.add(normalized_target)
        targets.append(target)
    return targets


def _build_description_removal_pattern(phrase: str) -> re.Pattern:
    tokens = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]+|\d+", phrase)
    if not tokens:
        return re.compile(re.escape(phrase), flags=re.IGNORECASE)
    joined = r"[\s_-]*".join(re.escape(token) for token in tokens)
    return re.compile(rf"\b{joined}\b\s*[:=-]?", flags=re.IGNORECASE)


def _classify_draft_message_intent(message: str) -> Optional[str]:
    if not message:
        return None

    from core.brain import Guardrails

    if Guardrails.detect_confirmation(message):
        return "publish"

    if _extract_description_removal_phrase(message):
        return "remove_description_text"

    if _should_try_direct_edit(message):
        return "field_update"

    # Copy improvement requests ("başlığı daha güzel yaz") intentionally return None:
    # they belong to the Brain, which carries the category writing profile. Note that
    # _should_try_direct_edit must still reject them, so they are not mistaken for a
    # literal field edit - see _looks_like_enrichment_only_value.
    return None


async def _format_search_continuation_page(listings: List[Dict[str, Any]], start_idx: int, page_size: int = 5) -> str:
    total = len(listings or [])
    if total == 0 or start_idx >= total:
        return "📄 Gösterilecek başka ilan kalmadı."

    end_idx = min(start_idx + page_size, total)
    chunk = listings[start_idx:end_idx]

    lines: List[str] = [f"📄 {start_idx + 1}-{end_idx}. ilanlar:", ""]
    for i, listing in enumerate(chunk, start=start_idx + 1):
        title = prefix_example_listing_title(listing.get("title") or "Başlıksız", listing)
        price = listing.get("price")
        price_txt = f"{price} TL" if price is not None else "Fiyat belirtilmemiş"
        category = listing.get("category") or "Kategori yok"

        lines.append(f"{i}. {title} - {price_txt} - {category}")

        short_desc = str(listing.get("description") or "")[:120].strip()
        if short_desc:
            lines.append(short_desc + "...")

        listing_id = str(listing.get("id") or "").strip()
        if listing_id:
            try:
                token_row = await supabase_client.ensure_contact_token_for_listing(listing_id)
                token = str((token_row or {}).get("token") or "").strip() if isinstance(token_row, dict) else ""
                if token:
                    frontend_base = (getattr(settings, "frontend_base_url", None) or "https://pazarglobal.com").strip().rstrip("/")
                    lines.append(f"Mesaj Gönder: {frontend_base}/contact/{token}")
            except Exception as e:
                logger.warning(f"Failed to attach contact link in search pagination (listing_id={listing_id}): {e}")

        lines.append("")

    if end_idx < total:
        lines.append(f"Toplam {total} ilanın {end_idx} tanesini gösterdim. Devamı için 'daha fazla göster' yazabilirsiniz.")
    else:
        lines.append("Tüm ilanları gösterdim. Detay için: '6 nolu ilanın detayını göster' yazabilirsiniz.")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# FSM ENGINE - Deterministic JSON Processing
# ═══════════════════════════════════════════════════════════════════

class FSMEngine:
    """
    FSM Engine - JSON validasyon ve publish
    
    Görevler:
    - JSON validasyon (schema kontrolü)
    - Otomatik kategori belirleme (category_library kullanarak) - LLM DEĞİL!
    - Keywords üretme
    - Wallet kontrolü
    - İlan yayınlama
    
    NOT: Resim zorunlu DEĞİL
    NOT: Kategori FSM tarafından otomatik belirlenir!
    """
    
    # Category FSM tarafından otomatik belirlenir - missing fields'a dahil değil!
    REQUIRED_FIELDS = ["title", "price", "description", "location"]
    
    # Import from category_library - single source of truth
    from services.category_library import SUPPORTED_CATEGORIES, classify_category, normalize_category_id
    ALLOWED_CATEGORIES = set(SUPPORTED_CATEGORIES)
    
    ALLOWED_CONDITIONS = {"Sıfır", "Az Kullanılmış", "2. El"}
    
    @classmethod
    def validate(cls, listing_data: Dict[str, Any], confirmed_claims: Optional[set[str]] = None) -> tuple[bool, List[str]]:
        """
        JSON validasyon - FSM'in beklediği formata uygun mu?
        
        Returns:
            (is_valid, missing_fields)
        """
        missing = []

        # Normalize text fields to the TR/EN keyboard-safe alphabet, and sentence-case
        # only text that clearly needs it.
        #
        # sentence_case_tr applies Turkish casing to the first letter of each sentence, so
        # it turns "iPhone 15 Pro" into "İPhone 15 Pro". That was tolerable when the copy
        # arrived flattened to lowercase anyway; now that the assistant writes properly
        # cased listing copy, re-casing it does more damage than good. It is only applied
        # to text with no capitals at all - the "user typed everything lowercase" case it
        # was written for.
        try:
            from services.text_normalization import normalize_keyboard_text, sentence_case_tr

            for key in ["title", "description", "location"]:
                raw_val = listing_data.get(key)
                if isinstance(raw_val, str) and raw_val.strip():
                    normalized = normalize_keyboard_text(raw_val)
                    if not any(ch.isupper() for ch in normalized):
                        normalized = sentence_case_tr(normalized)
                    listing_data[key] = normalized
        except Exception:
            pass

        # Title - minimum 5 karakter (zorunlu alan)
        title = listing_data.get("title", "")
        if not title or len(str(title).strip()) < 5:
            missing.append("title")

        # Description - minimum 10 karakter (zorunlu alan)
        description = listing_data.get("description", "")
        if not description or len(str(description).strip()) < 10:
            missing.append("description")
        elif _collect_description_validation_errors(listing_data, confirmed_claims):
            missing.append("description")

        # Price
        price = listing_data.get("price")
        if price is None:
            missing.append("price")
        else:
            try:
                price_val = float(price)
                if not (1 <= price_val <= 100_000_000):
                    missing.append("price")
            except (ValueError, TypeError):
                missing.append("price")

        # Location - now required for publish readiness
        location = listing_data.get("location", "")
        if not location or len(str(location).strip()) < 2:
            missing.append("location")

        # Category - FSM OTOMATİK BELİRLER (LLM sorumluluğunda değil!)
        category = listing_data.get("category")
        # Önce normalize et (kullanıcı "Tarım&Gıda" yazmışsa "Tarım & Gıda" yap)
        if category and category not in ["Sistem", "Otomatik", ""]:
            normalized = cls.normalize_category_id(category)
            if normalized and normalized in cls.ALLOWED_CATEGORIES:
                listing_data["category"] = normalized
                category = normalized
        
        # Kategori boş, "Sistem", "Otomatik", "Diğer" veya geçersizse → başlık/açıklamadan
        # otomatik belirle.
        #
        # "Diğer" is this function's own fallback for "nothing matched", not a choice
        # anyone made - category is derived here, never asked of the seller. Treating it
        # as a settled answer made it stick: a Jetta whose title and description classify
        # cleanly as "Otomotiv" stayed in "Diğer" because an earlier turn, when the draft
        # was still an empty shell with nothing to classify, had already written it there.
        if not listing_data.get("category_locked") and (
            not category
            or category in ["Sistem", "Otomatik", "Diğer"]
            or category not in cls.ALLOWED_CATEGORIES
        ):
            title = listing_data.get("title", "")
            description = listing_data.get("description", "")
            auto_category = cls.classify_category(f"{title} {description}")
            if auto_category and auto_category in cls.ALLOWED_CATEGORIES:
                listing_data["category"] = auto_category
                category = auto_category
                logger.info(f"FSM auto-classified category: {auto_category} from title/desc")
            else:
                # Hiç bulunamazsa → "Diğer" (asla boş bırakma!)
                listing_data["category"] = "Diğer"
                category = "Diğer"
                logger.info(f"FSM defaulted category to 'Diğer' - no match found")

        # The lock above skips re-derivation, not the guarantee that a category exists:
        # a locked value that is empty or not a real category would otherwise reach the
        # insert as-is.
        if category not in cls.ALLOWED_CATEGORIES:
            listing_data["category"] = "Diğer"
            category = "Diğer"

        # Kategori artık kesinlikle dolu - missing'e ekleme (FSM her zaman doldurur)
        # NOT: Kategori artık hiçbir zaman missing olmaz!
        
        is_valid = len(missing) == 0
        return is_valid, missing
    
    @classmethod
    def generate_keywords(cls, listing_data: Dict[str, Any]) -> str:
        """
        Deterministic keywords for metadata.keywords_text.
        Uses synonym expansion so broad queries (araba/otomobil/araç) do not miss listings.
        """
        title = str(listing_data.get("title") or "")
        desc = str(listing_data.get("description") or "")
        category = str(listing_data.get("category") or "")
        condition = str(listing_data.get("condition") or "")
        location = str(listing_data.get("location") or "")

        from services.text_normalization import lower_tr, normalize_keyboard_text

        # Recall helpers key off category and title only. Scanning the description meant a
        # single incidental word triggered a whole synonym family: a Mudanya flat whose
        # description said "telefon" was indexed as "telefon, cep telefonu, akıllı telefon"
        # and surfaced in phone searches.
        haystack = normalize_for_match(f"{category} {title}")
        tokens: List[str] = []

        stop = {
            "satilik", "kiralik", "urun", "esya", "temiz", "kullanilmis",
            "fiyat", "tl", "acil", "hemen", "durumda",
            # Turkish function words: pure noise that used to eat the token budget.
            "ve", "ile", "veya", "bir", "bu", "icin", "için", "ise", "yani",
            "sira", "sıra", "olan", "olarak",
        }

        # Category-level recall helpers
        if any(k in haystack for k in ["otomotiv", "vasita", "arac"]):
            tokens.extend(["araba", "otomobil", "arac", "otomotiv"])
        if any(k in haystack for k in ["emlak", "konut", "gayrimenkul"]):
            tokens.extend(["emlak", "ev", "daire", "konut"])
        if any(k in haystack for k in ["elektronik", "telefon", "bilgisayar"]):
            tokens.extend(["elektronik"])
        if any(k in haystack for k in ["iphone", "samsung", "xiaomi", "telefon", "akilli telefon"]):
            tokens.extend(["telefon", "cep telefonu", "akilli telefon"])
        if any(k in haystack for k in ["laptop", "notebook", "lenovo", "dell", "asus", "hp", "macbook"]):
            tokens.extend(["bilgisayar", "laptop", "notebook"])

        # Order matters: the cap below truncates, so the short high-signal fields go
        # first and the long description spends whatever budget is left.
        for src in [title, category, condition, location, desc]:
            # lower_tr(), not .lower(): Python maps the Turkish dotted capital İ to
            # "i" + U+0307 (combining dot), which the character class below rejects, so
            # "İstanbul" indexed as "stanbul" and never matched a search for "istanbul".
            # search_listings() lowercases the query the same way, so both sides agree.
            normalized_src = lower_tr(normalize_keyboard_text(src or ""))
            # Contact numbers live in some descriptions. Indexing them would make a
            # seller's phone number a searchable term, which the publish flow deliberately
            # avoids everywhere else.
            normalized_src = re.sub(r"\+?\d[\d\s().-]{7,}\d", " ", normalized_src)
            for t in re.findall(r"[0-9a-zçğıöşü\+]{2,}", normalized_src, flags=re.IGNORECASE):
                w = t.strip("+").strip()
                if not w or w in stop:
                    continue
                tokens.append(w)

        seen: set[str] = set()
        deduped: List[str] = []
        for w in tokens:
            if w in seen:
                continue
            seen.add(w)
            deduped.append(w)
            if len(deduped) >= 40:
                break

        return " ".join(deduped)
    
    @classmethod
    async def check_wallet(cls, user_id: str, required_amount: float = 55.0) -> tuple[bool, float]:
        """
        Wallet kontrolü
        
        Returns:
            (has_enough, current_balance)
        """
        try:
            logger.info(f"Checking wallet for user_id: {user_id}")
            # Use .limit(1) instead of .single() to avoid exception when no row found
            # Column name is balance_bigint (not balance) in actual Supabase table
            result = (
                supabase_client.client
                .table("wallets")
                .select("balance_bigint, free_unlimited_until")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            
            if not result.data or len(result.data) == 0:
                logger.warning(f"No wallet found for user_id: {user_id}, creating one with 0 balance")
                # First ensure profile exists (foreign key constraint)
                await cls.ensure_profile_exists(user_id)
                # Auto-create wallet with 0 balance
                try:
                    supabase_client.client.table("wallets").insert({
                        "user_id": user_id,
                        "balance_bigint": 0,
                    }).execute()
                    logger.info(f"Created wallet with 0 balance for {user_id}")
                except Exception as create_err:
                    logger.error(f"Failed to create wallet: {create_err}")
                return False, 0.0

            row = result.data[0] if isinstance(result.data, list) and result.data else {}
            promo_until = row.get("free_unlimited_until")
            try:
                if promo_until:
                    # If promo is active, treat as unlimited credits.
                    # We return a high balance for display purposes.
                    from datetime import datetime, timezone
                    dt = promo_until
                    if isinstance(dt, str):
                        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                    if isinstance(dt, datetime) and dt > datetime.now(timezone.utc):
                        logger.info(f"Promo unlimited credits active until {dt.isoformat()} for user {user_id}")
                        return True, 10**12
            except Exception:
                pass

            balance = float(row.get("balance_bigint", 0) or 0)
            logger.info(f"Wallet balance for {user_id}: {balance} TL (required: {required_amount})")
            return balance >= required_amount, balance
            
        except Exception as e:
            logger.error(f"Wallet check error for {user_id}: {e}", exc_info=True)
            return False, 0.0
    
    @classmethod
    async def deduct_credit(cls, user_id: str, amount: float = 55.0) -> bool:
        """Wallet'tan kredi düş"""
        try:
            logger.info(f"Deducting {amount} TL from user_id: {user_id}")
            # Get current balance - use .limit(1) instead of .single() to avoid exception
            # Column name is balance_bigint (not balance) in actual Supabase table
            result = (
                supabase_client.client
                .table("wallets")
                .select("balance_bigint, free_unlimited_until")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            
            if not result.data or len(result.data) == 0:
                logger.error(f"No wallet found for deduction: {user_id}")
                return False

            row = result.data[0] if isinstance(result.data, list) and result.data else {}
            promo_until = row.get("free_unlimited_until")
            try:
                if promo_until and float(amount) > 0:
                    from datetime import datetime, timezone
                    dt = promo_until
                    if isinstance(dt, str):
                        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                    if isinstance(dt, datetime) and dt > datetime.now(timezone.utc):
                        logger.info(f"Promo active; skipping credit deduction for user {user_id}")
                        return True
            except Exception:
                pass

            current = float(row.get("balance_bigint", 0) or 0)
            new_balance = current - amount
            
            if new_balance < 0:
                logger.warning(f"Insufficient balance for {user_id}: {current} < {amount}")
                return False
            
            # Update
            supabase_client.client.table("wallets").update({"balance_bigint": int(new_balance)}).eq("user_id", user_id).execute()
            logger.info(f"Deducted {amount} TL from {user_id}. New balance: {new_balance}")
            return True
            
        except Exception as e:
            logger.error(f"Deduct credit error for {user_id}: {e}", exc_info=True)
            return False
    
    @classmethod
    async def ensure_profile_exists(cls, user_id: str) -> bool:
        """Kullanıcı profili var mı kontrol et, yoksa oluştur"""
        try:
            # Check if profile exists
            result = supabase_client.client.table("profiles").select("id").eq("id", user_id).limit(1).execute()
            
            if result.data and len(result.data) > 0:
                return True
            
            # Create profile
            logger.info(f"Creating profile for user_id: {user_id}")
            supabase_client.client.table("profiles").insert({
                "id": user_id,
            }).execute()
            return True
            
        except Exception as e:
            logger.error(f"Profile check/create error for {user_id}: {e}", exc_info=True)
            return False
    
    @classmethod
    async def publish(
        cls,
        user_id: str,
        listing_data: Dict[str, Any],
        confirmed_claims: Optional[set[str]] = None,
    ) -> tuple[bool, str, Optional[str]]:
        """
        İlan yayınla
        
        Returns:
            (success, message, listing_id)
        """
        try:
            # 0. Ensure profile exists (foreign key constraint)
            if not await cls.ensure_profile_exists(user_id):
                return False, "Kullanıcı profili oluşturulamadı. Lütfen tekrar deneyin.", None
            
            # 1. Validate
            is_valid, missing = cls.validate(listing_data, confirmed_claims)
            if not is_valid:
                description_errors = _collect_description_validation_errors(listing_data, confirmed_claims)
                if description_errors:
                    return False, " | ".join(description_errors), None
                return False, f"Eksik alanlar: {', '.join(missing)}", None

            prohibited_term = _detect_prohibited_listing_term(listing_data)
            if prohibited_term:
                return False, "🚫 Bu içerik platform politikalarına aykırı olduğu için yayınlanamaz.", None
            
            # 2-3. Reserve the publish credit atomically.
            # The listing id doubles as the reservation's idempotency key, so it has to
            # exist before the charge: a retried webhook then reuses the same reference
            # instead of billing the user twice.
            listing_id = str(uuid.uuid4())
            cost = int(getattr(settings, "listing_credit_cost", 55) or 55)

            reservation = await supabase_client.reserve_listing_credit(user_id, cost, listing_id)
            credit_reserved = bool(reservation.get("success"))
            legacy_promo = False

            if not credit_reserved and reservation.get("error") == "rpc_unavailable":
                # migrations/005_atomic_listing_credit.sql is not applied yet. Keep
                # publishing via the legacy read-modify-write path so a missing migration
                # never takes the flow down; the race stays open until it is applied.
                has_enough, balance = await cls.check_wallet(user_id, cost)
                if not has_enough:
                    return False, f"💳 Bakiyeniz yetersiz (Mevcut: {balance:.0f} TL). İlan yayınlamak için {cost} TL gerekiyor.", None

                # check_wallet reports this sentinel balance only while the promo window
                # is active, which is also when deduct_credit deliberately charges nothing.
                legacy_promo = balance >= 10**12

                if not await cls.deduct_credit(user_id, cost):
                    return False, "Kredi düşürülemedi. Lütfen tekrar deneyin.", None

            elif not credit_reserved:
                error = str(reservation.get("error") or "")
                if error == "insufficient_balance":
                    balance = float(reservation.get("balance") or 0)
                    return False, f"💳 Bakiyeniz yetersiz (Mevcut: {balance:.0f} TL). İlan yayınlamak için {cost} TL gerekiyor.", None
                if error == "wallet_not_found":
                    return False, "Cüzdan bulunamadı. Lütfen tekrar deneyin.", None
                logger.error(f"Credit reservation failed for {user_id}: {reservation}")
                return False, "Kredi düşürülemedi. Lütfen tekrar deneyin.", None

            async def release_credit() -> None:
                """Undo the charge, via whichever path actually took the money."""
                if credit_reserved:
                    await supabase_client.refund_listing_credit(user_id, listing_id)
                elif not legacy_promo:
                    # Never refund a promo user: they were not charged, so a refund would
                    # hand them credits they never spent.
                    await cls.deduct_credit(user_id, -float(cost))

            # 4. Prepare listing for Supabase
            
            # Generate keywords
            keywords_text = cls.generate_keywords(listing_data)

            # Build metadata (standardized format)
            from datetime import datetime, timezone

            existing_metadata = listing_data.get("metadata")
            metadata: Dict[str, Any] = existing_metadata if isinstance(existing_metadata, dict) else {}

            created_via = listing_data.get("created_via") or "webchat"
            keyword_list = [
                k.lower()
                for k in re.findall(r"[\wğüşöçıİĞÜŞÖÇ]+", keywords_text, flags=re.UNICODE)
                if k
            ]

            metadata.update({
                "source": "agent",
                "created_via": created_via,
                "client_app": "pazarglobal-agent",
                "flow_version": "2026-01-18",
                "keyword_source": metadata.get("keyword_source") or "fsm",
                "created_at_client": metadata.get("created_at_client") or datetime.now(timezone.utc).isoformat(),
                "attributes": metadata.get("attributes") or {},
                "keywords": metadata.get("keywords") or keyword_list,
                "keywords_text": metadata.get("keywords_text") or keywords_text,
            })
            
            # Sanitize images before publish
            safe_images = _filter_valid_images(listing_data.get("images", []))

            # Build final listing object
            final_listing = {
                "id": listing_id,
                "user_id": user_id,
                "title": listing_data.get("title"),
                "description": listing_data.get("description"),
                "category": listing_data.get("category"),
                "price": float(listing_data.get("price", 0)),
                "condition": listing_data.get("condition", "2. El"),
                "location": listing_data.get("location"),
                "status": "active",
                "images": safe_images,
                "image_url": safe_images[0] if safe_images else None,
                "metadata": metadata,
            }

            # Attach user display info (best-effort)
            try:
                user_name = await supabase_client.get_user_display_name(user_id)
            except Exception:
                user_name = None
            try:
                user_phone = await supabase_client.get_user_phone(user_id)
            except Exception:
                user_phone = None

            if not user_phone and isinstance(listing_data, dict):
                raw_phone = listing_data.get("contact_phone")
                if isinstance(raw_phone, str) and raw_phone.strip():
                    user_phone = raw_phone.strip()

            phone_visibility = "public"
            name_visibility = "public"
            try:
                profile_res = (
                    supabase_client.client
                    .table("profiles")
                    .select("phone_visibility,name_visibility")
                    .eq("id", user_id)
                    .limit(1)
                    .execute()
                )
                profile_row = profile_res.data[0] if profile_res.data else None
                if isinstance(profile_row, dict):
                    if str(profile_row.get("phone_visibility") or "").strip().lower() == "hidden":
                        phone_visibility = "hidden"
                    if str(profile_row.get("name_visibility") or "").strip().lower() == "hidden":
                        name_visibility = "hidden"
            except Exception:
                pass

            # The seller asked for their number in the listing text. It is appended here,
            # from the verified profile, and never from whatever number was typed into
            # chat - a pasted number could be wrong or belong to someone else.
            if listing_data.get("include_phone_in_description") and user_phone:
                description = str(final_listing.get("description") or "").strip()
                if user_phone not in description:
                    final_listing["description"] = (
                        f"{description}\n\nİletişim: {user_phone}".strip()[:2000]
                    )

            final_listing["user_name"] = user_name
            final_listing["user_phone"] = user_phone
            final_listing["phone_visibility"] = phone_visibility
            final_listing["name_visibility"] = name_visibility
            
            # 5. Insert to Supabase
            logger.info(f"Inserting listing to Supabase: {listing_id}")
            logger.info(f"Listing data for insert: title={final_listing.get('title')}, category={final_listing.get('category')}, price={final_listing.get('price')}, user_id={user_id}")
            
            try:
                result = supabase_client.client.table("listings").insert(final_listing).execute()
                logger.info(f"Supabase insert result: data={bool(result.data)}, count={len(result.data) if result.data else 0}")
            except Exception as insert_err:
                logger.error(f"Supabase insert exception: {insert_err}", exc_info=True)
                logger.error(f"Failed listing data: {final_listing}")
                await release_credit()
                return False, f"İlan kaydedilemedi: {str(insert_err)}", None
            
            if not result.data:
                logger.error(f"Supabase insert returned no data. Result: {result}")
                await release_credit()
                return False, "İlan kaydedilemedi. Lütfen tekrar deneyin.", None
            
            logger.info(f"Listing published successfully: {listing_id}")

            # Search results are cached in Redis with no write-through invalidation.
            # Without this the freshly published listing stays invisible to search until
            # the cache TTL expires.
            try:
                cleared = await redis_client.clear_search_cache()
                logger.info(f"Search cache invalidated after publish ({cleared} keys)")
            except Exception as cache_err:
                logger.warning(f"Search cache invalidation failed after publish: {cache_err}")

            return True, "İlan başarıyla yayınlandı!", listing_id
            
        except Exception as e:
            logger.error(f"Publish error: {e}", exc_info=True)
            return False, f"Yayınlama hatası: {str(e)}", None


# ═══════════════════════════════════════════════════════════════════
# FSM STATE MACHINE - Deterministic confirmation flow (LLM bypass)
# ═══════════════════════════════════════════════════════════════════

# FSM Commands (deterministic, LLM bypassed)
# IMPORTANT: In PENDING_CONFIRMATION state, these commands trigger direct action without LLM
# Only consulted in PENDING_CONFIRMATION, where the assistant has just asked one yes/no
# question, so words that are ambiguous anywhere else ("tamam", "olur") can only mean one
# thing here. Guardrails.detect_confirmation stays narrow precisely because it runs in
# every state.
#
# The list is deliberately generous. On WhatsApp there are no buttons to press - the
# platform only supports quick replies through pre-registered Content Templates, which
# cannot carry the per-message labels this agent generates - so the reply is always
# free-typed. A confirmation the map does not recognise silently falls through to the LLM,
# drops back to DRAFTING and leaves the seller wondering why nothing was published.
#
# Matching stays whole-message on purpose: "evet ama fiyatı değiştir" must NOT publish.
FSM_COMMANDS = {
    cmd: "CONFIRM"
    for cmd in (
        "onayla", "onaylıyorum", "onayliyorum", "onaylayalım", "onaylayalim",
        "onaylıyorum yayınla", "evet onayla", "evet yayınla", "evet yayinla",
        "yayınla", "yayinla", "yayınlayalım", "yayinlayalim", "yayına al", "yayina al",
        "evet", "evet lütfen", "evet lutfen", "tamam", "tamamdır", "tamamdir",
        "olur", "olsun", "peki", "hadi", "hadi yayınla", "hadi yayinla",
        "oldu", "uygun", "kabul", "kabul ediyorum", "onay",
    )
} | {
    cmd: "CANCEL"
    for cmd in (
        "iptal", "iptal et", "iptal edelim", "vazgeç", "vazgeçtim", "vazgec", "vazgectim",
        "hayır", "hayir", "yok", "olmaz", "istemiyorum", "dur", "boş ver", "bos ver",
        "şimdilik olmasın", "simdilik olmasin",
    )
}

# The exact phrases above can never cover how people actually type. A seller who wrote
# "uygundur yayınla" and then "evet onaylıyorum" was sent to the LLM both times, saw the
# same preview come back, and had to say "evet" a third time before anything published -
# and the extra LLM pass rewrote the description nobody asked it to touch.
#
# So a message also counts as a command when EVERY word in it is a confirmation word (or
# every word a cancellation word). "evet onaylıyorum" and "uygundur yayınla" publish;
# "evet ama fiyatı değiştir" still does not, because "ama" belongs to neither set.
#
# These are whole words, never prefixes. Turkish negation is a suffix, so matching
# "yayınla" as a prefix would also match "yayınlama" - and publish a refusal to publish.
_CONFIRM_WORDS = {
    "evet", "evt", "tabi", "tabii", "aynen", "kesinlikle", "elbette", "lütfen", "lutfen",
    "onay", "onayla", "onaylıyorum", "onayliyorum", "onaylayalım", "onaylayalim",
    "onaydır", "onaydir", "kabul", "ediyorum", "edelim",
    "yayınla", "yayinla", "yayınlayalım", "yayinlayalim", "yayına", "yayina", "al",
    "tamam", "tamamdır", "tamamdir", "tmm", "olur", "olsun", "peki", "hadi", "oldu",
    "uygun", "uygundur", "güzel", "guzel", "harika", "süper", "super", "devam",
}
_CANCEL_WORDS = {
    "iptal", "et", "edelim", "vazgeç", "vazgec", "vazgeçtim", "vazgectim",
    "hayır", "hayir", "yok", "olmaz", "istemiyorum", "dur", "boş", "bos", "ver",
    "şimdilik", "simdilik", "olmasın", "olmasin", "gerek", "kalsın", "kalsin",
}


def _match_fsm_command(normalized_cmd: str) -> Optional[str]:
    """Read a confirmation or cancellation out of a whole message.

    Exact phrases win first; otherwise the message counts only if every one of its words
    belongs to the same set, which is what keeps "evet ama fiyatı değiştir" out.
    """
    exact = FSM_COMMANDS.get(normalized_cmd)
    if exact:
        return exact

    words = [w for w in (normalized_cmd or "").split() if w]
    if not words or len(words) > 4:
        return None
    if all(w in _CONFIRM_WORDS for w in words):
        return "CONFIRM"
    if all(w in _CANCEL_WORDS for w in words):
        return "CANCEL"
    return None

# FSM Edit commands (deterministic, LLM bypassed)
EDIT_FIELD_MAP = {
    "başlık": "title",
    "başlığı": "title",
    "başlığını": "title",
    "baslik": "title",
    "basligi": "title",
    "basligini": "title",
    "title": "title",
    "açıklama": "description",
    "açıklamayı": "description",
    "açıklamasını": "description",
    "aciklama": "description",
    "aciklamayi": "description",
    "aciklamasini": "description",
    "description": "description",
    "fiyat": "price",
    "fiyatı": "price",
    "fiyatini": "price",
    "fiyati": "price",
    "price": "price",
    "durum": "condition",
    "durumu": "condition",
    "condition": "condition",
    "lokasyon": "location",
    "lokasyonu": "location",
    "konum": "location",
    "konumu": "location",
    "location": "location",
    "kategori": "category",
    "kategoriyi": "category",
    "category": "category",
}

_EDIT_FIELD_PATTERN = "|".join(
    sorted((re.escape(key) for key in EDIT_FIELD_MAP), key=len, reverse=True)
)

_EDIT_TRAILING_DIRECTIVE_PATTERNS = [
    r"\s+(?:olarak\s+)?(?:değiştir|degistir|güncelle|guncelle|ayarla|belirle|olsun)\s*$",
    r"\s+yap\s*$",
]

_EDIT_LEADING_CONTEXT_PATTERNS = [
    re.compile(
        r"^\s*(?:lütfen\s+)?(?:(?:açıklamayı|aciklamayi|açıklama|aciklama|ilanı|ilani|metni|ilan metnini)\s+(?:düzelt|duzelt|güncelle|guncelle|değiştir|degistir|yenile)\s+)+",
        flags=re.IGNORECASE,
    ),
]

_ENRICHMENT_ONLY_VALUE_HINTS = [
    "daha iyi", "daha guzel", "guzel", "profesyonel", "yeniden",
    "kisalt", "uzat", "parlat", "sade", "akici", "detayli",
    "duzelt", "iyilestir", "gelistir",
]


def _strip_trailing_edit_directives(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""

    for pattern in _EDIT_TRAILING_DIRECTIVE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)

    return cleaned.strip().strip("`'\"")


def _strip_leading_edit_context(part: str) -> str:
    cleaned = (part or "").strip()
    for pattern in _EDIT_LEADING_CONTEXT_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip(" ,")


def _looks_like_enrichment_only_value(field: str, value: str) -> bool:
    if field not in {"title", "description"}:
        return False

    normalized = _normalize_intent_text(value)
    if not normalized:
        return True

    return len(normalized.split()) <= 6 and any(hint in normalized for hint in _ENRICHMENT_ONLY_VALUE_HINTS)

CONDITION_ALIASES = {
    "sıfır": "Sıfır",
    "sifir": "Sıfır",
    "az kullanılmış": "Az Kullanılmış",
    "az kullanilmis": "Az Kullanılmış",
    "az kullanilmis": "Az Kullanılmış",
    "2. el": "2. El",
    "2.el": "2. El",
    "2 el": "2. El",
    "2el": "2. El",
    "ikinci el": "2. El",
}


def _parse_price_value(raw_value: str) -> Optional[int]:
    cleaned = re.sub(r"[^0-9]", "", raw_value or "")
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_condition_value(raw_value: str) -> Optional[str]:
    raw = (raw_value or "").strip()
    if not raw:
        return None

    canonical = canonicalize_condition(raw)
    if canonical:
        return canonical

    normalized = normalize_for_match(raw)
    if normalized in CONDITION_ALIASES:
        return CONDITION_ALIASES[normalized]

    # Allow exact matches if user already typed a valid condition
    for allowed in FSMEngine.ALLOWED_CONDITIONS:
        if normalized == normalize_for_match(allowed):
            return allowed

    return None


def _parse_edit_updates(message: str) -> tuple[Dict[str, Any], List[str]]:
    updates: Dict[str, Any] = {}
    errors: List[str] = []

    if not message:
        return updates, errors

    # Split by lines or semicolons for multi-field edits
    parts = re.split(r"[\n;]+", message)
    for part in parts:
        part = _strip_leading_edit_context(part)
        key = ""
        value = ""
        used_compact_pattern = False

        if ":" in part:
            raw_key, raw_value = part.split(":", 1)
            key = raw_key.strip().lower()
            value = (raw_value or "").strip()
        else:
            # Support compact edits like "konum kadikoy" or "fiyat 12500"
            compact = re.match(rf"^\s*(?P<field>{_EDIT_FIELD_PATTERN})\s+(?P<value>.+?)\s*$", part, flags=re.IGNORECASE)
            if compact:
                key = compact.group("field").strip().lower()
                value = _strip_trailing_edit_directives(compact.group("value"))
                used_compact_pattern = True

        if not value:
            continue

        field = EDIT_FIELD_MAP.get(key)
        if not field:
            continue

        if used_compact_pattern and _looks_like_enrichment_only_value(field, value):
            continue

        if field == "price":
            parsed_price = _parse_price_value(value)
            if parsed_price is None:
                errors.append("fiyat")
            else:
                updates[field] = parsed_price
        elif field == "condition":
            parsed_condition = _parse_condition_value(value)
            if parsed_condition is None:
                errors.append("durum")
            else:
                updates[field] = parsed_condition
        elif field == "category":
            updates[field] = value
            # Category is normally derived from the title and description, and "Diğer" is
            # the "nothing matched" fallback that validate() re-runs past. Someone who
            # typed the category themselves means it, including "Diğer", so mark it as
            # settled and stop re-deriving it underneath them.
            updates["category_locked"] = True
        else:
            updates[field] = value

    return updates, errors


# What the bridge sends in place of an empty body on a media-only WhatsApp message.
# Kept normalised (normalize_for_match strips Turkish diacritics) to match either spelling.
_MEDIA_ONLY_PLACEHOLDERS = {
    "fotograf gonderdim",
    "resim gonderdim",
    "gorsel gonderdim",
}


def _apply_structured_prefill_to_listing(
    listing_data: Dict[str, Any],
    prefill: Dict[str, Any],
    user_text: str = "",
) -> bool:
    """Merge vision-derived prefill into the draft, without outranking the seller.

    The prefill is a guess made from a photo, and it is applied before the Brain has read
    the message. So when someone sent photos of their Jetta along with the words
    "2011 model Jetta", a misread of the first photo ("Toyota Corolla") became the title,
    and every later turn treated that as established fact.

    A guess may fill a gap; it may never contradict what the seller actually typed.
    """
    changed = False

    # Did the seller actually describe the item, or is this a photo-only message?
    # The bridge substitutes a fixed placeholder when a WhatsApp message carries media
    # and no text, and that placeholder must not be mistaken for a description - it is
    # exactly the case where the photo guess is the only thing we have.
    normalized_text = normalize_for_match(user_text or "").strip()
    user_described_item = (
        normalized_text not in _MEDIA_ONLY_PLACEHOLDERS and len(normalized_text) >= 15
    )

    raw_title = prefill.get("title")
    if isinstance(raw_title, str):
        title = raw_title.strip()
        existing_title = str(listing_data.get("title") or "").strip()
        if title and len(existing_title) < 3 and not user_described_item:
            listing_data["title"] = title[:200]
            changed = True

    raw_condition = prefill.get("condition")
    if isinstance(raw_condition, str) and raw_condition.strip():
        parsed_condition = _parse_condition_value(raw_condition)
        if parsed_condition:
            existing_condition = str(listing_data.get("condition") or "").strip()
            if not existing_condition or existing_condition == "2. El":
                listing_data["condition"] = parsed_condition
                changed = True

    raw_desc = prefill.get("description_start") or prefill.get("description")
    if isinstance(raw_desc, str):
        desc_seed, _ = _sanitize_ai_generated_description(raw_desc.strip(), confirmed_claims=set())
        if desc_seed:
            existing_desc = str(listing_data.get("description") or "").strip()
            if not existing_desc:
                listing_data["description"] = desc_seed[:2000]
                changed = True
            elif desc_seed.lower() not in existing_desc.lower():
                listing_data["description"] = f"{existing_desc}\n\n{desc_seed}"[:2000]
                changed = True

    return changed


def _should_try_direct_edit(message: str) -> bool:
    """Return True only when user message likely carries direct field update intent."""
    if not message:
        return False

    lower = message.lower().strip()
    # Keep price-research requests away from deterministic field parser.
    if re.search(r"fiyat\s*(araştır|arastir|öğren|ogren)|kaç\s*para|piyasa\s*değeri|fiyat[ıi]\s*ne\s*kadar", lower, flags=re.IGNORECASE):
        return False

    updates, errors = _parse_edit_updates(message)
    if updates or errors:
        return True

    if ":" in lower:
        return True

    return False


async def _apply_drafting_edit_request(user_id: str, channel: str, session: Dict, message: str) -> Optional[MessageResponse]:
    """Apply deterministic field updates while drafting and keep session/listing in sync."""
    if not _should_try_direct_edit(message):
        return None

    updates, errors = _parse_edit_updates(message)
    if errors:
        return MessageResponse(
            success=False,
            text=(
                "❌ Geçersiz değer girdiniz.\n\n"
                "Geçerli alanlar: başlık, açıklama, fiyat, durum, lokasyon.\n"
                "Örnek: `başlık: Yeni Başlık` veya `fiyat: 12500`"
            ),
            metadata={"error": "invalid_edit_value", "fields": errors},
        )

    if not updates:
        return None

    listing = session.get("listing_data", {})
    if not isinstance(listing, dict):
        listing = {}

    previous_listing = dict(listing)

    listing.update(updates)
    confirmed_claims = _collect_confirmed_description_claims(session, listing, message)
    if "description" in updates:
        listing["description"] = _cleanup_description_text(str(listing.get("description") or ""))[:2000]
        description_errors = _collect_description_validation_errors(listing, confirmed_claims)
        if description_errors:
            listing["description"] = previous_listing.get("description", "")
            return MessageResponse(
                success=False,
                text="❌ Açıklama güncellenemedi.\n\n" + "\n".join(f"- {error}" for error in description_errors),
                metadata={"error": "invalid_description", "description_errors": description_errors},
            )

    is_valid, missing = FSMEngine.validate(listing, confirmed_claims)
    description_errors = _collect_description_validation_errors(listing, confirmed_claims)

    session["listing_data"] = listing
    session["state"] = "READY" if is_valid else "DRAFTING"
    session["fsm_state"] = FSM_STATE_DRAFTING
    session["last_intent"] = "CREATE"
    session["draft_updated_at"] = datetime.utcnow().isoformat()
    if "description" in updates:
        _remember_description_claims(session, message, str(listing.get("description") or ""))
    await save_session(user_id, channel, session)

    text = f"✅ İlan bilgilerini güncelledim.\n\n{_format_preview(listing)}"
    if missing:
        text += f"\n\nEksik zorunlu alanlar: {', '.join(missing)}"
    else:
        text += "\n\nİlan hazır görünüyor. İsterseniz `yayınla` yazabilirsiniz."
    if description_errors:
        text += "\n\nAçıklama düzeltmeleri gerekli:\n" + "\n".join(f"- {error}" for error in description_errors)

    buttons = [
        ButtonResponse(text="✅ Yayınla", payload="yayınla") if is_valid else ButtonResponse(text="✏️ Devam Et", payload="devam"),
        ButtonResponse(text="❌ İptal", payload="iptal"),
    ]

    return MessageResponse(
        success=True,
        text=text,
        listing_preview=listing,
        buttons=buttons,
        metadata={
            "intent": "CREATE",
            "state": session["state"],
            "missing_fields": missing,
            "ready_for_publish": is_valid,
            "description_errors": description_errors,
            "update_source": "deterministic_edit",
        },
    )


async def _fsm_show_confirmation_preview(user_id: str, channel: str, session: Dict) -> MessageResponse:
    """FSM: Show detailed confirmation preview with credit info"""
    listing = session.get("listing_data", {})
    
    # FSM validate - kategori otomatik belirlensin!
    confirmed_claims = _collect_confirmed_description_claims(session, listing)
    is_valid, missing = FSMEngine.validate(listing, confirmed_claims)
    description_errors = _collect_description_validation_errors(listing, confirmed_claims)

    if not is_valid:
        text = "⚠️ İlan henüz yayınlanamaz."
        if missing:
            text += f"\n\nEksik/geçersiz alanlar: {', '.join(missing)}"
        if description_errors:
            text += "\n\nAçıklama sorunları:\n" + "\n".join(f"- {error}" for error in description_errors)
        text += f"\n\n{_format_preview(listing, show_full_description=True)}"
        return MessageResponse(
            success=True,
            text=text,
            buttons=[
                ButtonResponse(text="✏️ Düzenle", payload="düzenlemek istiyorum"),
                ButtonResponse(text="❌ İptal", payload="iptal"),
            ],
            metadata={
                "intent": "CREATE",
                "state": "DRAFTING",
                "missing_fields": missing,
                "description_errors": description_errors,
            },
        )
    
    # Get user balance - use sync client
    try:
        result = (
            supabase_client.client
            .table("wallets")
            .select("balance_bigint, free_unlimited_until")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        row = result.data[0] if result.data else {}
        balance = float(row.get("balance_bigint") or 0)

        promo_until = row.get("free_unlimited_until")
        try:
            if promo_until:
                from datetime import datetime, timezone
                dt = promo_until
                if isinstance(dt, str):
                    dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                if isinstance(dt, datetime) and dt > datetime.now(timezone.utc):
                    balance = float(10**12)
        except Exception:
            pass

        logger.info(f"FSM: Wallet balance for {user_id}: {balance} (raw: {result.data})")
    except Exception as e:
        logger.error(f"FSM: Failed to get wallet balance for {user_id}: {e}")
        balance = 0
    
    # Same source of truth as FSMEngine.publish, so the preview cannot promise a
    # different price than the one actually charged.
    credit_cost = int(getattr(settings, "listing_credit_cost", 55) or 55)
    
    # Kategori gösterimi (FSM tarafından belirlendi)
    category_display = listing.get('category', 'Diğer')
    if category_display in ["Sistem", "Otomatik", ""]:
        category_display = "Diğer"
    
    # Format detailed preview
    try:
        from services.text_normalization import sentence_case_tr
    except Exception:
        sentence_case_tr = lambda s: s

    # Last screen before money moves, so it stays explicit about the charge - but it
    # reads like a question, not a control panel. The old version printed a boxed
    # "YAYIN ONCESI KONTROL" form and then taught the reader command syntax
    # ("Onayla: `onayla`", "baslik: Yeni Baslik"), which is most of what made the
    # whole flow feel like software rather than an assistant.
    photo_count = len(_filter_valid_images(listing.get('images')))
    photo_note = f"{photo_count} fotoğraf" if photo_count else "fotoğrafsız"

    money_line = (
        f"Yayınlamak {credit_cost} kredi düşürecek; {balance:,.0f} krediniz var."
        if balance >= credit_cost
        else f"Yayın ücreti {credit_cost} kredi ama bakiyeniz {balance:,.0f}. Yüklemeden yayınlayamıyorum."
    )

    closing = (
        "Onaylıyor musunuz? Değiştirmek istediğiniz bir şey varsa da söyleyin."
        if balance >= credit_cost
        else "Kredi yükledikten sonra kaldığımız yerden devam edebiliriz."
    )

    preview = (
        f"**{sentence_case_tr(listing.get('title') or '—')}**\n"
        f"{listing.get('price', 0):,.0f} ₺ · {sentence_case_tr(listing.get('location') or 'Konum belirtilmemiş')} · "
        f"{listing.get('condition') or '2. El'} · {category_display} · {photo_note}\n\n"
        f"{sentence_case_tr(listing.get('description') or '—')}\n\n"
        f"{money_line}\n\n"
        f"{closing}"
    )

    # Update session state with auto-categorized listing
    session["listing_data"] = listing  # Save with auto-category
    session["fsm_state"] = FSM_STATE_PENDING_CONFIRMATION
    session["pending_publish_balance"] = balance
    session["pending_publish_cost"] = credit_cost
    
    # DEBUG: Log that we're setting PENDING_CONFIRMATION state
    logger.info(f"FSM: Setting fsm_state=PENDING_CONFIRMATION for user={user_id}")
    
    await save_session(user_id, channel, session)
    
    buttons = []
    if balance >= credit_cost:
        buttons = [
            ButtonResponse(text="✅ Onayla", payload="onayla"),
            ButtonResponse(text="❌ İptal", payload="iptal"),
        ]
    else:
        buttons = [
            ButtonResponse(text="💳 Kredi Yükle", payload="kredi yükle"),
            ButtonResponse(text="❌ İptal", payload="iptal"),
        ]
    
    return MessageResponse(
        success=True,
        text=preview,
        buttons=buttons,
        metadata={
            "intent": "PENDING_CONFIRMATION",
            "fsm_state": FSM_STATE_PENDING_CONFIRMATION,
            "balance": balance,
            "cost": credit_cost,
        },
    )


async def _fsm_handle_confirmation(user_id: str, channel: str, session: Dict, command: str) -> MessageResponse:
    """FSM: Handle deterministic commands in PENDING_CONFIRMATION state"""
    
    if command == "CONFIRM":
        # Direct publish - no LLM involved
        listing = session.get("listing_data", {})
        cost = session.get("pending_publish_cost", 55)
        confirmed_claims = _collect_confirmed_description_claims(session, listing)
        description_errors = _collect_description_validation_errors(listing, confirmed_claims)
        is_valid, missing = FSMEngine.validate(listing, confirmed_claims)

        if not is_valid:
            text = "❌ İlan yayınlanamaz."
            if missing:
                text += f"\n\nEksik/geçersiz alanlar: {', '.join(missing)}"
            if description_errors:
                text += "\n\nAçıklama sorunları:\n" + "\n".join(f"- {error}" for error in description_errors)
            return MessageResponse(
                success=False,
                text=text,
                buttons=[ButtonResponse(text="✏️ Düzenle", payload="düzenlemek istiyorum")],
                metadata={
                    "error": "listing_not_publishable",
                    "missing_fields": missing,
                    "description_errors": description_errors,
                },
            )
        
        # Get FRESH balance from DB (not from session - may be stale)
        try:
            result = (
                supabase_client.client
                .table("wallets")
                .select("balance_bigint, free_unlimited_until")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            row = result.data[0] if result.data else {}
            balance = float(row.get("balance_bigint") or 0)

            promo_until = row.get("free_unlimited_until")
            try:
                if promo_until and cost > 0:
                    from datetime import datetime, timezone
                    dt = promo_until
                    if isinstance(dt, str):
                        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                    if isinstance(dt, datetime) and dt > datetime.now(timezone.utc):
                        balance = float(10**12)
            except Exception:
                pass

            logger.info(f"FSM CONFIRM: Fresh balance for {user_id}: {balance}")
        except Exception as e:
            logger.error(f"FSM CONFIRM: Failed to get balance: {e}")
            balance = 0
        
        if balance < cost:
            return MessageResponse(
                success=False,
                text=f"❌ Bakiye yetersiz!\n\nMevcut: {balance:,.0f} kredi\nGerekli: {cost} kredi\n\nKredi yükleyip tekrar deneyin.",
                buttons=[ButtonResponse(text="💳 Kredi Yükle", payload="kredi yükle")],
                metadata={"error": "insufficient_balance"},
            )
        
        # Publish
        success, message, listing_id = await FSMEngine.publish(user_id, listing, confirmed_claims)
        
        if success:
            # Clear session
            await clear_session(user_id, channel)
            
            return MessageResponse(
                success=True,
                text=f"""🎉 **İlanınız Yayınlandı!**

📋 **{listing.get('title')}**
💰 {listing.get('price', 0):,.0f} TL
📍 {listing.get('location', 'Belirtilmemiş')}

💳 Kalan bakiye: {balance - cost:,.0f} kredi

🔗 pazarglobal.com/listing/{listing_id}""",
                buttons=[
                    ButtonResponse(text="📸 Yeni İlan", payload="yeni ilan vermek istiyorum"),
                    ButtonResponse(text="📋 İlanlarım", payload="ilanlarım"),
                ],
                metadata={"intent": "PUBLISHED", "listing_id": listing_id},
            )
        else:
            return MessageResponse(
                success=False,
                text=f"❌ Yayınlama hatası: {message}",
                buttons=[ButtonResponse(text="🔄 Tekrar Dene", payload="onayla")],
                metadata={"error": message},
            )
    
    elif command == "CANCEL":
        # Reset to drafting
        session["fsm_state"] = FSM_STATE_DRAFTING
        await save_session(user_id, channel, session)
        
        return MessageResponse(
            success=True,
            text="❌ Yayınlama iptal edildi.\n\nİlanınız taslak olarak saklandı. Düzenleme yapabilir veya daha sonra yayınlayabilirsiniz.",
            buttons=[
                ButtonResponse(text="✏️ Düzenle", payload="düzenlemek istiyorum"),
                ButtonResponse(text="🗑️ Taslağı Sil", payload="taslağı sil"),
                ButtonResponse(text="📋 Önizle", payload="önizleme göster"),
            ],
            metadata={"intent": "CANCEL", "fsm_state": FSM_STATE_DRAFTING},
        )
    
    return MessageResponse(
        success=False,
        text="Geçersiz komut. 'onayla' veya 'iptal' yazın.",
    )


async def _fsm_handle_edit_request(user_id: str, channel: str, session: Dict, message: str) -> MessageResponse:
    """FSM: Handle edit requests in PENDING_CONFIRMATION state without LLM"""
    updates, errors = _parse_edit_updates(message)

    if errors:
        return MessageResponse(
            success=False,
            text=(
                "❌ Geçersiz değer girdiniz.\n\n"
                "Geçerli alanlar: başlık, açıklama, fiyat, durum, lokasyon.\n"
                "Örnek: `başlık: Yeni Başlık` veya `fiyat: 12500`"
            ),
            metadata={"error": "invalid_edit_value", "fields": errors},
        )

    if not updates:
        return MessageResponse(
            success=True,
            text=(
                "Düzenlemek için şu formatı kullanın:\n"
                "`başlık: Yeni Başlık`\n"
                "`açıklama: Yeni açıklama`\n"
                "`fiyat: 12500`\n"
                "`durum: Sıfır | Az Kullanılmış | 2. El`\n"
                "`lokasyon: Kadıköy, İstanbul`\n\n"
                "Onaylamak için **onayla**, iptal için **iptal** yazın."
            ),
            metadata={"intent": "PENDING_CONFIRMATION"},
        )

    listing = session.get("listing_data", {})
    if not isinstance(listing, dict):
        listing = {}

    previous_listing = dict(listing)
    listing.update(updates)

    confirmed_claims = _collect_confirmed_description_claims(session, listing, message)
    if "description" in updates:
        listing["description"] = _cleanup_description_text(str(listing.get("description") or ""))[:2000]
        description_errors = _collect_description_validation_errors(listing, confirmed_claims)
        if description_errors:
            listing["description"] = previous_listing.get("description", "")
            return MessageResponse(
                success=False,
                text="❌ Açıklama güncellenemedi.\n\n" + "\n".join(f"- {error}" for error in description_errors),
                metadata={"error": "invalid_description", "description_errors": description_errors},
            )

    session["listing_data"] = listing
    session["draft_updated_at"] = datetime.utcnow().isoformat()
    if "description" in updates:
        _remember_description_claims(session, message, str(listing.get("description") or ""))

    return await _fsm_show_confirmation_preview(user_id, channel, session)


async def _apply_description_removal_request(user_id: str, channel: str, session: Dict, message: str) -> Optional[MessageResponse]:
    phrase = _extract_description_removal_phrase(message)
    if not phrase:
        return None

    removal_targets = _split_description_removal_targets(phrase) or [phrase]

    listing = session.get("listing_data", {})
    if not isinstance(listing, dict):
        listing = {}

    description = str(listing.get("description") or "").strip()
    if not description:
        return MessageResponse(
            success=True,
            text="Açıklamada düzenlenecek bir metin yok.",
            metadata={"intent": "CREATE", "edit_intent": "remove_description_text"},
        )

    updated_description = description
    removed_targets: List[str] = []
    removed_count = 0
    for target in removal_targets:
        pattern = _build_description_removal_pattern(target)
        updated_description, target_count = pattern.subn(" ", updated_description)
        if target_count:
            removed_targets.append(target)
            removed_count += target_count

    if removed_count == 0:
        return MessageResponse(
            success=True,
            text=f"Açıklamada '{phrase}' ifadesini bulamadım.",
            metadata={"intent": "CREATE", "edit_intent": "remove_description_text", "matched": False},
        )

    listing["description"] = _cleanup_description_text(updated_description)[:2000]
    confirmed_claims = _collect_confirmed_description_claims(session, listing)
    is_valid, missing = FSMEngine.validate(listing, confirmed_claims)
    description_errors = _collect_description_validation_errors(listing, confirmed_claims)

    session["listing_data"] = listing
    session["state"] = "READY" if is_valid else "DRAFTING"
    session["fsm_state"] = FSM_STATE_DRAFTING
    session["last_intent"] = "CREATE"
    session["draft_updated_at"] = datetime.utcnow().isoformat()
    _remove_description_claims(session, *removed_targets)
    await save_session(user_id, channel, session)

    removed_label = ", ".join(f"'{target}'" for target in removed_targets) if removed_targets else f"'{phrase}'"
    text = f"✅ Açıklamadan {removed_label} ifadesini kaldırdım.\n\n{_format_preview(listing, show_full_description=True)}"
    if description_errors:
        text += "\n\nAçıklama düzeltmeleri gerekli:\n" + "\n".join(f"- {error}" for error in description_errors)
    elif missing:
        text += f"\n\nEksik/geçersiz alanlar: {', '.join(missing)}"

    return MessageResponse(
        success=True,
        text=text,
        listing_preview=listing,
        buttons=[
            ButtonResponse(text="✅ Yayınla", payload="yayınla") if is_valid else ButtonResponse(text="✏️ Devam Et", payload="devam"),
            ButtonResponse(text="❌ İptal", payload="iptal"),
        ],
        metadata={
            "intent": "CREATE",
            "edit_intent": "remove_description_text",
            "missing_fields": missing,
            "description_errors": description_errors,
            "ready_for_publish": is_valid,
        },
    )


# ═══════════════════════════════════════════════════════════════════
# MAIN ENDPOINT
# ═══════════════════════════════════════════════════════════════════

@router.post("/message", response_model=MessageResponse)
async def handle_message(
    request: MessageRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    internal_secret: Optional[str] = Header(default=None, alias="X-Internal-Secret"),
) -> MessageResponse:
    """
    V3 Gateway - LLM Brain + FSM Engine
    
    Security:
    - webchat: JWT token required (Authorization header)
    - whatsapp: user_id from Edge Function (already PIN verified)
    """
    logger.info(f"V3: user={request.user_id}, channel={request.channel}, msg={request.message[:50]}...")
    
    try:
        # ═══════════════════════════════════════════════════════════════════
        # 0. SECURITY: Verify user_id based on channel
        # ═══════════════════════════════════════════════════════════════════
        # NOTE: request.channel is client-controlled. The "whatsapp" value skips JWT
        # verification, so it is only trusted when the shared internal secret proves the
        # request came from our Edge traffic controller.
        is_valid, verified_user_id, auth_error = await get_user_id_from_request(
            authorization=authorization,
            request_user_id=request.user_id,
            channel=request.channel,
            internal_secret=internal_secret,
        )
        
        if not is_valid:
            logger.warning(f"Auth failed: {auth_error}")
            return MessageResponse(
                success=False,
                text=f"🔒 Kimlik doğrulama hatası: {auth_error}",
                error=auth_error,
                metadata={"auth_error": True}
            )
        
        # Use verified user_id for all operations
        user_id = verified_user_id
        logger.info(f"✅ Auth verified: user_id={user_id}")
        
        # 1. Load session (channel scoped)
        session = await load_session(user_id, request.channel)

        # Record what the user said before anything else looks at it. This archive is the
        # provenance source for the description guard, so it has to capture every turn
        # regardless of which branch below handles the message.
        _remember_user_statement(session, request.message or "")
        await save_session(user_id, request.channel, session)

        # Ensure created_via is tracked for consistent metadata
        if request.channel in ("webchat", "whatsapp"):
            listing_data = session.get("listing_data", {})
            if isinstance(listing_data, dict) and not listing_data.get("created_via"):
                listing_data["created_via"] = request.channel
                session["listing_data"] = listing_data
                await save_session(user_id, request.channel, session)

        # 0.85 Draft TTL check - MUST run before the prefill and media steps below.
        #
        # Those two steps both stamp draft_updated_at with the current time. While the
        # check sat after them, every arriving photo refreshed the very timestamp it was
        # about to be judged by, so an abandoned draft could never go stale: an hour-old
        # draft was revived by the message that was starting a new listing, and its images
        # and category came along. A Jetta went out carrying a photo from an earlier,
        # unrelated draft and stayed in that draft's "Diğer" category.
        def _reset_draft() -> None:
            """Abandon the current draft. The provenance archive belongs to that draft,
            so it has to reset with it - otherwise claims from a previous product would
            authorise details in the next one."""
            session["listing_data"] = {}
            session["state"] = "IDLE"
            session["fsm_state"] = FSM_STATE_IDLE
            session["draft_updated_at"] = None
            session["last_intent"] = None
            session[_SESSION_DESCRIPTION_CLAIMS_KEY] = []
            # Keep the current turn's message: the user is starting a new draft with it.
            session[_SESSION_USER_STATEMENTS_KEY] = _get_user_statements(session)[-1:]

        draft_updated_at = session.get("draft_updated_at")
        if draft_updated_at:
            try:
                last_ts = datetime.fromisoformat(draft_updated_at)
                if (datetime.utcnow() - last_ts).total_seconds() > 600:
                    logger.info("Draft expired (>10 min) - starting clean before prefill/media")
                    _reset_draft()
                    await save_session(user_id, request.channel, session)
            except Exception:
                # If parsing fails, reset draft defensively
                _reset_draft()
                await save_session(user_id, request.channel, session)

        # 0.9 Structured prefill injection (channel-agnostic, currently fed by WhatsApp vision bridge)
        if isinstance(request.prefill_listing_data, dict) and request.prefill_listing_data:
            listing_data = session.get("listing_data", {})
            if not isinstance(listing_data, dict):
                listing_data = {}

            if _apply_structured_prefill_to_listing(
                listing_data, request.prefill_listing_data, request.message or ""
            ):
                session["listing_data"] = listing_data
                session["state"] = "DRAFTING"
                session["fsm_state"] = FSM_STATE_DRAFTING
                session["last_intent"] = "CREATE"
                session["draft_updated_at"] = datetime.utcnow().isoformat()
                await save_session(user_id, request.channel, session)
                logger.info("Applied structured prefill to listing_data before routing")

        # 1.0 Attach WhatsApp media paths to listing_data (if provided)
        incoming_images: List[str] = []
        if request.media_urls:
            normalized_media = _normalize_media_urls(request.media_urls)
            if normalized_media:
                media_safety = await vision_safety_gate.check_media(normalized_media, user_id=user_id)
                if not media_safety.get("safe", False):
                    return MessageResponse(
                        success=False,
                        text=f"🚫 {media_safety.get('block_reason') or 'Görsel güvenlik politikalarına uygun değil.'}",
                        metadata={
                            "intent": "SAFETY_BLOCK",
                            "flagged_categories": media_safety.get("flagged_categories", []),
                        },
                    )

                for url in normalized_media[:3]:
                    analysis = await vision_service.analyze_product(url)
                    prohibited_term = vision_service.detect_prohibited_product(analysis)
                    if prohibited_term:
                        return MessageResponse(
                            success=False,
                            text="🚫 Bu görselde platformda yayınlanmasına izin verilmeyen bir ürün tespit edildi.",
                            metadata={
                                "intent": "SAFETY_BLOCK",
                                "flagged_categories": ["illicit_item", prohibited_term],
                            },
                        )

                incoming_images = normalized_media
                listing_data = session.get("listing_data", {})
                existing_images = _filter_valid_images(listing_data.get("images", []))
                for url in normalized_media:
                    if url not in existing_images:
                        existing_images.append(url)
                listing_data["images"] = existing_images[:5]
                session["listing_data"] = listing_data
                session["draft_updated_at"] = datetime.utcnow().isoformat()
                await save_session(user_id, request.channel, session)

        # 1.1 Draft TTL is checked at 0.85, before prefill and media touch the timestamp.

        # ═══════════════════════════════════════════════════════════════════
        # 1.2 FSM STATE CHECK - LLM BYPASS for deterministic commands
        # ═══════════════════════════════════════════════════════════════════
        lower_msg = (request.message or "").lower().strip()
        normalized_cmd = re.sub(r"[^\wşğıöçü]+", " ", lower_msg, flags=re.UNICODE).strip()
        fsm_state = session.get("fsm_state", FSM_STATE_IDLE)
        draft_message_intent = _classify_draft_message_intent(request.message or "")
        
        # DEBUG: Log FSM state for troubleshooting
        logger.info(f"FSM state check: fsm_state={fsm_state}, msg={lower_msg}")
        
        if fsm_state == FSM_STATE_PENDING_CONFIRMATION:
            # In confirmation state - check for deterministic commands
            fsm_command = _match_fsm_command(normalized_cmd)
            
            if fsm_command:
                # Deterministic command - LLM BYPASSED
                logger.info(f"FSM: PENDING_CONFIRMATION state, command={fsm_command}, bypassing LLM")
                return await _fsm_handle_confirmation(user_id, request.channel, session, fsm_command)

            if draft_message_intent == "remove_description_text":
                logger.info("FSM: PENDING_CONFIRMATION state, description removal detected, bypassing LLM")
                removal_response = await _apply_description_removal_request(
                    user_id,
                    request.channel,
                    session,
                    request.message or "",
                )
                if removal_response is not None:
                    return removal_response

            # Try deterministic field edit handling first (e.g. "konum: Kadikoy" / "konum Kadikoy")
            if draft_message_intent == "field_update":
                logger.info("FSM: PENDING_CONFIRMATION state, deterministic edit detected, bypassing LLM")
                return await _fsm_handle_edit_request(user_id, request.channel, session, request.message or "")

            # Copy improvement requests ("daha güzel yaz") deliberately fall through to
            # the LLM now: it carries the category writing profile and composes listing
            # copy by default, so intercepting these with a regex produced a canned reply
            # from a second, less informed prompt.

            # Not a command or deterministic edit - allow free-form edits via LLM
            session["fsm_state"] = FSM_STATE_DRAFTING
            await save_session(user_id, request.channel, session)
            logger.info("FSM: PENDING_CONFIRMATION state, non-command received, routing to LLM for edits")
        
        # 1.3 Drafting-stage deterministic edit updates (LLM bypass)
        if session.get("listing_data") and fsm_state in {FSM_STATE_DRAFTING, FSM_STATE_IDLE}:
            if draft_message_intent == "remove_description_text":
                deterministic_remove_response = await _apply_description_removal_request(
                    user_id,
                    request.channel,
                    session,
                    request.message or "",
                )
                if deterministic_remove_response is not None:
                    logger.info("FSM: DRAFTING deterministic description removal handled before LLM")
                    return deterministic_remove_response

            # Copy improvement requests fall through to the LLM (see 1.2 above).

            deterministic_edit_response = await _apply_drafting_edit_request(
                user_id,
                request.channel,
                session,
                request.message or "",
            )
            if deterministic_edit_response is not None:
                logger.info("FSM: DRAFTING deterministic edit handled before LLM")
                return deterministic_edit_response

        # 1.4 Detail command handling (uses last search cache)
        detail_match = re.search(r"(\d+)\s*nolu\s*ilan", lower_msg)
        if detail_match and ("detay" in lower_msg or "goster" in lower_msg or "göster" in lower_msg):
            idx = int(detail_match.group(1)) - 1
            search_cache = session.get("search_cache") or []
            if 0 <= idx < len(search_cache):
                return await _format_listing_detail_response(search_cache[idx])

        # 1.5 Pagination command handling (uses last search cache)
        if _is_show_more_command(lower_msg):
            search_cache = session.get("search_cache") or []
            if search_cache:
                cursor = int(session.get("search_cursor", 0) or 0)
                if cursor < len(search_cache):
                    page_text = await _format_search_continuation_page(search_cache, cursor, page_size=5)
                    session["search_cursor"] = min(cursor + 5, len(search_cache))
                    await save_session(user_id, request.channel, session)
                    return MessageResponse(
                        success=True,
                        text=page_text,
                        metadata={
                            "intent": "SEARCH",
                            "count": len(search_cache),
                            "shown_until": session.get("search_cursor", 0),
                        },
                    )

                return MessageResponse(
                    success=True,
                    text="📄 Tüm arama sonuçlarını zaten gösterdim. Yeni arama yapmak ister misiniz?",
                    metadata={"intent": "SEARCH", "count": len(search_cache)},
                )
        
        # 1.6 Preview/Son hal shortcut - skip LLM if user just wants to see current draft
        preview_keywords = ["son hal", "önizleme", "preview", "göster bana", "goster bana"]
        if any(kw in lower_msg for kw in preview_keywords) and session.get("listing_data"):
            current_listing = session.get("listing_data", {})
            if current_listing.get("title"):  # At least title exists
                preview = _format_preview(current_listing, show_full_description=True)
                return MessageResponse(
                    success=True,
                    text=f"{preview}\n\nİlanı yayınlamak için 'yayınla' yazabilirsiniz.",
                    buttons=[
                        ButtonResponse(text="✅ Yayınla", payload="yayınla"),
                        ButtonResponse(text="✏️ Düzenle", payload="düzenleme yapmak istiyorum"),
                    ],
                    metadata={"intent": "PREVIEW"},
                )
        
        # 2. Calculate context for Brain
        current_listing = session.get("listing_data", {})
        fsm_state = session.get("state", "IDLE")
        last_intent = session.get("last_intent")
        context_images = _filter_valid_images(current_listing.get("images", [])) or incoming_images
        
        # NOTE: Deterministik price detection kaldırıldı!
        # Artık Brain (LLM) native function calling ile Perplexity tool'unu çağırıyor.
        # LLM "kaç para eder" gibi sorguları algılayıp tool_call döndürüyor.
        
        # Pre-calculate missing fields
        _, missing_fields = FSMEngine.validate(current_listing) if current_listing else (False, FSMEngine.REQUIRED_FIELDS.copy())
        
        # 3. Call Brain with rich context
        brain_output = await brain.process(
            message=request.message,
            current_listing=current_listing,
            images=context_images,
            conversation_history=session.get("conversation_history"),
            # Zengin context
            fsm_state=fsm_state,
            missing_fields=missing_fields,
            last_intent=last_intent,
        )

        # Soft fallback: keep LLM-first, but recover missed flexible price requests.
        if (
            not brain_output.tool_call
            and brain_output.intent == Intent.CHAT
            and _looks_like_price_research_request(request.message)
        ):
            logger.info("Price research fallback detector triggered from CHAT message")
            brain_output.tool_call = {"name": "perplexity", "query": request.message}
        
        # 3.5. PERPLEXITY TOOL CALL - Handle BEFORE intent routing!
        # "kaç para eder" / "fiyat araştır" queries should call Perplexity regardless of intent
        if brain_output.tool_call and brain_output.tool_call.get("name") == "perplexity":
            query = brain_output.tool_call.get("query", request.message)
            logger.info(f"Perplexity tool call detected: query={query}")
            
            price_result = await _call_perplexity_with_response(query)
            
            # Save to session for future reference and preserve active draft workflow when present.
            has_active_draft = bool(session.get("listing_data"))
            session["last_intent"] = "CREATE" if has_active_draft else "CHAT"
            session["last_price_query"] = query
            if price_result.get("suggested_price"):
                session["last_suggested_price"] = price_result["suggested_price"]
            await save_session(user_id, request.channel, session)

            response_text = price_result["response"]
            buttons = [
                ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
                ButtonResponse(text="🔍 Ürün Ara", payload="aramak istiyorum"),
            ]
            if has_active_draft:
                response_text = f"{response_text}\n\n🧩 Taslağınız korunuyor. İsterseniz bilgileri güncellemeye devam edebilir veya `yayınla` yazabilirsiniz."
                buttons = [
                    ButtonResponse(text="✏️ İlana Devam Et", payload="devam"),
                    ButtonResponse(text="✅ Yayınla", payload="yayınla"),
                ]
            
            return MessageResponse(
                success=True,
                text=response_text,
                buttons=buttons,
                metadata={"intent": "PRICE_RESEARCH", "tool": "perplexity", "price": price_result.get("suggested_price")},
            )

        # 3.6. The hybrid enrichment bridge used to sit here, routing "başlığı iyileştir"
        # style messages to the ai-assistant Edge function. Those category writing rules
        # now live in config/category_profiles.py and are injected straight into this
        # Brain's prompt, so the detour just replaced a context-aware answer with a
        # context-free one.

        # 4. Handle by intent
        if brain_output.intent == Intent.CANCEL:
            # LLM override - reset
            await clear_session(user_id, request.channel)
            return MessageResponse(
                success=True,
                text=brain_output.response_text,
                buttons=[
                    ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
                    ButtonResponse(text="🔍 Ürün Ara", payload="aramak istiyorum"),
                ],
                metadata={"intent": "CANCEL"},
            )

        elif brain_output.intent == Intent.REPORT:
            return await _handle_report(user_id, request.channel, session, brain_output)
        
        elif brain_output.intent == Intent.SEARCH:
            # Save last intent
            session["last_intent"] = "SEARCH"
            await save_session(user_id, request.channel, session)
            return await _handle_search(user_id, request.channel, session, request.message)
        
        elif brain_output.intent == Intent.CREATE:
            return await _handle_create(user_id, request.channel, session, brain_output, request.message)
        
        else:  # CHAT
            # IMPORTANT: Check if user is trying to confirm an existing draft
            # Even if Brain says CHAT, if there's an active draft and user confirms, publish it!
            from core.brain import Guardrails
            if last_intent == "CREATE" and current_listing and Guardrails.detect_confirmation(request.message):
                logger.info(f"User confirming existing draft via CHAT intent, routing to CREATE handler")
                return await _handle_create(user_id, request.channel, session, brain_output, request.message)
            
            session["last_intent"] = "CHAT"
            return await _handle_chat(user_id, request.channel, session, brain_output)
    
    except Exception as e:
        error_text = _safe_exception_text(e)
        logger.error(f"V3 error: {error_text}", exc_info=True)
        return MessageResponse(
            success=False,
            text="⚠️ Bir hata oluştu. Lütfen tekrar deneyin.",
            error=error_text,
        )


# ═══════════════════════════════════════════════════════════════════
# INTENT HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def _handle_report(user_id: str, channel: str, session: Dict, brain_output: BrainOutput) -> MessageResponse:
    """
    REPORT intent — kullanıcı bir ilanı şikayet ediyor.
    illegal_reports tablosuna kayıt düşer.
    """
    report_data = brain_output.report_data or {}
    listing_id = report_data.get("listing_id")
    reason = report_data.get("reason") or "Belirtilmedi"

    if not listing_id:
        # listing_id bilinmiyor — kullanıcıdan iste
        return MessageResponse(
            success=True,
            text=(
                "📋 Şikayetinizi almak istiyorum. Lütfen şikayet etmek istediğiniz ilanın "
                "numarasını veya başlığını belirtin, ben de kaydedelim."
            ),
            metadata={"intent": "REPORT", "waiting_for": "listing_id"},
        )

    try:
        from tools.report_tool import report_illegal_listing_tool
        result = await report_illegal_listing_tool.execute(
            reporter_user_id=user_id,
            listing_id=listing_id,
            reason=reason,
        )
        if result.get("success"):
            text = (
                f"✅ Şikayetiniz kaydedildi. Ekibimiz en kısa sürede inceleyecek.\n\n"
                f"📌 **Şikayet sebebi:** {reason}"
            )
        else:
            text = "⚠️ Şikayet kaydedilirken bir sorun oluştu. Lütfen daha sonra tekrar deneyin."
    except Exception as e:
        logger.error(f"_handle_report hatası: {e}")
        text = "⚠️ Şikayet işlenirken hata oluştu."

    return MessageResponse(
        success=True,
        text=brain_output.response_text or text,
        metadata={"intent": "REPORT", "listing_id": listing_id},
    )


async def _handle_create(user_id: str, channel: str, session: Dict, brain_output: BrainOutput, user_message: str) -> MessageResponse:
    """
    CREATE intent - LLM'den JSON al, FSM'e gönder
    """
    # Merge new data with existing - preserve images from session!
    current = session.get("listing_data", {})
    if not isinstance(current, dict):
        current = {}
    previous_listing = dict(current)
    
    # First preserve existing images
    existing_images = current.get("images", [])
    
    # Merge Brain output
    for key, value in brain_output.listing_data.items():
        if value is not None:
            current[key] = value
    
    # Sanitize images and preserve existing ones if LLM overwrote with placeholders
    sanitized_images = _filter_valid_images(current.get("images", []))
    if sanitized_images:
        current["images"] = sanitized_images
    elif existing_images:
        fallback_images = _filter_valid_images(existing_images)
        if fallback_images:
            current["images"] = fallback_images

    confirmed_claims = _collect_confirmed_description_claims(session, current, user_message)
    description_errors = _apply_ai_description_guard(current, previous_listing, confirmed_claims)
    _remember_description_claims(session, user_message)

    # The seller asked for their number to appear in the listing text. Record the intent
    # on the draft; the number itself is only ever added at publish time, from the
    # verified profile, so a number pasted into chat cannot be published blind.
    if brain_output.include_phone_in_description:
        current["include_phone_in_description"] = True
    
    logger.info(f"CREATE: current listing data: {current}")
    logger.info(f"CREATE: user_confirmed={brain_output.user_confirmed}, ready_for_fsm={brain_output.ready_for_fsm}")
    
    # FSM validates
    is_valid, missing = FSMEngine.validate(current, confirmed_claims)
    
    # Update session
    session["listing_data"] = current
    session["state"] = "READY" if is_valid else "DRAFTING"
    session["fsm_state"] = FSM_STATE_DRAFTING  # Still in drafting until explicit confirmation flow
    session["last_intent"] = "CREATE"  # Brain'in context için bilmesi lazım
    session["draft_updated_at"] = datetime.utcnow().isoformat()
    
    # Add to history
    session.setdefault("conversation_history", []).append({
        "role": "user", "content": user_message
    })
    session["conversation_history"].append({
        "role": "assistant", "content": brain_output.response_text
    })
    
    await save_session(user_id, channel, session)
    
    # Handle Perplexity tool call
    response_text = (brain_output.response_text or "").strip()
    if brain_output.tool_call and brain_output.tool_call.get("name") == "perplexity":
        price_result = await _call_perplexity(brain_output.tool_call["query"])
        if price_result:
            response_text += f"\n\n💰 **Piyasa Araştırması:** {price_result:,.0f} TL civarı"

    # Safety: never return an empty assistant message.
    # Frontend commits assistant response only when `text` is non-empty.
    if not response_text:
        response_text = "✅ İlan bilgilerini güncelledim."

    # Preview is the server's job now, on every channel: the model used to be ordered to
    # print one in every single message, which is what made the flow read like a form.
    #
    # It is shown only when it carries new information - the draft is complete and looks
    # different from the last preview the user saw. While fields are still missing the
    # assistant just asks its one question, and repeated previews of an unchanged draft
    # are suppressed.
    preview_signature = _draft_signature(current)
    if (
        is_valid
        and current.get("title")
        and "📋" not in response_text
        and preview_signature != session.get("last_preview_signature")
    ):
        response_text = f"{response_text}\n\n{_format_preview(current)}"
        session["last_preview_signature"] = preview_signature
        await save_session(user_id, channel, session)
    if description_errors:
        response_text += "\n\nNot: Doğrulanmamış açıklama ifadeleri kaydedilmedi."

    # A value the seller wrote ambiguously ("800.000₺+") gets asked about rather than
    # guessed. If the model already worked the question into its reply, don't repeat it.
    ambiguities = brain_output.ambiguities or []
    for item in ambiguities:
        question = item.get("question", "").strip()
        if question and question not in response_text:
            response_text = f"{response_text}\n\n{question}".strip()

    # Check if user wants to publish - use direct confirmation detection on user message
    from core.brain import Guardrails
    user_wants_to_publish = Guardrails.detect_confirmation(user_message)

    # An unresolved ambiguity must not ride through into a published listing.
    if ambiguities and user_wants_to_publish:
        logger.info(f"CREATE: holding publish, unresolved ambiguities: {ambiguities}")
        user_wants_to_publish = False

    logger.info(f"CREATE: is_valid={is_valid}, user_wants_to_publish={user_wants_to_publish}")

    if user_wants_to_publish and is_valid:
        # ═══════════════════════════════════════════════════════════════════
        # NEW FSM FLOW: Show confirmation preview instead of direct publish
        # ═══════════════════════════════════════════════════════════════════
        logger.info(f"User wants to publish - showing FSM confirmation preview")
        return await _fsm_show_confirmation_preview(user_id, channel, session)
    
    elif user_wants_to_publish and not is_valid:
        # User wants to publish but listing is incomplete
        logger.info(f"User wants to publish but missing fields: {missing}")
        
        # Show what's missing
        missing_text = ", ".join(missing)
        return MessageResponse(
            success=True,
            text=f"⚠️ İlan yayınlamak için şu alanları tamamlamanız gerekiyor:\n\n**Eksik:** {missing_text}\n\n{response_text}",
            listing_preview=current,
            buttons=[ButtonResponse(text="❌ İptal", payload="iptal")],
            metadata={
                "intent": "CREATE",
                "state": "DRAFTING",
                "missing_fields": missing,
                "ready_for_publish": False,
                "description_errors": description_errors,
            },
        )
    
    # Not ready or not confirmed - show preview
    buttons = []
    if is_valid:
        buttons = [
            ButtonResponse(text="✅ Yayınla", payload="yayınla"),
            ButtonResponse(text="✏️ Düzenle", payload="düzenlemek istiyorum"),
            ButtonResponse(text="❌ İptal", payload="iptal"),
        ]
    else:
        buttons = [
            ButtonResponse(text="❌ İptal", payload="iptal"),
        ]
    
    return MessageResponse(
        success=True,
        text=response_text,
        listing_preview=current,
        buttons=buttons,
        metadata={
            "intent": "CREATE",
            "state": session["state"],
            "missing_fields": missing,
            "ready_for_publish": is_valid,
            "description_errors": description_errors,
            "suggestions": brain_output.suggestions,
            "ambiguities": ambiguities,
        },
    )


async def _handle_search(user_id: str, channel: str, session: Dict, query: str) -> MessageResponse:
    """SEARCH intent - SearchComposerAgent'a delege et"""
    try:
        from agents.search_agents import SearchComposerAgent
        search_agent = SearchComposerAgent()
        result = await search_agent.orchestrate_search(user_message=query)
        
        if isinstance(result, dict):
            message = result.get("message", "")
            listings = result.get("listings", [])
            listings_full = result.get("listings_full", []) if isinstance(result.get("listings_full", []), list) else []
            cache_list = listings_full or listings or []
            
            if message:
                session["search_cache"] = cache_list
                session["search_cursor"] = min(len(listings or []), len(cache_list))
                await save_session(user_id, channel, session)
                return MessageResponse(
                    success=True,
                    text=message,
                    metadata={"intent": "SEARCH", "count": result.get("count", len(cache_list))},
                )
            elif listings:
                session["search_cache"] = cache_list
                session["search_cursor"] = min(5, len(cache_list))
                await save_session(user_id, channel, session)
                results_text = f"🔍 **{len(listings)} sonuç bulundu:**\n\n"
                for i, listing in enumerate(listings[:5], 1):
                    title = prefix_example_listing_title(listing.get("title", "İsimsiz"), listing)
                    price = listing.get("price", 0)
                    results_text += f"{i}. **{title}**\n   💰 {price:,.0f} TL\n\n"
                    listing_id = str(listing.get("id") or "").strip()
                    if listing_id:
                        try:
                            token_row = await supabase_client.ensure_contact_token_for_listing(listing_id)
                            token = str((token_row or {}).get("token") or "").strip() if isinstance(token_row, dict) else ""
                            if token:
                                frontend_base = (getattr(settings, "frontend_base_url", None) or "https://pazarglobal.com").strip().rstrip("/")
                                results_text += f"   ✉️ Mesaj Gönder: {frontend_base}/contact/{token}\n\n"
                        except Exception as e:
                            logger.warning(f"Failed to attach contact link in search fallback (listing_id={listing_id}): {e}")
                
                return MessageResponse(
                    success=True,
                    text=results_text,
                    metadata={"intent": "SEARCH", "count": len(listings)},
                )
            else:
                session["search_cache"] = []
                session["search_cursor"] = 0
                await save_session(user_id, channel, session)
                return MessageResponse(
                    success=True,
                    text="🔍 Aramanıza uygun ilan bulunamadı. Farklı kelimelerle deneyin.",
                    buttons=[ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum")],
                    metadata={"intent": "SEARCH", "count": 0},
                )
        
        return MessageResponse(
            success=True,
            text=str(result),
            metadata={"intent": "SEARCH"},
        )
    
    except Exception as e:
        logger.error(f"Search error: {e}")
        return MessageResponse(
            success=True,
            text="🔍 Arama yapılırken sorun oluştu. Tekrar deneyin.",
        )


async def _handle_chat(user_id: str, channel: str, session: Dict, brain_output: BrainOutput) -> MessageResponse:
    """CHAT intent - Genel sohbet"""
    response_text = (brain_output.response_text or "").strip()
    if not response_text:
        response_text = "Size yardımcı olmak için buradayım. İsterseniz ilan vermeye başlayabiliriz veya ürün arayabiliriz."
    
    session.setdefault("conversation_history", []).append({
        "role": "assistant",
        "content": response_text,
    })
    await save_session(user_id, channel, session)
    
    return MessageResponse(
        success=True,
        text=response_text,
        buttons=[
            ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
            ButtonResponse(text="🔍 Ürün Ara", payload="aramak istiyorum"),
        ],
        metadata={"intent": "CHAT"},
    )




# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _detect_price_query(message: str) -> Optional[str]:
    """
    Detect if message is asking for price research (not search).
    Returns the product name to research, or None if not a price query.
    
    Patterns detected:
    - "X kaç para eder" / "X kaç para"
    - "X fiyatı ne kadar" / "X fiyatı nekadar"  
    - "X piyasa değeri" / "X piyasası nekadar"
    - "X ne kadara satılır"
    - "fiyat araştır" / "fiyat öğren"
    """
    if not message:
        return None
    
    lower = message.lower().strip()
    
    # Price query patterns - order matters (more specific first)
    price_patterns = [
        r"(.+?)\s*kaç\s*para\s*eder",
        r"(.+?)\s*kaç\s*para$",
        r"(.+?)\s*fiyat[ıi]\s*ne\s*kadar",
        r"(.+?)\s*fiyat[ıi]\s*nekadar",
        r"(.+?)\s*piyasa\s*değeri",
        r"(.+?)\s*piyasas[ıi]\s*ne\s*kadar",
        r"(.+?)\s*piyasas[ıi]\s*nekadar",
        r"(.+?)\s*ne\s*kadara\s*sat[ıi]l[ıi]r",
        r"(.+?)\s*için\s*fiyat\s*araştır",
        r"(.+?)\s*fiyat\s*araştır",
        r"fiyat\s*araştır[ıi]?\s*(.+)",
        r"(.+?)\s*değeri\s*ne",
    ]
    
    for pattern in price_patterns:
        match = re.search(pattern, lower)
        if match:
            product = match.group(1).strip()
            # Clean up common prefixes
            product = re.sub(r"^(bir|bu|şu|o)\s+", "", product)
            # Remove "ürünü", "telefon" etc. suffixes if they're standalone
            product = re.sub(r"\s+(ürünü|telfon|telefon)$", "", product)
            
            if len(product) > 2:  # At least 3 chars
                return product
    
    # Also detect standalone price research requests WITH context
    standalone_patterns = [
        "fiyat araştırması yap",
        "fiyat öğren", 
        "piyasa araştır",
    ]
    if any(p in lower for p in standalone_patterns):
        # Try to extract product from the same message
        # Remove the command part and see what's left
        cleaned = lower
        for p in standalone_patterns:
            cleaned = cleaned.replace(p, "")
        cleaned = cleaned.strip(" .,!?")
        if len(cleaned) > 2:
            return cleaned
    
    return None


def _format_preview(listing: Dict[str, Any], show_full_description: bool = False) -> str:
    """Format current draft as preview text."""
    lines = ["📋 İlan Önizleme:"]

    try:
        from services.text_normalization import sentence_case_tr
    except Exception:
        sentence_case_tr = lambda s: s
    
    title = sentence_case_tr(listing.get("title") or "")
    price = listing.get("price")
    category = listing.get("category")
    description = sentence_case_tr(listing.get("description") or "")
    condition = listing.get("condition")
    location = sentence_case_tr(listing.get("location") or "")
    images = listing.get("images") or []
    
    # Show fields with checkmarks for filled, hourglass for missing
    if title:
        lines.append(f"✅ Başlık: {title}")
    else:
        lines.append("⏳ Başlık: (eksik)")
    
    if price:
        lines.append(f"✅ Fiyat: {price:,.0f} TL".replace(",", "."))
    else:
        lines.append("⏳ Fiyat: (eksik)")
    
    if category:
        lines.append(f"✅ Kategori: {category}")
    else:
        lines.append("⏳ Kategori: (eksik)")
    
    if description:
        if show_full_description:
            lines.append("✅ Açıklama:")
            lines.append(description)
        else:
            lines.append(f"✅ Açıklama: {description[:100]}{'...' if len(description) > 100 else ''}")
    else:
        lines.append("⏳ Açıklama: (opsiyonel)")
    
    if condition:
        lines.append(f"✅ Durum: {condition}")
    else:
        lines.append("⏳ Durum: (varsayılan: 2. El)")
    
    if location:
        lines.append(f"✅ Konum: {location}")
    else:
        lines.append("⏳ Konum: (opsiyonel)")
    
    if images:
        lines.append(f"✅ Fotoğraf: {len(images)} adet")
    else:
        lines.append("⏳ Fotoğraf: (opsiyonel)")
    
    return "\n".join(lines)

async def _format_listing_detail_response(listing: Dict[str, Any]) -> MessageResponse:
    """Format listing detail like WhatsApp card style.
    
    Clean, structured format with image, price, contact info.
    """
    title = listing.get("title") or "İlan"
    price = listing.get("price")
    category = listing.get("category") or ""
    description = listing.get("description") or ""
    condition = listing.get("condition") or "2. El"
    location = listing.get("location") or ""
    listing_id = str(listing.get("id") or "").strip()

    phone_visibility = str(listing.get("phone_visibility") or "public").strip().lower()
    name_visibility = str(listing.get("name_visibility") or "public").strip().lower()
    
    # Get primary image
    image_url = listing.get("image_url")
    images = listing.get("images") if isinstance(listing.get("images"), list) else []
    primary_image = image_url or (images[0] if images else None)
    
    # Get owner info
    owner_name = listing.get("user_name")
    owner_phone = listing.get("user_phone")

    if name_visibility == "hidden":
        owner_name = "İlan Sahibi"
    if phone_visibility == "hidden":
        owner_phone = supabase_client._mask_phone(owner_phone) or "+90xxxxxxxxxx"
    
    if not owner_name or not owner_phone:
        owner_id = listing.get("owner_id") or listing.get("user_id")
        if owner_id:
            try:
                if not owner_phone:
                    owner_phone = await supabase_client.get_user_phone(owner_id)
                if not owner_name:
                    owner_name = await supabase_client.get_user_display_name(owner_id)
            except Exception as e:
                logger.warning(f"Failed to fetch owner info: {e}")

    # Build WhatsApp-style card text
    lines = []
    
    # Image (markdown format for frontend to render)
    if primary_image:
        lines.append(f"![{title}]({primary_image})")
        lines.append("")
    
    # Title bold
    lines.append(f"*{title}*")
    
    # Price | Location | Category (one line)
    meta_parts = []
    if price:
        meta_parts.append(f"{float(price):,.0f} ₺")
    if location:
        meta_parts.append(location)
    if category:
        meta_parts.append(category)
    if meta_parts:
        lines.append(" | ".join(meta_parts))
    
    # Seller info
    seller_parts = []
    if owner_name:
        seller_parts.append(f"Satıcı: {owner_name}")
    if owner_phone:
        seller_parts.append(f"Telefon: {owner_phone}")
    if seller_parts:
        lines.append(" | ".join(seller_parts))

    # Always provide site-internal contact link in detail mode.
    # This is especially important for WhatsApp where inline UI actions are limited.
    try:
        contact_url = None
        if listing_id:
            token_row = await supabase_client.ensure_contact_token_for_listing(listing_id)
            token = str((token_row or {}).get("token") or "").strip() if isinstance(token_row, dict) else ""
            if token:
                frontend_base = (getattr(settings, "frontend_base_url", None) or "https://pazarglobal.com").strip().rstrip("/")
                contact_url = f"{frontend_base}/contact/{token}"
        if contact_url:
            lines.append("")
            lines.append(f"Mesaj göndermek için: {contact_url}")
    except Exception as e:
        logger.warning(f"Failed to attach contact link for detail response: {e}")
    
    # Description
    if description:
        lines.append("")
        lines.append("Açıklama:")
        lines.append(description)
    
    # Condition
    if condition:
        lines.append("")
        lines.append(f"Durum: {condition}")

    return MessageResponse(
        success=True,
        text="\n".join(lines),
        metadata={"intent": "SEARCH", "detail": True, "listing_id": listing.get("id")},
    )

    return MessageResponse(
        success=True,
        text="\n".join(lines),
        metadata={"intent": "SEARCH", "detail": True, "listing_id": listing.get("id")},
    )

async def _call_perplexity(query: str) -> Optional[float]:
    """Perplexity API - fiyat araştırması (sadece fiyat döner)"""
    try:
        result = await supabase_client.client.functions.invoke(
            "ai-assistant-cached",
            invoke_options={
                "body": {
                    "action": "suggest_price",
                    "title": query,
                    "category": "Genel",
                    "condition": "İyi",
                }
            }
        )
        
        if result.get("data", {}).get("suggested_price"):
            return float(result["data"]["suggested_price"])
    except Exception as e:
        logger.error(f"Perplexity error: {e}")
    
    return None


async def _call_perplexity_with_response(query: str) -> Dict[str, Any]:
    """Price research response builder.

    Flow:
    1) Edge cached pipeline (ai-assistant-cached) - preferred, writes/reads snapshots
    2) Direct Perplexity fallback - only if edge path fails
    """
    import httpx
    import json as json_module
    
    try:
        logger.info(f"Price research requested: query={query}")

        # 1) Preferred path: Edge cached function (consistent with listing flow)
        try:
            edge_result = await supabase_client.suggest_price_cached(
                title=query,
                category="Diğer",
                condition="2. El",
            )

            if isinstance(edge_result, dict):
                # Edge outputs may vary by deployment: normalize common shapes
                success = bool(edge_result.get("success", True))
                payload = edge_result.get("data") if isinstance(edge_result.get("data"), dict) else edge_result

                if success and isinstance(payload, dict):
                    suggested_price = payload.get("suggested_price")
                    if suggested_price is None:
                        suggested_price = payload.get("price")

                    min_price = payload.get("min_price")
                    max_price = payload.get("max_price")
                    reasoning = payload.get("reasoning") or payload.get("result") or ""
                    cached = bool(payload.get("cached", False))

                    if suggested_price is not None:
                        price_float = float(suggested_price)
                        min_price = float(min_price) if min_price is not None else price_float * 0.8
                        max_price = float(max_price) if max_price is not None else price_float * 1.2

                        freshness_note = "(cache)" if cached else "(guncel)"
                        response_text = f"""🔍 **{query}** için fiyat araştırması {freshness_note}:

💰 **Önerilen Fiyat:** {price_float:,.0f} TL

📊 **Fiyat Aralığı:**
• Minimum: {min_price:,.0f} TL
• Maksimum: {max_price:,.0f} TL

{f"📝 {reasoning}" if reasoning else ""}

Bu fiyatlar Türkiye 2. el piyasa verilerine göre hesaplanmıştır. İlan vermek isterseniz "ilan ver" yazabilirsiniz!"""

                        return {
                            "suggested_price": price_float,
                            "min_price": min_price,
                            "max_price": max_price,
                            "response": response_text,
                            "source": "edge_cached",
                            "cached": cached,
                        }
        except Exception as edge_err:
            logger.warning(f"Edge cached price research failed, falling back to direct Perplexity: {edge_err}")

        # 2) Fallback path: direct Perplexity call
        logger.info(f"Calling Perplexity API directly for fallback: {query}")

        if not settings.perplexity_api_key:
            logger.error("PERPLEXITY_API_KEY not configured")
            return {
                "suggested_price": None,
                "response": f"🔍 Fiyat araştırması yapılandırılmamış.\n\n**{query}** için ilan vermek ister misiniz?",
            }
        
        # Build Perplexity prompt for strict Turkey market price research
        prompt = f"""SADECE Türkiye piyasasında \"{query}\" ürününün 2. el fiyatını araştır.

    Kurallar:
    - Yalnız Türkiye içi piyasa verileri kullan (sahibinden, letgo, trendyol ikinci el vb. Türkiye odaklı kaynak mantığı).
    - Yurt dışı fiyatlarını, USD/EUR dönüşümlerini ve global pazar verilerini kullanma.
    - Fiyatları sadece TL olarak değerlendir.
    - Mümkünse son 30 gün verisini esas al; veri yetersizse bunu reasoning alanında belirt.

Sadece aşağıdaki JSON formatında yanıt ver, başka bir şey yazma:
{{
  "suggested_price": <sayı - TL cinsinden ortalama fiyat>,
  "min_price": <sayı - minimum fiyat>,
  "max_price": <sayı - maksimum fiyat>,
  "reasoning": "<kısa açıklama - max 100 karakter>"
}}

Eğer fiyat bulamazsan: {{"suggested_price": null, "reasoning": "Fiyat bilgisi bulunamadı"}}"""

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.perplexity.ai/chat/completions",
                json={
                    "model": "sonar",
                    "messages": [
                        {"role": "system", "content": "Sen SADECE Türkiye 2. el piyasası fiyat araştırma asistanısın. Global pazarları dahil etme. Çıktıyı yalnız JSON ver."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1,
                    "max_tokens": 300,
                },
                headers={
                    "Authorization": f"Bearer {settings.perplexity_api_key}",
                    "Content-Type": "application/json",
                }
            )
            
            if response.status_code != 200:
                logger.error(f"Perplexity API returned {response.status_code}: {response.text}")
                raise Exception(f"Perplexity API error: {response.status_code}")
            
            result = response.json()
        
        # Extract content from Perplexity response
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.debug(f"Perplexity raw response: {content}")
        
        # Parse JSON from response (handle markdown code blocks)
        json_str = content
        if "```json" in content:
            json_str = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            json_str = content.split("```")[1].split("```")[0].strip()
        
        try:
            data = json_module.loads(json_str)
        except json_module.JSONDecodeError:
            # Try to extract just the price if JSON parse fails
            logger.warning(f"Could not parse Perplexity JSON: {content}")
            data = {"suggested_price": None, "reasoning": "Fiyat bilgisi alınamadı"}
        
        suggested_price = data.get("suggested_price")
        min_price = data.get("min_price")
        max_price = data.get("max_price")
        reasoning = data.get("reasoning", "")
        
        if suggested_price:
            price_float = float(suggested_price)
            min_price = float(min_price) if min_price else price_float * 0.8
            max_price = float(max_price) if max_price else price_float * 1.2
            
            response_text = f"""🔍 **{query}** için fiyat araştırması:

💰 **Önerilen Fiyat:** {price_float:,.0f} TL

📊 **Fiyat Aralığı:**
• Minimum: {min_price:,.0f} TL
• Maksimum: {max_price:,.0f} TL

{f"📝 {reasoning}" if reasoning else ""}

Bu fiyatlar güncel piyasa verilerine göre hesaplanmıştır. İlan vermek isterseniz "ilan ver" yazabilirsiniz!"""
            
            return {
                "suggested_price": price_float,
                "min_price": min_price,
                "max_price": max_price,
                "response": response_text,
            }
        else:
            return {
                "suggested_price": None,
                "response": f"🤔 **{query}** için fiyat bilgisi bulunamadı.\n\nFarklı bir ürün sorabilir veya direkt ilan verebilirsiniz!",
            }
            
    except Exception as e:
        logger.error(f"Perplexity error: {e}")
        return {
            "suggested_price": None,
            "response": f"🔍 Fiyat araştırması şu an yapılamıyor.\n\n**{query}** için ilan vermek ister misiniz?",
        }


# ═══════════════════════════════════════════════════════════════════
# MEDIA ANALYZE ENDPOINT (for webchat image upload)
# ═══════════════════════════════════════════════════════════════════

class MediaAnalyzeRequest(BaseModel):
    session_id: str = Field(..., description="Session ID (usually user_id)")
    user_id: str = Field(..., description="User ID")
    phone_number: Optional[str] = Field(default=None)
    media_urls: List[str] = Field(default=[])


class MediaAnalyzeResponse(BaseModel):
    success: bool = True
    message: str = ""
    data: Optional[Dict[str, Any]] = None


class MediaPrecheckRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    media_data_url: str = Field(..., description="data:image/...;base64,... payload")


class MediaPrecheckResponse(BaseModel):
    success: bool = True
    safe: bool = True
    message: str = ""
    flagged_categories: List[str] = Field(default_factory=list)


@router.post("/webchat/media/analyze", response_model=MediaAnalyzeResponse)
async def analyze_media(
    request: MediaAnalyzeRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization")
) -> MediaAnalyzeResponse:
    """
    Analyze uploaded media for product information.
    
    Security: JWT token required for webchat channel
    
    Flow:
    1. Verify JWT token
    2. Check safety (content moderation)
    3. Analyze product (GPT-4 Vision)
    4. Save images to session/draft
    5. Return description of what AI sees
    """
    # Security check
    is_valid, verified_user_id, auth_error = await get_user_id_from_request(
        authorization=authorization,
        request_user_id=request.user_id,
        channel="webchat"
    )
    
    if not is_valid:
        logger.warning(f"Media analyze auth failed: {auth_error}")
        return MediaAnalyzeResponse(
            success=False,
            message=f"🔐 Kimlik doğrulama hatası: {auth_error}",
        )
    
    user_id = verified_user_id
    logger.info(f"Media analyze: user={user_id}, urls={len(request.media_urls)}")
    
    if not request.media_urls:
        return MediaAnalyzeResponse(
            success=False,
            message="Görsel bulunamadı. Lütfen bir görsel yükleyin.",
        )
    
    try:
        # Process each image
        all_descriptions = []
        analyzed_products = []
        blocked_images = []
        
        for i, url in enumerate(request.media_urls[:5]):  # Max 5 images
            logger.info(f"Analyzing image {i+1}: {url[:100]}...")
            
            # 1. Safety check
            safety = await vision_service.check_safety(url)
            if not safety.get("safe", True):
                blocked_images.append({
                    "index": i,
                    "reason": ", ".join(safety.get("flagged_categories", ["policy"]))
                })
                continue
            
            # 2. Product analysis
            analysis = await vision_service.analyze_product(url)
            
            if analysis.get("error"):
                logger.warning(f"Analysis error for image {i+1}: {analysis.get('error')}")
                all_descriptions.append(f"Görsel {i+1}: Analiz edilemedi.")
                continue

            prohibited_term = vision_service.detect_prohibited_product(analysis)
            if prohibited_term:
                blocked_images.append({
                    "index": i,
                    "reason": f"illicit_item:{prohibited_term}",
                })
                continue
            
            # Build description
            parts = []
            if analysis.get("product"):
                parts.append(f"**Ürün:** {analysis['product']}")
            if analysis.get("brand"):
                parts.append(f"**Marka:** {analysis['brand']}")
            if analysis.get("category"):
                parts.append(f"**Kategori:** {analysis['category']}")
            if analysis.get("condition"):
                parts.append(f"**Durum:** {analysis['condition']}")
            if analysis.get("color"):
                parts.append(f"**Renk:** {analysis['color']}")
            
            if parts:
                desc = f"📷 Görsel {i+1}:\n" + "\n".join(parts)
                all_descriptions.append(desc)
                analyzed_products.append(analysis)
            else:
                all_descriptions.append(f"Görsel {i+1}: Ürün tanınamadı.")
        
        # 3. Save to session
        session = await load_session(user_id, "webchat")
        
        # Add images to listing_data
        listing_data = session.get("listing_data", {})
        existing_images = listing_data.get("images", [])
        
        # Add new images (avoid duplicates)
        for url in request.media_urls:
            if url not in existing_images:
                existing_images.append(url)
        
        listing_data["images"] = existing_images[:5]  # Max 5 images
        
        # Pre-fill from first analysis if no data yet
        if analyzed_products and not listing_data.get("title"):
            first = analyzed_products[0]
            if first.get("product"):
                listing_data["title"] = first["product"]
            if first.get("category"):
                listing_data["category"] = first["category"]
            if first.get("condition"):
                listing_data["condition"] = first["condition"]
        
        session["listing_data"] = listing_data
        session["state"] = "DRAFTING"
        session["draft_updated_at"] = datetime.utcnow().isoformat()
        await save_session(user_id, "webchat", session)
        
        # 4. Build response message
        if blocked_images:
            block_msg = f"⚠️ {len(blocked_images)} görsel içerik politikası nedeniyle engellendi.\n\n"
        else:
            block_msg = ""
        
        if all_descriptions:
            analysis_msg = "\n\n".join(all_descriptions)
            response_msg = f"{block_msg}📸 Yüklenen görseller analiz edildi:\n\n{analysis_msg}\n\n💡 İlan oluşturmak için fiyat ve açıklama ekleyin."
        else:
            response_msg = f"{block_msg}Görseller yüklendi ancak ürün tanınamadı. Lütfen başlık ve fiyat bilgisi verin."
        
        return MediaAnalyzeResponse(
            success=True,
            message=response_msg,
            data={
                "analyzed_products": analyzed_products,
                "image_count": len(existing_images),
                "listing_preview": listing_data if listing_data.get("title") else None,
            }
        )
        
    except Exception as e:
        logger.error(f"Media analyze error: {e}", exc_info=True)
        return MediaAnalyzeResponse(
            success=False,
            message=f"Görsel analiz hatası: {str(e)}",
        )


@router.post("/webchat/media/precheck", response_model=MediaPrecheckResponse)
async def precheck_media_before_upload(
    request: MediaPrecheckRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization")
) -> MediaPrecheckResponse:
    """Run moderation on image bytes/data-url before storage upload."""
    is_valid, verified_user_id, auth_error = await get_user_id_from_request(
        authorization=authorization,
        request_user_id=request.user_id,
        channel="webchat"
    )
    if not is_valid:
        return MediaPrecheckResponse(
            success=False,
            safe=False,
            message=f"🔐 Kimlik doğrulama hatası: {auth_error}",
            flagged_categories=["auth_error"],
        )

    media_data_url = (request.media_data_url or "").strip()
    if not media_data_url.startswith("data:image/"):
        return MediaPrecheckResponse(
            success=False,
            safe=False,
            message="Geçersiz görsel formatı. Lütfen tekrar deneyin.",
            flagged_categories=["invalid_media_data"],
        )

    safety = await vision_service.check_safety(media_data_url)
    if not safety.get("safe", False):
        flagged = safety.get("flagged_categories", []) or []
        return MediaPrecheckResponse(
            success=True,
            safe=False,
            message="🚫 Bu görsel içerik politikaları nedeniyle yüklenemez.",
            flagged_categories=[str(c) for c in flagged],
        )

    analysis = await vision_service.analyze_product(media_data_url)
    prohibited_term = vision_service.detect_prohibited_product(analysis if isinstance(analysis, dict) else {})
    if prohibited_term:
        return MediaPrecheckResponse(
            success=True,
            safe=False,
            message="🚫 Bu görselde platformda yayınlanmasına izin verilmeyen bir ürün tespit edildi.",
            flagged_categories=["illicit_item", prohibited_term],
        )

    logger.info(f"Webchat media precheck passed for user={verified_user_id}")
    return MediaPrecheckResponse(
        success=True,
        safe=True,
        message="Görsel güvenlik kontrolünden geçti.",
        flagged_categories=[],
    )
