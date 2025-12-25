"""
Search Agents - Handle different types of listing searches
"""
from .base_agent import BaseAgent
from config.prompts import (
    CATEGORY_SEARCH_AGENT_PROMPT,
    PRICE_SEARCH_AGENT_PROMPT,
    CONTENT_SEARCH_AGENT_PROMPT,
    SEARCH_COMPOSER_AGENT_PROMPT
)
from tools import search_listings_tool, market_price_tool
from typing import Dict, Any
import asyncio
from loguru import logger


class CategorySearchAgent(BaseAgent):
    """Agent for category-based search"""
    
    def __init__(self):
        super().__init__(
            name="CategorySearchAgent",
            system_prompt=CATEGORY_SEARCH_AGENT_PROMPT,
            tools=[search_listings_tool]
        )


class PriceSearchAgent(BaseAgent):
    """Agent for price-based search"""
    
    def __init__(self):
        super().__init__(
            name="PriceSearchAgent",
            system_prompt=PRICE_SEARCH_AGENT_PROMPT,
            tools=[search_listings_tool]
        )


class ContentSearchAgent(BaseAgent):
    """Agent for content-based search (title/description)"""
    
    def __init__(self):
        super().__init__(
            name="ContentSearchAgent",
            system_prompt=CONTENT_SEARCH_AGENT_PROMPT,
            tools=[search_listings_tool]
        )


class SearchComposerAgent(BaseAgent):
    """Composer agent that orchestrates parallel search operations"""
    
    def __init__(self):
        super().__init__(
            name="SearchComposerAgent",
            system_prompt=SEARCH_COMPOSER_AGENT_PROMPT,
            tools=[search_listings_tool, market_price_tool]
        )
        
        # Initialize sub-agents
        self.category_agent = CategorySearchAgent()
        self.price_agent = PriceSearchAgent()
        self.content_agent = ContentSearchAgent()
    
    async def orchestrate_search(
        self,
        user_message: str,
        context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Orchestrate parallel search operations
        
        Args:
            user_message: User's search query
            context: Additional context
        
        Returns:
            Combined search results
        """
        try:
            message_lower = user_message.lower()
            tasks = []
            
            # Determine which search agents to run
            if any(word in message_lower for word in ["category", "kategori", "type", "tür"]):
                tasks.append(self.category_agent.run(user_message, context))
            
            if any(word in message_lower for word in ["price", "fiyat", "cheap", "ucuz", "expensive", "pahalı", "cost"]):
                tasks.append(self.price_agent.run(user_message, context))
            
            # Always run content search as fallback
            tasks.append(self.content_agent.run(user_message, context))
            
            # Execute searches in parallel
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Combine and deduplicate results
            all_listings = []
            seen_ids = set()
            
            for result in results:
                if isinstance(result, dict) and result.get("success"):
                    for tool_call in result.get("tool_calls", []):
                        if tool_call.get("result", {}).get("success"):
                            listings = tool_call["result"]["data"].get("listings", [])
                            for listing in listings:
                                # Guard: do not hybridize across listing_ids; pick first occurrence only
                                listing_id = listing.get("id")
                                if listing_id and listing_id not in seen_ids:
                                    all_listings.append(listing)
                                    seen_ids.add(listing_id)
            
            # Fetch market price context
            market_data = await market_price_tool.execute(product_key=user_message)
            insights = []
            if market_data.get("success") and market_data["data"].get("snapshots"):
                snaps = market_data["data"]["snapshots"]
                avg_prices = [s.get("avg_price") for s in snaps if s.get("avg_price") is not None]
                if avg_prices:
                    market_avg = sum(avg_prices) / len(avg_prices)
                    insights.append(f"Piyasa ortalaması ~{market_avg:.2f} ({len(avg_prices)} kaynak)")
            
            # Limit to 5 items for response to avoid token blowup
            preview_listings = all_listings[:5]
            remaining = max(len(all_listings) - len(preview_listings), 0)
            msg = f"{len(all_listings)} ilan bulundu."
            if remaining > 0:
                msg += f" İlk {len(preview_listings)} tanesini gösteriyorum. Daha fazlası için söyleyin."

            return {
                "success": True,
                "listings": preview_listings,
                "count": len(all_listings),
                "market_data": market_data["data"] if market_data.get("success") else {},
                "insights": insights,
                "message": msg
            }
        
        except Exception as e:
            logger.error(f"Search orchestration error: {e}")
            return {
                "success": False,
                "error": str(e),
                "listings": [],
                "count": 0,
                "market_data": {},
                "insights": []
            }
