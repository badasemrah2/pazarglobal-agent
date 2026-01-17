"""
Listing management tools (publish, delete, search)
"""
from typing import Dict, Any, Optional
from loguru import logger
from .base_tool import BaseTool
from services import supabase_client
from services.supabase_client import InsufficientCreditsError


class PublishListingTool(BaseTool):
    """Tool to publish a draft as a live listing"""
    
    def get_name(self) -> str:
        return "publish_listing"
    
    def get_description(self) -> str:
        return "Publish a draft listing to make it publicly visible. Requires user confirmation. Credit cost is configured server-side."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Draft ID to publish"
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID"
                }
            },
            "required": ["draft_id", "user_id"]
        }
    
    async def execute(self, draft_id: str, user_id: str, credit_cost: Optional[int] = None) -> Dict[str, Any]:
        try:
            # NOTE: credit_cost is ignored by SupabaseClient (cost is deterministic from settings).
            listing = await supabase_client.publish_listing(draft_id, user_id, cost=credit_cost)
        except InsufficientCreditsError as exc:
            return self.format_error(str(exc))
        except Exception as exc:
            logger.error(f"Publish listing tool failed: {exc}")
            return self.format_error("Yayınlama sırasında beklenmeyen bir hata oluştu.")

        if listing:
            return self.format_success({
                "listing_id": listing["id"],
                "message": "İlan başarıyla yayınlandı"
            })
        return self.format_error("İlan yayınlanamadı.")


class DeleteListingTool(BaseTool):
    """Tool to delete a listing"""
    
    def get_name(self) -> str:
        return "delete_listing"
    
    def get_description(self) -> str:
        return "Delete a published listing. Requires user confirmation."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "listing_id": {
                    "type": "string",
                    "description": "Listing ID to delete"
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID performing deletion"
                }
            },
            "required": ["listing_id"]
        }
    
    async def execute(self, listing_id: str, user_id: str = None) -> Dict[str, Any]:
        success = await supabase_client.delete_listing(listing_id, user_id=user_id)
        if success:
            return self.format_success({"message": "İlan silindi"})
        return self.format_error("Failed to delete listing")


class SearchListingsTool(BaseTool):
    """Tool to search listings with filters"""
    
    def get_name(self) -> str:
        return "search_listings"
    
    def get_description(self) -> str:
        return "Search for listings using category, price range, or text filters"
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Filter by category"
                },
                "min_price": {
                    "type": "number",
                    "description": "Minimum price filter"
                },
                "max_price": {
                    "type": "number",
                    "description": "Maximum price filter"
                },
                "search_text": {
                    "type": "string",
                    "description": "Search in title and description"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default 20)"
                }
            },
            "required": []
        }
    
    async def execute(
        self,
        category: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        search_text: Optional[str] = None,
        limit: int = 20
    ) -> Dict[str, Any]:
        # Normalize category to canonical DB value so searches don't miss listings.
        # Example: UI/LLM may send "Vasıta" but DB stores "Otomotiv".
        if category:
            try:
                from services.category_library import normalize_category_id

                normalized = normalize_category_id(category)
                if normalized:
                    category = normalized
            except Exception:
                # best-effort; keep original
                pass

        # Try cache first (only for deterministic searches with search_text)
        from services.redis_client import redis_client

        if search_text:
            cache_filters = {
                "category": category,
                "min_price": min_price,
                "max_price": max_price,
                "limit": limit
            }
            cached_listings = await redis_client.get_search_cache(search_text, cache_filters)
            if cached_listings:
                logger.info(f"✅ Cache HIT for search: {search_text[:50]}")
                return self.format_success({
                    "listings": cached_listings,
                    "count": len(cached_listings),
                    "cached": True
                })
            logger.info(f"⚠️ Cache MISS for search: {search_text[:50]}")

        listings = await supabase_client.search_listings(
            category=category,
            min_price=min_price,
            max_price=max_price,
            search_text=search_text,
            limit=limit
        )

        # Cache the results for future queries (only if search_text exists)
        if search_text and listings:
            cache_filters = {
                "category": category,
                "min_price": min_price,
                "max_price": max_price,
                "limit": limit
            }
            await redis_client.set_search_cache(search_text, listings, cache_filters)
        return self.format_success({
            "listings": listings,
            "count": len(listings)
        })


class MarketPriceTool(BaseTool):
    """Tool to fetch market price snapshots"""
    
    def get_name(self) -> str:
        return "get_market_price_data"
    
    def get_description(self) -> str:
        return "Get market price snapshots for a product or category"
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "product_key": {
                    "type": "string",
                    "description": "Product key/title fragment"
                },
                "category": {
                    "type": "string",
                    "description": "Category filter"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 5)"
                }
            },
            "required": []
        }
    
    async def execute(
        self,
        product_key: str = None,
        category: str = None,
        limit: int = 5
    ) -> Dict[str, Any]:
        snapshots = await supabase_client.get_market_price_data(
            product_key=product_key,
            category=category,
            limit=limit
        )
        return self.format_success({
            "snapshots": snapshots,
            "count": len(snapshots)
        })


# Tool instances
publish_listing_tool = PublishListingTool()
delete_listing_tool = DeleteListingTool()
search_listings_tool = SearchListingsTool()
market_price_tool = MarketPriceTool()
