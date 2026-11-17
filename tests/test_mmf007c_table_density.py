"""MMF-007C Round 2 §24-§29：P0-4 空白/低信息表格治理。

上一轮交付了 150 张表，实测其中 50 张的表体有六成以上的格子是空的 —— 多数是"只写了
行标签、格子全空"的矩阵和风险登记表。它们真实地占掉了页数，却不是内容。这里钉住：

1. 不承载信息的行/列在装配前就删掉（删行列不丢信息）；整张没有有效内容的表不渲染；
2. 真正留给投标人填的表单可以留，但必须带【待投标人填写】标记，并且要压成省行的形状；
3. 装配台账与交付件实测分开记账（Planned / Rendered / Meaningful / Placeholder）；
4. 仍不达标的表进交付门：AUTO_REPAIR，不得 COMPLETED_CLEAN，但 Word 照旧交付；
5. 页数不再只数"翻过去几页"：整页几乎没字的页不计入 effective_content_pages。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
for item in (APP, TESTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from delivery_state import DELIVERY_REVIEW, classify_fail_soft
from governance.final_artifact_qa import evaluate_docx_artifact
from governance.table_qa import (BIDDER_FILL_MARK, audit_docx_tables, classify_table,
                                 plan_table, table_metrics)
from longform.pagination import MIN_PAGE_CONTENT_CHARS, page_density

BRIEF = {"project_name": "表格信息密度试炼项目", "scenario": "完整物业服务方案", "medium": "WORD"}
_MATRIX_HEADER = ["里程碑事项", "完成标准", "责任岗位", "甲方签认", "例外处置"]
_REAL_ROW = ["进场动员会", "会后 24 小时内形成纪要并分发各班组", "项目经理", "工程部经理", "48 小时内补开"]


def _table(title: str, columns: list[str], rows: list[list[str]]) -> dict:
    return {"title": title, "columns": columns, "rows": rows}


def _paragraph(index: int) -> str:
    return (f"第{index}节作业标准说明：本单元以招标公告、技术需求书与合同文本已确认的内容为"
            "唯一事实来源，形成可核查、可交接、可复核的作业底稿，并在部位或班次发生变更时"
            "由责任岗位发起增补复核，复核结论回写台账并向甲方接口人书面确认。") * 3


def _artifact(sections: list[dict]) -> dict:
    return {"title": "表格信息密度试炼项目完整物业服务方案", "include_toc": True, "toc_depth": 1,
            "lead": [f"{BRIEF['project_name']}｜技术标"], "sections": sections}


def _build(root: Path, artifact: dict) -> tuple[Path, dict]:
    """通过生产装配入口建文件，并取回装配台账。"""
    content = root / "content.json"
    content.write_text(json.dumps({"brief": BRIEF, "artifact": artifact}, ensure_ascii=False),
                       encoding="utf-8")
    docx = root / "artifact.docx"
    metrics = root / "tables.json"
    env = dict(os.environ, MMF_ARTIFACT_BUILD_METRICS=str(metrics), PYTHONPATH=str(APP),
               PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, str(APP / "build_docx.py"), str(content), str(docx)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300, env=env)
    assert done.returncode == 0 and docx.is_file(), done.stderr[-400:]
    ledger = json.loads(metrics.read_text(encoding="utf-8")) if metrics.is_file() else {}
    return docx, ledger


class Tbl01EmptyScaffoldNeverReachesPaper(unittest.TestCase):
    def test_label_only_matrix_is_pruned_to_its_labels_not_left_as_a_blank_grid(self):
        # 这正是上一轮 #140 那张表的形状：只有行标签，四列全空。行标签是对需求的响应，
        # 所以删的是那 32 个空格子，不是这张表。
        label_rows = [[f"里程碑{index}", "", "", "", ""] for index in range(1, 9)]
        artifact = _artifact([
            {"heading": "进度与接口管理", "level": 1, "paragraphs": [_paragraph(1)],
             "tables": [_table("里程碑责任与签认接口矩阵", _MATRIX_HEADER, label_rows),
                        _table("里程碑作业标准表", _MATRIX_HEADER, [_REAL_ROW])]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            audit = audit_docx_tables(docx)
            self.assertEqual(ledger["tables_planned"], 2)
            self.assertEqual(ledger["tables_rendered"], 2, "有行标签就还有内容，不该整张丢掉")
            self.assertEqual(ledger["tables_dropped"], 0)
            self.assertEqual(ledger["tables_compacted"], 1)
            self.assertEqual(audit["tables_rendered"], 2)
            self.assertEqual(audit["classification_histogram"].get("empty_scaffold", 0), 0)
            # 台账不能只说"压了"，还要说省下了多少空格。
            self.assertEqual(ledger["planned_empty_cells"], 32)
            self.assertEqual(ledger["rendered_empty_cells"], 0)
            pruned = next(row for row in ledger["ledger"]
                          if row["title"] == "里程碑责任与签认接口矩阵")
            self.assertEqual(pruned["planned_cells"], 40)
            self.assertEqual(pruned["rendered_cells"], 8)

    def test_a_table_with_nothing_in_it_does_not_reach_paper(self):
        artifact = _artifact([
            {"heading": "环境保洁作业", "level": 1, "paragraphs": [_paragraph(5)],
             "tables": [_table("保洁质量检查表", _MATRIX_HEADER,
                               [["—", "待填写", "/", "", ""] for _ in range(3)]),
                        _table("保洁作业标准表", _MATRIX_HEADER, [_REAL_ROW])]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            self.assertEqual(ledger["tables_planned"], 2)
            self.assertEqual(ledger["tables_rendered"], 1)
            self.assertEqual(ledger["tables_dropped"], 1)
            self.assertEqual(ledger["dropped_tables"][0]["title"], "保洁质量检查表")
            self.assertIn("无有效内容", ledger["dropped_tables"][0]["reason"])
            self.assertEqual(audit_docx_tables(docx)["tables_rendered"], 1)
            self.assertEqual(ledger["rendered_empty_cells"], 0)
            # 空格与占位符分开记：把 15 个 "/" 说成 15 个空格就是换口径。
            self.assertEqual(ledger["planned_placeholder_cells"], 9)
            self.assertEqual(ledger["planned_empty_cells"], 6)
            self.assertEqual(ledger["rendered_placeholder_cells"], 0)


class Tbl02PlaceholderIsNotContent(unittest.TestCase):
    def test_placeholder_tokens_and_blanks_score_zero(self):
        metrics = table_metrics(["检查项", "标准", "记录"],
                                [["—", "待填写", "/"], ["暂无", "", "……"], ["无", "待定", "-"]])
        self.assertEqual(metrics["content_cells"], 0)
        self.assertEqual(metrics["meaningful_cell_ratio"], 0.0)
        self.assertEqual(metrics["empty_cells"], 1)
        self.assertEqual(metrics["placeholder_cells"], 8)
        self.assertEqual(classify_table(metrics), "empty_scaffold")
        plan = plan_table(_table("巡检标准表", ["检查项", "标准", "记录"],
                                 [["—", "待填写", "/"], ["暂无", "", "……"], ["无", "待定", "-"]]))
        self.assertEqual(plan["action"], "drop")
        self.assertEqual(plan["rows"], [])

    def test_short_but_real_cells_are_content_not_placeholder(self):
        # 不把"班长""日检"这类两格字当成占位符 —— 用长度阈值会造出不存在的缺陷。
        metrics = table_metrics(["岗位", "班次", "职责"], [["班长", "日检", "带班巡查并签认记录"]])
        self.assertEqual(metrics["content_cells"], 3)
        self.assertEqual(classify_table(metrics), "meaningful")


class Tbl03CompactionKeepsInformation(unittest.TestCase):
    def test_blank_and_filler_rows_are_removed_without_losing_a_row_of_substance(self):
        rows = [
            _REAL_ROW,
            ["", "", "", "", ""],
            ["—", "待填写", "/", "", ""],
            ["设备房巡查", "每班两次，雨淋季节加密", "工程维修领班", "工程部经理", "报停并挂牌"],
        ]
        plan = plan_table(_table("巡查计划表", _MATRIX_HEADER, rows))
        self.assertEqual(plan["action"], "compact")
        self.assertEqual(len(plan["rows"]), 2)
        self.assertIn("drop_blank_rows:1", plan["actions"])
        self.assertIn("drop_filler_rows:1", plan["actions"])
        self.assertEqual(plan["metrics"]["meaningful_cell_ratio"], 1.0)

    def test_empty_columns_are_pruned_not_left_as_blank_grid(self):
        label_rows = [[f"风险{index}", "", "", ""] for index in range(1, 7)]
        plan = plan_table(_table("运行风险登记表", ["风险源", "等级", "控制措施", "记录"], label_rows))
        self.assertIn("prune_empty_columns:3", plan["actions"])
        self.assertEqual(plan["metrics"]["columns"], 1)
        self.assertEqual(plan["metrics"]["empty_cells"], 0, "空格没被删掉的话，这一项不可能为 0")
        self.assertNotEqual(plan["action"], "drop", "行标签是真内容，删掉它就是丢响应")

    def test_identical_rows_are_collapsed_once(self):
        rows = [_REAL_ROW, _REAL_ROW, _REAL_ROW,
                ["夜间值守", "22:00 起每小时一巡", "秩序领班", "品质主管", "即时上报"]]
        plan = plan_table(_table("值守安排表", _MATRIX_HEADER, rows))
        self.assertIn("merge_duplicate_rows:2", plan["actions"])
        self.assertEqual(len(plan["rows"]), 2)
        self.assertEqual(plan["metrics"]["duplicate_row_ratio"], 0.0)


class Tbl04BidderFillFormStaysAndGetsCompact(unittest.TestCase):
    def test_marked_form_survives_empty_cells_and_is_paired_into_fewer_rows(self):
        fields = [[f"字段{index}", "按项目要求填写"] for index in range(1, 13)]
        plan = plan_table(_table(f"设备巡检记录表{BIDDER_FILL_MARK}", ["字段", "填写要求"], fields))
        self.assertTrue(plan["bidder_fill"])
        self.assertEqual(plan["verdict"], "bidder_fill")
        self.assertNotEqual(plan["action"], "drop")
        self.assertEqual(plan["metrics"]["columns"], 4)
        self.assertEqual(len(plan["rows"]), 6, "两列并置省一半行，内容一格不少")
        self.assertIn("pair_bidder_fill_columns:12->6rows", plan["actions"])
        self.assertEqual(sum(len(row) for row in plan["rows"]), 24)

    def test_unmarked_all_blank_table_is_still_gone(self):
        plan = plan_table(_table("设备台账", ["资产编号", "型号", "数量"], [["", "", ""]]))
        self.assertEqual(plan["action"], "drop")

    def test_mark_reaches_the_rendered_document(self):
        artifact = _artifact([{"heading": "记录表单", "level": 1, "paragraphs": [_paragraph(2)],
                               "tables": [_table(f"客服回访记录表{BIDDER_FILL_MARK}",
                                                 ["字段", "填写要求"],
                                                 [[f"字段{index}", "按项目要求填写"] for index in range(1, 12)])]}])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            self.assertEqual(ledger["tables_rendered"], 1)
            self.assertEqual(ledger["tables_bidder_fill"], 1)
            audit = audit_docx_tables(docx)
            self.assertEqual(audit["tables_bidder_fill"], 1)
            self.assertEqual(audit["tables_empty_scaffold"], 0)


class Tbl05DeliveryGate(unittest.TestCase):
    def _docx_with_blank_table(self, root: Path) -> Path:
        """绕过装配端，直接造一张已交付形态的空表 —— 门必须自己抓得住。"""
        from docx import Document
        doc = Document()
        doc.add_heading("交付门试炼", level=1)
        doc.add_paragraph(_paragraph(3))
        table = doc.add_table(rows=4, cols=4)
        for index, value in enumerate(_MATRIX_HEADER[:4]):
            table.rows[0].cells[index].text = value
        target = root / "blank_tables.docx"
        doc.save(str(target))
        return target

    def test_blank_table_in_the_artifact_is_measured_and_cannot_complete_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docx = self._docx_with_blank_table(root)
            audit = audit_docx_tables(docx)
            self.assertEqual(audit["tables_empty_scaffold"], 1)
            self.assertEqual(audit["empty_cell_ratio_overall"], 1.0)
            qa = evaluate_docx_artifact(docx)
            rules = {row["rule_id"] for row in qa["findings"]}
            self.assertIn("TABLE_EMPTY_SCAFFOLD", rules)
            for row in qa["findings"]:
                if str(row["rule_id"]).startswith("TABLE_"):
                    self.assertEqual(row["severity"], "AUTO_REPAIR",
                                     "表格不达标不得把文件判成没有文件")
            self.assertEqual(qa["tables"]["tables_rendered"], 1)
            decision = classify_fail_soft(artifact_path=docx, artifact_qa=qa)
            self.assertEqual(decision["delivery_status"], DELIVERY_REVIEW)
            self.assertTrue(decision["artifact_available"])

    def test_clean_tables_produce_no_table_findings(self):
        artifact = _artifact([{"heading": "作业标准", "level": 1, "paragraphs": [_paragraph(4)],
                               "tables": [_table("里程碑作业标准表", _MATRIX_HEADER,
                                                 [_REAL_ROW,
                                                  ["首期交底", "开工前完成书面交底并签认", "项目总工", "甲方代表", "未交底不得开工"]])]}])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            self.assertEqual(ledger["tables_dropped"], 0)
            self.assertEqual(ledger["tables_rendered"], 1)
            qa = evaluate_docx_artifact(docx, brief=BRIEF, run_dir=docx.parent)
            self.assertEqual([row for row in qa["findings"]
                              if str(row["rule_id"]).startswith("TABLE_")], [])
            self.assertGreater(qa["tables"]["meaningful_table_chars"], 0)


class Tbl06PageFactsComeFromPages(unittest.TestCase):
    def test_effective_content_pages_excludes_near_blank_pages(self):
        density = page_density(["x" * 900, "", "y" * 400, "z" * 45])
        self.assertEqual(density["rendered_pages"], 4)
        self.assertEqual(density["thin_page_count"], 1)
        self.assertEqual(density["effective_content_pages"], 3)
        self.assertEqual(density["meaningful_page_ratio"], 0.75)

    def test_the_cut_is_the_declared_threshold_not_a_guess(self):
        # 线画在哪要说得出来：一页 40 字以下就是"这页没有内容"。
        self.assertEqual(MIN_PAGE_CONTENT_CHARS, 40)
        self.assertEqual(page_density(["字" * (MIN_PAGE_CONTENT_CHARS - 1)])["effective_content_pages"], 0)
        self.assertEqual(page_density(["字" * MIN_PAGE_CONTENT_CHARS])["effective_content_pages"], 1)


if __name__ == "__main__":
    unittest.main()
