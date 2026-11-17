import json
import pytest
from providers.account_recognition import parse_recognition
from providers.base import ProviderError

@pytest.mark.parametrize('provider',['qoder_account','zcode_cn'])
def test_single_missing_information_note_is_preserved_as_one_list_member(provider):
    note='本块为空白人员登记表；人员数量与资格须依据其他来源核实。'
    row={'chunk_id':'CHK-0006-S002','proposed_items':[],'proposed_boilerplate':[{'reason':'空白表格'}],
         'proposed_conflicts':[],'proposed_scoring_items':[],'chunk_missing_facts':note}
    result,audit=parse_recognition(provider,{'chunk_id':row['chunk_id']},json.dumps(row,ensure_ascii=False))
    assert result['chunk_missing_facts']==[note] and result['proposed_boilerplate']==row['proposed_boilerplate']
    assert row['chunk_missing_facts']==note and audit['applied'] and not audit['business_text_changed']
    assert audit['container_normalizations'][0]['field']=='chunk_missing_facts'

@pytest.mark.parametrize('field',['proposed_items','proposed_conflicts','proposed_boilerplate','proposed_scoring_items'])
def test_scalar_business_arrays_are_still_rejected(field):
    row={'chunk_id':'c','proposed_items':[],field:'not an object list'}
    with pytest.raises(ProviderError):parse_recognition('qoder_account',{'chunk_id':'c'},row)

def test_wrong_identity_and_incomplete_json_are_not_repaired():
    for value in [{'chunk_id':'other','proposed_items':[],'chunk_missing_facts':'note'},'{"chunk_id":"c","chunk_missing_facts":"unfinished']:
        with pytest.raises(ProviderError):parse_recognition('qoder_account',{'chunk_id':'c'},value)
