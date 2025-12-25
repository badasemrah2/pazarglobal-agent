"""
Image Agent - Handles image processing with vision capabilities
"""
from .base_agent import BaseAgent
from config.prompts import IMAGE_AGENT_PROMPT
from tools import process_image_tool, read_draft_tool


class ImageAgent(BaseAgent):
    """Agent for processing product images with vision"""
    
    def __init__(self):
        super().__init__(
            name="ImageAgent",
            system_prompt=IMAGE_AGENT_PROMPT,
            tools=[read_draft_tool, process_image_tool]
        )
