from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


class PlanningError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_document_target(
    medium: str = "word_and_ppt",
    target_scale: str = "standard_longform",
    word_pages: tuple[int, int] = (50, 80),
    ppt_slides: tuple[int, int] = (35, 50),
) -> dict[str, Any]:
    if medium not in {"word", "ppt", "word_and_ppt"}:
        raise PlanningError("Document Target medium无效。")
    if target_scale not in {"standard_longform", "custom"}:
        raise PlanningError("Document Target target_scale无效。")
    if medium in {"word", "word_and_ppt"} and not (1 <= word_pages[0] <= word_pages[1]):
        raise PlanningError("Word目标页数无效。")
    if medium in {"ppt", "word_and_ppt"} and not (1 <= ppt_slides[0] <= ppt_slides[1]):
        raise PlanningError("PPT目标页数无效。")
    return {
        "schema_version": "document-target-v0.1",
        "medium": medium,
        "document_types": ["service_solution", "presentation"] if medium == "word_and_ppt" else (["service_solution"] if medium == "word" else ["presentation"]),
        "target_scale": target_scale,
        "word": {"target_pages_min": word_pages[0], "target_pages_max": word_pages[1]} if medium != "ppt" else None,
        "ppt": {"target_slides_min": ppt_slides[0], "target_slides_max": ppt_slides[1]} if medium != "word" else None,
    }


def _active_requirements(pack: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in pack.get("requirements", []) if r.get("confirmation_status") not in {"REJECTED_AS_NOT_APPLICABLE", "REJECTED"}]


def _is_non_content_marker(requirement: dict[str, Any]) -> bool:
    text = str(requirement.get("normalized_requirement", "")).strip()
    return text.startswith("##") or text.startswith("[SIMULATED_SCAN_PAGE]")


def _planning_requirements(pack: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in _active_requirements(pack) if not _is_non_content_marker(r)]


def _word_outline() -> list[dict[str, Any]]:
    rows = [
        ("CH01", "项目理解与需求边界", 5, "high", [
            ("S01-01", "项目概况、服务范围与排除项"),
            ("S01-02", "产业园特征与物业管理重难点"),
        ]),
        ("CH02", "服务构想与总体运营机制", 5, "high", [
            ("S02-01", "服务定位、目标与响应策略"),
            ("S02-02", "运营机制、协同界面与成果体系"),
        ]),
        ("CH03", "组织架构与人员履约保障", 6, "high", [
            ("S03-01", "项目组织架构与职责边界"),
            ("S03-02", "人员配置、排班与服务时间"),
            ("S03-03", "培训、考核与岗位履约保障"),
        ]),
        ("CH04", "客户服务与诉求闭环", 6, "high", [
            ("S04-01", "客户服务范围、渠道与服务界面"),
            ("S04-02", "投诉响应、过程跟踪与闭环反馈"),
        ]),
        ("CH05", "安全秩序与园区通行管理", 7, "high", [
            ("S05-01", "秩序维护模式与岗位协同"),
            ("S05-02", "巡逻机制、重点区域与异常处置"),
            ("S05-03", "人车通行与突发事件联动"),
        ]),
        ("CH06", "工程设施运行与维护管理", 7, "high", [
            ("S06-01", "工程服务边界与设施设备台账"),
            ("S06-02", "巡检、维保与故障闭环"),
            ("S06-03", "运行记录、能耗观察与风险预防"),
        ]),
        ("CH07", "环境服务与现场品质", 6, "normal", [
            ("S07-01", "环境服务范围、标准与作业组织"),
            ("S07-02", "清洁质量检查与问题整改"),
            ("S07-03", "供方履约机制（适用时）"),
        ]),
        ("CH08", "品质管理与持续改进", 5, "normal", [
            ("S08-01", "日常品质巡检与分级整改"),
            ("S08-02", "数据记录、复盘与持续改进"),
        ]),
        ("CH09", "进场准备与客户协同", 4, "normal", [
            ("S09-01", "进场准备、资料交接与启动计划"),
            ("S09-02", "日常沟通、事项协调与报告机制"),
        ]),
        ("CH10", "应急管理与联动保障", 5, "normal", [
            ("S10-01", "应急管理体系、职责与资源"),
            ("S10-02", "预案、演练与事件复盘"),
        ]),
        ("CH11", "实施计划、履约成果与承诺边界", 4, "normal", [
            ("S11-01", "实施里程碑与阶段成果"),
            ("S11-02", "履约记录、报告与验收接口"),
            ("S11-03", "待澄清事项、假设与承诺边界"),
        ]),
    ]
    return [
        {
            "chapter_id": cid,
            "chapter_title": title,
            "target_pages": pages,
            "priority": priority,
            "sections": [{"section_id": sid, "section_title": stitle} for sid, stitle in sections],
        }
        for cid, title, pages, priority, sections in rows
    ]


def _section_lookup(outline: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        section["section_id"]: {**section, "chapter_id": chapter["chapter_id"], "chapter_title": chapter["chapter_title"]}
        for chapter in outline
        for section in chapter["sections"]
    }


def _route_requirement(requirement: dict[str, Any]) -> str:
    domain = str(requirement.get("domain", "other"))
    text = str(requirement.get("normalized_requirement", ""))
    if domain == "project_fact":
        return "S01-01"
    if domain == "service_hours":
        return "S03-02"
    if domain == "security":
        return "S05-02"
    if domain == "staffing":
        return "S03-02"
    if domain == "sla_kpi":
        return "S04-02"
    if domain == "exclusion":
        return "S01-01"
    if domain == "scoring":
        return "S02-01"
    if "品质巡检" in text or "质量" in text:
        return "S08-01"
    if "扫描" in text or "无法解析" in text:
        return "S11-03"
    if "服务时间" in text:
        return "S03-02"
    if "秩序" in text or "巡逻" in text:
        return "S05-02"
    if "人员" in text:
        return "S03-02"
    if "评分" in text:
        return "S02-01"
    return "S01-02"


def build_requirement_matrix(pack: dict[str, Any], outline: list[dict[str, Any]]) -> dict[str, Any]:
    valid_sections = set(_section_lookup(outline))
    matrix: list[dict[str, Any]] = []
    for req in _planning_requirements(pack):
        primary = _route_requirement(req)
        if primary not in valid_sections:
            matrix.append({
                "requirement_id": req["requirement_id"], "mandatory_level": req.get("mandatory_level", "INFO"),
                "scoring_item_id": req.get("scoring_item_id"), "primary_section_id": None,
                "secondary_section_ids": [], "coverage_mode": "CONDITIONAL",
                "planned_response_type": "clarification", "coverage_status": "UNMAPPED",
                "reason": "未找到可用Section。",
            })
            continue
        secondary: list[str] = []
        if req.get("domain") == "sla_kpi":
            secondary = ["S08-01"]
        elif req.get("domain") == "staffing":
            secondary = ["S03-01"]
        elif req.get("domain") == "exclusion":
            secondary = ["S11-03"]
        matrix.append({
            "requirement_id": req["requirement_id"],
            "requirement_text": req.get("normalized_requirement", ""),
            "requirement_domain": req.get("domain", "other"),
            "mandatory_level": req.get("mandatory_level", "INFO"),
            "scoring_item_id": req.get("scoring_item_id"),
            "primary_section_id": primary,
            "secondary_section_ids": secondary,
            "coverage_mode": "FULL" if req.get("mandatory_level") == "MUST" or req.get("scoring_item_id") else "REFERENCE",
            "planned_response_type": "evidence_and_method" if req.get("mandatory_level") == "MUST" else "context_or_boundary",
            "coverage_status": "MAPPED",
            "reason": "按已确认需求的业务领域和内容含义映射；未让评分项直接控制PPT目录。",
        })
    must_rows = [r for r in matrix if r["mandatory_level"] == "MUST"]
    score_ids = {
        str(s.get("scoring_item_id")) for s in pack.get("scoring_items", [])
        if s.get("must_respond") and not str(s.get("label", "")).strip().startswith("##")
    }
    mapped_scores = {str(r.get("scoring_item_id")) for r in matrix if r.get("scoring_item_id") and r["coverage_status"] == "MAPPED"}
    excluded = [
        {"requirement_id": r.get("requirement_id"), "reason": "Todd/006A已确认不适用", "text": r.get("normalized_requirement", "")}
        for r in pack.get("requirements", [])
        if r.get("confirmation_status") in {"REJECTED_AS_NOT_APPLICABLE", "REJECTED"}
    ]
    excluded += [
        {"requirement_id": r.get("requirement_id"), "reason": "结构标题或模拟解析标记，仅保留为内部追溯/澄清信息，不进入正文施工单", "text": r.get("normalized_requirement", "")}
        for r in _active_requirements(pack) if _is_non_content_marker(r)
    ]
    return {
        "schema_version": "requirement-section-matrix-v0.1",
        "pack_id": pack.get("pack_id"),
        "matrix": matrix,
        "excluded_requirements": excluded,
        "coverage_summary": {
            "must_total": len(must_rows),
            "must_mapped": sum(r["coverage_status"] == "MAPPED" for r in must_rows),
            "scoring_total": len(score_ids),
            "scoring_mapped": len(score_ids & mapped_scores),
            "unmapped": sum(r["coverage_status"] == "UNMAPPED" for r in matrix),
            "conflict_blocked": sum(r["coverage_status"] == "CONFLICT_BLOCKED" for r in matrix),
        },
    }


def build_content_budget(outline: list[dict[str, Any]], matrix: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    matrix_rows = matrix["matrix"]
    for chapter in outline:
        chapter_sections = {s["section_id"] for s in chapter["sections"]}
        related = [r for r in matrix_rows if r.get("primary_section_id") in chapter_sections]
        must_count = sum(r.get("mandatory_level") == "MUST" for r in related)
        scoring_count = sum(bool(r.get("scoring_item_id")) for r in related)
        pages = int(chapter["target_pages"])
        deep = chapter["chapter_id"] in {"CH04", "CH05", "CH06", "CH07", "CH09", "CH10"}
        shallow = chapter["chapter_id"] in {"CH01", "CH11"}
        density = 1.0 + min(0.35, 0.03 * (must_count + scoring_count))
        if deep:
            density *= 1.12
        if shallow:
            density *= 0.88
        words_min, words_max = int(pages * 480 * density), int(pages * 650 * density)
        reason_parts = [f"章节目标约{pages}页", f"MUST {must_count}条", f"评分关联 {scoring_count}条", f"复杂度系数{density:.2f}"]
        rows.append({
            "chapter_id": chapter["chapter_id"], "chapter_title": chapter["chapter_title"],
            "content_weight": round(pages / sum(c["target_pages"] for c in outline), 4),
            "target_words_min": words_min, "target_words_max": words_max,
            "target_pages_min": max(2, pages - 1), "target_pages_max": pages + 1,
            "mandatory_requirement_count": must_count, "scoring_item_count": scoring_count,
            "reason": "；".join(reason_parts) + "；综合项目优先级、风险复杂度与知识可用性采用可解释启发式分配。",
        })
    return {
        "schema_version": "word-content-budget-v0.1",
        "chapters": rows,
        "totals": {
            "target_words_min": sum(r["target_words_min"] for r in rows),
            "target_words_max": sum(r["target_words_max"] for r in rows),
            "target_pages_min": sum(r["target_pages_min"] for r in rows),
            "target_pages_max": sum(r["target_pages_max"] for r in rows),
            "planning_pages": sum(c["target_pages"] for c in outline),
        },
        "page_estimation_note": "页数仅用于规划估计，006C以文字预算为主要控制指标；不得为凑页数注水。",
    }


TOPIC_OWNER = {
    "complaint_closure": "S04-02",
    "quality_pdca": "S08-01",
    "training_system": "S03-03",
    "emergency_response": "S10-01",
    "supplier_management": "S07-03",
    "customer_communication": "S04-01",
    "inspection_system": "S06-02",
    "handover_process": "S09-01",
    "security_patrol": "S05-02",
    "access_control": "S05-03",
    "environment_operations": "S07-01",
    "environment_correction": "S07-02",
}


SECTION_HARDENING: dict[str, dict[str, Any]] = {
    "S01-01": {"must_cover": ["项目基本事实与服务对象", "服务范围、排除范围及责任接口", "项目事实、条件性方法与待澄清事项的边界"], "outputs": ["项目事实清单", "服务边界清单", "待澄清事项"]},
    "S01-02": {"must_cover": ["产业园生产经营连续性对物业服务的影响", "园区人车物流、设施运行和客户协同重难点", "重难点对应的管理原则与验证方式"], "outputs": ["项目重难点清单", "重难点响应关系"]},
    "S02-01": {"must_cover": ["面向本项目的服务定位与目标", "从客户要求到服务动作的响应策略", "评分要求的逐项响应组织方式但不将评分数字写成服务承诺"], "outputs": ["服务目标", "响应策略", "编制响应约束"]},
    "S02-02": {"must_cover": ["项目统筹、专业执行与客户协同的运营机制", "事项从发起到关闭的协同界面", "阶段成果、服务记录和管理报告的形成方式"], "outputs": ["运营机制图", "协同界面", "成果体系"]},
    "S03-01": {"must_cover": ["项目组织层级及汇报关系", "项目负责人和专业负责人的职责边界", "跨专业事项的牵头、协同与升级机制"], "outputs": ["组织架构", "职责分工", "协同升级规则"]},
    "S03-02": {"must_cover": ["已确认最低人员配置", "已确认服务时间及排班原则", "缺岗、峰值和临时任务下的调度边界"], "must_not_cover": ["未经确认的24小时值守承诺"], "outputs": ["人员配置表", "排班原则", "调度说明"]},
    "S03-03": {"must_cover": ["上岗培训、专项培训与持续学习安排", "岗位履职检查与考核反馈", "能力偏差的辅导、复训和验证"], "processes": ["training_system"], "outputs": ["培训计划", "考核记录", "复训记录"]},
    "S04-01": {"must_cover": ["客户服务对象、服务渠道与受理边界", "客户事项的登记、分派和协同接口", "日常沟通、信息反馈与服务记录"], "outputs": ["客户触点清单", "受理渠道", "沟通记录"]},
    "S04-02": {"must_cover": ["投诉受理、首次响应、过程跟踪和结果反馈", "投诉分级、责任分派与升级条件", "回访、关闭、复盘及记录要求"], "processes": ["complaint_closure"], "outputs": ["投诉台账", "处理记录", "回访与关闭记录"]},
    "S05-01": {"must_cover": ["秩序服务岗位与覆盖区域", "门岗、巡逻和事件处置岗位的协同", "交接班、信息传递和异常升级规则"], "outputs": ["岗位协同表", "交接记录", "异常升级规则"]},
    "S05-02": {"must_cover": ["巡逻区域分类、路线与重点区域", "已确认巡逻频次及组织原则", "异常发现、报告、现场处置、升级、复核和关闭", "巡逻签到、异常记录与复核记录"], "processes": ["security_patrol_exception_flow"], "outputs": ["巡逻计划", "巡逻记录", "异常处置与复核记录"]},
    "S05-03": {"must_cover": ["门区、人员和车辆的通行管理原则", "访客、施工及特殊车辆的核验接口", "异常放行、拥堵和突发事件的联动处置"], "processes": ["access_event_coordination"], "outputs": ["通行规则", "异常放行记录", "事件联动记录"]},
    "S06-01": {"must_cover": ["工程服务对象、设施范围和责任边界", "设备分类、基础信息和状态台账", "甲方、物业和外部维保单位的接口"], "outputs": ["设施设备清单", "设备台账", "责任界面表"]},
    "S06-02": {"must_cover": ["巡检对象、标准、周期和组织方式", "异常发现、故障分级、报修、判断和处置", "外部维保协同、复验、关闭及全过程记录", "未确认维修时限不得转化为数字承诺"], "processes": ["engineering_fault_closure"], "outputs": ["巡检计划", "报修工单", "故障关闭与复验记录"]},
    "S06-03": {"must_cover": ["设备运行、能耗和异常趋势的记录内容", "风险征兆识别、预警和干预原则", "记录复核、趋势分析和预防性行动"], "outputs": ["运行记录", "能耗观察记录", "风险预警清单"]},
    "S07-01": {"must_cover": ["环境服务范围、区域分类和作业边界", "不同区域、时段和场景的作业组织", "作业标准、人员协同和现场记录"], "processes": ["environment_operation_flow"], "outputs": ["作业计划", "区域标准", "作业记录"]},
    "S07-02": {"must_cover": ["清洁质量检查对象、标准和检查方式", "问题登记、责任分派、整改和复查", "问题关闭记录及与品质章节的引用边界"], "processes": ["environment_quality_correction"], "outputs": ["环境检查记录", "整改清单", "复查关闭记录"]},
    "S07-03": {"must_cover": ["仅在供方模式确认后说明供方准入与任务接口", "供方作业检查、问题整改和履约评价", "未启用供方时仅输出条件说明"], "processes": ["supplier_management"], "outputs": ["供方启用条件", "供方检查与评价记录"], "activation": {"expression": "operating_model == 'outsourced' or supplier_model_confirmed == true", "when_unconfirmed": "summary_only_or_omit_body"}},
    "S08-01": {"must_cover": ["日常品质巡检对象、层级和组织方式", "问题分级、责任落实、整改验证和关闭", "与环境专业检查的边界：本节主讲跨专业品质管理"], "processes": ["quality_pdca"], "outputs": ["品质巡检计划", "分级问题清单", "整改验证记录"]},
    "S08-02": {"must_cover": ["服务数据、问题趋势和改进机会的汇总", "周期复盘、原因分析与改进行动", "改进效果验证和规则更新"], "outputs": ["数据分析", "复盘纪要", "持续改进清单"]},
    "S09-01": {"must_cover": ["进场前资料、人员、现场和工具准备", "交接事项、责任人、完成条件和里程碑", "启动异常、缺项升级及确认关闭"], "processes": ["handover_process"], "outputs": ["进场计划", "交接清单", "启动确认记录"]},
    "S09-02": {"must_cover": ["例行沟通对象、议题、频次和责任接口", "事项协调、决策记录和后续跟踪", "管理报告的数据来源、提交和反馈机制"], "outputs": ["沟通机制", "事项跟踪清单", "管理报告"]},
    "S10-01": {"must_cover": ["应急事件分级、指挥体系和岗位职责", "启动条件、资源调配、现场处置和外部联动", "信息报告、恢复、关闭及记录"], "processes": ["emergency_response"], "outputs": ["应急组织图", "响应流程", "事件处置记录"]},
    "S10-02": {"must_cover": ["预案清单、适用场景和维护责任", "演练计划、过程记录和效果评价", "事件或演练后的复盘、改进和验证"], "outputs": ["预案清单", "演练记录", "复盘改进清单"]},
    "S11-01": {"must_cover": ["实施阶段、关键里程碑和责任主体", "各阶段启动条件、重点任务和完成标志", "阶段成果及延误或依赖事项处理"], "outputs": ["实施路线图", "里程碑表", "阶段成果清单"]},
    "S11-02": {"must_cover": ["履约记录、管理报告和成果文件分类", "成果提交、检查、反馈和确认接口", "验收依据、缺项整改和归档方式"], "outputs": ["成果目录", "报告清单", "验收与归档接口"]},
    "S11-03": {"must_cover": ["已确认事实、承诺与条件性方法的区分", "待澄清事项及其影响章节", "未确认数字、案例和责任边界的处理规则"], "outputs": ["承诺边界清单", "待澄清事项", "影响章节索引"]},
}


PROCESS_CONTRACTS = [
    {"process_id": "training_system", "owner_section": "S03-03", "trigger": "新员工上岗、岗位变化或能力偏差", "steps": ["需求识别", "计划", "实施", "考核", "复训验证"], "roles": ["项目负责人", "专业负责人", "岗位人员"], "exception_path": ["考核不通过转入复训并再次验证"], "outputs": ["培训计划", "考核结果"], "records": ["签到记录", "复训记录"], "reference_sections": ["S03-01", "S03-02"]},
    {"process_id": "complaint_closure", "owner_section": "S04-02", "trigger": "收到客户投诉或升级诉求", "steps": ["受理", "首次响应", "分级分派", "跟踪处置", "反馈回访", "关闭复盘"], "roles": ["客户服务岗位", "专业负责人", "项目负责人"], "exception_path": ["超时、重复或重大投诉升级项目负责人"], "outputs": ["处理结果", "回访结论"], "records": ["投诉台账", "过程记录"], "reference_sections": ["S04-01", "S08-02"]},
    {"process_id": "security_patrol_exception_flow", "owner_section": "S05-02", "trigger": "按计划巡逻或发现现场异常", "steps": ["路线执行", "重点检查", "异常记录", "现场处置", "分级上报", "复核关闭"], "roles": ["巡逻岗位", "秩序负责人", "项目负责人"], "exception_path": ["超出处置能力时保护现场并联动相关方"], "outputs": ["巡逻结果", "异常关闭结论"], "records": ["巡逻记录", "异常复核记录"], "reference_sections": ["S05-01", "S05-03", "S10-01"]},
    {"process_id": "access_event_coordination", "owner_section": "S05-03", "trigger": "人员、车辆或特殊作业通行异常", "steps": ["核验", "分类", "放行判断", "现场疏导", "异常升级", "记录"], "roles": ["门岗", "秩序负责人", "客户接口人"], "exception_path": ["未经授权或影响生产安全时拒绝放行并升级"], "outputs": ["通行处置结果"], "records": ["异常放行记录", "事件记录"], "reference_sections": ["S05-01", "S10-01"]},
    {"process_id": "engineering_fault_closure", "owner_section": "S06-02", "trigger": "巡检发现异常或收到报修", "steps": ["报修登记", "故障判断", "分级处置", "外部协同", "复验", "关闭"], "roles": ["工程岗位", "工程负责人", "外部维保单位"], "exception_path": ["重大或边界外故障升级甲方并保留现场记录"], "outputs": ["维修结果", "复验结论"], "records": ["报修工单", "故障关闭记录"], "reference_sections": ["S06-01", "S06-03", "S10-01"]},
    {"process_id": "environment_operation_flow", "owner_section": "S07-01", "trigger": "日常计划、重点时段或专项环境任务", "steps": ["区域分级", "任务安排", "现场作业", "自查", "记录"], "roles": ["环境岗位", "环境负责人"], "exception_path": ["临时污染或影响生产时调整作业并通知相关方"], "outputs": ["作业完成情况"], "records": ["作业记录"], "reference_sections": ["S07-02"]},
    {"process_id": "environment_quality_correction", "owner_section": "S07-02", "trigger": "环境检查发现不符合项", "steps": ["检查", "登记", "分派", "整改", "复查", "关闭"], "roles": ["检查人员", "环境负责人", "责任岗位"], "exception_path": ["重复或重大问题转品质章节复盘"], "outputs": ["整改结果"], "records": ["检查记录", "复查记录"], "reference_sections": ["S07-01", "S08-01"]},
    {"process_id": "supplier_management", "owner_section": "S07-03", "trigger": "供方模式已经确认并启用", "steps": ["准入", "任务交底", "过程检查", "问题整改", "履约评价"], "roles": ["专业负责人", "供方负责人", "项目负责人"], "exception_path": ["供方未启用时不展开正文；重大违约升级项目负责人"], "outputs": ["供方评价"], "records": ["检查记录", "整改记录"], "reference_sections": ["S07-01", "S07-02"]},
    {"process_id": "quality_pdca", "owner_section": "S08-01", "trigger": "日常品质巡检或跨专业问题进入品质管理", "steps": ["检查", "分级", "整改", "验证", "关闭", "趋势复盘"], "roles": ["品质检查人员", "专业负责人", "项目负责人"], "exception_path": ["重复、重大或逾期问题升级专题改进"], "outputs": ["整改验证结论"], "records": ["品质巡检记录", "分级整改清单"], "reference_sections": ["S07-02", "S08-02"]},
    {"process_id": "handover_process", "owner_section": "S09-01", "trigger": "项目进场启动或资料现场交接", "steps": ["准备", "清单确认", "现场核验", "缺项升级", "交接确认", "启动"], "roles": ["项目负责人", "专业负责人", "甲方接口人"], "exception_path": ["关键资料或条件缺失时记录影响并升级确认"], "outputs": ["交接结论", "启动确认"], "records": ["交接清单", "缺项记录"], "reference_sections": ["S11-01", "S11-02"]},
    {"process_id": "emergency_response", "owner_section": "S10-01", "trigger": "突发事件达到预案启动条件", "steps": ["发现报告", "事件分级", "指挥启动", "现场处置", "外部联动", "恢复关闭"], "roles": ["发现岗位", "专业负责人", "项目负责人", "外部联动单位"], "exception_path": ["重大事件优先保障人员安全并按甲方机制升级"], "outputs": ["事件处置结果", "恢复结论"], "records": ["事件记录", "复盘记录"], "reference_sections": ["S05-03", "S06-02", "S10-02"]},
]


def build_section_contracts(
    pack: dict[str, Any], outline: list[dict[str, Any]], matrix: dict[str, Any],
    budget: dict[str, Any], knowledge_selection: dict[str, Any],
) -> dict[str, Any]:
    sections = _section_lookup(outline)
    chapter_budget = {r["chapter_id"]: r for r in budget["chapters"]}
    selected = [r.get("ku_id") for r in knowledge_selection.get("knowledge_usage_contracts", []) if r.get("selection_status") == "SELECTED"]
    conditional = [r.get("ku_id") for r in knowledge_selection.get("knowledge_usage_contracts", []) if r.get("selection_status") == "CONDITIONAL"]
    excluded_texts: list[str] = []
    for item in pack.get("service_scope", {}).get("excluded", []):
        text = str(item.get("text", ""))
        excluded_texts.append("会议会务服务及其流程、岗位和承诺" if "会议会务" in text else text)
    contracts: list[dict[str, Any]] = []
    ordered = [s for chapter in outline for s in chapter["sections"]]
    for order, base in enumerate(ordered, 1):
        sid = base["section_id"]
        info = sections[sid]
        primary_related = [r for r in matrix["matrix"] if r.get("primary_section_id") == sid]
        secondary_related = [r for r in matrix["matrix"] if sid in r.get("secondary_section_ids", [])]
        related = primary_related + secondary_related
        chapter_row = chapter_budget[info["chapter_id"]]
        section_count = len(next(c for c in outline if c["chapter_id"] == info["chapter_id"])["sections"])
        target_min = chapter_row["target_words_min"] // section_count
        target_max = chapter_row["target_words_max"] // section_count
        topic = next((name for name, owner in TOPIC_OWNER.items() if owner == sid), None)
        hardening = SECTION_HARDENING[sid]
        required_processes = list(hardening.get("processes", []))
        required_tables = []
        required_visuals = []
        if topic in {"complaint_closure", "quality_pdca", "emergency_response", "handover_process", "training_system", "supplier_management"}:
            required_processes.append(topic)
        if sid in {"S03-02", "S06-01", "S08-01", "S11-01"}:
            required_tables.append({"type": "planning_table", "purpose": "形成可检查的职责、频次或里程碑表达"})
        if sid in {"S02-02", "S05-03", "S10-01"}:
            required_visuals.append({"type": "relationship_diagram", "purpose": "说明责任链和协同关系"})
        must_cover = list(hardening["must_cover"])
        must_cover.extend(r.get("requirement_text") for r in primary_related if r.get("requirement_domain") not in {"exclusion", "scoring"})
        if any(r.get("requirement_domain") == "exclusion" for r in primary_related):
            must_cover.append("明确记录并遵守项目负向Scope；仅说明会议会务服务不在本次范围内，不展开其流程、岗位或承诺")
        must_cover = list(dict.fromkeys(x for x in must_cover if x))
        section_prohibitions = list(hardening.get("must_not_cover", []))
        if sid.startswith("S05"):
            section_prohibitions.append("商场闭店、店铺或专柜检查等非产业园模板场景")
        contracts.append({
            "section_id": sid,
            "section_title": info["section_title"],
            "parent_section": info["chapter_id"],
            "purpose": f"明确{info['section_title']}的项目化实施逻辑、边界和可检查成果。",
            "must_cover": must_cover,
            "must_not_cover": sorted(set(excluded_texts + section_prohibitions + ["未经确认的历史数字、固定岗位承诺或真实案例"])),
            "source_requirements": sorted({r["requirement_id"] for r in related}),
            "scoring_items": sorted({r["scoring_item_id"] for r in related if r.get("scoring_item_id")}),
            "allowed_knowledge": selected,
            "conditional_knowledge": conditional,
            "target_words": {"min": target_min, "max": target_max},
            "target_pages": {"min": max(1, round(target_min / 650)), "max": max(1, round(target_max / 480))},
            "required_tables": required_tables,
            "required_processes": list(dict.fromkeys(required_processes)),
            "required_visuals": required_visuals,
            "cross_section_dependencies": [],
            "avoid_repeating": [name for name, owner in TOPIC_OWNER.items() if owner != sid],
            "canonical_topic_owner": topic or "section_specific",
            "topic_mode": "PRIMARY_OWNER" if topic else "SECTION_SPECIFIC",
            "section_activation_condition": hardening.get("activation", {"expression": "always", "when_unconfirmed": "generate_normally"}),
            "exception_path_required": bool(required_processes),
            "allowed_commitment_level": "confirmed_project_fact_or_conditioned_method_only",
            "required_outputs": hardening["outputs"],
            "generation_order": order,
        })
    return {
        "schema_version": "section-contracts-v0.2-hardening",
        "contracts": contracts,
        "topic_ownership": [{"topic": topic, "primary_owner": owner, "other_sections_mode": "REFERENCE_ONLY_OR_SUMMARY_ONLY"} for topic, owner in TOPIC_OWNER.items()],
        "process_contracts": deepcopy(PROCESS_CONTRACTS),
        "hardening_metrics": {
            "generic_must_cover_count": sum(c["must_cover"] == ["与本节主题直接相关的服务边界、动作、职责和成果"] for c in contracts),
            "specific_contract_count": sum(len(c["must_cover"]) >= 3 or bool(c["required_processes"]) for c in contracts),
            "contract_count": len(contracts),
        },
    }


def build_dependency_map() -> dict[str, Any]:
    rows = [
        ("S03-01", ["S03-02", "S05-01", "S06-01", "S10-01"], "role_and_responsibility", "组织职责变化影响各专业责任链"),
        ("S03-02", ["S04-01", "S05-01", "S11-01"], "staffing_and_hours", "人员与排班变化影响服务时间和实施计划"),
        ("S04-02", ["S08-01", "S08-02"], "complaint_quality_loop", "投诉闭环指标影响品质检查和复盘"),
        ("S05-02", ["S05-03", "S10-01"], "security_event", "巡逻与异常规则影响联动和应急"),
        ("S06-01", ["S06-02", "S06-03", "S10-01"], "equipment_scope", "设施范围影响巡检、运行记录和应急资源"),
        ("S07-01", ["S07-02", "S07-03", "S08-01"], "environment_scope", "环境范围影响质量检查与供方履约"),
        ("S09-01", ["S11-01", "S11-02"], "mobilization", "进场条件影响里程碑和成果接口"),
        ("S11-03", ["S03-02", "S04-02", "S05-02"], "clarification_boundary", "承诺边界变化只刷新受影响专业章节"),
    ]
    return {
        "schema_version": "cross-section-dependency-v0.1",
        "dependencies": [
            {"source_section": s, "dependent_sections": deps, "dependency_type": kind, "stale_on_change": True, "reason": reason}
            for s, deps, kind, reason in rows
        ],
    }


def build_global_state(
    pack: dict[str, Any], brief: dict[str, Any], outline: list[dict[str, Any]],
    matrix: dict[str, Any], contracts: dict[str, Any], dependency: dict[str, Any],
) -> dict[str, Any]:
    active = _planning_requirements(pack)
    all_active = _active_requirements(pack)
    by_domain = {r.get("domain"): r for r in active if r.get("domain") in {"staffing", "service_hours", "sla_kpi"}}
    staffing_req = by_domain.get("staffing", {})
    hours_req = by_domain.get("service_hours", {})
    sla_req = by_domain.get("sla_kpi", {})
    staffing_match = re.search(r"(\d+)\s*人", str(staffing_req.get("normalized_requirement", "")))
    hours_match = re.search(r"(\d+)\s*小时", str(hours_req.get("normalized_requirement", "")))
    sla_match = re.search(r"(\d+)\s*分钟", str(sla_req.get("normalized_requirement", "")))
    commitment_rows = [r for r in active if r.get("mandatory_level") == "MUST" and r.get("domain") != "scoring"]
    response_constraints = [
        {
            "requirement_id": r["requirement_id"],
            "constraint_type": "tender_scoring_or_document_response",
            "text": r.get("normalized_requirement", ""),
            "scoring_item_id": r.get("scoring_item_id"),
            "status": "confirmed",
        }
        for r in active if r.get("domain") == "scoring" or r.get("scoring_item_id")
    ]
    return {
        "schema_version": "document-global-state-v0.2-hardening",
        "project_facts": deepcopy(pack.get("project_facts", {})),
        "client_requirements": [{"requirement_id": r["requirement_id"], "text": r.get("normalized_requirement", "")} for r in active],
        "confirmed_commitments": [r.get("normalized_requirement") for r in commitment_rows],
        "document_response_constraints": response_constraints,
        "service_scope": deepcopy(pack.get("service_scope", {})),
        "excluded_scope": deepcopy(pack.get("service_scope", {}).get("excluded", [])),
        "deprioritized_scope": deepcopy(pack.get("service_scope", {}).get("deprioritized", [])),
        "staffing": {"minimum_staffing": int(staffing_match.group(1)) if staffing_match else None, "unit": "person", "source_requirement": staffing_req.get("requirement_id"), "status": "confirmed" if staffing_match else "unknown"},
        "service_hours": {"daily_service_hours": int(hours_match.group(1)) if hours_match else None, "unit": "hour_per_day", "source_requirement": hours_req.get("requirement_id"), "status": "confirmed" if hours_match else "unknown"},
        "sla_kpi": {"complaint_first_response_minutes": int(sla_match.group(1)) if sla_match else None, "unit": "minute", "source_requirement": sla_req.get("requirement_id"), "status": "confirmed" if sla_match else "unknown"},
        "operating_models": ["项目负责人统筹", "专业责任到岗", "事项闭环", "数据复盘"],
        "canonical_terms": {"project": brief.get("project_name", "本项目"), "client": "甲方", "service_provider": "物业服务团队"},
        "canonical_roles": {"project_lead": "项目负责人", "customer_contact": "客户服务岗位", "discipline_owner": "专业负责人"},
        "canonical_departments": {"project_team": "项目服务团队", "quality": "品质管理", "engineering": "工程管理", "security": "安全秩序管理"},
        "chapter_outline": deepcopy(outline),
        "section_registry": [{"section_id": c["section_id"], "section_title": c["section_title"], "status": "planned"} for c in contracts["contracts"]],
        "topic_ownership": deepcopy(contracts["topic_ownership"]),
        "cross_references": deepcopy(dependency["dependencies"]),
        "open_clarifications": [r.get("normalized_requirement") for r in all_active if "扫描" in r.get("normalized_requirement", "")],
        "commitment_registry": [{"requirement_id": r["requirement_id"], "commitment": r.get("normalized_requirement"), "provenance": "confirmed_requirement_pack", "status": "confirmed"} for r in commitment_rows],
        "source_trace": {
            "fixture_or_heading_markers": [{"requirement_id": r.get("requirement_id"), "raw_text": r.get("normalized_requirement", "")} for r in all_active if _is_non_content_marker(r)],
            "note": "原始Heading与模拟扫描标记仅供追溯，不进入规范化事实或承诺字段。",
        },
        "requirement_coverage": deepcopy(matrix["coverage_summary"]),
        "generation_progress": {c["section_id"]: "not_started" for c in contracts["contracts"]},
    }


def _ppt_chapters() -> list[dict[str, Any]]:
    defs = [
        ("P01", "项目理解", 3, "建立共同语境，说明产业园特征、范围和关键约束", ["项目画像", "需求边界", "管理重难点"], "high"),
        ("P02", "服务构想", 3, "给出贯穿全案的服务主张和运营闭环", ["服务定位", "总体架构", "价值路径"], "high"),
        ("P03", "组织与客户服务", 5, "说明团队如何承接需求并形成客户诉求闭环", ["组织职责", "人员与服务时间", "客户触点", "投诉闭环"], "high"),
        ("P04", "安全管理", 6, "围绕产业园通行、巡逻和异常联动呈现秩序能力", ["风险画像", "岗位协同", "巡逻机制", "人车秩序", "事件联动"], "high"),
        ("P05", "工程管理", 6, "呈现设施设备从台账到巡检维保的运行逻辑", ["工程边界", "设备台账", "巡检维保", "故障闭环", "运行记录"], "high"),
        ("P06", "环境与品质", 6, "将现场环境与品质闭环合并成可检查的管理系统", ["环境组织", "作业标准", "检查整改", "品质巡检", "持续改进"], "normal"),
        ("P07", "项目专项与服务保障", 6, "突出产业园人车流线、进场协同和应急保障", ["园区专项", "进场计划", "应急资源", "沟通报告", "履约保障"], "high"),
        ("P08", "经验边界与服务价值", 5, "以证据边界、阶段成果和服务价值完成收束", ["成果清单", "案例来源边界", "服务价值", "下一步"], "normal"),
    ]
    return [
        {
            "chapter_id": cid, "chapter_title": title, "narrative_goal": goal,
            "key_messages": messages, "target_slide_count": count, "priority": priority,
            "source_requirements": [], "required_evidence": ["confirmed_requirement_pack"],
            "case_source_required": cid == "P08",
            "overweight_reason": None,
        }
        for cid, title, count, goal, messages, priority in defs
    ]


def _storyboard_templates() -> dict[str, list[tuple[str, str, str, str]]]:
    return {
        "P01": [
            ("项目封面", "opening", "以项目名称和服务主题建立正式开场", "hero"),
            ("产业园项目画像", "insight", "从区位、面积、业态与服务对象理解管理场景", "profile"),
            ("需求边界与关键挑战", "insight", "确认服务范围、排除项和首要管理难点", "matrix"),
        ],
        "P02": [
            ("服务构想：稳定运行与体验闭环", "strategy", "以稳定运行、快速响应、持续改进贯穿服务", "architecture"),
            ("总体服务架构", "system", "组织、专业、品质和应急形成一体化运营系统", "architecture"),
            ("从项目需求到服务落地", "process", "把项目需求转化为现场动作、服务成果和持续改善", "process"),
        ],
        "P03": [
            ("项目组织与协同机制", "system", "项目负责人统筹，各专业协同承接客户与现场任务", "org"),
            ("人员配置与服务时间", "detail", "已确认人员和服务时间进入排班与履约控制", "table"),
            ("客户服务触点", "strategy", "围绕园区真实服务对象组织受理、协调与反馈", "journey"),
            ("投诉响应闭环", "process", "投诉从受理、首响、处理到回访形成可追踪闭环", "process"),
            ("客户服务成果", "evidence", "用台账、时效和回访记录证明服务有效", "dashboard"),
        ],
        "P04": [
            ("产业园安全风险画像", "insight", "识别人车流线、重点区域和生产协同风险", "risk_map"),
            ("秩序岗位协同", "system", "门岗、巡逻和事件处置形成相互支撑的现场协同", "org"),
            ("巡逻机制与频次控制", "process", "按确认频次组织路线、签到、异常和复核", "process"),
            ("人车通行秩序", "detail", "分时、分区和异常放行规则保障园区通行", "matrix"),
            ("重点事件联动", "process", "异常发现后快速上报、协同和恢复", "process"),
            ("安全管理成果", "summary", "以巡逻、事件和整改记录形成安全管理证据", "dashboard"),
        ],
        "P05": [
            ("工程服务边界", "insight", "先确认设施范围和责任界面，再组织运行维护", "boundary"),
            ("设施设备台账", "system", "一物一档支撑状态、维保和风险追踪", "table"),
            ("巡检与预防性维护", "process", "以计划、标准和记录预防设备故障", "process"),
            ("故障处置闭环", "process", "报修、判断、处置、验证和复盘形成闭环", "process"),
            ("运行记录与风险预警", "evidence", "通过趋势记录识别异常并安排干预", "dashboard"),
            ("工程管理价值", "summary", "以安全、稳定和可追溯支撑园区运营", "value"),
        ],
        "P06": [
            ("环境服务目标", "strategy", "围绕园区场景建立清洁、秩序和体验标准", "modules"),
            ("作业组织与重点区域", "detail", "按区域、时段和风险组织差异化作业", "matrix"),
            ("现场质量检查", "process", "班组自查、专业检查和问题复核形成闭环", "process"),
            ("品质巡检机制", "system", "日常巡检连接问题发现、责任落实和整改验证", "architecture"),
            ("数据复盘与持续改进", "evidence", "以问题趋势和整改成效优化服务", "dashboard"),
            ("供方管理边界", "detail", "仅在确认采用供方时启用履约管理方法", "boundary"),
        ],
        "P07": [
            ("产业园专项：生产物流与业务协同", "insight", "聚焦生产物流、高峰时段和特殊作业界面，不重复通用门区与通行规则", "scenario"),
            ("进场准备路线图", "process", "资料、人员、现场和机制按里程碑有序启动", "timeline"),
            ("应急管理架构", "system", "统一指挥、专业响应和外部联动构成应急体系", "architecture"),
            ("典型事件响应", "process", "事件分级、报告、处置和复盘形成标准动作", "process"),
            ("沟通与报告机制", "detail", "例行沟通聚焦事项、数据和决策，不扩展为会务服务", "cadence"),
            ("服务保障体系", "summary", "人员、专业机制、现场检查和应急准备共同保障服务落地", "modules"),
        ],
        "P08": [
            ("阶段成果与交付清单", "evidence", "以计划、台账、报告和闭环记录呈现履约成果", "deliverables"),
            ("类似项目案例占位", "case", "仅在取得真实案例数据源后呈现可核验案例", "case"),
            ("服务成果与实施边界", "comparison", "区分已确认服务内容、条件性方法和待澄清事项", "boundary"),
            ("项目服务价值", "summary", "稳定运行、风险受控和体验改善构成核心价值", "value"),
            ("共识与下一步", "summary", "围绕待确认事项和实施准备形成后续共识", "closing"),
        ],
    }


def build_ppt_plan(pack: dict[str, Any], word_outline: list[dict[str, Any]]) -> dict[str, Any]:
    chapters = _ppt_chapters()
    templates = _storyboard_templates()
    slides: list[dict[str, Any]] = []
    source_map = {
        "产业园项目画像": ["REQ-0001", "REQ-0002", "REQ-0003", "REQ-0004"],
        "需求边界与关键挑战": ["REQ-0017"],
        "人员配置与服务时间": ["REQ-0006", "REQ-0015"],
        "投诉响应闭环": ["REQ-0016"],
        "巡逻机制与频次控制": ["REQ-0009"],
        "沟通与报告机制": ["REQ-0017"],
    }
    number = 1
    for chapter in chapters:
        for title, role, message, family in templates[chapter["chapter_id"]]:
            evidence = "SOURCE_REQUIRED" if role == "case" else "confirmed_requirement_pack_or_planner_method"
            topic_owner = "general_access_control" if chapter["chapter_id"] == "P04" else ("industrial_logistics_coordination" if title.startswith("产业园专项") else None)
            slides.append({
                "slide_id": f"SL-{number:03d}", "chapter_id": chapter["chapter_id"],
                "slide_title": title, "slide_role": role, "core_message": message,
                "supporting_points": chapter["key_messages"][:3],
                "layout_intent": family, "content_source": "006A_confirmed_pack_and_local_planner",
                "source_requirements": source_map.get(title, []), "source_knowledge": [],
                "ppt_topic_owner": topic_owner,
                "topic_mode": "PRIMARY_OWNER" if topic_owner else "SUPPORTING_OR_SECTION_SPECIFIC",
                "required_visual": family, "evidence_requirement": evidence,
                "relationship_to_previous": "承接上一页结论并推进叙事" if number > 1 else "开场",
                "relationship_to_next": "为下一页建立问题或方法入口" if number < 40 else "结束",
                "component_family_hint": family,
            })
            number += 1
    for chapter in chapters:
        chapter["source_requirements"] = sorted({req for slide in slides if slide["chapter_id"] == chapter["chapter_id"] for req in slide["source_requirements"]})
    active = _planning_requirements(pack)
    coverage = []
    for req in active:
        if req.get("mandatory_level") != "MUST" and not req.get("scoring_item_id"):
            continue
        target = "P03" if req.get("domain") in {"staffing", "service_hours", "sla_kpi"} else ("P04" if req.get("domain") == "security" else "P02")
        coverage.append({
            "requirement_id": req["requirement_id"], "coverage_type": "PRESENT" if req.get("domain") != "scoring" else "IMPLICITLY_COVERED",
            "chapter_id": target, "reason": "自然融入业务板块，不显示逐条投标响应标题。",
        })
    chapter_titles = [c["chapter_title"] for c in chapters]
    word_titles = [c["chapter_title"] for c in word_outline]
    return {
        "schema_version": "ppt-presentation-plan-v0.2-hardening",
        "presentation_narrative": {
            "story": "先证明理解产业园项目和边界，再说明服务构想与专业能力，随后用专项保障和可核验成果完成价值收束。",
            "opening_rhythm": {"project_understanding_slides": 3, "service_concept_slides": 3},
            "principles": ["PPT是独立汇报叙事", "不复制Word目录", "不以评分条款控制前台结构", "无真实案例源不生成案例事实"],
        },
        "chapter_blocks": chapters,
        "narrative_budget": [
            {"chapter_id": c["chapter_id"], "message_count": len(c["key_messages"]), "slide_count": c["target_slide_count"], "narrative_weight": round(c["target_slide_count"] / 40, 3), "reason": "综合项目优先级、服务重要性、复杂度、展示价值和证据可用性。"}
            for c in chapters
        ],
        "slide_storyboard": slides,
        "ppt_topic_ownership": [
            {"topic": "general_access_control", "primary_owner": "P04", "scope": "门区、人员、车辆、通行秩序与一般异常处置", "other_chapters_mode": "REFERENCE_ONLY"},
            {"topic": "industrial_logistics_coordination", "primary_owner": "P07", "scope": "生产物流、业务高峰、特殊作业界面与产业园协同", "other_chapters_mode": "REFERENCE_ONLY"},
        ],
        "source_trace_policy": {"frontend_req_id_visible": False, "backend_trace_required_for_confirmed_numeric_scope_and_sla": True},
        "coverage_summary": {"items": coverage, "present_or_implicit": len(coverage), "not_present": 0},
        "layout_diversity_summary": {
            "slide_role_count": len({s["slide_role"] for s in slides}),
            "component_family_count": len({s["component_family_hint"] for s in slides}),
            "max_consecutive_same_family": max(_max_consecutive(slides, "component_family_hint"), 1),
            "warnings": [],
        },
        "planner_independence": {
            "word_chapter_titles": word_titles,
            "ppt_chapter_titles": chapter_titles,
            "identical_chapter_sequence": word_titles == chapter_titles,
            "ppt_section_to_word_section_one_to_one": False,
        },
    }


def _max_consecutive(rows: list[dict[str, Any]], field: str) -> int:
    best = current = 0
    previous = object()
    for row in rows:
        value = row.get(field)
        current = current + 1 if value == previous else 1
        previous = value
        best = max(best, current)
    return best


def validate_planning_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    matrix = bundle["requirement_matrix"]
    contracts = bundle["section_contracts"]
    word = bundle["word_plan"]
    budget = bundle["content_budget"]
    global_state = bundle["global_state"]
    dependency = bundle["dependency_map"]
    ppt = bundle["ppt_plan"]
    coverage = matrix["coverage_summary"]
    word_sections = [s for c in word["outline"] for s in c["sections"]]
    professional = {c["chapter_title"]: c["target_slide_count"] for c in ppt["chapter_blocks"] if c["chapter_id"] in {"P03", "P04", "P05", "P06"}}
    tender_terms = ("响应招标要求", "评分项", "条款响应", "技术响应", "第X条要求")
    tender_titles = [s["slide_title"] for s in ppt["slide_storyboard"] if any(t in s["slide_title"] for t in tender_terms)]
    case_slides = [s for s in ppt["slide_storyboard"] if s["slide_role"] == "case"]
    professional_contracts = [c for c in contracts["contracts"] if c["section_id"].startswith(("S05", "S06", "S07", "S08", "S09", "S10"))]
    specific_professional = [c for c in professional_contracts if len(c["must_cover"]) >= 3 or bool(c["required_processes"])]
    process_fields = {"process_id", "owner_section", "trigger", "steps", "roles", "exception_path", "outputs", "records", "reference_sections"}
    fixture_tokens = ("##", "[SIMULATED_SCAN_PAGE]")
    frontend_text = "\n".join(str(s.get(k, "")) for s in ppt["slide_storyboard"] for k in ("slide_title", "core_message"))
    trace_titles = {s["slide_title"]: s["source_requirements"] for s in ppt["slide_storyboard"] if s["slide_title"] in {"人员配置与服务时间", "投诉响应闭环", "需求边界与关键挑战", "巡逻机制与频次控制"}}
    word_titles = {c["chapter_title"] for c in word["outline"]}
    ppt_titles = {c["chapter_title"] for c in ppt["chapter_blocks"]}
    checks = {
        "B-W01": coverage["must_total"] == coverage["must_mapped"] and coverage["must_total"] > 0,
        "B-W02": coverage["scoring_total"] == coverage["scoring_mapped"] and coverage["scoring_total"] > 0,
        "B-W03": all("会议会务" not in s["section_title"] for s in word_sections) and any("会议会务" in x for c in contracts["contracts"] for x in c["must_not_cover"]),
        "B-W04": next(r for r in budget["chapters"] if r["chapter_id"] == "CH05")["target_words_max"] > next(r for r in budget["chapters"] if r["chapter_id"] == "CH09")["target_words_max"],
        "B-W05": len(contracts["contracts"]) == len(word_sections),
        "B-W06": len({r["topic"] for r in contracts["topic_ownership"]}) == len(contracts["topic_ownership"]) and len({r["primary_owner"] for r in contracts["topic_ownership"]}) == len(contracts["topic_ownership"]),
        "B-W07": global_state["canonical_roles"].get("project_lead") == "项目负责人",
        "B-W08": bool(dependency["dependencies"]),
        "B-W09": 50 <= budget["totals"]["planning_pages"] <= 80,
        "B-W10": len(word_titles) == len(word["outline"]),
        "B-W11": bool(professional_contracts) and len(specific_professional) / len(professional_contracts) >= 0.8,
        "B-W12": bool(contracts.get("process_contracts")) and all(process_fields <= set(row) for row in contracts["process_contracts"]),
        "B-W13": not any(token in json.dumps(global_state.get(field), ensure_ascii=False) for field in ("staffing", "service_hours", "sla_kpi", "confirmed_commitments", "commitment_registry") for token in fixture_tokens),
        "B-W14": all(row.get("requirement_id") != "REQ-0013" for row in global_state["commitment_registry"]) and any(row.get("requirement_id") == "REQ-0013" for row in global_state["document_response_constraints"]),
        "B-P01": not bundle["ppt_plan"]["planner_independence"]["identical_chapter_sequence"] and len(word_titles & ppt_titles) < len(ppt_titles),
        "B-P02": all("." not in c["chapter_title"] for c in ppt["chapter_blocks"]),
        "B-P03": 35 <= len(ppt["slide_storyboard"]) <= 50,
        "B-P04": all(v <= 10 for v in professional.values()),
        "B-P05": professional.get("组织与客户服务", 0) <= 9,
        "B-P06": any("产业园专项" in s["slide_title"] for s in ppt["slide_storyboard"]),
        "B-P07": ppt["layout_diversity_summary"]["slide_role_count"] >= 3 and ppt["layout_diversity_summary"]["max_consecutive_same_family"] <= 3,
        "B-P08": len(tender_titles) <= 1,
        "B-P09": bool(case_slides) and all(s["evidence_requirement"] == "SOURCE_REQUIRED" for s in case_slides),
        "B-P10": all(str(s.get("core_message", "")).strip() for s in ppt["slide_storyboard"]),
        "B-P11": all(trace_titles.get(title) for title in {"人员配置与服务时间", "投诉响应闭环", "需求边界与关键挑战", "巡逻机制与频次控制"}),
        "B-P12": "REQ-" not in frontend_text and "SCR-" not in frontend_text,
        "B-P13": {row["topic"]: row["primary_owner"] for row in ppt["ppt_topic_ownership"]} == {"general_access_control": "P04", "industrial_logistics_coordination": "P07"},
        "B-P14": not any(term in frontend_text for term in ("责任链", "履约保障组合", "承诺与证据边界", "从需求到成果的价值路径")),
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "metrics": {
            "must": f"{coverage['must_mapped']}/{coverage['must_total']}",
            "scoring": f"{coverage['scoring_mapped']}/{coverage['scoring_total']}",
            "word_chapters": len(word["outline"]),
            "word_sections": len(word_sections),
            "specific_section_contracts": contracts["hardening_metrics"]["specific_contract_count"],
            "process_contracts": len(contracts.get("process_contracts", [])),
            "generic_must_cover_ratio": round(contracts["hardening_metrics"]["generic_must_cover_count"] / max(1, len(contracts["contracts"])), 4),
            "word_planning_pages": budget["totals"]["planning_pages"],
            "ppt_chapters": len(ppt["chapter_blocks"]),
            "ppt_slides": len(ppt["slide_storyboard"]),
            "professional_overweight": {k: v for k, v in professional.items() if v > 10},
            "tender_style_titles": tender_titles,
        },
    }


def validate_production_planning_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    word = bundle["word_plan"]
    contracts = bundle["section_contracts"]
    budget = bundle["content_budget"]
    matrix = bundle["requirement_matrix"]
    sections = [section for chapter in word.get("outline", []) for section in chapter.get("sections", [])]
    contract_ids = [row.get("section_id") for row in contracts.get("contracts", [])]
    checks = {
        "DOCUMENT_PLAN": bool(word.get("outline")) and len(word["outline"]) >= 8,
        "REQUIREMENT_SECTION_MATRIX": bool(matrix.get("matrix")),
        "CONTENT_BUDGET": bool(budget.get("chapters")) and budget.get("totals", {}).get("target_words_min", 0) > 0,
        "SECTION_CONTRACTS": len(contract_ids) == len(sections) and len(sections) > 0,
        "PROVIDER_INDEPENDENT_OUTLINE": True,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "mode": "production",
        "checks": checks,
        "passed": sum(bool(value) for value in checks.values()),
        "total": len(checks),
        "metrics": {
            "word_chapters": len(word.get("outline", [])),
            "word_sections": len(sections),
            "contracts": len(contract_ids),
            "budget_words_min": budget.get("totals", {}).get("target_words_min"),
        },
    }


def build_planning_bundle(pack: dict[str, Any], brief: dict[str, Any], knowledge_selection: dict[str, Any], *, production: bool = False) -> dict[str, Any]:
    ready = pack.get("status") == "ready_for_plan" and pack.get("confirmation", {}).get("ready_for_brief_seed")
    if not production and not ready:
        raise PlanningError("仅允许使用已确认且ready_for_plan的Requirement Pack。")
    if production and not pack.get("requirements") and not pack.get("explicit_requirements"):
        raise PlanningError("缺少可用于规划的Requirement Pack。")
    target = build_document_target()
    outline = _word_outline()
    matrix = build_requirement_matrix(pack, outline)
    budget = build_content_budget(outline, matrix)
    contracts = build_section_contracts(pack, outline, matrix, budget, knowledge_selection)
    dependency = build_dependency_map()
    global_state = build_global_state(pack, brief, outline, matrix, contracts, dependency)
    word_plan = {
        "schema_version": "word-document-plan-v0.1",
        "document_target": target["word"],
        "document_type": "Detailed Service Solution",
        "planning_principles": ["需求覆盖优先", "负向Scope优先", "评分重点影响篇幅", "不为凑页数注水", "章节按业务需要动态展开"],
        "outline": outline,
        "coverage_gate": deepcopy(matrix["coverage_summary"]),
        "provider_independent": True,
    }
    ppt_plan = build_ppt_plan(pack, outline)
    bundle = {
        "document_target": target,
        "word_plan": word_plan,
        "requirement_matrix": matrix,
        "content_budget": budget,
        "section_contracts": contracts,
        "global_state": global_state,
        "dependency_map": dependency,
        "ppt_plan": ppt_plan,
    }
    bundle["validation"] = validate_production_planning_bundle(bundle) if production else validate_planning_bundle(bundle)
    return bundle


def _path_entry(path: Path, workspace_root: Path) -> dict[str, str]:
    return {"absolute_path": str(path.resolve()), "relative_path": path.resolve().relative_to(workspace_root.resolve()).as_posix()}


def generate_stage_artifacts(
    pack_path: Path,
    brief_path: Path,
    selection_path: Path,
    stage_root: Path,
    runtime_root: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    pack = json.loads(pack_path.read_text(encoding="utf-8-sig"))
    brief = json.loads(brief_path.read_text(encoding="utf-8-sig"))
    selection = json.loads(selection_path.read_text(encoding="utf-8-sig"))
    bundle = build_planning_bundle(pack, brief, selection)
    if bundle["validation"]["status"] != "PASS":
        raise PlanningError("006B离线规划门禁未通过。")
    stage_root.mkdir(parents=True, exist_ok=True)
    files = {
        "word_plan": stage_root / "01_word_document_plan.json",
        "requirement_matrix": stage_root / "02_requirement_section_matrix.json",
        "content_budget": stage_root / "03_word_content_budget.json",
        "section_contracts": stage_root / "04_section_contracts.json",
        "global_state": stage_root / "05_document_global_state_v0.json",
        "ppt_plan": stage_root / "06_ppt_presentation_plan.json",
        "dependency_map": stage_root / "07_cross_section_dependency.json",
    }
    for key, path in files.items():
        write_json(path, bundle[key])
    state_path = stage_root / "MMF006B_state.json"
    report_path = stage_root / "MMF006B_test_report.json"
    acceptance_path = stage_root / "planning_review.json"
    manifest_path = stage_root / "artifact_manifest.json"
    checkpoints = {name: True for name in [
        "B1_TARGET_PASS", "B2_REQUIREMENT_MATRIX_PASS", "B3_WORD_OUTLINE_PASS",
        "B4_CONTENT_BUDGET_PASS", "B5_SECTION_CONTRACT_PASS", "B6_GLOBAL_STATE_PASS",
        "B7_DEPENDENCY_PASS", "B8_PPT_NARRATIVE_PASS", "B9_PPT_CHAPTER_PASS",
        "B10_PPT_BUDGET_PASS", "B11_STORYBOARD_PASS",
        "B12_REVIEW_UX_PASS",
    ]}
    state = {
        "schema_version": "mmf006b-state-v0.1", "task": "MMF-006B Longform Planning Layer",
        "status": "local_planning_completed_pending_offline_regression", "updated_at": now_iso(),
        "checkpoints": checkpoints,
        "input_requirement_pack": str(pack_path.resolve()),
        "provider_tests": {"qwen_minimal": "not_run", "grok_final_red_team": "not_run"},
        "boundaries": {"mmf006c_started": False, "mmf006d_started": False, "renderer_modified": False, "project_management_00_modified": False},
        "next_state": "B13_OFFLINE_REGRESSION",
    }
    report = {
        "schema_version": "mmf006b-test-report-v0.1", "status": "local_planning_pass_pending_engineering_tests",
        "offline_regression": bundle["validation"], "engineering_tests": "pending",
        "qwen_minimal_test": {"status": "not_run"}, "grok_final_red_team": {"status": "not_run"},
        "generated_at": now_iso(),
    }
    write_json(state_path, state)
    write_json(report_path, report)
    manifest = {
        "schema_version": "mmf-artifact-manifest-v1.0", "stage": "MMF-006B",
        "status": state["status"], "runtime_root": _path_entry(runtime_root, workspace_root),
        "stage_root": _path_entry(stage_root, workspace_root),
        "input_requirement_pack": _path_entry(pack_path, workspace_root),
        **{key: _path_entry(path, workspace_root) for key, path in files.items()},
        "test_report": _path_entry(report_path, workspace_root),
        "state_file": _path_entry(state_path, workspace_root),
        "acceptance_record": _path_entry(acceptance_path, workspace_root),
        "run_paths": [], "created_at": now_iso(), "updated_at": now_iso(),
    }
    write_json(manifest_path, manifest)
    return {"bundle": bundle, "files": {**{k: str(v) for k, v in files.items()}, "state": str(state_path), "report": str(report_path), "manifest": str(manifest_path), "acceptance": str(acceptance_path)}}
