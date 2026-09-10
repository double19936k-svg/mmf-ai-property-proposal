from __future__ import annotations

import ast
import copy
import json
import re
from typing import Any
from .rules import load_rule
from .customer_hygiene import apply_hygiene_tree, hygiene_findings
from .text_sanitize import has_conditional_postprocess_pollution, rewrite_unconfirmed_durations

FORMAL_RULES = load_rule("formal_artifact_qa_v0.1.json")

SAFE_MAP = {"greasy": "油污", "internally": "内部", "internal": "内部", "本方案不预填": "具体参数以项目确认结果为准"}
ALLOWED_ENGLISH = {"word", "ppt", "sla", "kpi", "vip", "capex", "iot", "hvac", "sop", "oem", "iso", "led", "ups", "wifi", "pdf", "ok", "ceo", "cctv", "bpm"}
INTERNAL = re.compile(
    r"(?:Guardrail|Knowledge\s*Unit|治理规则|内部门禁|AI提示|安全说明|模型判断|provider|prompt|内部治理|"
    r"Document\s*Plan|MMF规划层|Content\s*Budget|Section\s*Contract|模型仅撰写|统一Document\s*Plan)",
    re.I,
)
INTERNAL_GOVERNANCE = re.compile(
    r"(?:事实边界|条件性方法|承诺边界|未确认.{0,24}(?:不作为|不得作为|不升级为).{0,12}承诺|"
    r"本方案(?:不预设|按照条件性方法表述)|不升级为既定承诺)",
    re.I,
)
TENDER_TEMPLATE = re.compile(
    r"(?:本授权书声明|授权委托书|法定代表人授权书|注册于[（(]国家或地区的名称[）)]|"
    r"[（(]公司名称[）)]|[（(]法人代表姓名[）)]|[（(]被授权人的姓名、?职务[）)]|"
    r"投标书格式|投标函格式|服务费报价单|外装信封|投标文件密封)",
    re.I,
)
JSON_DEBRIS = re.compile(r"(?:\{\s*[\"']\w+[\"']\s*:|```json|\[\s*\{\s*[\"'])", re.I)
PLACEHOLDER = re.compile(r"(?:TODO|TBD|待补充|这里填写|XXX)", re.I)
KU_ID = re.compile(r"KU-\d{4}(?:-[A-Z0-9]+)?", re.I)
ENGLISH = re.compile(r"\b[A-Za-z]{4,}\b")
TRUNCATED = re.compile(r"(?:清洁作业按|按照$|包括$|主要为$|如下[:：]?$)")
EMPTY_QUOTE = re.compile(r"[“‘][\s\u3000]*[”’]")
ADJACENT_QUOTE = re.compile(r"[”’][\s\u3000]*[“‘]")
AREA_VALUE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(万)?\s*(?:平方米|㎡)")
CORE_AREA_VALUE = re.compile(
    r"(?:总建筑面积|建筑面积合计|项目(?:总)?面积|物业服务面积|管理面积|总面积)\s*(?:为|约|共计|合计|[:：])?\s*"
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(万)?\s*(?:平方米|㎡)",
    re.I,
)


def normalize_quotes(text: str) -> str:
    """Remove empty/orphan quote marks without altering valid quoted phrases."""
    value = str(text or "")
    previous = None
    while value != previous:
        previous = value
        value = EMPTY_QUOTE.sub("", value)
        value = ADJACENT_QUOTE.sub("", value)
    for opening, closing in (("“", "”"), ("‘", "’")):
        stack: list[int] = []
        unmatched_closing: list[int] = []
        for index, char in enumerate(value):
            if char == opening:
                stack.append(index)
            elif char == closing:
                if stack:
                    stack.pop()
                else:
                    unmatched_closing.append(index)
        remove = set(stack + unmatched_closing)
        if remove:
            value = "".join(char for index, char in enumerate(value) if index not in remove)
    return value


def quote_issues(text: str) -> list[str]:
    issues: list[str] = []
    if EMPTY_QUOTE.search(text):
        issues.append("empty quote pair")
    if ADJACENT_QUOTE.search(text):
        issues.append("adjacent quote boundary")
    if text.count('"') % 2 or text.count("“") != text.count("”") or text.count("‘") != text.count("’"):
        issues.append("unbalanced quotes")
    return issues


def _area_number(value: str) -> float | None:
    match = AREA_VALUE.search(str(value or ""))
    if not match:
        return None
    number = float(match.group(1)) * (10000 if match.group(2) else 1)
    return round(number, 2)


def _source_area_numbers(source: dict[str, Any]) -> set[float]:
    """Collect area values explicitly present in confirmed source facts.

    A project can legitimately contain both a project-wide total and sub-building
    totals. The former check compared every phrase containing ``总建筑面积`` only
    with the project-wide value, so a confirmed sub-building fact was permanently
    misclassified as drift.
    """
    values: set[float] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
            return
        if isinstance(value, list):
            for child in value:
                collect(child)
            return
        if not isinstance(value, str):
            return
        for match in AREA_VALUE.finditer(value):
            number = float(match.group(1)) * (10000 if match.group(2) else 1)
            values.add(round(number, 2))

    # These collections are assembled from Todd-confirmed requirements by the
    # planning layer. Deliberately do not scan arbitrary generated/runtime data.
    for key in ("client_requirements", "confirmed_requirements", "source_trace"):
        collect(source.get(key))
    return values


def project_fact_conflicts(texts: list[str], canonical_facts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    expected: set[float] = set()
    source = canonical_facts or {}
    project_facts = source.get("project_facts") if isinstance(source, dict) else None
    if isinstance(project_facts, dict):
        for key in ("gross_area", "managed_area"):
            row = project_facts.get(key)
            raw = row.get("value") if isinstance(row, dict) else row
            parsed = _area_number(str(raw or ""))
            if parsed is not None:
                expected.add(parsed)
    for key in ("area", "gross_area", "managed_area"):
        raw = source.get(key) if isinstance(source, dict) else None
        parsed = _area_number(str(raw or ""))
        if parsed is not None:
            expected.add(parsed)
    if isinstance(source, dict):
        expected.update(_source_area_numbers(source))
    observed: set[float] = set()
    for text in texts:
        for match in CORE_AREA_VALUE.finditer(str(text or "")):
            number = float(match.group(1)) * (10000 if match.group(2) else 1)
            observed.add(round(number, 2))
    if expected:
        unexpected = sorted(value for value in observed if value not in expected)
        return [{"expected": sorted(expected), "observed": sorted(observed), "unexpected": unexpected}] if unexpected else []
    return [{"expected": [], "observed": sorted(observed), "unexpected": sorted(observed)}] if len(observed) > 1 else []


def _english_action(token: str) -> tuple[str, str]:
    low = token.lower()
    if low in SAFE_MAP:
        return "AUTO_REPAIR", SAFE_MAP[low]
    if low in ALLOWED_ENGLISH:
        return "PASS", token
    return "AUTO_REPAIR", ""


def flatten_structured_text(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if JSON_DEBRIS.search(text):
            parsed = None
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError, MemoryError):
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError, ValueError):
                    parsed = None
            if parsed is not None and parsed != text:
                return flatten_structured_text(parsed)
            text = re.sub(r"```json|```", "", text).strip()
        return [text] if text else []
    if isinstance(value, dict):
        lines: list[str] = []
        title = str(value.get("title") or value.get("heading") or "").strip()
        if title:
            lines.append(title)
        for key in ("items", "points", "content", "text", "paragraphs", "bullets"):
            child = value.get(key)
            if child is None or child == "" or child == title:
                continue
            lines.extend(flatten_structured_text(child))
        return lines
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(flatten_structured_text(item))
        return out
    text = str(value).strip()
    return [text] if text else []


def _walk_strings(value: Any, path: str = "artifact") -> list[tuple[str, str]]:
    # Structural renderer enums are backend instructions, not customer-visible copy.
    if isinstance(value, dict): return [x for k, v in value.items() if k != "layout" for x in _walk_strings(v, f"{path}.{k}")]
    if isinstance(value, list): return [x for i, v in enumerate(value) for x in _walk_strings(v, f"{path}[{i}]")]
    return [(path, value)] if isinstance(value, str) else []


def evaluate_artifact(generated: dict[str, Any], canonical_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    if not FORMAL_RULES.get("rules"):
        raise ValueError("Formal Artifact QA rules are empty")
    findings: list[dict[str, Any]] = []
    seen_paragraphs: dict[str, str] = {}
    walked = _walk_strings(generated.get("artifact", generated))
    for path, text in walked:
        if KU_ID.search(text): findings.append({"rule_id": "FAQ-005", "path": path, "severity": "BLOCK", "issue": "KU ID leaked into formal body"})
        if TRUNCATED.search(text.strip()): findings.append({"rule_id": "FAQ-009", "path": path, "severity": "BLOCK", "issue": "truncated sentence"})
        if JSON_DEBRIS.search(text): findings.append({"rule_id": "FAQ-004", "path": path, "severity": "BLOCK", "issue": "JSON debris"})
        if INTERNAL.search(text) or INTERNAL_GOVERNANCE.search(text) or "不套用其他项目" in text: findings.append({"rule_id": "CLIENT_ARTIFACT_INTERNAL_META", "path": path, "severity": "AUTO_REPAIR", "issue": "internal governance language"})
        if TENDER_TEMPLATE.search(text): findings.append({"rule_id": "TENDER_TEMPLATE_LEAK", "path": path, "severity": "BLOCK", "issue": "tender form/template leaked into customer artifact"})
        if PLACEHOLDER.search(text): findings.append({"rule_id": "FAQ-007", "path": path, "severity": "BLOCK", "issue": "placeholder"})
        for token in ENGLISH.findall(text):
            severity, _ = _english_action(token)
            if severity == "AUTO_REPAIR":
                findings.append({"rule_id": "FAQ-002", "path": path, "severity": "AUTO_REPAIR", "issue": f"unknown English token: {token}"})
        if "本方案不预填" in text: findings.append({"rule_id": "FAQ-005", "path": path, "severity": "AUTO_REPAIR", "issue": "reverse governance phrasing"})
        if has_conditional_postprocess_pollution(text):
            findings.append({"rule_id": "CONDITIONAL_POSTPROCESS_POLLUTION", "path": path, "severity": "AUTO_REPAIR", "issue": "conditionalization post-process polluted customer text"})
        findings.extend(hygiene_findings(text, path, severity="AUTO_REPAIR"))
        norm = re.sub(r"\s+", "", text)
        if len(norm) >= 18 and norm in seen_paragraphs: findings.append({"rule_id": "FAQ-008", "path": path, "severity": "AUTO_REPAIR", "issue": f"duplicate of {seen_paragraphs[norm]}"})
        else: seen_paragraphs[norm] = path
        for issue in quote_issues(text):
            findings.append({"rule_id": "QUOTE_BALANCE_QA", "path": path, "severity": "BLOCK", "issue": issue})
    for conflict in project_fact_conflicts([text for _path, text in walked], canonical_facts):
        findings.append({"rule_id": "PROJECT_FACT_CONSISTENCY", "path": "artifact", "severity": "BLOCK", "issue": "conflicting project area", **conflict})
    status = "BLOCK" if any(x["severity"] == "BLOCK" for x in findings) else ("AUTO_REPAIR" if findings else "PASS")
    return {"schema_version": "formal-artifact-qa-v0.1", "status": status, "findings": findings, "summary": {"block_count": sum(x["severity"] == "BLOCK" for x in findings), "auto_repair_count": sum(x["severity"] == "AUTO_REPAIR" for x in findings)}}


def merge_longform_qa(formal: dict[str, Any], depth: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(formal or {})
    extra = list(merged.get("findings") or [])
    if depth:
        extra.extend(depth.get("findings") or [])
        merged["longform_depth"] = depth
        if depth.get("status") == "BLOCK":
            merged["status"] = "BLOCK"
    merged["findings"] = extra
    merged["summary"] = {
        "block_count": sum(item.get("severity") == "BLOCK" for item in extra),
        "auto_repair_count": sum(item.get("severity") == "AUTO_REPAIR" for item in extra),
    }
    return merged


def apply_artifact_repairs(generated: dict[str, Any]) -> dict[str, Any]:
    repaired = copy.deepcopy(generated)
    seen: set[str] = set()
    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items() if k not in {"provider_metadata", "layout"}}
        if isinstance(value, list):
            out = []
            for item in value:
                fixed = walk(item)
                key = re.sub(r"\s+", "", fixed) if isinstance(fixed, str) else ""
                if key and len(key) >= 18 and key in seen: continue
                if key: seen.add(key)
                out.append(fixed)
            return out
        if isinstance(value, str):
            if JSON_DEBRIS.search(value):
                flattened = flatten_structured_text(value)
                value = "\n".join(flattened) if flattened else value
            for old, new in SAFE_MAP.items(): value = re.sub(re.escape(old), new, value, flags=re.I)
            value = re.sub(r"[^。；！]*不套用其他项目[^。；！]*[。；！]?", "", value)
            value = INTERNAL.sub("", value)
            def _replace_english(match: re.Match[str]) -> str:
                _, replacement = _english_action(match.group(0))
                return replacement
            value = ENGLISH.sub(_replace_english, value)
            value = rewrite_unconfirmed_durations(value)
            value = apply_hygiene_tree(value)
            value = re.sub(r" +", " ", value)
            value = re.sub(r"\s+([，。；：、）】])", r"\1", value)
            value = normalize_quotes(value)
            natural_replacements = {
                "本方案按照条件性方法表述，不升级为既定承诺": "相关措施结合项目实际条件实施",
                "未确认事项一律不作为承诺": "相关事项以双方确认的项目要求为准",
                "本方案不预设": "具体安排结合项目实际确定",
                "项目事实、条件性方法与待澄清事项的边界": "项目条件、实施建议与待确认事项的适用范围",
                "待澄清事项、风险与承诺边界": "待确认事项、风险及服务责任范围",
                "明确项目事实、承诺与条件性方法的区分": "明确项目条件、服务责任与实施建议的适用范围",
                "承诺边界清单": "服务责任与实施条件清单",
                "事实边界": "项目条件适用范围",
                "条件性方法": "结合实际条件的实施建议",
                "承诺边界": "服务责任与实施条件",
            }
            for old, new in natural_replacements.items():
                value = value.replace(old, new)
            value = re.sub(r"确保([^。；]{0,40})零故障运行", r"保障\1稳定运行并降低故障发生概率", value)
            return value.strip()
        return value
    if isinstance(repaired, dict) and "artifact" in repaired:
        repaired["artifact"] = walk(repaired["artifact"])
        return repaired
    return walk(repaired)
