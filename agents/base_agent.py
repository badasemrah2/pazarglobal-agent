"""
Base agent class following OpenAI SDK patterns
"""
from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
from services import openai_client
from tools.base_tool import BaseTool
from loguru import logger
import json


class BaseAgent(ABC):
    """Base class for all agents following OpenAI best practices"""
    
    def __init__(self, name: str, system_prompt: str, tools: Optional[List[BaseTool]] = None):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools or []
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
            
            while iteration < max_iterations:
                iteration += 1
                
                # Get completion from OpenAI
                response = await openai_client.create_chat_completion(
                    messages=messages,
                    tools=self._get_tools_spec()
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
                                result = await tool.execute(**args)
                                tool_calls_made.append({
                                    "tool": tool_name,
                                    "args": args,
                                    "result": result
                                })
                                
                                # Add tool response to messages
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "content": json.dumps(result)
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
