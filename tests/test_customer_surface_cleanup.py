"""Customer-facing cleanup: keep tender terms, and do not fill form cells with stock phrases."""
from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from governance.proposal_fidelity import clean_internal_artifacts, display_cell
from governance.table_qa import form_template_table
from longform.orchestrator import fragment_to_section


def test_access_card_term_is_restored():
    text = clean_internal_artifacts("沿用本节〔1〕所述要求，办理正式经确认的人员通行凭证。")
    assert "〔1〕" not in text
    assert "经确认的人员通行凭证" not in text
    assert "正式门禁卡" in text


def test_role_columns_are_not_filled_with_stock_phrase():
    assert display_cell("", header=False, column_name="责任岗位") != "按标准执行"
    assert display_cell("按标准执行", header=False, column_name="触发条件") != "按标准执行"
    assert "项目负责人" in display_cell("", header=False, column_name="责任岗位")


def test_form_footer_keeps_four_labels_without_placeholder():
    table = form_template_table({"fields": ["记录编号"], "form_name": "复查关闭记录"})
    notes = table["notes"]
    for label in ("适用场景：", "保存位置：", "保存期限：", "复核要求："):
        assert label in notes
    assert "待确认" not in notes


def test_process_heading_uses_the_section_title():
    section = fragment_to_section(
        {"processes": [{"steps": ["登记", "复核"]}]},
        "巡逻与异常处置",
        section_id="S05-02",
    )
    assert "办理顺序" not in "\n".join(section["paragraphs"])
    assert any("巡逻与异常处置" in line for line in section["paragraphs"])
