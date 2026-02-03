"""
PazarGlobal Agent V3 - Single LLM Brain

Ana Beyin:
- Vision Security Guard
- Serbest konuşma
- Intent belirleme (CREATE, SEARCH, CHAT)
- JSON üretme (Supabase listings schema)
- Override yetkisi: SADECE iptal/reset
- Tek tool: Perplexity (fiyat önerisi)

FSM Engine JSON'u alır, validate eder, publish eder.
"""
import asyncio
import json
import re
from typing import Optional, Dict, Any, List, cast
from dataclasses import dataclass, field
from enum import Enum

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from services.logger import get_logger
from config import settings

logger = get_logger(__name__)


class Intent(Enum):
    CREATE = "CREATE"
    SEARCH = "SEARCH"
    CHAT = "CHAT"
    CANCEL = "CANCEL"  # FSM override - işlemi iptal et


@dataclass
class BrainOutput:
    """LLM çıktısı"""
    intent: Intent
    response_text: str
    listing_data: Dict[str, Any]
    missing_fields: List[str]
    ready_for_fsm: bool  # FSM'e gönderilmeye hazır mı
    user_confirmed: bool  # Kullanıcı onay verdi mi
    tool_call: Optional[Dict[str, str]] = None  # {"name": "perplexity", "query": "..."}
    suggestions: List[str] = field(default_factory=list)  # Başlık/açıklama tavsiyeleri
    raw_response: Dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# SUPABASE LISTINGS SCHEMA - FSM'in beklediği format
# ═══════════════════════════════════════════════════════════════════

LISTING_SCHEMA = {
    "title": {"type": "string", "max_length": 200, "required": True},
    "description": {"type": "string", "min_length": 10, "max_length": 2000, "required": True},  # REQUIRED - matches REQUIRED_FIELDS
    "category": {
        "type": "enum",
        "values": ["Elektronik", "Otomotiv", "Emlak", "Mobilya & Dekorasyon", 
                   "Moda & Aksesuar", "Spor & Hobi", "Hobi, Koleksiyon & Sanat", "Diğer", "Sistem"],
        "required": False  # FSM auto-determines, LLM writes "Sistem"
    },
    "price": {"type": "number", "min": 1, "max": 100_000_000, "required": True},
    "condition": {
        "type": "enum",
        "values": ["Sıfır", "Az Kullanılmış", "2. El"],
        "required": False,
        "default": "2. El"
    },
    "location": {"type": "string", "max_length": 100, "required": False},
    "images": {"type": "array", "required": False},  # FSM zorunlu tutmaz
}

# Category FSM tarafından otomatik belirlenir - LLM sorumlu değil!
REQUIRED_FIELDS = ["title", "price", "description"]


# ═══════════════════════════════════════════════════════════════════
# GUARDRAILS - LLM çıktısını validate ve sanitize
# ═══════════════════════════════════════════════════════════════════

class Guardrails:
    """LLM çıktısını validate et - deterministik, halüsinasyon yok"""
    
    # "Sistem" is a sentinel value - LLM writes it, FSM replaces with real category
    ALLOWED_CATEGORIES = {
        "Elektronik", "Otomotiv", "Emlak", "Mobilya & Dekorasyon",
        "Moda & Aksesuar", "Spor & Hobi", "Hobi, Koleksiyon & Sanat", "Diğer",
        "Sistem"  # Sentinel - FSM will auto-determine from title/description
    }
    
    ALLOWED_CONDITIONS = {"Sıfır", "Az Kullanılmış", "2. El"}
    
    CANCEL_PATTERNS = [
        r"\b(iptal|vazgeç|vazgec|istemiyorum|bırak|birak|dur|durdur|reset|sıfırla|sifirla)\b",
        r"^(hayır|yok|olmaz)$"
    ]
    
    @classmethod
    def detect_cancel(cls, message: str) -> bool:
        """Kullanıcı işlemi iptal etmek istiyor mu?"""
        msg_lower = message.lower().strip()
        for pattern in cls.CANCEL_PATTERNS:
            if re.search(pattern, msg_lower):
                return True
        return False
    
    @classmethod
    def detect_confirmation(cls, message: str) -> bool:
        """Kullanıcı onay veriyor mu?"""
        msg_lower = message.lower().strip()
        
        # Negative patterns - bunlar onay DEĞİL
        negative_patterns = [
            r"bekliyorum",
            r"bekle",
            r"düşün",
            r"bakarım",
            r"sonra",
            r"değil",
            r"hayır",
            r"iptal",
        ]
        for pattern in negative_patterns:
            if re.search(pattern, msg_lower):
                return False
        
        # Positive patterns - bunlar onay
        # NOTE: FSM uses 2-step confirmation: "yayınla" → preview → "onayla" → publish
        # So Brain's confirmation is just for initial "yayınla" detection
        confirm_patterns = [
            r"^(yayınla|yayinla|onayla|onaylıyorum|onayliyorum)$",  # Tek kelime - evet/olur ÇIKARTILDI (çok belirsiz)
            r"\byayınla\b",
            r"\byayinla\b",
            r"\bonaylıyorum\b",
            r"\bonayliyorum\b",
            r"\bonayla\b",
            # "^tamam$" ÇIKARTILDI - çok belirsiz, "anladım" anlamında da kullanılır
            # "^evet$" ÇIKARTILDI - çok belirsiz
            # "^olur$" ÇIKARTILDI - çok belirsiz
            r"yayına al",
            r"onay.*ver",
            r"ilan.*yayınla",
        ]
        for pattern in confirm_patterns:
            if re.search(pattern, msg_lower):
                return True
        return False
    
    @classmethod
    def sanitize(cls, llm_response: Dict[str, Any], user_message: str) -> BrainOutput:
        """LLM çıktısını sanitize et"""
        
        # Önce iptal kontrolü
        if cls.detect_cancel(user_message):
            return BrainOutput(
                intent=Intent.CANCEL,
                response_text="✅ İşlem iptal edildi. Yeni bir işlem için hazırım.",
                listing_data={},
                missing_fields=[],
                ready_for_fsm=False,
                user_confirmed=False,
                raw_response={"cancelled": True}
            )
        
        # 1. Intent kontrolü
        intent_str = llm_response.get("intent", "CHAT").upper()
        if intent_str == "CANCEL":
            intent = Intent.CANCEL
        elif intent_str == "CREATE":
            intent = Intent.CREATE
        elif intent_str == "SEARCH":
            intent = Intent.SEARCH
        else:
            intent = Intent.CHAT
        
        # 2. Listing data kontrolü - FSM'in beklediği formata uygun
        listing_data = llm_response.get("listing_data") or {}
        sanitized_data = {}
        
        # Title
        if listing_data.get("title"):
            sanitized_data["title"] = str(listing_data["title"])[:200]
        
        # Description - fazla bilgiler buraya eklenir
        if listing_data.get("description"):
            sanitized_data["description"] = str(listing_data["description"])[:2000]
        
        # Category - enum kontrolü
        category = listing_data.get("category")
        if category in cls.ALLOWED_CATEGORIES:
            sanitized_data["category"] = category
        
        # Price - sayısal kontrol
        price = listing_data.get("price")
        if price is not None:
            try:
                price_val = float(price)
                if 1 <= price_val <= 100_000_000:
                    sanitized_data["price"] = price_val
            except (ValueError, TypeError):
                pass
        
        # Condition - enum kontrolü
        condition = listing_data.get("condition")
        if condition in cls.ALLOWED_CONDITIONS:
            sanitized_data["condition"] = condition
        else:
            sanitized_data["condition"] = "2. El"  # Default
        
        # Location
        if listing_data.get("location"):
            sanitized_data["location"] = str(listing_data["location"])[:100]
        
        # Images
        images = listing_data.get("images")
        if isinstance(images, list):
            sanitized_data["images"] = [str(url)[:500] for url in images[:10] if url]
        
        # 3. Missing fields
        missing = []
        for field_name in REQUIRED_FIELDS:
            if field_name not in sanitized_data or sanitized_data[field_name] is None:
                missing.append(field_name)
        
        # 4. Ready for FSM - tüm zorunlu alanlar dolu
        ready_for_fsm = len(missing) == 0 and intent == Intent.CREATE
        
        # 5. User confirmed
        user_confirmed = cls.detect_confirmation(user_message) and ready_for_fsm
        
        # 6. Tool call
        # DEPRECATED: JSON-based tool_call removed - we use native OpenAI function calling
        # Tool calls are handled in Brain.process() BEFORE Guardrails.sanitize() is called
        # If we reach here, there's no tool call (native function calling returns early)
        tool_call = None
        
        # 7. Suggestions
        suggestions = llm_response.get("suggestions") or []
        if isinstance(suggestions, list):
            suggestions = [str(s)[:200] for s in suggestions[:3]]
        else:
            suggestions = []
        
        return BrainOutput(
            intent=intent,
            response_text=str(llm_response.get("response_text", ""))[:2000],
            listing_data=sanitized_data,
            missing_fields=missing,
            ready_for_fsm=ready_for_fsm,
            user_confirmed=user_confirmed,
            tool_call=tool_call,
            suggestions=suggestions,
            raw_response=llm_response,
        )


# ═══════════════════════════════════════════════════════════════════
# INPUT SANITIZATION
# ═══════════════════════════════════════════════════════════════════

def sanitize_input(message: str) -> str:
    """Kullanıcı girdisini temizle - prompt injection koruması"""
    
    if not message:
        return ""
    
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
    ]
    for pattern in injection_patterns:
        message = re.sub(pattern, "", message, flags=re.IGNORECASE)
    
    return message.strip()


# ═══════════════════════════════════════════════════════════════════
# SYSTEM PROMPT - LLM Brain Talimatları
# ═══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """# PazarGlobal İlan Asistanı - Ana Beyin

Sen PazarGlobal'ın yapay zeka asistanısın. Kullanıcıyla serbest, doğal bir şekilde sohbet edersin ama JSON üretiminde tamamen deterministiksin.

## GÖREVLER

1. **Vision Analizi (ÇOK ÖNEMLİ)**: Kullanıcı görsel gönderdiğinde:
   - Uygunsuz içerik varsa REDDET
   - Görselde ne gördüğünü AÇIKLA
   - Ürünün fiziksel durumunu değerlendir (çizik, hasar, temizlik)
   - Marka/model tespit et
   - Renk, boyut gibi detayları çıkar
   - Bu bilgileri description alanına zenginleştirerek yaz
   
   Örnek: Görsel analizi: "Siyah iPhone 13, ekran ve kasa temiz görünüyor, kutulu, şarj kablosu mevcut."

2. **Intent Belirleme**:
   - CREATE: "satmak istiyorum", "satıyorum", "ilan ver", "satılık"
   - SEARCH: "var mı", "arıyorum", "bul", "ara"
   - CHAT: merhaba, teşekkürler, yardım, diğer sohbet
   - Not: İptal tespiti Guardrails tarafından yapılır

3. **JSON Üretme**: Supabase listings tablosuna uygun JSON üret. ŞEMAYI DEĞİŞTİRME.

4. **Preview Göster (HER MESAJDA ZORUNLU)**: Her yanıtta ilanın güncel durumunu göster:
   ```
   📋 İlan Önizleme:
   ✅ Başlık: Samsung Galaxy S24
   ✅ Fiyat: 45.000 TL
   ✅ Kategori: Elektronik
   ✅ Açıklama: Siyah renk, 256GB, kutulu...
   ✅ Durum: 2. El
   ⏳ Konum: (eksik)
   📷 Fotoğraf: 1 adet
   
   Yayınlamak için 'yayınla' yazabilirsiniz.
   ```

5. **Tavsiye Ver**: Başlık ve açıklama için iyileştirme öner.

6. **Perplexity Tool**: SADECE "kaç para eder", "fiyat öner", "piyasa değeri" sorulduğunda çağır.

## JSON SCHEMA (Supabase listings - DEĞİŞTİRİLEMEZ)

```json
{
  "title": "string, max 200 karakter, ZORUNLU",
  "description": "string, min 10 karakter, max 2000 karakter, ZORUNLU - ürün detayları ve görsel analizi buraya",
  "category": "Sistem (FSM otomatik belirler - SEN TAHMİN YAPMA!)",
  "price": "number, 1-100000000 arası TL, ZORUNLU",
  "condition": "Sıfır|Az Kullanılmış|2. El, default: 2. El",
  "location": "string, şehir, opsiyonel",
  "images": "array of URLs, opsiyonel (FSM resim zorunlu tutmaz)"
}
```

**KATEGORİ KURALI (ÇOK ÖNEMLİ!):**
- KATEGORİYİ SEN BELİRLEME! Her zaman "Sistem" yaz.
- FSM yayın anında başlık ve açıklamadan otomatik belirleyecek.
- Önizlemede "Kategori: Sistem belirleyecek" göster.
- Kullanıcıya "Kategori sistem tarafından otomatik belirlenecek" de.

## EKSTRA BİLGİ KURALI

Kullanıcı schema dışı bilgi verirse VEYA görsellerden tespit edersen, bunları description alanına ekle:
- Araba: model yılı, km, tramer durumu, renk
- Telefon: hafıza, renk, aksesuar, ekran/kasa durumu
- Emlak: oda sayısı, metrekare, kat, ısınma
- Genel: marka, model, renk, boyut, fiziksel durum

Örnek: "2020 model, 45.000 km, tramersiz, gri renk" → description: "2020 model araç. Gri renk, 45.000 km'de, tramersiz. Bakımlı ve temiz."

## OUTPUT FORMAT (HER ZAMAN JSON)

```json
{
  "intent": "CREATE|SEARCH|CHAT",
  "response_text": "Türkçe, samimi kullanıcı mesajı + HER ZAMAN preview göster",
  "listing_data": {
    "title": "...",
    "description": "...",
    "category": "Sistem",
    "price": 0,
    "condition": "...",
    "location": "...",
    "images": []
  },
  "suggestions": ["Başlık önerisi: ...", "Açıklama önerisi: ..."]
}
```

## PERPLEXITY FİYAT ARAŞTIRMASI

Sistem otomatik olarak "kaç para eder", "fiyat öner", "piyasa değeri" sorularını algılar ve Perplexity API'yi çağırır.
Sen sadece normal JSON yanıtı döndür - tool çağrısı sistem tarafından otomatik yapılır.

ÖNEMLİ: "kaç para eder" sorulduğunda SEARCH intent KULLANMA! CHAT intent kullan.

## YASAKLAR
- Schema'ya olmayan alan ekleme (örn: km, tramer alanı yok - description'a yaz)
- Fiyat tahmini/uydurma (Perplexity kullan veya kullanıcıya sor)
- Eksik alanlarla ready_for_fsm: true döndürme
- Preview GÖSTERMEDEN yanıt verme (her mesajda güncel durumu göster!)
- "kaç para eder" sorgularını SEARCH olarak yorumlama - her zaman tool_call kullan!"""


# ═══════════════════════════════════════════════════════════════════
# BRAIN - Ana LLM Beyni
# ═══════════════════════════════════════════════════════════════════

class Brain:
    """
    Ana Beyin - Tek LLM
    
    Görevler:
    - Vision security guard
    - Serbest sohbet
    - Intent belirleme
    - JSON üretme (deterministik)
    - Preview sunma
    - Tavsiye verme
    
    Override yetkisi: SADECE iptal (Guardrails tarafından)
    Tek tool: Perplexity (fiyat önerisi)
    """
    
    # Perplexity tool definition for OpenAI function calling
    # Type hint: ChatCompletionToolParam
    PERPLEXITY_TOOL: dict = {
        "type": "function",
        "function": {
            "name": "perplexity_price_research",
            "description": "Bir ürünün piyasa fiyatını araştırır. SADECE kullanıcı fiyat öğrenmek istediğinde çağır: 'kaç para eder', 'fiyatı ne kadar', 'piyasa değeri', 'ne kadara satılır' gibi sorularda.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Fiyatı araştırılacak ürünün adı ve modeli (örn: 'Samsung Galaxy S21', 'iPhone 14 Pro 256GB', 'Golf 7 1.6 TDI')"
                    }
                },
                "required": ["product_name"]
            }
        }
    }
    
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
        # YENİ: Zengin context parametreleri
        fsm_state: str = "IDLE",  # IDLE, DRAFTING, READY
        missing_fields: Optional[List[str]] = None,
        last_intent: Optional[str] = None,
    ) -> BrainOutput:
        """
        Ana beyin fonksiyonu.
        
        Args:
            message: Kullanıcı mesajı
            current_listing: Mevcut listing_data (session'dan)
            images: Görsel URL'leri
            conversation_history: Geçmiş mesajlar
            fsm_state: FSM durumu (IDLE, DRAFTING, READY)
            missing_fields: Eksik alanlar listesi
            last_intent: Son intent (CREATE, SEARCH, CHAT)
        
        Returns:
            BrainOutput: Sanitized LLM çıktısı
        """
        try:
            # Input sanitization
            clean_message = sanitize_input(message)
            
            # Önce iptal kontrolü - LLM'e gitmeden
            if Guardrails.detect_cancel(clean_message):
                return BrainOutput(
                    intent=Intent.CANCEL,
                    response_text="✅ İşlem iptal edildi. Yeni bir işlem başlatmak için hazırım!",
                    listing_data={},
                    missing_fields=[],
                    ready_for_fsm=False,
                    user_confirmed=False,
                    raw_response={"cancelled": True}
                )
            
            # Build context
            context = self._build_context(
                current_listing=current_listing,
                fsm_state=fsm_state,
                missing_fields=missing_fields or [],
                last_intent=last_intent,
            )
            
            # Build messages
            messages = self._build_messages(clean_message, context, images, conversation_history)
            
            # Choose model
            model = self.vision_model if images else self.model
            
            logger.debug(f"Brain calling LLM: model={model}, message={clean_message[:100]}, fsm_state={fsm_state}")
            
            # Call LLM with timeout and native function calling
            try:
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tools=cast(Any, [self.PERPLEXITY_TOOL]),  # Native function calling!
                        tool_choice="auto",  # LLM decides when to call
                        response_format={"type": "json_object"},
                        max_tokens=1500,
                        temperature=0.3,  # Deterministik JSON için düşük
                    ),
                    timeout=30.0  # 30 second timeout
                )
            except asyncio.TimeoutError:
                logger.error("Brain LLM call timed out after 30s")
                return self._fallback_response("LLM timeout - tekrar deneyin")
            
            # Check if LLM wants to call a tool
            choice = response.choices[0]
            if choice.message.tool_calls:
                tool_call = choice.message.tool_calls[0]
                # Access function info safely
                func_name = getattr(tool_call, 'function', None)
                if func_name and hasattr(func_name, 'name') and func_name.name == "perplexity_price_research":
                    import json as json_module
                    args = json_module.loads(func_name.arguments)
                    product_name = args.get("product_name", clean_message)
                    logger.info(f"Brain requested Perplexity tool for: {product_name}")
                    
                    # Return special output indicating tool call needed
                    return BrainOutput(
                        intent=Intent.CHAT,
                        response_text="🔍 Fiyat araştırması yapıyorum...",
                        listing_data={},
                        missing_fields=[],
                        ready_for_fsm=False,
                        user_confirmed=False,
                        raw_response={"tool_requested": True},
                        tool_call={"name": "perplexity", "query": product_name}
                    )
            
            # Parse regular response
            content = choice.message.content
            logger.debug(f"Brain LLM response: {content[:500] if content else 'EMPTY'}")
            if not content:
                raise ValueError("LLM response is empty")
            llm_output = json.loads(content)
            
            # Guardrails - validate and sanitize
            result = Guardrails.sanitize(llm_output, clean_message)
            
            logger.info(f"Brain: intent={result.intent.value}, ready_for_fsm={result.ready_for_fsm}, confirmed={result.user_confirmed}, missing={result.missing_fields}")
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"LLM JSON parse error: {e}, raw content may be malformed")
            return self._fallback_response("JSON parse hatası - LLM geçersiz yanıt döndü")
        
        except Exception as e:
            error_type = type(e).__name__
            logger.error(f"Brain error ({error_type}): {e}", exc_info=True)
            # More helpful error message for debugging
            if "rate_limit" in str(e).lower():
                return self._fallback_response("API rate limit - biraz bekleyin")
            elif "timeout" in str(e).lower():
                return self._fallback_response("API timeout - tekrar deneyin")
            return self._fallback_response(f"Sistem hatası: {error_type}")
    
    def _build_context(
        self,
        current_listing: Optional[Dict],
        fsm_state: str,
        missing_fields: List[str],
        last_intent: Optional[str],
    ) -> str:
        """
        Zengin context oluştur - Brain'in durumu anlaması için
        """
        lines = ["## 📍 MEVCUT DURUM"]
        
        # FSM State
        state_emoji = {"IDLE": "🆕", "DRAFTING": "✏️", "READY": "✅"}.get(fsm_state, "❓")
        state_desc = {
            "IDLE": "Yeni başlıyor, henüz ilan oluşturma başlamadı",
            "DRAFTING": "İlan oluşturuluyor, bazı bilgiler eksik",
            "READY": "İlan hazır, kullanıcı onayı bekleniyor"
        }.get(fsm_state, "Bilinmiyor")
        lines.append(f"**Durum:** {state_emoji} {fsm_state} - {state_desc}")
        
        # Last Intent
        if last_intent:
            lines.append(f"**Son işlem:** {last_intent}")
        
        # Current Listing Data
        if current_listing:
            lines.append("\n**Mevcut İlan Verisi:**")
            lines.append("```json")
            lines.append(json.dumps(current_listing, ensure_ascii=False, indent=2))
            lines.append("```")
            
            # Filled fields
            filled = [k for k, v in current_listing.items() if v]
            if filled:
                lines.append(f"✅ Dolu alanlar: {', '.join(filled)}")
        else:
            lines.append("\n**Mevcut İlan Verisi:** Henüz yok (yeni başlıyor)")
        
        # Missing Fields
        if missing_fields:
            lines.append(f"⏳ **Eksik zorunlu alanlar:** {', '.join(missing_fields)}")
            lines.append("→ Bu alanları kullanıcıdan iste!")
        elif fsm_state == "READY":
            lines.append("✅ **Tüm zorunlu alanlar tamam!** Yayınlamak için onay iste.")
        
        # Instructions based on state
        lines.append("\n## 📋 NE YAPMALISIN")
        if fsm_state == "IDLE":
            lines.append("- Yeni kullanıcı, selamla ve ne yapmak istediğini sor")
            lines.append("- İlan vermek istiyorsa CREATE intent ile başla")
        elif fsm_state == "DRAFTING":
            lines.append("- Eksik alanları doğal bir şekilde sor")
            lines.append("- Mevcut verileri KORU, üzerine ekle")
            lines.append("- Her mesajda preview göster")
        elif fsm_state == "READY":
            lines.append("- İlan hazır, son preview göster")
            lines.append("- Kullanıcıdan 'yayınla' onayı iste")
            lines.append("- Düzenleme isterse yardımcı ol")
        
        return "\n".join(lines)
    
    def _build_messages(
        self,
        message: str,
        context: str,
        images: Optional[List[str]],
        history: Optional[List[Dict]],
    ) -> List[ChatCompletionMessageParam]:
        """LLM mesajlarını oluştur"""

        messages: List[ChatCompletionMessageParam] = [
            cast(ChatCompletionMessageParam, {"role": "system", "content": SYSTEM_PROMPT})
        ]
        
        # Conversation history (son 10 mesaj)
        if history:
            for msg in history[-10:]:
                messages.append(
                    cast(
                        ChatCompletionMessageParam,
                        {
                            "role": msg.get("role", "user"),
                            "content": msg.get("content", ""),
                        },
                    )
                )
        
        # User message content
        user_content: List[Dict[str, Any]] = []
        
        # Zengin context
        user_content.append({"type": "text", "text": context})
        
        # User message
        user_content.append({"type": "text", "text": f"\n## 💬 KULLANICI MESAJI\n{message}"})
        
        # Images - Vision analysis
        if images:
            user_content.append({"type": "text", "text": "\n## 📷 GELEN GÖRSELLER"})
            for img_url in images[:3]:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": img_url, "detail": "low"}
                })
        
        messages.append(
            cast(ChatCompletionMessageParam, {"role": "user", "content": user_content})
        )

        return cast(List[ChatCompletionMessageParam], messages)
    
    def _fallback_response(self, error: str) -> BrainOutput:
        """Hata durumunda fallback"""
        return BrainOutput(
            intent=Intent.CHAT,
            response_text="🔄 Bir sorun oluştu. Lütfen tekrar deneyin.",
            listing_data={},
            missing_fields=[],
            ready_for_fsm=False,
            user_confirmed=False,
            raw_response={"error": error}
        )


# Singleton
brain = Brain()
