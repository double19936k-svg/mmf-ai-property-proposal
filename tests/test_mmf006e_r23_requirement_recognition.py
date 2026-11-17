import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import app_core
from providers.base import ProviderUnavailableError
from providers.mock import MockProvider


def _extraction():
    return {
        "schema_version": "tender-extraction-v0.1",
        "extraction_id": "EXT-TEST",
        "run_id": "tender_test_ok",
        "files": [{"file_id": "FILE-001", "original_filename": "req.txt"}],
        "outline": [],
        "pages": [],
        "paragraphs": [{
            "file_id": "FILE-001",
            "para_id": "P-1",
            "page_no": 1,
            "paragraph_index": 0,
            "heading_path": ["安保"],
            "text": "本项目必须提供24小时安保服务。物业服务应包含保洁和绿化。",
            "outline_node_id": None,
        }],
        "tables": [],
        "warnings": [],
        "processing_mode": "txt",
    }


class RequirementRecognitionHang(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.runtime = self.root / "runtime"
        self.runs.mkdir()
        self.runtime.mkdir()
        self.patches = [
            patch.object(app_core, "RUNS_DIR", self.runs),
            patch.object(app_core, "RUNTIME_ROOT", self.root),
            patch.object(app_core, "RUNTIME_DIR", self.runtime),
            patch.object(app_core, "MMF006A_STATE_FILE", self.runtime / "MMF006A_state.json"),
            patch.object(app_core, "ACCESS_AUDIT_FILE", self.runtime / "access_audit.jsonl"),
            patch.object(app_core, "_checked", side_effect=lambda path: Path(path)),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def _prepare_run(self, run_id: str, provider_name: str = "mock") -> Path:
        tender = self.runs / run_id / "tender"
        tender.mkdir(parents=True)
        (self.runtime / "access_audit.jsonl").write_text("", encoding="utf-8")
        extraction = _extraction()
        extraction["run_id"] = run_id
        (tender / "extraction.json").write_text(json.dumps(extraction, ensure_ascii=False), encoding="utf-8")
        (tender / "status.json").write_text(json.dumps({
            "schema_version": "mmf006a-status-v0.1",
            "run_id": run_id,
            "provider_name": provider_name,
            "upload_completed": True,
            "extraction_completed": True,
            "chunks_total": 0,
            "chunks_completed": 0,
            "failed_chunk": None,
            "pack_status": "not_started",
            "stage": "A2_EXTRACTION_PASS",
            "updated_at": app_core.now_iso(),
        }, ensure_ascii=False), encoding="utf-8")
        return tender

    def test_failed_health_gate_is_not_infinite_running(self):
        run_id = "tender_test_fail"
        self._prepare_run(run_id, "qwen_modelstudio")
        app_core.mark_tender_processing(run_id, "qwen_modelstudio")
        status = app_core.mark_tender_failed(
            run_id,
            ProviderUnavailableError("网络连接失败，未能完成AI解析"),
        )
        self.assertEqual(status["pack_status"], "understanding_failed")
        self.assertEqual(status["error_code"], "network_failed")
        self.assertIn("需求识别失败", status["error"])
        self.assertNotIn("login", status["error"].lower())
        self.assertNotIn("401", status["error"])

    def test_not_started_is_not_marked_stale(self):
        run_id = "tender_test_idle"
        self._prepare_run(run_id)
        status = app_core.reconcile_tender_status(run_id, worker_alive=False)
        self.assertEqual(status["pack_status"], "not_started")
        self.assertFalse(status.get("error"))

    def test_dead_worker_marks_stale_failed(self):
        run_id = "tender_test_stale"
        self._prepare_run(run_id)
        app_core.mark_tender_processing(run_id, "mock")
        stale_at = (datetime.now().astimezone() - timedelta(seconds=90)).isoformat(timespec="seconds")
        app_core.write_tender_status(run_id, {"updated_at": stale_at, "understanding_started_at": stale_at})
        # write_tender_status overwrites updated_at with now; force an old timestamp on disk.
        path = self.runs / run_id / "tender" / "status.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["updated_at"] = stale_at
        payload["understanding_started_at"] = stale_at
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        status = app_core.reconcile_tender_status(run_id, json.loads(path.read_text(encoding="utf-8")), worker_alive=False)
        self.assertEqual(status["pack_status"], "understanding_failed")
        self.assertEqual(status["error_code"], "TASK_STALE")
        self.assertIn("请重试", status["error"])

    def test_live_worker_is_not_killed(self):
        run_id = "tender_test_live"
        self._prepare_run(run_id)
        app_core.mark_tender_processing(run_id, "mock")
        stale_at = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
        status = app_core.reconcile_tender_status(
            run_id,
            {"pack_status": "understanding", "stage": "A4_PROVIDER_UNDERSTANDING", "updated_at": stale_at},
            worker_alive=True,
        )
        self.assertEqual(status["pack_status"], "understanding")
        self.assertFalse(status.get("error"))

    def test_process_skips_require_available_health_gate_and_builds_pack(self):
        run_id = "tender_test_ok"
        self._prepare_run(run_id, "mock")
        provider = MockProvider({
            "provider_name": "mock",
            "provider_type": "mock",
            "enabled": True,
            "test_mode": True,
            "model": "deterministic-mock-v0.1",
            "display_name": "Mock",
        })
        manager = Mock()
        manager.get.return_value = provider
        with patch.object(app_core, "provider_manager", return_value=manager):
            result = app_core.process_tender_run(run_id, "mock")
        manager.get.assert_called_once()
        _args, kwargs = manager.get.call_args
        self.assertEqual(_args[0], "mock")
        self.assertFalse(kwargs.get("require_available", _args[1] if len(_args) > 1 else True))
        self.assertTrue(result["pack"]["requirements"])
        disk = json.loads((self.runs / run_id / "tender" / "requirement_pack.json").read_text(encoding="utf-8"))
        self.assertTrue(disk["requirements"])
        status = json.loads((self.runs / run_id / "tender" / "status.json").read_text(encoding="utf-8"))
        self.assertNotEqual(status["pack_status"], "not_started")
        self.assertNotEqual(status["pack_status"], "understanding")

    def test_provider_failure_falls_back_to_local_pack(self):
        from tender_intake.understanding import understand_run
        run_id = "tender_test_fallback"
        run_dir = self.runs / run_id
        (run_dir / "tender").mkdir(parents=True)
        extraction = _extraction()
        provider = Mock()
        provider.config = {"provider_name": "qwen_modelstudio", "display_name": "千问 Model Studio"}
        provider.invoke_structured.side_effect = ProviderUnavailableError("千问拒绝了本次请求（HTTP 403）。可换一个引擎重试，本地解析结果仍会保留。")
        result = understand_run(run_dir, extraction, provider, resume=False)
        self.assertTrue(result["pack"]["requirements"])
        self.assertEqual(result["status"]["stage"], "A5_TODD_CONFIRMATION")
        self.assertNotEqual(result["status"]["pack_status"], "understanding_failed")
        self.assertIn("本地解析", result["status"].get("warning", ""))
        self.assertTrue((run_dir / "tender" / "requirement_pack.json").is_file())

    def test_transient_timeout_is_retried_with_extended_recognition_timeout(self):
        from tender_intake.understanding import RECOGNITION_TIMEOUT_SECONDS, understand_run

        run_id = "tender_test_transient_retry"
        run_dir = self.runs / run_id
        (run_dir / "tender").mkdir(parents=True)
        extraction = _extraction()
        extraction["run_id"] = run_id
        provider = Mock()
        provider.config = {"provider_name": "glm_zhipu", "display_name": "智谱 GLM"}
        calls = []

        def invoke(request, task_dir):
            calls.append((request, task_dir))
            if len(calls) == 1:
                raise ProviderUnavailableError("调用智谱GLM超时，未能完成AI解析")
            return dict(request["mock_response"])

        provider.invoke_structured.side_effect = invoke
        with patch("tender_intake.understanding.RECOGNITION_RETRY_DELAY_SECONDS", 0):
            result = understand_run(run_dir, extraction, provider, resume=False)

        self.assertEqual(result["status"]["recognition_outcome"], "AI_RECOGNITION_SUCCESS")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0]["generation_mode"], "tender_requirement_recognition")
        self.assertEqual(calls[0][0]["timeout_seconds"], RECOGNITION_TIMEOUT_SECONDS)
        self.assertEqual(calls[0][1].name, "provider_call")
        self.assertEqual(calls[1][1].name, "provider_call_retry_02")
        audit = json.loads((run_dir / "tender" / "chunks" / "CHK-0001" / "recognition_retry_audit.json").read_text(encoding="utf-8"))
        self.assertEqual([row["status"] for row in audit["attempts"]], ["FAIL", "PASS"])

    def test_authentication_failure_is_not_retried(self):
        from tender_intake.understanding import understand_run

        run_id = "tender_test_auth_no_retry"
        run_dir = self.runs / run_id
        (run_dir / "tender").mkdir(parents=True)
        extraction = _extraction()
        extraction["run_id"] = run_id
        provider = Mock()
        provider.config = {"provider_name": "qwen_modelstudio", "display_name": "千问 Model Studio"}
        provider.invoke_structured.side_effect = ProviderUnavailableError("HTTP 401: Unauthorized，密钥无效")

        result = understand_run(run_dir, extraction, provider, resume=False)

        self.assertEqual(result["status"]["recognition_outcome"], "AI_RECOGNITION_FAILED_WITH_LOCAL_FALLBACK")
        self.assertEqual(provider.invoke_structured.call_count, 1)

    def test_concurrent_status_writes_do_not_raise(self):
        path = self.runtime / "status.json"
        errors = []

        def worker(n):
            try:
                app_core.write_json(path, {"n": n, "payload": "x" * 200})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for item in threads:
            item.start()
        for item in threads:
            item.join()
        self.assertEqual(errors, [])
        json.loads(path.read_text(encoding="utf-8"))

    def test_worker_exception_is_persisted(self):
        import server as mmf_server
        run_id = "tender_test_thread"
        self._prepare_run(run_id, "qwen_modelstudio")
        app_core.mark_tender_processing(run_id, "qwen_modelstudio")
        with patch.object(mmf_server, "process_tender_run", side_effect=ProviderUnavailableError("网络连接失败，未能完成AI解析")):
            mmf_server._run_tender_job(run_id, "qwen_modelstudio")
        status = json.loads((self.runs / run_id / "tender" / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["pack_status"], "understanding_failed")
        self.assertIn("需求识别失败", status["error"])
        self.assertTrue(status.get("error_type"))


if __name__ == "__main__":
    unittest.main()
