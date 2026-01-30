"""
Search Handler - Wrapper around existing SearchComposerAgent

This handler delegates to the existing SearchComposerAgent which is
already well-tested and functional. We just wrap it with the new
response building system.
"""
from typing import Dict, Any, Optional, List

from core.response_builder import ResponseBuilder, Response, create_builder

from services.logger import get_logger

logger = get_logger(__name__)


class SearchHandler:
    """
    Search handler - delegates to SearchComposerAgent.
    
    The SearchComposerAgent is PRESERVED from the old architecture
    because it works well and is isolated.
    """
    
    def __init__(self):
        self.response_builder: Optional[ResponseBuilder] = None
        self.search_agent = None
    
    async def initialize(self, channel: str = "webchat"):
        """Lazy initialization"""
        self.response_builder = create_builder(channel)
        
        # Import existing agent
        from agents.search_agents import SearchComposerAgent
        self.search_agent = SearchComposerAgent()
    
    async def handle(
        self,
        user_id: str,
        message: str,
        channel: str = "webchat",
    ) -> Response:
        """
        Handle search request.
        
        Args:
            user_id: User identifier
            message: Search query
            channel: Communication channel
        
        Returns:
            Response with search results
        """
        await self.initialize(channel)
        
        logger.info(f"Search handler: user={user_id}, query={message}")
        
        try:
            # Delegate to SearchComposerAgent
            result = await self.search_agent.run(
                query=message,
                user_id=user_id,
            )
            
            # Parse result
            if isinstance(result, dict):
                listings = result.get("listings", [])
                summary = result.get("summary", "")
            else:
                # Agent returned string
                return self.response_builder.build_custom(str(result))
            
            # Build response
            if not listings:
                return self.response_builder.build("search_no_results")
            
            return self.response_builder.build_search_results(listings)
        
        except Exception as e:
            logger.error(f"Search error: {e}")
            return self.response_builder.build("search_no_results")
    
    async def handle_with_filters(
        self,
        user_id: str,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        channel: str = "webchat",
    ) -> Response:
        """
        Handle search with explicit filters.
        
        Args:
            user_id: User identifier
            query: Search query
            filters: Explicit filters (category, price_min, price_max, location)
            channel: Communication channel
        
        Returns:
            Response with filtered results
        """
        await self.initialize(channel)
        
        logger.info(f"Search handler (filtered): user={user_id}, query={query}, filters={filters}")
        
        try:
            # Build search message with filters
            search_message = query
            
            if filters:
                if filters.get("category"):
                    search_message += f" kategori:{filters['category']}"
                if filters.get("price_min"):
                    search_message += f" min:{filters['price_min']}"
                if filters.get("price_max"):
                    search_message += f" max:{filters['price_max']}"
                if filters.get("location"):
                    search_message += f" konum:{filters['location']}"
            
            return await self.handle(user_id, search_message, channel)
        
        except Exception as e:
            logger.error(f"Filtered search error: {e}")
            return self.response_builder.build("search_no_results")


# Singleton
search_handler = SearchHandler()
