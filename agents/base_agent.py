"""Base agent class following OpenAI SDK patterns."""
from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
from services import openai_client
from tools.base_tool import BaseTool
from services.logger import get_logger
import json


logger = get_logger(__name__)


class BaseAgent(ABC):
    """Base class for all agents following OpenAI best practices"""
    
    def __init__(
        self,
        name: str,
        system_prompt: str,
        tools: Optional[List[BaseTool]] = None,
        tool_choice: Optional[Any] = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.tool_choice = tool_choice
        self.conversation_history: List[Dict[str, str]] = []
    
    def _get_tools_spec(self) -> Optional[List[Dict[str, Any]]]:
        """Get OpenAI tools specification"""
        if not self.tools:
            return None
        return [tool.to_openai_tool() for tool in self.tools]
    
    def _add_message(self, role: str, content: str):
        """Add message to conversation history"""
        self.conversation_history.append({
            "role": role,
            "content": content
        })
    
    def reset_history(self):
        """Reset conversation history"""
        self.conversation_history = []
    
    async def run(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        max_iterations: int = 5
    ) -> Dict[str, Any]:
        """
        Run the agent with user message and optional context
        
        Args:
            user_message: User's input message
            context: Additional context (draft_id, user_id, etc.)
            max_iterations: Max tool call iterations
        
        Returns:
            Dict with:
                - response: Final text response
                - tool_calls: List of tool calls made
                - success: Whether execution succeeded
        """
        try:
            def _forced_tool_name() -> Optional[str]:
                tc = self.tool_choice
                if isinstance(tc, dict):
                    fn = (tc.get("function") or {}) if isinstance(tc.get("function"), dict) else {}
                    name = fn.get("name")
                    return str(name) if name else None
                return None

            def _shrink_tool_result(tool_name: str, result: Any) -> Any:
                """Reduce token/payload bloat for tools whose results can be huge."""

                if tool_name != "search_listings":
                    return result

                if not isinstance(result, dict):
                    return result

                data = result.get("data")
                if not isinstance(data, dict):
                    return result

                listings = data.get("listings")
                if not isinstance(listings, list):
                    return result

                preview: list[dict[str, Any]] = []
                for item in listings[:10]:
                    if not isinstance(item, dict):
                        continue
                    preview.append(
                        {
                            "id": item.get("id"),
                            "title": item.get("title"),
                            "price": item.get("price"),
                            "category": item.get("category"),
                            "location": item.get("location"),
                            "image_url": item.get("image_url"),
                            "status": item.get("status"),
                        }
                    )

                new_data = dict(data)
                new_data["listings"] = preview
                out = dict(result)
                out["data"] = new_data
                return out

            # Start fresh conversation
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message}
            ]
            
            # Add context to system message if provided
            if context:
                context_msg = f"\n\nContext: {json.dumps(context)}"
                messages[0]["content"] += context_msg
            
            tool_calls_made = []
            iteration = 0
            forced = _forced_tool_name()
            executed_forced_once = False
            
            while iteration < max_iterations:
                iteration += 1
                
                # Get completion from OpenAI
                response = await openai_client.create_chat_completion(
                    messages=messages,
                    tools=self._get_tools_spec(),
                    tool_choice=self.tool_choice,
                )
                
                assistant_message = response.choices[0].message
                
                # Check if agent wants to call tools
                if assistant_message.tool_calls:
                    # Add assistant message with tool calls
                    messages.append({
                        "role": "assistant",
                        "content": assistant_message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            }
                            for tc in assistant_message.tool_calls
                        ]
                    })
                    
                    # Execute each tool call
                    for tool_call in assistant_message.tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = tool_call.function.arguments
                        
                        logger.info(f"Agent {self.name} calling tool: {tool_name}")
                        
                        # Find and execute tool
                        tool = next((t for t in self.tools if t.name == tool_name), None)
                        if tool:
                            try:
                                args = json.loads(tool_args)
                                
                                # SECURITY: Auto-inject user_id from context if tool accepts it
                                if context and "user_id" in context:
                                    # Only add user_id if tool has this parameter and it's not already set
                                    import inspect
                                    sig = inspect.signature(tool.execute)
                                    if "user_id" in sig.parameters and "user_id" not in args:
                                        args["user_id"] = context["user_id"]

                                # Enforce single-call tools when tool_choice forces them.
                                if forced == "search_listings" and tool_name == "search_listings":
                                    if executed_forced_once:
                                        continue
                                
                                result = await tool.execute(**args)
                                tool_calls_made.append({
                                    "tool": tool_name,
                                    "args": args,
                                    "result": result
                                })

                                if forced == "search_listings" and tool_name == "search_listings":
                                    executed_forced_once = True
                                
                                # Add tool response to messages
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": json.dumps(_shrink_tool_result(tool_name, result))
                                })
                            except Exception as e:
                                logger.error(f"Tool execution error: {e}")
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": json.dumps({"error": str(e)})
                                })
                        else:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({"error": f"Tool {tool_name} not found"})
                            })
                    
                    # Special-case: for forced search_listings agents, stop after first tool call.
                    if forced == "search_listings" and executed_forced_once:
                        return {
                            "response": None,
                            "tool_calls": tool_calls_made,
                            "success": True,
                        }

                    # Continue loop to get next response
                    continue
                
                # No more tool calls, return final response
                return {
                    "response": assistant_message.content,
                    "tool_calls": tool_calls_made,
                    "success": True
                }
            
            # Max iterations reached
            return {
                "response": "Maximum iterations reached. Please try again.",
                "tool_calls": tool_calls_made,
                "success": False
            }
        
        except Exception as e:
            logger.error(f"Agent {self.name} error: {e}")
            return {
                "response": f"Agent error: {str(e)}",
                "tool_calls": [],
                "success": False
            }
    
    async def run_simple(self, user_message: str) -> str:
        """
        Simple run without tools (for simple agents like SmallTalk)
        
        Args:
            user_message: User's input
        
        Returns:
            Agent's text response
        """
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message}
            ]
            
            response = await openai_client.create_chat_completion(messages=messages)
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Agent {self.name} simple run error: {e}")
            return f"Error: {str(e)}"
