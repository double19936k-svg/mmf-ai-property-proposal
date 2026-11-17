"""Final editorial facts, conservative deletion, and canonical process roles."""
import json
import zipfile

import pytest
from docx import Document

from governance.commitment_provenance import evaluate_commitments, apply_local_repairs
from governance.final_editorial import SourceFacts, edit_text, run_final_editorial
from longform.factory import _repair_customer_text
from longform.process_integrity import process_integrity, visual_output_present


@pytest.fixture
def facts():
    return SourceFacts({"tables": [{"table_id": "T-STAFF", "columns": ["岗位", "人员配置（人）"],
                       "rows": [["项目主管", "1"], ["工程", "6"], ["秩序维护", "7"], ["前台", "2"],
                                ["室内保洁", "4"], ["厨师", "3"], ["面点师", "2"], ["打荷", "2"], ["合计", "27"]]}],
                        "paragraphs": [{"para_id": "P-AREA", "text": "建筑面积9163平方米"},
                                       {"para_id": "P-TIME", "text": "巡视每2小时一次"}]})


def test_source_staff_restoration_and_preservation(facts):
    text = "服务人员合计按现场条件配置的人员，工程按现场条件配置的人员，8类岗位。"
    edited, audit = edit_text(text, facts)
    assert "27人" in edited and "工程6人" in edited
    assert len(audit["numeric_bindings"]) == 2
    assert _repair_customer_text("工程6人，服务人员合计27人。", facts) == "工程6人，服务人员合计27人。"
    assert "99人" not in _repair_customer_text("工程99人。", facts)
    assert "27人" not in edit_text("参演编组按按现场条件配置的人员岗位口径落到条线。", facts)[0]
    assert '4类岗位' in edit_text('按4类岗位安排值守，并在原有编制中轮换。', facts)[0]
    kitchen, audit = edit_text('厨师、面点师、打荷三条后厨作业线合计按现场条件配置的人员。', facts)
    assert kitchen == '厨师、面点师、打荷三条后厨作业线合计7人。'
    assert audit['numeric_bindings'][0]['derivation'] == '厨师+面点师+打荷'
    assert edit_text('后厨按厨师、面点师、打荷三条作业线组织，合计按现场条件配置的人员。', facts)[0].endswith('合计7人。')
    assert '27人' not in edit_text('参演人员共按现场条件配置的人员。', facts)[0]
    assert edit_text('项目主管即项目负责人按现场条件配置的人员。', facts)[0].endswith('项目负责人1人。')


def test_no_source_does_not_guess():
    result, _ = edit_text("服务人员合计按现场条件配置的人员。")
    assert "27" not in result and "以经确认的人员配置表为准" in result


def test_numeric_projection_keeps_source_conflict_and_literal_quotes():
    source = {"paragraphs": [
        {"para_id": "TECH", "heading_path": ["第四章 技术需求书"], "text": "建筑面积：9200平方米"},
        {"para_id": "CONTRACT", "heading_path": ["合同主要条款"], "text": "建筑面积：9200.8㎡"},
        {"para_id": "PATROL", "text": "每两个小时巡视一次"}]}
    catalog = SourceFacts(source)
    assert edit_text("物业服务面积为9200.8平方米。", catalog)[0] == "物业服务面积为9200平方米。"
    assert edit_text("合同范本原文建筑面积9200.8平方米。", catalog)[0].endswith("9200.8平方米。")
    comparison = "建筑面积9200平方米与9200.8平方米并存，登记为待认定事实。"
    assert edit_text(comparison, catalog)[0] == comparison
    cited = "办公用房建筑面积在需求书中记为9200平方米，公告中记为9200.8㎡。两处差异待确认。"
    assert edit_text(cited, catalog)[0] == cited
    assert len(catalog.source_conflicts) == 1
    assert source["paragraphs"][1]["text"] == "建筑面积：9200.8㎡"
    assert "2小时" in _repair_customer_text("每2小时巡视一次。", catalog)


@pytest.mark.parametrize("text, expected", [
    ("状态改记为已完成须同时具备使用人签认与现场复查两项记录。", "PASS"),
    ("洗消岗位交接当面点验，交代当班已完成批次与待复核专柜。", "PASS"),
    ("交接写明已完成的判断与已采取的控制措施。", "PASS"),
    ("关闭条件为：条目已修订，责任岗位已完成交接说明并签认。", "PASS"),
    ("岗位交接核验：岗位已完成交接说明并书面签认。", "PASS"),
    ("作业已完成但交办方未确认 | 完成栏有字而确认栏空白 | 保留开放状态并补录后重新确认", "PASS"),
    ("状态改记为已完成须签认，此外地下机房已安装自动灭火装置。", "BLOCK"),
    ("关闭条件为：条目已修订；此外地下机房已配备防爆照明装置。", "BLOCK"),
    ("设备核验：地下机房已安装自动灭火装置。", "BLOCK"),
    ("消防应急处置就近取用已配置灭火器材与应急照明。", "BLOCK"),
    ("优先调用现场已配置且经甲方确认的消防器材。", "BLOCK"),
    ("假设是否合理尚待核验且地下机房已安装自动灭火装置。", "BLOCK"),
    ("前提是否成立仍待确认，地下机房已配备防爆照明装置。", "BLOCK"),
    ("假设 A，则拟 B，此外地下机房已安装自动灭火装置。", "BLOCK"),
])
def test_existing_authority_process_roles(text, expected):
    assert evaluate_commitments({}, [], {"artifact": {"text": text}})["status"] == expected
    if expected == "BLOCK":
        assert "将安装" not in apply_local_repairs({"artifact": {"text": text}})["artifact"]["text"]


def test_pipeline_integrity_and_business_preservation(facts):
    result, _ = edit_text("本单元规定工程6人按按第二步巡查。所有记录以patrol_record编号归档。共7个字段；其余字段：复核意见。", facts)
    assert result == "工程6人按第二步巡查。所有记录以巡逻记录编号归档。补充记录项：复核意见。"
    assert edit_text(result, facts)[0] == result
    assert "第第二步" not in edit_text("按第第二步接办，并在第第四步完成移交。", facts)[0]


def test_exact_dedup_preserves_unique_action_form_and_native_fields(tmp_path, facts):
    path = tmp_path / "editorial.docx"
    doc = Document()
    doc.add_heading("工单登记与处理与关闭", 2)
    sentence = "工程负责人登记异常并填写工单，复验合格后由使用人签认并关闭工单。"
    doc.add_paragraph(sentence)
    doc.add_paragraph(sentence + "班内未完成的事项交给下一班持续跟踪。")
    doc.add_paragraph("工程负责人隔离漏电设备并记录动作时间，不得直接关闭工单。")
    doc.add_paragraph("联系人【待投标人确认：真实姓名与联系电话】")
    doc.add_paragraph("交接记录表【待投标人填写】")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "记录编号", "执行人"
    doc.save(path)
    with zipfile.ZipFile(path) as z:
        original_parts = {n: z.read(n) for n in z.namelist() if n != "word/document.xml"}
    report = run_final_editorial(path, facts=facts, output_dir=tmp_path)
    assert report["duplicate_cluster_count"] == 2
    text = "".join(p.text for p in Document(path).paragraphs)
    assert '工单登记、处理与关闭' in text
    assert "下一班持续跟踪" in text and "隔离漏电设备" in text
    import re
    assert re.sub(r"〔\d+〕", "", text).count(sentence) == 1
    assert '沿用本节〔1〕所述要求' in text
    assert report["human_input_index"]["unfilled_form_tables"] == 1
    assert report["human_input_index"]["entry_count"] == 4
    with zipfile.ZipFile(path) as z:
        assert all(z.read(name) == data for name, data in original_parts.items())
    assert run_final_editorial(path, facts=facts, output_dir=tmp_path)["duplicate_cluster_count"] == 0
    inline = tmp_path / 'inline.docx'
    document = Document()
    document.add_paragraph(sentence + sentence)
    document.save(inline)
    report = run_final_editorial(inline, facts=facts, output_dir=tmp_path / 'inline')
    assert report['duplicate_cluster_count'] == 2
    text = Document(inline).paragraphs[0].text
    assert '〔1〕工程负责人' in text and '沿用本节〔1〕所述要求' in text


def test_process_facets_require_operational_bindings():
    assert not process_integrity("触发条件。责任岗位。处理。验证。台账。关闭。")["complete"]
    assert process_integrity("发现设备异常后，工程负责人登记工单并组织维修。使用人复验结果并签认台账，负责人关闭工单。")["complete"]
    assert not visual_output_present({"tables": [{"title": "应急组织图"}]}, "应急组织图")
    assert not visual_output_present({"body_blocks": [{"type": "diagram", "asset_path": "ghost.png"}]}, "应急组织图")
