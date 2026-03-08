"""
JWT Authentication Service for Supabase

Verifies Supabase JWT tokens and extracts user_id.
Used to secure WebChat API endpoints.
"""
import httpx
import jwt
from typing import Optional, Tuple
from functools import lru_cache
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

# Supabase JWT settings
SUPABASE_JWT_SECRET = settings.supabase_service_key  # Service key can verify JWTs
SUPABASE_URL = settings.supabase_url


@lru_cache(maxsize=1)
def get_jwt_secret() -> str:
    """
    Get Supabase JWT secret.
    Supabase uses the service key's secret portion for JWT signing.
    For Supabase, we can verify against auth.getUser() API instead.
    """
    return SUPABASE_JWT_SECRET


async def verify_supabase_token(token: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Verify Supabase JWT token by calling Supabase Auth API.
    
    This is the most reliable method as Supabase handles:
    - Token expiration
    - Token revocation
    - Refresh token rotation
    
    Args:
        token: Bearer token from Authorization header
        
    Returns:
        (is_valid, user_id, error_message)
    """
    if not token:
        return False, None, "Token gerekli"
    
    # Remove "Bearer " prefix if present
    if token.startswith("Bearer "):
        token = token[7:]
    
    try:
        # Call Supabase Auth API to verify token
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": settings.supabase_key,
                }
            )
            
            if response.status_code == 200:
                user_data = response.json()
                user_id = user_data.get("id")
                
                if user_id:
                    logger.info(f"✅ JWT verified - user_id: {user_id}")
                    return True, user_id, None
                else:
                    return False, None, "Token geçersiz - user_id bulunamadı"
            
            elif response.status_code == 401:
                logger.warning("❌ JWT verification failed - token expired or invalid")
                return False, None, "Token süresi dolmuş veya geçersiz"
            
            else:
                logger.error(f"❌ JWT verification failed - status: {response.status_code}")
                return False, None, f"Token doğrulama hatası: {response.status_code}"
                
    except httpx.TimeoutException:
        logger.error("❌ JWT verification timeout")
        return False, None, "Token doğrulama zaman aşımı"
    except Exception as e:
        logger.error(f"❌ JWT verification error: {e}")
        return False, None, f"Token doğrulama hatası: {str(e)}"


async def get_user_id_from_request(
    authorization: Optional[str],
    request_user_id: Optional[str],
    channel: str
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Get verified user_id based on channel type.
    
    Security model:
    - webchat: REQUIRES valid JWT token → user_id from token
    - whatsapp: user_id comes from Edge Function (already verified via PIN)
    
    Args:
        authorization: Authorization header value
        request_user_id: user_id from request body
        channel: "webchat" or "whatsapp"
        
    Returns:
        (is_valid, user_id, error_message)
    """
    if channel == "webchat":
        # WebChat: JWT doğrulama ZORUNLU
        if not authorization:
            return False, None, "Authorization header gerekli"
        
        is_valid, user_id, error = await verify_supabase_token(authorization)
        
        if not is_valid:
            return False, None, error
        
        # Extra security: if request also has user_id and differs, trust JWT subject.
        # This avoids false "security violation" responses when stale client state sends
        # an old/local/custom id while Authorization token belongs to the real Supabase user.
        if request_user_id and request_user_id != user_id:
            logger.warning(f"⚠️ user_id mismatch! Token: {user_id}, Request: {request_user_id}")
            return True, user_id, None
        
        return True, user_id, None
    
    elif channel == "whatsapp":
        # WhatsApp: Edge Function tarafından doğrulanmış user_id
        # Edge Function PIN doğrulaması yapıp user_id inject ediyor
        if not request_user_id:
            return False, None, "WhatsApp: user_id gerekli (Edge Function tarafından sağlanmalı)"
        
        # WhatsApp user_id'leri UUID formatında olmalı
        # (Edge Function profiles tablosundan alıyor)
        import uuid
        try:
            uuid.UUID(request_user_id)
            return True, request_user_id, None
        except ValueError:
            logger.warning(f"⚠️ WhatsApp invalid user_id format: {request_user_id}")
            return False, None, "Geçersiz user_id formatı"
    
    else:
        # Unknown channel - reject
        return False, None, f"Bilinmeyen kanal: {channel}"
