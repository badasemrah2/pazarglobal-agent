"""
Context Interpreter Layer (STUB - v2 Planned)

This module will handle user intent interpretation separately from FSM.
Currently a stub to document the planned architecture.

⚠️ NOT IN USE - Planned for v2 refactor
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class ContextAction(Enum):
    """Actions that ContextInterpreter can recommend to FSM."""
    CONTINUE = "continue"      # Continue current flow
    PAUSE = "pause"            # Pause current flow, do side action
    SWITCH = "switch"          # Switch to different flow entirely
    CANCEL = "cancel"          # Cancel current flow
    MODIFY = "modify"          # Modify current context (e.g., change product)


@dataclass
class ContextDecision:
    """
    Decision returned by ContextInterpreter.
    FSM should consume this without re-interpreting the message.
    """
    action: ContextAction
    target_intent: Optional[str] = None      # e.g., "search_listings" when SWITCH
    side_action: Optional[str] = None        # e.g., "search" when PAUSE
    modification: Optional[Dict[str, Any]] = None  # e.g., {"new_product": "MacBook"}
    reason: Optional[str] = None             # For logging/debugging
    
    @classmethod
    def continue_flow(cls) -> "ContextDecision":
        return cls(action=ContextAction.CONTINUE)
    
    @classmethod
    def pause_for_search(cls, search_query: str) -> "ContextDecision":
        return cls(
            action=ContextAction.PAUSE,
            side_action="search",
            modification={"search_query": search_query},
            reason="user_initiated_search"
        )
    
    @classmethod
    def switch_to(cls, target_intent: str, reason: Optional[str] = None) -> "ContextDecision":
        return cls(
            action=ContextAction.SWITCH,
            target_intent=target_intent,
            reason=reason
        )
    
    @classmethod
    def cancel_flow(cls, reason: Optional[str] = None) -> "ContextDecision":
        return cls(action=ContextAction.CANCEL, reason=reason)
    
    @classmethod
    def modify_context(cls, modification: Dict[str, Any]) -> "ContextDecision":
        return cls(
            action=ContextAction.MODIFY,
            modification=modification
        )


class ContextInterpreter:
    """
    Interprets user messages and returns ContextDecision for FSM.
    
    ⚠️ STUB - Not currently in use.
    
    When implemented, this will:
    1. Analyze user message
    2. Consider current session state
    3. Return a ContextDecision
    
    FSM will then:
    1. Consume ContextDecision
    2. Execute state transition
    3. NOT re-interpret the message
    
    Benefits:
    - Separation of concerns
    - Testable interpretation logic
    - Extensible without touching FSM
    
    Usage (v2):
        interpreter = ContextInterpreter()
        decision = interpreter.interpret(message, session_state)
        
        if decision.action == ContextAction.CONTINUE:
            # Continue current FSM flow
        elif decision.action == ContextAction.PAUSE:
            # Pause, execute side action, offer return
        elif decision.action == ContextAction.SWITCH:
            # Switch to decision.target_intent
        # etc.
    """
    
    def __init__(self):
        # Future: load intent patterns, ML model, etc.
        pass
    
    def interpret(
        self, 
        message: str, 
        current_state: Dict[str, Any]
    ) -> ContextDecision:
        """
        Interpret user message in context of current state.
        
        Args:
            message: User's message text
            current_state: Current session state including:
                - locked_intent
                - active_draft_id
                - paused_context
                - etc.
        
        Returns:
            ContextDecision indicating what FSM should do
        
        ⚠️ STUB - Always returns CONTINUE for now.
        """
        # TODO: Implement actual interpretation logic
        # This will be extracted from webchat.py in v2 refactor
        #
        # Planned logic:
        # 1. Check for cancel signals → CANCEL
        # 2. Check for search signals during create → PAUSE
        # 3. Check for product change signals → MODIFY
        # 4. Check for resume signals → CONTINUE (with context restore)
        # 5. Default → CONTINUE
        
        return ContextDecision.continue_flow()
    
    def _is_cancel_signal(self, message: str) -> bool:
        """Detect cancel/abort signals."""
        # TODO: Move is_cancel_command() logic here
        return False
    
    def _is_search_signal(self, message: str) -> bool:
        """Detect search intent signals."""
        # TODO: Move is_search_command() logic here
        return False
    
    def _is_product_change_signal(self, message: str) -> Optional[str]:
        """Detect product change signals, return new product if found."""
        # TODO: Move detects_product_change() logic here
        return None
    
    def _is_resume_signal(self, message: str) -> bool:
        """Detect resume/continue signals."""
        # TODO: Move is_resume_listing_command() logic here
        return False


# Singleton instance (for future use)
_interpreter: Optional[ContextInterpreter] = None


def get_context_interpreter() -> ContextInterpreter:
    """Get or create singleton ContextInterpreter instance."""
    global _interpreter
    if _interpreter is None:
        _interpreter = ContextInterpreter()
    return _interpreter
