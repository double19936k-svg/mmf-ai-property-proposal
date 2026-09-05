from __future__ import annotations

import copy
import re
from typing import Any
from .rules import load_rule

FORMAL_RULES = load_rule("commitment_provenance_rules_v0.1.json")

SEMANTIC_PATTERNS = {
    "C1": [r"\d+(?:\.\d+)?\s*(?:人|小时|分钟|次|天|%|％)", r"日清日洁|每天清扫|按日保洁|每日保洁|每日清扫"],
    "C2": [r"固定岗|专人持续值守|定点值守|常设岗亭|新增岗位|配备\d+人"],
    "C3": [r"采用外包模式|采用外委|外委作业|专业供方承担|委托第三方|采用自营"],
    "C4": [r"封闭管理|封闭式管理|封闭管控|门禁卡|外来人员禁止进入|访客证"],
    "C5": [r"项目经理批示|由项目经理审批|甲方批准|负责审批"],
    "C6": [r"必须.{0,12}(审批|上报|复盘)"],
    "C7": [r"费用与.{0,6}(结算|付款)挂钩|考核与付款挂钩|收费标准为|收费单价为"],
    "C8": [r"24\s*小时|全天候|昼夜不间断|7\s*[×xX*]\s*24|连续值守"],
    "C9": [r"系统将自动|平台统一实现|智能识别|实时监控无死角"],
    "C10": [r"依据公司制度|按现行政策|统一制度规定"],
}

RECOMMENDATION_PREFIX = re.compile(r"(?:建议|可考虑|可根据|结合.{0,12}条件|在.{0,16}情况下|如采用|若采用|是否采用|可按|如经确认|经确认后|确认后|与招标人确认|待确认)")


def _walk_strings(value: Any, path: str = "artifact") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [item for k, v in value.items() for item in _walk_strings(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [item for i, v in enumerate(value) for item in _walk_strings(v, f"{path}[{i}]")]
    return [(path, value)] if isinstance(value, str) else []


def _brief_supports(brief_text: str, matched: str) -> bool:
    normalized = re.sub(r"\s+", "", matched)
    compact = re.sub(r"\s+", "", brief_text)
    if not normalized or normalized not in compact:
        return False
    for found in re.finditer(re.escape(normalized), compact):
        context = compact[max(0, found.start() - 14):found.end() + 14]
        if not re.search(r"(?:尚未确认|未确认|未知|待确认)", context):
            return True
    return False


def evaluate_commitments(brief: dict[str, Any], contracts: list[dict[str, Any]], generated: dict[str, Any]) -> dict[str, Any]:
    if len(FORMAL_RULES.get("claim_types", [])) != 10:
        raise ValueError("Formal Commitment rules must define C1-C10")
    brief_text = str(brief)
    violations: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    conditional_terms = "\n".join(c.get("frontend_conditional_phrasing", "") for c in contracts)
    for path, text in _walk_strings(generated.get("artifact", generated)):
        for claim_type, patterns in SEMANTIC_PATTERNS.items():
            for pattern in patterns:
                for match in re.finditer(pattern, text, re.I):
                    phrase = match.group(0)
                    supported = _brief_supports(brief_text, phrase)
                    recommendation = bool(RECOMMENDATION_PREFIX.search(text[max(0, match.start()-24):match.end()+12]))
                    conditional = recommendation or any(token and token in text for token in ("如采用", "若采用", "在确认", "可根据", "如经确认", "经确认后", "确认后", "招标人确认", "待确认", "尚未确认"))
                    provenance = "PROJECT_FACT" if supported else ("CONDITIONAL_KU" if conditional else "UNSUPPORTED")
                    language = "FACT" if supported else ("CONDITIONAL_METHOD" if conditional else "COMMITMENT")
                    row = {"claim_type": claim_type, "text": phrase, "path": path, "provenance": provenance, "language_level": language}
                    claims.append(row)
                    if not supported and not conditional:
                        repair = "RP-DOWNGRADE-RECOMMENDATION"
                        if claim_type in {"C2", "C3", "C4", "C7", "C8"}:
                            repair = "RP-CONDITIONALIZE"
                        violations.append({**row, "severity": "BLOCK", "repair_action": repair, "reason": "Plausibility is not evidence; current project support is absent."})
    unique = []
    seen = set()
    for row in violations:
        key = (row["path"], row["claim_type"], row["text"])
        if key not in seen:
            seen.add(key); unique.append(row)
    return {"schema_version": "commitment-provenance-gate-v0.1", "status": "BLOCK" if unique else "PASS", "claims": claims, "violations": unique, "repair_actions": list(dict.fromkeys(x["repair_action"] for x in unique))}


def _repair_text(text: str) -> str:
    replacements = {
        "采用外包模式": "如经确认使用外委方式，可按合同要求设置供方管理机制",
        "采用外委": "如经确认使用外委方式",
        "外委作业": "如经确认后的外委作业",
        "由专业供方承担日常作业": "如经确认使用外委方式，可由专业供方按合同承担相应作业",
        "实行封闭管理": "可结合园区开放条件评估分区管理方式",
        "封闭式管理": "分区管理",
        "外来人员禁止进入": "可按经确认的访客权限实施分区管控",
        "项目经理批示": "按当前项目确认的审批权限办理",
        "费用与结算挂钩": "履约结果可按当前合同约定用于管理评价",
        "考核与付款挂钩": "履约结果可按当前合同约定用于管理评价",
        "门禁卡": "经确认的人员通行凭证",
        "日清日洁": "按项目确认的保洁频次组织作业",
        "每天清扫": "按项目确认的保洁频次组织作业",
        "每日保洁": "按项目确认的保洁频次组织作业",
        "24小时值守": "可根据风险等级确定值守时段",
        "全天候监控": "可根据风险等级确定监控时段",
        "固定岗": "值守责任",
        "专人持续值守": "明确值守责任",
        "统一受理": "按确认路径受理",
        "响应时限": "响应安排",
        "到场时限": "到场安排",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def apply_local_repairs(generated: dict[str, Any]) -> dict[str, Any]:
    repaired = copy.deepcopy(generated)
    def walk(value: Any) -> Any:
        if isinstance(value, dict): return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list): return [walk(v) for v in value]
        if isinstance(value, str): return _repair_text(value)
        return value
    return walk(repaired)
