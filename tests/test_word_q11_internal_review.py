import copy
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from governance.final_artifact_qa import evaluate_docx_artifact, audit_docx_layout
from planning.content_engineering import classify_technical_scope
from planning.scope_gated_requirements import split_requirement_evidence, evidence_alias_hits
from longform.orchestrator import _scoring_requirement_evidence, evaluate_requirement_coverage_regression


@pytest.mark.parametrize('text,expected',[
    ('不得擅自增加物业收费项目，提高物业管理费','PRICING_EXCLUDED'),
    ('逾期交纳物业管理服务费，按每日0.3‰收取滞纳金','PRICING_EXCLUDED'),
    ('必须在偏差说明一栏中详细说明偏差','COMMERCIAL_EXCLUDED'),
    ('厨房刀具必须清洗并按生熟分类存放','TECHNICAL_INCLUDED'),
    ('工程人员须持电工上岗证，有三年以上工作经验','TECHNICAL_INCLUDED'),
])
def test_financial_and_bid_form_requirements_do_not_enter_technical_body(text,expected):
    assert classify_technical_scope({'text':text})==expected


def test_budget_split_preserves_service_duration_and_does_not_waive_wrong_quantity():
    r=split_requirement_evidence('费用预算不超过人民币570万元且服务期限3年')
    assert r['technical_text']=='服务期限3年'
    assert r['registered_fragments']
    assert not evidence_alias_hits(r['technical_text'],'服务期2年，执行岗位作业')
    assert evidence_alias_hits(r['technical_text'],'服务期3年，执行岗位作业')
    assert evidence_alias_hits(r['technical_text'],'本合同服务期三年，执行岗位作业')
    assert not evidence_alias_hits(r['technical_text'],'服务期两年，档案保留3年')
    assert split_requirement_evidence('费用预算不超过人民币570万元')['equivalence_basis']=='REJECTED_ALIAS_WOULD_EMPTY_REQUIREMENT'


def test_only_real_business_header_receives_header_context(tmp_path):
    d=Document();t=d.add_table(rows=2,cols=2)
    t.cell(0,0).text='事项';t.cell(0,1).text='判定标准'
    t.cell(1,0).text='人员到岗';t.cell(1,1).text='项目主管核对配置并登记上岗证。'
    t.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    p=tmp_path/'header.docx';d.save(p)
    q=evaluate_docx_artifact(p)
    assert not any(f.get('rule_id')=='CLIENT_ARTIFACT_INTERNAL_NARRATION' for f in q['findings'])
    t.cell(1,1).text='本节只说明写作方法，不输出正文内容。';d.save(p)
    q=evaluate_docx_artifact(p)
    assert any(f.get('rule_id')=='CLIENT_ARTIFACT_INTERNAL_NARRATION' for f in q['findings'])


def test_caption_is_structural_but_unindented_prose_still_fails(tmp_path):
    d=Document();d.styles['Normal'].paragraph_format.alignment=WD_ALIGN_PARAGRAPH.LEFT
    t=d.add_paragraph(style='Title');t.alignment=WD_ALIGN_PARAGRAPH.CENTER;t.add_run('物业服务方案').font.size=Pt(22)
    t=d.add_paragraph(style='Subtitle');t.alignment=WD_ALIGN_PARAGRAPH.CENTER;t.add_run('物业服务项目').font.size=Pt(18)
    t=d.add_paragraph();i=OxmlElement('w:instrText');i.text=' TOC \\o "1-2" MMF_TOC_1 ';t._p.append(i)
    d.add_paragraph('月度材料消耗与报废报表',style='Caption')
    p=tmp_path/'caption.docx';d.save(p)
    assert audit_docx_layout(p)['status']=='PASS'
    d.add_paragraph('项目主管负责复核材料消耗记录并签认报表。');d.save(p)
    assert audit_docx_layout(p)['prose_first_line_violations']


def test_coverage_uses_scoring_substance_without_printing_grading_words():
    row={'requirement_id':'R','requirement_text':'退出移交评分优得3分一般得2分',
         'scoring_item_id':'SC','mandatory_level':'MUST','scope_class':'TECHNICAL_INCLUDED','primary_section_id':'S'}
    base={'rows':[{'scoring_item_id':'SC','requirement_ids':['R'],
            'fulfillment':{'status':'FULFILLED','evidence_found':True,'response_cu':['C'],'artifact_location':['table[1]']}}],
          'canonical_scoring_records':[{'canonical_scoring_id':'K','responsiveness_class':'TECHNICAL_BODY_DIRECT',
            'evaluation_row_ids':['SC'],'fulfillment':{'status':'FULFILLED'}}]}
    fragments={'S':{'body_blocks':[{'content':'项目负责人组织人员撤场并办理移交，双方签认清单。'}]}}
    evidence=_scoring_requirement_evidence(base);assert evidence['R']['response_cu']==['C']
    q=evaluate_requirement_coverage_regression({'matrix':[row]},fragments,scoring_evidence_by_requirement=evidence)
    assert q['planned_must']==q['covered_must']==1
    base['canonical_scoring_records'][0]['fulfillment']['status']='PARTIAL'
    assert not _scoring_requirement_evidence(base)
    q=evaluate_requirement_coverage_regression({'matrix':[row]},fragments,scoring_evidence_by_requirement={})
    assert q['missing']==['R']


def test_cover_uses_title_style_without_decorative_border_or_orphan_last_character():
    from build_docx import add_title_block
    d=Document();pr=d.styles['Title']._element.get_or_add_pPr();border=OxmlElement('w:pBdr');pr.append(border)
    add_title_block(d,{'title':'中国进出口银行山东省分行物业服务方案','subtitle':'办公楼与职工食堂物业服务实施方案'}, {'project_name':'中国进出口银行山东省分行物业服务项目'})
    title=next(p for p in d.paragraphs if p.style.name=='Title')
    assert title.text=='中国进出口银行山东省分行\n物业服务方案'
    assert not d.styles['Title']._element.xpath('./w:pPr/w:pBdr')
    assert not title._p.xpath('./w:pPr/w:pBdr')
