from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


_TR_MAP = str.maketrans({
    "ç": "c",
    "ğ": "g",
    "ı": "i",
    "İ": "i",
    "ö": "o",
    "ş": "s",
    "ü": "u",
    "Ç": "c",
    "Ğ": "g",
    "Ö": "o",
    "Ş": "s",
    "Ü": "u",
})


def _norm(text: str) -> str:
    s = (text or "").strip().lower().translate(_TR_MAP)
    # Replace separators with spaces, collapse whitespace
    # Keep '+' so room-format tokens like '2+1' survive for Emlak heuristics.
    s = re.sub(r"[^0-9a-z&+]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokenize(text: str) -> List[str]:
    s = _norm(text)
    if not s:
        return []
    return [t for t in s.split(" ") if t]


@dataclass(frozen=True)
class CategorySpec:
    label: str
    strong: Tuple[str, ...]
    weak: Tuple[str, ...]


@dataclass(frozen=True)
class CategoryOption:
    id: str
    label: str


# Public API: single-source category options.
# - `id` is the canonical value stored in DB.
# - `label` is what UI should show (unified taxonomy).
# Note: Do not copy third-party marketplace taxonomies verbatim unless you have permission.
CATEGORY_OPTIONS: Tuple[CategoryOption, ...] = (
    CategoryOption(id="Emlak", label="Emlak"),
    # UI label; DB stores existing canonical id
    CategoryOption(id="Otomotiv", label="Otomotiv"),
    CategoryOption(id="Elektronik", label="Elektronik"),
    CategoryOption(id="Ev & Yaşam", label="Ev & Yaşam"),
    CategoryOption(id="Moda & Aksesuar", label="Moda & Aksesuar"),
    CategoryOption(id="Anne, Bebek & Oyuncak", label="Anne, Bebek & Oyuncak"),
    CategoryOption(id="Spor & Outdoor", label="Spor & Outdoor"),
    CategoryOption(id="Hobi, Koleksiyon & Sanat", label="Hobi, Koleksiyon & Sanat"),
    CategoryOption(id="İş Makineleri & Sanayi", label="İş Makineleri & Sanayi"),
    CategoryOption(id="Yedek Parça & Aksesuar", label="Yedek Parça & Aksesuar"),
    # UI shows "Ustalar & Hizmetler" but DB stores existing canonical "Hizmetler"
    CategoryOption(id="Hizmetler", label="Ustalar & Hizmetler"),
    # UI shows "Özel Ders & Eğitim" but DB stores existing canonical "Eğitim & Kurs"
    CategoryOption(id="Eğitim & Kurs", label="Özel Ders & Eğitim"),
    CategoryOption(id="İş İlanları", label="İş İlanları"),
    CategoryOption(id="Dijital Ürün & Hizmetler", label="Dijital Ürün & Hizmetler"),
    # UI shows "Genel / Diğer" but DB stores existing canonical "Diğer"
    CategoryOption(id="Diğer", label="Genel / Diğer"),
)


SUPPORTED_CATEGORIES: Tuple[str, ...] = tuple(opt.id for opt in CATEGORY_OPTIONS)


def get_supported_categories() -> List[str]:
    """Return canonical category IDs (DB values)."""
    return list(SUPPORTED_CATEGORIES)


def get_category_options() -> List[Dict[str, str]]:
    """Return UI-ready category options (id + label)."""
    return [{"id": opt.id, "label": opt.label} for opt in CATEGORY_OPTIONS]


def normalize_category_id(text: str) -> Optional[str]:
    """Normalize a category-like input to a canonical category id.

    Accepts either:
    - canonical id (e.g. "Otomotiv")
    - UI label (e.g. "Vasıta")
    - free text (tries classify_category)
    """
    raw = (text or "").strip()
    if not raw:
        return None

    raw_norm = _norm(raw)
    if not raw_norm:
        return None

    # Exact match against known ids/labels
    for opt in CATEGORY_OPTIONS:
        if _norm(opt.id) == raw_norm:
            return opt.id
        if _norm(opt.label) == raw_norm:
            return opt.id

    # Fallback: deterministic classification from free text
    guessed = classify_category(raw)
    if guessed:
        return guessed
    return None


def _contains_phrase(haystack: str, phrase: str) -> bool:
    # Both inputs must be normalized already
    if not haystack or not phrase:
        return False
    padded = f" {haystack} "
    return f" {phrase} " in padded


def _count_matches(text_norm: str, tokens: Set[str], phrases: Sequence[str]) -> int:
    score = 0
    for p in phrases:
        if _contains_phrase(text_norm, p):
            score += 1
    for t in tokens:
        # Exact token presence is safer than substring
        if re.search(rf"\b{re.escape(t)}\b", text_norm):
            score += 1
    return score


# Keep the library intentionally finite and maintainable.
# Goal: deterministic, high-precision category mapping (not perfect recall).
_CATEGORIES: Tuple[CategorySpec, ...] = (
    CategorySpec(
        label="Otomotiv",
        strong=(
            "otomotiv",
            "otomobil",
            "araba",
            "arac",
            "vasita",
            "kamyonet",
            "motorsiklet",
            "motosiklet",
            "scooter",
            "atv",
            "pickup",
            "suv",
            "deniz araci",
            "jet ski",
            "tekne",
        ),
        weak=(
            # Brands (weak because brands can appear in other contexts)
            "bmw",
            "mercedes",
            "mercedes benz",
            "audi",
            "volkswagen",
            "vw",
            "renault",
            "fiat",
            "ford",
            "toyota",
            "honda",
            "hyundai",
            "kia",
            "peugeot",
            "citroen",
            "opel",
            "nissan",
            "volvo",
            "skoda",
            "seat",
            "dacia",
            "tofas",
            "togg",
            "tesla",
            "porsche",
            "jeep",
        ),
    ),
    CategorySpec(
        label="Elektronik",
        strong=(
            "elektronik",
            "telefon",
            "akilli telefon",
            "smartphone",
            "iphone",
            "ipad",
            "macbook",
            "laptop",
            "notebook",
            "bilgisayar",
            "pc",
            "masaustu",
            "monitor",
            "monitör",
            "ekran",
            "ekran karti",
            "playstation",
            "ps5",
            "xbox",
            "nintendo",
            "kulaklik",
            "airpods",
            "kamera",
            "fotograf makinesi",
        ),
        weak=(
            "apple",
            "samsung",
            "xiaomi",
            "redmi",
            "huawei",
            "honor",
            "oppo",
            "vivo",
            "oneplus",
            "realme",
            "lenovo",
            "hp",
            "dell",
            "asus",
            "acer",
            "msi",
            "lg",
            "sony",
            "canon",
            "nikon",
        ),
    ),
    CategorySpec(
        # White goods / furniture / decoration are grouped under the unified main category
        label="Ev & Yaşam",
        strong=(
            "buzdolabi",
            "buz dolabi",
            "camasir makinesi",
            "bulasik makinesi",
            "kurutma makinesi",
            "klima",
            "firin",
            "ocak",
            "mikrodalga",
            "derin dondurucu",
            "mobilya",
            "koltuk",
            "kanepe",
            "masa",
            "sandalye",
            "yatak",
            "gardrop",
            "dolap",
            "sehpa",
            "dekorasyon",
            "hali",
            "halı",
            "perde",
        ),
        weak=(
            "arcelik",
            "beko",
            "bosch",
            "siemens",
            "vestel",
            "profilo",
            "regal",
            "altus",
            "electrolux",
            "ariston",
            "indesit",
            "lg",
            "samsung",
        ),
    ),
    CategorySpec(
        label="Emlak",
        strong=(
            "emlak",
            "daire",
            "ev",
            "apartman",
            "apart",
            "konut",
            "rezidans",
            "villa",
            "yazlik",
            "müstakil",
            "mustakil",
            "dubleks",
            "dupleks",
            "dulex",
            "triplex",
            "studyo daire",
            "stüdyo daire",
            "arsa",
            "tarla",
            "dükkan",
            "dukkan",
            "ofis",
        ),
        weak=(
            "metrekare",
            "m2",
            "tapu",
            "site ici",
            "site içi",
            "siteli",
            "havuzlu",
            "kat",
            # These words often appear in any category and should not be decisive alone.
            "kiralik",
            "satilik",
        ),
    ),
    CategorySpec(
        label="Moda & Aksesuar",
        strong=(
            "giyim",
            "aksesuar",
            "ayakkabi",
            "elbise",
            "mont",
            "ceket",
            "pantolon",
            "kazak",
            "canta",
            "çanta",
            "saat",
            "takı",
            "taki",
        ),
        weak=(
            "nike",
            "adidas",
            "puma",
            "zara",
            "hm",
            "mango",
        ),
    ),
    CategorySpec(
        label="Spor & Outdoor",
        strong=(
            "spor",
            "outdoor",
            "kamp",
            "çadır",
            "cadir",
            "uyku tulumu",
            "bisiklet",
            "fitness",
            "dambıl",
            "dambil",
        ),
        weak=(
            "decathlon",
        ),
    ),
    CategorySpec(
        label="Hobi, Koleksiyon & Sanat",
        strong=(
            "kitap",
            "roman",
            "dergi",
            "müzik",
            "muzik",
            "cd",
            "plak",
            "hobi",
            "koleksiyon",
            "antika",
            "müzik aleti",
            "muzik aleti",
            "gitar",
            "piyano",
            "keman",
            "resim",
            "tablo",
            "heykel",
            "sanat",
        ),
        weak=(
            "lego",
        ),
    ),
    CategorySpec(
        label="Anne, Bebek & Oyuncak",
        strong=(
            "bebek",
            "anne",
            "oyuncak",
            "puset",
            "bebek arabasi",
            "oto koltugu",
            "mama",
        ),
        weak=(
            "chicco",
            "baby",
        ),
    ),
    CategorySpec(
        label="Hayvanlar Alemi",
        strong=(
            "kedi",
            "köpek",
            "kopek",
            "pet",
            "mama",
            "kum",
            "tasma",
            "akvaryum",
            "evcil",
            "kuş",
            "kus",
        ),
        weak=(
            "royal canin",
            "pro plan",
        ),
    ),
    CategorySpec(
        label="İş Makineleri & Sanayi",
        strong=(
            "sanayi",
            "endustri",
            "endüstri",
            "is makinasi",
            "iş makinesi",
            "makine",
            "forklift",
            "jenerator",
            "jeneratör",
            "insaat",
            "inşaat",
            "tarim",
            "tarım",
        ),
        weak=(
            "tesis",
        ),
    ),
    CategorySpec(
        label="Eğitim & Kurs",
        strong=(
            "kurs",
            "egitim",
            "eğitim",
            "özel ders",
            "ozel ders",
        ),
        weak=(
            "sertifika",
        ),
    ),
    CategorySpec(
        label="Hizmetler",
        strong=(
            "hizmet",
            "tamir",
            "montaj",
            "nakliye",
            "temizlik",
        ),
        weak=(),
    ),
    CategorySpec(
        label="Yedek Parça & Aksesuar",
        strong=(
            "yedek parca",
            "yedek parça",
            "parca",
            "parça",
            "aksesuar",
            "lastik",
            "jant",
            "akü",
            "aku",
            "sarj aleti",
            "şarj aleti",
        ),
        weak=(),
    ),
    CategorySpec(
        label="İş İlanları",
        strong=(
            "is ilani",
            "iş ilanı",
            "is ariyorum",
            "iş arıyorum",
            "ise alım",
            "işe alım",
            "full time",
            "tam zamanli",
            "yarim zamanli",
            "freelance",
            "cv",
        ),
        weak=(),
    ),
    CategorySpec(
        label="Dijital Ürün & Hizmetler",
        strong=(
            "dijital",
            "abonelik",
            "hesap",
            "kod",
            "lisans",
            "yazilim",
            "yazılım",
            "steam",
            "playstation plus",
            "ps plus",
        ),
        weak=(),
    ),
    CategorySpec(
        label="Diğer",
        strong=(
            "diger",
            "diğer",
        ),
        weak=(),
    ),
)


def classify_category(text: str) -> Optional[str]:
    """Deterministically classify a category from free text.

    Conservative by design:
    - Returns None when no confident signal is found.
    - Prefers strong (product-type) keywords; uses brands as a weaker fallback.
    """
    text_norm = _norm(text)
    if not text_norm:
        return None

    token_set = set(_tokenize(text))

    # Emlak heuristic: room-format queries like "2+1" are common but ambiguous
    # (e.g. speaker systems). Only treat as Emlak when there is additional
    # real-estate context.
    has_room_format = bool(re.search(r"\b\d\+\d\b", text_norm))
    if has_room_format:
        emlak_context_tokens = {
            "emlak",
            "daire",
            "ev",
            "konut",
            "apart",
            "apartman",
            "rezidans",
            "villa",
            "yazlik",
            "müstakil",
            "mustakil",
            "arsa",
            "tarla",
            "ofis",
            "dukkan",
        }
        if token_set & emlak_context_tokens or _contains_phrase(text_norm, "studyo daire"):
            return "Emlak"

    best: Optional[Tuple[str, int, int]] = None  # (label, strong_score, weak_score)

    for spec in _CATEGORIES:
        strong_phrases = [_norm(p) for p in spec.strong if p]
        weak_phrases = [_norm(p) for p in spec.weak if p]

        strong_tokens = {p for p in strong_phrases if " " not in p}
        strong_multi = [p for p in strong_phrases if " " in p]
        weak_tokens = {p for p in weak_phrases if " " not in p}
        weak_multi = [p for p in weak_phrases if " " in p]

        strong_score = _count_matches(text_norm, strong_tokens & token_set, strong_multi)
        weak_score = _count_matches(text_norm, weak_tokens & token_set, weak_multi)

        # Confidence rule:
        # - Accept if we have any strong match
        # - Or if we have >=2 weak matches
        # - Special-case Otomotiv: 1 brand match is acceptable if there is an extra vehicle signal
        #   (year / km / model), because many users type just "citroen c3 2018".
        if strong_score <= 0:
            if weak_score >= 2:
                pass
            elif spec.label == "Otomotiv" and weak_score >= 1:
                has_year = bool(re.search(r"\b(19|20)\d{2}\b", text_norm))
                has_km = bool(re.search(r"\b\d{1,3}(?:\s*\.?\s*\d{3})?\s*km\b", text_norm)) or ("kilometre" in text_norm)
                has_model_signal = "model" in text_norm
                if not (has_year or has_km or has_model_signal):
                    continue
            else:
                continue

        candidate = (spec.label, strong_score, weak_score)
        if best is None:
            best = candidate
            continue

        # Prefer higher strong score, then weak score
        if candidate[1] > best[1] or (candidate[1] == best[1] and candidate[2] > best[2]):
            best = candidate

    return best[0] if best else None
