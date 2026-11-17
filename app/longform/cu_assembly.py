"""MMF-007C Round 4 §16–§20 / §63–§64：Content Unit → Section Fragment 的唯一装配口径。

削减执行器必须能把改动后的 CU 用**同一条**规则重新装配成节正文，否则"CU 已削减"与
"交付件真的变短"是两件事，那是 Round 3 里"表格在额度之外长字"的同一类故障。
本模块的形状与原 `LongformGenerationFactory._assemble_section_from_cus` 内联代码一致，
搬移只改形状、不改判据：叙述进 `body_blocks`，表格/流程/清单/表单各自进自己的槽位。

§64 在这一层记账：CU 的预算账按 payload 记的是**治理前**的字，而表格进 Word 之前还要过
`plan_section_tables`（同列低信息表合并、治理后仍低信息转删）。装配点这里同时握着
"CU 交了什么"和"治理后渲染什么"，所以两个数在同一把尺下对账，差额回填 Section 预算 ——
不回填就会拿"永远不会渲染的字"去压下一批的额度。
"""
from __future__ import annotations

import copy
from typing import Any

from governance.table_qa import CU_PROVENANCE_KEY, form_template_table, plan_section_tables
from planning.budget_control import visible_chars_of
from planning.unified_budget import (FIELD_ARCHETYPES, STEP_ARCHETYPES,
                                     TABLE_LIKE_ARCHETYPES)

# 装配分支按 archetype 分流；FORM_TEMPLATE 不走 payload.rows 那条路，它有自己的
# 九要素渲染器（`form_template_table`），所以表格里这一支单独列。
# 集合名沿用本模块的叫法，成员一律取自 `planning.unified_budget`：装配认得的 archetype
# 与预算算成本的 archetype 必须是同一批，两边各列一份迟早分叉。
TABLE_ARCHETYPES = set(TABLE_LIKE_ARCHETYPES)
PROCESS_ARCHETYPES = set(STEP_ARCHETYPES)
FORM_ARCHETYPES = set(FIELD_ARCHETYPES)
# CU 装配出来的表格/流程要打出处：`tables`/`processes` 是在 base fragment 上**追加**的，
# 削减执行器重建同一节时若不认得"哪几张表是 CU 加的"，就会把表加第二遍。
CU_STRUCTURE_PROVENANCE = CU_PROVENANCE_KEY
# 只有这两类 archetype 的 payload 会被渲染成表格，也只有它们的字要过表格治理对账。
STRUCTURE_TABLE_ARCHETYPES = TABLE_ARCHETYPES | FORM_ARCHETYPES


def cu_table_descriptors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CU 记录 → 它将要渲染成的那张表（§64 的唯一换算规则）。

    `apply_content_units` 用的就是这个函数，所以"CU 会变成什么表"在全仓只有一处定义；
    预算侧要量治理后的字数，只能把同一批表再喂给治理器，不能自己想象它们的形状。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        archetype = str(row.get("archetype") or "NARRATIVE")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        cu_id = row.get("content_unit_id")
        budget = row.get("budget") if isinstance(row.get("budget"), dict) else {}
        protected = bool(budget.get("protected") or row.get("requirement_ids")
                         or row.get("source_scoring_items"))
        if archetype in TABLE_ARCHETYPES and payload.get("rows"):
            from governance.table_fidelity import KEY, coerce_table, source_contract
            # §35：archetype 一并带进表格描述，装配台账与表格门才分得清"正文表格"和
            # "表单模板"，两者用的是两种口径。列数若只是一个整数，先还原成表头和行，
            # 否则 source_contract 会对这个整数做 list()，整份文稿在修复阶段中断。
            if not isinstance(payload.get("columns"), (list, tuple)) or any(
                    not isinstance(row, (list, tuple)) for row in payload.get("rows") or []):
                payload = coerce_table(payload)
            out.append({**payload, "archetype": archetype, CU_PROVENANCE_KEY: cu_id,
                        "protected": protected, KEY: source_contract(payload, cu_id)})
        elif archetype in FORM_ARCHETYPES and payload.get("fields"):
            # §61/§37：表单模板渲染成"≤6 行示例结构 + 表下字段说明"，九要素缺项由
            # `form_template_table` 记进 form_elements_missing，交付端据此出 FORM_* 门。
            out.append({**form_template_table(payload, title=payload.get("form_name")),
                        CU_PROVENANCE_KEY: cu_id, "protected": protected})
    return out


def rendered_shape(plan: dict[str, Any]) -> dict[str, Any]:
    """一张治理后的表在 Word 里**真的有的**那几段：表题、列、行、表下说明。

    CU 预算账按 `visible_chars_of(payload)` 记账，而 payload 里不止这四段（来源、口径、
    单位、图注等元数据键在 Word 里没有落点）。§64 要的就是这个差：没渲染的字不能继续
    算已用额度，所以两侧必须都按"渲染形状"量，而不是拿 payload 的整体去比 cell 集合。
    """
    return {"title": plan.get("title") or "", "columns": plan.get("columns") or [],
            "rows": plan.get("rows") or [], "notes": plan.get("notes") or ""}


def table_governance_accounting(rows: list[dict[str, Any]], *, section_id: str,
                                base_fragment: dict[str, Any],
                                cu_tables: list[dict[str, Any]]) -> dict[str, Any]:
    """同一批 CU 表：**记账字**（payload）与**治理后会渲染的字**（渲染形状）各量一次。

    两侧都用 `planning.budget_control.visible_chars_of`，所以差额是"治理与渲染之间丢掉的字"，
    不是两把尺子的误差。被合并的表按参与合并的成员数分摊治理后的字数；一张表里既有 CU 表
    又有节自带表时记 `attribution_mixed_source_merges`，不把整张表的字算到 CU 头上。
    治理加字（表下说明的要素标签）时差额为负，同样如实回填 —— 只允许往回收不是诚实。
    """
    charged = {str(row.get("content_unit_id")): visible_chars_of(row.get("payload"))
               for row in rows
               if str(row.get("archetype") or "") in STRUCTURE_TABLE_ARCHETYPES}
    base_tables = [item for item in (base_fragment.get("tables") or []) if isinstance(item, dict)]
    governed = plan_section_tables([*base_tables, *cu_tables])
    attributed: dict[str, int] = {}
    mixed = 0
    for plan in governed["tables"]:
        sources = plan.get(CU_PROVENANCE_KEY) or ""
        cu_ids = [str(item) for item in sources] if isinstance(sources, (list, tuple)) \
            else ([str(sources)] if sources else [])
        if not cu_ids:
            continue
        chars = visible_chars_of(rendered_shape(plan))
        members = max(int(plan.get("source_tables") or 1), len(cu_ids))
        share = chars / max(1, members)
        if len(cu_ids) < members:
            mixed += 1
        for cu_id in cu_ids:
            attributed[cu_id] = attributed.get(cu_id, 0) + int(round(share))
    for cu_id in charged:
        attributed.setdefault(cu_id, 0)   # 转删的表：治理后一个字都不渲染
    planned_total = sum(charged.values())
    governed_total = sum(attributed.get(cu_id, 0) for cu_id in charged)
    report = governed["report"]
    return {
        "schema_version": "section-table-governance-account-v0.1",
        "section_id": str(section_id or ""),
        "basis": {
            "charged": "CU payload 的可见字（= judge_content_unit 记进预算账的那个结构字）",
            "governed": "同一批表过 plan_section_tables 之后的渲染形状：表题/列/行/表下说明",
            "char_ruler": "planning.budget_control.visible_chars_of",
        },
        "structure_units_charged": len(charged),
        "structure_chars_charged": planned_total,
        "structure_chars_governed": governed_total,
        "reclaimed_chars": planned_total - governed_total,
        # 正负分开报：合并/转删是回收，要素标签是加字。混成一个"回收率"会把两件事说成一件。
        "units_where_governance_removed_chars": sum(
            1 for cu_id, chars in charged.items() if chars > attributed.get(cu_id, 0)),
        "units_where_governance_added_chars": sum(
            1 for cu_id, chars in charged.items() if chars < attributed.get(cu_id, 0)),
        "attribution_mixed_source_merges": mixed,
        "tables_planned": report.get("tables_planned", 0),
        "tables_rendered": report.get("tables_rendered", 0),
        "tables_dropped": report.get("tables_dropped", 0),
        "tables_merged_groups": report.get("tables_merged_groups", 0),
        "low_information_dropped": report.get("low_information_dropped", 0),
        "per_content_unit": [
            {"content_unit_id": cu_id, "charged_chars": charged[cu_id],
             "governed_chars": attributed.get(cu_id, 0),
             "reclaimed_chars": charged[cu_id] - attributed.get(cu_id, 0)}
            for cu_id in sorted(charged)],
        "dropped_tables": list(report.get("dropped") or []),
        "merges": list(report.get("merges") or []),
    }


def strip_cu_structures(fragment: dict[str, Any]) -> dict[str, Any]:
    """去掉上一次 CU 装配追加的表格/流程，留下 base 自带的那些（供应商正文、手工规范化）。"""
    kept = {
        "tables": [row for row in (fragment.get("tables") or [])
                   if not (isinstance(row, dict) and row.get(CU_STRUCTURE_PROVENANCE))],
        "processes": [row for row in (fragment.get("processes") or [])
                      if not (isinstance(row, dict) and row.get(CU_STRUCTURE_PROVENANCE))],
    }
    dropped = {key: len(fragment.get(key) or []) - len(rows) for key, rows in kept.items()}
    return {**fragment, **kept, "cu_structures_stripped": dropped}


def apply_content_units(rows: list[dict[str, Any]], *, base_fragment: dict[str, Any],
                        section_id: str, planned_ids: list[str],
                        missing_ids: list[str] | None = None) -> dict[str, Any]:
    """用 `rows`（到达的 CU 记录）重建 `base_fragment` 的正文与结构槽位。

    返回深拷贝，不改入参：调用方手里可能是仍在使用的旧 fragment。
    """
    outstanding = list(missing_ids if missing_ids is not None
                       else [cu for cu in planned_ids if cu not in {str(r.get("content_unit_id")) for r in rows}])
    assembled = copy.deepcopy(base_fragment)
    retained = strip_cu_structures(assembled)
    assembled.update({key: retained[key] for key in ("tables", "processes")})
    blocks: list[dict[str, Any]] = []
    base_tables = [item for item in (assembled.get("tables") or []) if isinstance(item, dict)]
    cu_tables = cu_table_descriptors(rows)
    processes = list(assembled.get("processes") or [])
    for row in rows:
        archetype = str(row.get("archetype") or "NARRATIVE")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        content = str(row.get("content") or "")
        if content:
            blocks.append({"type": "paragraph", "content": content,
                           CU_STRUCTURE_PROVENANCE: row.get('content_unit_id')})
        if archetype in PROCESS_ARCHETYPES and payload.get("steps"):
            processes.append({**payload, CU_STRUCTURE_PROVENANCE: row.get("content_unit_id")})
        elif archetype == "CHECKLIST" and payload.get("items"):
            blocks.append({"type": "bullet_group", "content": payload.get("items"),
                           CU_STRUCTURE_PROVENANCE: row.get("content_unit_id")})
    assembled["body_blocks"] = blocks
    # 表格槽位保持"治理前"的形状：治理是渲染前的动作（build_docx 与预算账各自调同一个
    # `plan_section_tables`），在这里就改掉会让"CU 交了什么"失去可查的原始形状。
    assembled["tables"] = [*base_tables, *cu_tables]
    assembled["processes"] = processes
    assembled["table_governance"] = table_governance_accounting(
        rows, section_id=section_id, base_fragment={"tables": base_tables},
        cu_tables=cu_tables)
    assembled["content_unit_fragments"] = [
        {
            "content_unit_id": row.get("content_unit_id"),
            "parent_section_id": section_id,
            "content": row.get("content") or "",
            "archetype": row.get("archetype") or "NARRATIVE",
            "payload": row.get("payload"),
            "generation_origin": row.get("generation_origin") or "provider_content_unit",
            **{key: copy.deepcopy(row[key]) for key in (
                "used_requirement_ids", "used_ku_ids", "planned_requirement_ids",
                "planned_scoring_item_ids", "section_response_used_requirement_ids",
                "requirement_usage_kind", "supplement_registration_id") if key in row},
        }
        for row in rows
    ]
    assembled["content_unit_execution"] = {
        "mode": "CONTENT_UNIT_GRANULAR",
        "generation_origin": "provider_content_unit",
        "assembled_content_unit_ids": [row.get("content_unit_id") for row in rows],
        "planned_content_unit_ids": list(planned_ids),
        "outstanding_content_unit_ids": outstanding,
    }
    for key in ("used_requirement_ids", "used_ku_ids"):
        groups = [base_fragment.get(key), *(row.get(key) for row in rows)]
        assembled[key] = list(dict.fromkeys(value for group in groups
                                             if isinstance(group, list)
                                             for value in group if isinstance(value, str)))
    return assembled
