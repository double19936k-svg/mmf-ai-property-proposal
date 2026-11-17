import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from provider_view import public_status
from providers.credential_store import CredentialStore


class ProviderCredentialPrecedence(unittest.TestCase):
    def test_current_session_key_overrides_stale_environment_key(self):
        store = CredentialStore("test_property_ai")
        store._advapi32 = None
        target = store.credential_ref("qwen_modelstudio")
        store._session[target] = "fresh-ui-key"

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "stale-env-key"}):
            value, source = store.get("qwen_modelstudio", "DASHSCOPE_API_KEY")

        self.assertEqual(value, "fresh-ui-key")
        self.assertEqual(source, "session_only")


class ProviderPublicStatus(unittest.TestCase):
    def _row(self, **updates):
        row = {
            "provider_name": "qwen_modelstudio",
            "provider_type": "openai_compatible",
            "configured": True,
            "available": False,
            "status": "pending_test",
            "connection_status": "not_tested",
            "message": "已配置，正在后台检查连接",
        }
        row.update(updates)
        return row

    def test_configured_but_untested_is_not_connected_or_selectable(self):
        result = public_status(self._row())
        self.assertEqual(result["user_status"], "not_tested")
        self.assertEqual(result["user_status_label"], "待测试")
        self.assertFalse(result["selectable"])

    def test_failed_connection_is_unavailable_not_connected(self):
        result = public_status(self._row(status="unavailable", connection_status="connection_failed", message="连接失败"))
        self.assertEqual(result["user_status"], "unavailable")
        self.assertFalse(result["selectable"])

    def test_successful_connection_remains_selectable(self):
        result = public_status(self._row(status="available", connection_status="connected", available=True, message="可用"))
        self.assertEqual(result["user_status"], "connected")
        self.assertTrue(result["selectable"])


if __name__ == "__main__":
    unittest.main()
