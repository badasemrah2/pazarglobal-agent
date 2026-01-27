"""Keyword generation for listings.

This module intentionally avoids importing from `services.__init__` to prevent import cycles.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import json
import re
from loguru import logger

from .openai_client import openai_client


def _normalize_keyword(token: str) -> Optional[str]:
    token = (token or "").strip().lower()
    if not token:
        return None

    # Basic cleanup
    token = token.replace(".", " ")
    token = re.sub(r"\s+", " ", token)
    token = token.strip("-•,.;:()[]{}\"'“”‘’`")
    # Keep only letters/numbers/space/+ (for 1+1 etc.), remove emojis/punctuation
    token = re.sub(r"[^0-9a-zçğıöşü\+ ]+", "", token, flags=re.IGNORECASE)
    token = " ".join(token.split())

    # Avoid useless tokens
    if token in {"ürün", "esya", "eşya", "satılık", "satilik", "ikinci el", "2. el"}:
        return None
    if len(token) < 2:
        return None
    return token


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for it in items:
        k = it.lower().strip()
        if not k or k in seen:
            continue
        out.append(it)
        seen.add(k)
    return out


def _augment_keywords(
    *,
    keywords: List[str],
    title: str,
    category: str,
    vision_product: Optional[Dict[str, Any]] = None,
) -> List[str]:
    extras: List[str] = []
    title_norm = (title or "").lower()
    category_norm = (category or "").lower()
    vision_norm = str((vision_product or {}).get("product") or "").lower()

    if "elektronik" in category_norm:
        extras.extend(["elektronik"])

    if any(tok in title_norm for tok in ["iphone", "galaxy", "xiaomi", "telefon", "cep telefonu"]) or "telefon" in vision_norm:
        extras.extend(["telefon", "cep telefonu", "akıllı telefon"])

    normalized_extras: List[str] = []
    for ex in extras:
        kw = _normalize_keyword(ex)
        if kw:
            normalized_extras.append(kw)

    combined = _dedupe_preserve_order(keywords + normalized_extras)
    return combined


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    raw = text.strip()

    # Strip code fences if present
    if "```" in raw:
        parts = raw.split("```")
        # Prefer the first fenced block content
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if "{" in candidate and "}" in candidate:
                raw = candidate
                break

    # Extract the first JSON object substring
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = raw[start:end + 1]
    try:
        parsed = json.loads(snippet)
    except Exception:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _fallback_tokens(title: str, category: str, description: str) -> List[str]:
    def tokenize(text: str) -> List[str]:
        t = (text or "").lower()
        raw = re.findall(r"[0-9a-zçğıöşü\+]{2,}", t, flags=re.IGNORECASE)
        return [r.strip("+") if r.endswith("+") else r for r in raw if r]

    stop = {
        "satılık", "satilik", "kiralık", "kiralik", "urun", "ürün", "esya", "eşya",
        "temiz", "az", "kullanılmış", "kullanilmis", "iyi", "durumda", "fiyat", "tl",
        "acil", "hemen", "pazarlik", "pazarlık",
    }

    words: List[str] = []
    for src in [title, category, description]:
        for w in tokenize(src):
            if not w or w in stop or len(w) < 2:
                continue
            words.append(w)
    return _dedupe_preserve_order(words)


async def generate_listing_keywords(
    *,
    title: str,
    category: str,
    description: str = "",
    condition: str = "",
    vision_product: Optional[Dict[str, Any]] = None,
    max_keywords: int = 12,
) -> Dict[str, Any]:
    """Generate Turkish keywords for a listing.

    Returns:
      {"keywords": [..], "keywords_text": ".."}

    Notes:
    - Best-effort and safe to fail (caller should fall back to empty metadata).
    - Output is normalized to lowercase and deduplicated.
    """

    title = (title or "").strip()
    category = (category or "").strip()
    description = (description or "").strip()
    condition = (condition or "").strip()

    # Minimal guard
    if not title:
        return {"keywords": [], "keywords_text": ""}

    vision = vision_product if isinstance(vision_product, dict) else {}

    system = (
        "Sen bir ilan etiket/anahtar kelime üretim asistanısın. "
        "Çıktın SADECE JSON olmalı ve şu şemaya uymalı: "
        "{\"keywords\": [string, ...]}. "
        "Kurallar: Türkçe yaz; 6-12 arası anahtar kelime üret; hepsi küçük harf olsun; "
        "noktalama/emoji yok; tekrar yok. "
        "İstisna: emlak ilanlarında oda formatı gibi ifadeler (1+1, 2+1, 3+1 vb.) kullanılabilir. "
        "Sadece çok genel olmayan ama aramayı kolaylaştıran terimler üret: "
        "ürün türü, kategori, marka, model, varyant, eş anlamlı/üst sınıf terimler (ör: araba/otomobil/araç), "
        "ve ilgili kullanım alanı. "
        "Yasak: kişi bilgisi/telefon/konum, fiyat, seri numarası. "
        "API JSON Schema zorluyor; şema dışına çıkan çıktı reddedilir." 
    )

    payload = {
        "title": title,
        "category": category,
        "description": description,
        "condition": condition,
        "vision": {
            "product": vision.get("product"),
            "category": vision.get("category"),
            "features": vision.get("features"),
        },
        "max_keywords": int(max_keywords),
    }

    user = (
        "Aşağıdaki ilan bilgisinden arama için anahtar kelimeler üret. "
        "Örnek: 'citroen c3' için 'araba', 'otomobil', 'araç' gibi üst terimler ekle.\n\n"
        "Eğer kategori emlak ise uygun oldukça şu tür terimleri ekle: villa, dubleks, triplex, havuzlu, 1+1/2+1 gibi oda formatları.\n\n"
        f"ILAN_JSON: {json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        last_error: Optional[str] = None
        for attempt in range(2):
            retry_note = "\n\nSADECE JSON döndür. Kod bloğu, açıklama veya ekstra metin YAZMA." if attempt == 1 else ""
            resp = await openai_client.create_chat_completion(
                messages=[
                    {"role": "system", "content": system + retry_note},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
                max_tokens=250,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "keywords_schema",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "keywords": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 6,
                                    "maxItems": 12,
                                }
                            },
                            "required": ["keywords"],
                            "additionalProperties": False,
                        },
                    },
                },
            )
            text = (resp.choices[0].message.content or "").strip()
            data = _extract_json_object(text)
            if not data:
                last_error = "invalid_json"
                continue

            raw = data.get("keywords") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                last_error = "missing_keywords"
                continue

            normed: List[str] = []
            for t in raw:
                kw = _normalize_keyword(str(t))
                if kw:
                    normed.append(kw)
            normed = _dedupe_preserve_order(normed)

            # Fill to minimum length if needed
            if len(normed) < 6:
                extras = _fallback_tokens(title, category, description)
                for ex in extras:
                    if len(normed) >= 6:
                        break
                    kw = _normalize_keyword(ex)
                    if kw and kw not in normed:
                        normed.append(kw)

            # Deterministic augmentation for better search recall
            normed = _augment_keywords(
                keywords=normed,
                title=title,
                category=category,
                vision_product=vision,
            )

            # Cap size
            normed = normed[: max(1, int(max_keywords))]

            return {
                "keywords": normed,
                "keywords_text": " ".join(normed),
            }

        logger.warning(f"Keyword generation failed: {last_error or 'unknown'}")
        return {"keywords": [], "keywords_text": ""}
    except Exception as e:
        logger.warning(f"Keyword generation failed: {e}")
        return {"keywords": [], "keywords_text": ""}
