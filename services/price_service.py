"""
Price Service - Market price research via Edge Functions

Flow:
1. Check cache (Supabase perplexity_cache)
2. If miss → Call Edge Function (ai-assistant-cached)
3. Edge Function calls Perplexity API
4. Cache result and return
"""
from typing import Dict, Any, Optional
from dataclasses import dataclass
import httpx

from services.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PriceResult:
    """Market price research result"""
    suggested_price: Optional[float] = None
    price_range: Optional[tuple] = None  # (min, max)
    market_info: Optional[str] = None
    source: str = "unknown"  # cache, edge_function, fallback
    confidence: float = 0.0


class PriceService:
    """
    Market price research service.
    
    Uses Supabase Edge Functions that call Perplexity API
    for real-time market research.
    
    Edge Functions:
    - ai-assistant-cached: With caching (preferred)
    - ai-assistant: Without caching (fallback)
    """
    
    # Edge function URLs
    EDGE_FUNCTION_BASE = "https://snovwbffwvmkgjulrtsm.supabase.co/functions/v1"
    CACHED_ENDPOINT = f"{EDGE_FUNCTION_BASE}/ai-assistant-cached"
    DIRECT_ENDPOINT = f"{EDGE_FUNCTION_BASE}/ai-assistant"
    
    def __init__(self):
        self.supabase = None
        self.http_client = None
    
    async def _get_supabase(self):
        """Lazy load Supabase client"""
        if not self.supabase:
            from services.supabase_client import supabase_client
            self.supabase = supabase_client
        return self.supabase
    
    async def _get_http_client(self):
        """Lazy load HTTP client"""
        if not self.http_client:
            self.http_client = httpx.AsyncClient(timeout=30.0)
        return self.http_client
    
    async def get_market_price(
        self,
        product_name: str,
        category: Optional[str] = None,
        condition: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get market price for a product.
        
        Args:
            product_name: Product name/description
            category: Optional category hint
            condition: Optional condition hint
        
        Returns:
            Dict with price info
        """
        logger.info(f"Price research: {product_name}")
        
        # 1. Try cache first
        cached = await self._check_cache(product_name)
        if cached:
            logger.info(f"Price cache hit: {cached}")
            return {
                "suggested_price": cached.get("suggested_price"),
                "price_range": cached.get("price_range"),
                "market_info": cached.get("market_info"),
                "source": "cache",
                "confidence": 0.9,
            }
        
        # 2. Call edge function
        try:
            result = await self._call_edge_function(
                product_name=product_name,
                category=category,
                condition=condition,
            )
            
            if result:
                logger.info(f"Price from edge function: {result}")
                return {
                    "suggested_price": result.get("suggested_price"),
                    "price_range": result.get("price_range"),
                    "market_info": result.get("market_info"),
                    "source": "edge_function",
                    "confidence": 0.8,
                }
        
        except Exception as e:
            logger.error(f"Edge function error: {e}")
        
        # 3. Fallback - no price
        return {
            "suggested_price": None,
            "price_range": None,
            "market_info": None,
            "source": "fallback",
            "confidence": 0.0,
        }
    
    async def _check_cache(self, product_name: str) -> Optional[Dict[str, Any]]:
        """
        Check local cache.
        
        Note: Edge Function (ai-assistant-cached) has its own cache in 
        market_price_snapshots table. We skip local cache and let Edge 
        Function handle caching.
        """
        # Edge Function handles caching internally via market_price_snapshots
        # No need for duplicate cache check here
        return None
    
    async def _call_edge_function(
        self,
        product_name: str,
        category: Optional[str] = None,
        condition: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call Supabase Edge Function for price research"""
        try:
            client = await self._get_http_client()
            
            # Get service key for auth
            from config.settings import settings
            
            # Edge Function expects this payload format:
            # { action: "suggest_price", title, category, condition, description? }
            payload = {
                "action": "suggest_price",
                "title": product_name,
                "category": category or "Genel",
                "condition": condition or "İyi Durumda",
            }
            
            logger.info(f"Calling Edge Function with payload: {payload}")
            
            response = await client.post(
                self.CACHED_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": "application/json",
                },
            )
            
            logger.info(f"Edge function response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Edge function response: success={data.get('success')}, price={data.get('price')}")
                
                # Edge Function returns structured response
                return self._parse_edge_response(data)
            
            error_text = response.text[:200] if response.text else "No response body"
            logger.warning(f"Edge function returned {response.status_code}: {error_text}")
            return None
        
        except Exception as e:
            logger.error(f"Edge function call error: {e}")
            return None
    
    def _parse_edge_response(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse Edge Function response (already structured)"""
        if not data.get("success"):
            logger.warning(f"Edge function returned error: {data.get('error')}")
            return None
        
        # Edge Function returns: { success, price, min_price, max_price, avg_price, confidence, result }
        return {
            "suggested_price": data.get("price"),
            "price_range": (data.get("min_price"), data.get("max_price")),
            "market_info": data.get("result"),  # Full explanation text
            "confidence": data.get("confidence", 0.7),
            "source": "edge_function",
            "cached": data.get("cached", False),
        }
    
    def _parse_perplexity_response(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse Perplexity API response for price info"""
        try:
            content = data.get("content") or data.get("answer") or ""
            
            if not content:
                return None
            
            # Try to extract price from response
            import re
            
            # Match Turkish Lira amounts
            price_patterns = [
                r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:TL|₺|lira)",
                r"(?:fiyat|değer|piyasa)[^\d]*(\d{1,3}(?:\.\d{3})*)",
                r"(\d+)\s*-\s*(\d+)\s*(?:TL|₺|lira)",  # Range: 1000 - 2000 TL
            ]
            
            prices = []
            for pattern in price_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    if isinstance(match, tuple):
                        # Range match
                        for m in match:
                            if m:
                                price = self._parse_price(m)
                                if price and 10 < price < 10000000:  # Sanity check
                                    prices.append(price)
                    else:
                        price = self._parse_price(match)
                        if price and 10 < price < 10000000:
                            prices.append(price)
            
            if not prices:
                return {"market_info": content}
            
            # Calculate suggested price (median)
            prices.sort()
            median = prices[len(prices) // 2]
            
            return {
                "suggested_price": median,
                "price_range": (min(prices), max(prices)) if len(prices) > 1 else None,
                "market_info": content[:500],  # Truncate
            }
        
        except Exception as e:
            logger.warning(f"Failed to parse Perplexity response: {e}")
            return None
    
    def _parse_price(self, price_str: str) -> Optional[float]:
        """Parse price string to float"""
        try:
            # Remove thousand separators (dots) and convert comma to dot
            clean = str(price_str).replace(".", "").replace(",", ".")
            return float(clean)
        except (ValueError, TypeError):
            return None


# Singleton
price_service = PriceService()
