"""MMF-007C Round 3 §34-§39：P0-4 FORM / TABLE GOVERNANCE。

上一轮 156 张表里有 31 张表单模板，表体写着 162 个"按项目要求填写"。量尺当时把这句
占位话当成有效内容，于是 156 张表在报告里全都"没问题"。这里钉住四件事：

1. §35 两种口径：正文表格必须有实际技术内容，表单模板允许待填写字段但必须紧凑；
   口径由 CU 的 archetype / 结构信号决定，不许用"登记表/台账"这类表题词钻空子。
2. §36/§61 九要素（表单名称/适用场景/关键字段/填写要求/责任岗位/触发条件/保存位置/
   保存期限/复核要求）：缺就点名，绝不拿占位话把格子填满 —— 填满等于把缺陷测量抹平。
3. §37 一张表单只展示 3～6 行示例结构，溢出字段进表下"字段说明"，不做 20 行空表撑页。
4. §38/§39 报告按 Tables Total / Meaningful Tables / Form Templates / Placeholder
   Tables / Low-Information Tables 分开数；占位表占比过高只拉到 NEEDS_REVIEW，不整篇 FAIL。

§49 的 TABLE-01 / TABLE-02 / TABLE-03 对应下面前三个 TestCase。
"""
from __future__ import annotations

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

from docx import Document

from delivery_state import DELIVERY_REVIEW, classify_fail_soft
from governance.final_artifact_qa import evaluate_docx_artifact
from governance.table_qa import (BIDDER_FILL_MARK, DEFAULT_FORM_NAME, FORM_ELEMENTS,
                                 FORM_ELEMENT_V03_MAPPING, FORM_EXAMPLE_ROW_MAX,
                                 FORM_PAYLOAD_SPEC, audit_docx_tables, classify_cell,
                                 classify_table, form_elements_present, form_template_table,
                                 is_form_table, plan_table, table_findings, table_metrics)
from longform.envelope import envelope_contract_lines
from longform.factory import LongformGenerationFactory
from providers.mock import MockProvider
from tests.test_mmf006e_r21_q01_q04 import _minimal_factory
from tests.test_mmf007c_table_density import (BRIEF, _MATRIX_HEADER, _REAL_ROW, _artifact,
                                              _build, _paragraph, _table)

_CONTENT_ROW = ["地库排水沟", "每季度清掏一次，雨季前加做一次", "工程维修领班", "品质主管", "堵塞即报即清"]
_CLAIM_FREE_ROW_A = ["进场动员会", "会后形成纪要并分发各班组", "项目经理", "工程部经理", "补开会议"]
_CLAIM_FREE_ROW_B = ["地库排水沟", "清掏后出水畅通、无淤积", "工程维修领班", "品质主管", "现场即报即清"]

_REPORT_BUCKETS = ("tables_total", "tables_meaningful", "tables_form_templates",
                   "tables_placeholder", "tables_low_information", "placeholder_table_share")


def _blank_tables(table_count: int, rows_per_table: int = 4):
    """直接造交付形态的空表，绕开装配端 —— 交付门必须自己抓得住。"""
    doc = Document()
    doc.add_heading("表格门试炼", level=1)
    doc.add_paragraph(_paragraph(3))
    for _ in range(table_count):
        _add_table(doc, _MATRIX_HEADER, [[""] * len(_MATRIX_HEADER)] * (rows_per_table - 1))
    return doc


def _add_table(doc, columns: list[str], rows: list[list[str]]):
    table = doc.add_table(rows=len(rows) + 1, cols=len(columns))
    for index, value in enumerate(columns):
        table.rows[0].cells[index].text = value
    for row, values in zip(table.rows[1:], rows):
        for index, cell in enumerate(row.cells[:len(values)]):
            cell.text = values[index]
    return table


class Tbl01MeaningfulTable(unittest.TestCase):
    """§49 TABLE-01：正文表格必须有实际技术内容。"""

    def test_content_table_renders_untouched_and_counts_as_meaningful(self):
        plan = plan_table(_table("排水与防汛作业标准表", _MATRIX_HEADER, [_REAL_ROW, _CONTENT_ROW]))
        self.assertFalse(plan["is_form"])
        self.assertEqual(plan["action"], "render")
        self.assertEqual(plan["verdict"], "meaningful")
        self.assertEqual(plan["metrics"]["meaningful_cell_ratio"], 1.0)

    def test_row_label_matrix_is_measured_not_exempted(self):
        # §35：内容型 archetype 走严格口径 —— 只有行标签、格子全空的矩阵仍然是空壳。
        label_rows = [[f"风险{index}", "", "", "", ""] for index in range(1, 9)]
        plan = plan_table({"title": "运行风险矩阵", "columns": _MATRIX_HEADER,
                           "rows": label_rows, "archetype": "MATRIX"})
        self.assertFalse(plan["is_form"], "风险矩阵被当成表单就等于拿到了免检牌")
        self.assertEqual(plan["metrics_before"]["empty_cells"], 32)
        self.assertNotEqual(plan["action"], "drop", "行标签是对招标需求的响应，删不得")
        self.assertEqual(plan["metrics"]["empty_cells"], 0, "空格必须被压掉而不是留着")

    def test_form_detection_uses_structure_not_title_words(self):
        # "登记表/台账"这类词在正文表格标题上太常见，按词判会让真表躲开空格门。
        self.assertFalse(is_form_table("保洁质量检查登记表", ["检查项", "标准", "记录"]))
        self.assertTrue(is_form_table("保洁质量检查表", ["字段", "填写规则", "责任岗位", "记录频次"]))
        self.assertTrue(is_form_table("任何标题", [], archetype="FORM_TEMPLATE"))
        self.assertTrue(is_form_table(f"任何标题{BIDDER_FILL_MARK}", ["检查项", "记录"]))

    def test_meaningful_table_produces_no_table_findings(self):
        artifact = _artifact([{"heading": "作业标准", "level": 1, "paragraphs": [_paragraph(4)],
                               "tables": [_table("里程碑作业标准表", _MATRIX_HEADER,
                                                 [_REAL_ROW, _CONTENT_ROW,
                                                  ["首期交底", "开工前完成书面交底并签认", "项目总工",
                                                   "甲方代表", "未交底不得开工"]])]}])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            self.assertEqual(ledger["tables_dropped"], 0)
            qa = evaluate_docx_artifact(docx, brief=BRIEF, run_dir=docx.parent)
            self.assertEqual([row for row in qa["findings"] if str(row["rule_id"]).startswith(("TABLE_", "FORM_"))], [])


class Tbl02FormTemplate(unittest.TestCase):
    """§49 TABLE-02：表单模板允许待填写字段，但九要素要齐、篇幅要紧凑、缺项要留痕。"""

    def test_nine_elements_render_as_example_rows_plus_field_notes(self):
        built = form_template_table({
            "form_name": "设备巡检记录表", "purpose": "记录设备运行状态并支持追溯",
            "fields": [f"字段{index}" for index in range(1, 15)],
            "field_rules": {f"字段{index}": f"填写第{index}项实测读数" for index in range(1, 15)},
            "approval": "专业负责人复核", "frequency": "每班一次", "retention": "归档保存两年",
            "storage_location": "项目部资料室归档", "review": "品质主管抽查",
        })
        self.assertEqual(built["archetype"], "FORM_TEMPLATE")
        self.assertLessEqual(built["rows_rendered"], FORM_EXAMPLE_ROW_MAX)
        self.assertGreaterEqual(built["rows_rendered"], 3)
        self.assertEqual(built["fields_total"], 14)
        self.assertEqual(built["fields_overflow"], 8)
        self.assertEqual(built["form_elements_missing"], [])
        self.assertEqual(set(built["form_elements"]), set(FORM_ELEMENTS))
        # 溢出的是字段名，只能进表下说明，不能凭空消失。
        self.assertIn("其余字段：字段7、字段8", built["notes"])
        self.assertNotIn("按项目要求填写", built["notes"])

    def test_missing_elements_stay_blank_and_are_named(self):
        # 招标文件没给填写要求、责任岗位、触发条件、保存位置和复核要求时，正确做法是
        # 留空 + 记缺陷，不是编一个。
        built = form_template_table({
            "form_name": "投诉处理登记表", "purpose": "记录投诉受理与闭环",
            "fields": ["投诉人", "联系电话", "投诉内容", "处理结果"], "retention": "一年",
        })
        self.assertEqual(built["form_elements_missing"],
                         ["填写要求", "责任岗位", "触发条件", "保存位置", "复核要求"])
        self.assertNotIn("适用场景", built["form_elements_missing"])
        self.assertNotIn("保存期限", built["form_elements_missing"])
        flat = "；".join("".join(row) for row in built["rows"]) + built["notes"]
        self.assertNotIn("按项目要求填写", flat)
        self.assertNotIn("待定", flat)

    def test_form_is_never_judged_by_blank_cell_ratio(self):
        # §35/§36：待填写格子是这张表的用途，不是缺陷；缺陷只有"撑页"和"要素不齐"两种。
        built = form_template_table({"form_name": "交接班记录表", "fields": ["交接事项", "遗留问题"]})
        metrics = table_metrics(built["columns"], built["rows"], title=built["title"],
                                archetype="FORM_TEMPLATE")
        self.assertTrue(metrics["is_form"])
        self.assertGreater(metrics["blank_or_placeholder_ratio"], 0.5, "空格确实很多，但它不是缺陷")
        # 装配端给每张表单都盖了【待投标人填写】，所以口径名是 bidder_fill；两者都不是缺陷类。
        self.assertIn(classify_table(metrics), {"form_template", "bidder_fill"})
        unmarked = table_metrics(built["columns"], built["rows"], title="交接班记录表",
                                 archetype="FORM_TEMPLATE")
        self.assertEqual(classify_table(unmarked), "form_template")

    def test_bloated_form_is_capped_and_overflow_moves_to_notes(self):
        plan = plan_table({"title": f"保洁作业记录表{BIDDER_FILL_MARK}",
                           "columns": ["字段", "填写规则"],
                           "rows": [[f"字段{index}", f"填写第{index}项"] for index in range(1, 25)],
                           "archetype": "FORM_TEMPLATE"})
        self.assertTrue(plan["is_form"])
        self.assertLessEqual(len(plan["rows"]), FORM_EXAMPLE_ROW_MAX)
        self.assertEqual(plan["fields_overflow"], 12, "溢出字段数必须记账，不能凭空丢掉")
        self.assertIn("其余字段：字段13", plan["notes"])
        self.assertTrue(any(action.startswith("cap_form_rows:") for action in plan["actions"]))

    def test_notes_survive_assembly_and_the_nine_elements_read_back(self):
        """装配写下去的字段说明，交付端必须读得回来 —— 否则九要素在真实产物上永远不齐。"""
        artifact = _artifact([{"heading": "记录表单", "level": 1, "paragraphs": [_paragraph(2)],
                               "tables": [_table(f"客服回访记录表{BIDDER_FILL_MARK}", ["字段", "填写规则"],
                                                 [[f"字段{index}", f"回访当日填写第{index}项"]
                                                  for index in range(1, 20)])]}])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            form = next(row for row in ledger["ledger"] if row["title"] == f"客服回访记录表{BIDDER_FILL_MARK}")
            self.assertTrue(form["is_form"])
            self.assertLessEqual(form["rendered_rows"], FORM_EXAMPLE_ROW_MAX)
            self.assertTrue(form["notes"])
            audit = audit_docx_tables(docx)
            measured = next(row for row in audit["per_table"] if row["is_form"])
            self.assertLessEqual(measured["rows"], FORM_EXAMPLE_ROW_MAX)
            self.assertEqual(audit["tables_form_templates"], 1)
            self.assertEqual(audit["tables_rows_over_form_limit"], 0)
            self.assertIn("其余字段", measured["form_notes"], "表下说明没落到 Word 里")
            self.assertEqual(measured["form_elements_missing"],
                             ["适用场景", "责任岗位", "触发条件", "保存位置", "保存期限", "复核要求"])
            rules = {row["rule_id"] for row in table_findings(audit, path=docx)}
            self.assertIn("FORM_ELEMENTS_INCOMPLETE", rules)
            self.assertNotIn("FORM_TEMPLATE_BLOAT", rules)

    def test_bloated_form_that_bypassed_assembly_is_caught(self):
        doc = _blank_tables(0)
        _add_table(doc, ["字段", "填写要求"], [[f"字段{index}", "当场填写并签字确认"] for index in range(1, 20)])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bloated_form.docx"
            doc.save(str(target))
            audit = audit_docx_tables(target)
            self.assertEqual(audit["tables_form_templates"], 1)
            self.assertEqual(audit["tables_rows_over_form_limit"], 1)
            self.assertEqual(audit["tables_placeholder"], 0, "填写规则列是真内容，不是占位话")
            rules = {row["rule_id"] for row in table_findings(audit, path=target)}
            self.assertIn("FORM_TEMPLATE_BLOAT", rules)
            self.assertNotIn("TABLE_EMPTY_SCAFFOLD", rules, "有字段名的表单不该按空壳判")

    def test_form_filled_with_placeholder_prose_is_still_a_defect(self):
        """§36：允许空着待填写，不允许拿"按项目要求填写"把格子填满。

        上一轮 31 张表单就是这个形状，所以它必须同时进 Form Templates 和
        Placeholder Tables 两个桶 —— 只进一个就是换口径掩盖。形态判定（表单就是表单）
        与质量判定（套话填格是缺陷）是两条线，不能互相遮蔽。
        """
        for marked in (False, True):
            doc = _blank_tables(0)
            title = f"设备巡检记录表{BIDDER_FILL_MARK}" if marked else "设备巡检记录表"
            doc.add_heading(title, level=2)
            _add_table(doc, ["字段", "填写要求"],
                       [[f"字段{index}", "按项目要求填写"] for index in range(1, 20)])
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "legacy_form.docx"
                doc.save(str(target))
                audit = audit_docx_tables(target)
                measured = audit["per_table"][0]
                self.assertTrue(measured["is_form"], "字段/填写要求 两列就是表单形状")
                self.assertTrue(measured["placeholder_prose_heavy"])
                self.assertIn(measured["classification"], {"form_template", "bidder_fill"})
                self.assertEqual(audit["tables_form_templates"], 1)
                self.assertEqual(audit["tables_placeholder"], 1, f"marked={marked}")
                self.assertGreater(audit["placeholder_table_share"], 0.15)
                qa = evaluate_docx_artifact(target)
                self.assertEqual(qa["status"], "NEEDS_REVIEW", f"marked={marked}")
                rules = {row["rule_id"] for row in qa["findings"]}
                self.assertIn("FORM_ELEMENTS_INCOMPLETE", rules, f"marked={marked}")
                self.assertIn("TABLE_PLACEHOLDER_HEAVY", rules, f"marked={marked}")
                missing = next(row for row in qa["findings"] if row["rule_id"] == "FORM_ELEMENTS_INCOMPLETE")
                self.assertIn("填写要求", missing["missing_elements"])


class Tbl03EmptyPlaceholderTable(unittest.TestCase):
    """§49 TABLE-03：空占位表不再被当成有效内容，也不靠它撑篇幅。"""

    def test_the_previous_round_placeholder_phrase_is_not_content(self):
        # 这一条就是量尺本身的修复：上一轮 162 个"按项目要求填写"全被算成了有效内容。
        self.assertEqual(classify_cell("按项目要求填写"), "placeholder")
        metrics = table_metrics(["字段", "填写要求"],
                                [[f"字段{index}", "按项目要求填写"] for index in range(1, 21)])
        self.assertEqual(metrics["content_cells"], 20, "只有字段名那一列是真内容")
        self.assertEqual(metrics["placeholder_cells"], 20)
        self.assertEqual(metrics["meaningful_cell_ratio"], 0.5)
        body_only = table_metrics(["项目", "标准", "记录"],
                                  [["按项目要求填写"] * 3 for _ in range(5)])
        self.assertEqual(body_only["content_cells"], 0)
        self.assertEqual(classify_table(body_only), "empty_scaffold")

    def test_all_placeholder_body_is_dropped_before_paper(self):
        plan = plan_table(_table("表 6-1 服务标准", ["项目", "标准", "记录"],
                                 [["按项目要求填写"] * 3 for _ in range(10)]))
        self.assertEqual(plan["action"], "drop")
        self.assertIn("无有效内容", plan["reason"])

    def test_untouched_blank_grid_is_measured_and_gated_not_deleted(self):
        doc = _blank_tables(1)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blank_tables.docx"
            doc.save(str(target))
            audit = audit_docx_tables(target)
            self.assertEqual(audit["tables_placeholder"], 1)
            qa = evaluate_docx_artifact(target)
            self.assertEqual(qa["tables"]["tables_placeholder"], 1)
            self.assertEqual(qa["status"], "NEEDS_REVIEW", "表格缺陷只能拉到待复核")
            decision = classify_fail_soft(artifact_path=target, artifact_qa=qa)
            self.assertEqual(decision["delivery_status"], DELIVERY_REVIEW)
            self.assertTrue(decision["artifact_available"], "有缺陷也要交付文件，不能判成没有文件")

    def test_low_information_table_is_counted_separately(self):
        # 不空，但整张表可核查字数太少 —— §38 要单独有一个 Low-Information 口径。
        metrics = table_metrics(_MATRIX_HEADER, [_REAL_ROW])
        self.assertTrue(metrics["low_information"])
        self.assertEqual(classify_table(metrics), "meaningful")


class Tbl04QualityReportAndRatioGate(unittest.TestCase):
    """§38 / §39：报告分桶 + 占比门。"""

    def test_report_splits_tables_into_the_five_declared_buckets(self):
        artifact = _artifact([
            {"heading": "作业标准", "level": 1, "paragraphs": [_paragraph(1)],
             "tables": [_table("里程碑作业标准表", _MATRIX_HEADER, [_REAL_ROW, _CONTENT_ROW])]},
            {"heading": "记录表单", "level": 1, "paragraphs": [_paragraph(2)],
             "tables": [_table("设备巡检记录表", ["字段", "填写规则"],
                               [[f"字段{index}", f"当日填写第{index}项"] for index in range(1, 9)])]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            audit = audit_docx_tables(docx)
            for key in _REPORT_BUCKETS:
                self.assertIn(key, audit, f"§38 交付端报告缺 {key}")
            self.assertEqual(audit["tables_total"], audit["tables_rendered"])
            self.assertEqual(audit["tables_form_templates"], 1)
            self.assertEqual(audit["tables_placeholder"], 0)
            self.assertEqual(audit["tables_meaningful"], 1)
            self.assertLess(audit["placeholder_table_share"], 0.15)
            # 装配台账同一口径，交付前就能看到表单缺哪几项。
            for key in ("tables_form_templates", "tables_low_information",
                        "tables_form_elements_incomplete", "placeholder_table_share"):
                self.assertIn(key, ledger, f"§38 装配台账缺 {key}")
            self.assertEqual(ledger["tables_form_templates"], 1)
            self.assertEqual(ledger["tables_planned"], 2)

    def test_high_placeholder_share_reviews_the_document_without_failing_it(self):
        doc = _blank_tables(3)
        # 夹具里避带数字与时限的措辞：承诺溯源门（C1/C8）会对无据数字出 TRUE_BLOCK，
        # 那是另一条门的正确行为，混进来就测不到"表格占比过高只到待复核"这一条。
        _add_table(doc, list(_MATRIX_HEADER), [_CLAIM_FREE_ROW_A, _CLAIM_FREE_ROW_B])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ratio.docx"
            doc.save(str(target))
            audit = audit_docx_tables(target)
            self.assertGreater(audit["placeholder_table_share"], 0.15)
            findings = table_findings(audit, path=target)
            ratio = [row for row in findings if row["rule_id"] == "TABLE_PLACEHOLDER_RATIO"]
            self.assertEqual(len(ratio), 1)
            self.assertEqual(ratio[0]["severity"], "AUTO_REPAIR")
            qa = evaluate_docx_artifact(target)
            self.assertEqual(qa["status"], "NEEDS_REVIEW", "§39：占比过高只到待复核，不整篇 FAIL")
            self.assertEqual(qa["summary"]["block_count"], 0)


class Tbl05ProviderIsAskedForNineElements(unittest.TestCase):
    """§36/§61：装配端渲染得出来，前提是三种请求形态都把九要素要到过。"""

    _CONTEXT = {"section_contract": {
        "section_id": "S09-01", "section_title": "记录表单", "must_cover": ["巡检记录"],
        "target_words": {"min": 80, "max": 200}, "source_requirements": ["REQ-0009"],
        "required_processes": []}}

    def test_the_nine_element_authority_is_named_and_asked_for(self):
        """§61：要素清单只有一份，而且向 Provider 要的那份必须点得到每一行。

        只测"清单有九项"是不够的：装配端读得回来、但提示词里没要到过，就是永远缺两项。
        """
        self.assertEqual(len(FORM_ELEMENTS), 9)
        self.assertEqual(list(FORM_ELEMENTS),
                         ["表单名称", "适用场景", "关键字段", "填写要求", "责任岗位",
                          "触发条件", "保存位置", "保存期限", "复核要求"])
        for element in FORM_ELEMENTS:
            self.assertIn(element, FORM_PAYLOAD_SPEC, f"契约句里没点到 {element}")
        # 上一轮的六个名字必须还能映射回来，否则历史 Run 的表会被新口径判成缺项。
        self.assertEqual(set(FORM_ELEMENT_V03_MAPPING), set(FORM_ELEMENTS))
        self.assertEqual(FORM_ELEMENT_V03_MAPPING["填写要求"], "填写规则")
        self.assertEqual(FORM_ELEMENT_V03_MAPPING["触发条件"], "记录频次")
        self.assertEqual(FORM_ELEMENT_V03_MAPPING["适用场景"], "用途")
        for element in ("表单名称", "保存位置", "复核要求"):
            self.assertEqual(FORM_ELEMENT_V03_MAPPING[element], "", f"{element} 是 §61 新增项")

    def test_section_json_prompt_carries_the_spec(self):
        self.assertIn(FORM_PAYLOAD_SPEC, LongformGenerationFactory._word_prompt(self._CONTEXT)["prompt"])

    def test_batch_json_prompt_carries_the_spec(self):
        factory = _minimal_factory(Path(tempfile.mkdtemp()))
        self.assertIn(FORM_PAYLOAD_SPEC, factory._batch_prompt([self._CONTEXT], batch_id="GB-09")["prompt"])

    def test_envelope_prompt_carries_the_spec_only_when_a_form_is_requested(self):
        forms = envelope_contract_lines([{"content_unit_id": "CU-1", "archetype": "FORM_TEMPLATE"}])
        self.assertIn(FORM_PAYLOAD_SPEC, "\n".join(forms))
        others = envelope_contract_lines([{"content_unit_id": "CU-2", "archetype": "KPI_TABLE"}])
        self.assertNotIn(FORM_PAYLOAD_SPEC, "\n".join(others))

    def test_provider_payload_goes_through_the_real_assembly_seam(self):
        """生产链路里 Provider 回来的 FORM_TEMPLATE payload 必须走 form_template_table。

        这一段是 §36/§37 的真正落点：装配片段决定 Section 里那张表长什么样，
        它如果还按旧的"字段 × 套话"铺行，后面所有门都只是在给撑页的表记账。
        """
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            factory = _minimal_factory(Path(tmp))
            factory.cus_by_section = {"S09-01": ["CU-1"]}
            cus_dir = factory.word_root / "sections" / "S09-01" / "cus"
            cus_dir.mkdir(parents=True, exist_ok=True)
            (cus_dir / "CU-1.json").write_text(_json.dumps({
                "content_unit_id": "CU-1", "archetype": "FORM_TEMPLATE",
                "content": "巡检记录按字段留痕。",
                "generation_origin": "provider_content_unit",
                "payload": {"form_name": "设备巡检记录表", "purpose": "记录巡检并支持追溯",
                            "fields": [f"字段{index}" for index in range(1, 13)],
                            "field_rules": {f"字段{index}": f"填写第{index}项" for index in range(1, 13)},
                            "approval": "专业负责人复核", "frequency": "每班一次", "retention": "两年",
                            "storage_location": "项目部资料室归档", "review": "品质主管抽查"},
            }, ensure_ascii=False), encoding="utf-8")
            assembled, issues = factory._assemble_section_from_cus("S09-01", {"tables": []})
            self.assertEqual(issues, [])
            tables = assembled.get("tables") or []
            self.assertEqual(len(tables), 1)
            built = tables[0]
            self.assertEqual(built["archetype"], "FORM_TEMPLATE")
            self.assertLessEqual(len(built["rows"]), FORM_EXAMPLE_ROW_MAX)
            self.assertEqual(built["fields_total"], 12)
            self.assertIn("其余字段：字段7", built["notes"])
            self.assertEqual(built["form_elements_missing"], [])
            plan = plan_table(built)
            self.assertTrue(plan["is_form"])
            self.assertEqual(len(plan["rows"]), 6)

    def test_provider_payload_reaches_a_complete_form(self):
        # 六要素在真实链路里要真能凑齐，否则上面的"点名要"只是一句空话。
        provider = MockProvider({"provider_name": "mock", "provider_type": "mock",
                                 "enabled": True, "model": "mock"})
        fragment = provider._section_fragment({
            "section_id": "S09-01",
            "content_unit_contracts": [{"content_unit_id": "CU-1", "archetype": "FORM_TEMPLATE",
                                        "title": "设备巡检记录表", "intent": "记录巡检",
                                        "must_cover": ["巡检字段"],
                                        "target_budget": {"fields": 8, "effective_chars": 400}}],
        })
        rows = fragment.get("content_unit_fragments") or []
        self.assertEqual(len(rows), 1)
        payload = rows[0].get("payload") or {}
        self.assertTrue(payload.get("fields"), "mock 必须给出字段，否则测不到表单链")
        built = form_template_table(payload)
        self.assertEqual(built["form_elements_missing"], [])
        self.assertLessEqual(built["rows_rendered"], FORM_EXAMPLE_ROW_MAX)


class Tbl06ElementEvidence(unittest.TestCase):
    """要素判据：列名在但整列是占位话，不算齐 —— 那正是上一轮的形状。"""

    def test_column_label_without_content_is_not_an_element(self):
        present = form_elements_present(["关键字段", "填写要求", "责任岗位", "触发条件"], "",
                                        [["设备编号", "按项目要求填写", "/", ""]],
                                        title="设备巡检记录表")
        self.assertTrue(present["关键字段"])
        self.assertFalse(present["填写要求"])
        self.assertFalse(present["责任岗位"])
        self.assertFalse(present["触发条件"])

    def test_legacy_column_wordings_still_map_to_the_nine_elements(self):
        """Round 3 的表还挂着 字段名/填写规则/记录频次/用途 的旧列名，量尺要认得回来。

        不认旧名就会把"上一轮的表其实已经写过填写规则和频次"报成缺项 —— 那是把
        口径升级说成内容缺陷，两类问题会被混成同一批 FORM_* 门。
        """
        present = form_elements_present(["字段", "填写规则", "责任岗位", "记录频次"],
                                        "用途：设备运行追溯",
                                        [["设备编号", "填台账编号", "专业负责人", "每班"]],
                                        title="设备巡检记录表")
        self.assertTrue(present["关键字段"])
        self.assertTrue(present["填写要求"])
        self.assertTrue(present["触发条件"])
        self.assertTrue(present["适用场景"])
        self.assertFalse(present["保存位置"], "§61 新增要素：旧表没写过就是没写过")
        self.assertFalse(present["复核要求"])

    def test_value_written_in_field_notes_counts_once(self):
        present = form_elements_present(["字段", "填写规则"],
                                        "共 14 个字段；其余字段：字段7、字段8；责任岗位：专业负责人",
                                        [["设备编号", "填写台账编号"], ["运行状态", "记录异常读数"]],
                                        title="设备巡检记录表")
        self.assertTrue(present["责任岗位"])
        self.assertFalse(present["适用场景"], "只有标签没有值的句子不能算要素")
        self.assertFalse(present["保存期限"])

    def test_default_form_name_is_not_an_element(self):
        # 表题兜底成"业务记录表"时不能反过来给"表单名称"记一分：那是量尺自己造的字。
        present = form_elements_present(["关键字段"], "", [["设备编号"]],
                                        title=DEFAULT_FORM_NAME)
        self.assertFalse(present["表单名称"])


class Tbl07StructureCountsReachTheReport(unittest.TestCase):
    """上一轮真实 300 页文档的结构报告一路报 forms=0 / matrices=0 —— 那两个键从没被加过数。"""

    def test_rendered_forms_and_matrices_are_counted(self):
        from governance.document_qa import count_artifact_structure

        form = form_template_table({"form_name": "设备巡检记录表", "purpose": "追溯",
                                    "fields": ["设备编号", "运行状态", "异常描述"],
                                    "field_rules": {"设备编号": "填台账编号", "运行状态": "填实测值",
                                                    "异常描述": "无异常填无"},
                                    "approval": "专业负责人", "frequency": "每班", "retention": "两年"})
        artifact = {"sections": [
            {"heading": "记录表单", "tables": [form]},
            {"heading": "责任矩阵", "tables": [{"title": "接口矩阵", "archetype": "RESPONSIBILITY_MATRIX",
                                              "columns": list(_MATRIX_HEADER), "rows": [_CLAIM_FREE_ROW_A]}]},
        ]}
        counts = count_artifact_structure(artifact)
        self.assertEqual(counts["forms"], 1, "表单数出来了才算量尺修好")
        self.assertEqual(counts["matrices"], 1)
        self.assertEqual(counts["tables"], 2)


class Tbl08TwoEndsAgreeOnTheFiveBuckets(unittest.TestCase):
    """§38 的同一栏在装配端与交付端必须是同一个数。

    此前装配端只按 `verdict` 数 Placeholder，不看 `placeholder_prose_heavy`，于是同一份文件
    可以一边报 3、一边报 34；`tables_meaningful` 还把待填写表单算成技术内容。修的是量尺一致性，
    不是把两边各自改成好看的数。
    """

    def test_build_ledger_and_delivered_audit_report_the_same_buckets(self):
        prose_form = _table(f"客服回访记录表{BIDDER_FILL_MARK}", ["字段", "填写要求"],
                            [[f"字段{index}", "按项目要求填写"] for index in range(1, 9)])
        six_element = form_template_table(
            {"form_name": "设备巡检记录表", "purpose": "追溯异常", "retention": "两年",
             "approval": "专业负责人", "frequency": "每班",
             "fields": ["设备编号", "运行状态", "异常描述", "处置人", "复核人", "关闭时间", "备注"],
             "field_rules": {"设备编号": "填台账编号", "运行状态": "填实测值"}})
        artifact = _artifact([{"heading": "记录与矩阵", "level": 1, "paragraphs": [_paragraph(2)],
                               "tables": [_table("里程碑作业标准表", _MATRIX_HEADER,
                                                 [_CONTENT_ROW, _CLAIM_FREE_ROW_A, _CLAIM_FREE_ROW_B]),
                                          prose_form, six_element]}])
        with tempfile.TemporaryDirectory() as tmp:
            docx, ledger = _build(Path(tmp), artifact)
            audit = audit_docx_tables(docx)
            self.assertEqual(ledger["tables_dropped"], 0, "三张表都应活到纸上，否则两边分母不同")
            self.assertEqual(ledger["tables_rendered"], audit["tables_total"])
            for key in _REPORT_BUCKETS[1:]:
                self.assertEqual(ledger[key], audit[key], f"装配端与交付端 {key} 不一致")
            # 这一条是被修掉的缺陷本体：填满足话的表单在两端都必须进 Placeholder 桶。
            self.assertEqual(audit["tables_placeholder"], 1)
            self.assertEqual(ledger["tables_placeholder"], 1)
            self.assertEqual(ledger["tables_meaningful"], 1, "两张表单不算技术内容表")
            self.assertEqual(ledger["tables_form_templates"], 2)
            self.assertGreater(ledger["placeholder_table_share"], 0.15)


if __name__ == "__main__":
    unittest.main()
