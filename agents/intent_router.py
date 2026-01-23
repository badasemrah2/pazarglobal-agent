"""
Intent Router Agent - Routes user messages to appropriate workflow
"""
from .base_agent import BaseAgent
from config.prompts import INTENT_ROUTER_PROMPT
from services import openai_client
import json
from services.logger import get_logger


logger = get_logger(__name__)


class IntentRouterAgent(BaseAgent):
    """Router agent to classify user intent"""
    
    def __init__(self):
        super().__init__(
            name="IntentRouter",
            system_prompt=INTENT_ROUTER_PROMPT,
            tools=[]  # No tools needed
        )
    
    async def classify_intent(self, user_message: str) -> dict:
        """
        Classify user message into one of the intents
        
        Returns:
            Dict with 'intent' and optional 'detected_intents' for ambiguous cases:
            - intent: primary intent (create_listing, publish_or_delete, search_listings, small_talk, ambiguous)
            - detected_intents: list of detected intents when ambiguous
            - confidence: confidence level
        """
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Bu mesajın niyetini sınıflandır: {user_message}"}
            ]
            
            # Use function calling for structured output
            functions = [{
                "name": "classify_intent",
                "description": "Classify the user's intent, detecting multiple intents if present",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "enum": ["create_listing", "publish_or_delete", "search_listings", "price_research", "small_talk", "ambiguous"],
                            "description": "The primary classified intent. Use 'ambiguous' if multiple clear intents detected. Use 'price_research' when user ONLY wants to learn price (no listing creation or search)."
                        },
                        "detected_intents": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["create_listing", "search_listings", "price_research"]
                            },
                            "description": "List of all detected intents when ambiguous. Empty if not ambiguous. price_research = standalone price inquiry."
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Confidence level"
                        }
                    },
                    "required": ["intent"]
                }
            }]
            
            response = await openai_client.create_chat_completion(
                messages=messages,
                tools=[{"type": "function", "function": functions[0]}],
                tool_choice={"type": "function", "function": {"name": "classify_intent"}}
            )
            
            if response.choices[0].message.tool_calls:
                tool_call = response.choices[0].message.tool_calls[0]
                result = json.loads(tool_call.function.arguments)
                intent = result.get("intent", "small_talk")
                detected_intents = result.get("detected_intents", [])
                confidence = result.get("confidence", "medium")
                
                logger.info(f"Classified intent: {intent}, detected_intents: {detected_intents}, confidence: {confidence}")
                
                return {
                    "intent": intent,
                    "detected_intents": detected_intents,
                    "confidence": confidence
                }
            
            return {"intent": "small_talk", "detected_intents": [], "confidence": "low"}
        
        except Exception as e:
            logger.error(f"Intent classification error: {e}")
            return {"intent": "small_talk", "detected_intents": [], "confidence": "low"}
