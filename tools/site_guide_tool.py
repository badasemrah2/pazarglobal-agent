"""
Site guide tool for small-talk assistance.
"""
from __future__ import annotations

from typing import Dict, Any, List
from pathlib import Path
from .base_tool import BaseTool


GUIDE_PATH = Path(__file__).resolve().parents[1] / "docs" / "site_guide.txt"


def _load_guide() -> str:
    try:
        return GUIDE_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""


def _split_sections(text: str) -> List[Dict[str, str]]:
    sections: List[Dict[str, str]] = []
    current_title = "Genel"
    current_body: List[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_body:
                sections.append({
                    "title": current_title.strip(),
                    "content": "\n".join(current_body).strip(),
                })
            current_title = line.replace("##", "").strip()
            current_body = []
        else:
            current_body.append(line)

    if current_body:
        sections.append({
            "title": current_title.strip(),
            "content": "\n".join(current_body).strip(),
        })

    return sections


def _rank_sections(sections: List[Dict[str, str]], query: str) -> List[Dict[str, str]]:
    if not query:
        return sections[:2]

    q = query.lower()
    scored = []
    for sec in sections:
        hay = f"{sec.get('title', '')}\n{sec.get('content', '')}".lower()
        score = sum(1 for tok in q.split() if tok and tok in hay)
        if score > 0:
            scored.append((score, sec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:2]]


class ReadSiteGuideTool(BaseTool):
    """Tool to read user-facing site guide content."""

    def get_name(self) -> str:
        return "read_site_guide"

    def get_description(self) -> str:
        return "Read user-facing site guide sections to answer platform usage questions."

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "User question or keywords"
                }
            },
            "required": []
        }

    async def execute(self, query: str = "") -> Dict[str, Any]:
        text = _load_guide()
        if not text:
            return self.format_error("guide_not_available")

        sections = _split_sections(text)
        picks = _rank_sections(sections, query or "")
        if not picks:
            return self.format_success({
                "sections": [],
                "message": "İlgili rehber bölümü bulunamadı."
            })

        return self.format_success({
            "sections": picks,
            "message": "Rehberden ilgili bölümler getirildi."
        })


read_site_guide_tool = ReadSiteGuideTool()
