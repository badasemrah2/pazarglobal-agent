"""
Publish/Delete Agent - Handles listing publication and deletion
"""
from .base_agent import BaseAgent
from config.prompts import PUBLISH_DELETE_AGENT_PROMPT
from tools import (
    publish_listing_tool,
    delete_listing_tool,
    get_wallet_balance_tool,
    deduct_credits_tool,
    read_draft_tool
)


class PublishDeleteAgent(BaseAgent):
    """Agent for publishing and deleting listings"""
    
    def __init__(self):
        super().__init__(
            name="PublishDeleteAgent",
            system_prompt=PUBLISH_DELETE_AGENT_PROMPT,
            tools=[
                read_draft_tool,
                get_wallet_balance_tool,
                deduct_credits_tool,
                publish_listing_tool,
                delete_listing_tool
            ]
        )
