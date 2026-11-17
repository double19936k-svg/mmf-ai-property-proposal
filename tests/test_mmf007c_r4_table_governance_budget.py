"""MMF-007C Round 4 · P1-F（§63–§64）：低信息表的节级治理 × Section 预算回填。

全部离线：Synthetic Fixture + 规划层纯函数 + 生产装配入口，不发起任何 Provider 调用。
上一轮把表格治理做成"逐张表各自压一遍"，于是同一节里两张只有行标签的同列孪生表各自
存活、各自占页。§63 补的是**节级**这一层：先合并、合并机会用完后仍低信息的转删、
带需求应答的 protected 表保留并点名、表单永不参与合并。

§64 补的是这一层与预算之间的缝：CU 预算账按 payload 记结构字（治理**前**），而合并掉、
转删掉的那些字永远不会渲染。不回填，下一批的降额度判决就是在为一个不存在的表格压正文。

这里钉的是**口径一致性**，不是好看的数：装配端（build_docx）与预算端
（longform/cu_assembly）必须调同一个 `plan_section_tables`、用同一把 `visible_chars_of`。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for item in (APP, TESTS, ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from governance.table_qa import (BIDDER_FILL_MARK, FRAME_MAX_COLUMNS, LOW_INFORMATION_CELL_MIN,
                                audit_docx_tables, plan_section_tables, table_metrics)
from longform.cu_assembly import (apply_content_units, cu_table_descriptors, rendered_shape,
                                 strip_cu_structures)
from planning.budget_control import visible_chars_of
from planning.unified_budget import empty_midrun_state, observe_batch
from tests.test_mmf007c_table_density import (BRIEF, _artifact, _build, _paragraph, _table)
from tests.test_mmf007c_unified_budget import _planned_tree

_HEADERS = ["里程碑事项", "完成标准", "责任岗位", "甲方签认", "例外处置"]
_OTHER_HEADERS = ["检查项", "标准", "记录"]
# §63 那条动手的形状线不是想象出来的：Run D 交付件里 7 张低信息表全是"剪掉没有内容的
# 列之后只剩 1 列、有效格 1～5 个"。夹具照同一个形状给 —— 声明 5 列、只有行标签那一列
# 有字，于是单表治理先把 4 列空列剪掉（`prune_empty_columns`），留下的才是线上的框。
# 用"真的只有一列"当夹具会把这条线测歪：那 4 个空格子占掉的页正是治理要回收的东西。
_FRAME_A = ["进场动员会后形成纪要", "首期三级书面交底归档", "隐蔽工程联合验收", "竣工资料分册移交"]
_FRAME_B = ["周计划滚动分解到班组", "月度品质巡检覆盖全域", "异常事项闭环销项", "班次交接双人签认"]
_ROW_A = ["进场动员会", "会后24小时内形成纪要并分发至各班组与甲方接口人", "项目经理",
          "工程部经理签字确认", "48小时内补开并书面说明"]
_ROW_B = ["首期交底", "开工前完成三级书面交底并由甲方代表签认后归档", "项目总工",
          "甲方代表签字", "未交底不得开工并上报"]
_RICH_ROWS = [_ROW_A, _ROW_B]


def _frame(title: str, labels: list[str], columns=None, **extra) -> dict:
    """一张 §63 意义上的框：多列表体、只有行标签那一列有字。"""
    cols = columns or _HEADERS
    return {"title": title, "columns": list(cols),
            "rows": [[label] + [""] * (len(cols) - 1) for label in labels],
            "notes": "", **extra}


def _real(title: str, rows=None, columns=None, **extra) -> dict:
    """一张有内容的正文表格：§63 三条腿都不许碰它。"""
    return {"title": title, "columns": columns or _HEADERS,
            "rows": rows if rows is not None else _RICH_ROWS, "notes": "", **extra}


def _is_frame(table: dict) -> bool:
    """夹具自检：这张表确实落在 §63 那条线上，而不是靠表题看起来薄。

    没有这一句，下面所有"合并/转删"的用例都可能只是在测一张本来就会被单表治理删掉的
    垃圾表 —— 那 §63 的节级这一层等于没测。
    """
    metrics = table_metrics(table["columns"], table["rows"], title=table["title"])
    return (metrics["low_information"]
            and metrics["content_cells"] < LOW_INFORMATION_CELL_MIN)


def _matrix_cu(cu_id: str, title: str, *, columns=None, labels=None,
               metadata: bool = True) -> dict:
    """一条与装配端同形状的 CU 记录（payload 里带两个不渲染的元数据键）。"""
    cols = columns or _HEADERS
    labels = labels if labels is not None else _FRAME_A
    payload = {"title": title, "columns": list(cols),
               "rows": [[label] + [""] * (len(cols) - 1) for label in labels], "notes": ""}
    if metadata:
        # §64 的差额来源之一：payload 里这类键预算记了字，Word 里却没有落点。
        payload["source"] = "招标文件技术需求书条款"
        payload["unit"] = "每班次一次全区域覆盖巡检"
    return {"content_unit_id": cu_id, "parent_section_id": "S-X", "archetype": "MATRIX",
            "content": "", "generation_origin": "provider_content_unit", "payload": payload}


class Sec63FixtureShape(unittest.TestCase):
    """夹具必须先落在被测那条线上，否则用例绿的是别的东西。"""

    def test_the_frame_fixture_sits_on_the_governance_line_not_on_the_title(self):
        self.assertTrue(_is_frame(_frame("里程碑作业标准表（一）", _FRAME_A)))
        self.assertFalse(_is_frame(_real("作业标准表")), "有内容的表不能被判成框")
        # 表题里带不带"表"字与判定无关：量的是形状，不是名字。
        self.assertTrue(_is_frame(_frame("接口矩阵", _FRAME_A)))

    def test_the_frame_shape_is_what_the_ruler_measures_after_column_pruning(self):
        plan = plan_section_tables([_frame("里程碑作业标准表（一）", _FRAME_A)])["ledger"][0]
        self.assertEqual(len(plan["planned_columns"]), len(_HEADERS), "声明的列要留痕")
        self.assertLess(len(plan["columns"]), FRAME_MAX_COLUMNS,
                        "剪掉空列后只剩一列 —— 这就是 Run D 量到的那个框")


class Sec63MergeAndDrop(unittest.TestCase):
    """§63：合并 → 转删 → 保留，三条腿都要有名字，而且只在低信息表上动。"""

    def test_two_low_information_tables_with_same_columns_merge_into_one(self):
        governed = plan_section_tables([_frame("里程碑作业标准表（一）", _FRAME_A),
                                       _frame("里程碑作业标准表（二）", _FRAME_B)])
        report = governed["report"]
        self.assertEqual(report["tables_planned"], 2)
        self.assertEqual(report["tables_rendered"], 1)
        self.assertEqual(report["tables_merged_groups"], 1)
        self.assertEqual(report["low_information_merged"], 2)
        self.assertEqual(report["tables_dropped"], 0)
        plan = governed["tables"][0]
        self.assertEqual(plan["governance"], "merged_low_information")
        self.assertEqual(plan["action"], "compact", "合并后的形状必须留痕，不能装作原本就一张")
        self.assertEqual(plan["source_tables"], 2)
        self.assertEqual(plan["merged_from"], ["里程碑作业标准表（一）", "里程碑作业标准表（二）"])
        self.assertEqual(len(plan["rows"]), 8, "两张表的行都要活下来")
        self.assertGreaterEqual(len(plan["rows"]), LOW_INFORMATION_CELL_MIN,
                                "并完越过那条线才叫增益：两张框并成一张还能留的表")
        self.assertIn("merge_low_information_tables:2", plan["reason"])
        # 台账按来源表逐张留痕：读账的人要能看出这张表是谁给的。
        self.assertEqual(len(governed["ledger"]), 1)
        merge = report["merges"][0]
        self.assertEqual(merge["columns"], _HEADERS, "合并键就是治理前的列名，不是猜的")
        self.assertEqual(merge["into"], "里程碑作业标准表（一）")

    def test_merge_dedupes_word_identical_rows_and_keeps_distinct_notes(self):
        shared = _FRAME_A[0]
        governed = plan_section_tables([
            _frame("甲表", _FRAME_A, notes="依据技术需求书巡检条款执行"),
            _frame("乙表", [shared, *_FRAME_B[:3]], notes="由品质部按月复核")])
        plan = governed["tables"][0]
        self.assertEqual(plan["source_tables"], 2)
        self.assertEqual(len(plan["rows"]), 7, "逐字重复的行只留一条，合并不是叠罗汉")
        self.assertEqual(sum(1 for row in plan["rows"] if row[0] == shared), 1)
        self.assertEqual(plan["notes"], "依据技术需求书巡检条款执行；由品质部按月复核")

    def test_two_frames_that_are_still_a_frame_after_merging_are_dropped_too(self):
        # 合并是转删的替代动作，不是免死牌：两张逐字相同的框并完还是框，就要照样转删。
        # 否则"两张空框并一张"就能把框洗到纸上 —— 那是换形状撑页。
        governed = plan_section_tables([_frame("甲表", [_FRAME_A[0]]),
                                       _frame("乙表", [_FRAME_A[0], _FRAME_A[1]])])
        report = governed["report"]
        self.assertEqual(report["tables_merged_groups"], 1)
        self.assertEqual(report["tables_rendered"], 0)
        self.assertEqual(report["low_information_dropped"], 2,
                         "并完仍转删的两张来源表都要数进 §63 的账")
        plan = governed["ledger"][0]
        self.assertEqual(plan["governance"], "merged_low_information_dropped")
        self.assertEqual(plan["rows"], [])
        self.assertIn("并入后仍然只剩", plan["reason"])

    def test_a_single_low_information_table_with_no_partner_is_dropped(self):
        governed = plan_section_tables([_frame("里程碑作业标准表（一）", _FRAME_A)])
        report = governed["report"]
        self.assertEqual(report["low_information_dropped"], 1)
        self.assertEqual(report["tables_rendered"], 0, "只剩框和空格的表占页不占内容")
        plan = governed["ledger"][0]
        self.assertEqual(plan["action"], "drop")
        self.assertEqual(plan["governance"], "low_information_dropped")
        self.assertIn("只剩 1 列", plan["reason"], "转删的理由必须点名形状，不能只说「薄」")
        self.assertIn(f"有效格 {len(_FRAME_A)} 个", plan["reason"])
        self.assertEqual(plan["rows"], [])
        dropped = report["dropped"][0]
        self.assertEqual(dropped["title"], "里程碑作业标准表（一）")
        self.assertEqual(dropped["content_cells"], len(_FRAME_A))

    def test_thin_but_real_table_is_retained_and_named_not_read_as_compliant(self):
        """§63 只删"只剩框"的那些：行标签多到还能算一张表的，留着，但要点名低信息保留。

        Round 2 的 §29 明确写过"行标签是对需求的响应，删不得"，这条保证不能被 §63 顺手
        抹掉 —— 所以留是留，账上仍要看得见它薄。
        """
        thin = _frame("需求应答矩阵", [f"里程碑{index}" for index in range(1, 10)])
        governed = plan_section_tables([thin])
        report = governed["report"]
        self.assertTrue(table_metrics(thin["columns"], thin["rows"])["low_information"],
                        "夹具要真的薄，否则这条腿是空测")
        self.assertFalse(_is_frame(thin), "但有效格已经够多 —— 它还是张表，不是框")
        self.assertEqual(report["tables_rendered"], 1)
        self.assertEqual(report["low_information_dropped"], 0)
        self.assertEqual(report["low_information_retained"], 1)
        self.assertEqual(governed["ledger"][0]["governance"], "low_information_retained")

    def test_protected_low_information_table_is_retained_and_named(self):
        # 带需求/评分应答的表是答题边界，宁可留一张薄表也不能替甲方决定删掉谁的应答。
        governed = plan_section_tables([_frame("接口矩阵（需求应答）", _FRAME_A, protected=True)])
        report = governed["report"]
        self.assertEqual(report["low_information_retained_protected"], 1)
        self.assertEqual(report["low_information_dropped"], 0)
        self.assertEqual(report["tables_merged_groups"], 0, "protected 表不许被并进别人的表体")
        self.assertEqual(report["tables_rendered"], 1)
        plan = governed["tables"][0]
        self.assertEqual(plan["governance"], "low_information_retained_protected")
        self.assertTrue(plan["protected"])

    def test_forms_never_enter_a_merge_group(self):
        # 表单的空格是留给甲方填的，两张待填写表并一张会把填写区域搞乱。
        forms = [{**_frame(f"巡检记录表{BIDDER_FILL_MARK}", ["设备编号", "运行状态"],
                          columns=["关键字段", "填写要求", "责任岗位", "触发条件"]),
                  "archetype": "FORM_TEMPLATE"}]
        second = {**forms[0], "title": f"交接班记录表{BIDDER_FILL_MARK}"}
        governed = plan_section_tables([forms[0], second])
        report = governed["report"]
        self.assertEqual(report["tables_merged_groups"], 0)
        self.assertEqual(report["tables_dropped"], 0, "待填写表单不是低信息缺陷")
        self.assertEqual(report["tables_rendered"], 2)
        self.assertTrue(all(plan["governance"] == "rendered" for plan in governed["ledger"]))

    def test_a_frame_and_a_form_with_same_columns_do_not_merge(self):
        # 表单即使列名撞上也不同框：合并 candidacy 先把表单请出去。
        frame = _frame("设备清单", ["消防泵", "应急照明"])
        form = {**frame, "title": f"设备确认表{BIDDER_FILL_MARK}",
                "archetype": "FORM_TEMPLATE",
                "rows": [["消防泵", "按项目要求填写"], ["应急照明", "按项目要求填写"]]}
        report = plan_section_tables([frame, form])["report"]
        self.assertEqual(report["tables_merged_groups"], 0)

    def test_meaningful_tables_and_different_columns_stay_separate(self):
        rich = plan_section_tables([_real("作业标准表A"), _real("作业标准表B")])
        self.assertEqual(rich["report"]["tables_merged_groups"], 0, "有内容的表不许被并")
        self.assertEqual(rich["report"]["tables_rendered"], 2)
        # 两张都是框、但列不同：合了就是把两张不相干的表拼成一张假表，所以只能各自走
        # 各自的转删。这一条在收紧形状线之前是空测（当时两张都不是候选）。
        diff = plan_section_tables([_frame("甲", _FRAME_A),
                                   _frame("乙", ["消防通道畅通", "器材在有效期内"],
                                          columns=_OTHER_HEADERS)])
        self.assertEqual(diff["report"]["tables_merged_groups"], 0, "列不同就不是同一张表")
        self.assertEqual(diff["report"]["low_information_dropped"], 2)

    def test_governance_never_mutates_the_input_tables(self):
        tables = [_frame("甲表", _FRAME_A), _frame("乙表", _FRAME_B)]
        snapshot = json.dumps(tables, ensure_ascii=False, sort_keys=True)
        plan_section_tables(tables)
        self.assertEqual(json.dumps(tables, ensure_ascii=False, sort_keys=True), snapshot,
                         "治理返回新形状，不能改调用方手里仍在用的表")


class Sec63ReachesTheArtifact(unittest.TestCase):
    """§63 的动作必须落在真实 Word 与台账里，否则只是函数返回值上的治理。"""

    @staticmethod
    def _artifact_table(title: str, labels: list[str], columns=None) -> dict:
        cols = columns or _HEADERS
        return _table(title, cols, [[label] + [""] * (len(cols) - 1) for label in labels])

    def test_merged_and_dropped_tables_show_up_in_the_build_ledger_and_the_docx(self):
        artifact = _artifact([
            {"heading": "进度与接口管理", "level": 1, "paragraphs": [_paragraph(3)],
             "tables": [self._artifact_table("里程碑作业标准表（一）", _FRAME_A),
                        self._artifact_table("里程碑作业标准表（二）", _FRAME_B)]},
            {"heading": "消防与应急", "level": 1, "paragraphs": [_paragraph(4)],
             "tables": [self._artifact_table("消防通道检查表", ["消防通道畅通"],
                                             columns=_OTHER_HEADERS)]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            self.assertEqual(ledger["tables_planned"], 3, "Planned 按来源表数，合并不能把自己数没了")
            self.assertEqual(ledger["tables_rendered"], 1, "两张低信息表并一张，第三张转删")
            self.assertEqual(ledger["tables_dropped"], 1)
            self.assertEqual(ledger["tables_low_information_governed_away"], 3,
                             "并掉的两张 + 转删的一张，都是治理动的手")
            block = ledger["section_table_governance"]
            self.assertEqual(block["tables_planned"], 3)
            self.assertEqual(block["merge_groups"], 1)
            self.assertEqual(block["low_information_dropped"], 1)
            self.assertEqual(audit_docx_tables(docx)["tables_total"], 1,
                             "交付件里只剩一张 —— 这页是真的省下来了")
            merged = next(row for row in ledger["ledger"] if row["governance"] == "merged_low_information")
            self.assertEqual(merged["source_tables"], 2)
            self.assertEqual(merged["merged_from"], ["里程碑作业标准表（一）", "里程碑作业标准表（二）"])
            self.assertEqual(merged["planned_columns"], _HEADERS)
            self.assertEqual(merged["rendered_columns"], ["里程碑事项"],
                             "剪掉的 4 列空格在台账里要看得出被剪了")
            self.assertEqual(audit_docx_tables(docx)["tables_low_information"], 0,
                             "交付端量到的低信息表为 0 是因为治理动了手，不是天生干净")


class Sec64AssemblyAccounting(unittest.TestCase):
    """§64：装配点同时握着"CU 交了什么"和"治理后渲染什么"，两个数必须一起记。"""

    def test_dropped_and_merged_tables_reclaim_only_what_never_renders(self):
        rows = [_matrix_cu("CU-1", "甲表", labels=_FRAME_A),
                _matrix_cu("CU-2", "乙表", labels=_FRAME_B),
                _matrix_cu("CU-3", "丙表", columns=_OTHER_HEADERS, labels=["消防通道畅通"])]
        fragment = apply_content_units(rows, base_fragment={"tables": [], "processes": []},
                                       section_id="S-X",
                                       planned_ids=["CU-1", "CU-2", "CU-3"])
        account = fragment["table_governance"]
        self.assertEqual(account["structure_units_charged"], 3)
        self.assertEqual(account["structure_chars_charged"],
                         sum(visible_chars_of(row["payload"]) for row in rows))
        self.assertEqual(account["tables_planned"], 3)
        self.assertEqual(account["tables_merged_groups"], 1)
        self.assertEqual(account["low_information_dropped"], 1)
        by_id = {row["content_unit_id"]: row for row in account["per_content_unit"]}
        self.assertEqual(by_id["CU-3"]["governed_chars"], 0, "转删的表一个字都不渲染")
        self.assertEqual(by_id["CU-1"]["governed_chars"], by_id["CU-2"]["governed_chars"],
                         "同一次合并的两位成员按成员数平摊")
        self.assertLess(by_id["CU-1"]["governed_chars"], by_id["CU-1"]["charged_chars"])
        self.assertEqual(account["structure_chars_governed"],
                         sum(row["governed_chars"] for row in account["per_content_unit"]))
        self.assertEqual(account["reclaimed_chars"],
                         account["structure_chars_charged"] - account["structure_chars_governed"])
        self.assertEqual(account["units_where_governance_removed_chars"], 3)
        self.assertEqual(account["units_where_governance_added_chars"], 0)
        self.assertEqual(sum(row["reclaimed_chars"] for row in account["per_content_unit"]),
                         account["reclaimed_chars"], "节级差额必须等于逐 CU 差额之和")
        # 回收的正是"不会渲染的那部分"：元数据键 + 被剪掉的空列 + 转删的整张表。
        self.assertEqual(by_id["CU-3"]["reclaimed_chars"], by_id["CU-3"]["charged_chars"])

    def test_payload_metadata_and_governance_are_both_visible_in_the_account(self):
        # 只报一个"回收率"会把两件事说成一件：元数据键从来没有落点，治理是主动删并。
        rows = [_matrix_cu("CU-1", "甲表", metadata=False),
                _matrix_cu("CU-2", "乙表", metadata=False)]
        plain = apply_content_units(rows, base_fragment={"tables": [], "processes": []},
                                    section_id="S-X", planned_ids=["CU-1", "CU-2"])
        rich = apply_content_units([_matrix_cu("CU-1", "甲表"), _matrix_cu("CU-2", "乙表")],
                                   base_fragment={"tables": [], "processes": []},
                                   section_id="S-X", planned_ids=["CU-1", "CU-2"])
        self.assertEqual(plain["table_governance"]["structure_chars_governed"],
                         rich["table_governance"]["structure_chars_governed"],
                         "治理后的形状与 payload 里的元数据键无关")
        self.assertGreater(rich["table_governance"]["structure_chars_charged"],
                           plain["table_governance"]["structure_chars_charged"],
                           "元数据键确实被记进了预算账 —— 这才是要回填的部分")
        self.assertGreater(rich["table_governance"]["reclaimed_chars"],
                           plain["table_governance"]["reclaimed_chars"],
                           "没有落点的元数据字全部落在回收那一栏，不能算已用额度")

    def test_mixed_merges_are_flagged_instead_of_blamed_on_the_cu(self):
        # 节自带一张同列低信息表 + CU 一张：出处只能记查得到的那一半，并显式标 mixed，
        # 不能让"部分归属"看起来像"这张表全是 CU 的"。
        fragment = apply_content_units(
            [_matrix_cu("CU-1", "甲表", labels=_FRAME_A)],
            base_fragment={"tables": [], "processes": []},
            section_id="S-X", planned_ids=["CU-1"])
        self.assertEqual(fragment["table_governance"]["attribution_mixed_source_merges"], 0)
        base = [_frame("节自带表", _FRAME_B)]
        mixed = apply_content_units([_matrix_cu("CU-1", "甲表", labels=_FRAME_A)],
                                    base_fragment={"tables": base, "processes": []},
                                    section_id="S-X", planned_ids=["CU-1"])
        account = mixed["table_governance"]
        self.assertEqual(account["tables_merged_groups"], 1)
        self.assertEqual(account["attribution_mixed_source_merges"], 1)
        self.assertEqual(len(mixed["tables"]), 2, "合并发生在渲染前，装配形状保持治理前口径")
        by_id = {row["content_unit_id"]: row for row in account["per_content_unit"]}
        whole = sum(visible_chars_of(rendered_shape(plan))
                    for plan in plan_section_tables(mixed["tables"])["tables"])
        self.assertLess(by_id["CU-1"]["governed_chars"], whole,
                        "只归 CU 那一份，节自带表的那一半不能记在 CU 头上")

    def test_cu_to_table_conversion_has_exactly_one_rule(self):
        rows = [_matrix_cu("CU-1", "甲表"),
                {"content_unit_id": "CU-2", "parent_section_id": "S-X",
                 "archetype": "SOP", "content": "步骤", "generation_origin": "provider_content_unit",
                 "payload": {"steps": ["执行", "记录", "复核"]}},
                {"content_unit_id": "CU-3", "parent_section_id": "S-X",
                 "archetype": "FORM_TEMPLATE", "content": "",
                 "generation_origin": "provider_content_unit",
                 "payload": {"form_name": "巡检记录表", "fields": ["设备编号", "运行状态"]}}]
        descriptors = cu_table_descriptors(rows)
        self.assertEqual([row["source_content_unit_id"] for row in descriptors], ["CU-1", "CU-3"],
                         "流程型 CU 不产表格，装配端与预算端认的是同一批 archetype")
        fragment = apply_content_units(rows, base_fragment={"tables": [], "processes": []},
                                       section_id="S-X",
                                       planned_ids=["CU-1", "CU-2", "CU-3"])
        self.assertEqual(len(fragment["tables"]), 2)
        self.assertEqual(len(fragment["processes"]), 1)
        account = fragment["table_governance"]
        self.assertEqual({row["content_unit_id"] for row in account["per_content_unit"]},
                         {"CU-1", "CU-3"})

    def test_reducing_a_cu_shrinks_the_account_instead_of_growing_tables(self):
        """削减执行器走的就是这条重装配路：削掉一张表，节要真的变短、表不能多出来。"""
        rows = [_matrix_cu("CU-1", "甲表", labels=_FRAME_A),
                _matrix_cu("CU-2", "乙表", labels=_FRAME_B),
                _matrix_cu("CU-3", "丙表", labels=_FRAME_A[:2] + _FRAME_B[:2])]
        full = apply_content_units(rows, base_fragment={"tables": [], "processes": []},
                                   section_id="S-X", planned_ids=["CU-1", "CU-2", "CU-3"])
        for _ in range(3):   # 反复重建不应长表：旧 CU 加的那几份必须先剥掉
            full = apply_content_units(rows, base_fragment=strip_cu_structures(full),
                                       section_id="S-X", planned_ids=["CU-1", "CU-2", "CU-3"])
        self.assertEqual(len(full["tables"]), 3)
        self.assertEqual(full["table_governance"]["structure_chars_charged"],
                         sum(visible_chars_of(row["payload"]) for row in rows))
        cut = apply_content_units(rows[:2], base_fragment=strip_cu_structures(full),
                                  section_id="S-X", planned_ids=["CU-1", "CU-2"],
                                  missing_ids=["CU-3"])
        self.assertEqual(len(cut["tables"]), 2)
        self.assertEqual(cut["content_unit_execution"]["outstanding_content_unit_ids"], ["CU-3"])
        self.assertLess(cut["table_governance"]["structure_chars_charged"],
                        full["table_governance"]["structure_chars_charged"])
        self.assertEqual(cut["table_governance"]["structure_units_charged"], 2)
        # 同列的 CU-1/CU-2 合并、CU-3 的标签与前两张逐字相同 → 三张框并完仍是框，
        # 削减之后仍是这个结论：账随形状走，不是随 CU 个数线性走。
        self.assertEqual(cut["table_governance"]["tables_merged_groups"], 1)
        self.assertEqual(full["table_governance"]["tables_merged_groups"], 1)

    def test_rendered_shape_is_the_same_four_pieces_the_assembler_uses(self):
        plan = plan_section_tables([_real("甲表")])["tables"][0]
        shape = rendered_shape(plan)
        self.assertEqual(set(shape), {"title", "columns", "rows", "notes"})
        self.assertEqual(visible_chars_of(shape),
                         visible_chars_of({"title": plan["title"], "columns": plan["columns"],
                                           "rows": plan["rows"], "notes": plan["notes"]}))
        governed = plan_section_tables([_frame("乙表", _FRAME_A)])
        self.assertEqual(governed["tables"], [],
                         "转删的表不进渲染清单：预算账按渲染清单量，所以它贡献 0 字")
        self.assertEqual(governed["ledger"][0]["rows"], [])


class Sec64BudgetWriteBack(unittest.TestCase):
    """§64：治理回收要回填 `used_table_chars`，下一批的额度才不是为不存在的表压正文。"""

    @staticmethod
    def _record(sid: str, index: int, prose: int, structure: int) -> dict:
        return {"content_unit_id": f"CU-{sid}-{index:02d}", "section_id": sid,
                "archetype": "MATRIX", "prose_chars": prose, "structure_chars": structure,
                "actual_chars": prose + structure}

    def test_governance_reclaim_returns_chars_to_the_section_budget(self):
        tree = _planned_tree([("S-A", ["NARRATIVE", "MATRIX"], 22000),
                              ("S-B", ["NARRATIVE", "MATRIX"], 22000)])
        plan = empty_midrun_state(tree)
        observed = observe_batch(plan, [self._record("S-B", 1, 8000, 3000)], table_governance={
            "S-B": {"structure_chars_charged": 3000, "structure_chars_governed": 1200,
                    "tables_merged_groups": 1, "tables_dropped": 1, "low_information_dropped": 1}})
        row = observed["sections"]["S-B"]
        self.assertEqual(row["used_table_chars"], 1200, "只剩会渲染的那 1200 字")
        self.assertEqual(row["table_governance"]["applied_chars"], 1800)
        self.assertEqual(row["table_governance"]["unapplied_chars"], 0)
        self.assertEqual(row["remaining_section_chars"],
                         row["section_budget"] - row["used_narrative_chars"] - 1200)
        roll = observed["table_governance"]
        self.assertEqual(roll["status"], "OBSERVED")
        self.assertEqual(roll["sections_observed"], 1)
        self.assertEqual(roll["reclaimed_chars"], 1800)
        self.assertEqual(roll["tables_merged"], 1)
        self.assertEqual(roll["low_information_dropped"], 1)
        self.assertEqual(observed["provider_calls_made"], 0)

    def test_reclaim_cannot_go_below_zero_and_the_remainder_stays_visible(self):
        # 账上的表格字比治理还少（老账、或别的 archetype 挪过来的数）时，差额必须报出来，
        # 不能凭空造出正文额度，也不能悄悄吞掉。
        tree = _planned_tree([("S-A", ["NARRATIVE", "MATRIX"], 22000),
                              ("S-B", ["NARRATIVE", "MATRIX"], 22000)])
        plan = empty_midrun_state(tree)
        observed = observe_batch(plan, [self._record("S-B", 1, 8000, 500)], table_governance={
            "S-B": {"structure_chars_charged": 3000, "structure_chars_governed": 0}})
        row = observed["sections"]["S-B"]
        self.assertEqual(row["used_table_chars"], 0)
        self.assertEqual(row["table_governance"]["applied_chars"], 500)
        self.assertEqual(row["table_governance"]["unapplied_chars"], 2500)
        self.assertGreaterEqual(row["used_narrative_chars"], 0)
        self.assertGreaterEqual(row["remaining_section_chars"], 0)

    def test_a_governed_table_that_costs_more_than_booked_is_not_hidden(self):
        # 治理也会加字（表下说明的要素标签）。加字方向如实记账，不假装回收。
        tree = _planned_tree([("S-A", ["NARRATIVE", "MATRIX"], 22000),
                              ("S-B", ["NARRATIVE", "MATRIX"], 22000)])
        plan = empty_midrun_state(tree)
        observed = observe_batch(plan, [self._record("S-B", 1, 8000, 1000)], table_governance={
            "S-B": {"structure_chars_charged": 1000, "structure_chars_governed": 1400}})
        row = observed["sections"]["S-B"]
        self.assertEqual(row["used_table_chars"], 1000, "没有回收就没有回填，不能倒着扣")
        self.assertEqual(row["table_governance"]["reclaimed_chars"], 0)

    def test_sections_without_a_governance_account_are_reported_not_observed(self):
        tree = _planned_tree([("S-A", ["NARRATIVE", "MATRIX"], 22000),
                              ("S-B", ["NARRATIVE", "MATRIX"], 22000)])
        plan = empty_midrun_state(tree)
        observed = observe_batch(plan, [self._record("S-B", 1, 8000, 3000)])
        self.assertEqual(observed["table_governance"]["status"], "NOT_OBSERVED")
        self.assertEqual(observed["sections"]["S-B"]["used_table_chars"], 3000,
                         "没有治理账就是没测到，不猜一个系数")
        self.assertNotIn("table_governance", observed["sections"]["S-B"])

    def test_the_budget_account_and_the_render_plan_agree_on_one_number(self):
        """装配端渲染哪几张表、预算端记 governed 多少字，必须是同一个决定算出来的。

        全部表格都来自 CU 时（没有节自带表），两边应当逐字相等；允许的差异只有
        合并成员平摊时的取整，所以容差按 CU 数给，不按比例给。
        """
        rows = [_matrix_cu("CU-1", "甲表", labels=_FRAME_A),
                _matrix_cu("CU-2", "乙表", labels=_FRAME_B)]
        fragment = apply_content_units(rows, base_fragment={"tables": [], "processes": []},
                                       section_id="S-X", planned_ids=["CU-1", "CU-2"])
        rendered = plan_section_tables(fragment["tables"])["tables"]
        rendered_chars = sum(visible_chars_of(rendered_shape(plan)) for plan in rendered)
        account = fragment["table_governance"]
        self.assertGreater(rendered_chars, 0, "两张框并完要真的留下东西，否则比的是 0 和 0")
        self.assertLessEqual(abs(account["structure_chars_governed"] - rendered_chars),
                             account["structure_units_charged"],
                             "账上的 governed 与渲染形状不是同一个数")
        # 有节自带表时，账上只归 CU 那一份，不能超过整节渲染量。
        mixed = apply_content_units(
            [_matrix_cu("CU-1", "甲表", labels=_FRAME_A)],
            base_fragment={"tables": [_frame("节自带表", _FRAME_B)], "processes": []},
            section_id="S-X", planned_ids=["CU-1"])
        whole = sum(visible_chars_of(rendered_shape(plan))
                    for plan in plan_section_tables(mixed["tables"])["tables"])
        self.assertLess(mixed["table_governance"]["structure_chars_governed"], whole)
        self.assertEqual(mixed["table_governance"]["attribution_mixed_source_merges"], 1)


if __name__ == "__main__":
    unittest.main()
