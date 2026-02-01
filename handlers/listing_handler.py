"""
Listing Handler - Main orchestration for listing creation/editing

Flow:
1. Media received → Vision analysis → Product recognition
2. Extract slots from message
3. If missing required slots → Ask user
4. If all slots filled → Show preview
5. On confirm → Publish

State is stored in Supabase (single source of truth)
"""
import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from core.state_machine import StateMachine, ListingState, StateContext
from core.slot_filler import slot_filler, ExtractionResult
from core.response_builder import ResponseBuilder, Response, Button

from services.supabase_client import supabase_client
from services.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ListingContext:
    """Current listing context"""
    user_id: str
    draft_id: Optional[str] = None
    state: ListingState = ListingState.IDLE
    slots: Dict[str, Any] = None
    images: List[str] = None
    
    def __post_init__(self):
        self.slots = self.slots or {}
        self.images = self.images or []


class ListingHandler:
    """
    Listing creation/editing orchestration.
    
    Responsibilities:
    - Manage draft lifecycle (create, update, delete)
    - Coordinate with vision service for image analysis
    - Coordinate with price service for market research
    - Fill slots and build responses
    """
    
    # Required slots for publishing
    REQUIRED_SLOTS = ["title", "price", "images"]
    OPTIONAL_SLOTS = ["description", "category", "condition", "location"]
    
    def __init__(self):
        self.state_machine = StateMachine()
        self.response_builder: Optional[ResponseBuilder] = None
        self.supabase = None
        self.vision_service = None
        self.price_service = None
    
    async def initialize(self, channel: str = "webchat"):
        """Lazy initialization of services"""
        from core.response_builder import create_builder
        self.response_builder = create_builder(channel)
        self.supabase = supabase_client
    
    async def handle(
        self,
        user_id: str,
        message: str,
        media_urls: Optional[List[str]] = None,
        channel: str = "webchat",
        session_context: Optional[Dict[str, Any]] = None,
    ) -> Response:
        """
        Main entry point for listing flow.
        
        Args:
            user_id: User identifier
            message: User message
            media_urls: Attached media URLs
            channel: Communication channel
            session_context: Minimal Redis state (locked_intent, waiting_for, draft_id)
        
        Returns:
            Response to send to user
        """
        await self.initialize(channel)
        session_context = session_context or {}
        
        # 1. Load context from Supabase (using draft_id hint from Redis)
        context = await self._load_context(user_id, session_context.get("draft_id"))
        waiting_for = session_context.get("waiting_for")
        
        logger.info(f"Listing handler: user={user_id}, state={context.state}, draft={context.draft_id}, waiting_for={waiting_for}")
        
        # 2. Check for cancel command
        if self._is_cancel_command(message):
            return await self._handle_cancel(context)
        
        # 3. Handle media (image-first flow)
        if media_urls:
            return await self._handle_media(context, media_urls, message)
        
        # 4. Handle text based on state
        if context.state == ListingState.IDLE:
            return await self._handle_idle(context, message)
        
        elif context.state == ListingState.DRAFTING:
            return await self._handle_drafting(context, message)
        
        elif context.state == ListingState.PREVIEW:
            return await self._handle_preview(context, message)
        
        else:
            # Unknown state, reset
            await self._reset_context(context)
            return self.response_builder.build("error_generic")
    
    async def _load_context(self, user_id: str, hint_draft_id: Optional[str] = None) -> ListingContext:
        """Load listing context from Supabase, optionally using draft_id hint from Redis"""
        context = ListingContext(user_id=user_id)
        
        try:
            # Check for active draft (use hint if available for faster lookup)
            draft = await self._get_active_draft(user_id, hint_draft_id)
            
            if draft:
                context.draft_id = draft.get("id")
                context.state = self._parse_state(draft.get("state", "IDLE"))
                context.slots = {
                    "title": draft.get("title"),
                    "description": draft.get("description"),
                    "price": draft.get("price"),
                    "category": draft.get("category"),
                    "condition": draft.get("condition"),
                    "location": draft.get("location"),
                }
                context.images = draft.get("images") or []
                
                # Clean up None values
                context.slots = {k: v for k, v in context.slots.items() if v is not None}
        
        except Exception as e:
            logger.error(f"Error loading context: {e}")
        
        return context
    
    async def _save_context(self, context: ListingContext) -> bool:
        """Save context to Supabase"""
        try:
            data = {
                "user_id": context.user_id,
                "state": context.state.value,
                "title": context.slots.get("title"),
                "description": context.slots.get("description"),
                "price": context.slots.get("price"),
                "category": context.slots.get("category"),
                "condition": context.slots.get("condition"),
                "location": context.slots.get("location"),
                "images": context.images,
            }
            
            if context.draft_id:
                # Update existing draft
                await self.supabase.table("active_drafts").update(data).eq("id", context.draft_id).execute()
            else:
                # Create new draft
                result = await self.supabase.table("active_drafts").insert(data).execute()
                if result.data:
                    context.draft_id = result.data[0].get("id")
            
            return True
        
        except Exception as e:
            logger.error(f"Error saving context: {e}")
            return False
    
    async def _reset_context(self, context: ListingContext):
        """Delete draft and reset context"""
        try:
            if context.draft_id:
                await self.supabase.table("active_drafts").delete().eq("id", context.draft_id).execute()
            
            context.draft_id = None
            context.state = ListingState.IDLE
            context.slots = {}
            context.images = []
        
        except Exception as e:
            logger.error(f"Error resetting context: {e}")
    
    async def _get_active_draft(self, user_id: str, hint_draft_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get active draft for user, optionally using draft_id hint"""
        try:
            # Fast path: use hint_draft_id if provided
            if hint_draft_id:
                result = await self.supabase.table("active_drafts").select("*").eq("id", hint_draft_id).eq("user_id", user_id).limit(1).execute()
                if result.data:
                    return result.data[0]
            
            # Fallback: search by user_id
            result = await self.supabase.table("active_drafts").select("*").eq("user_id", user_id).limit(1).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting draft: {e}")
            return None
    
    def _parse_state(self, state_str: str) -> ListingState:
        """Parse state string to enum"""
        try:
            return ListingState(state_str.upper())
        except ValueError:
            return ListingState.IDLE
    
    def _is_cancel_command(self, message: str) -> bool:
        """Check if message is a cancel command"""
        cancel_words = ["iptal", "vazgeç", "vazgec", "cancel", "sil", "bırak", "birak"]
        return message.lower().strip() in cancel_words
    
    async def _handle_cancel(self, context: ListingContext) -> Response:
        """Handle cancel command"""
        await self._reset_context(context)
        response = self.response_builder.build("listing_cancelled")
        # Signal flow complete to clear Redis session
        response.metadata["flow_complete"] = True
        return response
    
    async def _handle_media(
        self,
        context: ListingContext,
        media_urls: List[str],
        message: str,
    ) -> Response:
        """Handle incoming media"""
        from services.vision_service import VisionService
        
        # 1. Vision safety check
        vision_service = VisionService()
        safety_result = await vision_service.check_safety(media_urls[0])
        
        if not safety_result.get("safe", True):
            return self.response_builder.build("error_vision_blocked")
        
        # 2. Analyze image for product info
        analysis = await vision_service.analyze_product(media_urls[0])
        
        # 3. Add images to context
        context.images.extend(media_urls)
        
        # 4. Extract slots from vision + message
        extraction = slot_filler.extract(message, media_urls, analysis)
        self._merge_slots(context, extraction)
        
        # 5. Transition to DRAFTING
        context.state = ListingState.DRAFTING
        await self._save_context(context)
        
        # 6. Build response
        return await self._build_slot_request(context, analysis)
    
    async def _handle_idle(self, context: ListingContext, message: str) -> Response:
        """Handle message in IDLE state"""
        # Extract any slots from message
        extraction = slot_filler.extract(message)
        
        if extraction.slots:
            # User provided some info, start draft
            self._merge_slots(context, extraction)
            context.state = ListingState.DRAFTING
            await self._save_context(context)
            return await self._build_slot_request(context)
        
        # No slots, ask for image
        response = self.response_builder.build("listing_start")
        # Signal that we're in CREATE flow, waiting for image
        response.metadata["continue_flow"] = True
        response.metadata["waiting_for"] = "image"
        return response
    
    async def _handle_drafting(self, context: ListingContext, message: str) -> Response:
        """Handle message in DRAFTING state"""
        # Extract slots from message
        extraction = slot_filler.extract(message)
        self._merge_slots(context, extraction)
        
        # Check if we have raw text (title candidate)
        if extraction.raw_text and "title" not in context.slots:
            context.slots["title"] = extraction.raw_text
        
        await self._save_context(context)
        
        # Check if ready for preview
        if self._has_required_slots(context):
            context.state = ListingState.PREVIEW
            await self._save_context(context)
            response = self.response_builder.build_preview(self._build_draft_dict(context))
            # Continue flow, waiting for confirmation
            response.metadata["continue_flow"] = True
            response.metadata["waiting_for"] = "confirmation"
            response.metadata["draft_id"] = context.draft_id
            return response
        
        # Ask for missing slots
        return await self._build_slot_request(context)
    
    async def _handle_preview(self, context: ListingContext, message: str) -> Response:
        """Handle message in PREVIEW state"""
        message_lower = message.lower().strip()
        
        # Check for publish command
        if message_lower in ["yayınla", "yayinla", "publish", "onayla", "tamam", "evet"]:
            return await self._publish_listing(context)
        
        # Check for edit command (e.g., "fiyat 500", "başlık yeni başlık")
        extraction = slot_filler.extract(message)
        if extraction.slots:
            self._merge_slots(context, extraction)
            await self._save_context(context)
            response = self.response_builder.build_preview(self._build_draft_dict(context))
            response.metadata["continue_flow"] = True
            response.metadata["waiting_for"] = "confirmation"
            response.metadata["draft_id"] = context.draft_id
            return response
        
        # Unknown, show preview again
        response = self.response_builder.build_preview(self._build_draft_dict(context))
        response.metadata["continue_flow"] = True
        response.metadata["waiting_for"] = "confirmation"
        response.metadata["draft_id"] = context.draft_id
        return response
    
    async def _publish_listing(self, context: ListingContext) -> Response:
        """Publish listing to main table"""
        try:
            # 1. Build listing data
            listing_data = {
                "user_id": context.user_id,
                "title": context.slots.get("title"),
                "description": context.slots.get("description"),
                "price": context.slots.get("price"),
                "category": context.slots.get("category", "Diğer"),
                "condition": context.slots.get("condition"),
                "location": context.slots.get("location"),
                "images": context.images,
                "status": "active",
            }
            
            # 2. Insert into listings
            result = await self.supabase.table("listings").insert(listing_data).execute()
            listing_id = result.data[0].get("id") if result.data else None
            
            # 3. Delete draft
            await self._reset_context(context)
            
            # 4. Build response with flow_complete to clear Redis session
            url = f"https://pazarglobal.com/listing/{listing_id}" if listing_id else ""
            response = self.response_builder.build(
                "listing_published",
                format_args={"url": url},
                metadata={"listing_id": listing_id, "flow_complete": True},
            )
            return response
        
        except Exception as e:
            logger.error(f"Error publishing listing: {e}")
            return self.response_builder.build("error_generic")
    
    def _merge_slots(self, context: ListingContext, extraction: ExtractionResult):
        """Merge extracted slots into context (don't overwrite existing)"""
        for slot_name, slot_value in extraction.slots.items():
            if slot_name not in context.slots or context.slots[slot_name] is None:
                context.slots[slot_name] = slot_value.value
    
    def _has_required_slots(self, context: ListingContext) -> bool:
        """Check if all required slots are filled"""
        for slot in self.REQUIRED_SLOTS:
            if slot == "images":
                if not context.images:
                    return False
            elif slot not in context.slots or context.slots[slot] is None:
                return False
        return True
    
    def _get_missing_slots(self, context: ListingContext) -> List[str]:
        """Get list of missing required slots"""
        missing = []
        for slot in self.REQUIRED_SLOTS:
            if slot == "images":
                if not context.images:
                    missing.append(slot)
            elif slot not in context.slots or context.slots[slot] is None:
                missing.append(slot)
        return missing
    
    async def _build_slot_request(
        self,
        context: ListingContext,
        vision_data: Optional[Dict[str, Any]] = None,
    ) -> Response:
        """Build response asking for missing slots"""
        missing = self._get_missing_slots(context)
        
        if not missing:
            # All slots filled, move to preview
            context.state = ListingState.PREVIEW
            await self._save_context(context)
            return self.response_builder.build_preview(self._build_draft_dict(context))
        
        # Build message based on first missing slot
        first_missing = missing[0]
        
        # Add vision hints if available
        hint = ""
        if vision_data:
            product = vision_data.get("product")
            suggested_price = vision_data.get("suggested_price")
            if product and first_missing == "title":
                hint = f"\n💡 Önerim: **{product}**"
            if suggested_price and first_missing == "price":
                hint = f"\n💡 Piyasa tahmini: {suggested_price:,.0f} TL"
        
        message_key = f"listing_need_{first_missing}"
        response = self.response_builder.build_custom(
            self.response_builder.MESSAGES.get(message_key, f"Lütfen {first_missing} bilgisini girin.") + hint,
            metadata={
                "missing_slots": missing, 
                "draft_id": context.draft_id,
                "continue_flow": True,
                "waiting_for": first_missing,
            },
        )
        return response
    
    def _build_draft_dict(self, context: ListingContext) -> Dict[str, Any]:
        """Build draft dict for preview"""
        return {
            "id": context.draft_id,
            "title": context.slots.get("title"),
            "description": context.slots.get("description"),
            "price": context.slots.get("price"),
            "category": context.slots.get("category"),
            "condition": context.slots.get("condition"),
            "location": context.slots.get("location"),
            "images": context.images,
        }


# Singleton
listing_handler = ListingHandler()
