"""Fail-soft delivery state machine. QA findings stay recorded; delivery is separate."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

QA_PASS = "PASS"
QA_PASS_AFTER_REPAIR = "PASS_AFTER_REPAIR"
QA_WARNINGS_REMAIN = "WARNINGS_REMAIN"
QA_REVIEW_REQUIRED = "REVIEW_REQUIRED"
QA_CRITICAL = "CRITICAL_CONTENT_RISK"

DELIVERY_GENERATING = "GENERATING"
DELIVERY_CLEAN = "COMPLETED_CLEAN"
DELIVERY_WARNINGS = "COMPLETED_WITH_WARNINGS"
DELIVERY_REVIEW = "NEEDS_REVIEW"
DELIVERY_TECHNICAL = "TECHNICAL_FAILED"
DELIVERY_CANCELLED = "USER_CANCELLED"

MILD_RULES = {
    "CONDITIONAL_REWRITE_GRAMMAR",
    "INTERNAL_GOVERNANCE_TONE",
    "BROKEN_FLOW_SERIALIZATION",
    "CONDITIONAL_POSTPROCESS_POLLUTION",
    "CLIENT_ARTIFACT_INTERNAL_META",
}
REVIEW_RULES = {
    "UNSUPPORTED_COMPLETION_CLAIM",
    "ABSOLUTE_PERFORMANCE_COMMITMENT",
    "UNSUPPORTED_NUMERIC_THRESHOLD",
    "UNSUPPORTED_SYSTEM_CAPABILITY",
    "BROKEN_CROSS_REFERENCE",
    "EMPTY_HEADING_BLOCK",
    "INTERNAL_SECTION_ID",
    "BROKEN_QUOTE_SERIALIZATION",
    "QUOTE_BALANCE_QA",
    "PROJECT_FACT_CONSISTENCY",
    "CG-001",
    "CG-002",
    "CG-005",
}
PHYSICAL_RULES = {"FINAL_ARTIFACT_MISSING"}
SCOPE_BY_RULE = {
    "UNSUPPORTED_COMPLETION_CLAIM": "sentence",
    "ABSOLUTE_PERFORMANCE_COMMITMENT": "sentence",
    "UNSUPPORTED_NUMERIC_THRESHOLD": "sentence",
    "UNSUPPORTED_SYSTEM_CAPABILITY": "sentence",
    "BROKEN_CROSS_REFERENCE": "paragraph",
    "CONDITIONAL_REWRITE_GRAMMAR": "sentence",
    "INTERNAL_GOVERNANCE_TONE": "sentence",
    "BROKEN_FLOW_SERIALIZATION": "paragraph",
    "EMPTY_HEADING_BLOCK": "section",
    "INTERNAL_SECTION_ID": "sentence",
    "BROKEN_QUOTE_SERIALIZATION": "artifact",
    "QUOTE_BALANCE_QA": "artifact",
    "PROJECT_FACT_CONSISTENCY": "section",
    "FINAL_ARTIFACT_MISSING": "workflow",
}
ISSUE_LABELS = {
    "UNSUPPORTED_COMPLETION_CLAIM": "存在尚未完全核实的完成或附件表述",
    "ABSOLUTE_PERFORMANCE_COMMITMENT": "存在需要人工确认的绝对化表述",
    "UNSUPPORTED_NUMERIC_THRESHOLD": "存在尚未确认的量化门槛",
    "UNSUPPORTED_SYSTEM_CAPABILITY": "存在尚未在项目资料中确认的系统能力表述",
    "BROKEN_CROSS_REFERENCE": "章节交叉引用不完整",
    "EMPTY_HEADING_BLOCK": "部分标题下缺少正文",
    "INTERNAL_SECTION_ID": "残留内部章节编号",
    "BROKEN_QUOTE_SERIALIZATION": "部分引号格式可能影响阅读",
    "QUOTE_BALANCE_QA": "部分引号格式可能影响阅读",
    "PROJECT_FACT_CONSISTENCY": "局部项目事实存在疑点",
    "CONDITIONAL_REWRITE_GRAMMAR": "个别措辞仍不够自然",
    "INTERNAL_GOVERNANCE_TONE": "个别表述偏内部口径",
    "BROKEN_FLOW_SERIALIZATION": "个别流程符号不完整",
    "CONDITIONAL_POSTPROCESS_POLLUTION": "个别条件化表达不自然",
    "CLIENT_ARTIFACT_INTERNAL_META": "个别内部标记残留",
    "CG-001": "存在尚未确认的时长或频次",
    "LF-002": "部分章节内容仍不完整",
}
MANUAL_ACTIONS = {
    "UNSUPPORTED_COMPLETION_CLAIM": "请人工确认该完成或附件表述，或改为待提交材料说明。",
    "UNSUPPORTED_SYSTEM_CAPABILITY": "请按现场已确认的系统能力改写，或标明待项目确认。",
    "UNSUPPORTED_NUMERIC_THRESHOLD": "请删除未确认数字，或改为双方确认后的安排。",
    "BROKEN_CROSS_REFERENCE": "请改为可读的专项安排名称，避免空泛交叉引用。",
    "EMPTY_HEADING_BLOCK": "请为该标题补充正文，或删除空标题。",
    "BROKEN_QUOTE_SERIALIZATION": "请检查该段引号在 Word / WPS 中的显示。",
    "PROJECT_FACT_CONSISTENCY": "请核对项目面积、名称等已确认事实。",
}
INTERNAL_CODE = re.compile(r"\b(?:CG|LF|QR|FAQ|RP|ART)-[0-9]{2,4}\b|\b[A-Z]{3,}(?:_[A-Z0-9]+){1,}\b")
LAST_VALID_DIR = "last_valid_artifact"
LAST_VALID_META = "last_valid_artifact.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _rule_id(row: dict[str, Any]) -> str:
    return str(row.get("rule_id") or row.get("claim_type") or "")


def remaining_issues(*reports: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report in reports:
        if not report:
            continue
        for key in ("findings", "violations"):
            for row in report.get(key) or []:
                if row.get("severity") in {"BLOCK", "AUTO_REPAIR", "WARNING"}:
                    rows.append(row)
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (_rule_id(row), str(row.get("path") or ""), str(row.get("issue") or row.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def artifact_physically_invalid(path: Path | str | None) -> bool:
    if not path:
        return True
    target = Path(path)
    if not target.is_file() or target.stat().st_size < 64:
        return True
    suffix = target.suffix.lower()
    if suffix not in {".docx", ".pptx"}:
        return True
    if suffix == ".docx":
        try:
            from docx import Document
            Document(str(target))
        except Exception:
            return True
    return False


def artifact_structurally_incomplete(artifact_qa: dict[str, Any] | None) -> bool:
    metrics = ((artifact_qa or {}).get("longform_depth") or {}).get("metrics") or {}
    planned = int(metrics.get("planned_sections") or 0)
    generated = int(metrics.get("generated_sections") or 0)
    missing = metrics.get("missing_sections") or []
    if planned >= 20 and generated <= 5:
        return True
    if planned >= 10 and generated <= planned * 0.4:
        return True
    if planned >= 10 and len(missing) >= max(1, int(planned * 0.5)):
        return True
    return False


def should_withhold_before_render(
    compliance: dict[str, Any] | None = None,
    commitment: dict[str, Any] | None = None,
    artifact_qa: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
) -> bool:
    """Only block assembly when the document cannot form a reviewable artifact."""
    del compliance, commitment, quality
    return artifact_structurally_incomplete(artifact_qa)


def issue_scope(row: dict[str, Any]) -> str:
    if row.get("scope") in {"sentence", "paragraph", "section", "batch", "artifact", "workflow"}:
        return str(row.get("scope"))
    rule = _rule_id(row)
    if rule in SCOPE_BY_RULE:
        return SCOPE_BY_RULE[rule]
    if rule.startswith("LF-"):
        return "section"
    if rule.startswith(("CG-", "FAQ-", "C")):
        return "sentence"
    return "paragraph"


def _band_for_rule(rule: str) -> str:
    if rule in PHYSICAL_RULES or rule.startswith("QR-"):
        return "physical"
    if rule in REVIEW_RULES or rule.startswith(("CG-", "LF-", "C")):
        return "review"
    if rule in MILD_RULES:
        return "mild"
    if rule:
        return "review"
    return "mild"


def public_issue_label(rule: str) -> str:
    return ISSUE_LABELS.get(rule) or "存在建议人工复核的内容"


def strip_internal_codes(text: str) -> str:
    value = INTERNAL_CODE.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", value).strip(" ，,;；")


def contains_internal_code(text: str) -> bool:
    return bool(INTERNAL_CODE.search(str(text or "")))


def build_review_summary(
    issues: list[dict[str, Any]],
    *,
    repair_attempts: int = 0,
    artifact_path: str = "",
) -> dict[str, Any]:
    rows = []
    for index, row in enumerate(issues, start=1):
        rule = _rule_id(row)
        excerpt = str(row.get("excerpt") or row.get("text") or row.get("issue") or "")[:180]
        rows.append({
            "issue_id": f"REV-{index:03d}",
            "issue_type": rule,
            "public_label": public_issue_label(rule),
            "severity": "review" if _band_for_rule(rule) != "mild" else "warning",
            "section": str(row.get("path") or row.get("section") or ""),
            "page": row.get("page"),
            "excerpt": excerpt,
            "reason": strip_internal_codes(str(row.get("issue") or row.get("reason") or public_issue_label(rule))),
            "repair_attempts": int(row.get("repair_attempts") or repair_attempts),
            "remaining_problem": excerpt or public_issue_label(rule),
            "recommended_manual_action": MANUAL_ACTIONS.get(rule) or "请结合项目资料人工确认后修改。",
            "scope": issue_scope(row),
        })
    return {
        "schema_version": "qa-review-summary-v0.1",
        "artifact_path": artifact_path,
        "issue_count": len(rows),
        "issues": rows,
    }


def public_message(delivery_status: str, issue_count: int = 0) -> str:
    if delivery_status == DELIVERY_CLEAN:
        return "方案已生成。"
    if delivery_status == DELIVERY_WARNINGS:
        return "方案已生成，发现少量建议复核内容。"
    if delivery_status == DELIVERY_REVIEW:
        return f"方案已生成，但有{issue_count}处内容建议人工复核。"
    if delivery_status == DELIVERY_CANCELLED:
        return "本次生成已停止。已生成的文件仍保留。"
    return "方案未能形成可打开的文件，请检查生成过程后重试。"


def history_label(delivery_status: str) -> str:
    return {
        DELIVERY_CLEAN: "生成成功",
        DELIVERY_WARNINGS: "生成成功（有提示）",
        DELIVERY_REVIEW: "待人工复核",
        DELIVERY_TECHNICAL: "技术失败",
        DELIVERY_CANCELLED: "已停止生成",
        DELIVERY_GENERATING: "正在生成",
    }.get(delivery_status, "生成结果")


def classify_fail_soft(
    *,
    artifact_path: Path | str | None = None,
    findings: list[dict[str, Any]] | None = None,
    artifact_qa: dict[str, Any] | None = None,
    commitment: dict[str, Any] | None = None,
    compliance: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    repair_attempts: int = 0,
    renderer_failed: bool = False,
    cancelled: bool = False,
    repaired: bool = False,
) -> dict[str, Any]:
    path = Path(artifact_path) if artifact_path else None
    physical = renderer_failed or artifact_physically_invalid(path)
    structural = artifact_structurally_incomplete(artifact_qa)
    if findings is None:
        issues = remaining_issues(artifact_qa, commitment, compliance)
    else:
        issues = list(findings)
    if cancelled:
        available = bool(path and path.is_file() and not artifact_physically_invalid(path))
        delivery = DELIVERY_CANCELLED
        qa_status = QA_WARNINGS_REMAIN if issues else QA_PASS
        summary = build_review_summary(issues, repair_attempts=repair_attempts, artifact_path=str(path or ""))
        return _result(qa_status, delivery, available, issues, summary, repair_attempts, path)
    if physical and not (path and path.is_file()):
        summary = build_review_summary(issues, repair_attempts=repair_attempts)
        return _result(QA_CRITICAL, DELIVERY_TECHNICAL, False, issues, summary, repair_attempts, path)
    if physical:
        summary = build_review_summary(issues, repair_attempts=repair_attempts, artifact_path=str(path))
        return _result(QA_CRITICAL, DELIVERY_TECHNICAL, False, issues, summary, repair_attempts, path)
    if structural:
        summary = build_review_summary(issues, repair_attempts=repair_attempts, artifact_path=str(path or ""))
        return _result(QA_CRITICAL, DELIVERY_TECHNICAL, False, issues, summary, repair_attempts, path)
    bands = {_band_for_rule(_rule_id(row)) for row in issues}
    if "physical" in bands and not path:
        summary = build_review_summary(issues, repair_attempts=repair_attempts)
        return _result(QA_CRITICAL, DELIVERY_TECHNICAL, False, issues, summary, repair_attempts, path)
    if any(_band_for_rule(_rule_id(row)) == "review" for row in issues):
        qa_status = QA_REVIEW_REQUIRED
        delivery = DELIVERY_REVIEW
    elif issues:
        qa_status = QA_WARNINGS_REMAIN
        delivery = DELIVERY_WARNINGS
    elif repaired:
        qa_status = QA_PASS_AFTER_REPAIR
        delivery = DELIVERY_CLEAN
    else:
        qa_status = QA_PASS
        delivery = DELIVERY_CLEAN
    summary = build_review_summary(issues, repair_attempts=repair_attempts, artifact_path=str(path or ""))
    return _result(qa_status, delivery, True, issues, summary, repair_attempts, path)


def _result(
    qa_status: str,
    delivery: str,
    available: bool,
    issues: list[dict[str, Any]],
    summary: dict[str, Any],
    repair_attempts: int,
    path: Path | None,
) -> dict[str, Any]:
    scopes = [issue_scope(row) for row in issues]
    worst = "workflow"
    for candidate in ("sentence", "paragraph", "section", "batch", "artifact", "workflow"):
        if candidate in scopes:
            worst = candidate
            break
    if not issues:
        worst = "artifact" if available else "workflow"
    message = public_message(delivery, summary.get("issue_count") or 0)
    return {
        "qa_status": qa_status,
        "delivery_status": delivery,
        "artifact_available": available,
        "public_message": message,
        "review_count": summary.get("issue_count") or 0,
        "qa_review_summary": summary,
        "failure_scope": worst,
        "repair_attempts": repair_attempts,
        "artifact_path": str(path) if path else "",
        "history_label": history_label(delivery),
        "downloadable": available and delivery != DELIVERY_TECHNICAL,
    }


def last_valid_meta_path(run_dir: Path) -> Path:
    return Path(run_dir) / LAST_VALID_META


def record_last_valid_artifact(run_dir: Path, source: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    source = Path(source)
    dest_dir = run_dir / LAST_VALID_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    if dest.resolve() != source.resolve():
        shutil.copy2(source, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    meta = {
        "last_valid_artifact_path": str(dest),
        "last_valid_artifact_name": dest.name,
        "last_valid_artifact_hash": digest,
        "artifact_created_at": now_iso(),
        "source_path": str(source),
    }
    (run_dir / LAST_VALID_META).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def load_last_valid_artifact(run_dir: Path) -> dict[str, Any]:
    meta_path = last_valid_meta_path(run_dir)
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def restore_last_valid_artifact(run_dir: Path, target: Path) -> bool:
    meta = load_last_valid_artifact(run_dir)
    source = Path(str(meta.get("last_valid_artifact_path") or ""))
    if not source.is_file():
        fallback = Path(run_dir) / LAST_VALID_DIR
        files = list(fallback.glob("*.docx")) + list(fallback.glob("*.pptx"))
        source = files[0] if files else source
    if not source.is_file():
        return False
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return True


def write_review_summary(run_dir: Path, summary: dict[str, Any]) -> Path:
    path = Path(run_dir) / "qa_review_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
