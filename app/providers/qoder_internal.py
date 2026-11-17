"""Qoder 内部模型桥接 Provider（MMF-007C 阶段7，仅任务作用域）。

为什么存在：本任务只允许消耗 Qoder 环境自带的 Qwen3.8-Flash 免费额度，禁止使用任何外部
付费 Key。该 Provider 不发起任何网络请求，而是把 MMF 正式的 Plan / CU 契约 / Batch /
Repair / QA 请求原样投递到文件队列，由 Qoder 侧执行者取走生成后把响应放回队列。

因此 prompt 构造、契约校验、信封解析、QA 分级与修复预算全部沿用正式实现，只有"模型本体"
换成队列另一端的内部额度；正式 Provider 配置文件（providers.local.json / 凭据库）不被写入。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import (
    AIProvider,
    ProviderError,
    ProviderUnavailableError,
    normalize_generation,
    normalize_recommendation,
    parse_provider_payload,
    write_json,
)

DEFAULT_WAIT_SECONDS = 3600.0
POLL_INTERVAL_SECONDS = 1.0
MODEL_LABEL = "qwen3.8-flash (Qoder internal free quota)"
CREDENTIAL_SOURCE = "qoder_internal_free_quota"


def response_wait_seconds(request:dict[str,Any], configured:float, *, now:float|None=None)->float:
    if request.get("generation_mode") in {"ppt_slide", "ppt_recommend"} and request.get("response_deadline_epoch") is not None:
        remaining=float(request["response_deadline_epoch"])-(time.time() if now is None else now)
        if remaining<=0:
            raise ProviderError("PPT response deadline reached before queue dispatch",error_code="PPT_TIME_BUDGET")
        return min(configured,remaining)
    return configured


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _stamp(epoch: float | None) -> str:
    """带时区偏移的 ISO 时间，和 workflow_timing 同一口径，便于跨文件对表。"""
    if not epoch:
        return ""
    return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")


class QoderInternalProvider(AIProvider):
    """文件队列传输：MMF 契约进队，Qoder 内部模型出队。"""

    provider_version = "0.1"

    def __init__(self, config: dict[str, Any], credential_store: Any = None):
        # 故意丢弃 credential_store：本通道没有凭据概念，误配也不会读到任何 Key。
        super().__init__(config, None)

    # ── 队列布局 ────────────────────────────────────────────────
    def queue_root(self) -> Path:
        raw = str(self.config.get("queue_dir") or os.environ.get("MMF_QODER_INTERNAL_QUEUE_DIR") or "").strip()
        if not raw:
            raise ProviderUnavailableError("qoder_internal 未配置 queue_dir，无法使用文件队列。")
        return Path(raw)

    def _dir(self, *parts: str) -> Path:
        path = self.queue_root().joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_metadata(self) -> dict[str, Any]:
        return {
            "provider_name": self.config.get("provider_name", "qoder_internal"),
            "provider_type": "qoder_internal",
            "model": MODEL_LABEL,
            "endpoint_alias": "qoder_internal_queue",
            "provider_version": self.provider_version,
            "reasoning_mode": self.config.get("reasoning_mode", ""),
            "fallback_used": False,
            "fallback_reason": None,
            "test_mode": bool(self.config.get("test_mode", False)),
            "credential_source": CREDENTIAL_SOURCE,
            "external_network_calls": 0,
        }

    def health_check(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", True))
        message = "内部模型队列可用"
        available = enabled
        if enabled:
            try:
                self._dir("out")
                self._dir("in")
            except OSError as exc:
                available = False
                message = f"队列目录不可写：{exc}"
        else:
            message = "未启用"
        return {
            "configured": enabled,
            "available": available,
            "status": "available" if available else "not_configured",
            "message": message,
            "metadata": self.get_metadata(),
        }

    # ── 账本（QCODE_INTERNAL_QWEN_CALLS 的原始证据） ─────────────
    def _ledger(self, event: dict[str, Any]) -> None:
        path = self._dir("ledger") / "calls.jsonl"
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _bridge_times(self, job_id: str) -> dict[str, Any]:
        """执行者侧的第一方时间戳：取单时刻与答案落盘时刻。

        Round 1 只有 out/claims/answers 的文件 mtime 可用来重建四段时长，
        任何一次目录复制或清理都会把它们抹掉（本轮 Run 目录就遇到过）。
        把这两个值写进账本后，吞吐核算不再依赖 mtime。
        """
        times: dict[str, Any] = {}
        claim = _read_json(self._dir("claims") / f"{job_id}.claim.json")
        claimed = claim.get("claimed_at")
        if claimed:
            times["claimed_at"] = round(float(claimed), 3)
            times["claimed_at_iso"] = _stamp(claimed)
        answer = self._dir("answers") / f"{job_id}.answer"
        try:
            answered = answer.stat().st_mtime
        except OSError:
            answered = 0.0
        if answered:
            times["answered_at"] = round(answered, 3)
            times["answered_at_iso"] = _stamp(answered)
        return times

    def _cancelled(self) -> bool:
        if (self.queue_root() / "STOP").exists():
            return True
        try:
            from .openai_compatible import CURRENT_RUN_ID, is_cancelled
            run_id = CURRENT_RUN_ID.get()
            return bool(run_id and is_cancelled(run_id))
        except Exception:
            return False

    def _abandon(self, request_path: Path, job_id: str, reason: str) -> None:
        target = self._dir("abandoned") / f"{job_id}.{reason}.request.json"
        # Workers move out/ -> claims/ on claim. Retire the actual request,
        # otherwise collect_late_response sees claims forever and cannot recover it.
        claimed = self._dir("claims") / f"{job_id}.request.json"
        for candidate in (request_path, claimed):
            try:
                os.replace(candidate, target)
                return
            except OSError:
                continue
        # Do not delete an unretired request: its original identity is needed
        # for reconciliation, and an I/O failure must remain inspectable.

    def _retire(self, request_path: Path, job_id: str) -> None:
        # 已答请求必须离开 out/，否则执行者会在 pending 里看到旧单并重复消耗内部额度。
        if not request_path.is_file():
            request_path = self.queue_root() / "claims" / f"{job_id}.request.json"
        try:
            os.replace(request_path, self._dir("done") / f"{job_id}.request.json")
        except OSError:
            pass

    def _request_location(self, job_id: str) -> str:
        root = self.queue_root()
        for folder in ("out", "claims", "done", "abandoned"):
            for path in (root / folder).glob(f"{job_id}*.request.json"):
                return folder
        return "missing"

    def _park(self, response_path: Path, job_id: str) -> None:
        # 回收过的响应留在 late/：in/ 必须清空，否则下一次扫描会把同一份内容再算一次。
        try:
            os.replace(response_path, self._dir("late") / f"{job_id}.response.json")
        except OSError:
            try:
                response_path.unlink()
            except OSError:
                pass

    def _archive_response(self, response_path: Path, job_id: str) -> None:
        """消费掉的响应留在 ledger/，不要直接删除。

        Round 1 把 in/ 里的原件删干净之后，"执行者何时提交"这一段时间只能靠
        幸存的少数文件推断，吞吐四段里唯一没有第一方来源的就是它。留一份副本
        的代价是每轮几 MB，换来的是 handoff/submitted_at 可核对。
        """
        try:
            os.replace(response_path, self._dir("ledger") / f"{job_id}.response.json")
        except OSError:
            pass

    def collect_late_response(self, request: dict[str, Any], task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """P0-1：主协程已经超时之后才抵达的响应，必须按原请求回收，不得丢弃。"""
        job_id = str(request.get("job_id") or "")
        if not job_id:
            return None
        response_path = self._dir("in") / f"{job_id}.response.json"
        if not response_path.is_file():
            return None
        location = self._request_location(job_id)
        if location in {"out", "claims"}:
            # 请求还没被等待者放弃，响应归它；抢走会造成同一请求两次计费。
            return None
        payload = _read_json(response_path)
        if not payload:
            return None
        body = payload.get("body") if "body" in payload else payload.get("text")
        status = str(payload.get("status") or "").upper()
        if status == "ERROR" or body is None or body == "":
            code = str(payload.get("error_code") or "EMPTY_RESPONSE")
            self._ledger({
                "event": "LATE_RESPONSE_UNUSABLE",
                "job_id": job_id,
                "request_id": request.get("request_id"),
                "run_id": request.get("run_id"),
                "reason": status.lower() or "empty",
                "error_code": str(payload.get("error_code") or "EMPTY_RESPONSE"),
            })
            self._park(response_path, job_id)
            # An identified PPT worker rejection is no longer an unknown response.
            # Keep Word/legacy and unbound envelopes on their existing safe path.
            if (request.get("generation_mode") == "ppt_slide" and status == "ERROR"
                    and code in {"INVALID_JSON", "OUTPUT_CONTRACT_ERROR"}
                    and payload.get("job_id") == job_id
                    and payload.get("task_id") == request.get("task_id")):
                raise ProviderError(str(payload.get("error") or "structured output rejected"), error_code=code)
            return None
        self._ledger({
            "event": "LATE_RESPONSE_RECOVERED",
            "job_id": job_id,
            "task_id": str(request.get("task_id") or ""),
            "request_id": request.get("request_id"),
            "run_id": request.get("run_id"),
            "request_location": location,
            "worker": payload.get("worker"),
            "response_chars": len(body) if isinstance(body, str) else len(json.dumps(body, ensure_ascii=False)),
            "arrived_after_seconds": round(time.time() - float(payload.get("answered_at") or payload.get("created_at") or time.time()), 3),
            "received_at": round(time.time(), 3),
            "received_at_iso": _stamp(time.time()),
            "submitted_at": payload.get("submitted_at"),
            **self._bridge_times(job_id),
        })
        request_path = self._dir("abandoned") / f"{job_id}.TIMEOUT.request.json"
        if request_path.is_file():
            self._retire(request_path, job_id)
        result, metadata = self._finish(request, task_dir, job_id, body, payload)
        self._park(response_path, job_id)
        metadata["late_response_recovered"] = True
        return result, metadata

    # ── 调用 ───────────────────────────────────────────────────
    def _invoke(self, request: dict[str, Any], task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        if str(request.get("purpose") or "") == "capability_probe" or str(request.get("generation_mode") or "") == "capability_probe":
            # 连接自检验证的是传输通道本身，不进队列、不消耗内部额度、不计入模型调用。
            return {"probe": True, "ok": True}, self._task_metadata(
                str(request.get("task_id") or ""),
                formal_output=False,
                structured_purpose="capability_probe",
                serialization_repair={"applied": False},
                external_network_calls=0,
                credential_source=CREDENTIAL_SOURCE,
                transport="qoder_internal_file_queue",
                queued=False,
            )
        # A cancelled volume must not enqueue every remaining CU before noticing STOP.
        if self._cancelled():
            from .openai_compatible import GenerationCancelled
            raise GenerationCancelled("已停止生成")
        task_id = str(request.get("task_id") or f"job-{uuid.uuid4().hex[:12]}")
        # 调用方给出 job_id 时按它命名队列文件：晚到响应只能被"可重算的名字"回收。
        job_id = str(request.get("job_id") or "") or f"{task_id}-{uuid.uuid4().hex[:8]}"
        request_path = self._dir("out") / f"{job_id}.request.json"
        response_path = self._dir("in") / f"{job_id}.response.json"
        wait_seconds = response_wait_seconds(request,float(self.config.get("wait_timeout") or DEFAULT_WAIT_SECONDS))
        job = {
            "job_id": job_id,
            "task_id": task_id,
            "request_id": str(request.get("request_id") or ""),
            "run_id": str(request.get("run_id") or ""),
            "created_at": time.time(),
            "attempt_mode": request.get("attempt_mode"),
            "generation_mode": request.get("generation_mode"),
            "purpose": request.get("purpose"),
            "response_protocol": request.get("response_protocol") or (
                "batch_json" if request.get("generation_mode") == "longform_batch" else "strict_json"),
            "response_schema": request.get("json_schema"),
            "required_keys": sorted(set(request.get("required_keys") or [])),
            "batch_id": request.get("batch_id"),
            "section_ids": request.get("section_ids") or ([request.get("section_id")] if request.get("section_id") else []),
            "content_unit_ids": [
                str(row.get("content_unit_id"))
                for row in (request.get("content_unit_contracts") or [])
                if isinstance(row, dict) and row.get("content_unit_id")
            ],
            "max_tokens": request.get("max_tokens"),
            "system_prompt": str(request.get("system_prompt") or ""),
            "prompt": str(request.get("prompt") or ""),
        }
        if request.get("slide_id"):
            job["slide_id"] = str(request["slide_id"])
        write_json(request_path, job)
        self._ledger({
            "event": "REQUEST_ENQUEUED",
            "job_id": job_id,
            "task_id": task_id,
            "request_id": job["request_id"],
            "run_id": job["run_id"],
            "attempt_mode": job["attempt_mode"],
            "generation_mode": job["generation_mode"],
            "batch_id": job["batch_id"],
            "section_ids": job["section_ids"],
            "content_unit_ids": job["content_unit_ids"],
            "response_protocol": job["response_protocol"],
            "prompt_chars": len(job["prompt"]),
            "queued_at": round(job["created_at"], 3),
            "queued_at_iso": _stamp(job["created_at"]),
            "wait_budget_seconds": round(wait_seconds, 1),
        })
        interval = float(self.config.get("poll_interval") or POLL_INTERVAL_SECONDS)
        deadline = time.monotonic() + wait_seconds
        while True:
            if response_path.is_file():
                payload = _read_json(response_path)
                if not payload:
                    # 执行者未必原子落盘：半截 JSON 不能被判成"空响应"，继续等。
                    time.sleep(interval)
                    continue
                # 先离开 in/（否则晚到扫描会把它再算一次），但原件留在 ledger/ 供核算。
                self._archive_response(response_path, job_id)
                body = payload.get("body") if "body" in payload else payload.get("text")
                if str(payload.get("status") or "").upper() == "ERROR":
                    code = str(payload.get("error_code") or "PROVIDER_RUNTIME_ERROR")
                    self._ledger({"event": "WORKER_ERROR", "job_id": job_id, "error_code": code, "error": str(payload.get("error") or "")[:400]})
                    raise ProviderError(str(payload.get("error") or "Qoder 内部执行者返回错误。"), error_code=code)
                if body is None or body == "":
                    self._ledger({"event": "EMPTY_RESPONSE", "job_id": job_id})
                    raise ProviderError("Qoder 内部执行者返回空响应。", error_code="EMPTY_RESPONSE")
                now = time.time()
                times = self._bridge_times(job_id)
                claimed = float(times.get("claimed_at") or now)
                answered = float(times.get("answered_at") or claimed)
                self._ledger({
                    "event": "RESPONSE_RECEIVED",
                    "job_id": job_id,
                    "task_id": task_id,
                    "request_id": job["request_id"],
                    "run_id": job["run_id"],
                    "worker": payload.get("worker"),
                    "model": payload.get("model") or MODEL_LABEL,
                    "response_chars": len(body) if isinstance(body, str) else len(json.dumps(body, ensure_ascii=False)),
                    "wait_seconds": round(now - job["created_at"], 3),
                    "received_at": round(now, 3),
                    "received_at_iso": _stamp(now),
                    "submitted_at": payload.get("submitted_at"),
                    "submitted_at_iso": _stamp(payload.get("submitted_at")),
                    "claimed_at": round(claimed, 3) if times.get("claimed_at") else None,
                    "claimed_at_iso": times.get("claimed_at_iso") or "",
                    "answered_at": round(answered, 3) if times.get("answered_at") else None,
                    "answered_at_iso": times.get("answered_at_iso") or "",
                    "queue_wait_seconds": round(claimed - job["created_at"], 3) if times.get("claimed_at") else None,
                    "worker_latency_seconds": round(answered - claimed, 3) if times.get("answered_at") else None,
                    "handoff_seconds": round(now - answered, 3) if times.get("answered_at") else None,
                    "content_unit_count": len(job["content_unit_ids"]),
                })
                self._retire(request_path, job_id)
                result, metadata = self._finish(request, task_dir, job_id, body, payload)
                # 同一份四段时长也要落在 Run 目录里：队列目录是临时工位，Run 目录才是交付证据。
                metadata["bridge_timing"] = {
                    "queued_at": round(job["created_at"], 3),
                    "queued_at_iso": _stamp(job["created_at"]),
                    "claimed_at": round(claimed, 3) if times.get("claimed_at") else None,
                    "answered_at": round(answered, 3) if times.get("answered_at") else None,
                    "received_at": round(now, 3),
                    "received_at_iso": _stamp(now),
                    "submitted_at": payload.get("submitted_at"),
                    "queue_wait_seconds": round(claimed - job["created_at"], 3) if times.get("claimed_at") else None,
                    "worker_latency_seconds": round(answered - claimed, 3) if times.get("answered_at") else None,
                    "handoff_seconds": round(now - answered, 3) if times.get("answered_at") else None,
                    "total_seconds": round(now - job["created_at"], 3),
                }
                return result, metadata
            if self._cancelled():
                # 停止时队列里未答的请求不得继续消耗内部额度，但已回队内容一律保留。
                self._abandon(request_path, job_id, "CANCELLED")
                self._ledger({"event": "REQUEST_CANCELLED", "job_id": job_id, "task_id": task_id})
                from .openai_compatible import GenerationCancelled
                raise GenerationCancelled("已停止生成")
            if time.monotonic() >= deadline:
                self._abandon(request_path, job_id, "TIMEOUT")
                # 放弃等待 ≠ 请求失败：请求留在 abandoned/，晚到响应仍可被 reconcile 回收。
                self._ledger({
                    "event": "REQUEST_TIMED_OUT",
                    "job_id": job_id,
                    "task_id": task_id,
                    "request_id": job["request_id"],
                    "run_id": job["run_id"],
                    "wait_seconds": round(wait_seconds, 1),
                    "timed_out_at": round(time.time(), 3),
                    "timed_out_at_iso": _stamp(time.time()),
                    "content_unit_ids": job["content_unit_ids"],
                    **self._bridge_times(job_id),
                    "late_response_recoverable": True,
                })
                raise ProviderError(f"Qoder 内部执行者未在 {int(wait_seconds)} 秒内回队。", error_code="TIMEOUT")
            time.sleep(interval)

    def _finish(self, request: dict[str, Any], task_dir: Path, job_id: str, body: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        result, repair = parse_provider_payload(request, body, set(request.get("required_keys") or []))
        metadata = self._task_metadata(
            str(request.get("task_id") or ""),
            formal_output=True,
            structured_purpose=request.get("purpose", "generic"),
            serialization_repair=repair or {"applied": False},
            external_network_calls=0,
            credential_source=CREDENTIAL_SOURCE,
            transport="qoder_internal_file_queue",
            finish_reason="stop",
        )
        metadata["worker"] = payload.get("worker")
        metadata["job_id"] = job_id
        task_dir.mkdir(parents=True, exist_ok=True)
        write_json(task_dir / "provider_raw_envelope.json", {"qoder_internal": True, "job_id": job_id, "payload": body})
        write_json(task_dir / "provider_structured_output.json", result)
        write_json(task_dir / "provider_audit.json", metadata)
        return result, metadata

    def recommend_knowledge(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return normalize_recommendation(result, metadata)

    def generate_solution(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return normalize_generation(result, metadata)

    def invoke_structured(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return {**result, "provider_metadata": metadata}
