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
from run_layout import ensure_run_layout, finalize_run
from user_errors import classify, sanitize

paths.reload()
paths.ensure_directories()

from app_core import (  # noqa: E402
    MEDIA,
    MMFError,
    RUNS_DIR,
    SCENARIOS,
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
    process_tender_run,
    reconcile_tender_status,
    provider_manager,
    provider_status,
    recommend_knowledge,
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


APP_CONFIG = json.loads((paths.CONFIG_DIR / "app.json").read_text(encoding="utf-8-sig"))
HOST = os.environ.get("MMF_HOST", APP_CONFIG.get("host", "127.0.0.1"))
PORT = int(os.environ.get("MMF_PORT", APP_CONFIG.get("port", 3050)))
if HOST not in {"127.0.0.1", "localhost"}:
    HOST = "127.0.0.1"

GENERATION_JOBS: dict[str, threading.Thread] = {}
GENERATION_JOBS_LOCK = threading.RLock()
TENDER_JOBS: dict[str, threading.Thread] = {}
TENDER_JOBS_LOCK = threading.RLock()
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
                if phase_path.is_file():
                    phase_record = json.loads(phase_path.read_text(encoding="utf-8-sig"))
                    phase = str(phase_record.get("phase") or phase)
                    message = str(phase_record.get("message") or message)
                elif checkpoint_path.is_file():
                    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
                    phase = str(checkpoint.get("public_phase") or phase)
                    if phase in {"AUTO_REPAIRING", "RECHECKING", "SECTION_QA"}:
                        message = "正在检查并完善方案内容。"
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            write_json(status_path, {
                "run_id": run_id,
                "status": "running",
                "started_at": started_at,
                "elapsed_seconds": elapsed,
                "phase": phase,
                "recoverable_qa": phase in {"SECTION_QA", "AUTO_REPAIRING", "RECHECKING"},
                "message": message,
            })

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()
    def _finish_status(status: str, extra: dict | None = None) -> dict:
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

    try:
        result = generate_artifact(run_id, selected_positive_ids, clarification_answers, speed_profile=speed_profile, max_parallelism=max_parallelism, trust_submitted_selection=trust_submitted_selection)
        if is_cancelled(run_id):
            _finish_status("cancelled", {"error": "已停止生成"})
            return
        result = finalize_run(run_id, result)
        result["started_at"] = started_at
        result["elapsed_seconds"] = max(0, int(time.monotonic() - started))
        status = "completed" if result.get("status") == "generation_completed" else result.get("status", "completed")
        _finish_status(status, {"result": result})
    except (GenerationCancelled, ProviderUnavailableError) as exc:
        if is_cancelled(run_id) or "已停止生成" in str(exc):
            _finish_status("cancelled", {"error": "已停止生成"})
        else:
            log_exception(f"generate:{run_id}", exc)
            mapped = classify(exc)
            _finish_status("failed", {"error": mapped["error"], "error_code": mapped["error_code"]})
    except Exception as exc:
        if is_cancelled(run_id) or "已停止生成" in str(exc):
            _finish_status("cancelled", {"error": "已停止生成"})
        else:
            log_exception(f"generate:{run_id}", exc)
            mapped = classify(exc)
            _finish_status("failed", {"error": mapped["error"], "error_code": mapped["error_code"]})
    finally:
        stop.set()
        unbind_run(run_id)
        with GENERATION_JOBS_LOCK:
            GENERATION_JOBS.pop(run_id, None)


def cancel_generation(run_id: str) -> dict:
    status_path = _generation_status_path(run_id)
    started_at = None
    previous_elapsed = None
    if status_path.is_file():
        try:
            previous = json.loads(status_path.read_text(encoding="utf-8-sig"))
            started_at = previous.get("started_at")
            previous_elapsed = previous.get("elapsed_seconds")
        except (OSError, json.JSONDecodeError, TypeError):
            previous = {}
    abort_run_http(run_id)
    tender_status = paths.current().runs_dir / run_id / "tender" / "status.json"
    if tender_status.is_file():
        try:
            tender = json.loads(tender_status.read_text(encoding="utf-8-sig"))
            tender["error"] = "已停止识别"
            tender["error_code"] = "CANCELLED"
            tender["pack_status"] = "cancelled"
            tender["updated_at"] = now_iso()
            write_json(tender_status, tender)
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    finished_at = now_iso()
    elapsed = duration_seconds(started_at, finished_at, previous_elapsed)
    write_json(status_path, {
        "run_id": run_id,
        "status": "cancelled",
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": elapsed,
        "error": "已停止生成",
    })
    finish_workflow(paths.current().runs_dir / run_id, "cancelled")
    return {"ok": True, "run_id": run_id, "status": "cancelled", "delivery_status": "USER_CANCELLED", "elapsed_seconds": elapsed, "message": "已停止生成。已生成的文件仍保留。"}


def _start_generation_job(run_id: str, selected_positive_ids: list[str], clarification_answers: dict, speed_profile: str | None = None, max_parallelism: int | None = None, trust_submitted_selection: bool = False) -> dict:
    current = _enrich_run(load_run_status(run_id))
    if current.get("run_status") == "generation_completed":
        return finalize_run(run_id, {"run_id": run_id, "status": "generation_completed", "download_url": current.get("download_url", "")})
    with GENERATION_JOBS_LOCK:
        existing = GENERATION_JOBS.get(run_id)
        if existing and existing.is_alive():
            return {"run_id": run_id, "status": "generation_in_progress", "started_at": current.get("started_at"), "provider_name": current.get("provider_name"), "message": "当前任务仍在生成。"}
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
                    "default_provider": settings.get("default_provider") or "qwen_modelstudio",
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
            if path == "/api/providers/health":
                self._json({"providers": _providers_payload(refresh=False)})
                return
            if path == "/api/providers":
                manager = provider_manager()
                self._json({
                    "providers": _providers_payload(),
                    "credential_capability": manager.credential_store.capability(),
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
                    if isinstance(record.get("result"), dict):
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
                self._json(_start_tender_job(data["run_id"], data.get("provider_name", "")))
                return
            if self.path == "/api/tender/accept-local-fallback":
                data = self._read_json()
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
                self._json(delete_run(run_id, delete_files=bool(data.get("delete_files"))))
                return
            if self.path.startswith("/api/runs/") and self.path.endswith("/cancel"):
                run_id = unquote(self.path.split("/")[3])
                self._json(cancel_generation(run_id))
                return
            if self.path.startswith("/api/providers/") and self.path.endswith("/config"):
                name = self.path.split("/")[3]
                data = self._read_json()
                saved = provider_manager().save_config(name, data.get("config", {}), data.get("api_key", ""))
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
                name = str(data.get("provider_name") or settings.get("image_provider_name") or "qwen_image")
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


def _tender_worker_alive(run_id: str) -> bool:
    with TENDER_JOBS_LOCK:
        worker = TENDER_JOBS.get(run_id)
        return bool(worker and worker.is_alive())


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


def _start_tender_job(run_id: str, provider_name: str) -> dict:
    with TENDER_JOBS_LOCK:
        existing = TENDER_JOBS.get(run_id)
        if existing and existing.is_alive():
            return {"run_id": run_id, "status": "processing"}
        mark_tender_processing(run_id, provider_name)
        worker = threading.Thread(target=_run_tender_job, args=(run_id, provider_name), daemon=True)
        TENDER_JOBS[run_id] = worker
        worker.start()
    return {"run_id": run_id, "status": "processing"}


def main() -> None:
    _ensure_local_noproxy()
    rebind_app_core()
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
