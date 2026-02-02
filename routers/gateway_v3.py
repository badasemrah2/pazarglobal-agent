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
    - Otomatik kategori belirleme (category_library kullanarak)
    - Keywords üretme
    - Wallet kontrolü
    - İlan yayınlama
    
    NOT: Resim zorunlu DEĞİL
    """
    
    REQUIRED_FIELDS = ["title", "price", "category"]
    
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
        
        # Category - otomatik belirleme dene
        category = listing_data.get("category")
        
        # Önce normalize et (kullanıcı "Tarım&Gıda" yazmışsa "Tarım & Gıda" yap)
        if category:
            normalized = cls.normalize_category_id(category)
            if normalized:
                listing_data["category"] = normalized
                category = normalized
        
        # Kategori hala geçersizse, başlık ve açıklamadan otomatik belirle
        if not category or category not in cls.ALLOWED_CATEGORIES:
            title = listing_data.get("title", "")
            description = listing_data.get("description", "")
            auto_category = cls.classify_category(f"{title} {description}")
            if auto_category:
                listing_data["category"] = auto_category
                category = auto_category
                logger.info(f"Auto-classified category: {auto_category} from title/desc")
        
        # Son kontrol
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
            # Use .limit(1) instead of .single() to avoid exception when no row found
            # Column name is balance_bigint (not balance) in actual Supabase table
            result = supabase_client.client.table("wallets").select("balance_bigint").eq("user_id", user_id).limit(1).execute()
            
            if not result.data or len(result.data) == 0:
                logger.warning(f"No wallet found for user_id: {user_id}, creating one with 0 balance")
                # First ensure profile exists (foreign key constraint)
                await cls.ensure_profile_exists(user_id)
                # Auto-create wallet with 0 balance
                try:
                    supabase_client.client.table("wallets").insert({
                        "user_id": user_id,
                        "balance_bigint": 0
                    }).execute()
                    logger.info(f"Created wallet with 0 balance for {user_id}")
                except Exception as create_err:
                    logger.error(f"Failed to create wallet: {create_err}")
                return False, 0.0
            
            balance = float(result.data[0].get("balance_bigint", 0))
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
            result = supabase_client.client.table("wallets").select("balance_bigint").eq("user_id", user_id).limit(1).execute()
            
            if not result.data or len(result.data) == 0:
                logger.error(f"No wallet found for deduction: {user_id}")
                return False
            
            current = float(result.data[0].get("balance_bigint", 0))
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
        
        # NOTE: Deterministik price detection kaldırıldı!
        # Artık Brain (LLM) native function calling ile Perplexity tool'unu çağırıyor.
        # LLM "kaç para eder" gibi sorguları algılayıp tool_call döndürüyor.
        
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
            await save_session(request.user_id, request.channel, session)
            
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
    """Perplexity API - fiyat araştırması ile detaylı cevap"""
    try:
        logger.info(f"Calling Perplexity for price research: {query}")
        
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
        
        data = result.get("data", {})
        suggested_price = data.get("suggested_price")
        price_range = data.get("price_range", {})
        reasoning = data.get("reasoning", "")
        
        if suggested_price:
            price_float = float(suggested_price)
            min_price = price_range.get("min", price_float * 0.8)
            max_price = price_range.get("max", price_float * 1.2)
            
            response = f"""🔍 **{query}** için fiyat araştırması:

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
                "response": response,
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
async def analyze_media(request: MediaAnalyzeRequest) -> MediaAnalyzeResponse:
    """
    Analyze uploaded media for product information.
    
    Flow:
    1. Check safety (content moderation)
    2. Analyze product (GPT-4 Vision)
    3. Save images to session/draft
    4. Return description of what AI sees
    """
    logger.info(f"Media analyze: user={request.user_id}, urls={len(request.media_urls)}")
    
    if not request.media_urls:
        return MediaAnalyzeResponse(
            success=False,
            message="Görsel bulunamadı. Lütfen bir görsel yükleyin.",
        )
    
    try:
        from services.vision_service import vision_service
        
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
        session = await load_session(request.user_id, "webchat")
        
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
        await save_session(request.user_id, "webchat", session)
        
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
