from typing import Any, Dict


EXAMPLE_LISTING_OWNER_ID = "3ec55e9d-93e8-40c5-8e0e-7dc933da997f"
EXAMPLE_LISTING_LABEL = "Örnek İlan"


def is_example_listing_owner(owner_id: Any) -> bool:
    return str(owner_id or "").strip() == EXAMPLE_LISTING_OWNER_ID


def is_example_listing(listing: Dict[str, Any] | None) -> bool:
    if not isinstance(listing, dict):
        return False
    owner_id = listing.get("user_id") or listing.get("owner_id")
    return is_example_listing_owner(owner_id)


def prefix_example_listing_title(title: str, listing: Dict[str, Any] | None) -> str:
    clean_title = str(title or "").strip() or "Başlıksız"
    if not is_example_listing(listing):
        return clean_title
    return f"[{EXAMPLE_LISTING_LABEL}] {clean_title}"