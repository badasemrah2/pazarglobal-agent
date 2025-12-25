"""
Price Agent - Handles price extraction and normalization
"""
from .base_agent import BaseAgent
from config.prompts import PRICE_AGENT_PROMPT
from tools import update_price_tool, read_draft_tool


class PriceAgent(BaseAgent):
    """Agent for extracting and normalizing prices"""
    
    def __init__(self):
        super().__init__(
            name="PriceAgent",
            system_prompt=PRICE_AGENT_PROMPT,
            tools=[read_draft_tool, update_price_tool]
        )
