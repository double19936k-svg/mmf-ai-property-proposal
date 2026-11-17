import copy, json
import pytest
from docx import Document
from governance.artifact_qa import apply_artifact_repairs
from governance.table_fidelity import (KEY, TableFidelityError, source_contract,
    require_source_tables, digest, final_table_findings)
from longform.orchestrator import fragment_to_section

def sample():
    table = {'columns':['事项','处理方法'], 'rows':[
        ['培训','能否在情景复述中说明确认、上报与引导的先后顺序以及所在楼层的疏散方向'],
        ['版本','修订与版本维护：变化按生效时点进入文本而不改写历史']]}
    table[KEY] = source_contract(table, 'CU-TEST-01')
    return table

def test_customer_cleanup_preserves_contract_and_operating_values():
    original=sample(); fixed=apply_artifact_repairs(original)
    assert fixed[KEY] == original[KEY]
    require_source_tables([fixed])
    assert len(fragment_to_section({'tables':[fixed]},'测试')['tables']) == 1

def test_existing_domain_translation_is_permitted_but_numeric_change_is_not():
    from governance.commitment_provenance import apply_local_repairs
    table={'columns':['事项','要求'],'rows':[['staffing','必须保持12项记录']]}
    table[KEY]=source_contract(table,'CU-DOMAIN')
    translated=apply_artifact_repairs(apply_local_repairs(table))
    require_source_tables(translated)
    translated['rows'][0][1]='必须保持13项记录'
    with pytest.raises(TableFidelityError):require_source_tables(translated)

def test_visible_budget_counts_rendered_slots_once_and_excludes_source_snapshot():
    from longform.factory import visible_text
    table=sample()
    section={'paragraphs':['正文内容'],'bullets':['办理动作'],
        'content_blocks':[{'type':'paragraph','content':'正文内容','source_content_unit_id':'CU-01'},
                          {'type':'numbered_steps','items':['办理动作']}],'tables':[table]}
    visible=visible_text(section)
    assert visible.count('正文内容')==1 and visible.count('办理动作')==1
    assert visible.count(table['rows'][0][1])==1 and 'CU-TEST-01' not in visible

@pytest.mark.parametrize('mutation',['empty','truncated','shift','duplicate','header','digest'])
def test_one_source_cell_loss_or_shift_blocks_assembly(mutation):
    table=sample()
    if mutation=='empty':table['rows'][0][1]=''
    elif mutation=='truncated':table['rows'][0][1]='能否在情景复述中说明确认'
    elif mutation=='shift':table['rows'][0].reverse()
    elif mutation=='duplicate':table['rows'][1]=table['rows'][0][:]
    elif mutation=='header':table['columns'][0]=''
    else:table[KEY]['source_values_digest']='0'*64
    with pytest.raises(TableFidelityError):
        fragment_to_section({'tables':[table]},'测试')

def test_actual_docx_single_cell_loss_is_not_averaged(tmp_path):
    values=[['项目','处理方法'],['培训','按先确认、再上报、后引导的顺序复述疏散程序']]
    doc=Document();table=doc.add_table(rows=2,cols=2)
    for r,row in enumerate(values):
        for c,value in enumerate(row):table.cell(r,c).text=value
    target=tmp_path/'good.docx';doc.save(target)
    receipt={'rendered_cell_contracts':[{'values':values,'values_digest':digest(values)}]}
    (tmp_path/'artifact_table_build_metrics.json').write_text(json.dumps(receipt),encoding='utf-8')
    assert final_table_findings(target,tmp_path)==[]
    table.cell(1,1).text='';bad=tmp_path/'lost.docx';doc.save(bad)
    assert any('/row/1/cell/1' in f['path'] for f in final_table_findings(bad,tmp_path))
