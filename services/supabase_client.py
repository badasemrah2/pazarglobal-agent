"""
Supabase client for database operations
"""
from supabase import create_client, Client
from config import settings
from typing import Optional, Dict, List, Any
from loguru import logger


class SupabaseClient:
    """Supabase database client"""
    
    def __init__(self):
        self._client: Optional[Client] = None
    
    @property
    def client(self) -> Client:
        """Get or create Supabase client"""
        if self._client is None:
            self._client = create_client(
                settings.supabase_url,
                settings.supabase_service_key
            )
        return self._client
    
    # Active Drafts Operations
    async def create_draft(self, user_id: str, phone_number: str) -> Dict[str, Any]:
        """Create a new draft listing aligned to active_drafts schema."""
        try:
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
            logger.error(f"Error creating draft: {e}")
            raise
    
    async def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Get draft by ID"""
        try:
            result = self.client.table("active_drafts").select("*").eq("id", draft_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting draft: {e}")
            return None
    
    async def update_draft_title(self, draft_id: str, title: str) -> bool:
        """Update draft title inside listing_data"""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            listing_data["title"] = title
            result = self.client.table("active_drafts").update({
                "listing_data": listing_data
            }).eq("id", draft_id).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating title: {e}")
            return False
    
    async def update_draft_description(self, draft_id: str, description: str) -> bool:
        """Update draft description inside listing_data"""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            listing_data["description"] = description
            result = self.client.table("active_drafts").update({
                "listing_data": listing_data
            }).eq("id", draft_id).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating description: {e}")
            return False
    
    async def update_draft_price(self, draft_id: str, price: float) -> bool:
        """Update draft price inside listing_data"""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            listing_data["price"] = price
            result = self.client.table("active_drafts").update({
                "listing_data": listing_data
            }).eq("id", draft_id).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating price: {e}")
            return False
    
    async def update_draft_category(self, draft_id: str, category: str, vision_product: Dict[str, Any] = None) -> bool:
        """Update draft category inside listing_data and optionally vision_product"""
        try:
            draft = await self.get_draft(draft_id)
            if not draft:
                return False
            listing_data = draft.get("listing_data") or {}
            listing_data["category"] = category
            update_payload = {
                "listing_data": listing_data
            }
            if vision_product is not None:
                update_payload["vision_product"] = vision_product
            result = self.client.table("active_drafts").update(update_payload).eq("id", draft_id).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating category: {e}")
            return False
    
    # Listing Images Operations
    async def add_listing_image(self, listing_id: str, image_url: str, metadata: Dict = None) -> bool:
        """
        Add image to draft (active_drafts.images) or to published listing (product_images/images).
        If listing_id refers to a draft, append to images array; otherwise insert to product_images.
        """
        try:
            # Try draft first
            draft = await self.get_draft(listing_id)
            if draft:
                images = draft.get("images") or []
                images.append({"image_url": image_url, "metadata": metadata or {}})
                result = self.client.table("active_drafts").update({
                    "images": images
                }).eq("id", listing_id).execute()
                return bool(result.data)
            
            # Otherwise treat as published listing
            self.client.table("product_images").insert({
                "listing_id": listing_id,
                "public_url": image_url
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Error adding image: {e}")
            return False
    
    async def get_listing_images(self, listing_id: str) -> List[Dict[str, Any]]:
        """Get all images for a listing"""
        try:
            result = self.client.table("listing_images").select("*").eq("listing_id", listing_id).execute()
            return result.data or []
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
            images = draft.get("images") or []
            
            # Insert into listings
            result = self.client.table("listings").insert({
                "user_id": user_id,
                "title": listing_data.get("title"),
                "description": listing_data.get("description"),
                "price": listing_data.get("price"),
                "category": listing_data.get("category"),
                "status": "active",
                "images": images
            }).execute()
            
            if result.data:
                listing_id = result.data[0]["id"]
                
                # Persist product_images records
                for img in images:
                    try:
                        self.client.table("product_images").insert({
                            "listing_id": listing_id,
                            "public_url": img.get("image_url")
                        }).execute()
                    except Exception as e:
                        logger.warning(f"Failed to copy image to product_images: {e}")
                
                # Deduct credits if needed
                if cost > 0:
                    await self.deduct_credits(user_id, cost, f"publish_listing:{listing_id}")
                
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
                query = query.or_(f"title.ilike.%{search_text}%,description.ilike.%{search_text}%")
            
            result = query.limit(limit).execute()
            return result.data or []
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
            if balance is None or balance < amount:
                return False
            
            new_balance = balance - amount
            result = self.client.table("wallets").update({
                "balance_bigint": new_balance
            }).eq("user_id", user_id).execute()
            
            if result.data:
                self.client.table("wallet_transactions").insert({
                    "user_id": user_id,
                    "amount_bigint": -amount,
                    "kind": "debit",
                    "reference": description,
                    "metadata": {}
                }).execute()
                await self.log_action(
                    action="deduct_credits",
                    metadata={"amount": amount, "description": description},
                    resource_type="wallet",
                    resource_id=user_id,
                    user_id=user_id
                )
                return True
            
            return False
        except Exception as e:
            logger.error(f"Error deducting credits: {e}")
            return False
    
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
            payload = {
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "user_id": user_id,
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


# Global instance
supabase_client = SupabaseClient()
