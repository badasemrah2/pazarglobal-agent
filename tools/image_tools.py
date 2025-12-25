"""
Image processing tools
"""
from typing import Dict, Any
import json
from .base_tool import BaseTool
from services import supabase_client, openai_client


class ProcessImageTool(BaseTool):
    """Tool to process and analyze product images"""
    
    def get_name(self) -> str:
        return "process_image"
    
    def get_description(self) -> str:
        return "Process product image: analyze content, detect category, check safety. Requires draft_id."
    
    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Draft ID (MANDATORY)"
                },
                "image_url": {
                    "type": "string",
                    "description": "URL of the image to process"
                }
            },
            "required": ["draft_id", "image_url"]
        }
    
    async def execute(self, draft_id: str, image_url: str) -> Dict[str, Any]:
        if not draft_id:
            return self.format_error("missing_listing_id: draft_id is required")
        
        try:
            # Analyze image using OpenAI Vision
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Analyze this product image. Determine: 1) Product category 2) Product condition 3) Key features visible 4) Any safety concerns. Respond in JSON format with keys: category, condition, features, safety_flags."
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url}
                        }
                    ]
                }
            ]
            
            response = await openai_client.create_vision_completion(messages)
            analysis_text = response.choices[0].message.content or "{}"
            try:
                analysis = json.loads(analysis_text)
            except Exception:
                analysis = {"raw": analysis_text}
            
            # Store image in database (draft images array)
            metadata = {"analysis": analysis}
            await supabase_client.add_listing_image(draft_id, image_url, metadata)
            
            # Update category/vision_product if detected
            detected_category = analysis.get("category") if isinstance(analysis, dict) else None
            await supabase_client.update_draft_category(
                draft_id,
                detected_category or "unspecified",
                vision_product=analysis if isinstance(analysis, dict) else {"raw": analysis_text}
            )
            
            return self.format_success({
                "image_url": image_url,
                "analysis": analysis,
                "stored": True
            })
        except Exception as e:
            return self.format_error(f"Image processing failed: {str(e)}")


# Tool instance
process_image_tool = ProcessImageTool()
