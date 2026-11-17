"""MMF-007 final production-path fault tests PF-01~06.

No real provider is invoked. Faults are injected at the same adapter,
checkpoint, assembly and resume boundaries used by the product.
"""
from __future__ import annotations

import errno
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
for item in (APP, TESTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import test_mmf006e_r21_q01_q04 as r21
import test_mmf006e_r2_scheduler as scheduler_fixture
from longform.orchestrator import generate_longform
from longform.run_state import collect_facts, derive_run_state
from providers.base import ProviderUnavailableError
from providers.mock import MockProvider
from providers.openai_compatible import OpenAICompatibleProvider

MOCK_CONFIG = {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-pf"}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _new_run(root: Path) -> tuple[Path, dict, dict]:
    brief = {**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案"}
    selection = r21._selection()
    return r21._write_run(root, r21._complex_pack(), brief, selection), brief, selection


def _generate(run: Path, brief: dict, selection: dict, provider: MockProvider):
    return generate_longform(
        run_dir=run,
        provider=provider,
        provider_name="mock",
        brief=brief,
        selection=selection,
        selected_ids=["KU-0001"],
        task_mode="full_longform",
        require_section_min=True,
        speed_profile="fast",
    )


class PF01NetworkFailure(unittest.TestCase):
    def test_second_provider_batch_drops_then_resume_only_missing_cus(self):
        class DropSecond(MockProvider):
            def __init__(self):
                super().__init__(MOCK_CONFIG)
                self.calls = 0

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                # Keep the provider unavailable from the second production call
                # onward.  A one-shot disconnect is auto-repaired within the same
                # run and therefore cannot prove cross-run checkpoint/resume.
                if self.calls >= 2:
                    raise ConnectionError("PF-01 injected second-batch disconnect")
                return super().invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = _new_run(Path(tmp))
            _generate(run, brief, selection, DropSecond())
            before = set((_read(run / "checkpoint" / "state.json").get("completed_content_units") or []))
            self.assertTrue(before, "network failure must preserve completed CU checkpoint")
            resume = MockProvider(MOCK_CONFIG)
            _generate(run, brief, selection, resume)
            received = {cu for row in resume.received_requests for cu in row["content_unit_ids"]}
            self.assertTrue(received, "resume must request missing CU")
            self.assertTrue(received.isdisjoint(before), "resume must not resend completed CU")


class PF02RateLimit(unittest.TestCase):
    def test_http_429_through_openai_compatible_adapter_is_rate_limit(self):
        provider = OpenAICompatibleProvider({
            "provider_name": "qwen_fixture",
            "provider_type": "openai_compatible",
            "enabled": True,
            "base_url": "https://example.invalid/v1",
            "model": "fixture-model",
        })
        error = urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions", 429, "Too Many Requests",
            {"Retry-After": "1"}, io.BytesIO(b'{"message":"rate limit"}')
        )
        request = {"task_id": "PF-02", "system_prompt": "fixture", "prompt": "fixture", "required_keys": ["ok"]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(provider, "_configured", return_value=True), patch.object(provider, "_api_key", return_value=("fake", "test")), patch("providers.openai_compatible._http_open", side_effect=error):
            with self.assertRaises(ProviderUnavailableError) as caught:
                provider.invoke_structured(request, Path(tmp))
        self.assertEqual(caught.exception.error_code, "RATE_LIMIT")
        self.assertNotEqual(caught.exception.error_code, "AUTH_FAILED")


class PF03DiskFull(unittest.TestCase):
    def test_checkpoint_enospc_preserves_last_valid_and_blocks_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = _new_run(Path(tmp))
            _generate(run, brief, selection, MockProvider(MOCK_CONFIG))
            checkpoint = run / "checkpoint" / "state.json"
            before_bytes = checkpoint.read_bytes()
            target = str(checkpoint.resolve()).lower()
            import longform.hierarchical as hierarchical
            original = hierarchical.atomic_write_json

            def fail_checkpoint(path, value):
                if str(Path(path).resolve()).lower() == target:
                    raise OSError(errno.ENOSPC, "PF-03 no space left on device")
                return original(path, value)

            with patch("longform.hierarchical.atomic_write_json", side_effect=fail_checkpoint):
                with self.assertRaises(OSError):
                    _generate(run, brief, selection, MockProvider(MOCK_CONFIG))
            self.assertEqual(checkpoint.read_bytes(), before_bytes, "last valid checkpoint must remain byte-valid")
            self.assertFalse(any(checkpoint.parent.glob("*.tmp")), "no half checkpoint may remain")
            state = derive_run_state(collect_facts(run, medium="WORD", draft_delivery="COMPLETED_CLEAN"))
            self.assertNotEqual(state["delivery_status"], "COMPLETED_CLEAN")


class PF04AssemblyLock(unittest.TestCase):
    def test_permission_error_at_formal_docx_assembly_is_technical_failed(self):
        import app_core

        with tempfile.TemporaryDirectory() as tmp:
            catalog = app_core.corpus_catalog()
            root = Path(tmp)
            runs = root / "runs"
            runtime = root / "runtime"
            runs.mkdir()
            runtime.mkdir()
            brief = {**scheduler_fixture._brief(), "provider_name": "mock", "medium": "WORD"}
            selection = scheduler_fixture._selection()
            run = scheduler_fixture._write_run(runs, scheduler_fixture._pack(), brief, selection)
            provider = MockProvider(MOCK_CONFIG)
            manager = SimpleNamespace(get=lambda *args, **kwargs: provider)
            original_run = app_core.subprocess.run

            def lock_final_docx(command, *args, **kwargs):
                output = str(command[3]) if isinstance(command, list) and len(command) > 3 else ""
                if "volumes" not in output.replace("\\", "/"):
                    raise PermissionError("PF-04 simulated WPS file lock")
                return original_run(command, *args, **kwargs)

            with patch.object(app_core, "RUNS_DIR", runs), patch.object(app_core, "RUNTIME_DIR", runtime), patch.object(app_core, "RUNTIME_ROOT", root), patch.object(app_core, "_checked", side_effect=lambda value: Path(value)), patch.object(app_core, "_audit_read", return_value=None), patch.object(app_core, "corpus_catalog", return_value=catalog), patch.object(app_core, "provider_manager", return_value=manager), patch.object(app_core.subprocess, "run", side_effect=lock_final_docx):
                with self.assertRaises(app_core.MMFError):
                    app_core.generate_artifact(run.name, ["KU-0001"], {}, auto_repair=False)
            failure = _read(run / "technical_failure.json")
            self.assertEqual(failure["status"], "ASSEMBLY_FAILED")
            state = _read(run / "run_state.json")
            self.assertEqual(state["derived"]["delivery_status"], "TECHNICAL_FAILED")


class PF05CorruptCheckpoint(unittest.TestCase):
    def test_corrupt_checkpoint_through_formal_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = _new_run(Path(tmp))
            _generate(run, brief, selection, MockProvider(MOCK_CONFIG))
            checkpoint = run / "checkpoint" / "state.json"
            checkpoint.write_text('{"schema_version":"longform-check', encoding="utf-8")
            result = _generate(run, brief, selection, MockProvider(MOCK_CONFIG))
            self.assertIsNotNone(result["generated"]["artifact"])
            self.assertEqual(_read(checkpoint)["schema_version"], "longform-checkpoint-v0.3-hierarchical")


class PF06ResponseUnknown(unittest.TestCase):
    """V1.1 FAULT-10 契约在 MMF-007C Round 2 §7 下被有意收窄后再放开一格。

    旧契约：RESPONSE_UNKNOWN 之后任何路径都不得重发同一请求。
    新契约：普通 Draft/Repair/Resume 路径仍然一律不得重发；只有 Final Assembly 前的
    Reconciliation 阶段可以为一个未知请求花"最多一次"受控重发，超过即落到明确状态。
    这里两段都测：守卫没有放松，重发有上界。
    """

    def test_response_unknown_is_recorded_and_resume_does_not_resend(self):
        class Unknown(MockProvider):
            def invoke_structured(self, request, task_dir):
                self.received_requests.append({
                    "task_id": request.get("task_id"),
                    "generation_mode": request.get("generation_mode"),
                    "section_ids": list(request.get("section_ids") or ([request.get("section_id")] if request.get("section_id") else [])),
                    "content_unit_ids": [row.get("content_unit_id") for row in request.get("content_unit_contracts") or []],
                })
                raise TimeoutError("PF-06 response unknown after request accepted")

        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = _new_run(Path(tmp))
            first = Unknown(MOCK_CONFIG)
            _generate(run, brief, selection, first)
            rows = [json.loads(line) for line in (run / "provider_unknown_requests.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertIn("REQUEST_SENT", {row["status"] for row in rows})
            self.assertIn("RESPONSE_UNKNOWN", {row["status"] for row in rows})
            unknown_fingerprints = {row["request_fingerprint"] for row in rows if row["status"] == "RESPONSE_UNKNOWN"}
            calls_before = len(first.received_requests)
            resends_before = sum(1 for row in rows if row["status"] == "RESEND_CONTROLLED")
            _generate(run, brief, selection, first)
            rows_after = [json.loads(line) for line in (run / "provider_unknown_requests.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(unknown_fingerprints, {row["request_fingerprint"] for row in rows_after if row["status"] == "RESPONSE_UNKNOWN"})
            resends = [
                row for row in rows_after
                if row["status"] == "RESEND_CONTROLLED" and row["request_fingerprint"] in unknown_fingerprints
            ]
            resends_after = sum(1 for row in rows_after if row["status"] == "RESEND_CONTROLLED")
            # 新契约：Reconciliation 阶段的受控重发上界是"每个未知请求一次"。
            self.assertLessEqual(len(resends), len(unknown_fingerprints))
            self.assertEqual(
                len({row["request_fingerprint"] for row in resends}), len(resends),
                "同一逻辑请求出现第二次重发即违反 §7 的有界重发",
            )
            self.assertEqual(
                len(first.received_requests) - calls_before, resends_after - resends_before,
                "本次清扫新增的 Provider 调用必须与新增的受控重发记账一一对应",
            )
            # 旧契约仍然成立的部分：第二次清扫之后不得再有第三次。
            _generate(run, brief, selection, first)
            thrice = [json.loads(line) for line in (run / "provider_unknown_requests.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            # 这条判据过去数的是"RESEND_CONTROLLED 的总行数没变"，那是把两件事数成一件事：
            # 总行数既会因为"老请求被重发了第二次"而涨，也会因为"这一轮才长出一个新的未知
            # 请求、它按契约拿到唯一一次受控重发"而涨 —— 后者合法却被判成违规。
            # Round 4.1 把每技术页额度从 1,183 改成实测 930 之后批次切法变了，第三次清扫
            # 确实长出一个新指纹（探针 `r41_pf06_resend_ledger_probe.py`：12→13 个指纹，
            # 逐指纹重发次数全部 == 1），旧写法就把合法形状判红了。现在按**指纹**数。
            resent_by_fingerprint: dict[str, int] = {}
            for row in thrice:
                if row["status"] == "RESEND_CONTROLLED":
                    key = str(row["request_fingerprint"])
                    resent_by_fingerprint[key] = resent_by_fingerprint.get(key, 0) + 1
            unknown_now = {row["request_fingerprint"] for row in thrice
                           if row["status"] == "RESPONSE_UNKNOWN"}
            resent_before = {row["request_fingerprint"] for row in resends}
            self.assertEqual([], sorted(fp for fp, count in resent_by_fingerprint.items() if count > 1),
                             "§7 有界重发被违反：同一逻辑请求拿到了第二次受控重发")
            self.assertEqual(
                len([row for row in thrice if row["status"] == "RESEND_CONTROLLED"]),
                len(resent_by_fingerprint),
                "受控重发必须一指纹一行，出现第二次即违反 §7 的有界重发",
            )
            for fp in set(resent_by_fingerprint) - resent_before:
                self.assertIn(fp, unknown_now - unknown_fingerprints,
                              "第三次清扫新增的受控重发必须对应一个**本轮新出现**的未知请求，"
                              "而不是给老请求再花一次预算")


if __name__ == "__main__":
    unittest.main()
