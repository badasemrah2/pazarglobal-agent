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

    # Explicit used
    if any(tok in text for tok in [
        "2 el",
        "2el",
        "ikinci el",
        "kullanilmis",
        "used",
    ]):
        return "2. El"

    # Adjective-only condition labels ("orta", "normal"...) are typically used items.
    if any(tok in text for tok in ["orta", "normal", "idare", "vasat", "eski", "yipran"]):
        return "2. El"

    return None
