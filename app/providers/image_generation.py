from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .base import AIProvider, ProviderUnavailableError, write_json


def normalize_image_base_url(value: str, backend: str = "dashscope_wanx") -> str:
    """Normalize an image API root without applying chat-completions paths."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/")
    backend = str(backend or "dashscope_wanx")
    if backend == "dashscope_wanx":
        marker = "/api/v1"
        return origin + (path[: path.find(marker) + len(marker)] if marker in path else marker)
    if backend == "zhipu_cogview":
        marker = "/api/paas/v4"
        return origin + (path[: path.find(marker) + len(marker)] if marker in path else marker)
    if backend == "xai_imagine":
        marker = "/v1"
        return origin + (path[: path.find(marker) + len(marker)] if marker in path else marker)
    return raw.rstrip("/")


class ImageGenerationProvider(AIProvider):
    provider_version = "0.1-image"

    def _api_key(self) -> tuple[str, str]:
        env_name = str(self.config.get("api_key_env", "")).strip()
        # 配置了 reuse_text_provider 时优先复用文字 Provider 的凭据（同一账号共用 Key），
        # 避免本地遗留的失效独立 Key 遮蔽复用逻辑。
        reuse = str(self.config.get("extra_options", {}).get("reuse_text_provider") or "").strip()
        if reuse and self.credential_store:
            value, source = self.credential_store.get(reuse, env_name)
            if value:
                return value, f"{source}:reused_from_{reuse}"
        if self.credential_store:
            value, source = self.credential_store.get(self.config["provider_name"], env_name)
            if value:
                return value, source
        if env_name and os.environ.get(env_name, "").strip():
            return os.environ[env_name].strip(), "environment"
        return "", "none"

    def _backend(self) -> str:
        extra = self.config.get("extra_options") or {}
        return str(extra.get("backend") or "dashscope_wanx")

    def _base_url(self) -> str:
        defaults = {
            "dashscope_wanx": "https://dashscope.aliyuncs.com/api/v1",
            "zhipu_cogview": "https://open.bigmodel.cn/api/paas/v4",
            "xai_imagine": "https://api.x.ai/v1",
        }
        return normalize_image_base_url(
            str(self.config.get("base_url") or defaults.get(self._backend()) or ""),
            self._backend(),
        )

    def recommend_knowledge(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        raise ProviderUnavailableError("AI图片服务不能用于文字方案生成，请选择千问或其他文字引擎。")

    def generate_solution(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        raise ProviderUnavailableError("AI图片服务不能用于文字方案生成，请选择千问或其他文字引擎。")

    def health_check(self) -> dict[str, Any]:
        api_key, credential_source = self._api_key()
        enabled = bool(self.config.get("enabled", False))
        configured = enabled and bool(self.config.get("model")) and bool(api_key)
        base = {"configured": configured, "credential_source": credential_source, "metadata": self.get_metadata()}
        if not enabled:
            return {**base, "available": False, "status": "not_configured", "message": "未启用"}
        if not configured:
            return {**base, "available": False, "status": "not_configured", "message": "尚未配置模型或API Key"}
        url = self._health_url()
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        models: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=int(self.config.get("health_timeout", 20))) as response:
                available = 200 <= response.status < 300
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {}
                rows = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    output = payload.get("output") if isinstance(payload, dict) else None
                    rows = output.get("models") if isinstance(output, dict) else None
                if isinstance(rows, list):
                    models = [
                        str(item.get("id") or item.get("model") or item.get("model_id") or "")
                        for item in rows if isinstance(item, dict) and (item.get("id") or item.get("model") or item.get("model_id"))
                    ]
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                return {**base, "available": False, "status": "authentication_required", "message": "图片服务密钥无效或需要重新填写"}
            available = False
        except (OSError, urllib.error.URLError):
            available = False
        return {
            **base,
            "available": available,
            "status": "available" if available else "unavailable",
            "message": "图片服务可用" if available else "图片服务连接失败",
            "models": models,
        }

    def _health_url(self) -> str:
        return self._base_url().rstrip("/") + "/models"

    def generate_image(self, prompt: str, output_dir: Path, *, size: str = "1024*1024") -> dict[str, Any]:
        text = str(prompt or "").strip()
        if not text:
            raise ProviderUnavailableError("请填写图片描述。")
        health = self.health_check()
        if not health.get("available"):
            raise ProviderUnavailableError(health.get("message") or "图片服务尚未配置")
        output_dir.mkdir(parents=True, exist_ok=True)
        if self._backend() == "xai_imagine":
            url, raw = self._xai_generate(text)
        elif self._backend() == "zhipu_cogview":
            url, raw = self._zhipu_generate(text, size)
        else:
            url, raw = self._dashscope_generate(text, size)
        saved = self._download(url, output_dir)
        audit = {
            "provider_name": self.config["provider_name"],
            "model": self.config.get("model"),
            "backend": self._backend(),
            "prompt": text,
            "source_url_host": urlparse(url).hostname,
            "saved_path": str(saved),
        }
        write_json(output_dir / "image_audit.json", {**audit, "raw_status": raw.get("status") or raw.get("task_status") or "ok"})
        return {"ok": True, "path": str(saved), "model": self.config.get("model"), "backend": self._backend()}

    def _dashscope_generate(self, prompt: str, size: str) -> tuple[str, dict[str, Any]]:
        api_key, _ = self._api_key()
        base_url = self._base_url()
        configured_url = str(self.config.get("image_api_url") or "").strip()
        # A workspace-scoped key must stay on its workspace host for task create
        # and polling.  Do not leak back to the public DashScope hostname.
        if "maas.aliyuncs.com" in base_url.lower():
            create_url = base_url + "/services/aigc/text2image/image-synthesis"
        else:
            create_url = configured_url or base_url + "/services/aigc/text2image/image-synthesis"
        body = {
            "model": self.config.get("model") or "wanx-v1",
            "input": {"prompt": prompt},
            "parameters": {"size": size, "n": 1},
        }
        created = self._http_json(create_url, body, api_key, extra_headers={"X-DashScope-Async": "enable"})
        task_id = ((created.get("output") or {}).get("task_id")) or created.get("task_id")
        if not task_id:
            raise ProviderUnavailableError("图片任务创建失败。")
        task_url = base_url + "/tasks/" + str(task_id)
        deadline = time.time() + int(self.config.get("timeout", 180))
        latest: dict[str, Any] = created
        while time.time() < deadline:
            latest = self._http_json(task_url, None, api_key, method="GET")
            status = str((latest.get("output") or {}).get("task_status") or latest.get("task_status") or "")
            if status in {"SUCCEEDED", "SUCCESS"}:
                results = (latest.get("output") or {}).get("results") or []
                url = results[0].get("url") if results else ""
                if not url:
                    raise ProviderUnavailableError("图片生成成功但未返回文件地址。")
                return str(url), latest
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise ProviderUnavailableError("图片生成失败，请稍后重试。")
            time.sleep(2)
        raise ProviderUnavailableError("图片生成超时，请稍后重试。")

    def _xai_generate(self, prompt: str) -> tuple[str, dict[str, Any]]:
        api_key, _ = self._api_key()
        url = self._base_url() + "/images/generations"
        body = {"model": self.config.get("model") or "grok-imagine-image-2.0", "prompt": prompt}
        result = self._http_json(url, body, api_key)
        data = result.get("data") or []
        image_url = data[0].get("url") if data else ""
        if not image_url:
            raise ProviderUnavailableError("图片生成成功但未返回文件地址。")
        return str(image_url), result

    def _zhipu_generate(self, prompt: str, size: str) -> tuple[str, dict[str, Any]]:
        api_key, _ = self._api_key()
        url = self._base_url() + "/images/generations"
        body = {
            "model": self.config.get("model") or "cogview-4-250304",
            "prompt": prompt,
            "size": str(size or "1024*1024").replace("*", "x"),
        }
        result = self._http_json(url, body, api_key)
        data = result.get("data") or []
        image_url = data[0].get("url") if data and isinstance(data[0], dict) else ""
        if not image_url:
            raise ProviderUnavailableError("智谱图片生成成功但未返回文件地址。")
        return str(image_url), result

    def _http_json(self, url: str, body: dict[str, Any] | None, api_key: str, *, method: str | None = None, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=data, method=method or ("GET" if data is None else "POST"), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=int(self.config.get("timeout", 180))) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw else {}
                detail = str((payload.get("error") or {}).get("message") or payload.get("message") or payload.get("code") or "")
            except Exception:
                detail = ""
            if exc.code in {401, 403}:
                raise ProviderUnavailableError("图片服务密钥无效或需要重新填写") from exc
            suffix = f"：{detail[:180]}" if detail else f"（HTTP {exc.code}）"
            raise ProviderUnavailableError("图片服务调用失败" + suffix) from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ProviderUnavailableError("网络连接失败") from exc

    def _download(self, url: str, output_dir: Path) -> Path:
        request = urllib.request.Request(url, headers={"Accept": "image/*,application/octet-stream"})
        with urllib.request.urlopen(request, timeout=60) as response:
            blob = response.read()
            content_type = str(response.headers.get("Content-Type") or "")
        suffix = ".png" if "png" in content_type else ".jpg"
        path = output_dir / f"image_{int(time.time())}{suffix}"
        path.write_bytes(blob)
        return path
