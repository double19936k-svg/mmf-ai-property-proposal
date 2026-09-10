from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from providers import ProviderError, ProviderManager, ProviderUnavailableError
from compliance import evaluate_compliance, write_default_rules
from recovery_orchestrator import leftover_warning_rows, recover_governance, withhold_delivery
from delivery_state import (
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
from longform.orchestrator import generate_longform
from longform.reasoning import normalize_speed_profile, product_speed_catalog
from planning.canonical import ensure_requirement_pack
from planning.planner import PlanningError
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


import paths as _mmf_paths
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
    }


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


def _audit_read(path: Path, purpose: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    relative = _checked(path).relative_to(RUNTIME_ROOT).as_posix()
    record = {"at": now_iso(), "operation": "read", "purpose": purpose, "relative_path": relative}
    with ACCESS_AUDIT_FILE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_json(path: Path, purpose: str = "runtime_data") -> Any:
    checked = _checked(path)
    _audit_read(checked, purpose)
    return json.loads(checked.read_text(encoding="utf-8-sig"))


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
    checked.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    last_exc: OSError | None = None
    with _json_write_lock(checked):
        for attempt in range(8):
            tmp = checked.with_name(f"{checked.name}.{os.getpid()}.{threading.get_ident()}.{attempt}.tmp")
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, checked)
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
        "SECTION_QA": "content_and_compliance_qa",
        "AUTO_REPAIRING": "section_repair",
        "RECHECKING": "content_and_compliance_qa",
        "FACT_COMMITMENT_CHECK": "content_and_compliance_qa",
        "ASSEMBLING": "artifact_assembly",
        "FINAL_ARTIFACT_QA": "final_artifact_qa",
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


def read_jsonl(path: Path, purpose: str = "asset") -> list[dict[str, Any]]:
    checked = _checked(path)
    _audit_read(checked, purpose)
    return [json.loads(line) for line in checked.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


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
            "system_prompt": "你是AI物业方案智能体的封闭包知识推荐引擎。只能使用提示中的显式输入；不读文件、不联网、不调用工具；只输出一个完整JSON对象。",
            "prompt": _recommendation_prompt(brief, catalog),
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
    _write_mmf006a_state(
        run_id=run_id, provider_test={selected_provider: "PASS" if recognition_success else "FAILED_WITH_LOCAL_FALLBACK"},
        pack={"status": result["pack"]["status"], "requirements": len(result["pack"]["requirements"])},
        checkpoints={"A3_PACK_BUILDER_PASS": True, "A4_PROVIDER_UNDERSTANDING_PASS": recognition_success},
    )
    return {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "status": result["pack"]["status"], "pack": result["pack"], "checkpoint": result["status"]}


def load_tender_run(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    tender_dir = run_dir / "tender"
    status = read_tender_status(run_id)
    pack = None
    pack_path = tender_dir / "requirement_pack.json"
    if pack_path.is_file() and str(status.get("pack_status") or "") != "understanding":
        try:
            loaded = json.loads(pack_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                pack = loaded
        except (OSError, json.JSONDecodeError, TypeError):
            pack = None
    return {"run_id": run_id, "status": status, "pack": pack, "brief_seeded": (run_dir / "brief.json").exists(), "knowledge_recommended": (run_dir / "knowledge_selection.json").exists()}


def acknowledge_tender_local_fallback(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    pack_path = run_dir / "tender" / "requirement_pack.json"
    if not run_dir.is_dir() or not pack_path.is_file():
        raise MMFError("本地解析结果不存在，请重新识别。")
    status = read_tender_status(run_id)
    if status.get("recognition_outcome") != "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK":
        raise MMFError("当前任务不需要确认本地解析降级。")
    status.update({
        "local_fallback_confirmed": True,
        "local_fallback_confirmed_at": now_iso(),
        "message": "已确认继续使用本地解析结果；生成完整方案前仍应人工核对关键要求。",
    })
    write_json(run_dir / "tender" / "status.json", status)
    return {"run_id": run_id, "status": status, "pack": read_json(pack_path, "tender_requirement_pack")}


def confirm_tender_run(run_id: str, decisions: dict[str, Any], brief_options: dict[str, Any]) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    pack_path = run_dir / "tender" / "requirement_pack.json"
    if not pack_path.is_file():
        raise MMFError("Requirement Pack尚未生成。")
    tender_status = read_tender_status(run_id)
    full_proposal = str(brief_options.get("scenario") or "") in FULL_PROPOSAL_SCENARIOS
    fallback_requires_confirmation = (
        tender_status.get("recognition_outcome") == "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK"
        and not tender_status.get("local_fallback_confirmed")
    )
    if full_proposal and fallback_requires_confirmation:
        raise MMFError("AI需求识别未完成。请先重新识别，或明确选择‘继续使用本地解析结果’后再生成完整方案。")
    try:
        with track_stage(run_dir, "brief_generation", workflow_kind="tender_file"):
            pack = read_json(pack_path, "tender_requirement_pack")
            decisions = dict(decisions or {})
            if "accept_remaining" not in decisions:
                decisions["accept_remaining"] = True
            confirmed = apply_tender_confirmation(pack, decisions)
            validate_requirement_pack(confirmed)
            write_json(pack_path, confirmed)
            write_json(run_dir / "tender" / "confirmation.json", {"confirmed_at": now_iso(), "decisions": decisions, "pack_version": confirmed.get("pack_version")})
            if not confirmed["confirmation"]["ready_for_brief_seed"]:
                write_json(run_dir / "tender" / "status.json", {"run_id": run_id, "stage": "A5_CONFIRMATION_REQUIRED", "upload_completed": True, "extraction_completed": True, "pack_status": "blocked_clarification", "updated_at": now_iso()})
                return {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "status": "blocked_clarification", "pack": confirmed, "message": "仍有高影响要求、冲突、疑似模板或缺失事实待确认。"}
            brief = seed_tender_brief(confirmed, brief_options)
            write_json(run_dir / "brief.json", validate_brief(brief))
            _write_mmf006a_state(run_id=run_id, confirmation={"status": "PASS"}, brief_seed={"status": "PASS"}, checkpoints={"A5_CONFIRMATION_UX_PASS": True, "A6_BRIEF_SEED_PASS": True})
        recommendation = _recommend_knowledge_in_run(brief, run_id, run_dir)
        write_json(run_dir / "tender" / "status.json", {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "stage": "MMF006A_COMPLETED_PENDING_TODD_ACCEPTANCE", "upload_completed": True, "extraction_completed": True, "pack_status": "ready_for_plan", "confirmation_completed": True, "brief_seed_completed": True, "knowledge_recommendation_completed": True, "updated_at": now_iso()})
        return {"run_id": run_id, "workflow_id": workflow_audit_fields(run_dir).get("workflow_id"), "status": "MMF006A_completed_pending_todd_acceptance", "pack": confirmed, "brief": brief, "recommendation": recommendation}
    except Exception:
        finish_workflow(run_dir, "failed")
        raise


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
    canonical_facts = ((longform_result.get("bundle") or {}).get("global_state") or brief)
    quality = longform_result.get("quality_regression") or {}
    _write_generation_phase(run_dir, "FACT_COMMITMENT_CHECK", "正在检查事实与承诺。")
    recovery = recover_governance(
        generated,
        brief=brief,
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
    if recovery.get("repair_count"):
        _write_generation_phase(run_dir, "AUTO_REPAIRING", f"正在自动完善{recovery['repair_count']}处内容。")
        _write_generation_phase(run_dir, "RECHECKING", "正在复核已完善的方案内容。")
    write_json(run_dir / "generation_raw.json", generated)
    write_json(run_dir / "recovery_audit.json", {"level": recovery.get("level"), "terminal": recovery.get("terminal"), "rounds": recovery.get("audit") or []})
    if quality.get("status") == "FAIL":
        findings = list(artifact_qa.get("findings") or [])
        findings.append({
            "rule_id": "QR-001",
            "severity": "BLOCK",
            "issue": "QUALITY_REGRESSION_GATE FAIL: " + "、".join(str(item) for item in (quality.get("missing") or [])[:8]),
        })
        artifact_qa = {**artifact_qa, "status": "BLOCK", "findings": findings, "QUALITY_REGRESSION_GATE": "FAIL"}
    write_json(run_dir / "commitment_provenance_report.json", commitment)
    write_json(run_dir / "artifact_qa_report.json", artifact_qa)
    write_json(run_dir / "compliance_report.json", compliance)
    write_json(run_dir / "quality_regression.json", quality)
    repair_attempts = 0
    while delivery_blocked(compliance, commitment, artifact_qa, quality) and auto_repair:
        if not _recoverable_integrity_failure(compliance, commitment, artifact_qa, quality):
            break
        repair_limit = _automatic_repair_limit(compliance, commitment, artifact_qa, quality)
        if repair_attempts >= repair_limit:
            break
        repair_attempts += 1
        _write_generation_phase(run_dir, "AUTO_REPAIRING", "正在自动完善方案内容。")
        repair_started = time.monotonic()
        repair_error: Exception | None = None
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
        except Exception as exc:
            repair_error = exc
        finally:
            repair_elapsed = round(time.monotonic() - repair_started, 3)
            external_repair_wall += repair_elapsed
            record_longform_attempt(run_dir, repair_elapsed, repair_attempt=True, outcome="failed" if repair_error else "completed")
        if repair_error is not None:
            record_stage_duration(run_dir, "content_and_compliance_qa", max(0.0, (time.monotonic() - qa_started) - external_repair_wall), outcome="failed")
            actual_time = round(time.monotonic() - wall_started, 3)
            _write_run_audit(run_dir, {
                "run_id": run_id, "status": "repair_exception", "partial": True, "phase": "repair",
                "error": str(repair_error), "error_type": type(repair_error).__name__, "actual_time": actual_time,
                "repair_reasoning": _repair_reasoning_value(longform_result), **_longform_runtime_audit(longform_result),
                "created_at": now_iso(),
            })
            raise MMFError(str(repair_error)) from repair_error
        raw_quote_audit = quote_serialization_status(longform_result["generated"])
        generated = apply_local_repairs(apply_artifact_repairs(longform_result["generated"]))
        postprocess_quote_audit = quote_serialization_status(generated)
        write_json(run_dir / f"generation_raw_repair_{repair_attempts}.json", generated)
        canonical_facts = ((longform_result.get("bundle") or {}).get("global_state") or brief)
        quality = longform_result.get("quality_regression") or quality
        recovery = recover_governance(
            generated,
            brief=brief,
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
        if quality.get("status") == "FAIL":
            findings = list(artifact_qa.get("findings") or [])
            findings.append({"rule_id": "QR-001", "severity": "BLOCK", "issue": "QUALITY_REGRESSION_GATE FAIL"})
            artifact_qa = {**artifact_qa, "status": "BLOCK", "findings": findings, "QUALITY_REGRESSION_GATE": "FAIL"}
        write_json(run_dir / f"commitment_provenance_report_repair_{repair_attempts}.json", commitment)
        write_json(run_dir / f"artifact_qa_report_repair_{repair_attempts}.json", artifact_qa)
        write_json(run_dir / f"compliance_report_repair_{repair_attempts}.json", compliance)
        _write_generation_phase(run_dir, "RECHECKING", "正在复核已完善的方案内容。")
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
        _write_generation_phase(run_dir, "TERMINAL_BLOCKED", decision["public_message"], recoverable=False)
        actual_time = round(time.monotonic() - wall_started, 3)
        blocked = {
            "run_id": run_id,
            "status": "technical_failed",
            "qa_state": DELIVERY_TECHNICAL,
            "delivery_status": decision["delivery_status"],
            "qa_status": decision["qa_status"],
            "artifact_available": False,
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
        cmd = [str(runtime.get("python_executable") or sys.executable), str(APP_ROOT / "build_docx.py"), str(content_path), str(final_path)]
    else:
        final_path = artifact_display_path(run_dir, brief)
        qa_dir = run_dir / "_qa_ppt"
        cmd = [str(runtime.get("node_executable") or "node"), str(APP_ROOT / "build_ppt.mjs"), str(content_path), str(final_path), str(qa_dir)]
    try:
        completed = subprocess.run(cmd, cwd=str(APP_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, shell=False)
    except Exception as exc:
        record_stage_duration(run_dir, "artifact_assembly", time.monotonic() - render_started, outcome="failed")
        actual_time = round(time.monotonic() - wall_started, 3)
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
        _record_terminal_latency(
            run_id=run_id, brief=brief, provider_name=provider_name, longform_result=longform_result,
            status="render_blocked", eligible=False, actual_time=actual_time, qa_seconds=qa_seconds, build_id=build_id,
        )
        raise MMFError(str(exc)) from exc
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
        _record_terminal_latency(
            run_id=run_id, brief=brief, provider_name=provider_name, longform_result=longform_result,
            status="render_blocked", eligible=False, actual_time=actual_time, qa_seconds=qa_seconds, build_id=build_id,
        )
        raise MMFError("生成失败，请查看运行日志")
    record_stage_duration(run_dir, "artifact_assembly", time.monotonic() - render_started)
    if not artifact_physically_invalid(final_path):
        record_last_valid_artifact(run_dir, final_path)
    delivery_decision = classify_fail_soft(artifact_path=final_path, artifact_qa=artifact_qa, commitment=commitment, compliance=compliance, quality=quality, repair_attempts=repair_attempts)
    _write_generation_phase(run_dir, "FINAL_ARTIFACT_QA", "正在执行最终文件检查。")
    if brief["medium"] == "WORD":
        final_qa_started = time.monotonic()
        try:
            final_artifact_qa = evaluate_docx_artifact(
                final_path,
                canonical_facts=canonical_facts,
                brief=brief,
                contracts=contracts,
            )
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
                _write_generation_phase(run_dir, "AUTO_REPAIRING", "正在自动完善方案内容。")
                final_artifact_qa = evaluate_docx_artifact(
                    final_path,
                    canonical_facts=canonical_facts,
                    brief=brief,
                    contracts=contracts,
                )
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
        write_json(run_dir / "final_artifact_qa.json", final_artifact_qa)
        if artifact_physically_invalid(final_path):
            restore_last_valid_artifact(run_dir, final_path)
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
            delivery_decision = classify_fail_soft(
                artifact_path=final_path,
                findings=list(final_artifact_qa.get("findings") or []),
                artifact_qa=artifact_qa,
                commitment=commitment,
                compliance=compliance,
                quality=quality,
                repair_attempts=max(repair_attempts, len(repair_rounds)),
                repaired=bool(repair_rounds),
            )
        write_review_summary(run_dir, delivery_decision["qa_review_summary"])
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
        "generated_artifact_path": str(final_path.relative_to(RUNTIME_ROOT)),
        "artifact_display_name": final_path.name,
        "artifact_internal_path": str(final_path.relative_to(RUNTIME_ROOT)),
        "compliance_status": compliance["status"],
        "commitment_provenance_status": commitment["status"],
        "artifact_qa_status": artifact_qa["status"],
        "final_artifact_qa_status": (final_artifact_qa.get("status") if brief["medium"] == "WORD" else "NOT_APPLICABLE"),
        "leftover_accepted": bool(leftover_warnings),
        "leftover_warning_count": len(leftover_warnings),
        "delivery_status": delivery_decision.get("delivery_status"),
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
        "created_at": now_iso(),
        "generation_started_at": (read_json(run_dir / "generation_status.json", "generation_status") or {}).get("started_at") if (run_dir / "generation_status.json").is_file() else None,
    }
    run_audit["status"] = "generation_completed"
    run_audit = _write_run_audit(run_dir, run_audit)
    _write_generation_phase(run_dir, "COMPLETED", delivery_decision.get("public_message") or "方案生成完成。", recoverable=False)
    _record_terminal_latency(
        run_id=run_id,
        brief=brief,
        provider_name=provider_name,
        longform_result=longform_result,
        status="generation_completed",
        eligible=True,
        actual_time=actual_time,
        qa_seconds=qa_seconds,
        build_id=build_id,
    )
    return {
        "run_id": run_id,
        "status": "generation_completed",
        "medium": brief["medium"],
        "artifact_name": final_path.name,
        "download_url": f"/files/{run_id}/{final_path.name}",
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
        "public_message": delivery_decision.get("public_message") or public_message(delivery_decision.get("delivery_status") or "COMPLETED_CLEAN", delivery_decision.get("review_count") or 0),
        "review_count": delivery_decision.get("review_count") or 0,
        "qa_review_summary": delivery_decision.get("qa_review_summary") or {"issues": []},
        "history_label": delivery_decision.get("history_label"),
        "workflow_id": run_audit.get("workflow_id"),
        "total_processing_seconds": run_audit.get("total_processing_seconds"),
        "knowledge_used": bool(selected),
        "knowledge_status": selection.get("knowledge_status"),
        "recommended_knowledge_count": len(recommended_ids),
        "selected_knowledge_count": len(selected),
        "active_guardrail_ids": guardrail_ids,
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
    for folder in (run_dir, run_dir / "last_valid_artifact", run_dir / "artifact", run_dir / "_blocked_artifact"):
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
    stage = str(status.get("stage") or "")
    if status.get("error") or "FAILED" in stage.upper():
        label = "需求文件处理未完成"
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
        if artifact.exists():
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
            "can_resume_recommendation": recommendation_ready and run_status in {"waiting_todd_knowledge_confirmation", "generation_failed", "generation_interrupted", "generation_incomplete", "generation_cancelled"} and not artifact.exists(),
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
    write_default_rules(COMPLIANCE_RULES)
if not REVIEW_FILE.exists():
    write_json(REVIEW_FILE, {"review_type": "product_acceptance", "review_version": "MMF-004", "status": "in_progress", "updated_at": now_iso(), "answers": {}})
