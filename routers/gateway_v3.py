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
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import re
import uuid
from datetime import datetime

from core.brain import brain, BrainOutput, Intent
from services.redis_client import redis_client
from services.supabase_client import supabase_client
from services.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v3", tags=["gateway-v3"])


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


# ═══════════════════════════════════════════════════════════════════
# FSM ENGINE - Deterministic JSON Processing
# ═══════════════════════════════════════════════════════════════════

class FSMEngine:
    """
    FSM Engine - JSON validasyon ve publish
    
    Görevler:
    - JSON validasyon (schema kontrolü)
    - Keywords üretme
    - Wallet kontrolü
    - İlan yayınlama
    
    NOT: Resim zorunlu DEĞİL
    """
    
    REQUIRED_FIELDS = ["title", "price", "category"]
    
    ALLOWED_CATEGORIES = {
        "Elektronik", "Otomotiv", "Emlak", "Mobilya & Dekorasyon",
        "Moda & Aksesuar", "Spor & Hobi", "Hobi, Koleksiyon & Sanat", "Diğer"
    }
    
    ALLOWED_CONDITIONS = {"Sıfır", "Az Kullanılmış", "2. El"}
    
    @classmethod
    def validate(cls, listing_data: Dict[str, Any]) -> tuple[bool, List[str]]:
        """
        JSON validasyon - FSM'in beklediği formata uygun mu?
        
        Returns:
            (is_valid, missing_fields)
        """
        missing = []
        
        # Title
        if not listing_data.get("title"):
            missing.append("title")
        
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
        
        # Category
        category = listing_data.get("category")
        if not category or category not in cls.ALLOWED_CATEGORIES:
            missing.append("category")
        
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
            result = supabase_client.client.table("wallets").select("balance").eq("user_id", user_id).single().execute()
            
            if not result.data:
                logger.warning(f"No wallet found for user_id: {user_id}, creating one with 0 balance")
                # Auto-create wallet with 0 balance
                try:
                    supabase_client.client.table("wallets").insert({
                        "user_id": user_id,
                        "balance": 0
                    }).execute()
                except Exception as create_err:
                    logger.error(f"Failed to create wallet: {create_err}")
                return False, 0.0
            
            balance = float(result.data.get("balance", 0))
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
            # Get current balance
            result = supabase_client.client.table("wallets").select("balance").eq("user_id", user_id).single().execute()
            
            if not result.data:
                logger.error(f"No wallet found for deduction: {user_id}")
                return False
            
            current = float(result.data.get("balance", 0))
            new_balance = current - amount
            
            if new_balance < 0:
                logger.warning(f"Insufficient balance for {user_id}: {current} < {amount}")
                return False
            
            # Update
            supabase_client.client.table("wallets").update({"balance": new_balance}).eq("user_id", user_id).execute()
            logger.info(f"Deducted {amount} TL from {user_id}. New balance: {new_balance}")
            return True
            
        except Exception as e:
            logger.error(f"Deduct credit error for {user_id}: {e}", exc_info=True)
            return False
    
    @classmethod
    async def publish(cls, user_id: str, listing_data: Dict[str, Any]) -> tuple[bool, str, Optional[str]]:
        """
        İlan yayınla
        
        Returns:
            (success, message, listing_id)
        """
        try:
            # 1. Validate
            is_valid, missing = cls.validate(listing_data)
            if not is_valid:
                return False, f"Eksik alanlar: {', '.join(missing)}", None
            
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
            
            # Build metadata
            metadata = listing_data.get("metadata", {})
            metadata["keywords_text"] = keywords_text
            
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
                "images": listing_data.get("images", []),
                "image_url": listing_data.get("images", [None])[0] if listing_data.get("images") else None,
                "metadata": metadata,
            }
            
            # 5. Insert to Supabase
            result = supabase_client.client.table("listings").insert(final_listing).execute()
            
            if not result.data:
                # Refund credit
                await cls.deduct_credit(user_id, -55.0)
                return False, "İlan kaydedilemedi. Lütfen tekrar deneyin.", None
            
            return True, "İlan başarıyla yayınlandı!", listing_id
            
        except Exception as e:
            logger.error(f"Publish error: {e}", exc_info=True)
            return False, f"Yayınlama hatası: {str(e)}", None


# ═══════════════════════════════════════════════════════════════════
# MAIN ENDPOINT
# ═══════════════════════════════════════════════════════════════════

@router.post("/message", response_model=MessageResponse)
async def handle_message(request: MessageRequest) -> MessageResponse:
    """
    V3 Gateway - LLM Brain + FSM Engine
    """
    logger.info(f"V3: user={request.user_id}, msg={request.message[:50]}...")
    
    try:
        # 1. Load session (channel scoped)
        session = await load_session(request.user_id, request.channel)

        # 1.1 Draft TTL check (10 minutes)
        draft_updated_at = session.get("draft_updated_at")
        if draft_updated_at:
            try:
                last_ts = datetime.fromisoformat(draft_updated_at)
                if (datetime.utcnow() - last_ts).total_seconds() > 600:
                    session["listing_data"] = {}
                    session["state"] = "IDLE"
                    session["draft_updated_at"] = None
                    session["last_intent"] = None
                    await save_session(request.user_id, request.channel, session)
            except Exception:
                # If parsing fails, reset draft defensively
                session["listing_data"] = {}
                session["state"] = "IDLE"
                session["draft_updated_at"] = None
                session["last_intent"] = None
                await save_session(request.user_id, request.channel, session)

        # 1.2 Detail command handling (uses last search cache)
        lower_msg = (request.message or "").lower()
        detail_match = re.search(r"(\d+)\s*nolu\s*ilan", lower_msg)
        if detail_match and ("detay" in lower_msg or "goster" in lower_msg or "göster" in lower_msg):
            idx = int(detail_match.group(1)) - 1
            search_cache = session.get("search_cache") or []
            if 0 <= idx < len(search_cache):
                return await _format_listing_detail_response(search_cache[idx])
        
        # 1.3 Preview/Son hal shortcut - skip LLM if user just wants to see current draft
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
        
        # Pre-calculate missing fields
        _, missing_fields = FSMEngine.validate(current_listing) if current_listing else (False, ["title", "price", "category"])
        
        # 3. Call Brain with rich context
        brain_output = await brain.process(
            message=request.message,
            current_listing=current_listing,
            images=request.media_urls,
            conversation_history=session.get("conversation_history"),
            # Zengin context
            fsm_state=fsm_state,
            missing_fields=missing_fields,
            last_intent=last_intent,
        )
        
        # 4. Handle by intent
        if brain_output.intent == Intent.CANCEL:
            # LLM override - reset
            await clear_session(request.user_id, request.channel)
            return MessageResponse(
                success=True,
                text=brain_output.response_text,
                buttons=[
                    ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
                    ButtonResponse(text="🔍 Ürün Ara", payload="aramak istiyorum"),
                ],
                metadata={"intent": "CANCEL"},
            )
        
        elif brain_output.intent == Intent.SEARCH:
            # Save last intent
            session["last_intent"] = "SEARCH"
            await save_session(request.user_id, request.channel, session)
            return await _handle_search(request.user_id, request.channel, session, request.message)
        
        elif brain_output.intent == Intent.CREATE:
            return await _handle_create(request.user_id, request.channel, session, brain_output, request.message)
        
        else:  # CHAT
            # IMPORTANT: Check if user is trying to confirm an existing draft
            # Even if Brain says CHAT, if there's an active draft and user confirms, publish it!
            from core.brain import Guardrails
            if last_intent == "CREATE" and current_listing and Guardrails.detect_confirmation(request.message):
                logger.info(f"User confirming existing draft via CHAT intent, routing to CREATE handler")
                return await _handle_create(request.user_id, request.channel, session, brain_output, request.message)
            
            session["last_intent"] = "CHAT"
            return await _handle_chat(request.user_id, request.channel, session, brain_output)
    
    except Exception as e:
        logger.error(f"V3 error: {e}", exc_info=True)
        return MessageResponse(
            success=False,
            text="⚠️ Bir hata oluştu. Lütfen tekrar deneyin.",
            error=str(e),
        )


# ═══════════════════════════════════════════════════════════════════
# INTENT HANDLERS
# ═══════════════════════════════════════════════════════════════════

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
    
    # Ensure images are preserved (don't overwrite with empty list)
    if existing_images and not current.get("images"):
        current["images"] = existing_images
    
    logger.info(f"CREATE: current listing data: {current}")
    logger.info(f"CREATE: user_confirmed={brain_output.user_confirmed}, ready_for_fsm={brain_output.ready_for_fsm}")
    
    # FSM validates
    is_valid, missing = FSMEngine.validate(current)
    
    # Update session
    session["listing_data"] = current
    session["state"] = "READY" if is_valid else "DRAFTING"
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
    response_text = brain_output.response_text
    if brain_output.tool_call and brain_output.tool_call.get("name") == "perplexity":
        price_result = await _call_perplexity(brain_output.tool_call["query"])
        if price_result:
            response_text += f"\n\n💰 **Piyasa Araştırması:** {price_result:,.0f} TL civarı"
    
    # Check if user confirmed - use direct confirmation detection on user message
    # because brain_output.user_confirmed depends on Brain's listing_data which may be incomplete
    from core.brain import Guardrails
    user_wants_to_publish = Guardrails.detect_confirmation(user_message)
    
    logger.info(f"CREATE: is_valid={is_valid}, user_wants_to_publish={user_wants_to_publish}")
    
    if user_wants_to_publish and is_valid:
        # FSM → Publish
        logger.info(f"Publishing listing for user {user_id}: {current}")
        success, message, listing_id = await FSMEngine.publish(user_id, current)
        
        if success:
            await clear_session(user_id, channel)
            return MessageResponse(
                success=True,
                text=f"🎉 **İlanınız Yayınlandı!**\n\n📋 {current.get('title')}\n💰 {current.get('price', 0):,.0f} TL\n📍 {current.get('location', 'Belirtilmemiş')}\n\n🔗 pazarglobal.com/listing/{listing_id}",
                buttons=[
                    ButtonResponse(text="📸 Yeni İlan", payload="yeni ilan vermek istiyorum"),
                    ButtonResponse(text="📋 İlanlarım", payload="ilanlarım"),
                ],
                metadata={"intent": "CREATE", "listing_id": listing_id, "published": True},
            )
        else:
            # Publish failed - return error
            logger.error(f"Publish failed for user {user_id}: {message}")
            return MessageResponse(
                success=False,
                text=message,
                listing_preview=current,
                buttons=[ButtonResponse(text="🔄 Tekrar Dene", payload="yayınla")],
                metadata={"intent": "CREATE", "error": message},
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
            
            if message:
                session["search_cache"] = listings or []
                await save_session(user_id, channel, session)
                return MessageResponse(
                    success=True,
                    text=message,
                    metadata={"intent": "SEARCH", "count": result.get("count", len(listings))},
                )
            elif listings:
                session["search_cache"] = listings
                await save_session(user_id, channel, session)
                results_text = f"🔍 **{len(listings)} sonuç bulundu:**\n\n"
                for i, listing in enumerate(listings[:5], 1):
                    title = listing.get("title", "İsimsiz")
                    price = listing.get("price", 0)
                    results_text += f"{i}. **{title}**\n   💰 {price:,.0f} TL\n\n"
                
                return MessageResponse(
                    success=True,
                    text=results_text,
                    metadata={"intent": "SEARCH", "count": len(listings)},
                )
            else:
                session["search_cache"] = []
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

def _format_preview(listing: Dict[str, Any]) -> str:
    """Format current draft as preview text."""
    lines = ["📋 İlan Önizleme:"]
    
    title = listing.get("title")
    price = listing.get("price")
    category = listing.get("category")
    description = listing.get("description")
    condition = listing.get("condition")
    location = listing.get("location")
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
    
    # Get primary image
    image_url = listing.get("image_url")
    images = listing.get("images") if isinstance(listing.get("images"), list) else []
    primary_image = image_url or (images[0] if images else None)
    
    # Get owner info
    owner_name = listing.get("user_name")
    owner_phone = listing.get("user_phone")
    
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
    """Perplexity API - fiyat araştırması"""
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
