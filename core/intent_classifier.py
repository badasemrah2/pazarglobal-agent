"""
Intent Classifier - Rule-based + LLM fallback

5 Intent:
    CREATE  - İlan oluşturma
    SEARCH  - İlan arama
    PUBLISH - İlan yayınlama
    PRICE   - Fiyat araştırması (standalone)
    CHAT    - Genel sohbet
"""
from enum import Enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
import re


class Intent(Enum):
    """Supported intents"""
    CREATE = "create"
    SEARCH = "search"
    PUBLISH = "publish"
    PRICE = "price"  # Standalone price research
    CHAT = "chat"


@dataclass
class ClassificationResult:
    """Intent classification result"""
    intent: Intent
    confidence: float  # 0.0 - 1.0
    extracted_data: Dict[str, Any]  # Any extracted info
    method: str  # "rules" or "llm"


class IntentClassifier:
    """
    Rule-based intent classifier with LLM fallback.
    
    Priority:
    1. Explicit commands (highest confidence)
    2. Keyword patterns
    3. LLM classification (fallback)
    """
    
    # Explicit commands → instant classification
    # NOTE: Keys must be pre-normalized (Turkish chars converted)
    EXPLICIT_COMMANDS = {
        # CREATE
        "satmak istiyorum": Intent.CREATE,
        "ilan vermek istiyorum": Intent.CREATE,
        "satayim": Intent.CREATE,
        "satacagim": Intent.CREATE,
        "satilik": Intent.CREATE,
        "ilan olustur": Intent.CREATE,
        "yeni ilan": Intent.CREATE,
        
        # SEARCH
        "ara": Intent.SEARCH,
        "bul": Intent.SEARCH,
        "goster": Intent.SEARCH,
        "var mi": Intent.SEARCH,
        "varmi": Intent.SEARCH,
        "mevcut mu": Intent.SEARCH,
        "ilanlar": Intent.SEARCH,
        "ilanlara bak": Intent.SEARCH,
        
        # PUBLISH
        "yayinla": Intent.PUBLISH,
        "paylas": Intent.PUBLISH,
        "ilanlarim": Intent.PUBLISH,
        "my listings": Intent.PUBLISH,
        "sil": Intent.PUBLISH,
        "kaldir": Intent.PUBLISH,
        "satildi": Intent.PUBLISH,
        
        # CANCEL (special - resets to CHAT)
        "iptal": Intent.CHAT,
        "vazgec": Intent.CHAT,
        "bosver": Intent.CHAT,
    }
    
    # Keyword patterns for each intent
    CREATE_PATTERNS = [
        r"sat(?:mak|ayım|acağım|ıyorum|ılık)",
        r"ilan\s*(?:ver|oluştur|hazırla)",
        r"elim(?:de|deki)\s+(?:var|bir)",
        r"(?:bunu|şunu)\s+sat",
    ]
    
    SEARCH_PATTERNS = [
        r"(?:var\s*mı|varmı|varmi|mevcut)",
        r"(?:ara|bul|göster|listele)",
        # Removed price patterns - now handled by PRICE intent
        r"ilan(?:lar)?(?:a|ı)?\s*bak",
    ]
    
    PUBLISH_PATTERNS = [
        r"yayınla",
        r"yayinla",
        r"paylaş",
        r"publish",
    ]
    
    CHAT_PATTERNS = [
        r"^(?:merhaba|selam|hey|hi|hello)",
        r"^(?:nasılsın|naber|ne\s*haber)",
        r"(?:teşekkür|sağol|eyvallah)",
        r"(?:yardım|help)",
    ]
    
    # Price research patterns (standalone - not part of create/search)
    # NOTE: Use normalized chars (ç→c, ş→s, ö→o, ü→u, ğ→g, ı→i)
    PRICE_PATTERNS = [
        r"(?:kac|ne\s*kadar)\s*(?:para|tl|lira|eder|ederi)",
        r"fiyat\s*(?:oner|arastir|nedir|ne)",
        r"piyasa\s*(?:degeri|fiyat)",
        r"\bne\s*kadar\s*eder\b",
        r"\bkaca\s*(?:satilir|gider)\b",
        r"\bfiyati\s*(?:nedir|ne)\b",
        r"\bederi\s*(?:nedir|ne)\b",
        r"\bkac\s*para\b",
    ]
    
    def __init__(self):
        # Compile patterns for efficiency
        self._create_re = [re.compile(p, re.IGNORECASE) for p in self.CREATE_PATTERNS]
        self._search_re = [re.compile(p, re.IGNORECASE) for p in self.SEARCH_PATTERNS]
        self._publish_re = [re.compile(p, re.IGNORECASE) for p in self.PUBLISH_PATTERNS]
        self._chat_re = [re.compile(p, re.IGNORECASE) for p in self.CHAT_PATTERNS]
        self._price_re = [re.compile(p, re.IGNORECASE) for p in self.PRICE_PATTERNS]
    
    def classify(
        self,
        message: str,
        current_state: Optional[str] = None,
        has_media: bool = False,
    ) -> ClassificationResult:
        """
        Classify user message intent.
        
        Args:
            message: User message
            current_state: Current state (idle, drafting, preview)
            has_media: Whether message has attached media
        
        Returns:
            ClassificationResult
        """
        msg = self._normalize(message)
        
        # 1. Check explicit commands (highest priority)
        # Sort by length descending to match longest first
        sorted_commands = sorted(self.EXPLICIT_COMMANDS.keys(), key=len, reverse=True)
        for cmd in sorted_commands:
            if cmd in msg:
                intent = self.EXPLICIT_COMMANDS[cmd]
                return ClassificationResult(
                    intent=intent,
                    confidence=1.0,
                    extracted_data={},
                    method="rules",
                )
        
        # 2. Context-aware classification
        # If in DRAFTING state, most messages are slot data (not new intent)
        if current_state == "drafting":
            # Check for explicit state change commands
            if self._matches_any(msg, self._search_re):
                return ClassificationResult(
                    intent=Intent.SEARCH,
                    confidence=0.8,
                    extracted_data={},
                    method="rules",
                )
            # Default: stay in CREATE (user is providing data)
            return ClassificationResult(
                intent=Intent.CREATE,
                confidence=0.9,
                extracted_data={},
                method="rules",
            )
        
        # If in PREVIEW state
        if current_state == "preview":
            if self._matches_any(msg, self._publish_re):
                return ClassificationResult(
                    intent=Intent.PUBLISH,
                    confidence=1.0,
                    extracted_data={},
                    method="rules",
                )
            # Edit commands stay in CREATE
            if self._is_edit_command(msg):
                return ClassificationResult(
                    intent=Intent.CREATE,
                    confidence=0.9,
                    extracted_data={},
                    method="rules",
                )
        
        # 3. Pattern matching (IDLE state or unmatched)
        scores = {
            Intent.CREATE: self._score_patterns(msg, self._create_re),
            Intent.SEARCH: self._score_patterns(msg, self._search_re),
            Intent.PUBLISH: self._score_patterns(msg, self._publish_re),
            Intent.PRICE: self._score_patterns(msg, self._price_re),
            Intent.CHAT: self._score_patterns(msg, self._chat_re),
        }
        
        # PRICE intent boost: price queries should prioritize PRICE over SEARCH
        # "kaç para eder", "fiyatı nedir" etc. should go to price research
        if scores[Intent.PRICE] > 0 and scores[Intent.PRICE] >= scores[Intent.SEARCH]:
            scores[Intent.PRICE] += 0.3  # Boost price intent
        
        # Media without explicit intent → likely CREATE
        if has_media and scores[Intent.CREATE] < 0.5:
            scores[Intent.CREATE] = 0.6
        
        # Find best match
        best_intent = max(scores, key=scores.get)
        best_score = scores[best_intent]
        
        # If no clear winner, default to CHAT
        if best_score < 0.3:
            return ClassificationResult(
                intent=Intent.CHAT,
                confidence=0.5,
                extracted_data={},
                method="rules",
            )
        
        return ClassificationResult(
            intent=best_intent,
            confidence=best_score,
            extracted_data={},
            method="rules",
        )
    
    def _normalize(self, text: str) -> str:
        """Normalize text for matching"""
        text = text.lower().strip()
        # Turkish character normalization
        text = text.replace("ı", "i").replace("ğ", "g").replace("ü", "u")
        text = text.replace("ş", "s").replace("ö", "o").replace("ç", "c")
        return text
    
    def _matches_any(self, text: str, patterns: List[re.Pattern]) -> bool:
        """Check if text matches any pattern"""
        return any(p.search(text) for p in patterns)
    
    def _score_patterns(self, text: str, patterns: List[re.Pattern]) -> float:
        """Score text against patterns (0.0 - 1.0)"""
        matches = sum(1 for p in patterns if p.search(text))
        if matches == 0:
            return 0.0
        return min(1.0, matches * 0.4)  # Each match adds 0.4, max 1.0
    
    def _is_edit_command(self, text: str) -> bool:
        """Check if message is an edit command"""
        edit_patterns = [
            r"(?:başlık|baslik)[\s:]+",
            r"(?:açıklama|aciklama)[\s:]+",
            r"(?:fiyat|price)[\s:]+",
            r"(?:lokasyon|konum|location)[\s:]+",
            r"(?:değiştir|degistir|düzelt|duzelt)",
        ]
        return any(re.search(p, text, re.IGNORECASE) for p in edit_patterns)
