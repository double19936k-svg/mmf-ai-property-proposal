"""LOCAL_NORMALIZATION：装配前的确定性整备阶段。

本阶段只允许本地代码运行，不得调用任何 Provider。它处理的是"传输与序列化留下
的确定性问题"（残缺围栏、破折号、表格不成形、字段名漂移、控制字符），而不是内容
质量问题；内容质量由文档级 QA 判定。任何在此处被改写的内容都必须留下可审计记录。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable


CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
FENCE_LINE = re.compile(r"^\s*```[A-Za-z0-9_\-]*\s*$|^\s*```\s*[A-Za-z0-9_\-]*\s*$")
LEADING_HASH = re.compile(r"^\s*#{1,6}\s*")
LEADING_NUMBER = re.compile(r"^\s*(?:\d{1,2}\s*[\.、)\]]|第[一二三四五六七八九十]{1,3}步|步骤\s*\d{1,2})\s*[:：]?\s*")
TRAILING_JSON_TAIL = re.compile(r"(?:[\"}\]]\s*[,:]\s*){1,}[\"{\[]?[\s\W]*$")
# CU envelope tokens are transport syntax; a recovered block can carry a stray or
# duplicated terminator, and it would otherwise be printed into the customer DOCX.
TRANSPORT_RESIDUE = re.compile(
    r"[ \t]*<<<\s*(?:CU_START\s*:[^>\n]{0,80}|CU_START|CU_?END)\s*>>>[ \t]*\n*", re.I
)

BODY_BLOCK_TYPES = {
    "paragraph", "subheading", "bullet_group", "numbered_steps", "note", "summary",
}
LIST_BLOCK_TYPES = {"bullet_group", "numbered_steps"}
TABLE_ARCHETYPES = {
    "STANDARD_TABLE", "MATRIX", "KPI_TABLE", "RESPONSIBILITY_MATRIX",
    "RISK_REGISTER", "SCHEDULE", "FORM_TEMPLATE",
}
PROCESS_ARCHETYPES = {"SOP", "PROCESS", "EMERGENCY_PLAYBOOK"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "；".join(_text(item) for item in value if _text(item))
    if isinstance(value, dict):
        for key in ("content", "text", "value", "body", "description"):
            if _text(value.get(key)):
                return _text(value.get(key))
        return ""
    return str(value)


def has_transport_residue(value: Any) -> bool:
    raw = value if isinstance(value, str) else str(value)
    return bool(TRANSPORT_RESIDUE.search(raw))


def normalize_text(value: Any) -> str:
    """Encoding + serialization cleanup for one customer-facing string."""
    text = _text(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = CONTROL_CHARS.sub("", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = TRANSPORT_RESIDUE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_fences(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    kept = [line for line in lines if not FENCE_LINE.match(line)]
    changed = len(kept) != len(lines)
    body = "\n".join(kept).strip()
    if body.startswith("```") or body.endswith("```"):
        body = body.strip("`").strip()
        changed = True
    return body, changed


def cut_json_tail(text: str) -> tuple[str, bool]:
    """Drop a truncated JSON/brace tail left by a cut-off response.

    Only fires when the tail is clearly structural residue: an unbalanced quote
    run at the very end of an otherwise finished sentence.
    """
    if not text or len(text) < 24:
        return text, False
    if text.count('"') - text.count('\\"') < 2:
        return text, False
    match = TRAILING_JSON_TAIL.search(text)
    if not match or match.start() < len(text) - 24:
        return text, False
    head = text[: match.start()].rstrip()
    if len(head) < 12 or not re.search(r"[。；：！？.]$", head):
        return text, False
    return head, True


def _items(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [normalize_text(item) for item in value if normalize_text(item)]
    if isinstance(value, dict):
        nested = value.get("items") or value.get("points") or value.get("content")
        if nested is not None:
            return _items(nested)
        row = normalize_text(value)
        return [row] if row else []
    text = normalize_text(value)
    if not text:
        return []
    parts = [part.strip(" -\t") for part in re.split(r"[；;\n]", text)]
    return [part for part in parts if part]


def normalize_block(block: Any) -> tuple[dict[str, Any] | None, list[str]]:
    report: list[str] = []
    if has_transport_residue(block):
        report.append("transport_residue_removed")
    if not isinstance(block, dict):
        items = _items(block)
        if not items:
            return None, ["drop_empty_block"]
        report.append("block_type_added")
        return {"type": "paragraph", "content": "；".join(items)}, report
    kind = str(block.get("type") or "").strip()
    content = block.get("content")
    if content is None or content == "":
        content = block.get("text") or block.get("items") or block.get("points") or ""
    items = _items(block.get("items") if block.get("items") is not None else block.get("points"))
    body = normalize_text(content)
    if kind not in BODY_BLOCK_TYPES:
        kind = "paragraph" if items == _items(body) else "paragraph"
        report.append("block_type_normalized")
    if kind in LIST_BLOCK_TYPES:
        # Preserve explicit JSON item boundaries. Converting a list to prose
        # first joins it with semicolons and splits genuine in-item clauses.
        lines = items or _items(content)
        if not lines:
            return None, ["drop_empty_block"]
        cleaned = []
        for line in lines:
            stripped = strip_fences(line)[0]
            if kind == "numbered_steps":
                stripped = LEADING_NUMBER.sub("", stripped)
            if stripped:
                cleaned.append(stripped)
        return {"type": kind, "content": cleaned}, report
    body, fenced = strip_fences(body)
    if fenced:
        report.append("fence_removed")
    body, cut = cut_json_tail(body)
    if cut:
        report.append("json_tail_cut")
    if kind == "subheading":
        body = LEADING_HASH.sub("", body)
        body = body.strip(" \t:：-—")
        if len(body) > 40:
            report.append("subheading_demoted")
            return {"type": "paragraph", "content": body}, report
    if not body:
        return None, ["drop_empty_block"]
    return {"type": kind, "content": body}, report


def normalize_table(table: Any) -> tuple[dict[str, Any] | None, list[str]]:
    report: list[str] = []
    if has_transport_residue(table):
        report.append("transport_residue_removed")
    if not isinstance(table, dict):
        return None, ["drop_non_object_table"]
    from governance.table_fidelity import coerce_table
    raw_rows = table.get("rows")
    needs_shared_shape = not isinstance(table.get("columns"), (list, tuple)) or (
        isinstance(raw_rows, list) and raw_rows and all(isinstance(row, str) for row in raw_rows)
    )
    if needs_shared_shape or (raw_rows is not None and raw_rows != "" and not isinstance(raw_rows, (list, tuple))):
        table = coerce_table(table)
        if table.get("needs_review"):
            report.append(str(table.get("table_review_reason") or "表格待复核"))
            return table, report
    columns_raw = table.get("columns")
    if not isinstance(columns_raw, (list, tuple)):
        headers = table.get("headers")
        columns_raw = headers if isinstance(headers, (list, tuple)) else None
    # Headers are positional, including an empty slot; filtering shifts columns.
    columns_source = list(columns_raw) if isinstance(columns_raw, (list,tuple)) else _items(columns_raw)
    columns = [normalize_text(item) for item in columns_source]
    rows_raw = table.get("rows")
    rows_source = rows_raw if isinstance(rows_raw, (list, tuple)) else _items(rows_raw)
    if not columns and isinstance(rows_source, list) and rows_source:
        first = rows_source[0]
        if isinstance(first, dict):
            columns = [normalize_text(key) for key in first.keys()]
            report.append("columns_inferred")
        elif isinstance(first, (list, tuple)):
            columns = [f"列{index + 1}" for index in range(len(first))]
            report.append("columns_generated")
    if not columns:
        return None, ["drop_table_without_columns"]
    if (isinstance(rows_source, list) and rows_source and all(isinstance(row, str) for row in rows_source)
            and len(columns) > 1 and len(rows_source) % len(columns) == 0):
        width = len(columns)
        rows_source = [list(rows_source[index:index + width]) for index in range(0, len(rows_source), width)]
        report.append("flat_cells_grouped")
    rows: list[list[str]] = []
    for row in rows_source if isinstance(rows_source, list) else []:
        if isinstance(row, dict):
            cells = [normalize_text(row.get(key, "")) for key in columns]
            if all(not cell for cell in cells):
                cells = [normalize_text(value) for value in row.values()]
            report.append("row_object_flattened")
        elif isinstance(row, (list, tuple)):
            cells = [normalize_text(value) for value in row]
        else:
            cells = _items(row)
        cells = [cell.replace("\n", " ") for cell in cells]
        if not any(cells):
            continue
        if len(cells) < len(columns):
            cells += [""] * (len(columns) - len(cells))
            report.append("row_padded")
        elif len(cells) > len(columns):
            cells = cells[: len(columns) - 1] + ["；".join([cell for cell in cells[len(columns) - 1:] if cell])]
            report.append("row_trimmed")
        rows.append(cells)
    if not rows:
        return None, ["drop_table_without_rows"]
    title = normalize_text(table.get("title") or table.get("caption") or "")
    title, _ = strip_fences(title)
    notes = normalize_text(table.get("notes") or table.get("remark") or "")
    result = {"title": title, "columns": columns, "rows": rows, "notes": notes}
    for key in ('table_source_contract', 'source_content_unit_id', 'archetype', 'protected'):
        if key in table:
            result[key] = table[key]
    return result, report


def normalize_process(process: Any) -> tuple[dict[str, Any] | None, list[str]]:
    report: list[str] = []
    if has_transport_residue(process):
        report.append("transport_residue_removed")
    if not isinstance(process, dict):
        return None, ["drop_non_object_process"]
    steps_source = process.get("steps")
    steps: list[str] = []
    for step in _items(steps_source) or []:
        text = strip_fences(step)[0]
        if text.startswith("{") or text.startswith("["):
            continue
        text = LEADING_NUMBER.sub("", text)
        if text:
            steps.append(text)
    if not steps:
        return None, ["drop_process_without_steps"]
    title = normalize_text(process.get("title") or process.get("name") or process.get("purpose") or "")
    result: dict[str, Any] = {"title": title, "steps": steps}
    if "source_content_unit_id" in process:
        result["source_content_unit_id"] = process["source_content_unit_id"]
    for key in ("purpose", "roles", "trigger", "outputs", "records", "escalation", "sla"):
        value = normalize_text(process.get(key))
        if value:
            result[key] = value
    if isinstance(steps_source, (int, float)):
        report.append("step_count_replaced")
    return result, report


def normalize_fragment(
    fragment: dict[str, Any],
    *,
    section_id: str = "",
    contract: dict[str, Any] | None = None,
    text_pipeline: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic, provider-free cleanup of one Section fragment."""
    report: list[str] = []
    source = dict(fragment or {})
    if text_pipeline is not None:
        try:
            piped = text_pipeline(dict(source))
            if piped != source:
                source = dict(piped)
                report.append("text_pipeline_applied")
        except Exception:  # a local cleaner must never lose the section content
            report.append("text_pipeline_failed")
    sid = str(source.get("section_id") or section_id or "")
    if not str(source.get("section_id") or "") and sid:
        report.append("section_id_restored")

    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in source.get("body_blocks") or []:
        normalized, block_report = normalize_block(block)
        report.extend(item for item in block_report if item != "drop_empty_block")
        if normalized is None:
            report.append("drop_empty_block")
            continue
        if isinstance(block, dict) and "source_content_unit_id" in block:
            normalized["source_content_unit_id"] = block["source_content_unit_id"]
        if normalized.get("type") == "paragraph":
            key = _text(normalized.get("content"))
            if key and key in seen:
                report.append("drop_duplicate_paragraph")
                continue
            if key:
                seen.add(key)
        blocks.append(normalized)
    if not blocks:
        for row in source.get("content_unit_fragments") or []:
            if not isinstance(row, dict):
                continue
            content = normalize_text(row.get("content") or row.get("text"))
            if content:
                blocks.append({"type": "paragraph", "content": content})
                report.append("body_from_content_units")

    tables: list[dict[str, Any]] = []
    for table in source.get("tables") or []:
        normalized, table_report = normalize_table(table)
        if normalized is None:
            report.extend(table_report)
            continue
        report.extend(item for item in table_report if item != "row_object_flattened")
        tables.append(normalized)

    processes: list[dict[str, Any]] = []
    for process in source.get("processes") or []:
        normalized, process_report = normalize_process(process)
        if normalized is None:
            report.extend(process_report)
            continue
        report.extend(process_report)
        processes.append(normalized)

    title = strip_fences(normalize_text(source.get("title")))[0]
    if not title and contract is not None:
        title = normalize_text(contract.get("section_title") or contract.get("title"))
        if title:
            report.append("title_from_contract")

    repaired = {
        "section_id": sid,
        "title": title,
        "body_blocks": blocks,
        "tables": tables,
        "processes": processes,
        "callouts": [row for row in (source.get("callouts") or []) if _text(row)],
        "cross_references": [row for row in (source.get("cross_references") or []) if _text(row)],
        "claims": source.get("claims") if isinstance(source.get("claims"), list) else [],
        "used_requirement_ids": source.get("used_requirement_ids") if isinstance(source.get("used_requirement_ids"), list) else [],
        "used_ku_ids": source.get("used_ku_ids") if isinstance(source.get("used_ku_ids"), list) else [],
        "generation_notes": source.get("generation_notes") if isinstance(source.get("generation_notes"), list) else [],
    }
    for key, value in source.items():
        if key not in repaired:
            repaired[key] = value
    return repaired, report


def normalize_cu_row(row: dict[str, Any], *, section_id: str = "") -> tuple[dict[str, Any], list[str]]:
    report: list[str] = []
    source = dict(row or {})
    if has_transport_residue(source):
        report.append("transport_residue_removed")
    cu_id = str(source.get("content_unit_id") or "").strip()
    archetype = str(source.get("archetype") or "NARRATIVE").strip().upper()
    if archetype != str(source.get("archetype") or ""):
        report.append("archetype_canonicalized")
    content, fenced = strip_fences(normalize_text(source.get("content") or source.get("text")))
    if fenced:
        report.append("fence_removed")
    payload = source.get("payload")
    if isinstance(payload, str):
        decoded, clean = strip_fences(payload)
        report.append("payload_unwrapped")
        payload = {"raw": decoded} if not clean else {"raw": decoded}
    if isinstance(payload, dict) and archetype in TABLE_ARCHETYPES:
        normalized, table_report = normalize_table({**payload, "title": payload.get("title") or source.get("title")})
        payload = normalized or payload
        report.extend(f"payload:{item}" for item in table_report)
    if isinstance(payload, dict) and archetype in PROCESS_ARCHETYPES:
        normalized, process_report = normalize_process(payload)
        payload = normalized or payload
        report.extend(f"payload:{item}" for item in process_report)
    if not str(source.get("parent_section_id") or "") and section_id:
        report.append("parent_section_restored")
    result = {
        **source,
        "content_unit_id": cu_id,
        "parent_section_id": str(source.get("parent_section_id") or section_id or ""),
        "archetype": archetype or "NARRATIVE",
        "content": content,
        "payload": payload,
        "effective_chars": len(content),
    }
    return result, report


def run_local_normalization(
    run_root: Any,
    *,
    section_ids: list[str] | None = None,
    text_pipeline: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    write_back: bool = True,
) -> dict[str, Any]:
    """Normalize every persisted Section fragment in place, with a full report.

    Reads only what generation already wrote to disk; performs no provider call,
    and never deletes a Section it cannot fully repair.
    """
    import json
    from pathlib import Path

    root = Path(str(run_root))
    sections_root = root / "longform" / "word" / "sections"
    report: dict[str, Any] = {
        "schema_version": "local-normalization-v0.1",
        "provider_called": False,
        "sections_scanned": 0,
        "sections_changed": 0,
        "fix_counts": {},
        "sections": {},
    }

    def bump(kind: str) -> None:
        report["fix_counts"][kind] = int(report["fix_counts"].get(kind, 0)) + 1

    if not sections_root.is_dir():
        report["status"] = "NO_SECTIONS"
        return report
    selected = set(section_ids or [])
    for unit in sorted(sections_root.iterdir()):
        fragment_path = unit / "fragment.json"
        if not unit.is_dir() or not fragment_path.is_file():
            continue
        if selected and unit.name not in selected:
            continue
        try:
            fragment = json.loads(fragment_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            report["sections"][unit.name] = {"status": "UNREADABLE", "provider_called": False}
            continue
        contract: dict[str, Any] = {}
        contract_path = next(
            (root / name for name in ("04_section_contracts.json", "section_contracts.json") if (root / name).is_file()),
            None,
        )
        if contract_path is not None:
            try:
                bundle = json.loads(contract_path.read_text(encoding="utf-8-sig"))
                contract = next(
                    (row for row in bundle.get("contracts") or [] if str(row.get("section_id") or "") == unit.name),
                    {},
                )
            except (OSError, json.JSONDecodeError):
                contract = {}
        cu_reports: dict[str, list[str]] = {}
        cus_dir = unit / "cus"
        if cus_dir.is_dir() and write_back:
            for cu_path in sorted(cus_dir.glob("CU-*.json")):
                try:
                    row = json.loads(cu_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    continue
                normalized, cu_report = normalize_cu_row(row, section_id=unit.name)
                if cu_report:
                    cu_reports[cu_path.stem] = cu_report
                    cu_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        repaired, fixes = normalize_fragment(
            fragment,
            section_id=unit.name,
            contract=contract or None,
            text_pipeline=text_pipeline,
        )
        report["sections_scanned"] += 1
        for fix in fixes:
            bump(fix)
        changed = bool(fixes or cu_reports)
        if changed and write_back:
            fragment_path.write_text(json.dumps(repaired, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report["sections_changed"] += 1
        report["sections"][unit.name] = {
            "status": "NORMALIZED" if changed else "CLEAN",
            "fixes": fixes,
            "content_unit_fixes": cu_reports,
            "effective_chars": len(_text(repaired.get("body_blocks"))),
            "tables": len(repaired.get("tables") or []),
            "processes": len(repaired.get("processes") or []),
            "provider_called": False,
        }
    report["status"] = "PASS"
    target = root / "longform" / "word" / "local_normalization_report.json"
    if write_back:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
