"""MMF-007 Phase 8：Provider-neutral Visual Planner Contract + Mock Planner。

任何 Planner（Kimi/Qwen/GLM/Mock）输出同一 Slide Contract：
{slide_id, archetype, title, message, content_blocks, visual_intent, visual_data,
 image_requirement, source_requirements, source_scoring_items}
"""
from __future__ import annotations

from typing import Any

SLIDE_CONTRACT_KEYS = {
    "slide_id", "archetype", "title", "message", "content_blocks",
    "visual_intent", "visual_data", "image_requirement",
    "source_requirements", "source_scoring_items",
}
ALLOWED_ARCHETYPES = {
    "Cover", "Agenda", "Section Divider", "Hero Message", "Image + Text", "Cards",
    "Process", "Timeline", "Matrix", "Org Chart", "KPI", "Comparison", "Case",
    "Architecture", "Closing",
}


def validate_slide_contract(payload: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(SLIDE_CONTRACT_KEYS - set(payload))
    archetype = str(payload.get("archetype") or "")
    unknown_archetype = archetype not in ALLOWED_ARCHETYPES
    return {
        "schema_version": "slide-contract-validation-v0.1",
        "status": "PASS" if not missing and not unknown_archetype else "FAIL",
        "missing_keys": missing,
        "unknown_archetype": archetype if unknown_archetype else None,
    }


def mock_visual_planner(storyboard_row: dict[str, Any], index: int = 0) -> dict[str, Any]:
    """确定性 Mock Planner：从 Storyboard 行生成合法 Slide Contract（Synthetic）。"""
    role = str(storyboard_row.get("slide_role") or "detail")
    archetype = {
        "opening": "Cover", "summary": "Hero Message", "process": "Process",
        "comparison": "Comparison", "case": "Case", "evidence": "Table",
        "system": "Architecture", "strategy": "Cards", "insight": "Hero Message",
        "detail": "Cards",
    }.get(role, "Cards")
    return {
        "slide_id": str(storyboard_row.get("slide_id") or f"SL-{index:03d}"),
        "archetype": archetype,
        "title": str(storyboard_row.get("slide_title") or ""),
        "message": str(storyboard_row.get("core_message") or ""),
        "content_blocks": [
            {"type": "point", "title": str(point)[:30], "description": str(point)}
            for point in (storyboard_row.get("supporting_points") or [])[:4]
        ],
        "visual_intent": str(storyboard_row.get("required_visual") or "cards"),
        "visual_data": {},
        "image_requirement": "none",
        "source_requirements": [str(r) for r in storyboard_row.get("source_requirements") or []],
        "source_scoring_items": [],
    }
