"""
State Machine - 4 state ile ilan yaşam döngüsü yönetimi

States:
    IDLE      - Kullanıcı boşta, intent bekleniyor
    DRAFTING  - İlan oluşturuluyor, slotlar dolduruluyor
    PREVIEW   - Önizleme aşaması, düzenleme yapılabilir
    PUBLISHED - Yayınlandı, flow sona erdi
"""
from enum import Enum
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime, timezone


class ListingState(Enum):
    """İlan durumları"""
    IDLE = "idle"
    DRAFTING = "drafting"
    PREVIEW = "preview"
    PUBLISHED = "published"


@dataclass
class StateContext:
    """State machine context"""
    user_id: str
    state: ListingState
    draft_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "state": self.state.value,
            "draft_id": self.draft_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateContext":
        return cls(
            user_id=data["user_id"],
            state=ListingState(data.get("state", "idle")),
            draft_id=data.get("draft_id"),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
        )


class StateMachine:
    """
    4-state FSM for listing lifecycle.
    
    Transitions:
        IDLE → DRAFTING (create intent)
        DRAFTING → PREVIEW (all slots filled)
        DRAFTING → IDLE (cancel)
        PREVIEW → PUBLISHED (publish command)
        PREVIEW → PREVIEW (edit command)
        PREVIEW → IDLE (cancel)
        PUBLISHED → IDLE (new listing)
    """
    
    # Valid transitions: (from_state, event) → to_state
    TRANSITIONS = {
        (ListingState.IDLE, "create"): ListingState.DRAFTING,
        (ListingState.DRAFTING, "slots_complete"): ListingState.PREVIEW,
        (ListingState.DRAFTING, "cancel"): ListingState.IDLE,
        (ListingState.PREVIEW, "publish"): ListingState.PUBLISHED,
        (ListingState.PREVIEW, "edit"): ListingState.PREVIEW,
        (ListingState.PREVIEW, "cancel"): ListingState.IDLE,
        (ListingState.PUBLISHED, "new_listing"): ListingState.IDLE,
    }
    
    def __init__(self, context: Optional[StateContext] = None):
        self.context = context
    
    def can_transition(self, event: str) -> bool:
        """Check if transition is valid"""
        if not self.context:
            return False
        key = (self.context.state, event)
        return key in self.TRANSITIONS
    
    def transition(self, event: str) -> bool:
        """
        Execute state transition.
        
        Args:
            event: Event name (create, slots_complete, cancel, publish, edit, new_listing)
        
        Returns:
            True if transition successful, False otherwise
        """
        if not self.context:
            return False
        
        key = (self.context.state, event)
        new_state = self.TRANSITIONS.get(key)
        
        if new_state is None:
            return False
        
        old_state = self.context.state
        self.context.state = new_state
        self.context.updated_at = datetime.now(timezone.utc)
        
        # Clear draft_id on IDLE transition
        if new_state == ListingState.IDLE:
            self.context.draft_id = None
        
        return True
    
    def get_state(self) -> ListingState:
        """Get current state"""
        return self.context.state if self.context else ListingState.IDLE
    
    def set_draft_id(self, draft_id: str) -> None:
        """Set draft ID"""
        if self.context:
            self.context.draft_id = draft_id
    
    @classmethod
    def create_for_user(cls, user_id: str) -> "StateMachine":
        """Create new state machine for user"""
        context = StateContext(
            user_id=user_id,
            state=ListingState.IDLE,
            updated_at=datetime.now(timezone.utc),
        )
        return cls(context)
    
    @classmethod
    def from_draft(cls, draft: Dict[str, Any]) -> "StateMachine":
        """
        Create state machine from Supabase draft.
        
        Maps draft.state to ListingState:
            "in_progress" → DRAFTING
            "preview" → PREVIEW
            "published" → PUBLISHED (rare)
            else → IDLE
        """
        draft_state = draft.get("state", "")
        user_id = draft.get("user_id", "")
        draft_id = draft.get("id")
        
        state_map = {
            "in_progress": ListingState.DRAFTING,
            "drafting": ListingState.DRAFTING,
            "preview": ListingState.PREVIEW,
            "published": ListingState.PUBLISHED,
        }
        
        state = state_map.get(draft_state, ListingState.IDLE)
        
        # Check if draft has all required slots → PREVIEW
        if state == ListingState.DRAFTING:
            listing_data = draft.get("listing_data") or {}
            if _draft_is_complete(listing_data):
                state = ListingState.PREVIEW
        
        context = StateContext(
            user_id=user_id,
            state=state,
            draft_id=draft_id,
            updated_at=datetime.now(timezone.utc),
        )
        return cls(context)


def _draft_is_complete(listing_data: Dict[str, Any]) -> bool:
    """Check if all required slots are filled"""
    required = ["title", "price", "condition", "location"]
    for slot in required:
        value = listing_data.get(slot)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True
