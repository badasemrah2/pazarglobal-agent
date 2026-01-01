"""
OpenAI client wrapper following official SDK patterns
"""
from openai import AsyncOpenAI
from typing import Optional, List, Dict, Any
from config import settings
from loguru import logger


class OpenAIClient:
    """OpenAI API client wrapper"""
    
    def __init__(self):
        self._client: Optional[AsyncOpenAI] = None
    
    @property
    def client(self) -> AsyncOpenAI:
        """Get or create OpenAI client"""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client
    
    async def create_chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None
    ) -> Any:
        """
        Create a chat completion following OpenAI SDK patterns
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Model to use (defaults to settings)
            temperature: Temperature for generation
            max_tokens: Max tokens to generate
            tools: List of tool definitions
            tool_choice: Tool choice strategy
        
        Returns:
            OpenAI ChatCompletion response
        """
        try:
            params = {
                "model": model or settings.openai_model,
                "messages": messages,
                "temperature": temperature or settings.openai_temperature,
                "max_tokens": max_tokens or settings.openai_max_tokens
            }
            
            if tools:
                params["tools"] = tools
                params["tool_choice"] = tool_choice or "auto"
            
            response = await self.client.chat.completions.create(**params)
            return response
        except Exception as e:
            logger.error(f"Error creating chat completion: {e}")
            raise
    
    async def create_vision_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None
    ) -> Any:
        """
        Create a vision completion for image analysis
        
        Args:
            messages: List of message dicts (can include image URLs)
            model: Vision model to use
            max_tokens: Max tokens to generate
        
        Returns:
            OpenAI ChatCompletion response
        """
        try:
            params: Dict[str, Any] = {
                "model": model or settings.openai_vision_model,
                "messages": messages,
                "max_tokens": max_tokens or 500
            }
            if response_format:
                params["response_format"] = response_format

            response = await self.client.chat.completions.create(**params)
            return response
        except Exception as e:
            error_text = str(e)
            logger.error(f"Error creating vision completion: {e}")

            # Fallback: if deployment still points to a deprecated vision model via env,
            # retry with a known-good current model.
            if ("model_not_found" in error_text) or ("deprecated" in error_text):
                try:
                    fallback_params = dict(params)
                    fallback_params["model"] = "gpt-4o-mini"
                    response = await self.client.chat.completions.create(**fallback_params)
                    return response
                except Exception as fallback_error:
                    logger.error(f"Vision fallback model failed: {fallback_error}")
                    raise

            raise
    
    async def parse_tool_calls(self, response: Any) -> List[Dict[str, Any]]:
        """
        Parse tool calls from OpenAI response
        
        Args:
            response: OpenAI ChatCompletion response
        
        Returns:
            List of tool call dicts with name and arguments
        """
        tool_calls = []
        
        if response.choices[0].message.tool_calls:
            for tool_call in response.choices[0].message.tool_calls:
                tool_calls.append({
                    "id": tool_call.id,
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments
                })
        
        return tool_calls
    
    async def create_tool_response_message(
        self,
        tool_call_id: str,
        content: str
    ) -> Dict[str, str]:
        """
        Create a tool response message for the conversation
        
        Args:
            tool_call_id: ID of the tool call
            content: Tool execution result
        
        Returns:
            Message dict for conversation
        """
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content
        }


# Global instance
openai_client = OpenAIClient()
