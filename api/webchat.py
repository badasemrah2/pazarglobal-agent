"""
WebChat API endpoints for frontend integration
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from loguru import logger
from services import redis_client
from agents import IntentRouterAgent, ComposerAgent, PublishDeleteAgent, SearchComposerAgent, SmallTalkAgent
import json
import uuid
import re

# In-memory cache for last search results (when Redis is disabled)
LAST_SEARCH_CACHE: Dict[str, list] = {}

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

    message_parts = ["📋 Taslak durumu güncellendi."]
    if summary_lines:
        message_parts.append("\n".join(summary_lines))

    if missing:
        message_parts.append(
            "Eksik bilgiler: " + ", ".join(missing) + ". Lütfen bu detayları yazarak veya fotoğraf yükleyerek paylaşın."
        )
    else:
        message_parts.append("Tüm temel bilgiler tamam. Hazırsanız 'yayınla' yazarak ilanı yayınlayabilirsiniz.")

    return "\n\n".join(part.strip() for part in message_parts if part.strip())

router = APIRouter(prefix="/webchat", tags=["webchat"])


class ChatMessage(BaseModel):
    """Chat message model"""
    session_id: str
    message: str
    user_id: Optional[str] = None
    media_url: Optional[str] = None


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
    try:
        # Support both single and multiple media URLs
        all_media_urls = media_urls or (
            [media_url] if media_url else []
        )
        # Get or create session (safe even if redis is disabled)
        session = await redis_client.get_session(session_id)
        # Normalize session to dict to avoid attribute errors
        if session is None or not isinstance(session, dict):
            session = {
                "user_id": user_id,
                "intent": None,
                "active_draft_id": None
            }

        raw_user_id = session.get("user_id") or user_id
        normalized_user_id = normalize_user_id(raw_user_id)
        session["user_id"] = normalized_user_id
        user_id = normalized_user_id
        # Persist only if redis is enabled
        if not getattr(redis_client, "disabled", False):
            await redis_client.set_session(session_id, session)
        
        # Store message in history
        if not getattr(redis_client, "disabled", False):
            await redis_client.add_message(session_id, {
                "role": "user",
                "content": message_body,
                "timestamp": str(uuid.uuid1().time)
            })
        
        # Get or determine intent
        intent = session.get("intent")
        if not intent:
            router_agent = IntentRouterAgent()
            intent = await router_agent.classify_intent(message_body)
            session["intent"] = intent
            if not getattr(redis_client, "disabled", False):
                await redis_client.set_intent(session_id, intent)
            logger.info(f"WebChat intent for {session_id}: {intent}")
        
        response_data = {"intent": intent}
        
        # Route to appropriate agent
        if intent == "create_listing":
            composer = ComposerAgent()
            # Pass all media URLs to composer, which can distribute to image agents
            result = await composer.orchestrate_listing_creation(
                user_message=message_body,
                user_id=session.get("user_id"),
                phone_number=session_id,  # Use session_id as identifier
                draft_id=session.get("active_draft_id"),
                media_urls=all_media_urls  # Pass list of all media URLs
            )
            # Guard against unexpected None/invalid result
            if not result or not isinstance(result, dict):
                return {
                    "success": False,
                    "message": "Internal error: listing creation failed",
                    "data": None,
                    "intent": intent
                }

            if result.get("success"):
                session["active_draft_id"] = result["draft_id"]
                if not getattr(redis_client, "disabled", False):
                    await redis_client.set_active_draft(session_id, result["draft_id"])
                
                draft = result["draft"]
                response_text = build_draft_status_message(draft)
                
                response_data.update({
                    "draft_id": result["draft_id"],
                    "draft": draft,
                    "type": "draft_update"
                })
                
                return {
                    "success": True,
                    "message": response_text,
                    "data": response_data,
                    "intent": intent
                }
            else:
                return {
                    "success": False,
                    "message": (result.get("error") if isinstance(result, dict) else "Failed to create listing"),
                    "data": None,
                    "intent": intent
                }
        
        elif intent == "publish_or_delete":
            agent = PublishDeleteAgent()
            result = await agent.run(
                user_message=message_body,
                context={
                    "user_id": session.get("user_id"),
                    "draft_id": session.get("active_draft_id")
                }
            )
            # Guard against unexpected None/invalid result
            if not result or not isinstance(result, dict):
                return {
                    "success": False,
                    "message": "Internal error: publish/delete failed",
                    "data": None,
                    "intent": intent
                }

            response_data["type"] = "publish_delete"
            return {
                "success": result.get("success", False),
                "message": result.get("response", ""),
                "data": response_data,
                "intent": intent
            }
        
        elif intent == "search_listings":
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
                        extra_images = [
                            img.get("image_url") or img.get("public_url") or img
                            for img in listing["images"][1:]
                            if isinstance(img, (dict, str))
                        ]
                    detail_msg = f"![{title}]({image_url})\n" if image_url else ""
                    detail_msg += f"**{title}**\n{price_txt} | {location} | {category}\nSatıcı: {owner} | Telefon: {phone}\n\nAçıklama:\n{description}"
                    if extra_images:
                        links = "\n".join([f"[Foto {i+2}]({url})" for i, url in enumerate(extra_images) if url])
                        if links:
                            detail_msg += f"\n\nEk görseller:\n{links}"
                    return {
                        "success": True,
                        "message": detail_msg,
                        "data": {"listing": listing, "type": "search_results"},
                        "intent": intent
                    }
                else:
                    return {
                        "success": False,
                        "message": "Önce bir arama yapın ya da geçerli bir ilan numarası belirtin (örn: '1 nolu ilanın detayını göster').",
                        "data": None,
                        "intent": intent
                    }

            composer = SearchComposerAgent()
            result = await composer.orchestrate_search(message_body)

            if not result or not isinstance(result, dict):
                return {
                    "success": False,
                    "message": "Internal error: search failed",
                    "data": None,
                    "intent": intent
                }

            response_data.update({
                "listings": result.get("listings", []),
                "count": result.get("count", 0),
                "type": "search_results"
            })

            # Cache full results for follow-up detail requests
            if result.get("listings_full") is not None:
                LAST_SEARCH_CACHE[session_id] = result["listings_full"]

            return {
                "success": result.get("success", False),
                "message": result.get("message", "Search completed"),
                "data": response_data,
                "intent": intent
            }
        
        else:  # small_talk
            agent = SmallTalkAgent()
            response = await agent.run_simple(message_body)

            response_data["type"] = "conversation"
            return {
                "success": True,
                "message": response or "",
                "data": response_data,
                "intent": intent
            }
    
    except Exception as e:
        logger.error(f"WebChat message processing error: {e}")
        return {
            "success": False,
            "message": "An error occurred. Please try again.",
            "data": None,
            "intent": None
        }


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
        media_url=chat_message.media_url
    )
    
    # Store response in history
    await redis_client.add_message(chat_message.session_id, {
        "role": "assistant",
        "content": result["message"],
        "timestamp": str(uuid.uuid1().time)
    })
    
    return ChatResponse(**result)


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
    session = await redis_client.get_session(session_id)
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
    
    await redis_client.set_session(session_id, {
        "user_id": user_id or str(uuid.uuid4()),
        "intent": None,
        "active_draft_id": None
    })
    
    return {
        "session_id": session_id,
        "message": "Session created successfully"
    }


@router.delete("/session/{session_id}")
async def delete_session(session_id: str):
    """Delete a chat session"""
    success = await redis_client.delete_session(session_id)
    
    if success:
        return {"message": "Session deleted successfully"}
    raise HTTPException(status_code=404, detail="Session not found")
