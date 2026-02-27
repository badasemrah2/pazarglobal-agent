"""
Vision Service - Image analysis and content moderation

Components:
1. Safety Gate - OpenAI Moderation API for content policy
2. Product Analyzer - GPT-4 Vision for product recognition
"""
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from services.logger import get_logger

logger = get_logger(__name__)


PROHIBITED_PRODUCT_TERMS = {
    "silah", "tabanca", "tufek", "tüfek", "pistol", "gun", "firearm", "revolver", "shotgun",
    "mermi", "cephane", "bomba", "patlayici", "patlayıcı", "explosive", "uyusturucu", "uyuşturucu",
    "kokain", "eroin", "esrar", "meth", "amfetamin", "cocaine", "heroin",
}


@dataclass
class SafetyResult:
    """Safety check result"""
    safe: bool
    flagged_categories: List[str]
    error: Optional[str] = None


@dataclass
class ProductAnalysis:
    """Product analysis result"""
    product: Optional[str] = None
    category: Optional[str] = None
    condition: Optional[str] = None
    brand: Optional[str] = None
    color: Optional[str] = None
    suggested_price: Optional[float] = None
    confidence: float = 0.0
    raw_response: Optional[str] = None


async def _log_safety_flag_to_db(
    flag_type: str,
    confidence: str,
    message: str,
    user_id: Optional[str] = None,
    image_url: Optional[str] = None,
) -> None:
    """image_safety_flags tablosuna engelleme kaydı düşer. Hata olsa bile akışı kesmez."""
    try:
        from services.supabase_client import supabase_client
        await supabase_client.log_image_safety_flag(
            flag_type=flag_type,
            confidence=confidence,
            message=message,
            user_id=user_id,
            image_url=image_url,
            status="pending",
        )
    except Exception as e:
        logger.error(f"image_safety_flags loglama hatası (akış devam ediyor): {e}")


class VisionService:
    """
    Image analysis service.
    
    Uses OpenAI Vision API for:
    - Content safety moderation
    - Product identification
    - Category suggestion
    - Market price estimation
    """
    
    # Blocked content categories
    BLOCKED_CATEGORIES = [
        "sexual",
        "sexual/minors",
        "hate",
        "hate/threatening",
        "violence",
        "violence/graphic",
        "self-harm",
        "self-harm/intent",
        "self-harm/instructions",
        "harassment",
        "harassment/threatening",
    ]
    
    def __init__(self):
        self.openai_client = None
    
    async def _get_client(self):
        """Lazy load OpenAI client"""
        if not self.openai_client:
            from services.openai_client import get_openai_client
            self.openai_client = await get_openai_client()
        return self.openai_client

    async def _is_allowed_apparel_exception(self, image_url: str) -> bool:
        """Allow underwear/swimwear product photos when moderation only flags soft sexual content."""
        try:
            client = await self._get_client()
            prompt = (
                "Bu görsel e-ticaret ürün istisnasına giriyor mu? "
                "Sadece iç çamaşırı/mayo/bikini gibi legal giyim ürünü satışı için uygun ürün fotoğrafıysa true döndür. "
                "Reşit olmayan kişi, pornografik çıplaklık, şiddet, silah/bomba veya yasa dışı içerik varsa false döndür. "
                "Emin değilsen false döndür. "
                "Sadece JSON döndür: {\"allow_exception\": true|false, \"reason\": \"...\"}"
            )
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
                        ],
                    }
                ],
                temperature=0,
                max_tokens=120,
            )
            content = (response.choices[0].message.content or "{}").strip()
            payload = json.loads(content)
            return bool(payload.get("allow_exception") is True)
        except Exception as e:
            logger.warning(f"Apparel exception check failed: {e}")
            return False
    
    async def check_safety(self, image_url: str) -> Dict[str, Any]:
        """
        Check image for content policy violations.
        
        Uses OpenAI Moderation API.
        FAIL-CLOSED: On any error, content is BLOCKED (not allowed through).
        This prevents illegal content slipping past on API failures.
        
        Args:
            image_url: URL or base64 of image
        
        Returns:
            Dict with 'safe' boolean and 'flagged_categories' list
        """
        try:
            client = await self._get_client()
            
            # Use moderation API with image — MUST use object array format,
            # NOT plain string, otherwise OpenAI cannot actually moderate the image.
            response = await client.moderations.create(
                model="omni-moderation-latest",
                input=[
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            )
            
            result = response.results[0] if response.results else None
            
            if not result:
                # Empty moderation response — treat as UNSAFE (fail-closed)
                logger.error("Empty moderation response — blocking content (fail-closed)")
                return {
                    "safe": False,
                    "flagged_categories": ["moderation_empty_response"],
                    "error": "empty_response",
                }
            
            # Check categories
            flagged = []
            categories = result.categories
            
            for category in self.BLOCKED_CATEGORIES:
                # Handle nested categories like "sexual/minors"
                category_parts = category.split("/")
                if len(category_parts) == 2:
                    parent, child = category_parts
                    attr_name = f"{parent}_{child}".replace("-", "_")
                else:
                    attr_name = category.replace("-", "_").replace("/", "_")
                
                if getattr(categories, attr_name, False):
                    flagged.append(category)

            if set(flagged) == {"sexual"}:
                if await self._is_allowed_apparel_exception(image_url):
                    logger.info("Vision safety exception applied: underwear/swimwear product image allowed")
                    return {
                        "safe": True,
                        "flagged_categories": [],
                    }
            
            is_safe = len(flagged) == 0
            
            logger.info(f"Vision safety: safe={is_safe}, flagged={flagged}")

            if not is_safe:
                # image_safety_flags tablosuna kayıt düş
                await _log_safety_flag_to_db(
                    flag_type=", ".join(flagged),
                    confidence="high",
                    message=f"Moderation API engelledi: {', '.join(flagged)}",
                    image_url=image_url,
                )
            
            return {
                "safe": is_safe,
                "flagged_categories": flagged,
            }
        
        except Exception as e:
            # FAIL-CLOSED: Block content if moderation check fails.
            # This is critical — fail-open would allow weapons/illegal content through.
            logger.error(f"Moderation API error (fail-CLOSED, blocking content): {e}")
            await _log_safety_flag_to_db(
                flag_type="moderation_api_error",
                confidence="unknown",
                message=f"Moderation API hatası (fail-closed): {str(e)[:200]}",
                image_url=image_url,
            )
            return {
                "safe": False,
                "flagged_categories": ["moderation_api_error"],
                "error": str(e),
            }
    
    async def analyze_product(self, image_url: str) -> Dict[str, Any]:
        """
        Analyze image for product information.
        
        Uses GPT-4 Vision to identify:
        - Product type/name
        - Category
        - Condition (if visible)
        - Brand (if visible)
        - Color
        
        Args:
            image_url: URL or base64 of image
        
        Returns:
            Dict with product information
        """
        try:
            client = await self._get_client()
            
            prompt = """Bu görseldeki ürünü analiz et ve JSON formatında bilgi ver:

{
    "product": "Ürün adı (örn: iPhone 13 Pro)",
    "category": "Kategori (Elektronik, Otomotiv, Emlak, Mobilya & Dekorasyon, Giyim & Aksesuar, Diğer)",
    "condition": "Durum tahmini (Sıfır, 2. El, Belirsiz)",
    "brand": "Marka (varsa)",
    "color": "Renk (varsa)",
    "confidence": "Güven skoru 0-1 arası"
}

Sadece JSON döndür, açıklama ekleme."""

            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url, "detail": "low"}
                            }
                        ]
                    }
                ],
                max_tokens=300,
            )
            
            content = response.choices[0].message.content
            
            # Clean markdown code blocks if present
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            
            data = json.loads(content.strip())
            
            logger.info(f"Vision analysis: {data}")
            
            return {
                "product": data.get("product"),
                "category": data.get("category"),
                "condition": data.get("condition"),
                "brand": data.get("brand"),
                "color": data.get("color"),
                "confidence": float(data.get("confidence", 0.5)),
            }
        
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse vision response: {e}")
            return {"error": "parse_error", "raw": content if 'content' in dir() else None}
        
        except Exception as e:
            logger.error(f"Vision analysis error: {e}")
            return {"error": str(e)}

    def detect_prohibited_product(self, analysis: Dict[str, Any]) -> Optional[str]:
        from services.text_normalization import normalize_for_match

        if not isinstance(analysis, dict):
            return None

        haystack = " ".join(
            str(analysis.get(key) or "")
            for key in ("product", "category", "brand", "raw_response")
        )
        normalized = normalize_for_match(haystack)
        if not normalized:
            return None

        for term in PROHIBITED_PRODUCT_TERMS:
            if term in normalized:
                return term
        return None
    
    async def get_price_suggestion(
        self,
        product_info: Dict[str, Any],
        image_url: Optional[str] = None,
    ) -> Optional[float]:
        """
        Get price suggestion for product.
        
        Delegates to PriceService for market research.
        
        Args:
            product_info: Product analysis result
            image_url: Optional image URL for additional context
        
        Returns:
            Suggested price in TL or None
        """
        from services.price_service import price_service
        
        product_name = product_info.get("product")
        if not product_name:
            return None
        
        try:
            result = await price_service.get_market_price(
                product_name=product_name,
                category=product_info.get("category"),
                condition=product_info.get("condition"),
            )
            
            return result.get("suggested_price")
        
        except Exception as e:
            logger.error(f"Price suggestion error: {e}")
            return None


# Singleton
vision_service = VisionService()
