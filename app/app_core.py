from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import re
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from providers import ProviderError, ProviderManager, ProviderUnavailableError
from providers.base import short_stem
from filesystem_paths import filesystem_path
from compliance import evaluate_compliance, write_default_rules
from recovery_orchestrator import leftover_warning_rows, recover_governance, withhold_delivery
from delivery_state import (
    DELIVERY_REVIEW,
    DELIVERY_TECHNICAL,
    artifact_physically_invalid,
    classify_fail_soft,
    load_last_valid_artifact,
    public_message,
    record_last_valid_artifact,
    restore_last_valid_artifact,
    write_review_summary,
)
from governance import apply_artifact_repairs, apply_local_repairs, artifact_findings_are_locally_recoverable, build_contracts, evaluate_artifact, evaluate_commitments, evaluate_docx_artifact, evaluate_selection, merge_longform_qa, quote_serialization_status, repair_docx_artifact
from longform.eta import append_latency_sample, build_terminal_latency_sample, estimate_runtime
from longform.orchestrator import FIRST_DRAFT_PAGE_CLASSIFICATION_FILE, generate_longform
from longform.reasoning import normalize_speed_profile, product_speed_catalog
from planning.canonical import ensure_requirement_pack
from planning.content_engineering import build_technical_review_summary
from planning.planner import PlanningError, normalize_document_scale
from workflow_policy import (
    attach_recommendation_state,
    generation_may_proceed,
    resolve_selected_positive_ids,
)
from workflow_timing import (
    clear_active_stage,
    ensure_workflow,
    finish_workflow,
    history_duration,
    record_longform_attempt,
    record_stage_duration,
    set_active_stage,
    track_stage,
    workflow_audit_fields,
)

from tender_intake import (
    TenderError, apply_confirmation as apply_tender_confirmation, extract_run as extract_tender_run,
    save_uploads as save_tender_uploads, seed_brief as seed_tender_brief,
    understand_run as understand_tender_run, validate_requirement_pack,
)
from tender_intake.fact_sources import brief_project_fields


import paths as _mmf_paths
_mmf_paths.ensure_directories()
APP_ROOT = _mmf_paths.APP_ROOT
RUNTIME_ROOT = _mmf_paths.RUNTIME_ROOT
ASSETS_DIR = _mmf_paths.ASSETS_DIR
CONFIG_DIR = _mmf_paths.CONFIG_DIR
RUNS_DIR = _mmf_paths.RUNS_DIR
RUNTIME_DIR = _mmf_paths.RUNTIME_DIR
STATIC_DIR = _mmf_paths.STATIC_DIR
REVIEW_FILE = RUNTIME_DIR / "product_review.json"
STATE_FILE = RUNTIME_DIR / "mmf_state.json"
MMF006A_STATE_FILE = RUNTIME_DIR / "MMF006A_state.json"
ACCESS_AUDIT_FILE = RUNTIME_DIR / "access_audit.jsonl"

SNAPSHOT = ASSETS_DIR / "knowledge" / "accepted_ku_b1.2.jsonl"
CORPUS_INDEX = ASSETS_DIR / "knowledge" / "candidate_corpus_index_v0.1.json"
STYLE_RULES = ASSETS_DIR / "style" / "user_writing_style_rules_v0.2.json"
GENERATION_PATCH = ASSETS_DIR / "generation" / "generation_layer_patch_v0.2.json"
OUTPUT_PROFILE = ASSETS_DIR / "rendering" / "output_medium_profile_v0.1.json"
ASSET_MANIFEST = ASSETS_DIR / "asset_manifest.json"
RUNTIME_CONFIG = RUNTIME_DIR / "runtime_config.json"
PROVIDERS = ProviderManager(APP_ROOT, config_dir=CONFIG_DIR)
COMPLIANCE_RULES = ASSETS_DIR / "compliance" / "provider_compliance_rules_v0.1.json"

SCENARIOS = [
    "完整物业服务方案",
    "投标全套服务方案",
    "前期介入",
    "客户服务",
    "工程运维",
    "环境服务与供方履约",
    "进场启动与承接查验",
    "秩序与安全管理",
    "品质管理与客户满意度",
]
FULL_PROPOSAL_SCENARIOS = {"完整物业服务方案", "投标全套服务方案"}
MEDIA = ["WORD", "PPT"]
STYLE_IDS = {
    "M-001", "M-003", "M-004", "M-008", "M-009", "M-010", "M-015",
    "C-002", "C-008", "C-009", "C-011", "C-013", "C-017",
    "D-004", "D-005", "D-007", "P-004", "P-006", "P-007", "P-009", "P-012",
}
DEFAULT_AUTOMATIC_REPAIR_ATTEMPTS = 1
MAX_AUTOMATIC_INTEGRITY_REPAIR_ATTEMPTS = 3
RECOVERABLE_INTEGRITY_RULE_PREFIXES = ("LF-", "QR-")


class MMFError(RuntimeError):
    pass


def _refresh_final_scoring_delivery(run_dir: Path, artifact_path: Path,
                                    final_qa: dict[str, Any]) -> dict[str, Any]:
    """Only the reopened delivery artifact may supply final scoring evidence."""
    from planning.scoring_final_evidence import refresh_final_scoring

    try:
        reading = refresh_final_scoring(
            run_dir, artifact_path, expected_artifact_sha256=final_qa.get("sha256"))
    except Exception as exc:
        reading = {"status": "FAILED", "reason_code": "FINAL_SCORING_CHECK_FAILED",
                   "error_type": type(exc).__name__, "artifact_path": str(artifact_path),
                   "may_claim_final_scoring": False, "completed_clean_allowed": False}
        write_json(run_dir / "final_scoring_report.json", reading)
    findings = [row for row in final_qa.get("findings") or []
                if row.get("rule_id") not in {
                    "FINAL_SCORING_MEASUREMENT_FAILED", "FINAL_SCORING_COVERAGE_BELOW_GATE"}]
    result = {**final_qa, "final_scoring": reading, "findings": findings}
    if reading.get("status") == "FAILED":
        findings.append({"rule_id": "FINAL_SCORING_MEASUREMENT_FAILED", "severity": "BLOCK", "qa_class": "TRUE_BLOCK",
                         "issue": "成稿评分证据未完成有效核验，生成阶段评分不能作为最终满足证明。",
                         "reason_code": reading.get("reason_code")})
        result["status"] = "BLOCK"
    elif reading.get("status") == "VERIFIED" and not reading.get("completed_clean_allowed"):
        findings.append({"rule_id": "FINAL_SCORING_COVERAGE_BELOW_GATE", "severity": "NEEDS_REVIEW", "qa_class": "NEEDS_REVIEW",
                         "issue": "实际成稿的技术评分响应未达到既有验收门槛。",
                         "coverage": (reading.get("fulfillment") or {}).get("technical_scoring_coverage")})
        if result.get("status") not in {"BLOCK", "NEEDS_REVIEW"}:
            result["status"] = "NEEDS_REVIEW"
    result["summary"] = {**(result.get("summary") or {}),
                         "block_count": sum(row.get("severity") == "BLOCK" for row in findings),
                         "needs_review_count": sum(row.get("severity") == "NEEDS_REVIEW" for row in findings),
                         "warning_count": sum(row.get("severity") == "WARNING" for row in findings)}
    if "qa_classification" in result:
        from qa_severity import classify_findings
        false_positives = [{**row, "qa_class": "FALSE_POSITIVE", "severity": "FALSE_POSITIVE"}
                           for row in result.get("false_positives") or []]
        classified = classify_findings([*findings, *false_positives])
        result["findings"] = classified["findings"]
        result["qa_classification"] = {**result["qa_classification"], **classified["summary"]}
    return result


def delivery_blocked(compliance: dict[str, Any], commitment: dict[str, Any], artifact_qa: dict[str, Any], quality_regression: dict[str, Any] | None = None) -> bool:
    quality = quality_regression or {}
    return (
        compliance.get("status") == "BLOCK"
        or commitment.get("status") == "BLOCK"
        or artifact_qa.get("status") == "BLOCK"
        or quality.get("status") == "FAIL"
        or quality.get("QUALITY_REGRESSION_GATE") == "FAIL"
    )


def _recoverable_integrity_failure(compliance: dict[str, Any], commitment: dict[str, Any], artifact_qa: dict[str, Any], quality_regression: dict[str, Any] | None = None) -> bool:
    """Return True only for failures the generation pipeline can repair itself."""
    if compliance.get("status") == "BLOCK" or commitment.get("status") == "BLOCK":
        return False
    quality = quality_regression or {}
    if quality.get("status") == "FAIL" or quality.get("QUALITY_REGRESSION_GATE") == "FAIL":
        return True
    blockers = [
        str(item.get("rule_id") or "")
        for item in (artifact_qa.get("findings") or [])
        if item.get("severity") == "BLOCK"
    ]
    return bool(blockers) and all(rule.startswith(RECOVERABLE_INTEGRITY_RULE_PREFIXES) for rule in blockers)


def _automatic_repair_limit(compliance: dict[str, Any], commitment: dict[str, Any], artifact_qa: dict[str, Any], quality_regression: dict[str, Any] | None = None) -> int:
    if _recoverable_integrity_failure(compliance, commitment, artifact_qa, quality_regression):
        return MAX_AUTOMATIC_INTEGRITY_REPAIR_ATTEMPTS
    return DEFAULT_AUTOMATIC_REPAIR_ATTEMPTS


def _numeric_seconds(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return round(float(value), 3)
    except (TypeError, ValueError):
        return float(default)


def _repair_reasoning_value(longform_result: dict[str, Any] | None) -> str:
    performance = (longform_result or {}).get("performance") or {}
    value = performance.get("repair_reasoning")
    if value in {None, "", "null"}:
        return "not_invoked"
    return str(value)


def _token_accounting(longform_result: dict[str, Any] | None) -> dict[str, Any]:
    performance = (longform_result or {}).get("performance") or {}
    row = dict(performance.get("token_accounting") or {})
    row.setdefault("input_tokens", 0)
    row.setdefault("output_tokens", 0)
    row.setdefault("cache_read_tokens", 0)
    row.setdefault("provenance", row.get("provenance") or "unknown")
    row["cost_usd"] = None
    row["cost_provenance"] = "unknown_unverified_pricing"
    return row


def _record_terminal_latency(
    *,
    run_id: str,
    brief: dict[str, Any],
    provider_name: str,
    longform_result: dict[str, Any] | None,
    status: str,
    eligible: bool,
    actual_time: float,
    qa_seconds: float,
    build_id: str,
) -> None:
    result = longform_result or {}
    word = result.get("word") or {}
    performance = result.get("performance") or {}
    this_run = int(word.get("this_run_calls") if word.get("this_run_calls") is not None else performance.get("this_run_calls") or 0)
    this_resume = int(word.get("this_resume_calls") if word.get("this_resume_calls") is not None else performance.get("this_resume_calls") or 0)
    planned = int(word.get("generation_batch_count") or performance.get("generation_batch_count") or 0)
    zero_generation = this_run <= 0
    if zero_generation:
        return
    partial_resume = bool(this_resume > 0 and planned > 0 and this_run < planned)
    tokens = _token_accounting(result)
    sample = build_terminal_latency_sample(
        provider=provider_name,
        model=str(((result.get("capability") or {}).get("effective_settings") or {}).get("model") or brief.get("model") or ""),
        mode=str(brief.get("speed_profile") or "balanced"),
        stage="draft",
        section_count=int(word.get("logical_section_count") or 0),
        batch_count=word.get("generation_batch_count"),
        elapsed_seconds=actual_time,
        qa_seconds=qa_seconds,
        provider_wait_seconds=_numeric_seconds(performance.get("provider_wait_seconds")),
        success=eligible and status == "generation_completed" and not partial_resume,
        eligible_for_eta=eligible and status == "generation_completed" and not partial_resume,
        final_outcome=status,
        token_accounting=tokens,
        build_id=build_id,
        run_id=run_id,
        this_run_calls=this_run,
        zero_generation=False,
        token_provenance=str(tokens.get("provenance") or "unknown"),
        generation_started_at=(result.get("performance") or {}).get("started_at"),
        invocation_id=f"{run_id}:{build_id}:{this_run}:{time.time_ns()}",
        sample_scope="partial_resume" if partial_resume else "full_document",
        partial_resume=partial_resume,
    )
    try:
        append_latency_sample(RUNTIME_DIR / "latency_history.jsonl", sample)
        # Only clean, finalized full runs teach per-batch latency budgets. Failed
        # attempts and repaired/partial workloads must not poison this pool.
        if sample["eligible_for_eta"] and performance.get("repair_reasoning") in (None, "not_invoked"):
            for index, attempt in enumerate(performance.get("provider_attempt_samples") or []):
                if attempt.get("failure_class") or float(attempt.get("elapsed_seconds") or 0) <= 0:
                    continue
                append_latency_sample(RUNTIME_DIR / "latency_history.jsonl", {
                    **sample, **attempt, "sample_scope": "provider_attempt", "workload_scope": "provider_attempt",
                    "history_id": sample["invocation_id"] + f":provider-attempt:{index}",
                    "section_count": attempt.get("batch_size"), "size_bucket": "provider_attempt",
                })
    except Exception as exc:
        result.setdefault("telemetry_error", str(exc))


def _longform_runtime_audit(longform_result: dict[str, Any] | None) -> dict[str, Any]:
    result = longform_result or {}
    word = result.get("word") or {}
    performance = result.get("performance") or {}
    capability = result.get("capability") or {}
    effective = capability.get("effective_settings") or {}
    document_qa = result.get("document_qa") or {}
    budget_audit = document_qa.get("budget") or word.get("global_repair_budget") or {}
    tokens = _token_accounting(result)
    return {
        "provider_call_count": word.get("provider_call_count") if word.get("provider_call_count") is not None else performance.get("provider_calls_total"),
        "provider_calls_total": word.get("provider_calls_total") if word.get("provider_calls_total") is not None else performance.get("provider_calls_total"),
        "this_run_calls": word.get("this_run_calls") if word.get("this_run_calls") is not None else performance.get("this_run_calls"),
        "this_resume_calls": word.get("this_resume_calls") if word.get("this_resume_calls") is not None else performance.get("this_resume_calls"),
        "quality_escalation_requested": word.get("quality_escalation_requested", performance.get("quality_escalation_requested")),
        "quality_escalation_applied": word.get("quality_escalation_applied", performance.get("quality_escalation_applied")),
        "reasoning_before": word.get("reasoning_before", performance.get("reasoning_before") or effective.get("reasoning_before")),
        "reasoning_after": word.get("reasoning_after", performance.get("reasoning_after") or effective.get("reasoning_after") or effective.get("effective_reasoning")),
        "escalation_unavailable_reason": word.get("escalation_unavailable_reason", performance.get("escalation_unavailable_reason") or effective.get("escalation_unavailable_reason")),
        "completed_batches": word.get("completed_batches"),
        "pending_batches": word.get("pending_batches"),
        "failed_batches": word.get("failed_batches"),
        "planning_seconds": _numeric_seconds(performance.get("planning_seconds")),
        "generation_seconds": _numeric_seconds(performance.get("generation_seconds")),
        "provider_wait_seconds": _numeric_seconds(performance.get("provider_wait_seconds")),
        "qa_seconds": _numeric_seconds(performance.get("qa_seconds")),
        "repair_seconds": _numeric_seconds(performance.get("repair_seconds")),
        "token_accounting": tokens,
        "input_tokens": tokens.get("input_tokens"),
        "output_tokens": tokens.get("output_tokens"),
        "token_provenance": tokens.get("provenance"),
        "soft_latency_observations": performance.get("soft_latency_observations"),
        "soft_latency_policy": performance.get("soft_latency_policy"),
        "INITIAL_GENERATION_CALLS": word.get("INITIAL_GENERATION_CALLS"),
        "CONTINUATION_CALLS": word.get("CONTINUATION_CALLS"),
        "SECTION_REPAIR_CALLS": word.get("SECTION_REPAIR_CALLS"),
        "RETRY_CALLS": word.get("RETRY_CALLS"),
        "TOTAL_PROVIDER_CALLS": word.get("TOTAL_PROVIDER_CALLS") or word.get("provider_call_count"),
        "INITIAL_SECTIONS_PER_CALL": word.get("INITIAL_SECTIONS_PER_CALL"),
        "INITIAL_CALL_REDUCTION_RATIO": word.get("INITIAL_CALL_REDUCTION_RATIO"),
        "PROVIDER_REPAIR_COUNT": word.get("PROVIDER_REPAIR_COUNT"),
        "LOCAL_REPAIR_COUNT": word.get("LOCAL_REPAIR_COUNT"),
        "REPAIR_BUDGET_USED": word.get("REPAIR_BUDGET_USED"),
        "REPAIR_BUDGET_MAX": word.get("REPAIR_BUDGET_MAX"),
        "REPAIR_SKIPPED_LOW_VALUE": word.get("REPAIR_SKIPPED_LOW_VALUE"),
        "MAX_OBSERVED_CONCURRENCY": word.get("MAX_OBSERVED_CONCURRENCY"),
        "AVG_OBSERVED_CONCURRENCY": word.get("AVG_OBSERVED_CONCURRENCY"),
        "INITIAL_GENERATION_SECONDS": word.get("INITIAL_GENERATION_SECONDS"),
        "REPAIR_PROVIDER_SECONDS": word.get("REPAIR_PROVIDER_SECONDS"),
        # §44-46：调用与耗时按初稿/技术重试/修复/补充分别记账，避免"修了三小时"
        # 被混进初稿耗时里看不见。
        "INITIAL_PROVIDER_CALLS": word.get("INITIAL_GENERATION_CALLS"),
        "TECHNICAL_RETRY_CALLS": word.get("TECHNICAL_RETRY_CALLS"),
        "REPAIR_CALLS": word.get("REPAIR_CALLS"),
        "SUPPLEMENT_CALLS": word.get("SUPPLEMENT_CALLS"),
        "TOTAL_CALLS": word.get("TOTAL_CALLS"),
        "TECHNICAL_RETRY_SECONDS": word.get("TECHNICAL_RETRY_SECONDS"),
        "SUPPLEMENT_SECONDS": word.get("SUPPLEMENT_SECONDS"),
        "SELECTIVE_REPAIR_WALL_SECONDS": word.get("SELECTIVE_REPAIR_WALL_SECONDS"),
        "DOCUMENT_QA_SECONDS": _numeric_seconds(performance.get("document_qa_seconds")),
        "DEPTH_SUPPLEMENT_SECONDS": _numeric_seconds(performance.get("depth_supplement_seconds")),
        "DOCUMENT_QA_REPAIR": document_qa.get("status"),
        "DOCUMENT_QA_ATTEMPTED": document_qa.get("attempted"),
        "DOCUMENT_QA_REPAIRED": document_qa.get("repaired"),
        "DOCUMENT_QA_ROUNDS": document_qa.get("rounds"),
        "FULL_DOCUMENT_REGENERATION": bool(document_qa.get("full_document_regeneration")),
        "REPAIR_STORM_GUARD": "TRIGGERED" if budget_audit.get("storm_triggered") else "PASS",
        "QA_PERFORMANCE_WARNING": bool(
            budget_audit.get("storm_triggered") or budget_audit.get("repair_over_initial")
        ),
    }


def _pagination_audit_fields(run_dir: Path) -> dict[str, Any]:
    """把真实分页量尺摊平成审计字段。

    页码必须来自渲染后的 Artifact，而不是构建期缓存：本轮的判据就是"目录页码
    全部为 1 则不能 VERIFIED"，所以没有渲染器时如实留 SKIPPED_*，不猜测。
    """
    path = run_dir / "artifact_pagination.json"
    if not path.is_file():
        return {}
    try:
        report = read_json(path, "artifact_pagination")
    except Exception as exc:
        return {"PAGINATION_STATUS": "REPORT_UNREADABLE", "PAGINATION_ERROR": str(exc)[:160]}
    if not isinstance(report, dict):
        return {}
    counts = [int(v) for v in (report.get("page_char_counts") or []) if isinstance(v, (int, float))]
    rendered = int(report.get("rendered_pages") or 0)
    return {
        "PAGINATION_STATUS": report.get("status"),
        "PAGINATION_RENDERER": report.get("renderer"),
        "RENDERED_PAGES": rendered,
        "VISIBLE_EFFECTIVE_CHARS": int(report.get("visible_chars") or 0),
        "PAGE_CHAR_MEAN": round(sum(counts) / len(counts), 1) if counts else 0.0,
        "PAGE_CHAR_MIN": min(counts) if counts else 0,
        "THIN_PAGE_COUNT": int(report.get("thin_page_count") or 0),
        "EFFECTIVE_CONTENT_PAGES": int(report.get("effective_content_pages") or 0),
        "MEANINGFUL_PAGE_RATIO": report.get("meaningful_page_ratio"),
        "TOC_ENTRY_COUNT": int(report.get("toc_entry_count") or 0),
        "TOC_PAGES_RESOLVED": int(report.get("toc_pages_resolved") or 0),
        "TOC_PAGES_UNRESOLVED": int(report.get("toc_pages_unresolved") or 0),
        "TOC_PAGES_REWRITTEN": int(report.get("toc_pages_refreshed") or 0),
        "TOC_PAGES_VERIFIED": int(report.get("toc_pages_verified") or 0),
        "TOC_OUTLINE_MARK_COUNT": int(report.get("toc_outline_mark_count") or 0),
        "ARTIFACT_XML_REWRITTEN": bool(report.get("document_xml_rewritten")),
        "TOC_IDENTICAL_PAGE_SHARE": report.get("identical_page_share"),
        "TOC_DISPLAYED_PAGES": report.get("displayed_pages") or [],
        "TOC_BODY_START_PAGE": report.get("toc_body_start_page"),
        # §13：只报"404 页"不算说清楚。物理页、技术有效页与六类页数必须并列出现在
        # 同一份交付审计里；量不到就留 NOT_MEASURED/None，不得折算成 0 冒充"没有超页"。
        "PAGE_CLASSIFICATION_STATUS": report.get("page_classification_status") or "NOT_MEASURED",
        "PHYSICAL_PAGES": int(report.get("physical_pages") or rendered or 0),
        "TECHNICAL_EFFECTIVE_PAGES": report.get("technical_effective_pages"),
        "NARRATIVE_TECHNICAL_PAGES": report.get("narrative_technical_pages"),
        "STRUCTURED_TECHNICAL_PAGES": report.get("structured_technical_pages"),
        "FRONT_MATTER_PAGES": report.get("front_matter_pages"),
        "BLANK_PAGES": report.get("blank_pages"),
        "PLACEHOLDER_PAGES": report.get("placeholder_pages"),
        "PLACEHOLDER_PAGE_RATIO": report.get("placeholder_page_ratio"),
        "EXCLUDED_SCOPE_PAGES": report.get("excluded_scope_pages"),
        # §41：凑页数最顺手的资信/证书/业绩/履历附件页要单独立一个字段，
        # 否则"2 页报价条款"和"2 页证书附件"在审计里长得一模一样。
        # CREDENTIAL_ATTACHMENT_PAGES 只数"资信词占多数"的那一页（= §41 禁的形状）；
        # 正文里顺带提一句"合同复印件"的页记在 ..._MENTIONED 里点名，两个数都要在。
        "EXCLUDED_SCOPE_GROUP_HITS": report.get("excluded_scope_group_hits"),
        "CREDENTIAL_ATTACHMENT_PAGES": report.get("credential_attachment_pages"),
        "CREDENTIAL_ATTACHMENT_PAGES_MENTIONED": report.get("credential_attachment_pages_present"),
        "CREDENTIAL_ATTACHMENT_PAGE_DETAILS": report.get("credential_attachment_page_details"),
        "NON_TECHNICAL_PAGE_RATIO": report.get("non_technical_page_ratio"),
        "PAGE_BASIS": report.get("page_basis"),
        "LAYOUT_BASIS": report.get("layout_basis"),
        "TECHNICAL_PAGE_COUNT_BOUND": report.get("technical_page_count_bound") or "UNKNOWN",
        "CHARS_PER_TECHNICAL_PAGE": report.get("chars_per_technical_page"),
    }


def _technical_review_summary_measurement(run_dir: Path,
                                          pagination_report: dict[str, Any] | None
                                          ) -> tuple[dict[str, Any], str]:
    """§41/§44：Technical Review Summary 的页数取哪一层实测。

    优先最终 DOCX 的逐页分类；本机没有渲染器时退回初稿渲染那一层，并把 basis 写清楚，
    让"最终文件没量过"留在证据里，而不是被悄悄替换成 0。
    """
    if (pagination_report or {}).get("page_classification_status") == "MEASURED":
        return pagination_report, "FINAL_DOCX_PAGINATION"
    path = run_dir / FIRST_DRAFT_PAGE_CLASSIFICATION_FILE
    if path.is_file():
        try:
            return read_json(path, "technical_review_page_classification"), "FIRST_DRAFT_PAGINATION"
        except Exception:
            return {}, "NOT_MEASURED"
    return {}, "NOT_MEASURED"


def _write_technical_review_summary(run_dir: Path, gate: dict[str, Any],
                                    pagination_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """§44：随件写一份**内部** Technical Review Summary（只列待投标人确认的技术事实）。"""
    measured, basis = _technical_review_summary_measurement(run_dir, pagination_report)

    def count(key: str) -> int | None:
        value = measured.get(key)
        return None if value is None else int(value)

    summary = build_technical_review_summary(
        gate,
        excluded_scope_pages=count("excluded_scope_pages"),
        credential_attachment_pages=count("credential_attachment_pages"),
        credential_attachment_pages_mentioned=count("credential_attachment_pages_present"),
        credential_attachment_page_details=measured.get("credential_attachment_page_details") or None,
        page_basis=basis,
    )
    write_json(run_dir / "technical_review_summary.json", summary)
    return summary


def _table_audit_fields(run_dir: Path) -> dict[str, Any]:
    """§29：Tables Planned / Rendered / Meaningful / Placeholder 必须分开报。

    两份来源拼在一起：装配台账（build_docx 子进程写的，知道"计划了几张、删了几张、
    为什么删"）与交付件实测（final_artifact_qa 写的，知道"文件里真实长什么样"）。
    只报其中一个都会说谎 —— 前者不含后续修复，后者不知道被删掉的那些。
    """
    fields: dict[str, Any] = {}
    build_path = run_dir / "artifact_table_build_metrics.json"
    if build_path.is_file():
        try:
            build = read_json(build_path, "artifact_table_build_metrics")
        except Exception as exc:
            build = {}
            fields["TABLE_BUILD_METRICS_ERROR"] = str(exc)[:160]
        if isinstance(build, dict) and build:
            fields.update({
                "TABLES_PLANNED": int(build.get("tables_planned") or 0),
                "TABLES_RENDERED": int(build.get("tables_rendered") or 0),
                "TABLES_DROPPED": int(build.get("tables_dropped") or 0),
                "TABLES_COMPACTED": int(build.get("tables_compacted") or 0),
                "TABLES_BIDDER_FILL": int(build.get("tables_bidder_fill") or 0),
                # 台账里这两个是"空"格，不是"/、待填写"那类占位符格 —— 名字必须对上，
                # 否则报告里说"占位格 1471"就是在换一个口径冒充另一个口径。
                "TABLE_EMPTY_CELLS_PLANNED": int(build.get("planned_empty_cells") or 0),
                "TABLE_EMPTY_CELLS_RENDERED": int(build.get("rendered_empty_cells") or 0),
                "TABLE_PLACEHOLDER_CELLS_PLANNED": int(build.get("planned_placeholder_cells") or 0),
                "TABLE_PLACEHOLDER_CELLS_RENDERED": int(build.get("rendered_placeholder_cells") or 0),
                "TABLE_CELLS_PLANNED": int(build.get("planned_cells") or 0),
                "TABLE_CELLS_RENDERED": int(build.get("rendered_cells") or 0),
                "DROPPED_TABLE_TITLES": [str(row.get("title") or "")[:60]
                                         for row in (build.get("dropped_tables") or [])][:20],
            })
    qa_path = run_dir / "final_artifact_qa.json"
    if qa_path.is_file():
        try:
            measured = (read_json(qa_path, "final_artifact_qa") or {}).get("tables") or {}
        except Exception:
            measured = {}
        if measured:
            fields.update({
                "TABLES_MEANINGFUL": int(measured.get("tables_meaningful") or 0),
                "TABLES_EMPTY_SCAFFOLD": int(measured.get("tables_empty_scaffold") or 0),
                "TABLES_PLACEHOLDER_HEAVY": int(measured.get("tables_placeholder_heavy") or 0),
                "TABLES_DUPLICATE_ROWS": int(measured.get("tables_duplicate_rows") or 0),
                "TABLE_MEANINGFUL_CELL_RATIO": measured.get("meaningful_cell_ratio_overall"),
                "TABLE_EMPTY_CELL_RATIO": measured.get("empty_cell_ratio_overall"),
                "MEANINGFUL_STRUCTURED_TABLE_CHARS": int(measured.get("meaningful_table_chars") or 0),
                "MEANINGFUL_TABLE_SHARE": measured.get("meaningful_table_share"),
            })
    return fields


def _write_run_audit(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    terminal = "completed" if payload.get("status") == "generation_completed" else "failed"
    finish_workflow(run_dir, terminal)
    merged = {**payload, **workflow_audit_fields(run_dir)}
    merged.setdefault("provider_wait_seconds", 0.0)
    merged.setdefault("generation_seconds", 0.0)
    merged.setdefault("repair_seconds", 0.0)
    merged.setdefault("artifact_seconds", 0.0)
    merged.setdefault("qa_seconds", 0.0)
    write_json(run_dir / "run_audit.json", merged)
    return merged


def _write_generation_error_diagnostics(run_dir: Path, run_id: str, exc: BaseException, *, phase: str, provider: str = "", recoverable: bool = True, section_id: str | None = None, content_unit_id: str | None = None, batch_id: str | None = None) -> str:
    """Persist actionable, secret-free diagnostics for every generation exception."""
    error_id = f"ERR-{uuid.uuid4().hex[:8].upper()}"
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    frame = frames[-1] if frames else None
    raw_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    sanitized = re.sub(r"(?i)(authorization|api[_ -]?key|token)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", raw_trace)
    payload = {
        "error_id": error_id, "timestamp": now_iso(), "run_id": run_id,
        "phase": phase, "subphase": phase, "exception_type": type(exc).__name__,
        "exception_message": str(exc)[:500], "source_file": Path(frame.filename).name if frame else None,
        "function": frame.name if frame else None, "line": frame.lineno if frame else None,
        "section_id": section_id, "content_unit_id": content_unit_id, "batch_id": batch_id,
        "provider": provider, "resume_state": "recoverable_partial_run" if recoverable else "terminal",
        "recoverable": recoverable, "traceback": sanitized[:12000],
    }
    write_json(run_dir / "error_trace.json", payload)
    (run_dir / "log").mkdir(parents=True, exist_ok=True)
    (run_dir / "log" / "error.log").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return error_id


def _blocked_message(compliance: dict[str, Any], commitment: dict[str, Any], artifact_qa: dict[str, Any], repair_attempts: int = 0) -> str:
    missing = ((artifact_qa.get("longform_depth") or {}).get("metrics") or {}).get("missing_sections") or []
    if missing:
        shown = "、".join(str(item) for item in missing[:8])
        return f"方案中仍有少量内容无法在当前资料下安全确认，系统已完成自动修复尝试。请补充项目资料或重新生成相关章节。受影响章节：{shown}。"
    return "方案中仍有少量内容无法在当前资料下安全确认，系统已完成自动修复尝试。请补充项目资料或重新生成相关章节。"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def engine_display(provider_name: str) -> str:
    blob = str(provider_name or "").lower()
    if "qwen" in blob or "千问" in blob:
        return "千问"
    if "grok" in blob:
        return "Grok"
    if "kimi" in blob or "moonshot" in blob:
        return "Kimi"
    if "glm" in blob or "zhipu" in blob or "智谱" in blob:
        return "智谱GLM"
    if "mock" in blob:
        return "测试引擎"
    return str(provider_name or "当前引擎")


def parse_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def duration_seconds(started_at: str | None, finished_at: str | None = None, fallback: int | None = None) -> int:
    start = parse_iso(started_at)
    end = parse_iso(finished_at)
    if start and end:
        if start.tzinfo is None:
            start = start.replace(tzinfo=end.tzinfo)
        if end.tzinfo is None:
            end = end.replace(tzinfo=start.tzinfo)
        return max(0, int((end - start).total_seconds()))
    if fallback is not None:
        try:
            return max(0, int(fallback))
        except (TypeError, ValueError):
            return 0
    if start:
        now = datetime.now().astimezone()
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
        return max(0, int((now - start).total_seconds()))
    return 0


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _checked(path: Path) -> Path:
    resolved = path.resolve()
    allowed = list(_mmf_paths.current().allowed_roots())
    if not any(_is_within(resolved, root) for root in allowed):
        raise MMFError("当前路径不在应用允许的工作范围内。")
    forbidden_raw = os.environ.get("MMF_FORBIDDEN_ROOTS", "")
    for value in [item for item in forbidden_raw.split(os.pathsep) if item.strip()]:
        if _is_within(resolved, Path(value)):
            raise MMFError("当前环境不允许访问研发目录。")
    return resolved


def _path_label(path: Path) -> str:
    """审计与报告里记的是"这个文件在哪"，能否折算成包内相对路径只是排版问题：
    把数据根指到包外是受支持的部署姿态（dist 侧 E2E、只读安装目录），旧的裸 relative_to
    在那种姿态下抛 ValueError，把一次合法读取/一次已完成的生成本身变成失败。"""
    try:
        return path.resolve().relative_to(RUNTIME_ROOT).as_posix()
    except ValueError:
        return str(path)


_ARTIFACT_QA_RANK = {"PASS": 0, "WARNING": 1, "NEEDS_REVIEW": 2, "BLOCK": 3}


def _attempt_account_review_repair(final_path: Path, run_dir: Path, baseline: dict[str, Any],
                                   evaluate, pagination: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Try one local repair on a copy and keep it only after rechecking delivery evidence."""
    from longform.pagination import refresh_with_retry
    from planning.scoring_final_evidence import refresh_final_scoring

    def findings(report):
        return Counter((row.get("rule_id"), row.get("qa_class") or row.get("severity"), row.get("issue"))
                       for row in report.get("findings") or []
                       if row.get("rule_id") not in {
                           "FINAL_SCORING_MEASUREMENT_FAILED", "FINAL_SCORING_COVERAGE_BELOW_GATE"})

    report = {"status": "NO_CHANGE", "provider_calls": 0}
    try:
        with tempfile.TemporaryDirectory(dir=run_dir, prefix=".review_repair_") as work:
            candidate = Path(work) / "candidate.docx"
            shutil.copyfile(final_path, candidate)
            changes = repair_docx_artifact(candidate)["repairs"]
            report["repairs"] = changes
            if not any(count for rule, count in changes.items() if rule != "empty_heading_skipped"):
                return report, pagination
            if artifact_physically_invalid(candidate):
                return {**report, "status": "REJECTED", "reason": "INVALID_FILE"}, pagination
            new_pagination = refresh_with_retry(candidate, attempts=1)
            if new_pagination.get("status") != "REFRESHED":
                return {**report, "status": "REJECTED", "reason": "PAGINATION_NOT_VERIFIED"}, pagination
            after = evaluate(candidate)
            before_findings, after_findings = findings(baseline), findings(after)
            if (not (after_findings < before_findings)
                    or _ARTIFACT_QA_RANK.get(after.get("status"), 3) > _ARTIFACT_QA_RANK.get(baseline.get("status"), 3)):
                return {**report, "status": "REJECTED", "reason": "QA_NOT_IMPROVED"}, pagination
            before_scoring = baseline.get("final_scoring") or {}
            after_scoring = refresh_final_scoring(
                run_dir, candidate, expected_artifact_sha256=after.get("sha256"), persist=False)
            old_coverage = (before_scoring.get("fulfillment") or {}).get("technical_scoring_coverage")
            new_coverage = (after_scoring.get("fulfillment") or {}).get("technical_scoring_coverage")
            if (after_scoring.get("status") != before_scoring.get("status")
                    or after_scoring.get("status") == "FAILED"
                    or (before_scoring.get("completed_clean_allowed") and not after_scoring.get("completed_clean_allowed"))
                    or (old_coverage is not None and (new_coverage is None or new_coverage < old_coverage))):
                return {**report, "status": "REJECTED", "reason": "SCORING_NOT_PRESERVED"}, pagination
            candidate.replace(final_path)
            return {**report, "status": "APPLIED"}, {**new_pagination, "artifact_path": str(final_path)}
    except Exception as exc:
        return {**report, "status": "FAILED", "error_type": type(exc).__name__}, pagination


_NON_FORMAT_REVIEW = re.compile(r"页数|页码|PAGE|SCORING|评分|覆盖|FACT|事实|出处|PROVENANCE")


def _review_issues_are_not_format(report: dict[str, Any]) -> bool:
    findings = list((report or {}).get("findings") or [])
    if not findings:
        return False
    return all(_NON_FORMAT_REVIEW.search(
        f"{row.get('rule_id') or ''}{row.get('issue') or ''}{row.get('qa_class') or ''}") for row in findings)


def _record_account_review_repair(final_path: Path, run_dir: Path, baseline: dict[str, Any],
                                  evaluate, pagination: dict[str, Any], provider_name: str,
                                  delivery_status: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One local format repair for the whole task, including a review found only at final delivery."""
    if delivery_status != DELIVERY_REVIEW:
        return None, pagination
    from providers.cursor_account import is_cursor_provider
    if provider_name not in {"qoder_account", "zcode_cn"} and not is_cursor_provider(provider_name):
        return None, pagination
    record_path = run_dir / "local_review_repair.json"
    if record_path.is_file():
        saved = read_json(record_path)
        return saved if isinstance(saved, dict) else None, pagination
    if _review_issues_are_not_format(baseline):
        report = {"status": "NO_FORMAT_FIX", "provider_calls": 0,
                  "reason": "页数、评分覆盖或项目事实不足，保留原复核原因"}
        write_json(record_path, report)
        return report, pagination
    report, pagination = _attempt_account_review_repair(final_path, run_dir, baseline, evaluate, pagination)
    write_json(record_path, report)
    return report, pagination


def _external_delivery_eligible(cleanup: dict[str, Any], external_qa: dict[str, Any],
                                direct_qa: dict[str, Any]) -> bool:
    """对外清理件只有在已重排页码、无残留且 QA 不差于直接件时才可交付。"""
    status = str(cleanup.get("status") or "")
    if status not in {"CLEANED", "NO_CHANGES"}:
        return False
    if int(cleanup.get("residual_count") or 0) != 0 or not external_qa:
        return False
    direct_rank = _ARTIFACT_QA_RANK.get(str((direct_qa or {}).get("status")), 3)
    external_rank = _ARTIFACT_QA_RANK.get(str(external_qa.get("status")), 3)
    if external_rank > direct_rank:
        return False
    # 文字改动会移动分页；未在对外副本上重测出页码时不得交付缓存页码的副本。
    return status == "NO_CHANGES" or str((cleanup.get("pagination") or {}).get("status") or "") == "REFRESHED"


def _audit_read(path: Path, purpose: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    checked = _checked(path)
    # 分页实测读不回来就退化成 NOT_MEASURED，而"没量过"在交付复核里和
    # "量过且没问题"长得一模一样，所以这里必须总能记下一个可用的标签。
    record = {"at": now_iso(), "operation": "read", "purpose": purpose, "relative_path": _path_label(checked)}
    with ACCESS_AUDIT_FILE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


_TRANSIENT_READ_ATTEMPTS = 6


def _read_text(checked: Path) -> str:
    """读盘要能扛住"同一次原子替换"的撞车。

    Windows 上 os.replace 需要目标文件的 DELETE 访问，而 CPython 的 open() 开出的句柄
    不带 FILE_SHARE_DELETE：读的一方句柄还在，写的一方就报 [WinError 5]；反过来正被
    替换的那一瞬间去 open()，读的一方拿到 [Errno 13] Permission denied。审计类文件天生
    一边写一边读（历史页/状态页轮询 run_audit.json，生成线程同时在收尾写它），一次撞车
    就把整条交付判成失败——MMF-006E-R2 挂到 Round 3 的那条"flow_b 偶发"就是这个，
    量尺见 MMF007C_R3_20260921/r3_run_audit_readwrite_race_probe.py（25 秒内 4155 次读里
    撞出 7 次，签名与现场一致）。只重试瞬时共享冲突，预算用尽照原样抛，不把真不可读咽掉。
    """
    last_exc: PermissionError | None = None
    for attempt in range(_TRANSIENT_READ_ATTEMPTS):
        try:
            return filesystem_path(checked).read_text(encoding="utf-8-sig")
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.03 * (attempt + 1))
    raise last_exc


def read_json(path: Path, purpose: str = "runtime_data") -> Any:
    checked = _checked(path)
    _audit_read(checked, purpose)
    return json.loads(_read_text(checked))


_JSON_WRITE_LOCKS: dict[str, threading.Lock] = {}
_JSON_WRITE_LOCKS_GUARD = threading.Lock()


def _json_write_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _JSON_WRITE_LOCKS_GUARD:
        lock = _JSON_WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JSON_WRITE_LOCKS[key] = lock
        return lock


def write_json(path: Path, value: Any) -> None:
    checked = _checked(path)
    filesystem_path(checked.parent).mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    last_exc: OSError | None = None
    with _json_write_lock(checked):
        for attempt in range(8):
            # 临时名必须留在 MAX_PATH=260 以内：完整文件名再拼 pid/tid 会让深层
            # Run 目录里的每一次写盘失败。保留扩展名避免同目录同名不同后缀相撞。
            tmp = checked.with_name(f".{short_stem(checked, 24)}.{os.getpid() % 100000}.{uuid.uuid4().hex[:6]}{checked.suffix}.tmp")
            try:
                filesystem_path(tmp).write_text(payload, encoding="utf-8")
                os.replace(filesystem_path(tmp), filesystem_path(checked))
                return
            except OSError as exc:
                last_exc = exc
                time.sleep(0.04 * (attempt + 1))
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
    if last_exc:
        raise last_exc


def _write_generation_phase(run_dir: Path, phase: str, message: str, *, recoverable: bool = True) -> None:
    stage_by_phase = {
        "GENERATING": "generation_preparation",
        "TECHNICAL_RETRYING": "generation_preparation",
        "SECTION_QA": "content_and_compliance_qa",
        "AUTO_REPAIRING": "section_repair",
        "RECHECKING": "content_and_compliance_qa",
        "DOCUMENT_QA": "content_and_compliance_qa",
        "SELECTIVE_REPAIR": "section_repair",
        "DEPTH_SUPPLEMENTING": "section_repair",
        "FACT_COMMITMENT_CHECK": "content_and_compliance_qa",
        "GENERATING_IMAGES": "ppt_image_generation",
        "ASSEMBLING": "artifact_assembly",
        "FINAL_ARTIFACT_QA": "final_artifact_qa",
        "LOCAL_REVIEW_REPAIR": "final_artifact_qa",
        "LOCAL_NORMALIZING": "local_normalization",
        "FIRST_DRAFT": "first_draft_assembly",
    }
    if phase in stage_by_phase:
        set_active_stage(run_dir, stage_by_phase[phase])
    elif phase in {"TERMINAL_BLOCKED", "COMPLETED"}:
        clear_active_stage(run_dir)
    write_json(run_dir / "generation_phase.json", {
        "phase": phase,
        "message": message,
        "recoverable": recoverable,
        "terminal": phase == "TERMINAL_BLOCKED",
        "updated_at": now_iso(),
    })


def render_first_draft_file(run_dir: Path, brief: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    """把初稿渲染成可下载 DOCX，发生在任何文档级 QA 判定之前。

    渲染失败只记录状态：初稿的"存在且可下载"优先于"完美"，正式交付文件仍由
    ASSEMBLING 阶段产出，这里不覆盖它。
    """
    if str(brief.get("medium") or "") != "WORD":
        return {"status": "NOT_APPLICABLE", "path": None}
    draft_dir = run_dir / "first_draft"
    target = draft_dir / "FIRST_DRAFT.docx"
    try:
        content_path = draft_dir / "artifact_content.json"
        write_json(content_path, {"brief": brief, "artifact": artifact})
        runtime = _runtime()
        env = os.environ.copy()
        env["RUNTIME_NODE"] = str(runtime.get("node_executable", ""))
        env["RUNTIME_NODE_MODULES"] = str(runtime.get("node_modules", ""))
        env["RUNTIME_BIN_DIR"] = str(runtime.get("bin_dir", ""))
        started = time.monotonic()
        completed = subprocess.run(
            [str(runtime.get("python_executable") or sys.executable), str(APP_ROOT / "build_docx.py"), str(content_path), str(target)],
            cwd=str(APP_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, shell=False,
        )
        elapsed = round(time.monotonic() - started, 3)
        ok = completed.returncode == 0 and target.is_file() and target.stat().st_size > 0
        record_stage_duration(run_dir, "first_draft_assembly", elapsed, outcome="completed" if ok else "failed")
        if not ok:
            return {"status": "RENDER_FAILED", "path": None, "returncode": completed.returncode, "error": (completed.stderr or "")[-300:]}
        return {"status": "DOWNLOADABLE", "path": str(target), "bytes": target.stat().st_size, "seconds": elapsed}
    except Exception as exc:
        return {"status": "RENDER_FAILED", "path": None, "error": str(exc)[:300]}


def read_jsonl(path: Path, purpose: str = "asset") -> list[dict[str, Any]]:
    checked = _checked(path)
    _audit_read(checked, purpose)
    return [json.loads(line) for line in _read_text(checked).splitlines() if line.strip()]


def verify_assets() -> dict[str, Any]:
    manifest = read_json(ASSET_MANIFEST, "asset_manifest")
    failures = []
    for item in manifest.get("assets", []):
        path = _checked(ASSETS_DIR / item["relative_path"])
        if not path.is_file():
            failures.append({"relative_path": item["relative_path"], "reason": "missing"})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            failures.append({"relative_path": item["relative_path"], "reason": "hash_mismatch"})
    return {"status": "PASS" if not failures else "FAIL", "asset_count": len(manifest.get("assets", [])), "failures": failures}


def corpus_catalog() -> dict[str, Any]:
    records = read_jsonl(SNAPSHOT, "accepted_ku_snapshot")
    index = read_json(CORPUS_INDEX, "candidate_corpus_index")
    by_id = {row["knowledge_unit_id"]: row for row in records}
    index_by_id = {row["knowledge_unit_id"]: row for row in index["items"]}

    def group(membership: str) -> list[dict[str, Any]]:
        result = []
        for ku_id, meta in index_by_id.items():
            if meta["membership"] != membership:
                continue
            record = by_id[ku_id]
            result.append({
                "ku_id": ku_id,
                "core_knowledge": record.get("core_knowledge", ""),
                "applicability": record.get("applicability", ""),
                "non_applicable_conditions": record.get("non_applicable_conditions", ""),
                "reason": meta.get("reason", ""),
                "risk_tags": meta.get("risk_tags", []),
                "record": record,
            })
        return result

    return {
        "accepted_count": sum(1 for row in records if row.get("review_status") == "accepted"),
        "positive": group("candidate_positive"),
        "guardrail": group("candidate_guardrail"),
    }


def _walk_rules(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            found.append(value)
        for child in value.values():
            found.extend(_walk_rules(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_rules(child))
    return found


def style_subset() -> list[dict[str, Any]]:
    return [row for row in _walk_rules(read_json(STYLE_RULES, "writing_style_rules")) if row.get("id") in STYLE_IDS]


def provider_manager() -> ProviderManager:
    return PROVIDERS


def provider_status(refresh: bool = False) -> list[dict[str, Any]]:
    return provider_manager().list_status(refresh=refresh)


def validate_brief(brief: dict[str, Any]) -> dict[str, Any]:
    required = ["project_name", "project_type", "scenario", "medium", "requirements", "provider_name"]
    missing = [key for key in required if not str(brief.get(key, "")).strip()]
    if missing:
        raise MMFError("请补齐必填项：" + "、".join(missing))
    if brief["scenario"] not in SCENARIOS:
        raise MMFError("请选择方案场景。")
    if brief["medium"] not in MEDIA:
        raise MMFError("输出媒介必须明确选择WORD或PPT。")
    clean = {str(key): value for key, value in brief.items()}
    clean["fact_boundary"] = "表单中的全部数字均为current_project_fact；历史KU数字不得迁移。"
    clean["speed_profile"] = normalize_speed_profile(brief.get("speed_profile") or "balanced")
    clean["document_scale"] = normalize_document_scale(brief.get("document_scale") or "regular_solution")
    if brief.get("max_parallelism") not in {None, ""}:
        try:
            clean["max_parallelism"] = max(1, min(3, int(brief.get("max_parallelism"))))
        except (TypeError, ValueError):
            clean["max_parallelism"] = 3
    return clean


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _safe_filename_part(value: str, limit: int) -> str:
    clean = re.sub(r'[\\/:*?"<>|]+', "_", str(value or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip(" ._")
    return (clean or "未命名")[:limit].rstrip(" ._")


def artifact_display_path(run_dir: Path, brief: dict[str, Any]) -> Path:
    ext = ".docx" if brief["medium"] == "WORD" else ".pptx"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{_safe_filename_part(brief.get('project_name', ''), 50)}_{_safe_filename_part(brief.get('scenario', ''), 30)}_{stamp}"
    target = run_dir / f"{stem}{ext}"
    index = 1
    while target.exists():
        target = run_dir / f"{stem}_{index:02d}{ext}"
        index += 1
    return target


def _recommendation_prompt(brief: dict[str, Any], catalog: dict[str, Any]) -> str:
    candidates = [{key: row[key] for key in ("ku_id", "core_knowledge", "applicability", "non_applicable_conditions", "reason", "risk_tags")} for row in catalog["positive"]]
    guardrails = [{key: row[key] for key in ("ku_id", "core_knowledge", "reason", "risk_tags")} for row in catalog["guardrail"]]
    return f"""你负责MMF-002的知识包推荐，不生成方案正文。

【当前项目Brief】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【唯一允许推荐为正文素材的Candidate Positive】
{json.dumps(candidates, ensure_ascii=False, indent=2)}

【只用于风险控制的Candidate Guardrail】
{json.dumps(guardrails, ensure_ascii=False, indent=2)}

规则：
1. 只从Candidate Positive推荐与当前场景和项目条件直接相关的知识；不得新造KU，不得把Guardrail或not_selected作为正文素材。
2. 从Guardrail中匹配需要自动启用的风险控制，重点阻止历史项目事实、SLA、KPI、数字、日期、品牌和公司制度迁移。
3. 找出正式生成前仍缺少的current_project_fact。未知即可，不得补值。
4. 推荐理由使用Todd可理解的物业业务语言，不展示Schema字段。
5. 只返回JSON，根字段必须且只能为recommended_positive、applicable_guardrails、missing_information。
6. recommended_positive每项字段：ku_id、knowledge_name、summary、reason。
7. applicable_guardrails每项字段：ku_id、risk_content、reason。
8. missing_information为字符串数组。
"""


def _reuse_recommendation(run_dir: Path) -> dict[str, Any] | None:
    for folder in sorted(run_dir.glob("provider_recommendation*"), reverse=True):
        target = folder / "provider_structured_output.json"
        if not target.is_file():
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("recommended_positive"):
            return payload
    return None


def _recommend_knowledge_impl(brief_input: dict[str, Any], run_id: str, run_dir: Path) -> dict[str, Any]:
    from providers.cursor_account import is_cursor_provider

    brief = validate_brief(brief_input)
    provider = provider_manager().get(brief["provider_name"], require_available=False)
    catalog = corpus_catalog()
    write_json(run_dir / "brief.json", brief)
    reused = _reuse_recommendation(run_dir)
    provider_failed = False
    knowledge_warning = None
    if reused:
        result = {**reused, "provider_metadata": {**(reused.get("provider_metadata") or {}), "reused_existing_recommendation": True}}
        provider_failed = str(reused.get("knowledge_status") or "") == "PROVIDER_FAILED"
        knowledge_warning = reused.get("knowledge_warning")
    else:
        request = {
            "task_id": f"{run_id}-recommend",
            "run_id": run_id,
            "generation_mode": "knowledge_recommendation",
            "system_prompt": "你是AI物业方案智能体的封闭包知识推荐引擎。只能使用提示中的显式输入；不读文件、不联网、不调用工具；只输出一个完整JSON对象。",
            "prompt": _recommendation_prompt(
                _compact_generation_brief(brief) if is_cursor_provider(brief["provider_name"]) else brief,
                catalog,
            ),
            "brief": brief,
            "catalog": catalog,
            "reasoning_effort": "low",
            "required_keys": ["recommended_positive", "applicable_guardrails", "missing_information"],
            "json_schema": {
                "type": "object",
                "required": ["recommended_positive", "applicable_guardrails", "missing_information"],
                "properties": {
                    "recommended_positive": {"type": "array", "items": {"type": "object"}},
                    "applicable_guardrails": {"type": "array", "items": {"type": "object"}},
                    "missing_information": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
        try:
            if brief.get("medium") == "PPT" and brief.get("provider_name") == "qoder_internal":
                from planning.ppt_delivery import RECOMMEND_SECONDS
                request["generation_mode"] = "ppt_recommend"
                request["response_deadline_epoch"] = time.time() + RECOMMEND_SECONDS
                request["prompt"] += "\n本次PPT推荐最多5条直接相关知识，每条summary和reason各不超过40字；缺信息合并列出，不展开写方案。"
            result = provider.recommend_knowledge(request, _next_provider_task_dir(run_dir, "provider_recommendation"))
        except (ProviderError, ProviderUnavailableError) as exc:
            provider_failed = True
            knowledge_warning = str(exc)
            result = {
                "recommended_positive": [],
                "applicable_guardrails": [],
                "missing_information": [],
                "provider_metadata": {
                    "provider_name": brief["provider_name"],
                    "fallback_used": True,
                    "fallback_reason": "knowledge_provider_failed",
                    "error": str(exc),
                },
            }
    pos_allowed = {row["ku_id"] for row in catalog["positive"]}
    grd_allowed = {row["ku_id"] for row in catalog["guardrail"]}
    recommended = [row for row in result.get("recommended_positive") or [] if isinstance(row, dict) and row.get("ku_id") in pos_allowed]
    guardrails = [row for row in result.get("applicable_guardrails") or [] if isinstance(row, dict) and row.get("ku_id") in grd_allowed]
    result = {**result, "recommended_positive": recommended, "applicable_guardrails": guardrails, "missing_information": result.get("missing_information") if isinstance(result.get("missing_information"), list) else []}
    selection_audit = evaluate_selection(brief, catalog["positive"], recommended, result["missing_information"], {})
    contracts = build_contracts(catalog["positive"], selection_audit)
    allowed_ids = {row["ku_id"] for row in selection_audit if row["selection_status"] in {"SELECTED", "CONDITIONAL"}}
    recommended = [row for row in recommended if row.get("ku_id") in allowed_ids]
    selection = {
        "run_id": run_id,
        "status": "waiting_todd_knowledge_confirmation",
        "provider_name": brief["provider_name"],
        "recommended_positive": recommended,
        "applicable_guardrails": guardrails,
        "missing_information": result["missing_information"],
        "selection_audit": selection_audit,
        "knowledge_usage_contracts": contracts,
        "auto_selected_positive_ids": [row["ku_id"] for row in selection_audit if row["selection_status"] == "SELECTED" and row["provider_recommended"]],
        "conditional_confirmations": [
            {"ku_id": row["ku_id"], "question": row["human_confirmation_question"], "default": "exclude"}
            for row in contracts if row["selection_status"] == "CONDITIONAL" and row.get("human_confirmation_question")
        ],
        "provider_metadata": result.get("provider_metadata") or {},
    }
    attach_recommendation_state(selection, provider_failed=provider_failed, warning=knowledge_warning, confirmed=False)
    write_json(run_dir / "knowledge_selection.json", selection)
    return selection


def _recommend_knowledge_in_run(brief_input: dict[str, Any], run_id: str, run_dir: Path) -> dict[str, Any]:
    with track_stage(run_dir, "knowledge_recommendation"):
        return _recommend_knowledge_impl(brief_input, run_id, run_dir)


def retry_knowledge_recommendation(run_id: str) -> dict[str, Any]:
    """Ask the same run's account again. Does not create a run or reuse a failed selection."""
    run_dir = _checked(RUNS_DIR / run_id)
    brief_path = run_dir / "brief.json"
    if not brief_path.is_file():
        raise MMFError("尚未形成项目简报，不能重新推荐。")
    if (run_dir / "generation_status.json").is_file():
        raise MMFError("正式初稿已开始，不再另行请求知识推荐。")
    brief = read_json(brief_path, "brief")
    from providers.openai_compatible import bind_run, unbind_run
    bind_run(run_id)
    try:
        return _recommend_knowledge_in_run(brief, run_id, run_dir)
    finally:
        unbind_run(run_id)


def recommend_knowledge(brief_input: dict[str, Any]) -> dict[str, Any]:
    run_id = new_run_id()
    run_dir = _checked(RUNS_DIR / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    ensure_workflow(run_dir, "new_plan")
    try:
        with track_stage(run_dir, "project_input_processing", workflow_kind="new_plan"):
            validate_brief(brief_input)
        result = _recommend_knowledge_in_run(brief_input, run_id, run_dir)
        return {**result, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id")}
    except Exception:
        finish_workflow(run_dir, "failed")
        raise


def _write_mmf006a_state(**changes: Any) -> dict[str, Any]:
    current = read_json(MMF006A_STATE_FILE, "mmf006a_state") if MMF006A_STATE_FILE.exists() else {
        "task": "MMF-006A Tender Intake & Requirement Extraction",
        "status": "in_progress",
        "checkpoints": {
            "A1_UPLOAD_PASS": False, "A2_EXTRACTION_PASS": False, "A3_PACK_BUILDER_PASS": False,
            "A4_PROVIDER_UNDERSTANDING_PASS": False, "A5_CONFIRMATION_UX_PASS": False,
            "A6_BRIEF_SEED_PASS": False, "A7_MMF005_REGRESSION_PASS": False,
        },
        "mmf006b_started": False,
    }
    for key, value in changes.items():
        if key == "checkpoints" and isinstance(value, dict):
            current.setdefault("checkpoints", {}).update(value)
        elif key == "provider_test" and isinstance(value, dict):
            current.setdefault("provider_test", {}).update(value)
        else:
            current[key] = value
    current["updated_at"] = now_iso()
    write_json(MMF006A_STATE_FILE, current)
    return current


def create_tender_run(files: list[tuple[str, bytes]], provider_name: str) -> dict[str, Any]:
    provider_manager().get(provider_name, require_available=False)
    run_id = new_run_id("tender")
    run_dir = _checked(RUNS_DIR / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    workflow = ensure_workflow(run_dir, "tender_file")
    try:
        with track_stage(run_dir, "upload_and_file_processing", workflow_kind="tender_file"):
            uploads = save_tender_uploads(RUNTIME_ROOT, run_dir, files)
        _write_mmf006a_state(run_id=run_id, upload={"status": "PASS", "file_count": len(uploads)}, checkpoints={"A1_UPLOAD_PASS": True})
        with track_stage(run_dir, "local_parse", workflow_kind="tender_file"):
            extraction = extract_tender_run(RUNTIME_ROOT, run_dir)
        status = {
            "schema_version": "mmf006a-status-v0.1", "run_id": run_id, "provider_name": provider_name,
            "workflow_id": workflow["workflow_id"],
            "upload_completed": True, "extraction_completed": True, "chunks_total": 0, "chunks_completed": 0,
            "failed_chunk": None, "pack_status": "not_started", "stage": "A2_EXTRACTION_PASS", "updated_at": now_iso(),
        }
        write_json(run_dir / "tender" / "status.json", status)
        _write_mmf006a_state(extraction={"status": "PASS", "processing_mode": extraction["processing_mode"]}, checkpoints={"A2_EXTRACTION_PASS": True})
        return {"run_id": run_id, "workflow_id": workflow["workflow_id"], "status": "extraction_completed", "provider_name": provider_name, "uploads": uploads, "extraction_summary": {"processing_mode": extraction["processing_mode"], "files": len(extraction["files"]), "pages": len(extraction["pages"]), "paragraphs": len(extraction["paragraphs"]), "tables": len(extraction["tables"]), "warnings": extraction["warnings"]}}
    except Exception:
        finish_workflow(run_dir, "failed")
        if not (run_dir / "tender" / "status.json").exists():
            write_json(run_dir / "tender" / "status.json", {"run_id": run_id, "stage": "A2_EXTRACTION_FAILED", "upload_completed": (run_dir / "tender" / "uploads.json").exists(), "extraction_completed": False, "updated_at": now_iso()})
        raise


TENDER_STALE_GRACE_SECONDS = 20
TENDER_FAILED_PACK_STATUSES = {"understanding_failed", "cancelled", "failed", "stale"}
TENDER_ACTIVE_PACK_STATUSES = {"understanding"}
TENDER_ACTIVE_STAGES = {"A4_PROVIDER_UNDERSTANDING"}


def _tender_status_file(run_id: str) -> Path:
    return RUNS_DIR / run_id / "tender" / "status.json"


def read_tender_status(run_id: str) -> dict[str, Any]:
    path = _tender_status_file(run_id)
    if not path.is_file():
        return {"run_id": run_id}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"run_id": run_id}
    return payload if isinstance(payload, dict) else {"run_id": run_id}


def write_tender_status(run_id: str, updates: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
    current = dict(updates) if replace else {**read_tender_status(run_id), **updates}
    current["run_id"] = run_id
    current["updated_at"] = now_iso()
    write_json(_tender_status_file(run_id), current)
    return current


def mark_tender_processing(run_id: str, provider_name: str | None = None) -> dict[str, Any]:
    current = read_tender_status(run_id)
    for key in ("error", "error_code", "error_type", "error_message", "warning"):
        current.pop(key, None)
    selected = str(provider_name or current.get("provider_name") or "")
    engine = engine_display(selected)
    current.update({
        "provider_name": selected or current.get("provider_name"),
        "upload_completed": True,
        "extraction_completed": True,
        "pack_status": "understanding",
        "stage": "A4_PROVIDER_UNDERSTANDING",
        "failed_chunk": None,
        "understanding_started_at": current.get("understanding_started_at") or now_iso(),
        "engine_label": engine,
        "message": f"正在调用{engine}识别需求。",
        "recognition_outcome": "AI_RECOGNITION_PENDING",
        "local_fallback_confirmation_required": False,
        "local_fallback_confirmed": False,
    })
    return write_tender_status(run_id, current, replace=True)


def mark_tender_failed(run_id: str, exc: BaseException | str, *, error_code: str | None = None) -> dict[str, Any]:
    current = read_tender_status(run_id)
    if str(current.get("pack_status") or "") == "cancelled":
        return current
    if current.get("pack") or (RUNS_DIR / run_id / "tender" / "requirement_pack.json").is_file():
        return current
    from user_errors import classify
    raw = exc
    if isinstance(exc, BaseException) and getattr(exc, "__cause__", None) is not None:
        generic = {"需求识别未完成。本地文件已解析保留，请重试识别。", "任务未正常完成，请重试"}
        if str(exc).strip() in generic:
            raw = exc.__cause__
    if isinstance(raw, str):
        mapped = {"error": raw, "error_code": error_code or "UNDERSTAND_FAILED", "detail": raw}
        error_type = "TenderJobError"
    else:
        mapped = classify(raw)
        error_type = type(raw).__name__
    reason = str(mapped.get("error") or "任务未正常完成，请重试").strip()
    if reason in {"生成失败，请查看运行日志", "找不到这次生成任务"}:
        if isinstance(raw, OSError) or "WinError 5" in str(raw) or "拒绝访问" in str(raw):
            reason = "任务状态写入冲突，请再点一次重新识别"
        else:
            reason = str(raw) if any("\u4e00" <= ch <= "\u9fff" for ch in str(raw)) else "任务未正常完成，请重试"
    user = reason if reason.startswith("需求识别失败") else f"需求识别失败：{reason}"
    finish_workflow(RUNS_DIR / run_id, "failed")
    return write_tender_status(run_id, {
        "pack_status": "understanding_failed",
        "stage": "A4_PROVIDER_UNDERSTANDING_FAILED",
        "error": user,
        "error_code": error_code or mapped.get("error_code") or "UNDERSTAND_FAILED",
        "error_type": error_type,
        "error_message": mapped.get("detail") or reason,
        "message": user,
    })


def reconcile_tender_status(run_id: str, status: dict[str, Any] | None = None, *, worker_alive: bool = False) -> dict[str, Any]:
    current = dict(status or read_tender_status(run_id))
    pack_status = str(current.get("pack_status") or "")
    stage = str(current.get("stage") or "")
    if pack_status in TENDER_FAILED_PACK_STATUSES or current.get("error"):
        return current
    if (RUNS_DIR / run_id / "tender" / "requirement_pack.json").is_file():
        return current
    active = pack_status in TENDER_ACTIVE_PACK_STATUSES or stage in TENDER_ACTIVE_STAGES
    if not active:
        return current
    if worker_alive:
        return current
    age = duration_seconds(current.get("updated_at") or current.get("understanding_started_at"))
    if age < TENDER_STALE_GRACE_SECONDS:
        return current
    return mark_tender_failed(run_id, "任务未正常完成，请重试", error_code="TASK_STALE")


def process_tender_run(run_id: str, provider_name: str | None = None) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    ensure_workflow(run_dir, "tender_file" if (run_dir / "tender").is_dir() else "new_plan")
    status_path = run_dir / "tender" / "status.json"
    status = read_json(status_path, "tender_status")
    selected_provider = provider_name or status.get("provider_name")
    if str(status.get("pack_status") or "") != "understanding":
        mark_tender_processing(run_id, selected_provider)
    # Short pre-generation task: do not gate on longform health/probe/admission.
    provider = provider_manager().get(selected_provider, require_available=False)
    extraction = read_json(run_dir / "tender" / "extraction.json", "tender_extraction")
    try:
        with track_stage(run_dir, "requirement_recognition_and_structuring", workflow_kind="tender_file"):
            result = understand_tender_run(run_dir, extraction, provider, resume=True)
    except Exception:
        finish_workflow(run_dir, "failed")
        raise
    recognition_success = result["status"].get("recognition_outcome") == "AI_RECOGNITION_SUCCESS"
    from tender_intake.cursor_recognition_recovery import WAITING
    recognition_state = ("PASS" if recognition_success else
                         "WAITING_FOR_USER" if result["status"].get("recognition_outcome") == WAITING else
                         "FAILED_WITH_LOCAL_FALLBACK")
    _write_mmf006a_state(
        run_id=run_id, provider_test={selected_provider: recognition_state},
        pack={"status": result["pack"]["status"], "requirements": len(result["pack"]["requirements"])},
        checkpoints={"A3_PACK_BUILDER_PASS": True, "A4_PROVIDER_UNDERSTANDING_PASS": recognition_success},
    )
    pack = {**result["pack"], "brief_project_fields": brief_project_fields(result["pack"], extraction)}
    return {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "status": pack["status"], "pack": pack, "checkpoint": result["status"]}


def _tender_pack_for_brief(pack: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    extraction_path = run_dir / "tender" / "extraction.json"
    extraction = read_json(extraction_path, "tender_extraction") if extraction_path.is_file() else {}
    return {**pack, "brief_project_fields": brief_project_fields(pack, extraction)}


def load_tender_run(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    tender_dir = run_dir / "tender"
    status = read_tender_status(run_id)
    from tender_intake.cursor_recognition_recovery import FALLBACK, WAITING, pending_choice
    if status.get("recognition_outcome") == FALLBACK and pending_choice(run_dir, status):
        status = {**status, "recognition_outcome": WAITING}
    pack = None
    pack_path = tender_dir / "requirement_pack.json"
    if pack_path.is_file() and str(status.get("pack_status") or "") != "understanding":
        try:
            loaded = json.loads(pack_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                pack = _tender_pack_for_brief(loaded, run_dir)
        except (OSError, json.JSONDecodeError, TypeError):
            pack = None
    return {"run_id": run_id, "status": status, "pack": pack, "brief_seeded": (run_dir / "brief.json").exists(), "knowledge_recommended": (run_dir / "knowledge_selection.json").exists()}


def acknowledge_tender_local_fallback(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    pack_path = run_dir / "tender" / "requirement_pack.json"
    if not run_dir.is_dir() or not pack_path.is_file():
        raise MMFError("本地解析结果不存在，请重新识别。")
    status = read_tender_status(run_id)
    from tender_intake.cursor_recognition_recovery import FALLBACK, WAITING
    if status.get("recognition_outcome") not in {FALLBACK, WAITING}:
        raise MMFError("当前任务不需要确认本地解析降级。")
    status.update({
        "recognition_outcome": FALLBACK,
        "local_fallback_confirmed": True,
        "local_fallback_confirmed_at": now_iso(),
        "message": "已确认继续使用本地解析结果；生成完整方案前仍应人工核对关键要求。",
    })
    write_json(run_dir / "tender" / "status.json", status)
    pack = _tender_pack_for_brief(read_json(pack_path, "tender_requirement_pack"), run_dir)
    return {"run_id": run_id, "status": status, "pack": pack}


def confirm_tender_run(run_id: str, decisions: dict[str, Any], brief_options: dict[str, Any]) -> dict[str, Any]:
    # Validate the externally supplied vocabulary before mutating the Pack.
    if brief_options.get("scenario") not in SCENARIOS:
        raise MMFError("请选择方案场景。")
    if brief_options.get("medium") and brief_options["medium"] not in MEDIA:
        raise MMFError("输出媒介必须明确选择WORD或PPT。")
    run_dir = _checked(RUNS_DIR / run_id)
    pack_path = run_dir / "tender" / "requirement_pack.json"
    if not pack_path.is_file():
        raise MMFError("Requirement Pack尚未生成。")
    tender_status = read_tender_status(run_id)
    from tender_intake.cursor_recognition_recovery import WAITING, pending_choice
    if tender_status.get("recognition_outcome") == WAITING or pending_choice(run_dir, tender_status):
        raise MMFError("AI需求识别已暂停，请先在「账号任务进度与处理」选择处理方式。")
    full_proposal = str(brief_options.get("scenario") or "") in FULL_PROPOSAL_SCENARIOS
    fallback_requires_confirmation = (
        tender_status.get("recognition_outcome") == "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK"
        and not tender_status.get("local_fallback_confirmed")
    )
    if full_proposal and fallback_requires_confirmation:
        raise MMFError("AI需求识别未完成。请先重新识别，或明确选择‘继续使用本地解析结果’后再生成完整方案。")
    from providers.openai_compatible import bind_run, unbind_run
    bind_run(run_id)
    try:
        with track_stage(run_dir, "brief_generation", workflow_kind="tender_file"):
            pack = read_json(pack_path, "tender_requirement_pack")
            decisions = dict(decisions or {})
            if "accept_remaining" not in decisions:
                decisions["accept_remaining"] = True
            confirmed = apply_tender_confirmation(pack, decisions)
            validate_requirement_pack(confirmed)
            view_pack = _tender_pack_for_brief(confirmed, run_dir)
            brief = validate_brief(seed_tender_brief(
                confirmed, brief_options, project_fields=view_pack["brief_project_fields"],
            )) if confirmed["confirmation"]["ready_for_brief_seed"] else None
            write_json(pack_path, confirmed)
            write_json(run_dir / "tender" / "confirmation.json", {"confirmed_at": now_iso(), "decisions": decisions, "pack_version": confirmed.get("pack_version")})
            if not confirmed["confirmation"]["ready_for_brief_seed"]:
                write_json(run_dir / "tender" / "status.json", {"run_id": run_id, "stage": "A5_CONFIRMATION_REQUIRED", "upload_completed": True, "extraction_completed": True, "pack_status": "blocked_clarification", "updated_at": now_iso()})
                return {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "status": "blocked_clarification", "pack": view_pack, "message": "仍有高影响要求、冲突、疑似模板或缺失事实待确认。"}
            write_json(run_dir / "brief.json", brief)
            _write_mmf006a_state(run_id=run_id, confirmation={"status": "PASS"}, brief_seed={"status": "PASS"}, checkpoints={"A5_CONFIRMATION_UX_PASS": True, "A6_BRIEF_SEED_PASS": True})
        recommendation = _recommend_knowledge_in_run(brief, run_id, run_dir)
        write_json(run_dir / "tender" / "status.json", {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "stage": "MMF006A_COMPLETED_PENDING_TODD_ACCEPTANCE", "upload_completed": True, "extraction_completed": True, "pack_status": "ready_for_plan", "confirmation_completed": True, "brief_seed_completed": True, "knowledge_recommendation_completed": True, "updated_at": now_iso()})
        return {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "status": "MMF006A_completed_pending_todd_acceptance", "pack": view_pack, "brief": brief, "recommendation": recommendation}
    except Exception:
        finish_workflow(run_dir, "failed")
        raise
    finally:
        unbind_run(run_id)


def _compact_generation_brief(brief: dict[str, Any]) -> dict[str, Any]:
    slim = dict(brief)
    seen: dict[str, str] = {}
    for key in ("requirements", "service_scope", "client_requirements"):
        value = str(slim.get(key) or "").strip()
        if not value:
            continue
        if value in seen:
            slim[key] = f"（同{seen[value]}）"
        else:
            seen[value] = key
    return slim


def _generation_prompt(brief: dict[str, Any], positives: list[dict[str, Any]], guardrails: list[dict[str, Any]], clarification_answers: dict[str, str], knowledge_usage_contracts: list[dict[str, Any]] | None = None, repair_constraints: list[str] | None = None) -> str:
    medium = brief["medium"]
    full_proposal = brief.get("scenario") in FULL_PROPOSAL_SCENARIOS
    if medium == "WORD":
        artifact_schema = {
            "title": "方案标题", "subtitle": "项目名称｜完整物业服务方案",
            "lead": ["1-2段导语"],
            "sections": [{"heading": "一级标题", "paragraphs": ["自然正文"], "bullets": ["必要要点"], "table": {"columns": ["列名"], "rows": [["单元格"]]}}],
        }
        scope_rule = "当前是完整物业服务方案。请按本项目Brief和招标实际覆盖的专业条线组织章节，把已要求的内容写到可实施的措施层（目标、做法、责任、检查与成果）。不要因为输出JSON就改成提纲，也不要把多个已要求的专业条线收进同一章用两三句话带过。若某条线招标并未要求，可以不单列。"
    else:
        artifact_schema = {
            "title": "方案标题", "subtitle": "项目名称｜完整物业服务方案",
            "slides": [{"title": "观点式页标题", "core_message": "本页核心信息", "layout": "overview|table|process|modules|responsibility_matrix|timeline|comparison", "bullets": ["要点"], "table": {"columns": ["列名"], "rows": [["单元格"]]}, "steps": [{"title": "步骤", "body": "动作"}], "modules": [{"title": "模块", "body": "措施"}]}],
        }
        scope_rule = "PPT必须覆盖完整服务方案主线，生成8-16页，一页一个主题；不得只做3-6页模块摘录。"
    if not full_proposal:
        scope_rule = "PPT生成3-6页，标题页保持简洁；每页只承担一个主要信息任务。WORD生成完整的一个场景章节。"
    profile_key = "Profile-W" if medium == "WORD" else "Profile-P"
    return f"""你负责正式物业服务方案生成。输出媒介为{medium}，必须按对应Output Medium Profile组织内容。

【当前项目Brief｜优先级最高，全部数字均为current_project_fact】
{json.dumps(_compact_generation_brief(brief), ensure_ascii=False, indent=2)}

【Todd确认的Positive知识｜只作为方法、经验和结构参考】
{json.dumps(positives, ensure_ascii=False, indent=2)}

【Knowledge Usage Contracts｜本次使用权威】
{json.dumps(knowledge_usage_contracts or [], ensure_ascii=False, indent=2)}

【自动启用的Guardrail｜只用于阻止迁移，禁止成为正文素材】
{json.dumps(guardrails, ensure_ascii=False, indent=2)}

【Todd补充或保持未知的澄清项】
{json.dumps(clarification_answers, ensure_ascii=False, indent=2)}

【User Writing Style Model V0.2子集】
{json.dumps(style_subset(), ensure_ascii=False, indent=2)}

【Generation Layer Patch V0.2】
{json.dumps(read_json(GENERATION_PATCH, 'generation_layer'), ensure_ascii=False, indent=2)}

【Output Medium Profile V0.1】
{json.dumps(read_json(OUTPUT_PROFILE, 'output_medium_profile')['profiles'][profile_key], ensure_ascii=False, indent=2)}

【Compliance Repair Constraints｜必须逐条执行】
{json.dumps(repair_constraints or [], ensure_ascii=False, indent=2)}

生成规则：
1. 优先使用当前项目事实；历史KU只能提供方法、经验和结构。
2. 禁止迁移历史项目事实、历史SLA/KPI、数字、日期、品牌和公司承诺。未知内容不得补值，进入clarification_list。
3. 正式正文不得出现KU ID、Guardrail、AI提示、安全说明、实验说明、引用调试信息或结构标签。
4. 使用自然物业专业语言；不用伪专业术语；不把内部审查信息写进客户正文。
5. WORD采用连续可读章节、自然正文和必要真实表格；PPT一页一个主题，使用表格、流程、模块、责任矩阵等页面结构，禁止把Word长段落塞进页面。
6. {scope_rule}
7. 只输出JSON，根字段必须且只能为artifact、citation_registry、guardrail_non_use、clarification_list。
8. artifact严格使用下列结构；不适用的table/steps/modules可省略，不得输出空的占位说明：
{json.dumps(artifact_schema, ensure_ascii=False, indent=2)}
9. citation_registry每项包含claim、source_type(current_project_fact或positive_ku)、source_id；guardrail_non_use每项包含ku_id、not_used_content。
10. 输出结束前必须检查根对象同时包含artifact、citation_registry、guardrail_non_use、clarification_list四个字段；后三项即使为空也必须输出空数组，禁止只返回artifact后提前结束。
11. 必须遵守每条Knowledge Usage Contract的allowed_usage、required_conditions、forbidden_escalations和language_level；条件未满足时只能使用frontend_conditional_phrasing。
"""


def _public_positive(catalog: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    by_id = {row["ku_id"]: row for row in catalog["positive"]}
    return [{key: by_id[ku_id][key] for key in ("ku_id", "core_knowledge", "applicability", "non_applicable_conditions")} for ku_id in ids if ku_id in by_id]


def _public_guardrails(catalog: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    by_id = {row["ku_id"]: row for row in catalog["guardrail"]}
    return [{key: by_id[ku_id][key] for key in ("ku_id", "core_knowledge", "reason", "risk_tags")} for ku_id in ids if ku_id in by_id]


def _attach_ppt_images(run_dir: Path, artifact: dict[str, Any], writing_provider: str = "") -> None:
    """Use reviewed assets first and fill missing scenes with the selected provider."""
    from first_run import current_settings, resolve_image_provider
    from providers.account_image import ACCOUNT_IMAGE_NAMES

    settings = current_settings()
    image_name = resolve_image_provider(settings, writing_provider)
    images_enabled = bool(settings.get("enable_image_provider", True))
    if artifact.get("profile_p_contract"):
        from project_assets import attach_project_images
        from providers.account_image import fill_planned_scenes
        from functools import partial

        fill_scenes = None
        scene_policy = "image_service_disabled" if not images_enabled else "other_image_provider"
        if images_enabled and image_name in ACCOUNT_IMAGE_NAMES:
            provider = provider_manager().providers.get(image_name)
            scene_policy = "provider_not_registered"
            if provider is not None:
                def show_progress(current: int, total: int) -> None:
                    _write_generation_phase(run_dir, "GENERATING_IMAGES", f"正在生成PPT场景配图（第{current}/{total}张）。")

                fill_scenes = partial(fill_planned_scenes, provider=provider, on_attempt=show_progress)
                scene_policy = "automatic"
        attach_project_images(run_dir, artifact, fill_scenes=fill_scenes, scene_policy=scene_policy)
        return
    chapter_prompts = {
        "P01": "现代高端商务写字楼外立面与入口大堂，专业礼宾服务，商务摄影风格，冷色调，无文字",
        "P02": "现代化物业运营指挥中心，多屏监控大屏与专业管理团队，商务摄影风格，冷色调，无文字",
        "P03": "高端写字楼前台礼宾服务场景，专业客服人员，明亮整洁，商务摄影风格，无文字",
        "P04": "专业安保人员在商务园区巡逻，秩序井然，商务纪实摄影，冷色调，无文字",
        "P05": "物业工程人员在设备机房进行专业巡检维护，工业质感，商务摄影，无文字",
        "P06": "现代园区绿化与环境养护场景，整洁明亮，商务摄影风格，自然光，无文字",
        "P07": "项目管理团队在会议室进行服务品质复盘汇报，专业协作氛围，商务摄影，无文字",
    }
    try:
        if not images_enabled:
            return
        image_provider = provider_manager().get(image_name, require_available=False)
    except Exception:
        image_provider = None
    if image_provider is None:
        write_json(run_dir / "images" / "image_audit.json", {"skipped": True, "reason": "image provider unavailable"})
        return
    image_dir = run_dir / "images"
    images: dict[str, Any] = {}
    if image_name in ACCOUNT_IMAGE_NAMES:
        _write_generation_phase(run_dir, "GENERATING_IMAGES", "正在生成PPT封面与章节配图。")
    try:
        result = image_provider.generate_image(
            "现代高端商务楼宇物业服务方案汇报封面背景，玻璃幕墙写字楼与专业服务团队剪影，深蓝绿色调，留白构图，商务摄影，无文字无水印",
            image_dir, size="1024*1024",
        )
        images["cover"] = result["path"]
    except Exception as exc:
        write_json(image_dir / "image_audit.json", {"cover": "failed", "error": str(exc)[:200]})
    by_chapter: dict[str, str] = {}
    chapter_ids = sorted({str(slide.get("chapter_id") or "") for slide in (artifact.get("slides") or []) if slide.get("chapter_id")})
    for cid in chapter_ids[:2 if image_name in ACCOUNT_IMAGE_NAMES else 5]:
        prompt = chapter_prompts.get(cid) or chapter_prompts["P02"]
        try:
            result = image_provider.generate_image(prompt, image_dir, size="720*1280")
            by_chapter[cid] = result["path"]
        except Exception:
            continue
    if by_chapter:
        images["by_chapter"] = by_chapter
    if images:
        artifact["images"] = images


def _runtime() -> dict[str, Any]:
    if RUNTIME_CONFIG.exists():
        return read_json(RUNTIME_CONFIG, "runtime_config")
    return {
        "python_executable": sys.executable,
        "node_executable": os.environ.get("RUNTIME_NODE") or shutil.which("node") or "node",
        "node_modules": os.environ.get("RUNTIME_NODE_MODULES", ""),
        "bin_dir": os.environ.get("RUNTIME_BIN_DIR", ""),
    }


def _next_provider_task_dir(run_dir: Path, stem: str) -> Path:
    candidate = run_dir / stem
    if not candidate.exists():
        return candidate
    index = 1
    while (run_dir / f"{stem}_retry_{index}").exists():
        index += 1
    return run_dir / f"{stem}_retry_{index}"


def estimate_generation_runtime(provider_name: str, speed_profile: str = "balanced", model: str = "", stage: str = "draft", section_count: int | None = None) -> dict[str, Any]:
    history = RUNTIME_DIR / "latency_history.jsonl"
    return estimate_runtime(
        provider=provider_name,
        model=model,
        mode=normalize_speed_profile(speed_profile),
        stage=stage,
        history_path=history,
        section_count=section_count,
    )


def generate_artifact(run_id: str, selected_positive_ids: list[str], clarification_answers: dict[str, str], auto_repair: bool = True, speed_profile: str | None = None, max_parallelism: int | None = None, trust_submitted_selection: bool = False) -> dict[str, Any]:
    wall_started = time.monotonic()
    longform_result: dict[str, Any] | None = None
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    ensure_workflow(run_dir, "tender_file" if (run_dir / "tender").is_dir() else "new_plan")
    _write_generation_phase(run_dir, "GENERATING", "正在生成方案内容。")
    brief = read_json(run_dir / "brief.json", "run_brief")
    recommendation = read_json(run_dir / "knowledge_selection.json", "knowledge_selection")
    provider_name = recommendation["provider_name"]
    provider = provider_manager().get(provider_name, require_available=True)
    recommended_ids = {row["ku_id"] for row in recommendation.get("recommended_positive") or [] if isinstance(row, dict) and row.get("ku_id")}
    auto_selected = [ku_id for ku_id in (recommendation.get("auto_selected_positive_ids") or []) if ku_id in recommended_ids]
    submitted = list(selected_positive_ids or [])
    selected = resolve_selected_positive_ids(
        recommended_ids,
        submitted if trust_submitted_selection else [*auto_selected, *submitted],
    )
    provider_failed = str(recommendation.get("knowledge_status") or "") == "PROVIDER_FAILED" or bool((recommendation.get("provider_metadata") or {}).get("fallback_reason") == "knowledge_provider_failed")
    attach_recommendation_state(
        recommendation,
        provider_failed=provider_failed,
        warning=recommendation.get("knowledge_warning"),
        selected_ids=selected,
        confirmed=True,
    )
    if not generation_may_proceed(str(recommendation.get("knowledge_status") or "")):
        raise MMFError("知识子系统内部错误，请重试推荐后再生成。")
    guardrail_ids = [row["ku_id"] for row in recommendation.get("applicable_guardrails") or [] if isinstance(row, dict) and row.get("ku_id")]
    catalog = corpus_catalog()
    positives = _public_positive(catalog, selected)
    guardrails = _public_guardrails(catalog, guardrail_ids)
    contracts = [row for row in recommendation.get("knowledge_usage_contracts", []) if row.get("ku_id") in selected]
    selection = {
        **recommendation,
        "status": "todd_confirmed_for_generation",
        "selected_positive_ku_ids": selected,
        "active_guardrail_ids": guardrail_ids,
        "clarification_answers": clarification_answers,
        "knowledge_usage_contracts": contracts,
        "confirmed_at": now_iso(),
    }
    write_json(run_dir / "knowledge_selection.json", selection)
    if speed_profile:
        brief["speed_profile"] = normalize_speed_profile(speed_profile)
    else:
        brief["speed_profile"] = normalize_speed_profile(brief.get("speed_profile") or "balanced")
    if max_parallelism is not None:
        brief["max_parallelism"] = max(1, min(3, int(max_parallelism)))
    write_json(run_dir / "brief.json", brief)
    planned_sections = None
    plan_path = run_dir / "01_word_document_plan.json"
    if plan_path.is_file():
        try:
            outline = read_json(plan_path, "word_plan").get("outline") or []
            planned_sections = sum(len(chapter.get("sections") or []) for chapter in outline)
        except Exception:
            planned_sections = None
    eta_before = estimate_generation_runtime(
        provider_name,
        brief["speed_profile"],
        str(getattr(provider, "config", {}).get("model") if isinstance(getattr(provider, "config", {}), dict) else ""),
        section_count=planned_sections,
    )
    profile = "Profile-W" if brief["medium"] == "WORD" else "Profile-P"
    generation_input = {
        "current_project_brief": brief,
        "todd_confirmed_positive_kus": positives,
        "applicable_guardrails": guardrails,
        "clarification_answers": clarification_answers,
        "generation_layer_version": "Generation Layer Patch V0.2",
        "output_medium_profile": profile,
        "provider_name": provider_name,
    }
    write_json(run_dir / "generation_input.json", generation_input)
    ensure_requirement_pack(run_dir, brief)
    request = {
        "task_id": f"{run_id}-generate",
        "system_prompt": "你是AI物业方案智能体的封闭包分析与生成引擎。只能使用提示中的显式输入；不读文件、不联网、不调用工具；只输出一个完整JSON对象。",
        "prompt": _generation_prompt(brief, positives, guardrails, clarification_answers, contracts),
        "brief": brief,
        "positives": positives,
        "guardrails": guardrails,
        "knowledge_usage_contracts": contracts,
        "clarification_answers": clarification_answers,
        "required_keys": ["artifact", "citation_registry", "guardrail_non_use", "clarification_list"],
        "json_schema": {
            "type": "object",
            "required": ["artifact", "citation_registry", "guardrail_non_use", "clarification_list"],
        },
    }
    record_stage_duration(run_dir, "generation_preparation", time.monotonic() - wall_started)
    set_active_stage(run_dir, "longform_generation")
    longform_started = time.monotonic()
    try:
        longform_result = generate_longform(
            run_dir=run_dir,
            provider=provider,
            provider_name=provider_name,
            brief=brief,
            selection=selection,
            selected_ids=selected,
            speed_profile=brief.get("speed_profile"),
            max_parallelism=brief.get("max_parallelism"),
            history_path=RUNTIME_DIR / "latency_history.jsonl",
        )
        longform_wall = round(time.monotonic() - longform_started, 3)
        longform_performance = longform_result.get("performance") or {}
        record_longform_attempt(
            run_dir,
            longform_wall,
            planning_seconds=_numeric_seconds(longform_performance.get("planning_seconds")),
            repair_seconds=_numeric_seconds(longform_performance.get("repair_seconds")),
        )
        _write_generation_phase(run_dir, "SECTION_QA", "正在检查方案章节完整性。")
    except PlanningError as exc:
        record_stage_duration(run_dir, "document_planning", time.monotonic() - longform_started, outcome="failed")
        actual_time = round(time.monotonic() - wall_started, 3)
        _write_run_audit(run_dir, {
            "run_id": run_id,
            "status": "planning_blocked",
            "partial": True,
            "phase": "planning",
            "error": str(exc),
            "error_type": "PlanningError",
            "qa_seconds": 0.0,
            "actual_time": actual_time,
            "planning_seconds": actual_time,
            "generation_seconds": 0.0,
            "provider_wait_seconds": 0.0,
            "render_seconds": 0.0,
            "repair_reasoning": "not_invoked",
            "runtime_source": (_mmf_paths.load_build_manifest().get("runtime_source") or "source"),
            "build_id": (_mmf_paths.load_build_manifest().get("build_id") or "dev-unpacked"),
            "TEST_TARGET": "production_dist" if (_mmf_paths.load_build_manifest().get("runtime_source") == "dist") else "source",
            "created_at": now_iso(),
        })
        raise MMFError(str(exc)) from exc
    except Exception as exc:
        record_stage_duration(run_dir, "longform_generation", time.monotonic() - longform_started, outcome="failed")
        actual_time = round(time.monotonic() - wall_started, 3)
        performance = ((longform_result or {}).get("performance") or {}) if longform_result else {}
        phase = str(performance.get("phase") or "generation")
        error_id = _write_generation_error_diagnostics(run_dir, run_id, exc, phase=phase, provider=provider_name, recoverable=True)
        audit = {
            "run_id": run_id,
            "status": "generation_exception",
            "partial": True,
            "phase": phase,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "qa_seconds": _numeric_seconds(performance.get("qa_seconds")),
            "actual_time": actual_time,
            "planning_seconds": _numeric_seconds(performance.get("planning_seconds")),
            "generation_seconds": _numeric_seconds(performance.get("generation_seconds"), 0.0 if phase == "planning" else actual_time),
            "provider_wait_seconds": _numeric_seconds(performance.get("provider_wait_seconds")),
            "render_seconds": 0.0,
            "repair_reasoning": _repair_reasoning_value(longform_result),
            **_longform_runtime_audit(longform_result),
            "runtime_source": (_mmf_paths.load_build_manifest().get("runtime_source") or "source"),
            "build_id": (_mmf_paths.load_build_manifest().get("build_id") or "dev-unpacked"),
            "TEST_TARGET": "production_dist" if (_mmf_paths.load_build_manifest().get("runtime_source") == "dist") else "source",
            "created_at": now_iso(),
        }
        try:
            _write_run_audit(run_dir, audit)
            _record_terminal_latency(
                run_id=run_id,
                brief=brief,
                provider_name=provider_name,
                longform_result=longform_result or {"performance": performance, "word": {}},
                status="generation_exception",
                eligible=False,
                actual_time=actual_time,
                qa_seconds=_numeric_seconds(audit.get("qa_seconds")),
                build_id=str(audit.get("build_id") or ""),
            )
        except Exception as telemetry_exc:
            audit["telemetry_error"] = str(telemetry_exc)
            _write_run_audit(run_dir, audit)
        raise MMFError(str(exc)) from exc
    qa_started = time.monotonic()
    external_repair_wall = 0.0
    try:
        raw_quote_audit = quote_serialization_status(longform_result["generated"])
        generated = apply_local_repairs(apply_artifact_repairs(longform_result["generated"]))
        postprocess_quote_audit = quote_serialization_status(generated)
    except Exception as exc:
        record_stage_duration(run_dir, "content_and_compliance_qa", time.monotonic() - qa_started, outcome="failed")
        actual_time = round(time.monotonic() - wall_started, 3)
        _write_run_audit(run_dir, {
            "run_id": run_id,
            "status": "qa_exception",
            "partial": True,
            "phase": "qa",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "qa_seconds": round(time.monotonic() - qa_started, 3),
            "actual_time": actual_time,
            "repair_reasoning": _repair_reasoning_value(longform_result),
            **_longform_runtime_audit(longform_result),
            "created_at": now_iso(),
        })
        raise MMFError(str(exc)) from exc
    write_json(run_dir / "generation_raw.json", generated)
    # 初稿先成为"可下载的文件"，再接受任何文档级判定：QA 之后即使降级为
    # NEEDS_REVIEW 或技术性中断，投标人手上也已经有本方案初稿。
    _write_generation_phase(run_dir, "FIRST_DRAFT", "正在整理文档。")
    first_draft_file = render_first_draft_file(run_dir, brief, generated["artifact"])
    write_json(run_dir / "first_draft.json", {
        "schema_version": "first-draft-artifact-v0.1",
        "artifact_kind": "FIRST_DRAFT",
        "section_states": ((longform_result.get("word") or {}).get("section_states") or {}),
        **first_draft_file,
        "recorded_at": now_iso(),
    })
    canonical_facts = ((longform_result.get("bundle") or {}).get("global_state") or brief)
    from governance.final_editorial import SourceFacts, run_final_editorial
    editorial_facts = SourceFacts.from_run(run_dir)
    canonical_facts = {**canonical_facts, **editorial_facts.claim_source()}
    final_qa_source = {**brief, **editorial_facts.claim_source()}
    quality = longform_result.get("quality_regression") or {}
    _write_generation_phase(run_dir, "FACT_COMMITMENT_CHECK", "正在检查事实与承诺。")
    recovery = recover_governance(
        generated,
        brief=final_qa_source if brief["medium"] == "WORD" else brief,
        positives=positives,
        guardrails=guardrails,
        contracts=contracts,
        canonical_facts=canonical_facts,
        depth=longform_result.get("depth"),
        quality=quality,
    )
    generated = recovery["generated"]
    commitment = recovery["commitment"]
    artifact_qa = recovery["artifact_qa"]
    compliance = recovery["compliance"]
    bidder_gate = generated.get("bidder_evidence_gate") or {}
    if bidder_gate.get("findings"):
        artifact_qa = {
            **artifact_qa,
            "findings": [*(artifact_qa.get("findings") or []), *(bidder_gate.get("findings") or [])],
            "bidder_evidence_gate": bidder_gate.get("status"),
        }
    padding_gate = generated.get("content_padding_quality_gate") or {}
    if padding_gate.get("findings"):
        # §45：低信息增量进的是同一条分级通道（NEEDS_REVIEW），不是另起一个阻断口子。
        artifact_qa = {
            **artifact_qa,
            "findings": [*(artifact_qa.get("findings") or []), *(padding_gate.get("findings") or [])],
            "content_padding_gate": padding_gate.get("status"),
        }
    if recovery.get("repair_count"):
        # 本地治理修复（不消耗 Provider 调用）不再是"AUTO_REPAIRING"，
        # §38-43 只把它作为完整性检查过程中的一次本地整理。
        _write_generation_phase(run_dir, "DOCUMENT_QA", f"正在检查完整性，本地完善{recovery['repair_count']}处内容。")
    write_json(run_dir / "generation_raw.json", generated)
    write_json(run_dir / "recovery_audit.json", {"level": recovery.get("level"), "terminal": recovery.get("terminal"), "rounds": recovery.get("audit") or []})
    if quality.get("status") == "FAIL":
        findings = list(artifact_qa.get("findings") or [])
        findings.append({
            "rule_id": "QR-001",
            "severity": "BLOCK",
            "issue": "QUALITY_REGRESSION_GATE FAIL: " + "、".join(str(item) for item in (quality.get("missing") or [])),
        })
        artifact_qa = {**artifact_qa, "status": "BLOCK", "findings": findings, "QUALITY_REGRESSION_GATE": "FAIL"}
    from governance.artifact_qa import merge_longform_qa
    artifact_qa = merge_longform_qa(artifact_qa, None)
    write_json(run_dir / "commitment_provenance_report.json", commitment)
    write_json(run_dir / "artifact_qa_report.json", artifact_qa)
    write_json(run_dir / "compliance_report.json", compliance)
    write_json(run_dir / "quality_regression.json", quality)
    # §25-29：文档级QA之后的 Provider 修复已在 longform 层的有界预算内执行完毕（一轮，
    # 只补缺口），这里绝不再整篇重跑；剩余问题按统一严重性进入终态判定。
    document_qa_stage = longform_result.get("document_qa") or {}
    repair_attempts = int(document_qa_stage.get("attempted") or 0)
    repair_budget_audit = (
        document_qa_stage.get("budget")
        or ((longform_result.get("word") or {}).get("global_repair_budget"))
        or {}
    )
    # §44-46：修复风暴守卫——修复耗时超过初稿耗时，或同一问题被反复处理，都要
    # 留下 QA Performance Warning，而不是让 AUTO_REPAIRING 静默跑三小时。
    qa_performance_warning = bool(
        repair_budget_audit.get("storm_triggered")
        or repair_budget_audit.get("repair_over_initial")
    )
    if qa_performance_warning:
        write_json(run_dir / "qa_performance_warning.json", {
            "schema_version": "qa-performance-warning-v0.1",
            "REPAIR_STORM_GUARD": bool(repair_budget_audit.get("storm_triggered")),
            "repair_over_initial": bool(repair_budget_audit.get("repair_over_initial")),
            "budget": repair_budget_audit,
            "recorded_at": now_iso(),
        })
    findings_total = int(((document_qa_stage.get("counts") or {}).get("repair_candidates") or 0)) + int(
        ((document_qa_stage.get("counts") or {}).get("supplement_candidates") or 0))
    budget_used = int(repair_budget_audit.get("repair_calls_used") or 0) + int(repair_budget_audit.get("supplement_calls_used") or 0)
    budget_max = repair_budget_audit.get("max_repair_calls")
    _write_generation_phase(
        run_dir, "DOCUMENT_QA",
        f"正在检查完整性，发现关键问题 {int(((document_qa_stage.get('counts') or {}).get('block') or 0))} 项。"
        if findings_total else "正在检查完整性。",
    )
    if auto_repair and (document_qa_stage.get("attempted") or 0):
        _write_generation_phase(
            run_dir, "SELECTIVE_REPAIR",
            f"关键问题 {findings_total} 项 / 正在修复 {int(document_qa_stage.get('attempted') or 0)}/{findings_total}"
            + (f" / 自动修复预算 {budget_used}/{budget_max} calls" if budget_max else ""),
        )
    qa_seconds = round(
        (time.monotonic() - qa_started) + _numeric_seconds(((longform_result or {}).get("performance") or {}).get("qa_seconds")),
        3,
    )
    workflow_qa_seconds = max(0.0, (time.monotonic() - qa_started) - external_repair_wall)
    record_stage_duration(run_dir, "content_and_compliance_qa", workflow_qa_seconds)
    manifest = _mmf_paths.load_build_manifest()
    build_id = str(manifest.get("build_id") or "dev-unpacked")
    runtime_source = str(manifest.get("runtime_source") or "source")
    leftover_warnings: list[dict[str, Any]] = leftover_warning_rows(compliance, commitment, artifact_qa, quality)
    if leftover_warnings:
        write_json(run_dir / "leftover_warnings.json", leftover_warnings)
    if delivery_blocked(compliance, commitment, artifact_qa, quality) and withhold_delivery(compliance, commitment, artifact_qa, quality):
        recoverable_integrity = _recoverable_integrity_failure(compliance, commitment, artifact_qa, quality)
        repair_limit = _automatic_repair_limit(compliance, commitment, artifact_qa, quality)
        decision = classify_fail_soft(
            artifact_qa=artifact_qa,
            commitment=commitment,
            compliance=compliance,
            quality=quality,
            repair_attempts=repair_attempts,
        )
        # §17-19：技术性失败也不得隐藏已生成的文件——初稿在 QA 判定之前就已落盘，
        # 这里如实报告它是否仍然存在、能否下载，而不是恒报 False。
        partial_artifact = resolve_run_artifact(run_dir)
        first_draft_meta = (
            read_json(run_dir / "first_draft.json", "first_draft_availability")
            if (run_dir / "first_draft.json").is_file() else {}
        )
        draft_path = Path(str(first_draft_meta.get("path") or ""))
        if partial_artifact is None and draft_path.is_file():
            partial_artifact = draft_path
        artifact_available = bool(partial_artifact is not None and partial_artifact.is_file())
        partial_relative = ""
        if artifact_available:
            try:
                partial_relative = partial_artifact.relative_to(run_dir).as_posix()
            except ValueError:
                partial_relative = partial_artifact.name
        _write_generation_phase(run_dir, "TERMINAL_BLOCKED", decision["public_message"], recoverable=False)
        actual_time = round(time.monotonic() - wall_started, 3)
        blocked = {
            "run_id": run_id,
            "status": "technical_failed",
            "qa_state": DELIVERY_TECHNICAL,
            "delivery_status": decision["delivery_status"],
            "qa_status": decision["qa_status"],
            "artifact_available": artifact_available,
            "artifact_name": partial_artifact.name if artifact_available else "",
            "download_url": f"/files/{run_id}/{partial_relative}" if artifact_available else "",
            "partial_artifact_kind": ("FIRST_DRAFT" if "first_draft/" in partial_relative else "LAST_VALID_ARTIFACT") if artifact_available else "",
            "public_message": decision["public_message"],
            "message": decision["public_message"],
            "compliance": compliance,
            "commitment_provenance": commitment,
            "artifact_qa": artifact_qa,
            "repair_attempts": repair_attempts,
            "automatic_repair_exhausted": bool(recoverable_integrity and repair_attempts >= repair_limit),
            "user_repair_required": False,
            "actions": [] if recoverable_integrity else ["choose_provider"],
            "qa_review_summary": decision["qa_review_summary"],
        }
        _write_run_audit(run_dir, {
            "run_id": run_id,
            "status": "technical_failed",
            "delivery_status": decision["delivery_status"],
            "qa_status": decision["qa_status"],
            "partial": True,
            "compliance_status": compliance["status"],
            "commitment_provenance_status": commitment["status"],
            "artifact_qa_status": artifact_qa["status"],
            "block_reasons": [item.get("rule_id") for item in (compliance.get("violations") or []) if item.get("severity") == "BLOCK"] + [item.get("rule_id") for item in (artifact_qa.get("findings") or []) if item.get("severity") == "BLOCK"],
            "artifact_available": artifact_available,
            "partial_artifact_path": partial_relative,
            "first_draft_artifact_status": first_draft_meta.get("status"),
            "qa_performance_warning": qa_performance_warning,
            "LONGFORM_ORCHESTRATOR": "ACTIVE",
            "ONE_SHOT_FULL_DOCUMENT_GENERATION": False,
            "SECTION_LEVEL_GENERATION": True,
            "task_mode": longform_result.get("task_mode"),
            "runtime_source": runtime_source,
            "build_id": build_id,
            "TEST_TARGET": "production_dist" if runtime_source == "dist" else "source",
            "speed_profile": brief.get("speed_profile"),
            "logical_section_count": ((longform_result.get("word") or {}).get("logical_section_count")),
            "generation_batch_count": ((longform_result.get("word") or {}).get("generation_batch_count")),
            **_longform_runtime_audit(longform_result),
            "parallelism": ((longform_result.get("word") or {}).get("parallelism")),
            "mode": brief.get("speed_profile"),
            "planning_reasoning": "high",
            "planning_execution": "local_deterministic",
            "draft_reasoning": ((longform_result.get("capability") or {}).get("effective_settings") or {}).get("effective_reasoning"),
            "repair_reasoning": _repair_reasoning_value(longform_result),
            "QUALITY_REGRESSION_GATE": ((longform_result.get("quality_regression") or {}).get("status")),
            "estimated_time_before_run": eta_before,
            "requested_settings": ((longform_result.get("capability") or {}).get("requested_settings")),
            "effective_settings": ((longform_result.get("capability") or {}).get("effective_settings")),
            "qa_seconds": qa_seconds,
            "actual_time": actual_time,
            "render_seconds": 0.0,
            "created_at": now_iso(),
        })
        _record_terminal_latency(
            run_id=run_id,
            brief=brief,
            provider_name=provider_name,
            longform_result=longform_result,
            status="technical_failed",
            eligible=False,
            actual_time=actual_time,
            qa_seconds=qa_seconds,
            build_id=build_id,
        )
        return blocked
    write_json(run_dir / "generation_raw.json", generated)
    if brief["medium"] != "WORD":
        _attach_ppt_images(run_dir, generated["artifact"], provider_name)
    content_path = run_dir / "artifact_content.json"
    write_json(content_path, {"brief": brief, "artifact": generated["artifact"]})
    _write_generation_phase(run_dir, "ASSEMBLING", "正在生成Word文件。" if brief["medium"] == "WORD" else "正在生成PPT文件。")
    render_started = time.monotonic()
    runtime = _runtime()
    env = os.environ.copy()
    env["RUNTIME_NODE"] = str(runtime.get("node_executable", ""))
    env["RUNTIME_NODE_MODULES"] = str(runtime.get("node_modules", ""))
    env["RUNTIME_BIN_DIR"] = str(runtime.get("bin_dir", ""))
    if brief["medium"] == "WORD":
        final_path = artifact_display_path(run_dir, brief)
        env["MMF_ARTIFACT_BUILD_METRICS"] = str(run_dir / "artifact_table_build_metrics.json")
        cmd = [str(runtime.get("python_executable") or sys.executable), str(APP_ROOT / "build_docx.py"), str(content_path), str(final_path)]
    else:
        final_path = artifact_display_path(run_dir, brief)
        qa_dir = run_dir / "_qa_ppt"
        env["MMF_PPT_PYTHON"] = str(runtime.get("python_executable") or sys.executable)
        cmd = [str(runtime.get("node_executable") or "node"), str(APP_ROOT / "build_ppt.mjs"), str(content_path), str(final_path), str(qa_dir)]
    try:
        completed = subprocess.run(cmd, cwd=str(APP_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, shell=False)
    except Exception as exc:
        record_stage_duration(run_dir, "artifact_assembly", time.monotonic() - render_started, outcome="failed")
        actual_time = round(time.monotonic() - wall_started, 3)
        error_id = _write_generation_error_diagnostics(run_dir, run_id, exc, phase="render", provider=provider_name, recoverable=True)
        _write_run_audit(run_dir, {
            "run_id": run_id,
            "status": "render_blocked",
            "partial": True,
            "phase": "render",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "qa_seconds": qa_seconds,
            "actual_time": actual_time,
            "render_seconds": round(time.monotonic() - render_started, 3),
            "repair_reasoning": _repair_reasoning_value(longform_result),
            **_longform_runtime_audit(longform_result),
            "runtime_source": runtime_source,
            "build_id": build_id,
            "TEST_TARGET": "production_dist" if runtime_source == "dist" else "source",
            "created_at": now_iso(),
        })
        write_json(run_dir / "technical_failure.json", {
            "status": "ASSEMBLY_FAILED",
            "error_type": type(exc).__name__,
            "error_id": error_id,
            "recoverable": True,
            "error": str(exc)[:500],
            "recorded_at": now_iso(),
        })
        from longform.run_state import finalize_delivery_state
        finalize_delivery_state(run_dir, medium=brief.get("medium"), draft_delivery=DELIVERY_TECHNICAL)
        _record_terminal_latency(
            run_id=run_id, brief=brief, provider_name=provider_name, longform_result=longform_result,
            status="render_blocked", eligible=False, actual_time=actual_time, qa_seconds=qa_seconds, build_id=build_id,
        )
        raise MMFError(f"生成过程中发生内部错误（错误编号：{error_id}），系统已保留已完成内容，可继续恢复。") from exc
    (run_dir / "artifact_build.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (run_dir / "artifact_build.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0 or not final_path.is_file():
        record_stage_duration(run_dir, "artifact_assembly", time.monotonic() - render_started, outcome="failed")
        actual_time = round(time.monotonic() - wall_started, 3)
        _write_run_audit(run_dir, {
            "run_id": run_id,
            "status": "render_blocked",
            "partial": True,
            "phase": "render",
            "error": "artifact_build_failed",
            "qa_seconds": qa_seconds,
            "actual_time": actual_time,
            "render_seconds": round(time.monotonic() - render_started, 3),
            "repair_reasoning": _repair_reasoning_value(longform_result),
            **_longform_runtime_audit(longform_result),
            "runtime_source": runtime_source,
            "build_id": build_id,
            "TEST_TARGET": "production_dist" if runtime_source == "dist" else "source",
            "created_at": now_iso(),
        })
        write_json(run_dir / "technical_failure.json", {
            "status": "ASSEMBLY_FAILED",
            "error_type": "RendererFailure",
            "error": (completed.stderr or "artifact_build_failed")[-500:],
            "recorded_at": now_iso(),
        })
        from longform.run_state import finalize_delivery_state
        finalize_delivery_state(run_dir, medium=brief.get("medium"), draft_delivery=DELIVERY_TECHNICAL)
        _record_terminal_latency(
            run_id=run_id, brief=brief, provider_name=provider_name, longform_result=longform_result,
            status="render_blocked", eligible=False, actual_time=actual_time, qa_seconds=qa_seconds, build_id=build_id,
        )
        raise MMFError("生成失败，请查看运行日志")
    record_stage_duration(run_dir, "artifact_assembly", time.monotonic() - render_started)
    if brief["medium"] == "PPT" and generated["artifact"].get("profile_p_contract"):
        from ppt_final_verify import verify_pptx
        final_artifact_qa = verify_pptx(final_path, generated["artifact"])
        final_artifact_qa["file_integrity_status"] = final_artifact_qa["status"]
        if final_artifact_qa["status"] != "BLOCK":
            final_artifact_qa["status"] = artifact_qa.get("status") or "NEEDS_REVIEW"
        write_json(run_dir / "final_artifact_qa.json", final_artifact_qa)
        artifact_qa = {**artifact_qa,
            "status": "BLOCK" if final_artifact_qa["status"] == "BLOCK" else artifact_qa.get("status"),
            "findings": [*(artifact_qa.get("findings") or []), *final_artifact_qa["findings"]],
            "final_pptx": final_artifact_qa}
        write_json(run_dir / "artifact_qa_report.json", artifact_qa)
    if brief["medium"] == "WORD":
        _write_generation_phase(run_dir, "FINAL_EDITORIAL", "正在校核成稿文字与来源事实。")
        editorial_started = time.monotonic()
        # One final artifact pass precedes pagination and final QA. Failure leaves
        # the readable file available, but enters the existing QA delivery gate.
        try:
            run_final_editorial(final_path, facts=editorial_facts, output_dir=run_dir,
                                section_titles={str(s.get("section_id")): str(s.get("heading") or s.get("title") or "")
                                                for s in generated.get("artifact", {}).get("sections", [])})
        except Exception as exc:
            artifact_qa = {**(artifact_qa or {}), "status": "BLOCK",
                           "findings": list((artifact_qa or {}).get("findings") or []) +
                           [{"rule_id": "FINAL_ARTIFACT_QA_FAILED", "severity": "BLOCK",
                             "issue": f"FINAL_EDITORIAL failed: {type(exc).__name__}"}]}
            write_json(run_dir / "final_editorial_report.json", {"status": "FAILED", "error": str(exc)[:200]})
        record_stage_duration(run_dir, "final_editorial", time.monotonic() - editorial_started)
    if not artifact_physically_invalid(final_path):
        record_last_valid_artifact(run_dir, final_path)
    pagination_report: dict[str, Any] = {}
    if brief["medium"] == "WORD" and final_path.is_file():
        # §18-§23 装配顺序：正文与附录装配 → 分页 → 目录字段刷新 → 最终 Artifact QA。
        # 渲染器只是量尺：原生目录域、书签和 updateFields 一律不动，WPS 打开仍会自刷新。
        _write_generation_phase(run_dir, "TOC_PAGINATION_REFRESH", "正在按真实分页刷新目录页码。")
        pagination_started = time.monotonic()
        try:
            from longform.pagination import refresh_with_retry
            pagination_report = refresh_with_retry(final_path, run_dir=run_dir)
        except Exception as exc:
            # 没有渲染器不能把已经生成的文件变成"没有文件"。
            pagination_report = {"schema_version": "artifact-pagination-v0.1", "status": "PAGINATION_ERROR",
                                 "error": str(exc)[:200]}
            write_json(run_dir / "artifact_pagination.json", pagination_report)
        record_stage_duration(run_dir, "toc_pagination_refresh", time.monotonic() - pagination_started)
        from governance.final_editorial import bind_human_input_pages
        bind_human_input_pages(run_dir, pagination_report)
        # 只要 document.xml 被真实重写过，磁盘上的字节就变了：LAST_VALID 必须指向
        # 这一版，否则回滚会把已经刷新过分页的产物退回未刷新的旧副本。
        if pagination_report.get("document_xml_rewritten") and not artifact_physically_invalid(final_path):
            record_last_valid_artifact(run_dir, final_path)
    # V1.1 P1 / Round 2 §34：Consistency 结果必须进入交付门。
    #   PASS → 正常；NEEDS_REVIEW（含 CS-FAIL）→ 不得 COMPLETED_CLEAN；
    #   BLOCK → 按严重性 NEEDS_REVIEW 或 Terminal Block。
    consistency_status = str((generated.get("consistency") or {}).get("status") or "")
    consistency_issues = (generated.get("consistency") or {}).get("issues") or []
    if consistency_status in {"CONSISTENCY_CHECK_FAILED", "NEEDS_REVIEW", "BLOCK"} and len(consistency_issues) > 0:
        severity = "BLOCK" if consistency_status == "BLOCK" else "NEEDS_REVIEW"
        cs_findings = [{"rule_id": f"CS-{severity}", "severity": severity,
                        "issue": f"一致性检查 {consistency_status}：关键事实核查存在未解决问题（{len(consistency_issues)} 项）。"}]
        artifact_qa = {**(artifact_qa or {}),
                       "findings": list((artifact_qa or {}).get("findings") or []) + cs_findings,
                       "consistency_gate": severity}
    delivery_decision = classify_fail_soft(artifact_path=final_path, artifact_qa=artifact_qa, commitment=commitment, compliance=compliance, quality=quality, repair_attempts=repair_attempts, artifact=(generated.get("artifact") if isinstance(generated, dict) else None), medium=str(brief.get("medium") or ""))
    local_review_repair = None
    _write_generation_phase(run_dir, "FINAL_ARTIFACT_QA", "正在执行最终文件检查。")
    if brief["medium"] == "WORD":
        def evaluate_final_docx(path):
            return evaluate_docx_artifact(path, canonical_facts=canonical_facts, brief=final_qa_source,
                                          contracts=contracts, run_dir=run_dir)

        final_qa_started = time.monotonic()
        try:
            final_artifact_qa = evaluate_final_docx(final_path)
            pre_repair_docx_quote_status = final_artifact_qa.get("quote_status")
            repair_rounds = []
            for _round in range(2):
                if final_artifact_qa.get("status") != "BLOCK" or not artifact_findings_are_locally_recoverable(final_artifact_qa):
                    break
                if _round == 0:
                    write_json(run_dir / "final_artifact_qa_pre_repair.json", final_artifact_qa)
                try:
                    artifact_repair = repair_docx_artifact(final_path)
                except Exception:
                    restore_last_valid_artifact(run_dir, final_path)
                    break
                if artifact_physically_invalid(final_path):
                    restore_last_valid_artifact(run_dir, final_path)
                    break
                record_last_valid_artifact(run_dir, final_path)
                repair_rounds.append(artifact_repair)
                _write_generation_phase(run_dir, "FINAL_ARTIFACT_QA", "正在检查文件完整性并本地修正格式。")
                final_artifact_qa = evaluate_final_docx(final_path)
            if repair_rounds:
                final_artifact_qa["post_repair_reopened_actual_docx"] = True
                final_artifact_qa["repair_summary"] = repair_rounds[-1]
                write_json(run_dir / "final_artifact_repair_log.json", {"rounds": repair_rounds})
            quote_pipeline_audit = {
                "RAW_QUOTE_STATUS": raw_quote_audit.get("status"),
                "POSTPROCESS_QUOTE_STATUS": postprocess_quote_audit.get("status"),
                "PRE_REPAIR_FINAL_DOCX_QUOTE_STATUS": pre_repair_docx_quote_status,
                "FINAL_DOCX_QUOTE_STATUS": final_artifact_qa.get("quote_status"),
                "raw": raw_quote_audit,
                "postprocess": postprocess_quote_audit,
            }
            final_artifact_qa["quote_pipeline_audit"] = quote_pipeline_audit
            write_json(run_dir / "quote_pipeline_audit.json", quote_pipeline_audit)
        except Exception as exc:
            record_stage_duration(run_dir, "final_artifact_qa", time.monotonic() - final_qa_started, outcome="failed")
            restore_last_valid_artifact(run_dir, final_path)
            _write_run_audit(run_dir, {
                "run_id": run_id, "status": "final_artifact_qa_exception", "partial": True,
                "phase": "FINAL_ARTIFACT_QA", "error": str(exc), "error_type": type(exc).__name__,
                "runtime_source": runtime_source, "build_id": build_id,
                "TEST_TARGET": "production_dist" if runtime_source == "dist" else "source", "created_at": now_iso(),
            })
            raise MMFError(str(exc)) from exc
        else:
            record_stage_duration(run_dir, "final_artifact_qa", time.monotonic() - final_qa_started)
        if artifact_physically_invalid(final_path) and restore_last_valid_artifact(run_dir, final_path):
            final_artifact_qa = evaluate_final_docx(final_path)
        _write_generation_phase(run_dir, "FINAL_SCORING", "正在核验成稿对招标评分要求的响应。")
        final_artifact_qa = _refresh_final_scoring_delivery(run_dir, final_path, final_artifact_qa)
        write_json(run_dir / "final_artifact_qa.json", final_artifact_qa)
        delivery_decision = classify_fail_soft(
            artifact_path=final_path,
            findings=list(final_artifact_qa.get("findings") or []),
            artifact_qa={**artifact_qa, "final_docx": final_artifact_qa, "findings": [*(artifact_qa.get("findings") or []), *(final_artifact_qa.get("findings") or [])]},
            commitment=commitment,
            compliance=compliance,
            quality=quality,
            repair_attempts=max(repair_attempts, len(repair_rounds)),
            repaired=bool(repair_rounds),
        )
        if delivery_decision["delivery_status"] == DELIVERY_TECHNICAL and restore_last_valid_artifact(run_dir, final_path) and not artifact_physically_invalid(final_path):
            # Restoration can change the landed bytes: re-open both QA and
            # scoring instead of retaining the reading of the rejected file.
            final_artifact_qa = evaluate_final_docx(final_path)
            final_artifact_qa = _refresh_final_scoring_delivery(run_dir, final_path, final_artifact_qa)
            write_json(run_dir / "final_artifact_qa.json", final_artifact_qa)
            delivery_decision = classify_fail_soft(
                artifact_path=final_path,
                findings=list(final_artifact_qa.get("findings") or []),
                artifact_qa={**artifact_qa, "final_docx": final_artifact_qa,
                             "findings": [*(artifact_qa.get("findings") or []),
                                          *(final_artifact_qa.get("findings") or [])]},
                commitment=commitment,
                compliance=compliance,
                quality=quality,
                repair_attempts=max(repair_attempts, len(repair_rounds)),
                repaired=bool(repair_rounds),
            )
        from providers.cursor_account import is_cursor_provider
        if (delivery_decision["delivery_status"] == DELIVERY_REVIEW
                and (provider_name in {"qoder_account", "zcode_cn"} or is_cursor_provider(provider_name))):
            _write_generation_phase(run_dir, "LOCAL_REVIEW_REPAIR", "正在自动尝试修复待复核稿。")
            local_review_repair, pagination_report = _record_account_review_repair(
                final_path, run_dir, final_artifact_qa, evaluate_final_docx, pagination_report,
                provider_name, delivery_decision["delivery_status"])
            if local_review_repair and local_review_repair["status"] == "APPLIED":
                repair_rounds.append(local_review_repair)
                write_json(run_dir / "artifact_pagination.json", pagination_report)
                bind_human_input_pages(run_dir, pagination_report)
                record_last_valid_artifact(run_dir, final_path)
                final_artifact_qa = _refresh_final_scoring_delivery(run_dir, final_path, evaluate_final_docx(final_path))
                final_artifact_qa["quote_pipeline_audit"] = {**quote_pipeline_audit,
                                                            "FINAL_DOCX_QUOTE_STATUS": final_artifact_qa.get("quote_status")}
                write_json(run_dir / "quote_pipeline_audit.json", final_artifact_qa["quote_pipeline_audit"])
                write_json(run_dir / "final_artifact_qa.json", final_artifact_qa)
                delivery_decision = classify_fail_soft(
                    artifact_path=final_path,
                    findings=list(final_artifact_qa.get("findings") or []),
                    artifact_qa={**artifact_qa, "final_docx": final_artifact_qa,
                                 "findings": [*(artifact_qa.get("findings") or []),
                                              *(final_artifact_qa.get("findings") or [])]},
                    commitment=commitment,
                    compliance=compliance,
                    quality=quality,
                    repair_attempts=max(repair_attempts, len(repair_rounds)),
                    repaired=True,
                )
            write_json(run_dir / "local_review_repair.json", local_review_repair)
        write_review_summary(run_dir, delivery_decision["qa_review_summary"])
        # §44：技术标正文之外的"待投标人确认"事项走随件内部汇总，不进 Word 正文。
        technical_review_summary = _write_technical_review_summary(
            run_dir, generated.get("bidder_evidence_gate") or {}, pagination_report)
        leftover_warnings = leftover_warnings + list((delivery_decision.get("qa_review_summary") or {}).get("issues") or [])
        write_json(run_dir / "leftover_warnings.json", leftover_warnings)
        if delivery_decision["delivery_status"] == DELIVERY_TECHNICAL and not delivery_decision.get("artifact_available"):
            _write_generation_phase(run_dir, "TERMINAL_BLOCKED", delivery_decision["public_message"], recoverable=False)
            _write_run_audit(run_dir, {
                "run_id": run_id,
                "status": "technical_failed",
                "delivery_status": DELIVERY_TECHNICAL,
                "qa_status": delivery_decision["qa_status"],
                "partial": True,
                "phase": "FINAL_ARTIFACT_QA",
                "artifact_qa_status": artifact_qa.get("status"),
                "final_artifact_qa_status": final_artifact_qa.get("status"),
                "runtime_source": runtime_source,
                "build_id": build_id,
                "TEST_TARGET": "production_dist" if runtime_source == "dist" else "source",
                "created_at": now_iso(),
            })
            return {
                "run_id": run_id,
                "status": "technical_failed",
                "qa_state": DELIVERY_TECHNICAL,
                "delivery_status": DELIVERY_TECHNICAL,
                "qa_status": delivery_decision["qa_status"],
                "artifact_available": False,
                "medium": brief["medium"],
                "artifact_qa": artifact_qa,
                "commitment_provenance": commitment,
                "compliance": compliance,
                "repair_attempts": repair_attempts,
                "actions": ["choose_provider"],
                "user_repair_required": False,
                "public_message": delivery_decision["public_message"],
                "message": delivery_decision["public_message"],
                "qa_review_summary": delivery_decision["qa_review_summary"],
            }
    if not (run_dir / "qa_review_summary.json").is_file():
        write_review_summary(run_dir, delivery_decision.get("qa_review_summary") or {"issues": []})
    # §交付：产品直接输出保留原件；内部留档主稿与对外清理件分别命名并计算 SHA。
    # 清理是可重复的产品阶段（不是人工修末端 DOCX）：只改对外副本，原件字节不动，
    # 逐段保留前后映射，清理后再分页并由 Artifact QA 实测对外副本。
    delivery_path = final_path
    delivered_artifact_qa = final_artifact_qa if brief["medium"] == "WORD" else {}
    external_cleanup: dict[str, Any] = {
        "schema_version": "external-cleanup-v1", "status": "NOT_APPLICABLE",
        "provider_calls": 0, "changes": [], "rule_counts": {}, "residual_count": 0,
    }
    artifact_bindings: dict[str, Any] = {}
    if brief["medium"] == "WORD" and final_path.is_file():
        from governance.external_cleanup import clean_docx, write_report
        from longform.pagination import refresh_with_retry
        cleanup_started = time.monotonic()
        _write_generation_phase(run_dir, "EXTERNAL_CLEANUP", "正在保留产品原件并生成对外清理件。")
        direct_bytes = final_path.read_bytes()
        direct_sha = hashlib.sha256(direct_bytes).hexdigest()
        stem, suffix = final_path.stem, (final_path.suffix or ".docx")
        internal_path = run_dir / f"{stem}_内部留档{suffix}"
        external_path = run_dir / f"{stem}_对外清理{suffix}"
        shutil.copyfile(final_path, internal_path)
        direct_pagination_path = run_dir / "artifact_pagination.json"
        direct_pagination = read_json(direct_pagination_path, "direct_pagination") if direct_pagination_path.is_file() else None
        try:
            external_cleanup = clean_docx(final_path, external_path)
        except Exception as exc:
            external_cleanup = {
                "schema_version": "external-cleanup-v1", "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "provider_calls": 0, "changes": [], "rule_counts": {},
                "residual_count": None,
            }
            external_path.unlink(missing_ok=True)
        else:
            write_report(external_cleanup, run_dir / "external_cleanup.json")
        external_artifact_qa: dict[str, Any] = {}
        if external_cleanup.get("status") == "CLEANED":
            # The cleanup changes visible text; page numbers cached in the native
            # TOC must be re-measured on the cleaned copy before it is delivered.
            try:
                external_pagination = refresh_with_retry(external_path, run_dir=run_dir)
            except Exception as exc:
                external_pagination = {
                    "schema_version": "artifact-pagination-v0.1",
                    "status": "PAGINATION_ERROR", "error": str(exc)[:200],
                }
            write_json(run_dir / "artifact_pagination_external.json", external_pagination)
            if direct_pagination is not None:
                write_json(run_dir / "artifact_pagination.json", direct_pagination)
            external_cleanup["pagination"] = external_pagination
        if external_cleanup.get("status") in {"CLEANED", "NO_CHANGES"} and external_path.is_file():
            try:
                external_artifact_qa = evaluate_docx_artifact(
                    external_path, canonical_facts=canonical_facts, brief=final_qa_source,
                    contracts=contracts, run_dir=None)
                write_json(run_dir / "external_artifact_qa.json", external_artifact_qa)
            except Exception as exc:
                external_artifact_qa = {
                    "status": "BLOCK",
                    "findings": [{"rule_id": "EXTERNAL_ARTIFACT_QA_FAILED", "severity": "BLOCK",
                                  "issue": f"{type(exc).__name__}: {exc}"[:300]}],
                }
                write_json(run_dir / "external_artifact_qa.json", external_artifact_qa)
        external_cleanup["qa"] = external_artifact_qa
        if external_cleanup.get("status") in {"CLEANED", "NO_CHANGES"}:
            write_report(external_cleanup, run_dir / "external_cleanup.json")
        external_eligible = _external_delivery_eligible(
            external_cleanup, external_artifact_qa, final_artifact_qa)
        if external_eligible:
            delivery_path = external_path
            delivered_artifact_qa = external_artifact_qa
            delivery_decision = classify_fail_soft(
                artifact_path=external_path,
                findings=list(external_artifact_qa.get("findings") or []),
                artifact_qa={**artifact_qa, "final_docx": external_artifact_qa,
                             "findings": [*(artifact_qa.get("findings") or []),
                                          *(external_artifact_qa.get("findings") or [])]},
                commitment=commitment,
                compliance=compliance,
                quality=quality,
                repair_attempts=max(repair_attempts, len(repair_rounds)),
                repaired=bool(repair_rounds) or external_cleanup.get("status") == "CLEANED",
            )
            write_review_summary(run_dir, delivery_decision["qa_review_summary"])
            leftover_warnings = leftover_warnings + list(
                (delivery_decision.get("qa_review_summary") or {}).get("issues") or [])
            write_json(run_dir / "leftover_warnings.json", leftover_warnings)
            if local_review_repair is None and delivery_decision["delivery_status"] == DELIVERY_REVIEW:
                _write_generation_phase(run_dir, "LOCAL_REVIEW_REPAIR", "正在自动尝试修复待复核稿。")
                local_review_repair, pagination_report = _record_account_review_repair(
                    external_path, run_dir, external_artifact_qa, evaluate_final_docx, pagination_report,
                    provider_name, delivery_decision["delivery_status"])
                if local_review_repair and local_review_repair.get("status") == "APPLIED":
                    repair_rounds.append(local_review_repair)
                    external_artifact_qa = _refresh_final_scoring_delivery(
                        run_dir, external_path, evaluate_final_docx(external_path))
                    delivered_artifact_qa = external_artifact_qa
                    write_json(run_dir / "external_artifact_qa.json", external_artifact_qa)
                    delivery_decision = classify_fail_soft(
                        artifact_path=external_path,
                        findings=list(external_artifact_qa.get("findings") or []),
                        artifact_qa={**artifact_qa, "final_docx": external_artifact_qa,
                                     "findings": [*(artifact_qa.get("findings") or []),
                                                  *(external_artifact_qa.get("findings") or [])]},
                        commitment=commitment,
                        compliance=compliance,
                        quality=quality,
                        repair_attempts=max(repair_attempts, len(repair_rounds)),
                        repaired=True,
                    )
                    write_review_summary(run_dir, delivery_decision["qa_review_summary"])
        for row in external_cleanup.get("changes") or []:
            row["direct_artifact_sha256"] = direct_sha
        artifact_bindings = {
            "schema_version": "artifact-bindings-v1",
            "product_direct": {
                "name": final_path.name, "path": _path_label(final_path),
                "sha256": direct_sha, "bytes": len(direct_bytes),
            },
            "internal_master": {
                "name": internal_path.name, "path": _path_label(internal_path),
                "sha256": hashlib.sha256(internal_path.read_bytes()).hexdigest(),
                "bytes": internal_path.stat().st_size,
                "byte_identical_to_product_direct": internal_path.read_bytes() == direct_bytes,
            },
            "external_clean": {
                "name": external_path.name if external_path.is_file() else "",
                "path": _path_label(external_path) if external_path.is_file() else "",
                "sha256": hashlib.sha256(external_path.read_bytes()).hexdigest() if external_path.is_file() else "",
                "bytes": external_path.stat().st_size if external_path.is_file() else 0,
                "status": external_cleanup.get("status"),
                "changed_paragraphs": external_cleanup.get("changed_paragraphs"),
                "residual_count": external_cleanup.get("residual_count"),
            },
            "delivered": {
                "role": "external_clean" if delivery_path == external_path else "product_direct",
                "name": delivery_path.name,
                "sha256": hashlib.sha256(delivery_path.read_bytes()).hexdigest(),
            },
            "provider_calls": 0,
        }
        write_json(run_dir / "artifact_bindings.json", artifact_bindings)
        record_stage_duration(run_dir, "external_cleanup", time.monotonic() - cleanup_started)
    render_seconds = round(time.monotonic() - render_started, 3)
    actual_time = round(time.monotonic() - wall_started, 3)
    recommendation_metadata = recommendation.get("provider_metadata") if isinstance(recommendation.get("provider_metadata"), dict) else {}
    generation_metadata = generated.get("provider_metadata") if isinstance(generated.get("provider_metadata"), dict) else {}
    rec_provider = str(recommendation_metadata.get("provider_name") or recommendation.get("provider_name") or "")
    gen_provider = str(generation_metadata.get("provider_name") or rec_provider)
    switched = bool(rec_provider and gen_provider and rec_provider != gen_provider)
    run_audit = {
        "run_id": run_id,
        "project_brief": brief,
        "selected_positive_ku_ids": selected,
        "knowledge_used": bool(selected),
        "knowledge_status": selection.get("knowledge_status"),
        "recommended_knowledge_count": len(recommended_ids),
        "selected_knowledge_count": len(selected),
        "active_guardrail_ids": guardrail_ids,
        "clarification_list": generated["clarification_list"],
        "provider_name": gen_provider or rec_provider,
        "provider_type": generation_metadata.get("provider_type") or "grok_build",
        "model": generation_metadata.get("model") or "",
        "endpoint_alias": generation_metadata.get("endpoint_alias") or "",
        "provider_version": generation_metadata.get("provider_version") or "",
        "reasoning_mode": generation_metadata.get("reasoning_mode") or "",
        "fallback_used": False,
        "fallback_reason": None,
        "provider_switches": ([{"from": rec_provider, "to": gen_provider, "at": now_iso()}] if switched else []),
        "test_mode": generation_metadata.get("test_mode", False),
        "generation_task_id": generation_metadata.get("task_id") or f"{run_id}-generate",
        "generation_layer_version": "Generation Layer Patch V0.2",
        "output_medium_profile": profile,
        "generated_artifact_path": _path_label(final_path),
        "artifact_display_name": delivery_path.name,
        "artifact_internal_path": _path_label(final_path),
        "product_direct_artifact_path": _path_label(final_path),
        "product_direct_artifact_name": final_path.name,
        "product_direct_artifact_sha256": (artifact_bindings.get("product_direct") or {}).get("sha256"),
        "internal_master_artifact_path": (artifact_bindings.get("internal_master") or {}).get("path"),
        "internal_master_artifact_name": (artifact_bindings.get("internal_master") or {}).get("name"),
        "internal_master_artifact_sha256": (artifact_bindings.get("internal_master") or {}).get("sha256"),
        "external_clean_artifact_path": (artifact_bindings.get("external_clean") or {}).get("path"),
        "external_clean_artifact_name": (artifact_bindings.get("external_clean") or {}).get("name"),
        "external_clean_artifact_sha256": (artifact_bindings.get("external_clean") or {}).get("sha256"),
        "delivered_artifact_role": (artifact_bindings.get("delivered") or {}).get("role"),
        "delivered_artifact_name": delivery_path.name,
        "delivered_artifact_sha256": (artifact_bindings.get("delivered") or {}).get("sha256"),
        "artifact_bindings_path": _path_label(run_dir / "artifact_bindings.json") if artifact_bindings else None,
        "external_cleanup_status": external_cleanup.get("status"),
        "external_cleanup_changed_paragraphs": external_cleanup.get("changed_paragraphs"),
        "external_cleanup_residual_count": external_cleanup.get("residual_count"),
        "external_cleanup_provider_calls": external_cleanup.get("provider_calls", 0),
        "compliance_status": compliance["status"],
        "commitment_provenance_status": commitment["status"],
        "artifact_qa_status": artifact_qa["status"],
        "final_artifact_qa_status": ((delivered_artifact_qa or {}).get("status") if brief["medium"] == "WORD" else (final_artifact_qa.get("status") if generated["artifact"].get("profile_p_contract") else "NOT_APPLICABLE")),
        "product_direct_artifact_qa_status": ((final_artifact_qa or {}).get("status") if brief["medium"] == "WORD" else "NOT_APPLICABLE"),
        "external_artifact_qa_status": ((external_cleanup.get("qa") or {}).get("status") if isinstance(external_cleanup.get("qa"), dict) else None),
        "leftover_accepted": bool(leftover_warnings),
        "leftover_warning_count": len(leftover_warnings),
        "candidate_delivery_status": delivery_decision.get("delivery_status"),
        "qa_status": delivery_decision.get("qa_status"),
        "artifact_available": True,
        "review_count": delivery_decision.get("review_count") or 0,
        "compliance_repair_attempts": repair_attempts,
        "LONGFORM_ORCHESTRATOR": "ACTIVE",
        "ONE_SHOT_FULL_DOCUMENT_GENERATION": False,
        "SECTION_LEVEL_GENERATION": True,
        "task_mode": longform_result.get("task_mode"),
        "requested_settings": ((longform_result.get("capability") or {}).get("requested_settings")),
        "effective_settings": ((longform_result.get("capability") or {}).get("effective_settings")),
        "longform_depth": (longform_result.get("depth") or {}).get("status"),
        "runtime_source": runtime_source,
        "build_id": build_id,
        "source_hash": (manifest.get("source_hash") or ""),
        "TEST_TARGET": "production_dist" if runtime_source == "dist" else "source",
        "speed_profile": brief.get("speed_profile"),
        "logical_section_count": ((longform_result.get("word") or {}).get("logical_section_count")),
        "generation_batch_count": ((longform_result.get("word") or {}).get("generation_batch_count")),
        **_longform_runtime_audit(longform_result),
        **_pagination_audit_fields(run_dir),
        **_table_audit_fields(run_dir),
        "SECTIONS_PER_PROVIDER_CALL": ((longform_result.get("word") or {}).get("SECTIONS_PER_PROVIDER_CALL")),
        "CALL_REDUCTION_RATIO": ((longform_result.get("word") or {}).get("CALL_REDUCTION_RATIO")),
        "parallelism": ((longform_result.get("word") or {}).get("parallelism")),
        "mode": brief.get("speed_profile"),
        "planning_reasoning": "high",
        "planning_execution": "local_deterministic",
        "draft_reasoning": ((longform_result.get("capability") or {}).get("effective_settings") or {}).get("effective_reasoning"),
        "repair_reasoning": _repair_reasoning_value(longform_result),
        # Kept as a compatibility alias for the final generation call only.
        # Complete workflow duration is supplied by _write_run_audit below.
        "total_elapsed_seconds": actual_time,
        "provider_wait_seconds": _numeric_seconds(((longform_result.get("performance") or {}).get("provider_wait_seconds"))),
        "generation_seconds": _numeric_seconds(((longform_result.get("performance") or {}).get("generation_seconds"))),
        "planning_seconds": _numeric_seconds(((longform_result.get("performance") or {}).get("planning_seconds"))),
        "retry_count": ((longform_result.get("word") or {}).get("repair_count")),
        "latency_downgrade_count": ((longform_result.get("word") or {}).get("latency_downgrade_count")),
        "quality_escalation_count": ((longform_result.get("word") or {}).get("quality_escalation_count")),
        "estimated_time_before_run": eta_before,
        "qa_seconds": qa_seconds,
        "render_seconds": render_seconds,
        "actual_time": actual_time,
        "QUALITY_REGRESSION_GATE": ((longform_result.get("quality_regression") or {}).get("status")),
        "content_metrics": longform_result.get("content_metrics") or {},
        "created_at": now_iso(),
        "generation_started_at": (read_json(run_dir / "generation_status.json", "generation_status") or {}).get("started_at") if (run_dir / "generation_status.json").is_file() else None,
    }
    from planning.ppt_delivery import completion_state
    ppt_completion = completion_state(run_dir) if brief["medium"] == "PPT" else None
    partial_ppt = ppt_completion is not None and not ppt_completion["complete"]
    terminal_status = "generation_partial" if partial_ppt else "generation_completed"
    terminal_message = (f"已生成{ppt_completion['completed_body_pages']}/{ppt_completion['planned_body_pages']}张正文，部分稿已保留，可继续未完成页面。"
                        if partial_ppt else delivery_decision.get("public_message") or "方案生成完成。")
    run_audit["status"] = terminal_status
    if local_review_repair is not None:
        run_audit["local_review_repair"] = local_review_repair
    if ppt_completion is not None:
        run_audit["ppt_completion"] = ppt_completion
    run_audit = _write_run_audit(run_dir, run_audit)
    _write_generation_phase(run_dir, "TERMINAL_BLOCKED" if partial_ppt else "COMPLETED", terminal_message, recoverable=partial_ppt)
    _record_terminal_latency(
        run_id=run_id,
        brief=brief,
        provider_name=provider_name,
        longform_result=longform_result,
        status=terminal_status,
        eligible=not partial_ppt,
        actual_time=actual_time,
        qa_seconds=qa_seconds,
        build_id=build_id,
    )
    return {
        "run_id": run_id,
        "status": terminal_status,
        "ppt_completion": ppt_completion,
        "resume_available": partial_ppt,
        "medium": brief["medium"],
        "artifact_name": delivery_path.name,
        "download_url": f"/files/{run_id}/{delivery_path.name}",
        "artifact_sha256": (artifact_bindings.get("delivered") or {}).get("sha256"),
        "product_direct_artifact_name": final_path.name,
        "product_direct_artifact_sha256": (artifact_bindings.get("product_direct") or {}).get("sha256"),
        "internal_master_artifact_name": (artifact_bindings.get("internal_master") or {}).get("name"),
        "internal_master_artifact_sha256": (artifact_bindings.get("internal_master") or {}).get("sha256"),
        "external_clean_artifact_name": (artifact_bindings.get("external_clean") or {}).get("name"),
        "external_clean_artifact_sha256": (artifact_bindings.get("external_clean") or {}).get("sha256"),
        "external_cleanup": {
            "status": external_cleanup.get("status"),
            "changed_paragraphs": external_cleanup.get("changed_paragraphs"),
            "residual_count": external_cleanup.get("residual_count"),
            "provider_calls": external_cleanup.get("provider_calls", 0),
            "source_sha256": external_cleanup.get("source_sha256"),
            "target_sha256": external_cleanup.get("target_sha256"),
        },
        "clarification_list": generated["clarification_list"],
        "provider_metadata": generation_metadata,
        "test_mode": generation_metadata.get("test_mode", False),
        "compliance": compliance,
        "commitment_provenance": commitment,
        "artifact_qa": artifact_qa,
        "leftover_warnings": leftover_warnings,
        "delivery_status": delivery_decision.get("delivery_status"),
        "qa_status": delivery_decision.get("qa_status"),
        "artifact_available": True,
        "public_message": terminal_message if partial_ppt else delivery_decision.get("public_message") or public_message(delivery_decision.get("delivery_status") or "COMPLETED_CLEAN", delivery_decision.get("review_count") or 0),
        "review_count": delivery_decision.get("review_count") or 0,
        "qa_review_summary": delivery_decision.get("qa_review_summary") or {"issues": []},
        **({"local_review_repair": local_review_repair} if local_review_repair is not None else {}),
        "history_label": delivery_decision.get("history_label"),
        "workflow_id": run_audit.get("workflow_id"),
        "total_processing_seconds": run_audit.get("total_processing_seconds"),
        "knowledge_used": bool(selected),
        "knowledge_status": selection.get("knowledge_status"),
        "recommended_knowledge_count": len(recommended_ids),
        "selected_knowledge_count": len(selected),
        "active_guardrail_ids": guardrail_ids,
        "content_metrics": longform_result.get("content_metrics") or {},
    }


def repair_artifact(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    selection = read_json(run_dir / "knowledge_selection.json", "repair_selection")
    return generate_artifact(run_id, selection.get("selected_positive_ku_ids", []), selection.get("clarification_answers", {}), auto_repair=True)


def resolve_run_artifact(run_dir: Path, audit: dict[str, Any] | None = None) -> Path | None:
    audit = audit or {}
    candidates: list[Path] = []
    name = str(audit.get("artifact_display_name") or "")
    if name:
        candidates.append(run_dir / name)
    meta = load_last_valid_artifact(run_dir)
    if meta.get("last_valid_artifact_path"):
        candidates.append(Path(str(meta["last_valid_artifact_path"])))
    for folder in (run_dir, run_dir / "last_valid_artifact", run_dir / "artifact", run_dir / "_blocked_artifact", run_dir / "first_draft"):
        if folder.is_dir():
            candidates.extend(sorted(folder.glob("*.docx"), key=lambda item: item.stat().st_mtime, reverse=True))
            candidates.extend(sorted(folder.glob("*.pptx"), key=lambda item: item.stat().st_mtime, reverse=True))
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        if not artifact_physically_invalid(path):
            return path
    return None


def repair_local_artifact(run_id: str) -> dict[str, Any]:
    """Local-only leftover repair. Never re-calls the full provider."""
    run_dir = _checked(RUNS_DIR / run_id)
    audit = read_json(run_dir / "run_audit.json", "repair_local_audit") if (run_dir / "run_audit.json").is_file() else {}
    brief = read_json(run_dir / "brief.json", "repair_local_brief")
    target = resolve_run_artifact(run_dir, audit)
    if target is None:
        raise MMFError("没有可修复的方案文件。")
    if not artifact_physically_invalid(target):
        record_last_valid_artifact(run_dir, target)
    try:
        repair_docx_artifact(target)
    except Exception:
        restore_last_valid_artifact(run_dir, target)
        raise
    if artifact_physically_invalid(target):
        restore_last_valid_artifact(run_dir, target)
    else:
        record_last_valid_artifact(run_dir, target)
    report = evaluate_docx_artifact(target, canonical_facts=brief, brief=brief, contracts=[])
    decision = classify_fail_soft(artifact_path=target, findings=list(report.get("findings") or []), repaired=True, repair_attempts=1)
    write_json(run_dir / "final_artifact_qa.json", report)
    write_review_summary(run_dir, decision["qa_review_summary"])
    return {
        "run_id": run_id,
        "status": "generation_completed",
        "medium": brief.get("medium") or "WORD",
        "artifact_name": target.name,
        "download_url": f"/files/{run_id}/{target.name}",
        "artifact_available": True,
        "delivery_status": decision["delivery_status"],
        "qa_status": decision["qa_status"],
        "public_message": decision["public_message"],
        "review_count": decision["review_count"],
        "qa_review_summary": decision["qa_review_summary"],
        "history_label": decision["history_label"],
        "local_repair_only": True,
    }


def load_run_recommendation(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    selection_path = run_dir / "knowledge_selection.json"
    if not selection_path.is_file():
        raise MMFError("该记录的知识推荐尚未完成，暂时不能继续生成。")
    return read_json(selection_path, "knowledge_selection_resume")


def load_run_status(run_id: str) -> dict[str, Any]:
    row = next((item for item in list_runs() if item["run_id"] == run_id), None)
    if row is None:
        raise MMFError("Run不存在。")
    status_path = _checked(RUNS_DIR / run_id / "generation_status.json")
    if status_path.is_file():
        status_record = read_json(status_path, "generation_status")
        if isinstance(status_record.get("result"), dict):
            row["result"] = status_record["result"]
        if status_record.get("error"):
            row["error"] = status_record["error"]
        row["started_at"] = status_record.get("started_at")
        row["generation_elapsed_seconds"] = status_record.get("elapsed_seconds")
        if row.get("total_processing_seconds") is None:
            row["elapsed_seconds"] = status_record.get("elapsed_seconds")
        row["progress_message"] = status_record.get("message")
        row["generation_job_status"] = status_record.get("status")
    row["checked_at"] = now_iso()
    return row


def _validated_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not value or ".." in value or "/" in value or "\\" in value:
        raise MMFError("找不到这次生成任务")
    if not value.replace("_", "").replace("-", "").isalnum():
        raise MMFError("找不到这次生成任务")
    return value


def _force_rmtree(path: Path) -> None:
    target = Path(path)
    if not target.exists():
        return

    def _unlock(item: Path) -> None:
        try:
            os.chmod(item, stat.S_IWRITE)
        except OSError:
            pass

    def _onexc(func, name, exc):
        _unlock(Path(name))
        try:
            func(name)
        except OSError:
            pass

    last_error: OSError | None = None
    for attempt in range(6):
        if not target.exists():
            return
        try:
            shutil.rmtree(target, onexc=_onexc)
        except TypeError:
            shutil.rmtree(target, onerror=lambda func, name, _err: _onexc(func, name, _err))
        except OSError as exc:
            last_error = exc
        if not target.exists():
            return
        time.sleep(0.15 * (attempt + 1))
        try:
            target.rmdir()
        except OSError as exc:
            last_error = exc
    if target.exists():
        raise MMFError("无法删除文件夹，可能正被资源管理器打开，请关闭该文件夹后重试") from last_error


def _tender_history_name(folder: Path) -> str:
    extraction_path = folder / "tender" / "extraction.json"
    if extraction_path.is_file():
        try:
            extraction = json.loads(extraction_path.read_text(encoding="utf-8-sig"))
            name = brief_project_fields({}, extraction)["project_name"]
            if name.get("official"):
                return name["value"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    uploads_path = folder / "tender" / "uploads.json"
    if uploads_path.is_file():
        try:
            data = json.loads(uploads_path.read_text(encoding="utf-8-sig"))
            files = data.get("files") or []
            if files:
                name = str(files[0].get("original_filename") or "").strip()
                if name:
                    return Path(name).stem
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass
    return folder.name


def _incomplete_run_row(folder: Path) -> dict[str, Any]:
    status: dict[str, Any] = {}
    status_path = folder / "tender" / "status.json"
    if status_path.is_file():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                status = loaded
        except (OSError, json.JSONDecodeError, TypeError):
            status = {}
    from providers.cursor_account import is_cursor_provider

    account = status.get("provider_name") in {"qoder_account", "zcode_cn"} or is_cursor_provider(status.get("provider_name"))
    from tender_intake.cursor_recognition_recovery import WAITING, pending_choice
    if status.get("recognition_outcome") == WAITING or pending_choice(folder, status):
        status["recognition_outcome"] = WAITING
    stage = str(status.get("stage") or "")
    if status.get("recognition_outcome") == WAITING:
        label = "识别已暂停，等待选择"
    elif status.get("error") or "FAILED" in stage.upper():
        label = "需求文件处理未完成"
    elif account and status.get('recognition_outcome')=='AI_RECOGNITION_SUCCESS':
        label='AI识别完成，待确认需求'
    elif str(status.get("pack_status") or "") == "blocked_clarification":
        label = "需求确认未完成"
    else:
        label = "未完成的任务"
    timing = history_duration(folder)
    return {
        "run_id": folder.name,
        "project_name": _tender_history_name(folder),
        "scenario": "从需求文件创建",
        "medium": "",
        "provider_name": str(status.get("provider_name") or ""),
        "test_mode": False,
        "created_at": datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        "generated": False,
        "run_status": "incomplete_tender",
        "status_label": label,
        "can_resume_recommendation": False,
        "can_resume_tender": True,
        **({'recognition_outcome':status.get('recognition_outcome')} if account else {}),
        "can_check_status": False,
        "artifact_display_name": "",
        "download_url": "",
        "todd_final_imported": False,
        **timing,
        "elapsed_seconds": timing.get("total_processing_seconds") or timing.get("generation_stage_seconds") or 0,
    }


def delete_run(run_id: str, *, delete_files: bool = False) -> dict[str, Any]:
    run_id = _validated_run_id(run_id)
    roots = _mmf_paths.current()
    run_dir = roots.runs_dir / run_id
    output_dir = roots.output_root / run_id
    if run_dir.exists():
        run_dir = _checked(run_dir)
    if not run_dir.is_dir() and not output_dir.exists():
        raise MMFError("找不到这次生成任务")
    if run_dir.resolve() == roots.runs_dir.resolve() or (output_dir.exists() and output_dir.resolve() == roots.output_root.resolve()):
        raise MMFError("找不到这次生成任务")
    if delete_files:
        status_path = run_dir / "generation_status.json"
        if run_dir.is_dir() and status_path.is_file():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, TypeError):
                status = {}
            if status.get("status") == "running":
                raise MMFError("该任务仍在生成中，请等待完成后再删除文件")
        deleted_paths: list[str] = []
        if output_dir.exists():
            output_resolved = _checked(output_dir)
            _force_rmtree(output_resolved)
            deleted_paths.append(str(output_resolved))
        if run_dir.is_dir():
            _force_rmtree(run_dir)
            deleted_paths.append(str(run_dir))
        leftover = [str(item) for item in (output_dir, run_dir) if item.exists()]
        if leftover:
            raise MMFError("文件夹未能完全删除，请关闭资源管理器中对应目录后重试")
        return {
            "ok": True,
            "run_id": run_id,
            "mode": "history_and_files",
            "deleted_files": True,
            "deleted_paths": deleted_paths,
            "message": "已删除历史记录及相关文件夹",
        }
    write_json(run_dir / "history_hidden.json", {
        "hidden": True,
        "hidden_at": now_iso(),
        "mode": "history_only",
    })
    return {
        "ok": True,
        "run_id": run_id,
        "mode": "history",
        "deleted_files": False,
        "message": "已从历史记录中移除，生成文件仍保留",
    }


def list_runs() -> list[dict[str, Any]]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for folder in sorted(RUNS_DIR.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if not folder.is_dir():
            continue
        if (folder / "history_hidden.json").exists():
            continue
        if not (folder / "brief.json").exists():
            rows.append(_incomplete_run_row(folder))
            continue
        brief = read_json(folder / "brief.json", "run_history")
        audit = read_json(folder / "run_audit.json", "run_history_audit") if (folder / "run_audit.json").exists() else {}
        artifact = resolve_run_artifact(folder, audit)
        if artifact is None:
            artifact = folder / str(audit.get("artifact_display_name", "") or ("final.docx" if brief.get("medium") == "WORD" else "final.pptx"))
        recommendation_ready = (folder / "knowledge_selection.json").is_file()
        generation_started = (folder / "generation_input.json").is_file()
        recommendation_started = (folder / "provider_recommendation" / "prompt.md").is_file()
        status_path = folder / "generation_status.json"
        status_record = read_json(status_path, "generation_status") if status_path.is_file() else {}
        provider_prompt = folder / "provider_generation" / "prompt.md"
        generation_age = datetime.now().timestamp() - provider_prompt.stat().st_mtime if provider_prompt.is_file() else None
        delivery_status = str(audit.get("delivery_status") or "")
        review_count = int(audit.get("review_count") or 0)
        from planning.ppt_delivery import completion_state
        ppt_completion = completion_state(folder) if brief.get("medium") == "PPT" else None
        if ppt_completion is not None and not ppt_completion["complete"] and generation_started:
            run_status = "generation_in_progress" if status_record.get("status") == "running" else "generation_partial"
            status_label = f"正文{ppt_completion['completed_body_pages']}/{ppt_completion['planned_body_pages']}页，" + ("正在继续生成" if run_status == "generation_in_progress" else "可继续生成")
        elif artifact.exists():
            run_status = "generation_completed"
            if delivery_status == "NEEDS_REVIEW":
                status_label = f"待人工复核" + (f"：{review_count}项" if review_count else "")
            elif delivery_status == "COMPLETED_WITH_WARNINGS":
                status_label = "生成成功（有提示）"
            elif delivery_status == "TECHNICAL_FAILED":
                status_label = "技术失败"
            else:
                status_label = "生成成功"
        elif status_record.get("status") == "running":
            run_status = "generation_in_progress"
            elapsed = int(status_record.get("elapsed_seconds") or 0)
            if elapsed <= 0 and status_record.get("started_at"):
                try:
                    elapsed = max(0, int(datetime.now().timestamp() - datetime.fromisoformat(str(status_record["started_at"])).timestamp()))
                except ValueError:
                    elapsed = 0
            engine = engine_display(str(brief.get("provider_name") or ""))
            status_label = f"{engine}正在生成，请到「新建方案」第3步查看进度"
        elif status_record.get("status") == "cancelled":
            run_status = "generation_cancelled"
            status_label = "已停止生成，可恢复知识确认后重试"
        elif status_record.get("status") == "failed":
            run_status = "generation_failed"
            status_label = "生成失败，可恢复知识确认后重试"
        elif (folder / "compliance_report.json").is_file():
            run_status = "generation_incomplete"
            status_label = "生成未完成，请检查"
        elif generation_started:
            if generation_age is not None and generation_age <= 2100:
                run_status = "generation_in_progress"
                engine = engine_display(str(brief.get("provider_name") or ""))
                status_label = f"{engine}正在生成，请到「新建方案」第3步查看进度"
            else:
                run_status = "generation_interrupted"
                status_label = "生成已中断，可恢复知识确认后重试"
        elif recommendation_ready:
            run_status = "waiting_todd_knowledge_confirmation"
            status_label = "知识推荐已完成，可继续生成"
        elif recommendation_started:
            run_status = "recommendation_incomplete"
            status_label = "知识推荐未完成"
        else:
            run_status = "created"
            status_label = "任务已创建"
        legacy_elapsed = duration_seconds(
            status_record.get("started_at") or audit.get("generation_started_at"),
            status_record.get("finished_at") or audit.get("generation_finished_at"),
            status_record.get("elapsed_seconds") or audit.get("generation_elapsed_seconds"),
        )
        timing = history_duration(folder, legacy_elapsed)
        rows.append({
            "run_id": folder.name,
            "project_name": brief.get("project_name", ""),
            "scenario": brief.get("scenario", ""),
            "medium": brief.get("medium", ""),
            "provider_name": brief.get("provider_name", ""),
            "test_mode": audit.get("test_mode", False),
            "created_at": datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            "generated": artifact.exists(),
            "run_status": run_status,
            "status_label": status_label,
            "can_resume_recommendation": recommendation_ready and (run_status == "generation_partial" or (run_status in {"waiting_todd_knowledge_confirmation", "generation_failed", "generation_interrupted", "generation_incomplete", "generation_cancelled"} and not artifact.exists())),
            "ppt_completion": ppt_completion,
            "can_check_status": run_status == "generation_in_progress",
            "artifact_display_name": artifact.name if artifact.exists() else "",
            "download_url": f"/files/{folder.name}/{artifact.name}" if artifact.exists() else "",
            "delivery_status": delivery_status or ("COMPLETED_CLEAN" if artifact.exists() else ""),
            "qa_status": audit.get("qa_status") or "",
            "review_count": review_count,
            "artifact_available": bool(artifact.exists()),
            "history_label": status_label,
            "todd_final_imported": (folder / ("todd_final.docx" if brief.get("medium") == "WORD" else "todd_final.pptx")).exists(),
            "engine_label": engine_display(str(brief.get("provider_name") or "")),
            "speed_profile": brief.get("speed_profile") or "balanced",
            "content_metrics": audit.get("content_metrics") or {},
            **timing,
            "elapsed_seconds": timing.get("total_processing_seconds") if timing.get("total_processing_seconds") is not None else timing.get("generation_stage_seconds") or 0,
        })
    return rows


def save_todd_final(run_id: str, filename: str, data: bytes) -> Path:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    brief = read_json(run_dir / "brief.json", "run_brief")
    expected = ".docx" if brief["medium"] == "WORD" else ".pptx"
    if Path(filename).suffix.lower() != expected:
        raise MMFError(f"本Run只接受{expected}格式的Todd修改稿。")
    target = _checked(run_dir / f"todd_final{expected}")
    target.write_bytes(data)
    return target


def initial_state() -> dict[str, Any]:
    return {
        "task": "MMF-005 Provider-independent Knowledge & Commitment Governance",
        "status": "completed",
        "acceptance_status": "approved",
        "acceptance_actor": "Todd",
        "acceptance_timestamp": "2026-08-30T13:30:50+08:00",
        "acceptance_record": "Todd_MMF005_人工验收记录.json",
        "runtime_mode": "MODE_C",
        "supported_scenarios": SCENARIOS,
        "supported_media": MEDIA,
        "knowledge_source": {"accepted_ku": 42, "candidate_positive": 18, "candidate_guardrail": 19},
        "default_provider": "grok_build",
        "updated_at": now_iso(),
    }


RUNS_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
if not STATE_FILE.exists():
    write_json(STATE_FILE, initial_state())
if not COMPLIANCE_RULES.exists():
    COMPLIANCE_RULES = RUNTIME_DIR / "provider_compliance_rules_v0.1.json"
    write_default_rules(COMPLIANCE_RULES)
if not REVIEW_FILE.exists():
    write_json(REVIEW_FILE, {"review_type": "product_acceptance", "review_version": "MMF-004", "status": "in_progress", "updated_at": now_iso(), "answers": {}})
