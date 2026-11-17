"""Context-aware customer-facing rewrite. Never splice unconfirmed numbers into 按项目确认."""

from __future__ import annotations

import re
from typing import Any

from .client_facing_prose import CLIENT_FACING_PROSE_RULE_ID, client_facing_prose
from .text_sanitize import collapse_repeated_conditionals, rewrite_unconfirmed_durations

HYGIENE_RULES = (
    "CONDITIONAL_REWRITE_GRAMMAR",
    "BROKEN_CROSS_REFERENCE",
    "BROKEN_FLOW_SERIALIZATION",
    "UNSUPPORTED_SYSTEM_CAPABILITY",
    "UNSUPPORTED_NUMERIC_THRESHOLD",
    "INTERNAL_GOVERNANCE_TONE",
    CLIENT_FACING_PROSE_RULE_ID,
)

_GRAMMAR_MARKERS = (
    "可根据项目确认要求确定响应安排",
    "通风按项目确认的安排标准",
    "不少于可根据项目确认",
    "会前可根据项目确认要求确定响应安排",
    "按项目确认的周期复盘主要聚焦于当月",
    "按项目确认的周期设施设备",
    "执行通风按项目确认",
)

_CROSS_REF_PATTERNS = (
    re.compile(r"相关章节、相关章节"),
    re.compile(r"相关章节明确"),
    re.compile(r"基于相关章节"),
    re.compile(r"品质章节"),
    re.compile(r"引用边界"),
    re.compile(r"(?<![A-Za-z0-9])S\d{2}-\d{2}(?![A-Za-z0-9])"),
)

_FLOW_BROKEN = re.compile(r"(?:—\s*){2,}|—{3,}|(?:-\s){2,}-")
_FLOW_OK = re.compile(r"数据记录—趋势分析—周期复盘")

_SYSTEM_PATTERNS = (
    re.compile(r"人脸识别"),
    re.compile(r"智能门禁"),
    re.compile(r"无感通行"),
    re.compile(r"生物识别"),
    re.compile(r"RFID"),
    re.compile(r"统一的?客户关系管理系统"),
    re.compile(r"系统后台自动记录通行轨迹"),
)
_SYSTEM_ALREADY_CONDITIONAL = re.compile(
    r"如项目已配置|可结合IC卡|建议通过统一客户服务台账|按现场已确认的通行核验|如已配置相应通行"
)

_THRESHOLD_PATTERNS = (
    re.compile(r"重复投诉超过三[次回]"),
    re.compile(r"投诉超过\d+次"),
    re.compile(r"超过三次"),
)

_TONE_PATTERNS = (
    re.compile(r"刚性基础"),
    re.compile(r"不作为考核依据"),
    re.compile(r"不构成对本项目的直接商业承诺"),
    re.compile(r"值守责任位编制"),
    re.compile(r"值守责任位的绝对承诺"),
    re.compile(r"不作为待澄清内容"),
    # §43：Run D 真实泄漏的那一句就在这一族里 —— Round 3 的 QA 认得它，
    # 但 hygiene 既不报也不改，于是"检出口"有、"修复口"没有，句子原样进了客户 DOCX。
    re.compile(r"不作为承诺前提"),
    re.compile(r"按未确认处理"),
    re.compile(r"写入正文"),
    re.compile(r"(?<![A-Za-z0-9])(?:CH\d{2}|CU-?\d{3,}|REQ-?\d{3,}|SCR-?\d{2,}|KU-?\d{4})(?![A-Za-z0-9])"),
    re.compile(r"TECHNICAL_ONLY_SCOPE_GATE|COMPLETED_CLEAN|SOURCE_REQUIRED|coverage_status|content_unit_id"),
)

_SECTION_TITLES = {
    "S03-02": "岗位履职安排",
    "S04-02": "客户投诉处理",
    "S05-02": "客户服务标准",
    "S06-02": "工程巡检维保",
    "S07-02": "清洁质量检查",
    "S08-01": "品质管理",
    "S11-03": "待确认事项",
}


# 后端字段的**值**是寻址口径或渲染器枚举，不是客户文案：`final_assembly` 量客户可见
# 文本时用的可见基座只有 title/body_blocks/tables/processes/callouts，这一批键都不在
# 里面。检测端与修复端共用这一条尺子。
#
# 为什么必须包含枚举键（Round 4 实测）：英文清洗器 `ENGLISH = \b[A-Za-z]{4,}\b` 会
# 把 `"paragraph"`、`"bullet_group"`、`"TABLE"`、`"provider_content_unit"` 一律清空，
# 于是落到磁盘的 fragment.json 里 `body_blocks[].type=""`——分点段在装配时被当成普通
# 段落，`content_unit_fragments[].archetype=""`——表格 CU 不再走表格槽位。同时
# `S03-01 → 相关专项安排` 把 `planned_content_unit_ids` 全部改成中文归类词，任何按
# CU 出处找回正文的动作（削减重建、晚到回收、§64 表格账）都会静默落空。
_STRUCTURAL_STEMS = (
    r"profile_p_contract|output_contract|image|images|image_topic|image_asset_id|image_width|image_height|image_source|image_side|scene_topic|image_caption|source_trace|expected_steps|topic_id|"
    r"section_id|chapter_id|content_unit_id|unit_id|requirement_id|"
    r"scoring_item_id|contract_id|table_id|process_id|volume_id|page_id|"
    r"slide_id|batch_id|attempt_id|job_id|task_id|record_id|node_id|"
    r"owner_section|reference_sections|source_requirements|layout|type|kind|mode|archetype|"
    r"generation_origin|execution_status|length_status|status|severity|finish_reason|"
    r"[a-z][a-z0-9_]*_ids|[a-z][a-z0-9_]*_refs|table_source_contract|source_table_findings|source_table_repairs|"
    r"table_governance|content_unit_execution|cu_structures_stripped|id"
)
_STRUCTURAL_KEY = re.compile(
    rf"(?:^|\.)(?:[a-z][a-z0-9_]*_)?(?:{_STRUCTURAL_STEMS})$"
)
# 列表下标只是路径的一部分，不参与键名判定：`...planned_content_unit_ids[7]` 仍然
# 是同一个字段的第 7 个值。
_PATH_INDEX = re.compile(r"(?:\[\d+\])+$")


def is_structural_path(path: str) -> bool:
    value = _PATH_INDEX.sub("", str(path or ""))
    return bool(_STRUCTURAL_KEY.search(value))


def _evidence_blob(*parts: Any) -> str:
    chunks: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    for part in parts:
        visit(part)
    return "\n".join(chunks)


def rewrite_customer_hygiene(text: str, *, evidence_text: str = "") -> str:
    value = rewrite_unconfirmed_durations(str(text or ""), include_bare_minutes=False)
    phrase_map = (
        (r"执行通风按项目确认的安排标准", "按双方确认的时长通风"),
        (r"通风按项目确认的安排标准", "按双方确认的时长通风"),
        (r"杯具及毛巾消毒不少于可根据项目确认要求确定响应安排", "杯具及毛巾按双方确认的时长消毒"),
        (r"不少于可根据项目确认要求确定响应安排", "时长由双方确认"),
        (r"会前可根据项目确认要求确定响应安排到岗迎候", "会前按双方确认的时点到岗迎候"),
        (r"会前可根据项目确认要求确定响应安排", "会前按双方确认的时点"),
        (r"茶叶等物料会前按项目确认的安排备妥", "茶叶等物料会前按双方确认的时点备妥"),
        (r"可根据项目确认要求确定响应安排", "由双方确认后实施"),
        (r"按项目确认的周期复盘主要聚焦于当月发生的典型问题、未关闭的遗留事项及关键指标波动情况", "项目可按照双方确认的周期开展复盘，重点分析阶段内发生的典型问题、未关闭事项及关键指标波动"),
        (r"建立按项目确认的周期、季度及年度复盘会议机制", "建立阶段性、季度及年度复盘会议机制"),
        (r"《按项目确认的周期设施设备维护保养计划》", "《设施设备周期维护保养计划》"),
        (r"按项目确认的周期设施设备维护保养计划", "设施设备周期维护保养计划"),
        (r"全生命周期计划与按项目确认的周期执行计划两层架构", "全生命周期计划与阶段性执行计划两层架构"),
        (r"按项目确认的周期计划则结合实际使用强度", "阶段性计划则结合实际使用强度"),
        (r"落到按项目确认的周期执行表中", "落到阶段性执行表中"),
        (r"按项目确认的周期评估", "阶段性评估"),
        (r"按项目确认的周期计划", "阶段性维护计划"),
        (r"按项目确认的周期", "双方确认的周期"),
        (r"按项目确认的安排标准", "双方确认的作业安排"),
        (r"按项目确认的安排", "双方确认后的安排"),
        (r"按项目确认的时长通风", "按双方确认的时长通风"),
        (r"按项目确认的时点", "双方确认的时点"),
        (r"按项目确认的时长", "双方确认的时长"),
        (r"按项目确认的服务时段", "合同约定的服务时段"),
        (r"按项目确认的频次", "合同约定的作业频次"),
        (r"按项目确认的指标", "合同约定的指标"),
        (r"相关章节、相关章节及相关章节等章节的最终落地", "后续专项安排的最终落地"),
        (r"相关章节、相关章节及相关章节等章节", "后续专项安排"),
        (r"升级至品质章节进行深度复盘", "升级至项目品质管理机制进行专项复盘"),
        (r"与品质章节保持明确的引用边界", "与项目品质管理记录保持对应关系"),
        (r"基于相关章节界定的工程服务边界", "基于已明确的工程服务边界"),
        (r"相关章节明确的设施设备台账", "已建立的设施设备台账"),
        (r"相关章节", "已明确的专项安排"),
        (r"品质章节", "项目品质管理"),
        (r"引用边界", "对应关系"),
        (r"客户重复投诉超过三次或客户明确表示不满，需自动升级至更高层级处理", "出现重复投诉、处理超时或客户持续不满意等情况时，应启动升级处理机制"),
        (r"客户重复投诉超过三次", "出现重复投诉"),
        (r"投诉在规定时间内未得到有效解决", "投诉处理超时"),
        (r"需在规定时间内进行回访", "应及时回访"),
        (r"在规定时间内完成相应的清洁作业或纠正措施", "应及时完成相应的清洁作业或纠正措施"),
        (r"要求供方在规定时间内完成整改", "要求供方及时完成整改"),
        (r"在规定时间内", "及时"),
        (r"内部员工及长期驻场人员依托智能门禁系统，通过人脸识别或IC卡实现无感通行，系统后台自动记录通行轨迹。", "如项目已配置智能门禁系统，可结合IC卡、人脸识别等方式核验内部员工及长期驻场人员通行，并按现场条件记录通行情况。"),
        (r"采用生物识别或RFID技术实现无感通行", "如项目已配置相应通行系统，可采用生物识别或RFID等方式核验通行"),
        (r"实现无感通行", "按现场已确认的通行核验方式放行"),
        (r"无感通行", "按现场已确认的通行核验方式通行"),
        (r"所有渠道受理的事项均需录入统一的客户关系管理系统，形成标准化的受理台账。", "建议通过统一客户服务台账或现有客户关系管理平台进行归口记录，形成标准化受理台账。"),
        (r"上述事实作为方案编制的刚性基础，不作为待澄清内容。", "上述已确认事实作为本方案编制前提。"),
        (r"值守责任位编制", "人员配置安排"),
        (r"值守责任位的绝对承诺", "未经确认的值守安排承诺"),
        (r"不构成对本项目的直接商业承诺。所有涉及服务响应安排、人员配置数量及外包服务范围的描述，均遵循「符合行业标准并满足项目实际需求」的原则。在未获得甲方书面确认前，此类内容不作为考核依据。", "具体人员配置、服务频次及量化指标，将在项目启动后结合合同约定和现场条件进一步确认。"),
        (r"不作为考核依据", "以合同约定和双方确认结果为准"),
        (r"不构成对本项目的直接商业承诺", "需结合合同约定和现场条件确认后实施"),
        # §43：把"这份文档怎么写"的内部口径改写成客户可读的承诺语态（拟/将/按合同确认）。
        # 三条按**具体到一般**排列，且输出不再含源串，所以重复调用是幂等的。
        (r"本方案按未确认处理，不作为承诺前提写入正文", "本方案不作预先设定，相关内容将在中标后按合同约定确认"),
        (r"不作为承诺前提写入正文", "不在本方案中预先设定"),
        (r"按未确认处理", "按待确认事项登记，以中标后书面确认为准"),
        (r"写入正文", "在本方案中列明"),
    )
    for pattern, repl in phrase_map:
        value = re.sub(pattern, repl, value)
    for sid, title in _SECTION_TITLES.items():
        value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(sid)}(?![A-Za-z0-9])", title, value)
    value = re.sub(r"(?<![A-Za-z0-9])S\d{2}-\d{2}(?![A-Za-z0-9])", "相关专项安排", value)
    # §43：其余内部标识符与门名/枚举一律换成客户可读指代，绝不留进成品。
    value = re.sub(r"(?<![A-Za-z0-9])(?:CH\d{2})(?![A-Za-z0-9])", "本章", value)
    value = re.sub(r"(?<![A-Za-z0-9])(?:CU-?\d{3,})(?![A-Za-z0-9])", "本项内容", value)
    value = re.sub(r"(?<![A-Za-z0-9])(?:REQ-?\d{3,})(?![A-Za-z0-9])", "本条需求", value)
    value = re.sub(r"(?<![A-Za-z0-9])(?:SCR-?\d{2,})(?![A-Za-z0-9])", "相关评分要求", value)
    value = re.sub(r"(?<![A-Za-z0-9])(?:KU-?\d{4}(?:-[A-Z0-9]+)?)(?![A-Za-z0-9])", "既有知识条目", value)
    value = re.sub(r"TECHNICAL_ONLY_SCOPE_GATE|COMPLETED_CLEAN|SOURCE_REQUIRED|SCOPE_EXCLUDED", "内部确认流程", value, flags=re.I)
    value = re.sub(r"(?:coverage_status|content_unit_id|requirement_id|section_id|scoring_item_id|target_words)",
                   "相关登记项", value, flags=re.I)
    value = _FLOW_BROKEN.sub("—", value)
    value = re.sub(r"确保([^。；]{0,40})零故障运行", r"通过预防性维护降低\1故障风险，提升运行稳定性", value)
    value = re.sub(r"所有人员资历证明已整理成册", "相关人员资历及证明材料应按招标要求另行提供", value)
    if not _SYSTEM_ALREADY_CONDITIONAL.search(value) and any(pattern.search(value) for pattern in _SYSTEM_PATTERNS):
        value = re.sub(r"采用生物识别或RFID技术实现无感通行", "如项目已配置相应通行系统，可采用生物识别或RFID等方式核验通行", value)
        value = re.sub(r"实现无感通行", "按现场已确认的通行核验方式放行", value)
        value = re.sub(r"无感通行", "按现场已确认的通行核验方式通行", value)
    value = collapse_repeated_conditionals(re.sub(r"\s{2,}", " ", value).strip())
    # MMF008A-C：最后一道是"内部推理 → 客户语态"的整句处置。放在这里（而不是各调用点各接一次），
    # 是因为修复端有三条入口（Artifact QA 的 `apply_hygiene_tree`、恢复编排、DOCX 段落级
    # `repair_docx_artifact`）——上一轮 §43 的教训正是"检出口有、修复口没有"，词表分头维护就
    # 一定会漏一条。整句语义与 KEEP 优先由 `client_facing_prose` 自己保证。
    value, _prose_actions = client_facing_prose(value)
    from governance.proposal_fidelity import clean_internal_artifacts
    return clean_internal_artifacts(value)


def hygiene_findings(text: str, path: str = "artifact", *, evidence_text: str = "", severity: str = "BLOCK") -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    value = str(text or "")
    # 结构化寻址值（artifact.sections[0].section_id 等）里的标识符是后端寻址口径，
    # 与 Artifact QA 对 CLIENT_ARTIFACT_INTERNAL_META 的路径豁免同一条尺子；
    # 只豁免"标识符派生"的两条规则，散文质量规则照常判。
    identifier_only = is_structural_path(path)
    if any(marker in value for marker in _GRAMMAR_MARKERS):
        findings.append({"rule_id": "CONDITIONAL_REWRITE_GRAMMAR", "path": path, "severity": severity, "issue": "conditional rewrite produced ungrammatical customer text", "repair_action": "REWRITE_DURATION_PHRASE", "scope": "sentence"})
    if not identifier_only and any(pattern.search(value) for pattern in _CROSS_REF_PATTERNS):
        findings.append({"rule_id": "BROKEN_CROSS_REFERENCE", "path": path, "severity": severity, "issue": "unresolved or repeated chapter reference in customer artifact", "repair_action": "REWRITE_CROSS_REFERENCE", "scope": "sentence"})
    if _FLOW_BROKEN.search(value) and not _FLOW_OK.search(value):
        findings.append({"rule_id": "BROKEN_FLOW_SERIALIZATION", "path": path, "severity": severity, "issue": "process/flow arrows collapsed into empty dashes", "repair_action": "NORMALIZE_FLOW", "scope": "sentence"})
    if not any(token in evidence_text for token in ("人脸识别", "智能门禁", "RFID", "生物识别")):
        if not _SYSTEM_ALREADY_CONDITIONAL.search(value) and any(pattern.search(value) for pattern in _SYSTEM_PATTERNS):
            findings.append({"rule_id": "UNSUPPORTED_SYSTEM_CAPABILITY", "path": path, "severity": severity, "issue": "system capability stated as current fact without evidence", "repair_action": "CONDITIONALIZE_SYSTEM", "scope": "sentence"})
    if any(pattern.search(value) for pattern in _THRESHOLD_PATTERNS) and "三次" not in evidence_text:
        findings.append({"rule_id": "UNSUPPORTED_NUMERIC_THRESHOLD", "path": path, "severity": severity, "issue": "numeric threshold is not in the requirement map", "repair_action": "REMOVE_THRESHOLD", "scope": "sentence"})
    if not identifier_only and any(pattern.search(value) for pattern in _TONE_PATTERNS):
        findings.append({"rule_id": "INTERNAL_GOVERNANCE_TONE", "path": path, "severity": "NEEDS_REVIEW" if severity == "BLOCK" else severity, "issue": "internal governance tone needs editorial review; no factual defect established", "repair_action": "NATURALIZE_TONE", "scope": "sentence"})
    return findings


def apply_hygiene_tree(value: Any, *, evidence_text: str = "") -> Any:
    return _hygiene_visit(value, evidence_text, False)


def _hygiene_visit(value: Any, evidence_text: str, structural: bool) -> Any:
    # structural 子树是后端寻址值（section_id / content_unit_id / …），QA 门按路径豁免，
    # 修复端必须同口径：否则把 "S03-02" 改成"岗位履职安排"会断掉覆盖映射与装配寻址。
    if structural:
        return value
    if isinstance(value, dict):
        from .table_context import visit_table
        contextual = visit_table(value, lambda item: _hygiene_visit(item, evidence_text, False), is_structural_path)
        if contextual is not None:
            return contextual
        return {key: _hygiene_visit(item, evidence_text, is_structural_path(str(key)))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_hygiene_visit(item, evidence_text, structural) for item in value]
    if isinstance(value, str):
        return rewrite_customer_hygiene(value, evidence_text=evidence_text)
    return value
