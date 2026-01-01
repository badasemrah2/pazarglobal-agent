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
    token = re.sub(r"\s+", " ", token)
    token = token.strip("-•,.;:()[]{}\"'“”‘’")

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
        "Sadece çok genel olmayan ama aramayı kolaylaştıran terimler üret: "
        "ürün türü, kategori, marka, model, varyant, eş anlamlı/üst sınıf terimler (ör: araba/otomobil/araç), "
        "ve ilgili kullanım alanı. "
        "Yasak: kişi bilgisi/telefon/konum, fiyat, seri numarası." 
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
        f"ILAN_JSON: {json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        resp = await openai_client.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=250,
        )
        text = (resp.choices[0].message.content or "").strip()
        data = json.loads(text) if text else {}
        raw = data.get("keywords") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raw = []

        normed: List[str] = []
        for t in raw:
            kw = _normalize_keyword(str(t))
            if kw:
                normed.append(kw)
        normed = _dedupe_preserve_order(normed)

        # Cap size
        normed = normed[: max(1, int(max_keywords))]

        return {
            "keywords": normed,
            "keywords_text": " ".join(normed),
        }
    except Exception as e:
        logger.warning(f"Keyword generation failed: {e}")
        return {"keywords": [], "keywords_text": ""}
