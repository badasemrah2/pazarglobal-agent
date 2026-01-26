"""
Small Talk Agent - Handles general conversation and platform questions
"""
from .base_agent import BaseAgent
from config.prompts import SMALL_TALK_AGENT_PROMPT
from tools import read_site_guide_tool


class SmallTalkAgent(BaseAgent):
    """Agent for handling small talk and platform information"""
    
    def __init__(self):
        super().__init__(
            name="SmallTalkAgent",
            system_prompt=SMALL_TALK_AGENT_PROMPT,
            tools=[read_site_guide_tool]
        )
