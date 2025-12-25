"""
WebChat API endpoints for frontend integration
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any
from loguru import logger
from services import redis_client
from agents import IntentRouterAgent, ComposerAgent, PublishDeleteAgent, SearchComposerAgent, SmallTalkAgent
import json
import uuid

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
    media_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Process webchat message and route to appropriate agent
    
    Args:
        message_body: Message text
        session_id: Session identifier
        user_id: User ID (optional)
        media_url: Optional media URL
    
    Returns:
        Response dict
    """
    try:
        # Get or create session
        session = await redis_client.get_session(session_id)
        if not session:
            # Create new session
            await redis_client.set_session(session_id, {
                "user_id": user_id or str(uuid.uuid4()),
                "intent": None,
                "active_draft_id": None
            })
            session = await redis_client.get_session(session_id)
        
        # Store message in history
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
            await redis_client.set_intent(session_id, intent)
            logger.info(f"WebChat intent for {session_id}: {intent}")
        
        response_data = {"intent": intent}
        
        # Route to appropriate agent
        if intent == "create_listing":
            composer = ComposerAgent()
            result = await composer.orchestrate_listing_creation(
                user_message=message_body,
                user_id=session["user_id"],
                phone_number=session_id,  # Use session_id as identifier
                draft_id=session.get("active_draft_id"),
                media_url=media_url
            )
            
            if result["success"]:
                await redis_client.set_active_draft(session_id, result["draft_id"])
                
                draft = result["draft"]
                response_text = "✅ Draft updated successfully!\n\n"
                
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
                    "message": result.get("error", "Failed to create listing"),
                    "data": None,
                    "intent": intent
                }
        
        elif intent == "publish_or_delete":
            agent = PublishDeleteAgent()
            result = await agent.run(
                user_message=message_body,
                context={
                    "user_id": session["user_id"],
                    "draft_id": session.get("active_draft_id")
                }
            )
            
            response_data["type"] = "publish_delete"
            return {
                "success": result["success"],
                "message": result["response"],
                "data": response_data,
                "intent": intent
            }
        
        elif intent == "search_listings":
            composer = SearchComposerAgent()
            result = await composer.orchestrate_search(message_body)
            
            response_data.update({
                "listings": result["listings"],
                "count": result["count"],
                "type": "search_results"
            })
            
            return {
                "success": result["success"],
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
                "message": response,
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
