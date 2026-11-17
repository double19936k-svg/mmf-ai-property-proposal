"""DEPTH_SUPPLEMENT：把"篇幅不足"翻译成"缺少新的内容单元"。

旧的 UNDER_LENGTH_REWRITE 让模型"把最后一段写得更详细"，产出的是同一个意图的
更长版本：字数上去了，信息量没有上去，而且整节重新付费。本模块把深度缺口改成
覆盖缺口——只补充携带新意图、新输出形态、新原型的 Content Unit，并用
INFORMATION_GAIN / NOVELTY 门丢弃低增量产物。

本模块是纯本地计算：它只提出补充计划并判定增量，不直接调用 Provider。
"""

from __future__ import annotations

import re
from typing import Any

from planning.content_engineering import CONTENT_ARCHETYPES, choose_archetype
from planning.unified_budget import (
    ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D,
    contract_budget_row,
    pages_from_ledger_chars,
    unified_unit_budget,
)

from .capability_gaps import SUPPLEMENT_UNIT_CHARS
from .cu_state import depth_deficit_class, depth_ratio

MAX_SUPPLEMENT_UNITS_PER_SECTION = 3
MIN_SUPPLEMENT_CHARS = 360
# 与已有正文的 8 字窗重合度超过该比例即视为低增量，补充内容丢弃。
MAX_OVERLAP_RATIO = 0.55
_SHINGLE = 8

DIMENSION_TEMPLATES = (
    {
        "key": "role_accountability", "topic": "岗位职责与接口",
        "archetype": "RESPONSIBILITY_MATRIX", "output": "岗位职责与接口矩阵",
        "signals": ("负责", "岗位", "职责", "接口", "分工"),
    },
    {
        "key": "frequency_schedule", "topic": "作业频次与时间安排",
        "archetype": "SCHEDULE", "output": "分岗位作业频次与时间安排表",
        "signals": ("频次", "每日", "每周", "每班", "排班", "时段"),
    },
    {
        "key": "record_ledger", "topic": "检查记录与台账",
        "archetype": "FORM_TEMPLATE", "output": "检查记录表单与台账字段清单",
        "signals": ("记录", "台账", "表单", "签字", "存档"),
    },
    {
        "key": "exception_escalation", "topic": "异常处置与升级路径",
        "archetype": "EMERGENCY_PLAYBOOK", "output": "异常分级处置与升级路径",
        "signals": ("异常", "突发", "升级", "应急", "故障"),
    },
    {
        "key": "quality_indicator", "topic": "品质指标与考核口径",
        "archetype": "KPI_TABLE", "output": "可量化服务指标与考核口径",
        "signals": ("指标", "考核", "满意度", "达标", "评分"),
    },
    {
        "key": "resource_configuration", "topic": "人员与设备配置",
        "archetype": "STANDARD_TABLE", "output": "人员编制与设备配置标准表",
        "signals": ("配置", "编制", "设备", "工具", "物料"),
    },
    {
        "key": "self_inspection", "topic": "自查与整改闭环",
        "archetype": "CHECKLIST", "output": "周期性自查与整改闭环清单",
        "signals": ("自查", "整改", "闭环", "复查", "抽查"),
    },
)


def _canonical(value: str) -> str:
    return re.sub(r"\s+|[，。；：、,.!?！？]", "", str(value or ""))


def target_minimum(contract: dict[str, Any]) -> int:
    return int(((contract or {}).get("target_words") or {}).get("min") or 0)


def arrived_ledger_chars(gate: dict[str, Any]) -> int:
    """把门量的"装配后可见字"折回账本字，再跟契约额度相减。

    `gate["content_units"]` 数的是装配后正文的可见字，契约 `target_words` 是统一账本字
    （含结构 payload 与 CU 回声）。Run D 实测两者之比 0.7707，不换算就会把一节
    已经写满契约的稿子读成还差 23%。
    """
    arrived = int((gate or {}).get("content_units") or 0)
    return int(round(arrived / ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D)) if arrived else 0


def supplement_unit_count(gate: dict[str, Any], contract: dict[str, Any], *, cap: int = MAX_SUPPLEMENT_UNITS_PER_SECTION) -> int:
    """How many new Content Units this Section is missing (never a rewrite)."""
    target = target_minimum(contract)
    if target <= 0:
        return 0
    arrived = arrived_ledger_chars(gate)
    deficit = target - arrived
    if deficit <= 0:
        return 0
    per_unit = max(1, int(target / max(1, len((contract or {}).get("must_cover") or []) or 3)))
    return max(1, min(cap, int(-(-deficit // max(per_unit, 1)))))


def _existing_text(gate: dict[str, Any], fragment: dict[str, Any] | None) -> str:
    parts = [_canonical(str((gate or {}).get("issue") or ""))]
    for row in (fragment or {}).get("body_blocks") or []:
        parts.append(_canonical(row.get("content") if isinstance(row, dict) else row))
    for row in (fragment or {}).get("tables") or []:
        if isinstance(row, dict):
            parts.append(_canonical(row.get("title")))
            parts.extend(_canonical(cell) for cell in row.get("columns") or [])
    for row in (fragment or {}).get("processes") or []:
        if isinstance(row, dict):
            parts.append(_canonical(row.get("title")))
            parts.extend(_canonical(step) for step in row.get("steps") or [])
    return "\n".join(part for part in parts if part)


def _missing_labels(gate: dict[str, Any]) -> list[str]:
    return [str(key) for key, value in ((gate or {}).get("must_cover") or {}).items() if value == "MISSING"]


def _pick_archetype(gap: dict[str, Any], contract: dict[str, Any],
                    used: set[str], existing: set[str]) -> str:
    """缺口点名的形态优先。上一轮把「应急物资定期检查表」记成 RISK_REGISTER，
    预算口径（行/列 vs 风险项）与交付物就对不上，§12 要的结构化内容落不到纸面：
    形态是目录对该交付物的规定，不是本节用过就没的配额 —— 重复的是写法，不是内容，
    内容是否重复由 novelty_gate 判。缺口自己没点形态时才让位给本节没用过的写法，
    并且不选 NARRATIVE：补深要的是新形态，不是又一段话。"""
    named = str(gap.get("archetype") or "")
    if named in CONTENT_ARCHETYPES and named != "NARRATIVE":
        return named
    topic = str(gap.get("topic") or "")
    probe = {"section_title": str((contract or {}).get("section_title") or topic)}
    shape = {"must_cover": [topic], "required_outputs": list(gap.get("outputs") or [])}
    for avoid_existing in (True, False):
        for index in range(1, len(CONTENT_ARCHETYPES) + 1):
            candidate = choose_archetype(probe, shape, index)
            if candidate in used or candidate == "NARRATIVE":
                continue
            if avoid_existing and candidate in existing:
                continue
            return candidate
    return choose_archetype(probe, shape, 1)


def _shingles(text: str) -> set[str]:
    value = _canonical(text)
    if len(value) < _SHINGLE:
        return {value} if value else set()
    return {value[index:index + _SHINGLE] for index in range(len(value) - _SHINGLE + 1)}


def novelty_gate(existing_text: str, added_text: str) -> dict[str, Any]:
    """INFORMATION_GAIN / NOVELTY check for one supplement payload."""
    before = _shingles(existing_text)
    after = _shingles(added_text)
    if not after:
        return {"novelty": 0.0, "overlap_ratio": 1.0, "arrived_chars": 0, "passed": False, "reason": "EMPTY_SUPPLEMENT"}
    overlap = len(before & after) / len(after)
    novelty = round(1.0 - overlap, 4)
    arrived = len(_canonical(added_text))
    if arrived < MIN_SUPPLEMENT_CHARS:
        return {"novelty": novelty, "overlap_ratio": round(overlap, 4), "arrived_chars": arrived, "passed": False, "reason": "BELOW_MIN_SUPPLEMENT_CHARS"}
    if overlap > MAX_OVERLAP_RATIO:
        return {"novelty": novelty, "overlap_ratio": round(overlap, 4), "arrived_chars": arrived, "passed": False, "reason": "LOW_INFORMATION_GAIN"}
    return {"novelty": novelty, "overlap_ratio": round(overlap, 4), "arrived_chars": arrived, "passed": True, "reason": "INFORMATION_GAIN_OK"}


def next_supplement_ids(section_id: str, existing_ids: list[str], count: int) -> list[str]:
    """Continue the planned CU numbering so supplement ids stay in contract format."""
    used: set[int] = set()
    for cu_id in existing_ids or []:
        match = re.search(r"-(\d{2,})$", str(cu_id or ""))
        if match:
            used.add(int(match.group(1)))
    index = max(used, default=0) + 1
    ids: list[str] = []
    while len(ids) < max(0, int(count)):
        candidate = f"CU-{section_id}-{index:02d}"
        if candidate not in set(existing_ids or []):
            ids.append(candidate)
        index += 1
    return ids


def plan_depth_supplement(
    contract: dict[str, Any],
    gate: dict[str, Any],
    *,
    fragment: dict[str, Any] | None = None,
    planned_cu_ids: list[str] | None = None,
    existing_archetypes: list[str] | None = None,
    focus: list[dict[str, Any]] | None = None,
    cap: int = MAX_SUPPLEMENT_UNITS_PER_SECTION,
) -> list[dict[str, Any]]:
    """Build new Content Unit contracts for one under-depth Section.

    Each row carries its own intent, required output and archetype; none of them
    says "expand what is already there".
    """
    sid = str((contract or {}).get("section_id") or "")
    if not sid:
        return []
    focus_rows = [row for row in (focus or []) if isinstance(row, dict)]
    wanted = supplement_unit_count(gate, contract, cap=cap)
    # 文档级QA点名了几项缺口，就要补几个新单元：只按"本节字数差多少"计量的话，
    # 一份已达自身篇幅契约却缺台账/缺矩阵的章节会被判成"什么都不缺"，
    # 而它正是全书差的那几十页。
    wanted = max(wanted, min(cap, sum(1 for row in focus_rows
                                     if str(row.get("topic") or row.get("missing_capability") or "").strip())))
    if wanted <= 0:
        wanted = min(cap, 1)
    text = _existing_text(gate, fragment)
    gaps: list[dict[str, Any]] = []

    # 文档级QA点名的缺口（未覆盖的招标要求 / 未响应的评分项 / 能力目录判出的缺项）
    # 优先补齐。带 required_output 与 archetype 的缺口原样传递：补的是那张台账，
    # 不是"把本节写得更详细"。
    for row in focus_rows:
        topic = str(row.get("topic") or row.get("missing_capability") or "").strip()
        if not topic:
            continue
        named = [str(value).strip() for value in (row.get("outputs") or []) if str(value or "").strip()]
        if not named and row.get("required_output"):
            named = [str(row["required_output"]).strip()]
        gaps.append({
            "topic": topic,
            "intent": str(row.get("intent") or f"落实「{topic}」在本章节的可核查安排"),
            "outputs": named or [f"{topic}的执行标准、责任岗位与验证记录"],
            "reason": str(row.get("reason") or "DOCUMENT_QA_GAP"),
            "archetype": str(row.get("archetype") or ""),
            "missing_capability": str(row.get("missing_capability") or topic),
            "novelty_basis": str(row.get("novelty_basis") or ""),
            **{key: str(row[key]) for key in ("requirement_id", "scoring_item_id") if row.get(key)},
        })
    for label in _missing_labels(gate):
        gaps.append({
            "topic": label,
            "intent": f"落实招标要求「{label}」在本章节的具体执行安排",
            "outputs": [f"{label}的执行标准与验证记录"],
            "reason": "MUST_COVERAGE_MISSING",
            "missing_capability": label,
            "novelty_basis": f"已到达正文未覆盖招标要求「{label}」的关键表述",
        })
    for output in list((gate or {}).get("required_outputs_missing") or []):
        gaps.append({
            "topic": str(output),
            "intent": f"补齐契约要求的交付物「{output}」",
            "outputs": [str(output)],
            "reason": "REQUIRED_OUTPUT_MISSING",
            "missing_capability": str(output),
            "novelty_basis": f"章节契约要求的交付物「{output}」尚未出现在正文里",
        })
    for row in DIMENSION_TEMPLATES:
        if any(signal in text for signal in row["signals"]):
            continue
        gaps.append({
            "topic": row["topic"],
            "intent": f"从「{row['topic']}」维度补充本章节的可执行安排",
            "outputs": [row["output"]],
            "archetype": row["archetype"],
            "reason": "DEPTH_DIMENSION_MISSING",
            "missing_capability": row["topic"],
            "novelty_basis": "已有正文未出现「" + "、".join(row["signals"][:3]) + "」任一判据",
        })

    deficit_chars = max(0, target_minimum(contract) - arrived_ledger_chars(gate))
    ids = next_supplement_ids(sid, list(planned_cu_ids or []), min(wanted, max(1, len(gaps))))
    # 一个补充单元锚定在一页（900 有效字）：这是 §16 页面缺口的换算基准，几十项缺口
    # 才等于一条能核对的页数。旧口径按"本节契约字数 ÷ 单元数"分配，一个 9000 字的节
    # 补 3 个单元就一次多出 9000 字，全书页数会直接冲过区间上限；本节真实差额最多
    # 允许补到两页/单元。
    per_unit_chars = min(2 * SUPPLEMENT_UNIT_CHARS, max(
        MIN_SUPPLEMENT_CHARS, SUPPLEMENT_UNIT_CHARS, int(deficit_chars / max(1, len(ids)))))
    used_archetypes = {str(row or "") for row in (existing_archetypes or [])}
    existing_shapes = set(used_archetypes)

    rows: list[dict[str, Any]] = []
    for cu_id, gap in zip(ids, gaps[: len(ids)]):
        archetype = _pick_archetype(gap, contract, used_archetypes, existing_shapes)
        used_archetypes.add(archetype)
        # 补深单元也用**统一额度**：Round 3 的 `archetype_budget` 只给正文标额度，
        # 补出来的每张表都在额度之外长字，正好把刚修好的预算模型从后门又漏掉。
        target = contract_budget_row(archetype, unified_unit_budget(archetype, per_unit_chars))
        minimum = contract_budget_row(
            archetype, unified_unit_budget(archetype, max(1, int(round(per_unit_chars * 0.8)))))
        maximum = contract_budget_row(
            archetype, unified_unit_budget(archetype, max(1, int(round(per_unit_chars * 1.2)))))
        unit_chars = max(1, int(target.get("effective_chars") or per_unit_chars))
        outputs = list(gap.get("outputs") or [])
        rows.append({
            "content_unit_id": cu_id,
            # §14 的补深记录口径：新增 CU 的身份、缺的是哪项能力、依据什么判定不重复、
            # 预计占几页多少 token，都要随契约一起留下，报告里才不用事后编。
            "supplement_cu_id": cu_id,
            "parent_section_id": sid,
            "title": gap["topic"],
            "intent": gap["intent"],
            "must_cover": [gap["topic"]],
            "required_outputs": outputs,
            "required_output": outputs[0] if outputs else gap["topic"],
            "missing_capability": str(gap.get("missing_capability") or gap["topic"]),
            "novelty_basis": str(gap.get("novelty_basis") or "新增意图与原正文不重叠"),
            "estimated_pages": float(pages_from_ledger_chars(unit_chars)),
            "estimated_tokens": max(1, unit_chars // 4),
            "archetype": archetype,
            "budget_measure": "unified_effective_chars",
            "source_requirements": sorted({
                *((contract or {}).get("source_requirements") or []),
                *([gap["requirement_id"]] if gap.get("requirement_id") else []),
            }),
            "source_scoring_items": [gap["scoring_item_id"]] if gap.get("scoring_item_id") else [],
            "fact_dependencies": [],
            "evidence_dependencies": [],
            "min_budget": minimum,
            "target_budget": target,
            "max_budget": maximum,
            "scoring_refs": [],
            "dependency_refs": [],
            "supplement_reason": gap["reason"],
            "generation_origin": "depth_supplement",
        })
    return rows


def supplement_prompt_rules(rows: list[dict[str, Any]]) -> list[str]:
    return [
        "本轮为DEPTH_SUPPLEMENT：只输出下列新增Content Unit，禁止改写、扩写或重排已有正文",
        "每个新增Content Unit必须承载与已有正文不同的意图、输出形态与原型",
        "已有正文已表达过的结论不得再写一遍，重复内容会被系统判定为低增量并丢弃",
        "不得出现“进一步细化上文”“如上所述”式的注水表述",
        "新增Content Unit ID固定为：" + "、".join(str(row.get("content_unit_id")) for row in rows),
    ]
