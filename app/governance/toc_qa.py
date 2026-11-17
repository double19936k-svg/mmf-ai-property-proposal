from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# §18-§23：一页装不下整本目录，超过这个占比就是"页码没有真的分页"。
SAME_PAGE_LIMIT = 0.8
PAGEREF = re.compile(r"PAGEREF\s+(\S+)", re.I)


def evaluate_native_toc(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        settings = ET.fromstring(archive.read("word/settings.xml"))
    paragraphs = root.findall(f".//{W}p")
    heading_counts = {1: 0, 2: 0, 3: 0}
    bookmark_text: dict[str, str] = {}
    zero_length = 0
    for paragraph in paragraphs:
        style = paragraph.find(f"./{W}pPr/{W}pStyle")
        value = style.get(f"{W}val", "") if style is not None else ""
        for level in heading_counts:
            if value in {f"Heading{level}", str(level)}:
                heading_counts[level] += 1
        children = list(paragraph)
        starts = {child.get(f"{W}id"): (index, child.get(f"{W}name", "")) for index, child in enumerate(children) if child.tag == f"{W}bookmarkStart"}
        ends = {child.get(f"{W}id"): index for index, child in enumerate(children) if child.tag == f"{W}bookmarkEnd"}
        for bookmark_id, (start_index, name) in starts.items():
            end_index = ends.get(bookmark_id, start_index)
            text = "".join(node.text or "" for child in children[start_index + 1:end_index] for node in child.iter(f"{W}t"))
            bookmark_text[name] = text
            if not text.strip():
                zero_length += 1
    instructions = " ".join(node.text or "" for node in root.findall(f".//{W}instrText"))
    toc_field = "TOC" in instructions
    pageref_count = instructions.count("PAGEREF")
    depth = 2 if '1-2' in instructions else 1
    anchors = [node.get(f"{W}anchor", "") for node in root.findall(f".//{W}hyperlink")]
    toc_anchors = [value for value in anchors if value.startswith("MMF_TOC_")]
    expected_entries = heading_counts[1] + (heading_counts[2] if depth >= 2 else 0)
    valid_targets = sum(bool(bookmark_text.get(anchor, "").strip()) for anchor in toc_anchors)
    update_node = settings.find(f".//{W}updateFields")
    update_on_open = update_node is not None and update_node.get(f"{W}val", "true") not in {"false", "0"}
    continuation_titles = sum("目录（续）" in "".join(node.text or "" for node in p.iter(f"{W}t")) for p in paragraphs)
    displayed_pages = _displayed_pages(path)
    identical_share = _identical_share(displayed_pages)
    # The cached result is what a reader sees before any field refresh: eleven
    # rows all saying "1" is a broken directory no matter how well-formed the
    # field structure is. It is reported as its own measured fact rather than
    # folded into the structural verdict, because a machine without a page
    # renderer must still deliver the file — as NEEDS_REVIEW, not as a block.
    page_variance_pass = not displayed_pages or identical_share < SAME_PAGE_LIMIT
    status = "PASS" if toc_field and expected_entries > 0 and len(toc_anchors) == expected_entries and pageref_count == expected_entries and valid_targets == expected_entries and zero_length == 0 and update_on_open and continuation_titles == 0 else "FAIL"
    return {
        "schema_version": "native-toc-qa-v0.1", "status": status,
        "native_toc": toc_field, "toc_depth": depth, "toc_entry_count": len(toc_anchors), "pageref_count": pageref_count,
        "valid_target_count": valid_targets, "heading1_count": heading_counts[1],
        "heading2_count": heading_counts[2], "heading3_count": heading_counts[3],
        "bookmark_count": len(bookmark_text), "zero_length_bookmark_count": zero_length,
        "update_fields_on_open": update_on_open, "toc_continuation_title_count": continuation_titles,
        "toc_displayed_pages": displayed_pages,
        "toc_distinct_page_count": len({value for value in displayed_pages if value}),
        "toc_identical_page_share": identical_share,
        "toc_page_variance_pass": page_variance_pass,
        "wps_compatible_structure": status == "PASS",
    }


def _displayed_pages(path: Path) -> list[str]:
    """Cached PAGEREF results in directory order ("" when a row has none)."""
    pages: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except Exception:
        return pages
    for paragraph in root.iter(f"{W}p"):
        anchor = ""
        for instruction in paragraph.iter(f"{W}instrText"):
            match = PAGEREF.search(instruction.text or "")
            if match:
                anchor = match.group(1)
                break
        if not anchor:
            continue
        after_separate = False
        cached = ""
        for run in paragraph.findall(f"{W}r"):
            field = run.find(f"{W}fldChar")
            instruction = run.find(f"{W}instrText")
            if instruction is not None and anchor.upper() in (instruction.text or "").upper():
                after_separate = False
                continue
            if field is not None and field.get(f"{W}fldCharType") == "separate":
                after_separate = True
                continue
            if after_separate:
                node = run.find(f"{W}t")
                cached = (node.text or "").strip() if node is not None else ""
                break
        pages.append(cached)
    return pages


def _identical_share(values: list[str]) -> float:
    filled = [value for value in values if value]
    if len(filled) < 2:
        return 0.0
    counts: dict[str, int] = {}
    for value in filled:
        counts[value] = counts.get(value, 0) + 1
    return round(max(counts.values()) / len(filled), 4)
