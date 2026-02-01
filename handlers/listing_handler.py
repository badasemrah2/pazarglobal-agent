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
import re
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
        
        # 4. Handle text based on state and waiting_for
        # If we were waiting for image but user sent text, treat as product info
        if context.state == ListingState.IDLE and waiting_for == "image" and message.strip():
            # User sent text while we were waiting for image
            # Start drafting with this as potential title/product
            extraction = slot_filler.extract(message)
            if extraction.raw_text:
                context.slots["title"] = extraction.raw_text
            self._merge_slots(context, extraction)
            context.state = ListingState.DRAFTING
            await self._save_context(context)
            return await self._build_slot_request(context)
        
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
                
                # Extract slots from listing_data JSON
                listing_data = draft.get("listing_data") or {}
                context.slots = {
                    "title": listing_data.get("title"),
                    "description": listing_data.get("description"),
                    "price": listing_data.get("price"),
                    "category": listing_data.get("category"),
                    "condition": listing_data.get("condition"),
                    "location": listing_data.get("location"),
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
            # Build listing_data JSON for the active_drafts schema
            listing_data = {
                "title": context.slots.get("title"),
                "description": context.slots.get("description"),
                "price": context.slots.get("price"),
                "category": context.slots.get("category"),
                "condition": context.slots.get("condition"),
                "location": context.slots.get("location"),
            }
            
            data = {
                "user_id": context.user_id,
                "state": context.state.value.lower(),  # active_drafts uses lowercase state
                "listing_data": listing_data,
                "images": context.images,
            }
            
            if context.draft_id:
                # Update existing draft
                self.supabase.client.table("active_drafts").update(data).eq("id", context.draft_id).execute()
            else:
                # Create new draft
                result = self.supabase.client.table("active_drafts").insert(data).execute()
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
                self.supabase.client.table("active_drafts").delete().eq("id", context.draft_id).execute()
            
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
                result = self.supabase.client.table("active_drafts").select("*").eq("id", hint_draft_id).eq("user_id", user_id).limit(1).execute()
                if result.data:
                    return result.data[0]
            
            # Fallback: search by user_id
            result = self.supabase.client.table("active_drafts").select("*").eq("user_id", user_id).limit(1).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting draft: {e}")
            return None
    
    def _parse_state(self, state_str: str) -> ListingState:
        """Parse state string to enum"""
        try:
            # Handle both uppercase and lowercase state values
            return ListingState(state_str.upper())
        except ValueError:
            # Map common state names
            state_map = {
                "in_progress": ListingState.DRAFTING,
                "drafting": ListingState.DRAFTING,
                "preview": ListingState.PREVIEW,
                "idle": ListingState.IDLE,
            }
            return state_map.get(state_str.lower(), ListingState.IDLE)
    
    def _is_cancel_command(self, message: str) -> bool:
        """Check if message is a cancel command"""
        cancel_words = ["iptal", "vazgeç", "vazgec", "cancel", "sil", "bırak", "birak"]
        return message.lower().strip() in cancel_words
    
    def _parse_bare_price(self, message: str) -> Optional[float]:
        """
        Parse a bare number as price (when user just sends "2500" or "1.500")
        Returns None if message is not a bare number.
        """
        # Clean message
        clean = message.strip()
        
        # Check if it's mostly numeric (allow dots, commas, spaces as thousand separators)
        # Also allow "k" suffix (50k = 50000)
        if re.match(r"^[\d\.\,\s]+(?:k|K)?$", clean):
            try:
                # Handle "k" suffix
                if clean.lower().endswith("k"):
                    num_part = re.sub(r"[^\d]", "", clean[:-1])
                    return float(num_part) * 1000
                
                # Remove thousand separators and convert
                num_part = re.sub(r"[\.\s]", "", clean)  # Remove dots and spaces (thousand sep)
                num_part = num_part.replace(",", ".")   # Convert comma to decimal point
                return float(num_part)
            except (ValueError, TypeError):
                return None
        
        return None
    
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
        from services.vision_service import vision_service
        
        try:
            # 1. Analyze image for product info (safety already checked in gateway)
            analysis = await vision_service.analyze_product(media_urls[0])
            logger.info(f"Vision analysis result: {analysis}")
            
            # 2. Add images to context
            context.images.extend(media_urls)
            
            # 3. Extract slots from vision + message
            extraction = slot_filler.extract(message, media_urls, analysis)
            self._merge_slots(context, extraction)
            
            # 4. Transition to DRAFTING
            context.state = ListingState.DRAFTING
            await self._save_context(context)
            
            # 5. Build response
            return await self._build_slot_request(context, analysis)
            
        except Exception as e:
            logger.error(f"Media handling error: {e}", exc_info=True)
            response = self.response_builder.build("error_generic")
            response.metadata["error_detail"] = str(e)
            return response
    
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
        message_lower = message.lower().strip()
        
        # Check for price suggestion request
        if any(p in message_lower for p in ["fiyat öner", "fiyat oner", "fiyat araştır", "fiyat arastir", "ne kadar eder"]):
            return await self._handle_price_suggestion(context)
        
        # Check if message is just a number (likely price)
        if "price" not in context.slots or context.slots.get("price") is None:
            price_value = self._parse_bare_price(message)
            if price_value is not None:
                context.slots["price"] = price_value
                await self._save_context(context)
                
                # Check if ready for preview
                if self._has_required_slots(context):
                    context.state = ListingState.PREVIEW
                    await self._save_context(context)
                    response = self.response_builder.build_preview(self._build_draft_dict(context))
                    response.metadata["continue_flow"] = True
                    response.metadata["waiting_for"] = "confirmation"
                    response.metadata["draft_id"] = context.draft_id
                    return response
                
                return await self._build_slot_request(context)
        
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
            
            logger.info(f"Publishing listing: {listing_data}")
            
            # 2. Insert into listings
            result = self.supabase.client.table("listings").insert(listing_data).execute()
            
            if not result.data:
                logger.error(f"Empty result from listings insert")
                return self.response_builder.build("error_generic")
            
            listing_id = result.data[0].get("id")
            logger.info(f"Published listing {listing_id}")
            
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
            logger.error(f"Error publishing listing: {e}", exc_info=True)
            response = self.response_builder.build("error_generic")
            response.metadata["error_detail"] = str(e)
            return response
    
    async def _handle_price_suggestion(self, context: ListingContext) -> Response:
        """Handle price suggestion request within listing flow"""
        title = context.slots.get("title")
        
        if not title:
            response = Response(
                text="💡 Fiyat önerisi için önce ürün adını belirtin.",
                metadata={"continue_flow": True, "waiting_for": "title", "draft_id": context.draft_id},
            )
            return response
        
        try:
            from services.price_service import price_service
            
            result = await price_service.get_market_price(
                product_name=title,
                category=context.slots.get("category"),
                condition=context.slots.get("condition"),
            )
            
            if result and result.get("suggested_price"):
                suggested = result["suggested_price"]
                response = Response(
                    text=f"💰 **{title}** için önerilen fiyat: **{int(suggested):,} TL**\n\n"
                         f"Bu fiyatı kullanmak ister misiniz? (evet/hayır veya kendi fiyatınızı yazın)",
                    metadata={
                        "suggested_price": suggested,
                        "continue_flow": True,
                        "waiting_for": "price",
                        "draft_id": context.draft_id,
                    },
                )
                return response
            else:
                response = Response(
                    text="😕 Bu ürün için fiyat önerisi bulunamadı. Lütfen fiyatı kendiniz belirleyin.",
                    metadata={"continue_flow": True, "waiting_for": "price", "draft_id": context.draft_id},
                )
                return response
                
        except Exception as e:
            logger.error(f"Price suggestion error: {e}")
            response = Response(
                text="😕 Fiyat önerisi alınamadı. Lütfen fiyatı kendiniz belirleyin.",
                metadata={"continue_flow": True, "waiting_for": "price", "draft_id": context.draft_id},
            )
            return response
    
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
