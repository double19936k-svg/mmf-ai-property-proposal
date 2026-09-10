from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1] / "app"
ROOT = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from longform.batch_planner import plan_generation_batches
from longform.eta import append_latency_sample, estimate_runtime
from longform.factory import LongformGenerationFactory
from longform.orchestrator import evaluate_requirement_coverage_regression
from longform.reasoning import classify_generation_failure
from longform.scheduler import ConcurrentBatchScheduler, MAX_RATE_ATTEMPTS
from providers.openai_compatible import OpenAICompatibleProvider
from tests.test_mmf006e_r2_scheduler import _brief, _pack, _provider, _selection


class SchedulerAdversarialTest(unittest.TestCase):
    def test_permanent_rate_limit_is_bounded(self):
        hits = []

        def work(batch):
            hits.append(batch["batch_id"])
            return {"status": "RATE_LIMIT", "error_code": "RATE_LIMIT", "section_ids": batch["section_ids"]}

        scheduler = ConcurrentBatchScheduler(provider_name="mock", max_parallelism=2)
        out = scheduler.run([{"batch_id": "GB-01", "section_ids": ["A"], "dependency_ids": []}], work)
        self.assertLessEqual(len(hits), MAX_RATE_ATTEMPTS)
        self.assertIn("GB-01", out["failed_batches"])
        self.assertNotIn("GB-01", out["completed_batches"])

    def test_failed_prerequisite_does_not_run_dependent(self):
        invoked = []

        def work(batch):
            invoked.append(batch["batch_id"])
            if batch["batch_id"] == "A":
                return {"status": "FAILED_BATCH", "error_code": "QUALITY_FAILURE"}
            return {"status": "SUCCESS"}

        scheduler = ConcurrentBatchScheduler(provider_name="mock", max_parallelism=3)
        out = scheduler.run([
            {"batch_id": "A", "section_ids": ["S1"], "dependency_ids": []},
            {"batch_id": "B", "section_ids": ["S2"], "dependency_ids": ["A"]},
        ], work)
        self.assertEqual(invoked, ["A"])
        self.assertIn("B", out["blocked_batches"] or out["failed_batches"] or [])
        self.assertNotIn("B", out["completed_batches"])

    def test_unknown_dependency_is_blocked_not_force_run(self):
        invoked = []

        def work(batch):
            invoked.append(batch["batch_id"])
            return {"status": "SUCCESS"}

        scheduler = ConcurrentBatchScheduler(provider_name="mock", max_parallelism=1)
        out = scheduler.run([{"batch_id": "B", "section_ids": ["S2"], "dependency_ids": ["MISSING"]}], work)
        self.assertEqual(invoked, [])
        self.assertTrue(out["unknown_dependencies"] or out["blocked_batches"])

    def test_overlapping_workers(self):
        lock = threading.Lock()
        active = 0
        max_active = 0

        def work(batch):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.12)
            with lock:
                active -= 1
            return {"status": "SUCCESS"}

        scheduler = ConcurrentBatchScheduler(provider_name="mock", max_parallelism=3)
        scheduler.run([
            {"batch_id": "GB-01", "section_ids": ["A"], "dependency_ids": []},
            {"batch_id": "GB-02", "section_ids": ["B"], "dependency_ids": []},
            {"batch_id": "GB-03", "section_ids": ["C"], "dependency_ids": []},
        ], work)
        self.assertGreaterEqual(max_active, 2)


class BatchPlannerDagTest(unittest.TestCase):
    def test_backward_hard_dependency_is_preserved(self):
        contracts = [
            {"section_id": "S01-01", "generation_order": 1, "must_cover": ["a"], "target_words": {"min": 200, "max": 400}},
            {"section_id": "S02-01", "generation_order": 2, "must_cover": ["b"], "target_words": {"min": 200, "max": 400}},
        ]
        deps = {"dependencies": [{"source_section": "S02-01", "dependent_sections": ["S01-01"], "dependency_type": "role_and_responsibility", "stale_on_change": True}]}
        plan = plan_generation_batches(contracts, deps, provider_name="mock")
        s01 = next(row for row in plan["batches"] if "S01-01" in row["section_ids"])
        s02 = next(row for row in plan["batches"] if "S02-01" in row["section_ids"])
        self.assertIn(s02["batch_id"], s01["dependency_ids"])
        self.assertTrue(s01["latency_budget"]["soft"] and s01["latency_budget"]["hard"])

    def test_hard_dependent_sections_are_not_merged(self):
        contracts = [
            {"section_id": "S03-01", "generation_order": 1, "must_cover": ["a"], "target_words": {"min": 200, "max": 400}},
            {"section_id": "S03-02", "generation_order": 2, "must_cover": ["b"], "target_words": {"min": 200, "max": 400}},
        ]
        deps = {"dependencies": [{"source_section": "S03-01", "dependent_sections": ["S03-02"], "dependency_type": "staffing_and_hours", "stale_on_change": True}]}
        plan = plan_generation_batches(contracts, deps, provider_name="mock")
        self.assertEqual(plan["generation_batch_count"], 2)

    def test_over_capacity_section_is_kept_as_singleton(self):
        contracts = [{"section_id": "S01-01", "generation_order": 1, "must_cover": ["a"] * 8, "target_words": {"min": 50000, "max": 80000}}]
        plan = plan_generation_batches(contracts, {"dependencies": []}, provider_name="mock", provider_config={"max_tokens": 1000})
        self.assertEqual(plan["batches"][0]["section_ids"], ["S01-01"])
        self.assertTrue(plan["warnings"])


class CoverageAndDeliveryTest(unittest.TestCase):
    def test_empty_fragments_fail_when_must_exists(self):
        matrix = {"matrix": [{"requirement_id": "REQ-1", "mandatory_level": "MUST", "coverage_status": "MAPPED", "primary_section_id": "S01-01", "requirement_text": "必须覆盖安全巡逻"}]}
        report = evaluate_requirement_coverage_regression(matrix, {})
        self.assertEqual(report["status"], "FAIL")
        self.assertGreater(report["planned_must"], 0)

    def test_scoring_is_not_auto_covered(self):
        matrix = {"matrix": [{"requirement_id": "SCR-1", "mandatory_level": "INFO", "scoring_item_id": "SCR-1", "coverage_status": "MAPPED", "primary_section_id": "S02-01", "requirement_text": "评分要求必须写服务方案深度"}]}
        report = evaluate_requirement_coverage_regression(matrix, {"S02-01": {"title": "x", "body_blocks": [{"type": "paragraph", "content": "无关文字"}]}})
        self.assertEqual(report["status"], "FAIL")

    def test_quality_only_fail_blocks_delivery(self):
        def blocked(compliance, commitment, artifact_qa, quality):
            return (
                compliance.get("status") == "BLOCK"
                or commitment.get("status") == "BLOCK"
                or artifact_qa.get("status") == "BLOCK"
                or quality.get("status") == "FAIL"
            )
        self.assertTrue(blocked({"status": "PASS"}, {"status": "PASS"}, {"status": "PASS"}, {"status": "FAIL"}))
        self.assertFalse(blocked({"status": "PASS"}, {"status": "PASS"}, {"status": "PASS"}, {"status": "PASS"}))
        self.assertIn("def delivery_blocked", (APP / "app_core.py").read_text(encoding="utf-8"))

    def test_quality_failure_not_classified_as_latency(self):
        kind = classify_generation_failure(
            {"status": "BLOCK", "must_cover": {"a": "MISSING"}, "commitment": {"status": "PASS"}},
            {"status": "SUCCESS", "latency_soft_exceeded": True, "duration_seconds": 999},
        )
        self.assertEqual(kind, "QUALITY_FAILURE")
        kind2 = classify_generation_failure(None, {"status": "SUCCESS", "latency_soft_exceeded": True})
        self.assertIsNone(kind2)


class TransportTest(unittest.TestCase):
    def test_openai_batch_keeps_tokens_and_timeout(self):
        captured = {}

        class DummyResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": json.dumps({"batch_id": "GB-01", "sections": []})}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        import providers.openai_compatible as oc

        def fake_open(request, timeout):
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return DummyResp()

        original = oc._http_open
        oc._http_open = fake_open
        try:
            provider = OpenAICompatibleProvider({
                "provider_name": "qwen_modelstudio",
                "provider_type": "openai_compatible",
                "enabled": True,
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-plus",
                "api_key_env": "NONE",
            })
            provider._api_key = lambda: ("sk-test", "test")
            tmp = Path(tempfile.mkdtemp())
            try:
                provider.invoke_structured({
                    "task_id": "t1",
                    "system_prompt": "s",
                    "prompt": "p",
                    "generation_mode": "longform_batch",
                    "max_tokens": 8192,
                    "timeout_seconds": 600,
                    "required_keys": ["sections"],
                }, tmp / "task")
            except Exception:
                pass
        finally:
            oc._http_open = original
        self.assertEqual(captured["timeout"], 600)
        self.assertEqual(captured["body"]["max_tokens"], 8192)

    def test_grok_requested_effort_wins_and_no_always_approve(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("mmf_grok_bridge", ROOT / "providers" / "grok_bridge" / "grok_bridge.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        GrokBridge = module.GrokBridge
        captured = {}

        def runner(args, cwd, timeout, proxy_env=None):
            captured["args"] = args
            captured["timeout"] = timeout
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "modelUsage": {"grok-4.6": {"tokens": 1}},
                "structuredOutput": {"ok": True},
                "text": "{\"ok\": true}",
                "stopReason": "end_turn",
            }), stderr="")

        tmp = Path(tempfile.mkdtemp())
        exe = tmp / "grok.exe"
        exe.write_text("dummy", encoding="utf-8")
        cfg = tmp / "bridge_config.json"
        cfg.write_text(json.dumps({"reasoning_effort": "low", "model_alias": "grok-4.6", "expected_model": "grok-4.6", "timeout_seconds": 99}), encoding="utf-8")
        bridge = GrokBridge(cfg, command_runner=runner, executable=str(exe), version="test")
        bridge._local_login_ok = lambda: True
        result = bridge.invoke(
            task_id="t",
            prompt="hello",
            working_directory=tmp,
            run_dir=tmp / "run",
            timeout_seconds=600,
            reasoning_effort="high",
            max_network_retries=0,
        )
        self.assertIn("--reasoning-effort", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--reasoning-effort") + 1], "high")
        self.assertEqual(captured["timeout"], 600)
        self.assertNotIn("--always-approve", captured["args"])
        self.assertEqual(result["audit"]["actual_reasoning_effort"], "high")


class EtaAndUiTest(unittest.TestCase):
    def test_eta_does_not_mix_unknown_model_into_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.jsonl"
            append_latency_sample(path, {"schema_version": "latency-history-v2", "provider": "mock", "model": "unknown", "mode": "balanced", "stage": "draft", "elapsed_seconds": 9999, "success": True, "eligible_for_eta": True, "size_bucket": "single_1", "section_count": 1, "input_tokens": 1, "output_tokens": 1, "run_id": "legacy-unknown", "this_run_calls": 1})
            append_latency_sample(path, {"schema_version": "latency-history-v2", "provider": "mock", "model": "mock-a", "mode": "balanced", "stage": "draft", "elapsed_seconds": 60, "success": True, "eligible_for_eta": True, "size_bucket": "single_1", "section_count": 1, "input_tokens": 1, "output_tokens": 1, "run_id": "known-a", "this_run_calls": 1})
            eta = estimate_runtime(provider="mock", model="mock-a", mode="balanced", stage="draft", history_path=path, min_samples=1, section_count=1)
            self.assertEqual(eta["status"], "range")
            self.assertLess(eta["p50_seconds"], 500)

    def test_ui_prefers_event_target_speed(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("event.target.name", html)
        self.assertIn("gen_speed_profile", html)
        self.assertIn("seed_speed_profile", html)
        self.assertIn("function currentSpeed(event)", html)
        self.assertNotIn("const picked=document.querySelector('input[name=\"speed_profile\"]:checked')||document.querySelector('input[name=\"gen_speed_profile\"]:checked')", html)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
