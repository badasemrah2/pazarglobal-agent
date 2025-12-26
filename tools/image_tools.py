"""
Image processing tools
"""
from typing import Dict, Any
import json
from .base_tool import BaseTool
from services import supabase_client, openai_client


ALLOWED_CATEGORIES = [
    "Elektronik",
    "Otomotiv",
    "Emlak",
    "Mobilya & Dekorasyon",
    "Giyim & Aksesuar",
    "Gıda & İçecek",
    "Kozmetik & Kişisel Bakım",
    "Kozmetik & Bakım",
    "Kitap, Dergi & Müzik",
    "Spor & Outdoor",
    "Anne, Bebek & Oyuncak",
    "Hayvan & Pet Shop",
    "Yapı Market & Bahçe",
    "Hobi & Oyun",
    "Sanat & Zanaat",
    "İş & Sanayi",
    "Eğitim & Kurs",
    "Etkinlik & Bilet",
    "Hizmetler",
    "Diğer",
]


def normalize_category(raw_category: str) -> str:
    """Map model output into a stable, frontend-compatible category."""
    if not raw_category:
        return "Diğer"
    cat = str(raw_category).strip()
    if cat in ALLOWED_CATEGORIES:
        return cat

    lower = cat.lower()
    # Electronics
    if any(k in lower for k in ["bilgisayar", "laptop", "notebook", "dizüstü", "dizustu", "telefon", "tablet", "tv", "telev", "kamera", "kulaklık", "kulaklik", "playstation", "xbox"]):
        return "Elektronik"
    # Automotive
    if any(k in lower for k in ["araba", "otomobil", "motor", "motosiklet", "oto", "jant", "lastik", "aksesuar"]):
        return "Otomotiv"
    # Real estate
    if any(k in lower for k in ["ev", "daire", "arsa", "kiralık", "kiralik", "satılık", "satilik", "emlak", "ofis"]):
        return "Emlak"
    # Furniture
    if any(k in lower for k in ["mobilya", "koltuk", "masa", "sandalye", "dolap", "yatak", "dekor"]):
        return "Mobilya & Dekorasyon"
    # Clothing
    if any(k in lower for k in ["giyim", "ayakkabı", "ayakkabi", "çanta", "canta", "aksesuar", "mont", "elbise", "pantolon"]):
        return "Giyim & Aksesuar"

    return "Diğer"


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
        
        # Always store the image URL first so drafts don't end up with "no photos"
        # when vision analysis fails due to model/config issues.
        stored_ok = await supabase_client.add_listing_image(draft_id, image_url, metadata={})

        analysis: Dict[str, Any] = {}
        analysis_text = "{}"
        try:
            system_prompt = (
                "You are a marketplace vision assistant that returns concise Turkish JSON. "
                "Always respond with a single JSON object containing these keys: "
                "product (string), category (string), condition (string), features (array of up to 5 short strings), "
                "description (string), safety_flags (array of short warning strings). "
                "Never return an empty object. If unsure, make your best guess."
            )
            user_prompt = "Görseldeki ürünü analiz et ve JSON alanlarını doldur."

            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }
            ]

            response = await openai_client.create_vision_completion(
                messages,
                max_tokens=600,
                response_format={"type": "json_object"}
            )
            analysis_text = response.choices[0].message.content or "{}"
            try:
                parsed = json.loads(analysis_text)
                analysis = parsed if isinstance(parsed, dict) else {"summary": parsed}
            except Exception:
                analysis = {"summary": analysis_text}
        except Exception as vision_error:
            analysis = {"error": str(vision_error)}

        # Update image metadata with analysis (best-effort)
        try:
            await supabase_client.add_listing_image(draft_id, image_url, metadata={"analysis": analysis})
        except Exception:
            # If metadata update fails, keep the previously stored URL.
            pass

        # Best-effort: update category + vision_product for downstream draft summaries
        try:
            detected_category = ""
            if isinstance(analysis, dict):
                detected_category = str(analysis.get("category") or "").strip()
            detected_category = normalize_category(detected_category)
            await supabase_client.update_draft_category(
                draft_id,
                detected_category or "Diğer",
                vision_product=analysis if isinstance(analysis, dict) else {"raw": analysis_text}
            )
        except Exception:
            pass

        # Best-effort: auto-fill title/description if empty using vision output
        try:
            draft = await supabase_client.get_draft(draft_id)
            listing_data = (draft or {}).get("listing_data") or {}
            title_missing = not (listing_data.get("title") or "").strip()
            desc_missing = not (listing_data.get("description") or "").strip()

            if isinstance(analysis, dict):
                product = str(analysis.get("product") or analysis.get("category") or "").strip()
                description = str(analysis.get("description") or "").strip()
                features = analysis.get("features")
                if desc_missing and (not description) and isinstance(features, list) and features:
                    description = "Öne çıkan özellikler: " + ", ".join([str(f) for f in features[:5] if f])

                if title_missing and product:
                    await supabase_client.update_draft_title(draft_id, product[:100])
                if desc_missing and description:
                    await supabase_client.update_draft_description(draft_id, description)
        except Exception:
            pass

        return self.format_success({
            "image_url": image_url,
            "analysis": analysis,
            "stored": bool(stored_ok)
        })


# Tool instance
process_image_tool = ProcessImageTool()
