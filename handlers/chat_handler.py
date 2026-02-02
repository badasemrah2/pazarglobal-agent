"""
Chat Handler - Small talk, help, and general conversation

Handles:
- Greetings
- Help requests
- FAQ
- Unknown messages
"""
from typing import Optional

from core.response_builder import ResponseBuilder, Response, Button, create_builder

from services.logger import get_logger

logger = get_logger(__name__)


class ChatHandler:
    """
    General chat handler for non-listing interactions.
    """
    
    # Turkish small talk patterns and responses
    RESPONSES = {
        # Greetings
        "greeting": {
            "patterns": ["merhaba", "selam", "hey", "hi", "hello", "günaydın", "iyi akşamlar"],
            "response": "👋 Merhaba! Ben PazarGlobal asistanı. Size nasıl yardımcı olabilirim?\n\n"
                       "🛒 **İlan Vermek İçin:**\n"
                       "• Ürün fotoğrafı gönderin (önerilen)\n"
                       "• veya sadece ürünü anlatın: 'iPhone 14 satıyorum'\n\n"
                       "🔍 **Ürün Aramak İçin:**\n"
                       "• Aradığınızı yazın: 'koltuk var mı?'\n\n"
                       "💰 **Fiyat Araştırması:**\n"
                       "• 'MacBook Pro kaç para eder?' gibi sorun\n\n"
                       "📋 **İlanlarınız:** 'ilanlarım' yazın",
            "buttons": [
                {"text": "📸 İlan Ver", "payload": "ilan vermek istiyorum"},
                {"text": "🔍 Ürün Ara", "payload": "aramak istiyorum"},
                {"text": "📋 İlanlarım", "payload": "ilanlarım"}
            ],
        },
        
        # Help
        "help": {
            "patterns": ["yardım", "yardim", "help", "nasıl", "nasil", "ne yapabilirim"],
            "response": "📌 **PazarGlobal Kullanım Kılavuzu**\n\n"
                       "**İlan Vermek:**\n"
                       "1. Ürün fotoğrafı gönderin\n"
                       "2. Sorulan bilgileri cevaplayın\n"
                       "3. Önizlemeyi onaylayın\n\n"
                       "**Ürün Aramak:**\n"
                       "Ne aradığınızı yazın. Örn: 'iPhone 13 var mı?'\n\n"
                       "**İlanlarınız:**\n"
                       "'ilanlarım' yazarak aktif ilanlarınızı görün",
        },
        
        # Thanks
        "thanks": {
            "patterns": ["teşekkür", "tesekkur", "sağol", "sagol", "thanks", "thank you", "eyvallah"],
            "response": "😊 Rica ederim! Başka yardımcı olabileceğim bir şey var mı?",
        },
        
        # Goodbye
        "goodbye": {
            "patterns": ["görüşürüz", "gorusuruz", "bye", "hoşçakal", "hoscakal", "güle güle"],
            "response": "👋 Görüşmek üzere! İyi günler dilerim.",
        },
        
        # Status/About
        "about": {
            "patterns": ["kimsin", "nesin", "sen kimsin", "hakkında", "about"],
            "response": "🤖 Ben PazarGlobal'ın yapay zeka asistanıyım.\n\n"
                       "Türkiye'nin konuşarak alışveriş platformunda size yardımcı olmak için buradayım!",
        },
        
        # Price inquiry
        "price_inquiry": {
            "patterns": ["ne kadar", "kaç para", "fiyatı ne", "fiyati ne"],
            "response": "💰 Hangi ürünün fiyatını öğrenmek istiyorsunuz?\n\n"
                       "Ürün adı veya fotoğrafı gönderin, piyasa araştırması yapayım.",
        },
    }
    
    def __init__(self):
        self.response_builder: Optional[ResponseBuilder] = None
    
    async def initialize(self, channel: str = "webchat"):
        """Lazy initialization"""
        self.response_builder = create_builder(channel)
    
    async def handle(
        self,
        user_id: str,
        message: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Handle general chat message.
        
        Args:
            user_id: User identifier
            message: User message
            channel: Communication channel
        
        Returns:
            Response to user
        """
        await self.initialize(channel)
        
        message_lower = message.lower().strip()
        
        logger.info(f"Chat handler: user={user_id}, message={message_lower}")
        
        # Check for pattern matches
        for intent_type, data in self.RESPONSES.items():
            for pattern in data["patterns"]:
                if pattern in message_lower:
                    custom_buttons = data.get("buttons")
                    return self._build_response(intent_type, data["response"], custom_buttons)
        
        # Check for specific commands
        if message_lower in ["ilanlarım", "ilanlarim", "my listings", "ilanlar"]:
            # Delegate to publish handler
            from handlers.publish_handler import publish_handler
            return await publish_handler.get_my_listings(user_id, channel)
        
        # Unknown - default response
        return self._build_unknown_response()
    
    def _build_response(self, intent_type: str, text: str, custom_buttons: list = None) -> Response:
        """Build response with appropriate buttons"""
        buttons = []
        
        # Use custom buttons if provided
        if custom_buttons:
            buttons = [
                Button(b["text"], b["payload"]) for b in custom_buttons
            ]
        elif intent_type in ["greeting", "help"]:
            buttons = [
                Button("📸 İlan Ver", "ilan vermek istiyorum"),
                Button("🔍 Ara", "aramak istiyorum"),
                Button("📋 İlanlarım", "ilanlarım"),
            ]
        
        return self.response_builder.build_custom(text, buttons=buttons)
    
    def _build_unknown_response(self) -> Response:
        """Build response for unknown messages"""
        buttons = [
            Button("📸 İlan Ver", "create_listing"),
            Button("🔍 Ara", "search"),
            Button("❓ Yardım", "help"),
        ]
        
        return self.response_builder.build_custom(
            "🤔 Tam olarak anlamadım.\n\n"
            "**İlan vermek** için ürün fotoğrafı gönderin\n"
            "**Ürün aramak** için ne aradığınızı yazın",
            buttons=buttons,
        )
    
    async def handle_faq(
        self,
        user_id: str,
        question: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Handle FAQ questions.
        
        Args:
            user_id: User identifier
            question: FAQ question
            channel: Communication channel
        
        Returns:
            FAQ answer
        """
        await self.initialize(channel)
        
        question_lower = question.lower()
        
        # FAQ database
        faqs = {
            "ücret": "✅ PazarGlobal'da ilan vermek şu an **ücretsiz**!",
            "ucret": "✅ PazarGlobal'da ilan vermek şu an **ücretsiz**!",
            "güvenli": "🔒 PazarGlobal'da tüm işlemler güvenli altyapımız üzerinden gerçekleşir.",
            "guvenli": "🔒 PazarGlobal'da tüm işlemler güvenli altyapımız üzerinden gerçekleşir.",
            "ne kadar sürer": "⚡ İlanınız onaylandıktan sonra hemen yayınlanır!",
            "kargo": "📦 Kargo seçenekleri satıcı ile alıcı arasında belirlenir.",
            "iade": "🔄 İade politikaları satıcıya bağlıdır. Satın almadan önce satıcıyla iletişime geçin.",
        }
        
        for keyword, answer in faqs.items():
            if keyword in question_lower:
                return self.response_builder.build_custom(answer)
        
        return self.response_builder.build_custom(
            "📚 Bu sorunun cevabını bulamadım.\n\n"
            "Daha fazla yardım için: destek@pazarglobal.com"
        )


# Singleton
chat_handler = ChatHandler()
