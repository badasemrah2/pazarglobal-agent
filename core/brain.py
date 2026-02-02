"""
PazarGlobal Agent V3 - Single LLM Brain

Tek LLM, tek tool, iki FSM.
Bu dosya tüm LLM etkileşimlerini yönetir.
"""
import json
import re
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum

from openai import AsyncOpenAI

from services.logger import get_logger
from config import settings

logger = get_logger(__name__)


class Intent(Enum):
    CREATE = "CREATE"
    SEARCH = "SEARCH"
    CHAT = "CHAT"


@dataclass
class BrainOutput:
    """LLM çıktısı - sanitize edilmiş"""
    intent: Intent
    response_text: str
    listing_data: Dict[str, Any]
    missing_fields: List[str]
    ready_to_publish: bool
    tool_call: Optional[Dict[str, str]]  # {"name": "perplexity", "query": "..."}
    raw_response: Dict[str, Any]  # Debug için


# ═══════════════════════════════════════════════════════════════════
# GUARDRAILS - LLM çıktısını validate ve sanitize
# ═══════════════════════════════════════════════════════════════════

class Guardrails:
    """LLM çıktısını validate et - asla güvenme, her zaman doğrula"""
    
    ALLOWED_FIELDS = {"title", "description", "price", "category", "condition", "location", "images"}
    ALLOWED_INTENTS = {"CREATE", "SEARCH", "CHAT"}
    ALLOWED_CATEGORIES = {
        "Elektronik", "Otomotiv", "Emlak", 
        "Mobilya & Dekorasyon", "Giyim & Aksesuar", 
        "Spor & Hobi", "Diğer"
    }
    ALLOWED_CONDITIONS = {"Sıfır", "Az Kullanılmış", "İyi", "Yıpranmış"}
    REQUIRED_FOR_PUBLISH = {"title", "price", "category"}
    
    @classmethod
    def sanitize(cls, llm_response: Dict[str, Any]) -> BrainOutput:
        """LLM çıktısını sanitize et"""
        
        # 1. Intent kontrolü
        intent_str = llm_response.get("intent", "CHAT").upper()
        if intent_str not in cls.ALLOWED_INTENTS:
            intent_str = "CHAT"
        intent = Intent(intent_str)
        
        # 2. Listing data kontrolü
        listing_data = llm_response.get("listing_data") or {}
        sanitized_data = {}
        
        for field in cls.ALLOWED_FIELDS:
            if field in listing_data and listing_data[field] is not None:
                sanitized_data[field] = cls._validate_field(field, listing_data[field])
        
        # 3. Missing fields
        missing = []
        for field in cls.REQUIRED_FOR_PUBLISH:
            if field not in sanitized_data or sanitized_data[field] is None:
                missing.append(field)
        
        # 4. Ready to publish
        ready = len(missing) == 0 and intent == Intent.CREATE
        
        # 5. Tool call validation
        tool_call = None
        raw_tool = llm_response.get("tool_call")
        if raw_tool and isinstance(raw_tool, dict):
            if raw_tool.get("name") == "perplexity" and raw_tool.get("query"):
                tool_call = {"name": "perplexity", "query": str(raw_tool["query"])[:200]}
        
        return BrainOutput(
            intent=intent,
            response_text=str(llm_response.get("response_text", ""))[:2000],
            listing_data=sanitized_data,
            missing_fields=missing,
            ready_to_publish=ready,
            tool_call=tool_call,
            raw_response=llm_response,
        )
    
    @classmethod
    def _validate_field(cls, field: str, value: Any) -> Any:
        """Alan bazında validation"""
        
        if field == "title":
            return str(value)[:100] if value else None
        
        elif field == "description":
            return str(value)[:1000] if value else None
        
        elif field == "price":
            try:
                price = float(value)
                if 1 <= price <= 100_000_000:
                    return price
            except (ValueError, TypeError):
                pass
            return None
        
        elif field == "category":
            if value in cls.ALLOWED_CATEGORIES:
                return value
            return None
        
        elif field == "condition":
            if value in cls.ALLOWED_CONDITIONS:
                return value
            return "İyi"  # Default
        
        elif field == "location":
            return str(value)[:100] if value else None
        
        elif field == "images":
            if isinstance(value, list):
                return [str(url)[:500] for url in value[:10]]
            return []
        
        return value


# ═══════════════════════════════════════════════════════════════════
# INPUT SANITIZATION
# ═══════════════════════════════════════════════════════════════════

def sanitize_input(message: str) -> str:
    """Kullanıcı girdisini temizle - prompt injection koruması"""
    
    if not message:
        return ""
    
    # Max length
    if len(message) > 2000:
        message = message[:2000]
    
    # Prompt injection patterns
    injection_patterns = [
        r"ignore\s+previous\s+instructions",
        r"forget\s+everything",
        r"system\s*:",
        r"assistant\s*:",
        r"<\|.*?\|>",
        r"\[INST\]",
        r"\[/INST\]",
        r"<<SYS>>",
        r"<</SYS>>",
    ]
    for pattern in injection_patterns:
        message = re.sub(pattern, "", message, flags=re.IGNORECASE)
    
    # Normalize whitespace
    message = " ".join(message.split())
    
    return message.strip()


# ═══════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """# PazarGlobal İlan Asistanı

Sen PazarGlobal'ın yapay zeka asistanısın. Görevin kullanıcıların ilan vermesine ve ürün aramasına yardımcı olmak.

## JSON Schema (DEĞİŞTİRİLEMEZ)

```json
{
  "title": "string (max 100 karakter, zorunlu)",
  "description": "string (max 1000 karakter, opsiyonel)",
  "price": "number (1-100000000 TL arası, zorunlu)",
  "category": "enum: Elektronik|Otomotiv|Emlak|Mobilya & Dekorasyon|Giyim & Aksesuar|Spor & Hobi|Diğer (zorunlu)",
  "condition": "enum: Sıfır|Az Kullanılmış|İyi|Yıpranmış (default: İyi)",
  "location": "string (şehir, opsiyonel)",
  "images": "array of URLs (opsiyonel)"
}
```

## KURALLAR

1. **Intent Belirleme**:
   - CREATE: satmak, satıyorum, ilan vermek, satılık
   - SEARCH: var mı, arıyorum, bul, ara, mevcut mu
   - CHAT: merhaba, yardım, teşekkürler, diğer her şey

2. **Fotoğraf Analizi**: Görsel geldiğinde ürünü tanı, category belirle, condition tahmin et. Fiyat TAHMİN ETME.

3. **Preview Göster**: Her adımda mevcut listing_data'yı preview olarak göster. ✅ dolu alanlar, ⏳ eksik alanlar.

4. **Perplexity Tool**: SADECE "kaç para eder", "fiyat öner", "piyasa değeri" sorulduğunda çağır.

5. **Ekstra Bilgi**: Schema dışı bilgiler description alanına ekle.

6. **ready_to_publish**: Ancak title + price + category doluysa true.

## OUTPUT FORMAT (HER ZAMAN)

```json
{
  "intent": "CREATE|SEARCH|CHAT",
  "response_text": "Türkçe kullanıcı mesajı",
  "listing_data": {"title": "...", "price": 0, "category": "...", ...},
  "missing_fields": ["field1"],
  "ready_to_publish": false,
  "tool_call": null
}
```

## YASAKLAR
- Schema dışı alan ekleme
- Fiyat tahmini yapma
- Eksik zorunlu alanlarla yayınlamaya izin verme"""


# ═══════════════════════════════════════════════════════════════════
# BRAIN - Tek LLM Beyni
# ═══════════════════════════════════════════════════════════════════

class Brain:
    """Tek LLM brain - intent, vision, slot filling hepsi burada"""
    
    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model or "gpt-4o"
        self.vision_model = settings.openai_vision_model or "gpt-4o"
    
    async def process(
        self,
        message: str,
        current_listing: Optional[Dict[str, Any]] = None,
        images: Optional[List[str]] = None,
        conversation_history: Optional[List[Dict]] = None,
    ) -> BrainOutput:
        """
        Ana beyin fonksiyonu - her mesajı işle.
        
        Args:
            message: Kullanıcı mesajı (sanitized)
            current_listing: Mevcut listing_data (session'dan)
            images: Görsel URL'leri
            conversation_history: Geçmiş mesajlar
        
        Returns:
            BrainOutput: Sanitized LLM çıktısı
        """
        try:
            # Input sanitization
            clean_message = sanitize_input(message)
            
            # Build messages
            messages = self._build_messages(clean_message, current_listing, images, conversation_history)
            
            # Choose model
            model = self.vision_model if images else self.model
            
            # Call LLM
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=1000,
                temperature=0.3,  # Daha deterministik
            )
            
            # Parse response
            content = response.choices[0].message.content
            llm_output = json.loads(content)
            
            # Guardrails - validate and sanitize
            result = Guardrails.sanitize(llm_output)
            
            logger.info(f"Brain output: intent={result.intent.value}, ready={result.ready_to_publish}, missing={result.missing_fields}")
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"LLM JSON parse error: {e}")
            return self._fallback_response("JSON parse hatası")
        
        except Exception as e:
            logger.error(f"Brain error: {e}", exc_info=True)
            return self._fallback_response(str(e))
    
    def _build_messages(
        self,
        message: str,
        current_listing: Optional[Dict],
        images: Optional[List[str]],
        history: Optional[List[Dict]],
    ) -> List[Dict]:
        """LLM mesajlarını oluştur"""
        
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # Add conversation history (last 10 messages)
        if history:
            for msg in history[-10:]:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })
        
        # Build user message content
        user_content = []
        
        # Current listing context
        if current_listing:
            context = f"Mevcut ilan durumu: {json.dumps(current_listing, ensure_ascii=False)}\n\n"
            user_content.append({"type": "text", "text": context})
        
        # User message
        user_content.append({"type": "text", "text": f"Kullanıcı: {message}"})
        
        # Images
        if images:
            for img_url in images[:3]:  # Max 3 görsel
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": img_url, "detail": "low"}
                })
        
        messages.append({"role": "user", "content": user_content})
        
        return messages
    
    def _fallback_response(self, error: str) -> BrainOutput:
        """Hata durumunda fallback response"""
        return BrainOutput(
            intent=Intent.CHAT,
            response_text="🔄 Bir saniye, tekrar deniyorum... Lütfen mesajınızı yeniden gönderin.",
            listing_data={},
            missing_fields=[],
            ready_to_publish=False,
            tool_call=None,
            raw_response={"error": error},
        )


# Singleton
brain = Brain()
