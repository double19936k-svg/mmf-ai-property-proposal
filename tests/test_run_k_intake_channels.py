"""Source-bound channel preservation and confirmation atomicity regression."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"app"))
import app_core
from tender_intake.understanding import understand_run, build_chunks, _normalize_payload
from tender_intake.proposal_channels import normalize_channels
from tender_intake.pack_builder import build_requirement_pack, candidates_from_extraction
from tender_intake.confirmation import apply_confirmation, seed_brief

def extraction():
    return {"schema_version":"tender-extraction-v0.1","extraction_id":"EXT-T","run_id":"tender_test_channels",
        "files":[{"file_id":"F","original_filename":"t.txt"}],"outline":[],"pages":[],"tables":[],"warnings":[],"processing_mode":"txt",
        "paragraphs":[{"file_id":"F","para_id":f"PAR-{n}","paragraph_index":n,"text":text,"page_no":1,"heading_path":[]}
            for n,text in enumerate(["本项目建筑面积：9163平方米。","本合同范围建筑面积：9163.8㎡。",
                                    "本项目必须提供24小时安保服务。","投标函格式：项目名称待填写。"],1)]}
def payload(chunk):
    return {"chunk_id":chunk["chunk_id"],"proposed_items":[],"proposed_scoring_items":[],
        "proposed_conflicts":[{"description":"面积口径待核对","candidates":[
            {"source_id":"PAR-1","source_excerpt":"建筑面积：9163平方米","value":"9163"},
            {"source_id":"PAR-2","source_excerpt":"建筑面积：9163.8㎡","value":"9163.8"}]}],
        "proposed_boilerplate":[{"source_id":"PAR-4","source_excerpt":"投标函格式：项目名称待填写。","reason":"填空模板"}],
        "chunk_missing_facts":["当前块未显示服务起止日期，其他块可能载明。"]}
def pack_fixture():
    e=extraction();chunk=build_chunks(e)[0];p=payload(chunk);c=normalize_channels(p,chunk)
    return build_requirement_pack(e,candidates_from_extraction(e),provider_proposals=sum(c.values(),[]))
def decisions(pack,role="EXECUTOR_AGENT"):
    return {"accept_remaining":False,"reviewer_role":role,"reviewer_name":"Qoder","human_signoff_claimed":False,
        "requirements":{r["requirement_id"]:{"action":"confirm"} for r in pack["requirements"]},
        "conflicts":{c["conflict_group_id"]:{"resolution":"KEEP_CONDITIONAL"} for c in pack["conflicts"]},
        "boilerplate":{b["boilerplate_id"]:"CONDITIONAL" for b in pack["potential_boilerplate"]},
        "proposal_reviews":{p["proposal_id"]:"KEEP_PENDING" for p in pack["provider_proposals"]}}

class Channels(unittest.TestCase):
    def test_three_channels_survive_and_resume_without_model(self):
        e=extraction();chunk=build_chunks(e)[0];p=payload(chunk)
        provider=Mock();provider.config={"provider_name":"fixture"};provider.invoke_structured.return_value=p
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp)/e["run_id"]
            result=understand_run(run,e,provider,resume=False)
            norm=json.loads((run/"tender/chunks/CHK-0001/normalized.json").read_text("utf-8"))
            self.assertEqual([len(norm[k]) for k in ("proposed_conflicts","proposed_boilerplate","chunk_missing_facts")],[1,1,1])
            before=result["pack"]["provider_proposals"]
            result2=understand_run(run,e,provider,resume=True)
            self.assertEqual(before,result2["pack"]["provider_proposals"])
            self.assertEqual(provider.invoke_structured.call_count,1)
            self.assertEqual(len(before),3)
    def test_bad_cross_chunk_pointer_is_retained_unverified_not_promoted(self):
        e=extraction();chunk=build_chunks(e)[0];p=payload(chunk)
        p["proposed_conflicts"][0]["candidates"][1].update(source_id="PAR-1",source_excerpt="不在该段的9163.8㎡")
        rows=normalize_channels(p,chunk)
        self.assertEqual(rows["proposed_conflicts"][0]["validation_status"],"UNVERIFIED_PROPOSAL")
        pack=build_requirement_pack(e,candidates_from_extraction(e),provider_proposals=sum(rows.values(),[]))
        self.assertFalse(any(x.get("provenance")=="SOURCE_BOUND_PROVIDER_PROPOSAL" for x in pack["conflicts"]))
        self.assertTrue(any(c.get("proposal_id") for c in pack["clarification_items"]))
    def test_identical_references_do_not_create_two_source_conflict(self):
        c=build_chunks(extraction())[0];p=payload(c);refs=p["proposed_conflicts"][0]["candidates"]
        refs[1]=dict(refs[0])
        self.assertEqual(normalize_channels(p,c)["proposed_conflicts"][0]["validation_status"],"UNVERIFIED_PROPOSAL")
    def test_area_conflict_exists_even_when_provider_channels_empty(self):
        p=build_requirement_pack(extraction(),candidates_from_extraction(extraction()))
        area=next(c for c in p["conflicts"] if "gross_area" in c["issue"])
        self.assertEqual({x["value"] for x in area["options"]},{"9163","9163.8"})
        self.assertEqual(area["resolution"],"UNRESOLVED")
    def test_equivalent_area_decimal_spellings_are_not_conflicting(self):
        e=extraction();e["paragraphs"][1]["text"]="本合同范围建筑面积：9163.0㎡。"
        p=build_requirement_pack(e,candidates_from_extraction(e))
        self.assertFalse(any("gross_area" in c["issue"] for c in p["conflicts"]))
    def test_conflict_conditionality_prevents_area_being_seeded_as_fact(self):
        pack=pack_fixture();confirmed=apply_confirmation(pack,decisions(pack))
        self.assertTrue(confirmed["confirmation"]["ready_for_brief_seed"])
        brief=seed_brief(confirmed,{"scenario":"完整物业服务方案","medium":"WORD","provider_name":"mock"})
        self.assertEqual(brief["area"],"")
    def test_no_implicit_todd_or_human_signoff(self):
        pack=pack_fixture();confirmed=apply_confirmation(pack,decisions(pack))
        self.assertEqual(confirmed["confirmation"]["confirmed_by"],"Qoder")
        self.assertFalse(confirmed["confirmation"]["human_signoff_claimed"])
        self.assertEqual(confirmed["confirmation"]["reviewer_role"],"EXECUTOR_AGENT")
    def test_clarification_answer_records_real_reviewer(self):
        p=pack_fixture();d=decisions(p);d["clarifications"]={p["clarification_items"][0]["clarification_id"]:"待用户进一步核实"}
        c=apply_confirmation(p,d);self.assertEqual(c["clarification_items"][0]["answered_by"],"Qoder")
    def test_unreviewed_proposals_block_confirmation(self):
        p=pack_fixture();d=decisions(p);d.pop("proposal_reviews")
        c=apply_confirmation(p,d);self.assertFalse(c["confirmation"]["ready_for_brief_seed"])
        self.assertFalse(c["confirmation"]["provider_proposals_reviewed"])
    def test_proposal_cannot_be_promoted_to_confirmed_fact(self):
        p=pack_fixture();d=decisions(p);d["proposal_reviews"][p["provider_proposals"][0]["proposal_id"]]="CONFIRMED"
        with self.assertRaises(Exception):apply_confirmation(p,d)
    def test_rejected_scenario_does_not_advance_pack_or_write_confirmation(self):
        p=pack_fixture()
        with tempfile.TemporaryDirectory() as temp:
            runs=Path(temp);run=runs/p["run_id"];folder=run/"tender";folder.mkdir(parents=True)
            f=folder/"requirement_pack.json";f.write_text(json.dumps(p,ensure_ascii=False),"utf-8");before=f.read_bytes()
            with patch.object(app_core,"RUNS_DIR",runs),patch.object(app_core,"_checked",side_effect=Path):
                with self.assertRaisesRegex(app_core.MMFError,"方案场景"):
                    app_core.confirm_tender_run(p["run_id"],decisions(p),{"scenario":"完整物业服务方案（仅技术标正文）","medium":"WORD"})
            self.assertEqual(f.read_bytes(),before)
            self.assertFalse((folder/"confirmation.json").exists());self.assertFalse((run/"brief.json").exists())
    def test_ui_requires_review_of_retained_proposals(self):
        html=(Path(__file__).resolve().parents[1]/"static/index.html").read_text("utf-8")
        self.assertIn("proposal-review-ack",html);self.assertIn("proposal_reviews:proposalReviews",html)

if __name__=="__main__":unittest.main()
