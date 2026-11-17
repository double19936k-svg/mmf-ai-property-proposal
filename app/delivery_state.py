"""Fail-soft delivery state machine. QA findings stay recorded; delivery is separate."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from qa_severity import (
    CLASS_FALSE_POSITIVE, CLASS_NEEDS_REVIEW, CLASS_TRUE_BLOCK, CLASS_WARNING,
)

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
    "THIN_HEADING_BODY",
    "HEADING_WITHOUT_SUBSTANTIVE_BODY",
    "INTERNAL_SECTION_ID",
    "BROKEN_QUOTE_SERIALIZATION",
    "QUOTE_BALANCE_QA",
    "PROJECT_FACT_CONSISTENCY",
    "CG-001",
    "CG-002",
    "CG-005",
    "UNSUPPORTED_BIDDER_CLAIM",
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
    "THIN_HEADING_BODY": "section",
    "HEADING_WITHOUT_SUBSTANTIVE_BODY": "section",
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
    "THIN_HEADING_BODY": "部分标题下的正文过短",
    "HEADING_WITHOUT_SUBSTANTIVE_BODY": "部分标题下只有待填写表格",
    "INTERNAL_SECTION_ID": "残留内部章节编号",
    "BROKEN_QUOTE_SERIALIZATION": "部分引号格式可能影响阅读",
    "QUOTE_BALANCE_QA": "部分引号格式可能影响阅读",
    "PROJECT_FACT_CONSISTENCY": "局部项目事实存在疑点",
    "UNSUPPORTED_BIDDER_CLAIM": "投标人能力陈述缺少已核验的证明材料",
    "CONDITIONAL_REWRITE_GRAMMAR": "个别措辞仍不够自然",
    "INTERNAL_GOVERNANCE_TONE": "个别表述偏内部口径",
    "BROKEN_FLOW_SERIALIZATION": "个别流程符号不完整",
    "CONDITIONAL_POSTPROCESS_POLLUTION": "个别条件化表达不自然",
    "CLIENT_ARTIFACT_INTERNAL_META": "个别内部标记残留",
    "CG-001": "存在尚未确认的时长或频次",
    "LF-002": "部分章节内容仍不完整",
    "FAQ-008": "有一段文字和前面重复",
}
MANUAL_ACTIONS = {
    "UNSUPPORTED_COMPLETION_CLAIM": "请人工确认该完成或附件表述，或改为待提交材料说明。",
    "UNSUPPORTED_SYSTEM_CAPABILITY": "请按现场已确认的系统能力改写，或标明待项目确认。",
    "UNSUPPORTED_NUMERIC_THRESHOLD": "请删除未确认数字，或改为双方确认后的安排。",
    "BROKEN_CROSS_REFERENCE": "请改为可读的专项安排名称，避免空泛交叉引用。",
    "EMPTY_HEADING_BLOCK": "请为该标题补充正文，或删除空标题。",
    "THIN_HEADING_BODY": "请补充该标题下的具体做法、频次与记录方式。",
    "HEADING_WITHOUT_SUBSTANTIVE_BODY": "请把该处占位表格补齐为可填写表单，或改为正文说明。",
    "BROKEN_QUOTE_SERIALIZATION": "请检查该段引号在 Word / WPS 中的显示。",
    "PROJECT_FACT_CONSISTENCY": "请核对项目面积、名称等已确认事实。",
    "UNSUPPORTED_BIDDER_CLAIM": "请补充有效证明材料，或将该项保留为待投标人确认。",
}
INTERNAL_CODE = re.compile(r"\b(?:CG|LF|QR|FAQ|RP|ART)-[0-9]{2,4}\b|\b[A-Z]{3,}(?:_[A-Z0-9]+){1,}\b")
LAST_VALID_DIR = "last_valid_artifact"
LAST_VALID_META = "last_valid_artifact.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _rule_id(row: dict[str, Any]) -> str:
    return str(row.get("rule_id") or row.get("claim_type") or "")


def remaining_issues(*reports: dict[str, Any] | None) -> list[dict[str, Any]]:
    """§32：先按 qa_class 分类，再决定哪些行进入 Delivery。FALSE_POSITIVE 只留档不计入。"""
    rows: list[dict[str, Any]] = []
    for report in reports:
        if not report:
            continue
        for key in ("findings", "violations"):
            for row in report.get(key) or []:
                if str(row.get("qa_class") or "") == CLASS_FALSE_POSITIVE:
                    continue
                if row.get("severity") in {"BLOCK", "AUTO_REPAIR", "WARNING", "NEEDS_REVIEW"}:
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


def _band_for_row(row: dict[str, Any]) -> str:
    """§32：Delivery 只看分类结果。没有 qa_class 的旧报告（compliance / document_qa）仍按规则名定档。

    TRUE_BLOCK 与 NEEDS_REVIEW 都落到 review 档 —— 不是把 BLOCK 降级：AGENTS.md 要求
    QA 与 Delivery 解耦、局部失败不得隐藏完整文件，所以两者都出文件但都进待复核清单。
    两者的区别记在 true_block_count 与报告的 qa_classification 里，不靠扣文件表达。
    """
    qa_class = str(row.get("qa_class") or "")
    if qa_class == CLASS_FALSE_POSITIVE:
        return "ignored"
    if qa_class in {CLASS_TRUE_BLOCK, CLASS_NEEDS_REVIEW}:
        return "review"
    if qa_class == CLASS_WARNING:
        return "mild"
    return _band_for_rule(_rule_id(row))


def public_issue_label(rule: str) -> str:
    return ISSUE_LABELS.get(rule) or "有一处内容需要你看一下"


def _plain_excerpt(rule: str, excerpt: str) -> str:
    text = str(excerpt or "")
    if "duplicate of artifact" in text or re.search(r"artifact\.(?:paragraphs|sections|tables)\[", text):
        return "这里有一段和前面相同的文字，打开文稿后删去重复的那段即可。"
    if re.search(r"[A-Za-z_]{5,}\[", text):
        return "这里有一句写得不完整。请改成谁在什么时候做什么，不要保留半句话。"
    if rule == "UNSUPPORTED_COMPLETION_CLAIM" and text:
        return f"这句话把尚未发生的事写成了已经完成。原句片段：{text}"
    return strip_internal_codes(text)


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
        excerpt = _plain_excerpt(rule, str(row.get("excerpt") or row.get("text") or row.get("issue") or "")[:180])
        rows.append({
            "issue_id": f"REV-{index:03d}",
            "issue_type": rule,
            "public_label": public_issue_label(rule),
            "severity": "review" if _band_for_row(row) != "mild" else "warning",
            "section": str(row.get("path") or row.get("section") or ""),
            "page": row.get("page"),
            "excerpt": excerpt,
            "reason": strip_internal_codes(str(row.get("issue") or row.get("reason") or public_issue_label(rule))),
            "repair_attempts": int(row.get("repair_attempts") or repair_attempts),
            "remaining_problem": excerpt or public_issue_label(rule),
            "recommended_manual_action": MANUAL_ACTIONS.get(rule) or "请结合项目资料人工确认后修改。",
            "scope": issue_scope(row),
            "display": str(row.get("display") or ""),
            "slide_index": row.get("slide_index"),
            "slide_title": str(row.get("slide_title") or ""),
            "component_label": str(row.get("component_label") or ""),
            "full_sentence": str(row.get("full_sentence") or excerpt),
            "fragment": str(row.get("fragment") or excerpt),
            "section_no": row.get("section_no"),
            "section_title": str(row.get("section_title") or ""),
            "para_index": row.get("para_index"),
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
    artifact: dict[str, Any] | None = None,
    medium: str = "",
) -> dict[str, Any]:
    path = Path(artifact_path) if artifact_path else None
    physical = renderer_failed or artifact_physically_invalid(path)
    structural = artifact_structurally_incomplete(artifact_qa)
    if findings is None:
        issues = remaining_issues(artifact_qa, commitment, compliance)
    else:
        issues = [row for row in findings
                  if str(row.get("qa_class") or "") != CLASS_FALSE_POSITIVE]
    from governance.review_warnings import prepare_review_issues
    issues = prepare_review_issues(issues, artifact=artifact, medium=medium)
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
    if any(_band_for_row(row) == "review" for row in issues):
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
        "true_block_count": sum(1 for row in issues if str(row.get("qa_class") or "") == CLASS_TRUE_BLOCK),
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
