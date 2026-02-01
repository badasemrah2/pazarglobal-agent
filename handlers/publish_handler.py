"""
Publish Handler - Listing lifecycle management

Handles:
- Publishing draft to live listing
- Deleting listings
- Updating listing status (sold, archived)
"""
from typing import Dict, Any, Optional

from core.response_builder import ResponseBuilder, Response, Button, create_builder

from services.supabase_client import supabase_client
from services.logger import get_logger

logger = get_logger(__name__)


class PublishHandler:
    """
    Publish/delete handler for listings.
    """
    
    def __init__(self):
        self.response_builder: Optional[ResponseBuilder] = None
        self.supabase = None
    
    async def initialize(self, channel: str = "webchat"):
        """Lazy initialization"""
        self.response_builder = create_builder(channel)
        self.supabase = supabase_client
    
    async def publish_draft(
        self,
        user_id: str,
        draft_id: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Publish a draft to live listings.
        
        Args:
            user_id: User identifier
            draft_id: Draft to publish
            channel: Communication channel
        
        Returns:
            Response with publish result
        """
        await self.initialize(channel)
        
        logger.info(f"Publishing draft: user={user_id}, draft={draft_id}")
        
        try:
            # 1. Get draft
            draft_result = await self.supabase.table("active_drafts")\
                .select("*")\
                .eq("id", draft_id)\
                .eq("user_id", user_id)\
                .execute()
            
            if not draft_result.data:
                return self.response_builder.build_custom(
                    "⚠️ İlan taslağı bulunamadı."
                )
            
            draft = draft_result.data[0]
            listing_data = draft.get("listing_data") or {}
            if not isinstance(listing_data, dict):
                listing_data = {}
            images = draft.get("images") or []
            
            # 2. Validate required fields
            if not listing_data.get("title") or not listing_data.get("price") or not images:
                missing = []
                if not listing_data.get("title"):
                    missing.append("başlık")
                if not listing_data.get("price"):
                    missing.append("fiyat")
                if not images:
                    missing.append("görsel")
                
                return self.response_builder.build_custom(
                    f"⚠️ İlan yayınlamak için şunlar gerekli: {', '.join(missing)}"
                )
            
            # 3. Check wallet (if credit system enabled)
            # wallet_ok = await self._check_wallet(user_id)
            # if not wallet_ok:
            #     return self.response_builder.build_custom("⚠️ Yetersiz kredi.")
            
            # 4. Publish via shared flow (handles market_price_at_publish, product_images, audit)
            result_row = await self.supabase.publish_listing(draft_id, user_id, cost=0)
            listing_id = result_row.get("id") if isinstance(result_row, dict) else None
            
            # 6. Build response
            url = f"https://pazarglobal.com/listing/{listing_id}" if listing_id else ""
            
            return self.response_builder.build(
                "listing_published",
                format_args={"url": url},
                metadata={"listing_id": listing_id, "status": "published"},
            )
        
        except Exception as e:
            logger.error(f"Publish error: {e}")
            return self.response_builder.build("error_generic")
    
    async def delete_listing(
        self,
        user_id: str,
        listing_id: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Delete a listing.
        
        Args:
            user_id: User identifier
            listing_id: Listing to delete
            channel: Communication channel
        
        Returns:
            Response with delete result
        """
        await self.initialize(channel)
        
        logger.info(f"Deleting listing: user={user_id}, listing={listing_id}")
        
        try:
            # Verify ownership
            listing_result = await self.supabase.table("listings")\
                .select("id, user_id, title")\
                .eq("id", listing_id)\
                .execute()
            
            if not listing_result.data:
                return self.response_builder.build_custom("⚠️ İlan bulunamadı.")
            
            listing = listing_result.data[0]
            
            if listing.get("user_id") != user_id:
                return self.response_builder.build_custom("⚠️ Bu ilan size ait değil.")
            
            # Delete (soft delete - set status to deleted)
            await self.supabase.table("listings")\
                .update({"status": "deleted"})\
                .eq("id", listing_id)\
                .execute()
            
            title = listing.get("title", "İlan")
            return self.response_builder.build_custom(
                f"🗑️ **{title}** silindi.",
                metadata={"listing_id": listing_id, "status": "deleted"},
            )
        
        except Exception as e:
            logger.error(f"Delete error: {e}")
            return self.response_builder.build("error_generic")
    
    async def mark_sold(
        self,
        user_id: str,
        listing_id: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Mark listing as sold.
        
        Args:
            user_id: User identifier
            listing_id: Listing to mark
            channel: Communication channel
        
        Returns:
            Response with result
        """
        await self.initialize(channel)
        
        logger.info(f"Marking sold: user={user_id}, listing={listing_id}")
        
        try:
            # Verify ownership
            listing_result = await self.supabase.table("listings")\
                .select("id, user_id, title")\
                .eq("id", listing_id)\
                .eq("user_id", user_id)\
                .execute()
            
            if not listing_result.data:
                return self.response_builder.build_custom("⚠️ İlan bulunamadı veya size ait değil.")
            
            listing = listing_result.data[0]
            
            # Update status
            await self.supabase.table("listings")\
                .update({"status": "sold"})\
                .eq("id", listing_id)\
                .execute()
            
            title = listing.get("title", "İlan")
            return self.response_builder.build_custom(
                f"✅ **{title}** satıldı olarak işaretlendi.",
                metadata={"listing_id": listing_id, "status": "sold"},
            )
        
        except Exception as e:
            logger.error(f"Mark sold error: {e}")
            return self.response_builder.build("error_generic")
    
    async def get_my_listings(
        self,
        user_id: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Get user's listings.
        
        Args:
            user_id: User identifier
            channel: Communication channel
        
        Returns:
            Response with user's listings
        """
        await self.initialize(channel)
        
        try:
            result = await self.supabase.table("listings")\
                .select("*")\
                .eq("user_id", user_id)\
                .eq("status", "active")\
                .order("created_at", desc=True)\
                .limit(10)\
                .execute()
            
            listings = result.data or []
            
            if not listings:
                return self.response_builder.build_custom(
                    "📭 Henüz aktif ilanınız yok.\n\nİlan vermek için ürün fotoğrafı gönderin!"
                )
            
            # Format listings
            lines = ["📋 **Aktif İlanlarınız:**\n"]
            for i, listing in enumerate(listings, 1):
                title = listing.get("title", "İsimsiz")
                price = listing.get("price")
                price_str = f"{price:,.0f} TL" if price else "Fiyat yok"
                lines.append(f"{i}. {title} - {price_str}")
            
            buttons = [
                Button("➕ Yeni İlan", "new_listing"),
            ]
            
            return self.response_builder.build_custom(
                "\n".join(lines),
                buttons=buttons,
                metadata={"listing_ids": [l.get("id") for l in listings]},
            )
        
        except Exception as e:
            logger.error(f"Get listings error: {e}")
            return self.response_builder.build("error_generic")


# Singleton
publish_handler = PublishHandler()
