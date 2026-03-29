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
from agents.vision_safety_gate import vision_safety_gate
from services.vision_service import vision_service
from services.text_normalization import normalize_for_match
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


class ButtonResponse(BaseModel):
    text: str
    payload: str


class MessageResponse(BaseModel):
    success: bool = True
    text: str
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
    "silah", "tabanca", "tufek", "tüfek", "pistol", "gun", "firearm", "revolver", "shotgun",
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
    """Load session from Redis (scoped by channel)"""
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


def _is_show_more_command(lower_msg: str) -> bool:
    msg = (lower_msg or "").strip().lower()
    triggers = [
        "daha fazla göster",
        "daha fazla",
        "devamını göster",
        "devamini goster",
        "devamını",
        "devamini",
        "devam",
    ]
    return any(trigger in msg for trigger in triggers)


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

    # Fallback token logic for colloquial variants
    has_price_signal = any(t in msg for t in ["fiyat", "piyasa", "deger", "eder", "ortalama"])
    has_question_signal = any(t in msg for t in ["?", "nedir", "ne", "kac", "kaca", "ne kadar", "arastir", "soyler misin"])
    return has_price_signal and has_question_signal


async def _format_search_continuation_page(listings: List[Dict[str, Any]], start_idx: int, page_size: int = 5) -> str:
    total = len(listings or [])
    if total == 0 or start_idx >= total:
        return "📄 Gösterilecek başka ilan kalmadı."

    end_idx = min(start_idx + page_size, total)
    chunk = listings[start_idx:end_idx]

    lines: List[str] = [f"📄 {start_idx + 1}-{end_idx}. ilanlar:", ""]
    for i, listing in enumerate(chunk, start=start_idx + 1):
        title = listing.get("title") or "Başlıksız"
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
    REQUIRED_FIELDS = ["title", "price", "description"]
    
    # Import from category_library - single source of truth
    from services.category_library import SUPPORTED_CATEGORIES, classify_category, normalize_category_id
    ALLOWED_CATEGORIES = set(SUPPORTED_CATEGORIES)
    
    ALLOWED_CONDITIONS = {"Sıfır", "Az Kullanılmış", "2. El"}
    
    @classmethod
    def validate(cls, listing_data: Dict[str, Any]) -> tuple[bool, List[str]]:
        """
        JSON validasyon - FSM'in beklediği formata uygun mu?
        
        Returns:
            (is_valid, missing_fields)
        """
        missing = []

        # Normalize text fields to TR/EN keyboard-safe alphabet + sentence case
        try:
            from services.text_normalization import normalize_keyboard_text, sentence_case_tr

            for key in ["title", "description", "location"]:
                raw_val = listing_data.get(key)
                if isinstance(raw_val, str) and raw_val.strip():
                    normalized = normalize_keyboard_text(raw_val)
                    listing_data[key] = sentence_case_tr(normalized)
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

        # Category - FSM OTOMATİK BELİRLER (LLM sorumluluğunda değil!)
        category = listing_data.get("category")
        # Önce normalize et (kullanıcı "Tarım&Gıda" yazmışsa "Tarım & Gıda" yap)
        if category and category not in ["Sistem", "Otomatik", ""]:
            normalized = cls.normalize_category_id(category)
            if normalized and normalized in cls.ALLOWED_CATEGORIES:
                listing_data["category"] = normalized
                category = normalized
        
        # Kategori boş, "Sistem", "Otomatik" veya geçersizse → başlık/açıklamadan otomatik belirle
        if not category or category in ["Sistem", "Otomatik"] or category not in cls.ALLOWED_CATEGORIES:
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
        
        # Kategori artık kesinlikle dolu - missing'e ekleme (FSM her zaman doldurur)
        # NOT: Kategori artık hiçbir zaman missing olmaz!
        
        is_valid = len(missing) == 0
        return is_valid, missing
    
    @classmethod
    def generate_keywords(cls, listing_data: Dict[str, Any]) -> str:
        """
        Keywords üret - arama için metadata.keywords_text
        """
        parts = []
        
        # Title words
        title = listing_data.get("title", "")
        if title:
            parts.extend(title.lower().split())
        
        # Description words (ilk 100 karakter)
        desc = listing_data.get("description", "")
        if desc:
            parts.extend(desc[:100].lower().split())
        
        # Category
        category = listing_data.get("category", "")
        if category:
            parts.append(category.lower())
        
        # Location
        location = listing_data.get("location", "")
        if location:
            parts.append(location.lower())
        
        # Condition
        condition = listing_data.get("condition", "")
        if condition:
            parts.append(condition.lower())
        
        # Dedupe and clean
        keywords = list(set(parts))
        return " ".join(keywords[:50])
    
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
    async def publish(cls, user_id: str, listing_data: Dict[str, Any]) -> tuple[bool, str, Optional[str]]:
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
            is_valid, missing = cls.validate(listing_data)
            if not is_valid:
                return False, f"Eksik alanlar: {', '.join(missing)}", None

            prohibited_term = _detect_prohibited_listing_term(listing_data)
            if prohibited_term:
                return False, "🚫 Bu içerik platform politikalarına aykırı olduğu için yayınlanamaz.", None
            
            # 2. Wallet check
            has_enough, balance = await cls.check_wallet(user_id)
            if not has_enough:
                return False, f"💳 Bakiyeniz yetersiz (Mevcut: {balance:.0f} TL). İlan yayınlamak için 55 TL gerekiyor.", None
            
            # 3. Deduct credit
            if not await cls.deduct_credit(user_id):
                return False, "Kredi düşürülemedi. Lütfen tekrar deneyin.", None
            
            # 4. Prepare listing for Supabase
            listing_id = str(uuid.uuid4())
            
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
                # Refund credit
                await cls.deduct_credit(user_id, -55.0)
                return False, f"İlan kaydedilemedi: {str(insert_err)}", None
            
            if not result.data:
                logger.error(f"Supabase insert returned no data. Result: {result}")
                # Refund credit
                await cls.deduct_credit(user_id, -55.0)
                return False, "İlan kaydedilemedi. Lütfen tekrar deneyin.", None
            
            logger.info(f"Listing published successfully: {listing_id}")
            return True, "İlan başarıyla yayınlandı!", listing_id
            
        except Exception as e:
            logger.error(f"Publish error: {e}", exc_info=True)
            return False, f"Yayınlama hatası: {str(e)}", None


# ═══════════════════════════════════════════════════════════════════
# FSM STATE MACHINE - Deterministic confirmation flow (LLM bypass)
# ═══════════════════════════════════════════════════════════════════

# FSM Commands (deterministic, LLM bypassed)
# IMPORTANT: In PENDING_CONFIRMATION state, these commands trigger direct action without LLM
FSM_COMMANDS = {
    # Confirmation commands
    "onayla": "CONFIRM",
    "onaylıyorum": "CONFIRM",
    "evet onayla": "CONFIRM",
    "yayınla": "CONFIRM",  # In PENDING_CONFIRMATION state, "yayınla" = final confirm
    "yayinla": "CONFIRM",  # Turkish keyboard variation
    "evet": "CONFIRM",
    "evet yayınla": "CONFIRM",
    # Cancel commands
    "iptal": "CANCEL",
    "vazgeçtim": "CANCEL",
    "iptal et": "CANCEL",
    "hayır": "CANCEL",
}

# FSM Edit commands (deterministic, LLM bypassed)
EDIT_FIELD_MAP = {
    "başlık": "title",
    "baslik": "title",
    "title": "title",
    "açıklama": "description",
    "aciklama": "description",
    "description": "description",
    "fiyat": "price",
    "price": "price",
    "durum": "condition",
    "condition": "condition",
    "lokasyon": "location",
    "konum": "location",
    "location": "location",
    "kategori": "category",
    "category": "category",
}

CONDITION_ALIASES = {
    "sıfır": "Sıfır",
    "sifir": "Sıfır",
    "az kullanılmış": "Az Kullanılmış",
    "az kullanilmis": "Az Kullanılmış",
    "az kullanilmis": "Az Kullanılmış",
    "2. el": "2. El",
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
    normalized = (raw_value or "").strip().lower()
    if normalized in CONDITION_ALIASES:
        return CONDITION_ALIASES[normalized]
    # Allow exact matches if user already typed a valid condition
    for allowed in FSMEngine.ALLOWED_CONDITIONS:
        if normalized == allowed.lower():
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
        if ":" not in part:
            continue
        raw_key, raw_value = part.split(":", 1)
        key = raw_key.strip().lower()
        value = (raw_value or "").strip()
        if not value:
            continue

        field = EDIT_FIELD_MAP.get(key)
        if not field:
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
        else:
            updates[field] = value

    return updates, errors


async def _fsm_show_confirmation_preview(user_id: str, channel: str, session: Dict) -> MessageResponse:
    """FSM: Show detailed confirmation preview with credit info"""
    listing = session.get("listing_data", {})
    
    # FSM validate - kategori otomatik belirlensin!
    FSMEngine.validate(listing)
    
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
    
    credit_cost = 55
    
    # Kategori gösterimi (FSM tarafından belirlendi)
    category_display = listing.get('category', 'Diğer')
    if category_display in ["Sistem", "Otomatik", ""]:
        category_display = "Diğer"
    
    # Format detailed preview
    try:
        from services.text_normalization import sentence_case_tr
    except Exception:
        sentence_case_tr = lambda s: s

    preview = f"""📋 **YAYIN ÖNCESİ KONTROL**

**BAŞLIK:**
{sentence_case_tr(listing.get('title', '—'))}

**AÇIKLAMA:**
{sentence_case_tr(listing.get('description', '—'))}

**FİYAT:**
{listing.get('price', 0):,.0f} ₺

**DURUM:**
{listing.get('condition', '2. El')}

**KATEGORİ:**
{category_display} ✅ (Sistem tarafından belirlendi)

**LOKASYON:**
{sentence_case_tr(listing.get('location', 'Belirtilmemiş'))}

**FOTOĞRAFLAR:**
{len(listing.get('images', []))} adet

─────────────────────────
💳 **Mevcut bakiyeniz:** {balance:,.0f} kredi
💰 **Yayın ücreti:** {credit_cost} kredi
{"✅ Bakiye yeterli" if balance >= credit_cost else "❌ Bakiye yetersiz!"}

─────────────────────────
🛠️ **KOMUTLAR**
👉 Onayla: `onayla`
👉 İptal: `iptal`
👉 Düzenle: değişiklik için yazın (örn: "başlık: Yeni Başlık")

İlanınızı yayınlamak için **onayla** yazın."""

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
        success, message, listing_id = await FSMEngine.publish(user_id, listing)
        
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
    listing.update(updates)
    session["listing_data"] = listing
    session["draft_updated_at"] = datetime.utcnow().isoformat()

    return await _fsm_show_confirmation_preview(user_id, channel, session)


# ═══════════════════════════════════════════════════════════════════
# MAIN ENDPOINT
# ═══════════════════════════════════════════════════════════════════

@router.post("/message", response_model=MessageResponse)
async def handle_message(
    request: MessageRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization")
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
        is_valid, verified_user_id, auth_error = await get_user_id_from_request(
            authorization=authorization,
            request_user_id=request.user_id,
            channel=request.channel
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

        # Ensure created_via is tracked for consistent metadata
        if request.channel in ("webchat", "whatsapp"):
            listing_data = session.get("listing_data", {})
            if isinstance(listing_data, dict) and not listing_data.get("created_via"):
                listing_data["created_via"] = request.channel
                session["listing_data"] = listing_data
                await save_session(user_id, request.channel, session)

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

        # 1.1 Draft TTL check (10 minutes)
        draft_updated_at = session.get("draft_updated_at")
        if draft_updated_at:
            try:
                last_ts = datetime.fromisoformat(draft_updated_at)
                if (datetime.utcnow() - last_ts).total_seconds() > 600:
                    session["listing_data"] = {}
                    session["state"] = "IDLE"
                    session["fsm_state"] = FSM_STATE_IDLE
                    session["draft_updated_at"] = None
                    session["last_intent"] = None
                    await save_session(user_id, request.channel, session)
            except Exception:
                # If parsing fails, reset draft defensively
                session["listing_data"] = {}
                session["state"] = "IDLE"
                session["fsm_state"] = FSM_STATE_IDLE
                session["draft_updated_at"] = None
                session["last_intent"] = None
                await save_session(user_id, request.channel, session)

        # ═══════════════════════════════════════════════════════════════════
        # 1.2 FSM STATE CHECK - LLM BYPASS for deterministic commands
        # ═══════════════════════════════════════════════════════════════════
        lower_msg = (request.message or "").lower().strip()
        normalized_cmd = re.sub(r"[^\wşğıöçü]+", " ", lower_msg, flags=re.UNICODE).strip()
        fsm_state = session.get("fsm_state", FSM_STATE_IDLE)
        
        # DEBUG: Log FSM state for troubleshooting
        logger.info(f"FSM state check: fsm_state={fsm_state}, msg={lower_msg}")
        
        if fsm_state == FSM_STATE_PENDING_CONFIRMATION:
            # In confirmation state - check for deterministic commands
            fsm_command = FSM_COMMANDS.get(normalized_cmd)
            
            if fsm_command:
                # Deterministic command - LLM BYPASSED
                logger.info(f"FSM: PENDING_CONFIRMATION state, command={fsm_command}, bypassing LLM")
                return await _fsm_handle_confirmation(user_id, request.channel, session, fsm_command)
            # Not a command - allow free-form edits via LLM
            session["fsm_state"] = FSM_STATE_DRAFTING
            await save_session(user_id, request.channel, session)
            logger.info("FSM: PENDING_CONFIRMATION state, non-command received, routing to LLM for edits")
        
        # 1.3 Detail command handling (uses last search cache)
        detail_match = re.search(r"(\d+)\s*nolu\s*ilan", lower_msg)
        if detail_match and ("detay" in lower_msg or "goster" in lower_msg or "göster" in lower_msg):
            idx = int(detail_match.group(1)) - 1
            search_cache = session.get("search_cache") or []
            if 0 <= idx < len(search_cache):
                return await _format_listing_detail_response(search_cache[idx])

        # 1.4 Pagination command handling (uses last search cache)
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
        
        # 1.5 Preview/Son hal shortcut - skip LLM if user just wants to see current draft
        preview_keywords = ["son hal", "önizleme", "preview", "göster bana", "goster bana"]
        if any(kw in lower_msg for kw in preview_keywords) and session.get("listing_data"):
            current_listing = session.get("listing_data", {})
            if current_listing.get("title"):  # At least title exists
                preview = _format_preview(current_listing)
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
        _, missing_fields = FSMEngine.validate(current_listing) if current_listing else (False, ["title", "price", "category"])
        
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
            
            # Save to session for future reference
            session["last_intent"] = "CHAT"
            session["last_price_query"] = query
            if price_result.get("suggested_price"):
                session["last_suggested_price"] = price_result["suggested_price"]
            await save_session(user_id, request.channel, session)
            
            return MessageResponse(
                success=True,
                text=price_result["response"],
                buttons=[
                    ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
                    ButtonResponse(text="🔍 Ürün Ara", payload="aramak istiyorum"),
                ],
                metadata={"intent": "PRICE_RESEARCH", "tool": "perplexity", "price": price_result.get("suggested_price")},
            )
        
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
    
    logger.info(f"CREATE: current listing data: {current}")
    logger.info(f"CREATE: user_confirmed={brain_output.user_confirmed}, ready_for_fsm={brain_output.ready_for_fsm}")
    
    # FSM validates
    is_valid, missing = FSMEngine.validate(current)
    
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

    # Channels like WhatsApp may not render `listing_preview`; include a compact preview in text.
    if channel != "webchat" and current.get("title") and "📋" not in response_text:
        response_text = f"{response_text}\n\n{_format_preview(current)}"
    
    # Check if user wants to publish - use direct confirmation detection on user message
    from core.brain import Guardrails
    user_wants_to_publish = Guardrails.detect_confirmation(user_message)
    
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
            "suggestions": brain_output.suggestions,
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
                    title = listing.get("title", "İsimsiz")
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
    
    session.setdefault("conversation_history", []).append({
        "role": "assistant",
        "content": brain_output.response_text,
    })
    await save_session(user_id, channel, session)
    
    return MessageResponse(
        success=True,
        text=brain_output.response_text,
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


def _format_preview(listing: Dict[str, Any]) -> str:
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
