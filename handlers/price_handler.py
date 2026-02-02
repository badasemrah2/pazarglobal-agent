"""
Price Handler - Standalone price research (not part of listing flow)

Handles:
- "iPhone 15 kaç para eder"
- "fiyat öner Samsung S24"
- "MacBook piyasa değeri"

Uses Edge Function for price research.
"""
import re
from typing import Optional

from core.response_builder import create_builder, Response, Button
from services.price_service import price_service
from services.logger import get_logger

logger = get_logger(__name__)


class PriceHandler:
    """Standalone price research handler"""
    
    def __init__(self):
        self.response_builder = None
    
    async def handle(
        self,
        user_id: str,
        message: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Handle standalone price research request.
        
        Examples:
        - "iPhone 15 Pro kaç para eder"
        - "Samsung S24 fiyatı nedir"
        - "MacBook Air 2023 piyasa değeri"
        """
        self.response_builder = create_builder(channel)
        
        # Extract product name from message
        product_name = self._extract_product_name(message)
        
        if not product_name:
            # Ask for product name
            return Response(
                text="🔍 Fiyat araştırması için ürünün marka ve modelini yazmanız gerekiyor.\n\n"
                     "Örnek: 'iPhone 13 128GB' veya 'Samsung S24 Ultra'",
                buttons=[],
                metadata={},
                channel=self.response_builder.channel,
            )
        
        # Extract condition hint
        condition = self._extract_condition(message)
        category = self._extract_category(product_name)
        
        logger.info(f"Price research: product={product_name}, condition={condition}, category={category}")
        
        try:
            # Call price service
            result = await price_service.get_market_price(
                product_name=product_name,
                category=category,
                condition=condition,
            )
            
            if result and result.get("suggested_price"):
                price = result["suggested_price"]
                min_price = result.get("price_range", (None, None))[0]
                max_price = result.get("price_range", (None, None))[1]
                
                # Build response
                text = f"📊 **{product_name}** ({condition or '2. El'})\n\n"
                text += f"💰 Ortalama Piyasa Değeri: **{int(price):,} TL**\n"
                
                if min_price and max_price:
                    text += f"📉 Aralık: {int(min_price):,} TL - {int(max_price):,} TL\n"
                
                text += "\nBu bilgiler güncel pazar verilerine dayanmaktadır.\n\n"
                text += "Ne yapmak istersiniz?\n"
                text += "• 'ilan ver' yazarak bu fiyattan satabilirsiniz\n"
                text += "• 'benzer ara' yazarak ilanlara bakabilirsiniz"
                
                return Response(
                    text=text,
                    buttons=[
                        Button("İlan Ver", "ilan vermek istiyorum"),
                        Button("Benzer Ara", f"{product_name} var mı"),
                    ],
                    metadata={
                        "suggested_price": price,
                        "product_name": product_name,
                    },
                    channel=self.response_builder.channel,
                )
            else:
                return Response(
                    text=f"😕 **{product_name}** için fiyat bilgisi bulunamadı.\n\n"
                         f"Lütfen daha spesifik bir model adı deneyin.\n"
                         f"Örnek: 'iPhone 15 Pro Max 256GB'",
                    buttons=[],
                    metadata={},
                    channel=self.response_builder.channel,
                )
                
        except Exception as e:
            logger.error(f"Price research error: {e}", exc_info=True)
            return Response(
                text=f"😕 Fiyat araştırması sırasında bir hata oluştu.\n\n"
                     f"Lütfen tekrar deneyin.",
                buttons=[],
                metadata={},
                channel=self.response_builder.channel,
            )
    
    def _extract_product_name(self, message: str) -> Optional[str]:
        """Extract product name from price query"""
        # Remove price query patterns to get product name
        patterns_to_remove = [
            r"\b(?:kaç|ne\s*kadar)\s*(?:para|tl|lira|eder|ederi)?\b",
            r"\bfiyat\s+(?:öner|oner|araştır|arastir|nedir|ne)\b",  # "fiyat öner" etc
            r"\bfiyat(?:ı|i)?\s*(?:nedir|ne)?\b",  # "fiyatı nedir", "fiyat"
            r"\bpiyasa\s*(?:değeri|degeri|fiyatı|fiyati)?\b",
            r"\bkaça\s*(?:satılır|satilir|gider)?\b",
            r"\bederi\s*(?:nedir|ne)?\b",
            r"\bikinci\s*el\b",
            r"\b2\.?\s*el\b",
            r"\bsıfır\b",
            r"\baz\s*kullanılmış\b",
            r"\b(?:öner|oner)\b",  # standalone "öner" or "oner"
        ]
        
        result = message
        for pattern in patterns_to_remove:
            result = re.sub(pattern, "", result, flags=re.IGNORECASE)
        
        # Clean up
        result = re.sub(r"\s+", " ", result).strip()
        
        # If result is too short, probably not a valid product name
        if len(result) < 3:
            return None
        
        return result
    
    def _extract_condition(self, message: str) -> Optional[str]:
        """Extract condition from message"""
        message_lower = message.lower()
        
        if "sıfır" in message_lower or "sifir" in message_lower:
            return "Sıfır"
        elif "az kullan" in message_lower:
            return "Az Kullanılmış"
        elif "2. el" in message_lower or "ikinci el" in message_lower or "2.el" in message_lower:
            return "2. El"
        
        # Default to 2. El for price research
        return "2. El"
    
    def _extract_category(self, product_name: str) -> Optional[str]:
        """Infer category from product name"""
        product_lower = product_name.lower()
        
        # Phone patterns
        if any(brand in product_lower for brand in ["iphone", "samsung", "xiaomi", "huawei", "oppo", "realme", "redmi"]):
            return "Elektronik"
        
        # Computer patterns
        if any(brand in product_lower for brand in ["macbook", "laptop", "notebook", "bilgisayar", "pc", "dell", "lenovo", "hp", "asus"]):
            return "Elektronik"
        
        # Car patterns
        if any(brand in product_lower for brand in ["bmw", "mercedes", "audi", "toyota", "honda", "ford", "fiat", "renault", "volkswagen", "volvo", "citroen", "hyundai", "kia"]):
            return "Araç"
        
        # Gaming
        if any(brand in product_lower for brand in ["playstation", "ps5", "ps4", "xbox", "nintendo", "switch"]):
            return "Elektronik"
        
        return "Genel"


# Singleton
price_handler = PriceHandler()
