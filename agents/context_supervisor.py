from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from services.openai_client import analyze_with_llm

class SupervisorDecision(BaseModel):
    action: str = Field(..., description="CONTINUE, CORRECTION, META_COMMENT, or CHANGE_INTENT")
    target_slot: Optional[str] = Field(None, description="If correction, which slot (title, price, description, category, condition)")
    reason: Optional[str] = Field(None, description="Explanation for the decision")
    suggested_reply: Optional[str] = Field(None, description="If META_COMMENT, reply to user directly")
    new_intent: Optional[str] = Field(None, description="If CHANGE_INTENT, the new intent (create_listing, search_listings, small_talk)")

SUPERVISOR_SYSTEM_PROMPT = """
You are the Context Supervisor for the PazarGlobal AI Agent.
Your job is to OVERSEE the user's interaction with the deterministic Finite State Machine (FSM).

Current State:
- Intent: {intent}
- Missing Slot: {missing_slot}

User Message: "{message}"

Analyze if the user is answering the missing slot question, or if they are deviating/correcting/complaining.

Allowable Actions:
1. CONTINUE: User is providing data for the missing slot (even if imperfect).
2. CORRECTION: User says the PREVIOUS value was wrong, OR user claims a field was skipped (e.g., "You didn't ask title"). This will clear the field and re-prompt.
3. META_COMMENT: PURE commentary/complaint that doesn't imply a state change (e.g., "Why do you need this?", "You are slow", "Is this AI?").
4. CHANGE_INTENT: User explicitly wants to do something else (e.g., "Search instead", "Sell car").

Output JSON matching the SupervisorDecision schema.
- If META_COMMENT, provide a polite suggested_reply in Turkish.
- If CORRECTION, identify the target_slot (title, price, description, category, condition).
"""

async def consult_supervisor(
    message: str,
    current_intent: str,
    missing_slot: Optional[str] = None
) -> SupervisorDecision:
    """
    Asks the LLM supervisor how to handle the current message given the locked context.
    Uses gpt-4o-mini for speed.
    """
    try:
        prompt = SUPERVISOR_SYSTEM_PROMPT.format(
            intent=current_intent,
            missing_slot=missing_slot or "None",
            message=message
        )
        
        response = await analyze_with_llm(
            system_prompt=prompt,
            user_message=message,
            response_model=SupervisorDecision,
            model="gpt-4o-mini",
            temperature=0.0
        )
        
        return response
    except Exception as e:
        # Fail-safe: continue as normal
        return SupervisorDecision(action="CONTINUE", reason=f"Supervisor failed: {e}")
