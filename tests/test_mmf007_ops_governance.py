"""MMF-007 Phase 4/5/6/7/8：治理/隐私/迁移/验收门/PPT Contract 行为测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.ops_governance import (
    acceptance_ready_gate,
    provider_data_disclosure,
    provider_status_layers,
    scan_log_for_secrets,
)


class ProviderStatusLayersTest(unittest.TestCase):
    def test_reachable_provider_layers(self):
        layers = provider_status_layers(
            app_installed=True,
            health={"configured": True, "status": "available", "credential_source": "windows_credential_manager"},
            config={"model": "qwen3.8-flash", "provider_type": "openai_compatible", "endpoint_alias": "x"},
        )
        self.assertTrue(layers["installed"])
        self.assertTrue(layers["reachable"])
        self.assertTrue(layers["authenticated"])
        self.assertEqual(layers["quota_available"]["status"], "ASSUMED_FROM_REACHABLE")
        self.assertEqual(layers["effective_config"]["model"], "qwen3.8-flash")

    def test_unprobed_layers_not_fake_available(self):
        layers = provider_status_layers(app_installed=True, health=None)
        self.assertFalse(layers["reachable"])
        self.assertFalse(layers["authenticated"])


class PrivacyTest(unittest.TestCase):
    def test_disclosure_records_scope_not_full_text(self):
        disclosure = provider_data_disclosure(
            provider_name="qwen_modelstudio", model="qwen3.8-flash",
            sent_file_names=["tender.docx"], sent_requirement_ids=["REQ-0001", "REQ-0002"],
            sent_knowledge_ids=["KU-0001"], redaction_applied=True,
        )
        text = json.dumps(disclosure, ensure_ascii=False)
        self.assertNotIn("本项目总建筑面积", text)
        self.assertEqual(disclosure["sent_file_names"], ["tender.docx"])
        self.assertTrue(disclosure["redaction_applied"])

    def test_log_secret_scan(self):
        hits = scan_log_for_secrets("config with sk-abcdef1234567890 and Authorization: Bearer abcdef12345")
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(scan_log_for_secrets("clean log line with 无秘密"), [])


class AcceptanceGateTest(unittest.TestCase):
    def test_all_green_ready(self):
        gate = acceptance_ready_gate(**{k: True for k in (
            "service_healthy", "ui_reachable", "links_valid", "artifact_exists",
            "artifact_downloadable", "open_folder_works", "internal_qa_complete",
            "repair_queue_settled", "production_runtime_known")})
        self.assertEqual(gate["status"], "READY_FOR_TODD_REVIEW")
        self.assertIn("PPT视觉", gate["todd_scope"])

    def test_missing_items_not_ready(self):
        gate = acceptance_ready_gate(**{
            "service_healthy": True, "ui_reachable": True, "links_valid": True,
            "artifact_exists": False, "artifact_downloadable": False, "open_folder_works": True,
            "internal_qa_complete": True, "repair_queue_settled": False, "production_runtime_known": True})
        self.assertEqual(gate["status"], "NOT_READY")
        self.assertIn("artifact_exists", gate["missing"])


class MigrationRollbackTest(unittest.TestCase):
    def _app_root(self, tmp: Path) -> Path:
        root = Path(tmp)
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "providers.local.json").write_text(json.dumps({"available_providers": []}), encoding="utf-8")
        (root / "config" / "user_settings.json").write_text(json.dumps({"theme": "D"}), encoding="utf-8")
        (root / "config" / "app.json").write_text(json.dumps({"port": 3050}), encoding="utf-8")
        return root

    def test_migrate_commits_and_records(self):
        sys.path.insert(0, str(APP.parent))
        from tools.migrate_app_data import migrate
        with tempfile.TemporaryDirectory() as tmp:
            root = self._app_root(Path(tmp))
            state = migrate(root)
            self.assertEqual(state["status"], "COMMITTED")
            config = json.loads((root / "config" / "providers.local.json").read_text(encoding="utf-8"))
            self.assertEqual(config["migration_meta"]["data_schema_version"], 2)
            self.assertTrue((root / "migration_state.json").is_file())

    def test_rollback_on_corrupt_target(self):
        sys.path.insert(0, str(APP.parent))
        from tools import migrate_app_data as mig
        with tempfile.TemporaryDirectory() as tmp:
            root = self._app_root(Path(tmp))
            original_validate = mig.validate
            mig.validate = lambda _root: {"ok": False}
            try:
                state = mig.migrate(root)
            finally:
                mig.validate = original_validate
            self.assertEqual(state["status"], "ROLLED_BACK")
            # 回滚后配置内容与备份一致（可解析、字段保留）
            config = json.loads((root / "config" / "providers.local.json").read_text(encoding="utf-8"))
            self.assertNotIn("migration_meta", config, "回滚后配置必须恢复为迁移前状态")
            self.assertEqual(config["available_providers"], [])


class PptPlannerContractTest(unittest.TestCase):
    def test_mock_planner_produces_valid_contract(self):
        sys.path.insert(0, str(APP))
        from ppt_renderer_v2.planner_contract import mock_visual_planner, validate_slide_contract
        row = {"slide_id": "SL-007", "slide_role": "process", "slide_title": "投诉闭环",
               "core_message": "全周期闭环", "supporting_points": ["受理", "处置", "回访"],
               "required_visual": "process_flow", "source_requirements": ["REQ-0004"]}
        contract = mock_visual_planner(row, 6)
        validation = validate_slide_contract(contract)
        self.assertEqual(validation["status"], "PASS", validation)
        self.assertEqual(contract["archetype"], "Process")

    def test_unknown_archetype_fails(self):
        from ppt_renderer_v2.planner_contract import validate_slide_contract
        payload = {key: "" for key in (
            "slide_id", "archetype", "title", "message", "content_blocks", "visual_intent",
            "visual_data", "image_requirement", "source_requirements", "source_scoring_items")}
        payload["archetype"] = "QuantumSlide"
        self.assertEqual(validate_slide_contract(payload)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
