from __future__ import annotations

import hashlib
import re
from collections import Counter
from copy import deepcopy
from typing import Any

from qa_severity import (
    CLASS_FALSE_POSITIVE, CLASS_NEEDS_REVIEW, clause_of, semantic_status,
)


CONTENT_ARCHETYPES = (
    "NARRATIVE", "SOP", "PROCESS", "MATRIX", "CHECKLIST", "STANDARD_TABLE",
    "SCHEDULE", "RISK_REGISTER", "FORM_TEMPLATE", "KPI_TABLE",
    "RESPONSIBILITY_MATRIX", "EMERGENCY_PLAYBOOK",
)

REQUIREMENT_CLASSES = ("TECHNICAL", "COMMERCIAL", "QUALIFICATION", "LEGAL", "SCORING", "REFERENCE")
FACT_TYPES = (
    "CONFIRMED_PROJECT_FACT", "TENDER_REQUIREMENT", "BIDDER_CONFIRMED_FACT",
    "CONDITIONAL_RECOMMENDATION", "UNKNOWN",
)

# P0-0 TECHNICAL_ONLY_SCOPE_GATE：产品只交付技术标正文，因此每条 Requirement /
# Scoring Item / Tender Content 必须先落到七个范围类别之一。
TECHNICAL_SCOPE_CLASSES = (
    "TECHNICAL_INCLUDED",
    "TECHNICAL_REFERENCE",
    "COMMERCIAL_EXCLUDED",
    "QUALIFICATION_EXCLUDED",
    "PRICING_EXCLUDED",
    "LEGAL_PROCEDURAL_EXCLUDED",
    "BIDDER_CREDENTIAL_EXCLUDED",
)
EXCLUDED_TECHNICAL_SCOPE_CLASSES = tuple(
    name for name in TECHNICAL_SCOPE_CLASSES if name.endswith("_EXCLUDED")
)
TECHNICAL_SCOPE_DISPOSITION = {
    "TECHNICAL_INCLUDED": "INCLUDED",
    "TECHNICAL_REFERENCE": "REFERENCE",
    **{name: "EXCLUDED" for name in EXCLUDED_TECHNICAL_SCOPE_CLASSES},
}
SCOPE_ENFORCEMENT = {
    "excluded_items_create_section": False,
    "excluded_items_create_content_unit": False,
    "excluded_items_receive_page_budget": False,
    "excluded_items_render_word_table": False,
    "excluded_items_build_blank_attachment_page": False,
    "commercial_compliance_matrix_in_final_word": False,
    "commercial_compliance_matrix_role": "INTERNAL_RECORD_ONLY",
}
_LEGACY_EXCLUDED_CLASSES = {
    "COMMERCIAL", "QUALIFICATION", "LEGAL",
    "BID_FORM_TEMPLATE", "LEGAL_FORM_TEMPLATE", "AUTHORIZATION_TEMPLATE", "ADMINISTRATIVE_TEMPLATE",
}

_COMMERCIAL = re.compile(r"投标保证金|履约保证金|代理服务费|报价方式|最高限价|联合体|税率|付款|结算|报价")
_QUALIFICATION = re.compile(r"营业执照|资质|资格|信用|AAA|认证|ISO|业绩|案例|项目经理履历|证书|成立年限")
_LEGAL = re.compile(r"法律|法规|合同法|违约|争议|诉讼|仲裁|保密")
_SCORING = re.compile(r"评分|分值|评审|得分")
_BIDDER_CLAIM = re.compile(
    r"(?:我司|本公司|投标人)(?:已|曾|现)?(?:具备|拥有|取得|通过|获得|服务|建立|配置|配备)"
    r"[^。；\n]{0,60}(?:AAA|信用|ISO|认证|资质|资格|荣誉|奖项|专利|案例|业绩|履历|证书|分支机构|呼叫中心|400电话|自有平台|团队|人员)"
)
_PRICING_SCOPE = re.compile(r"报价|单价|总价|合价|限价|价格|金额|税率|税金|付款|结算|计费|酬金|元/月|元/平方米|下浮|物业收费项目|提高物业管理费|物业管理服务费|滞纳金")
_BIDDER_CREDENTIAL_SCOPE = re.compile(r"简历|履历|荣誉证书|获奖|奖项|类似项目业绩|业绩证明|证明材料|自有平台|呼叫中心|400电话|分支机构")
# §42：投标人自身的"清白声明"（未被列入黑名单/无重大违法记录）属资格性审查材料，不是
# 技术方案内容。判定必须带否定式声明形状，否则会把"建立供应商黑名单管理制度"这类
# 真正的技术条款误判成排除项。
_BIDDER_STATUS_DECLARATION = re.compile(
    r"(?:未被|未列入|不被列入|不得列入|不属于)[^。；]{0,20}(?:黑名单|失信名单|失信被执行|不良记录)"
    r"|(?:无|不存在)[^。；]{0,10}(?:重大违法记录|不良诚信记录)"
)
# §42：允许的唯一形状是**行内**轻量提示【待投标人确认：X】。它同时是"这里还缺投标人事实"的
# 机器可读标记，所以 Technical Review Summary 直接按这个形状收集，不去猜自然语言。
_BIDDER_CONFIRM_HINT = re.compile(r"【待投标人确认[^】]{0,80}】")
_QUALIFICATION_SCOPE = re.compile(r"营业执照|资质|资格|信用|AAA|认证|ISO|业绩|案例|成立年限|证书|财务|审计|纳税|社保|税收|资历|职称|执业资格|年以上")
_PROCEDURAL_SCOPE = re.compile(
    r"密封|装订|份数|正本|副本|签字盖章|签章|开标|评标|定标|中标|投标截止|投标有效期|招标编号|"
    r"递交投标文件|澄清|答疑|废标|无效投标|资格性\s*审查|符合性\s*审查|授权委托书|法定代表人|"
    r"踏勘|标前会|唱标|补遗|招标代理机构|初步评审标准|采购任务取消|"
    r"影响采购公正|电子文档和纸质文件不符"
)
_COMMERCIAL_CLAUSE_SCOPE = re.compile(
    r"投标保证金|履约保证金|代理服务费|联合体|商务标|商务文件|商务条款|商务部分|偏离表|偏差说明一栏|合同条款|发票|质保金|保函"
)
_TECHNICAL_SUBJECT = re.compile(
    r"物业|服务|管理|工程|保洁|清洁|秩序|安保|消防|客服|应急|预案|方案|人员|岗位|培训|"
    r"设施|设备|绿化|消杀|巡检|维保|维修|品质|投诉|报修|环境|通行|能耗|台账|交接|移交|进场|退场|排班"
    r"|餐饮|厨房|餐厅|食堂|供餐|食品|菜肴|餐具|清洗|刀具|器具|排水|污水|房屋外观"
)


def is_service_personnel_requirement(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("normalized_requirement", "requirement_text", "text", "label"))
    role = re.search(r"大厨|厨师|面点师|打荷|电工|项目(?:经理|主管)|(?:驻场|进驻|服务|工程|餐厅|食堂|管理服务|保洁|消防|秩序维护)人员", text)
    duty = re.search(r"须|应|必须|持有|持.{0,12}证|年龄|工作经验|上岗|培训|身体健康", text)
    evidence = re.search(r"营业执照|资格性审查|财务|审计|项目经理履历|简历|业绩证明|证明材料|复印件|得分|评分|我司已|本公司已|投标人(?:已|现|拥有|具备)", text)
    return bool(role and duty and not evidence and not row.get('scoring_item_id'))


def classify_requirement(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("normalized_requirement", "requirement_text", "text", "domain", "label"))
    if is_service_personnel_requirement(row):
        return "TECHNICAL"
    if _COMMERCIAL.search(text):
        return "COMMERCIAL"
    if _QUALIFICATION.search(text):
        return "QUALIFICATION"
    if _LEGAL.search(text):
        return "LEGAL"
    if row.get("scoring_item_id") or _SCORING.search(text):
        return "SCORING"
    if _TECHNICAL_SUBJECT.search(text):
        return "TECHNICAL"
    return "REFERENCE"


def _scope_blob(row: dict[str, Any]) -> str:
    # `scoring_leaf_text` 是评分表里这一格的原文（事项名 + 本行评分标准，见
    # `scoring_graph.response_text_for_item`）：范围判定与第二维判定因此读同一格文本，
    # 不是两套输入。没有结构图的行这个键缺席，行为与上一轮逐字相同。
    keys = ("normalized_requirement", "requirement_text", "text", "domain", "label", "requirement_type",
            "requirement_domain", "scoring_leaf_text")
    parts = [str(row.get(key) or "") for key in keys]
    heading = row.get("heading_path") or row.get("source_headings") or []
    if isinstance(heading, (list, tuple)):
        parts.extend(str(item) for item in heading)
    elif heading:
        parts.append(str(heading))
    return " ".join(parts)


def classify_technical_scope(row: dict[str, Any]) -> str:
    """把一条 Requirement/Scoring/Tender Content 归入七个技术标范围类别之一。"""
    structural = row.get('source_scoring_scope') or {}
    if (structural.get('basis') == 'SCORING_TABLE_VOLUME_CELL'
            and (structural.get('locator') or {}).get('table_id')
            and structural.get('scope_class')):
        return str(structural['scope_class'])
    blob = _scope_blob(row)
    kind = classify_requirement(row)
    if is_service_personnel_requirement(row):
        return "TECHNICAL_INCLUDED"
    if _BIDDER_CLAIM.search(blob) or (kind == "QUALIFICATION" and _BIDDER_CREDENTIAL_SCOPE.search(blob)) \
            or _BIDDER_STATUS_DECLARATION.search(blob):
        return "BIDDER_CREDENTIAL_EXCLUDED"
    # 保证金、联合体、偏离表等属商务条款；纯价格与结算口径才归入报价类。
    if _COMMERCIAL_CLAUSE_SCOPE.search(blob):
        return "COMMERCIAL_EXCLUDED"
    if _PRICING_SCOPE.search(blob):
        return "PRICING_EXCLUDED"
    if kind == "QUALIFICATION" or _QUALIFICATION_SCOPE.search(blob):
        return "QUALIFICATION_EXCLUDED"
    if kind == "LEGAL" or _PROCEDURAL_SCOPE.search(blob):
        return "LEGAL_PROCEDURAL_EXCLUDED"
    if kind == "COMMERCIAL":
        return "COMMERCIAL_EXCLUDED"
    if _is_technical_reference(row, blob):
        return "TECHNICAL_REFERENCE"
    return "TECHNICAL_INCLUDED"


def _is_technical_reference(row: dict[str, Any], blob: str) -> bool:
    if str(row.get("requirement_type") or "") == "project_fact" or row.get("tender_content_class") == "PROJECT_FACT":
        return True
    domain = str(row.get("requirement_domain") or row.get("domain") or "")
    if domain in {"exclusion", "contract_or_duration"}:
        return True
    if domain == "other" and not (_TECHNICAL_SUBJECT.search(blob) and re.search(
            r"负责|巡检|巡查|清洗|供餐|清扫|维护|保养|检查|交接|移交|登记异常", blob)):
        return True
    return not _TECHNICAL_SUBJECT.search(blob)


def technical_scope_disposition(scope_class: str) -> str:
    name = str(scope_class or "")
    if name in TECHNICAL_SCOPE_DISPOSITION:
        return TECHNICAL_SCOPE_DISPOSITION[name]
    # 兼容旧 requirement_class 与 006A tender_content_class 口径：这些类别在
    # Technical-Only 产品合同下同样属于"只登记、不进正文"。
    if name in _LEGACY_EXCLUDED_CLASSES:
        return "EXCLUDED"
    return "INCLUDED"


def is_excluded_technical_scope(scope_class: str) -> bool:
    return technical_scope_disposition(scope_class) == "EXCLUDED"


def build_requirement_classification(pack: dict[str, Any]) -> dict[str, Any]:
    source = pack.get("requirements") or pack.get("explicit_requirements") or []
    rows = []
    for item in source:
        if not isinstance(item, dict):
            continue
        kind = classify_requirement(item)
        scope_class = classify_technical_scope(item)
        rows.append({
            "requirement_id": item.get("requirement_id"),
            "requirement_class": kind,
            "scope_class": scope_class,
            "scope_disposition": technical_scope_disposition(scope_class),
            "text": item.get("normalized_requirement") or item.get("requirement_text") or item.get("text") or "",
            "scoring_item_id": item.get("scoring_item_id"),
        })
    counts = Counter(row["requirement_class"] for row in rows)
    scope_counts = Counter(row["scope_class"] for row in rows)
    return {
        "schema_version": "requirement-classification-v0.2",
        "allowed_classes": list(REQUIREMENT_CLASSES),
        "allowed_scope_classes": list(TECHNICAL_SCOPE_CLASSES),
        "rows": rows,
        "counts": {key: counts.get(key, 0) for key in REQUIREMENT_CLASSES},
        "scope_counts": {key: scope_counts.get(key, 0) for key in TECHNICAL_SCOPE_CLASSES},
    }


def build_technical_scope_gate(
    classification: dict[str, Any],
    *,
    matrix: dict[str, Any] | None = None,
    contracts: dict[str, Any] | None = None,
    scoring_items: list[dict[str, Any]] | None = None,
    scoring_graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """P0-0：把范围分类结果固化为可审计门控；EXCLUDED 只登记，不进入技术标结构。"""
    rows = [row for row in classification.get("rows") or [] if isinstance(row, dict)]
    scope_counts = Counter(str(row.get("scope_class") or "TECHNICAL_INCLUDED") for row in rows)
    register = [{
        "requirement_id": row.get("requirement_id"),
        "scoring_item_id": row.get("scoring_item_id"),
        "scope_class": row.get("scope_class"),
        "disposition": "EXCLUDED_FROM_TECHNICAL_BID",
        "text": row.get("text") or "",
        "recorded_only": True,
        "allowed_actions": ["IDENTIFY", "CLASSIFY", "RECORD"],
        "forbidden_actions": [
            "CREATE_SECTION", "CREATE_CONTENT_UNIT", "ALLOCATE_PAGE_BUDGET",
            "RENDER_WORD_TABLE", "BUILD_BLANK_ATTACHMENT_PAGE",
        ],
    } for row in rows if is_excluded_technical_scope(row.get("scope_class"))]
    scoring_rows = [row for row in rows if row.get("scoring_item_id")]
    scoring_scope = Counter(str(row.get("scope_class") or "TECHNICAL_INCLUDED") for row in scoring_rows)
    item_rows = [item for item in (scoring_items or []) if isinstance(item, dict)]
    from .scoring_source_scope import scoring_source_scope
    item_rows = [{**item, 'source_scoring_scope': scoring_source_scope(item, scoring_graph or {})}
                 for item in item_rows]
    scoring_item_register = [{
        "scoring_item_id": item.get("scoring_item_id"),
        "label": str(item.get("label") or item.get("text") or "")[:200],
        "weight": item.get("weight_numeric") if item.get("weight_numeric") is not None else item.get("weight"),
        "scope_class": classify_technical_scope(item),
        "disposition": technical_scope_disposition(classify_technical_scope(item)),
        "source_scoring_scope": item.get('source_scoring_scope') or {},
    } for item in item_rows]
    item_scope_counts = Counter(row["scope_class"] for row in scoring_item_register)
    technical_scoring_ids = sorted(
        str(row["scoring_item_id"]) for row in scoring_item_register
        if row["disposition"] != "EXCLUDED" and row.get("scoring_item_id")
    )
    excluded_scoring_ids = sorted(
        str(row["scoring_item_id"]) for row in scoring_item_register
        if row["disposition"] == "EXCLUDED" and row.get("scoring_item_id")
    )
    matrix_rows = [row for row in ((matrix or {}).get("matrix") or []) if isinstance(row, dict)]
    leaked_matrix_rows = [
        row.get("requirement_id") for row in matrix_rows
        if is_excluded_technical_scope(row.get("scope_class")) or is_excluded_technical_scope(row.get("requirement_class"))
    ]
    # must_cover 里既有招标条款原文，也有规划层自己的施工单话术；只对与排除条款
    # 逐字相同的条目判泄漏，避免把"待澄清事项""案例边界"这类内部措辞误判成范围越界。
    excluded_texts = {str(row.get("text") or "").strip() for row in register}
    excluded_texts.discard("")
    contract_rows = [row for row in ((contracts or {}).get("contracts") or []) if isinstance(row, dict)]
    leaked_contract_items = [
        f"{contract.get('section_id')}:{str(item)[:40]}"
        for contract in contract_rows
        for item in (contract.get("must_cover") or [])
        if str(item or "").strip() in excluded_texts
    ]
    enforcement_clean = not leaked_matrix_rows and not leaked_contract_items
    return {
        "schema_version": "technical-only-scope-gate-v0.1",
        "product_contract": "MMF generates technical bid documents only",
        "allowed_scope_classes": list(TECHNICAL_SCOPE_CLASSES),
        "disposition_by_class": dict(TECHNICAL_SCOPE_DISPOSITION),
        "scope_counts": {key: scope_counts.get(key, 0) for key in TECHNICAL_SCOPE_CLASSES},
        "totals": {
            "classified_rows": len(rows),
            "technical_included": int(scope_counts.get("TECHNICAL_INCLUDED", 0)),
            "technical_reference": int(scope_counts.get("TECHNICAL_REFERENCE", 0)),
            "excluded": len(register),
        },
        "scoring_item_scope_counts": {key: scoring_scope.get(key, 0) for key in TECHNICAL_SCOPE_CLASSES},
        "scoring_item_total": len(scoring_rows),
        "excluded_register": register,
        "excluded_scoring_item_ids": sorted({
            str(row["scoring_item_id"]) for row in register
            if row.get("scoring_item_id")
        }),
        "scoring_register": scoring_item_register,
        "scoring_register_scope_counts": {key: item_scope_counts.get(key, 0) for key in TECHNICAL_SCOPE_CLASSES},
        "scoring_register_total": len(scoring_item_register),
        "technical_scoring_item_ids": technical_scoring_ids,
        "technical_scoring_denominator": len(technical_scoring_ids),
        "excluded_scoring_register_ids": excluded_scoring_ids,
        "enforcement": dict(SCOPE_ENFORCEMENT),
        "enforcement_evidence": {
            "matrix_rows_total": len(matrix_rows),
            "matrix_rows_with_excluded_scope": leaked_matrix_rows,
            "section_contract_rows_total": len(contract_rows),
            "section_contracts_with_excluded_must_cover": leaked_contract_items,
            "sections_created_for_excluded_items": 0,
            "word_tables_created_for_excluded_items": 0,
            "blank_attachment_pages_created_for_excluded_items": 0,
        },
        "status": "TECHNICAL_ONLY" if enforcement_clean else "SCOPE_LEAK",
    }


TECHNICAL_BODY_SCOPE_RULES = (
    (
        "EXCLUDED_SCOPE_INTERNAL_MATRIX_REF",
        re.compile(r"商务响应矩阵|商务偏离表|商务响应表|符合性审查矩阵"),
        "AUTO_REPAIR",
        "内部分类名称出现在技术正文，技术标文件不应引用商务响应矩阵等内部门禁产物。",
    ),
    (
        "EXCLUDED_SCOPE_BODY_LEAK",
        re.compile(
            r"报价表|报价清单|投标报价|最高限价|单价与总价|投标保证金|履约保证金|代理服务费|"
            r"营业执照|资质证书复印件|ISO证书|AAA信用|财务审计报告|业绩合同|授权委托书|资信证明|商务标正文"
        ),
        "NEEDS_REVIEW",
        "商务、资信或报价类事项出现在技术正文，应只在内部门禁登记并由投标人另行响应。",
    ),
)

# 本案（Word v12 人工稿）已否定的表达与范围口径。与上面两条“商务/资信越界”规则
# 分开登记：那两条是任何项目的技术标都成立的边界，这一组只属于本案的写作口径；
# 单列也让原有“技术正文只有两条范围规则”的防回归断言保持原样。
TECHNICAL_BODY_V12_SCOPE_RULES = (
    # 只认“当作论述口径在用”的句子；表头里的九要素字段名（触发条件、接口方式）
    # 不是论述，不能因为列名就判缺陷。
    (
        "TECHNICAL_SCOPE_REJECTED_EXPRESSION",
        re.compile(
            r"不属于其职责|不属于本团队职责|"
            r"(?:触发条件)(?:为|是|包括|如下|设定为|确定为)|"
            r"(?:事件|事项|投诉|故障|风险|响应|升级)(?:的|之)?触发条件|"
            r"(?:接口方式)(?:为|是|包括|如下|采用|通过|约定为)|"
            r"(?:通过|采用|明确|约定|规定)(?:了|的)?接口方式"
        ),
        "NEEDS_REVIEW",
        "本案已否定的表达出现在技术正文，需改写为物业服务行业表述。",
    ),
    (
        "TECHNICAL_SCOPE_AGENCY_FOREGROUND",
        re.compile(
            r"(?:招标代理机构|招标代理公司|代理机构)(?:地址|负责|承担|组织|编制)|"
            r"(?:本项目|本方案|技术标)(?:由|经|委托)?(?:招标代理机构|代理机构)(?:负责|组织|编制)"
        ),
        "NEEDS_REVIEW",
        "技术正文不应突出招标代理机构；代理机构不是本方案的服务主体。",
    ),
    (
        "TECHNICAL_SCOPE_ADDRESS_LISTING",
        re.compile(
            r"(?:地址|办公地址|联系地址|项目地址)\s*[:：]\s*\S+|"
            r"[\u4e00-\u9fff]{2,}(?:路|街|大道)[\u4e00-\u9fff0-9]{0,8}号"
        ),
        "NEEDS_REVIEW",
        "技术正文不应列代理机构或项目地址；地址属于商务信息，不是服务方案内容。",
    ),
    (
        "TECHNICAL_SCOPE_EQUIPMENT_NUMBER",
        re.compile(
            r"(?:设备|机组|风机|水泵|配电柜|电梯|空调|水箱)\s*(?:编号|No\.?|NO\.?)\s*[:：]?\s*[A-Za-z]{0,4}[-_]?\d{2,}"
        ),
        "NEEDS_REVIEW",
        "无业务意义的设备编号应改为按点位或区域指代，保留必要的设备名称与位置。",
    ),
)
_TECHNICAL_BODY_REPLACEMENTS = {
    "商务响应矩阵": "商务文件",
    "商务偏离表": "商务文件",
    "商务响应表": "商务文件",
    "符合性审查矩阵": "自查记录",
}


def scan_scope_text(pairs: Any) -> list[dict[str, Any]]:
    """范围扫描核心：输入 (path, text) 序列，只记账不删段。"""
    findings: list[dict[str, Any]] = []
    for path, text in pairs:
        for rule_id, pattern, severity, issue in (*TECHNICAL_BODY_SCOPE_RULES, *TECHNICAL_BODY_V12_SCOPE_RULES):
            for match in pattern.finditer(str(text or "")):
                findings.append({
                    "rule_id": rule_id,
                    "path": path,
                    "severity": severity,
                    "issue": issue,
                    "text": match.group(0),
                    "occurrence_count": 1,
                })
    return findings


def scan_technical_body_scope(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """技术正文范围扫描：只在文本层面识别越界内容，不删除段落。"""
    return scan_scope_text(_walk_strings(artifact))


def sanitize_technical_body_scope_text(text: str) -> tuple[str, int]:
    """把内部分类名称替换为对外可用词；纯词条替换，本身幂等。"""
    value = str(text or "")
    changed = 0
    for term, replacement in _TECHNICAL_BODY_REPLACEMENTS.items():
        if term in value:
            changed += value.count(term)
            value = value.replace(term, replacement)
    return value, changed


def repair_technical_body_scope(artifact: dict[str, Any]) -> tuple[dict[str, Any], int]:
    repaired = deepcopy(artifact)
    count = 0

    def walk(value: Any) -> Any:
        nonlocal count
        if isinstance(value, dict):
            return {key: walk(child) for key, child in value.items()}
        if isinstance(value, list):
            return [walk(child) for child in value]
        if isinstance(value, str):
            fixed, changed = sanitize_technical_body_scope_text(value)
            count += changed
            return fixed
        return value

    return walk(repaired), count


def build_commercial_compliance_matrix(classification: dict[str, Any]) -> dict[str, Any]:
    included = {"COMMERCIAL", "QUALIFICATION", "LEGAL"}
    rows = [{
        "requirement_id": row.get("requirement_id"),
        "requirement_class": row.get("requirement_class"),
        "clause": row.get("text"),
        "tender_requirement": row.get("text"),
        "response_status": "PENDING_BIDDER_CONFIRMATION",
        "bidder_confirmation_required": True,
        "evidence_or_filling_location": "【待投标人确认：商务响应及证明材料位置】",
        "scoring_item_id": row.get("scoring_item_id"),
        "technical_body_policy": "COMPACT_REFERENCE_ONLY",
    } for row in classification.get("rows") or [] if row.get("requirement_class") in included]
    return {
        "schema_version": "commercial-compliance-matrix-v0.2",
        "rows": rows,
        "commercial_body_expansion": False,
        "scoring_links_preserved": all("scoring_item_id" in row for row in rows),
        # P0-0 §5：矩阵只作为内部门禁台账存在，不得渲染进最终技术 Word，
        # 也不得把行内容或"商务响应矩阵"这一名称注入 Provider Context Pack。
        "audience": "INTERNAL_ONLY",
        "final_word_included": False,
        "provider_context_included": False,
    }


def build_bidder_evidence_ledger(pack: dict[str, Any]) -> dict[str, Any]:
    source = pack.get("bidder_evidence") or pack.get("bidder_evidence_ledger") or []
    rows = []
    for index, item in enumerate(source, 1):
        if not isinstance(item, dict):
            continue
        rows.append({
            "evidence_id": item.get("evidence_id") or f"BE-{index:04d}",
            "type": item.get("type") or "BIDDER_CONFIRMED_FACT",
            "claim": item.get("claim") or "",
            "source": item.get("source") or "",
            "verified": bool(item.get("verified")),
            "valid_until": item.get("valid_until"),
            "usable_for": list(item.get("usable_for") or []),
            "artifact_ref": item.get("artifact_ref"),
        })
    return {
        "schema_version": "bidder-evidence-ledger-v0.1",
        "fact_types": list(FACT_TYPES),
        "rows": rows,
        "verified_evidence_ids": [row["evidence_id"] for row in rows if row["verified"]],
    }


def _walk_strings(value: Any, path: str = "artifact") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in _walk_strings(child, f"{path}.{key}")]
    if isinstance(value, list):
        return [item for index, child in enumerate(value) for item in _walk_strings(child, f"{path}[{index}]")]
    return [(path, value)] if isinstance(value, str) else []


def evaluate_bidder_evidence(artifact: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in ledger.get("rows") or [] if isinstance(row, dict)]
    verified_rows = [row for row in rows if row.get("verified")]
    findings = []
    suppressed = []
    claim_evidence_links = []
    hints: list[dict[str, str]] = []
    for path, text in _walk_strings(artifact):
        for match in _BIDDER_CONFIRM_HINT.finditer(text):
            hints.append({"path": path, "hint": match.group(0)})
        for match in _BIDDER_CLAIM.finditer(text):
            claim = match.group(0)
            matched = [
                row for row in verified_rows
                if claim and str(row.get("claim") or "")
                and (claim in str(row.get("claim")) or str(row.get("claim")) in claim)
            ]
            if matched:
                claim_evidence_links.append({
                    "path": path,
                    "claim": claim,
                    "evidence_ids": [row.get("evidence_id") for row in matched if row.get("evidence_id")],
                    "status": "VERIFIED",
                })
                continue
            if not claim:
                continue
            # §33：否定句不能当承诺。"投标人并不具备/不声称已取得资质" 与 "投标人已取得资质"
            # 命中同一条正则，把免责说明当成无证据的资信主张会凭空造出待复核缺陷。
            status = semantic_status(text, match.start(), match.end())
            if status in {"DENIED", "MENTION", "CONDITIONAL"}:
                suppressed.append({
                    "rule_id": "UNSUPPORTED_BIDDER_CLAIM",
                    "qa_class": CLASS_FALSE_POSITIVE,
                    "path": path,
                    "claim": claim,
                    "semantic_status": status,
                    "sentence": clause_of(text, match.start(), match.end()),
                    "classification_reason": f"negation_aware:{status}",
                })
                continue
            findings.append({
                "rule_id": "UNSUPPORTED_BIDDER_CLAIM",
                "severity": "NEEDS_REVIEW",
                "qa_class": CLASS_NEEDS_REVIEW,
                "path": path,
                "claim": claim,
                "semantic_status": status,
                "sentence": clause_of(text, match.start(), match.end()),
                "issue": "投标人能力陈述缺少已核验Bidder Evidence。",
                "recommended_placeholder": f"【待投标人确认：{claim}的有效证明材料】",
            })
    return {
        "schema_version": "bidder-evidence-gate-v0.3",
        "status": "NEEDS_REVIEW" if findings else "PASS",
        "negation_aware": True,
        # §40：本轮没有投标人材料是**正常的输入状态**，不是证据链坏了。空台账必须自己讲清楚，
        # 否则下游看到 verified_evidence_ids=[] 会把它读成数据缺陷，甚至去补一份假证据。
        "ledger_rows": len(rows),
        "ledger_verified_rows": len(verified_rows),
        "ledger_state": "SUPPLIED" if rows else "EMPTY_AS_SUPPLIED",
        "empty_ledger_is_normal": not rows,
        "empty_ledger_note": ("本轮未提供投标人资信材料：台账为空属正常，"
                              "涉及投标人事实的位置只输出【待投标人确认：…】轻量提示，"
                              "不得编造证明材料，也不得生成资信附件页。"
                              if not rows else "台账已提供材料，未核验条目仍按缺证据处理。"),
        # §42：轻量提示是**允许的**，但只能是行内一句话，不能扩成整页空白履历表。
        "confirmation_hint_count": len(hints),
        "confirmation_hints": hints[:40],
        "findings": findings,
        "suppressed": suppressed,
        "suppressed_count": len(suppressed),
        "claim_evidence_links": claim_evidence_links,
        "unsupported_claim_count": len(findings),
        "fail_soft": True,
        # §43：本门只把交付拉到待复核，永远不参与 TECHNICAL_FAILED 的推导。
        "delivery_effect": "缺投标人事实最多 NEEDS_REVIEW，不构成 TECHNICAL_FAILED。",
    }


def build_technical_review_summary(gate: dict[str, Any], *, excluded_scope_pages: int | None = None,
                                   credential_attachment_pages: int | None = None,
                                   credential_attachment_pages_mentioned: int | None = None,
                                   credential_attachment_page_details: list[dict[str, Any]] | None = None,
                                   page_basis: str = "NOT_MEASURED") -> dict[str, Any]:
    """§44：随件附一份**内部** Technical Review Summary，只说哪些技术事实仍待投标人确认。

    刻意不输出完整商务/资信清单：条目只从已渲染正文里取，来源是缺证据主张与行内轻量提示，
    越界范围命中只报数量不报内容 —— 把商务条目抄进这份文件等于再造一个资信清单。
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in gate.get("findings") or []:
        claim = str(finding.get("claim") or "").strip()
        if not claim or claim in seen:
            continue
        seen.add(claim)
        rows.append({
            "item": claim,
            "state": "PENDING_BIDDER_CONFIRMATION",
            "where": str(finding.get("path") or ""),
            "hint": str(finding.get("recommended_placeholder") or f"【待投标人确认：{claim}的有效证明材料】"),
        })
    for hint in gate.get("confirmation_hints") or []:
        token = str(hint.get("hint") or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        rows.append({
            "item": token.strip("【】"),
            "state": "PENDING_BIDDER_CONFIRMATION",
            "where": str(hint.get("path") or ""),
            "hint": token,
        })
    # §41 的负向断言必须由**实测**填：越界页含报价/商务/程序页，资信附件页只算
    # 证书·资质·业绩·履历那一组。硬写 False（或默认 0）等于把"没量过"写成"没生成"，
    # 所以没量过时保持 None，并把量的是哪一层单独报出来。
    return {
        "schema_version": "technical-review-summary-v0.1",
        "audience": "INTERNAL_ONLY",
        "title": "技术文件待确认事项汇总",
        "ledger_state": gate.get("ledger_state"),
        "ledger_empty_is_normal": bool(gate.get("empty_ledger_is_normal")),
        "gate_status": gate.get("status"),
        "delivery_effect": gate.get("delivery_effect"),
        "pending_confirmation_items": rows,
        "pending_confirmation_count": len(rows),
        "confirmation_hint_count": gate.get("confirmation_hint_count", 0),
        "unsupported_claim_count": gate.get("unsupported_claim_count", 0),
        "page_measurement_basis": page_basis,
        "excluded_scope_page_count": excluded_scope_pages,
        # 与逐页分类同口径：count 是"资信词在整页占多数"的附件页（§41 禁的就是这个形状），
        # mentioned 只说"这页提到过资信词"，details 给页号与命中分布，结论要能被逐页核回去。
        "credential_attachment_page_count": credential_attachment_pages,
        "credential_attachment_pages_mentioned": credential_attachment_pages_mentioned,
        "credential_attachment_page_details": [dict(row) for row in credential_attachment_page_details or []],
        "credential_attachment_page_rule": ("技术标正文不生成资信/证书/资质/业绩/履历附件页；count 只数"
                                            "资信词在整页占多数的页，顺带提到这些词的页记在 mentioned 里点名。"
                                            "计数字段来自真实渲染的逐页分类；None 表示本轮没量到。"),
        "commercial_or_qualification_checklist_included": False,
        "technical_body_only": True,
    }


def choose_archetype(section: dict[str, Any], contract: dict[str, Any], index: int) -> str:
    text = " ".join([
        str(section.get("section_title") or ""),
        *[str(x) for x in contract.get("must_cover") or []],
        *[str(x) for x in contract.get("required_outputs") or []],
    ])
    preferred = []
    if re.search(r"应急|突发|预案", text): preferred += ["EMERGENCY_PLAYBOOK", "RISK_REGISTER", "CHECKLIST"]
    if re.search(r"流程|闭环|处置|操作|巡检|维修", text): preferred += ["SOP", "PROCESS", "CHECKLIST"]
    if re.search(r"计划|排班|里程碑|培训", text): preferred += ["SCHEDULE", "STANDARD_TABLE", "CHECKLIST"]
    if re.search(r"职责|组织|责任|接口", text): preferred += ["RESPONSIBILITY_MATRIX", "MATRIX"]
    if re.search(r"台账|记录|表单|清单", text): preferred += ["FORM_TEMPLATE", "CHECKLIST", "STANDARD_TABLE"]
    if re.search(r"指标|考核|品质", text): preferred += ["KPI_TABLE", "MATRIX", "CHECKLIST"]
    cycle = preferred + ["NARRATIVE", "STANDARD_TABLE", "SOP", "CHECKLIST", "PROCESS", "MATRIX", "FORM_TEMPLATE", "RISK_REGISTER"]
    return cycle[(index - 1) % len(cycle)]


def archetype_budget(archetype: str, target_chars: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    target_chars = max(360, int(target_chars))
    if archetype in {"STANDARD_TABLE", "MATRIX", "KPI_TABLE", "RESPONSIBILITY_MATRIX", "RISK_REGISTER", "SCHEDULE"}:
        rows = max(4, min(18, target_chars // 120))
        cols = 4 if archetype != "RESPONSIBILITY_MATRIX" else 5
        target = {"measure": "table", "effective_chars": target_chars, "rows": rows, "columns": cols, "min_cell_chars": 12}
    elif archetype in {"SOP", "PROCESS", "EMERGENCY_PLAYBOOK"}:
        target = {"measure": "steps", "effective_chars": target_chars, "steps": max(5, min(16, target_chars // 110))}
    elif archetype == "CHECKLIST":
        target = {"measure": "items", "effective_chars": target_chars, "items": max(6, min(24, target_chars // 80))}
    elif archetype == "FORM_TEMPLATE":
        target = {"measure": "fields", "effective_chars": target_chars, "fields": max(6, min(24, target_chars // 85))}
    else:
        target = {"measure": "effective_chars", "effective_chars": target_chars}
    minimum = deepcopy(target)
    maximum = deepcopy(target)
    for key, value in list(target.items()):
        if isinstance(value, int):
            minimum[key] = max(1, int(value * 0.8))
            maximum[key] = max(value, int(value * 1.2))
    return minimum, target, maximum


def content_quality_report(artifact: dict[str, Any], project_type: str = "") -> dict[str, Any]:
    walked = [(path, text.strip()) for path, text in _walk_strings(artifact) if text.strip()]
    all_texts = [text for _path, text in walked]
    paragraphs = [
        text for path, text in walked
        if len(text) >= 24 and any(marker in path for marker in (".paragraphs[", ".body_blocks["))
    ]
    normalized = [re.sub(r"\s+|[，。；：、,.!?！？]", "", text) for text in paragraphs]
    counts = Counter(normalized)
    duplicate_count = sum(value - 1 for value in counts.values() if value > 1)
    structured_values = [
        text for path, text in walked
        if len(text) >= 40 and any(marker in path for marker in (".tables[", ".bullets[", ".processes["))
    ]
    structured_normalized = [re.sub(r"\d+|\s+|[，。；：、,.!?！？]", "", text) for text in structured_values]
    structured_counts = Counter(structured_normalized)
    structured_repetition = sum(value - 1 for value in structured_counts.values() if value > 1)

    # 用稳定的8字窗MinHash识别近重复正文。表格单元格、标题和元数据不参与，
    # 避免“责任/记录”等受控列名被误判为内容注水。
    signatures: list[tuple[int, int]] = []
    for text in normalized:
        canonical = re.sub(r"\d+", "#", text)
        if len(canonical) < 80:
            continue
        shingles = {canonical[index:index + 8] for index in range(len(canonical) - 7)}
        if not shingles:
            continue
        vector = [0] * 64
        for shingle in shingles:
            value = int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")
            for bit in range(64):
                vector[bit] += 1 if value & (1 << bit) else -1
        signature = sum(1 << bit for bit, score in enumerate(vector) if score >= 0)
        signatures.append((len(canonical), signature))
    semantic_repetition = 0
    for index, (length, signature) in enumerate(signatures):
        for other_length, other_signature in signatures[:index]:
            if min(length, other_length) / max(length, other_length) < 0.90:
                continue
            if (signature ^ other_signature).bit_count() <= 3:
                semantic_repetition += 1
                break
    fact_pattern = re.compile(
        r"(?:项目名称|项目地点|建筑面积|总建筑面积|合同期限|预算|最高限价)"
        r"[^，。；\n]{0,30}"
    )
    facts = [re.sub(r"\s+", "", match.group(0)) for text in all_texts for match in fact_pattern.finditer(text)]
    fact_counts = Counter(facts)
    # 同一Canonical Fact允许在封面、项目理解及关键边界处合理复用3次；超出后计低增量复述。
    fact_repetition = sum(max(0, value - 3) for value in fact_counts.values())
    leakage = 0
    if "产业园" not in str(project_type):
        leakage = sum("产业园" in text for text in all_texts)
    broken = sum(bool(re.search(r"—\s+—\s+—|[“‘]\s*[”’]|(.{2,12})\1\1", text)) for text in paragraphs)
    return {
        "schema_version": "content-budget-quality-gate-v0.1",
        "status": "DEPTH_GATE_FAIL" if (
            duplicate_count + semantic_repetition > max(2, len(paragraphs) // 20)
            or structured_repetition > max(5, len(structured_values) // 10)
            or fact_repetition > 3
        ) else ("NEEDS_REVIEW" if leakage or broken else "PASS"),
        "duplicate_paragraph_count": duplicate_count,
        "semantic_near_duplicate_count": semantic_repetition,
        "structured_low_gain_repetition_count": structured_repetition,
        "canonical_fact_low_gain_repetition_count": fact_repetition,
        "template_leakage_count": leakage,
        "broken_serialization_count": broken,
        "semantic_novelty_policy": "narrative_exact_and_stable_simhash_near_duplicates_excluded; structured_values_exact_low_gain_checked; canonical_fact_reuse_above_three_flagged",
    }


def repair_template_leakage(artifact: dict[str, Any], project_type: str = "") -> tuple[dict[str, Any], int]:
    repaired = deepcopy(artifact)
    count = 0
    def walk(value: Any) -> Any:
        nonlocal count
        if isinstance(value, dict):
            return {key: walk(child) for key, child in value.items()}
        if isinstance(value, list):
            return [walk(child) for child in value]
        if isinstance(value, str) and "产业园" not in str(project_type) and "产业园" in value:
            count += value.count("产业园")
            return value.replace("产业园", "本项目")
        return value
    return walk(repaired), count
