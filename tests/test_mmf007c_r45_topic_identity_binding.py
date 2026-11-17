# -*- coding: utf-8 -*-
"""MMF-007C Round 4.5：Canonical Topic **Identity Binding**（Codex 最新 Final Gate）。

任务书只修一件事，本文件也只量这一件事：一条落盘的 `canonical_scoring_records`
必须先绑定到**恰好一个** canonical 主题身份，才有资格参与覆盖判定。

Codex 的两条反例（都用生产入口与正式落盘边界量，不测 helper 旁路）：

  * **non-transitive bridge**：A-B 低于合并阈值 ⇒ taxonomy 判 A、B 是两个独立主题；
    C-A、C-B 都过阈值 ⇒ 一条记录 C 就把 A、B 各"覆盖"了一次。上一轮的
    `missing = [k for k in expected if 任意一条记录与 k 相似]` 是多对多关系，
    所以 expected=2 / mapped=1 照样 `PASS`。
  * **identity spoof**：`record.topic = X` 而 `record.topic_key = A` 的 key ——
    身份取自 stored key，X 于是冒充 A，既躲开"宇宙外多出来"，又顶上了 A 的覆盖。

本轮立的不变量（I1–I6）：1 个命中 = 合法绑定；0 个命中 = 挡住（宇宙外）；
多个命中 = 挡住 `SCORING_MAPPING_TOPIC_IDENTITY_AMBIGUOUS`（不挑一个、不许一条顶两个）；
stored 与现算不符 = 挡住 `SCORING_MAPPING_TOPIC_IDENTITY_MISMATCH`。

同一条尺子里还钉着**不许误杀**：单义别名（只与 A 过阈值）必须正常绑定、完整覆盖必须
仍然 `PASS`、零主题必须仍是合法 `NOT_APPLICABLE`、`display_topic` 截断出来的显示形
不得被判成身份不符。

本轮一个字都没调阈值、没写第二套归一化、没碰终态算法 —— 文件末尾那几条结构哨兵
就是钉这一点的，不靠自觉。零 Provider 调用：纯本地临时目录 + Mock 夹具。
"""
from __future__ import annotations

import ast
import inspect
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
from longform import run_state  # noqa: E402
from longform.orchestrator import (  # noqa: E402
    PreGenerationScoringGateError, generate_longform,
    require_pre_generation_scoring_mapping_gate,
)
from longform.run_state import finalize_delivery_state  # noqa: E402
from longform.scoring_authority import (  # noqa: E402
    canonical_scoring_authority, canonical_topic_coverage,
)
import planning.scoring_taxonomy as taxonomy_module  # noqa: E402
from planning.scoring_taxonomy import (  # noqa: E402
    MIN_MERGE_TOPIC_CHARS, TOPIC_MERGE_SIMILARITY, display_topic, topic_key, topic_similarity,
)
from providers.mock import MockProvider  # noqa: E402
from test_longform_orchestrator import _brief, _pack, _selection  # noqa: E402
from test_mmf007c_r43_scoring_authority_closure import (  # noqa: E402
    HIERARCHY, MATRIX, PLAN_SECTIONS, PLAN_UNITS, STATE_09, _forged_clean,
    _gate_payload, _hierarchy, _matrix_payload, _planned_run, _write,
)
from test_mmf007c_r4_final_state_contract import _make_run  # noqa: E402

# —— 桥接三元组：A 与 B **不**过合并阈值（两个独立 canonical 主题），C 与两边**都**过。
# 一次编辑最多改动 4 个字符二元组，所以桥要构造在 ~60 字的长标签上；这三条字符串
# 只是夹具，产品里一个都不出现（见文件末尾的反硬编码哨兵）。
BRIDGE_A = ("双碳管理应急预案秩序维护培训演练设备台账保障体系网格管理满意度回访人员保障"
            "双碳管理信息公开品质检查服务体系成本控制考核监督")
BRIDGE_B = ("闭环管理应急预案秩序维护培训演练设备台账保障体系网格管理满意度回访人员保障"
            "双碳管理信息公开网格管理服务体系成本控制考核监督")
BRIDGE_C = ("闭环管理应急预案秩序维护培训演练设备台账保障体系网格管理满意度回访人员保障"
            "双碳管理信息公开品质检查服务体系成本控制考核监督")
# 单义别名：只与 A 过阈值，与 B 不过 ⇒ 必须能正常绑定成 A（收紧不许变成一律挡）。
ALIAS_OF_A = ("台账管理应急预案秩序维护培训演练设备台账保障体系网格管理满意度回访人员保障"
              "双碳管理信息公开品质检查服务体系成本控制考核监督")
OFF_TOPIC = "智慧物业平台建设投入承诺与资金来源说明"
# 真规划链能真的绑上落点的两个主题名（与 R4.4 同一对）：Provider 计数那条腿用它 ——
# 合成长标签在真规划链里连 CU 都绑不上，会被既有的 PLANNED_CONTENT_UNIT 先挡下来。
REAL_TOPIC_A = "安全秩序管理含门岗巡逻与异常处置"
REAL_TOPIC_B = "设备设施运行维护与保养方案"

AMBIGUOUS = "SCORING_MAPPING_TOPIC_IDENTITY_AMBIGUOUS"
MISMATCH = "SCORING_MAPPING_TOPIC_IDENTITY_MISMATCH"
AMBIGUOUS_BLOCKER = "SCORING_AUTHORITY_TOPIC_IDENTITY_AMBIGUOUS"
MISMATCH_BLOCKER = "SCORING_AUTHORITY_TOPIC_IDENTITY_MISMATCH"


def _merges(left: str, right: str) -> bool:
    ka, kb = topic_key(left), topic_key(right)
    return (topic_similarity(ka, kb) >= TOPIC_MERGE_SIMILARITY
            and min(len(ka), len(kb)) >= MIN_MERGE_TOPIC_CHARS)


def _items(labels: list[str]) -> list[dict]:
    return [{"scoring_item_id": f"SCR-{n:02d}", "must_respond": True, "label": label}
            for n, label in enumerate(labels, start=1)]


def _row(index: int, label: str) -> dict:
    return {"scoring_item_id": f"SCR-{index:02d}", "label": label, "must_respond": True,
           "scoring_class": "TECHNICAL_SUBSTANTIVE", "scope_class": "TECHNICAL_INCLUDED",
           "primary_section_id": PLAN_SECTIONS[0], "requirement_ids": ["REQ-0002"],
           "planned_response_cu": list(PLAN_UNITS[:1])}


def _record(index: int, label: str, *, section: str = PLAN_SECTIONS[0],
            cu: tuple[str, ...] = PLAN_UNITS[:1], stored_key: str | None = None,
            topic_text: str | None = None, drop_label: bool = False) -> dict:
    """照规划链写 canonical 记录的形状来造夹具：`label` / `topic` / `topic_key` 都在。

    合法记录的 stored key 一律由 label 现算 —— 只有伪造那一格才手工填别人的 key。
    """
    record = {"canonical_scoring_id": f"SCN-{index:03d}", "source_ids": [f"SCR-{index:02d}"],
              "label": label,
              "topic": topic_text if topic_text is not None else display_topic(label),
              "topic_key": stored_key if stored_key is not None else topic_key(label),
              "planned_response_section": section,
              "planned_response_sections": [section],
              "planned_response_cu": list(cu),
              "cu_binding": {"basis": "EXPLICIT_SCORING_ID"},
              "evidence_contract": {"concepts": ["覆盖范围", "响应时限"], "min_concepts": 2}}
    if drop_label:
        record.pop("label")
    return record


def _build_run(root: Path, *, rows: list[dict], records: list[dict],
               pack_labels: list[str], deliverable: bool = False) -> Path:
    """按事实落一份盘：09（行 + 记录 + 自洽的嵌门）+ 08（计划清单）+ canonical 抽取。

    嵌进去的门结论一律自洽（条数对得上、引用查得到），这样挡下来的原因
    只可能是本轮的**身份绑定**，不是上一轮已经关掉的那几格。
    """
    run = (_make_run(root, pages=286, page_targets=(250, 320)) if deliverable else root / "run")
    if not deliverable:
        (run / "tender").mkdir(parents=True, exist_ok=True)
    _write(run / MATRIX, _matrix_payload(records, rows=rows, verdict="PASS"))
    _write(run / HIERARCHY, _hierarchy())
    pack = _pack()
    pack["scoring_items"] = _items(pack_labels)
    _write(run / "tender" / "requirement_pack.json", pack)
    return run


def _authority(run: Path) -> dict:
    reading = canonical_scoring_authority(run)
    keep = ("status", "may_enter_generation", "reason", "clean_blockers",
            "topic_expected_count", "topic_mapped_count", "topic_missing_ids",
            "topic_extra_ids", "topic_duplicate_ids", "topic_record_binding_counts",
            "topic_identity_ambiguous_records", "topic_identity_mismatched_records",
            "topic_universe_layers")
    return {key: reading.get(key) for key in keep}


def _gate(run: Path) -> dict:
    try:
        require_pre_generation_scoring_mapping_gate(run)
        return {"raised": False, "error": None}
    except PreGenerationScoringGateError as exc:
        return {"raised": True, "error": str(exc)[:400]}


def _terminal(run: Path) -> str:
    return ((finalize_delivery_state(run, medium="WORD") or {}).get("derived") or {}).get(
        "delivery_status")


class BridgeShapeIsWhatItClaims(unittest.TestCase):
    """夹具不成立，后面所有的"复现"就都是空的 —— 先让尺子自己说话。"""

    def test_bridge_triple_and_alias_are_what_they_claim(self) -> None:
        self.assertFalse(_merges(BRIDGE_A, BRIDGE_B), "A-B 若过阈值，桥就塌成了一个主题")
        self.assertTrue(_merges(BRIDGE_C, BRIDGE_A))
        self.assertTrue(_merges(BRIDGE_C, BRIDGE_B))
        self.assertTrue(_merges(ALIAS_OF_A, BRIDGE_A), "别名必须与 A 同一件事，否则 I4 没在量别名")
        self.assertFalse(_merges(ALIAS_OF_A, BRIDGE_B), "别名若两边都过，它就成了第二个桥")
        self.assertFalse(_merges(OFF_TOPIC, BRIDGE_A))
        with self.subTest("阈值这一轮没被调过"):
            self.assertEqual(0.85, TOPIC_MERGE_SIMILARITY)
            self.assertEqual(6, MIN_MERGE_TOPIC_CHARS)


class CanonicalTopicIdentityBinding(unittest.TestCase):
    """I1–I6：绑定是唯一新增的判据，其余几档一律保持原样。"""

    def test_i1_single_bridge_record_blocks_ambiguously(self) -> None:
        """I1：expected{A,B} + 记录只有一条 C ⇒ BLOCK / AMBIGUOUS，不许 PASS。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _build_run(root / "a", rows=[_row(1, BRIDGE_A), _row(2, BRIDGE_B)],
                             records=[_record(1, BRIDGE_C)], pack_labels=[])
            reading = _authority(run)
            self.assertEqual(AMBIGUOUS, reading["status"], reading["reason"])
            self.assertFalse(reading["may_enter_generation"])
            self.assertIn(AMBIGUOUS_BLOCKER, reading["clean_blockers"])
            self.assertEqual(2, reading["topic_expected_count"])
            self.assertEqual(0, reading["topic_mapped_count"], "一条桥记录不得算成已绑定")
            self.assertEqual(2, len(reading["topic_missing_ids"]), "两个主题都还缺着")
            self.assertEqual({"BOUND": 0, "AMBIGUOUS": 1, "MISMATCH": 0,
                              "OUT_OF_UNIVERSE": 0, "IDENTITY_ABSENT": 0},
                             reading["topic_record_binding_counts"])
            self.assertEqual([topic_key(BRIDGE_A), topic_key(BRIDGE_B)],
                             reading["topic_identity_ambiguous_records"][0]["candidates"],
                             "读数必须点名这条记录撞到的是哪两个 canonical 身份")

    def test_i1_entry_gate_and_terminal_both_refuse_the_bridge(self) -> None:
        """同一个形状：入口门 raise，终态拿不到 Clean（一条顶两个主题不叫覆盖完整）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate_run = _build_run(root / "g", rows=[_row(1, BRIDGE_A), _row(2, BRIDGE_B)],
                                  records=[_record(1, BRIDGE_C)], pack_labels=[])
            self.assertTrue(_gate(gate_run)["raised"], "入口门必须对同一份读数 raise")
            delivered = _build_run(root / "d", rows=[_row(1, BRIDGE_A), _row(2, BRIDGE_B)],
                                   records=[_record(1, BRIDGE_C)], pack_labels=[],
                                   deliverable=True)
            self.assertNotEqual("COMPLETED_CLEAN", _terminal(delivered))
            forged = _forged_clean(delivered)
            self.assertNotEqual("COMPLETED_CLEAN", forged["delivery_status"],
                                "硬写 COMPLETED_CLEAN 也不许进投影")
            self.assertEqual("FAIL", forged["invariants"][STATE_09])

    def test_i2_duplicate_bridge_records_add_no_coverage(self) -> None:
        """I2：两条都是 C（落点不同）⇒ 仍然 BLOCK，条数冒充不了身份。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A), _row(2, BRIDGE_B)],
                             records=[_record(1, BRIDGE_C),
                                      _record(2, BRIDGE_C, section=PLAN_SECTIONS[1],
                                              cu=PLAN_UNITS[1:2])],
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual(AMBIGUOUS, reading["status"], reading["reason"])
            self.assertFalse(reading["may_enter_generation"])
            self.assertEqual(2, reading["topic_record_binding_counts"]["AMBIGUOUS"])
            self.assertEqual(0, reading["topic_mapped_count"])

    def test_i3_stored_topic_key_cannot_override_topic_text(self) -> None:
        """I3：topic=X 而 topic_key=A ⇒ BLOCK / MISMATCH，stored 不得压过现算。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A)],
                             records=[_record(1, OFF_TOPIC, stored_key=topic_key(BRIDGE_A),
                                              drop_label=True)],
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual(MISMATCH, reading["status"], reading["reason"])
            self.assertFalse(reading["may_enter_generation"])
            self.assertIn(MISMATCH_BLOCKER, reading["clean_blockers"])
            self.assertEqual(1, reading["topic_record_binding_counts"]["MISMATCH"])
            self.assertEqual(0, reading["topic_mapped_count"], "冒充来的身份不得参与覆盖")
            self.assertTrue(_gate(run)["raised"])
            delivered = _build_run(Path(tmp) / "d", rows=[_row(1, BRIDGE_A)],
                                   records=[_record(1, OFF_TOPIC, stored_key=topic_key(BRIDGE_A),
                                                    drop_label=True)],
                                   pack_labels=[], deliverable=True)
            self.assertNotEqual("COMPLETED_CLEAN", _terminal(delivered))

    def test_i3b_record_that_disagrees_with_itself_blocks(self) -> None:
        """I3b：label=A、topic=X（同一份记录内部身份来源互不相容）⇒ BLOCK。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A)],
                             records=[_record(1, BRIDGE_A, topic_text=OFF_TOPIC)],
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual(MISMATCH, reading["status"], reading["reason"])
            self.assertFalse(reading["may_enter_generation"])
            self.assertEqual({"label": [topic_key(BRIDGE_A)], "topic": [],
                              "stored_topic_key": [topic_key(BRIDGE_A)]},
                             reading["topic_identity_mismatched_records"][0]
                             ["identity_resolutions"],
                             "读数要看得见是哪一条线索落空了")

    def test_record_without_topic_text_is_not_an_identity(self) -> None:
        """只带 stored key、没有任何主题文本 ⇒ 核不出身份，宇宙不可核实（不是放行）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A)],
                             records=[{"canonical_scoring_id": "SCN-001",
                                       "topic_key": topic_key(BRIDGE_A),
                                       "planned_response_section": PLAN_SECTIONS[0],
                                       "planned_response_sections": [PLAN_SECTIONS[0]],
                                       "planned_response_cu": list(PLAN_UNITS[:1]),
                                       "cu_binding": {"basis": "EXPLICIT_SCORING_ID"},
                                       "evidence_contract": {"concepts": ["覆盖范围"],
                                                             "min_concepts": 1}}],
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual("SCORING_MAPPING_TOPIC_UNIVERSE_UNVERIFIABLE", reading["status"],
                             reading["reason"])
            self.assertFalse(reading["may_enter_generation"])
            self.assertEqual(1, reading["topic_record_binding_counts"]["IDENTITY_ABSENT"])

    def test_i4_single_sense_alias_still_binds(self) -> None:
        """I4：单义别名必须正常绑定成 A —— 收紧不许顺手误杀别名。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A)],
                             records=[_record(1, ALIAS_OF_A)], pack_labels=[])
            reading = _authority(run)
            self.assertEqual("PASS", reading["status"], reading["reason"])
            self.assertTrue(reading["may_enter_generation"])
            self.assertEqual(1, reading["topic_record_binding_counts"]["BOUND"])
            self.assertEqual([], reading["topic_missing_ids"])

    def test_i5_complete_coverage_still_passes_and_is_clean(self) -> None:
        """I5：两条记录各自唯一 resolve ⇒ PASS，合法 Clean 照旧拿得到。"""
        with tempfile.TemporaryDirectory() as tmp:
            rows = [_row(1, BRIDGE_A), _row(2, BRIDGE_B)]
            records = [_record(1, BRIDGE_A),
                       _record(2, BRIDGE_B, section=PLAN_SECTIONS[1], cu=PLAN_UNITS[1:2])]
            run = _build_run(Path(tmp) / "a", rows=rows, records=records, pack_labels=[])
            reading = _authority(run)
            self.assertEqual("PASS", reading["status"], reading["reason"])
            self.assertEqual(2, reading["topic_mapped_count"])
            self.assertEqual({"BOUND": 2, "AMBIGUOUS": 0, "MISMATCH": 0,
                              "OUT_OF_UNIVERSE": 0, "IDENTITY_ABSENT": 0},
                             reading["topic_record_binding_counts"])
            delivered = _build_run(Path(tmp) / "d", rows=rows, records=records,
                                   pack_labels=[], deliverable=True)
            self.assertEqual("COMPLETED_CLEAN", _terminal(delivered),
                             "完整覆盖 + QA PASS + 286 页在带内，Clean 不得被误杀")
            self.assertEqual("PASS", _forged_clean(delivered)["invariants"][STATE_09])

    def test_i6_zero_topic_stays_legal_not_applicable(self) -> None:
        """I6：期望宇宙算出来是 0 ⇒ 合法 N/A 继续放行（§5 的反方向）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[], records=[], pack_labels=[])
            reading = _authority(run)
            self.assertEqual("NOT_APPLICABLE", reading["status"], reading["reason"])
            self.assertTrue(reading["may_enter_generation"])
            self.assertEqual(0, reading["topic_expected_count"])

    def test_truncated_display_form_is_not_a_mismatch(self) -> None:
        """`display_topic` 截到 60 字：显示形与全名差个尾巴，不得判成身份不符。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A)],
                             records=[_record(1, BRIDGE_A, drop_label=True)], pack_labels=[])
            key_from_label = topic_key(BRIDGE_A)
            key_from_display = topic_key(display_topic(BRIDGE_A))
            self.assertNotEqual(key_from_label, key_from_display, "截断这一层必须真的存在")
            reading = _authority(run)
            self.assertEqual("PASS", reading["status"], reading["reason"])
            self.assertEqual(1, reading["topic_record_binding_counts"]["BOUND"])


class CoverageIsNotManyToMany(unittest.TestCase):
    """§"不得继续用逐 expected topic 找任意 similar record"：结构上钉死。"""

    def test_coverage_missing_is_a_set_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp), rows=[_row(1, BRIDGE_A), _row(2, BRIDGE_B)],
                             records=[_record(1, BRIDGE_C)], pack_labels=[])
            matrix = json.loads((run / MATRIX).read_text(encoding="utf-8-sig"))
            coverage = canonical_topic_coverage(
                run, matrix, matrix.get("canonical_scoring_records") or [])
            self.assertEqual(set(), coverage["mapped"], "桥记录不得贡献任何已绑定身份")
            self.assertEqual({topic_key(BRIDGE_A), topic_key(BRIDGE_B)}, set(coverage["missing"]))
            self.assertEqual(coverage["expected"], set(coverage["missing"]) | coverage["mapped"])
            for row in coverage["bindings"]:
                self.assertIn(row["status"], ("BOUND", "AMBIGUOUS", "MISMATCH",
                                              "OUT_OF_UNIVERSE", "IDENTITY_ABSENT"))

    def test_authority_does_not_search_records_per_expected_topic(self) -> None:
        """源码级哨兵：覆盖那一档不许再出现"对每个 expected 找相似记录"的写法。"""
        body = inspect.getsource(authority_module.canonical_topic_coverage)
        self.assertNotIn("for key in expected if not _topic_covered", body.replace("  ", " "))
        self.assertIn("_bind_record_identity", body)
        self.assertIn("expected - bound", body)
        # `_topic_covered` 只留给三层对账（宇宙之间），不许再出现在记录侧。
        self.assertNotIn("_topic_covered(key, mapped)", body)
        self.assertNotIn("_topic_covered(key, expected)", body)

    def test_no_threshold_tuning_and_no_second_normalization(self) -> None:
        """本轮禁改清单里最容易被"顺手改一下"违反的两条：阈值与归一化。"""
        tree = ast.parse(inspect.getsource(authority_module))
        defined = {node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("topic_key", "topic_similarity", "normalize_text", "display_topic",
                     "strip_scoring_noise"):
            self.assertNotIn(name, defined, f"Authority 里不得另立 {name} —— 只能引规划链那把尺")
        assigned = {str(getattr(target, "id", "")) for node in ast.walk(tree)
                    if isinstance(node, ast.Assign) for target in node.targets}
        self.assertNotIn("TOPIC_MERGE_SIMILARITY", assigned)
        self.assertNotIn("MIN_MERGE_TOPIC_CHARS", assigned)
        self.assertIs(TOPIC_MERGE_SIMILARITY, taxonomy_module.TOPIC_MERGE_SIMILARITY)
        self.assertIs(MIN_MERGE_TOPIC_CHARS, taxonomy_module.MIN_MERGE_TOPIC_CHARS)
        self.assertIs(authority_module.topic_similarity, taxonomy_module.topic_similarity)
        self.assertIs(authority_module.topic_key, taxonomy_module.topic_key)

    def test_bridge_labels_never_appear_in_the_product(self) -> None:
        """反例靠夹具成立，不靠产品里写死 A/B/C。"""
        source = (APP / "longform" / "scoring_authority.py").read_text(encoding="utf-8")
        for label in (BRIDGE_A[:20], BRIDGE_B[:20], BRIDGE_C[:20], OFF_TOPIC[:12]):
            self.assertNotIn(label, source, "硬编码主题名等于把不变式退化成一次匹配")

    def test_terminal_consumes_the_same_reading_and_has_no_identity_logic(self) -> None:
        """§不许修改 Finalizer 算法：身份绑定只准住在 Authority 这一侧。"""
        source = inspect.getsource(run_state)
        for token in ("_bind_record_identity", "canonical_topic_coverage",
                      "topic_identity_ambiguous_records", "SCORING_MAPPING_TOPIC_IDENTITY",
                      "TOPIC_MERGE_SIMILARITY", "build_scoring_taxonomy"):
            self.assertNotIn(token, source, f"终态里出现了 {token} —— 那就是第二套判据")


class EarlierAttacksStillBlocked(unittest.TestCase):
    """R4.3 / R4.4 关掉的那几格不许被这轮收紧顺手打开：都还在各自的名字下挡着。"""

    def test_record_count_still_cannot_mask_a_missing_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A), _row(2, BRIDGE_B)],
                             records=[_record(1, BRIDGE_A), _record(2, BRIDGE_A,
                                                                    section=PLAN_SECTIONS[1],
                                                                    cu=PLAN_UNITS[1:2])],
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual("SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE", reading["status"],
                             reading["reason"])
            self.assertEqual([topic_key(BRIDGE_B)], reading["topic_missing_ids"])
            self.assertEqual([topic_key(BRIDGE_A)], reading["topic_duplicate_ids"])

    def test_extra_topic_without_a_stored_key_is_still_not_in_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A)],
                             records=[_record(1, BRIDGE_A), _record(2, OFF_TOPIC)],
                             pack_labels=[])
            reading = _authority(run)
            self.assertEqual("SCORING_MAPPING_TOPIC_NOT_IN_UNIVERSE", reading["status"],
                             reading["reason"])
            self.assertEqual([topic_key(OFF_TOPIC)], reading["topic_extra_ids"])
            self.assertIn("SCORING_AUTHORITY_TOPIC_NOT_IN_UNIVERSE", reading["clean_blockers"])

    def test_a_legally_mapped_record_still_does_not_prove_the_rest(self) -> None:
        """R4.4 Codex 原样反例（2/2/1）：本轮不许把它放回 PASS。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _build_run(Path(tmp) / "a", rows=[_row(1, BRIDGE_A), _row(2, BRIDGE_B)],
                             records=[_record(1, BRIDGE_A)], pack_labels=[])
            reading = _authority(run)
            self.assertEqual("SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE", reading["status"],
                             reading["reason"])
            self.assertFalse(reading["may_enter_generation"])


class ProductionEntryBlocksBeforeAnyProviderCall(unittest.TestCase):
    """身份伪造在真规划链上被入口挡下：一条 Provider 请求都不许发出去。"""

    def test_forged_identity_raises_at_the_entry_with_zero_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _planned_run(root / "run", scoring_items=_items([REAL_TOPIC_A, REAL_TOPIC_B]))
            matrix = json.loads((run / MATRIX).read_text(encoding="utf-8-sig"))
            records = matrix.get("canonical_scoring_records") or []
            if len(records) < 2:
                self.skipTest(f"规划链只写了 {len(records)} 条 canonical 记录，"
                              "这条腿需要两条才能只改最后一条的主题文本")
            last = dict(records[-1])
            last["label"] = OFF_TOPIC
            last["topic"] = display_topic(OFF_TOPIC)   # stored topic_key 故意留着：Codex 的形状
            matrix["canonical_scoring_records"] = [dict(row) for row in records[:-1]] + [last]
            matrix["pre_generation_scoring_mapping_gate"] = _gate_payload(
                len(matrix["canonical_scoring_records"]), "PASS")
            _write(run / MATRIX, matrix)
            before = (run / MATRIX).read_bytes()

            provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                                     "enabled": True, "model": "mock-longform"})
            with self.assertRaises(PreGenerationScoringGateError) as caught:
                generate_longform(run_dir=run, provider=provider, provider_name="mock",
                                  brief=_brief(), selection=_selection(),
                                  selected_ids=["KU-0001"], task_mode="standard")
            self.assertIn("topic_key", str(caught.exception))
            self.assertEqual(0, len(getattr(provider, "received_requests") or []),
                             "挡下来必须发生在任何一次请求之前")
            self.assertEqual(before, (run / MATRIX).read_bytes(),
                             "这条腿要求计划被复用、矩阵不被改写，否则 Provider=0 不是在这份输入上量的")


if __name__ == "__main__":
    unittest.main()
