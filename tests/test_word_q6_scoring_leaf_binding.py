import json
from unittest.mock import patch
from planning.scoring_taxonomy import build_scoring_taxonomy
from planning.hierarchy import update_scoring_fulfillment
from test_mmf007c_scoring_taxonomy import _row


def test_derived_leaf_retains_evaluation_identity_without_fabricating_extracted_id():
 row=_row('LEAF-TAB-SYNTHETIC-R1','本项目退出移交方案应完整可行',scoring_leaf_id='TAB-SYNTHETIC-R1')
 tree=build_scoring_taxonomy([row]);records=tree['canonical_scoring_records']
 assert records
 record=records[0]
 assert record['source_ids']==[]
 assert record['evaluation_row_ids']==['LEAF-TAB-SYNTHETIC-R1']
 assert tree['rows'][0]['canonical_scoring_id']==record['canonical_scoring_id']


def test_legacy_leaf_plan_uses_real_row_verdict_and_missing_binding_stays_missing(tmp_path):
 row=_row('LEAF-TAB-SYNTHETIC-R1','本项目退出移交方案应完整可行',scoring_leaf_id='TAB-SYNTHETIC-R1',scope_disposition='INCLUDED',scope_class='TECHNICAL_INCLUDED',scoring_class='TECHNICAL_SUBSTANTIVE',responsiveness_class='TECHNICAL_BODY_DIRECT')
 record={'canonical_scoring_id':'SCN-LOCAL','source_ids':[],'scoring_leaf_id':'TAB-SYNTHETIC-R1','scoring_class':'TECHNICAL_SUBSTANTIVE','responsiveness_class':'TECHNICAL_BODY_DIRECT','planned_response_cu':['CU-1']}
 m={'rows':[row],'canonical_scoring_records':[record]};p=tmp_path/'matrix.json'
 verdict={'status':'FULFILLED','response_sections':['S1'],'response_cu':['CU-1'],'effective_chars':900,'evidence_found':True,'entity_backed':True}
 p.write_text(json.dumps(m),encoding='utf-8')
 with patch('planning.hierarchy.judge_scoring_row',return_value=verdict):
  update_scoring_fulfillment(p,{})
 out=json.loads(p.read_text(encoding='utf-8'));r=out['canonical_scoring_records'][0]
 assert r['fulfillment']['status']=='FULFILLED'
 assert r['fulfillment']['counts_rows']==1
 assert r['source_ids']==[]
 assert r['evaluation_row_ids']==['LEAF-TAB-SYNTHETIC-R1']
 # A different source-table leaf is not a permissible replacement.
 m['rows'][0]['scoring_leaf_id']='TAB-OTHER';p.write_text(json.dumps(m),encoding='utf-8')
 with patch('planning.hierarchy.judge_scoring_row',return_value=verdict):update_scoring_fulfillment(p,{})
 out=json.loads(p.read_text(encoding='utf-8'))
 assert out['canonical_scoring_records'][0]['fulfillment']['status']=='MISSING'
 assert out['coverage_summary']['technical_scoring_coverage']==0
