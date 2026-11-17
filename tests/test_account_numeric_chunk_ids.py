import pytest
from providers.account_recognition import parse_recognition
from providers.base import ProviderError

@pytest.mark.parametrize('provider',['qoder_account','zcode_cn'])
@pytest.mark.parametrize('actual',['CHK-00004-S002','CHK-4-S2','CHK-0004-S0002'])
def test_only_zero_padding_changes_are_normalized(provider,actual):
    row={'chunk_id':actual,'proposed_items':[],'chunk_missing_facts':['原说明']}
    result,audit=parse_recognition(provider,{'chunk_id':'CHK-0004-S002'},row)
    assert result['chunk_id']=='CHK-0004-S002' and result['chunk_missing_facts']==['原说明']
    assert row['chunk_id']==actual and audit['identity_normalizations'][0]['from']==actual
    assert not audit['business_text_changed']

@pytest.mark.parametrize('actual',['CHK-0005-S002','CHK-0004-S003','CHK-0004','CHK-0004-Z002','chunk-0004-S002','CHK-0004-S002-extra','CHK-0000-S002'])
def test_other_parents_children_kinds_or_suffixes_stay_isolated(actual):
    with pytest.raises(ProviderError) as error:
        parse_recognition('qoder_account',{'chunk_id':'CHK-0004-S002'},{'chunk_id':actual,'proposed_items':[]})
    assert error.value.error_code=='QODER_RECOGNITION_ID_MISMATCH'
