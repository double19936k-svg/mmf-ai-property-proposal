from __future__ import annotations

import copy
import re
from typing import Any
from .rules import load_rule

FORMAL_RULES = load_rule("formal_artifact_qa_v0.1.json")

SAFE_MAP = {"greasy": "油污", "internally": "内部", "internal": "内部", "本方案不预填": "具体参数以项目确认结果为准"}
ALLOWED_ENGLISH = {"word", "ppt", "sla", "kpi", "vip", "capex", "iot", "hvac", "sop", "oem", "iso", "led", "ups", "wifi", "pdf", "ok", "ceo", "cctv", "bpm"}
INTERNAL = re.compile(r"(?:Guardrail|Knowledge\s*Unit|治理规则|内部门禁|AI提示|安全说明|模型判断|provider|prompt|内部治理)", re.I)
JSON_DEBRIS = re.compile(r"(?:\{\s*[\"']\w+[\"']\s*:|```json|\[\s*\{\s*[\"'])", re.I)
PLACEHOLDER = re.compile(r"(?:TODO|TBD|待补充|这里填写|XXX)", re.I)
KU_ID = re.compile(r"KU-\d{4}(?:-[A-Z0-9]+)?", re.I)
ENGLISH = re.compile(r"\b[A-Za-z]{4,}\b")
TRUNCATED = re.compile(r"(?:清洁作业按|按照$|包括$|主要为$|如下[:：]?$)")


def _english_action(token: str) -> tuple[str, str]:
    low = token.lower()
    if low in SAFE_MAP:
        return "AUTO_REPAIR", SAFE_MAP[low]
    if low in ALLOWED_ENGLISH:
        return "PASS", token
    return "AUTO_REPAIR", ""


def _walk_strings(value: Any, path: str = "artifact") -> list[tuple[str, str]]:
    # Structural renderer enums are backend instructions, not customer-visible copy.
    if isinstance(value, dict): return [x for k, v in value.items() if k != "layout" for x in _walk_strings(v, f"{path}.{k}")]
    if isinstance(value, list): return [x for i, v in enumerate(value) for x in _walk_strings(v, f"{path}[{i}]")]
    return [(path, value)] if isinstance(value, str) else []


def evaluate_artifact(generated: dict[str, Any]) -> dict[str, Any]:
    if not FORMAL_RULES.get("rules"):
        raise ValueError("Formal Artifact QA rules are empty")
    findings: list[dict[str, Any]] = []
    seen_paragraphs: dict[str, str] = {}
    for path, text in _walk_strings(generated.get("artifact", generated)):
        if KU_ID.search(text): findings.append({"rule_id": "FAQ-005", "path": path, "severity": "BLOCK", "issue": "KU ID leaked into formal body"})
        if TRUNCATED.search(text.strip()): findings.append({"rule_id": "FAQ-009", "path": path, "severity": "BLOCK", "issue": "truncated sentence"})
        if JSON_DEBRIS.search(text): findings.append({"rule_id": "FAQ-004", "path": path, "severity": "BLOCK", "issue": "JSON debris"})
        if INTERNAL.search(text) or "不套用其他项目" in text: findings.append({"rule_id": "FAQ-005", "path": path, "severity": "AUTO_REPAIR", "issue": "internal governance language"})
        if PLACEHOLDER.search(text): findings.append({"rule_id": "FAQ-007", "path": path, "severity": "BLOCK", "issue": "placeholder"})
        for token in ENGLISH.findall(text):
            severity, _ = _english_action(token)
            if severity == "AUTO_REPAIR":
                findings.append({"rule_id": "FAQ-002", "path": path, "severity": "AUTO_REPAIR", "issue": f"unknown English token: {token}"})
        if "本方案不预填" in text: findings.append({"rule_id": "FAQ-005", "path": path, "severity": "AUTO_REPAIR", "issue": "reverse governance phrasing"})
        norm = re.sub(r"\s+", "", text)
        if len(norm) >= 18 and norm in seen_paragraphs: findings.append({"rule_id": "FAQ-008", "path": path, "severity": "AUTO_REPAIR", "issue": f"duplicate of {seen_paragraphs[norm]}"})
        else: seen_paragraphs[norm] = path
        if text.count('"') % 2 or text.count('“') != text.count('”'): findings.append({"rule_id": "FAQ-003", "path": path, "severity": "AUTO_REPAIR", "issue": "broken quotes"})
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
            for old, new in SAFE_MAP.items(): value = re.sub(re.escape(old), new, value, flags=re.I)
            value = re.sub(r"[^。；！]*不套用其他项目[^。；！]*[。；！]?", "", value)
            value = INTERNAL.sub("", value)
            def _replace_english(match: re.Match[str]) -> str:
                _, replacement = _english_action(match.group(0))
                return replacement
            value = ENGLISH.sub(_replace_english, value)
            value = re.sub(r" +", " ", value)
            value = re.sub(r"\s+([，。；：、）】])", r"\1", value)
            if value.count('“') > value.count('”'): value += '”'
            return value.strip()
        return value
    if isinstance(repaired, dict) and "artifact" in repaired:
        repaired["artifact"] = walk(repaired["artifact"])
        return repaired
    return walk(repaired)
