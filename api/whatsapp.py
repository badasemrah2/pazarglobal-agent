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
import re

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def is_publish_command(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return any(token in msg for token in ["yayınla", "yayina", "publish", "yayınlamak"])


def is_delete_command(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return any(token in msg for token in ["sil", "ilanı sil", "ilani sil", "kaldır", "kaldir", "delete"])


def is_create_listing_command(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
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

    # Availability-style queries: "bilgisayar var mı?" should be treated as a search intent.
    if bool(re.search(r"\bvar\s*m[ıi]\b", msg)) or any(token in msg for token in ["varmı", "varmi", "var mı", "var mi"]):
        return True
    if any(phrase in msg for phrase in [
        "arıyorum",
        "ariyorum",
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
        "search",
        "find",
    ]):
        return True
    return bool(re.search(r"\b(ara|bul|listele|goster|göster)\b", msg))


async def get_or_create_session(phone_number: str) -> str:
    """Get or create session for phone number, resolving to real Supabase user_id"""
    from services.supabase_client import supabase_client
    
    # Use phone number as session identifier
    session_id = f"whatsapp_{phone_number.replace('+', '').replace(':', '')}"
    
    # Check if session exists
    session = await redis_client.get_session(session_id)
    if not session:
        # CRITICAL: Map phone_number to real Supabase user_id via user_security table
        try:
            # Query user_security table (has phone→user_id mapping + PIN)
            response = supabase_client.client.table("user_security").select("user_id").eq("phone", phone_number).execute()
            
            data = getattr(response, "data", None)
            if isinstance(data, list) and data:
                first = data[0] if isinstance(data[0], dict) else None
                real_user_id = first.get("user_id") if isinstance(first, dict) else None
            else:
                real_user_id = None

            if real_user_id:
                logger.info(f"✅ WhatsApp phone {phone_number} mapped to user_id: {real_user_id}")
            else:
                # User not registered or hasn't set up WhatsApp PIN
                logger.warning(f"⚠️ WhatsApp phone {phone_number} NOT FOUND in user_security table!")
                logger.error(f"❌ User must register via web and set WhatsApp PIN in profile settings")
                # Return error message instead of creating phantom user
                raise ValueError(
                    "🔒 WhatsApp kullanımı için önce web sitesinden kayıt olup "
                    "profil ayarlarından WhatsApp PIN tanımlamanız gerekiyor."
                )
        except ValueError as ve:
            # Re-raise user-facing errors
            raise ve
        except Exception as e:
            logger.error(f"❌ Failed to query user_security table: {e}")
            raise Exception(
                "Sistem hatası oluştu. Lütfen daha sonra tekrar deneyin."
            )
        
        # Create new session with REAL user_id from user_security table
        await redis_client.set_session(session_id, {
            "phone_number": phone_number,
            "user_id": real_user_id,  # ✅ Real user_id from user_security table
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
        if not isinstance(session, dict):
            session = {}

        # Deterministic intent override each message (prevents sticky small_talk from blocking tasks)
        current_intent = session.get("intent")
        override_intent = None
        if is_publish_command(message_body) or is_delete_command(message_body):
            override_intent = "publish_or_delete"
        elif media_url is not None or is_create_listing_command(message_body):
            override_intent = "create_listing"
        elif is_search_command(message_body):
            override_intent = "search_listings"

        if override_intent and override_intent != current_intent:
            intent = override_intent
            await redis_client.set_intent(session_id, intent)
            session["intent"] = intent
        else:
            intent = current_intent
        
        # Get or determine intent
        if not intent:
            # First message - classify intent
            router_agent = IntentRouterAgent()
            router_result = await router_agent.classify_intent(message_body)
            intent = (router_result or {}).get("intent") if isinstance(router_result, dict) else None
            if intent:
                await redis_client.set_intent(session_id, intent)
                logger.info(f"New intent for {from_number}: {intent}")
        
        # Route to appropriate agent based on intent
        if intent == "create_listing":
            composer = ComposerAgent()
            user_id = session.get("user_id")
            if not user_id:
                return "❌ Kullanıcı doğrulanamadı. Lütfen web üzerinden WhatsApp PIN oluşturun."
            result = await composer.orchestrate_listing_creation(
                user_message=message_body,
                user_id=user_id,
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
            user_id = session.get("user_id")
            if not user_id:
                return "❌ Kullanıcı doğrulanamadı. Lütfen web üzerinden WhatsApp PIN oluşturun."
            result = await agent.run(
                user_message=message_body,
                context={
                    "user_id": user_id,
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
                # Emoji number mapping for better visibility
                emoji_numbers = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟"}
                
                response = f"🔍 {result['count']} ilan bulundu:\n\n"
                for i, listing in enumerate(result["listings"][:5], 1):
                    num_emoji = emoji_numbers.get(i, f"{i}.")
                    response += f"{num_emoji} {listing.get('title', 'Başlıksız')}\n"
                    response += f"   💰 {listing.get('price', 'N/A')} TL\n"
                    response += f"   📍 {listing.get('category', 'Kategori belirtilmemiş')}\n\n"
                
                if result["count"] > 5:
                    response += f"...ve {result['count'] - 5} ilan daha.\n"
                response += "Detay için: '1 nolu ilanın detayını göster' yazabilirsiniz."
                
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
