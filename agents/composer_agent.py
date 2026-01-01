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
from typing import Dict, Any, List, Optional
from loguru import logger
import asyncio
import re


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
        draft_id: Optional[str] = None,
        media_url: Optional[str] = None,
        media_urls: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Orchestrate the listing creation process.
        
        Args:
            user_message: User's text message
            user_id: User identifier
            phone_number: Phone/session identifier
            draft_id: Existing draft ID (optional)
            media_url: Legacy single media URL parameter
            media_urls: List of media URLs to process
        """
        try:
            # Support both single and multiple media URLs
            all_media_urls = media_urls or ([media_url] if media_url else [])
            if all_media_urls:
                deduped: List[str] = []
                seen = set()
                for url in all_media_urls:
                    if url and url not in seen:
                        deduped.append(url)
                        seen.add(url)
                all_media_urls = deduped
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

                # If the user is starting from fresh media, reset the (reused) draft so old fields
                # like price/title don't leak into the new flow.
                if all_media_urls:
                    try:
                        from services import supabase_client
                        await supabase_client.reset_draft(draft_id, phone_number=phone_number)
                    except Exception as reset_err:
                        logger.warning(f"Failed to reset draft {draft_id}: {reset_err}")
            
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
                "user_id": user_id,
                "media_urls": all_media_urls
            }
            
            # ALWAYS run all agents in parallel for create_listing intent
            # Each agent's system prompt determines what updates it should make
            # This ensures comprehensive listing data extraction from any user message
            # If the user message is just a flow command (e.g., "ilan oluştur") and we already have media,
            # avoid generating random titles/descriptions from empty text. Let ImageAgent / vision fill them.
            normalized_msg = (user_message or "").strip().lower()
            is_command_only = normalized_msg in {
                "ilan oluştur",
                "ilan olustur",
                "ilan",
                "başlat",
                "baslat",
                "devam",
                "devam et",
            }

            tasks = []
            if not (is_command_only and all_media_urls):
                tasks.extend([
                    self.title_agent.run(user_message, context),
                    self.description_agent.run(user_message, context)
                ])

            # Only run PriceAgent when user actually provided a price signal.
            # This prevents hallucinated or cached prices from being (re)written on commands like "ilan oluştur".
            msg = (user_message or "").lower()
            has_price_number = bool(re.search(r"\b\d{2,}\b", msg))
            has_currency_hint = any(tok in msg for tok in ["₺", "tl", "try", "lira", "fiyat"])
            if has_price_number and has_currency_hint:
                tasks.append(self.price_agent.run(user_message, context))
            
            # Add image agent for each media URL provided
            if all_media_urls:
                for media_url_item in all_media_urls:
                    tasks.append(self.image_agent.run(media_url_item, context))
            else:
                # Also check if message mentions images without explicit URLs
                message_lower = user_message.lower()
                if any(word in message_lower for word in ["image", "photo", "resim", "fotoğraf", "görsel", "resim yükle"]):
                    tasks.append(self.image_agent.run(user_message, context))
            
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
