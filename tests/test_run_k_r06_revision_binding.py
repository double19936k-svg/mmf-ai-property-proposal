import copy,hashlib,json
from docx import Document
import pytest
from longform.local_revision import create_docx_revision,RevisionError,sha
from governance.table_fidelity import final_table_findings,digest
from longform.orchestrator import fragment_to_section

def setup_cell(tmp_path):
    original=tmp_path/'old.docx';doc=Document();table=doc.add_table(rows=2,cols=4)
    values=[['对象','方法','责任','触发'],['培训','','岗位负责人','进场后']]
    for ri,row in enumerate(values):
        for ci,value in enumerate(row):table.cell(ri,ci).text=value
    doc.save(original)
    source=tmp_path/'cu.json'
    cu={'content_unit_id':'CU-01','archetype':'STANDARD_TABLE',
        'payload':{'columns':values[0],'rows':[['培训','情景复述确认疏散方向','岗位负责人','进场后']]}}
    source.write_text(json.dumps(cu,ensure_ascii=False),encoding='utf-8')
    op={'kind':'replace_cell','anchor':'table[0]/row[1]/cell[1]',
        'anchor_text_sha256':hashlib.sha256(b'').hexdigest(),'new':cu['payload']['rows'][0][1],
        'source_evidence':[{'file':'cu.json','sha256':sha(source),'pointer':'/payload/rows/0/1','quote':cu['payload']['rows'][0][1]}]}
    manifest={'schema_version':'source-bound-docx-revision-v1','source_artifact_sha256':sha(original),'operations':[op]}
    return original,source,cu,manifest

def test_proven_cell_restoration_preserves_old_artifact(tmp_path):
    original,_,_,m=setup_cell(tmp_path);before=sha(original);target=tmp_path/'new.docx'
    create_docx_revision(original,target,m,run_dir=tmp_path)
    assert Document(target).tables[0].cell(1,1).text==m['operations'][0]['new']
    assert sha(original)==before

def test_same_value_from_wrong_cu_row_does_not_bind(tmp_path):
    original,source,cu,m=setup_cell(tmp_path)
    cu['payload']['rows'][0][0]='不同事项';cu['payload']['rows'][0][2]='不同责任人'
    source.write_text(json.dumps(cu,ensure_ascii=False),encoding='utf-8')
    m['operations'][0]['source_evidence'][0]['sha256']=sha(source)
    with pytest.raises(RevisionError,match='stable peers'):
        create_docx_revision(original,tmp_path/'new.docx',m,run_dir=tmp_path)
    assert not (tmp_path/'new.docx').exists()

def test_forged_current_table_registration_is_checked_against_source_delta(tmp_path):
    original,_,_,m=setup_cell(tmp_path);target=tmp_path/'new.docx'
    create_docx_revision(original,target,m,run_dir=tmp_path)
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps(m,ensure_ascii=False),encoding='utf-8')
    doc=Document(target);values=[[c.text for c in row.cells] for row in doc.tables[0].rows]
    receipt={'rendered_cell_contracts':[{'values':values,'values_digest':digest(values)}],
             'revision_source_origin':{'file':original.name,'sha256':sha(original),
                 'manifest_file':manifest.name,'manifest_sha256':sha(manifest)}}
    witness=tmp_path/'artifact_table_build_metrics.json';witness.write_text(json.dumps(receipt,ensure_ascii=False),encoding='utf-8')
    assert final_table_findings(target,tmp_path)==[]
    doc.tables[0].cell(1,1).text='';doc.save(target)
    values[1][1]='';receipt['rendered_cell_contracts'][0]['values_digest']=digest(values)
    witness.write_text(json.dumps(receipt,ensure_ascii=False),encoding='utf-8')
    assert any('/registration/' in f['path'] for f in final_table_findings(target,tmp_path))

def test_process_stays_with_its_own_cu_body_and_each_title_has_steps():
    fragment={'title':'投诉处理','body_blocks':[
        {'type':'paragraph','content':'先受理再回告，具体步骤如下','source_content_unit_id':'CU-A'},
        {'type':'paragraph','content':'后续复盘安排','source_content_unit_id':'CU-B'}],
        'processes':[{'steps':['登记事项','按编号回告'],'source_content_unit_id':'CU-A'},
                     {'steps':['检查记录','归并改进'],'source_content_unit_id':'CU-B'}]}
    section=fragment_to_section(fragment,'投诉处理')
    blocks=section['content_blocks']
    assert [b['type'] for b in blocks]==['paragraph','subheading','numbered_steps']*2
    assert blocks[2]['items']==['登记事项','按编号回告']
    assert blocks[5]['items']==['检查记录','归并改进']
    assert '具体步骤如下' not in blocks[0]['content']
