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
    
    async def check_safety(self, image_url: str) -> Dict[str, Any]:
        """
        Check image for content policy violations.
        
        Uses OpenAI Moderation API. Fail-open on error.
        
        Args:
            image_url: URL or base64 of image
        
        Returns:
            Dict with 'safe' boolean and 'flagged_categories' list
        """
        try:
            client = await self._get_client()
            
            # Use moderation API with image
            response = await client.moderations.create(
                model="omni-moderation-latest",
                input=[
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            )
            
            result = response.results[0] if response.results else None
            
            if not result:
                logger.warning("Empty moderation response, allowing content")
                return {"safe": True, "flagged_categories": []}
            
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
            
            is_safe = len(flagged) == 0
            
            logger.info(f"Vision safety: safe={is_safe}, flagged={flagged}")
            
            return {
                "safe": is_safe,
                "flagged_categories": flagged,
            }
        
        except Exception as e:
            # Fail-open: allow content if moderation fails
            logger.error(f"Moderation API error (fail-open): {e}")
            return {
                "safe": True,
                "flagged_categories": [],
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
