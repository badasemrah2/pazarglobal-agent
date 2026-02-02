"""
PazarGlobal Agent V3 - Unified Gateway

Tek giriş noktası: POST /api/v3/message

Flow:
1. Auth & Session load
2. Brain (single LLM) → intent + listing_data
3. Route to FSM (CREATE or SEARCH)
4. Response
"""
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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

async def load_session(user_id: str) -> Dict[str, Any]:
    """Load session from Redis"""
    session = await redis_client.get_session(user_id) or {}
    return {
        "listing_data": session.get("listing_data", {}),
        "draft_id": session.get("draft_id"),
        "state": session.get("state", "IDLE"),  # IDLE, DRAFTING, PREVIEW
        "conversation_history": session.get("conversation_history", []),
    }


async def save_session(user_id: str, session: Dict[str, Any]):
    """Save session to Redis"""
    # Keep conversation history short
    history = session.get("conversation_history", [])
    if len(history) > 20:
        history = history[-20:]
    session["conversation_history"] = history
    
    await redis_client.set_session(user_id, session)


# ═══════════════════════════════════════════════════════════════════
# MAIN ENDPOINT
# ═══════════════════════════════════════════════════════════════════

@router.post("/message", response_model=MessageResponse)
async def handle_message(request: MessageRequest) -> MessageResponse:
    """
    V3 Gateway - Single LLM brain handles everything.
    """
    logger.info(f"V3 Gateway: user={request.user_id}, msg={request.message[:50]}...")
    
    try:
        # 1. Load session
        session = await load_session(request.user_id)
        
        # 2. Check for cancel command
        if _is_cancel(request.message):
            await _handle_cancel(request.user_id, session)
            return MessageResponse(
                success=True,
                text="✅ İşlem iptal edildi. Size nasıl yardımcı olabilirim?",
                buttons=[
                    ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
                    ButtonResponse(text="🔍 Ürün Ara", payload="aramak istiyorum"),
                ],
            )
        
        # 3. Call Brain (single LLM)
        brain_output = await brain.process(
            message=request.message,
            current_listing=session.get("listing_data"),
            images=request.media_urls,
            conversation_history=session.get("conversation_history"),
        )
        
        # 4. Handle Perplexity tool call if requested
        if brain_output.tool_call and brain_output.tool_call.get("name") == "perplexity":
            price_result = await _call_perplexity(brain_output.tool_call["query"])
            if price_result:
                # Add price to response
                brain_output.response_text += f"\n\n💰 Piyasa araştırması: **{price_result:,.0f} TL** civarı"
        
        # 5. Route based on intent
        if brain_output.intent == Intent.CREATE:
            return await _handle_create(request.user_id, session, brain_output)
        
        elif brain_output.intent == Intent.SEARCH:
            return await _handle_search(request.user_id, request.message, brain_output)
        
        else:  # CHAT
            return await _handle_chat(request.user_id, session, brain_output)
    
    except Exception as e:
        logger.error(f"V3 Gateway error: {e}", exc_info=True)
        return MessageResponse(
            success=False,
            text="⚠️ Bir hata oluştu. Lütfen tekrar deneyin.",
            error=str(e),
        )


# ═══════════════════════════════════════════════════════════════════
# INTENT HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def _handle_create(user_id: str, session: Dict, brain_output: BrainOutput) -> MessageResponse:
    """CREATE intent - İlan oluşturma FSM"""
    
    # Merge new data with existing
    current = session.get("listing_data", {})
    for key, value in brain_output.listing_data.items():
        if value is not None:
            current[key] = value
    
    # Update session
    session["listing_data"] = current
    session["state"] = "PREVIEW" if brain_output.ready_to_publish else "DRAFTING"
    
    # Add to conversation history
    session.setdefault("conversation_history", []).append({
        "role": "assistant",
        "content": brain_output.response_text,
    })
    
    await save_session(user_id, session)
    
    # Build buttons
    buttons = []
    if brain_output.ready_to_publish:
        buttons = [
            ButtonResponse(text="✅ Yayınla", payload="yayınla"),
            ButtonResponse(text="✏️ Düzenle", payload="düzenle"),
            ButtonResponse(text="❌ İptal", payload="iptal"),
        ]
    
    # Check for publish command
    msg_lower = session.get("conversation_history", [{}])[-1].get("content", "").lower() if session.get("conversation_history") else ""
    if "yayınla" in brain_output.response_text.lower() or brain_output.ready_to_publish:
        if any(p in msg_lower for p in ["yayınla", "yayinla", "paylaş", "publish"]):
            return await _publish_listing(user_id, session, current)
    
    return MessageResponse(
        success=True,
        text=brain_output.response_text,
        buttons=buttons,
        listing_preview=current if current else None,
        metadata={
            "intent": "CREATE",
            "state": session["state"],
            "missing_fields": brain_output.missing_fields,
        },
    )


async def _handle_search(user_id: str, query: str, brain_output: BrainOutput) -> MessageResponse:
    """SEARCH intent - SearchComposerAgent'a delege et"""
    
    try:
        # Use the battle-tested SearchComposerAgent
        from agents.search_agents import SearchComposerAgent
        search_agent = SearchComposerAgent()
        
        # Call orchestrate_search (not run)
        result = await search_agent.orchestrate_search(user_message=query)
        
        # Parse result
        if isinstance(result, dict):
            # Get message from search agent (already formatted nicely)
            message = result.get("message", "")
            listings = result.get("listings", [])
            
            if message:
                return MessageResponse(
                    success=True,
                    text=message,
                    metadata={"intent": "SEARCH", "count": result.get("count", len(listings))},
                )
            elif listings:
                # Format results
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
                return MessageResponse(
                    success=True,
                    text=f"🔍 Aramanıza uygun ilan bulunamadı. Farklı kelimelerle deneyin.",
                    buttons=[
                        ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
                    ],
                    metadata={"intent": "SEARCH", "count": 0},
                )
        else:
            # Agent returned string
            return MessageResponse(
                success=True,
                text=str(result),
                metadata={"intent": "SEARCH"},
            )
    
    except Exception as e:
        logger.error(f"Search error: {e}")
        return MessageResponse(
            success=True,
            text="🔍 Arama yapılırken bir sorun oluştu. Lütfen tekrar deneyin.",
        )


async def _handle_chat(user_id: str, session: Dict, brain_output: BrainOutput) -> MessageResponse:
    """CHAT intent - Genel sohbet"""
    
    # Add to history
    session.setdefault("conversation_history", []).append({
        "role": "assistant",
        "content": brain_output.response_text,
    })
    await save_session(user_id, session)
    
    return MessageResponse(
        success=True,
        text=brain_output.response_text,
        buttons=[
            ButtonResponse(text="📸 İlan Ver", payload="ilan vermek istiyorum"),
            ButtonResponse(text="🔍 Ürün Ara", payload="aramak istiyorum"),
            ButtonResponse(text="📋 İlanlarım", payload="ilanlarım"),
        ],
        metadata={"intent": "CHAT"},
    )


# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def _is_cancel(message: str) -> bool:
    """Check if message is cancel command"""
    cancel_words = ["iptal", "vazgeç", "vazgec", "cancel", "sıfırla", "reset"]
    return message.lower().strip() in cancel_words


async def _handle_cancel(user_id: str, session: Dict):
    """Handle cancel - delete draft and reset session"""
    draft_id = session.get("draft_id")
    if draft_id:
        try:
            supabase_client.client.table("active_drafts").delete().eq("id", draft_id).execute()
        except Exception as e:
            logger.error(f"Failed to delete draft: {e}")
    
    await redis_client.delete_session(user_id)


async def _call_perplexity(query: str) -> Optional[float]:
    """Call Perplexity API for price research"""
    try:
        # Edge function call
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


async def _publish_listing(user_id: str, session: Dict, listing_data: Dict) -> MessageResponse:
    """Publish listing - wallet check + DB insert"""
    
    try:
        # 1. Wallet check
        wallet = supabase_client.client.table("wallets").select("balance").eq("user_id", user_id).single().execute()
        
        if not wallet.data or wallet.data.get("balance", 0) < 55:
            return MessageResponse(
                success=False,
                text="💳 Bakiyeniz yetersiz. Kredi yüklemek için pazarglobal.com/wallet adresini ziyaret edin.",
                listing_preview=listing_data,
                buttons=[ButtonResponse(text="✏️ Düzenle", payload="düzenle")],
            )
        
        # 2. Deduct credit
        new_balance = wallet.data["balance"] - 55
        supabase_client.client.table("wallets").update({"balance": new_balance}).eq("user_id", user_id).execute()
        
        # 3. Insert listing
        listing_data["user_id"] = user_id
        listing_data["status"] = "active"
        result = supabase_client.client.table("listings").insert(listing_data).execute()
        
        # 4. Clear session
        await redis_client.delete_session(user_id)
        
        listing_id = result.data[0]["id"] if result.data else "unknown"
        
        return MessageResponse(
            success=True,
            text=f"🎉 İlanınız yayınlandı!\n\n📋 **{listing_data.get('title')}**\n💰 {listing_data.get('price', 0):,.0f} TL\n\n🔗 pazarglobal.com/listing/{listing_id}",
            buttons=[
                ButtonResponse(text="📸 Yeni İlan", payload="ilan vermek istiyorum"),
                ButtonResponse(text="📋 İlanlarım", payload="ilanlarım"),
            ],
            metadata={"listing_id": listing_id},
        )
    
    except Exception as e:
        logger.error(f"Publish error: {e}")
        return MessageResponse(
            success=False,
            text="⚠️ İlan yayınlanırken bir hata oluştu. Lütfen tekrar deneyin.",
            listing_preview=listing_data,
        )
