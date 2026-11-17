"""P0-2（Round 4.1 §12–§23）：把每一条唯一技术实质评分主题**自动映射**到真实落点。

R4-P0-B 已经建好十类分类、唯一主题去重与生成前门。但门只管"缺落点就挡住"，
不解决"落点从哪来"：Run D 只读回放实测 41 行 → 8 个唯一技术实质主题，其中 **4 个无落点**
（SCR-009 总纲行、项目服务方案、服务保障能力、人员保障方案），链路停在
`BLOCKED_SCORING_MAPPING_INCOMPLETE`。本轮补上 §16 那条链里缺的那一段：

    Scoring Extraction → Classification → Canonical Technical Topic
      → Requirement Link → **Planned Section → Planned CU**
      → Evidence Contract → Generation Plan

为什么不是"把相似度阈值调一调"（R41 证据目录里三次量具探针的读数）：
1. 整棵分层树上做 bigram Jaccard，最高只有 0.06 —— 阈值永远不会点火；
2. 只数"关键词命中个数"会把 `S01-02 项目特征与物业管理重难点` 顶成万能落点，
   连"项目应急预案"都会输给 S01-02 而不是 S10-01；
3. 只按要素命中打分时，`项目服务方案`（一条**方案级**主题）会输给
   `S07-01 环境服务范围、标准与作业组织` —— 专业域的节标题天然比总述节更具体，
   纯要素命中永远选不到总述节。

所以路由按 §16 的四类依据分优先级，每条例据都写进记录供人工回查：

    EXPLICIT_SCORING_ITEM_IDS > REQUIREMENT_REF_LINK > TOPIC_MATCH_IN_PLANNED_SECTION
      > TECHNICAL_TOPIC_KIND_ROUTE > AGGREGATE_COVERAGE_OF_MEMBER_TOPICS
      > DERIVED_TECHNICAL_RESPONSE_CU

三条不写死的规矩（§17/§20）：
- **种类数、要素数、主题条数都不参与判定**：七个技术主题种类是词表，不是计数目标；
  合成夹具给 4/8/12 条主题走的是同一段代码。
- **可信度不拍脑袋**：`mapping_confidence` 一律 = "合同要求的最低实质要素里，落点已能举证的比例"；
  `review_required` 一律 = "这个比例低于合同自己的 min_concepts 比例"。判据取自合同，
  本模块不设第二个阈值。
- **兜底不是放行**：既有节举证不足时按 §18 派生一个 Technical Response 内容单元，
  由 `hierarchy.apply_scoring_mapping_units` 真实并入 08 的 content_units（进而进生成计划），
  且 `review_required=True` —— 派生出来的家必须有人看过。绝不建商务/报价/资质/资信类单元。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .scoring_evidence import SUBSTANTIVE_UNIT_CHARS, SCORING_ENTITY_RUBRICS
from .scoring_responsiveness import (
    CONTRACTUAL_CONTEXT,
    CROSS_FILE_PROTECTED_EVIDENCE,
    WHOLE_DOCUMENT_RUBRIC,
    record_requires_body_response,
)

SCORING_MAPPING_VERSION = "technical-scoring-mapping-v0.1"

# 第二维判为"不由技术正文回答"的三类走这个 basis：不派生、不路由，但记录照旧落盘。
NON_RESPONDABLE_MAPPING_BASE = "NO_TECHNICAL_BODY_RESPONSE_REQUIRED"

# 派生一个 Technical Response 单元的最低额度：门槛就是实体证据门槛本身 ——
# 低于它的单元连一条评分要素都承载不了，建出来就是一个空标题。
MIN_DERIVED_UNIT_CHARS = SUBSTANTIVE_UNIT_CHARS
# 派生单元的目标额度与规划切单元用的是同一把尺子；一条节被多个主题派生时，
# 后面的主题只能拿剩下的机动额度，用完就退到该节既有单元（见 _decide）。
DERIVED_UNIT_TARGET_CHARS = 1000

# §19 点名的七个技术主题种类。detect 只用来**认主题**（打在原始标签上，见 _topic_text）。
TECHNICAL_TOPIC_KINDS: tuple[str, ...] = (
    "MANAGEMENT_TEAM",
    "SERVICE_PLAN",
    "SERVICE_GUARANTEE",
    "STAFF_GUARANTEE",
    "EXIT_HANDOVER",
    "EMERGENCY_RESPONSE",
    "PROJECT_STRENGTH_OR_SUGGESTION",
)

KIND_DETECT_TERMS: dict[str, tuple[str, ...]] = {
    "MANAGEMENT_TEAM": ("管理团队", "人员构成", "人员素质", "人员结构", "相关经验"),
    "SERVICE_PLAN": ("服务方案", "总体方案", "服务设想", "服务构想", "需求覆盖", "结构清晰"),
    "SERVICE_GUARANTEE": ("服务保障", "保障体系", "保障能力", "措施全面"),
    "STAFF_GUARANTEE": ("人员保障", "岗位保障", "培训计划"),
    "EXIT_HANDOVER": ("退出", "移交", "退场"),
    "EMERGENCY_RESPONSE": ("应急", "突发", "预案"),
    "PROJECT_STRENGTH_OR_SUGGESTION": ("优势", "管理建议", "亮点"),
}

# 没有命名 rubric 的三类，要素在这里定义；形状与 rubric 的 concepts 完全一致（要素 → 同义词）。
FALLBACK_TOPIC_ELEMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "MANAGEMENT_TEAM": {
        "人员构成": ("人员构成", "管理团队", "班子", "梯队", "人员结构"),
        "岗位职责": ("岗位职责", "职责", "岗位设置", "组织架构"),
        "任职资格": ("资格", "素质", "经验", "证书", "任职"),
        "排班到岗": ("排班", "到岗", "配置", "服务时间", "在位"),
        "考核培训": ("考核", "培训", "督导", "奖惩"),
    },
    "SERVICE_PLAN": {
        "需求覆盖": ("需求", "服务范围", "物业需求", "覆盖"),
        "服务内容": ("服务内容", "服务方案", "服务定位", "服务标准"),
        "服务标准": ("标准", "指标", "承诺"),
        "作业流程": ("流程", "作业", "机制", "制度", "规程"),
        "成果交付": ("成果", "报告", "台账", "记录", "交付"),
    },
    "STAFF_GUARANTEE": {
        "岗位设置": ("岗位", "人员配置", "定岗"),
        "人员储备": ("储备", "备用", "顶岗", "补位", "人力"),
        "培训计划": ("培训", "教学", "演练", "持证"),
        "考勤考核": ("考勤", "考核", "奖惩", "监督"),
        "稳定性保障": ("稳定", "流失", "替班", "应急人员"),
    },
}

# 主题种类与命名 rubric 同名的那几个不必列；这里只列"名字不同"的映射。
KIND_RUBRIC_ID: dict[str, str] = {
    "SERVICE_GUARANTEE": "SERVICE_GUARANTEE_SYSTEM",
    "EMERGENCY_RESPONSE": "EMERGENCY_DEPLOYMENT_RESPONSE",
    "PROJECT_STRENGTH_OR_SUGGESTION": "PROJECT_MANAGEMENT_SUGGESTION",
}

# 派生 Technical Response 单元时的 archetype 取向（只用既有 archetype，不新增类别）。
KIND_RESPONSE_ARCHETYPE: dict[str, str] = {
    "EXIT_HANDOVER": "SOP",
    "EMERGENCY_RESPONSE": "EMERGENCY_PLAYBOOK",
    "SERVICE_GUARANTEE": "PROCESS",
    "PROJECT_STRENGTH_OR_SUGGESTION": "NARRATIVE",
    "MANAGEMENT_TEAM": "RESPONSIBILITY_MATRIX",
    "STAFF_GUARANTEE": "CHECKLIST",
    "SERVICE_PLAN": "NARRATIVE",
}

# 一条评分项同时列举 ≥3 类主题时，它是"覆盖清单"而不是一篇新正文（Run D SCR-009）。
AGGREGATE_KIND_MINIMUM = 3

# 同级窗口：差一项要素以内算"同样举证得动"，这时按大纲顺序取更靠前的那一节。
# 依据不是词表，而是技术标本身的写法 —— 总体性响应写在总述章，专业域节是它的展开；
# 量具第 3 轮的读数显示：只按要素命中数排，`项目服务方案` 会输给
# `环境服务范围、标准与作业组织`（标题里正好摆着"服务范围/服务标准/作业"）。
ELEMENT_TIE_WINDOW = 1

# 语义面权重：命中的面越具体证据越强 —— "提过"不等于"是它的落点"。
SECTION_FACE_WEIGHT: dict[str, int] = {
    "section_title": 4,
    "contract_purpose": 3,
    "must_cover": 2,
    "unit_intents": 1,
}

LABEL_TOKEN_KIND = "LABEL_TOKENS"


def topic_kinds() -> tuple[str, ...]:
    return TECHNICAL_TOPIC_KINDS


def topic_elements(kind: str) -> dict[str, tuple[str, ...]]:
    """一个主题种类的最低实质要求要素。

    命名 rubric 在的地方它就是唯一真值：`evidence_contract` 用的也是同一批词。
    路由找的词与验收找的词必须同源，否则又是一处"两处两把尺子"。
    """
    name = str(kind or "")
    rubric_id = KIND_RUBRIC_ID.get(name, name)
    for rubric in SCORING_ENTITY_RUBRICS:
        if str(rubric.get("rubric_id")) == rubric_id:
            return {key: tuple(value) for key, value in (rubric.get("concepts") or {}).items()}
    return {key: tuple(value) for key, value in (FALLBACK_TOPIC_ELEMENTS.get(name) or {}).items()}


def _topic_text(record: dict[str, Any]) -> str:
    """种类识别必须打在**原始标签**上。

    Run D 的 SCR-041 "技术部分-项目管理（针对本项目描述物业管理优势2分并提出管理建议2分）"
    去掉分值噪声后 topic 只剩"技术部分-项目管理" —— "优势/管理建议"整段被当成打分档次剥掉了。
    只看 topic 会让"项目优势或管理建议"这一类主题永远认不出来。
    """
    return " ".join(str(record.get(key) or "") for key in ("label", "topic"))


def detect_topic_kinds(record: dict[str, Any]) -> list[str]:
    text = _topic_text(record)
    return [kind for kind in TECHNICAL_TOPIC_KINDS
            if any(term in text for term in KIND_DETECT_TERMS[kind])]


def is_aggregate_topic(record: dict[str, Any]) -> bool:
    return len(detect_topic_kinds(record)) >= AGGREGATE_KIND_MINIMUM


def elements_for(record: dict[str, Any], kinds: list[str] | None = None) -> dict[str, tuple[str, ...]]:
    """一条主题的最低实质要求要素表。认不出种类时用合同自己的标签词组。

    要素表按名字合并（同名要素的同义词并起来），所以总纲行列举多类时要素是并集，
    不会因为一条主题归了两类就把要素数乘一遍。
    """
    names = list(kinds if kinds is not None else detect_topic_kinds(record))
    contract = record.get('evidence_contract') or {}
    if contract.get('rubric_id') in {'MANAGEMENT_TEAM', 'MANAGEMENT_ADVANTAGE'}:
        return {name: tuple(contract.get('concept_synonyms', {}).get(name) or [name])
                for name in contract.get('concepts') or []}
    merged: dict[str, list[str]] = {}
    for kind in names:
        for name, terms in topic_elements(kind).items():
            bucket = merged.setdefault(name, [])
            bucket.extend(term for term in terms if term not in bucket)
    if not merged:
        # 合同的 `concepts` 是**要素名列表**，同义词在 `concept_synonyms` 里（见
        # `scoring_taxonomy.evidence_contract_for`）。按 dict 直接取会在这里炸。
        contract = (record.get("evidence_contract") or {})
        synonyms = contract.get("concept_synonyms") or {}
        for name in contract.get("concepts") or []:
            key = str(name)
            bucket = merged.setdefault(key, [])
            for term in [key] + [str(value) for value in (synonyms.get(key) or [])]:
                if term and term not in bucket:
                    bucket.append(term)
    return {name: tuple(terms) for name, terms in merged.items()}


def _best_face(terms: tuple[str, ...], faces: dict[str, str]) -> tuple[int, str]:
    weight, where = 0, ""
    for face_name, text in faces.items():
        if not text or weight >= SECTION_FACE_WEIGHT.get(face_name, 0):
            continue
        if any(term and term in text for term in terms):
            weight, where = SECTION_FACE_WEIGHT[face_name], face_name
    return weight, where


def score_section_fits(elements: dict[str, tuple[str, ...]], faces: dict[str, str]) -> dict[str, Any]:
    detail: dict[str, dict[str, Any]] = {}
    for name, terms in elements.items():
        weight, where = _best_face(terms, faces)
        detail[name] = {"weight": weight, "face": where}
    covered = [name for name, row in detail.items() if row["weight"]]
    return {"elements": detail, "covered_elements": covered, "element_total": len(elements),
            "face_strength": sum(int(row["weight"]) for row in detail.values())}


def _unit_element_hits(unit: dict[str, Any], elements: dict[str, tuple[str, ...]]) -> list[str]:
    text = " ".join(str(unit.get(key) or "") for key in ("intent", "title", "purpose", "archetype"))
    return sorted(name for name, terms in elements.items()
                  if any(term and term in text for term in terms))


def rank_sections(elements: dict[str, tuple[str, ...]], sections: list[dict[str, Any]],
                  *, tie_window: int = ELEMENT_TIE_WINDOW) -> list[dict[str, Any]]:
    """给一条主题排出候选节。

    先按"能举证几条要素"排，再把差距在 `tie_window` 以内的归为同级，同级里按大纲顺序取
    更靠前的一节 —— 大纲顺序是计划里已有的、可复现的事实，不引入随机性，也不需要一份
    "哪些词算总述"的词表。面权重只用来在同级同序时区分证据强度。
    """
    ranked: list[dict[str, Any]] = []
    for shape in sections or []:
        faces = shape.get("faces") or {}
        fit = score_section_fits(elements, faces)
        covered = len(fit["covered_elements"])
        ranked.append({
            "section_id": str(shape.get("section_id") or ""),
            "section_title": str(shape.get("section_title") or "") or None,
            "covered_elements": list(fit["covered_elements"]),
            "element_total": fit["element_total"],
            "coverage_ratio": (round(covered / fit["element_total"], 4) if fit["element_total"] else 0.0),
            "face_strength": fit["face_strength"],
            "reserve_chars": int(shape.get("reserve_chars") or 0),
            "order": int(shape.get("order") or 0),
            "faces": faces,
        })
    if not ranked:
        return []
    ranked.sort(key=lambda row: (-len(row["covered_elements"]), -row["face_strength"], row["order"]))
    tier_floor = max(0, len(ranked[0]["covered_elements"]) - max(0, int(tie_window)))
    same_tier = [row for row in ranked if len(row["covered_elements"]) >= tier_floor]
    rest = [row for row in ranked if len(row["covered_elements"]) < tier_floor]
    same_tier.sort(key=lambda row: (row["order"], -row["face_strength"]))
    rest.sort(key=lambda row: (-len(row["covered_elements"]), -row["face_strength"], row["order"]))
    return same_tier + rest


def contract_floor(contract: dict[str, Any], elements: dict[str, Any]) -> float:
    """合同自己要求的要素比例：min_concepts ÷ 要素数。本模块不设第二个阈值。"""
    total = max(1, len(elements))
    return round(min(1.0, int(contract.get("min_concepts") or 0) / total), 4)


def evidence_confidence(elements: dict[str, Any], faces: dict[str, str]) -> float | None:
    """可信度 = 落点语义面已能举证的要素比例。没有一个环节用常数。"""
    if not elements or not faces:
        return None
    hit = sum(1 for terms in elements.values()
              if any(term and any(term in text for text in faces.values()) for term in terms))
    return round(hit / len(elements), 4)


def _section_faces(sections: list[dict[str, Any]], section_ids: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    wanted = {str(sid) for sid in section_ids if sid}
    for shape in sections or []:
        if str(shape.get("section_id")) not in wanted:
            continue
        for name, text in (shape.get("faces") or {}).items():
            merged[name] = (merged.get(name, "") + " " + str(text or "")).strip()
    return merged


def _units_covering(section_id: str, elements: dict[str, tuple[str, ...]],
                    units_by_section: dict[str, list[dict[str, Any]]]) -> tuple[list[str], str]:
    """按 CU 自己的 content intent / archetype 选单元，而不是把整节 CU 都塞进去。

    "一条主题绑 37 个 CU"就是没绑。一个都选不出来时退回该节的第一个单元并在 basis 上
    标 `*_SECTION_FALLBACK` —— 那至少是**这一节的**响应，且看得见是退了一步。
    """
    units = units_by_section.get(str(section_id)) or []
    hits = [str(unit.get("content_unit_id")) for unit in units if _unit_element_hits(unit, elements)]
    if hits:
        return hits, "UNIT_INTENT_MATCH"
    first = [str(unit.get("content_unit_id")) for unit in units if unit.get("content_unit_id")]
    return first[:1], "SECTION_FIRST_UNIT_FALLBACK"


def plan_scoring_mapping(
    records: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    *,
    units_by_section: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """给每条 canonical 主题补上 §17 的七项，并产出需要派生的 Technical Response 单元。

    已有落点（显式评分项 id / 需求引用 / 节内主题匹配绑上的）**一律不推翻** —— 那些是
    引用级证据，比任何语义路由都强；本函数只补还没着落的那些，并给全部记录补上 mapping_* 字段。
    """
    units_by_section = units_by_section or {}
    ordered = [record for record in (records or []) if isinstance(record, dict)]
    aggregate_ids = {str(record.get("canonical_scoring_id") or "")
                     for record in ordered if is_aggregate_topic(record)}
    next_index = {sid: max([int(unit.get("unit_index") or 0) for unit in units] or [0])
                  for sid, units in units_by_section.items()}
    reserve_spent: dict[str, int] = {}
    derived: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}

    for record in ordered:
        cid = str(record.get("canonical_scoring_id") or "")
        # MMF008A-A §A：这条主题不由技术正文回答。路由与派生一律不给它做——
        # 给"类似项目经验"派生一个正文单元，等于为了覆盖率往技术标里塞资格证明；
        # 给"是否覆盖所有物业需求"派生一个单元，等于把整本合同的质量摊成一篇稿子。
        # 三类非响应主题的落点义务为零，但记录必须留在映射产物里，不能静默消失。
        if cid and not record_requires_body_response(record):
            decisions[cid] = _decide_non_respondable(record)
    for record in ordered:
        cid = str(record.get("canonical_scoring_id") or "")
        if cid and cid in decisions:
            continue
        if cid and cid not in aggregate_ids:
            decisions[cid] = _decide(record, sections, units_by_section, cid, derived,
                                      next_index, reserve_spent)
    for record in ordered:
        cid = str(record.get("canonical_scoring_id") or "")
        if cid and cid in aggregate_ids and cid not in decisions:
            decisions[cid] = _decide_aggregate(record, ordered, decisions, cid, aggregate_ids)

    for record in ordered:
        cid = str(record.get("canonical_scoring_id") or "")
        decision = decisions.get(cid) or {}
        record["mapping_basis"] = str(decision.get("mapping_basis") or "UNBOUND")
        record["mapping_confidence"] = decision.get("mapping_confidence")
        record["mapping_reason"] = str(decision.get("mapping_reason") or "")
        record["mapping_alternatives"] = list(decision.get("alternatives") or [])
        record["mapping_review_required"] = bool(decision.get("review_required"))
        record["minimum_substantive_requirements"] = list(decision.get("minimum_substantive_requirements") or [])
        record["mapping_fallback_cu"] = list(decision.get("mapping_fallback_cu") or [])
        record["topic_kind"] = list(decision.get("topic_kind") or [])
        if decision.get("planned_response_section"):
            record["planned_response_section"] = decision["planned_response_section"]
        if decision.get("planned_response_sections"):
            record["planned_response_sections"] = list(decision["planned_response_sections"])
        if decision.get("planned_response_cu"):
            record["planned_response_cu"] = list(decision["planned_response_cu"])
        if not record_requires_body_response(record):
            record['planned_response_section'] = None
            record['planned_response_sections'] = []
            record['planned_response_cu'] = []
        if not (record.get("cu_binding") or {}).get("basis") or (
                str((record.get("cu_binding") or {}).get("basis")) == "UNBOUND"
                and record.get("planned_response_cu")):
            record["cu_binding"] = {**(record.get("cu_binding") or {}),
                                    "basis": record["mapping_basis"]}

    mapped = [record for record in ordered
              if record_requires_body_response(record)
              and record.get("planned_response_section") and (record.get("planned_response_cu") or [])]
    unmapped_ids = sorted(str(record.get("canonical_scoring_id") or "") for record in ordered
                          if record_requires_body_response(record)
                          and not (record.get("planned_response_section")
                                   and (record.get("planned_response_cu") or [])))
    non_respondable_ids = sorted(str(record.get("canonical_scoring_id") or "") for record in ordered
                                 if not record_requires_body_response(record))
    return {
        "schema_version": SCORING_MAPPING_VERSION,
        "records": ordered,
        "derived_units": derived,
        "aggregate_topic_ids": sorted(aggregate_ids),
        "non_respondable_topic_ids": non_respondable_ids,
        "summary": {
            "topic_total": len(ordered),
            "mapped_total": len(mapped),
            "unmapped_total": len(unmapped_ids),
            "unmapped_ids": unmapped_ids,
            # 零落点是**应当**的，不是缺口；但也不能算进 mapped，否则"映射率"会虚高。
            "non_respondable_total": len(non_respondable_ids),
            "non_respondable_ids": non_respondable_ids,
            "derived_unit_total": len(derived),
            "review_required_total": sum(1 for record in ordered if record.get("mapping_review_required")),
            "mapping_basis_counts": _count(str(record.get("mapping_basis") or "") for record in ordered),
            "topic_kind_counts": _count(kind for record in ordered
                                        for kind in (record.get("topic_kind") or [])),
        },
    }


def _alternatives(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"section_id": row["section_id"], "section_title": row["section_title"],
             "coverage_ratio": row["coverage_ratio"],
             "covered_elements": row["covered_elements"][:8]} for row in ranked[1:4]]


def _decide(record: dict[str, Any], sections: list[dict[str, Any]],
            units_by_section: dict[str, list[dict[str, Any]]], cid: str,
            derived: list[dict[str, Any]], next_index: dict[str, int],
            reserve_spent: dict[str, int]) -> dict[str, Any]:
    kinds = detect_topic_kinds(record)
    elements = elements_for(record, kinds)
    contract = record.get("evidence_contract") or {}
    floor = contract_floor(contract, elements)
    ranked = rank_sections(elements, sections)
    common = {
        "topic_kind": kinds or [LABEL_TOKEN_KIND],
        "minimum_substantive_requirements": list(elements),
        "alternatives": _alternatives(ranked),
    }
    best = ranked[0] if ranked else None

    existing_cu = [str(cu) for cu in (record.get("planned_response_cu") or [])]
    existing_section = str(record.get("planned_response_section") or "")
    explicit_exit = any(
        re.search(r'项目退出|退出移交|合同终止', str(unit.get('title') or '') + str(unit.get('intent') or ''))
        and set(elements) <= set(unit.get('must_cover') or [])
        for unit in units_by_section.get(existing_section) or []
        if unit.get('content_unit_id') in existing_cu)
    needs_exit_point = 'EXIT_HANDOVER' in kinds and not explicit_exit
    existing_confidence = evidence_confidence(elements, _section_faces(sections, [existing_section])) if existing_section else None
    repair_weak_binding = (contract.get('rubric_id') in {'MANAGEMENT_TEAM', 'MANAGEMENT_ADVANTAGE'}
                           and (existing_confidence is None or existing_confidence < floor))
    if existing_cu and existing_section and not needs_exit_point and not repair_weak_binding:
        faces = _section_faces(sections, [existing_section])
        confidence = evidence_confidence(elements, faces)
        return {**common,
                "mapping_basis": str((record.get("cu_binding") or {}).get("basis") or "PLAN_BOUND"),
                "planned_response_section": existing_section,
                "planned_response_sections": [str(s) for s in (record.get("planned_response_sections") or [existing_section])],
                "planned_response_cu": existing_cu,
                "mapping_confidence": confidence,
                "mapping_reason": "引用级绑定（评分项 id / 需求引用 / 节内主题匹配）保留，语义路由不改写强证据。",
                "review_required": confidence is None or confidence < floor}

    if not needs_exit_point and best and best["covered_elements"] and best["coverage_ratio"] >= floor:
        cu_ids, unit_basis = _units_covering(best["section_id"], elements, units_by_section)
        if cu_ids:
            return {**common,
                    "mapping_basis": f"TECHNICAL_TOPIC_KIND_ROUTE/{unit_basis}",
                    "planned_response_section": best["section_id"],
                    "planned_response_sections": [best["section_id"]] + [
                        row["section_id"] for row in ranked[1:3] if row["covered_elements"]],
                    "planned_response_cu": cu_ids,
                    "mapping_confidence": evidence_confidence(elements, best["faces"]),
                    "mapping_reason": (
                        f"按技术主题种类 {('、'.join(kinds) or '合同标签词组')} 路由到 "
                        f"{best['section_id']}（{best['section_title']}）："
                        f"要素举证 {len(best['covered_elements'])}/{best['element_total']}，"
                        f"合同下限 {floor:.2f}；单元按 content intent 选定（{unit_basis}）。"
                    ),
                    "review_required": evidence_confidence(elements, best["faces"]) < floor}

    if needs_exit_point:
        # An exit procedure belongs in an operational/implementation section,
        # not a generic project-facts section or contractor entry procedure.
        appropriate = [r for r in ranked if re.search(r'退出|服务定位.*响应|履约|实施', r['section_title'])
                       and not re.search(r'供方|供应商|进场', r['section_title'])]
        if appropriate:
            best = appropriate[0]
    host = str(best["section_id"]) if best and best["section_id"] else str(
        (sections[0].get("section_id") if sections else "") or "")
    if not host:
        return {**common, "mapping_basis": "UNBOUND", "mapping_confidence": None,
                "mapping_reason": "规划里没有任何 Section，无法建立技术响应落点。",
                "review_required": True}
    # 一条节被多个主题派生时，后面的主题只能拿**剩下的**机动额度；额度门槛与
    # `hierarchy.apply_scoring_mapping_units` 用的是同一个口径（本模块的两条常数）。
    reserve = max(0, int((best or {}).get("reserve_chars") or 0)) - int(reserve_spent.get(host, 0))
    fallback_cu, unit_basis = _units_covering(host, elements, units_by_section)
    if reserve < MIN_DERIVED_UNIT_CHARS and fallback_cu:
        # 派生一个单元要有人给它字。既有节吃不下一个新单元时，**不造没额度的落点**：
        # 先让这条主题由该节既有单元承载（真实存在、真实会被生成），把额度缺口写成
        # 具名原因交给人复核。静默判未映射与静默造一个空头单元，是同一件事的两个错向。
        return {**common,
                "mapping_basis": "TECHNICAL_TOPIC_KIND_ROUTE/HOST_SECTION_HAS_NO_RESERVE",
                "planned_response_section": host,
                "planned_response_sections": [host],
                "planned_response_cu": fallback_cu,
                "mapping_confidence": evidence_confidence(elements, best["faces"]),
                "mapping_reason": (
                    f"该节剩余机动额度 {reserve} 字低于派生一个内容单元的门槛 "
                    f"{MIN_DERIVED_UNIT_CHARS} 字：不新建没有额度的单元，"
                    f"主题暂由 {host} 既有单元承载（{unit_basis}），"
                    f"要素举证 {len(best['covered_elements'])}/{best['element_total']} "
                    f"未达合同下限 {floor:.2f}，必须人工复核是否为其腾额度。"
                ),
                "review_required": True}
    next_index[host] = int(next_index.get(host, 0)) + 1
    chars = min(reserve, DERIVED_UNIT_TARGET_CHARS)
    reserve_spent[host] = int(reserve_spent.get(host, 0)) + chars
    unit = _derive_unit(record, cid, host, elements, next_index[host], chars, best, fallback_cu)
    derived.append(unit)
    units_by_section.setdefault(host, []).append(unit)
    return {**common,
            "mapping_basis": "DERIVED_TECHNICAL_RESPONSE_CU",
            "planned_response_section": host,
            "planned_response_sections": [host],
            "planned_response_cu": [unit["content_unit_id"]],
            "mapping_fallback_cu": fallback_cu,
            "mapping_confidence": evidence_confidence(elements, best["faces"] if best else {}),
            "mapping_reason": (
                f"既有节最多只能举证 {len(best['covered_elements']) if best else 0}/"
                f"{best['element_total'] if best else len(elements)} 项最低实质要求"
                f"（合同下限 {floor:.2f}）——按 §18 在 {host} 派生一个 Technical Response 内容单元，"
                f"额度取自该节剩余机动额度 {reserve} 字（本单元 {chars} 字），必须人工复核。"
            ),
            "review_required": True}


def _decide_aggregate(record: dict[str, Any], ordered: list[dict[str, Any]],
                      decisions: dict[str, dict[str, Any]], cid: str,
                      aggregate_ids: set[str]) -> dict[str, Any]:
    """总纲行：它的响应就是成员主题落点的并集，不另造一篇正文。"""
    kinds = detect_topic_kinds(record)
    elements = elements_for(record, kinds)
    members = [other for other in ordered
               if str(other.get("canonical_scoring_id") or "") != cid
               and str(other.get("canonical_scoring_id") or "") not in aggregate_ids]
    sections = [str(sid) for sid in (record.get("planned_response_sections") or [])]
    units = [str(cu) for cu in (record.get("planned_response_cu") or [])]
    missing: list[str] = []
    confidences: list[float] = []
    for other in members:
        ocid = str(other.get("canonical_scoring_id") or "")
        other = {**other, **(decisions.get(ocid) or {})}
        for sid in [str(s) for s in (other.get("planned_response_sections") or [])]:
            if sid and sid not in sections:
                sections.append(sid)
        for cu in [str(c) for c in (other.get("planned_response_cu") or [])]:
            if cu and cu not in units:
                units.append(cu)
        if (other.get("planned_response_section") and (other.get("planned_response_cu") or [])):
            if (decisions.get(ocid) or {}).get("mapping_confidence") is not None:
                confidences.append(float(decisions[ocid]["mapping_confidence"]))
        else:
            missing.append(ocid)
    confidence = round(sum(confidences) / len(confidences), 4) if confidences else None
    return {
        "topic_kind": kinds,
        "minimum_substantive_requirements": list(elements),
        "alternatives": [],
        "mapping_basis": "AGGREGATE_COVERAGE_OF_MEMBER_TOPICS",
        "planned_response_section": str(record.get("planned_response_section") or (sections[0] if sections else "")) or None,
        "planned_response_sections": sections,
        "planned_response_cu": units,
        "mapping_confidence": confidence,
        "mapping_reason": (
            f"该评分项同时列举 {len(kinds)} 个技术主题种类，是一张贴合评分表的覆盖清单而不是新正文："
            f"落点取 {len(members)} 条成员主题的节与内容单元并集。"
            + (f"成员仍缺落点：{missing}" if missing else "")
        ),
        "review_required": bool(missing),
    }


def _decide_non_respondable(record: dict[str, Any]) -> dict[str, Any]:
    """第二维判为"不由技术正文回答"的三类：落点义务为零，但记录照旧留在映射产物里。

    这里刻意**不**给任何 section/CU：一次派生就是把证明材料或整本质量摊成一篇正文稿子。
    原因文本带着命中标记词落盘，读的人能在招标原始标签里指着那几个字核对。
    """
    name = str(record.get("responsiveness_class") or "")
    markers = (record.get("responsiveness_evidence") or {}).get("matched_markers") or {}
    protected = [str(item) for item in (record.get("protected_evidence_terms") or [])]
    whole_document = [str(item) for item in (record.get("judged_by_whole_document_dimensions") or [])]
    if name == CROSS_FILE_PROTECTED_EVIDENCE:
        why = (
            "该主题的举证是个人资历、真实项目业绩、合同/证书/履约证明，权威位置在资格与商务"
            "文件，不在技术标正文：为凑覆盖率把它派生成一个正文单元，就是把证明材料拉进技术标。"
            + (f"点名的证明材料：{'、'.join(protected)}。" if protected else "")
        )
    elif name == WHOLE_DOCUMENT_RUBRIC:
        why = (
            "该评分口径评的是整本合同的质量（需求覆盖、结构、完整性、合理可行），"
            f"由整体文档 QA 的既有维度 {'、'.join(whole_document) or 'REQUIREMENT_COVERAGE 等'} 判，"
            "不派生一个内容单元去逐字对应招标原句。"
        )
    elif name == CONTRACTUAL_CONTEXT:
        why = ("该条评的是合同约束与商务/价格承诺（可终止、违约、付款、资质承诺一类），"
               "落点在合同与商务文件；技术正文不建立对应单元。")
    else:
        why = ("卷次标题、计分规则与投标程序语：这份表在这一行没有在问一件可响应的事，"
               "登记不响应，不建立技术正文落点。")
    return {
        "topic_kind": list(record.get("topic_kind") or []),
        "minimum_substantive_requirements": [],
        "alternatives": [],
        "mapping_basis": NON_RESPONDABLE_MAPPING_BASE,
        "mapping_confidence": None,
        "mapping_reason": (
            f"第二维类别 {name or '(未分类)'}（凭据 {json.dumps(markers, ensure_ascii=False)}）："
            + why
        ),
        "review_required": False,
        "response_requirement": str(record.get("response_requirement") or ""),
        "protected_evidence_terms": protected,
        "judged_by_whole_document_dimensions": whole_document,
    }


def _derive_unit(record: dict[str, Any], cid: str, host_section: str,
                 elements: dict[str, tuple[str, ...]], index: int, chars: int,
                 best: dict[str, Any] | None, fallback_cu: list[str]) -> dict[str, Any]:
    # 派生单元的 title/intent/purpose 是**展示与提示**用途，取展示形（≤TOPIC_DISPLAY_CHARS），
    # 与 Round 4.6 之前的产物形状逐字一致；身份与覆盖一律读 `record["topic"]`（完整主题）。
    topic = str(record.get("display_topic") or record.get("topic") or cid)[:80]
    kinds = detect_topic_kinds(record)
    return {
        "content_unit_id": f"CU-{host_section}-{index:02d}",
        "section_id": host_section,
        "parent_section_id": host_section,
        "unit_index": index,
        # 额度由映射端算好（该节剩余机动额度封顶），层级端只按这个数落单元：
        # 两处各自 min() 迟早会算出两个数，那又是一把尺子量两处。
        "unified_budget_chars": int(chars),
        "fallback_cu_ids": list(fallback_cu),
        "title": topic,
        "intent": topic,
        "purpose": topic,
        "archetype": KIND_RESPONSE_ARCHETYPE.get(kinds[0] if kinds else "", "NARRATIVE"),
        "must_cover": list(elements),
        "requirement_ids": [str(item) for item in (record.get("requirement_refs") or [])],
        "source_requirements": [str(item) for item in (record.get("requirement_refs") or [])],
        "scoring_item_ids": [str(item) for item in (record.get("evaluation_row_ids") or record.get("source_ids") or [])],
        "source_scoring_items": [str(item) for item in (record.get("evaluation_row_ids") or record.get("source_ids") or [])],
        "canonical_scoring_id": cid,
        "provenance": "SCORING_TECHNICAL_RESPONSE_UNIT",
        "derived_from": {"basis": "DERIVED_TECHNICAL_RESPONSE_CU",
                         "host": host_section,
                         "host_covered_elements": list((best or {}).get("covered_elements") or [])},
        "status": "PLANNED",
    }


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return out
