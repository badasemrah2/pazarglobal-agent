"""
Intent Router Agent - Routes user messages to appropriate workflow
"""
from .base_agent import BaseAgent
from config.prompts import INTENT_ROUTER_PROMPT
from services import openai_client
import json
from loguru import logger


class IntentRouterAgent(BaseAgent):
    """Router agent to classify user intent"""
    
    def __init__(self):
        super().__init__(
            name="IntentRouter",
            system_prompt=INTENT_ROUTER_PROMPT,
            tools=[]  # No tools needed
        )
    
    async def classify_intent(self, user_message: str) -> str:
        """
        Classify user message into one of the intents
        
        Returns:
            Intent string: create_listing, publish_or_delete, search_listings, small_talk
        """
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Classify this message: {user_message}"}
            ]
            
            # Use function calling for structured output
            functions = [{
                "name": "classify_intent",
                "description": "Classify the user's intent",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "enum": ["create_listing", "publish_or_delete", "search_listings", "small_talk"],
                            "description": "The classified intent"
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
                logger.info(f"Classified intent: {intent}")
                return intent
            
            return "small_talk"  # Default fallback
        
        except Exception as e:
            logger.error(f"Intent classification error: {e}")
            return "small_talk"  # Safe fallback
