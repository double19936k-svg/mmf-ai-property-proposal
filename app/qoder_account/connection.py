from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from providers.base import ProviderError, ProviderUnavailableError, write_json
from .edition import edition_settings


def enabled() -> bool:
    return os.environ.get("MMF_QODER_ACCOUNT_ENABLED", "").lower() in {"1", "true"}


def runtime_root() -> Path:
    from paths import current
    return current().runtime_dir / edition_settings()["state_dir"]


def _python() -> str:
    return os.environ.get("MMF_QODER_ACCOUNT_PYTHON") or sys.executable


def recognition_job_limit() -> int:
    return min(24, max(1, int(os.environ.get('MMF_QODER_ACCOUNT_RECOGNITION_MAX_JOBS') or 24)))


def is_recognition_job(row: dict) -> bool:
    return (row.get('generation_mode') == 'tender_requirement_recognition'
        or '-tender-CHK-' in str(row.get('task_id') or ''))


def recognition_grace_seconds(*, recognition, constrained, last_text, now):
    """Give an actively streaming recognition request one bounded finishing window."""
    if recognition and not constrained and last_text is not None and 0 <= now - last_text <= 20:
        return 120
    return 0


def terminal_failure(row: dict) -> ProviderError:
    if row.get("stop_reason") == "max_tokens":
        failure = ProviderError("Qoder回复达到输出上限，内容被截断；原答已保留。", error_code="QODER_OUTPUT_TRUNCATED")
        failure.max_output_tokens = row.get("max_output_tokens") or 8000
        return failure
    return ProviderError("Qoder本次请求返回错误，请核对账号、额度和模型。", error_code="QODER_REQUEST_ERROR")


class Connection:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.brand = edition_settings()
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()
        self.control_busy = False
        self.login_process = None
        self.suspended = False
        self.sequence = 0
        self.events: list[dict] = []
        self.state = {"status": "not_connected", "message": "尚未连接Qoder", "models": [],
            "usage": None, "usage_availability": "NOT_AVAILABLE", "connection_check_model_requests": 0}
        settings = self.root / "selection.json"
        try:
            self.selection = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.selection = {}

    def environment(self) -> dict:
        env = os.environ.copy()
        # Explicit local-login route: never inherit a PAT/API key or an alternate endpoint.
        for key in list(env):
            if re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)$", key, re.I) or key.startswith(("QODER_", "QODERCN_")):
                env.pop(key, None)
        work = self.root / "workspace"
        work.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "temp"
        tmp.mkdir(exist_ok=True)
        env[self.brand["config_env"]] = os.environ.get("MMF_QODER_ACCOUNT_HOME") or str(self.root / "account")
        env.update(MMF_QODER_ACCOUNT_EDITION=self.brand["id"],
            MMF_QODER_ACCOUNT_WORKSPACE=str(work), TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp),
            PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        return env

    def command(self, mode: str) -> list[str]:
        return [_python(), "-B", str(Path(__file__).with_name("worker.py")), mode]

    def event(self, phase: str, **fields) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.sequence += 1
            row = {"sequence": self.sequence, "time": time.time(), "phase": phase, **fields}
            self.events.append(row)
            self.events = self.events[-500:]
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")

    def snapshot(self, after: int = 0, run_id: str = "") -> dict:
        from account_budget import WORD_CALL_CAPS

        with self.lock:
            progress = {}
            # Persisted results provide counts even when streamed activity evicts old events.
            for path in (self.root / "jobs").glob("*/*.json"):
                if path.name.endswith(".answer.json"):
                    continue
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                rid = str(row.get("run_id") or "standalone")
                stats = progress.setdefault(rid, {"admitted": 0, "returned": 0})
                stats["admitted"] += 1
                stats["returned"] += int(row.get("status") == "returned")
            return {"enabled": enabled(), **self.state, "selected_model": self.selection.get("model", ""),
                "edition": self.brand["id"], "product_label": self.brand["label"],
                "connecting": self.control_busy, "suspended": self.suspended,
                "executing": self.execution_lock.locked(), "run_progress": progress,
                "max_jobs_per_run": int(os.environ.get("MMF_QODER_ACCOUNT_MAX_JOBS") or 8),
                "word_call_caps": None if os.environ.get("MMF_QODER_ACCOUNT_MAX_JOBS") else WORD_CALL_CAPS,
                "recognition_max_jobs_per_run": recognition_job_limit(),
                "events": [e for e in self.events if e["sequence"] > after and (not run_id or e.get("run_id") == run_id)],
                "event_cursor": self.sequence, "full_word_ppt_verified": False}

    def _one(self, mode: str, timeout: int = 45) -> dict:
        result = subprocess.run(self.command(mode), env=self.environment(), cwd=self.root / "workspace",
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in reversed(result.stdout.splitlines()):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") in {"runtime", "connection"}:
                return row
        raise ProviderUnavailableError("Qoder连接组件暂不可用，请检查专用运行环境。")

    def start_connect(self, login: bool = True) -> dict:
        with self.lock:
            if self.control_busy:
                return self.snapshot()
            self.suspended = False
            self.control_busy = True
            self.state.update(status="connecting", message="正在检查Qoder运行环境和账户…")
        threading.Thread(target=self._connect, args=(login,), daemon=True).start()
        return self.snapshot()

    def _connect(self, login: bool) -> None:
        try:
            self.environment()
            info = self._one("inspect", 20)
            if info.get("edition", self.brand["id"]) != self.brand["id"]:
                raise ProviderUnavailableError("Qoder运行组件与所选版本不一致，未开始登录。")
            row = self._one("connect", 65)
            if row.get("status") == "authentication_required" and login:
                with self.lock:
                    if self.suspended:
                        return
                    self.state.update(status="authentication_required", message="请在Qoder官方页面完成登录，登录后会自动检查。")
                self.event("login_required")
                # Only the initial PKCE login URL is held in memory as a browser fallback.
                # Never persist CLI output or authentication challenges to evidence/logs.
                login_env = self.environment()
                # Official headless flow reliably prints the URL. The product opens
                # only its validated Qoder URL, and also exposes a clickable fallback.
                login_env["CI"] = "1"
                process = subprocess.Popen([info["cli_path"], "login"], env=login_env,
                    cwd=self.root / "workspace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.login_process = process
                def read_login_url():
                    import webbrowser
                    from urllib.parse import urlsplit, parse_qs
                    opened = False
                    try:
                        for line in process.stdout:
                            for url in re.findall(r'https://[^\s\x1b]+', line):
                                parsed = urlsplit(url)
                                params = parse_qs(parsed.query)
                                allowed = {"challenge", "challenge_method", "nonce", "machine_id", "client_id", "machine_token"}
                                if (parsed.hostname in self.brand["login_hosts"] and parsed.path == "/device/selectAccounts"
                                        and set(params) <= allowed and params.get("challenge")):
                                    with self.lock:
                                        if not self.suspended:
                                            self.state["official_login_url"] = url
                                    if not opened and not self.suspended:
                                        opened = True
                                        webbrowser.open(url)
                    except (OSError, ValueError):
                        pass
                threading.Thread(target=read_login_url, daemon=True).start()
                try:
                    process.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
                finally:
                    self.login_process = None
                    with self.lock:
                        self.state.pop("official_login_url", None)
                row = self._one("connect", 65)
            with self.lock:
                if self.suspended:
                    return
                self.state.update({k: v for k, v in row.items() if k not in {"event", "model_requests"}})
                ready = row.get("status") == "ready"
                self.state["checked_at"] = time.time()
                self.state["message"] = "账户已连接，请选择模型；连接检查未调用模型。" if ready else "需要在官方页面登录后重新检查。"
            self.event("connected" if ready else "login_required", model_requests=0)
        except Exception:
            with self.lock:
                if not self.suspended:
                    self.state.update(status="unavailable", message="连接未完成，请重试检查或重新登录。", models=[])
            self.event("connection_unavailable")
        finally:
            with self.lock:
                self.control_busy = False

    def select_model(self, value: str) -> dict:
        with self.lock:
            models = self.state.get("models") or []
            if self.state.get("status") != "ready" or not any(m.get("value") == value and m.get("isEnabled") is True for m in models):
                raise ProviderError("请选择当前账户可用的Qoder模型。")
            self.selection = {"model": value}
            write_json(self.root / "selection.json", self.selection)
        return self.snapshot()

    def disconnect(self) -> dict:
        with self.lock:
            if self.execution_lock.locked():
                raise ProviderError("本产品仍有Qoder任务运行，请先停止本轮生成。")
            self.suspended = True
            self.state.pop("official_login_url", None)
            self.state.update(status="not_connected", message="已断开本产品连接，可随时重新连接。", models=[])
            process = self.login_process
        if process and process.poll() is None:
            process.terminate()
        self.event("disconnected")
        return self.snapshot()

    def health(self) -> dict:
        snapshot = self.snapshot()
        ready = snapshot["status"] == "ready" and bool(snapshot["selected_model"]) and not self.suspended
        return {"configured": enabled(), "available": ready, "status": "available" if ready else snapshot["status"],
            "message": "Qoder账户已连接" if ready else snapshot["message"],
            "models": [m["value"] for m in snapshot["models"] if m.get("isEnabled")],
            "credential_source": self.brand["credential_source"]}

    def prepare_ppt_run(self, run_id, body_pages, call_cap):
        from zcode_account.policy import prepare_ppt_policy
        if not isinstance(run_id, str) or not run_id:
            raise ProviderError("PPT任务编号无效，未发送请求。", error_code="QODER_PLAN_INVALID")
        jobs = self.root / "jobs" / hashlib.sha256(run_id.encode()).hexdigest()[:16]
        with self.execution_lock:
            return prepare_ppt_policy(jobs, run_id, self.selection.get("model", ""), int(body_pages), int(call_cap))

    def execute(self, request: dict, task_dir: Path, cancelled) -> dict:
        from providers.openai_compatible import GenerationCancelled
        if cancelled():
            raise GenerationCancelled("已停止生成")
        health = self.health()
        if not health["available"]:
            raise ProviderUnavailableError(health["message"])
        while not self.execution_lock.acquire(timeout=0.2):
            if cancelled():
                raise GenerationCancelled("已停止生成")
        try:
            return self._execute(request, task_dir, cancelled)
        finally:
            self.execution_lock.release()

    def _execute(self, request: dict, task_dir: Path, cancelled) -> dict:
        from providers.openai_compatible import GenerationCancelled
        if cancelled() or self.suspended:
            raise GenerationCancelled("已停止生成")
        run_id = str(request.get("run_id") or "standalone")
        model = str(self.selection["model"])
        identity = {k: request.get(k) for k in ("run_id", "job_id", "task_id", "request_id", "slide_id", "batch_id", "section_ids", "attempt_mode")}
        identity['generation_mode'] = request.get('generation_mode')
        digest = hashlib.sha256(json.dumps({"request": request, "model": model}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        slot = str(request.get("request_id") or request.get("job_id") or request.get("task_id") or task_dir.resolve())
        key = hashlib.sha256((run_id + "|" + slot).encode()).hexdigest()[:24]
        run_key = hashlib.sha256(run_id.encode()).hexdigest()[:16]
        ledger_dir = self.root / "jobs" / run_key
        ledger_dir.mkdir(parents=True, exist_ok=True)
        model_lock = ledger_dir / ".run_state"
        if model_lock.is_file():
            if json.loads(model_lock.read_text(encoding="utf-8")).get("model") != model:
                raise ProviderError("本轮已绑定原模型，请恢复原模型或新建任务。", error_code="QODER_MODEL_LOCKED")
        marker = ledger_dir / (key + ".json")
        if marker.is_file():
            previous = json.loads(marker.read_text(encoding="utf-8"))
            if previous.get("digest") != digest:
                raise ProviderError("请求身份与原内容不一致，未重新派发。", error_code="REQUEST_IDENTITY_MISMATCH")
            if previous.get("status") == "returned" and (ledger_dir / (key + ".answer.json")).is_file():
                cached = json.loads((ledger_dir / (key + ".answer.json")).read_text(encoding="utf-8"))
                if cached.get("is_error") or cached.get("stop_reason") == "max_tokens":
                    raise terminal_failure(cached)
                return cached
            raise ProviderError("原Qoder请求已派发，需先核对原结果，未重复调用。", error_code="RESPONSE_UNKNOWN")
        # Only admission markers count, not answer files. This survives process restarts.
        recognition = request.get('generation_mode') == 'tender_requirement_recognition'
        recommendation = request.get('generation_mode') == 'knowledge_recommendation'
        markers = []
        for marker_path in ledger_dir.glob('*.json'):
            if marker_path.name.endswith('.answer.json'):
                continue
            row = json.loads(marker_path.read_text(encoding='utf-8'))
            # A writing-budget record has no task. It must not count as a generation call.
            if isinstance(row, dict) and (row.get('task_id') or row.get('budget_scope') or row.get('generation_mode')):
                markers.append(row)
        def _recommendation_row(row):
            return row.get('budget_scope') == 'recommendation' or row.get('generation_mode') == 'knowledge_recommendation' or str(row.get('task_id') or '').endswith('-recommend')
        if recommendation:
            count = sum(_recommendation_row(row) for row in markers)
            limit = 2
        else:
            count = sum((is_recognition_job(row) == recognition) and not _recommendation_row(row) for row in markers)
            limit = recognition_job_limit() if recognition else int(os.environ.get('MMF_QODER_ACCOUNT_MAX_JOBS') or 8)
            if not recognition and not os.environ.get('MMF_QODER_ACCOUNT_MAX_JOBS'):
                if request.get('generation_mode') == 'ppt_slide' or (ledger_dir / '.ppt_policy').is_file():
                    from zcode_account.policy import generation_call_cap
                    limit = generation_call_cap(ledger_dir)
                else:
                    from account_budget import word_cap
                    limit=word_cap(ledger_dir,run_id,model)
        if count >= limit:
            kind = "文件识别" if recognition else "知识推荐" if recommendation else "生成"
            raise ProviderError(f"本轮Qoder{kind}已到{limit}次请求上限，已有内容已保留。", error_code="QODER_BUDGET_EXHAUSTED")
        budget = 240
        if request.get("generation_mode") == "tender_requirement_recognition":
            budget = min(600, max(1, float(request.get("timeout_seconds") or 600)))
        remaining = float(os.environ.get("MMF_QODER_ACCOUNT_TASK_SECONDS") or budget)
        if request.get("response_deadline_epoch"):
            remaining = min(remaining, float(request["response_deadline_epoch"]) - time.time())
        if remaining <= 0:
            raise ProviderError("本轮时间预算已到，未发送Qoder请求。", error_code="PPT_TIME_BUDGET")
        task_dir.mkdir(parents=True, exist_ok=True)
        if not model_lock.exists():
            write_json(model_lock, {"model": model})
        write_json(task_dir / "qoder_request_identity.json", {**identity, "digest": digest, "requested_model": model})
        scope = "recommendation" if recommendation else "recognition" if recognition else "generation"
        write_json(marker, {**identity, "digest": digest, "status": "admitted", "time": time.time(), "budget_scope": scope})
        self.event("admitted", run_id=run_id, task_id=request.get("task_id"), slide_id=request.get("slide_id"), job_id=key)
        payload = {"model": model, "system_prompt": request.get("system_prompt"),
            "prompt": request.get("prompt"), "max_tokens": request.get("max_tokens"),
            "generation_mode": request.get("generation_mode"), **({'native_session_id':request['native_session_id']} if request.get('native_session_id') else {})}
        process = subprocess.Popen(self.command("execute"), env=self.environment(), cwd=self.root / "workspace",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows: queue.Queue = queue.Queue()
        def reader():
            try:
                for line in process.stdout:
                    try:
                        rows.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                rows.put(None)
        threading.Thread(target=reader, daemon=True).start()
        terminal = None
        permissions=[]
        deadline = time.monotonic() + remaining
        can_extend = recognition and not (
            os.environ.get("MMF_QODER_ACCOUNT_TASK_SECONDS") or request.get("response_deadline_epoch"))
        last_text = None
        last_logged_activity = None
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False))
            process.stdin.close()
            while True:
                if cancelled():
                    raise GenerationCancelled("已停止本轮Qoder生成，已有内容保留；在途状态需核对。")
                now = time.monotonic()
                if now >= deadline:
                    grace = recognition_grace_seconds(recognition=recognition, constrained=not can_extend,
                                                      last_text=last_text, now=now)
                    if grace:
                        deadline += grace
                        can_extend = False
                        self.event("generating", run_id=run_id, task_id=request.get("task_id"), job_id=key,
                                   activity="recognition_finishing_window")
                    else:
                        self.event("response_unknown", run_id=run_id, task_id=request.get("task_id"), job_id=key,
                                   reason="deadline_while_streaming" if last_text is not None and now-last_text <= 20 else "deadline")
                        raise ProviderError("Qoder未在本次等待上限内返回完整结果，原请求状态需核对，未自动重发。", error_code="RESPONSE_UNKNOWN")
                try:
                    row = rows.get(timeout=min(0.2, max(0, deadline-now)))
                except queue.Empty:
                    continue
                if row is None:
                    break
                kind = row.get("event")
                if kind=='permission_denied':
                    permissions.append(str(row.get('tool_name','unknown'))[:100])
                    self.event('permission_denied',run_id=run_id,task_id=request.get('task_id'),message='当前工具权限未授予，操作未执行。')
                if kind=='session_started' and row.get('session_id'):
                    write_json(task_dir/'qoder_session.json',{'session_id':row['session_id']})
                if kind == "result":
                    terminal = {**row, "identity": identity, "digest": digest, "requested_model": model,'permission_denied_tools':permissions}
                    write_json(ledger_dir / (key + ".answer.json"), terminal)
                    write_json(task_dir / "qoder_raw_answer.json", terminal)
                    self.event("returned", run_id=run_id, task_id=request.get("task_id"), job_id=key, activity=kind)
                    break
                elif kind == "auth_expired" or (kind == "connection" and not row.get("logged_in")):
                    self.state.update(status="authentication_required", message="Qoder登录已失效，请重新连接。")
                if kind == "activity" and row.get("phase") == "generating":
                    if row.get("text_chars", 0) > 0:
                        last_text = time.monotonic()
                    if last_logged_activity is not None and now - last_logged_activity < 1:
                        continue
                    last_logged_activity = now
                self.event("generating", run_id=run_id, task_id=request.get("task_id"), job_id=key, activity=kind)
            if terminal is None:
                process.wait(timeout=5)
                raise ProviderError("Qoder未返回可核对的终态，已有内容保留。", error_code="RESPONSE_UNKNOWN")
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            write_json(marker, {**identity, "digest": digest, "status": "returned", "time": time.time(), "budget_scope": scope})
            if terminal.get("is_error") or terminal.get("stop_reason") == "max_tokens":
                raise terminal_failure(terminal)
            return terminal
        finally:
            if process.poll() is None:
                # Interrupt only this child tree. Never terminate the user's other Qoder sessions.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    process.terminate()
                process.wait(timeout=10)
            for handle in (process.stdout, process.stdin):
                if handle and not handle.closed:
                    handle.close()


_CONNECTIONS: dict[str, Connection] = {}
_LOCK = threading.Lock()


def connection() -> Connection:
    key = str(runtime_root().resolve())
    with _LOCK:
        if key not in _CONNECTIONS:
            _CONNECTIONS[key] = Connection(Path(key))
        return _CONNECTIONS[key]
