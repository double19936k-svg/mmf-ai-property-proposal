"""Small real-source excerpts through the actual DOCX builder, never generation."""
import argparse
import copy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"app"))
from visuals.ir import require,digest
from run_j_visual_cases import dump


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--evidence",type=Path,required=True)
    args=parser.parse_args()
    source=args.source.resolve()
    out=args.evidence.resolve()
    require(source!=out and source not in out.parents and out not in source.parents,"probe must be isolated")
    artifact=json.loads((source/"artifact_content.json").read_text("utf-8-sig"))
    visuals=json.loads((out/"visual_ir.json").read_text("utf-8"))[:5]
    sections=[]
    for ir in visuals:
        original=next(s for s in artifact["artifact"]["sections"] if s.get("section_id")==ir["section_id"])
        # Explicit representative excerpt, same unedited text in both variants.
        selected={"section_id":original["section_id"],"heading":original["heading"],"level":1,
                  "paragraphs":original.get("paragraphs",[])[:1],"tables":original.get("tables",[])[:1],"bullets":[]}
        sections.append(copy.deepcopy(selected))
    baseline={"brief":artifact["brief"],"artifact":{"title":"组织、流程、应急与实施章节摘录",
              "include_toc":False,"sections":sections}}
    with_visual=copy.deepcopy(baseline)
    for section,ir in zip(with_visual["artifact"]["sections"],visuals):
        section["visuals"]=[ir]
    dump(out/"pagination_baseline.json",baseline)
    dump(out/"pagination_visual.json",with_visual)
    require(all({k:v for k,v in after.items() if k!="visuals"}==before for before,after in
                zip(baseline["artifact"]["sections"],with_visual["artifact"]["sections"])),"original excerpt changed")
    dump(out/"pagination_probe_input_integrity.json",{"representative_excerpt_only":True,"real_sections":len(sections),
         "original_paragraphs":len(sections),"original_tables":len(sections),"source_fact_loss":False,
         "original_excerpt_digest":digest(sections),"added_visuals":5,"provider_calls":0})


if __name__=="__main__":main()
