"""CU envelope transport with partial recovery.

A strict-JSON contract makes a 300-class batch all-or-nothing: the narrative
body can be perfectly usable while the closing brace is cut off by the output
token limit, and the whole batch is then discarded and re-paid for.  The
envelope protocol keeps long text out of JSON string escaping, and the parser
recovers every block that actually arrived so only the failed Content Unit is
re-requested.
"""

from __future__ import annotations

import json
import re
from typing import Any

from governance.table_qa import FORM_PAYLOAD_SPEC

from .cu_state import CU_PAYLOAD_INCOMPLETE, CU_TRUNCATED


STRUCTURED_ARCHETYPES = {
    "STANDARD_TABLE",
    "MATRIX",
    "CHECKLIST",
    "FORM_TEMPLATE",
    "KPI_TABLE",
    "RESPONSIBILITY_MATRIX",
    "RISK_REGISTER",
    "SCHEDULE",
}
HYBRID_ARCHETYPES = {"SOP", "PROCESS", "EMERGENCY_PLAYBOOK"}
ENVELOPE_ARCHETYPES = {"NARRATIVE", "EMERGENCY_PLAYBOOK", "SOP", "PROCESS"}

_START_RE = re.compile(r"<<<\s*CU_START\s*:\s*([A-Za-z0-9_.:\-]+?)\s*(?:\|\s*arch\s*=\s*([A-Z_]+)\s*)?>>>", re.I)
_END_RE = re.compile(r"<<<\s*(?:CU_)?END\s*>>>", re.I)
_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_\-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)
_STEP_LINE_RE = re.compile(r"^\s*(?:\d{1,2}\s*[\.、)\]]|步骤\s*\d{1,2}|第[一二三四五六七八九十]{1,3}步)\s*[:：]?\s*(.+?)\s*$")
_LABEL_LINE_RE = re.compile(r"^\s*(标题|流程名称|适用岗位|责任岗位|触发条件|处置目标|时限要求|记录方式)\s*[:：]\s*(.+?)\s*$")


def requires_payload(archetype: str | None) -> bool:
    return str(archetype or "").upper() in STRUCTURED_ARCHETYPES


def is_hybrid(archetype: str | None) -> bool:
    return str(archetype or "").upper() in HYBRID_ARCHETYPES


def should_use_envelope(archetypes: list[str]) -> bool:
    """Envelope carries text-dominant batches; a structurally heavy batch keeps JSON."""
    if not archetypes:
        return False
    text_like = sum(1 for row in archetypes if str(row or "").upper() in ENVELOPE_ARCHETYPES)
    return text_like >= max(1, int(len(archetypes) * 0.6))


def _cu_budget_line(row: dict[str, Any]) -> str:
    """把该 CU 的**统一额度**写进信封：正文与结构体共用同一份量（§7/§9）。

    Round 3 的信封只报 `content_unit_id + archetype`，模型看到的唯一长度提示是
    Section 级的一句"目标内容量 X～Y"，表格/清单/表单是要**另外**交的东西 ——
    于是每条正文写满额度之后再挂一张表，Run D 的结构 payload 因此长出 203,778 字
    （占实际交付 43.7%），27/27 节全部 SECTION_OVER_LENGTH。
    """
    budget = row.get("target_budget") or {}
    unit_id = row.get("content_unit_id")
    archetype = str(row.get("archetype") or "NARRATIVE")
    chars = int(budget.get("effective_chars") or row.get("planned_words") or 0)
    parts = [f"- {unit_id}（archetype={archetype}"]
    if not chars:
        return parts[0] + "）"
    prose = int(budget.get("prose_chars") or chars)
    parts.append(f"，本单元总额度{chars}字：正文约{prose}字")
    if int(budget.get("rows") or 0):
        cell = int(budget.get("min_cell_chars") or 0)
        parts.append(f"，另含{budget['rows']}行×{budget.get('columns') or 4}列的表"
                     + (f"（每格不少于{cell}字，不许留空格撑行数）" if cell else ""))
    elif int(budget.get("steps") or 0):
        parts.append(f"，另含{budget['steps']}个步骤")
    elif int(budget.get("items") or 0):
        parts.append(f"，另含{budget['items']}个条目")
    elif int(budget.get("fields") or 0):
        parts.append(f"，另含{budget['fields']}个表单字段")
    parts.append("）")
    return "".join(parts)


def envelope_contract_lines(requested: list[dict[str, Any]]) -> list[str]:
    """Prompt rules for one envelope batch.  `requested` rows carry content_unit_id/archetype."""
    structured = [row for row in requested if requires_payload(row.get("archetype"))]
    forms = [row for row in requested if str(row.get("archetype") or "").upper() == "FORM_TEMPLATE"]
    budgeted = any(int((row.get("target_budget") or {}).get("effective_chars") or 0) for row in requested)
    lines = [
        "Output Protocol = CU_ENVELOPE。不要输出JSON文档、不要输出markdown代码围栏。",
        "每个Content Unit独立成块，块与块之间只允许空行：",
        "<<<CU_START:内容单元ID>>>",
        "该单元的正文",
        "<<<CU_END>>>",
        "块内正文按自然写作直接书写，允许中文标点、换行、编号列表和表格文字，不需要任何转义。",
        "必须为且仅为下列ID各输出一个块，顺序不限，不得合并、拆分或改名：",
    ]
    for row in requested:
        lines.append(_cu_budget_line(row) if budgeted
                     else f"- {row.get('content_unit_id')}（archetype={row.get('archetype') or 'NARRATIVE'}）")
    if budgeted:
        lines.append(
            "上面每个单元的额度是**统一额度**：表格、步骤、清单、表单的字数已经算在额度里面，"
            "不是额度之外另外交付的东西。写完正文再追加表格＝超出额度；"
            "装不下就按额度减少行数/条目数或压缩正文，不得靠加行、加列、留空格凑结构。")
    if structured:
        lines.append(
            "其中结构化单元（"
            + "、".join(str(row.get("content_unit_id")) for row in structured)
            + "）在块内输出一个JSON对象作为该单元的payload，字段按契约要求，块内不要再嵌套代码围栏。"
        )
    else:
        lines.append("本批全部为叙述/流程型单元，块内只输出自然语言正文；流程型可用“标题：/适用岗位：”加编号步骤行表达。")
    if forms:
        # §36/§61："字段按契约要求"这句太空，Provider 实际只回字段名（上一轮 31/31 张表单都这样），
        # 于是九要素里的 适用场景/触发条件/保存位置/保存期限/复核要求 从来没被要到过。信封模式点名要。
        lines.append(FORM_PAYLOAD_SPEC)
    return lines


def _strip_fence(body: str) -> str:
    text = body.strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_\-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _json_prefix(body: str) -> tuple[Any | None, bool]:
    """Return the first JSON value and whether it terminated cleanly."""
    text = _strip_fence(body)
    if not text:
        return None, False
    try:
        return json.loads(text), True
    except json.JSONDecodeError:
        pass
    start = min((index for index in (text.find("{"), text.find("[")) if index >= 0), default=-1)
    if start < 0:
        return None, False
    try:
        parsed, consumed = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None, False
    return parsed, start + consumed >= len(text.rstrip())


def parse_step_blocks(body: str) -> tuple[str, list[str], dict[str, str]]:
    """Read the Header + Step Blocks text form used by SOP / PROCESS units."""
    lines = [line for line in _strip_fence(body).splitlines()]
    steps: list[str] = []
    labels: dict[str, str] = {}
    prose: list[str] = []
    for line in lines:
        step = _STEP_LINE_RE.match(line)
        if step:
            steps.append(step.group(1).strip())
            continue
        label = _LABEL_LINE_RE.match(line)
        if label:
            labels[label.group(1)] = label.group(2).strip()
            continue
        if line.strip():
            prose.append(line.strip())
    return "\n".join(prose).strip(), steps, labels


def _split_segments(text: str) -> list[tuple[str, str | None, str, bool]]:
    starts = list(_START_RE.finditer(text))
    segments: list[tuple[str, str | None, str, bool]] = []
    for index, match in enumerate(starts):
        body_from = match.end()
        body_to = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        window = text[body_from:body_to]
        closing = _END_RE.search(window)
        if closing:
            # Keep the terminator as the boundary only: the marker is transport
            # syntax and must never travel into customer-facing content.
            segments.append((match.group(1), match.group(2), window[: closing.start()], True))
        else:
            segments.append((match.group(1), match.group(2), window, False))
    return segments


def parse_cu_envelope(
    text: Any,
    *,
    requested_ids: list[str],
    archetypes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Parse an envelope response, keeping every block that survived truncation."""
    requested = [str(item) for item in requested_ids or []]
    archetypes = archetypes or {}
    raw = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    segments = _split_segments(raw)
    blocks: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    if not segments:
        return {
            "protocol": "none_matched",
            "blocks": {},
            "issues": ["no_envelope_markers"],
            "complete_ids": [],
            "truncated_ids": [],
            "missing_ids": list(requested),
            "unrequested_ids": [],
            "raw_length": len(raw),
        }
    for cu_id, arch_hint, body, terminated in segments:
        body = _strip_fence(body)
        archetype = str(
            arch_hint or archetypes.get(cu_id) or blocks.get(cu_id, {}).get("archetype") or "NARRATIVE"
        ).upper()
        payload: Any = None
        payload_shape = "none"
        content = body
        steps: list[str] = []
        if requires_payload(archetype) or is_hybrid(archetype):
            decoded, clean = _json_prefix(body)
            if isinstance(decoded, dict):
                payload = decoded
                payload_shape = "object"
                content = str(decoded.get("content") or decoded.get("text") or decoded.get("summary") or "").strip()
                steps = [str(item) for item in (decoded.get("steps") or []) if str(item or "").strip()]
            elif decoded is not None:
                payload = decoded
                payload_shape = "array" if isinstance(decoded, list) else "scalar"
        if payload_shape == "none" and is_hybrid(archetype):
            content, steps, labels = parse_step_blocks(body)
            if steps:
                payload = {"title": labels.get("标题") or labels.get("流程名称"), "steps": steps}
                payload_shape = "object"
        elif payload_shape == "object" and not steps:
            steps = [str(item) for item in ((payload or {}).get("steps") or []) if str(item or "").strip()]
        if requires_payload(archetype) and payload_shape not in {"object", "array"}:
            status = CU_PAYLOAD_INCOMPLETE
            issues.append(f"payload_unrecoverable:{cu_id}")
        elif not terminated:
            status = CU_TRUNCATED
            issues.append(f"truncated:{cu_id}")
        else:
            status = "ARRIVED"
        arrived_units = len(re.findall(r"[一-鿿]|[A-Za-z0-9]+", content))
        if arrived_units == 0 and payload_shape == "none":
            status = CU_PAYLOAD_INCOMPLETE
            issues.append(f"empty_block:{cu_id}")
        previous = blocks.get(cu_id)
        if previous and previous["status"] != CU_TRUNCATED:
            issues.append(f"duplicate:{cu_id}")
            if previous["content_units"] >= arrived_units:
                continue
        blocks[cu_id] = {
            "content_unit_id": cu_id,
            "archetype": archetype,
            "content": content,
            "payload": payload,
            "payload_shape": payload_shape,
            "steps": steps,
            "status": status,
            "terminated": bool(terminated),
            "content_units": arrived_units,
        }
    complete = [cu_id for cu_id, row in blocks.items() if row["status"] == "ARRIVED"]
    truncated = [cu_id for cu_id, row in blocks.items() if row["status"] == CU_TRUNCATED]
    incomplete = [cu_id for cu_id, row in blocks.items() if row["status"] == CU_PAYLOAD_INCOMPLETE]
    unrequested = [cu_id for cu_id in blocks if requested and cu_id not in set(requested)]
    missing = [cu_id for cu_id in requested if cu_id not in blocks]
    return {
        "protocol": "cu_envelope",
        "blocks": blocks,
        "issues": issues,
        "complete_ids": complete,
        "truncated_ids": truncated,
        "payload_incomplete_ids": incomplete,
        "missing_ids": missing,
        "unrequested_ids": unrequested,
        "raw_length": len(raw),
    }
