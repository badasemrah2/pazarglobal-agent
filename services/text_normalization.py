import unicodedata


_ALLOWED_ALPHA = set("abcçdefgğhıijklmnoöprsştuüvyzqwx")

_CHAR_MAP = {
    # E -> e
    **{ch: "e" for ch in "éèêëēĕėęěÉÈÊËĒĔĖĘĚ"},
    # A -> a
    **{ch: "a" for ch in "áàâäãåāăąÁÀÂÄÃÅĀĂĄ"},
    # I -> i (note: Turkish İ -> i, dotless ı preserved below)
    **{ch: "i" for ch in "íìîïīĭįÍÌÎÏĪĬĮİI"},
    # O -> o (ö preserved separately)
    **{ch: "o" for ch in "óòôõøōŏőÓÒÔÕØŌŎŐ"},
    # U -> u (ü preserved separately)
    **{ch: "u" for ch in "úùûūŭůűųÚÙÛŪŬŮŰŲ"},
    # C -> c (ç preserved separately)
    **{ch: "c" for ch in "ćĉċčĆĈĊČ"},
    # G -> g (ğ preserved separately)
    **{ch: "g" for ch in "ĝġģĜĠĢ"},
    # S -> s (ş preserved separately)
    **{ch: "s" for ch in "śŝšŚŜŠ"},
    # N -> n
    **{ch: "n" for ch in "ñńņňÑŃŅŇ"},
    # Y -> y
    **{ch: "y" for ch in "ýÿŷÝŸŶ"},
    # Z -> z
    **{ch: "z" for ch in "źżžŹŻŽ"},
    # L -> l
    **{ch: "l" for ch in "łĺļľŁĹĻĽ"},
    # D -> d
    **{ch: "d" for ch in "đďĐĎ"},
    # R -> r
    **{ch: "r" for ch in "ŕŗřŔŖŘ"},
    # T -> t
    **{ch: "t" for ch in "ţťŧŢŤŦ"},
    # Turkish preserved letters
    "ç": "ç",
    "Ç": "ç",
    "ğ": "ğ",
    "Ğ": "ğ",
    "ı": "ı",
    "ö": "ö",
    "Ö": "ö",
    "ş": "ş",
    "Ş": "ş",
    "ü": "ü",
    "Ü": "ü",
    # Special multi-char conversions
    "ß": "ss",
    "æ": "ae",
    "Æ": "ae",
    "œ": "oe",
    "Œ": "oe",
    "þ": "th",
    "Þ": "th",
    "ð": "d",
    "Ð": "d",
}


_TR_UPPER = {
    "i": "İ",
    "ı": "I",
    "ç": "Ç",
    "ğ": "Ğ",
    "ö": "Ö",
    "ş": "Ş",
    "ü": "Ü",
}


def _tr_upper(ch: str) -> str:
    return _TR_UPPER.get(ch, ch.upper())


def sentence_case_tr(text: str) -> str:
    """Uppercase sentence starts with Turkish casing rules."""
    if text is None:
        return ""

    s = str(text).strip()
    if not s:
        return ""

    result: list[str] = []
    capitalize_next = True

    for ch in s:
        if capitalize_next and ch.isalpha():
            result.append(_tr_upper(ch))
            capitalize_next = False
        else:
            result.append(ch)

        if ch in ".!?\n":
            capitalize_next = True

    return "".join(result)


def normalize_keyboard_text(text: str) -> str:
    """Normalize text to TR/EN keyboard-safe alphabet (Turkish 29 + q/w/x).

    - Keeps Turkish letters (ç, ğ, ı, ö, ş, ü)
    - Maps accented variants to closest allowed letters
    - Preserves digits and punctuation
    """
    if text is None:
        return ""

    value = str(text)
    if not value:
        return ""

    output: list[str] = []
    for ch in value:
        if ch in _CHAR_MAP:
            output.append(_CHAR_MAP[ch])
            continue

        if ch.isalpha():
            lower = ch.lower()
            if lower in _ALLOWED_ALPHA:
                output.append(lower)
                continue

            # Fallback: strip diacritics and try again
            decomposed = unicodedata.normalize("NFKD", ch)
            base = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
            if base in _ALLOWED_ALPHA:
                output.append(base)
            # else drop unsupported letter
            continue

        # Keep digits and punctuation as-is
        output.append(ch)

    return "".join(output)


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
