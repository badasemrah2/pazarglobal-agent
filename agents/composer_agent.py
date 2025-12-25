"""
Composer Agent - Orchestrates Create Listing workflow
"""
from .base_agent import BaseAgent
from config.prompts import COMPOSER_AGENT_PROMPT
from tools import create_draft_tool, read_draft_tool
from .title_agent import TitleAgent
from .description_agent import DescriptionAgent
from .price_agent import PriceAgent
from .image_agent import ImageAgent
from typing import Dict, Any
from loguru import logger
import asyncio


class ComposerAgent(BaseAgent):
    """Composer agent that orchestrates parallel agents for listing creation"""
    
    def __init__(self):
        super().__init__(
            name="ComposerAgent",
            system_prompt=COMPOSER_AGENT_PROMPT,
            tools=[create_draft_tool, read_draft_tool]
        )
        
        # Initialize sub-agents
        self.title_agent = TitleAgent()
        self.description_agent = DescriptionAgent()
        self.price_agent = PriceAgent()
        self.image_agent = ImageAgent()
    
    async def orchestrate_listing_creation(
        self,
        user_message: str,
        user_id: str,
        phone_number: str,
        draft_id: str = None,
        media_url: str = None
    ) -> Dict[str, Any]:
        """
        Orchestrate the listing creation process.
        """
        try:
            # Create draft if not exists
            if not draft_id:
                result = await create_draft_tool.execute(
                    user_id=user_id,
                    phone_number=phone_number
                )
                if not result["success"]:
                    return {
                        "success": False,
                        "error": "Failed to create draft"
                    }
                draft_id = result["data"]["draft_id"]
                logger.info(f"Created new draft: {draft_id}")
            
            # Always read current draft state before updates
            current_draft = await read_draft_tool.execute(draft_id=draft_id)
            if not current_draft.get("success"):
                return {
                    "success": False,
                    "error": "Draft not found",
                    "code": "missing_listing_id"
                }
            
            # Context for all agents
            context = {
                "draft_id": draft_id,
                "user_id": user_id
            }
            
            # Run agents in parallel (they all work on the SAME draft_id)
            tasks = []
            
            # Determine which agents to run based on message content
            message_lower = user_message.lower()
            
            if any(word in message_lower for word in ["title", "name", "başlık", "isim"]):
                tasks.append(self.title_agent.run(user_message, context))
            
            if any(word in message_lower for word in ["description", "açıklama", "detay", "describe"]):
                tasks.append(self.description_agent.run(user_message, context))
            
            if any(word in message_lower for word in ["price", "fiyat", "cost", "ücret", "tl", "$", "₺"]):
                tasks.append(self.price_agent.run(user_message, context))
            
            # Media or image keywords trigger ImageAgent
            if media_url or any(word in message_lower for word in ["image", "photo", "resim", "fotoğraf", "görsel"]):
                tasks.append(self.image_agent.run(media_url or user_message, context))
            
            # If no specific agent matched, run all (include image if media exists)
            if not tasks:
                tasks = [
                    self.title_agent.run(user_message, context),
                    self.description_agent.run(user_message, context),
                    self.price_agent.run(user_message, context)
                ]
                if media_url:
                    tasks.append(self.image_agent.run(media_url, context))
            
            # Execute agents in parallel
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Check for listing_id conflicts (CRITICAL GUARD)
            draft_ids_used = set()
            for result in results:
                if isinstance(result, dict) and result.get("tool_calls"):
                    for tool_call in result["tool_calls"]:
                        if "draft_id" in tool_call.get("args", {}):
                            draft_ids_used.add(tool_call["args"]["draft_id"])
            
            if len(draft_ids_used) > 1:
                logger.error(f"CONFLICT: Multiple draft_ids detected: {draft_ids_used}")
                from services import supabase_client
                await supabase_client.log_action(
                    action="draft_conflict_detected",
                    metadata={"draft_ids": list(draft_ids_used)},
                    resource_type="draft",
                    resource_id=draft_id,
                    user_id=user_id
                )
                return {
                    "success": False,
                    "error": "Data integrity conflict detected. Please restart listing creation.",
                    "conflict_ids": list(draft_ids_used)
                }
            
            # Read final draft state
            draft_result = await read_draft_tool.execute(draft_id=draft_id)
            
            if draft_result["success"]:
                draft = draft_result["data"]
                return {
                    "success": True,
                    "draft_id": draft_id,
                    "draft": draft,
                    "message": "Listing draft updated successfully",
                    "agent_results": [r for r in results if not isinstance(r, Exception)]
                }
            
            return {
                "success": False,
                "error": "Failed to read draft"
            }
        
        except Exception as e:
            logger.error(f"Composer orchestration error: {e}")
            return {
                "success": False,
                "error": str(e)
            }
