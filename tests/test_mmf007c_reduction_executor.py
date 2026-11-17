"""MMF-007C Round 4 §16–§21：超页定向削减的**执行**与双边页门（全部离线）。

Round 3 的复判结论是 "Reduction 只有计划、没有执行"，本文件逐条钉住补上的那一半：

- 削减改的是磁盘上的 Content Unit 与它的 `fragment.json`，交付件真的变短（REDUCE-01）；
- 保护类、MUST 承诺句、结构几何下限三层护栏不得被"削到达标"这句话冲掉（REDUCE-02/03/04）；
- MUST 承诺在正文侧与结构侧同一口径：清单项、表格行整条删除也不许带走承诺
  （REDUCE-11，来自 Run D 真实 62 条动作的执行器回放）；
- 出处 id 找不回正文就不许重建：整体替换 `body_blocks` 那条路必须有不抹正文的护栏
  （REDUCE-12，同一份回放里 27 节 join 全部落空、S02-02 正文 8,600 → 0）；
- 结构槽位认不出出处时，只有"整槽都属于 CU"才敢重装，否则不重建
  （REDUCE-13，同一份回放里整份 Run D 的表格 166 → 278：削减后反而更长）；
- 落盘前逐节量渲染字数：重建会让这一节变长就停手（REDUCE-14，同一份回放里 S02-02 8,600 → 8,620）；
- 账本与削减同一条货币、同一个数（REDUCE-05）；
- 未生成的单元只压额度、不编正文（REDUCE-06）；
- 重建不得把表格加第二遍（REDUCE-07，Round 3 之后新发现的装配缺陷）；
- 全程零 Provider 调用、零整篇 Compression Pipeline（每条 report 断言都点名）；
- §21 双边门：>320 页的交付件在唯一终态权威里不可能是 COMPLETED_CLEAN，
  <250 页必须继续被读成 UNDER_LENGTH 而不是"投影合格就放行"（REDUCE-09/10）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

APP_ROOT = Path(__file__).resolve().parents[1]
APP = APP_ROOT / "app"
for _path in (str(APP), str(APP_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from longform.cu_assembly import apply_content_units
from longform.orchestrator import evaluate_page_overshoot, execute_overshoot_reduction
from longform.reduction import (
    EXECUTION_FILE,
    MIN_UNITS,
    SKIPPED_NOT_GENERATED,
    SKIPPED_PROTECTED,
    condense_prose,
    execute_reduction_plan,
    rebuild_section_fragment,
    shrink_structure,
)
from longform.run_state import collect_facts, derive_run_state
from test_mmf007c_r4_final_state_contract import _write_final_artifact_qa  # 同一份"最终 QA 怎么写"的写法
from planning.budget_control import (
    ARRIVED_ACTION,
    BUDGET_LEDGER_FILE,
    PENDING_ACTION,
    judge_content_unit,
    merge_ledger,
    targeted_reduction_plan,
    visible_chars_of,
    write_ledger,
)
from planning.unified_budget import classify_page_state

# ---------------------------------------------------------------- Synthetic 素材
# 3 条真正落得下现场的技术句（HIGH_VALUE）+ 一长串"任何项目都能照抄"的套话（GENERIC_PADDING）。
# 削减只能动后者，所以这两类必须在同一条 CU 里各自可辨 —— 这也是 §45 的度量能成立的前提。
_TECH = (
    "电梯机房温度控制在35℃以内，超过即启动排风并当日登记测点读数。",
    "地下车库排水泵每周试运转一次，形成巡检单由带班人签字留痕。",
    "秩序维护岗每2小时巡更一次，巡更点位覆盖出入口、通道与机房。",
)
_PADDING = (
    "项目部高度重视，积极协调各方资源。",
    "严格遵守相关法律法规，确保服务质量。",
    "我们不断总结经验，持续改进工作方式方法。",
    "相关人员认真组织学习，全面提高整体水平。",
    "该项工作思路积极稳妥，各方配合良好。",
    "项目部认真落实各项要求，积极营造和谐氛围。",
)


def _prose(*, tech: int = 3, padding: int = 6) -> str:
    return "".join(list(_TECH)[:tech] + [_PADDING[index % len(_PADDING)]
                                        for index in range(padding)])


def _contract(cu_id: str, sid: str, *, minimum: int, target: int, maximum: int,
              archetype: str = "NARRATIVE", **extra: Any) -> dict[str, Any]:
    return {
        "content_unit_id": cu_id, "parent_section_id": sid, "section_id": sid,
        "archetype": archetype,
        "min_budget": {"effective_chars": minimum},
        "target_budget": {"effective_chars": target},
        "max_budget": {"effective_chars": maximum},
        **extra,
    }


def _cu_row(cu_id: str, sid: str, *, archetype: str, content: str,
            payload: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "content_unit_id": cu_id, "parent_section_id": sid, "section_id": sid,
        "archetype": archetype, "content": content, "payload": payload,
        "generation_origin": "provider_content_unit",
        "effective_chars": visible_chars_of(content) + visible_chars_of(payload),
    }


def _table_payload(rows: int) -> dict[str, Any]:
    return {
        "title": "设备设施巡检频次与记录表",
        "headers": ["部位", "频次", "记录载体", "责任岗位"],
        "rows": [[f"{index}号楼屋面", f"每日{index}次", "巡检单", "工程岗"] for index in range(1, rows + 1)],
    }


@contextmanager
def _run_dir() -> Iterator[Path]:
    """沿用本仓库既有用例的 tempfile 口径：pytest 的 tmp_path 依赖共享 Temp 目录，权限不稳。"""
    with tempfile.TemporaryDirectory() as raw:
        yield Path(raw)


def _write_section(run_dir: Path, sid: str, rows: list[dict[str, Any]], *,
                   base_tables: list[dict[str, Any]] | None = None) -> Path:
    """按真实磁盘形状落一节：CU 文件 + 由 CU 装配出来的 fragment.json（与 factory 同一条路）。"""
    section = run_dir / "longform" / "word" / "sections" / sid
    cus = section / "cus"
    cus.mkdir(parents=True, exist_ok=True)
    for row in rows:
        (cus / f"{row['content_unit_id']}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    fragment = {
        "section_id": sid, "title": "设备设施运行维护方案", "layout": "modules",
        "body_blocks": [], "tables": list(base_tables or []), "processes": [],
    }
    fragment = apply_content_units(rows, base_fragment=fragment, section_id=sid,
                                   planned_ids=[row["content_unit_id"] for row in rows])
    (section / "fragment.json").write_text(json.dumps(fragment, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    return section


def _write_ledger(run_dir: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = merge_ledger({"records": []}, records)
    write_ledger(run_dir / BUDGET_LEDGER_FILE, ledger)
    return ledger


class _StubFactory:
    """只带执行器真正用到的两样东西：CU 契约表与下一批额度上限。"""

    def __init__(self, contracts: dict[str, Any]) -> None:
        self.cu_contracts = contracts
        self.budget_unit_caps: dict[str, int] = {}


def _fragment_prose_chars(section: Path) -> int:
    fragment = json.loads((section / "fragment.json").read_text(encoding="utf-8"))
    return sum(visible_chars_of(block.get("content")) for block in fragment.get("body_blocks") or [])


# 一条 CU 的额度画像：正文（3 技术句 91 字 + 6 套话 114 字）实测 205 字，
# 额度上限卡在 95 —— 只有把套话全删掉才可能落回额度内，正好用来验"削减不伤技术实质"。
_BUDGET = {"minimum": 80, "target": 90, "maximum": 95}


def _cu_a_contract(cu_id: str, sid: str, **extra: Any) -> dict[str, Any]:
    return _contract(cu_id, sid, archetype="NARRATIVE", **_BUDGET, **extra)


# ---------------------------------------------------------------- §16–§20 REDUCE-01
def test_reduce_01_arrived_units_are_actually_shrunk_on_disk():
    """REDUCE-01：计划点名已生成单元后，CU 文件与交付节正文真的变短，且只掉低增量句。"""
    sid = "S04-01"
    cu_a, cu_b = "CU-0001", "CU-0002"
    with _run_dir() as run:
        contracts = {cu_a: _cu_a_contract(cu_a, sid), cu_b: _cu_a_contract(cu_b, sid)}
        shapes = ((cu_a, 3, 6), (cu_b, 2, 5))
        rows = [_cu_row(cu, sid, archetype="NARRATIVE", content=_prose(tech=tech, padding=padding),
                        payload=None) for cu, tech, padding in shapes]
        section = _write_section(run, sid, rows)
        ledger = _write_ledger(run, [
            judge_content_unit(contracts[cu], content={"content": row["content"], "payload": None})
            for cu, row in zip([cu_a, cu_b], rows)])
        before_prose = _fragment_prose_chars(section)
        assert ledger["totals"]["over_budget_units"] == 2

        plan = targeted_reduction_plan(ledger=ledger, content_units=list(contracts.values()),
                                        page_targets=(250, 320), ruler=None,
                                        measured_technical_pages=321)
        assert [row["action"] for row in plan["actions"]] == [ARRIVED_ACTION, ARRIVED_ACTION]
        # 候选不足就是不足：计划必须自己承认覆盖不了缺口，而不是报一句"已安排削减"。
        assert plan["status"] == "REDUCTION_INSUFFICIENT"
        assert plan["uncovered_chars"] > 0

        report = execute_reduction_plan(run, plan, contracts=contracts)

        assert report["status"] == "EXECUTED"
        assert report["actions_executed"] == 2
        # §1 与 §17：执行器一律本地完成，两句话都得能被机器核对。
        assert report["provider_called"] is False
        assert report["compression_pipeline_called"] is False
        assert report["granularity"] == "SECTION_AND_CONTENT_UNIT"
        assert report["policy"] == "LOCAL_ONLY_NO_PROVIDER_NO_FULL_DOCUMENT_COMPRESSION"
        assert report["residual_excess_ledger_chars"] == (
            report["excess_ledger_chars"] - report["ledger_chars_removed"])

        for (cu, tech, _padding), row in zip(shapes, rows):
            after = json.loads((section / "cus" / f"{cu}.json").read_text(encoding="utf-8"))
            assert after["effective_chars"] <= _BUDGET["maximum"]
            assert after["effective_chars"] < visible_chars_of(row["content"])
            for sentence in list(_TECH)[:tech]:
                assert sentence in after["content"], f"{cu} 的技术句被删掉了"
            # 削的是低增量那头，但一到额度就停：§22 的优先级不是"能删多少删多少"。
            assert sum(after["content"].count(s) for s in _PADDING) < \
                sum(row["content"].count(s) for s in _PADDING)
            assert after["reduction"]["prose_removed"] > 0
            # 原文先落快照：削减不是永久删除，出问题要能一句话读回改前长什么样。
            snapshot = section / "reduction" / f"{cu}.json"
            assert snapshot.is_file()
            assert _PADDING[0] in snapshot.read_text(encoding="utf-8")
            assert after["reduction"]["removed_chars"] > 0
            assert after["budget"]["budget_status"] != "OVER_BUDGET"

        assert _fragment_prose_chars(section) < before_prose
        assert report["sections_rebuilt"][sid]["rebuilt"] is True
        persisted = json.loads((run / EXECUTION_FILE).read_text(encoding="utf-8"))
        assert persisted["ledger_chars_removed"] == report["ledger_chars_removed"]


# ---------------------------------------------------------------- §22 REDUCE-02
def test_reduce_02_protected_units_are_skipped_and_left_untouched():
    """REDUCE-02：MUST 应答 / 评分应答 / SOP / 技术表格既不被删，也不被"改一句"。"""
    sid = "S04-02"
    scored, sop = "CU-0001", "CU-0002"
    with _run_dir() as run:
        contracts = {
            scored: _cu_a_contract(scored, sid, source_scoring_items=["SC-03"]),
            sop: _contract(sop, sid, archetype="SOP", **_BUDGET),
        }
        rows = [
            _cu_row(scored, sid, archetype="NARRATIVE", content=_prose(), payload=None),
            _cu_row(sop, sid, archetype="SOP", content=_prose(),
                    payload={"steps": [f"第{index}步：接到报修后15分钟内到场处置" for index in range(1, 9)]}),
        ]
        section = _write_section(run, sid, rows)
        before = {cu: (section / "cus" / f"{cu}.json").read_text(encoding="utf-8")
                  for cu in (scored, sop)}
        # 保护判据来自计划自己：执行器不重新发明一遍"什么不能动"。
        records = [
            judge_content_unit(contracts[scored], content={"content": rows[0]["content"], "payload": None}),
            judge_content_unit(contracts[sop], content={"content": rows[1]["content"],
                                                        "payload": rows[1]["payload"]}),
        ]
        plan = targeted_reduction_plan(ledger=_write_ledger(run, records),
                                       content_units=list(contracts.values()),
                                       page_targets=(250, 320), ruler=None,
                                       measured_technical_pages=395)
        assert plan["actions"] == []
        assert plan["protected_units_skipped"] == 2

        forced = {"actions": [
            {"content_unit_id": scored, "section_id": sid, "action": ARRIVED_ACTION, "protected": True},
            {"content_unit_id": sop, "section_id": sid, "action": ARRIVED_ACTION, "protected": True}],
            "excess_ledger_chars": 9000, "trigger": "手动构造：绕过计划直接投喂保护单元"}
        report = execute_reduction_plan(run, forced, contracts=contracts)
        assert report["actions_skipped_protected"] == 2
        assert report["actions_executed"] == 0
        assert report["status"] == "NOTHING_REDUCIBLE"
        for cu in (scored, sop):
            assert (section / "cus" / f"{cu}.json").read_text(encoding="utf-8") == before[cu]
        assert not (section / "reduction").exists()


# ---------------------------------------------------------------- §22 REDUCE-03
def test_reduce_03_must_marker_sentences_survive_even_under_pressure():
    """REDUCE-03：含"严禁/不低于/按时限"的句子即便落在可删档里也永不删，压不到额度就如实报残余。"""
    text = ("相关人员认真组织学习，全面提高整体水平。"
            "秩序维护作业标准不低于合同约定，按时限完成整改。"
            "严禁脱岗空岗与代签。"
            "该项工作思路积极稳妥，各方配合良好。"
            "我们不断总结经验，持续改进工作方式方法。")
    row = condense_prose(text, keep_to_chars=0)
    assert "严禁脱岗空岗与代签。" in row["text"]
    assert "不低于" in row["text"] and "按时限" in row["text"]
    assert "相关人员认真组织学习" not in row["text"]
    assert row["protected_sentences_kept"] >= 1
    # 残余缺口必须写出来：压不下去就说压不下去，不是把承诺删了来"达标"。
    assert row["residual_over_chars"] > 0


# ---------------------------------------------------------------- §12 REDUCE-04
def test_reduce_04_structure_shrink_respects_the_geometry_floor():
    """REDUCE-04：表格按行削，但绝不下破结构几何下限（表 3 / 步 4 / 项 5 / 栏 5）。"""
    payload = _table_payload(10)
    row = shrink_structure(payload, archetype="STANDARD_TABLE", keep_to_chars=0)
    assert row["units_before"] == 10
    assert row["units_after"] == MIN_UNITS["table"]
    assert len(row["payload"]["rows"]) == MIN_UNITS["table"]
    assert row["residual_over_chars"] > 0

    steps = {"steps": [f"第{index}步：接到报修后15分钟内到场" for index in range(1, 9)]}
    process = shrink_structure(steps, archetype="SOP", keep_to_chars=0)
    assert process["units_after"] == MIN_UNITS["step"]

    # 额度内不达标就不动：削减不是"能删就删"。
    left_alone = shrink_structure(_table_payload(6), archetype="STANDARD_TABLE", keep_to_chars=9999)
    assert left_alone["chars_removed"] == 0 and left_alone["units_after"] == 6


def test_reduce_05_ledger_and_reduction_report_the_same_char_count():
    """REDUCE-05：账本重记与执行报告是同一个数 —— 不出现"账上仍超写、正文已削完"。"""
    sid = "S04-03"
    cu = "CU-0001"
    with _run_dir() as run:
        contract = _cu_a_contract(cu, sid)
        row = _cu_row(cu, sid, archetype="NARRATIVE", content=_prose(tech=3, padding=6), payload=None)
        section = _write_section(run, sid, [row])
        ledger = _write_ledger(run, [judge_content_unit(contract, content={"content": row["content"],
                                                                          "payload": None})])
        before = ledger["totals"]["actual_chars"]
        plan = targeted_reduction_plan(ledger=ledger, content_units=[contract], page_targets=(250, 320),
                                       ruler=None, measured_technical_pages=395)
        report = execute_reduction_plan(run, plan, contracts={cu: contract})

        assert report["ledger_chars_removed"] > 0
        assert report["budget_ledger_after"]["actual_chars"] == before - report["ledger_chars_removed"]
        assert report["budget_ledger_after"]["over_budget_units"] == 0
        assert report["budget_ledger_before"]["over_budget_units"] == 1
        # §7 一种货币：折算到产物可见字用同一个系数，不再各写一把尺子。
        assert report["artifact_chars_removed_estimate"] == round(
            report["ledger_chars_removed"] * report["artifact_char_yield"])
        residual = report["residual_excess_ledger_chars"]
        assert residual == max(0, report["excess_ledger_chars"] - report["ledger_chars_removed"])
        stored = json.loads((section / "cus" / f"{cu}.json").read_text(encoding="utf-8"))
        assert visible_chars_of(stored["content"]) < visible_chars_of(row["content"])
        assert stored["budget"]["actual_chars"] == visible_chars_of(stored["content"])


def test_reduce_06_pending_units_only_get_a_tighter_cap():
    """REDUCE-06：未生成的单元没有正文可削，真实动作是压下一批的每单元额度上限。"""
    sid = "S04-04"
    arrived, pending = "CU-0001", "CU-0002"
    with _run_dir() as run:
        contracts = {
            arrived: _cu_a_contract(arrived, sid),
            pending: _contract(pending, sid, minimum=240, target=520, maximum=640),
        }
        row = _cu_row(arrived, sid, archetype="NARRATIVE", content=_prose(), payload=None)
        section = _write_section(run, sid, [row])
        ledger = _write_ledger(run, [judge_content_unit(contracts[arrived],
                                                        content={"content": row["content"], "payload": None})])
        plan = targeted_reduction_plan(ledger=ledger, content_units=list(contracts.values()),
                                       page_targets=(250, 320), ruler=None,
                                       measured_technical_pages=395)
        actions = {str(item["content_unit_id"]): str(item["action"]) for item in plan["actions"]}
        assert actions[pending] == PENDING_ACTION

        report = execute_reduction_plan(run, plan, contracts=contracts)
        pending_row = next(item for item in report["actions"] if item["content_unit_id"] == pending)
        assert pending_row["execution_status"] == SKIPPED_NOT_GENERATED
        assert pending_row["pending_target_chars_cap"] == 240
        assert report["pending_section_caps"] == {sid: 240}
        # 没有编造正文：未生成的 CU 到执行时依然不落盘。
        assert not (section / "cus" / f"{pending}.json").exists()
        assert report["actions_skipped_other"] >= 1


def test_reduce_07_rebuild_never_stacks_a_second_copy_of_cu_tables():
    """REDUCE-07：重建剥掉上一次 CU 追加的表格/流程，装配两次也不会多一张表。"""
    sid = "S04-05"
    cu = "CU-0002"
    base_table = {"title": "章节自带表", "headers": ["项", "值"], "rows": [["甲", "乙"], ["丙", "丁"], ["戊", "己"]]}
    with _run_dir() as run:
        rows = [
            _cu_row("CU-0001", sid, archetype="NARRATIVE", content=_prose(tech=3, padding=2), payload=None),
            _cu_row(cu, sid, archetype="STANDARD_TABLE", content="", payload=_table_payload(8)),
        ]
        section = _write_section(run, sid, rows, base_tables=[base_table])
        first = json.loads((section / "fragment.json").read_text(encoding="utf-8"))
        assert len(first["tables"]) == 2  # 1 张本节自带 + 1 张 CU 表格

        (section / "cus" / f"{cu}.json").write_text(json.dumps({
            **rows[1], "payload": _table_payload(5)}, ensure_ascii=False), encoding="utf-8")
        first_rebuild = rebuild_section_fragment(section)
        second_rebuild = rebuild_section_fragment(section)
        fragment = json.loads((section / "fragment.json").read_text(encoding="utf-8"))
        assert first_rebuild["rebuilt"] is True and second_rebuild["rebuilt"] is True
        assert len(fragment["tables"]) == 2
        assert fragment["tables"][0]["title"] == base_table["title"]
        assert second_rebuild["cu_structures_stripped"]["tables"] == 1
        assert fragment["content_unit_execution"]["planned_content_unit_ids"] == ["CU-0001", cu]


def test_reduce_08_reduction_survives_the_local_text_pipeline():
    """REDUCE-08：重建走的是提交时同一条本地治理链，装配出处不能被治理洗掉。"""
    sid = "S04-06"
    cu = "CU-0001"
    with _run_dir() as run:
        contract = _cu_a_contract(cu, sid)
        row = _cu_row(cu, sid, archetype="NARRATIVE", content=_prose(), payload=None)
        section = _write_section(run, sid, [row])
        ledger = _write_ledger(run, [judge_content_unit(contract, content={"content": row["content"],
                                                                          "payload": None})])
        plan = targeted_reduction_plan(ledger=ledger, content_units=[contract], page_targets=(250, 320),
                                       ruler=None, measured_technical_pages=395)

        def normalize(fragment: dict[str, Any]) -> dict[str, Any]:
            marker = deepcopy(fragment)
            marker["harmless_marker"] = "LOCAL_PIPELINE_RAN"
            return marker

        report = execute_reduction_plan(run, plan, contracts={cu: contract}, normalize=normalize)
        assert report["local_text_pipeline_applied"] is True
        fragment = json.loads((section / "fragment.json").read_text(encoding="utf-8"))
        assert fragment["harmless_marker"] == "LOCAL_PIPELINE_RAN"
        # 治理链跑完还得认得"这一节是 CU 粒度装配出来的"，否则下一次重建直接罢工。
        assert fragment["content_unit_execution"]["planned_content_unit_ids"] == [cu]
        assert fragment["reduction_rebuild"]["normalized_after_rebuild"] is True


def test_reduce_09_gate_and_execution_share_the_two_sided_band():
    """REDUCE-09：三态只有一把尺子 —— 超上限进削减，低于下沿仍报 UNDER_LENGTH 不冒充合格。"""
    assert classify_page_state(395, [250, 320]) == "OVERSHOOT"
    assert classify_page_state(300, [250, 320]) == "TARGET_RANGE"
    assert classify_page_state(243, [250, 320]) == "UNDER_LENGTH"

    sid = "S04-07"
    cu = "CU-0001"
    with _run_dir() as run:
        contract = _cu_a_contract(cu, sid)
        row = _cu_row(cu, sid, archetype="NARRATIVE", content=_prose(), payload=None)
        _write_section(run, sid, [row])
        _write_ledger(run, [judge_content_unit(contract, content={"content": row["content"],
                                                                  "payload": None})])
        # 一条还没生成的 CU：超页时的真实动作是把下一批的额度压到契约下限（§14 的杠杆）。
        contracts = {cu: contract,
                     "CU-0002": _contract("CU-0002", sid, minimum=240, target=520, maximum=640)}
        overshoot = evaluate_page_overshoot(
            run, {"technical_effective_pages": 395, "rendered_pages": 404,
                  "page_basis": "MEASURED", "page_ruler": {"chars_per_technical_page": 900.0}},
            page_targets=[250, 320], contracts=contracts)
        assert overshoot["status"] == "OVERSHOOT_REDUCTION"
        assert overshoot["page_band"] == "OVERSHOOT"
        assert overshoot["depth_supplement_forbidden"] is True

        factory = _StubFactory(contracts)
        report = execute_overshoot_reduction(run, overshoot, factory)
        assert report["status"] == "EXECUTED"
        assert report["page_band_before"] == "OVERSHOOT"
        assert report["provider_called"] is False
        assert report["compression_pipeline_called"] is False
        assert report["pending_section_caps"] == {sid: 240}
        assert factory.budget_unit_caps == {sid: 240}
        assert overshoot["reduction_execution_status"] == "EXECUTED"
        projection = report["page_projection"]
        assert projection["measured_before_reduction_pages"] == 395
        # 削减后的页数是"实测减本地削掉量"的投影，报告必须自己说清 basis，不得冒充再量一次。
        assert projection["basis"] == "MEASURED_RENDER_MINUS_LOCAL_REDUCTION_PROJECTION"
        # 一条 CU 削掉百来个字修不了超 75 页：缺口没覆盖完就要照实写，
        # 页带也不会因为"执行过削减"就假装回到了区间内。
        assert projection["projected_pages_after_reduction"] <= 395
        assert report["coverage_of_excess"] < 1.0
        assert report["residual_excess_ledger_chars"] > 0
        assert report["page_band_after"] == "OVERSHOOT"
        assert overshoot["page_band_after_reduction"] == "OVERSHOOT"

        # 没有超页就没有执行：不得凭空造出一份"我们已经削减过"的记录。
        quiet = evaluate_page_overshoot(
            run, {"technical_effective_pages": 243, "rendered_pages": 250,
                  "page_basis": "MEASURED", "page_ruler": {"chars_per_technical_page": 900.0}},
            page_targets=[250, 320], contracts={})
        assert quiet["status"] == "UNDER_FLOOR_REBALANCED"
        assert quiet["page_band"] == "UNDER_LENGTH"
        factory.cu_contracts = {}
        assert execute_overshoot_reduction(run, quiet, factory)["status"] == "NOT_REQUIRED"


# ---------------------------------------------------------------- §16 §21 REDUCE-10
def _delivery_run(run: Path, *, pages: int | None) -> None:
    """造一份"其它条件全部干净"的交付现场，只让页数口径变化 —— 断言才归给页门。

    §44–§49 定 B 之后，"干净"的门槛提高了：终态先看四条**内容**证据
    （分卷 Section 正文齐 / CU 全部落盘 / 装配输入清单在场 / 最终件谱系成对），
    少写任何一条，终态就先被 B 门挡住，页门与评分门的读数再也归不到因头上。
    分卷 DOCX 反过来不再是必需（它只是 Resume 锚点），这里仍留一份，让 A 读数照旧在场。
    """
    (run / "artifact").mkdir(parents=True, exist_ok=True)
    (run / "artifact" / "final.docx").write_bytes(b"docx-bytes")
    (run / "volumes").mkdir(parents=True, exist_ok=True)
    (run / "volumes" / "VOL-01.docx").write_bytes(b"volume-bytes")
    (run / "11_volume_generation_plan.json").write_text(json.dumps(
        {"volumes": [{"volume_id": "VOL-01", "generation_status": "COMPLETED",
                      "section_ids": ["S01-01"], "content_unit_ids": ["CU-S01-01-01"]}]}),
        encoding="utf-8")
    section = run / "longform" / "word" / "sections" / "S01-01"
    section.mkdir(parents=True, exist_ok=True)
    (section / "status.json").write_text(json.dumps({"status": "COMPLETED"}), encoding="utf-8")
    (section / "fragment.json").write_text(json.dumps(
        {"section_id": "S01-01", "body_blocks": [{"type": "paragraph", "content": "技术实质内容"}]}),
        encoding="utf-8")
    (run / "content_unit_generation_audit.json").write_text(json.dumps(
        {"persisted_content_unit_ids": ["CU-S01-01-01"]}), encoding="utf-8")
    (run / "longform" / "word" / "first_draft_artifact.json").write_text(json.dumps(
        {"schema_version": "first-draft-artifact-v0.1",
         "section_states": {"S01-01": "COMPLETED"}}), encoding="utf-8")
    (run / "artifact_content.json").write_text(json.dumps(
        {"artifact": {"sections": [{"section_id": "S01-01"}]}}), encoding="utf-8")
    (run / "artifact_qa_report.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    # Round 4.7：交付件 `artifact/final.docx` 已在场 ⇒ 终态的 QA 凭据换成"对那份 DOCX 量的"
    # `final_artifact_qa.json`。这份夹具的全部用途是"其它条件都干净，只动页门那一根轴"，
    # 所以它必须自带一份**绑定到当前交付件**的合格最终 QA —— 缺了它，页门读数就再也
    # 归不到因头上（REDUCE-10 的 300 页正例第一个被挡）。写法人复用共享夹具那一位，
    # 不在测试里长出第二套身份算法。
    _write_final_artifact_qa(run, status="PASS", artifact=run / "artifact" / "final.docx")
    (run / "document_scale_report.json").write_text(json.dumps({
        "page_targets": [250, 320],
        "page_band": "OVERSHOOT" if (pages or 0) > 320 else "NOT_MEASURED",
        "page_report": {"technical_effective_pages": pages or 0, "measured": bool(pages)},
    }), encoding="utf-8")
    if pages:
        (run / "artifact_pagination.json").write_text(json.dumps(
            {"status": "REFRESHED", "technical_effective_pages": pages}), encoding="utf-8")


def test_reduce_11_must_markers_protect_structure_units_too():
    """REDUCE-11：MUST 承诺不能只在内文侧受保护 —— 清单项/表格行整条删除同样不许带走承诺。

    这条用例来自 Run D 真实 62 条削减动作的执行器回放：`condense_prose` 有句级 MUST 保护，
    `shrink_structure` 只看信息增量与几何下限，于是 CHECKLIST 的 `items` 里 5 条
    「不得/不少于/必须」承诺行被整行删掉，违反计划自己声明的
    `NEVER_DELETE_SCORING_RESPONSE_MUST_REQUIREMENT_...` 政策。
    """
    protected = ["甲方交办事项不得只在本方内部闭环，回告形式与对象均有记录。",
                 "问题清单按编号顺抽不少于三分之一进行当日复核。"]
    padding = ["项目部高度重视，积极协调各方资源。", "我们不断总结经验，持续改进工作方式方法。",
               "该项工作思路积极稳妥，各方配合良好。", "相关人员认真组织学习，全面提高整体水平。",
               "严格遵守相关法律法规，确保服务质量。", "本项目将不断完善管理体系，提升满意度。",
               "各方一致认为该做法值得推广。", "总体运行平稳，未见异常情况。"]
    row = shrink_structure({"title": "交办事项回告核对清单", "items": protected + padding},
                           archetype="CHECKLIST", keep_to_chars=0)
    kept_text = " ".join(str(item) for item in row["payload"]["items"])
    assert "不得只在本方内部闭环" in kept_text
    assert "不少于三分之一" in kept_text
    assert row["must_protected_units"] == 2
    # 只删得动低增量那头，删到几何下限就停；停在这里就如实报残余，不拿承诺凑数。
    assert row["units_after"] == MIN_UNITS["item"]
    assert row["residual_over_chars"] > 0

    # 落盘一条真形状的 CU：执行器削减后，承诺项必须还在磁盘上的 CU 与重建的 fragment 里。
    sid, cu = "S02-02", "CU-S02-02-02"
    payload = {"title": "交办事项回告核对清单", "items": protected + padding}
    contract = _contract(cu, sid, archetype="CHECKLIST", minimum=40, target=50, maximum=60)
    with _run_dir() as run:
        body = "秩序维护岗每2小时巡更一次，巡更点位覆盖出入口、通道与机房。"
        section = _write_section(run, sid, [_cu_row(cu, sid, archetype="CHECKLIST",
                                                    content=body, payload=payload)])
        ledger = _write_ledger(run, [judge_content_unit(
            contract, content={"content": body, "payload": payload})])
        plan = targeted_reduction_plan(ledger=ledger, content_units=[contract],
                                       page_targets=(250, 320), ruler=None,
                                       measured_technical_pages=395)
        report = execute_reduction_plan(run, plan, contracts={cu: contract})
        assert report["provider_called"] is False
        assert report["actions"], "回放前提：这条超写 CU 必须被计划点名"
        on_disk = json.loads((section / "cus" / f"{cu}.json").read_text(encoding="utf-8"))
        disk_text = " ".join(str(item) for item in (on_disk.get("payload") or {}).get("items") or [])
        assert "不得只在本方内部闭环" in disk_text
        assert "不少于三分之一" in disk_text
        fragment = json.loads((section / "fragment.json").read_text(encoding="utf-8"))
        assert "不得只在本方内部闭环" in json.dumps(fragment, ensure_ascii=False)
        # 账与正文同一条尺：削减后重记的字数不能小于留在盘上的承诺行。
        after = report["budget_ledger_after"]["actual_chars"]
        assert after <= report["budget_ledger_before"]["actual_chars"]


def _run_d_shaped_section(run: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """落一节"Run D 形状"的现场：磁盘 CU 用位置 id，fragment 的出处 id 用中文归类词。"""
    sid = "S02-02"
    cus = [f"CU-{sid}-0{index}" for index in (1, 2, 3)]
    contracts = {cus[0]: _contract(cus[0], sid, minimum=80, target=90, maximum=95),
                 cus[1]: _contract(cus[1], sid, minimum=80, target=90, maximum=9999),
                 cus[2]: _contract(cus[2], sid, minimum=80, target=90, maximum=9999)}
    rows = [_cu_row(cu, sid, archetype="NARRATIVE", content=_prose(), payload=None) for cu in cus]
    section = _write_section(run, sid, rows)
    ledger = _write_ledger(run, [judge_content_unit(
        contracts[cu], content={"content": _prose(), "payload": None}) for cu in cus])
    plan = targeted_reduction_plan(ledger=ledger, content_units=[contracts[cu] for cu in cus],
                                   page_targets=(250, 320), ruler=None,
                                   measured_technical_pages=395)
    labeled = [f"CU-相关专项安排-{index:02d}" for index in range(1, len(cus) + 1)]
    path = section / "fragment.json"
    fragment = json.loads(path.read_text(encoding="utf-8"))
    fragment["content_unit_execution"]["planned_content_unit_ids"] = labeled
    fragment["content_unit_execution"]["assembled_content_unit_ids"] = labeled
    path.write_text(json.dumps(fragment, ensure_ascii=False), encoding="utf-8")
    return section, contracts, ledger, plan


def test_reduce_12_rebuild_never_trades_section_prose_for_pages():
    """REDUCE-12：出处 id 找不回正文时重建必须停手 —— 不许拿整节正文去换页数。

    这条用例来自 Run D 真实 62 条动作的执行器回放：Round 3 的卫生治理把 `fragment.json` 里的
    `planned_content_unit_ids` 写成中文归类词（`CU-相关专项安排-01`），磁盘 CU 文件仍是位置 id
    （`CU-S02-02-01`），按 id join 得到 0 条，而 `apply_content_units` 是**整体替换**
    `body_blocks`，于是 S02-02 的 8,600 可见字在重建后变成 0 —— 账本与 CU 侧一切正常，
    只有交付件里的整节正文没了，27 节无一例外。当前构建不再产生这种形状，但执行器面对
    历史件必须"找不回正文就不重建"。
    """
    sid = "S02-02"
    with _run_dir() as run:
        section, contracts, ledger, plan = _run_d_shaped_section(run)
        before = _fragment_prose_chars(section)
        report = execute_reduction_plan(run, plan, contracts=contracts)
        rebuilt = report["sections_rebuilt"][sid]
        assert rebuilt["rebuilt"] is True
        assert rebuilt["provenance_basis"] == "SECTION_DIR_CONTENT_UNITS"
        after = _fragment_prose_chars(section)
        assert after > 0, "出处 id 对不上号，不能把整节正文抹掉"
        # 削掉多少就少多少：正文的缺口必须能被执行报告的削减字数解释，不多丢一个字。
        assert after >= before - int(report["ledger_chars_removed"]), (before, after, report)
        fragment_text = json.dumps(json.loads(
            (section / "fragment.json").read_text(encoding="utf-8")), ensure_ascii=False)
        for cu in contracts:
            on_disk = json.loads((section / "cus" / f"{cu}.json").read_text(encoding="utf-8"))
            assert on_disk["content"] in fragment_text, f"{cu} 的正文没被装回节交付件"
        assert report["provider_called"] is False

    # 退一档也凑不齐（本节磁盘 CU 少于装配过的条数）：宁可不短，也不抹正文 —— 原文件一字不改。
    with _run_dir() as run:
        section, contracts, ledger, plan = _run_d_shaped_section(run)
        (section / "cus" / f"CU-{sid}-03.json").unlink()
        kept = (section / "fragment.json").read_text(encoding="utf-8")
        report = execute_reduction_plan(run, plan, contracts=contracts)
        rebuilt = report["sections_rebuilt"][sid]
        assert rebuilt["rebuilt"] is False
        assert rebuilt["reason"] == "WOULD_DROP_ASSEMBLED_CONTENT"
        assert rebuilt["unrecallable_content_unit_ids"]
        assert (section / "fragment.json").read_text(encoding="utf-8") == kept


def _unmarked_table_section(run: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """落一节"上一次装配的表格没有出处标记"的现场（Round 3 及更早构建的形状）。

    一条可削的叙述 CU 提供动作，一条表格 CU 提供结构槽位：表格 archetype 全在 §22 保护类里，
    计划不会点名它，但重建整节时它会被**再装配一遍** —— 剥不掉旧表就是 166 → 278 的来源。
    """
    sid, cu_narr, cu_table = "S04-03", "CU-S04-03-01", "CU-S04-03-02"
    payload = _table_payload(8)
    contracts = {
        cu_narr: _cu_a_contract(cu_narr, sid),
        cu_table: _contract(cu_table, sid, archetype="STANDARD_TABLE",
                            minimum=40, target=50, maximum=9999),
    }
    rows = [
        _cu_row(cu_narr, sid, archetype="NARRATIVE", content=_prose(), payload=None),
        _cu_row(cu_table, sid, archetype="STANDARD_TABLE",
                content="设备机房温湿度每日两次抄表，读数超出即当日报警并留痕。", payload=payload),
    ]
    section = _write_section(run, sid, rows)
    ledger = _write_ledger(run, [judge_content_unit(
        contracts[row["content_unit_id"]],
        content={"content": row["content"], "payload": row["payload"]}) for row in rows])
    plan = targeted_reduction_plan(ledger=ledger, content_units=list(contracts.values()),
                                   page_targets=(250, 320), ruler=None,
                                   measured_technical_pages=395)
    path = section / "fragment.json"
    fragment = json.loads(path.read_text(encoding="utf-8"))
    for table in fragment.get("tables") or []:
        if isinstance(table, dict):
            table.pop("source_content_unit_id", None)
    path.write_text(json.dumps(fragment, ensure_ascii=False), encoding="utf-8")
    return section, contracts, ledger, plan


def test_reduce_13_unmarked_cu_tables_are_replaced_not_appended():
    """REDUCE-13：结构槽位认不出出处时，只有"整槽都是 CU 的"才敢重装，否则不重建。

    Run D 回放的第二半：Round 3 装配出来的表格不带 `source_content_unit_id`，
    `strip_cu_structures` 剥不动，削减后的表又被追加一遍 —— Run D 的表从 166 张长到 278 张，
    "削减后变短"在结构这一路反成变长。当前构建有标记、剥得动，所以这条只在历史件上暴露。
    判据换算用的是**这一次要装进去的 CU 批次**，不是 fragment 自己记的装配账 ——
    S06-01 那段就是记录被治理抹平 archetype 后算出的假绿灯（记录 4 张、实装 6 张）。
    """
    with _run_dir() as run:
        section, contracts, ledger, plan = _unmarked_table_section(run)
        assert [row["action"] for row in plan["actions"]] == [ARRIVED_ACTION]
        before = _fragment_prose_chars(section)
        report = execute_reduction_plan(run, plan, contracts=contracts)
        rebuilt = report["sections_rebuilt"]["S04-03"]
        assert rebuilt["rebuilt"] is True
        assert rebuilt["structure_basis"] == "DERIVED_FROM_ASSEMBLED_CONTENT_UNITS"
        assert _fragment_prose_chars(section) < before
        fragment = json.loads((section / "fragment.json").read_text(encoding="utf-8"))
        # 槽位里还是那一张表：被"削减版 + 未削版"叠成两张才是 Run D 回放里的真实形状。
        assert len(fragment["tables"]) == 1, fragment["tables"]
        assert fragment["tables"][0]["rows"] == _table_payload(8)["rows"]
        assert len(fragment["content_unit_fragments"]) == 2

    # 槽位里混着一张节自带的表：条数对不上就认不全，宁可不重建也不留下两张一样的表。
    with _run_dir() as run:
        section, contracts, ledger, plan = _unmarked_table_section(run)
        path = section / "fragment.json"
        fragment = json.loads(path.read_text(encoding="utf-8"))
        fragment["tables"].append({"title": "节自带表", "columns": ["a"], "rows": [["1"]]})
        path.write_text(json.dumps(fragment, ensure_ascii=False), encoding="utf-8")
        kept = path.read_text(encoding="utf-8")
        report = execute_reduction_plan(run, plan, contracts=contracts)
        rebuilt = report["sections_rebuilt"]["S04-03"]
        assert rebuilt["rebuilt"] is False
        assert rebuilt["reason"] == "CU_STRUCTURE_PROVENANCE_AMBIGUOUS"
        assert rebuilt["structure_slots"] == {"cu_side": [1, 0], "fragment_side": [2, 0]}
        assert path.read_text(encoding="utf-8") == kept

    # Run D 的 S06-01 形状：装配记录里的 archetype 被治理抹掉一批，记录只认得一半的表。
    # 拿记录换算会得到"条数对得上"的假绿灯，拿这一批 CU 换算才看得见实装会多出两张表 —— 停手。
    with _run_dir() as run:
        sid = "S06-01"
        cus = [f"CU-{sid}-0{index}" for index in (1, 2, 3)]
        contracts = {cus[0]: _cu_a_contract(cus[0], sid),
                     cus[1]: _contract(cus[1], sid, archetype="STANDARD_TABLE",
                                       minimum=40, target=50, maximum=9999),
                     cus[2]: _contract(cus[2], sid, archetype="MATRIX",
                                       minimum=40, target=50, maximum=9999)}
        rows = [
            _cu_row(cus[0], sid, archetype="NARRATIVE", content=_prose(), payload=None),
            _cu_row(cus[1], sid, archetype="STANDARD_TABLE",
                    content="设备机房温湿度每日两次抄表，读数超出即当日报警并留痕。",
                    payload=_table_payload(6)),
            _cu_row(cus[2], sid, archetype="MATRIX",
                    content="责任边界按岗位、班次与交接记录三向对齐。",
                    payload=_table_payload(7)),
        ]
        section = _write_section(run, sid, rows)
        ledger = _write_ledger(run, [judge_content_unit(
            contracts[row["content_unit_id"]],
            content={"content": row["content"], "payload": row["payload"]}) for row in rows])
        plan = targeted_reduction_plan(ledger=ledger, content_units=list(contracts.values()),
                                       page_targets=(250, 320), ruler=None,
                                       measured_technical_pages=395)
        path = section / "fragment.json"
        fragment = json.loads(path.read_text(encoding="utf-8"))
        assert len(fragment["tables"]) == 2
        fragment["tables"] = fragment["tables"][:1]          # 历史记录只装上了其中一张
        for table in fragment["tables"]:
            table.pop("source_content_unit_id", None)
        labeled = [f"CU-相关专项安排-{index:02d}" for index in range(1, len(cus) + 1)]
        fragment["content_unit_execution"]["planned_content_unit_ids"] = labeled
        fragment["content_unit_execution"]["assembled_content_unit_ids"] = labeled
        # 记录里的 archetype 也被抹平了：拿它换算只会算少，这正是假绿灯的来源。
        for row in fragment["content_unit_fragments"][1:]:
            row["archetype"] = ""
        path.write_text(json.dumps(fragment, ensure_ascii=False), encoding="utf-8")
        kept = path.read_text(encoding="utf-8")

        report = execute_reduction_plan(run, plan, contracts=contracts)
        rebuilt = report["sections_rebuilt"][sid]
        assert rebuilt["rebuilt"] is False
        assert rebuilt["reason"] == "CU_STRUCTURE_PROVENANCE_AMBIGUOUS"
        assert rebuilt["structure_slots"] == {"cu_side": [2, 0], "fragment_side": [1, 0]}
        assert path.read_text(encoding="utf-8") == kept


def test_reduce_14_rebuild_refuses_to_make_a_section_longer():
    """REDUCE-14：落盘之前逐节量长度 —— "削减"绝不许把交付件改长。

    磁盘 CU 存的是**治理前**正文，fragment 存的是治理后的：重装会把治理抹掉的字装回来。
    Run D 的 S02-02 实测 8,600 → 8,620，20 个字，账本与 CU 侧全绿，只有交付件悄悄变长。
    """
    sid, cu = "S04-04", "CU-0001"
    with _run_dir() as run:
        contracts = {cu: _cu_a_contract(cu, sid)}
        row = _cu_row(cu, sid, archetype="NARRATIVE", content=_prose(), payload=None)
        section = _write_section(run, sid, [row])
        ledger = _write_ledger(run, [judge_content_unit(
            contracts[cu], content={"content": row["content"], "payload": None})])
        plan = targeted_reduction_plan(ledger=ledger, content_units=[contracts[cu]],
                                       page_targets=(250, 320), ruler=None,
                                       measured_technical_pages=321)
        assert [item["action"] for item in plan["actions"]] == [ARRIVED_ACTION]
        path = section / "fragment.json"
        fragment = json.loads(path.read_text(encoding="utf-8"))
        governed = "机房温度超限当日处置。"
        fragment["body_blocks"] = [{"type": "paragraph", "content": governed}]
        fragment["content_unit_fragments"] = [
            {**item, "content": governed} for item in fragment["content_unit_fragments"]]
        path.write_text(json.dumps(fragment, ensure_ascii=False), encoding="utf-8")
        kept = path.read_text(encoding="utf-8")

        report = execute_reduction_plan(run, plan, contracts=contracts)
        rebuilt = report["sections_rebuilt"][sid]
        assert rebuilt["rebuilt"] is False
        assert rebuilt["reason"] == "WOULD_LENGTHEN_SECTION"
        assert rebuilt["rendered_chars"]["after"] > rebuilt["rendered_chars"]["before"]
        assert path.read_text(encoding="utf-8") == kept
        # CU 与账本该削还是削：停手只发生在"往交付件里装"这一步。
        assert report["ledger_chars_removed"] > 0
        after = json.loads((section / "cus" / f"{cu}.json").read_text(encoding="utf-8"))
        assert after["effective_chars"] < visible_chars_of(row["content"])


def test_reduce_10_overshoot_delivery_can_never_be_completed_clean():
    """REDUCE-10：页带**两侧**与"量不到"都不是 Clean 的凭据（Final Gate §4.2 收紧）。

    Round 3 的旧口径是"短文不机械补足，这里只挡 OVERSHOOT"，于是 243 页（低于 250 下沿）
    和"根本没量到页数"两条路都能拿 COMPLETED_CLEAN —— Codex 独立复核把这条列成 Blocker A。
    §4.2 现在要求 250–320 的**实测**页带是 Clean 的必要条件：UNDER_LENGTH / OVER_LENGTH /
    没有实测，一律不是 Clean。
    """
    cases = (
        (395, "OVERSHOOT", "NEEDS_REVIEW"),
        (300, "TARGET_RANGE", "COMPLETED_CLEAN"),
        (243, "UNDER_LENGTH", "NEEDS_REVIEW"),
        (0, "NOT_MEASURED", "NEEDS_REVIEW"),
    )
    for pages, band, delivery in cases:
        with _run_dir() as run:
            _delivery_run(run, pages=pages or None)
            facts = collect_facts(run, medium="WORD", draft_delivery=delivery)
            state = derive_run_state(facts)
            # 先自证现场干净：B 四条内容证据齐，终态差异才只可能来自页门。
            assert state["volume_inputs_complete"], (pages, facts)
            assert facts["page_band"] == band, f"{pages} 页应读成 {band}，实读 {facts['page_band']}"
            assert state["page_band"] == band
            assert state["delivery_status"] == delivery, (pages, state)
            if band == "OVERSHOOT":
                assert state["internal_phase"] == "COMPLETED_WITH_REVIEW"
                assert facts["page_band_basis"] == "MEASURED_FINAL_RENDER"
            if band == "NOT_MEASURED":
                assert facts["page_band_source"] == "NOT_MEASURED"
