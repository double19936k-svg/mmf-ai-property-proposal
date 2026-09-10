from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import traceback
import urllib.error
import urllib.request
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .base import AIProvider, ProviderError, ProviderUnavailableError, normalize_generation, normalize_recommendation, parse_json_object, write_json

CURRENT_RUN_ID: ContextVar[str] = ContextVar("CURRENT_RUN_ID", default="")
LAST_NETWORK_ROUTE: ContextVar[str] = ContextVar("LAST_NETWORK_ROUTE", default="unknown")
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_ACTIVE_CONNS: dict[str, list] = {}
_CANCEL_LOCK = threading.Lock()


class GenerationCancelled(ProviderUnavailableError):
    pass


def bind_run(run_id: str) -> threading.Event:
    event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_EVENTS[run_id] = event
        _ACTIVE_CONNS.setdefault(run_id, [])
    CURRENT_RUN_ID.set(run_id)
    return event


def unbind_run(run_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(run_id, None)
        _ACTIVE_CONNS.pop(run_id, None)
    if CURRENT_RUN_ID.get() == run_id:
        CURRENT_RUN_ID.set("")


def is_cancelled(run_id: str | None = None) -> bool:
    rid = run_id or CURRENT_RUN_ID.get()
    event = _CANCEL_EVENTS.get(rid or "")
    return bool(event and event.is_set())


def abort_run_http(run_id: str) -> None:
    with _CANCEL_LOCK:
        event = _CANCEL_EVENTS.get(run_id)
        conns = list(_ACTIVE_CONNS.get(run_id) or [])
    if event:
        event.set()
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass

DIRECT_HOST_SUFFIXES = (
    "aliyuncs.com",
    "moonshot.cn",
    "moonshot.ai",
    "bigmodel.cn",
    "zhipuai.cn",
    "z.ai",
)
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def normalize_openai_base_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if "://" not in raw:
        raw = "https://" + raw
    raw = raw.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
    host = (urlparse(raw).hostname or "").lower()
    if "maas.aliyuncs.com" in host or "dashscope.aliyuncs.com" in host:
        for junk in (
            "/api/v2/apps/protocols/compatible-mode/v1",
            "/compatible-mode/v1",
            "/api/v1",
            "/v1",
        ):
            if raw.endswith(junk):
                raw = raw[: -len(junk)].rstrip("/")
                break
        raw = raw.rstrip("/") + "/compatible-mode/v1"
    if "bigmodel.cn" in host and "/paas/v4" not in raw:
        raw = raw.rstrip("/") + "/api/paas/v4"
    if host in {"api.z.ai", "z.ai"} or host.endswith(".z.ai"):
        if "/paas/v4" not in raw and "/coding/paas/v4" not in raw:
            raw = raw.rstrip("/") + "/api/paas/v4"
    return raw


def _direct_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == item or host.endswith("." + item) for item in DIRECT_HOST_SUFFIXES)


def _winerror(exc: BaseException) -> int | None:
    current: BaseException | Any = exc
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "winerror", None)
        if isinstance(value, int):
            return value
        reason = getattr(current, "reason", None)
        current = reason if isinstance(reason, BaseException) else getattr(current, "__cause__", None)
    return None


def _fallback_proxy_url(scheme: str) -> str:
    keys = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy") if scheme == "https" else ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    for key in keys:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    # The launcher normally exports a detected proxy. If Windows denies its
    # process-enumeration check, probe only the same fixed loopback ports; never
    # bind or listen, and never scan arbitrary hosts or ports.
    for port in (7897, 7890, 10809, 10808, 6152, 8888):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return ""


class _HttpResponse:
    def __init__(self, status: int, data: bytes):
        self.status = status
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *_args) -> bool:
        return False


def _http_open(request: urllib.request.Request, timeout: int):
    url = request.full_url
    run_id = CURRENT_RUN_ID.get()
    if run_id and is_cancelled(run_id):
        raise GenerationCancelled("已停止生成")
    parsed = urlparse(url)
    if _direct_host(url) and parsed.scheme == "https":
        conn = http.client.HTTPSConnection(
            parsed.hostname or "",
            parsed.port or 443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        if run_id:
            with _CANCEL_LOCK:
                _ACTIVE_CONNS.setdefault(run_id, []).append(conn)
        try:
            LAST_NETWORK_ROUTE.set("direct")
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn.request(request.get_method() or "GET", path, request.data, dict(request.header_items()))
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
        except Exception as exc:
            if run_id and is_cancelled(run_id):
                raise GenerationCancelled("已停止生成") from exc
            # Some Windows/VPN policies reject direct outbound sockets with
            # WSAEACCES (10013) while the launcher-discovered local proxy is
            # available. The request has not reached the remote endpoint, so a
            # single proxy retry is safe and does not duplicate a provider call.
            proxy_url = _fallback_proxy_url(parsed.scheme) if _winerror(exc) == 10013 else ""
            if proxy_url:
                LAST_NETWORK_ROUTE.set("proxy_fallback_after_winerror_10013")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({parsed.scheme: proxy_url}))
                return opener.open(request, timeout=timeout)
            raise
        finally:
            if run_id:
                with _CANCEL_LOCK:
                    items = _ACTIVE_CONNS.get(run_id) or []
                    if conn in items:
                        items.remove(conn)
            try:
                conn.close()
            except Exception:
                pass
        if not (200 <= status < 300):
            raise urllib.error.HTTPError(url, status, "", None, BytesIO(data))
        return _HttpResponse(status, data)
    LAST_NETWORK_ROUTE.set("direct_no_proxy" if _direct_host(url) else "environment_proxy_policy")
    opener = _DIRECT_OPENER if _direct_host(url) else urllib.request.build_opener()
    return opener.open(request, timeout=timeout)


def _http_error_message(exc: BaseException, url: str, engine: str = "当前引擎", body: str = "") -> str:
    if isinstance(exc, GenerationCancelled) or "已停止生成" in str(exc):
        return "已停止生成"
    if isinstance(exc, urllib.error.HTTPError):
        if not body:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
        snippet = " ".join(body.split())[:180]
        blob = f"{snippet} {exc}".lower()
        if exc.code in {404, 405}:
            if engine == "千问":
                return "千问接口地址不正确。百炼/Model Studio 的API地址应类似 https://业务空间.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            return f"{engine}接口地址不正确，请到AI引擎设置检查API地址"
        if exc.code == 401 or any(token in blob for token in ("invalid api key", "incorrect api key", "invalid token", "unauthorized", "login required", "expired credential")):
            return f"{engine}密钥无效或没有当前模型权限，请到AI引擎设置重新填写并测试连接"
        if exc.code == 403:
            if "api-key restriction" in blob or "access_denied" in blob:
                return f"{engine}当前密钥有访问限制（不是登录过期）。常见原因是百炼控制台给该Key加了IP白名单、未开通当前模型，或限制了兼容模式。请改用Kimi或Grok继续，或到百炼检查该Key的限制条件。"
            return f"{engine}拒绝了本次请求。可换一个引擎继续，不必重新登录。"
        if exc.code == 400:
            return f"{engine}拒绝了本次请求（HTTP 400）{('：' + snippet) if snippet else ''}"
        if exc.code == 429:
            return f"{engine}请求过于频繁或额度不足，请稍后再试"
        return f"{engine}接口返回 HTTP {exc.code}{('：' + snippet) if snippet else ''}"
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return f"调用{engine}超时，未能完成AI解析。请检查网络后重试"
    if _winerror(exc) == 10013:
        return f"{engine}网络连接被Windows或网络策略阻止（WinError 10013），不是密钥或登录问题。请检查本机代理、防火墙或网络权限后重试"
    return "网络连接失败，未能完成AI解析"


class OpenAICompatibleProvider(AIProvider):
    provider_version = "0.1"

    def _api_key(self) -> tuple[str, str]:
        name = str(self.config.get("api_key_env", "")).strip()
        if self.credential_store:
            return self.credential_store.get(self.config["provider_name"], name)
        value = os.environ.get(name, "").strip() if name else ""
        return value, "environment" if value else "none"

    def _base_url(self) -> str:
        return normalize_openai_base_url(str(self.config.get("base_url", "")))

    def _engine_label(self) -> str:
        name = str(self.config.get("provider_name") or "")
        display = str(self.config.get("display_name") or "")
        blob = f"{name} {display}".lower()
        if "qwen" in blob or "千问" in display:
            return "千问"
        if "kimi" in blob or "moonshot" in blob:
            return "Kimi"
        if "glm" in blob or "zhipu" in blob or "智谱" in display:
            return "智谱GLM"
        if "grok" in blob:
            return "Grok"
        short = display.split("（")[0].strip()
        return short or "当前引擎"

    def _configured(self) -> bool:
        api_key, _ = self._api_key()
        return bool(self.config.get("enabled", False) and self._base_url() and api_key and self.config.get("model"))

    def health_check(self) -> dict[str, Any]:
        api_key, credential_source = self._api_key()
        configured = self._configured()
        base = {"configured": configured, "credential_source": credential_source, "metadata": self.get_metadata()}
        if not configured:
            return {**base, "available": False, "status": "not_configured", "message": "未配置API地址、模型或密钥环境变量"}
        request = urllib.request.Request(
            self._base_url() + "/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        models: list[str] = []
        rate_headers: dict[str, Any] = {}
        try:
            with _http_open(request, timeout=int(self.config.get("health_timeout", 15))) as response:
                available = 200 <= response.status < 300
                raw = response.read() if hasattr(response, "read") else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                rows = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(rows, list):
                    models = [str(item.get("id") or "") for item in rows if isinstance(item, dict) and item.get("id")]
                headers = getattr(response, "headers", None)
                if headers:
                    for key in ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens", "retry-after"):
                        if key in headers:
                            rate_headers[key] = headers.get(key)
        except urllib.error.HTTPError as exc:
            available = False
            code = int(getattr(exc, "code", 0) or 0)
            from .capability_probe import classify_probe_failure
            status = classify_probe_failure(exc, str(exc), code).lower()
            return {**base, "available": False, "status": status, "message": _http_error_message(exc, self._base_url(), self._engine_label()), "http_status": code}
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            available = False
            from .capability_probe import classify_probe_failure
            status = classify_probe_failure(exc, str(exc)).lower()
            return {**base, "available": False, "status": status, "message": _http_error_message(exc, self._base_url(), self._engine_label())}
        return {
            **base,
            "available": available,
            "status": "available" if available else "unavailable",
            "message": "可用" if available else "API健康检查失败",
            "models": models,
            "rate_limit_headers": rate_headers or "unknown",
        }

    def _invoke(self, request: dict[str, Any], task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._configured():
            raise ProviderUnavailableError("OpenAI兼容Provider当前不可用。")
        task_dir.mkdir(parents=True, exist_ok=True)
        extra = dict(self.config.get("extra_options") or {})
        # Scheduler/deployment controls are local policy, not vendor API fields.
        for local_key in ("rpm", "tpm", "latency_budget", "soft_latency_budget", "hard_timeout", "supported_reasoning_levels"):
            extra.pop(local_key, None)
        required = set(request.get("required_keys") or [])
        schema = request.get("json_schema")
        if not required and isinstance(schema, dict):
            required = {str(item) for item in (schema.get("required") or []) if item}
        extra.setdefault("response_format", {"type": "json_object"})
        extra.pop("temperature", None)
        host = self._base_url().lower()
        dashscope = "dashscope" in host or "maas.aliyuncs.com" in host
        moonshot = "moonshot" in host
        zhipu = "bigmodel.cn" in host or "zhipuai" in host or host.endswith(".z.ai") or host in {"api.z.ai", "z.ai"}
        if "artifact" in required:
            extra["max_tokens"] = int(self.config.get("max_tokens") or 16384)
        extra.pop("enable_thinking", None)
        extra.pop("thinking", None)
        longform = request.get("generation_mode") in {"longform_section", "longform_batch"}
        if dashscope:
            thinking = request.get("enable_thinking")
            extra["enable_thinking"] = bool(thinking) if thinking is not None else False
        if moonshot:
            extra.pop("enable_thinking", None)
            extra["reasoning_effort"] = request.get("reasoning_effort") or ("high" if longform else "low")
        if zhipu:
            extra.pop("enable_thinking", None)
            effort = request.get("reasoning_effort") or ("high" if longform else "low")
            extra["reasoning_effort"] = effort
            extra["thinking"] = {"type": "enabled" if effort in {"high", "max"} else "disabled"}
        if longform or request.get("generation_mode") == "capability_probe":
            extra["max_tokens"] = int(request.get("max_tokens") or self.config.get("max_tokens") or 8192)
        body = {
            "model": self.config["model"],
            "messages": [
                {"role": "system", "content": request["system_prompt"]},
                {"role": "user", "content": request["prompt"]},
            ],
            **extra,
        }
        if not moonshot:
            body["temperature"] = self.config.get("temperature", 0.2)
        write_json(task_dir / "request_without_key.json", body)
        api_key, _ = self._api_key()
        url = self._base_url() + "/chat/completions"
        timeout = int(request.get("timeout_seconds") or self.config.get("timeout", 300))
        if "artifact" not in required and not longform:
            timeout = min(timeout, 180)
        http_request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with _http_open(http_request, timeout=timeout) as response:
                outer = json.loads(response.read().decode("utf-8"))
        except GenerationCancelled:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
            if is_cancelled():
                raise GenerationCancelled("已停止生成") from exc
            body_text = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    body_text = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
            try:
                write_json(task_dir / "provider_http_error.json", {
                    "url": url,
                    "error": str(exc)[:400],
                    "error_type": type(exc).__name__,
                    "status": getattr(exc, "code", None),
                    "body": body_text[:800],
                    "network_stage": "TCP_CONNECT" if _winerror(exc) == 10013 else "HTTP_CLIENT",
                    "network_route": LAST_NETWORK_ROUTE.get(),
                    "winerror": _winerror(exc),
                    "traceback": traceback.format_exc()[:6000],
                })
            except Exception:
                pass
            retry_after = None
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                try:
                    retry_after = float((getattr(exc, "headers", None) or {}).get("Retry-After") or 0) or None
                except (TypeError, ValueError):
                    retry_after = None
                raise ProviderUnavailableError(_http_error_message(exc, url, self._engine_label(), body=body_text), error_code="RATE_LIMIT", retry_after=retry_after) from exc
            error_code = "NETWORK_SOCKET_PERMISSION_ERROR" if _winerror(exc) == 10013 else "PROVIDER_UNAVAILABLE"
            raise ProviderUnavailableError(_http_error_message(exc, url, self._engine_label(), body=body_text), error_code=error_code) from exc
        write_json(task_dir / "provider_raw_envelope.json", outer)
        try:
            message = outer["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("当前AI引擎返回结构不受支持。") from exc
        text = message.get("content")
        if not str(text or "").strip():
            text = message.get("reasoning_content") or message
        defaults = {}
        if request.get("chunk_id"):
            defaults["chunk_id"] = request["chunk_id"]
        prompt = str(request.get("prompt") or "")
        if "chunk_id=" in prompt and "chunk_id" not in defaults:
            defaults["chunk_id"] = prompt.split("chunk_id=", 1)[1].split("\n", 1)[0].strip()
        try:
            result, repair = parse_json_object(text, required, defaults)
        except ProviderError:
            raise ProviderError("模型返回格式不完整。请重试解析；已保留本地文件提取结果。", error_code="OUTPUT_CONTRACT_ERROR") from None
        choice = outer["choices"][0] if isinstance(outer.get("choices"), list) and outer["choices"] else {}
        usage = outer.get("usage") if isinstance(outer.get("usage"), dict) else {}
        metadata = self._task_metadata(
            request["task_id"],
            serialization_repair=repair or {"applied": False},
            finish_reason=choice.get("finish_reason") or "stop",
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            requested_settings=(request.get("capability_profile") or {}).get("requested_settings"),
            effective_settings=(request.get("capability_profile") or {}).get("effective_settings"),
            network_route=LAST_NETWORK_ROUTE.get(),
        )
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
