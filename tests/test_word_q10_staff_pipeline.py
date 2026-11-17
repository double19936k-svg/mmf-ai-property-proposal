import copy
import hashlib
import json
import re

import pytest
from governance.final_editorial import SourceFacts
from governance.table_fidelity import require_source_tables, TableFidelityError
from longform.cu_assembly import apply_content_units
from longform.factory import _repair_word_artifact
from longform.orchestrator import _local_text_pipeline
from longform.source_projection import STAFF_REDACTION, restore_staff_projection, restore_cached_staffing


def facts():
    return SourceFacts({'tables':[{'table_id':'STAFF','file_id':'F',
        'columns':['岗位','人员配置'],'rows':[['门岗',9],['室内保洁',4],['合计',13]]}]})


@pytest.mark.parametrize('pipeline',[_repair_word_artifact,_local_text_pipeline])
@pytest.mark.parametrize('text',[
    '最低配置按招标文件确定的2类岗位13人执行：门岗9人，室内保洁4人。',
    '人员配置上13人对应2类岗位，按来源表落实岗位职责。',
    '室内保洁按招标文件确定的4人配置分片到人。',
    '资源调配在2类岗位13人编制内按班次进行，应急器材以现场已配置的器材为准。',
    '按2类岗位13人到岗并核对证件。',
    '13人2类岗位配置、每月核对工作记录。',
    '按2类岗位编制组织13人到岗。',
])
def test_staffing_survives_generation_and_late_normalization(pipeline,text):
    actual=pipeline({'text':text},facts())['text']
    assert STAFF_REDACTION not in actual
    for token in re.findall(r'\d+人',text):assert token in actual


@pytest.mark.parametrize('pipeline',[_repair_word_artifact,_local_text_pipeline])
@pytest.mark.parametrize('text,unsupported',[
    ('组织参演13人。','13人'),
    ('室内保洁培训班4人配置。','4人'),
    ('按3类岗位13人到岗并核对证件。','13人'),
    ('2类岗位额外13人配置。','13人'),
])
def test_source_capacity_cannot_authorize_other_subjects(pipeline,text,unsupported):
    assert unsupported not in pipeline({'text':text},facts())['text']


@pytest.mark.parametrize('pipeline',[_repair_word_artifact,_local_text_pipeline])
def test_allocation_can_refer_to_sourced_role_across_sentences(pipeline):
    context='门岗承担日常巡逻并做好记录。'+'项目主管根据现场通行路径核对记录和交接安排。'*7
    text=context+'组织上把9人分为门区、楼层与外围三组。'
    assert '组织上把9人分为' in pipeline({'text':text},facts())['text']


def test_cross_sentence_allocation_is_not_exercise_attendance():
    context='门岗承担日常巡逻并做好记录。'+'项目主管根据现场通行路径核对记录和交接安排。'*7
    text=context+'演练时组织上把9人分为三组。'
    assert '9人' not in _repair_word_artifact({'text':text},facts())['text']


def projected():
    rows=[{'content_unit_id':'CU-A','parent_section_id':'S01-01','archetype':'NARRATIVE',
           'generation_origin':'provider_content_unit',
           'content':'最低配置按2类岗位13人执行，室内保洁4人，合计13人。'},
          {'content_unit_id':'CU-B','parent_section_id':'S01-01','archetype':'STANDARD_TABLE',
           'generation_origin':'provider_content_unit','content':'核对进场人员与证件。',
           'payload':{'title':'进场准备','columns':['阶段','要求'],
                      'rows':[['到岗核验','按2类岗位13人到岗并核对证件']]}}]
    expected=_repair_word_artifact(apply_content_units(rows,base_fragment={'title':'进场准备'},
                    section_id='S01-01',planned_ids=['CU-A','CU-B']),facts())
    current=copy.deepcopy(expected)
    current['body_blocks'][0]['content']=current['body_blocks'][0]['content'].replace('13人',STAFF_REDACTION,1)
    current['tables'][0]['rows'][0][1]=current['tables'][0]['rows'][0][1].replace('13人',STAFF_REDACTION)
    return rows,expected,current


def test_only_erased_evidenced_tokens_are_recovered_with_mixed_intact_counts():
    _,expected,current=projected();before=copy.deepcopy(current)
    fixed,actions=restore_staff_projection(current,expected,facts(),source_hashes={'CU-A':'a','CU-B':'b'})
    assert fixed==expected and len(actions)==2
    assert current==before
    require_source_tables(fixed['tables'])


@pytest.mark.parametrize('pipeline',[_repair_word_artifact,_local_text_pipeline])
def test_table_business_header_is_not_an_internal_prose_instruction(pipeline):
    from governance.table_fidelity import source_contract
    table={'columns':['表述类别','判定标准'],'rows':[['来源事实','以招标文件核对岗位人数']]}
    table['table_source_contract']=source_contract(table,'CU-H')
    result=pipeline({'tables':[table]},facts())
    assert result['tables'][0]['columns'][1]=='判定标准'
    require_source_tables(result['tables'])


def test_erased_header_restores_only_from_unchanged_cu_contract():
    from governance.table_fidelity import source_contract
    table={'source_content_unit_id':'CU-H','columns':['表述类别','判定标准'],
           'rows':[['来源事实','以招标文件核对岗位人数']]}
    table['table_source_contract']=source_contract(table,'CU-H')
    expected={'tables':[table]};current=copy.deepcopy(expected);current['tables'][0]['columns'][1]=''
    fixed,actions=restore_staff_projection(current,expected,facts(),source_hashes={'CU-H':'h'})
    assert fixed==expected and actions[0]['reason']=='RESTORE_SOURCE_BUSINESS_HEADER_ERASED_BY_PROSE_CLEANER'
    require_source_tables(fixed['tables'])


def test_native_final_editorial_keeps_header_and_negative_column_context(tmp_path):
    from docx import Document
    from governance.final_editorial import run_final_editorial
    p=tmp_path/'candidate.docx';doc=Document();table=doc.add_table(rows=2,cols=3)
    for c,t in zip(table.rows[0].cells,['信息类型','判定标准','不得升级为']):c.text=t
    for c,t in zip(table.rows[1].cells,['待核实信息','按来源核对条件','写成已经具备的资源、已经达成的结论或历史项目业绩']):c.text=t
    doc.save(p)
    run_final_editorial(p,facts=facts(),output_dir=tmp_path/'audit')
    result=Document(p)
    assert result.tables[0].cell(0,1).text=='判定标准'
    assert result.tables[0].cell(1,2).text=='写成已经具备的资源、已经达成的结论或历史项目业绩'


@pytest.mark.parametrize('tamper',['real_number_drift','contract_changed','additional_cell_edit','cu_identity_changed'])
def test_recovery_never_overwrites_other_edits_or_relaxes_fidelity(tamper):
    _,expected,current=projected();table=current['tables'][0]
    if tamper=='real_number_drift':table['rows'][0][1]=expected['tables'][0]['rows'][0][1].replace('13人','12人')
    elif tamper=='contract_changed':table['table_source_contract']['source_values_digest']='not-original'
    elif tamper=='additional_cell_edit':table['rows'][0][1]+='并增加人员'
    else:table['source_content_unit_id']='CU-FOREIGN'
    before=copy.deepcopy(table)
    fixed,_=restore_staff_projection(current,expected,facts(),source_hashes={'CU-A':'a','CU-B':'b'})
    assert fixed['tables'][0]==before
    with pytest.raises(TableFidelityError):require_source_tables(fixed['tables'])


def test_unsupported_attendance_cannot_be_recovered_from_staff_total():
    wanted={'body_blocks':[{'source_content_unit_id':'CU-A','content':'组织参演13人。'}]}
    current={'body_blocks':[{'source_content_unit_id':'CU-A','content':'组织参演'+STAFF_REDACTION+'。'}]}
    fixed,actions=restore_staff_projection(current,wanted,facts(),source_hashes={'CU-A':'a'})
    assert fixed==current and not actions


def test_cache_resume_repairs_summary_keeps_raw_cus_and_is_idempotent(tmp_path):
    original=tmp_path/'tender/originals/source.txt';original.parent.mkdir(parents=True)
    original.write_text('门岗9人，室内保洁4人，合计13人。',encoding='utf-8')
    extraction={'files':[{'file_id':'F','stored_relative_path':'source.txt','sha256':hashlib.sha256(original.read_bytes()).hexdigest()}],
        'tables':[{'file_id':'F','table_id':'STAFF','columns':['岗位','人员配置'],
                   'rows':[['门岗',9],['室内保洁',4],['合计',13]]}]}
    (tmp_path/'tender/extraction.json').write_text(json.dumps(extraction,ensure_ascii=False),encoding='utf-8')
    rows,expected,current=projected();unit=tmp_path/'longform/word/sections/S01-01'
    (unit/'cus').mkdir(parents=True)
    for row in rows:(unit/'cus'/f"{row['content_unit_id']}.json").write_text(json.dumps(row,ensure_ascii=False),encoding='utf-8')
    snapshots={p.name:p.read_bytes() for p in (unit/'cus').iterdir()}
    (unit/'fragment.json').write_text(json.dumps(current,ensure_ascii=False),encoding='utf-8')
    (unit/'summary.json').write_text(json.dumps({'core_points':STAFF_REDACTION}),encoding='utf-8')
    source=SourceFacts.from_run(tmp_path)
    report=restore_cached_staffing(tmp_path,None,lambda x:_local_text_pipeline(x,source))
    assert report['sections_changed']==['S01-01'] and report['restored_fields']==2
    assert json.loads((unit/'fragment.json').read_text(encoding='utf-8'))==expected
    assert '13人' in json.loads((unit/'summary.json').read_text(encoding='utf-8'))['core_points']
    assert STAFF_REDACTION not in json.loads((unit/'summary.json').read_text(encoding='utf-8'))['core_points']
    assert snapshots=={p.name:p.read_bytes() for p in (unit/'cus').iterdir()}
    assert restore_cached_staffing(tmp_path,['S01-01'],lambda x:_local_text_pipeline(x,source))['restored_fields']==0
