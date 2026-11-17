"""MMF-007C Round 4 · P0-C2（§44–§49）：Volume 中间 DOCX 定 B + 终态只有一处真值。

全部离线：tmp 目录里造状态文件 + 纯函数调用，不发起任何 Provider 调用（§1）。
本模块盯的是"代码按 B 跑、状态按 A 判"这个分裂：装配端从不需要 `volumes/Vxx.docx`，
而 STATE-04 与 COMPLETED_CLEAN 却拿它在场与否当判据 —— Run D 一次完整交付因此记 FAIL。
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
for _path in (str(APP), str(APP_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from longform.cu_state import SECTION_DRAFTABLE_STATES, SECTION_PASS_STATES  # noqa: E402
from longform.hierarchical import VOLUME_ARTIFACT_CONTRACT  # noqa: E402
from longform.run_state import collect_facts, derive_run_state, evaluate_invariants  # noqa: E402

VOLUMES = {
    "schema_version": "volume-generation-plan-v0.1",
    "volumes": [
        {"volume_id": "V01", "section_ids": ["S01-01", "S01-02"],
         "content_unit_ids": ["CU-001", "CU-002"], "generation_status": "COMPLETED"},
        {"volume_id": "V02", "section_ids": ["S02-01"],
         "content_unit_ids": ["CU-003"], "generation_status": "COMPLETED"},
    ],
}
CLEAN_SECTIONS = {"S01-01": "COMPLETED", "S01-02": "COMPLETED_WITH_WARNING", "S02-01": "COMPLETED"}


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


FINAL_ARTIFACT_QA_FILE = "final_artifact_qa.json"
_UNSET = object()


def _write_final_artifact_qa(run: Path, status: object = "PASS", *,
                             artifact: Path | None = None,
                             bind: bool = True,
                             inspected: bool = True) -> Path:
    """写一份"对当前落位交付件量过"的渲染后 QA —— 三个身份字段从盘上真实字节现算。

    Round 4.7：真实交付件已在场时，Clean 的 QA 凭据必须是这份报告，而且必须**绑**
    在它量的那份产物上（`governance/final_artifact_qa.py:644-646` 就是这么写的：
    `artifact_path` / `sha256` / `actual_docx_inspected`）。夹具若硬编码一个哈希，
    测的就不是绑定；所以这里一律读盘现算。

    `bind=False` / `inspected=False` 是要测的形状（量的是另一份字节 / 没真开过 DOCX），
    不是缺省 —— 缺省是一份**合法**的最终 QA。
    """
    target = artifact or (run / "artifact" / "final.docx")
    if bind and not target.is_file():
        # 交付件还没落位就写"绑它"的报告 = 夹具自己造出一份永不对得上的凭据。
        # （`run_layout.finalize_run` 才会把根目录的 DOCX 拷进 `artifact/`，
        #   要绑就绑那一份真的在场的字节。）
        raise AssertionError(f"REFUSE: 要绑定的交付件不存在 {target}")
    bound_bytes = target.read_bytes() if (bind and target.is_file()) else b"other-artifact-bytes"
    report = {
        "status": status,
        "findings": [],
        "artifact_path": str(target),
        "sha256": hashlib.sha256(bound_bytes).hexdigest(),
        "actual_docx_inspected": inspected,
        "schema_version": "final-artifact-qa-v0.1",
    }
    _write(run / FINAL_ARTIFACT_QA_FILE, report)
    return run / FINAL_ARTIFACT_QA_FILE


def _make_run(root: Path, *, sections: dict[str, str] | None = None,
              units: list[str] | None = ("CU-001", "CU-002", "CU-003"),
              render_docx: bool = True, with_final: bool = True,
              final_qa: object = "PASS",
              artifact_qa: object = _UNSET,
              pages: int | None = 286, page_targets: tuple[int, int] | None = (250, 320),
              projected_pages: int | None = None,
              final_qa_raw: str | None = None,
              artifact_qa_raw: str | None = None) -> Path:
    """造一个"内容全齐、但分卷 DOCX 一份都没渲染"的 Run —— B 语义下这就是可干净完成的交付。

    Round 4.1 §24：这份夹具过去**不写** `artifact_qa_report.json`，于是"干净完成"是
    靠终态判据把"没有最终 QA 报告"读成"最终 QA 通过"拿到的 —— 那正是本轮要堵的洞。
    现在夹具默认写一份 PASS 报告（一次真实的干净交付本来就有它），
    `final_qa=None` 才表示"报告缺席"，供 P0-3 的 fail-closed 用例显式构造。

    Round 4.7 再补一份：`final_qa` 量的是**渲染前 / 装配期**那份 `artifact_qa_report.json`
    （名字比语义先来，这里不改名以免动到全部调用点）；真实交付 DOCX 一旦落位，
    终态读的是 `final_artifact_qa.json`。所以夹具默认**两份都写**、状态一致，
    并且把后一份绑到夹具自己写出的那 12 字节 DOCX 上 —— 一次真实的干净交付本来就是这样。
    要构造两份不一致（Q2/Q3/Q5）用 `artifact_qa=`，要让"做数的那一份"读不出来用
    `artifact_qa_raw=`。

    Round 4.1 Final Gate §4（Blocker A）：产品页带同样是 Clean 的必要条件，所以夹具默认
    还写一份**实测在带内**的页数（286 页 / 250–320 档 = 一次 300 类干净交付该有的样子）。
    `pages=None` 表示"这份 Run 没有页数实测"，`projected_pages` 表示"只有投影、没有实测"
    （`measured:false`），两者都是反例要的形状，不是缺省。
    """
    states = dict(CLEAN_SECTIONS if sections is None else sections)
    run = root / "run"
    _write(run / "11_volume_generation_plan.json", VOLUMES)
    for sid in states:
        _write(run / "longform" / "word" / "sections" / sid / "status.json", {"status": states[sid]})
        _write(run / "longform" / "word" / "sections" / sid / "fragment.json",
               {"section_id": sid, "body_blocks": [{"type": "paragraph", "content": "技术实质内容"}]})
    _write(run / "content_unit_generation_audit.json", {
        "schema_version": "content-unit-generation-audit-v0.2",
        "persisted_content_unit_ids": list(units or [])})
    # 生产里这两份是同一张清单的两个层次：引擎先落 `longform/word/first_draft_artifact.json`，
    # app_core 再投影成根目录 `first_draft.json`。fixture 两份都写，才测得到"读哪个源"。
    _write(run / "longform" / "word" / "first_draft_artifact.json", {
        "schema_version": "first-draft-artifact-v0.1", "artifact_kind": "FIRST_DRAFT",
        "section_states": states,
        "artifact": {"sections": [{"section_id": s} for s in states]}})
    _write(run / "first_draft.json", {"status": "DOWNLOADABLE", "section_states": states})
    _write(run / "artifact_content.json", {"artifact": {"sections": [{"section_id": s} for s in states]}})
    if final_qa is not None:
        # 报告在场才算"最终 QA 量过了"。写 None 时**整份文件不存在**，那是要测的形状。
        _write(run / "artifact_qa_report.json",
               ({"status": final_qa, "findings": [], "summary": {}}
                if isinstance(final_qa, str) else dict(final_qa)))
    if final_qa_raw is not None:
        # 文件在，但内容读不出来 —— 与"报告缺席""状态字段为空"是三件不同的事（§5.4/§6.2）。
        (run / "artifact_qa_report.json").write_text(final_qa_raw, encoding="utf-8")
    if pages is not None or projected_pages is not None or page_targets:
        measured = pages is not None
        _write(run / "document_scale_report.json", {
            "page_targets": list(page_targets or []),
            "page_report": {"technical_effective_pages": (pages if measured
                                                          else projected_pages) or 0,
                            "measured": measured},
        })
        if measured:
            # 最终交付件真实分页是页数实测里最权威的那个源；缺了它就只剩初稿口径。
            _write(run / "artifact_pagination.json",
                   {"technical_effective_pages": pages,
                    "page_basis": "MEASURED_FINAL_RENDER"})
    if with_final:
        artifact = run / "artifact" / "final.docx"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"docx-bytes")
        # 真实交付件已在场 → 渲染后 QA 也必须在场（一次真实干净交付就是这么长的）。
        # 缺省跟随 `final_qa` 的状态，使两份报告在"合法夹具"里互相自洽；
        # 两份不一致的形状由 `artifact_qa=` 显式构造，不靠夹具默认值碰运气。
        status = artifact_qa
        if status is _UNSET:
            status = (final_qa if isinstance(final_qa, str)
                      else (final_qa or {}).get("status") if isinstance(final_qa, dict) else None)
        if status is not None:
            _write_final_artifact_qa(run, status=status, artifact=artifact)
        if artifact_qa_raw is not None:
            (run / FINAL_ARTIFACT_QA_FILE).write_text(artifact_qa_raw, encoding="utf-8")
    if render_docx:
        for volume in VOLUMES["volumes"]:
            docx = run / "volumes" / f"{volume['volume_id']}.docx"
            docx.parent.mkdir(parents=True, exist_ok=True)
            docx.write_bytes(b"x")
    return run


def _break_assembly_manifest(run: Path, section_states: dict[str, str]) -> None:
    """让装配输入真的缺：删掉引擎清单（权威源），再把 app_core 投影写成残缺版。

    只改投影造不出缺口 —— 引擎清单还在，状态端读到的仍是齐的那一份。
    """
    (run / "longform" / "word" / "first_draft_artifact.json").unlink()
    _write(run / "first_draft.json", {"status": "DOWNLOADABLE", "section_states": section_states})


class ContractIsDeclaredOnce(unittest.TestCase):
    def test_volume_docx_is_a_checkpoint_not_a_required_artifact(self) -> None:
        self.assertEqual("B_CHECKPOINT_ONLY", VOLUME_ARTIFACT_CONTRACT)
        facts = {}
        with tempfile.TemporaryDirectory() as tmp:
            facts = collect_facts(_make_run(Path(tmp), render_docx=False), medium="WORD")
        self.assertEqual(VOLUME_ARTIFACT_CONTRACT, facts["volume_artifact_contract"])
        self.assertFalse(facts["completed_volumes"], "一份分卷 DOCX 都没渲染 —— 这正是 B 要扛住的形状")

    def test_delivery_does_not_depend_on_rendered_volume_docx(self) -> None:
        """同一份内容，渲染 0 份与渲染 2/2 份分卷 DOCX，终态必须完全一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            no_docx = collect_facts(_make_run(root / "a", render_docx=False), medium="WORD")
            with_docx = collect_facts(_make_run(root / "b", render_docx=True), medium="WORD")
        derived_a, derived_b = derive_run_state(no_docx), derive_run_state(with_docx)
        self.assertEqual(derived_b["delivery_status"], derived_a["delivery_status"])
        self.assertEqual("COMPLETED_CLEAN", derived_a["delivery_status"])
        self.assertTrue(derived_a["volume_inputs_complete"])
        self.assertFalse(derived_a["volume_checkpoint_docx_done"], "磁盘读数照旧在场，只是不再决定终态")

    def test_state_rules_do_not_keep_an_a_contract_branch(self) -> None:
        """A 判据（expected ⊆ 磁盘 docx）不得再出现在终态里。"""
        source = (APP / "longform" / "run_state.py").read_text(encoding="utf-8")
        self.assertNotIn("STATE-04_final_implies_all_volumes", source)
        self.assertNotIn('"all_volumes_done"', source)
        self.assertIn("STATE-04c_final_implies_assembly_inputs_complete", source)

    def test_decision_record_lists_the_evidence_the_state_actually_reads(self) -> None:
        """决策记录里写的判据，必须就是 `run_state` 真正读的那几个 facts 键。

        这防的是又长出第二处"两处两把尺子"：契约说明写四条、终态实际读五条（或反过来）。
        """
        from longform.hierarchical import VOLUME_ARTIFACT_CONTRACT_RECORD
        state_source = (APP / "longform" / "run_state.py").read_text(encoding="utf-8")
        self.assertFalse(VOLUME_ARTIFACT_CONTRACT_RECORD["required_for_delivery"])
        for key in VOLUME_ARTIFACT_CONTRACT_RECORD["delivery_evidence"]:
            self.assertIn(f'facts.get("{key}")', state_source, key)
            self.assertIn(f'"{key}"', state_source, key)
        # 契约随进度文件出场：运营者看到 `volumes/` 少几份时不必自己猜哪一份该在
        self.assertIn("volume_artifact_contract()",
                      (APP / "longform" / "orchestrator.py").read_text(encoding="utf-8"))


class ProvisionalGuardSurvivesTheBDicision(unittest.TestCase):
    def test_provisional_section_blocks_clean_completion(self) -> None:
        """选 B 之后不能偷偷放宽：正文已到、深度待补仍不得 COMPLETED_CLEAN。"""
        with tempfile.TemporaryDirectory() as tmp:
            facts = collect_facts(
                _make_run(Path(tmp), sections={**CLEAN_SECTIONS, "S02-01": "COMPLETED_PROVISIONAL"}),
                medium="WORD")
        derived = derive_run_state(facts)
        self.assertEqual(["S02-01"], facts["provisional_sections"])
        self.assertEqual(1, derived["provisional_section_total"])
        self.assertEqual("NEEDS_REVIEW", derived["delivery_status"])

    def test_draftable_states_are_not_reimplemented_in_the_orchestrator(self) -> None:
        """分卷渲染端不得再抄一份 ok_statuses —— 那是 Run D 少渲染 5 份分卷的直接原因。"""
        source = (APP / "longform" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn('ok_statuses = {"COMPLETED", "COMPLETED_WITH_WARNING", "COMPLETED_CONDITIONAL"}', source)
        self.assertIn("SECTION_DRAFTABLE_STATES", source)
        self.assertTrue(SECTION_PASS_STATES < SECTION_DRAFTABLE_STATES,
                        "PROVISIONAL 必须是「内容已到」而不是「缺正文」，否则两处口径仍不一致")


class VolumeEvidenceBlocksCompletionWhenIncomplete(unittest.TestCase):
    def test_missing_assembly_input_is_not_a_clean_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp))
            _break_assembly_manifest(run, {k: v for k, v in CLEAN_SECTIONS.items() if k != "S02-01"})
            facts = collect_facts(run, medium="WORD")
        derived = derive_run_state(facts)
        self.assertEqual(["S02-01"], facts["assembly_input_gaps"])
        self.assertFalse(facts["assembly_inputs_complete"])
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])

    def test_missing_content_unit_persistence_is_visible_and_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            facts = collect_facts(_make_run(Path(tmp), units=["CU-001", "CU-002"]), medium="WORD")
        derived = derive_run_state(facts)
        self.assertEqual(["CU-003"], facts["volume_content_unit_gaps"])
        self.assertFalse(facts["volume_content_units_complete"])
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])

    def test_content_unit_audit_without_id_list_is_not_silently_passed(self) -> None:
        """审计只有计数没有清单 = 量不到，不得当成"齐"。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp))
            _write(run / "content_unit_generation_audit.json", {"persisted": 3})
            facts = collect_facts(run, medium="WORD")
        self.assertEqual("AUDIT_WITHOUT_ID_LIST", facts["volume_content_unit_audit_state"])
        self.assertFalse(facts["volume_content_units_complete"])
        self.assertNotEqual("COMPLETED_CLEAN", derive_run_state(facts)["delivery_status"])

    def test_section_listed_in_two_volumes_breaks_grouping_conservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp))
            plan = json.loads((run / "11_volume_generation_plan.json").read_text(encoding="utf-8"))
            plan["volumes"][1]["section_ids"] = ["S02-01", "S01-01"]  # 重复分组
            _write(run / "11_volume_generation_plan.json", plan)
            facts = collect_facts(run, medium="WORD")
        self.assertFalse(facts["volume_grouping_conservation_holds"])
        self.assertNotEqual("COMPLETED_CLEAN", derive_run_state(facts)["delivery_status"])


class AssemblyInputReadsTheEarliestAuthority(unittest.TestCase):
    """装配输入读哪一份：引擎自己落的第一稿清单是权威，根目录那份只是 app_core 投影。

    Round 4 的 E2E 就是这么翻车的：状态端只读投影，于是"没走 app_core"的链路被报成
    27 条装配缺口 —— 门没坏，是量门的尺读错了源。
    """

    def test_engine_manifest_alone_is_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), render_docx=False)
            (run / "first_draft.json").unlink()
            facts = collect_facts(run, medium="WORD")
        self.assertEqual("ENGINE_FIRST_DRAFT_MANIFEST", facts["assembly_input_source"])
        self.assertEqual("SECTION_STATES_PRESENT", facts["assembly_input_state"])
        self.assertEqual([], facts["assembly_input_gaps"])
        self.assertTrue(facts["assembly_inputs_complete"])
        self.assertEqual("COMPLETED_CLEAN", derive_run_state(facts)["delivery_status"])

    def test_no_manifest_is_unmeasured_not_a_fabricated_27_section_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), render_docx=False)
            (run / "first_draft.json").unlink()
            (run / "longform" / "word" / "first_draft_artifact.json").unlink()
            facts = collect_facts(run, medium="WORD")
        derived = derive_run_state(facts)
        self.assertEqual("NO_ASSEMBLY_INPUT_MANIFEST", facts["assembly_input_state"])
        self.assertEqual([], facts["assembly_input_gaps"], "读不到清单≠每条 Section 都缺")
        self.assertFalse(facts["assembly_inputs_complete"], "读不到也不能当成齐")
        self.assertNotEqual("COMPLETED_CLEAN", derived["delivery_status"])


class InvariantsCrossReadRealSources(unittest.TestCase):
    def _invariants(self, run: Path) -> dict[str, str]:
        facts = collect_facts(run, medium="WORD")
        state = {"facts": facts, "derived": derive_run_state(facts)}
        return evaluate_invariants(state, run)

    def test_all_four_checks_pass_with_zero_volume_docx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got = self._invariants(_make_run(Path(tmp), render_docx=False))
        for key in ("STATE-04a_final_implies_volume_sections_complete",
                    "STATE-04b_final_implies_content_units_complete",
                    "STATE-04c_final_implies_assembly_inputs_complete",
                    "STATE-04d_final_artifact_lineage_complete"):
            self.assertEqual("PASS", got[key], key)

    def test_each_check_fails_on_its_own_defect(self) -> None:
        cases = {
            "STATE-04a_final_implies_volume_sections_complete":
                lambda run: (run / "longform" / "word" / "sections" / "S02-01" / "fragment.json").unlink(),
            "STATE-04b_final_implies_content_units_complete":
                lambda run: _write(run / "content_unit_generation_audit.json",
                                   {"persisted_content_unit_ids": ["CU-001"]}),
            "STATE-04c_final_implies_assembly_inputs_complete":
                lambda run: _break_assembly_manifest(run, {"S01-01": "COMPLETED"}),
            "STATE-04d_final_artifact_lineage_complete":
                lambda run: (run / "artifact_content.json").unlink(),
        }
        for key, break_it in cases.items():
            with self.subTest(invariant=key), tempfile.TemporaryDirectory() as tmp:
                run = _make_run(Path(tmp), render_docx=False)
                break_it(run)
                got = self._invariants(run)
                self.assertEqual("FAIL", got[key], f"{key} 应被自己的缺陷抓到：{json.dumps(got, ensure_ascii=False)}")
                self.assertTrue(all(v != "FAIL" for k, v in got.items() if k != key),
                                f"{key} 之外的不变量不应被同一缺陷牵连：{got}")

    def test_invariants_are_not_applicable_for_a_run_without_word_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            pptx = run / "artifact" / "final.pptx"
            pptx.parent.mkdir(parents=True, exist_ok=True)
            pptx.write_bytes(b"pptx-bytes")
            facts = collect_facts(run, medium="PPT")
            self.assertFalse(facts["word_side_evidence_present"])
            got = evaluate_invariants({"facts": facts, "derived": derive_run_state(facts)}, run)
        for key in tuple(k for k in got if k.startswith("STATE-04")):
            self.assertEqual("NOT_APPLICABLE", got[key], key)


class BothContractShapesReplayedOneFormalContract(unittest.TestCase):
    """§69：合成用例两种契约形状都要跑，但代码里只能有一份现行正式契约。

    A 形状（分卷 DOCX 全渲染、每份可用）在旧口径下是唯一被判"完成"的形状；定 B 之后
    它必须与 B 形状（一份都没渲染）得出**完全相同**的终态，同时 A 的判据读数
    （渲染份数 / 覆盖到 expected 与否 / 落盘路径与体积）照样如实写进状态文件 ——
    将来若要回到 A，只需在 `derive_run_state` 里换用已经量出来的读数，不必重造一套状态机。
    """

    A_BEARING_KEYS = ("volume_checkpoint_docx_done", "volume_checkpoint_docx_total",
                      "volume_checkpoint_docx_expected_total")

    def _pair(self, root: Path) -> tuple[dict, dict, dict, dict]:
        a = collect_facts(_make_run(root / "a_shape", render_docx=True), medium="WORD")
        b = collect_facts(_make_run(root / "b_shape", render_docx=False), medium="WORD")
        return a, b, derive_run_state(a), derive_run_state(b)

    def test_a_shaped_and_b_shaped_runs_land_on_the_same_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fa, fb, da, db = self._pair(Path(tmp))
        # 终态三件套 + 交付判据必须逐字相同
        for key in ("internal_phase", "delivery_status", "public_phase",
                    "volume_inputs_complete", "page_band", "scoring_gate_state"):
            self.assertEqual(da[key], db[key], key)
        self.assertEqual("COMPLETED_CLEAN", da["delivery_status"], "两份契约形状都应干净完成")
        # A 判据在场且如实：全渲染时 done=True，一份没有时 done=False —— 只是读数不同
        self.assertTrue(da["volume_checkpoint_docx_done"])
        self.assertFalse(db["volume_checkpoint_docx_done"])
        self.assertEqual(2, da["volume_checkpoint_docx_total"])
        self.assertEqual(0, db["volume_checkpoint_docx_total"])
        self.assertEqual(2, fa["volume_plan_total"])
        # 除 A 读数外的每一条派生项都必须一致，否则"两种形状同判"只是挑了几个字段看
        for key in da:
            if key in self.A_BEARING_KEYS:
                continue
            self.assertEqual(da[key], db[key], f"派生项 {key} 因分卷 DOCX 渲染份数而改变")

    def test_checkpoint_files_are_still_verifiable_when_rendered(self) -> None:
        """B 不要求分卷 DOCX 在场，但在场的每一份仍是可用锚点：路径有效、体积非零。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = _make_run(Path(tmp), render_docx=True)
            facts = collect_facts(run, medium="WORD")
            for vid, path in (facts.get("artifact_paths") or {}).items():
                anchor = Path(path)
                self.assertTrue(anchor.is_file(), vid)
                self.assertGreater(anchor.stat().st_size, 0, vid)
        self.assertEqual({"V01", "V02"}, set(facts["completed_volumes"]))

    def test_only_one_formal_contract_is_declared_in_the_app(self) -> None:
        """契约声明处只能有一个：两份 A/B 常量并存 = 两处两把尺子重新长出来。"""
        declared = []
        for py in (APP / "longform").glob("*.py"):
            for line in py.read_text(encoding="utf-8").splitlines():
                if line.startswith("VOLUME_ARTIFACT_CONTRACT ="):
                    declared.append((py.name, line.strip()))
        self.assertEqual([("hierarchical.py", 'VOLUME_ARTIFACT_CONTRACT = "B_CHECKPOINT_ONLY"')],
                         declared, f"分卷契约声明不止一处：{declared}")
        # 另一份形状不得以常量形式活在代码里
        source = "".join((APP / "longform" / name).read_text(encoding="utf-8")
                         for name in ("run_state.py", "hierarchical.py", "orchestrator.py"))
        self.assertNotIn("A_REQUIRED_ARTIFACT", source)


class OfflineBoundary(unittest.TestCase):
    def test_state_module_does_not_touch_providers(self) -> None:
        source = (APP / "longform" / "run_state.py").read_text(encoding="utf-8")
        for banned in ("httpx", "requests", "providers.qwen", "providers.kimi", "openai"):
            self.assertNotIn(banned, source, banned)
        self.assertEqual(0, source.count("subprocess"), "终态对账不得启动渲染子进程")
