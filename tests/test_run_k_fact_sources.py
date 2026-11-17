import copy,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"app"))
from tender_intake.pack_builder import build_requirement_pack,candidates_from_extraction
from tender_intake.confirmation import apply_confirmation,seed_brief
from tender_intake.fact_sources import brief_project_fields

def extraction(*texts):
 return {"schema_version":"tender-extraction-v0.1","extraction_id":"EXT-FACTS","run_id":"fact_test",
 "files":[{"file_id":"FILE-001","original_filename":"fact.txt","media_type":"txt","sha256":"a"*64,"page_count":1}],
 "outline":[],"pages":[],"paragraphs":[{"file_id":"FILE-001","para_id":f"P-{i}","paragraph_index":i,
 "page_no":1,"heading_path":[],"outline_node_id":None,"text":text} for i,text in enumerate(texts)],
 "tables":[],"warnings":[],"processing_mode":"txt"}
def pack(*texts):
 e=extraction(*texts);return build_requirement_pack(e,candidates_from_extraction(e))
def decisions(p,conditional=True):
 return {"reviewer_role":"EXECUTOR_AGENT","reviewer_name":"test","human_signoff_claimed":False,"accept_remaining":False,
 "requirements":{r["requirement_id"]:{"action":"confirm"} for r in p["requirements"]},
 "conflicts":{c["conflict_group_id"]:{"resolution":"KEEP_CONDITIONAL" if conditional else "UNRESOLVED"} for c in p["conflicts"]},
 "boilerplate":{b["boilerplate_id"]:"APPLICABLE" for b in p["potential_boilerplate"]},"proposal_reviews":{}}
class FactSources(unittest.TestCase):
 def test_address_variants_create_source_bound_conflict(self):
  p=pack("本项目地址：青岛市东海西路2号甲","座落位置：青岛市市南区东海西路2号")
  c=next(c for c in p["conflicts"] if c.get("fact_slot")=="location")
  self.assertEqual({o["value"] for o in c["options"]},{"青岛市东海西路2号甲","青岛市市南区东海西路2号"})
  self.assertEqual(p["project_facts"]["location"]["value"],"")
 def test_zuoluo_spelling_is_recognized(self):
  for text in ("坐落位置：青岛市东海西路2号","座落位置：青岛市东海西路2号"):
   self.assertEqual(pack(text)["project_facts"]["location"]["value"],"青岛市东海西路2号")
 def test_contact_address_is_not_project_location(self):
  p=pack("招标人地址：另一个联系地址","项目地址：本项目服务地址")
  self.assertEqual(p["project_facts"]["location"]["value"],"本项目服务地址")
  self.assertFalse(any(c.get("fact_slot")=="location" for c in p["conflicts"]))
 def test_paraphrased_number_cannot_replace_source_fact(self):
  e=extraction("本项目建筑面积：9163平方米");items=candidates_from_extraction(e)
  items[0]["normalized_requirement"]="本项目建筑面积：99999平方米"
  p=build_requirement_pack(e,items);self.assertEqual(p["project_facts"]["gross_area"]["value"],"9163平方米")
  self.assertFalse(p["conflicts"])
 def test_fabricated_excerpt_cannot_seed_fact(self):
  e=extraction("本项目必须提供物业服务");items=candidates_from_extraction(e)
  items[0]["normalized_requirement"]="本项目地址：虚构地点"
  items[0]["source_locator"]["source_excerpt"]="本项目地址：虚构地点"
  self.assertNotIn("location",build_requirement_pack(e,items)["project_facts"])
 def test_wrong_locator_cannot_seed_fact(self):
  e=extraction("项目地址：地址A","本项目必须提供物业服务");items=candidates_from_extraction(e)
  items[0]["source_locator"]["para_id"]="P-1"
  self.assertNotIn("location",build_requirement_pack(e,items)["project_facts"])
 def test_duration_longer_label_consumed_first(self):
  for text in ("服务期限3年","服务期限：3年","合同期限：3年","服务期3年"):
   self.assertEqual(pack(text)["project_facts"]["contract_duration"]["value"],"3年")
 def test_equivalent_area_units_do_not_create_conflict(self):
  p=pack("建筑面积：9163平方米","建筑面积：0.9163万平方米")
  self.assertFalse(any(c.get("fact_slot")=="gross_area" for c in p["conflicts"]))
 def test_period_narrative_is_not_a_duration_value(self):
  p=pack("服务期限3年","合同期内发生事故","服务期间发生事故","本合同期满终止后，应完成移交")
  self.assertEqual(p["project_facts"]["contract_duration"]["value"],"3年")
  self.assertFalse(any(c.get("fact_slot")=="contract_duration" for c in p["conflicts"]))
 def test_opening_and_payment_addresses_are_not_project_location(self):
  p=pack("开标地址：开标室A","到以下地址：代理机构B","本项目地址：服务地址C")
  self.assertEqual(p["project_facts"]["location"]["value"],"服务地址C")
  self.assertFalse(any(c.get("fact_slot")=="location" for c in p["conflicts"]))
 def test_form_labels_do_not_become_project_facts(self):
  e=extraction("项目名称：已声明的项目","项目名称：用户名称")
  e["paragraphs"][1]["heading_path"]=["第六章 投标文件格式"]
  p=build_requirement_pack(e,candidates_from_extraction(e))
  self.assertEqual(p["project_facts"]["project_name"]["value"],"已声明的项目")
 def test_contact_context_blocks_bare_address(self):
  e=extraction("招标代理机构：公司A","地址：联系地址B","本项目地址：服务地址C")
  p=build_requirement_pack(e,candidates_from_extraction(e))
  self.assertEqual(p["project_facts"]["location"]["value"],"服务地址C")
 def test_table_column_labels_are_not_fact_values(self):
  e=extraction("项目名称：真实项目")
  e["tables"]=[{"file_id":"FILE-001","table_id":"T-1","table_index":0,"heading_path":[],"columns":["项目名称","用户名称","报价方式"],"rows":[]}]
  p=build_requirement_pack(e,candidates_from_extraction(e))
  self.assertEqual(p["project_facts"]["project_name"]["value"],"真实项目")
 def test_held_facts_do_not_leak_into_generation_context(self):
  from planning.planner import build_global_state
  from longform.factory import ContextPackBuilder
  p=pack("项目名称：工程验证","项目地址：地址A","座落位置：地址B","建筑面积：9163平方米","建筑面积：9163.8㎡","本项目必须提供保洁服务")
  c=apply_confirmation(p,decisions(p));b=seed_brief(c,{"scenario":"完整物业服务方案","medium":"WORD","provider_name":"mock"})
  contracts={"contracts":[],"topic_ownership":[],"process_contracts":[]}
  state=build_global_state(c,b,[],{"coverage_summary":{}},contracts,{"dependencies":[]})
  ctx=ContextPackBuilder(state,contracts,{"knowledge_usage_contracts":[]},{"dependencies":[]}).word({"section_id":"S1","source_requirements":[r["requirement_id"] for r in c["requirements"]]}, {})
  import json
  text=json.dumps(ctx,ensure_ascii=False)
  self.assertNotIn("9163",text);self.assertNotIn("地址A",text);self.assertNotIn("地址B",text)
  self.assertIn("保洁服务",text)
  self.assertTrue(state["source_trace"]["held_project_fact_requirements"])
 def test_project_number_conflict_detected_without_model(self):
  p=pack("项目编号：0747-2160SCCSD160","项目编号：0747-2160SCCSD165")
  self.assertTrue(any(c.get("fact_slot")=="project_number" for c in p["conflicts"]))
 def test_conditional_multi_values_do_not_leak_into_seed(self):
  p=pack("项目名称：工程验证","项目地址：地址A","座落位置：地址B","建筑面积：9163平方米","建筑面积：9163.8㎡")
  c=apply_confirmation(p,decisions(p));self.assertTrue(c["confirmation"]["ready_for_brief_seed"])
  b=seed_brief(c,{"scenario":"完整物业服务方案","medium":"WORD","provider_name":"mock"})
  self.assertEqual((b["location"],b["area"]),("",""))
  self.assertNotIn("地址A",b["requirements"]);self.assertNotIn("9163",b["requirements"])
 def test_unresolved_conflict_still_blocks_gate(self):
  p=pack("项目地址：地址A","座落位置：地址B");c=apply_confirmation(p,decisions(p,False))
  self.assertFalse(c["confirmation"]["ready_for_brief_seed"])
 def test_template_ack_cannot_override_conditional_conflict(self):
  p=pack("项目地址：地址A","座落位置：地址B")
  ids=p["conflicts"][0]["requirement_ids"]
  p["potential_boilerplate"].append({"boilerplate_id":"BPL-OVERLAP","requirement_ids":ids,"todd_decision":"UNCONFIRMED"})
  d=decisions(p);c=apply_confirmation(p,d)
  self.assertTrue(all(r["confirmation_status"]=="DEFERRED" for r in c["requirements"] if r["requirement_id"] in ids))
  self.assertNotEqual(c["project_facts"]["location"]["confirmation_status"],"CONFIRMED")
 def test_brief_uses_official_cover_and_document_property_type(self):
  title="东区新营业用房物业服务采购项目"
  e=extraction(title,"招标文件","目 录","项目名称：东区大楼服务项目",
               "项目名称：东区物业外包服务项目","一、办公用房概况")
  e["paragraphs"][0]["heading_path"]=[title]
  e["paragraphs"][1]["heading_path"]=[title]
  e["paragraphs"][3]["heading_path"]=["第一章 投标邀请"]
  e["paragraphs"][4]["heading_path"]=["第七章 合同文本"]
  e["paragraphs"][5]["heading_path"]=["第五章 项目需求书"]
  p=build_requirement_pack(e,candidates_from_extraction(e))
  self.assertEqual(p["project_facts"]["project_name"]["fact_status"],"MULTI_VALUE")
  fields=brief_project_fields(p,e)
  c=apply_confirmation(p,decisions(p))
  b=seed_brief(c,{"scenario":"完整物业服务方案","medium":"WORD","provider_name":"mock",
                  "project_name":"采购方名称"},project_fields=fields)
  self.assertEqual((b["project_name"],b["project_type"]),(title,"办公楼"))
  self.assertEqual(fields["project_name"]["source"],"招标文件封面")
 def test_brief_requires_only_missing_identity_information(self):
  e=extraction("项目名称：新城区项目","项目名称：南城区项目")
  p=build_requirement_pack(e,candidates_from_extraction(e))
  fields=brief_project_fields(p,e)
  self.assertFalse(fields["project_name"]["value"])
  self.assertFalse(fields["project_type"]["value"])
  c=apply_confirmation(p,decisions(p))
  opts={"scenario":"完整物业服务方案","medium":"WORD","provider_name":"mock","project_name":"南城区项目"}
  with self.assertRaisesRegex(Exception,"请补充项目类型"):
   seed_brief(c,opts,project_fields=fields)
  b=seed_brief(c,{**opts,"project_type":"住宅"},project_fields=fields)
  self.assertEqual((b["project_name"],b["project_type"]),("南城区项目","住宅"))
if __name__=="__main__":unittest.main()
