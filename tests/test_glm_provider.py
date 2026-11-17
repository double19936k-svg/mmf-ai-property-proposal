import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from app_core import engine_display
from providers.capability import native_reasoning_sequence, resolve_profile
from providers.openai_compatible import OpenAICompatibleProvider, normalize_openai_base_url


class GlmProvider(unittest.TestCase):
    def test_example_config_includes_glm(self):
        cfg = json.loads((ROOT / "config" / "providers.example.json").read_text(encoding="utf-8-sig"))
        names = [row.get("provider_name") for row in cfg.get("available_providers", [])]
        self.assertIn("glm_zhipu", names)
        glm = next(row for row in cfg["available_providers"] if row["provider_name"] == "glm_zhipu")
        self.assertEqual(glm["provider_type"], "openai_compatible")
        self.assertIn("bigmodel.cn", glm["base_url"])
        self.assertEqual(glm["model"], "glm-5.3-flash")

    def test_base_url_normalization(self):
        self.assertTrue(normalize_openai_base_url("https://open.bigmodel.cn").endswith("/api/paas/v4"))
        self.assertEqual(normalize_openai_base_url("https://open.bigmodel.cn/api/paas/v4"), "https://open.bigmodel.cn/api/paas/v4")
        self.assertTrue(normalize_openai_base_url("https://api.z.ai").endswith("/api/paas/v4"))

    def test_engine_display_and_reasoning(self):
        self.assertEqual(engine_display("glm_zhipu"), "智谱GLM")
        self.assertEqual(native_reasoning_sequence("glm_zhipu", {}), ["low", "high"])
        profile = resolve_profile("glm_zhipu", {"model": "glm-4.7"}, speed_profile="balanced", stage="draft")
        self.assertEqual((profile.get("effective_settings") or {}).get("reasoning_effort"), "high")

    def test_invoke_sends_glm_thinking_fields(self):
        provider = OpenAICompatibleProvider({
            "provider_name": "glm_zhipu",
            "provider_type": "openai_compatible",
            "enabled": True,
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-5.3-flash",
            "display_name": "智谱 GLM",
        })
        captured = []

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()

        def send(request, timeout):
            captured.append(json.loads(request.data))
            return Response()

        with patch.object(provider, "_configured", return_value=True), patch.object(provider, "_api_key", return_value=("test-key", "fixture")), patch("providers.openai_compatible._http_open", side_effect=send):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                provider.invoke_structured({"task_id": "t", "system_prompt": "s", "prompt": "p", "required_keys": ["ok"], "reasoning_effort": "high"}, Path(tmp))
        self.assertEqual(captured[0]["model"], "glm-5.3-flash")
        self.assertEqual(captured[0]["reasoning_effort"], "high")
        self.assertEqual(captured[0]["thinking"], {"type": "enabled"})
        self.assertNotIn("enable_thinking", captured[0])

    def test_glm_53_probe_cannot_disable_thinking(self):
        provider = OpenAICompatibleProvider({
            "provider_name": "glm_zhipu",
            "provider_type": "openai_compatible",
            "enabled": True,
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-5.3-flash",
            "display_name": "智谱 GLM",
            "extra_options": {"reasoning_effort": "low"},
        })
        captured = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": '{"probe":true,"ok":true}'}, "finish_reason": "stop"}]}).encode()

        def send(request, timeout):
            captured.append(json.loads(request.data))
            return Response()

        with patch.object(provider, "_configured", return_value=True), patch.object(provider, "_api_key", return_value=("test-key", "fixture")), patch("providers.openai_compatible._http_open", side_effect=send):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                provider.invoke_structured({"task_id": "probe", "system_prompt": "s", "prompt": "p", "required_keys": ["probe", "ok"], "reasoning_effort": "low", "enable_thinking": False}, Path(tmp))
        self.assertEqual(captured[0]["thinking"], {"type": "enabled"})
        self.assertEqual(captured[0]["reasoning_effort"], "low")

    def test_glm_47_does_not_receive_unsupported_reasoning_effort(self):
        provider = OpenAICompatibleProvider({
            "provider_name": "glm_zhipu", "provider_type": "openai_compatible", "enabled": True,
            "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.7", "display_name": "智谱 GLM",
        })
        captured = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]}).encode()

        with patch.object(provider, "_configured", return_value=True), patch.object(provider, "_api_key", return_value=("test-key", "fixture")), patch("providers.openai_compatible._http_open", side_effect=lambda request, timeout: (captured.append(json.loads(request.data)) or Response())):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                provider.invoke_structured({"task_id": "t", "system_prompt": "s", "prompt": "p", "required_keys": ["ok"], "reasoning_effort": "high"}, Path(tmp))
        self.assertNotIn("reasoning_effort", captured[0])
        self.assertEqual(captured[0]["thinking"], {"type": "enabled"})

    def test_tender_recognition_keeps_explicit_extended_timeout(self):
        provider = OpenAICompatibleProvider({
            "provider_name": "glm_zhipu",
            "provider_type": "openai_compatible",
            "enabled": True,
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4.7",
            "display_name": "智谱 GLM",
        })
        captured_timeouts = []

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                payload = {"chunk_id": "CHK-0001", "proposed_items": [], "proposed_conflicts": [], "proposed_boilerplate": [], "proposed_scoring_items": [], "chunk_missing_facts": []}
                return json.dumps({"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}).encode()

        def send(_request, timeout):
            captured_timeouts.append(timeout)
            return Response()

        request = {
            "task_id": "tender-timeout",
            "system_prompt": "s",
            "prompt": "p",
            "required_keys": ["chunk_id", "proposed_items", "proposed_conflicts", "proposed_boilerplate", "proposed_scoring_items", "chunk_missing_facts"],
            "chunk_id": "CHK-0001",
            "generation_mode": "tender_requirement_recognition",
            "timeout_seconds": 600,
        }
        with patch.object(provider, "_configured", return_value=True), patch.object(provider, "_api_key", return_value=("test-key", "fixture")), patch("providers.openai_compatible._http_open", side_effect=send):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                provider.invoke_structured(request, Path(tmp))
        self.assertEqual(captured_timeouts, [600])

    def test_custom_modelstudio_health_uses_workspace_model_list(self):
        provider = OpenAICompatibleProvider({
            "provider_name": "qwen_modelstudio",
            "provider_type": "openai_compatible",
            "enabled": True,
            "base_url": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.8-plus",
            "display_name": "千问 Model Studio",
        })
        captured = []

        class Response:
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"output": {"models": [{"model": "qwen3.8-plus"}, {"model": "qwen3.8-max"}]}}).encode()

        def send(request, timeout):
            captured.append((request, timeout))
            return Response()

        with patch.object(provider, "_api_key", return_value=("test-key", "session_only")), patch("providers.openai_compatible._http_open", side_effect=send):
            result = provider.health_check()

        self.assertTrue(result["available"])
        self.assertEqual(result["health_probe"], "workspace_model_list")
        self.assertEqual(captured[0][0].get_method(), "GET")
        self.assertIn("/api/v1/models?", captured[0][0].full_url)
        self.assertIn("name=qwen", captured[0][0].full_url)
        self.assertIn("page_size=100", captured[0][0].full_url)
        self.assertEqual(result["models"], ["qwen3.8-plus", "qwen3.8-max"])
        self.assertTrue(result["model_list_match"])

    def test_custom_modelstudio_deployment_id_continues_to_inference_probe(self):
        provider = OpenAICompatibleProvider({
            "provider_name": "qwen_modelstudio",
            "provider_type": "openai_compatible",
            "enabled": True,
            "base_url": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "model": "qwen",
            "display_name": "千问 Model Studio",
        })

        class Response:
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"output": {"models": []}}).encode()

        with patch.object(provider, "_api_key", return_value=("test-key", "session_only")), patch("providers.openai_compatible._http_open", return_value=Response()):
            result = provider.health_check()

        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "model_requires_inference_probe")
        self.assertIn("模型“qwen”", result["message"])
        self.assertIn("最小生成验证", result["message"])
        self.assertFalse(result["model_list_match"])

    def test_qwen_404_on_valid_workspace_path_is_not_mislabeled_as_bad_address(self):
        import urllib.error
        from io import BytesIO
        from providers.openai_compatible import _http_error_message

        url = "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
        body = b'{"error":{"code":"model_not_found","message":"model does not exist"}}'
        error = urllib.error.HTTPError(url, 404, "Not Found", None, BytesIO(body))
        message = _http_error_message(error, url, "千问")
        self.assertIn("模型名称不存在", message)
        self.assertNotIn("接口地址不正确", message)


if __name__ == "__main__":
    unittest.main()
