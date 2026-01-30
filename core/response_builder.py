"""
Response Builder - Channel-aware response formatting

Channels:
    webchat  - Rich HTML, markdown, buttons
    whatsapp - Plain text, limited buttons (max 3)
    api      - Raw JSON
"""
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum


class Channel(str, Enum):
    WEBCHAT = "webchat"
    WHATSAPP = "whatsapp"
    API = "api"


@dataclass
class Button:
    """Quick reply button"""
    text: str
    payload: str  # Sent back when clicked


@dataclass
class Response:
    """Formatted response"""
    text: str
    buttons: List[Button]
    metadata: Dict[str, Any]
    channel: Channel


class ResponseBuilder:
    """
    Channel-aware response formatting.
    
    Builds responses with:
    - Rich text (markdown/HTML for web)
    - Quick reply buttons
    - Metadata for frontend
    """
    
    # Default messages (Turkish)
    MESSAGES = {
        # Listing Flow
        "listing_start": "📸 Harika! İlan oluşturmak için önce ürününüzün fotoğrafını gönderin.",
        "listing_image_received": "✅ Görsel alındı! Ürününüzü analiz ediyorum...",
        "listing_need_title": "📝 Ürünün başlığı/adı ne olsun?",
        "listing_need_price": "💰 Ne kadara satmak istiyorsunuz?",
        "listing_need_category": "🏷️ Hangi kategoride olsun?",
        "listing_preview": "📋 İlanınızın önizlemesi:\n\n{preview}",
        "listing_confirm": "İlan yayınlamak için 'yayınla' yazın veya düzenlemek için alan adı yazın (örn: fiyat 500)",
        "listing_published": "🎉 İlanınız yayınlandı!\n\n{url}",
        "listing_cancelled": "❌ İlan iptal edildi.",
        
        # Search Flow
        "search_results": "🔍 {count} sonuç bulundu:\n\n{results}",
        "search_no_results": "😕 Aramanıza uygun ilan bulunamadı. Farklı kelimelerle deneyin.",
        
        # Errors
        "error_generic": "⚠️ Bir hata oluştu. Lütfen tekrar deneyin.",
        "error_rate_limit": "⏳ Çok fazla istek gönderdiniz. Biraz bekleyin.",
        "error_vision_blocked": "🚫 Bu görsel politikalarımıza uygun değil.",
        
        # Small Talk
        "greeting": "👋 Merhaba! Ben PazarGlobal asistanı. İlan vermek, aramak veya sormak istediğiniz bir şey var mı?",
        "help": "📌 Yapabilecekleriniz:\n• İlan vermek için fotoğraf gönderin\n• Ürün aramak için ne aradığınızı yazın\n• İlanlarınızı görmek için 'ilanlarım' yazın",
        "unknown": "🤔 Anlamadım. İlan vermek için fotoğraf gönderin veya ürün arayın.",
    }
    
    def __init__(self, channel: Channel = Channel.WEBCHAT):
        self.channel = channel
    
    def build(
        self,
        message_key: str,
        format_args: Optional[Dict[str, Any]] = None,
        buttons: Optional[List[Button]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Response:
        """
        Build response from message key.
        
        Args:
            message_key: Key in MESSAGES dict
            format_args: Values for format placeholders
            buttons: Quick reply buttons
            metadata: Extra data for frontend
        
        Returns:
            Response object
        """
        text = self.MESSAGES.get(message_key, message_key)
        
        if format_args:
            text = text.format(**format_args)
        
        # Adapt for channel
        text = self._adapt_text(text)
        buttons = self._adapt_buttons(buttons or [])
        
        return Response(
            text=text,
            buttons=buttons,
            metadata=metadata or {},
            channel=self.channel,
        )
    
    def build_preview(self, draft: Dict[str, Any]) -> Response:
        """Build listing preview response"""
        preview = self._format_preview(draft)
        
        buttons = [
            Button("✅ Yayınla", "publish"),
            Button("✏️ Düzenle", "edit"),
            Button("❌ İptal", "cancel"),
        ]
        
        return self.build(
            "listing_preview",
            format_args={"preview": preview},
            buttons=buttons,
            metadata={"draft_id": draft.get("id"), "state": "preview"},
        )
    
    def build_search_results(self, listings: List[Dict[str, Any]]) -> Response:
        """Build search results response"""
        if not listings:
            return self.build("search_no_results")
        
        results = self._format_search_results(listings)
        
        return self.build(
            "search_results",
            format_args={"count": len(listings), "results": results},
            metadata={"listing_ids": [l.get("id") for l in listings]},
        )
    
    def build_error(self, error_type: str = "generic") -> Response:
        """Build error response"""
        return self.build(f"error_{error_type}")
    
    def build_custom(
        self,
        text: str,
        buttons: Optional[List[Button]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Response:
        """Build custom response"""
        text = self._adapt_text(text)
        buttons = self._adapt_buttons(buttons or [])
        
        return Response(
            text=text,
            buttons=buttons,
            metadata=metadata or {},
            channel=self.channel,
        )
    
    def _adapt_text(self, text: str) -> str:
        """Adapt text for channel"""
        if self.channel == Channel.WHATSAPP:
            # Remove HTML tags
            import re
            text = re.sub(r"<[^>]+>", "", text)
            # Convert markdown bold to WhatsApp bold
            text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        
        return text
    
    def _adapt_buttons(self, buttons: List[Button]) -> List[Button]:
        """Adapt buttons for channel"""
        if self.channel == Channel.WHATSAPP:
            # WhatsApp only supports 3 buttons
            return buttons[:3]
        
        return buttons
    
    def _format_preview(self, draft: Dict[str, Any]) -> str:
        """Format draft as preview text"""
        lines = []
        
        if draft.get("title"):
            lines.append(f"📌 **{draft['title']}**")
        
        if draft.get("description"):
            desc = draft["description"]
            if len(desc) > 200:
                desc = desc[:200] + "..."
            lines.append(f"📝 {desc}")
        
        if draft.get("price"):
            lines.append(f"💰 {draft['price']:,.0f} TL")
        
        if draft.get("category"):
            lines.append(f"🏷️ {draft['category']}")
        
        if draft.get("condition"):
            lines.append(f"📦 {draft['condition']}")
        
        if draft.get("location"):
            lines.append(f"📍 {draft['location']}")
        
        images = draft.get("images") or []
        if images:
            lines.append(f"🖼️ {len(images)} görsel")
        
        # Missing fields indicator
        missing = []
        if not draft.get("title"):
            missing.append("başlık")
        if not draft.get("price"):
            missing.append("fiyat")
        if not draft.get("images"):
            missing.append("görsel")
        
        if missing:
            lines.append(f"\n⚠️ Eksik: {', '.join(missing)}")
        
        return "\n".join(lines)
    
    def _format_search_results(self, listings: List[Dict[str, Any]]) -> str:
        """Format listings as search results"""
        lines = []
        
        for i, listing in enumerate(listings[:5], 1):  # Max 5 results
            title = listing.get("title", "İsimsiz")
            price = listing.get("price")
            location = listing.get("location", "")
            
            price_str = f"{price:,.0f} TL" if price else "Fiyat yok"
            loc_str = f" • {location}" if location else ""
            
            lines.append(f"{i}. **{title}** - {price_str}{loc_str}")
        
        if len(listings) > 5:
            lines.append(f"\n... ve {len(listings) - 5} sonuç daha")
        
        return "\n".join(lines)
    
    def to_dict(self, response: Response) -> Dict[str, Any]:
        """Convert response to API format"""
        return {
            "text": response.text,
            "buttons": [
                {"text": b.text, "payload": b.payload}
                for b in response.buttons
            ],
            "metadata": response.metadata,
            "channel": response.channel.value,
        }


# Factory function
def create_builder(channel: str = "webchat") -> ResponseBuilder:
    """Create response builder for channel"""
    try:
        ch = Channel(channel.lower())
    except ValueError:
        ch = Channel.WEBCHAT
    
    return ResponseBuilder(ch)
