from __future__ import annotations

from typing import Any
from .rules import load_rule

FORMAL_SCHEMA = load_rule("knowledge_usage_contract_v0.1.schema.json")


def _contract(ku: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    ku_id = audit["ku_id"]
    status = audit["selection_status"]
    core = str(ku.get("core_knowledge", ""))
    required = list(audit.get("missing_preconditions", []))
    forbidden = ["不得把历史项目事实、数字、SLA或合同条件升级为当前项目承诺"]
    allowed = ["作为与当前需求匹配的方法、结构和业务动作参考"]
    frontend = ""
    question = ""
    backend = " AND ".join(required) if required else "preconditions_satisfied"
    language = "CONDITIONAL_METHOD" if status == "CONDITIONAL" or required else "RECOMMENDATION"
    if ku_id == "KU-9007-83FCB89D":
        core = "环境供方可用作业计划、现场台账、履约评价和资源备选机制连接管理闭环。"
        allowed = ["仅介绍外委情况下的供方治理方法"]
        required = ["环境服务采用外委方式"]
        forbidden += ["不得声明当前项目采用外包模式", "不得把考核与付款挂钩写成当前合同条款"]
        frontend = "环境服务采用外委方式时，可建立供方履约评价机制。"
        question = "目前尚未确认环境服务采用自营还是外委。是否加入‘采用外委方式时的供方管理方法’？"
    elif ku_id == "KU-9021-6AB7A5E7":
        core = "供方履约可按计划确认、现场纠偏、原因分析、问题归档和计划回写形成闭环。"
        allowed = ["在确认存在环境供方及合同要求后介绍履约闭环"]
        required = ["存在环境供方且合同/作业周期已确认"]
        forbidden += ["不得固定提前一周", "不得虚构月度或年度考核"]
        frontend = "如采用环境供方，可按合同周期建立计划确认、纠偏和复盘闭环。"
        question = "是否加入‘如采用环境供方时的履约管理方法’？"
    elif ku_id == "KU-9008-1DF2811A":
        core = core.replace("固定岗、巡逻岗", "值守与巡逻责任")
        forbidden += ["不得把固定岗、24小时值守或封闭管理写成既定配置"]
        allowed = ["使用分区分级、值守与巡逻责任、人防物防技防的方法骨架"]
    elif ku_id == "KU-9019-03A470D6":
        allowed = ["一般清洁顺序可直接参考；资料保护仅按条件启用"]
        forbidden += ["不得把资料禁看条款写成所有清洁场景的通用规定"]
        frontend = "如属于高信息安全区域，再按项目制度补充资料保护要求。"
    elif ku_id == "KU-9028-AAA62083":
        core = "报事可采用登记、分派、处理、反馈和复盘的简洁闭环。"
        allowed = ["仅保留与当前场景相连的简洁报事流程"]
        forbidden += ["不得保留项目经理批示作为默认审批路径", "不得扩写为园区客服专章"]

    must_conditionalize = status == "CONDITIONAL" or bool(required)
    backend = " AND ".join(required) if required else "preconditions_satisfied"
    return {
        "ku_id": ku_id,
        "usable_content": core,
        "selection_status": status,
        "allowed_usage": allowed,
        "required_conditions": required,
        "forbidden_escalations": list(dict.fromkeys(forbidden)),
        "must_conditionalize": must_conditionalize,
        "project_confirmation_required": status == "CONDITIONAL",
        "language_level": "CONDITIONAL_METHOD" if must_conditionalize else language,
        "backend_condition": backend,
        "frontend_conditional_phrasing": frontend,
        "human_confirmation_question": question,
        "body_visibility": "conditional_method" if must_conditionalize else ("full_method" if status == "SELECTED" else "mention_only"),
    }


def build_contracts(candidates: list[dict[str, Any]], selection_audit: list[dict[str, Any]]) -> list[dict[str, Any]]:
    required = set(FORMAL_SCHEMA.get("$defs", {}).get("ku_contract", {}).get("required", []))
    if required and not required.issubset(_contract({"ku_id": "probe"}, {"ku_id": "probe", "selection_status": "SELECTED", "missing_preconditions": []})):
        raise ValueError("Runtime KUC does not satisfy the formal schema")
    by_id = {str(x.get("ku_id") or x.get("knowledge_unit_id")): x for x in candidates}
    return [_contract(by_id[row["ku_id"]], row) for row in selection_audit if row["selection_status"] in {"SELECTED", "CONDITIONAL", "DEPRIORITIZED"} and row["ku_id"] in by_id]
