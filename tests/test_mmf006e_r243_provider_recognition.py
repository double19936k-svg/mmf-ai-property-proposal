from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import app_core  # noqa: E402
from providers import openai_compatible as oc  # noqa: E402


class _Response:
    status = 200

    def read(self):
        return b"{}"


class _BlockedDirectConnection:
    def request(self, *_args, **_kwargs):
        exc = PermissionError(13, "socket access denied")
        exc.winerror = 10013
        raise exc

    def close(self):
        return None


class ProviderSocketRoutingTests(unittest.TestCase):
    def test_winerror_10013_retries_once_through_configured_proxy(self) -> None:
        request = urllib.request.Request(
            "https://fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/models",
            method="GET",
        )
        opener = unittest.mock.Mock()
        opener.open.return_value = _Response()
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7897"}, clear=False), \
             patch.object(oc.http.client, "HTTPSConnection", return_value=_BlockedDirectConnection()), \
             patch.object(oc.urllib.request, "build_opener", return_value=opener):
            response = oc._http_open(request, timeout=5)
        self.assertEqual(response.status, 200)
        opener.open.assert_called_once()
        self.assertEqual(oc.LAST_NETWORK_ROUTE.get(), "proxy_fallback_after_winerror_10013")

    def test_winerror_10013_is_not_reported_as_auth(self) -> None:
        exc = PermissionError(13, "socket access denied")
        exc.winerror = 10013
        text = oc._http_error_message(exc, "https://example.invalid", "千问")
        self.assertIn("WinError 10013", text)
        self.assertIn("不是密钥或登录问题", text)


class ExplicitFallbackTests(unittest.TestCase):
    def test_complete_proposal_requires_explicit_local_fallback_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder)
            run_id = "tender_fixture_001"
            tender = runs / run_id / "tender"
            tender.mkdir(parents=True)
            (tender / "requirement_pack.json").write_text("{}", encoding="utf-8")
            (tender / "status.json").write_text(json.dumps({
                "run_id": run_id,
                "recognition_outcome": "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK",
                "local_fallback_confirmed": False,
            }), encoding="utf-8")
            with patch.object(app_core, "RUNS_DIR", runs), patch.object(app_core, "_checked", side_effect=lambda p: Path(p)):
                with self.assertRaisesRegex(app_core.MMFError, "继续使用本地解析结果"):
                    app_core.confirm_tender_run(run_id, {}, {"scenario": "完整物业服务方案"})

    def test_ui_exposes_both_actions_and_no_silent_downgrade(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK", html)
        self.assertIn("重新识别", html)
        self.assertIn("继续使用本地解析", html)
        self.assertIn("accept-local-fallback", html)


if __name__ == "__main__":
    unittest.main()
