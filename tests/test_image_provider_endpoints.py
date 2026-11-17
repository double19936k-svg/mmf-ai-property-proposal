from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from providers.image_generation import ImageGenerationProvider, normalize_image_base_url
from providers.manager import ProviderManager
from provider_view import public_status


class ImageProviderEndpoints(unittest.TestCase):
    def test_image_base_urls_do_not_inherit_chat_compatible_path(self):
        workspace = "https://ws-demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        self.assertEqual(
            normalize_image_base_url(workspace, "dashscope_wanx"),
            "https://ws-demo.cn-beijing.maas.aliyuncs.com/api/v1",
        )
        self.assertEqual(
            normalize_image_base_url("https://open.bigmodel.cn", "zhipu_cogview"),
            "https://open.bigmodel.cn/api/paas/v4",
        )

    def test_workspace_wanx_creation_and_polling_stay_on_workspace_host(self):
        provider = ImageGenerationProvider({
            "provider_name": "qwen_image", "provider_type": "image_generation", "enabled": True,
            "base_url": "https://ws-demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "image_api_url": "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
            "model": "wanx-v1", "extra_options": {"backend": "dashscope_wanx"},
        })
        calls = []

        def fake_http(url, body, api_key, **kwargs):
            calls.append((url, body, kwargs.get("method")))
            if body is not None:
                return {"output": {"task_id": "TASK-1", "task_status": "PENDING"}}
            return {"output": {"task_status": "SUCCEEDED", "results": [{"url": "https://example.invalid/image.png"}]}}

        with patch.object(provider, "_api_key", return_value=("test-key", "fixture")), patch.object(provider, "_http_json", side_effect=fake_http):
            url, _ = provider._dashscope_generate("物业服务场景", "1024*1024")
        self.assertEqual(url, "https://example.invalid/image.png")
        self.assertEqual(calls[0][0], "https://ws-demo.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis")
        self.assertEqual(calls[1][0], "https://ws-demo.cn-beijing.maas.aliyuncs.com/api/v1/tasks/TASK-1")

    def test_zhipu_cogview_uses_native_image_endpoint_and_size(self):
        provider = ImageGenerationProvider({
            "provider_name": "glm_image", "provider_type": "image_generation", "enabled": True,
            "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "cogview-4-250304",
            "extra_options": {"backend": "zhipu_cogview"},
        })
        calls = []

        def fake_http(url, body, api_key, **kwargs):
            calls.append((url, body))
            return {"data": [{"url": "https://example.invalid/cogview.png"}]}

        with patch.object(provider, "_api_key", return_value=("test-key", "fixture")), patch.object(provider, "_http_json", side_effect=fake_http):
            url, _ = provider._zhipu_generate("设备机房巡检", "720*1280")
        self.assertEqual(url, "https://example.invalid/cogview.png")
        self.assertEqual(calls[0][0], "https://open.bigmodel.cn/api/paas/v4/images/generations")
        self.assertEqual(calls[0][1]["size"], "720x1280")

    def test_config_and_ui_expose_glm_and_do_not_fake_kimi_generation(self):
        config = json.loads((ROOT / "config" / "providers.example.json").read_text(encoding="utf-8-sig"))
        rows = {row["provider_name"]: row for row in config["available_providers"]}
        self.assertEqual(rows["glm_image"]["extra_options"]["backend"], "zhipu_cogview")
        self.assertEqual(rows["glm_image"]["extra_options"]["reuse_text_provider"], "glm_zhipu")
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8-sig")
        self.assertIn('option value="glm_image"', page)
        self.assertIn("Kimi目前仅支持图片理解，不能生成图片", page)
        self.assertNotIn('option value="kimi_image"', page)
        self.assertIn("当前凭证：", page)
        self.assertIn("来源为", page)
        result = public_status({
            "provider_name": "qwen_image",
            "configured": True,
            "available": False,
            "connection_status": "connection_failed",
            "metadata": {"provider_type": "image_generation"},
        })
        self.assertEqual(result["capability"], "image")
        self.assertFalse(result["selectable"])

    def test_reused_key_also_inherits_matching_workspace_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            source = json.loads((ROOT / "config" / "providers.example.json").read_text(encoding="utf-8-sig"))
            qwen = next(row for row in source["available_providers"] if row["provider_name"] == "qwen_modelstudio")
            qwen["base_url"] = "https://ws-demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            (config_dir / "providers.example.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            manager = ProviderManager(ROOT / "app", config_dir=config_dir)
            image = manager.providers["qwen_image"]
        self.assertEqual(image.config["base_url"], "https://ws-demo.cn-beijing.maas.aliyuncs.com/api/v1")
        self.assertEqual(image.config["endpoint_inherited_from"], "qwen_modelstudio")


if __name__ == "__main__":
    unittest.main()
