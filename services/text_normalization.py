import unicodedata


def normalize_for_match(text: str) -> str:
    """Normalize user text for robust, case-insensitive matching.

    Handles Turkish I/İ edge-cases and strips diacritics so that:
    - "AÇIKLAMA" → "aciklama"
    - "Açıklama" → "aciklama"
    - "BAŞLIK" → "baslik"

    This is intended ONLY for matching/keyword detection, not for display.
    """

    if text is None:
        return ""

    value = str(text).strip()
    if not value:
        return ""

    # Unicode casefold, then normalize dotted/dotless i differences.
    value = value.casefold()
    value = value.replace("ı", "i")

    # Strip accents/diacritics (ş->s, ç->c, ğ->g, ö->o, ü->u, İ->i̇ -> i).
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def canonicalize_condition(raw: str | None) -> str | None:
    """Map free-form condition text into marketplace-standard labels.

    Canonical values:
    - "Sıfır" (unused/new)
    - "2. El" (used)
    - "Az Kullanılmış" (optional, lightly used)

    Returns None when we can't confidently map.
    """

    text = normalize_for_match(raw or "")
    if not text:
        return None

    # Normalize common punctuation that breaks token matching (e.g. "2.el", "2. el").
    text = (
        text.replace(".", " ")
        .replace("/", " ")
        .replace("-", " ")
    )
    text = " ".join(text.split())

    # Explicit used
    if any(tok in text for tok in [
        "2 el",
        "2el",
        "ikinci el",
        "kullanilmis",
        "kullanildi",
        "kullandim",
        "kullandık",
        "kullandik",
        "kullanildi",
        "kullanilmis",
        "used",
    ]):
        return "2. El"

    # Lightly used / like new
    if any(tok in text for tok in [
        "az kullan",
        "cok az kullan",
        "az kullanilmis",
        "cok az kullanilmis",
        "yeni gibi",
        "like new",
        "temiz",
        "cok temiz",
        "iyi durum",
        "cok iyi",
        "mukemmel",
        "kusursuz",
    ]):
        return "Az Kullanılmış"

    # New / unused
    if any(tok in text for tok in [
        "sifir",
        "0",
        "hic kullan",
        "kullanilmamis",
        "paketli",
        "kapali kutu",
        "kutusunda",
        "yeni",
        "sifir ayar",
    ]):
        return "Sıfır"

    # Adjective-only condition labels ("orta", "normal"...) are typically used items.
    if any(tok in text for tok in ["orta", "normal", "idare", "vasat", "eski", "yipran"]):
        return "2. El"

    return None


def _norm_tokens(values: list[str]) -> set[str]:
    """Normalize a list of keywords for reuse in guards."""

    normalized: set[str] = set()
    for value in values:
        norm = normalize_for_match(value)
        if norm:
            normalized.add(norm)
    return normalized


_ACTION_COMMAND_PHRASES = _norm_tokens([
    "fotografi ilana ekle",
    "fotografi ilanda kullan",
    "fotografi ekle",
    "resmi ilana ekle",
    "resmi ekle",
    "fotografi ekledim",
    "fotografi yukle",
    "resmi yukle",
    "gorseli ekle",
])

_IMAGE_WORDS = _norm_tokens([
    "foto",
    "fotograf",
    "fotografi",
    "fotograflar",
    "resim",
    "resmi",
    "resimler",
    "gorsel",
    "gorseli",
    "image",
])

_ACTION_VERBS = _norm_tokens([
    "ekle",
    "ekleyin",
    "ekledim",
    "ekler",
    "eklesene",
    "yukle",
    "yukledim",
    "gonder",
    "gonderdim",
    "dahil",
    "koy",
])

_LISTING_WORDS = _norm_tokens([
    "ilan",
    "ilana",
    "ilani",
    "taslak",
    "liste",
])

_ACTION_COMMAND_TOKEN_WHITELIST = _norm_tokens([
    "foto",
    "fotograf",
    "fotografi",
    "resim",
    "resmi",
    "gorsel",
    "gorseli",
    "ilan",
    "ilana",
    "ilani",
    "taslak",
    "taslaga",
    "taslagi",
    "bunu",
    "sun",
    "sunuda",
    "ekle",
    "yukle",
])


def _token_matches_keywords(token: str, keywords: set[str]) -> bool:
    token = token.strip()
    if not token:
        return False
    for key in keywords:
        if not key:
            continue
        if token == key:
            return True
        if token.startswith(key):
            return True
        if len(token) > len(key) and token.startswith(key[:-1]):
            return True
        if key in token and len(key) >= 4:
            return True
    return False


def looks_like_image_action_command(text: str) -> bool:
    """Return True when the user text is an image-only action command."""

    normalized = normalize_for_match(text)
    if not normalized:
        return False
    if normalized in _ACTION_COMMAND_PHRASES:
        return True

    tokens = [tok for tok in normalized.split() if tok]
    if not tokens or len(tokens) > 8:
        return False

    has_image_word = any(_token_matches_keywords(tok, _IMAGE_WORDS) for tok in tokens)
    if not has_image_word:
        return False

    has_action_verb = any(_token_matches_keywords(tok, _ACTION_VERBS) for tok in tokens)
    has_listing_word = any(_token_matches_keywords(tok, _LISTING_WORDS) for tok in tokens)

    if has_image_word and has_action_verb:
        return True
    if has_image_word and has_listing_word and len(tokens) <= 5:
        return True
    return False


def violates_listing_content_guard(text: str | None) -> bool:
    """Detect trivial/action-command payloads that should not overwrite slots."""

    normalized = normalize_for_match(text or "")
    if not normalized:
        return True
    if normalized in _ACTION_COMMAND_PHRASES:
        return True

    tokens = [tok for tok in normalized.split() if tok]
    if not tokens:
        return True

    if len(tokens) <= 5 and all(tok in _ACTION_COMMAND_TOKEN_WHITELIST for tok in tokens):
        return True

    if len(tokens) <= 3 and any(
        _token_matches_keywords(tok, _IMAGE_WORDS.union(_ACTION_VERBS))
        for tok in tokens
    ):
        return True

    return False
