from docx import Document

from governance.closing_patch import finish_document
from governance.proposal_fidelity import append_uncovered_topics


def test_closing_patch_removes_noise_duplicates_and_copies_tender_facts(tmp_path):
    doc = Document()
    doc.add_heading("第一章 项目理解", level=1)
    doc.add_heading("1.1 项目概况", level=2)
    doc.add_paragraph("相关证明资料尚待核实能耗归集范围限于公共区域。")
    doc.add_paragraph("电子响应文件须以密码加密，密码遗失将无法解密。")
    doc.add_heading("1.2 项目特征与物业管理重难点", level=2)
    doc.add_heading("项目特征与物业管理重难点", level=3)
    doc.add_paragraph("沿用本节〔1〕所述要求，该项安排而是让先期处置并行。")
    doc.add_heading("第十二章 评分专项响应", level=1)
    doc.add_heading("12.1 档案管理", level=2)
    doc.add_paragraph("8月30日前完成模块投入使用。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "事项"
    table.cell(0, 1).text = "责任"
    table.cell(1, 0).text = "催缴"
    table.cell(1, 1).text = "回执"
    doc.add_paragraph("供方准入与任务接口责任分工表")
    duplicate = doc.add_table(rows=3, cols=2)
    duplicate.cell(0, 0).text = "事项"
    duplicate.cell(0, 1).text = "责任"
    duplicate.cell(1, 0).text = "催缴"
    duplicate.cell(1, 1).text = "回执"
    duplicate.cell(2, 0).text = "表注"
    duplicate.cell(2, 1).text = "仅供核对"
    noise = doc.add_table(rows=2, cols=2)
    noise.cell(0, 0).text = "递交信息"
    noise.cell(0, 1).text = "电汇底单"
    noise.cell(1, 0).text = "保证金"
    noise.cell(1, 1).text = "齐鲁采购与招标网"
    corpus = (
        "采购单位：齐鲁交通投资有限公司。地址：济南市历下区龙奥西路1号银丰财富广场D座及裙房B-302。"
        "租务管理：按租赁合同支付方式提前2个月向租户下发费用催缴函。"
        "新租户入住后收集并存档营业执照副本、安全责任协议书副本。"
        "审核完成后将门禁办卡信息发至图文公司制作版面。"
        "风险管理：通过公共责任险、车位险的市场化分担机制提高突发事件处理能力。"
        "2019年8月30日前全部模块投入使用。"
        "评分细则包括服务承诺及优惠条件、合理化建议。"
        "协助通过LEED O+M认证；6-12周完成美国GBCI审核；预计33周，共计231天。"
    )
    finish_document(doc, corpus)
    text = "\n".join(p.text for p in doc.paragraphs)
    tables = "\n".join(cell.text for item in doc.tables for row in item.rows for cell in row.cells)
    whole = text + tables
    for word in ("齐鲁交通投资有限公司", "龙奥西路1号", "B-302", "提前2个月", "营业执照副本",
                 "安全责任协议书", "图文公司", "公共责任险", "车位险", "2019年8月30日",
                 "LEED", "GBCI", "231", "33周", "服务承诺", "合理化建议"):
        assert word in whole, word
    for word in ("保证金", "电汇底单", "齐鲁采购与招标网", "无法解密", "尚待核实", "〔1〕", "而是让"):
        assert word not in whole, word
    assert len(doc.tables) == 4
    headings = [p.text for p in doc.paragraphs if p.style and "Heading 3" in p.style.name]
    assert headings == ["项目特征与物业管理重难点作业与处置流程"]
    assert "供方准入与任务接口责任分工表" not in text


def test_scoring_sections_mount_from_the_tender_text():
    pack = {"scoring_items": [{"label": "服务承诺及优惠条件10分"}, {"label": "合理化建议10分"}],
            "commercial_or_assessment_requirements": [{"label": "协助通过LEED O+M认证"}],
            "mandatory_scope_text": "", "service_scope": {"included": []}}
    outline = append_uncovered_topics([], pack)
    titles = " ".join(row["section_title"] for chapter in outline for row in chapter["sections"])
    assert "服务承诺及优惠条件" in titles
    assert "合理化建议" in titles
    assert "LEED" in titles
