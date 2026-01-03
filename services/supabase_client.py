"""
Supabase client for database operations
"""
from supabase import create_client, Client
from config import settings
from typing import Optional, Dict, List, Any
from loguru import logger
import httpx
import re
import json

from .metadata_keywords import generate_listing_keywords


class InsufficientCreditsError(Exception):
    """Raised when wallet balance is not enough to publish a listing."""

    def __init__(self, required: int, balance: Optional[int]):
        self.required = int(required or 0)
        self.balance = int(balance) if balance is not None else None
        if self.balance is None:
            message = f"Cüzdan bakiyesi doğrulanamadı. Yayın için {self.required} kredi gerekiyor."
        else:
            message = (
                f"Bakiyeniz yetersiz. Yayın için {self.required} kredi gerekli, mevcut bakiye {self.balance} kredi."
            )
        super().__init__(message)


class SupabaseClient:
    """Supabase database client"""
    
    def __init__(self):
        self._client: Optional[Client] = None
        # Some deployments may not have the helper RPC installed in Supabase.
        # Cache its availability to avoid spamming logs and wasting network calls.
        self._rpc_update_listing_field_available: Optional[bool] = None
        self._rpc_update_listing_field_missing_logged: bool = False

    def _rpc_update_listing_field_is_missing(self, exc: Exception) -> bool:
        msg = str(exc) if exc is not None else ""
        msg_l = msg.lower()
        return (
            "pgrst202" in msg_l
            or "could not find the function" in msg_l
            or "update_listing_field" in msg_l and "could not find" in msg_l
        )

    def _maybe_disable_rpc_update_listing_field(self, exc: Exception) -> None:
        if self._rpc_update_listing_field_is_missing(exc):
            self._rpc_update_listing_field_available = False
            if not self._rpc_update_listing_field_missing_logged:
                logger.warning(
                    "Supabase RPC public.update_listing_field is missing; using direct updates for drafts. "
                    "(You can deploy supabase_rpc_update_listing_field.sql to enable atomic patching.)"
                )
                self._rpc_update_listing_field_missing_logged = True
    
    @property
    def client(self) -> Client:
        """Get or create Supabase client"""
        if self._client is None:
            url = (settings.supabase_url or "").strip()
            service_key = (settings.supabase_service_key or "").strip()

            if not url.startswith(("http://", "https://")):
                raise RuntimeError(
                    "SUPABASE_URL is missing/invalid. Set it in pazarglobal-agent/.env "
                    "(example: https://<project>.supabase.co)."
                )

            if not service_key or service_key.startswith("your_"):
                raise RuntimeError(
                    "SUPABASE_SERVICE_KEY is missing/invalid. Set your Supabase service role key in pazarglobal-agent/.env."
                )

            self._client = create_client(
                settings.supabase_url,
                settings.supabase_service_key
            )
        return self._client

    def _normalize_image_entry(self, entry: Any) -> Optional[Dict[str, Any]]:
        """Return a consistent image payload with image_url + metadata."""
        if entry is None:
            return None
        url: str = ""
        metadata: Dict[str, Any] = {}

        def to_public_url_if_needed(candidate: str) -> str:
            """Convert storage paths to public URLs when possible."""
            c = (candidate or "").strip()
            if not c:
                return ""
            if c.startswith(("http://", "https://")):
                return c
            # Already a storage URL path, missing hostname.
            if c.startswith("/storage/"):
                base = (getattr(settings, "supabase_url", "") or "").strip().rstrip("/")
                return f"{base}{c}" if base else c

            # Heuristic: treat as a storage object path in the default bucket.
            # Example stored value: "9054.../temp_xxx.jpg"
            base = (getattr(settings, "supabase_url", "") or "").strip().rstrip("/")
            if base and not any(ch in c for ch in ["{", "}", "\n", "\r", " "]):
                path = c.lstrip("/")
                return f"{base}/storage/v1/object/public/product-images/{path}"
            return c

        url_re = re.compile(r"https?://[^\s\)\]\"']+")

        def extract_first_url(value: Any, depth: int = 0) -> str:
            """Extract a usable http(s) URL from nested dict/list/JSON/markdown strings."""
            if depth > 4:
                return ""
            if value is None:
                return ""

            if isinstance(value, dict):
                for key in ["image_url", "public_url", "url", "storage_path", "path"]:
                    if key in value:
                        found = extract_first_url(value.get(key), depth + 1)
                        if found:
                            return found
                # Fallback: scan dict values
                for v in value.values():
                    found = extract_first_url(v, depth + 1)
                    if found:
                        return found
                return ""

            if isinstance(value, list):
                for item in value:
                    found = extract_first_url(item, depth + 1)
                    if found:
                        return found
                return ""

            if isinstance(value, str):
                s = value.strip()
                if not s:
                    return ""

                # Markdown image/link like ![x](https://...)
                md_match = re.search(r"\((https?://[^\s\)]+)\)", s)
                if md_match:
                    return md_match.group(1)

                # JSON payload stored as string (can be nested multiple times)
                if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                    try:
                        parsed = json.loads(s)
                        found = extract_first_url(parsed, depth + 1)
                        if found:
                            return found
                    except Exception:
                        pass

                # Raw URL inside a noisy string
                m = url_re.search(s)
                if m:
                    return m.group(0)

                # Storage path fallback (no http)
                return to_public_url_if_needed(s)

            return ""

        if isinstance(entry, dict):
            raw_url = entry.get("image_url") or entry.get("public_url") or entry.get("url") or entry.get("path")
            url = extract_first_url(raw_url)
            raw_meta = entry.get("metadata")
            if isinstance(raw_meta, dict):
                metadata = raw_meta
        elif isinstance(entry, str):
            url = extract_first_url(entry)
        else:
            return None

        if not url:
            return None
        return {"image_url": to_public_url_if_needed(url), "metadata": metadata}

    def _normalize_images(self, images: List[Any]) -> List[Dict[str, Any]]:
        """Normalize any image list into [{image_url, metadata}, ...]."""
        normalized: List[Dict[str, Any]] = []
        for entry in images or []:
            normalized_entry = self._normalize_image_entry(entry)
            if normalized_entry:
                normalized.append(normalized_entry)
        return normalized

    def _extract_image_url(self, entry: Any) -> Optional[str]:
        normalized = self._normalize_image_entry(entry)
        if normalized:
            return normalized.get("image_url")
        return None

    def _fallback_listing_keywords(self, *, title: str, category: str, description: str) -> Dict[str, Any]:
        """Deterministic keyword fallback when LLM keyword generation is unavailable.

        Produces a small, lowercased keyword list derived from title/category/description.
        """
        def tokenize(text: str) -> List[str]:
            t = (text or "").lower()
            # keep Turkish letters; keep + for room formats like 2+1
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
                w = w.strip()
                if not w or w in stop:
                    continue
                if len(w) < 2:
                    continue
                words.append(w)

        # Dedupe preserve order
        seen = set()
        deduped: List[str] = []
        for w in words:
            if w in seen:
                continue
            seen.add(w)
            deduped.append(w)

        deduped = deduped[:12]
        return {"keywords": deduped, "keywords_text": " ".join(deduped)}

    async def get_user_display_name(self, user_id: str) -> Optional[str]:
        """Resolve a friendly user display name from profiles.

        Tries display_name first, then full_name. Returns None when not found.
        """
        if not user_id:
            return None
        try:
            result = (
                self.client.table("profiles")
                .select("display_name, full_name")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
            row = (result.data or [None])[0]
            if not isinstance(row, dict):
                return None
            name = (row.get("display_name") or row.get("full_name") or "").strip()
            return name or None
        except Exception as e:
            logger.warning(f"Failed to resolve user display name: {e}")
            return None

    async def get_user_phone(self, user_id: str) -> Optional[str]:
        """Resolve user's phone from profiles (best-effort)."""
        if not user_id:
            return None
        try:
            result = (
                self.client.table("profiles")
                .select("phone")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
            row = (result.data or [None])[0]
            if not isinstance(row, dict):
                return None
            phone = (row.get("phone") or "").strip()
            return phone or None
        except Exception as e:
            logger.warning(f"Failed to resolve user phone: {e}")
            return None
    
    # Active Drafts Operations
    async def create_draft(self, user_id: str, phone_number: str) -> Dict[str, Any]:
        """Create a new draft listing aligned to active_drafts schema."""
        try:
            # Reuse existing draft if one is already in progress for this user
            existing = (self.client.table("active_drafts")
                        .select("*")
                        .eq("user_id", user_id)
                        .order("created_at", desc=True)
                        .limit(1)
                        .execute())
            if existing.data:
                draft = existing.data[0]
                if draft.get("state") != "in_progress":
                    try:
                        self.client.table("active_drafts").update({
                            "state": "in_progress"
                        }).eq("id", draft["id"]).execute()
                        draft["state"] = "in_progress"
                    except Exception as state_err:
                        logger.warning(f"Failed to refresh draft state for {draft['id']}: {state_err}")
                logger.info(f"Reusing existing draft {draft['id']} for user {user_id}")
                return draft

            listing_data = {
                "title": None,
                "description": None,
                "price": None,
                "category": None,
                "contact_phone": phone_number
            }
            result = self.client.table("active_drafts").insert({
                "user_id": user_id,
                "state": "in_progress",
                "listing_data": listing_data,
                "images": [],
                "vision_product": {}
            }).execute()
            
            if result.data:
                logger.info(f"Created draft: {result.data[0]['id']}")
                return result.data[0]
            
            raise Exception("Failed to create draft")
        except Exception as e:
            # Handle race condition: another draft may have been created after the initial check
            error_text = str(e)
            if "duplicate key value" in error_text and "active_drafts_user_id_key" in error_text:
                logger.warning(f"Draft already exists for user {user_id}, returning latest draft")
                fallback = (self.client.table("active_drafts")
                            .select("*")
                            .eq("user_id", user_id)
                            .order("created_at", desc=True)
                            .limit(1)
                            .execute())
                if fallback.data:
                    return fallback.data[0]
            logger.error(f"Error creating draft: {e}")
            raise

    async def reset_draft(self, draft_id: str, phone_number: Optional[str] = None) -> bool:
        """Reset an existing draft to a clean state.

        This is used when the platform enforces a single in-progress draft per user,
        but the user is clearly starting a brand-new listing flow.
        """
        try:
            listing_data = {
                "title": None,
                "description": None,
                "price": None,
                "category": None,
            }
            if phone_number:
                listing_data["contact_phone"] = phone_number

            result = self.client.table("active_drafts").update({
                "state": "in_progress",
                "listing_data": listing_data,
                "images": [],
                "vision_product": {}
            }).eq("id", draft_id).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error resetting draft: {e}")
            return False
    
    async def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Get draft by ID"""
        try:
            result = self.client.table("active_drafts").select("*").eq("id", draft_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting draft: {e}")
            return None

    async def get_latest_draft_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get the most recent draft for a user (best-effort)."""
        try:
            result = (
                self.client.table("active_drafts")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting latest draft for user: {e}")
            return None

    async def set_pending_price_suggestion(self, draft_id: str, suggested_price: int) -> bool:
        """Persist a pending suggested price into listing_data so any instance can later apply it."""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["_pending_price_suggestion"] = int(suggested_price)
            updated = (
                self.client.table("active_drafts")
                .update({"listing_data": listing_data})
                .eq("id", draft_id)
                .execute()
            )
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Failed to persist pending price suggestion: {e}")
            return False

    async def clear_pending_price_suggestion(self, draft_id: str) -> bool:
        """Remove the persisted pending suggested price from listing_data."""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            if "_pending_price_suggestion" in listing_data:
                listing_data.pop("_pending_price_suggestion", None)
                updated = (
                    self.client.table("active_drafts")
                    .update({"listing_data": listing_data})
                    .eq("id", draft_id)
                    .execute()
                )
                return bool(updated.data)
            return True
        except Exception as e:
            logger.warning(f"Failed to clear pending price suggestion: {e}")
            return False

    async def set_pending_publish_state(self, draft_id: str, state: Dict[str, Any]) -> bool:
        """Persist pending publish metadata inside listing_data."""
        if not draft_id or not isinstance(state, dict):
            return False
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["_pending_publish"] = state
            updated = (
                self.client.table("active_drafts")
                .update({"listing_data": listing_data})
                .eq("id", draft_id)
                .execute()
            )
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Failed to persist pending publish state: {e}")
            return False

    async def clear_pending_publish_state(self, draft_id: str) -> bool:
        """Remove pending publish metadata from listing_data (if present)."""
        if not draft_id:
            return False
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            if "_pending_publish" not in listing_data:
                return True
            listing_data.pop("_pending_publish", None)
            updated = (
                self.client.table("active_drafts")
                .update({"listing_data": listing_data})
                .eq("id", draft_id)
                .execute()
            )
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Failed to clear pending publish state: {e}")
            return False
    
    async def update_draft_title(self, draft_id: str, title: str) -> bool:
        """Update draft title inside listing_data"""
        if self._rpc_update_listing_field_available is not False:
            try:
                result = self.client.rpc("update_listing_field", {
                    "listing_id": draft_id,
                    "field_name": "title",
                    "field_value": title
                }).execute()
                if result.data:
                    self._rpc_update_listing_field_available = True
                    return True
            except Exception as e:
                self._maybe_disable_rpc_update_listing_field(e)
                if self._rpc_update_listing_field_available is not False:
                    logger.warning(f"RPC update_listing_field failed for title (falling back to direct update): {e}")

        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["title"] = title
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.error(f"Error updating title: {e}")
            return False
    
    async def update_draft_description(self, draft_id: str, description: str) -> bool:
        """Update draft description inside listing_data"""
        if self._rpc_update_listing_field_available is not False:
            try:
                result = self.client.rpc("update_listing_field", {
                    "listing_id": draft_id,
                    "field_name": "description",
                    "field_value": description
                }).execute()
                if result.data:
                    self._rpc_update_listing_field_available = True
                    return True
            except Exception as e:
                self._maybe_disable_rpc_update_listing_field(e)
                if self._rpc_update_listing_field_available is not False:
                    logger.warning(f"RPC update_listing_field failed for description (falling back to direct update): {e}")

        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["description"] = description
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.error(f"Error updating description: {e}")
            return False
    
    async def update_draft_price(self, draft_id: str, price: float) -> bool:
        """Update draft price inside listing_data"""
        if self._rpc_update_listing_field_available is not False:
            try:
                result = self.client.rpc("update_listing_field", {
                    "listing_id": draft_id,
                    "field_name": "price",
                    "field_value": price
                }).execute()
                if result.data:
                    self._rpc_update_listing_field_available = True
                    try:
                        await self.clear_pending_price_suggestion(draft_id)
                    except Exception:
                        pass
                    return True
            except Exception as e:
                self._maybe_disable_rpc_update_listing_field(e)
                if self._rpc_update_listing_field_available is not False:
                    logger.warning(f"RPC update_listing_field failed for price (falling back to direct update): {e}")

        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["price"] = price
            listing_data.pop("_pending_price_suggestion", None)
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.error(f"Error updating price: {e}")
            return False
    
    async def update_draft_category(self, draft_id: str, category: str, vision_product: Dict[str, Any] = None) -> bool:
        """Update draft category inside listing_data and optionally vision_product"""
        if self._rpc_update_listing_field_available is not False:
            try:
                rpc_result = self.client.rpc("update_listing_field", {
                    "listing_id": draft_id,
                    "field_name": "category",
                    "field_value": category
                }).execute()
                if rpc_result.data:
                    self._rpc_update_listing_field_available = True
                    if vision_product is not None:
                        self.client.table("active_drafts").update({
                            "vision_product": vision_product
                        }).eq("id", draft_id).execute()
                    return True
            except Exception as e:
                self._maybe_disable_rpc_update_listing_field(e)
                if self._rpc_update_listing_field_available is not False:
                    logger.warning(f"RPC update_listing_field failed for category (falling back to direct update): {e}")

        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["category"] = category

            payload: Dict[str, Any] = {"listing_data": listing_data}
            if vision_product is not None:
                payload["vision_product"] = vision_product

            updated = self.client.table("active_drafts").update(payload).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.error(f"Error updating category: {e}")
            return False

    async def update_draft_allow_no_images(self, draft_id: str, allow_no_images: bool) -> bool:
        """Persist user's preference to publish without images (listing_data.allow_no_images)."""
        if self._rpc_update_listing_field_available is not False:
            try:
                result = self.client.rpc("update_listing_field", {
                    "listing_id": draft_id,
                    "field_name": "allow_no_images",
                    "field_value": bool(allow_no_images)
                }).execute()
                if result.data:
                    self._rpc_update_listing_field_available = True
                    return True
            except Exception as e:
                self._maybe_disable_rpc_update_listing_field(e)
                if self._rpc_update_listing_field_available is not False:
                    logger.warning(f"RPC update_listing_field failed for allow_no_images (falling back to direct update): {e}")

        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["allow_no_images"] = bool(allow_no_images)
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.error(f"Error updating allow_no_images: {e}")
            return False

    async def update_draft_vision_product(self, draft_id: str, vision_product: Dict[str, Any]) -> bool:
        """Update draft vision_product without mutating listing_data/category."""
        try:
            if not draft_id:
                return False
            if not isinstance(vision_product, dict):
                return False
            updated = (
                self.client.table("active_drafts")
                .update({"vision_product": vision_product})
                .eq("id", draft_id)
                .execute()
            )
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Error updating vision_product: {e}")
            return False
    
    # Listing Images Operations
    async def add_listing_image(self, listing_id: str, image_url: str, metadata: Dict = None) -> bool:
        """
        Add image to draft (active_drafts.images) or to published listing (product_images/images).
        If listing_id refers to a draft, append to images array; otherwise insert to product_images.
        """
        try:
            metadata = metadata or {}
            normalized_new = self._normalize_image_entry({
                "image_url": image_url,
                "metadata": metadata
            })
            if not normalized_new:
                return False

            # Try draft first
            draft = await self.get_draft(listing_id)
            if draft:
                images = self._normalize_images(draft.get("images") or [])
                # Deduplicate: if the same URL already exists, update its metadata instead of appending.
                updated = False
                for img in images:
                    if img.get("image_url") == normalized_new["image_url"]:
                        merged_meta: Dict[str, Any] = {}
                        existing_meta = img.get("metadata")
                        if isinstance(existing_meta, dict):
                            merged_meta.update(existing_meta)
                        if metadata:
                            merged_meta.update(metadata)
                        img["metadata"] = merged_meta
                        updated = True
                        break
                if not updated:
                    images.append(normalized_new)
                result = self.client.table("active_drafts").update({
                    "images": images
                }).eq("id", listing_id).execute()
                return bool(result.data)
            
            # Otherwise treat as published listing
            self.client.table("product_images").insert({
                "listing_id": listing_id,
                "public_url": normalized_new["image_url"]
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error adding image: {e}")
            return False
    
    async def get_listing_images(self, listing_id: str) -> List[Dict[str, Any]]:
        """Get all images for a listing"""
        try:
            # Prefer the newer/production table when available.
            product_rows = (
                self.client.table("product_images")
                .select("public_url,storage_path,is_primary,display_order,file_size,mime_type,width,height,created_at")
                .eq("listing_id", listing_id)
                .order("display_order", desc=False)
                .execute()
            )
            if product_rows.data:
                normalized: List[Dict[str, Any]] = []
                for row in product_rows.data:
                    if not isinstance(row, dict):
                        continue
                    url = (row.get("public_url") or row.get("storage_path") or "").strip() if isinstance(row.get("public_url") or row.get("storage_path"), str) else ""
                    if not url:
                        continue

                    metadata: Dict[str, Any] = {}
                    for key in [
                        "storage_path",
                        "is_primary",
                        "display_order",
                        "file_size",
                        "mime_type",
                        "width",
                        "height",
                        "created_at",
                    ]:
                        if key in row and row.get(key) is not None:
                            metadata[key] = row.get(key)

                    normalized.append({"image_url": url, "metadata": metadata})
                return normalized

            # Backward-compat: older schema uses listing_images with (image_url, metadata)
            legacy_rows = (
                self.client.table("listing_images")
                .select("image_url,metadata,created_at")
                .eq("listing_id", listing_id)
                .execute()
            )
            images = self._normalize_images(legacy_rows.data or [])
            return images
        except Exception as e:
            logger.error(f"Error getting images: {e}")
            return []
    
    # Listings Operations
    async def publish_listing(self, draft_id: str, user_id: str, cost: int = 0) -> Optional[Dict[str, Any]]:
        """Publish a draft to listings table with wallet + audit flow."""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return None
            
            listing_data = draft.get("listing_data") or {}
            images = self._normalize_images(draft.get("images") or [])

            # Persist images in a frontend-compatible format.
            # - Some environments use listings.images as text[]
            # - Some use listings.images as jsonb
            # A plain list[str] works for both, while list[dict] breaks text[].
            image_urls: List[str] = []
            for img in images:
                if not isinstance(img, dict):
                    continue
                url = img.get("image_url")
                if isinstance(url, str) and url.strip():
                    image_urls.append(url.strip())
            primary_image_url = image_urls[0] if image_urls else None

            if cost > 0:
                balance = await self.get_wallet_balance(user_id)
                balance_int = int(balance) if balance is not None else None
                if balance_int is None or balance_int < cost:
                    raise InsufficientCreditsError(cost, balance_int)

            # Best-effort: generate listing-level metadata keywords to improve search recall.
            # This does NOT block publishing if generation fails.
            listing_metadata: Dict[str, Any] = {}
            try:
                if isinstance(listing_data, dict):
                    existing_keywords = listing_data.get("_keywords")
                else:
                    existing_keywords = None

                keywords: List[str] = []
                keywords_text = ""
                if isinstance(existing_keywords, list) and existing_keywords:
                    keywords = [str(k).strip().lower() for k in existing_keywords if str(k).strip()]
                    keywords_text = " ".join(keywords)
                else:
                    title = str(listing_data.get("title") or "").strip() if isinstance(listing_data, dict) else ""
                    category = str(listing_data.get("category") or "").strip() if isinstance(listing_data, dict) else ""
                    description = str(listing_data.get("description") or "").strip() if isinstance(listing_data, dict) else ""
                    condition = str(listing_data.get("condition") or "").strip() if isinstance(listing_data, dict) else ""
                    generated = await generate_listing_keywords(
                        title=title,
                        category=category,
                        description=description,
                        condition=condition,
                        vision_product=draft.get("vision_product") if isinstance(draft.get("vision_product"), dict) else None,
                    )
                    keywords = generated.get("keywords") or []
                    keywords_text = generated.get("keywords_text") or ""

                if keywords:
                    listing_metadata["keywords"] = keywords
                if keywords_text:
                    listing_metadata["keywords_text"] = keywords_text
            except Exception as meta_err:
                logger.warning(f"Failed to generate listing metadata: {meta_err}")

            # Deterministic fallback: ensure metadata is not empty even when OpenAI is unavailable.
            try:
                title_f = str(listing_data.get("title") or "").strip() if isinstance(listing_data, dict) else ""
                category_f = str(listing_data.get("category") or "").strip() if isinstance(listing_data, dict) else ""
                desc_f = str(listing_data.get("description") or "").strip() if isinstance(listing_data, dict) else ""
                if not listing_metadata.get("keywords") and title_f:
                    fallback = self._fallback_listing_keywords(title=title_f, category=category_f, description=desc_f)
                    if fallback.get("keywords"):
                        listing_metadata.update(fallback)
            except Exception:
                pass

            # Align with frontend fields used in listing cards.
            user_name = None
            user_phone = None
            try:
                user_name = await self.get_user_display_name(user_id)
            except Exception:
                user_name = None
            try:
                user_phone = await self.get_user_phone(user_id)
            except Exception:
                user_phone = None

            if not user_phone and isinstance(listing_data, dict):
                user_phone = (listing_data.get("contact_phone") or "").strip() or None

            # Add provenance to metadata so we can debug multi-channel write paths.
            listing_metadata.setdefault("source", "agent")
            listing_metadata.setdefault("created_via", "webchat")
            
            # Insert into listings
            result = self.client.table("listings").insert({
                "user_id": user_id,
                "title": listing_data.get("title"),
                "description": listing_data.get("description"),
                "price": listing_data.get("price"),
                "category": listing_data.get("category"),
                "user_name": user_name,
                "user_phone": user_phone,
                "status": "active",
                "image_url": primary_image_url,
                "images": image_urls,
                "metadata": listing_metadata
            }).execute()
            
            if result.data:
                listing_id = result.data[0]["id"]

                if cost > 0:
                    try:
                        await self.deduct_credits(user_id, cost, f"publish_listing:{listing_id}")
                    except Exception as wallet_err:
                        try:
                            self.client.table("listings").delete().eq("id", listing_id).execute()
                        except Exception as rollback_err:
                            logger.error(
                                f"Failed to rollback listing {listing_id} after wallet error: {rollback_err}"
                            )
                        raise wallet_err

                # Persist product_images records (only after wallet deduction succeeds)
                for url in image_urls:
                    try:
                        if not url:
                            continue
                        self.client.table("product_images").insert({
                            "listing_id": listing_id,
                            "public_url": url
                        }).execute()
                    except Exception as e:
                        logger.warning(f"Failed to copy image to product_images: {e}")

                # Delete draft
                self.client.table("active_drafts").delete().eq("id", draft_id).execute()
                
                await self.log_action(
                    action="publish_listing",
                    metadata={"draft_id": draft_id, "listing_id": listing_id},
                    resource_type="listing",
                    resource_id=listing_id,
                    user_id=user_id
                )
                
                return result.data[0]
            
            return None
        except InsufficientCreditsError:
            raise
        except Exception as e:
            logger.error(f"Error publishing listing: {e}")
            return None
    
    async def delete_listing(self, listing_id: str, user_id: Optional[str] = None) -> bool:
        """Delete a listing"""
        try:
            result = self.client.table("listings").delete().eq("id", listing_id).execute()
            if result.data:
                await self.log_action(
                    action="delete_listing",
                    metadata={"listing_id": listing_id},
                    resource_type="listing",
                    resource_id=listing_id,
                    user_id=user_id
                )
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting listing: {e}")
            return False
    
    async def search_listings(
        self, 
        category: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        search_text: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search listings with filters"""
        try:
            query = self.client.table("listings").select("*").eq("status", "active")
            
            if category:
                query = query.eq("category", category)
            
            if min_price is not None:
                query = query.gte("price", min_price)
            
            if max_price is not None:
                query = query.lte("price", max_price)
            
            if search_text:
                if getattr(settings, "enable_metadata_keyword_search", False):
                    # Also search in metadata keyword blob (best-effort) to improve recall.
                    # Use both the full phrase and a few tokens so queries like "telefon arıyorum"
                    # can still hit listings whose metadata contains "telefon".
                    clauses: List[str] = [
                        f"title.ilike.%{search_text}%",
                        f"description.ilike.%{search_text}%",
                    ]

                    tokens = [t for t in re.findall(r"[0-9a-zA-ZçğıöşüÇĞİÖŞÜ]+", search_text.lower()) if len(t) >= 3]
                    # Keep it bounded so the OR string doesn't explode
                    for tok in tokens[:4]:
                        clauses.append(f"metadata->>keywords_text.ilike.%{tok}%")

                    # Still include the full phrase as a fallback when it makes sense
                    clauses.append(f"metadata->>keywords_text.ilike.%{search_text}%")

                    query = query.or_(",".join(clauses))
                else:
                    query = query.or_(f"title.ilike.%{search_text}%,description.ilike.%{search_text}%")
            
            result = query.limit(limit).execute()
            rows = result.data or []

            # Normalize image_url/images for frontend + chat rendering.
            # - Ensure image_url is a usable public URL
            # - Ensure images is a list[str] of usable public URLs (no metadata objects)
            normalized_rows: List[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue

                # Collect URLs from both image_url and images fields
                urls: List[str] = []
                primary = self._extract_image_url(row.get("image_url"))
                if primary:
                    urls.append(primary)

                images_field = row.get("images")
                parsed_images: Any = images_field
                # Some schemas store images as a JSON string; attempt to parse.
                if isinstance(images_field, str):
                    s = images_field.strip()
                    if s:
                        try:
                            parsed_images = json.loads(s)
                        except Exception:
                            parsed_images = images_field

                if isinstance(parsed_images, list):
                    for img in parsed_images:
                        u = self._extract_image_url(img)
                        if u:
                            urls.append(u)
                else:
                    # If still a string (possibly noisy JSON/markdown), try extracting a URL.
                    u = self._extract_image_url(parsed_images)
                    if u:
                        urls.append(u)

                # Dedup, preserve order
                seen: set[str] = set()
                clean_urls: List[str] = []
                for u in urls:
                    if isinstance(u, str):
                        uu = u.strip()
                        if uu and uu not in seen:
                            clean_urls.append(uu)
                            seen.add(uu)

                # Final normalize via _normalize_image_entry/to_public_url_if_needed
                # (handles storage-path -> public URL conversion)
                final_urls: List[str] = []
                for u in clean_urls:
                    norm = self._normalize_image_entry(u)
                    if norm and norm.get("image_url"):
                        final_urls.append(str(norm["image_url"]))

                if final_urls:
                    row["image_url"] = final_urls[0]
                    row["images"] = final_urls
                else:
                    # Keep a consistent type for callers
                    row["images"] = []

                normalized_rows.append(row)

            return normalized_rows
        except Exception as e:
            logger.error(f"Error searching listings: {e}")
            return []
    
    # Wallet Operations
    async def get_wallet_balance(self, user_id: str) -> Optional[float]:
        """Get user wallet balance"""
        try:
            result = self.client.table("wallets").select("balance_bigint").eq("user_id", user_id).execute()
            return result.data[0]["balance_bigint"] if result.data else None
        except Exception as e:
            logger.error(f"Error getting wallet balance: {e}")
            return None
    
    async def deduct_credits(self, user_id: str, amount: int, description: str) -> bool:
        """Deduct credits from user wallet and record transaction"""
        try:
            balance = await self.get_wallet_balance(user_id)
            balance_int = int(balance) if balance is not None else None
            if balance_int is None or balance_int < amount:
                raise InsufficientCreditsError(amount, balance_int)

            new_balance = balance_int - amount
            result = (
                self.client.table("wallets")
                .update({"balance_bigint": new_balance})
                .eq("user_id", user_id)
                .execute()
            )

            if not result.data:
                raise RuntimeError("Wallet balance update failed")

            # Best-effort: record the transaction. Some Supabase deployments enforce a CHECK constraint
            # on wallet_transactions.kind (e.g., allowed enum values differ by environment). We should
            # not fail a publish after the wallet balance is already updated.
            tx_payload_base = {
                "user_id": user_id,
                "amount_bigint": -amount,
                "reference": description,
                "metadata": {},
            }
            tx_kinds_to_try = [
                "debit",  # preferred
                "spend",
                "usage",
                "credit",  # fallback for environments that only allow 'credit'/'debit' variants
            ]
            inserted = False
            last_err: Exception | None = None
            for kind in tx_kinds_to_try:
                try:
                    payload = dict(tx_payload_base)
                    payload["kind"] = kind
                    self.client.table("wallet_transactions").insert(payload).execute()
                    inserted = True
                    break
                except Exception as e:
                    last_err = e
                    continue
            if not inserted:
                logger.warning(f"wallet_transactions insert failed (continuing): {last_err}")

            await self.log_action(
                action="deduct_credits",
                metadata={"amount": amount, "description": description},
                resource_type="wallet",
                resource_id=user_id,
                user_id=user_id
            )
            return True
        except InsufficientCreditsError:
            raise
        except Exception as e:
            logger.error(f"Error deducting credits: {e}")
            raise
    
    # Audit Logging
    async def log_action(
        self,
        action: str,
        metadata: Dict[str, Any],
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> bool:
        """Log agent action to audit_logs (schema-aligned)."""
        try:
            phone: Optional[str] = None
            if isinstance(metadata, dict):
                phone = (metadata.get("phone") or metadata.get("contact_phone") or "").strip() or None

            if not phone and user_id:
                phone = await self.get_user_phone(user_id)

            # Some environments enforce NOT NULL on audit_logs.phone; keep inserts safe.
            phone = phone or ""

            payload = {
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "user_id": user_id,
                "phone": phone,
                "metadata": metadata
            }
            result = self.client.table("audit_logs").insert(payload).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error logging action: {e}")
            return False

    async def get_market_price_data(self, product_key: Optional[str] = None, category: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Fetch market price snapshots for search composer."""
        try:
            query = self.client.table("market_price_snapshots").select("*")
            if product_key:
                query = query.ilike("product_key", f"%{product_key}%")
            if category:
                query = query.eq("category", category)
            result = query.limit(limit).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching market price data: {e}")
            return []

    async def _call_edge_function(self, function_name: str, payload: Dict[str, Any], timeout_s: int = 30) -> Dict[str, Any]:
        """Call a Supabase Edge Function.

        Uses service role key to avoid RLS/Auth issues. Function URL pattern:
        {SUPABASE_URL}/functions/v1/{function_name}
        """
        url = f"{settings.supabase_url.rstrip('/')}/functions/v1/{function_name}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "apikey": settings.supabase_key,
        }

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
                # Some deployments return non-JSON on errors
                if resp.status_code >= 400:
                    return {"success": False, "status": resp.status_code, "error": resp.text}
                try:
                    return resp.json()
                except Exception:
                    return {"success": False, "status": resp.status_code, "error": "non_json_response", "raw": resp.text}
        except Exception as e:
            logger.error(f"Edge function call failed ({function_name}): {e}")
            return {"success": False, "error": str(e)}

    async def suggest_price_cached(
        self,
        title: str,
        category: str,
        description: Optional[str] = None,
        condition: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get a price suggestion using the cached Perplexity pipeline.

        This delegates caching/TTL/query logging to the `ai-assistant-cached` edge function.
        It will:
        - return cache hit if snapshot exists and not expired
        - otherwise call Perplexity and upsert into `market_price_snapshots`
        """
        payload = {
            "action": "suggest_price",
            "category": category or "Diğer",
            "title": title or "",
            "description": description or "",
            "condition": condition or "İyi Durumda",
        }
        return await self._call_edge_function("ai-assistant-cached", payload)


# Global instance
supabase_client = SupabaseClient()
