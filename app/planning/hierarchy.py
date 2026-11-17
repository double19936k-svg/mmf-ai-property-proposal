"""MMF-007A 增量数据契约：分层文档树、Scoring Matrix、谱系与失效策略。

本模块为纯增量设计，不修改既有规划行为：
- 分层文档树（Volume -> Chapter -> Section -> Content Unit）在既有
  word-document-plan / section-contracts 之上包装生成，既有的 chapter_id
  （CHxx）与 section_id（Sxx-yy）保持不变，只新增 volume_id 与
  content_unit_id 两层。
- Scoring Matrix 把 Requirement Pack 中已有的 scoring_item_id（SCR-nnn）
  升级为可查询、可审计的独立产物，回答"评分项在哪里响应、有多少证据"。
- 谱系（lineage）+ 失效策略支撑长任务恢复：需求、事实或章节契约变化时，
  明确哪些已完成片段可以复用、哪些必须失效，避免把旧事实带入新稿。

红线：不改变既有 ID 体系，不替换 Section Contract，不绑定具体 Provider。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .content_engineering import CONTENT_ARCHETYPES, choose_archetype
from .scoring_evidence import (
    EVIDENCE_CONTRACT_VERSION,
    NAMED_FOCUS_RUBRICS,
    TECHNICAL_SCORING_GATE_MIN,
    artifact_evidence_index,
    judge_scoring_row,
)
from .scoring_graph import (
    graph_readings as scoring_graph_readings,
    leaf_by_id,
    response_text_for_item,
    rubric_lineage,
    rubric_lineage_index,
)
from .scoring_mapping import (
    MIN_DERIVED_UNIT_CHARS,
    SCORING_MAPPING_VERSION,
    plan_scoring_mapping,
)
from .scoring_responsiveness import (
    RESPONSIVENESS_VERSION,
    RESPONSE_DIMENSIONS,
    record_requires_body_response,
    record_requires_technical_acceptance,
    responsiveness_fields,
)
from .scoring_taxonomy import (
    COVERAGE_DENOMINATOR as COVERAGE_DENOMINATOR_CANONICAL_TOPICS,
    COVERAGE_DENOMINATOR_ALL_CLASSES,
    SCORING_MAPPING_GATE_FILE,
    build_scoring_taxonomy,
    canonical_status,
    evidence_contract_for,
    must_respond_of,
    pre_generation_scoring_mapping_gate,
    row_scoring_class,
)
from .unified_budget import (
    BUDGET_GATE_FILE,
    build_budget_tree,
    contract_budget_row,
    density_profile_for,
    plan_section_budget,
    pre_generation_budget_gate,
    rebalance_archetype_mix,
    section_budget_split,
    unified_unit_budget,
)

HIERARCHY_SCHEMA_VERSION = "document-hierarchy-v0.2-technical-bid"
SCORING_MATRIX_SCHEMA_VERSION = "scoring-matrix-v0.3"
# 旧的按行分母口径保留为并列字段，但它已经不是门控用的那一个。
COVERAGE_DENOMINATOR_LEGACY_ROWS = "TECHNICAL_SCORING_COVERAGE_ROWS"
LINEAGE_SCHEMA_VERSION = "hierarchy-lineage-v0.1"

VOLUME_ID = re.compile(r"^V\d{2}$")
CHAPTER_ID = re.compile(r"^CH\d{2}$")
SECTION_ID = re.compile(r"^S\d{2}-\d{2}$")
CONTENT_UNIT_ID = re.compile(r"^CU-S\d{2}-\d{2}-\d{2}$")
REQUIREMENT_ID = re.compile(r"^REQ-\d{4}$")
SCORING_ITEM_ID = re.compile(r"^SCR-\d{3}$")

# 一个内容单元的**统一**额度目标（正文面 + 结构体）。Run D 规划用 900 字/单元、
# 304 个单元；把额度改成统一货币后仍按这条线切单元数，Content Unit 规模与
# Provider 调用次数不会因为换尺子而变（§55 要减调用，不能靠加单元）。
UNIFIED_UNIT_TARGET_CHARS = 1000
# §10 的 reserve 带是 5–10%；取哪一点取决于该节的预测难度：
# 纯叙述写多少像多少，结构重的节会被行/栏几何夹出零头，表单型最容易造出占页空表。
RESERVE_SHARE_BY_PROFILE = {
    "NARRATIVE_HEAVY": 0.05, "BALANCED": 0.07,
    "STRUCTURED_HEAVY": 0.08, "FORM_STRICT": 0.10,
}
VOLUME_TITLE_PREFIX = {1: "第一篇", 2: "第二篇", 3: "第三篇", 4: "第四篇", 5: "第五篇", 6: "第六篇", 7: "第七篇", 8: "第八篇", 9: "第九篇"}
EXTENSION_VOLUME_ID = "V09"
EXTENSION_VOLUME_TITLE = "第九篇 招标结构扩展"

# T4.1 评分项分诊：商务/行政类评分项不属于技术标正文响应范围
COMMERCIAL_LABEL_RE = re.compile(
    r"报价|业绩|资质|授权|营业执照|信用|资格证明|财务|审计|税收|税务|社保|纳税|信誉|奖项|荣誉|类似项目|类似业绩"
)
LABEL_ROUTE_RULES = [
    ("S04-02", ["投诉", "闭环", "回访", "诉求", "kpi", "sla", "考核指标"]),
    ("S04-01", ["服务界面", "受理渠道", "客服", "客户服务"]),
    ("S02-01", ["服务构想", "服务理念", "总体思路", "服务定位", "管理思路"]),
    ("S02-02", ["运营机制", "协同", "沟通机制"]),
    ("S01-02", ["重难点", "难点", "业态特点", "项目理解", "针对性"]),
    ("S01-01", ["项目概况", "基本情况", "总体概况"]),
    ("S03-01", ["组织架构", "组织机构", "职责分工", "管理体系"]),
    ("S03-02", ["人员配置", "人员安排", "岗位设置", "排班", "服务时间", "人员素质"]),
    ("S03-03", ["培训", "考核", "人才", "梯队"]),
    ("S05-02", ["安全", "秩序", "巡逻", "安保", "门岗"]),
    ("S05-03", ["通行", "车辆", "出入", "门禁"]),
    ("S06-01", ["设施设备", "设备台账", "设施管理"]),
    ("S06-02", ["工程", "维保", "维修", "巡检", "设备运行"]),
    ("S06-03", ["能耗", "节能", "运行记录"]),
    ("S07-01", ["环境", "保洁", "绿化", "保洁标准"]),
    ("S07-02", ["保洁质量", "环境检查", "卫生"]),
    ("S08-01", ["品质", "质量检查", "质量保证"]),
    ("S08-02", ["持续改进", "数据分析", "复盘"]),
    ("S09-01", ["进场", "交接", "承接查验", "开办"]),
    ("S09-02", ["报告制度", "沟通汇报", "例会"]),
    ("S10-01", ["应急", "突发事件", "消防"]),
    ("S10-02", ["演练", "预案"]),
    ("S11-01", ["实施计划", "里程碑", "进度安排", "进驻计划"]),
    ("S11-02", ["履约", "成果文件", "档案", "资料管理"]),
    ("S11-03", ["待澄清", "承诺边界", "假设"]),
]

VOLUME_RULES = [
    ("V01", "项目理解与总体思路", ["项目理解", "服务构想", "总体运营", "需求边界"]),
    ("V02", "组织及人员配置", ["组织", "人员"]),
    ("V03", "客户服务", ["客户服务"]),
    ("V04", "安全与秩序", ["安全", "秩序", "通行"]),
    ("V05", "工程管理", ["工程", "设施", "设备"]),
    ("V06", "环境与品质", ["环境", "品质"]),
    ("V07", "实施与保障", ["实施", "应急", "进场", "履约", "承诺"]),
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _requirement_rows(pack: dict[str, Any]) -> list[dict[str, Any]]:
    rows = pack.get("requirements")
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]
    explicit = pack.get("explicit_requirements")
    if isinstance(explicit, list):
        return [row for row in explicit if isinstance(row, dict)]
    return []


def _default_volume_id(chapter_title: str) -> str:
    for volume_id, _title, keywords in VOLUME_RULES:
        if any(keyword in chapter_title for keyword in keywords):
            return volume_id
    return EXTENSION_VOLUME_ID


def _volume_title(volume_id: str, chapters: list[dict[str, Any]]) -> str:
    for known_id, title, _keywords in VOLUME_RULES:
        if known_id == volume_id:
            index = int(volume_id[1:])
            prefix = VOLUME_TITLE_PREFIX.get(index, volume_id)
            return f"{prefix} {title}"
    return EXTENSION_VOLUME_TITLE


def _contract_target_words(contract: dict[str, Any]) -> int:
    value = contract.get("target_words")
    if isinstance(value, dict):
        return int(value.get("target") or value.get("max") or value.get("min") or 0)
    if isinstance(value, (int, float, str)) and str(value).strip():
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def reserve_share_for(profile: str) -> float:
    """该节留多少机动额度：由密度画像决定，不全局写死（§10/§11）。"""
    return float(RESERVE_SHARE_BY_PROFILE.get(profile, 0.07))


def _content_unit_plan(
    section: dict[str, Any],
    contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """按章节契约把 Section 拆为内容单元，并给出**统一**内容额度（§7–§11）。

    与 Round 3 的差别只有一句话：结构化 Archetype 的表体/步骤/清单/表单字数
    现在从这一节的额度里**扣**，不再是额度之外长出来的东西。Run D 实测那一路
    多写了 203,778 字（占实际交付 43.7%），这就是 395 页超上限 75 页的结构性来源。
    """
    target_words = _contract_target_words(contract)
    unit_count = max(1, -(-max(target_words, 1) // UNIFIED_UNIT_TARGET_CHARS)) if target_words else 1
    must_cover = [str(item) for item in (contract.get("must_cover") or []) if str(item).strip()]
    requirement_ids = [str(row) for row in (contract.get("source_requirements") or [])]
    scoring_item_ids = [str(row) for row in (contract.get("scoring_items") or [])]
    section_id = section["section_id"]
    # Archetype 组合先按该节的密度画像归位（§11）：表单附件型限量、叙述重的节少排表。
    planned_archetypes = [choose_archetype(section, contract, index) for index in range(1, unit_count + 1)]
    profile = density_profile_for({**section, "purpose": contract.get("purpose"),
                                   "must_cover": must_cover})
    mixed = rebalance_archetype_mix(planned_archetypes, profile=profile)
    archetypes = mixed["archetypes"]
    unit_budgets = (plan_section_budget(target_words, archetypes,
                                        reserve_share=reserve_share_for(profile))["unit_budgets"]
                    if target_words else [unified_unit_budget(key, UNIFIED_UNIT_TARGET_CHARS)
                                          for key in archetypes])
    units: list[dict[str, Any]] = []
    for index, archetype in enumerate(archetypes, start=1):
        purpose = must_cover[index - 1] if index <= len(must_cover) else (
            must_cover[(index - 1) % len(must_cover)] if must_cover else section.get("section_title", "")
        )
        budget = unit_budgets[index - 1] if index - 1 < len(unit_budgets) else unified_unit_budget(
            archetype, UNIFIED_UNIT_TARGET_CHARS)
        target_budget = contract_budget_row(archetype, budget)
        min_budget = contract_budget_row(
            archetype, unified_unit_budget(archetype, int(round(budget["effective_chars"] * 0.8)) + 1))
        max_budget = contract_budget_row(
            archetype, unified_unit_budget(archetype, int(round(budget["effective_chars"] * 1.2)) + 1))
        units.append({
            "content_unit_id": f"CU-{section_id}-{index:02d}",
            "section_id": section_id,
            "parent_section_id": section_id,
            "chapter_id": section.get("chapter_id"),
            "unit_index": index,
            "title": purpose if must_cover else f"{section.get('section_title', '')}（{index}/{unit_count}）",
            "planned_words": target_budget["effective_chars"],
            "purpose": purpose,
            "intent": purpose,
            "archetype": archetype,
            "density_profile": profile,
            "budget_measure": "unified_effective_chars",
            "unified_budget": budget,
            "must_cover": [purpose] if purpose else [],
            "required_outputs": list(contract.get("required_outputs") or []),
            "requirement_ids": requirement_ids,
            "source_requirements": requirement_ids,
            "scoring_item_ids": scoring_item_ids,
            "source_scoring_items": scoring_item_ids,
            "fact_dependencies": list(contract.get("fact_dependencies") or []),
            "evidence_dependencies": list(contract.get("evidence_dependencies") or []),
            "min_budget": min_budget,
            "target_budget": target_budget,
            "max_budget": max_budget,
            "status": "PLANNED",
            "generation_state": {"attempt_count": 0, "fragment_path": None, "qa_status": None, "updated_at": None},
        })
    return units


def _word_page_targets(bundle: dict[str, Any]) -> tuple[int, int] | None:
    """页数区间只有一个口径：技术有效页。规划、生成前门控、事后 QA 用的都是它（§4/§21）。

    拿不到区间就不投影 —— 返回 None 让门控报"没有页带"，而不是默认一个 250–320，
    把 150 页级方案按 300 页级的天花板放过。
    """
    word = ((bundle or {}).get("word_plan") or {}).get("document_target") or {}
    if not word:
        word = ((bundle or {}).get("document_target") or {}).get("word") or {}
    low = int(word.get("target_pages_min") or 0)
    high = int(word.get("target_pages_max") or 0)
    return (low, high) if low and high >= low else None


def build_unified_budget_view(bundle: dict[str, Any], sections: list[dict[str, Any]],
                              units_by_section: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """§8/§13：把分层树折算成五层统一预算，并跑生成前门控。

    Document 层取**另一条代码路径**的章节内容预算总数（`content_budget` 按章分配，
    Section/CU 按契约分配）。两条路径对不上，说明钱在某一层的分配里丢了或凭空多了，
    这正是守恒检查要抓的东西 —— 如果 Document 层也从 CU 求和上来，这个检查就是自证。
    """
    tree_volumes: dict[str, dict[str, Any]] = {}
    for section in sections:
        section_id = str(section["section_id"])
        split = section.get("budget_split") or {}
        volume_row = tree_volumes.setdefault(str(section["volume_id"]),
                                             {"volume_id": str(section["volume_id"]), "sections": []})
        volume_row["sections"].append({
            "section_id": section_id,
            "planned_total_chars": int(split.get("section_total_budget") or 0),
            "content_units": units_by_section.get(section_id, []),
        })
    chapter_rows = (bundle.get("content_budget") or {}).get("chapters") or []
    chapter_document_budget = sum(int(row.get("target_words_target") or 0) for row in chapter_rows)
    section_document_budget = sum(int((row.get("budget_split") or {}).get("section_total_budget") or 0)
                                  for row in sections)
    tree = build_budget_tree(
        document_target=chapter_document_budget or section_document_budget,
        volumes=[tree_volumes[key] for key in sorted(tree_volumes)],
        page_targets=_word_page_targets(bundle))
    tree["document_budget_source"] = ("CONTENT_BUDGET_CHAPTERS" if chapter_document_budget
                                      else "SECTION_CONTRACT_TARGETS")
    tree["document_budget_from_sections"] = section_document_budget
    tree["document_budget_from_chapters"] = chapter_document_budget
    return {"tree": tree, "gate": pre_generation_budget_gate(tree, page_targets=_word_page_targets(bundle))}


def build_document_hierarchy(pack: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """把既有规划包包装为分层文档树。不修改既有 outline 数据。"""
    outline = (bundle.get("word_plan") or {}).get("outline") or []
    contracts = {row.get("section_id"): row for row in (bundle.get("section_contracts") or {}).get("contracts") or []}

    volume_ids: dict[str, str] = {}
    volumes: dict[str, dict[str, Any]] = {}
    for chapter in outline:
        chapter_id = chapter.get("chapter_id")
        volume_id = _default_volume_id(str(chapter.get("chapter_title", "")))
        volume_ids[chapter_id] = volume_id
        volume = volumes.setdefault(volume_id, {
            "volume_id": volume_id,
            "volume_title": _volume_title(volume_id, outline),
            "chapter_ids": [],
            "target_pages": 0,
            "budget": {"min": 0, "target": 0, "max": 0, "measure": "effective_chars"},
            "priorities": [],
        })
        volume["chapter_ids"].append(chapter_id)
        volume["target_pages"] += int(chapter.get("target_pages") or 0)
        if chapter.get("priority"):
            volume["priorities"].append(chapter["priority"])

    chapters: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    content_units: list[dict[str, Any]] = []
    for chapter in outline:
        chapter_id = chapter.get("chapter_id")
        chapters.append({**chapter, "volume_id": volume_ids[chapter_id]})
        for section in chapter.get("sections") or []:
            section_id = section.get("section_id")
            contract = contracts.get(section_id) or {}
            section_entry = {
                "section_id": section_id,
                "section_title": section.get("section_title"),
                "chapter_id": chapter_id,
                "volume_id": volume_ids[chapter_id],
                "priority": chapter.get("priority"),
            }
            sections.append(section_entry)
            content_units.extend(_content_unit_plan(section_entry, contract))

    units_by_section: dict[str, list[dict[str, Any]]] = {}
    for unit in content_units:
        units_by_section.setdefault(str(unit["section_id"]), []).append(unit)
    for section in sections:
        section_id = str(section["section_id"])
        units = units_by_section.get(section_id, [])
        contract = contracts.get(section_id) or {}
        # CU 求和是"实际排出去的字"，契约总量还含机动额度（§10 reserve）。
        # 两者都记下来，守恒检查才有对象：差额必须等于 reserve，不能是"不知道去哪了"。
        section["budget"] = {
            "min": sum(int((unit.get("min_budget") or {}).get("effective_chars") or 0) for unit in units),
            "target": sum(int((unit.get("target_budget") or {}).get("effective_chars") or 0) for unit in units),
            "max": sum(int((unit.get("max_budget") or {}).get("effective_chars") or 0) for unit in units),
            "measure": "unified_effective_chars",
        }
        planned_total = _contract_target_words(contract)
        section["budget_split"] = section_budget_split(
            planned_total or section["budget"]["target"],
            [(unit.get("unified_budget") or {}) for unit in units])
        section["density_profile"] = next((str(unit.get("density_profile") or "") for unit in units), "")
    sections_by_chapter: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        sections_by_chapter.setdefault(str(section["chapter_id"]), []).append(section)
    for chapter in chapters:
        chapter_sections = sections_by_chapter.get(str(chapter["chapter_id"]), [])
        chapter["budget"] = {
            key: sum(int((row.get("budget") or {}).get(key) or 0) for row in chapter_sections)
            for key in ("min", "target", "max")
        }
        chapter["budget"]["measure"] = "effective_chars"
    chapters_by_volume: dict[str, list[dict[str, Any]]] = {}
    for chapter in chapters:
        chapters_by_volume.setdefault(str(chapter["volume_id"]), []).append(chapter)
    for volume in volumes.values():
        volume_chapters = chapters_by_volume.get(str(volume["volume_id"]), [])
        volume["budget"] = {
            key: sum(int((row.get("budget") or {}).get(key) or 0) for row in volume_chapters)
            for key in ("min", "target", "max")
        }
        volume["budget"]["measure"] = "effective_chars"

    hierarchy = {
        "schema_version": HIERARCHY_SCHEMA_VERSION,
        "pack_id": pack.get("pack_id"),
        "created_at": now_iso(),
        "volumes": [volumes[volume_id] for volume_id in sorted(volumes)],
        "chapters": chapters,
        "sections": sections,
        "content_units": content_units,
        "id_rules": {
            "volume_id": "V\\d{2}（V01-V08 为领域篇，V09 为招标结构扩展篇）",
            "chapter_id": "CH\\d{2}（沿用既有目录，新增章节顺序扩展）",
            "section_id": "S\\d{2}-\\d{2}（沿用既有目录）",
            "content_unit_id": "CU-S\\d{2}-\\d{2}-\\d{2}（Section 内顺序稳定，不跨 Section 复用）",
        },
        "counts": {
            "volumes": len(volumes),
            "chapters": len(chapters),
            "sections": len(sections),
            "content_units": len(content_units),
            "planned_words": sum(int(unit["planned_words"]) for unit in content_units),
            "content_units_by_archetype": {
                archetype: sum(unit.get("archetype") == archetype for unit in content_units)
                for archetype in CONTENT_ARCHETYPES
            },
        },
        "budget_hierarchy": "Document > Volume > Chapter > Section > Content Unit",
        "supported_archetypes": list(CONTENT_ARCHETYPES),
    }
    unified = build_unified_budget_view(bundle, sections, units_by_section)
    hierarchy["budget_tree"] = unified["tree"]
    hierarchy["pre_generation_budget_gate"] = unified["gate"]
    hierarchy["validation"] = validate_hierarchy(hierarchy)
    return hierarchy


def validate_hierarchy(hierarchy: dict[str, Any]) -> dict[str, Any]:
    """结构校验：重复 ID、缺失层级、孤儿单元、非法 ID 格式。"""
    errors: list[dict[str, str]] = []
    volume_ids = [row.get("volume_id") for row in hierarchy.get("volumes") or []]
    chapter_ids = [row.get("chapter_id") for row in hierarchy.get("chapters") or []]
    section_ids = [row.get("section_id") for row in hierarchy.get("sections") or []]
    unit_ids = [row.get("content_unit_id") for row in hierarchy.get("content_units") or []]

    def _duplicates(ids: list[Any], code: str) -> None:
        seen: set[str] = set()
        for value in ids:
            key = str(value)
            if key in seen:
                errors.append({"code": code, "subject": key, "message": f"重复ID：{key}"})
            seen.add(key)

    _duplicates(volume_ids, "DUPLICATE_VOLUME_ID")
    _duplicates(chapter_ids, "DUPLICATE_CHAPTER_ID")
    _duplicates(section_ids, "DUPLICATE_SECTION_ID")
    _duplicates(unit_ids, "DUPLICATE_CONTENT_UNIT_ID")

    valid_volume_ids = set(volume_ids)
    for chapter in hierarchy.get("chapters") or []:
        if chapter.get("volume_id") not in valid_volume_ids:
            errors.append({"code": "MISSING_VOLUME", "subject": str(chapter.get("chapter_id")),
                           "message": f"章节 {chapter.get('chapter_id')} 指向不存在的篇 {chapter.get('volume_id')}"})
        if not CHAPTER_ID.match(str(chapter.get("chapter_id") or "")):
            errors.append({"code": "BAD_CHAPTER_ID_FORMAT", "subject": str(chapter.get("chapter_id")), "message": "chapter_id 不符合 CH\\d{2}"})
    valid_chapter_ids = set(chapter_ids)
    valid_section_ids = set(section_ids)
    for section in hierarchy.get("sections") or []:
        if section.get("chapter_id") not in valid_chapter_ids:
            errors.append({"code": "MISSING_CHAPTER", "subject": str(section.get("section_id")),
                           "message": f"Section {section.get('section_id')} 指向不存在的章节 {section.get('chapter_id')}"})
        if not SECTION_ID.match(str(section.get("section_id") or "")):
            errors.append({"code": "BAD_SECTION_ID_FORMAT", "subject": str(section.get("section_id")), "message": "section_id 不符合 S\\d{2}-\\d{2}"})
    for unit in hierarchy.get("content_units") or []:
        if unit.get("section_id") not in valid_section_ids:
            errors.append({"code": "ORPHAN_CONTENT_UNIT", "subject": str(unit.get("content_unit_id")),
                           "message": f"内容单元 {unit.get('content_unit_id')} 指向不存在的 Section {unit.get('section_id')}"})
        if not CONTENT_UNIT_ID.match(str(unit.get("content_unit_id") or "")):
            errors.append({"code": "BAD_CONTENT_UNIT_ID_FORMAT", "subject": str(unit.get("content_unit_id")), "message": "content_unit_id 不符合 CU-S\\d{2}-\\d{2}-\\d{2}"})
    for row in hierarchy.get("volumes") or []:
        if not VOLUME_ID.match(str(row.get("volume_id") or "")):
            errors.append({"code": "BAD_VOLUME_ID_FORMAT", "subject": str(row.get("volume_id")), "message": "volume_id 不符合 V\\d{2}"})
    for section_id in section_ids:
        if not any(unit.get("section_id") == section_id for unit in hierarchy.get("content_units") or []):
            errors.append({"code": "SECTION_WITHOUT_CONTENT_UNIT", "subject": str(section_id), "message": f"Section {section_id} 没有任何内容单元"})

    return {"status": "PASS" if not errors else "FAIL", "errors": errors, "checked_at": now_iso()}


def _route_label(label: str, outline_order: dict[str, int]) -> list[str]:
    """按评分项标签关键词推导响应章节（可审计的派生映射，仅在无需求引用时使用）。"""
    text = str(label or "")
    for section_id, keywords in LABEL_ROUTE_RULES:
        if any(keyword in text.lower() for keyword in keywords) and section_id in outline_order:
            return [section_id]
    return []


def _triage_scoring_row(
    item: dict[str, Any],
    requirement_ids: list[str],
    mapped_sections: list[str],
    outline_order: dict[str, int],
    scope_disposition: str | None = None,
) -> tuple[str, str | None, list[str], str | None]:
    """评分项分诊：范围门禁排除 > 需求引用 > 商务项排除 > 标签派生路由 > 真缺失。

    返回 (response_status, mapping_source, section_ids, missing_reason)。
    """
    if scope_disposition == "EXCLUDED":
        return "OUT_OF_TECHNICAL_SCOPE", "technical_scope_gate", [], (
            "TECHNICAL_ONLY_SCOPE_GATE：该评分项属商务/报价/资格/资信/程序类，"
            "仅登记为 EXCLUDED_FROM_TECHNICAL_BID，不在技术正文建立响应。"
        )
    if mapped_sections:
        return "PLANNED", "requirement_link", mapped_sections, None
    label = str(item.get("label") or "")
    if COMMERCIAL_LABEL_RE.search(label):
        return "OUT_OF_TECHNICAL_SCOPE", "label_triage", [], "商务/行政类评分项（报价、业绩、资质等），不属于技术标正文响应范围，由商务标文件响应。"
    derived = _route_label(label, outline_order)
    if derived:
        return "DERIVED_PLANNED", "label_keyword_routing", derived, None
    if not requirement_ids:
        return "MISSING", None, [], "未映射到任何可用Section，且标签无法推导响应章节；需人工指定落点。"
    return "MISSING", None, [], "需求引用存在但路由未命中任何可用Section；需人工指定落点。"


def _entity_contract(label: Any, planned_evidence: dict[str, Any],
                     *, responsiveness_class: str = "") -> dict[str, Any]:
    """规划期写下的实体证据合同：生成后按同一份合同判定是否 FULFILLED。

    实现并入 §32 的 `evidence_contract_for`：规划期合同与 canonical 记录里的合同
    必须同源，两份各自分词迟早会分叉。MMF008A-A 起合同还要带第二维——
    匿名组织设计的合同要求结构化载体，验收时就不再另判一次。
    """
    return evidence_contract_for(label, planned_evidence,
                                 responsiveness_class=responsiveness_class)


def build_scoring_matrix(
    pack: dict[str, Any],
    bundle: dict[str, Any],
    hierarchy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 Requirement Pack 的评分项升级为可查询、可审计的 Scoring Matrix。"""
    outline = (bundle.get("word_plan") or {}).get("outline") or []
    contracts = {row.get("section_id"): row for row in (bundle.get("section_contracts") or {}).get("contracts") or []}
    matrix_rows = (bundle.get("requirement_matrix") or {}).get("matrix") or []
    outline_order = {row.get("section_id"): index for index, row in enumerate(
        section for chapter in outline for section in chapter.get("sections") or [])}

    reqs_by_scoring: dict[str, list[str]] = {}
    linkage_by_req: dict[str, dict[str, Any]] = {}
    req_meta: dict[str, dict[str, Any]] = {}
    for req in _requirement_rows(pack):
        req_id = str(req.get("requirement_id") or "")
        req_meta[req_id] = req
        scoring_id = req.get("scoring_item_id")
        if scoring_id:
            reqs_by_scoring.setdefault(str(scoring_id), []).append(req_id)
    # SCORING_REFERENCE_LINKAGE 注册表（V1.1 Phase 1）：独立于需求行字段的确定性关联
    linkage_confidence: dict[str, float] = {}
    for linkage in pack.get("scoring_linkages") or []:
        if not isinstance(linkage, dict):
            continue
        scoring_id = str(linkage.get("scoring_item_id") or "")
        req_id = str(linkage.get("requirement_id") or "")
        if scoring_id and req_id:
            reqs_by_scoring.setdefault(scoring_id, []).append(req_id)
            linkage_confidence[req_id] = float(linkage.get("mapping_confidence") or 0.0)
            linkage_by_req[req_id] = linkage

    section_by_requirement: dict[str, dict[str, Any]] = {}
    for row in matrix_rows:
        if row.get("coverage_status") != "UNMAPPED" and row.get("primary_section_id"):
            section_by_requirement[str(row.get("requirement_id"))] = row

    # §23：范围类别只认 TECHNICAL_ONLY_SCOPE_GATE 一处判定，标签正则仅作门禁缺席时的兜底。
    gate = bundle.get("technical_scope_gate") or {}
    scope_by_scoring: dict[str, dict[str, Any]] = {
        str(row.get("scoring_item_id")): row
        for row in gate.get("scoring_register") or []
        if isinstance(row, dict) and row.get("scoring_item_id")
    }
    # 计划期就写好响应 Content Unit：分层树里的 CU 已按契约挂了 scoring 归属。
    units_by_section: dict[str, list[str]] = {}
    for unit in (hierarchy or {}).get("content_units") or []:
        if not isinstance(unit, dict):
            continue
        for scoring_id in unit.get("scoring_item_ids") or []:
            units_by_section.setdefault(str(unit.get("section_id")), []).append(str(unit.get("content_unit_id")))

    scoring_items = pack.get("scoring_items") or []
    # 评分表结构图（`tender_intake.pack_builder` 在建包时按单元格现算）。图不在或建不出来时
    # 这里就是空索引，行照旧按打平标签分类——退路可见，不装作有结构。
    scoring_graph = pack.get("scoring_graph") or {}
    graph_leaves = leaf_by_id(scoring_graph) if scoring_graph.get("status") == "DERIVED" else {}
    lineage_index = rubric_lineage_index(scoring_graph)
    rows: list[dict[str, Any]] = []
    for item in scoring_items:
        if not isinstance(item, dict):
            continue
        scoring_id = str(item.get("scoring_item_id") or "")
        requirement_ids = reqs_by_scoring.get(scoring_id, [])
        mapped_sections: list[str] = []
        for req_id in requirement_ids:
            mapped_row = section_by_requirement.get(req_id) or {}
            if mapped_row.get("coverage_status") != "UNMAPPED" and mapped_row.get("primary_section_id") and mapped_row.get("primary_section_id") not in mapped_sections:
                mapped_sections.append(mapped_row.get("primary_section_id"))
            for secondary in mapped_row.get("secondary_section_ids") or []:
                if secondary not in mapped_sections:
                    mapped_sections.append(secondary)
        mapped_sections = [sid for sid in mapped_sections if sid in outline_order]
        mapped_sections.sort(key=lambda sid: outline_order[sid])
        primary = contracts.get(mapped_sections[0]) if mapped_sections else None
        planned_evidence = {
            "tables": len(primary.get("required_tables") or []) if primary else 0,
            "processes": len(primary.get("required_processes") or []) if primary else 0,
            "required_outputs": list(primary.get("required_outputs") or []) if primary else [],
        }
        planned_words = sum(_contract_target_words(contracts.get(sid) or {}) for sid in mapped_sections) if mapped_sections else 0
        scoring_scope = scope_by_scoring.get(scoring_id) or {}
        scope_class = str(scoring_scope.get("scope_class") or "") or None
        scope_disposition = str(scoring_scope.get("disposition") or "") or None
        response_status, mapping_source, mapped_sections, missing_reason = _triage_scoring_row(
            item, requirement_ids, mapped_sections, outline_order, scope_disposition
        )
        # MMF008A-A 第二维在**行上**就要落字段：合同（下面的 entity_contract）、自动映射
        # （plan_scoring_mapping 会不会给它派生一个正文单元）与终局覆盖率读的都是同一份。
        # 与 canonical 记录同源：两处都调 `row_scoring_class` + `responsiveness_fields`，
        # 输入只有"这一格文本"与 scope_class，所以同一行必然分出同一类。
        # MMF008A2-A：文本取评分表里的 事项名 + 本行评分标准（`scoring_graph` 一处决定），
        # 落在叶子上才换，落不到仍是打平标签——与 taxonomy 走同一个 `response_text_for_item`。
        structural_text, text_basis = response_text_for_item(item, graph_leaves)
        must_respond, must_respond_basis = must_respond_of(item)
        row_scoring_class_name = row_scoring_class(
            {"label": item.get("label"), "scope_class": scope_class})
        responsiveness = responsiveness_fields(
            item.get("label"), scoring_class=row_scoring_class_name,
            structural_text=structural_text, text_basis=text_basis,
            lineage=rubric_lineage(item, scoring_graph, leaves=graph_leaves,
                                   index=lineage_index))
        if response_status == "PLANNED" and any(req_id in linkage_by_req for req_id in requirement_ids):
            mapping_source = "scoring_linkage+requirement_link"
        rows.append({
            "scoring_item_id": scoring_id,
            "label": item.get("label"),
            # 来源必须随行落盘：`rubric_lineage` 与第一维分类器读的都是这一格（章节路径、
            # 表/段定位、表格 id）。以前行上不带它，规划期用 pack 项算出的血缘就在
            # taxonomy 重标的那一刻被 null 覆盖，矩阵往下游（Authority、终局覆盖率）
            # 只剩"有 anchor_basis 却没有来源"的半截证据。
            "source": dict(item.get("source") or {}),
            "max_score": item.get("max_score"),
            "must_respond": must_respond,
            # 这一格的凭据从哪来：pack 写过，还是因为"是评分事项"推出来的。
            # 与派生叶子行同一份 schema（`scoring_taxonomy.SCORING_ROW_REQUIRED_FIELDS`）。
            "must_respond_basis": must_respond_basis,
            "scoring_leaf_id": item.get("scoring_leaf_id"),
            "scope_class": scope_class,
            "scope_disposition": scope_disposition,
            "requirement_ids": requirement_ids,
            "section_ids": mapped_sections,
            "primary_section_id": mapped_sections[0] if mapped_sections else None,
            "planned_response_cu": sorted({cid for sid in mapped_sections for cid in units_by_section.get(sid, [])}),
            "mapping_source": mapping_source,
            "mapping_confidence": round(max((linkage_confidence.get(req_id, 0.0) for req_id in requirement_ids), default=0.0), 2) if mapping_source == "scoring_linkage+requirement_link" else (1.0 if mapping_source == "requirement_link" else None),
            "planned_evidence": planned_evidence,
            "entity_contract": _entity_contract(structural_text, planned_evidence,
                                                responsiveness_class=str(
                                                    responsiveness["responsiveness_class"])),
            "planned_response_words": planned_words,
            "response_status": response_status,
            "missing_reason": missing_reason,
            **responsiveness,
            "fulfillment": {
                "status": "NOT_GENERATED",
                "effective_chars": 0,
                "evidence_found": False,
                "response_section": mapped_sections[0] if mapped_sections else None,
                "response_cu": [],
                "evidence_text": [],
                "artifact_location": [],
                "last_validated_at": None,
            },
        })

    scoring_ids = {str(row.get("scoring_item_id")) for row in rows}
    matrix_scoring_refs = {
        str(row.get("scoring_item_id"))
        for row in matrix_rows
        if row.get("scoring_item_id") and str(row.get("scoring_item_id")) not in scoring_ids
    }
    # §24–§37：十类预分类 + 唯一实质主题去重。分类发生在路由之后，
    # 所以 canonical 记录继承的就是这份规划里的落点，不是另算一套。
    taxonomy = build_scoring_taxonomy(rows, (hierarchy or {}).get("content_units"),
                                      scoring_graph or None)
    rows = taxonomy["rows"]
    record_by_scoring = {
        str(source_id): record
        for record in taxonomy["canonical_scoring_records"]
        for source_id in record.get("evaluation_row_ids", record["source_ids"])
    }
    from .scoring_response_points import bind_source_obligations
    for record in taxonomy['canonical_scoring_records']:
        bind_source_obligations(record.get('evidence_contract') or {}, pack)
    for row in rows:
        record = record_by_scoring.get(str(row.get('scoring_item_id') or ''))
        if record:
            row['entity_contract'] = record['evidence_contract']
    for row in rows:
        record = record_by_scoring.get(str(row.get("scoring_item_id") or ""))
        # planned_response_cu 过去把整节的 CU 都算作"这条评分项的响应"，等于没有绑定。
        # 真绑定放在 *_bound 里，未绑定的必须看得见，不能靠整节候选蒙过去。
        row["planned_response_cu_bound"] = list((record or {}).get("planned_response_cu") or [])
        row["cu_binding_basis"] = ((record or {}).get("cu_binding") or {}).get("basis") or (
            "NOT_IN_TECHNICAL_DENOMINATOR" if record is None else "UNBOUND")
        row["canonical_scoring_id"] = (record or {}).get("canonical_scoring_id")
    # R41-P0-2 §16：分类与去重之后还缺"落点"这一段。没有落点的主题一律先自动映射，
    # 映射不出来才派生 Technical Response 单元；两者都写进 mapping_* 字段供人工回查。
    mapping = plan_scoring_mapping(
        taxonomy["canonical_scoring_records"],
        _scoring_section_shapes(hierarchy, bundle),
        units_by_section=_units_by_section((hierarchy or {}).get("content_units")),
    )
    for row in rows:
        record = record_by_scoring.get(str(row.get("scoring_item_id") or ""))
        if not record or row.get("section_ids"):
            continue
        # 落点回填到行：验收读的是 `judge_scoring_row(row)` 的 section_ids，
        # 只补 canonical 记录不补行，主题就"计划里有、验收里查无"。
        row["section_ids"] = [str(sid) for sid in (record.get("planned_response_sections") or []) if sid]
        row["primary_section_id"] = record.get("planned_response_section")
        row["planned_response_cu_bound"] = list(record.get("planned_response_cu") or [])
        row["cu_binding_basis"] = (record.get("cu_binding") or {}).get("basis") or record.get("mapping_basis")
        if row["section_ids"]:
            row["response_status"] = "DERIVED_PLANNED"
            row["mapping_source"] = "scoring_topic_auto_mapping"
            row["mapping_reason"] = str(record.get("mapping_reason") or "")
            row["mapping_confidence"] = record.get("mapping_confidence")
            row["missing_reason"] = None
    canonical_records = mapping["records"]
    from .scoring_global_acceptance import build_global_plan
    global_acceptance_plan = build_global_plan(pack, bundle)
    for row in rows:
        record = record_by_scoring.get(str(row.get('scoring_item_id') or ''))
        if record:
            row['planned_response_cu_bound'] = list(record.get('planned_response_cu') or [])
            if record_requires_body_response(record):
                row['section_ids'] = list(record.get('planned_response_sections') or [])
                row['primary_section_id'] = record.get('planned_response_section')
            if record.get('responsiveness_class') == 'WHOLE_DOCUMENT_RUBRIC':
                row['section_ids'] = []
    technical_rows = [row for row in rows if row.get("scope_disposition") != "EXCLUDED"]
    matrix = {
        "schema_version": SCORING_MATRIX_SCHEMA_VERSION,
        "pack_id": pack.get("pack_id"),
        "created_at": now_iso(),
        "rows": rows,
        "matrix_only_scoring_refs": sorted(matrix_scoring_refs),
        "scoring_taxonomy": {key: taxonomy[key] for key in (
            "schema_version", "allowed_scoring_classes", "allowed_scoring_statuses",
            "disposition_by_class", "scope_class_to_scoring_class", "class_counts",
            "class_source_counts", "denominator",
            # MMF008A-A：第二维的词表与计数也要随矩阵落盘，读矩阵的人才能自证
            # "这条主题为什么不在覆盖率里"。
            "responsiveness_version", "allowed_response_dimensions",
            "disposition_by_response_dimension", "fulfillment_required_response_dimensions",
            "response_dimension_counts")},
        # MMF008A2-A：结构图的读数随矩阵落盘。分母为什么是这个数，要能指着评分表的
        # 行列回答；图建不出来时这里写着 NOT_DERIVABLE 与原因，而不是留一个空字段。
        "scoring_graph_readings": scoring_graph_readings(scoring_graph),
        "canonical_scoring_records": canonical_records,
        'global_service_acceptance_plan': global_acceptance_plan,
        "coverage_summary": {
            "scoring_total": len(rows),
            "scoring_planned": sum(row["response_status"] == "PLANNED" for row in rows),
            "scoring_derived_planned": sum(row["response_status"] == "DERIVED_PLANNED" for row in rows),
            "scoring_out_of_scope": sum(row["response_status"] == "OUT_OF_TECHNICAL_SCOPE" for row in rows),
            "scoring_missing": sum(row["response_status"] == "MISSING" for row in rows),
            "must_respond_total": sum(row["must_respond"] for row in rows),
            "must_respond_planned": sum(row["must_respond"] and row["response_status"] in {"PLANNED", "DERIVED_PLANNED"} for row in rows),
            # §25/§33：唯一进分母的是"去重后的技术实质主题"。旧的按行口径与排除项计数
            # 并列报告：换分母必须能在同一张表里看见差了多少行、为什么差。
            "coverage_denominator": COVERAGE_DENOMINATOR_CANONICAL_TOPICS,
            # MMF008A-A：全量十类口径的分母字符串在规划期就并列写下，验收回填时同一个键
            # 换成实测值，读的人不需要等到生成后才看见分母换过。
            "coverage_denominator_all_response_classes": COVERAGE_DENOMINATOR_ALL_CLASSES,
            "canonical_scoring_total": len(canonical_records),
            "canonical_unbound_total": sum(
                1 for record in canonical_records if not record.get("planned_response_cu")),
            "scoring_class_counts": taxonomy["class_counts"],
            "row_coverage_denominator": COVERAGE_DENOMINATOR_LEGACY_ROWS,
            "technical_scoring_total": len(technical_rows),
            "technical_scoring_planned": sum(
                row["response_status"] in {"PLANNED", "DERIVED_PLANNED"} for row in technical_rows),
            "technical_included_total": sum(row.get("scope_class") == "TECHNICAL_INCLUDED" for row in rows),
            "excluded_from_technical_bid_total": len(rows) - len(technical_rows),
            "scope_ungated": not bool(scope_by_scoring),
            # R41 §20：自动映射的落点必须"看得见哪些要人看"。低可信度不静默放行，
            # 也不静默判未映射 —— 两个数都进门禁产物。
            "canonical_mapped_total": mapping["summary"]["mapped_total"],
            "canonical_mapping_review_required_total": mapping["summary"]["review_required_total"],
            "canonical_derived_unit_total": mapping["summary"]["derived_unit_total"],
        },
        "scoring_mapping": {
            "schema_version": SCORING_MAPPING_VERSION,
            "summary": mapping["summary"],
            "aggregate_topic_ids": mapping["aggregate_topic_ids"],
            "derived_units": mapping["derived_units"],
            "mapping_basis_by_topic": {
                str(record.get("canonical_scoring_id") or ""): {
                    "basis": record.get("mapping_basis"),
                    "confidence": record.get("mapping_confidence"),
                    "review_required": bool(record.get("mapping_review_required")),
                    "reason": record.get("mapping_reason"),
                    "alternatives": record.get("mapping_alternatives"),
                } for record in canonical_records
            },
        },
    }
    # §34：生成前评分映射门。规划阶段就没落点、没绑定、没契约的实质主题，
    # 不许带着"以后再说"进正式生成。
    matrix["pre_generation_scoring_mapping_gate"] = pre_generation_scoring_mapping_gate(
        matrix, planned_unit_ids=_planned_unit_ids(hierarchy),
        planned_section_ids=_planned_section_ids(hierarchy),
        planned_unit_sections={str(u['content_unit_id']): str(u['section_id']) for u in (hierarchy or {}).get('content_units') or []})
    return matrix


def _planned_unit_ids(hierarchy: dict[str, Any] | None) -> list[str]:
    """生成计划里真实存在的内容单元清单（映射门 §23 用它核对"落点指得到人"）。"""
    return sorted({str(unit.get("content_unit_id"))
                   for unit in (hierarchy or {}).get("content_units") or []
                   if isinstance(unit, dict) and unit.get("content_unit_id")})


def _planned_section_ids(hierarchy: dict[str, Any] | None) -> list[str]:
    """同一件事的另一半（Final Gate §5.3）：计划里真实存在的**节**。
    门只核对 CU 不核对节，就会放着 `S99-99` 这种查无此节的落点判 PASS。
    """
    return sorted({str(section.get("section_id"))
                   for section in (hierarchy or {}).get("sections") or []
                   if isinstance(section, dict) and section.get("section_id")})


def _units_by_section(units: list[dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for unit in units or []:
        if isinstance(unit, dict):
            index.setdefault(str(unit.get("section_id") or ""), []).append(unit)
    return index


def _scoring_section_shapes(hierarchy: dict[str, Any] | None,
                            bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """评分路由看的"节的语义面"：节标题 + 契约 purpose + must_cover + CU content intent。

    这四个面就是 §16 说的"section semantics / content intent"，顺序即大纲顺序，
    所以同一份计划每次都排出同一张候选表（不引入随机性）。
    """
    contracts = {row.get("section_id"): row
                 for row in ((bundle or {}).get("section_contracts") or {}).get("contracts") or []}
    units = _units_by_section((hierarchy or {}).get("content_units"))
    shapes: list[dict[str, Any]] = []
    for order, section in enumerate((hierarchy or {}).get("sections") or []):
        sid = str(section.get("section_id") or "")
        contract = contracts.get(sid) or {}
        own_units = units.get(sid, [])
        shapes.append({
            "order": order,
            "section_id": sid,
            "section_title": str(section.get("section_title") or ""),
            # 派生单元只能吃这一节的机动额度，所以路由阶段就得看得见还剩多少字。
            "reserve_chars": int((section.get("budget_split") or {}).get("reserve_budget") or 0),
            "faces": {
                "section_title": str(section.get("section_title") or ""),
                "contract_purpose": str(contract.get("purpose") or ""),
                "must_cover": " ".join(str(item) for item in (contract.get("must_cover") or [])),
                "unit_intents": " ".join(
                    f"{unit.get('intent') or unit.get('title') or ''} {unit.get('archetype') or ''}"
                    for unit in own_units),
            },
        })
    return shapes


def query_scoring_matrix(matrix: dict[str, Any], scoring_item_id: str) -> dict[str, Any] | None:
    """查询：这个评分项在哪里响应？写了多少？有哪些证据？"""
    for row in matrix.get("rows") or []:
        if row.get("scoring_item_id") == scoring_item_id:
            return row
    return None


def compute_lineage(pack: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """计算规划谱系指纹：需求、事实、章节契约任一实质变化都会改变指纹。"""
    requirement_fingerprint = _sha256([
        {
            "requirement_id": row.get("requirement_id"),
            "text": row.get("normalized_requirement"),
            "mandatory_level": row.get("mandatory_level"),
            "confirmation_status": row.get("confirmation_status"),
            "scoring_item_id": row.get("scoring_item_id"),
        }
        for row in _requirement_rows(pack)
    ])
    facts = pack.get("project_facts") or {}
    fact_fingerprint = _sha256({key: value.get("value") if isinstance(value, dict) else value for key, value in facts.items()})
    contract_fingerprints = {
        str(row.get("section_id")): _sha256(row)
        for row in (bundle.get("section_contracts") or {}).get("contracts") or []
    }
    return {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "pack_id": pack.get("pack_id"),
        "pack_version": pack.get("pack_version"),
        "requirement_fingerprint": requirement_fingerprint,
        "fact_fingerprint": fact_fingerprint,
        "contract_fingerprints": contract_fingerprints,
        "computed_at": now_iso(),
    }


def plan_invalidation(
    old_lineage: dict[str, Any],
    new_lineage: dict[str, Any],
    completed_sections: list[str],
    failed_sections: list[str] | None = None,
) -> dict[str, Any]:
    """对比新旧谱系，给出恢复时可复用与必须失效的 Section 清单。

    规则：需求或项目事实指纹变化 => 全部失效（旧事实可能已进入正文）；
    指纹一致 => 按 Section 契约指纹判断，契约未变且不在失败清单的已完成
    Section 可复用；失败 Section 一律视为待重做，不进入复用清单。
    """
    failed = set(failed_sections or [])
    completed = [sid for sid in completed_sections or [] if sid not in failed]
    if old_lineage.get("requirement_fingerprint") != new_lineage.get("requirement_fingerprint"):
        return {
            "reusable_sections": [],
            "invalidated_sections": sorted(set(completed)),
            "pending_sections": sorted(failed),
            "invalidation_scope": "ALL_REQUIREMENTS_CHANGED",
            "reason": "需求集或确认状态发生变化，全部已完成片段失效，避免旧事实/旧承诺进入新稿。",
            "evaluated_at": now_iso(),
        }
    if old_lineage.get("fact_fingerprint") != new_lineage.get("fact_fingerprint"):
        return {
            "reusable_sections": [],
            "invalidated_sections": sorted(set(completed)),
            "pending_sections": sorted(failed),
            "invalidation_scope": "ALL_FACTS_CHANGED",
            "reason": "项目事实发生变化，全部已完成片段失效。",
            "evaluated_at": now_iso(),
        }
    old_contracts = old_lineage.get("contract_fingerprints") or {}
    new_contracts = new_lineage.get("contract_fingerprints") or {}
    reusable = [sid for sid in completed if old_contracts.get(sid) == new_contracts.get(sid) and sid in new_contracts]
    invalidated = [sid for sid in completed if sid not in set(reusable)]
    return {
        "reusable_sections": reusable,
        "invalidated_sections": invalidated,
        "pending_sections": sorted(failed),
        "invalidation_scope": "SECTION_CONTRACT_CHANGED" if invalidated else "NONE",
        "reason": "章节契约变化导致局部失效" if invalidated else "谱系一致，已完成片段可复用。",
        "evaluated_at": now_iso(),
    }


def apply_scoring_mapping_units(hierarchy: dict[str, Any], matrix: dict[str, Any],
                                bundle: dict[str, Any]) -> dict[str, Any]:
    """R41 §18：把自动映射派生的 Technical Response 单元**真正并入分层树**（进而进生成计划）。

    额度只从该节自己的机动额度（§10 reserve）里取，一字节都不新造：文档总额度、页带投影
    与五层守恒都不因"补一个落点"而变化 —— 否则 P0-2 就又变回 Round 4 那 395 页的来源。
    机动额度不够时**不造单元**：没有额度的落点是假落点，记成 UNFUNDED 让门控挡住，
    而不是先写进计划再让生成链去猜这一节的字从哪来。
    """
    derived = [row for row in ((matrix.get("scoring_mapping") or {}).get("derived_units") or [])
               if isinstance(row, dict)]
    if not derived:
        from .scoring_response_points import attach_response_points
        attach_response_points(matrix, hierarchy)
        report = {"applied_total": 0, "unfunded_total": 0, "applied_unit_ids": [],
                  "unfunded": [], "budget_basis": "SECTION_RESERVE_ONLY"}
        matrix.setdefault("scoring_mapping", {})["application"] = report
        return report

    units = hierarchy.setdefault("content_units", [])
    by_section = _units_by_section(units)
    section_rows = {str(row.get("section_id")): row for row in hierarchy.get("sections") or []}
    contracts = {str(row.get("section_id")): row
                 for row in (bundle.get("section_contracts") or {}).get("contracts") or []}
    records = {str(row.get("canonical_scoring_id") or ""): row
               for row in (matrix.get("canonical_scoring_records") or []) if isinstance(row, dict)}
    committed: dict[str, int] = {}
    applied: list[str] = []
    unfunded: list[dict[str, Any]] = []
    touched: set[str] = set()
    for spec in derived:
        section_id = str(spec.get("section_id") or "")
        section_row = section_rows.get(section_id) or {}
        reserve = int((section_row.get("budget_split") or {}).get("reserve_budget") or 0) \
            - committed.get(section_id, 0)
        chars = min(reserve, int(spec.get("unified_budget_chars") or 0))
        if chars < MIN_DERIVED_UNIT_CHARS:
            # 映射端按同一口径算过额度，走到这里说明中间有东西改过该节预算。
            # 退回该节既有单元，绝不留下一个指向不存在单元的落点。
            fallback = [str(cu) for cu in (spec.get("fallback_cu_ids") or [])]
            record = records.get(str(spec.get("canonical_scoring_id") or ""))
            unfunded.append({
                "canonical_scoring_id": spec.get("canonical_scoring_id"),
                "section_id": section_id,
                "reserve_budget": max(0, reserve),
                "reassigned_to": fallback,
                "reason": "UNFUNDED_DERIVED_UNIT：该节机动额度不足以承载一个内容单元，"
                          "不造没有额度的落点。",
            })
            if record is not None and fallback:
                record["planned_response_cu"] = fallback
                record["mapping_basis"] = "TECHNICAL_TOPIC_KIND_ROUTE/HOST_SECTION_HAS_NO_RESERVE"
                record["cu_binding"] = {"basis": record["mapping_basis"],
                                        **(record.get("cu_binding") or {})}
                record["mapping_review_required"] = True
            continue
        committed[section_id] = committed.get(section_id, 0) + chars
        archetype = str(spec.get("archetype") or "NARRATIVE")
        budget = unified_unit_budget(archetype, chars)
        siblings = by_section.get(section_id) or []
        unit = {
            **spec,
            "chapter_id": section_row.get("chapter_id"),
            "planned_words": int(budget["effective_chars"]),
            "density_profile": str((siblings[0].get("density_profile") if siblings else "") or ""),
            "budget_measure": "unified_effective_chars",
            "unified_budget": budget,
            "required_outputs": [],
            "fact_dependencies": [],
            "evidence_dependencies": [],
            "min_budget": contract_budget_row(
                archetype, unified_unit_budget(archetype, int(round(budget["effective_chars"] * 0.8)) + 1)),
            "target_budget": contract_budget_row(archetype, budget),
            "max_budget": contract_budget_row(
                archetype, unified_unit_budget(archetype, int(round(budget["effective_chars"] * 1.2)) + 1)),
            "generation_state": {"attempt_count": 0, "fragment_path": None,
                                 "qa_status": None, "updated_at": None},
        }
        units.append(unit)
        siblings.append(unit)
        applied.append(str(unit["content_unit_id"]))
        touched.add(section_id)

    for section_id in touched:
        section_row = section_rows.get(section_id) or {}
        own = by_section.get(section_id) or []
        section_row["budget"] = {
            "min": sum(int((unit.get("min_budget") or {}).get("effective_chars") or 0) for unit in own),
            "target": sum(int((unit.get("target_budget") or {}).get("effective_chars") or 0) for unit in own),
            "max": sum(int((unit.get("max_budget") or {}).get("effective_chars") or 0) for unit in own),
            "measure": "unified_effective_chars",
        }
        planned_total = _contract_target_words(contracts.get(section_id) or {})
        section_row["budget_split"] = section_budget_split(
            planned_total or section_row["budget"]["target"],
            [(unit.get("unified_budget") or {}) for unit in own])

    counts = hierarchy.get("counts") or {}
    counts["content_units"] = len(units)
    counts["planned_words"] = sum(int(unit.get("planned_words") or 0) for unit in units)
    counts["content_units_by_archetype"] = {
        archetype: sum(unit.get("archetype") == archetype for unit in units)
        for archetype in CONTENT_ARCHETYPES
    }
    # 五层预算与门控按**并入之后**的树重算：派生单元吃的是该节机动额度，
    # 所以 allocated 涨、reserve 降，Document 层总额不变（守恒仍逐项核对）。
    view = build_unified_budget_view(bundle, hierarchy.get("sections") or [], by_section)
    hierarchy["budget_tree"] = view["tree"]
    hierarchy["pre_generation_budget_gate"] = view["gate"]
    hierarchy["validation"] = validate_hierarchy(hierarchy)
    report = {"applied_total": len(applied), "unfunded_total": len(unfunded),
              "applied_unit_ids": applied, "unfunded": unfunded,
              "budget_basis": "SECTION_RESERVE_ONLY"}
    matrix.setdefault("scoring_mapping", {})["application"] = report
    from .scoring_response_points import attach_response_points
    attach_response_points(matrix, hierarchy)
    return report


def attach_hierarchy_artifacts(run_dir: Path, bundle: dict[str, Any]) -> dict[str, Path] | None:
    """在规划产物目录写入 08/09 与谱系文件。缺少 Requirement Pack 时跳过。

    供 write_plan_artifacts 以 try/except 方式调用：任何失败都不影响既有
    规划链路（最小变更原则）。
    """
    pack = None
    for candidate in (run_dir / "tender" / "requirement_pack.json", run_dir / "requirement_pack.json"):
        if candidate.is_file():
            pack = json.loads(candidate.read_text(encoding="utf-8-sig"))
            break
    if not isinstance(pack, dict):
        return None
    hierarchy = build_document_hierarchy(pack, bundle)
    matrix = build_scoring_matrix(pack, bundle, hierarchy)
    # R41 §18：派生落点必须真的进计划。先并入分层树（08 是生成计划的唯一上游），
    # 再用**并入之后**的单元清单重跑一次映射门 —— 同一个函数、同一把尺子，
    # 只是这一次它手上有了"计划里到底有哪些单元"的证据（§23）。
    apply_scoring_mapping_units(hierarchy, matrix, bundle)
    matrix["pre_generation_scoring_mapping_gate"] = pre_generation_scoring_mapping_gate(
        matrix, planned_unit_ids=_planned_unit_ids(hierarchy),
        planned_section_ids=_planned_section_ids(hierarchy),
        planned_unit_sections={str(u['content_unit_id']): str(u['section_id']) for u in hierarchy.get('content_units') or []})
    lineage = compute_lineage(pack, bundle)
    written = {
        "hierarchy": run_dir / "08_document_hierarchy.json",
        "scoring_matrix": run_dir / "09_scoring_matrix.json",
        "lineage": run_dir / "10_hierarchy_lineage.json",
    }
    for path, value in (
        (written["hierarchy"], hierarchy),
        (written["scoring_matrix"], matrix),
        (written["lineage"], lineage),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # §13：生成前预算门单独落一份。规划阶段就要能看见"这个计划能不能进生成"，
    # 而不是等到生成链里第一次 Provider 调用之前才发现。
    gate = hierarchy.get("pre_generation_budget_gate") or {}
    if gate:
        gate_path = run_dir / BUDGET_GATE_FILE
        gate_path.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written["budget_gate"] = gate_path
    # §34：评分映射门与预算门同排。两条门都在第一次 Provider 调用之前，
    # 一个管"这个计划写多少页"，一个管"这个计划有没有把评分项落到具体章节与单元"。
    mapping_gate = matrix.get("pre_generation_scoring_mapping_gate") or {}
    if mapping_gate:
        mapping_path = run_dir / SCORING_MAPPING_GATE_FILE
        mapping_path.write_text(json.dumps(mapping_gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written["scoring_mapping_gate"] = mapping_path
    return written


def update_scoring_fulfillment(
    matrix_path: Path,
    fragments: dict[str, dict[str, Any]],
    contracts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成后回填 09_scoring_matrix.fulfillment（TECHNICAL_SCORING_ENTITY_EVIDENCE）。

    §24/§25：只有能在最终 Word 里定位到响应 Section、响应 Content Unit 与逐字证据、
    且评分要素达到实体证据合同条数的条目才判 FULFILLED；有字数但要素定位不到的
    一律 NEARBY_ONLY，不得计入覆盖。
    §33/§35：行级判定之上再合成 canonical 主题状态，门控覆盖率的分母是
    "去重后的唯一技术实质主题"，不是评分表行数；按行口径并列保留。
    §6/§7：范围排除条目记 EXCLUDED_FROM_TECHNICAL_BID，一律不进分母。
    空评分矩阵 → NOT_APPLICABLE（不得自称 PASS/VERIFIED）。
    """
    matrix = json.loads(Path(matrix_path).read_text(encoding="utf-8-sig"))
    rows = matrix.get("rows") or []
    if not rows:
        matrix["fulfillment_status"] = "NOT_APPLICABLE"
        matrix["coverage_summary"] = {**(matrix.get("coverage_summary") or {}), "note": "无评分项，Scoring Fulfillment 不适用"}
        Path(matrix_path).write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        return {"updated_rows": 0, "status": "NOT_APPLICABLE"}

    unit_index, structured_by_section, table_count_by_section = artifact_evidence_index(fragments)
    from .scoring_global_acceptance import judge_global_service_plan
    global_plan = matrix.get('global_service_acceptance_plan') or {}
    # Migrate persisted leaf-only plans using their exact original table identity.
    # Restore the recorded route and entity contract; never invent a new route.
    for record in matrix.get("canonical_scoring_records") or []:
        leaf_id = record.get("scoring_leaf_id")
        if not leaf_id:
            continue
        matched = [row for row in rows if row.get("scoring_leaf_id") == leaf_id
                   and str(row.get("scoring_item_id") or "").startswith("LEAF-")]
        if not matched:
            continue
        record.setdefault("evaluation_row_ids", [row["scoring_item_id"] for row in matched])
        for row in matched:
            row["canonical_scoring_id"] = record.get("canonical_scoring_id")
            if record.get("evidence_contract"):
                row["entity_contract"] = record["evidence_contract"]
            if not row.get("section_ids") and record.get("planned_response_sections"):
                row["section_ids"] = list(record["planned_response_sections"])
                row["primary_section_id"] = record.get("planned_response_section")
                row["planned_response_cu_bound"] = list(record.get("planned_response_cu") or [])
                row["mapping_source"] = "PERSISTED_SOURCE_TABLE_LEAF_PLAN"
    for row in rows:
        verdict = (judge_global_service_plan(global_plan, unit_index)
                   if row.get('responsiveness_class') == 'WHOLE_DOCUMENT_RUBRIC'
                   else judge_scoring_row(row, unit_index, structured_by_section, table_count_by_section))
        verdict["last_validated_at"] = now_iso()
        verdict["planned_response_cu"] = list(row.get("planned_response_cu") or [])
        verdict["artifact_sections"] = [sid for sid in (row.get("section_ids") or []) if sid in fragments]
        row["fulfillment"] = verdict

    technical_rows = [row for row in rows if (row.get("scope_disposition") or "") != "EXCLUDED"]
    included_rows = [row for row in technical_rows if row.get("scope_class") == "TECHNICAL_INCLUDED"]
    technical_statuses = [str((row.get("fulfillment") or {}).get("status") or "") for row in technical_rows]
    all_statuses = [str((row.get("fulfillment") or {}).get("status") or "") for row in rows]
    missing_kinds = [
        str((row.get("fulfillment") or {}).get("missing_kind") or "")
        for row in technical_rows
        if (row.get("fulfillment") or {}).get("status") == "MISSING"
    ]
    planned_rows = [row for row in technical_rows if row.get("section_ids")]
    fulfilled = sum(status == "FULFILLED" for status in technical_statuses)
    row_coverage = round(fulfilled / len(technical_rows), 4) if technical_rows else None

    # §33/§35：验收在 canonical 主题层做。规划期生成的 canonical 记录是"计划的那份"，
    # 生成后只往里回填实测，不在验收时另起一套分类。
    by_scoring_id = {str(row.get("scoring_item_id") or ""): row for row in rows if row.get("scoring_item_id")}
    records = [record for record in (matrix.get("canonical_scoring_records") or []) if isinstance(record, dict)]
    denominator_basis = "PLAN_OF_RECORD"
    if not records:
        records = build_scoring_taxonomy(rows)["canonical_scoring_records"]
        denominator_basis = "REBUILT_FROM_ROWS_NO_PLAN_TREE"
    for record in records:
        evaluation_ids = record.get("evaluation_row_ids") or record.get("source_ids") or []
        # A derived table leaf is a real evaluation row, while source_ids keeps
        # its original meaning (provider-extracted IDs). Recover old plans only
        # by an exact persisted table-leaf identity, never fuzzy topic matching.
        if not evaluation_ids and record.get("scoring_leaf_id"):
            evaluation_ids = [str(row["scoring_item_id"]) for row in rows
                              if row.get("scoring_item_id")
                              and row.get("scoring_leaf_id") == record["scoring_leaf_id"]]
            if evaluation_ids:
                record["evaluation_row_ids"] = evaluation_ids
                record["evaluation_row_binding_basis"] = "EXACT_SOURCE_TABLE_LEAF_ID"
        source_rows = [by_scoring_id[str(sid)] for sid in evaluation_ids if str(sid) in by_scoring_id]
        verdict = canonical_status(source_rows)
        if not source_rows and record_requires_technical_acceptance(record):
            verdict.update(status="MISSING", missing_reason="EVALUATION_ROW_BINDING_MISSING")
        verdict['missing_dimensions'] = sorted({str(m) for row in source_rows
                                                for m in (row.get('fulfillment') or {}).get('missing_dimensions') or []})
        verdict['selected_evidence'] = [e for row in source_rows for e in (row.get('fulfillment') or {}).get('selected_evidence') or []]
        verdict['evidence_candidates'] = [e for row in source_rows for e in (row.get('fulfillment') or {}).get('evidence_candidates') or []]
        verdict['rejected_evidence'] = [e for row in source_rows for e in (row.get('fulfillment') or {}).get('rejected_evidence') or []]
        planned = [str(cu) for cu in (record.get("planned_response_cu") or [])]
        found = list(verdict["response_cu"])
        # 计划与实际的交集才是"这条主题真被这份产物回答了"；证据落在计划之外照样记录，
        # 不假装没看见 —— 那是路由问题，不是可以抹掉的误差。
        verdict.update({
            "planned_response_section": record.get("planned_response_section"),
            "planned_response_sections": record.get("planned_response_sections") or [],
            "planned_response_cu": planned,
            "cu_binding_basis": (record.get("cu_binding") or {}).get("basis"),
            "evidence_in_planned_cu": sorted(set(planned) & set(found)),
            "evidence_outside_plan": sorted(set(found) - set(planned)),
            "evidence_contract": record.get("evidence_contract") or {},
            "weight_if_known": record.get("weight_if_known"),
            "last_validated_at": now_iso(),
        })
        record["fulfillment"] = verdict
    matrix["canonical_scoring_records"] = records
    canonical_statuses = [str(record["fulfillment"]["status"]) for record in records]
    canonical_fulfilled = sum(status == "FULFILLED" for status in canonical_statuses)
    # MMF008A-A：门控用的覆盖率只算第二维判为"正文可响应"的主题（可生成正文 / 匿名组织设计）。
    # 三条并列读数一起落盘，少一条都会让"分母为什么变小"看不出来：
    #   - `coverage`：门控值，respondable 口径；
    #   - `coverage_all_classes`：十类第一维全量主题口径（Run F 的 2/11=0.1818 就是这一条）；
    #   - `by_response_dimension`：逐类别的 fulfilled/total，谁离开分母、离开几条，逐条可查。
    respondable = [record for record in records if record_requires_technical_acceptance(record)]
    respondable_statuses = [str(record["fulfillment"]["status"]) for record in respondable]
    respondable_fulfilled = sum(status == "FULFILLED" for status in respondable_statuses)
    coverage_all_classes = (round(canonical_fulfilled / len(records), 4) if records else None)
    if records and not respondable:
        # 有主题却没有一条被判成正文可响应：这不是"没有东西要覆盖"，这是分类出了问题。
        # 判 0.0 并挂具名旗标，绝不判 None/NOT_APPLICABLE —— 后者会被读成"不适用所以干净"。
        coverage, no_respondable_topic = 0.0, True
    else:
        coverage = round(respondable_fulfilled / len(respondable), 4) if respondable else None
        no_respondable_topic = False
    dimension_readings: dict[str, dict[str, Any]] = {}
    for name in RESPONSE_DIMENSIONS:
        members = [record for record in records
                   if str(record.get("responsiveness_class") or "") == name]
        dimension_readings[name] = {
            "total": len(members),
            "fulfilled": sum(1 for item in members
                             if str(item["fulfillment"]["status"]) == "FULFILLED"),
            "counts_toward_gate": bool(members and record_requires_technical_acceptance(members[0])),
            "canonical_scoring_ids": [str(item.get("canonical_scoring_id") or "") for item in members],
        }

    summary = matrix.get("coverage_summary") or {}
    summary.update({
        "fulfilled": fulfilled,
        "partial": sum(status == "PARTIAL" for status in technical_statuses),
        "nearby_only": sum(status == "NEARBY_ONLY" for status in technical_statuses),
        "fulfillment_missing": len(missing_kinds),
        # 分解 MISSING：路由/预算不足还是产物空洞，指向不同的修复口，不得混成一个数。
        "fulfillment_missing_not_planned": sum(k == "NOT_PLANNED" for k in missing_kinds),
        "fulfillment_missing_empty_artifact": sum(k == "EMPTY_ARTIFACT" for k in missing_kinds),
        "technical_planned_total": len(planned_rows),
        "excluded_from_technical_bid": sum(status == "EXCLUDED_FROM_TECHNICAL_BID" for status in all_statuses),
        "technical_scoring_total": len(technical_rows),
        # §25/§33：门控用的是主题口径覆盖率；按行口径原名并列保留，换分母不藏数字。
        "technical_scoring_coverage": coverage,
        "coverage_denominator": COVERAGE_DENOMINATOR_CANONICAL_TOPICS,
        "canonical_scoring_total": len(records),
        "canonical_denominator_basis": denominator_basis,
        "canonical_fulfilled": canonical_fulfilled,
        # MMF008A-A 第二维的三处并列读数：谁进账、谁不进账、不进账的归谁判。
        "respondable_scoring_total": len(respondable),
        "respondable_fulfilled": respondable_fulfilled,
        "not_respondable_scoring_total": len(records) - len(respondable),
        "technical_scoring_coverage_all_response_classes": coverage_all_classes,
        "coverage_denominator_all_response_classes": COVERAGE_DENOMINATOR_ALL_CLASSES,
        "coverage_by_response_dimension": dimension_readings,
        "responsiveness_contract_version": RESPONSIVENESS_VERSION,
        "canonical_partial": sum(status == "PARTIAL" for status in canonical_statuses),
        "canonical_nearby_only": sum(status == "NEARBY_ONLY" for status in canonical_statuses),
        "canonical_missing": sum(status == "MISSING" for status in canonical_statuses),
        "canonical_not_applicable": sum(status == "NOT_APPLICABLE" for status in canonical_statuses),
        "canonical_unbound_total": sum(1 for record in records if not record["fulfillment"]["planned_response_cu"]),
        "canonical_deduped_source_rows": sum(len(record.get("source_ids") or []) for record in records),
        "row_coverage_denominator": COVERAGE_DENOMINATOR_LEGACY_ROWS,
        "technical_scoring_row_coverage": row_coverage,
        # 分母口径并列报告：TECHNICAL_INCLUDED 是门控要求建正文的条目，REFERENCE 只做参考。
        # 门控只看 technical_scoring_coverage，这里并列是为了暴露"未规划"集中在哪一类。
        "technical_included_total": len(included_rows),
        "technical_included_coverage": (
            round(sum((row.get("fulfillment") or {}).get("status") == "FULFILLED" for row in included_rows)
                  / len(included_rows), 4) if included_rows else None),
    })
    named_focus = {
        rubric_id: [
            {
                "scoring_item_id": row.get("scoring_item_id"),
                "label": re.sub(r"\s+", "", str(row.get("label") or ""))[:60],
                "status": (row.get("fulfillment") or {}).get("status"),
                "concepts_missing": (row.get("fulfillment") or {}).get("concepts_missing") or [],
                "response_section": (row.get("fulfillment") or {}).get("response_section"),
                "response_cu": (row.get("fulfillment") or {}).get("response_cu") or [],
            }
            for row in rows
            if (row.get("fulfillment") or {}).get("rubric_id") == rubric_id
        ]
        for rubric_id in NAMED_FOCUS_RUBRICS
    }
    matrix["coverage_summary"] = summary
    matrix["fulfillment_contract"] = EVIDENCE_CONTRACT_VERSION
    matrix["scoring_gate"] = {
        "min_technical_scoring_coverage": TECHNICAL_SCORING_GATE_MIN,
        "technical_scoring_coverage": coverage,
        "coverage_denominator": COVERAGE_DENOMINATOR_CANONICAL_TOPICS,
        "canonical_scoring_total": len(records),
        "canonical_denominator_basis": denominator_basis,
        "legacy_row_coverage": row_coverage,
        # §27/§35：唯一实质主题口径的实体覆盖率不足 80% 不得 COMPLETED_CLEAN。
        "completed_clean_allowed": coverage is not None and coverage >= TECHNICAL_SCORING_GATE_MIN,
        "measured": coverage is not None,
        # §26/§37：两条不许被"想通过"抹掉的规则，写进门禁产物本身。
        "nearby_only_counts_as_fulfilled": False,
        "commercial_content_for_coverage_forbidden": True,
        # MMF008A-A：分母口径改到第二维之后的三根保险，全部写进产物，不改阈值。
        "respondable_scoring_total": len(respondable),
        "all_response_classes_coverage": coverage_all_classes,
        "coverage_by_response_dimension": dimension_readings,
        "no_respondable_technical_topic": no_respondable_topic,
        'no_body_response_topic': bool(records) and not any(record_requires_body_response(r) for r in records),
        "proof_material_may_not_be_required_in_technical_body": True,
        "identity_material_never_required_in_technical_body": True,
    }
    matrix["named_focus_coverage"] = named_focus
    matrix["fulfillment_updated_at"] = now_iso()
    Path(matrix_path).write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    return {
        "updated_rows": len(rows),
        "coverage_summary": summary,
        "technical_scoring_coverage": coverage,
        "canonical_scoring_total": len(records),
        "canonical_denominator_basis": denominator_basis,
        "completed_clean_allowed": matrix["scoring_gate"]["completed_clean_allowed"],
        "named_focus_coverage": named_focus,
    }


def flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(flatten_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(flatten_text(v) for v in value)
    return str(value) if isinstance(value, str) else ""
