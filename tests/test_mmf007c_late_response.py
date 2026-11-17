"""MMF-007C Round 2 §9：P0-1 Late Response Reconciliation（LATE-01…LATE-04）。

桥式 Provider 的真实失败模式是"请求已被接受、答案在主协程放弃之后才落地"。
这里用 MockProvider 当桥的替身：invoke 侧注入超时，collect_late_response 侧决定
晚到内容是否可得。被测的是生产链同一条 `_invoke → 请求台账 → Final Assembly 前
清扫 → 交付门` 路径，不是替身自己。

不变量：晚到内容不得因为主协程超时而丢弃；重发最多一次；未解决只能落到
NEEDS_REVIEW，已生成的 Word 仍要交付。
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
for item in (APP, TESTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import test_mmf007_production_faults as pf
from delivery_state import DELIVERY_REVIEW, classify_fail_soft
from governance.final_artifact_qa import evaluate_docx_artifact
from longform import request_ledger
from providers.mock import MockProvider

BRIDGE_CONFIG = {"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-late"}


class LateBridge(MockProvider):
    """命中 poison 条件的请求按"已发出未确认"处理，其余正常应答。

    Poison 按 Section 集合命中：批次请求和它拆分出的单 Section 请求都会超时。这才
    是桥式 Provider 的真实形状 —— 否则批次一失败就被拆成的单节请求立刻补上，晚到
    答案变成冗余，回收路径永远不会被走到。

    ``deliver_late`` 决定替身是否真的把晚到答案交出来；``max_timeouts`` 决定被 Poison
    的请求一共超时多少次；``timeout_attempts`` 限定只有第几次 attempt 会超时（None =
    每一次都超时），用来把"受控重发能成功"和"重发也救不回来"两种结局分开。
    """

    def __init__(self, config, *, max_timeouts: int = 1, deliver_late: bool = True,
                 timeout_attempts: set[int] | None = None):
        super().__init__(config)
        self.max_timeouts = max_timeouts
        self.deliver_late = deliver_late
        self.timeout_attempts = timeout_attempts
        self.in_flight: dict[str, dict] = {}
        self.poison_sections: set[str] = set()
        self.timed_out = 0
        self.late_probes = 0

    @staticmethod
    def _sections(request: dict) -> set[str]:
        sids = {str(x) for x in (request.get("section_ids") or [])}
        if request.get("section_id"):
            sids.add(str(request.get("section_id")))
        return sids

    @staticmethod
    def _attempt_no(request: dict) -> int:
        # request_id 形如 REQ-<run>-<fingerprint>-A<n>，attempt 号是替身唯一能看到的
        # "这是不是一次受控重发"的信号。
        request_id = str(request.get("request_id") or "")
        return int(request_id.rsplit("-A", 1)[1]) if "-A" in request_id else 1

    def invoke_structured(self, request, task_dir):
        sids = self._sections(request)
        hit = bool(sids & self.poison_sections) if self.poison_sections else bool(sids)
        if self.timeout_attempts is not None:
            hit = hit and self._attempt_no(request) in self.timeout_attempts
        if hit and self.timed_out < self.max_timeouts:
            if not self.poison_sections:
                self.poison_sections = set(sids)
            self.timed_out += 1
            self.in_flight[str(request.get("job_id") or "")] = copy.deepcopy(dict(request))
            raise TimeoutError("LATE bridge: request accepted, response did not arrive within the wait budget")
        return super().invoke_structured(request, task_dir)

    def collect_late_response(self, request, task_dir):
        self.late_probes += 1
        job_id = str(request.get("job_id") or "")
        payload = self.in_flight.get(job_id)
        if payload is None or not self.deliver_late:
            return None
        # 替身这里直接走 MockProvider：晚到答案不算一次新的 Provider 调用。
        output = MockProvider.invoke_structured(self, payload, task_dir)
        self.in_flight.pop(job_id, None)
        return output, {"provider": "late_bridge", "job_id": job_id, "finish_reason": "stop"}


def _ledger(run: Path) -> list[dict]:
    path = run / "provider_unknown_requests.jsonl"
    if not path.is_file():
        return []
    return request_ledger.read_events(path)


def _statuses(run: Path) -> set[str]:
    return {str(row.get("status") or "") for row in _ledger(run)}


def _request_ids(run: Path, status: str) -> set[str]:
    return {
        str(row.get("request_id") or "")
        for row in _ledger(run)
        if str(row.get("status") or "") == status
    }


def _reconciliation(run: Path) -> dict:
    return json.loads((run / "late_response_reconciliation.json").read_text(encoding="utf-8-sig"))


def _longform(result: dict) -> dict:
    return (result.get("generated") or {}).get("longform") or {}


def _completed_sections(run: Path) -> set[str]:
    return {
        path.parent.name
        for path in (run / "longform" / "word" / "sections").glob("*/status.json")
        if json.loads(path.read_text(encoding="utf-8-sig")).get("status")
        in {"COMPLETED", "COMPLETED_WITH_WARNING", "COMPLETED_CONDITIONAL"}
    }


class Late01RecoveredBeforeFinalAssembly(unittest.TestCase):
    def test_timeout_then_late_response_auto_recovers_section_and_cus(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = pf._new_run(Path(tmp))
            # 桥式 Provider 的真实形状：等待侧每次都超时，答案一律晚到，所以正文
            # 一直到清扫前都是缺的 —— 回收是唯一的补齐路径。
            bridge = LateBridge(BRIDGE_CONFIG, max_timeouts=99, deliver_late=True)
            result = pf._generate(run, brief, selection, bridge)

            summary = _longform(result).get("word_summary") or {}
            self.assertGreaterEqual(
                int(summary.get("LATE_RESPONSE_RECOVERED") or 0), 1,
                "晚到响应必须被自动回收，不得因为主协程超时而丢弃",
            )
            # 回收的请求走完正常生命周期。
            late_ids = _request_ids(run, request_ledger.RESPONSE_RECEIVED_LATE)
            self.assertTrue(late_ids, "台账必须记录 RESPONSE_RECEIVED_LATE")
            resolved = _request_ids(run, request_ledger.RESOLVED)
            self.assertTrue(late_ids <= resolved, "回收的晚到响应必须经 PERSISTED 走到 RESOLVED")
            self.assertIn(request_ledger.PERSISTED, _statuses(run))
            # 内容真的落到了产物，而不是只改了状态。
            poisoned = set(bridge.poison_sections)
            self.assertTrue(poisoned, "替身必须至少 Poison 一个批次")
            done = _completed_sections(run)
            self.assertTrue(
                poisoned <= done,
                f"回收后 Section 必须完整到达正文，仍缺：{sorted(poisoned - done)}",
            )
            persisted_cus = {
                str(cu)
                for row in _ledger(run)
                if row.get("status") == request_ledger.PERSISTED
                for cu in (row.get("persisted_content_unit_ids") or [])
            }
            self.assertTrue(persisted_cus, "PERSISTED 必须点名已落盘的 Content Unit")
            reconciliation = _reconciliation(run)
            self.assertGreaterEqual(int(reconciliation.get("late_recovered") or 0), 1)
            self.assertEqual(
                int(summary.get("CONTROLLED_RESENDS") or 0), 0,
                "晚到内容已回收时不得再花一次额度重发",
            )
            self.assertIsNotNone(result["generated"]["artifact"])


class Late02NoDuplicateRequest(unittest.TestCase):
    def test_late_arriving_before_finalization_never_duplicates_the_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = pf._new_run(Path(tmp))
            bridge = LateBridge(BRIDGE_CONFIG, max_timeouts=99, deliver_late=True)
            pf._generate(run, brief, selection, bridge)
            events = _ledger(run)
            self.assertGreaterEqual(
                len(_request_ids(run, request_ledger.RESPONSE_RECEIVED_LATE)), 1,
                "前置条件：本用例必须真的发生过晚到回收",
            )
            self.assertEqual(
                [row for row in events if row.get("status") == request_ledger.RESEND_CONTROLLED],
                [],
                "§7：响应已经晚到并落地后，不得再发起第二次同一请求",
            )
            self.assertEqual(int(_reconciliation(run).get("still_unknown") or 0), 0)
            self.assertEqual(_reconciliation(run).get("unresolved_content_unit_ids"), [])


class Late03BoundedControlledResend(unittest.TestCase):
    def test_late_that_never_arrives_gets_at_most_one_controlled_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = pf._new_run(Path(tmp))
            # attempt 1 一律不到；受控重发（attempt 2）允许正常应答。
            bridge = LateBridge(
                BRIDGE_CONFIG, max_timeouts=99, deliver_late=False, timeout_attempts={1},
            )
            pf._generate(run, brief, selection, bridge)
            events = _ledger(run)
            resends = [row for row in events if row.get("status") == request_ledger.RESEND_CONTROLLED]
            self.assertEqual(len(resends), 1, f"§7：晚到始终不到时最多一次受控重发，实际 {len(resends)}")
            self.assertEqual(int(_reconciliation(run).get("controlled_resends") or 0), 1)
            # 受控重发的 attempt 成功了：原悬空行必须落到明确状态，不能永远 UNKNOWN。
            self.assertIn(
                "superseded_by_controlled_resend",
                {str(row.get("resolution") or "") for row in events},
                "同一请求的悬空行必须由受控重发顶替，否则 still_unknown 永远收不敛",
            )
            self.assertEqual(int(_reconciliation(run).get("still_unknown") or 0), 0)
            self.assertEqual(int(_reconciliation(run).get("provider_calls") or 0), 1,
                             "受控重发只花一次额度")

    def test_resend_that_also_fails_does_not_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, brief, selection = pf._new_run(Path(tmp))
            bridge = LateBridge(BRIDGE_CONFIG, max_timeouts=99, deliver_late=False)
            pf._generate(run, brief, selection, bridge)
            events = _ledger(run)
            by_fingerprint: dict[str, list[str]] = {}
            for row in events:
                by_fingerprint.setdefault(str(row.get("request_fingerprint") or ""), []).append(
                    str(row.get("status") or ""))
            for fingerprint, statuses in by_fingerprint.items():
                self.assertLessEqual(
                    statuses.count(request_ledger.RESEND_CONTROLLED), 1,
                    f"§7：同一请求的重发上限是 1，指纹 {fingerprint[:8]} 出现了 "
                    f"{statuses.count(request_ledger.RESEND_CONTROLLED)} 次",
                )


class Late04StillDeliversWord(unittest.TestCase):
    def test_unrecovered_request_forces_needs_review_but_keeps_the_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, brief, selection = pf._new_run(root)
            bridge = LateBridge(BRIDGE_CONFIG, max_timeouts=99, deliver_late=False)
            result = pf._generate(run, brief, selection, bridge)

            artifact = result["generated"]["artifact"]
            self.assertTrue(artifact, "§8：缺节不得吞掉整份文档")
            reconciliation = _reconciliation(run)
            self.assertGreaterEqual(int(reconciliation.get("still_unknown") or 0), 1)
            self.assertTrue(
                reconciliation.get("unresolved_content_unit_ids"),
                "未回收的 Content Unit 必须可点名，供复核清单使用",
            )

            # 交付门：清扫结果进入最终 Artifact QA，严重性停在 NEEDS_REVIEW。
            docx = root / "final_artifact.docx"
            from docx import Document
            document = Document()
            document.add_paragraph(str(artifact.get("title") or "物业服务项目技术标"))
            document.save(str(docx))

            qa = evaluate_docx_artifact(docx, run_dir=run)
            rules = {str(row.get("rule_id") or "") for row in qa.get("findings") or []}
            self.assertIn("LATE_RESPONSE_UNRESOLVED", rules)
            self.assertNotIn(
                "BLOCK",
                {str(row.get("severity")) for row in qa.get("findings") or []
                 if row.get("rule_id") == "LATE_RESPONSE_UNRESOLVED"},
                "§8：晚到未回收只能 NEEDS_REVIEW，不得把已生成的 Word 扣住",
            )
            decision = classify_fail_soft(artifact_path=docx, artifact_qa=qa)
            self.assertEqual(decision["delivery_status"], DELIVERY_REVIEW)
            self.assertTrue(decision["artifact_available"], "Word 仍必须可下载")
            self.assertNotEqual(decision["delivery_status"], "COMPLETED_CLEAN")


if __name__ == "__main__":
    unittest.main()
