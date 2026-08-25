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
from config.category_profiles import INVENTED_CLAIMS, SALES_MOVES, render_profile_prompt

logger = get_logger(__name__)


def _safe_exception_text(exc: Exception) -> str:
    """Return exception text without triggering secondary errors from buggy __str__."""
    try:
        return str(exc)
    except Exception:
        try:
            return repr(exc)
        except Exception:
            return f"<{type(exc).__name__}>"


class Intent(Enum):
    CREATE = "CREATE"
    SEARCH = "SEARCH"
    CHAT = "CHAT"
    CANCEL = "CANCEL"  # FSM override - işlemi iptal et
    REPORT = "REPORT"  # İlan şikayet / ihbar


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
    report_data: Optional[Dict[str, Any]] = None  # REPORT intent için: {listing_id, reason}
    # Values the seller wrote ambiguously (e.g. "800.000₺+"). The model asks instead of
    # silently picking a reading: [{"field": "price", "question": "..."}]
    ambiguities: List[Dict[str, str]] = field(default_factory=list)
    # Seller explicitly asked for their number in the listing text. The model never writes
    # a number itself - the server appends the verified profile one at publish time.
    include_phone_in_description: bool = False


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
    "location": {"type": "string", "max_length": 100, "required": True},
    "images": {"type": "array", "required": False},  # FSM zorunlu tutmaz
}

# Category FSM tarafından otomatik belirlenir - LLM sorumlu değil!
REQUIRED_FIELDS = ["title", "price", "description", "location"]


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
    
    # Matching here destroys the draft (handle_message calls clear_session), so these have
    # to mean "abandon the listing" and nothing else.
    #
    # A bare "hayır"/"yok"/"olmaz" used to match. Those are ordinary answers to an ordinary
    # question - asked "kutusu var mı?", a seller replying "yok" had their whole draft
    # deleted. In the publish confirmation step "hayır" still cancels, via FSM_COMMANDS,
    # where it can only mean one thing and only returns to drafting.
    #
    # "istemiyorum" is likewise dropped as a standalone: "bu rengi istemiyorum" is about
    # the product, not the session.
    CANCEL_PATTERNS = [
        # Short, unambiguous commands on their own.
        r"^\s*(iptal|iptal\s*et|vazgeç|vazgeçtim|vazgec|vazgectim|dur|durdur|reset|sıfırla|sifirla|başa\s*dön|basa\s*don)\s*[.!]?\s*$",
        # Explicit statements about the listing/process itself.
        r"\b(işlemi|islemi|ilanı|ilani|ilan)\s+(iptal|sil|durdur)\b",
        r"\b(iptal\s+et(mek)?\s+istiyorum|vazgeçtim|vazgectim)\b",
        r"\bilan\s+vermek\s+istemiyorum\b",
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
    def response_indicates_cancel(cls, response_text: str) -> bool:
        """Model cevabı iptal/sonlandırma bildiriyor mu? (son emniyet katmanı)"""
        text = (response_text or "").lower().strip()
        if not text:
            return False
        return any(
            phrase in text
            for phrase in [
                "işlem iptal edildi",
                "ilanınız iptal",
                "iptal edilmiştir",
                "vazgeçildi",
            ]
        )
    
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

        raw_response_text = str(llm_response.get("response_text", ""))[:2000]
        
        # Önce iptal kontrolü
        if cls.detect_cancel(user_message) or cls.response_indicates_cancel(raw_response_text):
            cancel_text = raw_response_text.strip() or "✅ İşlem iptal edildi. Yeni bir işlem için hazırım."
            return BrainOutput(
                intent=Intent.CANCEL,
                response_text=cancel_text,
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
        elif intent_str == "REPORT":
            intent = Intent.REPORT
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
        
        # Category is deliberately NOT taken from the model. FSMEngine.validate() derives
        # it from the title/description against category_library, which is the single
        # source of truth; brain.ALLOWED_CATEGORIES only ever held a stale 8-entry subset.

        # Price - sayısal kontrol
        price = listing_data.get("price")
        if price is not None:
            try:
                price_val = float(price)
                if 1 <= price_val <= 100_000_000:
                    sanitized_data["price"] = price_val
            except (ValueError, TypeError):
                pass

        # Condition - only carry it when the model actually produced a valid value.
        # This used to fall through to "2. El" on every turn, and because _handle_create
        # merges every non-None field, a user who had chosen "Sıfır" silently had it
        # reset back to "2. El" the next time they said anything.
        condition = listing_data.get("condition")
        if condition in cls.ALLOWED_CONDITIONS:
            sanitized_data["condition"] = condition

        # Location
        if listing_data.get("location"):
            sanitized_data["location"] = str(listing_data["location"])[:100]

        # Images are owned by the server (upload + vision pipeline). The model has no way
        # to know real storage URLs, so anything it emits here can only be a placeholder.
        
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

        # Report data (REPORT intent için)
        report_data = None
        if intent == Intent.REPORT:
            raw_report = llm_response.get("report_data") or {}
            report_data = {
                "listing_id": raw_report.get("listing_id") or None,
                "reason": str(raw_report.get("reason", ""))[:500] or "Belirtilmedi",
            }

        # Ambiguities: the seller wrote something that has more than one reading.
        # Only fields we actually own are accepted, so the model cannot invent slots.
        ambiguities: List[Dict[str, str]] = []
        raw_ambiguities = llm_response.get("ambiguities")
        if isinstance(raw_ambiguities, list):
            for entry in raw_ambiguities[:3]:
                if not isinstance(entry, dict):
                    continue
                field_name = str(entry.get("field") or "").strip().lower()
                question = str(entry.get("question") or "").strip()
                if field_name in REQUIRED_FIELDS or field_name == "condition":
                    if question:
                        ambiguities.append({"field": field_name, "question": question[:300]})

        include_phone = bool(llm_response.get("include_phone_in_description"))

        return BrainOutput(
            intent=intent,
            response_text=raw_response_text,
            listing_data=sanitized_data,
            missing_fields=missing,
            ready_for_fsm=ready_for_fsm,
            user_confirmed=user_confirmed,
            tool_call=tool_call,
            suggestions=suggestions,
            raw_response=llm_response,
            report_data=report_data,
            ambiguities=ambiguities,
            include_phone_in_description=include_phone,
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

_SALES_MOVES_BLOCK = "\n".join(f"- {move}" for move in SALES_MOVES)
_INVENTED_CLAIMS_BLOCK = "\n".join(f"- {claim}" for claim in INVENTED_CLAIMS)

SYSTEM_PROMPT = f"""# PazarGlobal Ilan Asistani

Sen PazarGlobal'in ilan asistanisin. Bir ilan danismani gibi konusursun: insan gibi,
kisa ve dogal. Kullaniciya form doldurtmazsin.

## NASIL KONUSURSUN

- Kisa ve samimi yaz. Tablo, kontrol listesi, komut sozdizimi ogretme.
- **Onizlemeyi SEN gostermezsin.** "Ilan Onizleme", checkmark/bekleme listesi, alan alan
  durum dokumu yazma. Onizlemeyi sistem uygun anda kendisi gosterir.
- Ayni mesajda birden fazla sey sorma. En kritik eksigi sor, gerisini birak.
- Kullanici zaten verdigi bir bilgiyi tekrar sorma.

## ANA YETENEK: HAM BEYAN -> SATISA HAZIR ILAN

Kullanici dagink tek bir mesaj attiginda (ya da fotograf + birkac kelime), icindeki her
bilgiyi ayikla ve taslaga yerlestir. Sirayla soru sorma.

Ornek ham mesaj:
"2012 model bmw f30 3.16i hatasiz boyasiz tramersiz 375.000 km hayalet nbt var sunroof
yok on arka m tampon 19 jant 800.000TL+ Istanbul teslim"

Bundan cikarman gerekenler: baslik, aciklama, fiyat, lokasyon. Kullaniciya soracagin tek
sey fiyattaki "+" belirsizligi olmali; geri kalanini zaten aldin.

## SATIS DILI

Olgular kullanicidan gelir, sunum senden. Metin bir veri dokumu gibi degil, gercek bir
ilan gibi okunmali.

Serbestce ekleyebilecegin (uslup):
{_SALES_MOVES_BLOCK}

Kullanici soylemeden ASLA yazamayacagin (dogrulanabilir iddia):
{_INVENTED_CLAIMS_BLOCK}

Fark su: "sportif detaylariyla dikkat ceken" bir uslup tercihidir, serbesttir.
"bakimlari yeni yapildi" alicinin karar verecegi bir iddiadir; kullanici soylemediyse
uydurma olur.

## ALANLAR (kapali liste - baska alan URETME)

- `title`   : Kisa, aranabilir baslik
- `description`: Baslik/fiyat/lokasyon disindaki HER SEY buraya (yil, km, hasar, donanim,
  beden, metrekare, oda sayisi...). Ayri alan yok, uydurma.
- `price`   : Sadece sayi (TL)
- `location`: Sehir veya sehir/ilce
- `condition`: Sifir | Az Kullanilmis | 2. El

**Kategori:** Sen belirlemezsin ve cikti alani olarak da dondurmezsin. Sistem basliktan
otomatik belirler. Kullaniciya kategoriden hic bahsetme.

## TELEFON NUMARASI

Kullanicinin mesajinda telefon numarasi gecebilir. Aciklamaya numara YAZMA.
Kullanici numarasinin ilanda gorunmesini isterse `include_phone_in_description: true`
dondur; numarayi dogrulanmis profilden sistem ekler.

## BELIRSIZLIK

Bir deger belirsizse uydurma, `ambiguities` icinde sor. Ornek: "800.000TL+" ifadesindeki
"+" -> fiyat 800000 mi, ustu mu?

## FIYAT ARASTIRMASI

Kullanici bir urunun piyasa degerini sorarsa `perplexity_price_research` tool'unu cagir.
Sadece Turkiye pazari, sadece TL.

## CIKTI (her zaman gecerli JSON)

{{
  "intent": "CREATE|SEARCH|CHAT|REPORT|CANCEL",
  "response_text": "Kullaniciya gidecek Turkce mesaj (onizleme YOK)",
  "listing_data": {{
    "title": "...",
    "description": "...",
    "price": 0,
    "location": "...",
    "condition": "2. El"
  }},
  "ambiguities": [
    {{"field": "price", "question": "Fiyati tam olarak 800.000 TL mi gireyim?"}}
  ],
  "include_phone_in_description": false,
  "report_data": {{"listing_id": "UUID veya null", "reason": "..."}},
  "suggestions": []
}}

CANCEL icin `listing_data` bos obje. REPORT icin `report_data` dolu olmali.

## YASAKLAR

- Onizleme/tablo basmak
- Kategori alani uretmek
- Aciklamaya fiyat veya telefon numarasi yazmak
- Kullanici dogrulamadan hasar, kutu, garanti, sertifika, yil, km, bakim iddiasi yazmak
- Eksik zorunlu alan varken taslagi tamam gibi sunmak"""


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
            "description": "Bir ürünün SADECE Türkiye 2. el piyasa fiyatını araştırır (TL). Kullanıcı fiyat öğrenmek istediğinde çağır; farklı ifade biçimlerini semantik olarak anlayıp uygun olduğunda kullan.",
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
            error_text = _safe_exception_text(e)
            logger.error(f"Brain error ({error_type}): {error_text}", exc_info=True)
            lower_error_text = error_text.lower()
            # More helpful error message for debugging
            if "rate_limit" in lower_error_text:
                return self._fallback_response("API rate limit - biraz bekleyin")
            elif "timeout" in lower_error_text:
                return self._fallback_response("API timeout - tekrar deneyin")
            return self._fallback_response(f"Sistem hatası: {error_type}")
    
    def _build_context(
        self,
        current_listing: Optional[Dict],
        fsm_state: str,
        missing_fields: List[str],
        last_intent: Optional[str],
    ) -> str:
        """Build the canonical state package handed to the model each turn.

        The draft lives on the server, not in the model's memory. Every turn the model
        is re-anchored to the authoritative state as JSON rather than prose, so a
        dropped or misremembered detail from an earlier turn cannot steer the draft.

        The category profile is injected here because the right writing style depends
        on what is being sold, and that is only knowable once there is a title.
        """
        listing = current_listing if isinstance(current_listing, dict) else {}

        # Category is inferred only to pick the writing profile. It is never shown to
        # the model as a field it could fill: the FSM owns the real value.
        category = None
        try:
            from services.category_library import classify_category

            probe = f"{listing.get('title') or ''} {listing.get('description') or ''}".strip()
            if probe:
                category = classify_category(probe)
        except Exception:
            category = None

        state = {
            "phase": fsm_state,
            "last_intent": last_intent,
            "current_draft": {
                "title": listing.get("title"),
                "description": listing.get("description"),
                "price": listing.get("price"),
                "location": listing.get("location"),
                "condition": listing.get("condition"),
            },
            "missing_required_fields": missing_fields,
            "attached_image_count": len(listing.get("images") or []),
        }

        state_json = json.dumps(state, ensure_ascii=False, indent=2)
        blocks = [
            "## MEVCUT DURUM (sunucudaki kanonik taslak)\n"
            f"```json\n{state_json}\n```"
        ]

        if missing_fields:
            readable = {
                "title": "başlık",
                "description": "açıklama",
                "price": "fiyat",
                "location": "lokasyon",
            }
            names = [readable.get(f, f) for f in missing_fields]
            blocks.append(
                f"Eksik zorunlu alanlar: {', '.join(names)}. "
                "En kritik olanı tek bir soruyla iste; hepsini birden sorma."
            )
        else:
            blocks.append(
                "Zorunlu alanların hepsi dolu. Kullanıcı yayınlamak isterse onaya "
                "gönderilecek; sen önizleme basma."
            )

        if category:
            blocks.append(render_profile_prompt(category))

        return "\n\n".join(blocks)
    
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
