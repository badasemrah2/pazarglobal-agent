"""
Search Agents - Handle different types of listing searches
"""
from .base_agent import BaseAgent
from config.prompts import (
    CATEGORY_SEARCH_AGENT_PROMPT,
    PRICE_SEARCH_AGENT_PROMPT,
    CONTENT_SEARCH_AGENT_PROMPT,
    SEARCH_COMPOSER_AGENT_PROMPT
)
from tools import search_listings_tool, market_price_tool
from typing import Dict, Any
import asyncio
import json
from loguru import logger
import re
from config import settings
from services.text_normalization import normalize_for_match


class CategorySearchAgent(BaseAgent):
    """Agent for category-based search"""
    
    def __init__(self):
        super().__init__(
            name="CategorySearchAgent",
            system_prompt=CATEGORY_SEARCH_AGENT_PROMPT,
            tools=[search_listings_tool],
            tool_choice={"type": "function", "function": {"name": "search_listings"}},
        )


class PriceSearchAgent(BaseAgent):
    """Agent for price-based search"""
    
    def __init__(self):
        super().__init__(
            name="PriceSearchAgent",
            system_prompt=PRICE_SEARCH_AGENT_PROMPT,
            tools=[search_listings_tool],
            tool_choice={"type": "function", "function": {"name": "search_listings"}},
        )


class ContentSearchAgent(BaseAgent):
    """Agent for content-based search (title/description)"""
    
    def __init__(self):
        super().__init__(
            name="ContentSearchAgent",
            system_prompt=CONTENT_SEARCH_AGENT_PROMPT,
            tools=[search_listings_tool],
            tool_choice={"type": "function", "function": {"name": "search_listings"}},
        )


class SearchComposerAgent(BaseAgent):
    """Composer agent that orchestrates parallel search operations"""
    
    def __init__(self):
        super().__init__(
            name="SearchComposerAgent",
            system_prompt=SEARCH_COMPOSER_AGENT_PROMPT,
            tools=[search_listings_tool, market_price_tool]
        )
    
    async def orchestrate_search(
        self,
        user_message: str,
        context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Orchestrate parallel search operations
        
        Args:
            user_message: User's search query
            context: Additional context
        
        Returns:
            Combined search results
        """
        try:
            message_lower = user_message.lower()

            def _clean_search_query(msg: str) -> str | None:
                if not msg:
                    return None
                raw_tokens = re.findall(r"[0-9a-zA-ZçğıöşüÇĞİÖŞÜ\+]+", msg)
                if not raw_tokens:
                    return None
                stop_tokens = {
                    "var",
                    "mi",
                    "mu",
                    "mı",
                    "mü",
                    "varmi",
                    "varmı",
                    "mevcut",
                    "mevcutmu",
                    "bulunur",
                    "bulunurmu",
                    "ara",
                    "arama",
                    "bul",
                    "ariyorum",
                    "arıyorum",
                    "bakiyorum",
                    "bakıyorum",
                    "istiyorum",
                    "lazim",
                    "lazım",
                    "satilik",
                    "satılık",
                    "fiyati",
                    "fiyatı",
                    "ne",
                    "nedir",
                }
                cleaned: list[str] = []
                for tok in raw_tokens:
                    norm = normalize_for_match(tok)
                    if not norm:
                        continue
                    if norm in stop_tokens:
                        continue
                    if len(norm) <= 1:
                        continue
                    cleaned.append(tok)
                joined = " ".join(cleaned).strip()
                return joined or None

            # Deterministic category inference (prevents false 0 results)
            inferred_category = None
            try:
                from services.category_library import classify_category

                inferred_category = classify_category(user_message)
            except Exception:
                inferred_category = None

            # If the query is mostly a category word (e.g. "araba arıyorum"),
            # avoid over-filtering with a narrow search_text.
            def _category_only_search_text(msg: str) -> str | None:
                """Return None if query is mostly category-only to avoid over-filtering.

                Keep search_text for specific single-token queries (brands/products)
                so results don't collapse to a broad category list.
                """
                s = (msg or "").strip().lower()
                if not s:
                    return None
                tokens = [t for t in re.findall(r"[0-9a-zA-ZçğıöşüÇĞİÖŞÜ]+", s) if t]
                stop = {
                    "ariyorum",
                    "arıyorum",
                    "bakiyorum",
                    "bakıyorum",
                    "var",
                    "mi",
                    "mu",
                    "varmi",
                    "varmı",
                    "istiyorum",
                    "lazim",
                    "lazım",
                    "satilik",
                    "satılık",
                }
                meaningful = [t for t in tokens if t not in stop and len(t) >= 3]
                if len(meaningful) >= 2:
                    return msg

                generic_category_terms = {
                    "telefon",
                    "laptop",
                    "bilgisayar",
                    "araba",
                    "ev",
                    "emlak",
                    "giyim",
                    "kıyafet",
                    "ayakkabı",
                    "ayakkabi",
                    "oyuncak",
                    "bisiklet",
                    "mobilya",
                    "koltuk",
                    "masa",
                    "sandalye",
                    "yatak",
                }
                if meaningful and meaningful[0] in generic_category_terms:
                    return None

                return msg

            def _extract_price_filters(msg: str) -> tuple[float | None, float | None]:
                """Parse common Turkish price range queries."""
                s = (msg or "").lower()
                if not s:
                    return (None, None)

                def _to_number(raw: str) -> float | None:
                    raw = (raw or "").strip().lower()
                    if not raw:
                        return None
                    raw_clean = re.sub(r"[^0-9.,]", "", raw)
                    if not raw_clean:
                        return None
                    raw_clean = raw_clean.replace(".", "").replace(",", "")
                    if not raw_clean.isdigit():
                        return None
                    try:
                        return float(int(raw_clean))
                    except Exception:
                        return None

                def _apply_multiplier(value: float | None, tail: str) -> float | None:
                    if value is None:
                        return None
                    tail = (tail or "").lower()
                    if any(k in tail for k in ["milyon", "million"]):
                        return value * 1_000_000
                    if any(k in tail for k in ["bin", "k"]):
                        return value * 1_000
                    return value

                m_range = re.search(
                    r"(\d[\d\s\.,]*)\s*(bin|k|milyon|million)?\s*(?:-|–|—|ile|arası|arasi|to)\s*(\d[\d\s\.,]*)\s*(bin|k|milyon|million)?",
                    s,
                )
                if m_range:
                    a = _apply_multiplier(_to_number(m_range.group(1)), m_range.group(2) or "")
                    b = _apply_multiplier(_to_number(m_range.group(3)), m_range.group(4) or "")
                    if a is not None and b is not None:
                        return (min(a, b), max(a, b))

                m_max = re.search(r"(\d[\d\s\.,]*)\s*(bin|k|milyon|million)?\s*(?:alt[ıi]|altinda|altında|en\s+fazla|maks)\b", s)
                if m_max:
                    b = _apply_multiplier(_to_number(m_max.group(1)), m_max.group(2) or "")
                    return (None, b) if b is not None else (None, None)

                m_min = re.search(r"(?:en\s+az)\s*(\d[\d\s\.,]*)\s*(bin|k|milyon|million)?", s)
                if not m_min:
                    m_min = re.search(r"(\d[\d\s\.,]*)\s*(bin|k|milyon|million)?\s*(?:ust[üu]|ustunde|üstü|üstünde|en\s+az|min)\b", s)
                if m_min:
                    a = _apply_multiplier(_to_number(m_min.group(1)), m_min.group(2) or "")
                    return (a, None) if a is not None else (None, None)

                return (None, None)

            # If the user is asking a category classification question (not searching listings),
            # answer directly to avoid confusing "0 ilan bulundu" responses.
            if any(phrase in message_lower for phrase in [
                "hangi kategoriye girer",
                "hangi kategori",
                "kategoriye girer",
                "kategorisi ne",
                "kategorisi nedir",
            ]):
                category_map = {
                    # Automotive
                    "araba": "Otomotiv",
                    "otomobil": "Otomotiv",
                    "citroen": "Otomotiv",
                    "renault": "Otomotiv",
                    "fiat": "Otomotiv",
                    "toyota": "Otomotiv",
                    "honda": "Otomotiv",
                    # Electronics
                    "telefon": "Elektronik",
                    "iphone": "Elektronik",
                    "samsung": "Elektronik",
                    "xiaomi": "Elektronik",
                    "harddisk": "Elektronik",
                    "hard disk": "Elektronik",
                    "ssd": "Elektronik",
                    # Fashion
                    "kazak": "Moda & Aksesuar",
                    "ayakkabı": "Moda & Aksesuar",
                    "ayakkabi": "Moda & Aksesuar",
                    "elbise": "Moda & Aksesuar",
                    "ceket": "Moda & Aksesuar",
                }

                chosen = None
                for key, cat in category_map.items():
                    if key in message_lower:
                        chosen = cat
                        break
                if not chosen:
                    chosen = "Diğer"

                msg = f"Bence bu ürün için en uygun kategori: {chosen}."
                return {
                    "success": True,
                    "listings": [],
                    "listings_full": [],
                    "count": 0,
                    "market_data": {},
                    "insights": [],
                    "message": msg
                }

            min_price, max_price = _extract_price_filters(user_message)
            cleaned_query = _clean_search_query(user_message)

            search_text = cleaned_query or user_message
            if inferred_category:
                search_text = _category_only_search_text(cleaned_query or user_message)

            def _tokenize_for_fuzzy(text: str) -> list[str]:
                norm = normalize_for_match(text)
                if not norm:
                    return []
                return [t for t in re.findall(r"[0-9a-zA-Zçğıöşü]+", norm) if len(t) >= 2]

            def _edit_distance(a: str, b: str) -> int:
                if a == b:
                    return 0
                if not a:
                    return len(b)
                if not b:
                    return len(a)
                dp = list(range(len(b) + 1))
                for i, ca in enumerate(a, 1):
                    prev = dp[0]
                    dp[0] = i
                    for j, cb in enumerate(b, 1):
                        cur = dp[j]
                        cost = 0 if ca == cb else 1
                        dp[j] = min(
                            dp[j] + 1,
                            dp[j - 1] + 1,
                            prev + cost,
                        )
                        prev = cur
                return dp[-1]

            def _fuzzy_match_token(token: str, words: list[str]) -> bool:
                if not token:
                    return False
                if token in words:
                    return True
                for w in words:
                    if not w:
                        continue
                    if abs(len(w) - len(token)) > 2:
                        continue
                    threshold = 1 if max(len(w), len(token)) <= 6 else 2
                    if _edit_distance(token, w) <= threshold:
                        return True
                return False

            def _extract_metadata_keywords_text(metadata: Any) -> str:
                if isinstance(metadata, dict):
                    return str(metadata.get("keywords_text") or "").strip()
                if isinstance(metadata, str):
                    raw = metadata.strip()
                    if not raw:
                        return ""
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            return str(parsed.get("keywords_text") or "").strip()
                    except Exception:
                        return raw
                return ""

            async def _fuzzy_fallback_search() -> list[Dict[str, Any]]:
                if not cleaned_query:
                    return []
                tokens = _tokenize_for_fuzzy(cleaned_query)
                if not tokens:
                    return []
                if len(tokens) > 3:
                    return []

                try:
                    fallback = await search_listings_tool.execute(
                        category=inferred_category,
                        min_price=min_price,
                        max_price=max_price,
                        search_text=None,
                        limit=50,
                    )
                except Exception:
                    return []

                if not isinstance(fallback, dict) or not fallback.get("success"):
                    return []
                pool = (fallback.get("data") or {}).get("listings") or []
                if not pool:
                    return []

                matched: list[Dict[str, Any]] = []
                for listing in pool:
                    if not isinstance(listing, dict):
                        continue
                    text = " ".join([
                        str(listing.get("title") or ""),
                        str(listing.get("description") or ""),
                        str(listing.get("category") or ""),
                        _extract_metadata_keywords_text(listing.get("metadata")),
                    ])
                    words = _tokenize_for_fuzzy(text)
                    if not words:
                        continue
                    if all(_fuzzy_match_token(tok, words) for tok in tokens):
                        matched.append(listing)
                return matched

            if getattr(settings, "debug", False):
                logger.info(
                    f"[search] fast_path category={inferred_category} min={min_price} max={max_price} search_text={search_text!r}"
                )

            search_task = search_listings_tool.execute(
                category=inferred_category,
                min_price=min_price,
                max_price=max_price,
                search_text=search_text,
                limit=20,
            )
            market_task = market_price_tool.execute(product_key=user_message)
            search_res, market_data = await asyncio.gather(search_task, market_task, return_exceptions=True)

            all_listings = []
            if isinstance(search_res, dict) and search_res.get("success"):
                all_listings = (search_res.get("data") or {}).get("listings") or []

            if not all_listings and inferred_category and search_text:
                try:
                    fallback = await search_listings_tool.execute(
                        category=None,
                        min_price=min_price,
                        max_price=max_price,
                        search_text=search_text,
                        limit=20,
                    )
                    if isinstance(fallback, dict) and fallback.get("success"):
                        all_listings = (fallback.get("data") or {}).get("listings") or []
                except Exception:
                    pass

            if not all_listings:
                fuzzy_hits = await _fuzzy_fallback_search()
                if fuzzy_hits:
                    all_listings = fuzzy_hits

            if not isinstance(market_data, dict):
                market_data = {}
            insights = []
            if market_data.get("success") and market_data["data"].get("snapshots"):
                snaps = market_data["data"]["snapshots"]
                avg_prices = [s.get("avg_price") for s in snaps if s.get("avg_price") is not None]
                if avg_prices:
                    market_avg = sum(avg_prices) / len(avg_prices)
                    insights.append(f"Piyasa ortalaması ~{market_avg:.2f} ({len(avg_prices)} kaynak)")
            
            # Limit to 5 items for response to avoid token blowup
            preview_listings = all_listings[:5]
            remaining = max(len(all_listings) - len(preview_listings), 0)
            msg_lines = [f"{len(all_listings)} ilan bulundu."]
            if preview_listings:
                for idx, listing in enumerate(preview_listings, 1):
                    title = listing.get("title") or "Başlıksız"
                    price = listing.get("price")
                    price_txt = f"{price} TL" if price is not None else "Fiyat belirtilmemiş"
                    category = listing.get("category") or "Kategori yok"
                    image_url = None
                    if listing.get("image_url"):
                        image_url = listing["image_url"]
                    elif listing.get("images") and isinstance(listing["images"], list) and listing["images"]:
                        first_img = listing["images"][0]
                        if isinstance(first_img, dict):
                            image_url = first_img.get("image_url") or first_img.get("public_url")
                        elif isinstance(first_img, str):
                            image_url = first_img
                    short_desc = (listing.get("description") or "")[:120]
                    msg_lines.append(f"{idx}. {title} - {price_txt} - {category}")
                    if image_url:
                        msg_lines.append(f"![{title}]({image_url})")
                    if short_desc:
                        msg_lines.append(short_desc + "...")
                    msg_lines.append("")
            if remaining > 0:
                msg_lines.append(f"İlk {len(preview_listings)} tanesi gösterildi. Daha fazlası için söyleyin.")
            if preview_listings:
                msg_lines.append("Detay için: '1 nolu ilanın detayını göster' yazabilirsiniz.")
            msg = "\n".join(msg_lines)

            # Attach cache marker for frontend parsing
            search_cache = {"results": preview_listings}
            msg = f"{msg}\n[SEARCH_CACHE]{json.dumps(search_cache)}"

            return {
                "success": True,
                "listings": preview_listings,
                "listings_full": all_listings,
                "count": len(all_listings),
                "market_data": market_data["data"] if market_data.get("success") else {},
                "insights": insights,
                "message": msg
            }
        
        except Exception as e:
            logger.error(f"Search orchestration error: {e}")
            return {
                "success": False,
                "error": str(e),
                "listings": [],
                "count": 0,
                "market_data": {},
                "insights": []
            }
