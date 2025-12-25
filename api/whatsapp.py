"""
WhatsApp webhook handlers using Twilio
"""
from fastapi import APIRouter, Request, Form, HTTPException
from typing import Optional
from loguru import logger
from twilio.twiml.messaging_response import MessagingResponse
from services import redis_client
from agents import IntentRouterAgent, ComposerAgent, PublishDeleteAgent, SearchComposerAgent, SmallTalkAgent
import uuid

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


async def get_or_create_session(phone_number: str) -> str:
    """Get or create session for phone number"""
    # Use phone number as session identifier
    session_id = f"whatsapp_{phone_number.replace('+', '').replace(':', '')}"
    
    # Check if session exists
    session = await redis_client.get_session(session_id)
    if not session:
        # Create new session
        await redis_client.set_session(session_id, {
            "phone_number": phone_number,
            "user_id": str(uuid.uuid4()),  # Generate temp user_id
            "intent": None,
            "active_draft_id": None
        })
    
    return session_id


async def process_whatsapp_message(
    message_body: str,
    from_number: str,
    media_url: Optional[str] = None
) -> str:
    """
    Process WhatsApp message and route to appropriate agent
    
    Args:
        message_body: Message text
        from_number: Sender's phone number
        media_url: Optional media URL (for images)
    
    Returns:
        Response text
    """
    try:
        # Get or create session
        session_id = await get_or_create_session(from_number)
        session = await redis_client.get_session(session_id)
        
        # Get or determine intent
        intent = session.get("intent")
        if not intent:
            # First message - classify intent
            router_agent = IntentRouterAgent()
            intent = await router_agent.classify_intent(message_body)
            await redis_client.set_intent(session_id, intent)
            logger.info(f"New intent for {from_number}: {intent}")
        
        # Route to appropriate agent based on intent
        if intent == "create_listing":
            composer = ComposerAgent()
            result = await composer.orchestrate_listing_creation(
                user_message=message_body,
                user_id=session["user_id"],
                phone_number=from_number,
                draft_id=session.get("active_draft_id"),
                media_url=media_url
            )
            
            if result["success"]:
                # Update active draft
                await redis_client.set_active_draft(session_id, result["draft_id"])
                
                draft = result["draft"]
                response = "✅ İlan taslağınız güncellendi!\n\n"
                if draft.get("title"):
                    response += f"📝 Başlık: {draft['title']}\n"
                if draft.get("description"):
                    response += f"📄 Açıklama: {draft['description'][:100]}...\n"
                if draft.get("price_normalized"):
                    response += f"💰 Fiyat: {draft['price_normalized']} TL\n"
                response += "\nDeğişiklik yapmak ister misiniz? Yoksa yayınlamak için 'yayınla' yazın."
                return response
            else:
                return f"❌ Hata: {result.get('error', 'İlan oluşturulamadı')}"
        
        elif intent == "publish_or_delete":
            agent = PublishDeleteAgent()
            result = await agent.run(
                user_message=message_body,
                context={
                    "user_id": session["user_id"],
                    "draft_id": session.get("active_draft_id")
                }
            )
            
            if result["success"]:
                return result["response"]
            else:
                return "❌ İşlem tamamlanamadı. Lütfen tekrar deneyin."
        
        elif intent == "search_listings":
            composer = SearchComposerAgent()
            result = await composer.orchestrate_search(message_body)
            
            if result["success"] and result["listings"]:
                response = f"🔍 {result['count']} ilan bulundu:\n\n"
                for i, listing in enumerate(result["listings"][:5], 1):
                    response += f"{i}. {listing.get('title', 'Başlıksız')}\n"
                    response += f"   💰 {listing.get('price', 'N/A')} TL\n"
                    response += f"   📍 {listing.get('category', 'Kategori belirtilmemiş')}\n\n"
                
                if result["count"] > 5:
                    response += f"...ve {result['count'] - 5} ilan daha.\n"
                
                return response
            else:
                return "🔍 Aramanıza uygun ilan bulunamadı. Farklı kriterlerle tekrar deneyin."
        
        else:  # small_talk
            agent = SmallTalkAgent()
            response = await agent.run_simple(message_body)
            return response
    
    except Exception as e:
        logger.error(f"WhatsApp message processing error: {e}")
        return "❌ Bir hata oluştu. Lütfen daha sonra tekrar deneyin."


@router.post("/webhook")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
    NumMedia: int = Form(0),
    MediaUrl0: Optional[str] = Form(None)
):
    """
    Twilio WhatsApp webhook endpoint
    
    Receives messages from WhatsApp via Twilio
    """
    try:
        logger.info(f"WhatsApp message from {From}: {Body}")
        
        # Process message
        response_text = await process_whatsapp_message(
            message_body=Body,
            from_number=From,
            media_url=MediaUrl0 if NumMedia > 0 else None
        )
        
        # Create Twilio response
        resp = MessagingResponse()
        resp.message(response_text)
        
        return str(resp)
    
    except Exception as e:
        logger.error(f"WhatsApp webhook error: {e}")
        resp = MessagingResponse()
        resp.message("Bir hata oluştu. Lütfen daha sonra tekrar deneyin.")
        return str(resp)


@router.get("/webhook")
async def whatsapp_webhook_verify():
    """Webhook verification endpoint"""
    return {"status": "ok", "message": "WhatsApp webhook is active"}
