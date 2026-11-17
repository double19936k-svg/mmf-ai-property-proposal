# -*- coding: utf-8 -*-
"""MMF-007C Round 4.6：真实链的 Topic **Identity 字段语义契约** + 门证据自洽。

被修的东西很具体（Run E `tender_20260926_100251_c718e5` 现场量出来的）：规划链把
`display_topic` 的 **60 字截断显示形**写进了带身份语义的 `topic` 字段，于是同一条
canonical 记录里躺着两套互相矛盾的主题文本（107 字全名 vs 60 字截断形），
按截断形现算的 key 与自报 key 相似度 0.6812 < 0.85 ⇒ Round 4.5 的身份绑定判
`MISMATCH` ⇒ 生成前门 fail-closed、0 CU 生成。**任何超过 ~60 字的真实评分主题都进不了生成。**

本轮的修法按任务书 §3 走**上游字段语义**，不是教 Authority 猜"这段大概是被截断的"：

  * identity-bearing：`topic` / `scoring_topic` ← `canonical_topic_text(label)`（去噪、**不截断**）
  * presentation-only：`display_topic` / `scoring_topic_display` ← 同一把尺的前缀截断，
    **不进身份线索**（`RECORD_PRESENTATION_FIELDS`）

以及 §7：`raise PlanningError` 之前先把本次 Authority 的终态 BLOCK 原子持久化进
`pre_generation_scoring_mapping_gate.json`，让"错误消息指向哪个文件"和"那个文件写着什么"
是同一件事（Run E 现场那份文件当时还是规划侧的 `status=PASS`）。

一个字都没调的：`TOPIC_MERGE_SIMILARITY` / `MIN_MERGE_TOPIC_CHARS` / `TOPIC_DISPLAY_CHARS`、
归一化算法、Finalizer、Topic Completeness 架构、页带、QA、预算、削减、表格、PPT、网络封死。
一个字都没碰的 Run：Run B/C/D/E 与 R10（本轮所有真实数据读数都来自**包外快照的副本**，
且那部分取证在 `MMF_Project_Records/…/MMF007C_R46_20260926/`，不在本文件里 ——
真实标书文本不进仓库）。零 Provider 调用：纯本地临时目录 + Mock 夹具。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import longform.scoring_authority as authority_module  # noqa: E402
import planning.scoring_taxonomy as taxonomy_module  # noqa: E402
from longform.orchestrator import (  # noqa: E402
    PreGenerationScoringGateError, require_pre_generation_scoring_mapping_gate)
from longform.scoring_authority import canonical_scoring_authority  # noqa: E402
from planning.scoring_taxonomy import (  # noqa: E402
    MIN_MERGE_TOPIC_CHARS, SCORING_MAPPING_GATE_FILE, TOPIC_DISPLAY_CHARS,
    TOPIC_MERGE_SIMILARITY, build_scoring_taxonomy, canonical_topic_text,
    display_topic, topic_key,
)
from test_mmf007c_r43_scoring_authority_closure import (  # noqa: E402
    MATRIX, _gate_payload, _write)
from test_mmf007c_r45_topic_identity_binding import (  # noqa: E402
    BRIDGE_A, BRIDGE_B, BRIDGE_C, OFF_TOPIC, PLAN_SECTIONS, PLAN_UNITS,
    _authority, _build_run, _gate, _record, _row,
)

MISMATCH = "SCORING_MAPPING_TOPIC_IDENTITY_MISMATCH"
COVERAGE_INCOMPLETE = "SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE"

# Run E 那条 blocker 的**形状**（文字是合成的物业行业表述，不是任何客户标书）：
# 带分值噪声、去噪之后仍然 >TOPIC_DISPLAY_CHARS 字的长评分主题。
LONG_A = ("3 | 技术部分（40分） | 秩序维护团队配置（8分） | 根据投标人拟投入本项目的秩序维护人员"
          "构成情况，综合考虑人员素质、年龄结构、持证比例及相关项目经验等进行评分。横向比较，"
          "优得6-8分，一般得3-5分，较差得0-2分。")
LONG_B = ("4 | 技术部分 | 环境绿化与保洁作业方案（6分） | 根据投标人对本项目公共区域保洁、"
          "绿化养护、垃圾分类清运的作业组织与质量自查安排是否完整可行进行评分，"
          "优得5-6分，一般得3-4分，较差得0-2分。")
SHORT_A = "安全秩序管理含门岗巡逻与异常处置"


def _producer_records(rows: list[dict]) -> list[dict]:
    """走真正的生产写入链（`build_scoring_taxonomy`），不手工拼 canonical 记录。"""
    return build_scoring_taxonomy(rows)["canonical_scoring_records"]


def _read_gate_file(run: Path) -> dict:
    return json.loads((run / SCORING_MAPPING_GATE_FILE).read_text(encoding="utf-8-sig"))


class FixtureShapeIsWhatItClaims(unittest.TestCase):
    """夹具不成立，后面的"复现"就是空的 —— 先把形状钉住。"""

    def test_long_labels_actually_overflow_the_display_ruler(self) -> None:
        self.assertGreater(len(canonical_topic_text(LONG_A)), TOPIC_DISPLAY_CHARS,
                           "长主题若不超过显示上限，本轮就没在量截断那件事")
        self.assertGreater(len(canonical_topic_text(LONG_B)), TOPIC_DISPLAY_CHARS)
        self.assertLessEqual(len(display_topic(LONG_A)), TOPIC_DISPLAY_CHARS)
        self.assertTrue(display_topic(LONG_A) == canonical_topic_text(LONG_A)[:TOPIC_DISPLAY_CHARS],
                        "展示形必须是身份文本的**前缀**：同一把归一化，不是第二套")
        self.assertNotEqual(topic_key(display_topic(LONG_A)), topic_key(LONG_A),
                            "截断形若与全名同 key，旧形状就没法在本轮被复现")
        self.assertFalse(_producer_records([_row(1, LONG_A)]) and
                         topic_key(canonical_topic_text(LONG_A)) != topic_key(LONG_A),
                         "去噪必须幂等：先取全名再算 key 与直接对 label 算 key 同值")


class ProducerFieldSemantics(unittest.TestCase):
    """§3：身份字段承载完整可重算身份；截断形只待在展示字段。"""

    def test_canonical_record_identity_fields_are_self_consistent(self) -> None:
        records = _producer_records([_row(1, LONG_A), _row(2, LONG_B)])
        self.assertEqual(2, len(records), "两个长主题不该被合并成一个（本轮没调阈值）")
        for rec in records:
            with self.subTest(record=rec["canonical_scoring_id"]):
                self.assertEqual(canonical_topic_text(rec["label"]), rec["topic"])
                self.assertGreater(len(rec["topic"]), TOPIC_DISPLAY_CHARS)
                self.assertEqual(display_topic(rec["label"]), rec["display_topic"])
                self.assertEqual(topic_key(rec["label"]), rec["topic_key"])
                self.assertEqual(topic_key(rec["topic"]), rec["topic_key"],
                                 "身份字段必须能重算出自己那条 key —— 这才叫自洽")
                self.assertTrue(rec["topic"].startswith(rec["display_topic"]))

    def test_presentation_field_cannot_move_identity(self) -> None:
        """展示字段被人改坏（甚至写成别的主题）也不得影响身份判定。"""
        rows = [_row(1, LONG_A)]
        records = _producer_records(rows)
        records[0]["display_topic"] = OFF_TOPIC          # 故意写坏展示形
        records[0]["scoring_topic_display"] = "任意胡说八道的显示名"
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows, records=records, pack_labels=[])
            reading = _authority(run)
            self.assertEqual("PASS", reading["status"], reading["reason"])
            self.assertEqual({"BOUND": 1, "AMBIGUOUS": 0, "MISMATCH": 0,
                              "OUT_OF_UNIVERSE": 0, "IDENTITY_ABSENT": 0},
                             reading["topic_record_binding_counts"])
            self.assertFalse(_gate(run)["raised"])


class R461RunELongTopicEntersGeneration(unittest.TestCase):
    """R46-1：>100 字真实形状的主题必须 PASS 身份校验，不得 false mismatch。"""

    def test_long_topic_from_producer_binds_and_enters_generation(self) -> None:
        rows = [_row(1, LONG_A), _row(2, LONG_B)]
        records = _producer_records(rows)
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows, records=records, pack_labels=[])
            reading = _authority(run)
            self.assertEqual("PASS", reading["status"], reading["reason"])
            self.assertTrue(reading["may_enter_generation"])
            self.assertEqual(2, reading["topic_expected_count"])
            self.assertEqual(2, reading["topic_mapped_count"])
            self.assertEqual([], reading["topic_missing_ids"])
            self.assertEqual(2, reading["topic_record_binding_counts"]["BOUND"])
            self.assertFalse(_gate(run)["raised"])

    def test_old_truncated_shape_still_fails_closed(self) -> None:
        """旧形状（`topic` = 60 字截断形）在新代码下仍然挡：本轮不改写 Run E，
        也不让"修复"变成对旧工件的宽容 —— 那道 MISMATCH 本来就是对的判据。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, LONG_A)],
                             records=[_record(1, LONG_A)],   # R4.5 夹具 = 旧生产形状
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual(MISMATCH, reading["status"], reading["reason"])
            self.assertFalse(reading["may_enter_generation"])
            self.assertEqual(1, reading["topic_record_binding_counts"]["MISMATCH"])
            self.assertTrue(_gate(run)["raised"])


class R462R463R464AdversarialStillBlock(unittest.TestCase):
    """§4：修截断不得把真错误一起放过去。"""

    def test_r462_stored_key_spoof_still_blocks(self) -> None:
        """topic=X 配 topic_key=A：身份仍只由主题文本决定，冒充不得成立。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, LONG_A)],
                             records=[_record(1, OFF_TOPIC, stored_key=topic_key(LONG_A),
                                              drop_label=True)],
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual(MISMATCH, reading["status"], reading["reason"])
            self.assertEqual(0, reading["topic_mapped_count"], "冒充来的身份不得参与覆盖")
            self.assertTrue(_gate(run)["raised"])

    def test_r462b_record_disagreeing_with_itself_still_blocks(self) -> None:
        rows = [_row(1, LONG_A)]
        records = _producer_records(rows)
        records[0]["topic"] = OFF_TOPIC            # 完整身份字段被换成另一个主题，key 不动
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "b", rows=rows, records=records, pack_labels=[])
            reading = _authority(run)
            self.assertEqual(MISMATCH, reading["status"], reading["reason"])
            self.assertEqual({"label": [topic_key(LONG_A)], "topic": [],
                              "stored_topic_key": [topic_key(LONG_A)]},
                             reading["topic_identity_mismatched_records"][0]["identity_resolutions"])

    def test_r463_non_transitive_bridge_still_ambiguous(self) -> None:
        rows = [_row(1, BRIDGE_A), _row(2, BRIDGE_B)]
        records = _producer_records(rows)          # 宇宙仍是两个独立身份
        bridge = build_scoring_taxonomy([_row(3, BRIDGE_C)])["canonical_scoring_records"][0]
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows + [_row(3, BRIDGE_C)],
                             records=records[:2] + [bridge], pack_labels=[])
            reading = _authority(run)
            self.assertEqual("SCORING_MAPPING_TOPIC_IDENTITY_AMBIGUOUS", reading["status"],
                             f"{reading['status']} / {records[0]['topic']!r} {bridge['topic']!r}")

    def test_r464_short_topic_unchanged(self) -> None:
        rows = [_row(1, SHORT_A)]
        records = _producer_records(rows)
        self.assertEqual(canonical_topic_text(SHORT_A), records[0]["topic"])
        self.assertEqual(display_topic(SHORT_A), records[0]["display_topic"],
                         "短主题的显示形与身份文本本就同源，修复不该动它")
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows, records=records, pack_labels=[])
            reading = _authority(run)
            self.assertEqual("PASS", reading["status"], reading["reason"])
            self.assertFalse(_gate(run)["raised"])


class R465CompletenessStillCorrect(unittest.TestCase):
    """§5 R46-5：身份修完之后，Topic Completeness 仍按身份集合核缺口。"""

    def test_missing_long_topic_still_blocks_coverage(self) -> None:
        rows = [_row(1, LONG_A), _row(2, LONG_B)]
        records = _producer_records(rows)
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows, records=records[:1], pack_labels=[])
            reading = _authority(run)
            self.assertEqual(COVERAGE_INCOMPLETE, reading["status"], reading["reason"])
            self.assertEqual([topic_key(LONG_B)], reading["topic_missing_ids"],
                             "缺的那一个长主题要被点名")
            self.assertTrue(_gate(run)["raised"])

    def test_extra_long_topic_still_blocks_as_not_in_universe(self) -> None:
        rows = [_row(1, LONG_A)]
        records = _producer_records([_row(1, LONG_A), _row(2, LONG_B)])
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows, records=records, pack_labels=[])
            reading = _authority(run)
            self.assertNotEqual("PASS", reading["status"], reading["reason"])
            self.assertFalse(reading["may_enter_generation"])

    def test_complete_long_topics_pass(self) -> None:
        rows = [_row(1, LONG_A), _row(2, LONG_B)]
        records = _producer_records(rows)
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows, records=records, pack_labels=[])
            self.assertEqual("PASS", _authority(run)["status"])


class R466GateEvidenceMatchesTheBlock(unittest.TestCase):
    """§7：raise 之前先持久化终态 BLOCK；消息、Authority、盘上证据三者一致。"""

    def _blocking_run(self, root: Path, name: str) -> tuple[Path, dict]:
        rows = [_row(1, LONG_A)]
        records = [_record(1, OFF_TOPIC, stored_key=topic_key(LONG_A), drop_label=True)]
        run = _build_run(root / name, rows=rows, records=records, pack_labels=[])
        # 规划链落 09 的同时也会写那份**独立**派生门文件（hierarchy.py 里那条非原子写）：
        # Run E 现场就是"文件在、写着 PASS"，本轮要修的就是 raise 之前把它换成本轮终态。
        _write(run / SCORING_MAPPING_GATE_FILE, _gate_payload(len(records), "PASS"))
        return run, records

    def test_gate_file_now_carries_this_runs_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, _ = self._blocking_run(root, "a")
            before = _read_gate_file(run)
            self.assertEqual("PASS", before.get("status"),
                             "夹具前提：规划侧那份自报是 PASS（Run E 现场就是这个形状）")
            fresh_before = canonical_scoring_authority(run)
            with self.assertRaises(PreGenerationScoringGateError) as ctx:
                require_pre_generation_scoring_mapping_gate(run)
            message = str(ctx.exception)
            after = _read_gate_file(run)
            self.assertEqual(SCORING_MAPPING_GATE_FILE, after["previous_derived_claim"]["file"])
            self.assertEqual("longform.orchestrator.require_pre_generation_scoring_mapping_gate",
                             after["written_by"], "证据要认得出是谁写的")
            self.assertEqual(fresh_before["status"], after["status"],
                             "盘上证据的 status 必须就是 Authority 本轮的判据")
            self.assertFalse(after["may_enter_generation"])
            self.assertTrue(after["fail_closed"])
            self.assertEqual(fresh_before["reason"], after["reason"])
            self.assertEqual(fresh_before["scoring_authority_identity"],
                             after["scoring_authority_identity"],
                             "错误消息指向的那份文件要带着它量的是哪一份工件的身份")
            self.assertEqual(fresh_before["topic_record_binding_counts"],
                             after["topic_record_binding_counts"])
            self.assertIn("SCN-001", json.dumps(after["topic_identity_mismatched_records"],
                                                ensure_ascii=False))
            self.assertEqual("PASS", after["previous_derived_claim"]["status"],
                             "旧的声称不删不改写，降级成声称留在证据里")
            self.assertFalse(after["provider_calls_initiated"])
            # 三者一致：消息里点名的记录、盘上点名的记录、Authority 点名的记录是同一批
            self.assertIn("SCN-001", message)
            self.assertEqual([row["record"] for row in fresh_before["topic_identity_mismatched_records"]],
                             [row["record"] for row in after["topic_identity_mismatched_records"]])
            # 写回不改变判据本身（幂等），也不把 BLOCK 变成一个新的"矛盾"名字
            again = canonical_scoring_authority(run)
            self.assertEqual(fresh_before["status"], again["status"], again["reason"])
            self.assertFalse(again["may_enter_generation"])
            self.assertTrue(_gate(run)["raised"])
            self.assertEqual(after["status"], _read_gate_file(run)["status"],
                             "第二次 raise 写的仍是同一份判据，不越写越花")

    def test_passing_entry_does_not_rewrite_the_gate(self) -> None:
        rows = [_row(1, LONG_A)]
        records = _producer_records(rows)
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=rows, records=records, pack_labels=[])
            claim = _gate_payload(len(records), "PASS")
            _write(run / SCORING_MAPPING_GATE_FILE, claim)
            require_pre_generation_scoring_mapping_gate(run)
            self.assertEqual(claim, _read_gate_file(run),
                             "放行路径不写 BLOCK 文件：入口不得替规划链自报结论")


class StructuralSentinelsNotGuessed(unittest.TestCase):
    """§2 的禁令用 AST 钉住，不靠自觉。"""

    @staticmethod
    def _tree(module):
        return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    def test_thresholds_and_display_ruler_untouched(self) -> None:
        self.assertEqual(0.85, TOPIC_MERGE_SIMILARITY)
        self.assertEqual(6, MIN_MERGE_TOPIC_CHARS)
        self.assertEqual(60, TOPIC_DISPLAY_CHARS)

    def test_authority_has_no_length_hardcoding_and_no_prefix_guessing(self) -> None:
        tree = self._tree(authority_module)
        bound = {node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("_record_topic_sources", "_bind_record_identity"):
            self.assertIn(name, bound)
        numbers = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, int)
                   and node.value in (60, 70, 107, 61)}
        self.assertEqual(set(), numbers, "身份路径里出现长度字面量 = 按长度硬编码")
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        for banned in ("startswith", "endswith", "removesuffix"):
            self.assertNotIn(banned, calls, f"Authority 用 {banned} 猜截断 = §3 明令禁止的那条路")

    def test_identity_cues_exclude_presentation_fields(self) -> None:
        self.assertEqual(("label", "topic", "scoring_topic"),
                         authority_module.RECORD_TOPIC_TEXT_FIELDS)
        presentation = set(authority_module.RECORD_PRESENTATION_FIELDS)
        self.assertTrue(presentation)
        self.assertEqual(set(), presentation & set(authority_module.RECORD_TOPIC_TEXT_FIELDS),
                         "展示字段一旦进了身份线索，截断形就又开始冒充身份")

    def test_single_normalization_family_and_no_second_ruler(self) -> None:
        tree = self._tree(taxonomy_module)
        defs = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertTrue({"strip_scoring_noise", "normalize_text", "topic_key",
                         "canonical_topic_text", "display_topic"} <= defs or
                        "normalize_text" not in defs)
        # display_topic 必须是 canonical_topic_text 的前缀截断（同一把尺）
        body = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "display_topic")
        src = ast.unparse(body)
        self.assertIn("canonical_topic_text", src)
        self.assertIn("TOPIC_DISPLAY_CHARS", src)
        self.assertNotIn("strip_scoring_noise", src, "显示形若另起一遍归一化，就是第二套尺")

    def test_no_topic_name_or_record_id_used_as_data(self) -> None:
        """产品码不得把某条记录号或某个主题名当数据用（注释与 docstring 里讲历史不算特判）。"""
        for module in (taxonomy_module, authority_module):
            tree = self._tree(module)
            doc_values = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                    continue
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    doc_values.add(body[0].value.value)
            data_strings = [node.value for node in ast.walk(tree)
                            if isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and node.value not in doc_values]
            joined = "\n".join(data_strings)
            for banned in ("SCN-001", "秩序维护团队配置", "环境绿化与保洁作业方案"):
                self.assertNotIn(banned, joined,
                                 f"{module.__name__} 把 {banned!r} 写成了数据 = 特判")


if __name__ == "__main__":
    unittest.main()
