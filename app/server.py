from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import paths
from desktop_actions import open_run_folder, pick_folder, safe_open
from env_check import run_environment_check, user_facing_check
from first_run import current_settings, save_settings, setup_payload
from provider_view import public_status
from providers import cursor_settings
from run_layout import ensure_run_layout, finalize_run
from user_errors import classify, sanitize

paths.reload()
paths.ensure_directories()

from app_core import (  # noqa: E402
    MEDIA,
    MMFError,
    RUNS_DIR,
    SCENARIOS,
    _write_generation_phase,
    acknowledge_tender_local_fallback,
    confirm_tender_run,
    create_tender_run,
    delete_run,
    duration_seconds,
    estimate_generation_runtime,
    generate_artifact,
    list_runs,
    load_run_recommendation,
    load_run_status,
    load_tender_run,
    mark_tender_failed,
    mark_tender_processing,
    now_iso,
    parse_iso,
    process_tender_run,
    reconcile_tender_status,
    provider_manager,
    provider_status,
    recommend_knowledge,
    render_first_draft_file,
    repair_artifact,
    repair_local_artifact,
    save_todd_final,
    verify_assets,
    write_json,
)
from longform.reasoning import product_speed_catalog
from providers.capability import resolve_profile
from providers import ProviderError, ProviderUnavailableError
from providers.openai_compatible import abort_run_http, bind_run, is_cancelled, unbind_run, GenerationCancelled
from tender_intake import TenderError
from workflow_timing import finish_workflow, workflow_audit_fields
from generation_lifecycle import TERMINAL, read_status as read_generation_status, run_lock, write_progress


APP_CONFIG = json.loads((paths.CONFIG_DIR / "app.json").read_text(encoding="utf-8-sig"))
HOST = os.environ.get("MMF_HOST", APP_CONFIG.get("host", "127.0.0.1"))
PORT = int(os.environ.get("MMF_PORT", APP_CONFIG.get("port", 3050)))
if HOST not in {"127.0.0.1", "localhost"}:
    HOST = "127.0.0.1"

GENERATION_JOBS: dict[str, threading.Thread] = {}
GENERATION_JOBS_LOCK = threading.RLock()
TENDER_JOBS: dict[str, threading.Thread] = {}
TENDER_JOBS_LOCK = threading.RLock()
SERVICE_STOPPING = False


def effective_provider_config() -> dict:
    manager = provider_manager()
    provider = manager.get(manager.default_provider, require_available=False)
    cfg = provider.config
    return {key: cfg.get(key) for key in ("provider_name", "provider_type", "enabled", "base_url",
            "queue_dir", "endpoint_alias", "test_mode", "qualification_status")}


def request_service_shutdown(identity: dict) -> dict:
    """Close an idle, identified service without cancelling any business run."""
    global SERVICE_STOPPING
    roots = paths.current()
    build = paths.load_build_manifest(roots.package_root)
    if (identity.get("process_id") != os.getpid()
            or identity.get("build_id") != build.get("build_id")
            or Path(identity.get("runs_root") or "").resolve() != roots.runs_dir.resolve()):
        raise MMFError("服务身份不匹配，未关闭服务。")
    with GENERATION_JOBS_LOCK, TENDER_JOBS_LOCK:
        if any(job.is_alive() for job in [*GENERATION_JOBS.values(), *TENDER_JOBS.values()]):
            raise MMFError("任务仍在运行，请先明确停止任务再关闭服务。")
        SERVICE_STOPPING = True
    return {"status": "service_closing", "process_id": os.getpid(), "business_state_modified": False}
LOG_LOCK = threading.Lock()
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)


def _ensure_local_noproxy() -> None:
    extra = ("localhost", "127.0.0.1", "::1")
    current = []
    for key in ("NO_PROXY", "no_proxy"):
        current.extend(part.strip() for part in str(os.environ.get(key) or "").split(",") if part.strip())
    merged = []
    seen = set()
    for item in [*current, *extra]:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    value = ",".join(merged)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


def log_exception(where: str, exc: BaseException) -> None:
    roots = paths.current()
    roots.logs_dir.mkdir(parents=True, exist_ok=True)
    text = f"{now_iso()} [{where}] {type(exc).__name__}: {sanitize(str(exc))}\n{traceback.format_exc()}\n"
    with LOG_LOCK:
        with (roots.logs_dir / "mmf_desktop.log").open("a", encoding="utf-8") as handle:
            handle.write(text)


def rebind_app_core() -> None:
    roots = paths.sync_module_aliases()
    import app_core
    app_core.APP_ROOT = roots.app_root
    app_core.RUNTIME_ROOT = roots.package_root
    app_core.CONFIG_DIR = roots.config_root
    app_core.RUNS_DIR = roots.runs_dir
    app_core.RUNTIME_DIR = roots.runtime_dir
    app_core.STATIC_DIR = roots.static_dir
    app_core.ASSETS_DIR = roots.app_root / "assets"
    app_core.SNAPSHOT = app_core.ASSETS_DIR / "knowledge" / "accepted_ku_b1.2.jsonl"
    app_core.CORPUS_INDEX = app_core.ASSETS_DIR / "knowledge" / "candidate_corpus_index_v0.1.json"
    app_core.STYLE_RULES = app_core.ASSETS_DIR / "style" / "user_writing_style_rules_v0.2.json"
    app_core.GENERATION_PATCH = app_core.ASSETS_DIR / "generation" / "generation_layer_patch_v0.2.json"
    app_core.OUTPUT_PROFILE = app_core.ASSETS_DIR / "rendering" / "output_medium_profile_v0.1.json"
    app_core.ASSET_MANIFEST = app_core.ASSETS_DIR / "asset_manifest.json"
    app_core.RUNTIME_CONFIG = roots.runtime_dir / "runtime_config.json"
    app_core.COMPLIANCE_RULES = app_core.ASSETS_DIR / "compliance" / "provider_compliance_rules_v0.1.json"
    app_core.REVIEW_FILE = roots.runtime_dir / "product_review.json"
    app_core.STATE_FILE = roots.runtime_dir / "mmf_state.json"
    # 006A 抽取进度状态与 mmf_state 同类：漏这一行，数据根被重定向后它仍写旧位置
    # （只读安装目录下会直接写失败，招标上传链路整条断掉）。
    app_core.MMF006A_STATE_FILE = roots.runtime_dir / "MMF006A_state.json"
    app_core.ACCESS_AUDIT_FILE = roots.runtime_dir / "access_audit.jsonl"
    global RUNS_DIR
    RUNS_DIR = roots.runs_dir


def _providers_payload(refresh: bool = False) -> list[dict]:
    settings = current_settings()
    rows = []
    try:
        rows = provider_status(refresh=refresh)
    except Exception as exc:
        log_exception("provider_status", exc)
        return []
    return [public_status(row, settings) for row in rows]


def _enrich_run(row: dict) -> dict:
    run_id = row.get("run_id", "")
    roots = paths.current()
    run_dir = roots.runs_dir / run_id
    output_dir = roots.output_root / run_id
    word = ""
    ppt = ""
    for folder in (output_dir, run_dir / "artifact", run_dir):
        if not folder.exists():
            continue
        for item in folder.glob("*.docx"):
            word = str(item)
        for item in folder.glob("*.pptx"):
            ppt = str(item)
    row.update({
        "run_dir": str(run_dir) if run_dir.exists() else "",
        "output_dir": str(output_dir) if output_dir.exists() else "",
        "word_path": word,
        "ppt_path": ppt,
    })
    if row.get('run_status') == 'incomplete_tender' and _tender_worker_alive(run_id):
        return {**row, "tender_running": True, "status_label": "正在识别需求"}
    if row.get('provider_name')=='zcode_cn' and (row.get('ppt_completion') or {}).get('complete') is False:
        from zcode_account.policy import partial_execution_issues
        row['execution_issues']=partial_execution_issues(run_dir)
    from account_delivery import project
    with GENERATION_JOBS_LOCK:
        worker=GENERATION_JOBS.get(run_id)
        alive=bool(worker and worker.is_alive())
    row=project(row,worker_alive=alive)
    return row


def _safe_runs() -> list[dict]:
    try:
        return [_enrich_run(row) for row in list_runs()]
    except Exception as exc:
        log_exception("list_runs", exc)
        return []


def _generation_status_path(run_id: str) -> Path:
    if not run_id.replace("_", "").replace("-", "").isalnum():
        raise MMFError("找不到这次生成任务")
    run_dir = paths.current().runs_dir / run_id
    if not run_dir.is_dir():
        raise MMFError("找不到这次生成任务")
    return run_dir / "generation_status.json"


# §38-43：长任务期间给出"当前在做什么 + 初稿进度"，不再出现静止几小时的
# "正在自动修复"观感。
_PHASE_HEARTBEAT_LABELS = {
    "TECHNICAL_RETRYING": "正在重试失败的网络请求",
    "LOCAL_NORMALIZING": "正在整理文档",
    "FIRST_DRAFT": "正在整理文档",
    "DOCUMENT_QA": "正在检查完整性",
    "SELECTIVE_REPAIR": "正在补充关键问题",
    "DEPTH_SUPPLEMENTING": "正在补充关键内容",
    "FACT_COMMITMENT_CHECK": "正在检查事实与承诺",
    "GENERATING_IMAGES": "正在生成PPT场景配图",
    "ASSEMBLING": "正在生成Word文件",
    "FINAL_ARTIFACT_QA": "正在检查文件完整性",
    "AUTO_REPAIRING": "正在检查并完善方案内容",
    "RECHECKING": "正在检查并完善方案内容",
    "SECTION_QA": "正在检查并完善方案内容",
}


def _heartbeat_progress_message(checkpoint: dict, elapsed: int) -> str:
    phase = str(checkpoint.get("public_phase") or "GENERATING")
    label = _PHASE_HEARTBEAT_LABELS.get(phase, "正在生成初稿")
    clock = f"，已用时{elapsed // 60}分{elapsed % 60}秒" if elapsed > 0 else ""
    progress = checkpoint.get("progress") if isinstance(checkpoint.get("progress"), dict) else {}
    try:
        done, total = int(progress.get("done") or 0), int(progress.get("total") or 0)
    except (TypeError, ValueError):
        done = total = 0
    if total > 0 and phase in {"GENERATING", "TECHNICAL_RETRYING"}:
        return f"{label} {done}/{total}（{round(done * 100 / total)}%）{clock}。请勿重复点击。"
    return f"{label}{clock}。"


def _run_generation_job(run_id: str, selected_positive_ids: list[str], clarification_answers: dict, speed_profile: str | None = None, max_parallelism: int | None = None, trust_submitted_selection: bool = False) -> None:
    import time
    status_path = _generation_status_path(run_id)
    started_at = now_iso()
    if status_path.is_file():
        try:
            previous = json.loads(status_path.read_text(encoding="utf-8-sig"))
            if previous.get("started_at"):
                started_at = str(previous["started_at"])
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    stop = threading.Event()
    started = time.monotonic()
    with run_lock(run_id):
        if read_generation_status(status_path).get("status") in TERMINAL:
            return
        bind_run(run_id)

    def _heartbeat() -> None:
        while not stop.wait(5):
            if is_cancelled(run_id):
                return
            elapsed = int(time.monotonic() - started)
            phase = "GENERATING"
            message = f"正在生成方案，已用时{elapsed // 60}分{elapsed % 60}秒。完整方案可能需要几分钟，请勿重复点击。"
            phase_path = paths.current().runs_dir / run_id / "generation_phase.json"
            checkpoint_path = paths.current().runs_dir / run_id / "checkpoint" / "state.json"
            try:
                phase_record = (
                    json.loads(phase_path.read_text(encoding="utf-8-sig"))
                    if phase_path.is_file() else {}
                )
                checkpoint = (
                    json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
                    if checkpoint_path.is_file() else {}
                )
                phase_ts = parse_iso(str(phase_record.get("updated_at") or ""))
                checkpoint_ts = parse_iso(str(checkpoint.get("updated_at") or ""))
                # Checkpoint 由生成器按批次刷新，比粗粒度阶段文件更新；两者谁更近谁说话。
                if checkpoint.get("public_phase") and (
                    phase_ts is None or (checkpoint_ts is not None and checkpoint_ts >= phase_ts)
                ):
                    phase = str(checkpoint["public_phase"])
                    message = _heartbeat_progress_message(checkpoint, elapsed)
                elif phase_record:
                    phase = str(phase_record.get("phase") or phase)
                    message = str(phase_record.get("message") or message)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
            write_progress(status_path, {
                "run_id": run_id,
                "status": "running",
                "started_at": started_at,
                "elapsed_seconds": elapsed,
                "phase": phase,
                "recoverable_qa": phase in {"SECTION_QA", "AUTO_REPAIRING", "RECHECKING", "DOCUMENT_QA", "SELECTIVE_REPAIR", "DEPTH_SUPPLEMENTING"},
                "message": message,
            }, write_json)

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()
    def _finish_status(status: str, extra: dict | None = None) -> dict:
        with run_lock(run_id):
            previous = read_generation_status(status_path)
            if previous.get("status") in TERMINAL:
                return previous
            elapsed = max(0, int(time.monotonic() - started))
            finished_at = now_iso()
            payload = {
                "run_id": run_id,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_seconds": elapsed,
            }
            if extra:
                payload.update(extra)
            write_json(status_path, payload)
            run_dir = paths.current().runs_dir / run_id
            finish_workflow(run_dir, "completed" if status == "completed" else status)
            audit_path = run_dir / "run_audit.json"
            try:
                audit = json.loads(audit_path.read_text(encoding="utf-8-sig")) if audit_path.is_file() else {
                    "run_id": run_id,
                    "status": "generation_completed" if status == "completed" else f"generation_{status}",
                    "partial": status != "completed",
                    "created_at": finished_at,
                }
                audit["generation_started_at"] = started_at
                audit["generation_finished_at"] = finished_at
                audit["generation_elapsed_seconds"] = elapsed
                audit.update(workflow_audit_fields(run_dir))
                write_json(audit_path, audit)
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            return payload

    def _cancelled_payload() -> dict:
        draft = _salvage_first_draft_on_stop(run_id)
        payload = {"error": "已停止生成", "first_draft": draft}
        if draft.get("path"):
            payload["message"] = "已停止生成。已完成内容已保存为方案初稿，可以下载，也可以稍后继续生成。"
        else:
            payload["message"] = "已停止生成。进度已保存为检查点，可以继续生成。"
        return payload

    try:
        result = generate_artifact(run_id, selected_positive_ids, clarification_answers, speed_profile=speed_profile, max_parallelism=max_parallelism, trust_submitted_selection=trust_submitted_selection)
        if is_cancelled(run_id):
            _finish_status("cancelled", _cancelled_payload())
            return
        from account_delivery import account_run,publish_partial,project
        account=account_run(run_id,result)
        if account:
            stop.set()
            heartbeat.join(timeout=6)
        with run_lock(run_id):
            if read_generation_status(status_path).get("status") in TERMINAL or is_cancelled(run_id):
                return
            # Account terminal publication must not race the heartbeat. Preserve
            # the existing API lifecycle and original QA/finalization authority.
            result = finalize_run(run_id, result)
            if account:
                publish_partial(run_id)
                result=project(result,worker_alive=False)
                result.pop('result',None)
            result["started_at"] = started_at
            result["elapsed_seconds"] = max(0, int(time.monotonic() - started))
            status = "completed" if result.get("status") == "generation_completed" else result.get("status", "completed")
            if account and status not in TERMINAL:status='failed'
            _finish_status(status, {"result": result})
    except (GenerationCancelled, ProviderUnavailableError) as exc:
        if is_cancelled(run_id) or "已停止生成" in str(exc):
            _finish_status("cancelled", _cancelled_payload())
        else:
            log_exception(f"generate:{run_id}", exc)
            mapped = classify(exc)
            _finish_status("failed", {"error": mapped["error"], "error_code": mapped["error_code"]})
    except Exception as exc:
        if is_cancelled(run_id) or "已停止生成" in str(exc):
            _finish_status("cancelled", _cancelled_payload())
        else:
            log_exception(f"generate:{run_id}", exc)
            mapped = classify(exc)
            _finish_status("failed", {"error": mapped["error"], "error_code": mapped["error_code"]})
    finally:
        stop.set()
        unbind_run(run_id)
        with GENERATION_JOBS_LOCK:
            GENERATION_JOBS.pop(run_id, None)


def _reassemble_from_committed_fragments(run_dir: Path) -> tuple[dict, dict]:
    """停止早于 FIRST_DRAFT 时，按已落盘片段就地装配一份初稿内容。"""
    plan_path = run_dir / "01_word_document_plan.json"
    if not plan_path.is_file():
        return {}, {}
    try:
        from longform.orchestrator import assemble_word_artifact, collect_fragments

        brief_path = run_dir / "brief.json"
        brief = json.loads(brief_path.read_text(encoding="utf-8-sig")) if brief_path.is_file() else {}
        word_plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        fragments = collect_fragments(run_dir)
        if not fragments:
            return brief, {}
        return brief, assemble_word_artifact(brief, word_plan, fragments)
    except Exception:  # 存稿是尽力而为，不得让停止按钮本身失败
        return {}, {}


def _publish_stop_with_draft(run_dir: Path) -> None:
    """停止时已经存下可下载初稿，公开阶段就不能再说"未能形成可打开的文件"。

    交付判定可能早于停止就把 TERMINAL_BLOCKED 写进 generation_phase.json；初稿
    落盘后不改写的话，界面会在文件其实可下载时继续提示硬失败，等于藏起了文件。
    """
    try:
        _write_generation_phase(
            run_dir,
            "FIRST_DRAFT",
            "已停止生成。已完成内容可作为方案初稿下载，未生成部分保留在检查点中，可稍后继续生成。",
            recoverable=True,
        )
    except Exception:  # 停止路径不得因为改写阶段而抛错
        pass


def _salvage_first_draft_on_stop(run_id: str) -> dict:
    """停止生成时把已到达的内容渲染成初稿文件，不丢弃任何已完成部分。

    Checkpoint 由生成链自己落盘；这里只负责让"停止"之后仍然有一个可下载的文件。
    """
    run_dir = RUNS_DIR / run_id
    content_path = run_dir / "longform" / "word" / "first_draft_content.json"
    manifest_path = run_dir / "first_draft.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            target = Path(str(manifest.get("path") or ""))
            if manifest.get("status") == "DOWNLOADABLE" and target.is_file():
                _publish_stop_with_draft(run_dir)
                return {"status": "DOWNLOADABLE", "path": str(target)}
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    if not content_path.is_file():
        # 停止发生在初稿装配之前：用已提交片段就地装配，已到达的内容不得作废。
        brief, artifact = _reassemble_from_committed_fragments(run_dir)
        if not (artifact or {}).get("sections"):
            return {"status": "NO_DRAFT_CONTENT"}
    else:
        content = json.loads(content_path.read_text(encoding="utf-8-sig"))
        brief, artifact = content.get("brief") or {}, content.get("artifact") or {}
    try:
        result = render_first_draft_file(run_dir, brief, artifact)
    except Exception as exc:  # 停止路径不得因为存稿失败而抛错
        return {"status": "RENDER_FAILED", "error": str(exc)[:200]}
    try:
        write_json(manifest_path, {
            "schema_version": "first-draft-artifact-v0.1",
            "artifact_kind": "FIRST_DRAFT",
            "saved_by": "stop_generation",
            **result,
            "recorded_at": now_iso(),
        })
    except (OSError, TypeError):
        pass
    if str(result.get("status") or "") == "DOWNLOADABLE":
        _publish_stop_with_draft(run_dir)
    return result


def cancel_generation(run_id: str) -> dict:
    status_path = _generation_status_path(run_id)
    with run_lock(run_id):
        previous = read_generation_status(status_path)
        status = previous.get("status")
        if status in TERMINAL:
            result = previous.get("result") or {}
            return {"ok": True, "run_id": run_id, "status": status,
                    "delivery_status": result.get("delivery_status") or previous.get("delivery_status"),
                    "elapsed_seconds": previous.get("elapsed_seconds"),
                    "finished_at": previous.get("finished_at"), "idempotent": True,
                    "message": "任务已经结束，保留原状态和文件。"}
        # Closing an idle service is not a generation cancellation.
        if status != "running" and not _tender_worker_alive(run_id):
            return {"ok": True, "run_id": run_id, "status": status or "not_running",
                    "idempotent": True, "message": "当前没有正在执行的任务。"}
        started_at = previous.get("started_at")
        abort_run_http(run_id)
        tender_status = paths.current().runs_dir / run_id / "tender" / "status.json"
        if _tender_worker_alive(run_id) and tender_status.is_file():
            tender = json.loads(tender_status.read_text(encoding="utf-8-sig"))
            tender.update(error="已停止识别", error_code="CANCELLED",
                          pack_status="cancelled", updated_at=now_iso())
            write_json(tender_status, tender)
        finished_at = now_iso()
        elapsed = duration_seconds(started_at, finished_at, previous.get("elapsed_seconds"))
        write_json(status_path, {
            "run_id": run_id, "status": "cancelled", "started_at": started_at,
            "finished_at": finished_at, "elapsed_seconds": elapsed, "error": "已停止生成",
        })
        finish_workflow(paths.current().runs_dir / run_id, "cancelled")
        return {"ok": True, "run_id": run_id, "status": "cancelled",
                "delivery_status": "USER_CANCELLED", "elapsed_seconds": elapsed,
                "message": "已停止生成。已生成的文件仍保留。"}


def _start_generation_job(run_id: str, selected_positive_ids: list[str], clarification_answers: dict, speed_profile: str | None = None, max_parallelism: int | None = None, trust_submitted_selection: bool = False, *, resume_cursor_word: bool = False) -> dict:
    current = _enrich_run(load_run_status(run_id))
    from planning.ppt_delivery import completion_state
    ppt_completion = completion_state(paths.current().runs_dir / run_id)
    if current.get("run_status") == "generation_completed" and (ppt_completion is None or ppt_completion["complete"]):
        return finalize_run(run_id, {"run_id": run_id, "status": "generation_completed", "download_url": current.get("download_url", "")})
    with run_lock(run_id), GENERATION_JOBS_LOCK:
        if SERVICE_STOPPING:
            raise MMFError("服务正在关闭，未启动新任务。")
        existing = GENERATION_JOBS.get(run_id)
        if existing and existing.is_alive():
            return {"run_id": run_id, "status": "generation_in_progress", "started_at": current.get("started_at"), "provider_name": current.get("provider_name"), "message": "当前任务仍在生成。"}
        if resume_cursor_word:
            from account_delivery import read
            from cursor_word_recovery import inspect, settle
            run = paths.current().runs_dir / run_id
            recovery = inspect(run)
            if not recovery or not recovery['can_resume']:
                raise MMFError("当前无法接续。请先核对原写稿请求结果。")
            saved = read(run / 'generation_input.json')
            brief = saved.get('current_project_brief') or {}
            positives = saved.get('todd_confirmed_positive_kus')
            answers = saved.get('clarification_answers')
            if (brief.get('provider_name') != current.get('provider_name')
                    or not isinstance(positives, list) or not isinstance(answers, dict)):
                raise MMFError("原写稿输入无法核验，未启动接续。")
            selected_positive_ids = [row['ku_id'] for row in positives]
            clarification_answers = answers
            speed_profile, max_parallelism = brief.get('speed_profile'), brief.get('max_parallelism')
            trust_submitted_selection = True
            settle(recovery)
        started_at = now_iso()
        write_json(_generation_status_path(run_id), {"run_id": run_id, "status": "running", "started_at": started_at})
        worker = threading.Thread(target=_run_generation_job, args=(run_id, list(selected_positive_ids), dict(clarification_answers), speed_profile, max_parallelism, bool(trust_submitted_selection)), daemon=True)
        GENERATION_JOBS[run_id] = worker
        worker.start()
    return {"run_id": run_id, "status": "generation_started", "started_at": started_at, "provider_name": current.get("provider_name"), "message": "任务已转入后台生成。"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(paths.current().static_dir), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except CLIENT_GONE:
            return

    def _error(self, exc: BaseException, status: int = 400) -> None:
        if isinstance(exc, CLIENT_GONE):
            return
        log_exception(self.path, exc)
        mapped = classify(exc)
        self._json(mapped, status)

    def _serve_index(self) -> None:
        target = paths.current().static_dir / "index.html"
        data = target.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except CLIENT_GONE:
            return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _serve_download(self, run_id: str, name: str) -> None:
        target = (paths.current().runs_dir / run_id / unquote(name)).resolve()
        try:
            target.relative_to(paths.current().runs_dir.resolve())
        except ValueError as exc:
            raise MMFError("文件路径无效") from exc
        if not target.is_file():
            output = (paths.current().output_root / run_id / target.name).resolve()
            fallbacks = [
                output,
                paths.current().runs_dir / run_id / "last_valid_artifact" / target.name,
                paths.current().runs_dir / run_id / "_blocked_artifact" / target.name,
                paths.current().runs_dir / run_id / "artifact" / target.name,
            ]
            target = next((item.resolve() for item in fallbacks if item.is_file()), target)
            if not target.is_file():
                raise MMFError("文件不存在")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        encoded = quote(target.name)
        self.send_header("Content-Disposition", f"attachment; filename=\"download{target.suffix}\"; filename*=UTF-8''{encoded}")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/zcode-account/status":
                from zcode_account.connection import connection, enabled
                self._json(connection().snapshot() if enabled() else {"enabled":False})
                return
            if path == '/api/account-agents':
                from account_tasks import list_tasks,workflow_view
                from account_progress import snapshot
                from urllib.parse import parse_qs
                run_id=str((parse_qs(parsed.query).get('run_id') or [''])[0])
                tasks=list_tasks(run_id);generation=None;recognition=None;alive=False
                if run_id and (paths.current().runs_dir/run_id/'generation_status.json').exists():
                    generation=_enrich_run(load_run_status(run_id))
                    with GENERATION_JOBS_LOCK:
                        worker=GENERATION_JOBS.get(run_id)
                        alive=bool(worker and worker.is_alive())
                elif run_id and (paths.current().runs_dir/run_id/'tender/status.json').exists():
                    recognition=load_tender_run(run_id).get('status') or {};alive=_tender_worker_alive(run_id)
                self._json({'tasks':tasks,'workflow':workflow_view(run_id) if run_id else None,
                            'progress':snapshot(run_id,tasks,generation,recognition,alive) if run_id else None})
                return
            if path == "/api/qoder-account/status":
                from qoder_account.connection import connection, enabled
                from urllib.parse import parse_qs
                if not enabled():
                    self._json({"enabled": False})
                    return
                params = parse_qs(parsed.query)
                after = max(0, int((params.get("after") or [0])[0]))
                run_id = str((params.get("run_id") or [""])[0])
                self._json(connection().snapshot(after, run_id))
                return
            if path.startswith("/api/runs/") and "/project-assets/" in path:
                import re
                match = re.fullmatch(r"/api/runs/([A-Za-z0-9_-]{1,90})/project-assets/([A-Za-z0-9_-]+)", path)
                if not match:
                    raise MMFError("图件地址无效。")
                from project_assets import verified_image
                target = verified_image(paths.current().runs_dir / match[1], match[2])
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg" if target.suffix.lower() in {".jpg", ".jpeg"} else "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/health":
                roots = paths.current()
                build = paths.load_build_manifest(roots.package_root)
                self._json({
                    "status": "ok",
                    "app": paths.APP_NAME,
                    "app_version": paths.APP_VERSION,
                    "app_status": paths.APP_STATUS,
                    "runtime_root": str(roots.package_root),
                    "runs_root": str(roots.runs_dir),
                    "output_root": str(roots.output_root),
                    "host": HOST,
                    "listen": "127.0.0.1",
                    "first_run_completed": bool(current_settings().get("first_run_completed")),
                    "runtime_source": build.get("runtime_source") or "source",
                    "build_id": build.get("build_id") or "dev-unpacked",
                    "source_hash": build.get("source_hash") or "",
                    "process_id": os.getpid(),
                    "longform_orchestrator_version": build.get("longform_orchestrator_version") or paths.LONGFORM_ORCHESTRATOR_VERSION,
                })
                return
            if path == "/api/env-check":
                report = run_environment_check(HOST, PORT, assume_self_listening=True)
                self._json({"technical": report, "user": user_facing_check(report)})
                return
            if path == "/api/setup":
                self._json(setup_payload())
                return
            if path == "/api/config":
                settings = current_settings()
                self._json({
                    "app_name": paths.APP_NAME,
                    "app_version": paths.APP_VERSION,
                    "app_status": paths.APP_STATUS,
                    "scenarios": SCENARIOS,
                    "media": MEDIA,
                    "providers": _providers_payload(),
                    "cursor_account": cursor_settings.status(settings.get("default_provider")),
                    "default_provider": settings.get("default_provider") or "qwen_modelstudio",
                    "effective_default_provider": provider_manager().default_provider,
                    "effective_provider": effective_provider_config(),
                    "default_medium": settings.get("default_medium") or "WORD",
                    "assets": verify_assets(),
                    "runs": _safe_runs(),
                    "first_run_completed": bool(settings.get("first_run_completed")),
                    "paths": {
                        "output_dir": settings.get("output_root"),
                        "runs_dir": settings.get("runs_dir"),
                        "logs_dir": settings.get("logs_dir"),
                    },
                    "image_provider": {
                        "enabled": bool(settings.get("enable_image_provider", True)),
                        "provider_name": settings.get("image_provider_name") or "qwen_image",
                        "status": settings.get("image_provider_status") or "not_configured",
                        "message": "可在AI引擎设置中配置千问万相或Grok Imagine。未配置时不影响文字方案生成。",
                    },
                    "speed_profiles": product_speed_catalog(),
                    "default_speed_profile": "balanced",
                })
                return
            if path == "/api/cursor-account/status":
                self._json(cursor_settings.status(current_settings().get("default_provider")))
                return
            if path == "/api/providers/health":
                self._json({"providers": _providers_payload(refresh=False),
                            "cursor_account": cursor_settings.status(current_settings().get("default_provider"))})
                return
            if path == "/api/providers":
                manager = provider_manager()
                self._json({
                    "providers": _providers_payload(),
                    "credential_capability": manager.credential_store.capability(),
                    "cursor_account": cursor_settings.status(current_settings().get("default_provider")),
                })
                return
            if path.startswith("/api/providers/") and path.endswith("/config"):
                name = path.split("/")[3]
                self._json(provider_manager().public_config(name))
                return
            if path == "/api/generation/estimate":
                query = urlparse(self.path).query
                params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
                provider = unquote(params.get("provider") or params.get("provider_name") or "")
                mode = unquote(params.get("mode") or params.get("speed_profile") or "balanced")
                stage = unquote(params.get("stage") or "draft")
                cfg = {}
                try:
                    if provider:
                        cfg = provider_manager().public_config(provider)
                except Exception:
                    cfg = {}
                model = unquote(params.get("model") or "") or str(cfg.get("model") or "")
                section_count = None
                raw_count = unquote(params.get("section_count") or params.get("logical_section_count") or "")
                if raw_count:
                    try:
                        section_count = int(raw_count)
                    except (TypeError, ValueError):
                        section_count = None
                run_id = unquote(params.get("run_id") or "")
                if section_count is None and run_id:
                    plan_path = paths.current().runs_dir / run_id / "01_word_document_plan.json"
                    if plan_path.is_file():
                        try:
                            outline = json.loads(plan_path.read_text(encoding="utf-8-sig")).get("outline") or []
                            section_count = sum(len(chapter.get("sections") or []) for chapter in outline)
                        except Exception:
                            section_count = None
                eta = estimate_generation_runtime(provider, mode, model, stage, section_count=section_count)
                cap = resolve_profile(provider or "mock", cfg if isinstance(cfg, dict) else {}, speed_profile=mode, stage=stage)
                effective = cap.get("effective_settings") or {}
                self._json({
                    "eta": eta,
                    "speed_profiles": product_speed_catalog(),
                    "effective_reasoning_label": effective.get("ui_reasoning_label"),
                    "ui_thinking_enabled": bool(effective.get("ui_thinking_enabled")),
                    "model": effective.get("model") or model,
                    "recommended_parallelism": effective.get("recommended_parallelism"),
                    "soft_latency_budget": effective.get("soft_latency_budget"),
                    "hard_timeout": effective.get("hard_timeout"),
                })
                return
            if path == "/api/runs":
                self._json({"runs": _safe_runs()})
                return
            if path.startswith("/api/runs/") and path.endswith("/recommendation"):
                run_id = path.split("/")[3]
                self._json(load_run_recommendation(run_id))
                return
            if path.startswith("/api/runs/") and path.endswith("/status"):
                run_id = path.split("/")[3]
                row = _enrich_run(load_run_status(run_id))
                status_path = paths.current().runs_dir / run_id / "generation_status.json"
                if status_path.is_file():
                    record = json.loads(status_path.read_text(encoding="utf-8-sig"))
                    from account_delivery import account_run
                    if isinstance(record.get("result"), dict) and not account_run(run_id,row):
                        row["result"] = finalize_run(run_id, record["result"])
                    if record.get("error"):
                        row["error"] = record["error"]
                self._json(row)
                return
            if path.startswith("/api/tender/runs/"):
                run_id = path.split("/")[4]
                payload = load_tender_run(run_id)
                alive = _tender_worker_alive(run_id)
                payload["status"] = reconcile_tender_status(run_id, payload.get("status") or {}, worker_alive=alive)
                payload["worker_alive"] = alive
                if not alive and (payload.get('status') or {}).get('provider_name') in {'qoder_account','zcode_cn'}:
                    from account_tasks import recognition_handling
                    payload['status']['account_handling']=recognition_handling(run_id,payload['status'])
                if alive and (payload.get('status') or {}).get('provider_name')=='zcode_cn':
                    from zcode_account.connection import connection
                    snapshot=connection().snapshot()
                    event=next((e for e in reversed(snapshot['events']) if e.get('run_id')==run_id),{})
                    payload['provider_activity']={'phase':event.get('phase'),'returned':(snapshot.get('run_progress',{}).get(run_id) or {}).get('returned',0)}
                self._json(payload)
                return
            if path.startswith("/files/"):
                parts = path.split("/")
                self._serve_download(parts[2], "/".join(parts[3:]))
                return
            if path in {"/", "/index.html"}:
                return self._serve_index()
            if path.startswith("/") and not path.startswith("/api/"):
                return SimpleHTTPRequestHandler.do_GET(self)
            self._json({"error": "页面不存在", "error_code": "not_found"}, 404)
        except CLIENT_GONE:
            return
        except (ProviderUnavailableError, ProviderError, MMFError, TenderError, ValueError) as exc:
            self._error(exc)
        except Exception as exc:
            self._error(exc, 500)

    def do_POST(self) -> None:
        try:
            if self.path in ('/api/account-agents/answer','/api/account-agents/cancel','/api/account-agents/continue'):
                from account_tasks import answer,cancel,continue_workflow
                origin=str(self.headers.get('Origin') or '')
                if (origin and origin not in {f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'}) or self.headers.get_content_type()!='application/json':
                    self._json({'error':'请从本机产品页面操作账号任务。'},403);return
                data=self._read_json()
                key=str(data.get('key') or '')
                if self.path.endswith('/answer'):result=answer(key,str(data.get('nonce') or ''),data.get('answer'),on_recovered=_resume_saved_account_workflow)
                elif self.path.endswith('/continue'):result=continue_workflow(key,_resume_saved_account_workflow)
                else:result=cancel(key)
                self._json(result);return
            if self.path in {"/api/cursor-account/connect", "/api/cursor-account/check",
                             "/api/cursor-account/disconnect", "/api/cursor-account/model"}:
                origin = str(self.headers.get("Origin") or "")
                if (origin and origin not in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}) or self.headers.get_content_type() != "application/json":
                    self._json({"error": "请从本机产品页面操作 Cursor 连接。"}, 403)
                    return
                action = self.path.rsplit("/", 1)[-1]
                data = self._read_json()
                default = current_settings().get("default_provider")
                if action == "disconnect":
                    with GENERATION_JOBS_LOCK, TENDER_JOBS_LOCK:
                        if any(job.is_alive() for job in [*GENERATION_JOBS.values(), *TENDER_JOBS.values()]):
                            self._json({"error": "有任务正在运行，请在任务结束后断开连接。"}, 409)
                            return
                        result = cursor_settings.update(action, data, default)
                else:
                    result = cursor_settings.update(action, data, default)
                self._json(result)
                return
            if self.path.startswith("/api/zcode-account/"):
                from zcode_account.connection import connection, enabled
                origin = str(self.headers.get("Origin") or "")
                if (origin and origin not in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}) or self.headers.get_content_type() != "application/json":
                    self._json({"error":"请从本机产品页面操作ZCode连接"},403)
                    return
                if not enabled():
                    self._json({"error":"ZCode国内账户连接尚未启用"},404)
                    return
                action=self.path.rsplit("/",1)[-1];data=self._read_json();ctl=connection()
                if action == "connect":result=ctl.start_connect(login=True)
                elif action == "check":result=ctl.start_connect(login=False)
                elif action == "model":
                    result=ctl.select_model(str(data.get("model") or ""));provider_manager().reload()
                elif action == "preferences":result=ctl.set_model_preferences(data.get('glm53_enabled'))
                elif action == "disconnect":result=ctl.disconnect()
                else:
                    self._json({"error":"未找到该操作"},404)
                    return
                self._json(result)
                return
            if self.path.startswith("/api/qoder-account/"):
                from qoder_account.connection import connection, enabled
                origin = str(self.headers.get("Origin") or "")
                local_origins = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
                if (origin and origin not in local_origins) or self.headers.get_content_type() != "application/json":
                    self._json({"error": "请从本机产品页面操作Qoder连接"}, 403)
                    return
                if not enabled():
                    self._json({"error": "Qoder账户连接尚未启用"}, 404)
                    return
                action = self.path.rsplit("/", 1)[-1]
                data = self._read_json()
                ctl = connection()
                if action == "connect":
                    result = ctl.start_connect(login=True)
                elif action == "check":
                    result = ctl.start_connect(login=False)
                elif action == "model":
                    result = ctl.select_model(str(data.get("model") or ""))
                    provider_manager().reload()
                elif action == "disconnect":
                    result = ctl.disconnect()
                else:
                    self._json({"error": "未找到该操作"}, 404)
                    return
                self._json(result)
                return
            if self.path.startswith("/api/runs/") and self.path.endswith("/project-assets/labels"):
                import re
                from project_assets import relabel_project_pdf
                run_id = self.path.split("/")[3]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", run_id):
                    raise MMFError("实例标识无效。")
                result = relabel_project_pdf(paths.current().runs_dir / run_id, self._read_json().get("annotations") or [])
                self._json(result)
                return
            if self.path.startswith("/api/runs/") and self.path.endswith("/project-assets"):
                return self._upload_project_assets()
            if self.path == "/api/service/shutdown":
                result = request_service_shutdown(self._read_json())
                self._json(result)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == "/api/setup":
                payload = self._read_json()
                saved = save_settings(payload)
                rebind_app_core()
                self._json({"saved": True, "settings": saved, "setup": setup_payload()})
                return
            if self.path == "/api/tender/upload":
                return self._upload_tender()
            if self.path == "/api/tender/process":
                data = self._read_json()
                if "recognition_choice" in data:
                    self._json(_start_tender_job(data["run_id"], data.get("provider_name", ""),
                                                 decision=data["recognition_choice"]))
                else:
                    self._json(_start_tender_job(data["run_id"], data.get("provider_name", "")))
                return
            if self.path == "/api/tender/accept-local-fallback":
                data = self._read_json()
                with TENDER_JOBS_LOCK:
                    if _tender_worker_alive(data["run_id"]):
                        raise MMFError("当前识别仍在运行，请等状态更新后再选择。")
                    self._json(acknowledge_tender_local_fallback(data["run_id"]))
                return
            if self.path == "/api/tender/confirm":
                data = self._read_json()
                self._json(confirm_tender_run(data["run_id"], data.get("decisions", {}), data.get("brief_options", {})))
                return
            if self.path == "/api/recommend":
                data = self._read_json()
                result = recommend_knowledge(data)
                ensure_run_layout(paths.current().runs_dir / result["run_id"])
                self._json(result)
                return
            if self.path == "/api/generate":
                data = self._read_json()
                self._json(_start_generation_job(
                    data["run_id"],
                    data.get("selected_positive_ids", []),
                    data.get("clarification_answers", {}),
                    data.get("speed_profile"),
                    data.get("max_parallelism"),
                    bool(data.get("knowledge_optional") or data.get("trust_submitted_selection")),
                    **({"resume_cursor_word": True} if data.get("resume_cursor_word") is True else {}),
                ), 202)
                return
            if self.path == "/api/repair":
                data = self._read_json()
                result = finalize_run(data["run_id"], repair_artifact(data["run_id"]))
                write_json(_generation_status_path(data["run_id"]), {
                    "run_id": data["run_id"],
                    "status": "completed" if result.get("status") == "generation_completed" else result.get("status", "completed"),
                    "finished_at": now_iso(),
                    "result": result,
                })
                self._json(result)
                return
            if self.path == "/api/repair-local":
                data = self._read_json()
                result = finalize_run(data["run_id"], repair_local_artifact(data["run_id"]))
                write_json(_generation_status_path(data["run_id"]), {
                    "run_id": data["run_id"],
                    "status": "completed",
                    "finished_at": now_iso(),
                    "result": result,
                })
                self._json(result)
                return
            if self.path.startswith("/api/runs/") and self.path.endswith("/delete"):
                run_id = unquote(self.path.split("/")[3])
                data = self._read_json()
                self._json(_delete_history_run(run_id, delete_files=bool(data.get("delete_files"))))
                return
            if self.path.startswith("/api/runs/") and self.path.endswith("/recommendation/retry"):
                from app_core import retry_knowledge_recommendation
                run_id = self.path.split("/")[3]
                self._json(retry_knowledge_recommendation(run_id))
                return
            if self.path.startswith("/api/runs/") and self.path.endswith("/cancel"):
                run_id = unquote(self.path.split("/")[3])
                self._json(cancel_generation(run_id))
                return
            if self.path.startswith("/api/providers/") and self.path.endswith("/config"):
                name = self.path.split("/")[3]
                data = self._read_json()
                saved = provider_manager().save_config(name, data.get("config", {}), data.get("api_key", ""))
                health = saved.get("health") if isinstance(saved.get("health"), dict) else {}
                saved["user"] = public_status(
                    {"provider_name": name, **health, "connection_status": health.get("status")},
                    current_settings(),
                )
                if "credential" in saved and isinstance(saved["credential"], dict):
                    saved["credential"].pop("secret", None)
                    saved["credential"]["api_key"] = ""
                self._json(saved)
                return
            if self.path.startswith("/api/providers/") and self.path.endswith("/test"):
                name = self.path.split("/")[3]
                manager = provider_manager()
                provider = manager.providers.get(name)
                if provider is not None:
                    provider.config["live_health"] = True
                try:
                    health = manager.health(name, refresh=True)
                finally:
                    if provider is not None:
                        provider.config["live_health"] = False
                mapped = public_status({"provider_name": name, **health, "connection_status": health.get("status")}, current_settings())
                self._json({"health": health, "user": mapped})
                return
            if self.path.startswith("/api/upload/"):
                run_id = self.path.split("/")[3]
                return self._upload_final(run_id)
            if self.path == "/api/open-file":
                data = self._read_json()
                self._json(safe_open(data.get("path", ""), folder=False))
                return
            if self.path == "/api/open-folder":
                data = self._read_json()
                try:
                    self._json(open_run_folder(data.get("run_id", ""), data.get("folder_kind", "output")))
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc), "error_code": "open_folder_failed"}, 400)
                return
            if self.path == "/api/pick-folder":
                data = self._read_json()
                self._json(pick_folder(str(data.get("title") or "选择文件夹"), str(data.get("start_path") or "")))
                return
            if self.path == "/api/images/generate":
                data = self._read_json()
                settings = current_settings()
                if not settings.get("enable_image_provider", True):
                    raise MMFError("尚未启用AI图片服务")
                from first_run import resolve_image_provider
                requested = data.get("provider_name")
                if requested:
                    settings = {**settings, "image_provider_name": str(requested)}
                name = resolve_image_provider(settings, str(settings.get("default_provider") or ""))
                from providers.account_image import ACCOUNT_IMAGE_NAMES
                if name in ACCOUNT_IMAGE_NAMES:
                    origin = str(self.headers.get("Origin") or "")
                    if (origin and origin not in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}) or self.headers.get_content_type() != "application/json":
                        self._json({"error": "请从本机产品请求账户生图。"}, 403)
                        return
                provider = provider_manager().get(name, require_available=False)
                if not hasattr(provider, "generate_image"):
                    raise MMFError("当前引擎不是图片服务")
                out_dir = paths.current().output_root / "images"
                result = provider.generate_image(str(data.get("prompt") or ""), out_dir)
                self._json(result)
                return
            self._json({"error": "接口不存在", "error_code": "not_found"}, 404)
        except CLIENT_GONE:
            return
        except (ProviderUnavailableError, ProviderError, MMFError, TenderError, ValueError) as exc:
            self._error(exc)
        except Exception as exc:
            self._error(exc, 500)

    def do_DELETE(self) -> None:
        try:
            if self.path.startswith("/api/providers/") and self.path.endswith("/credential"):
                name = self.path.split("/")[3]
                self._json(provider_manager().delete_credential(name))
                return
            self._json({"error": "接口不存在", "error_code": "not_found"}, 404)
        except Exception as exc:
            self._error(exc)

    def _upload_project_assets(self) -> None:
        import re
        from email.parser import BytesParser
        from email.policy import default
        from project_assets import save_project_pdf
        run_id = self.path.split("/")[3]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", run_id):
            raise MMFError("实例标识无效。")
        run_dir = paths.current().runs_dir / run_id
        if not run_dir.is_dir() or not (run_dir / "brief.json").is_file():
            raise MMFError("请先创建方案实例，再上传项目图件。")
        brief = json.loads((run_dir / "brief.json").read_text(encoding="utf-8"))
        if str(brief.get("medium")).upper() != "PPT":
            raise MMFError("本接口用于PPT项目图件。")
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not 0 < length <= 41 * 1024 * 1024:
            raise MMFError("项目PDF过大或为空。")
        header = ("Content-Type: " + self.headers.get("Content-Type", "") + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
        message = BytesParser(policy=default).parsebytes(header + self.rfile.read(length))
        files=[]; annotations=[]
        for part in message.iter_parts():
            raw=part.get_payload(decode=True) or b""
            if part.get_filename():
                files.append((Path(part.get_filename()).name, raw))
            elif part.get_param("name", header="content-disposition") == "annotations":
                annotations=json.loads(raw.decode("utf-8"))
        if len(files)!=1:
            raise MMFError("每个实例请上传一份项目简介PDF。")
        result=save_project_pdf(run_dir,files[0][0],files[0][1],annotations)
        self._json({"run_id":run_id,**result})


    def _upload_tender(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0") or 0)
        max_body = 96 * 1024 * 1024
        if length <= 0 or length > max_body:
            raise MMFError("上传文件过大或无效。每个文件不超过40MB，一次最多3个文件、合计不超过80MB。")
        raw = self.rfile.read(length)
        from email.parser import BytesParser
        from email.policy import default
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        message = BytesParser(policy=default).parsebytes(header + raw)
        files: list[tuple[str, bytes]] = []
        provider_name = ""
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files.append((filename, payload))
            elif name == "provider_name":
                provider_name = payload.decode("utf-8", errors="replace")
        result = create_tender_run(files, provider_name)
        ensure_run_layout(paths.current().runs_dir / result["run_id"])
        self._json(result)

    def _upload_final(self, run_id: str) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length)
        from email.parser import BytesParser
        from email.policy import default
        content_type = self.headers.get("Content-Type", "")
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        message = BytesParser(policy=default).parsebytes(header + raw)
        filename = "upload.bin"
        payload = b""
        for part in message.iter_parts():
            if part.get_filename():
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
        path = save_todd_final(run_id, filename, payload)
        self._json({"saved": True, "path": str(path)})


def _resume_saved_account_workflow(row,request):
    """Only after a validated resumed answer; never replay an unknown dispatch."""
    run_id=row['run_id'];provider=row['provider']
    if provider not in {'qoder_account','zcode_cn'}:return
    from account_tasks import run_visible
    if not run_visible(run_id):return
    if request.get('generation_mode')=='tender_requirement_recognition':
        _start_tender_job(run_id,provider);return
    run_dir=paths.current().runs_dir/run_id
    brief=json.loads((run_dir/'brief.json').read_text(encoding='utf-8-sig'))
    if brief.get('provider_name')!=provider:raise MMFError('任务引擎与原记录不同，未继续生成。')
    if str(request.get('task_id','')).endswith('-recommend'):
        from app_core import _recommend_knowledge_impl
        _recommend_knowledge_impl(brief,run_id,run_dir);return
    if request.get('generation_mode') not in {'ppt_slide','longform_batch','longform_section'}:return
    saved=json.loads((run_dir/'generation_input.json').read_text(encoding='utf-8-sig'))
    selection=json.loads((run_dir/'knowledge_selection.json').read_text(encoding='utf-8-sig'))
    ids=[x['ku_id'] for x in saved['todd_confirmed_positive_kus']] if 'todd_confirmed_positive_kus' in saved else saved.get('selected_positive_ids',selection.get('selected_positive_ku_ids',[]))
    _start_generation_job(run_id,list(ids),dict(saved.get('clarification_answers') or {}),speed_profile=brief.get('speed_profile'),max_parallelism=brief.get('max_parallelism'),trust_submitted_selection=True)

def _tender_worker_alive(run_id: str) -> bool:
    with TENDER_JOBS_LOCK:
        worker = TENDER_JOBS.get(run_id)
        return bool(worker and worker.is_alive())


def _delete_history_run(run_id: str, *, delete_files: bool) -> dict:
    from app_core import _validated_run_id

    run_id = _validated_run_id(run_id)
    with TENDER_JOBS_LOCK:
        if _tender_worker_alive(run_id):
            raise MMFError("该任务正在识别需求，完成或停止后才能删除。")
        return delete_run(run_id, delete_files=delete_files)


def _run_tender_job(run_id: str, provider_name: str) -> None:
    bind_run(run_id)
    try:
        process_tender_run(run_id, provider_name)
    except Exception as exc:
        if is_cancelled(run_id) or "已停止" in str(exc):
            return
        log_exception(f"tender:{run_id}", exc)
        mark_tender_failed(run_id, exc)
    finally:
        unbind_run(run_id)
        with TENDER_JOBS_LOCK:
            TENDER_JOBS.pop(run_id, None)


def _start_tender_job(run_id: str, provider_name: str, *, decision=None) -> dict:
    from app_core import _validated_run_id, read_tender_status
    from tender_intake.cursor_recognition_recovery import WAITING, authorize_choice, pending_choice

    run_id = _validated_run_id(run_id)
    with TENDER_JOBS_LOCK:
        if SERVICE_STOPPING:
            raise MMFError("服务正在关闭，未启动新任务。")
        existing = TENDER_JOBS.get(run_id)
        if existing and existing.is_alive():
            return {"run_id": run_id, "status": "processing"}
        status = read_tender_status(run_id)
        run_dir = paths.current().runs_dir / run_id
        if not (run_dir / 'tender/extraction.json').is_file() or (run_dir / 'history_hidden.json').exists():
            raise MMFError("找不到可识别的原任务，请从历史记录打开仍保留的任务。")
        choice = pending_choice(run_dir, status)
        if decision is not None or choice or status.get("recognition_outcome") == WAITING:
            if (not choice or not isinstance(decision, dict)
                    or provider_name != status.get("provider_name")
                    or not authorize_choice(choice, decision.get("action"), decision.get("nonce"))):
                raise MMFError("请在「账号任务进度与处理」重新选择当前回复的处理方式。")
        mark_tender_processing(run_id, provider_name)
        worker = threading.Thread(target=_run_tender_job, args=(run_id, provider_name), daemon=True)
        TENDER_JOBS[run_id] = worker
        worker.start()
    return {"run_id": run_id, "status": "processing"}


def _apply_provider_network_lockdown() -> None:
    """MMF-007C：显式开启时，本进程失去一切外部 Provider 调用能力。

    仅当 MMF_PROVIDER_NETWORK_LOCKDOWN=1 时生效，正常产品启动行为不变；开启后任何非回环
    连接都会立即报错，运行日志与 runtime/provider_lockdown.json 留下可审计证据。
    """
    from providers import network_lockdown
    if not network_lockdown.enabled_by_env():
        return
    row = network_lockdown.install()
    print(f"provider network lockdown installed: {row}", flush=True)
    try:
        target = paths.current().runtime_dir / "provider_lockdown.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({**row, "env_flag_value": "1", "installed_at": now_iso()}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 证据落盘失败不得阻止启动
        pass


def main() -> None:
    _ensure_local_noproxy()
    rebind_app_core()
    _apply_provider_network_lockdown()
    run_environment_check(HOST, PORT, assume_self_listening=False)
    print(f"{paths.APP_NAME} {paths.APP_VERSION} running at http://{HOST}:{PORT}/", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.allow_reuse_address = True
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.5)
    except CLIENT_GONE:
        pass
    finally:
        try:
            server.server_close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
