"""
Slot Filler - Deterministic field extraction from user messages

Slots:
    title       - Ürün adı (user + vision)
    description - Açıklama (user + LLM)
    price       - Fiyat (regex extraction)
    category    - Kategori (vision + rules)
    condition   - Durum (user explicit)
    location    - Lokasyon (NER + rules)
    images      - Görseller (media URLs)
"""
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
import re


@dataclass
class SlotValue:
    """Extracted slot value"""
    value: Any
    source: str  # "user", "vision", "rules", "llm"
    confidence: float


@dataclass
class ExtractionResult:
    """Slot extraction result"""
    slots: Dict[str, SlotValue]
    raw_text: str  # Remaining text after extraction


class SlotFiller:
    """
    Deterministic slot extraction.
    
    Priority:
    1. Explicit field markers (başlık: xxx, fiyat: xxx)
    2. Pattern matching (price, location)
    3. Leftover text → title/description candidate
    """
    
    # Turkish cities for location detection
    CITIES = {
        "istanbul", "ankara", "izmir", "bursa", "antalya", "adana",
        "konya", "gaziantep", "mersin", "diyarbakır", "kayseri",
        "eskişehir", "samsun", "denizli", "şanlıurfa", "malatya",
        "trabzon", "erzurum", "van", "batman", "elazığ", "manisa",
        "balıkesir", "kocaeli", "sakarya", "muğla", "hatay", "kahramanmaraş",
        "mardin", "aydın", "tekirdağ", "edirne", "kırklareli", "çanakkale",
    }
    
    # Condition keywords
    CONDITION_MAP = {
        # Sıfır (New)
        "sıfır": "Sıfır",
        "sifir": "Sıfır",
        "yeni": "Sıfır",
        "kutusunda": "Sıfır",
        "açılmamış": "Sıfır",
        "acilmamis": "Sıfır",
        
        # 2. El (Used)
        "2. el": "2. El",
        "2.el": "2. El",
        "ikinci el": "2. El",
        "kullanılmış": "2. El",
        "kullanilmis": "2. El",
        "second hand": "2. El",
        
        # Az Kullanılmış (Like New)
        "az kullanılmış": "Az Kullanılmış",
        "az kullanilmis": "Az Kullanılmış",
        "temiz": "Az Kullanılmış",
        "çok temiz": "Az Kullanılmış",
        "sorunsuz": "Az Kullanılmış",
    }
    
    # Category keywords
    CATEGORY_MAP = {
        # Elektronik
        "telefon": "Elektronik",
        "iphone": "Elektronik",
        "samsung": "Elektronik",
        "laptop": "Elektronik",
        "bilgisayar": "Elektronik",
        "tablet": "Elektronik",
        "kulaklık": "Elektronik",
        "playstation": "Elektronik",
        "xbox": "Elektronik",
        "televizyon": "Elektronik",
        "kamera": "Elektronik",
        
        # Otomotiv
        "araba": "Otomotiv",
        "otomobil": "Otomotiv",
        "araç": "Otomotiv",
        "motor": "Otomotiv",
        "motosiklet": "Otomotiv",
        
        # Emlak
        "ev": "Emlak",
        "daire": "Emlak",
        "arsa": "Emlak",
        "kiralık": "Emlak",
        "satılık": "Emlak",
        
        # Mobilya
        "koltuk": "Mobilya & Dekorasyon",
        "masa": "Mobilya & Dekorasyon",
        "sandalye": "Mobilya & Dekorasyon",
        "dolap": "Mobilya & Dekorasyon",
        "yatak": "Mobilya & Dekorasyon",
        
        # Giyim
        "ayakkabı": "Giyim & Aksesuar",
        "çanta": "Giyim & Aksesuar",
        "mont": "Giyim & Aksesuar",
        "ceket": "Giyim & Aksesuar",
        "elbise": "Giyim & Aksesuar",
    }
    
    def extract(
        self,
        message: str,
        media_urls: Optional[List[str]] = None,
        vision_data: Optional[Dict[str, Any]] = None,
    ) -> ExtractionResult:
        """
        Extract slot values from message.
        
        Args:
            message: User message
            media_urls: Attached media URLs
            vision_data: Vision analysis result
        
        Returns:
            ExtractionResult with extracted slots
        """
        slots: Dict[str, SlotValue] = {}
        remaining = message
        
        # 1. Extract explicit field markers
        remaining, explicit_slots = self._extract_explicit_fields(remaining)
        slots.update(explicit_slots)
        
        # 2. Extract price (pattern matching)
        if "price" not in slots:
            price, remaining = self._extract_price(remaining)
            if price is not None:
                slots["price"] = SlotValue(price, "rules", 0.9)
        
        # 3. Extract location (city names)
        if "location" not in slots:
            location, remaining = self._extract_location(remaining)
            if location:
                slots["location"] = SlotValue(location, "rules", 0.8)
        
        # 4. Extract condition
        if "condition" not in slots:
            condition = self._extract_condition(message)  # Use full message
            if condition:
                slots["condition"] = SlotValue(condition, "rules", 0.9)
        
        # 5. Extract category (keywords or vision)
        if "category" not in slots:
            category = self._extract_category(message, vision_data)
            if category:
                slots["category"] = SlotValue(category, "rules" if not vision_data else "vision", 0.7)
        
        # 6. Media URLs → images
        if media_urls:
            slots["images"] = SlotValue(media_urls, "user", 1.0)
        
        # 7. Vision data can provide title hints
        if vision_data and "title" not in slots:
            product = vision_data.get("product")
            if product:
                slots["title_hint"] = SlotValue(product, "vision", 0.6)
        
        return ExtractionResult(slots=slots, raw_text=remaining.strip())
    
    def _extract_explicit_fields(self, text: str) -> Tuple[str, Dict[str, SlotValue]]:
        """Extract explicitly marked fields (başlık: xxx)"""
        slots: Dict[str, SlotValue] = {}
        remaining = text
        
        patterns = [
            (r"(?:başlık|baslik|title)\s*[:\-=]\s*(.+?)(?:\n|$|,|\.)", "title"),
            (r"(?:açıklama|aciklama|desc)\s*[:\-=]\s*(.+?)(?:\n|$)", "description"),
            (r"(?:fiyat|price)\s*[:\-=]\s*(\d[\d\.\,\s]*)", "price"),
            (r"(?:lokasyon|konum|location|şehir|sehir)\s*[:\-=]\s*(\w+)", "location"),
            (r"(?:durum|condition)\s*[:\-=]\s*(.+?)(?:\n|$|,)", "condition"),
            (r"(?:kategori|category)\s*[:\-=]\s*(.+?)(?:\n|$|,)", "category"),
        ]
        
        for pattern, slot_name in patterns:
            match = re.search(pattern, remaining, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                
                # Special handling for price
                if slot_name == "price":
                    value = self._parse_price_value(value)
                
                # Special handling for condition
                if slot_name == "condition":
                    value = self._normalize_condition(value)
                
                if value:
                    slots[slot_name] = SlotValue(value, "user", 1.0)
                    remaining = remaining[:match.start()] + remaining[match.end():]
        
        return remaining, slots
    
    def _extract_price(self, text: str) -> Tuple[Optional[float], str]:
        """Extract price from text"""
        # Patterns for price detection
        patterns = [
            r"(\d{1,3}(?:[\.\,\s]?\d{3})*)\s*(?:tl|₺|lira|bin)",
            r"(\d+)\s*(?:k|K)\b",  # 50k = 50000
            r"(?:fiyat|ücret|bedel)[^\d]*(\d{1,3}(?:[\.\,\s]?\d{3})*)",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = self._parse_price_value(match.group(1))
                if value:
                    remaining = text[:match.start()] + text[match.end():]
                    return value, remaining
        
        return None, text
    
    def _parse_price_value(self, value: str) -> Optional[float]:
        """Parse price string to float"""
        try:
            # Remove spaces, dots (thousand separator), convert comma to dot
            clean = re.sub(r"[^\d,]", "", str(value))
            clean = clean.replace(",", ".")
            
            # Handle "k" suffix (50k = 50000)
            if "k" in str(value).lower():
                clean = re.sub(r"[^\d]", "", str(value))
                return float(clean) * 1000
            
            return float(clean)
        except (ValueError, TypeError):
            return None
    
    def _extract_location(self, text: str) -> Tuple[Optional[str], str]:
        """Extract location (city) from text"""
        text_lower = text.lower()
        
        for city in self.CITIES:
            # Use word boundary to prevent partial matches (e.g., "samsun" in "Samsung")
            pattern = re.compile(r'\b' + re.escape(city) + r'\b', re.IGNORECASE)
            match = pattern.search(text_lower)
            if match:
                # Capitalize properly
                location = city.title()
                # Handle Turkish İ
                location = location.replace("Istanbul", "İstanbul")
                location = location.replace("Izmir", "İzmir")
                
                # Remove from text using the same pattern
                remaining = pattern.sub("", text, count=1)
                
                return location, remaining
        
        return None, text
    
    def _extract_condition(self, text: str) -> Optional[str]:
        """Extract condition from text"""
        text_lower = text.lower()
        
        # Check longest patterns first
        sorted_conditions = sorted(self.CONDITION_MAP.keys(), key=len, reverse=True)
        
        for keyword in sorted_conditions:
            if keyword in text_lower:
                return self.CONDITION_MAP[keyword]
        
        return None
    
    def _normalize_condition(self, value: str) -> str:
        """Normalize condition value"""
        value_lower = value.lower().strip()
        return self.CONDITION_MAP.get(value_lower, value)
    
    def _extract_category(
        self,
        text: str,
        vision_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Extract category from text or vision"""
        # 1. Try vision category first
        if vision_data:
            vision_category = vision_data.get("category_tag") or vision_data.get("category")
            if vision_category:
                return self._normalize_category(str(vision_category))
        
        # 2. Keyword matching
        text_lower = text.lower()
        for keyword, category in self.CATEGORY_MAP.items():
            if keyword in text_lower:
                return category
        
        return None
    
    def _normalize_category(self, category: str) -> str:
        """Normalize category to standard values"""
        ALLOWED = [
            "Elektronik", "Otomotiv", "Emlak", "Mobilya & Dekorasyon",
            "Giyim & Aksesuar", "Gıda & İçecek", "Kozmetik & Kişisel Bakım",
            "Kitap, Dergi & Müzik", "Spor & Outdoor", "Anne, Bebek & Oyuncak",
            "Hayvan & Pet Shop", "Yapı Market & Bahçe", "Hobi & Oyun",
            "Sanat & Zanaat", "İş & Sanayi", "Eğitim & Kurs",
            "Etkinlik & Bilet", "Hizmetler", "Diğer",
        ]
        
        if category in ALLOWED:
            return category
        
        # Try matching
        cat_lower = category.lower()
        for allowed in ALLOWED:
            if allowed.lower() in cat_lower or cat_lower in allowed.lower():
                return allowed
        
        return "Diğer"


# Singleton instance
slot_filler = SlotFiller()
