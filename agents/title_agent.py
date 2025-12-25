"""
Title Agent - Handles listing title generation and editing
"""
from .base_agent import BaseAgent
from config.prompts import TITLE_AGENT_PROMPT
from tools import update_title_tool, read_draft_tool


class TitleAgent(BaseAgent):
    """Agent for generating and editing listing titles"""
    
    def __init__(self):
        super().__init__(
            name="TitleAgent",
            system_prompt=TITLE_AGENT_PROMPT,
            tools=[read_draft_tool, update_title_tool]
        )
