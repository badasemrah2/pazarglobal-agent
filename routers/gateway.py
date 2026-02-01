"""
Gateway Router - Unified API endpoint for all channels

Single endpoint: POST /api/v1/message

Flow:
1. Validate request
2. Load minimal session state from Redis (only: locked_intent, waiting_for, draft_id)
3. Safety check (if media)
4. Classify intent OR use locked_intent if in active flow
5. Route to handler
6. Save minimal state updates
7. Build response

Redis State (MINIMAL - prevents state poisoning):
- locked_intent: str | None  - Active flow (create/search)
- waiting_for: str | None    - Expected slot (title/price/etc)
- draft_id: str | None       - Supabase draft reference

All other data (slots, images) stays in Supabase active_drafts
"""
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from core.intent_classifier import IntentClassifier, Intent
from core.response_builder import create_builder

from handlers.listing_handler import listing_handler
from handlers.search_handler import search_handler
from handlers.publish_handler import publish_handler
from handlers.chat_handler import chat_handler

from services.vision_service import vision_service
from services.redis_client import redis_client
from services.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["gateway"])


# Minimal session state keys
SESSION_KEYS = ["locked_intent", "waiting_for", "draft_id"]


# Request/Response Models

class MessageRequest(BaseModel):
    """Incoming message request"""
    user_id: str = Field(..., description="User identifier")
    message: str = Field("", description="Text message")
    media_urls: Optional[List[str]] = Field(default=None, description="Media URLs")
    channel: str = Field("webchat", description="Channel: webchat, whatsapp, api")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Extra metadata")


class ButtonResponse(BaseModel):
    """Quick reply button"""
    text: str
    payload: str


class MessageResponse(BaseModel):
    """Outgoing message response"""
    success: bool = True
    text: str
    buttons: List[ButtonResponse] = []
    metadata: Dict[str, Any] = {}
    error: Optional[str] = None


# Main Endpoint

@router.post("/message", response_model=MessageResponse)
async def handle_message(request: MessageRequest) -> MessageResponse:
    """
    Main message handler.
    
    Receives messages from any channel and routes to appropriate handler.
    Uses minimal Redis state to maintain flow continuity.
    """
    logger.info(f"Gateway: user={request.user_id}, channel={request.channel}, media={bool(request.media_urls)}")
    
    try:
        # 1. Load minimal session state from Redis
        session = await redis_client.get_session(request.user_id) or {}
        locked_intent = session.get("locked_intent")  # Active flow
        waiting_for = session.get("waiting_for")      # Expected slot
        draft_id = session.get("draft_id")            # Supabase reference
        
        logger.debug(f"Session: locked_intent={locked_intent}, waiting_for={waiting_for}, draft_id={draft_id}")
        
        # 2. Check for cancel/reset command
        if _is_cancel_command(request.message):
            await redis_client.delete_session(request.user_id)
            return MessageResponse(
                success=True,
                text="✅ İşlem iptal edildi. Nasıl yardımcı olabilirim?",
            )
        
        # 3. Safety check for media
        if request.media_urls:
            safety_result = await vision_service.check_safety(request.media_urls[0])
            
            if not safety_result.get("safe", True):
                logger.warning(f"Blocked unsafe content: {safety_result.get('flagged_categories')}")
                return MessageResponse(
                    success=False,
                    text="🚫 Bu görsel politikalarımıza uygun değil.",
                    error="content_blocked",
                )
        
        # 4. Determine intent: use locked_intent if in active flow, else classify
        if locked_intent and not _is_flow_switch_command(request.message):
            # Continue existing flow
            intent = Intent(locked_intent)
            logger.info(f"Continuing locked flow: {intent.value}")
        else:
            # Classify new intent
            classifier = IntentClassifier()
            classification = classifier.classify(
                message=request.message,
                has_media=bool(request.media_urls),
            )
            intent = classification.intent
            logger.info(f"Classified intent: {intent.value} (confidence={classification.confidence})")
        
        # 5. Route to handler with session context
        response = await _route_to_handler(
            intent=intent,
            request=request,
            session_context={
                "locked_intent": locked_intent,
                "waiting_for": waiting_for,
                "draft_id": draft_id,
            }
        )
        
        # 6. Update minimal session state based on response metadata
        new_session = _extract_session_updates(response.metadata, intent)
        if new_session:
            await redis_client.set_session(request.user_id, new_session)
        
        # 7. Build response
        return MessageResponse(
            success=True,
            text=response.text,
            buttons=[
                ButtonResponse(text=b.text, payload=b.payload)
                for b in response.buttons
            ],
            metadata=response.metadata,
        )
    
    except Exception as e:
        logger.error(f"Gateway error: {e}", exc_info=True)
        return MessageResponse(
            success=False,
            text="⚠️ Bir hata oluştu. Lütfen tekrar deneyin.",
            error=str(e),
        )


def _is_cancel_command(message: str) -> bool:
    """Check if message is a cancel/reset command"""
    cancel_words = ["iptal", "vazgeç", "vazgec", "cancel", "sıfırla", "sifirla", "reset"]
    return message.lower().strip() in cancel_words


def _is_flow_switch_command(message: str) -> bool:
    """Check if user explicitly wants to switch flows"""
    switch_patterns = [
        "ilan vermek istiyorum",
        "satmak istiyorum", 
        "aramak istiyorum",
        "ilanlarım",
        "ilanlarim",
    ]
    msg_lower = message.lower().strip()
    return any(pattern in msg_lower for pattern in switch_patterns)


def _extract_session_updates(metadata: Dict[str, Any], intent: Intent) -> Optional[Dict[str, Any]]:
    """
    Extract minimal session state from handler response.
    
    ONLY these keys are stored in Redis:
    - locked_intent: Active flow to continue
    - waiting_for: Expected slot type
    - draft_id: Supabase draft reference
    
    Returns None if session should be cleared (flow complete)
    """
    if not metadata:
        return None
    
    # Check if flow is complete
    if metadata.get("flow_complete"):
        return None  # Clear session
    
    session = {}
    
    # Lock intent if handler indicates active flow
    if metadata.get("continue_flow"):
        session["locked_intent"] = intent.value
    
    # Track waiting slot
    if metadata.get("waiting_for"):
        session["waiting_for"] = metadata["waiting_for"]
    
    # Track draft reference
    if metadata.get("draft_id"):
        session["draft_id"] = metadata["draft_id"]
    
    return session if session else None


async def _route_to_handler(
    intent: Intent, 
    request: MessageRequest,
    session_context: Optional[Dict[str, Any]] = None,
):
    """Route request to appropriate handler with session context"""
    
    if intent == Intent.CREATE:
        return await listing_handler.handle(
            user_id=request.user_id,
            message=request.message,
            media_urls=request.media_urls,
            channel=request.channel,
            session_context=session_context,
        )
    
    elif intent == Intent.SEARCH:
        return await search_handler.handle(
            user_id=request.user_id,
            message=request.message,
            channel=request.channel,
        )
    
    elif intent == Intent.PUBLISH:
        # Determine specific action from message
        message_lower = request.message.lower().strip()
        
        if any(w in message_lower for w in ["sil", "kaldır", "kaldir", "delete"]):
            # Need listing_id from metadata
            listing_id = request.metadata.get("listing_id") if request.metadata else None
            if listing_id:
                return await publish_handler.delete_listing(
                    user_id=request.user_id,
                    listing_id=listing_id,
                    channel=request.channel,
                )
        
        elif any(w in message_lower for w in ["satıldı", "satildi", "sold"]):
            listing_id = request.metadata.get("listing_id") if request.metadata else None
            if listing_id:
                return await publish_handler.mark_sold(
                    user_id=request.user_id,
                    listing_id=listing_id,
                    channel=request.channel,
                )
        
        elif any(w in message_lower for w in ["ilanlarım", "ilanlarim", "my listings"]):
            return await publish_handler.get_my_listings(
                user_id=request.user_id,
                channel=request.channel,
            )
        
        # Default: publish current draft (check session_context first, then metadata)
        draft_id = None
        if session_context and session_context.get("draft_id"):
            draft_id = session_context["draft_id"]
        elif request.metadata:
            draft_id = request.metadata.get("draft_id")
            
        if draft_id:
            return await publish_handler.publish_draft(
                user_id=request.user_id,
                draft_id=draft_id,
                channel=request.channel,
            )
        
        # Fallback to chat
        return await chat_handler.handle(
            user_id=request.user_id,
            message=request.message,
            channel=request.channel,
        )
    
    else:  # Intent.CHAT or unknown
        return await chat_handler.handle(
            user_id=request.user_id,
            message=request.message,
            channel=request.channel,
        )


# Health Check

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "pazarglobal-agent",
        "version": "2.0.0",
    }


# Media Analysis (standalone)

class MediaAnalyzeRequest(BaseModel):
    """Media analysis request"""
    url: str
    user_id: Optional[str] = None


class MediaAnalyzeResponse(BaseModel):
    """Media analysis response"""
    safe: bool
    product: Optional[str] = None
    category: Optional[str] = None
    suggested_price: Optional[float] = None
    flagged_categories: List[str] = []


@router.post("/media/analyze", response_model=MediaAnalyzeResponse)
async def analyze_media(request: MediaAnalyzeRequest) -> MediaAnalyzeResponse:
    """
    Analyze media for safety and product info.
    
    Standalone endpoint for frontend preview.
    """
    try:
        # 1. Safety check
        safety_result = await vision_service.check_safety(request.url)
        
        if not safety_result.get("safe", True):
            return MediaAnalyzeResponse(
                safe=False,
                flagged_categories=safety_result.get("flagged_categories", []),
            )
        
        # 2. Product analysis
        analysis = await vision_service.analyze_product(request.url)
        
        # 3. Price suggestion
        suggested_price = None
        if analysis.get("product"):
            suggested_price = await vision_service.get_price_suggestion(analysis)
        
        return MediaAnalyzeResponse(
            safe=True,
            product=analysis.get("product"),
            category=analysis.get("category"),
            suggested_price=suggested_price,
        )
    
    except Exception as e:
        logger.error(f"Media analysis error: {e}")
        return MediaAnalyzeResponse(safe=True)  # Fail-open


# Draft Management

class DraftResponse(BaseModel):
    """Draft info response"""
    id: str
    state: str
    title: Optional[str] = None
    price: Optional[float] = None
    images: List[str] = []


@router.get("/draft/{user_id}")
async def get_draft(user_id: str) -> Optional[DraftResponse]:
    """Get user's active draft"""
    from services.supabase_client import supabase_client
    
    try:
        result = supabase_client.table("active_drafts")\
            .select("*")\
            .eq("user_id", user_id)\
            .limit(1)\
            .execute()
        
        if not result.data:
            return None
        
        draft = result.data[0]
        return DraftResponse(
            id=draft.get("id"),
            state=draft.get("state", "IDLE"),
            title=draft.get("title"),
            price=draft.get("price"),
            images=draft.get("images") or [],
        )
    
    except Exception as e:
        logger.error(f"Get draft error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/draft/{user_id}")
async def delete_draft(user_id: str) -> Dict[str, bool]:
    """Delete user's active draft"""
    from services.supabase_client import supabase_client
    
    try:
        supabase_client.table("active_drafts")\
            .delete()\
            .eq("user_id", user_id)\
            .execute()
        
        return {"success": True}
    
    except Exception as e:
        logger.error(f"Delete draft error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
