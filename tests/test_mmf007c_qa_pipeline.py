"""MMF-007C Generation & QA pipeline contract tests (QA-PIPE-01 … QA-PIPE-17).

These tests pin the new state machine on Mock/Synthetic evidence only:
Generation First → Local Normalize → Assemble → Whole-Artifact QA → Selective
Repair → Deliver.  Each test name carries its QA-PIPE id so a regression can be
traced back to the requirement it protects.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APP = PACKAGE_ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import app_core
import server
import workflow_timing
from governance.document_qa import (
    estimate_rendered_pages,
    evaluate_document_scale,
    grade_finding,
    regrade_findings,
    rendered_pages_for_artifact,
    summarize_severity,
)
from longform.depth_supplement import (
    MIN_SUPPLEMENT_CHARS,
    next_supplement_ids,
    novelty_gate,
    plan_depth_supplement,
    supplement_prompt_rules,
    supplement_unit_count,
)
from longform.envelope import parse_cu_envelope
from longform.local_normalize import run_local_normalization
from longform.orchestrator import generate_longform, normalize_generated_document
from longform.repair_policy import (
    MAX_AUTO_REPAIR_PER_ISSUE,
    PRIORITY_BLOCK,
    PRIORITY_DEPTH_GAP,
    PRIORITY_NEEDS_REVIEW,
    PRIORITY_REQUIREMENT_GAP,
    PRIORITY_SCORING_GAP,
    REPAIR_PRIORITY_ORDER,
    FastRepairBudget,
    GlobalRepairBudget,
    SEVERITY_MINOR,
    classify_section_issue,
    classify_under_length,
    sort_repair_candidates,
)
from planning.content_engineering import CONTENT_ARCHETYPES
from providers.mock import MockProvider


def _pack() -> dict:
    return {
        "schema_version": "tender-requirement-pack-v0.1",
        "pack_id": "PACK-TEST-QAPIPE",
        "status": "ready_for_plan",
        "project_facts": {
            "project_name": {"value": "QA PIPE 测试项目"},
            "location": {"value": "青岛市崂山区香港东路108号"},
            "gross_area": {"value": "120000平方米"},
        },
        "service_scope": {"included": [], "excluded": [], "deprioritized": [], "conditional": []},
        "requirements": [
            {"requirement_id": "REQ-0001", "normalized_requirement": "安全秩序管理含门岗巡逻与异常处置", "domain": "security", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED"},
            {"requirement_id": "REQ-0002", "normalized_requirement": "客户投诉闭环与回访", "domain": "sla_kpi", "mandatory_level": "MUST", "confirmation_status": "CONFIRMED", "scoring_item_id": "SCR-01"},
        ],
        "scoring_items": [{"scoring_item_id": "SCR-01", "must_respond": True, "label": "服务方案"}],
        "confirmation": {"ready_for_brief_seed": True},
    }


def _brief() -> dict:
    return {
        "project_name": "QA PIPE 测试项目",
        "project_type": "综合体",
        "scenario": "完整物业服务方案",
        "medium": "WORD",
        "provider_name": "mock",
        "requirements": "安全秩序与客服闭环",
    }


def _selection() -> dict:
    return {
        "provider_name": "mock",
        "recommended_positive": [{"ku_id": "KU-0001"}],
        "applicable_guardrails": [],
        "knowledge_usage_contracts": [{"ku_id": "KU-0001", "selection_status": "SELECTED", "usable_content": "闭环方法", "language_level": "method"}],
        "selected_positive_ku_ids": ["KU-0001"],
        "auto_selected_positive_ids": ["KU-0001"],
    }


def _provider(name: str = "mock") -> MockProvider:
    return MockProvider({"provider_name": name, "provider_type": "mock", "enabled": True, "model": "mock-qa"})


def _write_run(root: Path) -> Path:
    run = root / "run"
    (run / "tender").mkdir(parents=True)
    for name, value in (("tender/requirement_pack.json", _pack()), ("brief.json", _brief()), ("knowledge_selection.json", _selection())):
        (run / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return run


def _generate(run: Path, provider, *, sections=("S01-01", "S05-02"), name: str = "mock"):
    return generate_longform(
        run_dir=run,
        provider=provider,
        provider_name=name,
        brief=_brief(),
        selection=_selection(),
        selected_ids=["KU-0001"],
        section_ids=list(sections),
        task_mode="standard",
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _scratch_root() -> Path:
    """Round 2 §4.1 卫生项：默认写包内 runtime/tmp 在源码树里无害（打包排除集与 gitignore 双重
    挡住），但在便携包里跑测就是往"被验收的那份字节"里追加文件；进程被强杀时连删都来不及删
    ——Round 2 归档的残留里就有 `qapipe`。数据根被显式重定向时（dist 侧 E2E）scratch 跟着出去。
    """
    override = os.environ.get("MMF_TEMP_ROOT")
    base = Path(override).expanduser() if override else PACKAGE_ROOT / "runtime" / "tmp"
    return base / "qapipe"


SCRATCH_ROOT = _scratch_root()
_scratch_seq = itertools.count()


def _scoped_root(label: str) -> Path:
    """app_core 的落盘受工作范围校验限制，写盘类测试只能落在允许根内的 scratch 目录。"""
    root = SCRATCH_ROOT / f"{label}-{os.getpid()}-{next(_scratch_seq)}"
    root.mkdir(parents=True, exist_ok=True)
    return root


class QaPipeGenerationFirstTests(unittest.TestCase):
    def test_qa_pipe_01_ordinary_quality_gap_persists_and_generation_continues(self):
        """QA-PIPE-01: 普通质量问题记为 PROVISIONAL，Generation 继续到下一节。"""
        class ShortFirstCall:
            def __init__(self):
                self.calls = 0
                self.inner = _provider()
                self.config = self.inner.config

            def get_metadata(self):
                return self.inner.get_metadata()

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                payload = dict(request)
                if self.calls == 1:
                    payload["mock_short"] = True
                return self.inner.invoke_structured(payload, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            provider = ShortFirstCall()
            result = _generate(run, provider)
            states = result["word"]["section_states"]
            draft_states = _read(Path(result["first_draft"]["path"]))["section_states"]
            self.assertIn("COMPLETED_DEPTH_INCOMPLETE", draft_states.values(), "深度缺口必须带着初稿落盘")
            self.assertEqual(len(draft_states), 2, "缺口不得阻止后续Section继续生成")
            self.assertEqual(len(states), 2, "缺口不得阻止后续Section继续生成")
            self.assertTrue(result["generated"]["artifact"]["sections"])
            word = result["word"]
            budget = word["global_repair_budget"] or {}
            self.assertLessEqual(
                word["TOTAL_CALLS"] - word["INITIAL_GENERATION_CALLS"],
                int(budget.get("max_repair_calls") or 0),
                "QA之后的额外调用必须落在有界修复预算内",
            )
            self.assertEqual(word["TECHNICAL_RETRY_CALLS"], 0)
            self.assertEqual(word["SECTION_REPAIR_CALLS"], 0, "普通质量缺口不得走整节重写")
            self.assertEqual(word["CONTINUATION_CALLS"], 0)

    def test_qa_pipe_02_technical_failure_gets_bounded_retry_and_non_technical_gets_none(self):
        """QA-PIPE-02: 只有技术故障可立即重试，且每次只重请求未到达的单元。"""
        seen: list[str] = []

        class Flaky:
            def __init__(self):
                self.failed: set[str] = set()
                self.inner = _provider("mock-flaky")
                self.config = self.inner.config

            def get_metadata(self):
                return self.inner.get_metadata()

            def invoke_structured(self, request, task_dir):
                key = str(request.get("section_id") or request.get("batch_id") or "")
                seen.append(key)
                if key and key not in self.failed:
                    self.failed.add(key)
                    from providers.base import ProviderError
                    raise ProviderError("simulated network drop", error_code="NETWORK_ERROR")
                return self.inner.invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            result = _generate(run, Flaky(), name="mock-flaky")
            word = result["word"]
            self.assertGreater(word["TECHNICAL_RETRY_CALLS"], 0, "技术故障必须立即重试")
            self.assertLessEqual(word["TECHNICAL_RETRY_CALLS"], word["TECHNICAL_RETRY_BUDGET"])
            self.assertEqual(word["SECTION_REPAIR_CALLS"], 0, "技术重试不得升级成整书修复")
            self.assertEqual(word["CONTINUATION_CALLS"], 0)
            self.assertTrue(all(status.startswith("COMPLETED") for status in word["section_states"].values()), word["section_states"])
            self.assertEqual(word["TOTAL_CALLS"], len(seen), "每一次Provider调用都必须留下请求记录")

    def test_qa_pipe_03_under_length_never_reruns_the_section(self):
        """QA-PIPE-03: UNDER_LENGTH 只记录 DEPTH_INCOMPLETE，绝不重跑整节。"""
        class AlwaysShort:
            def __init__(self):
                self.calls = 0
                self.inner = _provider()
                self.config = self.inner.config

            def get_metadata(self):
                return self.inner.get_metadata()

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                return self.inner.invoke_structured({**request, "mock_short": True}, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            provider = AlwaysShort()
            result = _generate(run, provider, sections=("S01-01",))
            word = result["word"]
            budget = word["global_repair_budget"] or {}
            max_repair_calls = int(budget.get("max_repair_calls") or 0)
            initial_calls = int(word["INITIAL_GENERATION_CALLS"] or 0)
            self.assertEqual(initial_calls, word["generation_batch_count"] or 1, "初稿阶段一批一次调用")
            self.assertLessEqual(
                provider.calls - initial_calls, max_repair_calls,
                "长度缺口的后续调用受有界预算约束，不得无限重跑",
            )
            self.assertEqual(word["CONTINUATION_CALLS"], 0, "不得用续写整节来补长度")
            self.assertEqual(word["SECTION_REPAIR_CALLS"], 0, "UNDER_LENGTH 不得走整节重写")
            self.assertLessEqual(word["SUPPLEMENT_CALLS"], max_repair_calls)
            self.assertIn("S01-01", [row["section_id"] for row in word["DEPTH_GAP_SECTIONS"]])
            self.assertTrue((run / "longform" / "word" / "sections" / "S01-01" / "fragment.json").is_file())


class QaPipeLocalNormalizationTests(unittest.TestCase):
    def test_qa_pipe_04_local_normalization_runs_without_any_provider_call(self):
        """QA-PIPE-04: LOCAL_NORMALIZATION 就地清洗片段，Provider 调用为零。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            result = _generate(run, _provider(), sections=("S01-01",))
            calls_after_generation = len(list((run / "longform" / "word" / "sections" / "S01-01").glob("provider_attempt_*")))
            unit = run / "longform" / "word" / "sections" / "S01-01"
            fragment = _read(unit / "fragment.json")
            fragment["body_blocks"] = [
                {"type": "paragraph", "content": "```\n本项目秩序维护实行门岗值守与分区巡逻相结合。\n```"},
                {"type": "paragraph", "content": "无效空块"},
                {"type": "paragraph", "content": ""},
            ] + fragment.get("body_blocks", [])[1:]
            (unit / "fragment.json").write_text(json.dumps(fragment, ensure_ascii=False, indent=2), encoding="utf-8")

            report = normalize_generated_document(run, ["S01-01"])
            self.assertFalse(report["provider_called"])
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["sections_scanned"], 1)
            self.assertGreaterEqual(report["fix_counts"].get("fence_removed", 0), 1)
            self.assertGreaterEqual(report["fix_counts"].get("drop_empty_block", 0), 1)
            self.assertTrue((run / "longform" / "word" / "local_normalization_report.json").is_file())
            self.assertEqual(
                len(list((run / "longform" / "word" / "sections" / "S01-01").glob("provider_attempt_*"))),
                calls_after_generation,
                "归一化不得留下任何新的Provider证据",
            )
            self.assertEqual(result["local_normalization"]["provider_called"], False)
            self.assertEqual(_read(run / "orchestrator_status.json")["LOCAL_NORMALIZATION"], "PASS")

    def test_qa_pipe_04b_normalization_reports_clean_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _write_run(root)
            (run / "longform" / "word" / "sections" / "S09-09").mkdir(parents=True)
            (run / "longform" / "word" / "sections" / "S09-09" / "fragment.json").write_text(
                json.dumps({"section_id": "S09-09", "title": "已干净", "body_blocks": [{"type": "paragraph", "content": "内容完整。"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            report = run_local_normalization(run)
            self.assertEqual(report["sections"]["S09-09"]["status"], "CLEAN")
            self.assertEqual(report["fix_counts"], {})


class QaPipeEnvelopeTests(unittest.TestCase):
    def test_qa_pipe_05_truncated_batch_keeps_every_arrived_content_unit(self):
        """QA-PIPE-05: 4块完整 + 1块截断 → 保留4块，只重请求第5块。"""
        requested = [f"CU-S05-01-0{i}" for i in range(1, 6)]
        blocks = [
            f"<<<CU_START:{cu_id}>>>\n第{i}单元的措施内容：定岗、定量、定时执行并留痕记录。\n<<<CU_END>>>"
            for i, cu_id in enumerate(requested[:4], start=1)
        ]
        text = "\n\n".join(blocks) + f"\n\n<<<CU_START:{requested[4]}>>>\n第五单元的处置内容写到一半就被输出上限截断了"
        parsed = parse_cu_envelope(text, requested_ids=requested)
        self.assertEqual(parsed["protocol"], "cu_envelope")
        self.assertEqual(sorted(parsed["complete_ids"]), sorted(requested[:4]))
        self.assertEqual(parsed["truncated_ids"], [requested[4]])
        self.assertEqual(parsed["missing_ids"], [])
        self.assertIn("定岗", parsed["blocks"][requested[0]]["content"])
        self.assertTrue(parsed["blocks"][requested[4]]["content"], "截断块也已到达的文字不得丢弃")

    def test_qa_pipe_05b_envelope_is_only_used_for_text_dominant_batches(self):
        from longform.envelope import should_use_envelope
        self.assertTrue(should_use_envelope(["NARRATIVE"] * 4 + ["SOP"]))
        self.assertFalse(should_use_envelope(["STANDARD_TABLE", "MATRIX", "KPI_TABLE", "CHECKLIST"]))

    def test_qa_pipe_05c_payload_only_structured_content_unit_is_not_empty(self):
        """QA-PIPE-05c: 只带 payload 的结构化单元也是"到达"，两者皆空才算空单元。

        真实 Run 里表格类单元返回 {title,columns,rows} 而不带叙述摘要，被判
        empty_content_unit 后整节卡住并额外花费一次技术重试。
        """
        from longform.factory import LongformGenerationFactory

        root = Path(tempfile.mkdtemp())
        try:
            factory = object.__new__(LongformGenerationFactory)
            factory.word_root = root / "longform" / "word"
            factory.cus_by_section = {"S03-01": ["CU-S03-01-01", "CU-S03-01-02", "CU-S03-01-03"]}
            factory.cu_contracts = {
                "CU-S03-01-01": {"archetype": "STANDARD_TABLE"},
                "CU-S03-01-02": {"archetype": "CHECKLIST"},
                "CU-S03-01-03": {"archetype": "NARRATIVE"},
            }
            factory.returned_content_unit_ids = []
            fragment = {"content_unit_fragments": [
                {"content_unit_id": "CU-S03-01-01", "archetype": "STANDARD_TABLE", "content": "",
                 "payload": {"title": "岗位职责与接口表", "columns": ["岗位", "编制", "班次"],
                             "rows": [["客服主管", "1人", "常白班"], ["秩序领班", "2人", "两班倒"]]}},
                {"content_unit_id": "CU-S03-01-02", "archetype": "CHECKLIST", "content": "",
                 "payload": {"items": ["每日门岗签到核查", "每周甲方联合检查"]}},
                {"content_unit_id": "CU-S03-01-03", "archetype": "NARRATIVE", "content": "", "payload": {}},
            ]}
            returned, issues = factory._persist_provider_cus(
                "S03-01", fragment, list(factory.cus_by_section["S03-01"])
            )
            self.assertEqual(returned, ["CU-S03-01-01", "CU-S03-01-02"])
            self.assertIn("empty_content_unit:CU-S03-01-03", issues)
            self.assertIn("missing_content_unit:CU-S03-01-03", issues)
            stored = _read(factory.word_root / "sections" / "S03-01" / "cus" / "CU-S03-01-01.json")
            self.assertEqual(stored["content"], "", "不得为结构化单元编造叙述正文")
            self.assertEqual(stored["payload"]["rows"][0][0], "客服主管")
            self.assertGreater(stored["effective_chars"], 0)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class QaPipeTransportIsolationTests(unittest.TestCase):
    """QA-PIPE-14: 传输语法只能存在于 Provider 响应里，绝不能进入客户可见产物。

    真实 300-Class 试炼交付的 DOCX 里出现 34 个 `<<<CU_END>>>`，因为解析器把
    结束标记本身当成了正文的一部分。传输协议是内部实现细节，客户看到它即等于
    看到系统内部结构，属于必须阻断的交付缺陷。
    """

    def test_qa_pipe_14_envelope_terminator_never_reaches_content(self):
        text = (
            "<<<CU_START:CU-S05-01-01>>>\n前台定岗定责，异常十分钟内上报。\n<<<CU_END>>>\n\n"
            "<<<CU_START:CU-S05-01-02 | arch=NARRATIVE>>>\n夜间巡逻按项目确认的频次执行。\n<<<CU_END>>>\n<<<CU_END>>>\n\n"
            "<<<CU_START:CU-S05-01-03>>>\n第三单元写到一半就被输出上限截断了"
        )
        parsed = parse_cu_envelope(
            text,
            requested_ids=["CU-S05-01-01", "CU-S05-01-02", "CU-S05-01-03"],
            archetypes={"CU-S05-01-02": "NARRATIVE"},
        )
        for cu_id, row in parsed["blocks"].items():
            self.assertNotIn("<<<", row["content"], f"{cu_id} 正文残留传输标记")
            self.assertNotIn("CU_END", row["content"], f"{cu_id} 正文残留传输标记")
        self.assertTrue(parsed["blocks"]["CU-S05-01-01"]["terminated"])
        self.assertIn("定岗", parsed["blocks"]["CU-S05-01-01"]["content"])
        self.assertTrue(parsed["blocks"]["CU-S05-01-03"]["content"], "截断单元已到达的文字不得丢弃")

    def test_qa_pipe_14b_local_normalization_scrubs_residue_with_an_audit_trail(self):
        """已落盘的残缺正文由 §9 本地整备清洗，并留下可审计的修复记录。"""
        root = _scoped_root("residue")
        try:
            run = _write_run(root)
            section = run / "longform" / "word" / "sections" / "S05-01"
            (section / "cus").mkdir(parents=True)
            (section / "cus" / "CU-S05-01-01.json").write_text(json.dumps({
                "content_unit_id": "CU-S05-01-01",
                "archetype": "NARRATIVE",
                "content": "门岗进出登记齐全。<<<CU_END>>>\n<<<CU_START:CU-S05-01-01>>>\nvehicle",
            }, ensure_ascii=False), encoding="utf-8")
            (section / "fragment.json").write_text(json.dumps({
                "section_id": "S05-01",
                "title": "秩序维护",
                "body_blocks": [
                    {"type": "paragraph", "content": "门岗进出登记齐全。<<<CU_END>>>"},
                    {"type": "subheading", "content": "车辆管理<<<CU_END>>>"},
                ],
                "tables": [{"title": "岗位职责表", "columns": ["岗位", "班次"],
                            "rows": [["客服主管", "两班倒<<<CU_END>>>"]]}],
                "processes": [{"title": "电梯困人处置", "steps": ["1. 安抚乘客<<<CU_END>>>"]}],
            }, ensure_ascii=False), encoding="utf-8")

            report = run_local_normalization(run)

            self.assertEqual(report["fix_counts"].get("transport_residue_removed", 0), 4, report["fix_counts"])
            self.assertEqual(report["sections"]["S05-01"]["status"], "NORMALIZED")
            fragment = _read(section / "fragment.json")
            blob = json.dumps(fragment, ensure_ascii=False)
            self.assertNotIn("<<<", blob, "整备后的 Section 片段仍含传输标记")
            self.assertNotIn("CU_END", blob)
            self.assertIn("门岗进出登记齐全", fragment["body_blocks"][0]["content"])
            self.assertIn("两班倒", fragment["tables"][0]["rows"][0][1], "清洗只去标记，不得删掉单元格内容")
            cu = _read(section / "cus" / "CU-S05-01-01.json")
            self.assertNotIn("<<<", cu["content"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_qa_pipe_14c_document_qa_blocks_residue_and_local_repair_clears_it(self):
        """整篇 QA 必须把传输标记判为 BLOCK，且本地修复不需要再花一次调用。"""
        from governance.artifact_qa import apply_artifact_repairs, evaluate_artifact

        generated = {"artifact": {"sections": [
            {"title": "秩序维护", "paragraphs": ["门岗进出登记齐全。<<<CU_END>>>", "巡查按项目确认的频次执行。"]},
        ]}}
        findings = [row for row in evaluate_artifact(generated)["findings"]
                    if row["rule_id"] == "ENVELOPE_TRANSPORT_RESIDUE"]
        self.assertTrue(findings, "传输标记泄漏未被整篇 QA 发现")
        self.assertEqual(findings[0]["severity"], "BLOCK")
        self.assertEqual(evaluate_artifact(generated)["status"], "BLOCK")

        repaired = apply_artifact_repairs(generated)
        self.assertNotIn("<<<", json.dumps(repaired, ensure_ascii=False))
        self.assertIn("门岗进出登记齐全", repaired["artifact"]["sections"][0]["paragraphs"][0])
        self.assertEqual(
            [row for row in evaluate_artifact(repaired)["findings"]
             if row["rule_id"] == "ENVELOPE_TRANSPORT_RESIDUE"], [],
        )


class QaPipeFirstDraftTests(unittest.TestCase):
    def test_qa_pipe_06_first_draft_is_persisted_before_any_document_qa(self):
        """QA-PIPE-06: 装配完成的初稿必须先落盘，即使带着 PROVISIONAL 缺口。"""
        class AlwaysShort:
            def __init__(self):
                self.inner = _provider()
                self.config = self.inner.config

            def get_metadata(self):
                return self.inner.get_metadata()

            def invoke_structured(self, request, task_dir):
                return self.inner.invoke_structured({**request, "mock_short": True}, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            result = _generate(run, AlwaysShort(), sections=("S01-01", "S05-02"))
            draft = result["first_draft"]
            self.assertEqual(draft["status"], "FIRST_DRAFT_PERSISTED")
            payload = _read(Path(draft["path"]))
            self.assertEqual(payload["artifact_kind"], "FIRST_DRAFT")
            self.assertGreater(payload["effective_chars"], 0)
            self.assertEqual(len([row for row in payload["artifact"]["sections"] if row.get("level") != 1]), 2, "两节内容都要进入初稿")
            self.assertEqual(len(payload["section_states"]), 2)
            self.assertTrue((run / "longform" / "word" / "first_draft_content.json").is_file())
            self.assertEqual(_read(run / "orchestrator_status.json")["FIRST_DRAFT_ARTIFACT"], "FIRST_DRAFT_PERSISTED")
            self.assertTrue(any(status.endswith("DEPTH_INCOMPLETE") for status in payload["section_states"].values()))

    def test_qa_pipe_06b_technically_failed_sections_still_leave_a_first_draft(self):
        """QA-PIPE-06b: 后续Section技术失败不得抛断整轮，已到达内容必须仍在初稿里。"""
        class ExplodesAfterFirst:
            def __init__(self):
                self.calls = 0
                self.inner = _provider()
                self.config = self.inner.config

            def get_metadata(self):
                return self.inner.get_metadata()

            def invoke_structured(self, request, task_dir):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("provider runtime exploded")
                return self.inner.invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            provider = ExplodesAfterFirst()
            result = _generate(run, provider, sections=("S01-01", "S05-02"))
            word = result["word"]
            self.assertEqual(word["section_states"].get("S01-01", "").split("_")[0], "COMPLETED")
            self.assertIn("S05-02", word["TECHNICAL_FAILED_SECTIONS"], "技术失败必须显式记录，不得隐藏")
            payload = _read(run / "longform" / "word" / "first_draft_artifact.json")
            self.assertEqual(payload["artifact_kind"], "FIRST_DRAFT")
            self.assertGreater(payload["effective_chars"], 0)
            self.assertTrue(_read(run / "longform" / "word" / "first_draft_content.json")["artifact"]["sections"])
            self.assertLessEqual(word["TECHNICAL_RETRY_CALLS"], word["TECHNICAL_RETRY_BUDGET"])


class AlwaysShortProvider:
    """每一批都只回很少内容的 Provider：用于制造深度缺口。"""

    def __init__(self):
        self.calls = 0
        self.inner = _provider()
        self.config = self.inner.config

    def get_metadata(self):
        return self.inner.get_metadata()

    def invoke_structured(self, request, task_dir):
        self.calls += 1
        return self.inner.invoke_structured({**request, "mock_short": True}, task_dir)


def _contract(**overrides) -> dict:
    base = {
        "section_id": "S01-01",
        "section_title": "秩序维护与异常处置",
        "must_cover": ["门岗值守", "分区巡逻"],
        "required_outputs": ["岗位职责与接口矩阵"],
        "target_words": {"min": 1200, "target": 1600, "max": 2200},
        "source_requirements": ["REQ-0001"],
    }
    base.update(overrides)
    return base


def _gate(**overrides) -> dict:
    base = {
        "meaningful_body": True,
        "content_units": 240,
        "length_status": "SECTION_UNDER_LENGTH",
        "contract_complete": False,
        "must_cover": {"门岗值守": "COVERED", "分区巡逻": "MISSING"},
        "required_outputs_missing": [],
        "status": "PASS",
        "issue": "章节仅达到目标长度20%",
    }
    base.update(overrides)
    return base


class QaPipeSeverityGradingTests(unittest.TestCase):
    def test_qa_pipe_07_severity_is_decided_by_the_rule_not_by_what_a_detector_declared(self):
        """QA-PIPE-07（§20-24）：分级由规则决定，深度/篇幅永远不是 BLOCK。"""
        findings = {
            row["rule_id"]: row
            for row in regrade_findings([
                {"rule_id": "SECTION_UNDER_LENGTH", "severity": "BLOCK", "issue": "章节只有目标长度80%"},
                {"rule_id": "DEPTH_INCOMPLETE", "severity": "BLOCK", "issue": "内容单元不足"},
                {"rule_id": "WORDING", "severity": "BLOCK", "issue": "措辞建议"},
                {"rule_id": "LF-002", "severity": "NEEDS_REVIEW", "issue": "契约章节未出现在文档中"},
                {"rule_id": "FAQ-002", "severity": "BLOCK", "issue": "残留序列化结构"},
                {"rule_id": "UNKNOWN_BODY_PROSE", "severity": "BLOCK", "issue": "正文表述无法阅读"},
                {"rule_id": "UNKNOWN_UNREADABLE", "severity": "BLOCK", "issue": "assembled body is unreadable"},
            ])
        }
        self.assertEqual(findings["SECTION_UNDER_LENGTH"]["severity"], "NEEDS_REVIEW")
        self.assertEqual(findings["SECTION_UNDER_LENGTH"]["declared_severity"], "BLOCK")
        self.assertEqual(findings["SECTION_UNDER_LENGTH"]["repair_priority"], "NEEDS_REVIEW")
        self.assertFalse(findings["SECTION_UNDER_LENGTH"]["provider_repair_eligible"], "80%长度不得触发Provider修复")
        self.assertEqual(findings["DEPTH_INCOMPLETE"]["severity"], "NEEDS_REVIEW")
        self.assertFalse(findings["DEPTH_INCOMPLETE"]["provider_repair_eligible"])
        self.assertEqual(findings["WORDING"]["severity"], "WARNING", "措辞类只能是 WARNING")
        self.assertEqual(findings["WORDING"]["repair_priority"], "NONE")
        self.assertEqual(findings["LF-002"]["severity"], "BLOCK", "关键章节缺失不得被降级")
        self.assertTrue(findings["LF-002"]["provider_repair_eligible"])
        self.assertTrue(findings["FAQ-002"]["local_repair_only"])
        self.assertEqual(findings["FAQ-002"]["severity"], "NEEDS_REVIEW", "本地可修问题自己 escalates 也不算 BLOCK")
        self.assertFalse(findings["FAQ-002"]["provider_repair_eligible"], "本地可修问题不得消耗Provider调用")
        self.assertEqual(findings["UNKNOWN_BODY_PROSE"]["severity"], "NEEDS_REVIEW", "未知规则不得靠措辞自升 BLOCK")
        self.assertEqual(findings["UNKNOWN_UNREADABLE"]["severity"], "BLOCK", "不可阅读的正文是真 BLOCK")
        summary = summarize_severity(list(findings.values()))
        self.assertEqual(summary["status"], "BLOCK")
        self.assertEqual(summary["counts"], {"BLOCK": 2, "NEEDS_REVIEW": 4, "WARNING": 1})
        self.assertEqual(grade_finding({"rule_id": "DEPTH_INCOMPLETE", "severity": "BLOCK", "issue": "x"}), "NEEDS_REVIEW")

    def test_qa_pipe_07b_minor_under_length_and_warnings_never_buy_a_provider_call(self):
        gate = _gate(content_units=960, contract_complete=True, must_cover={"门岗值守": "COVERED", "分区巡逻": "COVERED"}, issue="章节只有目标长度80%")
        self.assertEqual(classify_under_length(gate, _contract()), SEVERITY_MINOR)
        decision = classify_section_issue(gate, _contract(), "UNDER_LENGTH", speed_profile="fast")
        self.assertEqual(decision["severity"], SEVERITY_MINOR)
        self.assertEqual(decision["max_provider_repairs"], 0, "Fast 模式下轻微长度缺口零次整节修复")
        self.assertFalse(decision["can_exceed_global"])
        self.assertFalse(FastRepairBudget(speed_profile="fast", initial_batch_count=1).allow(decision, previous_repairs=0))
        balanced = classify_section_issue(gate, _contract(), "UNDER_LENGTH", speed_profile="balanced")
        self.assertLessEqual(balanced["max_provider_repairs"], 1, "轻微缺口最多一次，且绝不与整节重写混用")
        graded = regrade_findings([{"rule_id": "WORDING", "severity": "WARNING", "issue": "表述可再打磨"}])[0]
        self.assertEqual(graded["severity"], "WARNING")
        budget = GlobalRepairBudget(initial_calls=100)
        allowed, reason = budget.allow("REVIEW:S01-01", priority=graded["repair_priority"])
        self.assertFalse(allowed)
        self.assertEqual(reason, "UNKNOWN_PRIORITY", "WARNING 没有修复优先级，也就没有预算")
        self.assertEqual(budget.repair_used, 0)


class QaPipeGlobalRepairBudgetTests(unittest.TestCase):
    def test_qa_pipe_08_repair_budget_is_global_prioritised_and_single_round(self):
        """QA-PIPE-08（§25-29）：全书一个预算，优先级决定谁拿到剩下的调用。"""
        self.assertEqual(GlobalRepairBudget.derive(157), 10)
        self.assertEqual(GlobalRepairBudget.derive(40), 8)
        self.assertEqual(GlobalRepairBudget.derive(4), 1)
        self.assertEqual(GlobalRepairBudget.derive(0), 0)
        budget = GlobalRepairBudget(initial_calls=40)
        self.assertEqual(budget.max_calls, 8)
        self.assertEqual(budget.per_issue_limit, MAX_AUTO_REPAIR_PER_ISSUE)
        for index in range(20):
            allowed, _ = budget.allow(f"REQ-{index:02d}", priority=PRIORITY_REQUIREMENT_GAP)
            if allowed:
                budget.note_attempt(f"REQ-{index:02d}")
        self.assertEqual(budget.repair_used, 8)
        self.assertEqual(budget.remaining, 0)
        allowed, reason = budget.allow("REQ-99", priority=PRIORITY_SCORING_GAP)
        self.assertFalse(allowed)
        self.assertEqual(reason, "GLOBAL_REPAIR_BUDGET_EXHAUSTED", "预算用尽即停，不得进入第二轮")
        self.assertTrue(budget.allow("NET:S01-01", priority=PRIORITY_BLOCK, technical_retry=True)[0], "技术重试不占修复预算")
        audit = budget.audit()
        self.assertEqual(audit["max_repair_calls"], 8)
        self.assertEqual(audit["remaining_calls"], 0)
        self.assertFalse(audit["storm_triggered"])
        self.assertEqual(audit["formula"], "min(10, ceil(initial_calls * 0.2))")

        single = GlobalRepairBudget(initial_calls=40)
        single.note_attempt("DEPTH:S01-01", kind="supplement")
        self.assertEqual(single.supplement_used, 1)
        self.assertEqual(single.provider_used, 1)
        allowed, reason = single.allow("DEPTH:S01-01", priority=PRIORITY_DEPTH_GAP)
        self.assertFalse(allowed)
        self.assertEqual(reason, "ISSUE_ALREADY_REPAIRED", "同一问题最多自动修一次")
        single.observe_repeated_failure("DEPTH:S01-01")
        self.assertEqual(single.storm_flags, [])
        single.stop("GLOBAL_REPAIR_BUDGET_EXHAUSTED")
        self.assertEqual(single.allow("REQ-01", priority=PRIORITY_BLOCK), (False, "GLOBAL_REPAIR_BUDGET_EXHAUSTED"))

        ordered = sort_repair_candidates([
            {"priority": PRIORITY_NEEDS_REVIEW, "value_score": 99},
            {"priority": PRIORITY_DEPTH_GAP, "value_score": 45},
            {"priority": PRIORITY_SCORING_GAP, "value_score": 60},
            {"priority": PRIORITY_BLOCK, "value_score": 90},
            {"priority": PRIORITY_REQUIREMENT_GAP, "value_score": 70},
            {"priority": PRIORITY_SCORING_GAP, "value_score": 85},
        ])
        self.assertEqual([row["priority"] for row in ordered], [PRIORITY_BLOCK, PRIORITY_SCORING_GAP, PRIORITY_SCORING_GAP, PRIORITY_REQUIREMENT_GAP, PRIORITY_DEPTH_GAP, PRIORITY_NEEDS_REVIEW])
        self.assertEqual([row["value_score"] for row in ordered], [90, 85, 60, 70, 45, 99])
        self.assertEqual(list(dict.fromkeys(row["priority"] for row in ordered)), list(REPAIR_PRIORITY_ORDER))

    def test_qa_pipe_08d_real_run_repairs_once_inside_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            provider = AlwaysShortProvider()
            result = _generate(run, provider)
            report = _read(run / "document_qa_report.json")
            word = result["word"]
            self.assertEqual(report["rounds"], 1, "有界修复只跑一轮")
            self.assertFalse(report["full_document_regeneration"], "不得回到整篇重生成")
            budget = report["budget"]
            self.assertIsNotNone(budget)
            self.assertEqual(budget["max_repair_calls"], GlobalRepairBudget.derive(budget["initial_provider_calls"]))
            self.assertLessEqual(budget["repair_calls_used"] + budget["supplement_calls_used"], budget["max_repair_calls"])
            self.assertLessEqual(
                word["TOTAL_CALLS"] - word["INITIAL_GENERATION_CALLS"],
                budget["max_repair_calls"],
            )
            self.assertEqual(budget["repair_calls_used"], word["SECTION_REPAIR_CALLS"])
            self.assertEqual(budget["supplement_calls_used"], word["SUPPLEMENT_CALLS"])
            candidates = report["candidates"]["repair"] + report["candidates"]["supplement"]
            self.assertEqual(len(candidates), report["counts"]["repair_candidates"] + report["counts"]["supplement_candidates"])
            self.assertTrue(all(row["priority"] in REPAIR_PRIORITY_ORDER for row in candidates))
            self.assertTrue(all(row["mode"] in {"repair", "supplement"} for row in candidates))
            self.assertEqual(len({row["issue_key"] for row in candidates}), len(candidates), "同一 section 的同类缺口只留一条")
            for bucket in (report["candidates"]["repair"], report["candidates"]["supplement"]):
                keys = [(row["priority_rank"], -row["value_score"]) for row in bucket]
                self.assertEqual(keys, sorted(keys), "候选队列必须按优先级排队，高价值缺口先拿到调用")
            self.assertFalse((run / "qa_performance_warning.json").is_file())


class QaPipeDepthSupplementTests(unittest.TestCase):
    def test_qa_pipe_09_depth_gap_becomes_new_content_units_not_a_longer_paragraph(self):
        """QA-PIPE-09（§30-34）：篇幅缺口＝覆盖缺口，补的是新 CU，不是"再写长一点"。"""
        planned = ["CU-S01-01-01", "CU-S01-01-02"]
        gate = _gate(content_units=240)
        self.assertEqual(supplement_unit_count(gate, _contract()), 2)
        self.assertEqual(supplement_unit_count(_gate(content_units=1200), _contract()), 0, "达标章节不补充")
        rows = plan_depth_supplement(
            _contract(), gate,
            fragment={"body_blocks": [{"type": "paragraph", "content": "本项目门岗实行全天候值守并留存交接班记录。"}]},
            planned_cu_ids=planned,
            existing_archetypes=["SOP"],
        )
        self.assertEqual([row["content_unit_id"] for row in rows], next_supplement_ids("S01-01", planned, len(rows)))
        self.assertEqual([row["content_unit_id"] for row in rows], ["CU-S01-01-03", "CU-S01-01-04"])
        self.assertEqual(len({row["archetype"] for row in rows}), len(rows), "同一节内补充原型不得重复")
        for row in rows:
            self.assertIn(row["archetype"], CONTENT_ARCHETYPES)
            self.assertEqual(row["generation_origin"], "depth_supplement")
            self.assertIn(row["supplement_reason"], {"MUST_COVERAGE_MISSING", "REQUIRED_OUTPUT_MISSING", "DEPTH_DIMENSION_MISSING"})
            self.assertTrue(row["required_outputs"], "每个新 CU 必须自带交付物")
            self.assertNotIn(row["content_unit_id"], planned, "补充单元必须是新 CU")
            self.assertEqual(row["parent_section_id"], "S01-01")
            self.assertEqual(row["min_budget"]["measure"], row["target_budget"]["measure"])
            self.assertLess(row["min_budget"]["effective_chars"], row["target_budget"]["effective_chars"])
            self.assertLess(row["target_budget"]["effective_chars"], row["max_budget"]["effective_chars"])
            blob = json.dumps(row, ensure_ascii=False)
            for padding in ("更详细", "扩写", "重写", "上文", "如上所述"):
                self.assertNotIn(padding, blob, f"补充计划不得出现注水式指令：{padding}")
        rules = supplement_prompt_rules(rows)
        joined = " ".join(rules)
        self.assertIn("DEPTH_SUPPLEMENT", joined)
        self.assertIn("禁止改写", joined)
        self.assertIn("CU-S01-01-03", joined)
        self.assertTrue(any("重复" in rule for rule in rules))

    def test_qa_pipe_09b_novelty_gate_rejects_low_information_gain(self):
        base = "本项目秩序维护实行门岗值守与分区巡逻相结合，每日两次全覆盖巡查并记录台账。"
        existing = base * 12
        self.assertGreaterEqual(len(existing), MIN_SUPPLEMENT_CHARS)
        copied = novelty_gate(existing, existing[:-2])
        self.assertFalse(copied["passed"])
        self.assertEqual(copied["reason"], "LOW_INFORMATION_GAIN")
        self.assertGreater(copied["overlap_ratio"], 0.55)
        short = novelty_gate(existing, "新增内容太短")
        self.assertEqual(short["reason"], "BELOW_MIN_SUPPLEMENT_CHARS")
        self.assertEqual(novelty_gate(existing, "")["reason"], "EMPTY_SUPPLEMENT")
        fresh = novelty_gate(existing, (
            "针对电梯困人突发情况，接报后三分钟内工程人员到场，秩序人员同步设置围挡并安抚乘客。"
            "救援完成后填写救援记录单，由品质专员在二十四小时内回访使用方并归档。"
            "同一楼栋重复故障触发设备专项排查，排查结论进入月度品质例会跟踪整改。"
            "变配电室巡检执行双人同行制度，绝缘工具按周期送检并保留检定证书编号。"
            "汛期前完成雨水泵站带载试车，沙袋与抽水泵按分区定额存放并纳入交接班清点。"
            "客服工单执行首问负责，超时未闭环的工单自动升级至项目主管并记入周考核。"
            "秩序维护员每季度开展一次突发事件演练，演练脚本、影像与讲评记录一并留存。"
            "品质检查采用随机抽样方式，抽样比例、扣分口径与整改期限在检查通知中明示。"
            "绿化养护按季节编制作业日历，乔木修剪、施肥与病虫害防治分别设定验收节点。"
            "保洁作业实行分区责任制，责任人、工具定额与频次在作业看板上公示。"
            "所有外委单位人员进场前完成安全交底，交底签字表随月度结算单一并报送。"
        ))
        self.assertTrue(fresh["passed"], fresh)
        self.assertEqual(fresh["reason"], "INFORMATION_GAIN_OK")
        self.assertGreater(fresh["novelty"], 0.45)

    def test_qa_pipe_09c_depth_gaps_are_routed_to_supplement_and_never_rewrite_the_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            result = _generate(run, AlwaysShortProvider())
            report = _read(run / "document_qa_report.json")
            word = result["word"]
            depth = [row for row in report["candidates"]["supplement"] if str(row["issue_key"]).startswith("DEPTH:")]
            self.assertTrue(depth, "深度缺口必须进入补充队列")
            self.assertEqual([row for row in report["candidates"]["repair"] if str(row["issue_key"]).startswith("DEPTH:")], [])
            self.assertTrue(all(row["mode"] == "supplement" for row in depth))
            self.assertEqual(word["SECTION_REPAIR_CALLS"], 0)
            self.assertEqual(word["CONTINUATION_CALLS"], 0)
            for path in sorted((run / "longform" / "word" / "sections").glob("*/supplement.json")):
                record = _read(path)
                attempts = _read(path.parent / "status.json")["attempts"]
                arrived = set(attempts[0]["provider_received_content_unit_ids"])
                self.assertTrue(set(record["content_unit_ids"]).isdisjoint(arrived), "补充写入的 CU 不得与初稿 CU 重叠")
                self.assertIn("novelty", record)


class QaPipeCompositeScaleTests(unittest.TestCase):
    def _scale(self, **overrides) -> dict:
        base = {
            "rendered_pages": 280,
            "page_target_min": 250,
            "page_target_max": 320,
            "effective_chars": 252000,
            "char_target_min": 240000,
            "requirement_coverage": 1.0,
            "scoring_coverage": 0.98,
            "cu_completion": 0.99,
            "archetype_counts": {"NARRATIVE": 20, "SOP": 80},
            "table_count": 169,
            "semantic_repetition": 0,
        }
        base.update(overrides)
        return evaluate_document_scale(**base)

    def test_qa_pipe_10_scale_is_composite_and_characters_are_reference_only(self):
        """QA-PIPE-10（§35-37）：240k～280k 字符不再是单点 BLOCK，250-320 页才是目标区间。"""
        clean = self._scale()
        self.assertEqual(clean["status"], "PASS")
        self.assertEqual(clean["page_state"], "IN_RANGE")
        self.assertEqual(clean["scale_score"], 1.0)
        self.assertEqual(clean["findings"], [])

        thin = self._scale(rendered_pages=235, effective_chars=180000)
        self.assertEqual(thin["status"], "NEEDS_REVIEW", "覆盖充分但篇幅偏低只能交人工复核")
        self.assertTrue(thin["coverage_strong"])
        finding = thin["findings"][0]
        self.assertEqual(finding["rule_id"], "DOCUMENT_SCALE_BELOW_TARGET")
        self.assertNotEqual(finding["severity"], "BLOCK")
        self.assertFalse(finding["auto_supplement"], "不得机械补足")
        self.assertTrue(finding["review_note"])

        weak = self._scale(rendered_pages=120, effective_chars=60000, requirement_coverage=0.6, scoring_coverage=0.5)
        self.assertFalse(weak["coverage_strong"])
        self.assertEqual(weak["status"], "BLOCK")
        self.assertTrue(weak["findings"][0]["auto_supplement"])

        above = self._scale(rendered_pages=400, effective_chars=380000)
        self.assertEqual(above["page_state"], "ABOVE_TARGET")
        self.assertEqual(above["findings"][0]["severity"], "WARNING", "超页数只是 WARNING，不得回头重生成")

        # 字符量差五倍，只要页数与覆盖一致，结论必须一致：字符是参考量。
        low_chars = self._scale(effective_chars=60000)
        high_chars = self._scale(effective_chars=300000)
        self.assertEqual(low_chars["status"], high_chars["status"])
        self.assertEqual(low_chars["scale_score"], high_chars["scale_score"])
        self.assertFalse(low_chars["character_reference"]["gating"])
        self.assertLess(low_chars["character_reference"]["ratio"], 1.0)

    def test_qa_pipe_10b_rendered_pages_measure_arrived_content(self):
        self.assertIsNone(estimate_rendered_pages(effective_chars=0, structure={}))
        self.assertIsNone(rendered_pages_for_artifact(None))
        structured = estimate_rendered_pages(effective_chars=0, structure={"tables": 169, "sections": 5})
        self.assertIsNotNone(structured)
        self.assertGreater(structured, 16, "结构化版面要计入页数，但不能替正文说话")
        dimension = self._scale(rendered_pages=None)["dimensions"]["RENDERED_PAGES"]
        self.assertFalse(dimension["met"])
        self.assertEqual(dimension["value"], 0)
        narrative = self._scale(archetype_counts={"NARRATIVE": 95, "SOP": 5})
        self.assertEqual(narrative["status"], "NEEDS_REVIEW", "90%以上纯叙述必须被拦下复核")
        self.assertIn("NARRATIVE_DOMINANT", [row["rule_id"] for row in narrative["findings"]])


class QaPipeTerminalStateAndAuditTests(unittest.TestCase):
    def test_qa_pipe_11_terminal_audit_splits_calls_by_stage_and_flags_repair_storms(self):
        """QA-PIPE-11（§44-46）：修了三小时必须看得见，REPAIR_STORM_GUARD 必须报警。"""
        budget = GlobalRepairBudget(initial_calls=10)
        budget.observe_time(initial_seconds=600.0, repair_seconds=1800.0)
        self.assertTrue(budget.storm_triggered)
        self.assertEqual(budget.storm_flags[0]["reason"], "REPAIR_TIME_EXCEEDED_GENERATION_TIME")
        budget.note_attempt("DEPTH:S01-01")
        budget.note_attempt("DEPTH:S01-01")
        budget.observe_repeated_failure("DEPTH:S01-01")
        self.assertIn("SAME_ISSUE_REPEATED", [row["reason"] for row in budget.storm_flags])
        audit = app_core._longform_runtime_audit({
            "word": {
                "INITIAL_GENERATION_CALLS": 150,
                "TECHNICAL_RETRY_CALLS": 4,
                "SECTION_REPAIR_CALLS": 2,
                "CONTINUATION_CALLS": 0,
                "SUPPLEMENT_CALLS": 6,
                "REPAIR_CALLS": 8,
                "TOTAL_CALLS": 158,
                "global_repair_budget": budget.audit(),
            },
            "document_qa": {"status": "COMPLETED_WITH_OPEN_ITEMS", "attempted": 8, "repaired": 5, "rounds": 1, "full_document_regeneration": False},
            "performance": {"document_qa_seconds": 12.5, "depth_supplement_seconds": 3.5},
        })
        self.assertEqual(audit["REPAIR_STORM_GUARD"], "TRIGGERED")
        self.assertTrue(audit["QA_PERFORMANCE_WARNING"], "修复耗时超过初稿耗时必须留下性能告警")
        self.assertEqual(audit["DOCUMENT_QA_ROUNDS"], 1)
        self.assertFalse(audit["FULL_DOCUMENT_REGENERATION"])
        self.assertEqual(audit["INITIAL_PROVIDER_CALLS"], 150)
        self.assertEqual(audit["TECHNICAL_RETRY_CALLS"], 4)
        self.assertEqual(audit["REPAIR_CALLS"], 8)
        self.assertEqual(audit["SUPPLEMENT_CALLS"], 6)
        self.assertEqual(audit["TOTAL_CALLS"], 158)

    def test_qa_pipe_11b_a_mock_run_finishes_with_a_clean_budgeted_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(Path(tmp))
            result = _generate(run, AlwaysShortProvider())
            audit = app_core._longform_runtime_audit(result)
            for key in (
                "INITIAL_PROVIDER_CALLS", "TECHNICAL_RETRY_CALLS", "REPAIR_CALLS", "SUPPLEMENT_CALLS",
                "TOTAL_CALLS", "DOCUMENT_QA_SECONDS", "DEPTH_SUPPLEMENT_SECONDS", "DOCUMENT_QA_ROUNDS",
                "FULL_DOCUMENT_REGENERATION", "REPAIR_STORM_GUARD", "QA_PERFORMANCE_WARNING",
            ):
                self.assertIn(key, audit)
            self.assertEqual(audit["REPAIR_STORM_GUARD"], "PASS")
            self.assertFalse(audit["QA_PERFORMANCE_WARNING"])
            self.assertEqual(audit["DOCUMENT_QA_ROUNDS"], 1)
            self.assertFalse(audit["FULL_DOCUMENT_REGENERATION"])
            self.assertEqual(result["word"]["status"], "COMPLETED", "质量缺口不得把生成链卡在修复状态")
            self.assertEqual(_read(run / "document_qa_report.json")["rounds"], 1)

    def test_qa_pipe_11c_ui_reports_progress_stages_instead_of_open_ended_repair(self):
        root = _scoped_root("phase")
        try:
            run = root / "run"
            run.mkdir()
            app_core._write_generation_phase(run, "DOCUMENT_QA", "正在检查完整性，关键问题4项。")
            row = _read(run / "generation_phase.json")
            self.assertEqual(row["phase"], "DOCUMENT_QA")
            self.assertFalse(row["terminal"])
            self.assertTrue(row["recoverable"])
            app_core._write_generation_phase(run, "SELECTIVE_REPAIR", "正在补充 2/4 个关键问题，自动修复预算 2/10 calls。")
            self.assertEqual(_read(run / "generation_phase.json")["phase"], "SELECTIVE_REPAIR")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(server._PHASE_HEARTBEAT_LABELS["DOCUMENT_QA"], "正在检查完整性")
        self.assertIn("正在补充", server._PHASE_HEARTBEAT_LABELS["SELECTIVE_REPAIR"])
        self.assertIn("正在补充", server._PHASE_HEARTBEAT_LABELS["DEPTH_SUPPLEMENTING"])
        self.assertEqual(server._PHASE_HEARTBEAT_LABELS["LOCAL_NORMALIZING"], "正在整理文档")
        message = server._heartbeat_progress_message({"public_phase": "DOCUMENT_QA"}, 95)
        self.assertIn("正在检查完整性", message)
        self.assertIn("1分35秒", message)
        generating = server._heartbeat_progress_message({"public_phase": "GENERATING", "progress": {"done": 3, "total": 12}}, 0)
        self.assertIn("3/12", generating)
        self.assertIn("25%", generating)
        for relative in ("app_core.py", "longform/orchestrator.py", "longform/factory.py"):
            source = (APP / relative).read_text(encoding="utf-8")
            self.assertNotIn('_write_generation_phase(run_dir, "AUTO_REPAIRING"', source, "无界自动修复阶段不得再对客展示")
            self.assertNotIn('"UNDER_LENGTH_REWRITE"', source, "UNDER_LENGTH_REWRITE 已被 DEPTH_SUPPLEMENT 取代")
        self.assertIn("自动修复预算", (APP / "app_core.py").read_text(encoding="utf-8"))


class QaPipeStopGenerationTests(unittest.TestCase):
    def setUp(self):
        self._runs_dir = server.RUNS_DIR

    def tearDown(self):
        server.RUNS_DIR = self._runs_dir

    def test_qa_pipe_12_stopping_keeps_the_checkpoint_and_salvages_arrived_content(self):
        """QA-PIPE-12（§43）：停止生成保留检查点与初稿，已到达的内容不得作废。"""
        root = _scoped_root("stop")
        try:
            run = _write_run(root)
            _generate(run, _provider(), sections=("S01-01",))
            content = run / "longform" / "word" / "first_draft_content.json"
            manifest = run / "first_draft.json"
            fragment = _read(run / "longform" / "word" / "sections" / "S01-01" / "fragment.json")
            self.assertTrue(content.is_file())
            server.RUNS_DIR = root

            manifest.write_text(json.dumps({"status": "DOWNLOADABLE", "path": str(content)}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(
                server._salvage_first_draft_on_stop("run"),
                {"status": "DOWNLOADABLE", "path": str(content)},
                "初稿已在磁盘上时，停止不得重复渲染或改指向",
            )
            self.assertTrue(manifest.is_file() and content.is_file())

            manifest.unlink()
            content.unlink()
            brief, artifact = server._reassemble_from_committed_fragments(run)
            self.assertEqual(brief.get("project_name"), "QA PIPE 测试项目")
            body = [row for row in artifact["sections"] if row.get("level") != 1]
            self.assertEqual([row.get("heading") for row in body], [fragment["title"]])
            dumped = json.dumps(artifact, ensure_ascii=False)
            self.assertIn(str(fragment["body_blocks"][0]["content"])[:8], dumped, "已到达的正文必须进入停止时的存稿")
            salvaged = server._salvage_first_draft_on_stop("run")
            self.assertNotEqual(salvaged["status"], "NO_DRAFT_CONTENT")
            self.assertEqual(salvaged["status"], "DOWNLOADABLE", salvaged)
            self.assertTrue(Path(salvaged["path"]).is_file())
            self.assertEqual(_read(run / "first_draft.json")["saved_by"], "stop_generation")
            self.assertTrue((run / "checkpoint" / "state.json").is_file(), "检查点必须保留，可继续生成")
            self.assertTrue((run / "longform" / "word" / "sections" / "S01-01" / "fragment.json").is_file())

            (root / "empty" / "run").mkdir(parents=True)
            self.assertEqual(server._salvage_first_draft_on_stop("empty")["status"], "NO_DRAFT_CONTENT")
        finally:
            server.RUNS_DIR = self._runs_dir
            shutil.rmtree(root, ignore_errors=True)

    def test_qa_pipe_12b_a_saved_draft_on_stop_must_not_stay_terminal_blocked(self):
        """QA-PIPE-12b（§38-43）：停止时已存下可下载初稿，公开阶段不得继续报"未能形成可打开的文件"。

        真实 300-Class 前置试炼里交付判定先写了 TERMINAL_BLOCKED，随后停止存稿成功，
        界面仍在提示硬失败——文件明明可下载，等于把已完成部分藏了起来。
        """
        root = _scoped_root("stopphase")
        try:
            run = _write_run(root)
            _generate(run, _provider(), sections=("S01-01",))
            server.RUNS_DIR = root
            blocked = {"phase": "TERMINAL_BLOCKED", "message": "本轮未能形成可打开的文件，请检查生成过程后重试。", "recoverable": False, "terminal": True}
            (run / "generation_phase.json").write_text(json.dumps(blocked, ensure_ascii=False), encoding="utf-8")
            (run / "first_draft.json").unlink(missing_ok=True)

            salvaged = server._salvage_first_draft_on_stop("run")
            self.assertEqual(salvaged["status"], "DOWNLOADABLE", salvaged)
            self.assertTrue(Path(salvaged["path"]).is_file())
            phase = _read(run / "generation_phase.json")
            self.assertNotEqual(phase["phase"], "TERMINAL_BLOCKED", "存稿成功后阶段必须改写，否则界面继续报硬失败")
            self.assertFalse(phase["terminal"])
            self.assertTrue(phase["recoverable"])
            self.assertIn("初稿", phase["message"])

            # 存不出文件时必须保留真实失败判定，不得借改写阶段粉饰成可下载。
            (root / "empty" / "run").mkdir(parents=True)
            (root / "empty" / "run" / "generation_phase.json").write_text(json.dumps(blocked, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(server._salvage_first_draft_on_stop("empty")["status"], "NO_DRAFT_CONTENT")
            self.assertEqual(_read(root / "empty" / "run" / "generation_phase.json")["phase"], "TERMINAL_BLOCKED")
        finally:
            server.RUNS_DIR = self._runs_dir
            shutil.rmtree(root, ignore_errors=True)


class QaPipeStatusConcurrencyTests(unittest.TestCase):
    """QA-PIPE-13（§38-43）：界面轮询状态与生成线程写阶段不得互相锁死。

    真实 300-Class 试炼在 DOCUMENT_QA 阶段撞出 AB-BA 死锁：set_active_stage 命中
    "同名阶段"分支时在 _ACTIVE_GUARD 内调用 load_workflow_timing，而读侧
    load_workflow_timing 先持 per-path 锁再进 _live_view 取 _ACTIVE_GUARD，两侧锁序
    相反 → 44 个状态请求线程全部挂住，界面永久停在"正在检查事实与承诺"。
    """

    def setUp(self):
        self._live_view = workflow_timing._live_view
        self._root = Path(tempfile.mkdtemp())
        self.run = self._root / "run"
        self.run.mkdir()
        workflow_timing.ensure_workflow(self.run, "tender_file")

    def tearDown(self):
        workflow_timing._live_view = self._live_view
        shutil.rmtree(self._root, ignore_errors=True)

    def test_qa_pipe_13_phase_write_and_status_read_do_not_deadlock(self):
        workflow_timing.set_active_stage(self.run, "DOCUMENT_QA")
        started_at = workflow_timing.load_workflow_timing(self.run)["current_active_stage"]["started_at"]

        inside_reader = threading.Event()
        release_reader = threading.Event()
        original = self._live_view

        def blocking_live_view(run_dir, payload):
            # 此刻读线程已持有 per-path 锁，正等待 _ACTIVE_GUARD。
            inside_reader.set()
            release_reader.wait(10)
            return original(run_dir, payload)

        workflow_timing._live_view = blocking_live_view
        reader = threading.Thread(target=lambda: workflow_timing.load_workflow_timing(self.run), daemon=True)
        reader.start()
        self.assertTrue(inside_reader.wait(10), "读线程未进入 _live_view")

        writer_done = threading.Event()

        def writer():
            workflow_timing.set_active_stage(self.run, "DOCUMENT_QA")
            writer_done.set()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        time.sleep(0.3)
        release_reader.set()
        self.assertTrue(writer_done.wait(10), "写阶段与读状态死锁：set_active_stage 再也没返回")
        thread.join(10)
        reader.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(reader.is_alive())

        # 同名阶段不得重置计时起点，否则 QA 等待时间会被反复清零、总耗时永远偏小。
        self.assertEqual(
            workflow_timing.load_workflow_timing(self.run)["current_active_stage"]["started_at"],
            started_at,
        )

    def test_qa_pipe_13b_a_new_stage_still_restarts_the_timer(self):
        # Real adjacent calls may round to 0.000 seconds. Advance the clock
        # explicitly and prove the new stage starts at its own time origin.
        with patch.object(workflow_timing.time, "monotonic", return_value=100.0) as clock:
            workflow_timing.set_active_stage(self.run, "DOCUMENT_QA")
            clock.return_value = 110.0
            qa = workflow_timing.load_workflow_timing(self.run)
            self.assertEqual(qa["current_active_stage"]["stage"], "DOCUMENT_QA")
            self.assertEqual(qa["current_active_stage_elapsed_seconds"], 10.0)
            workflow_timing.set_active_stage(self.run, "ARTIFACT_ASSEMBLY")
            clock.return_value = 112.5
            payload = workflow_timing.load_workflow_timing(self.run)
            self.assertEqual(payload["current_active_stage"]["stage"], "ARTIFACT_ASSEMBLY")
            self.assertEqual(payload["current_active_stage_elapsed_seconds"], 2.5)
            self.assertEqual(payload["current_total_processing_seconds"], 2.5)


class QaPipePartialContentUnitBatchTests(unittest.TestCase):
    """QA-PIPE-15：批次只到齐一部分 Content Unit 时，已到达的部分必须留在交付链上。

    Round 4 合成 300 类试炼（`tmp2aemnr1e`）抓到两条静默缺陷，都发生在"付费之后"：

    1. `_commit_arrived_content` 用 `cu_gaps`（**任何**计划 CU 未到达）整节否决。
       一条 PARTIAL 批次节因此永远没有 `fragment.json`：27 节里 5 节只剩标题，
       落盘的 35 条 CU、22,541 个正文字符（≈26 页）在装配链上凭空消失，账却照记。
       同一份产物两次跑出的页数因此在 183～230 之间摆。
    2. 落盘前的客户文案链（`apply_artifact_repairs` + `_repair_customer_text`）把
       **寻址值与渲染器枚举**一起洗掉：`S03-01 → 相关专项安排`、
       `provider_content_unit → _content_unit`、`type: "paragraph" → ""`、
       `archetype: "TABLE" → ""`。于是 fragment 里 `planned_content_unit_ids` 全是
       中文归类词，任何按 CU 出处找回正文的动作（削减重建、晚到回收、§64 表格账）
       都静默落空 —— 削减执行器一旦跑在这条链上就会把 7k 字的节重建空。
    """

    def test_qa_pipe_15_partial_cu_batch_keeps_arrived_body_and_the_gap(self):
        target = "S01-01"

        class DropsLastUnit:
            """把这一节的最后一条 CU 永久饿掉：首答少答一条，重试要它时也拿不到。

            只裁"多于一条"的响应，重试请求单条差集时若不再裁，缺口就会自己合上，
            测到的是重试路径而不是 settle 路径。
            """

            def __init__(self):
                self.inner = _provider()
                self.config = self.inner.config
                self.starved_id = ""
                self.drop_count = 0

            def get_metadata(self):
                return self.inner.get_metadata()

            def _shrink(self, payload: dict) -> dict:
                if str(payload.get("section_id") or "") != target:
                    return payload
                rows = payload.get("content_unit_fragments") or []
                if not self.starved_id:
                    if len(rows) < 2:
                        return payload
                    self.starved_id = str(rows[-1].get("content_unit_id"))
                kept = [row for row in rows
                        if str(row.get("content_unit_id")) != self.starved_id]
                if len(kept) == len(rows):
                    return payload
                self.drop_count += 1
                return {**payload, "content_unit_fragments": kept}

            def invoke_structured(self, request, task_dir):
                raw = self.inner.invoke_structured(request, task_dir)
                if isinstance(raw, dict) and isinstance(raw.get("sections"), list):
                    return {**raw, "sections": [self._shrink(row) for row in raw["sections"]]}
                return self._shrink(raw) if isinstance(raw, dict) else raw

        with tempfile.TemporaryDirectory() as tmp:
            provider = DropsLastUnit()
            run = _write_run(Path(tmp))
            result = _generate(run, provider, sections=(target,))
            # 注入必须真的发生过，否则这条测试什么都没测。
            self.assertTrue(provider.starved_id, "夹具没有少答任何 CU，故障未注入")
            self.assertGreaterEqual(provider.drop_count, 2,
                                    "重试也没有被饿掉，缺口在测试里自己合上了")
            lost_cu = provider.starved_id

            section = run / "longform" / "word" / "sections" / target
            self.assertTrue((section / "fragment.json").is_file(),
                            "已到达 CU 必须落成节片段，不能因为差一条 CU 就整节消失")
            fragment = _read(section / "fragment.json")
            execution = fragment.get("content_unit_execution") or {}
            assembled = [str(cu) for cu in execution.get("assembled_content_unit_ids") or []]
            self.assertTrue(assembled, "装配记录里必须有已到达的 CU")
            self.assertNotIn(lost_cu, assembled, "没到达的 CU 不得凭空出现在装配记录里")
            self.assertEqual([str(cu) for cu in execution.get("outstanding_content_unit_ids") or []],
                             [lost_cu], "缺口必须逐条点名，不得只写「本节不完整」")

            # 第 2 条缺陷：寻址值与渲染器枚举不许被文案清洗改写。
            self.assertEqual(str(fragment.get("section_id") or ""), target,
                             "section_id 被中文归类词顶掉后，补深/修复按 id 找回契约全部落空")
            self.assertTrue(all(str(cu).startswith("CU-") and "相关专项安排" not in str(cu)
                                for cu in execution.get("planned_content_unit_ids") or []),
                            execution.get("planned_content_unit_ids"))
            self.assertEqual({str(row.get("content_unit_id")) for row
                              in fragment.get("content_unit_fragments") or []}, set(assembled))
            self.assertTrue(all(str(block.get("type") or "") for block in fragment["body_blocks"]),
                            "body_blocks[].type 是渲染路由枚举，被清空后分点段/小标题退化成散文")

            partial = _read(section / "partial_generation.json")
            self.assertEqual(partial["status"], "PARTIAL_CU_ARRIVED")
            self.assertEqual(partial["remaining_content_unit_ids"], [lost_cu])
            self.assertEqual(_read(section / "status.json")["status"], "COMPLETED_DEPTH_INCOMPLETE",
                            "带缺口的节要以深度缺口留在交付链上，而不是记成技术性失败")
            self.assertNotIn(target, result["word"].get("TECHNICAL_FAILED_SECTIONS") or [])

            artifact = result["generated"]["artifact"]
            arrived_text = "".join(str(row.get("content") or "") for row
                                   in fragment["body_blocks"] if isinstance(row, dict))
            self.assertTrue(arrived_text.strip(), "已到达 CU 的正文必须进了片段")
            self.assertNotIn(lost_cu, json.dumps(artifact, ensure_ascii=False))
            # 装配链读到的正文里必须有已到达 CU 的文字（不是只剩标题）。
            from longform.factory import visible_text

            self.assertIn(arrived_text[:40], visible_text(artifact),
                          "已付费到达的正文没有进入交付初稿")


class QaPipeMachineFieldIntegrityTests(unittest.TestCase):
    """QA-PIPE-16：客户文案清洗只作用于会渲染的文本，不作用于寻址值与枚举。

    豁免口径必须与检测端同源（`customer_hygiene.is_structural_path`）：Round 3/4 里
    修复端各有一份键清单，`_repair_customer_text` 缺 `type`/`archetype`/`*_ids`，
    `apply_artifact_repairs` 只豁免了 `layout`，于是同一棵 fragment 树被洗成两套答案。
    """

    def _fragment(self) -> dict:
        return {
            "section_id": "S03-01",
            "generation_origin": "provider_content_unit",
            "title": "项目组织架构与职责边界",
            "content_unit_execution": {
                "mode": "CONTENT_UNIT_GRANULAR",
                "generation_origin": "provider_content_unit",
                "assembled_content_unit_ids": ["CU-S03-01-01"],
                "planned_content_unit_ids": ["CU-S03-01-01", "CU-S03-01-09"],
                "outstanding_content_unit_ids": ["CU-S03-01-09"],
            },
            "content_unit_fragments": [{
                "content_unit_id": "CU-S03-01-01", "parent_section_id": "S03-01",
                "archetype": "TABLE", "generation_origin": "provider_content_unit",
                "content": "现场巡查按合同约定执行，确保零故障运行。",
                "payload": {"table_id": "T-S03-01-01", "title": "岗位职责表",
                            "columns": ["岗位", "责任动作"],
                            "rows": [["秩序维护员", "按合同约定频次复核"]]},
            }],
            "used_ku_ids": ["KU-0001"],
            "used_requirement_ids": ["REQ-0834"],
            "body_blocks": [
                {"type": "paragraph", "content": "秩序维护班按 S03-01 的职责边界执行 CU-S03-01-01 的作业要求。"},
                {"type": "bullet_group", "content": ["每日两次巡更", "重点部位复核"]},
                {"type": "subheading", "content": "班组职责"},
            ],
        }

    def _cleaned(self) -> dict:
        from governance.artifact_qa import apply_artifact_repairs
        from governance.commitment_provenance import apply_local_repairs
        from longform.factory import _repair_customer_text

        return _repair_customer_text(apply_artifact_repairs(apply_local_repairs(self._fragment())))

    def test_qa_pipe_16a_cleaning_chain_keeps_machine_fields_and_cleans_prose(self):
        from longform.cu_assembly import cu_table_descriptors
        from longform.factory import visible_text

        cleaned = self._cleaned()
        execution = cleaned["content_unit_execution"]
        self.assertEqual(cleaned["section_id"], "S03-01")
        self.assertEqual(cleaned["generation_origin"], "provider_content_unit")
        self.assertEqual(execution["mode"], "CONTENT_UNIT_GRANULAR")
        self.assertEqual(execution["planned_content_unit_ids"], ["CU-S03-01-01", "CU-S03-01-09"])
        self.assertEqual(execution["outstanding_content_unit_ids"], ["CU-S03-01-09"])
        self.assertEqual(cleaned["content_unit_fragments"][0]["archetype"], "TABLE")
        self.assertEqual(cleaned["content_unit_fragments"][0]["content_unit_id"], "CU-S03-01-01")
        self.assertEqual(cleaned["used_ku_ids"], ["KU-0001"])
        self.assertEqual([block["type"] for block in cleaned["body_blocks"]],
                         ["paragraph", "bullet_group", "subheading"])
        # 该洗的照洗：散文里的内部标识符与绝对化承诺不得留在客户文本里。
        self.assertNotIn("S03-01", cleaned["body_blocks"][0]["content"])
        self.assertNotIn("CU-S03-01-01", cleaned["body_blocks"][0]["content"])
        self.assertNotIn("零故障", cleaned["content_unit_fragments"][0]["content"])
        # 会渲染的文本确实不含寻址 id —— 豁免才不会把内部标识符漏给客户。可见基座按
        # `final_assembly` 的量法取：title / body_blocks / tables，表格只取渲染出来的
        # title/列/行/注（`cu_table_descriptors` 就是装配时把 CU 载荷变成表的那一步）。
        visible = visible_text({
            "title": cleaned["title"],
            "body_blocks": cleaned["body_blocks"],
            "tables": cu_table_descriptors(cleaned["content_unit_fragments"]),
        })
        for token in ("S03-01", "CU-S03-01-01", "REQ-0834", "KU-0001", "T-S03-01-01"):
            self.assertNotIn(token, visible, f"{token} 从豁免字段漏进了可见文本")

    def test_qa_pipe_16b_document_qa_does_not_flag_machine_fields_as_leaks(self):
        from governance.artifact_qa import evaluate_artifact

        report = evaluate_artifact({"artifact": {"sections": [self._cleaned()]}})
        flagged = [row for row in report["findings"]
                   if row["rule_id"] in {"FAQ-002", "FAQ-005", "CLIENT_ARTIFACT_INTERNAL_META"}]
        self.assertEqual(flagged, [], f"寻址值被当成客户可见泄漏：{flagged[:3]}")

    def test_qa_pipe_16c_prose_leak_is_still_a_block_after_the_exemption(self):
        """豁免不能顺手把真泄漏也免掉：同一条 id 出现在散文里必须仍然被抓。"""
        from governance.artifact_qa import evaluate_artifact

        fragment = self._fragment()
        fragment["body_blocks"][0]["content"] = "本项工作由 S03-01 与 CU-S03-01-01 负责。"
        fragment["used_ku_ids"] = []
        report = evaluate_artifact({"artifact": {"sections": [fragment]}})
        leaked = [row for row in report["findings"]
                  if row["rule_id"] == "CLIENT_ARTIFACT_INTERNAL_META"
                  and "body_blocks" in str(row.get("path"))]
        self.assertTrue(leaked, "散文里的内部标识符必须仍然判为泄漏")

    def test_qa_pipe_16d_structural_ruler_tolerates_list_indices(self):
        """`...planned_content_unit_ids[7]` 与它的键名是同一个字段。"""
        from governance.customer_hygiene import is_structural_path

        for path in ("artifact.sections[0].content_unit_execution.planned_content_unit_ids[7]",
                     "artifact.sections[0].section_id",
                     "artifact.sections[0].body_blocks[3].type",
                     "artifact.sections[0].content_unit_fragments[2].archetype"):
            self.assertTrue(is_structural_path(path), path)
        for path in ("artifact.sections[0].title",
                     "artifact.sections[0].body_blocks[0].content",
                     "artifact.sections[0].tables[0].rows[1][2]"):
            self.assertFalse(is_structural_path(path), path)


class QaPipeRepairKeepsCuAssemblyTests(unittest.TestCase):
    """QA-PIPE-17：不带 CU 契约的重试，不得用「整节原始输出」覆盖 CU 装配。

    Round 4 合成 300 类实测（`r4_synthetic_chain_300class_postfix.json`）：S05-02 与
    S08-02 两节的 26 个 CU（其中 16 个结构单元：表/矩阵/清单/表单/SOP/流程）**全部到达并
    落盘**，但收尾的 `retry_technical` 因为「缺的已经齐了」而不携带任何 CU 契约，
    `_repair_section` 里那段"持久化 + 装配"整块被 `if expected_cus:` 挡住 —— 于是
    fragment.json 被写成 Provider 的 section-granular 原始输出：
    `content_unit_execution` 退化成 `{mode, received_content_unit_ids: []}`，
    16,880 字与全部表格/清单掉出正文，§64 治理账在这两节无账，而节状态仍是
    COMPLETED / COMPLETED_PROVISIONAL，四条余量与页数账全绿。
    """

    CU_IDS = ["CU-S-A-01", "CU-S-A-02"]

    def _factory_with_arrived_cus(self, root: Path):
        from test_mmf007c_unified_budget import _factory

        factory = _factory(root, cu_contracts={
            cu_id: {
                "content_unit_id": cu_id,
                "parent_section_id": "S-A",
                "section_id": "S-A",
                "title": "岗位履职安排表" if cu_id.endswith("01") else "每日检查清单",
                "intent": "把这一项工作写成可检查的口径",
                "archetype": "STANDARD_TABLE" if cu_id.endswith("01") else "CHECKLIST",
                "source_requirements": ["REQ-0834"],
            } for cu_id in self.CU_IDS})
        unit = factory.word_root / "sections" / "S-A"
        unit.mkdir(parents=True, exist_ok=True)
        # CU 落盘用产品自己的写手，不自造 schema —— 否则测的是夹具而不是链路。
        returned, issues = factory._persist_provider_cus("S-A", {
            "content_unit_fragments": [
                {"content_unit_id": "CU-S-A-01", "archetype": "STANDARD_TABLE",
                 "content": "各岗位履职口径按班次落地，责任到人，记录可追溯复核。",
                 "payload": {"title": "岗位履职安排表", "columns": ["岗位", "编制", "班次"],
                             "rows": [["客服主管", "1人", "常白班"], ["秩序领班", "2人", "两班倒"]]}},
                {"content_unit_id": "CU-S-A-02", "archetype": "CHECKLIST",
                 "content": "按日核对门岗签到与车辆放行记录，异常当场处置并留痕。",
                 "payload": {"items": ["每日门岗签到核查", "每周甲方联合检查"]}},
            ]}, self.CU_IDS)
        self.assertEqual(returned, self.CU_IDS, issues)
        self.assertEqual(factory._missing_cus("S-A"), [], "夹具必须造出「CU 已全部到达」的局面")
        contract = next(row for row in factory.inputs["section_contracts"]["contracts"]
                        if row["section_id"] == "S-A")
        contract.update({"section_title": "岗位与履职安排", "must_cover": [], "required_outputs": [],
                         "target_words": {"min": 900, "target": 1400, "max": 2200},
                         "source_requirements": ["REQ-0834"], "required_processes": []})
        # `_factory` 的 global_state 是最小夹具，真实规划件里带的进度容器要补上。
        factory.incremental_state.setdefault("generation_progress", {})
        factory.incremental_state.setdefault("cross_references", [])
        return factory, contract, unit

    def test_qa_pipe_17_retry_without_cu_contracts_still_rebuilds_body_from_arrived_units(self):
        seen: list[dict] = []

        class Recording:
            def __init__(self, inner):
                self.inner = inner
                self.config = inner.config

            def get_metadata(self):
                return self.inner.get_metadata()

            def invoke_structured(self, request, task_dir):
                seen.append(dict(request))
                return self.inner.invoke_structured(request, task_dir)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            factory, contract, unit = self._factory_with_arrived_cus(root)
            factory.provider = Recording(factory.provider)
            job = factory._job(factory.word_root, "WORD", 1)
            # 技术重试：此时本节 CU 已全部到达，所以请求不携带任何 CU 契约。
            self.assertTrue(factory._repair_section(job, contract, "LATENCY_FAILURE",
                                                    attempt_mode="retry_technical",
                                                    draftable_ok=True))
            self.assertTrue(seen, "重试必须真的发出过请求")
            self.assertEqual(seen[-1].get("content_unit_contracts") or [], [],
                             "这条测试的前提就是「本轮没带 CU 契约」，前提不成立则测试为空")

            fragment = _read(unit / "fragment.json")
            execution = fragment.get("content_unit_execution") or {}
            self.assertEqual(execution.get("planned_content_unit_ids"), self.CU_IDS,
                             "重试把 fragment 覆盖回整节原始形状，装配寻址就没了")
            self.assertEqual(execution.get("assembled_content_unit_ids"), self.CU_IDS)
            self.assertEqual(execution.get("outstanding_content_unit_ids"), [])
            self.assertIn("bullet_group", [block["type"] for block in fragment["body_blocks"]],
                          "已到达的清单 CU 必须仍在正文里")
            tables = json.dumps(fragment.get("tables") or [], ensure_ascii=False)
            self.assertIn("岗位履职安排表", tables, "已到达的表格 CU 必须仍在表格槽位里")
            self.assertIn("structure_units_charged", fragment.get("table_governance") or {},
                          "§64 治理账在这一节不得无账")


if __name__ == "__main__":
    unittest.main()
