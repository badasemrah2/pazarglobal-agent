"""
Supabase client for database operations
"""
from supabase import create_client, Client
from config import settings
from typing import Optional, Dict, List, Any, cast
from datetime import datetime, timezone
from loguru import logger
import httpx
import re
import json

from .metadata_keywords import generate_listing_keywords
from services.text_normalization import violates_listing_content_guard


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
        self._rpc_update_listing_field_invalid_fields: set[str] = set()
        self._wallet_transactions_disabled: bool = False
        self._wallet_transactions_disabled_logged: bool = False

    def _coerce_str(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return str(value)
        except Exception:
            return ""

    def _first_dict(self, data: Any) -> Optional[Dict[str, Any]]:
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                return cast(Dict[str, Any], first)
        return None

    def _list_of_dicts(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [cast(Dict[str, Any], row) for row in data if isinstance(row, dict)]
        return []

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

    def _rpc_update_listing_field_is_invalid_field(self, exc: Exception, field_name: str) -> bool:
        msg = str(exc) if exc is not None else ""
        msg_l = msg.lower()
        field_l = (field_name or "").strip().lower()
        return bool(field_l) and ("invalid field_name" in msg_l and field_l in msg_l)
    
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
    
    async def set_user_context(self, user_id: str) -> bool:
        """
        Set user context for Row Level Security (RLS) policies.
        Call this before database operations to enforce ownership at database level.
        
        This provides defense-in-depth: even if application code forgets to check
        user_id, the database will enforce ownership via RLS policies.
        """
        try:
            # Set PostgreSQL session variable for RLS policies
            self.client.rpc('set_user_context', {'p_user_id': user_id}).execute()
            return True
        except Exception as e:
            logger.warning(f"Failed to set user context for RLS: {e}")
            # Don't fail the operation - application-level checks still work
            return False

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
            row = self._first_dict(result.data)
            if not row:
                return None
            name = self._coerce_str(row.get("display_name") or row.get("full_name")).strip()
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
            row = self._first_dict(result.data)
            if not row:
                return None
            phone = self._coerce_str(row.get("phone")).strip()
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
                draft = self._first_dict(existing.data)
                if draft:
                    draft_id = self._coerce_str(draft.get("id"))
                    if draft.get("state") != "in_progress" and draft_id:
                        try:
                            self.client.table("active_drafts").update({
                                "state": "in_progress"
                            }).eq("id", draft_id).execute()
                            draft["state"] = "in_progress"
                        except Exception as state_err:
                            logger.warning(f"Failed to refresh draft state for {draft_id}: {state_err}")
                    logger.info(f"Reusing existing draft {draft_id or 'unknown'} for user {user_id}")
                    return draft

            listing_data: Dict[str, Any] = {
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
            
            created = self._first_dict(result.data)
            if created:
                logger.info(f"Created draft: {self._coerce_str(created.get('id'))}")
                return created
            
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
                    fallback_row = self._first_dict(fallback.data)
                    if fallback_row:
                        return fallback_row
            logger.error(f"Error creating draft: {e}")
            raise

    async def reset_draft(self, draft_id: str, phone_number: Optional[str] = None) -> bool:
        """Reset an existing draft to a clean state.

        This is used when the platform enforces a single in-progress draft per user,
        but the user is clearly starting a brand-new listing flow.
        
        FIX Problem 2: Temizlik kapsamı genişletildi:
        - listing_data tüm fieldları
        - vision_product (önceki resim analizi kalmasın)
        - images array
        - metadata (buffered_media temizleniyor)
        """
        try:
            listing_data: Dict[str, Any] = {
                "title": None,
                "description": None,
                "price": None,
                "category": None,
                "location": None,  # FIX: Eklendi
                "condition": None,  # FIX: Eklendi
            }
            if phone_number:
                listing_data["contact_phone"] = phone_number

            payload = {
                "state": "in_progress",
                "listing_data": listing_data,
                "images": [],
                "vision_product": {},
                "metadata": {}  # FIX: Metadata temizleniyor (buffered_media vb.)
            }

            try:
                result = self.client.table("active_drafts").update(payload).eq("id", draft_id).execute()
                return bool(result.data)
            except Exception as e:
                error_text = str(e)
                if "metadata" in error_text and "PGRST204" in error_text:
                    payload.pop("metadata", None)
                    result = self.client.table("active_drafts").update(payload).eq("id", draft_id).execute()
                    return bool(result.data)
                raise
        except Exception as e:
            logger.error(f"Error resetting draft: {e}")
            return False

    async def mark_draft_abandoned(
        self,
        draft_id: str,
        source: Optional[str] = None,
        reason: Optional[str] = None
    ) -> bool:
        """Mark an in-progress draft as abandoned (keeps data for potential resume/cleanup)."""
        if not draft_id:
            return False
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["_abandoned_at"] = datetime.now(timezone.utc).isoformat()
            if source:
                listing_data["_abandoned_source"] = source
            if reason:
                listing_data["_abandoned_reason"] = reason

            payload = {
                "state": "abandoned",
                "listing_data": listing_data,
            }
            updated = (
                self.client.table("active_drafts")
                .update(payload)
                .eq("id", draft_id)
                .execute()
            )
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Failed to mark draft abandoned: {e}")
            return False

    async def delete_draft(self, draft_id: str) -> bool:
        """Hard-delete a draft from active_drafts."""
        if not draft_id:
            return False
        try:
            result = self.client.table("active_drafts").delete().eq("id", draft_id).execute()
            return bool(result.data)
        except Exception as e:
            logger.warning(f"Failed to delete draft {draft_id}: {e}")
            return False

    async def clear_draft_abandoned(self, draft_id: str) -> bool:
        """Clear abandoned flags and mark draft back in progress."""
        if not draft_id:
            return False
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data.pop("_abandoned_at", None)
            listing_data.pop("_abandoned_source", None)
            listing_data.pop("_abandoned_reason", None)

            payload = {
                "state": "in_progress",
                "listing_data": listing_data,
            }
            updated = (
                self.client.table("active_drafts")
                .update(payload)
                .eq("id", draft_id)
                .execute()
            )
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Failed to clear abandoned flags for draft {draft_id}: {e}")
            return False
    
    async def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Get draft by ID"""
        try:
            result = self.client.table("active_drafts").select("*").eq("id", draft_id).execute()
            return self._first_dict(result.data)
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
            return self._first_dict(result.data)
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

    async def set_draft_listing_data_flag(self, draft_id: str, flag: str, value: Any = True) -> bool:
        """Persist an internal flag under listing_data.

        Used to make preview-stage behaviors deterministic across stateless instances.
        """
        if not draft_id or not flag:
            return False
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data[str(flag)] = value
            updated = (
                self.client.table("active_drafts")
                .update({"listing_data": listing_data})
                .eq("id", draft_id)
                .execute()
            )
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Failed to persist draft listing_data flag '{flag}': {e}")
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
    
    async def update_draft_title(self, draft_id: str, title: str, user_id: Optional[str] = None) -> bool:
        """Update draft title inside listing_data"""
        candidate = (title or "").strip()
        if candidate and violates_listing_content_guard(candidate):
            logger.info(f"Title guard skipped suspicious payload for draft {draft_id}")
            return True
        
        # Security: Verify draft ownership before update
        if user_id:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            draft_owner_id = draft.get("user_id")
            if str(draft_owner_id) != str(user_id):
                logger.warning(
                    f"User {user_id} attempted to update title of draft {draft_id} owned by {draft_owner_id}"
                )
                return False
        
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
    
    async def update_draft_description(self, draft_id: str, description: str, user_id: Optional[str] = None) -> bool:
        """Update draft description inside listing_data"""
        candidate = (description or "").strip()
        if candidate and violates_listing_content_guard(candidate):
            logger.info(f"Description guard skipped suspicious payload for draft {draft_id}")
            return True
        
        # Security: Verify draft ownership before update
        if user_id:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            draft_owner_id = draft.get("user_id")
            if str(draft_owner_id) != str(user_id):
                logger.warning(
                    f"User {user_id} attempted to update description of draft {draft_id} owned by {draft_owner_id}"
                )
                return False
        
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
    
    async def update_draft_price(self, draft_id: str, price: float, user_id: Optional[str] = None) -> bool:
        """Update draft price inside listing_data"""
        # Security: Verify draft ownership before update
        if user_id:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            draft_owner_id = draft.get("user_id")
            if str(draft_owner_id) != str(user_id):
                logger.warning(
                    f"User {user_id} attempted to update price of draft {draft_id} owned by {draft_owner_id}"
                )
                return False
        
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
    
    async def update_draft_category(self, draft_id: str, category: str, vision_product: Optional[Dict[str, Any]] = None, user_id: Optional[str] = None) -> bool:
        """Update draft category inside listing_data and optionally vision_product"""
        # Security: Verify draft ownership before update
        if user_id:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            draft_owner_id = draft.get("user_id")
            if str(draft_owner_id) != str(user_id):
                logger.warning(
                    f"User {user_id} attempted to update category of draft {draft_id} owned by {draft_owner_id}"
                )
                return False
        
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

    async def update_draft_location(self, draft_id: str, location: str, user_id: Optional[str] = None) -> bool:
        """Update draft location inside listing_data."""
        # Security: Verify draft ownership before update
        if user_id:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            draft_owner_id = draft.get("user_id")
            if str(draft_owner_id) != str(user_id):
                logger.warning(
                    f"User {user_id} attempted to update location of draft {draft_id} owned by {draft_owner_id}"
                )
                return False
        
        if self._rpc_update_listing_field_available is not False and "location" not in self._rpc_update_listing_field_invalid_fields:
            try:
                result = self.client.rpc("update_listing_field", {
                    "listing_id": draft_id,
                    "field_name": "location",
                    "field_value": location
                }).execute()
                if result.data:
                    self._rpc_update_listing_field_available = True
                    return True
            except Exception as e:
                self._maybe_disable_rpc_update_listing_field(e)
                if self._rpc_update_listing_field_is_invalid_field(e, "location"):
                    self._rpc_update_listing_field_invalid_fields.add("location")
                elif self._rpc_update_listing_field_available is not False:
                    logger.warning(f"RPC update_listing_field failed for location (falling back to direct update): {e}")

        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["location"] = location
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.error(f"Error updating location: {e}")
            return False

    async def update_draft_condition(self, draft_id: str, condition: str, user_id: Optional[str] = None) -> bool:
        """Update draft condition inside listing_data.
        
        Security: Verify draft ownership before update
        Best-effort: some deployments may not support this field in the RPC.
        """
        if user_id:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            draft_owner_id = draft.get("user_id")
            if str(draft_owner_id) != str(user_id):
                logger.warning(
                    f"User {user_id} attempted to update condition of draft {draft_id} owned by {draft_owner_id}"
                )
                return False

        if self._rpc_update_listing_field_available is not False and "condition" not in self._rpc_update_listing_field_invalid_fields:
            try:
                result = self.client.rpc("update_listing_field", {
                    "listing_id": draft_id,
                    "field_name": "condition",
                    "field_value": condition
                }).execute()
                if result.data:
                    self._rpc_update_listing_field_available = True
                    return True
            except Exception as e:
                self._maybe_disable_rpc_update_listing_field(e)
                if self._rpc_update_listing_field_is_invalid_field(e, "condition"):
                    self._rpc_update_listing_field_invalid_fields.add("condition")
                elif self._rpc_update_listing_field_available is not False:
                    logger.warning(f"RPC update_listing_field failed for condition (falling back to direct update): {e}")

        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data["condition"] = condition
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.error(f"Error updating condition: {e}")
            return False

    async def set_buffered_media(self, draft_id: str, media_urls: List[str], analyses: List[Dict[str, Any]]) -> bool:
        """Persist image-first buffered media to draft.listing_data.

        This intentionally does NOT write to active_drafts.images. It is used to survive
        non-sticky sessions when Redis is disabled.
        """
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}

            safe_urls: List[str] = []
            for u in media_urls or []:
                if isinstance(u, str) and u.strip():
                    safe_urls.append(u.strip())
            # keep max 5 buffered URLs
            safe_urls = list(dict.fromkeys(safe_urls))[:5]

            safe_analyses: List[Dict[str, Any]] = []
            if isinstance(analyses, list):
                for a in analyses[:5]:
                    if isinstance(a, dict) and a.get("image_url"):
                        safe_analyses.append(a)

            listing_data["_buffered_media_urls"] = safe_urls
            listing_data["_buffered_media_analysis"] = safe_analyses
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Error setting buffered media: {e}")
            return False

    async def clear_buffered_media(self, draft_id: str) -> bool:
        """Remove buffered media keys from draft.listing_data."""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            listing_data.pop("_buffered_media_urls", None)
            listing_data.pop("_buffered_media_analysis", None)
            updated = self.client.table("active_drafts").update({
                "listing_data": listing_data,
            }).eq("id", draft_id).execute()
            return bool(updated.data)
        except Exception as e:
            logger.warning(f"Error clearing buffered media: {e}")
            return False

    async def update_draft_allow_no_images(self, draft_id: str, allow_no_images: bool) -> bool:
        # Persist user's preference to publish without images (listing_data.allow_no_images).
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
    async def add_listing_image(self, listing_id: str, image_url: str, metadata: Optional[Dict] = None) -> bool:
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
                    url = self._coerce_str(row.get("public_url") or row.get("storage_path")).strip()
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
            
            # Security: Verify draft ownership before publishing
            draft_owner_id = draft.get("user_id")
            if str(draft_owner_id) != str(user_id):
                logger.warning(
                    f"User {user_id} attempted to publish draft {draft_id} owned by {draft_owner_id}"
                )
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

            # Normalize condition and try to capture market price snapshot for publish-time context.
            condition_raw = str(listing_data.get("condition") or "").strip() if isinstance(listing_data, dict) else ""
            try:
                from services.text_normalization import normalize_for_match, canonicalize_condition
            except Exception:
                normalize_for_match = None
                canonicalize_condition = None

            condition_canonical = None
            if callable(canonicalize_condition):
                condition_canonical = canonicalize_condition(condition_raw)
            if condition_canonical is None:
                condition_canonical = condition_raw or None

            listing_title = str(listing_data.get("title") or "").strip() if isinstance(listing_data, dict) else ""
            category_for_price = str(listing_data.get("category") or "").strip() if isinstance(listing_data, dict) else ""

            market_price_at_publish = None
            product_key = ""
            if callable(normalize_for_match):
                product_key = normalize_for_match(" ".join([listing_title, category_for_price, condition_canonical or ""]).strip())
            elif listing_title:
                product_key = listing_title.lower().strip()

            if product_key or listing_title:
                try:
                    price_query = self.client.table("market_price_snapshots").select(
                        "avg_price,min_price,max_price,product_key,category,last_updated_at,created_at"
                    )
                    if product_key:
                        price_query = price_query.ilike("product_key", f"%{product_key}%")
                    else:
                        price_query = price_query.ilike("product_key", f"%{listing_title}%")
                    if category_for_price:
                        price_query = price_query.eq("category", category_for_price)
                    price_query = price_query.order("last_updated_at", desc=True).limit(1)
                    price_result = price_query.execute()
                    price_row = self._first_dict(price_result.data)
                    if price_row:
                        market_price_at_publish = (
                            price_row.get("avg_price")
                            or price_row.get("min_price")
                            or price_row.get("max_price")
                        )
                except Exception as price_err:
                    logger.warning(f"Failed to read market_price_snapshots for publish: {price_err}")

            if cost > 0:
                balance = await self.get_wallet_balance(user_id)
                balance_int = int(balance) if balance is not None else None
                if balance_int is None or balance_int < cost:
                    raise InsufficientCreditsError(cost, balance_int)

            # Best-effort: generate listing-level metadata keywords to improve search recall.
            # This does NOT block publishing if generation fails.
            # STANDARDIZED METADATA FORMAT (same as frontend):
            # {
            #   "source": "agent" | "web",
            #   "created_via": "webchat" | "whatsapp" | "manual",
            #   "client_app": "pazarglobal-agent",
            #   "flow_version": "2026-01-18",
            #   "keyword_source": "llm" | "fallback" | "existing",
            #   "created_at_client": ISO timestamp,
            #   "keywords": [...],
            #   "keywords_text": "...",
            #   "attributes": {}
            # }
            from datetime import datetime, timezone
            
            listing_metadata: Dict[str, Any] = {
                "source": "agent",
                "created_via": "webchat",
                "client_app": "pazarglobal-agent",
                "flow_version": "2026-01-18",
                "keyword_source": "llm",  # will be updated below
                "created_at_client": datetime.now(timezone.utc).isoformat(),
                "attributes": {},
            }
            
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
                    listing_metadata["keyword_source"] = "existing"
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
                    listing_metadata["keyword_source"] = "llm"

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
                        listing_metadata["keywords"] = fallback["keywords"]
                        listing_metadata["keywords_text"] = fallback.get("keywords_text", "")
                        listing_metadata["keyword_source"] = "fallback"
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
                user_phone = self._coerce_str(listing_data.get("contact_phone")).strip() or None

            # Metadata already initialized with standard format above
            
            # Insert into listings
            result = self.client.table("listings").insert({
                "id": draft_id,
                "user_id": user_id,
                "title": listing_data.get("title"),
                "description": listing_data.get("description"),
                "price": listing_data.get("price"),
                "category": listing_data.get("category"),
                "condition": condition_canonical,
                "location": listing_data.get("location") if isinstance(listing_data, dict) else None,
                "user_name": user_name,
                "user_phone": user_phone,
                "status": "active",
                "image_url": primary_image_url,
                "images": image_urls,
                "metadata": listing_metadata,
                "market_price_at_publish": market_price_at_publish,
            }).execute()
            
            result_row = self._first_dict(result.data)
            if result_row:
                listing_id = self._coerce_str(result_row.get("id"))
                if not listing_id:
                    logger.error("Publish listing succeeded but returned empty id")
                    return None

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
                        storage_path = ""
                        try:
                            marker = "/storage/v1/object/public/"
                            if marker in url:
                                storage_path = url.split(marker, 1)[1]
                            else:
                                storage_path = url
                        except Exception:
                            storage_path = url
                        self.client.table("product_images").insert({
                            "listing_id": listing_id,
                            "public_url": url,
                            "storage_path": storage_path or url,
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
                
                return result_row
            
            return None
        except InsufficientCreditsError:
            raise
        except Exception as e:
            logger.error(f"Error publishing listing: {e}")
            return None
    
    async def delete_listing(self, listing_id: str, user_id: Optional[str] = None) -> bool:
        """Delete a listing (with ownership verification)"""
        try:
            # SECURITY: Verify ownership before deletion
            if user_id:
                # First, fetch the listing to verify ownership
                listing = self.client.table("listings").select("user_id").eq("id", listing_id).execute()
                listing_row = self._first_dict(listing.data)
                if not listing_row:
                    logger.warning(f"Listing {listing_id} not found for deletion")
                    return False
                
                listing_owner = listing_row.get("user_id")
                if str(listing_owner) != str(user_id):
                    logger.warning(f"User {user_id} attempted to delete listing {listing_id} owned by {listing_owner}")
                    return False
            
            # Ownership verified - use HTTP DELETE directly (Python SDK delete doesn't work reliably)
            headers = {
                "apikey": settings.supabase_service_key,
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "Prefer": "return=representation"
            }
            
            url = f"{settings.supabase_url}/rest/v1/listings?id=eq.{listing_id}"
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.delete(url, headers=headers)
                
                logger.info(f"Delete listing HTTP response: status={response.status_code}, body={response.text[:200]}")
                
                if response.status_code in [200, 204]:
                    await self.log_action(
                        action="delete_listing",
                        metadata={"listing_id": listing_id},
                        resource_type="listing",
                        resource_id=listing_id,
                        user_id=user_id
                    )
                    return True
                else:
                    logger.error(f"Delete failed: {response.status_code} - {response.text}")
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
            # Some deployments have legacy rows with NULL or 'published' status.
            # Treat them as visible to avoid silently missing listings.
            query = self.client.table("listings").select("*").or_("status.eq.active,status.eq.published,status.is.null")
            
            if category:
                query = query.eq("category", category)
            
            if min_price is not None:
                query = query.gte("price", min_price)
            
            if max_price is not None:
                query = query.lte("price", max_price)
            
            if search_text:
                if getattr(settings, "enable_metadata_keyword_search", False):
                    # Multi-word search strategy: search each token individually
                    # "nike ayakkabı" should match "Nike koşu ayakkabısı" (both words present)
                    tokens = [t for t in re.findall(r"[0-9a-zA-ZçğıöşüÇĞİÖŞÜ]+", search_text.lower()) if len(t) >= 2]
                    
                    if len(tokens) >= 2:
                        # Multi-word query: search ALL tokens (AND logic via multiple OR clauses per token)
                        # Each token must be present in title OR description OR keywords
                        # This allows "nike ayakkabı" to match "Nike koşu ayakkabısı"
                        clauses: List[str] = []
                        for tok in tokens[:5]:  # Limit to 5 tokens
                            clauses.append(f"title.ilike.%{tok}%")
                            clauses.append(f"description.ilike.%{tok}%")
                            clauses.append(f"metadata->>keywords_text.ilike.%{tok}%")
                        # Also try full phrase for exact matches (bonus)
                        clauses.append(f"title.ilike.%{search_text}%")
                        clauses.append(f"metadata->>keywords_text.ilike.%{search_text}%")
                        query = query.or_(",".join(clauses))
                    else:
                        # Single-word query: use broader matching for better recall
                        # This ensures "laptop" matches "Dell Laptop", "dizüstü" matches "dizüstü bilgisayar"
                        clauses: List[str] = [
                            f"title.ilike.%{search_text}%",
                            f"description.ilike.%{search_text}%",
                            f"category.ilike.%{search_text}%",  # Added: search in category too
                        ]
                        # Add individual token search for single words (broad recall)
                        for tok in tokens[:4]:
                            clauses.append(f"title.ilike.%{tok}%")
                            clauses.append(f"description.ilike.%{tok}%")
                            clauses.append(f"metadata->>keywords_text.ilike.%{tok}%")
                        clauses.append(f"metadata->>keywords_text.ilike.%{search_text}%")
                        query = query.or_(",".join(clauses))
                else:
                    # Legacy fallback: title/description only (less accurate but faster)
                    query = query.or_(f"title.ilike.%{search_text}%,description.ilike.%{search_text}%")
            
            result = query.limit(limit).execute()
            rows = self._list_of_dicts(result.data)

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
            row = self._first_dict(result.data)
            if not row:
                return None
            balance_value = row.get("balance_bigint")
            if balance_value is None:
                return None
            try:
                return float(balance_value)
            except Exception:
                return None
        except Exception as e:
            logger.error(f"Error getting wallet balance: {e}")
            return None

    async def get_wallet_transactions(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch latest wallet transactions for a user (best-effort)."""
        try:
            result = (
                self.client.table("wallet_transactions")
                .select("amount_bigint, reference, kind, created_at, metadata")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(max(1, min(limit, 50)))
                .execute()
            )
            return self._list_of_dicts(result.data)
        except Exception as e:
            # Some deployments may not have wallet_transactions table; fail-soft.
            logger.warning(f"Wallet transactions unavailable: {e}")
            return []
    
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
            if not self._wallet_transactions_disabled:
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
                    self._wallet_transactions_disabled = True
                    if not self._wallet_transactions_disabled_logged:
                        logger.warning(f"wallet_transactions insert failed (disabling future inserts; continuing): {last_err}")
                        self._wallet_transactions_disabled_logged = True

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
            return self._list_of_dicts(result.data)
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
        vision: Optional[Dict[str, Any]] = None,
        user_claim: Optional[str] = None,
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
            "condition": condition or "2. El",
            "vision": vision or None,
            "user_claim": user_claim or "",
        }
        return await self._call_edge_function("ai-assistant-cached", payload)


# Global instance
supabase_client = SupabaseClient()
