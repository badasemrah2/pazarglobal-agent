"""
WebChat API endpoints for frontend integration
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from loguru import logger
from services import redis_client, openai_client
from config import settings
from tools import publish_listing_tool, get_wallet_balance_tool
from agents import IntentRouterAgent, ComposerAgent, PublishDeleteAgent, SearchComposerAgent, SmallTalkAgent
from services import supabase_client
import json
import uuid
import re

# In-memory cache for last search results (when Redis is disabled)
LAST_SEARCH_CACHE: Dict[str, List[Any]] = {}

# Local session cache fallback when Redis is disabled
IN_MEMORY_SESSION_CACHE: Dict[str, Dict[str, Any]] = {}

MEDIA_ANALYSIS_SYSTEM_PROMPT = (
    "You are a marketplace vision assistant that returns concise Turkish JSON. Always respond with a single JSON object containing these keys: "
    "product (string), category (string), condition (string), features (array of up to 5 short strings), description (string), "
    "safety_flags (array of short warning strings, empty array when no issues). If you are unsure, set the field to an empty string or empty array."
)

MEDIA_ANALYSIS_USER_PROMPT = (
    "Lütfen görseldeki ürünü analiz et ve yukarıdaki JSON şemasını doldur. Ürünün türünü, olası kullanım alanını, durumunu ve dikkat çeken özelliklerini belirt."
)


def redis_is_disabled() -> bool:
    """Centralize redis enabled/disabled checks."""
    return getattr(redis_client, "disabled", False)


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


def merge_unique_urls(existing: List[str], new_urls: List[str]) -> List[str]:
    """Merge new media URLs while preserving order and removing duplicates."""
    seen: set[str] = set()
    merged: List[str] = []
    for url in existing + new_urls:
        if url and url not in seen:
            merged.append(url)
            seen.add(url)
    return merged


def is_publish_command(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return any(token in msg for token in [
        "yayınla",
        "yayınla!",
        "yayinla",
        "yayina",
        "yayınlamak",
        "yayinlamak",
        "publish",
    ])


def is_delete_command(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return any(token in msg for token in ["sil", "ilanı sil", "ilani sil", "kaldır", "kaldir", "delete"])


def is_create_listing_command(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    # Explicit create/sell commands
    if msg in {"ilan oluştur", "ilan olustur", "ilan ver", "sat", "satıyorum", "satiyorum", "satmak istiyorum"}:
        return True
    return any(phrase in msg for phrase in [
        "ilan oluştur",
        "ilan olustur",
        "ilan ver",
        "satmak istiyorum",
        "satıyorum",
        "satiyorum",
        "satacağım",
        "satacagim",
        "satışa koy",
        "satisa koy",
    ])


def is_search_command(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
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
        "göster",
        "goster",
        "ara ",
        " ara",
        "bul ",
        " bul",
        "search",
        "find",
    ]):
        return True

    # Word-boundary guard for short verbs like "ara" and "bul" to avoid matching inside other words.
    return bool(re.search(r"\b(ara|bul|listele|goster|göster)\b", msg))


def is_browse_all_command(message: str) -> bool:
    msg = (message or "").strip().lower()
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
    msg = (message or "").strip().lower()
    if not msg:
        return False
    # Common confirmations + typos
    return any(token in msg for token in [
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
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return any(token in msg for token in ["iptal", "vazgeç", "vazgec", "hayır", "hayir", "boşver", "bosver"])


def draft_is_publishable(draft: Dict[str, Any]) -> bool:
    listing = (draft or {}).get("listing_data") or {}
    images = (draft or {}).get("images") or []
    if not (listing.get("title") and str(listing.get("title")).strip()):
        return False
    if not (listing.get("description") and str(listing.get("description")).strip()):
        return False
    if listing.get("price") is None:
        return False
    if not (listing.get("category") and str(listing.get("category")).strip()):
        return False
    if not images:
        return False
    return True


async def handle_publish_or_delete_flow(
    message_body: str,
    session_id: str,
    session: Dict[str, Any],
    user_id: str,
    redis_disabled: bool,
    session_dirty: bool
) -> Dict[str, Any]:
    """Deterministic publish flow (no LLM): avoids looping confirmations and fake costs."""

    # Only support publish for now (delete can be added similarly)
    draft_id = session.get("active_draft_id")
    if not draft_id:
        return {
            "success": False,
            "message": "Aktif bir taslak bulunamadı. Önce 'ilan oluştur' ile taslak başlatın.",
            "data": {"type": "publish_delete"},
            "intent": "publish_or_delete"
        }

    # Read draft
    from services import supabase_client
    draft = await supabase_client.get_draft(draft_id)
    if not draft:
        return {
            "success": False,
            "message": "Taslak bulunamadı. Lütfen yeniden deneyin.",
            "data": {"type": "publish_delete"},
            "intent": "publish_or_delete"
        }

    # Pending confirmation state
    pending = session.get("pending_publish")
    if isinstance(pending, dict) and pending.get("draft_id") == draft_id:
        if is_cancel_command(message_body):
            session.pop("pending_publish", None)
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
                # Clear session state after publish
                session.pop("pending_publish", None)
                session["active_draft_id"] = None
                session["intent"] = None
                session_dirty = True
                listing_id = (result.get("data") or {}).get("listing_id")
                return {
                    "success": True,
                    "message": f"İlan yayınlandı. İlan ID: {listing_id}" if listing_id else "İlan yayınlandı.",
                    "data": {"type": "publish_delete", "listing_id": listing_id},
                    "intent": "publish_or_delete",
                    "_session_dirty": session_dirty
                }
            return {
                "success": False,
                "message": result.get("error") or "Yayınlama başarısız oldu.",
                "data": {"type": "publish_delete"},
                "intent": "publish_or_delete",
                "_session_dirty": session_dirty
            }

        # If pending exists but user didn't confirm/cancel, re-prompt succinctly.
        cost = int(pending.get("cost") or settings.listing_credit_cost)
        return {
            "success": True,
            "message": f"Yayınlama ücreti {cost} kredi. Onaylıyorsanız 'onayla', vazgeçmek için 'iptal' yazın.",
            "data": {"type": "publish_delete"},
            "intent": "publish_or_delete",
            "_session_dirty": session_dirty
        }

    # Not pending: if draft incomplete, show what is missing
    if not draft_is_publishable(draft):
        return {
            "success": True,
            "message": build_draft_status_message(draft),
            "data": {"type": "draft_update"},
            "intent": "create_listing",
            "_session_dirty": session_dirty
        }

    # If user said publish (or we are in publish intent), ask a single confirmation
    balance_result = await get_wallet_balance_tool.execute(user_id=user_id)
    balance = None
    if balance_result.get("success"):
        balance = (balance_result.get("data") or {}).get("balance")
    cost = int(settings.listing_credit_cost)

    # If user is just saying publish/onay, start confirmation step
    session["pending_publish"] = {"draft_id": draft_id, "cost": cost}
    session_dirty = True

    balance_text = f"Mevcut bakiyeniz: {balance} kredi. " if balance is not None else ""
    return {
        "success": True,
        "message": (
            f"İlanı yayınlamak üzeresiniz. {balance_text}Yayınlama ücreti: {cost} kredi. "
            "Onaylıyorsanız 'onayla', vazgeçmek için 'iptal' yazın."
        ),
        "data": {"type": "publish_delete", "draft_id": draft_id, "credit_cost": cost},
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
            parts.append(f"durum: {condition}")
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

    prompt_line = (
        "Bu ürün için ne yapmak istersiniz? 'ilan oluştur' yazarak satış taslağı başlatabilir "
        "veya 'benzer ara' yazarak benzer ürünleri inceleyebilirsiniz."
    )

    return "\n\n".join([
        "🔎 Görsel analizi hazır!",
        "\n".join(summary_lines),
        prompt_line
    ])

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


def build_draft_status_message(draft: Dict[str, Any]) -> str:
    """Generate a friendly status message about the current draft state."""
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
        missing.append("tahmini fiyat")

    category = listing.get("category")
    if category:
        add_line("Kategori", category)
    else:
        missing.append("kategori")

    add_line("Fotoğraflar", f"{len(images)} adet" if images else "henüz eklenmedi")
    if not images:
        missing.append("ürün fotoğrafları")

    vision = draft.get("vision_product")
    if isinstance(vision, dict):
        vision_category = vision.get("category") or vision.get("product")
        vision_condition = vision.get("condition")
        features = vision.get("features")
        if vision_category and not category:
            add_line("Kategori", str(vision_category))
        if vision_category:
            vision_lines.append(f"Ürün türü: {vision_category}")
        if vision_condition:
            vision_lines.append(f"Durum: {vision_condition}")
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
            "Eksik bilgiler: " + ", ".join(missing) + ". Lütfen bu detayları yazarak veya fotoğraf yükleyerek paylaşın."
        )
    else:
        message_parts.append("Tüm temel bilgiler tamam. Hazırsanız 'yayınla' yazarak ilanı yayınlayabilirsiniz.")

    return "\n\n".join(part.strip() for part in message_parts if part.strip())


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


def is_command_only_message(message: str) -> bool:
    msg = (message or "").strip().lower()
    return msg in _COMMAND_ONLY_TOKENS


def next_missing_slot(draft: Dict[str, Any]) -> Optional[str]:
    listing = (draft or {}).get("listing_data") or {}
    images = (draft or {}).get("images") or []
    # Ask for images first only if none exists (keeps flow predictable)
    if not images:
        return "images"
    if not (listing.get("title") or "").strip():
        return "title"
    if not (listing.get("description") or "").strip():
        return "description"
    if listing.get("price") is None:
        return "price"
    if not (listing.get("category") or "").strip():
        return "category"
    return None


def build_next_step_message(draft: Dict[str, Any]) -> str:
    slot = next_missing_slot(draft)
    vision = (draft or {}).get("vision_product") or {}
    suggested_category = ""
    if isinstance(vision, dict):
        suggested_category = str(vision.get("category") or vision.get("product") or "").strip()

    if slot == "images":
        return "İlanı hazırlayabilmem için lütfen 1-2 fotoğraf yükleyin."
    if slot == "title":
        return "Ürünün adı nedir? (Örn: 'iPhone 14 128GB siyah')"
    if slot == "description":
        return "Kısa bir açıklama yazar mısınız? (durum, çizik/hasar, kutu/fatura, takas vb.)"
    if slot == "price":
        return "Fiyat nedir? İsterseniz 'kaç para eder' yazın, piyasa verisine göre tahmin söyleyeyim."
    if slot == "category":
        if suggested_category:
            return f"Kategori nedir? (İsterseniz önerim: {suggested_category})"
        return "Kategori nedir? (Örn: Elektronik, Otomotiv...)"

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
                "pending_media_analysis": []
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
            if "locked_intent" not in session:
                session["locked_intent"] = None
                session_dirty = True

        async def _finalize_response(payload: Dict[str, Any]) -> Dict[str, Any]:
            if session_dirty:
                await persist_session_state(session_id, session)
            return payload
        finalize_response = _finalize_response

        raw_user_id = session.get("user_id") or user_id
        normalized_user_id = normalize_user_id(raw_user_id)
        if session.get("user_id") != normalized_user_id:
            session["user_id"] = normalized_user_id
            session_dirty = True
        user_id = normalized_user_id

        # If user issues a publish/delete command, override any sticky intent.
        # Otherwise the session may remain in create_listing and never reach PublishDeleteAgent.
        if is_publish_command(message_body) or is_delete_command(message_body):
            session["intent"] = "publish_or_delete"
            session_dirty = True
            if not redis_disabled:
                await redis_client.set_intent(session_id, "publish_or_delete")
        
        # Store message in history
        if not redis_disabled:
            await redis_client.add_message(session_id, {
                "role": "user",
                "content": message_body,
                "timestamp": str(uuid.uuid1().time)
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

                message_text = format_media_analysis_message(session.get("pending_media_analysis") or [])
                return await finalize_response({
                    "success": True,
                    "message": message_text,
                    "data": {
                        "type": "media_analysis",
                        "media_urls": all_media_urls,
                        "media_analysis": session.get("pending_media_analysis") or []
                    },
                    "intent": None
                })

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
            hint = ""
            if session.get("active_draft_id") or session.get("pending_media_urls") or session.get("pending_media_analysis"):
                hint = "\n\nİstersen ilan taslağına kaldığımız yerden devam edebiliriz. Ürünün adını (başlık) yazman yeterli."
            return await finalize_response({
                "success": True,
                "message": "Merhaba! Size nasıl yardımcı olabilirim?" + hint,
                "data": {"type": "conversation", "intent": "small_talk"},
                "intent": "small_talk",
            })

        # Get or determine intent
        intent = session.get("intent")
        locked_intent = session.get("locked_intent")

        # Sticky intent: once locked_intent is set, do not re-run global routing.
        # Publish/delete can still temporarily override.
        if locked_intent and intent != "publish_or_delete":
            intent = locked_intent
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
                intent = override_intent
                session["intent"] = intent
                session["locked_intent"] = intent
                locked_intent = intent
                session_dirty = True
                if not redis_disabled:
                    await redis_client.set_intent(session_id, intent)

        if not intent:
            router_agent = IntentRouterAgent()
            intent = await router_agent.classify_intent(message_body)
            session["intent"] = intent
            session_dirty = True
            if not redis_disabled:
                await redis_client.set_intent(session_id, intent)
            logger.info(f"WebChat intent for {session_id}: {intent}")

            # Only lock "task" intents; keep small_talk unlocked.
            if intent in {"create_listing", "search_listings"}:
                session["locked_intent"] = intent
                locked_intent = intent
                session_dirty = True
        
        response_data = {"intent": intent}
        
        # Route to appropriate agent
        if intent == "create_listing":
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

                # Best-effort: seed vision_product/category from first analysis
                first_analysis = None
                if isinstance(analyses, list) and analyses:
                    first = analyses[0]
                    if isinstance(first, dict):
                        first_analysis = first.get("analysis")
                if isinstance(first_analysis, dict):
                    try:
                        existing = await supabase_client.get_draft(draft_id)
                        listing_data = (existing or {}).get("listing_data") or {}
                        if not (listing_data.get("category") or "").strip():
                            suggested = str(first_analysis.get("category") or "").strip()
                            if suggested:
                                await supabase_client.update_draft_category(draft_id, suggested, vision_product=first_analysis)
                        else:
                            # still store vision_product
                            existing_category = str(listing_data.get("category") or "").strip()
                            await supabase_client.update_draft_category(
                                draft_id,
                                existing_category or "Diğer",
                                vision_product=first_analysis,
                            )
                    except Exception:
                        pass

                # Clear pre-intent buffer after consumption
                session["pending_media_urls"] = []
                session["pending_media_analysis"] = []
                session_dirty = True

            draft_id = session.get("active_draft_id")
            existing_draft = await supabase_client.get_draft(draft_id) if draft_id else None

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
                condition = ""
                if isinstance(vision, dict):
                    condition = str(vision.get("condition") or "").strip()

                # If we don't have a title yet, fall back to vision product/category
                if not title and isinstance(vision, dict):
                    title = str(vision.get("product") or vision.get("category") or "").strip()

                # If we don't have a category yet, let edge function handle defaulting.
                price_resp = await supabase_client.suggest_price_cached(
                    title=title or "Ürün",
                    category=category or "Diğer",
                    description=description or "",
                    condition=condition or "İyi Durumda",
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

                    return await finalize_response({
                        "success": True,
                        "message": (
                            f"Önerilen satış fiyatı: {suggested} ₺ {cached_txt}.{conf_txt} "
                            "Fiyatı bu şekilde yazayım mı? (evet/hayır ya da kendi fiyatınızı yazın)"
                        ),
                        "data": {
                            "type": "price_suggestion",
                            "suggested_price": suggested,
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
            if looks_like_greeting(message_body):
                run_composer = False

            # Also don't run composer on pure flow commands like "ilan oluştur" when we already
            # have media in the draft; otherwise title/description agents may hallucinate from
            # an empty/command-only message.
            if run_composer and is_command_only_message(message_body):
                active_draft_id = session.get("active_draft_id")
                if not existing_draft and isinstance(active_draft_id, str) and active_draft_id:
                    existing_draft = await supabase_client.get_draft(active_draft_id)
                if existing_draft and (existing_draft.get("images") or []):
                    run_composer = False

            # Pass no media URLs here because we already consumed pre-intent buffer into the draft.
            # If you later want to support post-lock image uploads in this endpoint, they will still
            # come through as media_urls and can be attached before calling composer.
            result = None
            if run_composer:
                active_draft_id = session.get("active_draft_id")
                composer_draft_id = active_draft_id if isinstance(active_draft_id, str) and active_draft_id else None
                result = await composer.orchestrate_listing_creation(
                    user_message=message_body,
                    user_id=user_id,
                    phone_number=session_id,  # Use session_id as identifier
                    draft_id=composer_draft_id,
                    media_urls=[]
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
                    response_text = build_draft_status_message(draft)
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
            publish_payload = await handle_publish_or_delete_flow(
                message_body=message_body,
                session_id=session_id,
                session=session,
                user_id=user_id,
                redis_disabled=redis_disabled,
                session_dirty=session_dirty
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
            result = await composer.orchestrate_search(message_body)

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
            agent = SmallTalkAgent()
            response = await agent.run_simple(message_body)

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

    raw_user_id = session.get("user_id") or chat_message.user_id
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
