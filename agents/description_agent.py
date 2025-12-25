"""
Description Agent - Handles listing description generation and editing
"""
from .base_agent import BaseAgent
from config.prompts import DESCRIPTION_AGENT_PROMPT
from tools import update_description_tool, read_draft_tool


class DescriptionAgent(BaseAgent):
    """Agent for generating and editing listing descriptions"""
    
    def __init__(self):
        super().__init__(
            name="DescriptionAgent",
            system_prompt=DESCRIPTION_AGENT_PROMPT,
            tools=[read_draft_tool, update_description_tool]
        )
