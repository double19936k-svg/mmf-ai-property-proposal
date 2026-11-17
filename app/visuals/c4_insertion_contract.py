# -*- coding: utf-8 -*-
"""production_ppt_insertion_contract — mirrored-from-production.

Source: app/build_ppt.mjs (Cover always; Divider on first content slide
whose chapter_id is in artifact.chapters AND chapter_id !== "P01";
then the content slide; Closing always).

Does not inspect IR SectionDivider slides. No sidecar-only insertion rules.
"""
from __future__ import annotations

from typing import Any

MIRRORED_FROM = "app/build_ppt.mjs Cover + non-P01 first-seen Divider + content + Closing"


def _chapter_ids(chapters: list[dict[str, Any]]) -> list[str]:
    out = []
    for row in chapters or []:
        cid = str(row.get("chapter_id") or "").strip()
        if cid:
            out.append(cid)
    return out


def simulate_build_ppt_mjs_insertion(artifact: dict[str, Any]) -> dict[str, Any]:
    """Line-level transcription of build_ppt.mjs insertion loop. Oracle for parity."""
    slides = list(artifact.get("slides") or [])
    chapters = list(artifact.get("chapters") or [])
    # dividerChapterIds = chapters where chapter_id && chapter_id !== "P01"
    divider_by_chapter = {
        str(row.get("chapter_id")): row
        for row in chapters
        if row.get("chapter_id") and str(row.get("chapter_id")) != "P01"
    }
    seen: set[str] = set()
    divider_plan: list[str] = []
    events: list[dict[str, Any]] = [{"role": "cover"}]
    for data in slides:
        cid = str(data.get("chapter_id") or "")
        if cid and cid in divider_by_chapter and cid not in seen:
            seen.add(cid)
            divider_plan.append(cid)
            events.append({"role": "divider", "chapter_id": cid, "before_slide": data.get("slide_id")})
        events.append({"role": "content", "slide_id": data.get("slide_id"), "chapter_id": cid})
    events.append({"role": "closing"})
    physical = len(events)  # cover + dividers + content + closing
    # totalPages in JS = slides.length + 1 + dividerTotal  (footer var, excludes closing)
    footer_total_pages = len(slides) + 1 + len(divider_plan)
    return {
        "production_divider_plan": divider_plan,
        "predicted_physical_slide_count": physical,
        "footer_total_pages_js": footer_total_pages,
        "content_slides": len(slides),
        "cover": 1,
        "closing": 1,
        "events": events,
    }


def production_ppt_insertion_contract(artifact: dict[str, Any]) -> dict[str, Any]:
    """Public adapter API. Same insertion as simulate_build_ppt_mjs_insertion.

    Also reports chapter/slide inconsistency instead of inventing chapters.
    """
    slides = list(artifact.get("slides") or [])
    chapters = list(artifact.get("chapters") or [])
    chapter_ids = _chapter_ids(chapters)
    chapter_set = set(chapter_ids)
    issues: list[dict[str, Any]] = []
    for i, s in enumerate(slides):
        cid = str(s.get("chapter_id") or "")
        if cid and cid not in chapter_set:
            issues.append({
                "kind": "SLIDE_CHAPTER_NOT_IN_ARTIFACT",
                "index": i,
                "slide_id": s.get("slide_id"),
                "chapter_id": cid,
            })
    for cid in chapter_ids:
        if not any(str(s.get("chapter_id") or "") == cid for s in slides):
            issues.append({"kind": "CHAPTER_NEVER_REFERENCED", "chapter_id": cid})

    sim = simulate_build_ppt_mjs_insertion(artifact)
    return {
        "mirrored_from": MIRRORED_FROM,
        "sidecar_specific_rules": False,
        "production_divider_plan": sim["production_divider_plan"],
        "predicted_physical_slide_count": sim["predicted_physical_slide_count"],
        "content_slides": sim["content_slides"],
        "cover": 1,
        "closing": 1,
        "divider_count": len(sim["production_divider_plan"]),
        "footer_total_pages_js": sim["footer_total_pages_js"],
        "issues": issues,
        "status": "FAIL" if issues else "PASS",
        "page_plan": sim["events"],
    }
