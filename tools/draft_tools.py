"""
Draft management tools
"""
from typing import Dict, Any
from .base_tool import BaseTool
from services import supabase_client


class CreateDraftTool(BaseTool):
    """Tool to create a new draft listing"""
    
    def get_name(self) -> str:
        return "create_draft"
    
    def get_description(self) -> str:
        return "Create a new draft listing for the user. Returns draft_id that MUST be used for all subsequent operations."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "User ID from session"
                },
                "phone_number": {
                    "type": "string",
                    "description": "User's phone number"
                }
            },
            "required": ["user_id", "phone_number"]
        }
    
    async def execute(self, user_id: str, phone_number: str) -> Dict[str, Any]:
        try:
            draft = await supabase_client.create_draft(user_id, phone_number)
            return self.format_success({
                "draft_id": draft["id"],
                "message": "Draft created successfully. Use this draft_id for all updates."
            })
        except Exception as e:
            return self.format_error(str(e))


class ReadDraftTool(BaseTool):
    """Tool to read draft details"""
    
    def get_name(self) -> str:
        return "read_draft"
    
    def get_description(self) -> str:
        return "Read the current state of a draft listing. ALWAYS call this before making updates."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Draft ID to read"
                }
            },
            "required": ["draft_id"]
        }
    
    async def execute(self, draft_id: str) -> Dict[str, Any]:
        draft = await supabase_client.get_draft(draft_id)
        if draft:
            return self.format_success(draft)
        return self.format_error("Draft not found")


class UpdateTitleTool(BaseTool):
    """Tool to update draft title"""
    
    def get_name(self) -> str:
        return "update_title"
    
    def get_description(self) -> str:
        return "Update the title of a draft listing. Requires valid draft_id."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Draft ID to update (MANDATORY)"
                },
                "title": {
                    "type": "string",
                    "description": "New title (max 100 characters)"
                }
            },
            "required": ["draft_id", "title"]
        }
    
    async def execute(self, draft_id: str, title: str) -> Dict[str, Any]:
        if not draft_id:
            return self.format_error("missing_listing_id: draft_id is required")
        
        success = await supabase_client.update_draft_title(draft_id, title)
        if success:
            return self.format_success({"title": title, "updated": True})
        return self.format_error("Failed to update title")


class UpdateDescriptionTool(BaseTool):
    """Tool to update draft description"""
    
    def get_name(self) -> str:
        return "update_description"
    
    def get_description(self) -> str:
        return "Update the description of a draft listing. Requires valid draft_id."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Draft ID to update (MANDATORY)"
                },
                "description": {
                    "type": "string",
                    "description": "New description"
                }
            },
            "required": ["draft_id", "description"]
        }
    
    async def execute(self, draft_id: str, description: str) -> Dict[str, Any]:
        if not draft_id:
            return self.format_error("missing_listing_id: draft_id is required")
        
        success = await supabase_client.update_draft_description(draft_id, description)
        if success:
            return self.format_success({"description": description, "updated": True})
        return self.format_error("Failed to update description")


class UpdatePriceTool(BaseTool):
    """Tool to update draft price"""
    
    def get_name(self) -> str:
        return "update_price"
    
    def get_description(self) -> str:
        return "Update the price of a draft listing. Requires valid draft_id."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Draft ID to update (MANDATORY)"
                },
                "price": {
                    "type": "number",
                    "description": "Normalized price (numeric only)"
                }
            },
            "required": ["draft_id", "price"]
        }
    
    async def execute(self, draft_id: str, price: float) -> Dict[str, Any]:
        if not draft_id:
            return self.format_error("missing_listing_id: draft_id is required")
        
        success = await supabase_client.update_draft_price(draft_id, price)
        if success:
            return self.format_success({"price": price, "updated": True})
        return self.format_error("Failed to update price")


# Tool instances
create_draft_tool = CreateDraftTool()
read_draft_tool = ReadDraftTool()
update_title_tool = UpdateTitleTool()
update_description_tool = UpdateDescriptionTool()
update_price_tool = UpdatePriceTool()
