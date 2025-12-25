"""
Base tool class following OpenAI function calling patterns
"""
from typing import Dict, Any, Optional, Callable
from abc import ABC, abstractmethod
import json


class BaseTool(ABC):
    """Base class for all agent tools following OpenAI function calling spec"""
    
    def __init__(self):
        self.name = self.get_name()
        self.description = self.get_description()
        self.parameters = self.get_parameters()
    
    @abstractmethod
    def get_name(self) -> str:
        """Return tool name"""
        pass
    
    @abstractmethod
    def get_description(self) -> str:
        """Return tool description"""
        pass
    
    @abstractmethod
    def get_parameters(self) -> Dict[str, Any]:
        """Return tool parameters schema (JSON Schema format)"""
        pass
    
    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Execute the tool with given parameters
        
        Returns:
            Dict with:
                - success: bool
                - data: Any (result data)
                - error: Optional[str] (error message if failed)
        """
        pass
    
    def to_openai_tool(self) -> Dict[str, Any]:
        """
        Convert tool to OpenAI function calling format
        
        Returns:
            Tool definition dict for OpenAI API
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }
    
    def format_success(self, data: Any) -> Dict[str, Any]:
        """Format successful execution result"""
        return {
            "success": True,
            "data": data,
            "error": None
        }
    
    def format_error(self, error: str) -> Dict[str, Any]:
        """Format error result"""
        return {
            "success": False,
            "data": None,
            "error": error
        }


class ToolRegistry:
    """Registry for managing tools"""
    
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
    
    def register(self, tool: BaseTool):
        """Register a tool"""
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name"""
        return self._tools.get(name)
    
    def get_all(self) -> Dict[str, BaseTool]:
        """Get all registered tools"""
        return self._tools
    
    def to_openai_tools(self) -> list:
        """Convert all tools to OpenAI format"""
        return [tool.to_openai_tool() for tool in self._tools.values()]
    
    async def execute_tool(self, name: str, arguments: str) -> Dict[str, Any]:
        """Execute a tool by name with JSON arguments"""
        tool = self.get(name)
        if not tool:
            return {
                "success": False,
                "data": None,
                "error": f"Tool '{name}' not found"
            }
        
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            return await tool.execute(**args)
        except Exception as e:
            return {
                "success": False,
                "data": None,
                "error": f"Tool execution error: {str(e)}"
            }


# Global tool registry
tool_registry = ToolRegistry()
