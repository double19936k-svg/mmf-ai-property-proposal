from __future__ import annotations

import json
import os
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

from longform.eta import append_latency_sample, estimate_runtime, load_samples, size_bucket
from longform.factory import LongformGenerationFactory
from longform.orchestrator import generate_longform
from longform.scheduler import ConcurrentBatchScheduler
from providers.capability import latency_budget_seconds, resolve_profile
from providers.capability_probe import observe_from_health
from providers.mock import MockProvider
from providers.openai_compatible import OpenAICompatibleProvider
from providers.rate_limit import AdmissionInvalidLimits, AdmissionOversized, AdmissionTimeout, SlidingWindowLimiter, reset_default_limiter_for_tests
from providers.token_usage import extract_token_usage, resolve_attempt_tokens
from tests.test_mmf006e_r2_scheduler import CountingProvider, _brief, _pack, _selection, _write_run


def _load_bridge():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mmf_grok_bridge_r22", ROOT / "providers" / "grok_bridge" / "grok_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMPLETE_ENVELOPE = {
    "text": json.dumps({"ok": True, "note": "business mentions HTTP 401 in content only"}, ensure_ascii=False),
    "stopReason": "end_turn",
    "sessionId": "sess-r22",
    "requestId": "req-r22",
    "usage": {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 100,
        "total_tokens": 115,
    },
    "modelUsage": {
        "grok-4.6": {
            "inputTokens": 10,
            "outputTokens": 5,
            "cacheReadInputTokens": 100,
            "cacheCreationInputTokens": 0,
            "modelCalls": 1,
        }
    },
    "structuredOutput": {"ok": True, "note": "business mentions HTTP 401 in content only"},
}

AUX_STDERR = (
    "WARN Final signal sync timed out during exit; continuing\n"
    "WARN Resident session actor exited unexpectedly; reaping as DeadFailed\n"
    "WARN proactive bundle sync failed error=Network error: error decoding response body\n"
)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class SoftLatencyAndBudgetTest(unittest.TestCase):
    def test_soft_latency_reduces_future_concurrency_and_recovers(self):
        """软延迟治理契约更新（MMF-007C Round 2 / P1-1）。

        旧契约（MMF-006E R22）：一次超软预算就把并发 −1。
        新契约：连续 `CONSECUTIVE_BREACHES_REQUIRED`（=2）次超预算才 −1，单次只记
        `SOFT_BREACH_WATCHING`。原因：qoder_internal 队列桥的实测 p50 是 184 秒，
        沿用通用 120 秒兜底口径时 73 次准点调用里 60 次（82.2%）"超预算"，
        旧契约在头两个批次内就把并发从 3 锁到 1 且再也回不来 —— 一次慢在队列传输上
        是常态而不是证据，只有连串慢才是证据。降档仍然只影响后续批次，不取消在飞工人。
        """
        seen = []
        scheduler = ConcurrentBatchScheduler(provider_name="mock", max_parallelism=3)
        breaches = {"GB-01", "GB-02"}

        def work(batch):
            seen.append(scheduler.current_parallelism)
            if batch["batch_id"] in breaches:
                scheduler.observe_soft_latency()
            else:
                scheduler.observe_within_budget()
            return {"status": "SUCCESS"}

        out = scheduler.run(
            [
                {"batch_id": "GB-01", "section_ids": ["A"], "dependency_ids": []},
                {"batch_id": "GB-02", "section_ids": ["B"], "dependency_ids": ["GB-01"]},
                {"batch_id": "GB-03", "section_ids": ["C"], "dependency_ids": ["GB-02"]},
                {"batch_id": "GB-04", "section_ids": ["D"], "dependency_ids": ["GB-03"]},
            ],
            work,
        )
        self.assertEqual(seen[0], 3)
        self.assertEqual(seen[1], 3, "单次超预算不得降并发（旧契约会在这里降到 2）")
        self.assertEqual(seen[2], 2, "连续两次超预算才 −1")
        self.assertFalse(out["cancelled_healthy_workers"])
        self.assertEqual(out["soft_latency_events"], 2)
        self.assertEqual([(row["action"], row["from"], row["to"]) for row in scheduler.concurrency_events],
                         [("DOWNGRADE", 3, 2), ("RECOVER", 2, 3)],
                         "降与还都必须在台账里留痕，否则报告无从判断并发为什么是某个值")
        scheduler.observe_within_budget()
        self.assertIn(scheduler.soft_latency_policy, {"BOUNDED_RECOVERY", "RECOVERED", "REDUCE_FUTURE_CONCURRENCY"})
        self.assertEqual(scheduler.current_parallelism, 3, "有界恢复仍要把这一步还回去")

    def test_quality_blocked_does_not_request_reasoning_downgrade(self):
        scheduler = ConcurrentBatchScheduler(provider_name="mock", max_parallelism=1)
        policy = scheduler.observe_soft_latency(quality_blocked=True)
        self.assertTrue(policy["future_batch_split"])
        self.assertFalse(policy["latency_reasoning_downgrade_pending"])

    def test_factory_soft_latency_changes_subsequent_parallelism(self):
        class Slow(CountingProvider):
            def invoke_structured(self, request, task_dir):
                time.sleep(0.03)
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            provider = Slow({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-fast", "extra_options": {"soft_latency_budget": 0.01, "hard_timeout": 30}})
            result = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01", "S03-01"],
                task_mode="standard",
                require_section_min=False,
                speed_profile="balanced",
            )
            self.assertGreaterEqual(result["performance"]["soft_latency_observations"], 1)
            self.assertNotEqual(result["performance"].get("soft_latency_policy"), "NONE")

    def test_two_configured_models_differ_and_unknown_is_labelled(self):
        flash = latency_budget_seconds("qwen_modelstudio", "balanced", "draft", 1, model="qwen-flash")
        plus = latency_budget_seconds("qwen_modelstudio", "balanced", "draft", 1, model="qwen3.7-plus")
        unknown = latency_budget_seconds("qwen_modelstudio", "balanced", "draft", 1, model="totally-unknown-xyz")
        self.assertNotEqual(flash["soft_latency_budget"], plus["soft_latency_budget"])
        self.assertEqual(flash["provenance"], "model_specific_table")
        self.assertEqual(plus["provenance"], "model_specific_table")
        self.assertTrue(unknown["assumed_unknown_model"])
        self.assertEqual(unknown["provenance"], "unknown_model_assumed")
        original = latency_budget_seconds("mock", "balanced", "draft", 1)
        self.assertIn("soft_latency_budget", original)
        profile = resolve_profile("qwen_modelstudio", {"model": "totally-unknown-xyz"}, speed_profile="balanced", stage="draft")
        self.assertTrue(profile["effective_settings"]["latency_budget_assumed_unknown_model"])


class RateLimitAdmissionTest(unittest.TestCase):
    def test_fake_clock_concurrent_retry_oversized_unknown(self):
        clock = FakeClock()
        limiter = SlidingWindowLimiter(clock=clock, sleeper=clock.advance, window_seconds=60)
        cfg = {"rpm": 2, "tpm": 50, "extra_options": {"rpm": 2, "tpm": 50}}
        first = limiter.admit(provider_name="qwen_modelstudio", model="qwen-flash", account_ref="acct-a", config=cfg, estimated_input=10, estimated_output=10, deadline_seconds=1)
        second = limiter.admit(provider_name="qwen_modelstudio", model="qwen-flash", account_ref="acct-a", config=cfg, estimated_input=10, estimated_output=10, deadline_seconds=1)
        self.assertTrue(first.enforced and second.enforced)
        with self.assertRaises(AdmissionTimeout):
            limiter.admit(provider_name="qwen_modelstudio", model="qwen-flash", account_ref="acct-a", config=cfg, estimated_input=10, estimated_output=10, deadline_seconds=0.0)
        with self.assertRaises(AdmissionOversized):
            limiter.admit(provider_name="qwen_modelstudio", model="qwen-flash", account_ref="acct-a", config=cfg, estimated_input=40, estimated_output=20, deadline_seconds=1)
        unknown = limiter.admit(provider_name="mock", model="mock", account_ref="x", config={}, estimated_input=99999, estimated_output=99999, deadline_seconds=1)
        self.assertFalse(unknown.enforced)
        self.assertEqual(unknown.provenance, "unknown_limits")
        hits = []

        def worker(n):
            row = limiter.admit(provider_name="kimi_moonshot", model="kimi-k3", account_ref="acct-b", config={"extra_options": {"rpm": 4, "tpm": 1000}}, estimated_input=1, estimated_output=1, deadline_seconds=2)
            hits.append(n)
            return row

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(hits), 3)
        limiter.reconcile(first, actual_input=8, actual_output=8)


class CapabilityProbeTest(unittest.TestCase):
    def test_mock_probe_and_unconfigured_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "obs.json"
            mock = MockProvider({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "deterministic-mock-v0.1", "test_mode": True})
            row = observe_from_health(mock, mock.health_check(), store_path=store, force=True)
            self.assertEqual(row["status"], "PASS")
            self.assertIn("max_context", row["unverified_fields"])
            self.assertFalse(row["inferred_from_model_prefix"])
            empty = OpenAICompatibleProvider({
                "provider_name": "qwen_modelstudio",
                "provider_type": "openai_compatible",
                "enabled": False,
                "base_url": "",
                "model": "",
                "api_key_env": "NONE",
            })
            missing = observe_from_health(empty, empty.health_check(), store_path=store, force=True)
            self.assertEqual(missing["status"], "UNAVAILABLE")
            self.assertNotEqual(missing["status"], "PASS")


class TokenAndHistoryTest(unittest.TestCase):
    def test_cache_tokens_not_double_counted(self):
        usage = extract_token_usage(COMPLETE_ENVELOPE)
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["cache_read_tokens"], 100)
        self.assertNotEqual(usage["input_tokens"], 110)
        resolved = resolve_attempt_tokens(actual=usage, estimated_input=99, estimated_output=99)
        self.assertEqual(resolved["token_provenance"], "provider_usage")
        self.assertEqual(resolved["input_tokens"], 10)

    def test_late_qa_block_and_legacy_and_size_segregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            append_latency_sample(path, {"provider": "mock", "model": "mock", "mode": "balanced", "stage": "draft", "elapsed_seconds": 12, "success": True, "input_tokens": "estimate", "output_tokens": "estimate"})
            append_latency_sample(path, {
                "schema_version": "latency-history-v2",
                "provider": "mock",
                "model": "mock",
                "mode": "balanced",
                "stage": "draft",
                "elapsed_seconds": 30,
                "success": False,
                "eligible_for_eta": False,
                "final_outcome": "compliance_blocked",
                "size_bucket": "full_20_plus",
                "section_count": 27,
                "input_tokens": 10,
                "output_tokens": 10,
                "run_id": "blocked",
                "this_run_calls": 15,
            })
            for seconds in (600, 620, 640):
                append_latency_sample(path, {
                    "schema_version": "latency-history-v2",
                    "provider": "mock",
                    "model": "mock",
                    "mode": "balanced",
                    "stage": "draft",
                    "elapsed_seconds": seconds,
                    "success": True,
                    "eligible_for_eta": True,
                    "size_bucket": "full_20_plus",
                    "section_count": 27,
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "run_id": f"full-{seconds}",
                    "this_run_calls": 15,
                })
            append_latency_sample(path, {
                "schema_version": "latency-history-v2",
                "provider": "mock",
                "model": "mock",
                "mode": "balanced",
                "stage": "draft",
                "elapsed_seconds": 5,
                "success": True,
                "eligible_for_eta": True,
                "size_bucket": "single_1",
                "section_count": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "run_id": "tiny",
                "this_run_calls": 1,
            })
            full = estimate_runtime(provider="mock", model="mock", mode="balanced", stage="draft", history_path=path, section_count=27)
            tiny = estimate_runtime(provider="mock", model="mock", mode="balanced", stage="draft", history_path=path, section_count=1, min_samples=3)
            self.assertEqual(full["status"], "range")
            self.assertGreaterEqual(full["p50_seconds"], 600)
            self.assertNotEqual(tiny["status"], "range")
            self.assertGreaterEqual(full.get("excluded_legacy_or_unverified") or 0, 1)

    def test_duplicate_finalization_and_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            sample = {
                "schema_version": "latency-history-v2",
                "provider": "mock",
                "model": "mock",
                "mode": "balanced",
                "stage": "draft",
                "elapsed_seconds": 12,
                "success": True,
                "eligible_for_eta": True,
                "size_bucket": "single_1",
                "section_count": 1,
                "input_tokens": 3,
                "output_tokens": 4,
                "run_id": "dup-run",
                "this_run_calls": 2,
                "history_id": "dup-run|generation_completed|2|",
            }
            self.assertTrue(append_latency_sample(path, sample))
            self.assertFalse(append_latency_sample(path, sample))
            errors = []

            def writer(idx):
                try:
                    append_latency_sample(path, {
                        **sample,
                        "run_id": f"c-{idx}",
                        "history_id": f"c-{idx}",
                        "elapsed_seconds": 10 + idx,
                    })
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            rows = load_samples(path)
            self.assertGreaterEqual(len(rows), 9)

    def test_zero_generation_not_written_by_factory(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            history = Path(tmp) / "latency.jsonl"
            provider = CountingProvider()
            generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01"],
                task_mode="standard",
                require_section_min=False,
                history_path=history,
            )
            generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01"],
                task_mode="standard",
                require_section_min=False,
                history_path=history,
            )
            self.assertFalse(history.is_file() or (history.is_file() and history.read_text(encoding="utf-8").strip()))

    def test_numeric_tokens_and_not_invoked_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            result = generate_longform(
                run_dir=run,
                provider=CountingProvider(),
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01"],
                task_mode="standard",
                require_section_min=False,
            )
            tokens = result["performance"]["token_accounting"]
            self.assertIsInstance(tokens["input_tokens"], int)
            self.assertIsInstance(tokens["output_tokens"], int)
            self.assertNotEqual(tokens["input_tokens"], "estimate")
            self.assertEqual(result["performance"]["repair_reasoning"], "not_invoked")
            self.assertIsInstance(result["performance"]["qa_seconds"], (int, float))
            self.assertIsInstance(result["performance"]["planning_seconds"], (int, float))


class BridgeCompletionTest(unittest.TestCase):
    def _bridge(self, runner, tmp: Path):
        module = _load_bridge()
        exe = tmp / "grok.exe"
        exe.write_text("dummy", encoding="utf-8")
        cfg = tmp / "bridge_config.json"
        cfg.write_text(json.dumps({"reasoning_effort": "low", "model_alias": "grok-4.6", "expected_model": "grok-4.6", "timeout_seconds": 30}), encoding="utf-8")
        bridge = module.GrokBridge(cfg, command_runner=runner, executable=str(exe), version="test")
        bridge._local_login_ok = lambda: True
        return module, bridge

    def test_nonzero_exit_valid_response_known_sync_no_retry(self):
        calls = {"n": 0}

        def runner(args, cwd, timeout, proxy_env=None):
            calls["n"] += 1
            return SimpleNamespace(returncode=1, stdout=json.dumps(COMPLETE_ENVELOPE), stderr=AUX_STDERR)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module, bridge = self._bridge(runner, root)
            result = bridge.invoke(task_id="t", prompt="hello", working_directory=root, run_dir=root / "run", max_network_retries=2)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(result["audit"]["status"], "SUCCESS")
            self.assertEqual(result["audit"]["classification"], "COMPLETED_WITH_AUXILIARY_EXIT_SYNC")
            self.assertEqual(result["audit"]["cli_exit_code"], 1)
            self.assertNotIn("thought", result["envelope"])
            self.assertTrue((root / "run" / "provider_raw_envelope.json").is_file())
            saved = json.loads((root / "run" / "provider_raw_envelope.json").read_text(encoding="utf-8"))
            self.assertNotIn("thought", saved)

    def test_401_in_business_text_is_not_auth(self):
        def runner(args, cwd, timeout, proxy_env=None):
            return SimpleNamespace(returncode=0, stdout=json.dumps(COMPLETE_ENVELOPE), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module, bridge = self._bridge(runner, root)
            result = bridge.invoke(task_id="t", prompt="hello", working_directory=root, run_dir=root / "run", max_network_retries=0)
            self.assertEqual(result["audit"]["status"], "SUCCESS")

    def test_explicit_auth_failure(self):
        def runner(args, cwd, timeout, proxy_env=None):
            return SimpleNamespace(returncode=1, stdout="", stderr="not authenticated: login required")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module, bridge = self._bridge(runner, root)
            with self.assertRaises(module.GrokBridgeError) as ctx:
                bridge.invoke(task_id="t", prompt="hello", working_directory=root, run_dir=root / "run", max_network_retries=0)
            self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_REQUIRED")

    def test_wrong_model(self):
        envelope = dict(COMPLETE_ENVELOPE)
        envelope["modelUsage"] = {"grok-3": {"inputTokens": 1, "outputTokens": 1}}

        def runner(args, cwd, timeout, proxy_env=None):
            return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module, bridge = self._bridge(runner, root)
            with self.assertRaises(module.GrokBridgeError) as ctx:
                bridge.invoke(task_id="t", prompt="hello", working_directory=root, run_dir=root / "run", max_network_retries=0)
            self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_incomplete_body(self):
        def runner(args, cwd, timeout, proxy_env=None):
            return SimpleNamespace(returncode=0, stdout="{", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module, bridge = self._bridge(runner, root)
            with self.assertRaises(module.GrokBridgeError) as ctx:
                bridge.invoke(task_id="t", prompt="hello", working_directory=root, run_dir=root / "run", max_network_retries=0)
            self.assertIn(ctx.exception.code, {"OUTPUT_CONTRACT_ERROR", "PROVIDER_RUNTIME_ERROR"})

    def test_unknown_crash_not_success(self):
        def runner(args, cwd, timeout, proxy_env=None):
            return SimpleNamespace(returncode=1, stdout="", stderr="segmentation fault in cli")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module, bridge = self._bridge(runner, root)
            with self.assertRaises(module.GrokBridgeError) as ctx:
                bridge.invoke(task_id="t", prompt="hello", working_directory=root, run_dir=root / "run", max_network_retries=0)
            self.assertEqual(ctx.exception.code, "PROVIDER_RUNTIME_ERROR")

    def test_access_denied_is_not_login_required(self):
        module = _load_bridge()
        code = module.classify_cli_error_channels("", "Access is denied", 1, envelope_ok=False)
        self.assertEqual(code, "PERMISSION_RESTRICTED")
        self.assertNotEqual(code, "PROVIDER_AUTH_REQUIRED")

    def test_turn_limit_not_success(self):
        envelope = dict(COMPLETE_ENVELOPE)
        envelope["stopReason"] = "max_turns"

        def runner(args, cwd, timeout, proxy_env=None):
            return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module, bridge = self._bridge(runner, root)
            with self.assertRaises(module.GrokBridgeError) as ctx:
                bridge.invoke(task_id="t", prompt="hello", working_directory=root, run_dir=root / "run", max_network_retries=0)
            self.assertEqual(ctx.exception.code, "CLI_MAX_TURNS")


class SizeBucketTest(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(size_bucket(1), "single_1")
        self.assertEqual(size_bucket(8), "small_2_8")
        self.assertEqual(size_bucket(19), "medium_9_19")
        self.assertEqual(size_bucket(27), "full_20_plus")
        self.assertEqual(size_bucket(None), "unknown_size")


class IndependentRepairRegressions(unittest.TestCase):
    def test_limiter_releases_at_window_boundary(self):
        clock = [0.0]
        limiter = SlidingWindowLimiter(clock=lambda: clock[0], sleeper=lambda n: clock.__setitem__(0, clock[0] + n))
        args = dict(provider_name="mock", model="edge", account_ref="a", config={"rpm": 1}, estimated_input=1, estimated_output=1, deadline_seconds=61)
        limiter.admit(**args)
        limiter.admit(**args)
        self.assertGreaterEqual(clock[0], 60)

    def test_retry_after_without_configured_limits(self):
        clock = [0.0]
        limiter = SlidingWindowLimiter(clock=lambda: clock[0], sleeper=lambda n: clock.__setitem__(0, clock[0] + n))
        limiter.admit(provider_name="mock", model="edge", account_ref="a", config={}, estimated_input=1, estimated_output=1, deadline_seconds=10, retry_after=2)
        self.assertGreaterEqual(clock[0], 2)

    def test_invalid_limits_rejected(self):
        limiter = SlidingWindowLimiter()
        with self.assertRaises(AdmissionInvalidLimits):
            limiter.admit(provider_name="mock", model="edge", account_ref="a", config={"rpm": 0}, estimated_input=1, estimated_output=1, deadline_seconds=5)

    def test_admission_oversized_does_not_count_provider_calls(self):
        reset_default_limiter_for_tests(SlidingWindowLimiter())
        class Counted(CountingProvider):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            provider = Counted({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock", "extra_options": {"tpm": 10, "rpm": 100}})
            result = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01"],
                task_mode="standard",
                require_section_min=False,
            )
            self.assertEqual(provider.calls, 0)
            self.assertEqual(result["word"]["this_run_calls"], 0)
            self.assertEqual(result["word"]["provider_calls_total"], 0)
            self.assertGreaterEqual(result["performance"].get("admission_attempts") or 0, 1)

    def test_token_resume_keeps_cumulative_without_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            provider = CountingProvider()
            first = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01"],
                task_mode="standard",
                require_section_min=False,
            )
            first_tokens = first["performance"]["token_accounting"]["cumulative"]["input_tokens"]
            self.assertGreater(first_tokens, 0)
            second = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S01-01"],
                task_mode="standard",
                require_section_min=False,
            )
            self.assertEqual(second["word"]["this_run_calls"], 0)
            self.assertEqual(second["performance"]["token_accounting"]["this_run"]["input_tokens"], 0)
            self.assertEqual(second["performance"]["token_accounting"]["cumulative"]["input_tokens"], first_tokens)

    def test_partial_resume_ineligible_for_full_eta(self):
        import app_core
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            original = app_core.RUNTIME_DIR
            app_core.RUNTIME_DIR = runtime
            try:
                app_core._record_terminal_latency(
                    run_id="partial",
                    brief={"speed_profile": "balanced"},
                    provider_name="mock",
                    longform_result={
                        "word": {"this_run_calls": 1, "this_resume_calls": 1, "generation_batch_count": 15, "logical_section_count": 27},
                        "capability": {"effective_settings": {"model": "mock"}},
                        "performance": {},
                    },
                    status="generation_completed",
                    eligible=True,
                    actual_time=1.2,
                    qa_seconds=0.1,
                    build_id="test",
                )
                rows = load_samples(runtime / "latency_history.jsonl")
                self.assertTrue(rows)
                self.assertFalse(rows[-1].get("eligible_for_eta"))
                self.assertTrue(rows[-1].get("partial_resume"))
            finally:
                app_core.RUNTIME_DIR = original

    def test_provider_exception_writes_partial_audit_not_planning(self):
        import app_core
        import shutil
        class Boom(MockProvider):
            def invoke_structured(self, request, task_dir):
                raise RuntimeError("adapter exploded")
        # Round 2 §4.1 卫生项：数据根被显式重定向时（dist 侧 E2E）这份隔离目录跟着落到包外，
        # 便携包内保持逐文件不变；未重定向时仍是包内 runtime/tmp。
        override = os.environ.get("MMF_TEMP_ROOT")
        iso_base = Path(override).expanduser() if override else ROOT / "runtime" / "tmp"
        iso = iso_base / "r22_exception_test"
        if iso.exists():
            shutil.rmtree(iso)
        iso.mkdir(parents=True)
        original_runs = app_core.RUNS_DIR
        original_runtime = app_core.RUNTIME_DIR
        app_core.RUNS_DIR = iso / "runs"
        app_core.RUNTIME_DIR = iso / "runtime"
        app_core.RUNS_DIR.mkdir(parents=True)
        app_core.RUNTIME_DIR.mkdir(parents=True)
        try:
                catalog = app_core.corpus_catalog()
                ku = str((catalog.get("positive") or [{"ku_id": "KU-0001"}])[0]["ku_id"])
                selection = {
                    **_selection(),
                    "provider_name": "mock",
                    "recommended_positive": [{"ku_id": ku, "knowledge_name": "闭环", "summary": "方法", "reason": "测试"}],
                    "selected_positive_ku_ids": [ku],
                    "auto_selected_positive_ids": [ku],
                    "knowledge_usage_contracts": [{"ku_id": ku, "selection_status": "SELECTED", "usable_content": "闭环方法", "language_level": "method"}],
                    "provider_metadata": {"provider_name": "mock", "test_mode": True},
                }
                run = _write_run(app_core.RUNS_DIR, _pack(), {**_brief(), "provider_name": "mock"}, selection)
                run_id = run.name
                manager = app_core.provider_manager()
                manager.providers["mock"] = Boom({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock", "test_mode": True})
                try:
                    app_core.generate_artifact(run_id, [ku], {}, auto_repair=False)
                except app_core.MMFError:
                    pass
                audit_path = run / "run_audit.json"
                self.assertTrue(audit_path.is_file())
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                blob = json.dumps(audit, ensure_ascii=False)
                for path in (run / "longform" / "word" / "sections").glob("*/audit.json"):
                    blob += path.read_text(encoding="utf-8")
                self.assertIn("adapter exploded", blob)
                self.assertNotEqual(audit.get("status"), "planning_blocked")
        finally:
            app_core.RUNS_DIR = original_runs
            app_core.RUNTIME_DIR = original_runtime
            shutil.rmtree(iso, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
