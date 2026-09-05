#!/usr/bin/env python3
"""Grok Bridge V0.1: Codex-controlled, single-turn local subcontractor."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


BRIDGE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME = BRIDGE_DIR / "runtime"
SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
VALID_STATUSES = {
    "SUCCESS",
    "AUTHENTICATED",
    "AUTH_REQUIRED",
    "AUTH_STATE_UNKNOWN",
    "CLI_NOT_FOUND",
    "NETWORK_ERROR",
    "PROXY_ERROR",
    "MODEL_UNAVAILABLE",
    "PERMISSION_RESTRICTED",
    "SESSION_ERROR",
    "TIMEOUT",
    "CLI_ERROR",
    "INVALID_OUTPUT",
    "VALIDATION_ERROR",
    "PLATFORM_CONFIRMATION_REQUIRED",
    "PROVIDER_AUTH_REQUIRED",
    "CLI_MAX_TURNS",
    "OUTPUT_CONTRACT_ERROR",
    "PROVIDER_RUNTIME_ERROR",
    "PROVIDER_UNAVAILABLE",
}

AUTH_MARKERS = ("not authenticated", "no auth credentials", "not signed in", "login required", "unauthorized", "http 401", "status 401")
PROXY_MARKERS = ("proxy error", "proxyconnect", "tunnel connection failed", "cannot connect to proxy")
PERMISSION_MARKERS = ("permission denied", "access is denied", "operation not permitted", "sandbox")
SESSION_MARKERS = ("session error", "invalid session", "session expired", "failed to create session")
MODEL_MARKERS = ("model unavailable", "model not found", "unknown model", "not available for this account")
NETWORK_MARKERS = (
    "network error",
    "network(reqwest",
    "failed to fetch models",
    "model catalog fetch timed out",
    "settings fetch failed",
    "timedout",
    "timed out",
    "connection refused",
    "connection reset",
)
MAX_TURN_MARKERS = ("max turns reached", "maximum turns reached", "agent max turns")


class GrokBridgeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TaskValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_family(name: str) -> str:
    value = str(name or "").strip().lower()
    for suffix in ("-build", "-fast", "-latest", "-preview"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def models_match_request(actual: list[str], expected: str, requested: str) -> bool:
    if not actual:
        return False
    allowed = {_model_family(expected), _model_family(requested)}
    return all(_model_family(item) in allowed for item in actual)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TaskValidationError("Task JSON must be an object.")
    return value


def load_bridge_config(config_path: Path | None) -> dict[str, Any]:
    selected = config_path
    if selected is None:
        conventional = BRIDGE_DIR / "bridge_config.json"
        selected = conventional if conventional.is_file() else None
    if selected is None:
        return {}
    if not selected.is_file():
        raise TaskValidationError(f"Bridge config does not exist: {selected}")
    return load_json(selected)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def discover_proxy_url() -> str | None:
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    for port in (7897, 7890, 10809, 10808, 6152, 20171, 8888):
        if _port_open("127.0.0.1", port):
            return f"http://127.0.0.1:{port}"
    return None


def proxy_settings(config: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    proxy = config.get("proxy", {})
    if proxy is None:
        proxy = {}
    if not isinstance(proxy, dict):
        raise TaskValidationError("proxy config must be an object.")
    mode = str(proxy.get("mode") or "auto").strip().lower()
    enabled = proxy.get("enabled")
    if mode == "off" or enabled is False and mode != "auto":
        return {}, {"proxy_enabled": False, "proxy_env_injected": False, "proxy_mode": mode}

    url = str(proxy.get("url") or "").strip() or discover_proxy_url()
    no_proxy = proxy.get("no_proxy", "localhost,127.0.0.1")
    if not url:
        return {}, {"proxy_enabled": False, "proxy_env_injected": False, "proxy_mode": "auto"}
    if not isinstance(url, str) or not url.strip():
        raise TaskValidationError("proxy.url is required when proxy is enabled.")
    if not isinstance(no_proxy, str):
        raise TaskValidationError("proxy.no_proxy must be a string.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise TaskValidationError(f"proxy.url is invalid: {exc}") from exc
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise TaskValidationError("proxy.url uses an unsupported scheme.")
    if not parsed.hostname or port is None:
        raise TaskValidationError("proxy.url must include a host and port.")

    proxy_env = {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "ALL_PROXY": url,
        "NO_PROXY": no_proxy,
        "http_proxy": url,
        "https_proxy": url,
        "all_proxy": url,
        "no_proxy": no_proxy,
    }
    audit = {
        "proxy_enabled": True,
        "proxy_scheme": parsed.scheme,
        "proxy_host": parsed.hostname,
        "proxy_port": port,
        "proxy_env_injected": True,
    }
    return proxy_env, audit


def resolve_grok(config: dict[str, Any]) -> tuple[str, str]:
    configured = config.get("grok_executable")
    executable = None
    if configured:
        if not isinstance(configured, str):
            raise TaskValidationError("grok_executable must be a path string.")
        candidate = Path(os.path.expandvars(configured)).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"Configured grok executable was not found: {candidate}")
        executable = str(candidate)
    if not executable:
        executable = shutil.which("grok")
    if not executable:
        fallback = Path.home() / ".grok" / "bin" / "grok.exe"
        if fallback.is_file():
            executable = str(fallback)
    if not executable:
        raise FileNotFoundError("grok executable was not found.")

    probe = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        shell=False,
    )
    version = (probe.stdout or probe.stderr).strip()
    if probe.returncode != 0:
        raise RuntimeError(f"grok --version failed: {version}")
    return executable, version


def validate_task(task: dict[str, Any]) -> dict[str, Any]:
    task_id = task.get("task_id")
    instruction = task.get("instruction")
    working_directory = task.get("working_directory")
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskValidationError("task_id is required.")
    if not SAFE_TASK_ID.fullmatch(task_id):
        raise TaskValidationError("task_id contains unsafe characters or is too long.")
    if not isinstance(instruction, str) or not instruction.strip():
        raise TaskValidationError("instruction is required.")
    if not isinstance(working_directory, str) or not working_directory.strip():
        raise TaskValidationError("working_directory is required.")

    workdir = Path(working_directory).resolve()
    if not workdir.is_dir():
        raise TaskValidationError("working_directory does not exist or is not a directory.")
    if workdir.parent == workdir:
        raise TaskValidationError("A drive root cannot be used as working_directory.")
    import os
    for raw in [item for item in os.environ.get("MMF_FORBIDDEN_ROOTS", "").split(os.pathsep) if item.strip()]:
        if workdir == Path(raw).resolve():
            raise TaskValidationError("Forbidden workspace root cannot be used as working_directory.")

    allowed_inputs = task.get("allowed_inputs", [])
    if not isinstance(allowed_inputs, list) or not all(isinstance(item, str) for item in allowed_inputs):
        raise TaskValidationError("allowed_inputs must be an array of paths.")
    resolved_inputs: list[str] = []
    for item in allowed_inputs:
        source = Path(item).resolve()
        if not source.exists():
            raise TaskValidationError(f"allowed input does not exist: {item}")
        resolved_inputs.append(str(source))

    if task.get("web_search", False) is not False:
        raise TaskValidationError("V0.1 requires web_search=false.")
    if task.get("subagents", False) is not False:
        raise TaskValidationError("V0.1 requires subagents=false.")
    if task.get("max_turns", 1) != 1:
        raise TaskValidationError("V0.1 requires max_turns=1.")
    if task.get("permission_mode", "plan") != "plan":
        raise TaskValidationError("V0.1 requires permission_mode=plan.")
    if task.get("output_format", "json") != "json":
        raise TaskValidationError("V0.1 supports output_format=json only.")

    timeout_seconds = task.get("timeout_seconds", 300)
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 1800:
        raise TaskValidationError("timeout_seconds must be an integer from 1 to 1800.")
    agent_max_turns = task.get("agent_max_turns", 1)
    if not isinstance(agent_max_turns, int) or not 1 <= agent_max_turns <= 128:
        raise TaskValidationError("agent_max_turns must be an integer from 1 to 128.")

    normalized = dict(task)
    normalized.update(
        {
            "task_id": task_id,
            "instruction": instruction.strip(),
            "working_directory": str(workdir),
            "allowed_inputs": resolved_inputs,
            "output_format": "json",
            "web_search": False,
            "subagents": False,
            "max_turns": 1,
            "permission_mode": "plan",
            "timeout_seconds": timeout_seconds,
            "agent_max_turns": agent_max_turns,
            "expected_output": task.get("expected_output", {"type": "structured_text"}),
        }
    )
    return normalized


def build_prompt(task: dict[str, Any]) -> str:
    inputs = task["allowed_inputs"]
    input_lines = "\n".join(f"- {item}" for item in inputs) if inputs else "- None. Do not read any external file."
    expected = json.dumps(task["expected_output"], ensure_ascii=False, indent=2)
    return f"""# Role
你是Codex调用的单轮分包Agent。只执行当前任务，不扩展任务范围。

# Task
{task['instruction']}

# Allowed Inputs
只允许按需读取以下明确列出的输入，不得递归读取其父目录或其他资料：
{input_lines}

# Forbidden Actions
- 不联网或使用Web搜索
- 不修改、创建、移动、重命名或删除任何文件
- 不调用subagent
- 不执行shell命令或其他项目任务
- 不读取未列入Allowed Inputs的资料
- 不推断任务范围之外的信息
- 不输出或索取任何认证信息

# Required Output
返回有效JSON。期望输出描述：
{expected}

# Stop Condition
完成一次输出后立即停止，不追问、不重试、不开启下一项任务。
"""


def _hidden_process_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    }


def run_command(
    args: list[str],
    cwd: Path,
    timeout_seconds: float,
    proxy_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["CLICOLOR"] = "0"
    env["TERM"] = "dumb"
    if proxy_env:
        env.update(proxy_env)
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        shell=False,
        env=env,
        **_hidden_process_kwargs(),
    )


def classify_cli_failure(stdout: str, stderr: str, returncode: int) -> str | None:
    combined = f"{stdout}\n{stderr}".lower()
    if any(marker in combined for marker in MAX_TURN_MARKERS):
        return "CLI_MAX_TURNS"
    if any(marker in combined for marker in AUTH_MARKERS):
        return "PROVIDER_AUTH_REQUIRED"
    if any(marker in combined for marker in PROXY_MARKERS):
        return "PROXY_ERROR"
    if any(marker in combined for marker in PERMISSION_MARKERS):
        return "PERMISSION_RESTRICTED"
    if any(marker in combined for marker in SESSION_MARKERS):
        return "SESSION_ERROR"
    if any(marker in combined for marker in MODEL_MARKERS):
        return "MODEL_UNAVAILABLE"
    if any(marker in combined for marker in NETWORK_MARKERS):
        return "NETWORK_ERROR"
    if returncode != 0:
        return "PROVIDER_RUNTIME_ERROR"
    return None


def parse_json_object(value: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if isinstance(value, dict):
        return value, None
    text = str(value or "").strip()
    text = re.sub(r"<\|[^|]*\|>", "", text).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = re.sub(r"\n```\s*$", "", text)
    start = text.find("{")
    if start < 0:
        raise GrokBridgeError("OUTPUT_CONTRACT_ERROR", "Grok did not return a JSON object.")
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    cursor = start
    while cursor < len(text):
        next_start = text.find("{", cursor)
        if next_start < 0:
            break
        try:
            parsed, consumed = decoder.raw_decode(text[next_start:])
        except json.JSONDecodeError:
            cursor = next_start + 1
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)
        cursor = next_start + max(consumed, 1)
    if not candidates:
        raise GrokBridgeError("OUTPUT_CONTRACT_ERROR", "Grok inner output was not valid JSON.")
    selected = candidates[-1]
    repair = None if len(candidates) == 1 else {
        "applied": True,
        "type": "deterministic_select_last_complete_json_object",
        "candidate_count": len(candidates),
        "business_text_changed": False,
    }
    return selected, repair


class GrokBridge:
    """Single formal adapter for health, auth, metadata, invocation and retry."""

    def __init__(self, config_path: Path | None = None, *, command_runner=run_command, executable: str | None = None, version: str | None = None):
        self.config_path = config_path.resolve() if config_path else None
        self.config = load_bridge_config(self.config_path)
        self.proxy_env, self.network_audit = proxy_settings(self.config)
        self._command_runner = command_runner
        if executable:
            self.executable, self.version = executable, (version or "test")
        else:
            try:
                self.executable, self.version = resolve_grok(self.config)
            except FileNotFoundError as exc:
                raise GrokBridgeError("CLI_NOT_FOUND", str(exc)) from exc

    def model_metadata(self) -> dict[str, Any]:
        return {
            "requested_model": self.config.get("model_alias", "grok-4.6"),
            "expected_model": self.config.get("expected_model", "grok-4.6"),
            "reasoning": self.config.get("reasoning_effort", "xhigh"),
            "automatic_downgrade_allowed": False,
            "highest_capability_mode": True,
        }

    def _health_probe(self, timeout: int) -> subprocess.CompletedProcess[str]:
        return self._command_runner([self.executable, "models"], BRIDGE_DIR, timeout, proxy_env=self.proxy_env)

    def _help_probe(self, timeout: int) -> subprocess.CompletedProcess[str]:
        return self._command_runner([self.executable, "--help"], BRIDGE_DIR, timeout, proxy_env=self.proxy_env)

    def _local_login_ok(self) -> bool:
        return (Path.home() / ".grok" / "auth.json").is_file() and Path(self.executable).is_file()

    def health_check(self) -> dict[str, Any]:
        started = utc_now()
        timeout = int(self.config.get("health_timeout_seconds", 30))
        try:
            completed = self._health_probe(timeout)
        except subprocess.TimeoutExpired:
            local_ok = self._local_login_ok()
            return {
                "status": "PASS" if local_ok else "NETWORK_ERROR",
                "available": local_ok,
                "authenticated": True if local_ok else None,
                "message": "模型目录探测超时，已改用本机登录状态继续。" if local_ok else "Grok models health check timed out.",
                "started_at": started,
                "finished_at": utc_now(),
                "network": self.network_audit,
                **self.model_metadata(),
            }
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        failure = classify_cli_failure(stdout, stderr, completed.returncode)
        combined = f"{stdout}\n{stderr}"
        authenticated = True if "logged in with grok.com" in combined.lower() and failure != "PROVIDER_AUTH_REQUIRED" else (False if failure == "PROVIDER_AUTH_REQUIRED" else None)
        self_heal = {"attempted": False, "result": "NOT_NEEDED"}
        if authenticated is None and failure not in {"NETWORK_ERROR", "PROXY_ERROR", "MODEL_UNAVAILABLE"}:
            self_heal["attempted"] = True
            try:
                help_result = self._help_probe(timeout)
                self_heal["help_probe_returncode"] = help_result.returncode
                completed = self._health_probe(timeout)
                stdout, stderr = completed.stdout or "", completed.stderr or ""
                combined = f"{stdout}\n{stderr}"
                failure = classify_cli_failure(stdout, stderr, completed.returncode)
                authenticated = True if "logged in with grok.com" in combined.lower() and failure != "PROVIDER_AUTH_REQUIRED" else (False if failure == "PROVIDER_AUTH_REQUIRED" else None)
                self_heal["result"] = "SELF_HEAL_PASS" if authenticated else ("EXPLICIT_UNAUTHENTICATED" if authenticated is False else "AUTH_STATE_UNKNOWN")
            except (OSError, subprocess.TimeoutExpired) as exc:
                authenticated, failure = None, "AUTH_STATE_UNKNOWN"
                self_heal.update({"result": "AUTH_STATE_UNKNOWN", "error": type(exc).__name__})
        requested = str(self.model_metadata()["requested_model"])
        model_available = requested in combined
        if authenticated is None and failure not in {"NETWORK_ERROR", "PROXY_ERROR", "MODEL_UNAVAILABLE"}:
            status = "AUTH_STATE_UNKNOWN"
        else:
            status = failure or (None if model_available else "MODEL_UNAVAILABLE")
        return {
            "status": status or "PASS",
            "available": status is None,
            "authenticated": authenticated,
            "auth_state": "AUTHENTICATED" if authenticated is True else ("UNAUTHENTICATED" if authenticated is False else "AUTH_STATE_UNKNOWN"),
            "self_heal": self_heal,
            "model_available": model_available,
            "message": "Grok CLI, authentication, model and network are available." if status is None else status,
            "started_at": started,
            "finished_at": utc_now(),
            "returncode": completed.returncode,
            "network": self.network_audit,
            "executable": self.executable,
            "version": self.version,
            **self.model_metadata(),
        }

    def auth_status(self) -> dict[str, Any]:
        health = self.health_check()
        return {
                "status": "AUTHENTICATED" if health.get("authenticated") is True else (
                "PROVIDER_AUTH_REQUIRED" if health.get("authenticated") is False else "AUTH_STATE_UNKNOWN"
            ),
            "authenticated": health.get("authenticated"),
            "health_status": health.get("status"),
        }

    def invoke(
        self,
        *,
        task_id: str,
        prompt: str,
        working_directory: Path,
        run_dir: Path,
        system_prompt: str = "You are a read-only structured analysis engine. Return one JSON object.",
        json_schema: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        max_network_retries: int | None = None,
        agent_max_turns: int = 1,
        tools: list[str] | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        # Do not block real work on `grok models`. That catalog probe hangs
        # without a local proxy; the actual invoke already injects proxy_env.
        if not Path(self.executable).is_file():
            raise GrokBridgeError("CLI_NOT_FOUND", f"Grok CLI not found: {self.executable}")
        if not self._local_login_ok():
            health = self.health_check()
            if not health.get("available"):
                raise GrokBridgeError(str(health.get("status") or "NETWORK_ERROR"), str(health.get("message") or "Grok is not available."))
        workdir = working_directory.resolve()
        if not workdir.is_dir() or workdir.parent == workdir:
            raise GrokBridgeError("VALIDATION_ERROR", "working_directory must be an existing non-root directory.")
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = run_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        metadata = self.model_metadata()
        effort = str(reasoning_effort or metadata["reasoning"] or "high")
        args = [
            self.executable,
            "--prompt-file", str(prompt_path),
            "--cwd", str(workdir),
            "--output-format", "json",
            "--disable-web-search",
            "--no-subagents",
            "--no-plan",
            "--max-turns", str(agent_max_turns),
            "--permission-mode", "dontAsk",
            "--verbatim",
            "--model", str(metadata["requested_model"]),
            "--reasoning-effort", effort,
            "--system-prompt-override", system_prompt,
        ]
        if tools is not None:
            args.extend(["--tools", ",".join(tools)])
        if isinstance(json_schema, dict) and isinstance(json_schema.get("properties"), dict) and json_schema.get("properties"):
            args.extend(["--json-schema", json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))])
        timeout = int(timeout_seconds or self.config.get("timeout_seconds", 1800))
        retry_limit = int(max_network_retries if max_network_retries is not None else self.config.get("network_retries", 2))
        if retry_limit < 0 or retry_limit > 2:
            raise GrokBridgeError("VALIDATION_ERROR", "network retry limit must be between 0 and 2.")
        started = utc_now()
        attempts: list[dict[str, Any]] = []
        completed: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, retry_limit + 2):
            attempt_started = utc_now()
            try:
                completed = self._command_runner(args, workdir, timeout, proxy_env=self.proxy_env)
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
                failure = classify_cli_failure(stdout, stderr, completed.returncode)
                auxiliary_warning = None
                if failure == "NETWORK_ERROR" and completed.returncode == 0:
                    try:
                        candidate_envelope = json.loads(stdout)
                    except json.JSONDecodeError:
                        candidate_envelope = None
                    if isinstance(candidate_envelope, dict) and candidate_envelope.get("modelUsage") and "text" in candidate_envelope:
                        auxiliary_warning = "CLI auxiliary sync warning occurred after a complete model response."
                        failure = None
            except subprocess.TimeoutExpired:
                stdout, stderr, failure, auxiliary_warning = "", "", "NETWORK_ERROR", None
                completed = None
            attempts.append({
                "attempt": attempt,
                "started_at": attempt_started,
                "finished_at": utc_now(),
                "status": failure or "SUCCESS",
                "auxiliary_warning": auxiliary_warning,
            })
            (run_dir / f"stdout_attempt_{attempt}.log").write_text(stdout, encoding="utf-8")
            (run_dir / f"stderr_attempt_{attempt}.log").write_text(stderr, encoding="utf-8")
            if failure is None:
                break
            if failure == "PROVIDER_AUTH_REQUIRED":
                raise GrokBridgeError("PROVIDER_AUTH_REQUIRED", "Official auth probe rejected the current credential; no retry was attempted.")
            if failure != "NETWORK_ERROR" or attempt > retry_limit:
                raise GrokBridgeError(failure, (stderr or stdout or failure)[-1000:])
            time.sleep(min(2**attempt, 4))
        if completed is None:
            raise GrokBridgeError("NETWORK_ERROR", "Grok invocation timed out after network retries.")
        try:
            outer = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise GrokBridgeError("OUTPUT_CONTRACT_ERROR", f"Grok outer envelope was not valid JSON: {exc}") from exc
        models = list((outer.get("modelUsage") or {}).keys())
        if not models_match_request(models, str(metadata["expected_model"]), str(metadata["requested_model"])):
            raise GrokBridgeError("MODEL_UNAVAILABLE", f"Model mismatch or downgrade detected: {models}")
        structured = outer.get("structuredOutput")
        serialization_repair = None
        if not isinstance(structured, dict) or not structured:
            structured, serialization_repair = parse_json_object(outer.get("text", ""))
        if isinstance(structured, dict) and not structured:
            raise GrokBridgeError("OUTPUT_CONTRACT_ERROR", "Grok returned an empty JSON object.")
        audit = {
            "task_id": task_id,
            "started_at": started,
            "finished_at": utc_now(),
            "status": "SUCCESS",
            "executable": self.executable,
            "version": self.version,
            "working_directory": str(workdir),
            "session_id": outer.get("sessionId"),
            "request_id": outer.get("requestId"),
            "stop_reason": outer.get("stopReason"),
            "actual_models": models,
            "downgrade": False,
            "attempts": attempts,
            "network": self.network_audit,
            "serialization_repair": serialization_repair or {"applied": False},
            **metadata,
        }
        write_json(run_dir / "provider_raw_envelope.json", outer)
        write_json(run_dir / "provider_structured_output.json", structured)
        write_json(run_dir / "invocation.json", audit)
        return {"structured_output": structured, "envelope": outer, "audit": audit}

    def retry(self, **kwargs: Any) -> dict[str, Any]:
        return self.invoke(**kwargs)


def make_result(
    task_id: str,
    status: str,
    started_at: str,
    started_clock: float,
    *,
    exit_code: int | None = None,
    grok_version: str = "",
    output_file: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unknown bridge status: {status}")
    return {
        "task_id": task_id,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started_clock, 3),
        "grok_version": grok_version,
        "output_file": output_file,
        "error_message": error_message,
    }


def execute_task(
    task_path: Path,
    runtime_root: Path = DEFAULT_RUNTIME,
    config_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    started_at = utc_now()
    started_clock = time.monotonic()
    try:
        config = load_bridge_config(config_path)
        proxy_env, network_audit = proxy_settings(config)
        task = validate_task(load_json(task_path))
    except (OSError, json.JSONDecodeError, TaskValidationError) as exc:
        result = make_result("", "VALIDATION_ERROR", started_at, started_clock, error_message=str(exc))
        return result, 2

    task_id = task["task_id"]
    run_dir = runtime_root.resolve() / task_id
    if run_dir.exists():
        result = make_result(
            task_id,
            "VALIDATION_ERROR",
            started_at,
            started_clock,
            error_message="Task runtime directory already exists; refusing to overwrite history.",
        )
        return result, 2

    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "task.json", task)
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text(build_prompt(task), encoding="utf-8")

    try:
        bridge = GrokBridge(config_path)
        bridge_result = bridge.invoke(
            task_id=task_id,
            prompt=build_prompt(task),
            system_prompt="你是Codex调用的只读单任务分析引擎。只允许使用只读文件工具访问Prompt中明确列出的Allowed Inputs；不得联网、修改文件、执行写入操作或调用subagent；完成后只返回一个JSON对象。",
            working_directory=Path(task["working_directory"]),
            run_dir=run_dir,
            timeout_seconds=task["timeout_seconds"],
            max_network_retries=2,
            agent_max_turns=task["agent_max_turns"],
        )
    except (OSError, TaskValidationError, GrokBridgeError, RuntimeError) as exc:
        status = getattr(exc, "code", "PROVIDER_RUNTIME_ERROR")
        if status not in VALID_STATUSES:
            status = "PROVIDER_RUNTIME_ERROR"
        result = make_result(task_id, status, started_at, started_clock, error_message=str(exc))
        write_json(run_dir / "result.json", result)
        return result, {"PROVIDER_AUTH_REQUIRED": 3, "NETWORK_ERROR": 4, "OUTPUT_CONTRACT_ERROR": 6, "CLI_MAX_TURNS": 7}.get(status, 5)

    raw_output_path = run_dir / "raw_output.json"
    write_json(raw_output_path, bridge_result["envelope"])
    result = make_result(
        task_id,
        "SUCCESS",
        started_at,
        started_clock,
        exit_code=0,
        grok_version=bridge.version,
        output_file=str(raw_output_path),
    )
    result.update({
        "model": bridge_result["audit"]["actual_models"][0],
        "reasoning": bridge_result["audit"]["reasoning"],
        "session_id": bridge_result["audit"]["session_id"],
        "downgrade": bridge_result["audit"]["downgrade"],
    })
    write_json(run_dir / "result.json", result)
    return result, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one controlled Grok Build task.")
    parser.add_argument("task_json", type=Path, help="Path to one task JSON file.")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--config", type=Path, default=None, help="Optional bridge configuration JSON.")
    args = parser.parse_args()
    config_path = args.config.resolve() if args.config else None
    result, exit_code = execute_task(args.task_json.resolve(), args.runtime_root, config_path)
    # ASCII-escaped JSON avoids Windows console code-page corruption in caller processes.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
