"""Public review-warning filter and location text.

This layer only decides which findings are shown to a person and how they are
addressed. It does not change Word generation, Word repair, or commitment rules.
"""
from __future__ import annotations

import re
from typing import Any

_EMPTY_CONSISTENCY = re.compile(r"未解决问题（\s*0\s*项）")
_RANGE = re.compile(r"第?\d+[—–\-~～至]\d+(?:天|日|小时|分钟)")
_RANGE_TAIL = re.compile(r"^\d+(?:天|日|小时|分钟)$")
_SLIDE = re.compile(r"slides\[(\d+)\]")
_CELL = re.compile(r"table\.rows\[(\d+)\]\[(\d+)\]")
_MODULE = re.compile(r"modules\[(\d+)\]")
_STEP = re.compile(r"steps\[(\d+)\]")
_SECTION = re.compile(r"sections\[(\d+)\]")
_PARA = re.compile(r"(?:paragraphs|body_blocks)\[(\d+)\]")
_TITLE_PATH = re.compile(r"(?:^|\.)(?:title|headline|header|footer)$|modules\[\d+\]\.title$")


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _path(row: dict[str, Any]) -> str:
    return str(row.get("path") or row.get("section") or "")


def _fragment(row: dict[str, Any]) -> str:
    return str(row.get("text") or row.get("excerpt") or row.get("remaining_problem") or "").strip()


def _sentence(row: dict[str, Any]) -> str:
    return str(row.get("sentence") or "").strip()


def _joined(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("issue", "excerpt", "text", "reason", "sentence", "remaining_problem"))


def false_review_fragment(row: dict[str, Any]) -> bool:
    """Drop empty consistency rows and fragments cut out of a normal phrase."""
    if _EMPTY_CONSISTENCY.search(_joined(row)):
        return True
    if row.get("unresolved_count") == 0 and "一致性检查" in _joined(row):
        return True
    fragment = _compact(_fragment(row))
    sentence = _compact(_sentence(row) or _fragment(row))
    if _RANGE_TAIL.fullmatch(fragment) and _RANGE.search(sentence + fragment):
        return True
    if fragment and sentence and fragment != sentence and re.search(rf"(?:或|及|与|和){re.escape(fragment)}", sentence):
        return True
    path = _path(row)
    label = sentence or fragment
    cell = _CELL.search(path)
    if cell and int(cell.group(2)) == 0 and 0 < len(label) <= 12:
        return True
    if _TITLE_PATH.search(path) and len(label) <= 12:
        return True
    return False


def _slide_pages(artifact: dict[str, Any] | None) -> dict[int, tuple[int, str]]:
    artifact = artifact or {}
    slides = list(artifact.get("slides") or [])
    service = artifact.get("profile_p_contract") == "profile-p-service-v1"
    divider_ids = {
        str(row.get("chapter_id"))
        for row in artifact.get("chapters") or []
        if row.get("chapter_id") and (service or row.get("chapter_id") != "P01")
    }
    page = 2 if service else 1
    seen: set[str] = set()
    found: dict[int, tuple[int, str]] = {}
    for index, slide in enumerate(slides):
        chapter = str(slide.get("chapter_id") or "")
        if chapter and chapter in divider_ids and chapter not in seen:
            seen.add(chapter)
            page += 1
        page += 1
        found[index] = (page, str(slide.get("title") or slide.get("slide_title") or ""))
    return found


def _cell_text(slide: dict[str, Any], path: str) -> str:
    match = _CELL.search(path)
    rows = (slide.get("table") or {}).get("rows") or []
    if not match:
        return ""
    row_index, column_index = int(match.group(1)), int(match.group(2))
    if row_index < len(rows) and column_index < len(rows[row_index]):
        return str(rows[row_index][column_index])
    return ""


def _component_label(path: str, slide: dict[str, Any] | None) -> str:
    slide = slide or {}
    cell = _CELL.search(path)
    if cell:
        row_index, column_index = int(cell.group(1)), int(cell.group(2))
        columns = (slide.get("table") or {}).get("columns") or []
        header = str(columns[column_index]) if column_index < len(columns) else ""
        suffix = f"[{header}]" if header else ""
        return f"表格第{row_index + 2}行第{column_index + 1}列{suffix}"
    module = _MODULE.search(path)
    if module:
        index = int(module.group(1))
        modules = slide.get("modules") or []
        title = str(modules[index].get("title") or "") if index < len(modules) and isinstance(modules[index], dict) else ""
        return f"卡片{index + 1:02d}[{title}]" if title else f"卡片{index + 1:02d}"
    step = _STEP.search(path)
    if step:
        index = int(step.group(1))
        steps = slide.get("steps") or []
        title = str(steps[index].get("title") or "") if index < len(steps) and isinstance(steps[index], dict) else ""
        return f"步骤{index + 1:02d}[{title}]" if title else f"步骤{index + 1:02d}"
    return "正文"


def locate_review_issue(row: dict[str, Any], artifact: dict[str, Any] | None = None, medium: str = "") -> dict[str, Any]:
    path = _path(row)
    slides = list((artifact or {}).get("slides") or [])
    sections = list((artifact or {}).get("sections") or [])
    slide_match = _SLIDE.search(path)
    slide = slides[int(slide_match.group(1))] if slide_match and int(slide_match.group(1)) < len(slides) else {}
    pages = _slide_pages(artifact)
    slide_index = slide_title = None
    if slide_match and int(slide_match.group(1)) in pages:
        slide_index, slide_title = pages[int(slide_match.group(1))]
    section_match = _SECTION.search(path)
    section = sections[int(section_match.group(1))] if section_match and int(section_match.group(1)) < len(sections) else {}
    section_no = int(section_match.group(1)) + 1 if section_match else None
    section_title = str(section.get("heading") or section.get("title") or "")
    para_match = _PARA.search(path)
    para_index = int(para_match.group(1)) + 1 if para_match else (1 if section_match else None)
    fragment = _fragment(row) or "该处"
    full_sentence = _cell_text(slide, path) or _sentence(row) or fragment
    component = _component_label(path, slide if slide_match else None)
    suggestion = str(row.get("recommended_manual_action") or "请按所在原句写清谁在做什么、做到什么程度；时间区间和并列宾语保持原样。")
    word = str(medium or "").upper() == "WORD" or bool(section_match)
    if word:
        head = f"【第 {section_no or 1} 章《{section_title or '未命名章节'}》· 第 {para_index or 1} 段】"
    elif slide_index:
        head = f"【第 {slide_index} 页《{slide_title or '未命名页面'}》· {component}】"
    else:
        head = "【全篇】"
    display = f"{head}存在待复核表述：发现片段“{fragment}”（所在原句：“{full_sentence}”）。建议修改：{suggestion}"
    return {
        "slide_index": slide_index,
        "slide_title": slide_title or "",
        "component_label": component,
        "full_sentence": full_sentence,
        "fragment": fragment,
        "section_no": section_no,
        "section_title": section_title,
        "para_index": para_index,
        "display": display,
    }


def prepare_review_issues(
    issues: list[dict[str, Any]] | None,
    *,
    artifact: dict[str, Any] | None = None,
    medium: str = "",
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in issues or []:
        if not isinstance(row, dict) or false_review_fragment(row):
            continue
        prepared.append({**row, **locate_review_issue(row, artifact, medium)})
    return prepared
