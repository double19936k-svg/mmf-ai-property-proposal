from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1] / "app"
ROOT = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from longform.factory import LongformGenerationFactory, write_json
from longform.orchestrator import generate_longform
from longform.reasoning import classify_generation_failure, classify_generation_outcome, quality_failure_reason
from providers.capability import apply_to_request, native_reasoning_sequence, next_higher_reasoning, resolve_profile
from providers.grok_build import GrokBuildProvider
from providers.mock import MockProvider
from providers.openai_compatible import OpenAICompatibleProvider
from planning.planner import build_planning_bundle
from tests.test_mmf006e_r2_scheduler import CountingProvider, _brief, _complex_pack, _pack, _selection, _write_run


def _minimal_factory(tmp: Path, provider=None) -> LongformGenerationFactory:
    return LongformGenerationFactory(
        run_root=tmp,
        provider=provider or MockProvider({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock"}),
        provider_name="mock",
        inputs={
            "word_plan": {},
            "requirement_matrix": {},
            "section_contracts": {"contracts": [], "process_contracts": []},
            "global_state": {
                "canonical_terms": {},
                "client_requirements": [],
                "project_facts": {},
                "staffing": {},
                "service_hours": {},
                "sla_kpi": {},
                "service_scope": {},
                "excluded_scope": [],
                "commitment_registry": [],
                "topic_ownership": [],
                "canonical_roles": {},
            },
            "ppt_plan": {},
            "dependency_map": {"dependencies": []},
            "knowledge_selection": {"knowledge_usage_contracts": []},
            "require_section_min": False,
        },
    )


def _two_contexts() -> list[dict]:
    return [
        {
            "section_contract": {
                "section_id": "S01-01",
                "section_title": "概述",
                "must_cover": ["项目位于青岛"],
                "target_words": {"min": 80, "max": 200},
                "source_requirements": ["REQ-0001"],
                "required_processes": [],
            }
        },
        {
            "section_contract": {
                "section_id": "S01-02",
                "section_title": "秩序",
                "must_cover": ["门岗巡逻"],
                "target_words": {"min": 80, "max": 200},
                "source_requirements": ["REQ-0002"],
                "required_processes": [],
            }
        },
    ]


class BatchIdContractTest(unittest.TestCase):
    def test_current_batch_id_is_in_final_prompt_text(self):
        factory = _minimal_factory(Path(tempfile.mkdtemp()))
        prompt = factory._batch_prompt(_two_contexts(), batch_id="GB-01")
        self.assertIn("CURRENT_BATCH_ID = GB-01", prompt["prompt"])
        self.assertIn("batch_id必须等于CURRENT_BATCH_ID", prompt["prompt"])
        single = factory._word_prompt(_two_contexts()[0])
        self.assertNotIn("CURRENT_BATCH_ID", single["prompt"])
        self.assertNotIn("batch_id", json.dumps(factory._section_schema()))

    def test_missing_empty_mismatch_batch_id_are_contract_errors(self):
        factory = _minimal_factory(Path(tempfile.mkdtemp()))
        requested = ["S01-01", "S01-02"]
        rows = [{"section_id": sid, "title": sid} for sid in requested]
        _, missing = factory._split_batch_output({"sections": rows}, requested, expected_batch_id="GB-01")
        self.assertIn("batch_id_missing", missing)
        _, empty = factory._split_batch_output({"batch_id": "", "sections": rows}, requested, expected_batch_id="GB-01")
        self.assertIn("batch_id_missing", empty)
        _, mismatch = factory._split_batch_output({"batch_id": "GB-99", "sections": rows}, requested, expected_batch_id="GB-01")
        self.assertIn("batch_id_mismatch:GB-99", mismatch)
        fragments, ok = factory._split_batch_output({"batch_id": "GB-01", "sections": rows}, requested, expected_batch_id="GB-01")
        self.assertFalse(any(item.startswith("batch_id") for item in ok))
        self.assertEqual(set(fragments), set(requested))
        _, single = factory._split_batch_output({"section_id": "S01-01", "title": "x"}, ["S01-01"], expected_batch_id="GB-01")
        self.assertFalse(any(item.startswith("batch_id") for item in single))

    def test_openai_http_json_contains_current_batch_id(self):
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
            captured["raw"] = request.data.decode("utf-8")
            return DummyResp()

        original = oc._http_open
        oc._http_open = fake_open
        try:
            factory = _minimal_factory(Path(tempfile.mkdtemp()))
            prompt = factory._batch_prompt(_two_contexts(), batch_id="GB-01")
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
            request = {
                "task_id": "t-batch",
                "system_prompt": prompt["system"],
                "prompt": prompt["prompt"],
                "generation_mode": "longform_batch",
                "batch_id": "GB-01",
                "max_tokens": 8192,
                "timeout_seconds": 600,
                "required_keys": ["batch_id", "sections"],
                "json_schema": factory._batch_schema("GB-01"),
            }
            provider.invoke_structured(request, tmp / "task")
        finally:
            oc._http_open = original
        user_text = captured["body"]["messages"][1]["content"]
        self.assertIn("CURRENT_BATCH_ID = GB-01", user_text)
        self.assertIn("CURRENT_BATCH_ID = GB-01", captured["raw"])
        self.assertEqual(captured["body"]["messages"][1]["content"], prompt["prompt"])

    def test_grok_bridge_prompt_contains_current_batch_id(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("mmf_grok_bridge_r21", ROOT / "providers" / "grok_bridge" / "grok_bridge.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        captured = {}

        def runner(args, cwd, timeout, proxy_env=None):
            captured["args"] = args
            prompt_file = args[args.index("--prompt-file") + 1]
            captured["prompt_file_text"] = Path(prompt_file).read_text(encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "modelUsage": {"grok-4.6": {"tokens": 1}},
                "structuredOutput": {"batch_id": "GB-01", "sections": []},
                "text": "{\"batch_id\":\"GB-01\",\"sections\":[]}",
                "stopReason": "end_turn",
            }), stderr="")

        tmp = Path(tempfile.mkdtemp())
        exe = tmp / "grok.exe"
        exe.write_text("dummy", encoding="utf-8")
        cfg = tmp / "bridge_config.json"
        cfg.write_text(json.dumps({"reasoning_effort": "low", "model_alias": "grok-4.6", "expected_model": "grok-4.6", "timeout_seconds": 99}), encoding="utf-8")
        factory = _minimal_factory(tmp)
        prompt = factory._batch_prompt(_two_contexts(), batch_id="GB-01")
        bridge = module.GrokBridge(cfg, command_runner=runner, executable=str(exe), version="test")
        bridge._local_login_ok = lambda: True
        bridge.invoke(
            task_id="t-batch",
            prompt=prompt["prompt"],
            system_prompt=prompt["system"],
            working_directory=tmp,
            run_dir=tmp / "run",
            timeout_seconds=30,
            reasoning_effort="high",
            json_schema=factory._batch_schema("GB-01"),
            max_network_retries=0,
        )
        self.assertIn("CURRENT_BATCH_ID = GB-01", captured["prompt_file_text"])
        self.assertEqual(captured["prompt_file_text"], prompt["prompt"])
        schema_arg = captured["args"][captured["args"].index("--json-schema") + 1]
        self.assertIn("GB-01", schema_arg)

    def test_grok_build_adapter_forwards_prompt_not_local_batch_field(self):
        captured = {}
        factory = _minimal_factory(Path(tempfile.mkdtemp()))
        prompt = factory._batch_prompt(_two_contexts(), batch_id="GB-02")

        class FakeBridge:
            def invoke(self, **kwargs):
                captured["prompt"] = kwargs["prompt"]
                captured["schema"] = kwargs.get("json_schema")
                return {
                    "structured_output": {"batch_id": "GB-02", "sections": []},
                    "audit": {"session_id": "s", "request_id": "r", "started_at": "t", "finished_at": "t", "attempts": []},
                }

        provider = GrokBuildProvider({"provider_name": "grok_build", "provider_type": "grok_build", "enabled": True, "command": "grok"})
        provider._bridge = lambda: FakeBridge()
        tmp = Path(tempfile.mkdtemp())
        provider.invoke_structured({
            "task_id": "t2",
            "system_prompt": prompt["system"],
            "prompt": prompt["prompt"],
            "batch_id": "GB-02",
            "json_schema": factory._batch_schema("GB-02"),
        }, tmp / "task")
        self.assertIn("CURRENT_BATCH_ID = GB-02", captured["prompt"])
        self.assertEqual(captured["schema"]["properties"]["batch_id"].get("const"), "GB-02")

    def test_retry_uses_current_batch_id(self):
        seen = []

        class Recording(MockProvider):
            def __init__(self, config=None):
                super().__init__(config or {})
                self.rate_limited = set()

            def invoke_structured(self, request, task_dir):
                seen.append({"prompt": request.get("prompt"), "batch_id": request.get("batch_id"), "mode": request.get("generation_mode")})
                bid = request.get("batch_id")
                if request.get("generation_mode") == "longform_batch" and bid and bid not in self.rate_limited:
                    self.rate_limited.add(bid)
                    from providers.base import ProviderError
                    raise ProviderError("429 rate limit", error_code="RATE_LIMIT")
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            pack = _complex_pack()
            brief = {**_brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
            run = _write_run(Path(tmp), pack, brief, _selection())
            provider = Recording({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-r21"})
            generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
            )
        batch_prompts = [row for row in seen if row["mode"] == "longform_batch"]
        self.assertTrue(batch_prompts)
        by_id = {}
        for row in batch_prompts:
            self.assertIn("CURRENT_BATCH_ID = ", row["prompt"])
            current = [line for line in row["prompt"].splitlines() if line.startswith("CURRENT_BATCH_ID = ")][0]
            value = current.split("=", 1)[1].strip()
            self.assertEqual(value, row["batch_id"])
            by_id.setdefault(value, []).append(row["prompt"])
        retried = [bid for bid, prompts in by_id.items() if len(prompts) >= 2]
        self.assertTrue(retried)
        for bid in retried:
            for prompt in by_id[bid]:
                self.assertIn(f"CURRENT_BATCH_ID = {bid}", prompt)


class QualityBeforeLatencyTest(unittest.TestCase):
    def test_success_hard_late_must_missing_is_quality(self):
        outcome = classify_generation_outcome(
            {"status": "BLOCK", "must_cover": {"required": "MISSING"}, "commitment": {"status": "PASS"}},
            {"status": "SUCCESS", "latency_hard_exceeded": True},
        )
        self.assertEqual(outcome["failure_class"], "QUALITY_FAILURE")
        self.assertEqual(outcome["quality_failure_reason"], "MUST_REQUIREMENT_MISSING")
        self.assertTrue(outcome["latency_exceeded"])

    def test_hard_late_complete_qa_pass_is_latency(self):
        kind = classify_generation_failure(
            {"status": "PASS", "must_cover": {"required": "COVERED"}, "commitment": {"status": "PASS"}, "artifact_qa": {"status": "PASS"}, "length_status": "PASS"},
            {"status": "SUCCESS", "latency_hard_exceeded": True},
        )
        self.assertEqual(kind, "LATENCY_FAILURE")

    def test_other_quality_cases_beat_latency(self):
        cases = [
            ({"status": "BLOCK", "scoring_cover": {"SCR-1": "MISSING"}}, "SCORING_REQUIREMENT_MISSING"),
            ({"status": "BLOCK", "artifact_qa": {"status": "BLOCK"}, "must_cover": {}}, "ARTIFACT_QA_BLOCK"),
            ({"status": "BLOCK", "commitment": {"status": "BLOCK"}, "must_cover": {}}, "COMMITMENT_BLOCK"),
            ({"status": "BLOCK", "compliance": {"status": "BLOCK"}, "must_cover": {}}, "COMPLIANCE_BLOCK"),
            ({"status": "BLOCK", "contradiction": True, "must_cover": {}}, "CONTRADICTION"),
            ({"status": "WARNING", "length_status": "SECTION_UNDER_LENGTH", "must_cover": {"a": "COVERED"}}, "SECTION_DEPTH_FAILURE"),
        ]
        for gate, reason in cases:
            outcome = classify_generation_outcome(gate, {"status": "SUCCESS", "latency_hard_exceeded": True})
            self.assertEqual(outcome["failure_class"], "QUALITY_FAILURE", reason)
            self.assertEqual(outcome["quality_failure_reason"], reason)
        self.assertEqual(quality_failure_reason({"status": "BLOCK", "must_cover": {"a": "MISSING"}}), "MUST_REQUIREMENT_MISSING")


class RealReasoningEscalationTest(unittest.TestCase):
    def test_grok_balanced_medium_to_high(self):
        draft = resolve_profile("grok_build", {"model": "grok"}, speed_profile="balanced", stage="draft")
        self.assertEqual(draft["effective_settings"]["effective_reasoning"], "medium")
        retry = resolve_profile("grok_build", {"model": "grok"}, speed_profile="balanced", stage="repair", failure_class="QUALITY_FAILURE")
        self.assertEqual(retry["effective_settings"]["reasoning_before"], "medium")
        self.assertEqual(retry["effective_settings"]["reasoning_after"], "high")
        self.assertEqual(retry["effective_settings"]["reasoning_effort"], "high")
        self.assertTrue(retry["effective_settings"]["quality_escalation_requested"])
        self.assertTrue(retry["effective_settings"]["quality_escalation_applied"])
        self.assertIsNone(retry["effective_settings"]["escalation_unavailable_reason"])
        payload = apply_to_request({"task_id": "t", "prompt": "p", "system_prompt": "s"}, retry)
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_grok_high_to_xhigh_only_in_repair(self):
        deep = resolve_profile("grok_build", {}, speed_profile="deep", stage="draft")
        self.assertEqual(deep["effective_settings"]["effective_reasoning"], "high")
        blocked = resolve_profile("grok_build", {}, speed_profile="deep", stage="draft", failure_class="QUALITY_FAILURE")
        self.assertFalse(blocked["effective_settings"]["quality_escalation_applied"])
        self.assertEqual(blocked["effective_settings"]["effective_reasoning"], "high")
        self.assertEqual(blocked["effective_settings"]["escalation_unavailable_reason"], "QUALITY_ESCALATION_UNAVAILABLE")
        repaired = resolve_profile("grok_build", {}, speed_profile="deep", stage="repair", failure_class="QUALITY_FAILURE")
        self.assertEqual(repaired["effective_settings"]["reasoning_before"], "high")
        self.assertEqual(repaired["effective_settings"]["reasoning_after"], "xhigh")
        self.assertTrue(repaired["effective_settings"]["quality_escalation_applied"])
        self.assertEqual(apply_to_request({"prompt": "p", "system_prompt": "s", "task_id": "t"}, repaired)["reasoning_effort"], "xhigh")

    def test_kimi_sequence_and_ceiling(self):
        balanced = resolve_profile("kimi_moonshot", {}, speed_profile="balanced", stage="draft")
        self.assertEqual(balanced["effective_settings"]["effective_reasoning"], "high")
        fast_retry = resolve_profile("kimi_moonshot", {}, speed_profile="fast", stage="repair", failure_class="QUALITY_FAILURE")
        self.assertEqual(fast_retry["effective_settings"]["reasoning_before"], "low")
        self.assertEqual(fast_retry["effective_settings"]["reasoning_after"], "high")
        self.assertTrue(fast_retry["effective_settings"]["quality_escalation_applied"])
        unknown_seq = native_reasoning_sequence("kimi_moonshot", {"model": "unknown-model"})
        self.assertEqual(unknown_seq, ["low", "high"])
        unknown = next_higher_reasoning("kimi_moonshot", "high", stage="repair", config={"model": "unknown-model"})
        self.assertFalse(unknown["applied"])
        self.assertEqual(unknown["next_level"], "high")
        self.assertEqual(unknown["unavailable_reason"], "QUALITY_ESCALATION_UNAVAILABLE")
        retry = resolve_profile("kimi_moonshot", {"model": "unknown-model"}, speed_profile="balanced", stage="repair", failure_class="QUALITY_FAILURE")
        self.assertEqual(retry["effective_settings"]["reasoning_before"], "high")
        self.assertEqual(retry["effective_settings"]["reasoning_after"], "high")
        self.assertTrue(retry["effective_settings"]["quality_escalation_requested"])
        self.assertFalse(retry["effective_settings"]["quality_escalation_applied"])
        self.assertEqual(retry["effective_settings"]["escalation_unavailable_reason"], "QUALITY_ESCALATION_UNAVAILABLE")
        payload = apply_to_request({"task_id": "t", "prompt": "p", "system_prompt": "s"}, retry)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotEqual(payload["reasoning_effort"], "max")
        captured = {}

        class DummyResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": json.dumps({"section_id": "S01-01", "title": "t", "body_blocks": [], "tables": [], "processes": [], "callouts": [], "cross_references": [], "claims": [], "used_requirement_ids": [], "used_ku_ids": [], "generation_notes": []})}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        import providers.openai_compatible as oc

        def fake_open(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return DummyResp()

        original = oc._http_open
        oc._http_open = fake_open
        try:
            provider = OpenAICompatibleProvider({
                "provider_name": "kimi_moonshot",
                "provider_type": "openai_compatible",
                "enabled": True,
                "base_url": "https://api.moonshot.cn/v1",
                "model": "unknown-model",
                "api_key_env": "NONE",
            })
            provider._api_key = lambda: ("sk-test", "test")
            tmp = Path(tempfile.mkdtemp())
            provider.invoke_structured({
                "task_id": "kimi-unknown",
                "system_prompt": "s",
                "prompt": "p",
                "generation_mode": "longform_section",
                "timeout_seconds": 60,
                "max_tokens": 1024,
                **{k: payload[k] for k in ("reasoning_effort", "capability_profile") if k in payload},
            }, tmp / "task")
        finally:
            oc._http_open = original
        self.assertEqual(captured["body"].get("reasoning_effort"), "high")
        self.assertNotEqual(captured["body"].get("reasoning_effort"), "max")
        opt_in = resolve_profile(
            "kimi_moonshot",
            {"model": "unknown-model", "supported_reasoning_levels": ["low", "high", "max"]},
            speed_profile="balanced",
            stage="repair",
            failure_class="QUALITY_FAILURE",
        )
        self.assertEqual(opt_in["effective_settings"]["reasoning_after"], "max")
        self.assertTrue(opt_in["effective_settings"]["quality_escalation_applied"])
        for model in ("kimi-k2", "kimi-k2.5", "kimi-k2-unlisted"):
            unverified = next_higher_reasoning("kimi_moonshot", "high", stage="repair", config={"model": model})
            self.assertFalse(unverified["applied"])
            self.assertEqual(unverified["next_level"], "high")
        ceiling = next_higher_reasoning("kimi_moonshot", "max", stage="repair", config={"model": "kimi-k2"})
        self.assertFalse(ceiling["applied"])
        self.assertEqual(ceiling["next_level"], "max")
        self.assertEqual(ceiling["unavailable_reason"], "QUALITY_ESCALATION_UNAVAILABLE")

    def test_qwen_disabled_unavailable(self):
        nxt = next_higher_reasoning("qwen_modelstudio", "disabled", stage="repair")
        self.assertFalse(nxt["applied"])
        self.assertEqual(nxt["unavailable_reason"], "REASONING_ESCALATION_UNAVAILABLE")
        retry = resolve_profile("qwen_modelstudio", {"model": "qwen-flash"}, speed_profile="deep", stage="repair", failure_class="QUALITY_FAILURE")
        self.assertTrue(retry["effective_settings"]["quality_escalation_requested"])
        self.assertFalse(retry["effective_settings"]["quality_escalation_applied"])
        self.assertEqual(retry["effective_settings"]["effective_reasoning"], "disabled")
        self.assertEqual(retry["effective_settings"]["escalation_unavailable_reason"], "REASONING_ESCALATION_UNAVAILABLE")
        self.assertEqual(retry["effective_settings"]["reasoning_before"], retry["effective_settings"]["reasoning_after"])
        payload = apply_to_request({"task_id": "t", "prompt": "p", "system_prompt": "s"}, retry)
        self.assertFalse(payload.get("enable_thinking"))


class CheckpointStateTest(unittest.TestCase):
    def test_completed_pending_failed_exclusive_and_resume_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = _complex_pack()
            brief = {**_brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案", "speed_profile": "balanced"}
            run = _write_run(Path(tmp), pack, brief, _selection())
            provider = CountingProvider()
            first = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
            )
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
            completed = set(checkpoint["completed_batches"])
            pending = set(checkpoint["pending_batches"])
            failed = set(checkpoint["failed_batches"])
            self.assertFalse(completed & pending)
            self.assertFalse(completed & failed)
            self.assertFalse(pending & failed)
            self.assertTrue(completed)
            self.assertEqual(len(pending), 0)
            hashes = {}
            for path in (run / "longform" / "word" / "sections").glob("*/fragment.json"):
                hashes[path.parent.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            first_total = first["word"]["provider_calls_total"]
            self.assertEqual(first_total, provider.calls)
            self.assertEqual(first["word"]["this_run_calls"], provider.calls)
            self.assertEqual(first["word"]["this_resume_calls"], 0)
            second = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
            )
            resume_checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(provider.calls, first_total)
            self.assertEqual(second["word"]["provider_calls_total"], first_total)
            self.assertEqual(second["word"]["this_run_calls"], 0)
            self.assertEqual(second["word"]["this_resume_calls"], 0)
            self.assertFalse(set(resume_checkpoint["completed_batches"]) & set(resume_checkpoint["pending_batches"]))
            self.assertEqual(len(resume_checkpoint["pending_batches"]), 0)
            for path in (run / "longform" / "word" / "sections").glob("*/fragment.json"):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashes[path.parent.name])
            third = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
            )
            self.assertEqual(provider.calls, first_total)
            self.assertEqual(third["word"]["this_resume_calls"], 0)

    def test_all_failed_then_success_counts_resume_calls(self):
        class Empty(MockProvider):
            def __init__(self, config=None):
                super().__init__(config or {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-empty"})
                self.calls = 0

            def _section_fragment(self, request):
                frag = super()._section_fragment(request)
                frag["body_blocks"] = [{"type": "paragraph", "content": "短"}]
                # CU-aware generation treats returned CU fragments as the
                # authoritative body.  Remove them as well so this fixture still
                # represents an actually empty provider response.
                frag["content_unit_fragments"] = []
                return frag

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            pack = _complex_pack()
            brief = {**_brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案", "speed_profile": "balanced"}
            run = _write_run(Path(tmp), pack, brief, _selection())
            empty = Empty()
            first = generate_longform(
                run_dir=run,
                provider=empty,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=True,
                speed_profile="balanced",
            )
            self.assertEqual(first["word"]["this_resume_calls"], 0)
            self.assertGreater(empty.calls, 0)
            self.assertEqual(first["word"]["provider_calls_total"], empty.calls)
            first_total = empty.calls
            good = CountingProvider()
            second = generate_longform(
                run_dir=run,
                provider=good,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=True,
                speed_profile="balanced",
            )
            self.assertEqual(second["word"]["this_run_calls"], good.calls)
            self.assertEqual(second["word"]["this_resume_calls"], good.calls)
            self.assertEqual(second["word"]["provider_calls_total"], first_total + good.calls)
            # R2.4 permits two bounded targeted repairs per section. V1.1 Phase 2
            # splits execution by Volume, so per-section batching no longer merges
            # across volumes; assert bounded attempts instead of an exact topology count.
            self.assertGreaterEqual(first_total, 7)
            self.assertLessEqual(first_total, 60)
            # CU-aware resume requests only missing Content Units.  Exact call
            # topology is intentionally scheduler-dependent; verify bounded
            # real work instead of preserving the old section-only count.
            self.assertGreater(good.calls, 0)
            self.assertLessEqual(good.calls, 27)
            # 记账一致性：provider_calls_total 必须等于两段会话调用之和
            # （Volume 分批后初次调用按节展开，精确拓扑数不再是稳定不变式）。
            self.assertEqual(second["word"]["provider_calls_total"], first_total + good.calls)
            self.assertEqual(second["word"]["this_resume_calls"], good.calls)

    def test_fresh_first_run_this_resume_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = _complex_pack()
            brief = {**_brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
            run = _write_run(Path(tmp), pack, brief, _selection())
            provider = CountingProvider()
            first = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
            )
            self.assertEqual(first["word"]["this_resume_calls"], 0)
            self.assertEqual(first["word"]["this_run_calls"], provider.calls)
            self.assertGreater(provider.calls, 0)

    def test_partial_one_short_section_is_not_repaid(self):
        class OneShort(CountingProvider):
            def invoke_structured(self, request, task_dir):
                payload = dict(request)
                if request.get("section_id") == "S11-03":
                    payload["mock_short"] = True
                else:
                    payload["mock_fail_sections"] = ["S11-03"]
                return super().invoke_structured(payload, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            pack = _complex_pack()
            brief = {**_brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
            run = _write_run(Path(tmp), pack, brief, _selection())
            first_provider = OneShort()
            first = generate_longform(
                run_dir=run,
                provider=first_provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=True,
                speed_profile="balanced",
            )
            # MMF-007C：UNDER_LENGTH 不再触发整节续写。Generation 只记录深度缺口，
            # 预算化的补充/修复属于文档级阶段，所以初稿的修复调用必须为 0。
            self.assertEqual(first["word"]["SECTION_REPAIR_CALLS"], 0)
            self.assertEqual(first["word"]["CONTINUATION_CALLS"], 0)
            self.assertEqual(first["word"]["TECHNICAL_RETRY_CALLS"], 0)
            self.assertIn("S11-03", [row.get("section_id") for row in first["word"]["DEPTH_GAP_SECTIONS"]])
            self.assertEqual(first_provider.calls, first["word"]["TOTAL_PROVIDER_CALLS"])
            self.assertGreaterEqual(first["word"]["provider_calls_total"], first["word"]["INITIAL_BATCH_COUNT"])
            self.assertEqual(first["word"]["this_resume_calls"], 0)
            # R2.6/R2.7 Fail-Soft contract: a short section is committed with its body
            # (content QA decoupled from delivery). MMF-007C keeps the raw QA severity but
            # records the gap as COMPLETED_DEPTH_INCOMPLETE instead of re-paying for it.
            self.assertEqual(first["word"]["failed_batches"], [])
            short_dir = run / "longform" / "word" / "sections" / "S11-03"
            short_status = json.loads((short_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(short_status["status"], "COMPLETED_DEPTH_INCOMPLETE")
            self.assertTrue((short_dir / "fragment.json").is_file())
            short_qa = json.loads((short_dir / "qa.json").read_text(encoding="utf-8"))
            self.assertEqual(short_qa["provisional"]["reason"], "COMPLETED_DEPTH_INCOMPLETE")
            hashes = {
                path.parent.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (run / "longform" / "word" / "sections").glob("*/fragment.json")
            }
            good = CountingProvider()
            second = generate_longform(
                run_dir=run,
                provider=good,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=True,
                speed_profile="balanced",
            )
            # Resume must not redo warned sections, and must not lose any fragment.
            self.assertEqual(good.calls, 0)
            self.assertEqual(second["word"]["this_resume_calls"], 0)
            self.assertEqual(second["word"]["provider_calls_total"], first["word"]["provider_calls_total"])
            for sid, digest in hashes.items():
                path = run / "longform" / "word" / "sections" / sid / "fragment.json"
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_partial_failed_section_resume_preserves_hashes_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = _complex_pack()
            brief = {**_brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
            run = _write_run(Path(tmp), pack, brief, _selection())
            provider = CountingProvider()
            first = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
            )
            sections = sorted((run / "longform" / "word" / "sections").glob("*/fragment.json"))
            victim = sections[-1].parent
            hashes = {path.parent.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sections if path.parent != victim}
            status = json.loads((victim / "status.json").read_text(encoding="utf-8"))
            status["status"] = "FAILED_SECTION"
            (victim / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            for name in ("fragment.json", "provider_raw.json", "qa.json", "governance.json"):
                (victim / name).unlink(missing_ok=True)
            before = provider.calls
            second = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=brief,
                selection=_selection(),
                selected_ids=["KU-0001"],
                task_mode="full_longform",
                require_section_min=False,
                speed_profile="balanced",
            )
            self.assertEqual(provider.calls, before + 1)
            self.assertEqual(second["word"]["this_resume_calls"], 1)
            self.assertEqual(second["word"]["provider_calls_total"], first["word"]["provider_calls_total"] + 1)
            for sid, digest in hashes.items():
                path = run / "longform" / "word" / "sections" / sid / "fragment.json"
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_quality_repair_attempt_keeps_escalation_audit(self):
        """MMF-007C：深度缺口不在 Generation 整节续写，但质量类修复调用仍必须带原生升级审计。"""
        seen = []

        class Recording(MockProvider):
            def invoke_structured(self, request, task_dir):
                seen.append(dict(request))
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = build_planning_bundle(_pack(), _brief(), _selection(), production=True)
            contract = next(row for row in bundle["section_contracts"]["contracts"] if row["section_id"] == "S01-01")
            factory = LongformGenerationFactory(
                run_root=root,
                provider=Recording({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-depth"}),
                provider_name="mock",
                inputs={
                    "word_plan": bundle["word_plan"],
                    "requirement_matrix": bundle["requirement_matrix"],
                    "section_contracts": bundle["section_contracts"],
                    "global_state": bundle["global_state"],
                    "ppt_plan": bundle["ppt_plan"],
                    "dependency_map": bundle["dependency_map"],
                    "knowledge_selection": _selection(),
                    "require_section_min": True,
                    "speed_profile": "balanced",
                },
            )
            unit = factory.word_root / "sections" / "S01-01"
            unit.mkdir(parents=True)
            write_json(unit / "provider_raw.json", {
                "section_id": "S01-01",
                "title": contract["section_title"],
                "body_blocks": [{"type": "paragraph", "content": "项目位于青岛市崂山区香港东路108号。"}],
            })
            job = factory._job(factory.word_root, "WORD", 1)
            self.assertTrue(factory._repair_section(job, contract, "UNDER_LENGTH"))
            request = seen[-1]
            self.assertEqual(request.get("attempt_mode"), "continue_section")
            self.assertIn("previous_fragment", request.get("prompt") or "")
            self.assertEqual(request.get("reasoning_effort"), "high")
            profile = (request.get("capability_profile") or {}).get("effective_settings") or {}
            self.assertTrue(profile.get("quality_escalation_requested"))
            self.assertTrue(profile.get("quality_escalation_applied"))
            self.assertEqual(profile.get("reasoning_before"), "medium")
            self.assertEqual(profile.get("reasoning_after"), "high")
            attempts = json.loads((unit / "audit.json").read_text(encoding="utf-8")).get("attempts") or []
            continue_attempts = [row for row in attempts if row.get("mode") == "continue_section"]
            self.assertTrue(continue_attempts)
            last = continue_attempts[-1]
            self.assertTrue(last.get("quality_escalation_requested"))
            self.assertTrue(last.get("quality_escalation_applied"))
            self.assertIsNone(last.get("escalation_unavailable_reason"))
            self.assertEqual(last.get("reasoning_before"), "medium")
            self.assertEqual(last.get("reasoning_after"), "high")

    def test_failed_then_success_clears_failed_and_retry(self):
        hits = {"n": 0}

        class Flaky(MockProvider):
            def invoke_structured(self, request, task_dir):
                hits["n"] += 1
                if hits["n"] == 1 and request.get("generation_mode") == "longform_batch":
                    result = super().invoke_structured(request, task_dir)
                    result["batch_id"] = "WRONG-ID"
                    return result
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp), _pack(), _brief(), _selection())
            provider = Flaky({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-flaky"})
            result = generate_longform(
                run_dir=run,
                provider=provider,
                provider_name="mock",
                brief=_brief(),
                selection=_selection(),
                selected_ids=["KU-0001"],
                section_ids=["S03-01", "S03-02"],
                task_mode="standard",
                require_section_min=False,
            )
            checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
            completed = set(checkpoint["completed_batches"])
            pending = set(checkpoint["pending_batches"])
            failed = set(checkpoint["failed_batches"])
            self.assertFalse(completed & pending)
            self.assertFalse(completed & failed)
            retry_ids = {item.get("batch_id") if isinstance(item, dict) else item for item in checkpoint.get("retry_queue") or []}
            self.assertFalse(completed & retry_ids)
            self.assertGreaterEqual(result["word"]["provider_call_count"], 1)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
