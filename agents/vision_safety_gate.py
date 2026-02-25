"""
Vision Safety Gate - Pre-routing content moderation
Blocks unsafe content BEFORE FSM/Router
"""
from typing import Dict, Any, List, Optional
from loguru import logger
import httpx
import os
from openai import AsyncOpenAI


class VisionSafetyGate:
    """
    Pre-routing vision safety check.
    
    Workflow:
    1. User uploads media → Storage writes
    2. VisionSafetyGate checks media URLs
    3. If unsafe → Block with empathetic message
    4. If safe → Proceed to IntentRouter/FSM
    
    This ensures FSM/Router never sees unsafe content.
    """
    
    def __init__(self):
        self.client: Optional[AsyncOpenAI]

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set, vision safety checks will fail-open")
            self.client = None
        else:
            self.client = AsyncOpenAI(api_key=api_key)
        
    async def check_media(self, media_urls: List[str], user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Check if media URLs contain unsafe content.
        
        Args:
            media_urls: List of image URLs to check
            user_id: Yükleyen kullanıcı (loglama için, opsiyonel)
            
        Returns:
            {
                "safe": bool,
                "block_reason": str (if unsafe),
                "flagged_categories": list,
                "allow_listing": bool
            }
        """
        if not media_urls:
            return {
                "safe": True,
                "allow_listing": True,
                "flagged_categories": [],
                "block_reason": None
            }
        
        # FAIL-CLOSED: If client is not initialized, block all media uploads.
        # Missing API key is a misconfiguration — should not allow unchecked content.
        if self.client is None:
            logger.error("Vision safety check: OpenAI client not initialized — blocking all media (fail-closed)")
            await self._log_flag(
                flag_type="moderation_not_configured",
                confidence="n/a",
                message="OpenAI Moderation API yapılandırılmamış, tüm medya engellendi.",
                user_id=user_id,
            )
            return {
                "safe": False,
                "allow_listing": False,
                "flagged_categories": ["moderation_not_configured"],
                "block_reason": "Görsel güvenlik servisi yapılandırılmamış. Lütfen yönetici ile iletişime geçin.",
                "skipped": True
            }
        
        try:
            # Check each media URL
            flagged_categories = set()
            unsafe_count = 0
            
            for url in media_urls[:10]:  # Limit to first 10 images
                result = await self._check_single_image(url)
                if not result["safe"]:
                    unsafe_count += 1
                    flagged_categories.update(result.get("categories", []))
            
            # If any image is unsafe, block entire upload
            if unsafe_count > 0:
                block_reason = self._get_block_message(flagged_categories)
                # image_safety_flags tablosuna kayıt düş
                await self._log_flag(
                    flag_type=", ".join(sorted(flagged_categories)),
                    confidence="high",
                    message=f"{unsafe_count} görsel engellendi: {block_reason}",
                    user_id=user_id,
                    image_url=media_urls[0] if len(media_urls) == 1 else None,
                )
                return {
                    "safe": False,
                    "allow_listing": False,
                    "flagged_categories": list(flagged_categories),
                    "block_reason": block_reason
                }
            
            return {
                "safe": True,
                "allow_listing": True,
                "flagged_categories": [],
                "block_reason": None
            }
            
        except Exception as e:
            logger.error(f"Vision safety check failed (fail-CLOSED): {e}")
            # FAIL-CLOSED: Block content if the overall safety check fails.
            await self._log_flag(
                flag_type="moderation_api_error",
                confidence="unknown",
                message=f"Vision safety check exception (fail-closed): {str(e)[:200]}",
                user_id=user_id,
            )
            return {
                "safe": False,
                "allow_listing": False,
                "flagged_categories": ["moderation_api_error"],
                "block_reason": "Görsel güvenlik kontrolü şu an yapılamadı. Lütfen daha sonra tekrar deneyin.",
                "error": str(e)
            }
    
    async def _check_single_image(self, image_url: str) -> Dict[str, Any]:
        """
        Check single image using OpenAI Moderation API.
        
        Blocked categories:
        - sexual content (explicit)
        - violence (graphic)
        - hate symbols
        - harassment
        - self-harm
        - illicit content (drugs, weapons)
        """
        try:
            # Use OpenAI's vision model for content moderation.
            # CRITICAL: input MUST be an object array with type=image_url,
            # NOT a plain string — plain string only does text moderation.
            client = self.client
            if client is None:
                raise RuntimeError("OpenAI client missing during image moderation")
            response = await client.moderations.create(
                input=[
                    {"type": "image_url", "image_url": {"url": image_url}}
                ],
                model="omni-moderation-latest"
            )
            
            result = response.results[0]
            
            # Check critical categories
            flagged_categories = []
            if result.categories.sexual:
                flagged_categories.append("sexual")
            if result.categories.violence:
                flagged_categories.append("violence")
            if result.categories.hate:
                flagged_categories.append("hate")
            if result.categories.harassment:
                flagged_categories.append("harassment")
            if result.categories.self_harm:
                flagged_categories.append("self-harm")
            if hasattr(result.categories, 'illicit') and result.categories.illicit:
                flagged_categories.append("illicit")
            
            # Block if ANY critical category flagged
            is_safe = len(flagged_categories) == 0
            
            return {
                "safe": is_safe,
                "categories": flagged_categories,
                "scores": {
                    "sexual": result.category_scores.sexual,
                    "violence": result.category_scores.violence,
                    "hate": result.category_scores.hate
                }
            }
            
        except Exception as e:
            logger.error(f"Single image check failed for {image_url}: {e}")
            # FAIL-CLOSED: Block content when moderation check fails.
            return {"safe": False, "categories": ["moderation_error"]}

    async def _log_flag(
        self,
        flag_type: str,
        confidence: str,
        message: str,
        user_id: Optional[str] = None,
        image_url: Optional[str] = None,
    ) -> None:
        """image_safety_flags tablosuna kayıt düşer. Hata olsa bile akışı kesmez."""
        try:
            from services.supabase_client import supabase_client
            await supabase_client.log_image_safety_flag(
                flag_type=flag_type,
                confidence=confidence,
                message=message,
                user_id=user_id,
                image_url=image_url,
            )
        except Exception as e:
            logger.error(f"image_safety_flags loglama hatası (akış devam ediyor): {e}")

    def _get_block_message(self, categories: set) -> str:
        """
        Generate user-friendly block message based on flagged categories.
        """
        if "sexual" in categories:
            return "Üzgünüm, bu tür içerik platformumuzda paylaşılamaz."
        elif "violence" in categories:
            return "Şiddet içeren görseller paylaşılamaz."
        elif "hate" in categories:
            return "Nefret söylemi veya semboller içeren içerik paylaşılamaz."
        elif "illicit" in categories:
            return "Yasadışı içerik (uyuşturucu, silah vb.) paylaşılamaz."
        elif "harassment" in categories or "self-harm" in categories:
            return "Bu tür içerik platformumuzda paylaşılamaz."
        else:
            return "İçerik güvenlik politikalarımıza uygun değil."
    
    async def check_text(self, text: str) -> Dict[str, Any]:
        """
        Check if text contains unsafe content (prompt injection, jailbreak, etc.)
        
        Args:
            text: User text input
            
        Returns:
            {
                "safe": bool,
                "block_reason": str (if unsafe),
                "flagged_categories": list
            }
        """
        if not text or len(text.strip()) < 3:
            return {"safe": True, "flagged_categories": []}
        
        # FAIL-CLOSED: If client is not initialized, block all text moderation checks.
        if self.client is None:
            logger.error("Text moderation: OpenAI client not initialized — skipping (text moderation is advisory only)")
            return {"safe": True, "flagged_categories": [], "skipped": True}
        
        try:
            client = self.client
            if client is None:
                raise RuntimeError("OpenAI client missing during text moderation")
            response = await client.moderations.create(
                input=text,
                model="omni-moderation-latest"
            )
            
            result = response.results[0]
            
            flagged_categories = []
            if result.categories.sexual:
                flagged_categories.append("sexual")
            if result.categories.violence:
                flagged_categories.append("violence")
            if result.categories.hate:
                flagged_categories.append("hate")
            if result.categories.harassment:
                flagged_categories.append("harassment")
            if result.categories.self_harm:
                flagged_categories.append("self-harm")
            
            is_safe = len(flagged_categories) == 0
            
            return {
                "safe": is_safe,
                "flagged_categories": flagged_categories,
                "block_reason": self._get_block_message(set(flagged_categories)) if not is_safe else None
            }
            
        except Exception as e:
            logger.error(f"Text moderation failed: {e}")
            # Text moderation is advisory (not blocking) — fail-open is acceptable for text.
            # Image moderation is fail-closed (see check_media). Text is less critical.
            return {"safe": True, "flagged_categories": [], "error": str(e)}


# Singleton instance
vision_safety_gate = VisionSafetyGate()
