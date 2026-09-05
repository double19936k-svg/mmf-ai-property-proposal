from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from providers import ProviderError, ProviderManager, ProviderUnavailableError
from compliance import evaluate_compliance, write_default_rules
from governance import apply_artifact_repairs, apply_local_repairs, build_contracts, evaluate_artifact, evaluate_commitments, evaluate_selection, merge_longform_qa
from longform.orchestrator import generate_longform
from planning.canonical import ensure_requirement_pack
from planning.planner import PlanningError
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


class MMFError(RuntimeError):
    pass


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


def write_json(path: Path, value: Any) -> None:
    checked = _checked(path)
    checked.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    tmp = checked.with_name(checked.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(checked)


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


def _recommend_knowledge_in_run(brief_input: dict[str, Any], run_id: str, run_dir: Path) -> dict[str, Any]:
    brief = validate_brief(brief_input)
    provider = provider_manager().get(brief["provider_name"], require_available=True)
    catalog = corpus_catalog()
    write_json(run_dir / "brief.json", brief)
    reused = _reuse_recommendation(run_dir)
    if reused:
        result = {**reused, "provider_metadata": {"reused_existing_recommendation": True}}
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
        result = provider.recommend_knowledge(request, _next_provider_task_dir(run_dir, "provider_recommendation"))
    pos_allowed = {row["ku_id"] for row in catalog["positive"]}
    grd_allowed = {row["ku_id"] for row in catalog["guardrail"]}
    recommended = [row for row in result.get("recommended_positive") or [] if isinstance(row, dict) and row.get("ku_id") in pos_allowed]
    guardrails = [row for row in result.get("applicable_guardrails") or [] if isinstance(row, dict) and row.get("ku_id") in grd_allowed]
    result = {**result, "recommended_positive": recommended, "applicable_guardrails": guardrails, "missing_information": result.get("missing_information") if isinstance(result.get("missing_information"), list) else []}
    if not recommended:
        raise MMFError("当前引擎没有推荐出可用知识。请重试，或改用千问。")
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
        "provider_metadata": result["provider_metadata"],
    }
    write_json(run_dir / "knowledge_selection.json", selection)
    return selection


def recommend_knowledge(brief_input: dict[str, Any]) -> dict[str, Any]:
    run_id = new_run_id()
    run_dir = _checked(RUNS_DIR / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    return _recommend_knowledge_in_run(brief_input, run_id, run_dir)


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
    try:
        uploads = save_tender_uploads(RUNTIME_ROOT, run_dir, files)
        _write_mmf006a_state(run_id=run_id, upload={"status": "PASS", "file_count": len(uploads)}, checkpoints={"A1_UPLOAD_PASS": True})
        extraction = extract_tender_run(RUNTIME_ROOT, run_dir)
        status = {
            "schema_version": "mmf006a-status-v0.1", "run_id": run_id, "provider_name": provider_name,
            "upload_completed": True, "extraction_completed": True, "chunks_total": 0, "chunks_completed": 0,
            "failed_chunk": None, "pack_status": "not_started", "stage": "A2_EXTRACTION_PASS", "updated_at": now_iso(),
        }
        write_json(run_dir / "tender" / "status.json", status)
        _write_mmf006a_state(extraction={"status": "PASS", "processing_mode": extraction["processing_mode"]}, checkpoints={"A2_EXTRACTION_PASS": True})
        return {"run_id": run_id, "status": "extraction_completed", "provider_name": provider_name, "uploads": uploads, "extraction_summary": {"processing_mode": extraction["processing_mode"], "files": len(extraction["files"]), "pages": len(extraction["pages"]), "paragraphs": len(extraction["paragraphs"]), "tables": len(extraction["tables"]), "warnings": extraction["warnings"]}}
    except Exception:
        if not (run_dir / "tender" / "status.json").exists():
            write_json(run_dir / "tender" / "status.json", {"run_id": run_id, "stage": "A2_EXTRACTION_FAILED", "upload_completed": (run_dir / "tender" / "uploads.json").exists(), "extraction_completed": False, "updated_at": now_iso()})
        raise


def process_tender_run(run_id: str, provider_name: str | None = None) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    status_path = run_dir / "tender" / "status.json"
    status = read_json(status_path, "tender_status")
    selected_provider = provider_name or status.get("provider_name")
    provider = provider_manager().get(selected_provider, require_available=True)
    extraction = read_json(run_dir / "tender" / "extraction.json", "tender_extraction")
    result = understand_tender_run(run_dir, extraction, provider, resume=True)
    _write_mmf006a_state(
        run_id=run_id, provider_test={selected_provider: "PASS"},
        pack={"status": result["pack"]["status"], "requirements": len(result["pack"]["requirements"])},
        checkpoints={"A3_PACK_BUILDER_PASS": True, "A4_PROVIDER_UNDERSTANDING_PASS": True},
    )
    return {"run_id": run_id, "status": result["pack"]["status"], "pack": result["pack"], "checkpoint": result["status"]}


def load_tender_run(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    tender_dir = run_dir / "tender"
    status = read_json(tender_dir / "status.json", "tender_status") if (tender_dir / "status.json").exists() else {}
    pack = read_json(tender_dir / "requirement_pack.json", "tender_requirement_pack") if (tender_dir / "requirement_pack.json").exists() else None
    return {"run_id": run_id, "status": status, "pack": pack, "brief_seeded": (run_dir / "brief.json").exists(), "knowledge_recommended": (run_dir / "knowledge_selection.json").exists()}


def confirm_tender_run(run_id: str, decisions: dict[str, Any], brief_options: dict[str, Any]) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    pack_path = run_dir / "tender" / "requirement_pack.json"
    if not pack_path.is_file():
        raise MMFError("Requirement Pack尚未生成。")
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
        return {"run_id": run_id, "status": "blocked_clarification", "pack": confirmed, "message": "仍有高影响要求、冲突、疑似模板或缺失事实待确认。"}
    brief = seed_tender_brief(confirmed, brief_options)
    write_json(run_dir / "brief.json", validate_brief(brief))
    _write_mmf006a_state(run_id=run_id, confirmation={"status": "PASS"}, brief_seed={"status": "PASS"}, checkpoints={"A5_CONFIRMATION_UX_PASS": True, "A6_BRIEF_SEED_PASS": True})
    recommendation = _recommend_knowledge_in_run(brief, run_id, run_dir)
    write_json(run_dir / "tender" / "status.json", {"run_id": run_id, "stage": "MMF006A_COMPLETED_PENDING_TODD_ACCEPTANCE", "upload_completed": True, "extraction_completed": True, "pack_status": "ready_for_plan", "confirmation_completed": True, "brief_seed_completed": True, "knowledge_recommendation_completed": True, "updated_at": now_iso()})
    return {"run_id": run_id, "status": "MMF006A_completed_pending_todd_acceptance", "pack": confirmed, "brief": brief, "recommendation": recommendation}


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
    return [{key: by_id[ku_id][key] for key in ("ku_id", "core_knowledge", "applicability", "non_applicable_conditions")} for ku_id in ids]


def _public_guardrails(catalog: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    by_id = {row["ku_id"]: row for row in catalog["guardrail"]}
    return [{key: by_id[ku_id][key] for key in ("ku_id", "core_knowledge", "reason", "risk_tags")} for ku_id in ids]


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


def generate_artifact(run_id: str, selected_positive_ids: list[str], clarification_answers: dict[str, str], auto_repair: bool = True) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    if not run_dir.is_dir():
        raise MMFError("Run不存在。")
    brief = read_json(run_dir / "brief.json", "run_brief")
    recommendation = read_json(run_dir / "knowledge_selection.json", "knowledge_selection")
    provider_name = recommendation["provider_name"]
    provider = provider_manager().get(provider_name, require_available=True)
    recommended_ids = {row["ku_id"] for row in recommendation["recommended_positive"]}
    auto_selected = set(recommendation.get("auto_selected_positive_ids", []))
    selected = list(dict.fromkeys([*auto_selected, *[ku_id for ku_id in selected_positive_ids if ku_id in recommended_ids]]))
    if not selected:
        raise MMFError("请至少保留一条推荐知识。")
    guardrail_ids = [row["ku_id"] for row in recommendation["applicable_guardrails"]]
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
    try:
        longform_result = generate_longform(
            run_dir=run_dir,
            provider=provider,
            provider_name=provider_name,
            brief=brief,
            selection=selection,
            selected_ids=selected,
        )
    except PlanningError as exc:
        raise MMFError(str(exc)) from exc
    generated = apply_local_repairs(apply_artifact_repairs(longform_result["generated"]))
    write_json(run_dir / "generation_raw.json", generated)
    commitment = evaluate_commitments(brief, contracts, generated)
    if commitment["status"] == "BLOCK":
        generated = apply_local_repairs(generated)
        commitment = evaluate_commitments(brief, contracts, generated)
    artifact_qa = merge_longform_qa(evaluate_artifact(generated), longform_result.get("depth"))
    if artifact_qa["status"] in {"AUTO_REPAIR", "BLOCK"}:
        generated = apply_artifact_repairs(generated)
        artifact_qa = merge_longform_qa(evaluate_artifact(generated), longform_result.get("depth"))
    compliance = evaluate_compliance(brief, positives, guardrails, generated)
    write_json(run_dir / "commitment_provenance_report.json", commitment)
    write_json(run_dir / "artifact_qa_report.json", artifact_qa)
    write_json(run_dir / "compliance_report.json", compliance)
    repair_attempts = 0
    while (compliance["status"] == "BLOCK" or commitment["status"] == "BLOCK" or artifact_qa["status"] == "BLOCK") and auto_repair and repair_attempts < 1:
        repair_attempts += 1
        longform_result = generate_longform(
            run_dir=run_dir,
            provider=provider,
            provider_name=provider_name,
            brief=brief,
            selection=selection,
            selected_ids=selected,
        )
        generated = apply_local_repairs(apply_artifact_repairs(longform_result["generated"]))
        write_json(run_dir / f"generation_raw_repair_{repair_attempts}.json", generated)
        commitment = evaluate_commitments(brief, contracts, generated)
        artifact_qa = merge_longform_qa(evaluate_artifact(generated), longform_result.get("depth"))
        compliance = evaluate_compliance(brief, positives, guardrails, generated)
        write_json(run_dir / f"commitment_provenance_report_repair_{repair_attempts}.json", commitment)
        write_json(run_dir / f"artifact_qa_report_repair_{repair_attempts}.json", artifact_qa)
        write_json(run_dir / f"compliance_report_repair_{repair_attempts}.json", compliance)
    if compliance["status"] == "BLOCK" or commitment["status"] == "BLOCK" or artifact_qa["status"] == "BLOCK":
        blocked = {"run_id": run_id, "status": "compliance_blocked", "compliance": compliance, "commitment_provenance": commitment, "artifact_qa": artifact_qa, "repair_attempts": repair_attempts, "actions": ["repair", "choose_provider"], "message": "当前输出未通过知识与承诺治理检查，尚未生成可下载文件。"}
        write_json(run_dir / "run_audit.json", {
            "run_id": run_id,
            "status": "compliance_blocked",
            "partial": True,
            "compliance_status": compliance["status"],
            "commitment_provenance_status": commitment["status"],
            "artifact_qa_status": artifact_qa["status"],
            "block_reasons": [item.get("rule_id") for item in (compliance.get("violations") or []) if item.get("severity") == "BLOCK"] + [item.get("rule_id") for item in (artifact_qa.get("findings") or []) if item.get("severity") == "BLOCK"],
            "LONGFORM_ORCHESTRATOR": "ACTIVE",
            "ONE_SHOT_FULL_DOCUMENT_GENERATION": False,
            "SECTION_LEVEL_GENERATION": True,
            "task_mode": longform_result.get("task_mode"),
            "runtime_source": (_mmf_paths.load_build_manifest().get("runtime_source") or "source"),
            "build_id": (_mmf_paths.load_build_manifest().get("build_id") or "dev-unpacked"),
            "TEST_TARGET": "production_dist" if (_mmf_paths.load_build_manifest().get("runtime_source") == "dist") else "source",
            "created_at": now_iso(),
        })
        return blocked
    write_json(run_dir / "generation_raw.json", generated)
    content_path = run_dir / "artifact_content.json"
    write_json(content_path, {"brief": brief, "artifact": generated["artifact"]})
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
    completed = subprocess.run(cmd, cwd=str(APP_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, shell=False)
    (run_dir / "artifact_build.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (run_dir / "artifact_build.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0 or not final_path.is_file():
        raise MMFError("生成失败，请查看运行日志")
    recommendation_metadata = recommendation.get("provider_metadata") if isinstance(recommendation.get("provider_metadata"), dict) else {}
    generation_metadata = generated.get("provider_metadata") if isinstance(generated.get("provider_metadata"), dict) else {}
    rec_provider = str(recommendation_metadata.get("provider_name") or recommendation.get("provider_name") or "")
    gen_provider = str(generation_metadata.get("provider_name") or rec_provider)
    switched = bool(rec_provider and gen_provider and rec_provider != gen_provider)
    run_audit = {
        "run_id": run_id,
        "project_brief": brief,
        "selected_positive_ku_ids": selected,
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
        "compliance_repair_attempts": repair_attempts,
        "LONGFORM_ORCHESTRATOR": "ACTIVE",
        "ONE_SHOT_FULL_DOCUMENT_GENERATION": False,
        "SECTION_LEVEL_GENERATION": True,
        "task_mode": longform_result.get("task_mode"),
        "requested_settings": ((longform_result.get("capability") or {}).get("requested_settings")),
        "effective_settings": ((longform_result.get("capability") or {}).get("effective_settings")),
        "longform_depth": (longform_result.get("depth") or {}).get("status"),
        "runtime_source": (_mmf_paths.load_build_manifest().get("runtime_source") or "source"),
        "build_id": (_mmf_paths.load_build_manifest().get("build_id") or "dev-unpacked"),
        "source_hash": (_mmf_paths.load_build_manifest().get("source_hash") or ""),
        "TEST_TARGET": "production_dist" if (_mmf_paths.load_build_manifest().get("runtime_source") == "dist") else "source",
        "created_at": now_iso(),
        "generation_started_at": (read_json(run_dir / "generation_status.json", "generation_status") or {}).get("started_at") if (run_dir / "generation_status.json").is_file() else None,
    }
    write_json(run_dir / "run_audit.json", run_audit)
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
    }


def repair_artifact(run_id: str) -> dict[str, Any]:
    run_dir = _checked(RUNS_DIR / run_id)
    selection = read_json(run_dir / "knowledge_selection.json", "repair_selection")
    return generate_artifact(run_id, selection.get("selected_positive_ku_ids", []), selection.get("clarification_answers", {}), auto_repair=True)


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
        artifact = folder / str(audit.get("artifact_display_name", "")) if audit.get("artifact_display_name") else folder / ("final.docx" if brief.get("medium") == "WORD" else "final.pptx")
        recommendation_ready = (folder / "knowledge_selection.json").is_file()
        generation_started = (folder / "generation_input.json").is_file()
        recommendation_started = (folder / "provider_recommendation" / "prompt.md").is_file()
        status_path = folder / "generation_status.json"
        status_record = read_json(status_path, "generation_status") if status_path.is_file() else {}
        provider_prompt = folder / "provider_generation" / "prompt.md"
        generation_age = datetime.now().timestamp() - provider_prompt.stat().st_mtime if provider_prompt.is_file() else None
        if artifact.exists():
            run_status = "generation_completed"
            status_label = "初稿已生成"
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
            "todd_final_imported": (folder / ("todd_final.docx" if brief.get("medium") == "WORD" else "todd_final.pptx")).exists(),
            "engine_label": engine_display(str(brief.get("provider_name") or "")),
            "elapsed_seconds": duration_seconds(
                status_record.get("started_at") or audit.get("generation_started_at"),
                status_record.get("finished_at") or audit.get("generation_finished_at"),
                status_record.get("elapsed_seconds") or audit.get("generation_elapsed_seconds"),
            ),
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
        "acceptance_actor": "reviewer",
        "acceptance_timestamp": "2026-08-30T13:30:50+08:00",
        "acceptance_record": "product_review.json",
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
