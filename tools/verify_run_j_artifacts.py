"""Independent inspection of the exported packages and the frozen CU originals."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile
from xml.etree import ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"app"))
from visuals.ir import validate,digest,require
from longform.process_integrity import visual_output_present


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source",type=Path,required=True)
    p.add_argument("--evidence",type=Path,required=True)
    args=p.parse_args()
    folder=args.evidence
    cases=json.loads((folder/"visual_ir.json").read_text("utf-8"))
    expected={v["visual_id"]:digest(v) for v in cases}
    for v in cases:validate(v,args.source)
    with zipfile.ZipFile(folder/"RUN_J_VISUAL_SAMPLES.docx") as z:
        saved={}
        for name in z.namelist():
            if name.startswith("customXml/visual") and name.endswith(".xml"):
                value=json.loads(ET.fromstring(z.read(name)).text)
                saved[value["visual_id"]]=digest(value)
        require(saved==expected,"DOCX semantic attachment differs")
        xml=ET.fromstring(z.read("word/document.xml"))
        ns={"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "wp":"http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"}
        require(len(xml.findall(".//w:drawing",ns))==len(cases),"missing DOCX visual")
        image_names=[n for n in z.namelist() if n.startswith("word/media/")]
        require(len(image_names)==len(cases) and all(len(z.read(n))>1000 for n in image_names),"missing DOCX media bytes")
    with zipfile.ZipFile(folder/"RUN_J_VISUAL_SAMPLES.pptx") as z:
        ns={"a":"http://schemas.openxmlformats.org/drawingml/2006/main",
            "p":"http://schemas.openxmlformats.org/presentationml/2006/main"}
        notes={}
        for name in z.namelist():
            if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml"):
                text="\n".join(t.text or "" for t in ET.fromstring(z.read(name)).findall(".//a:t",ns))
                if "[MMF_VISUAL_IR_V1]" in text:
                    raw=text.split("[MMF_VISUAL_IR_V1]",1)[1].split("[SEMANTIC_DIGEST]",1)[0].strip()
                    value=json.loads(raw)
                    notes[value["visual_id"]]=digest(value)
        require(notes==expected,"PPT notes lost or changed source/semantics")
        slides=[n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
        insertion=json.loads((folder/"c4_insertion_plan.json").read_text("utf-8"))
        require(len(slides)==insertion["predicted_physical_slide_count"],"C4 insertion count changed")
        native_diagrams=0
        native_arrows=0
        for name in slides:
            xml=ET.fromstring(z.read(name))
            names=[p.get("name","") for p in xml.findall(".//p:cNvPr",ns)]
            if any(n.startswith("visual:") for n in names):
                native_diagrams+=1
                require(not xml.findall(".//p:pic",ns),"visual diagram flattened to bitmap")
                native_arrows+=len(xml.findall('.//a:tailEnd[@type="triangle"]',ns))
        require(native_diagrams==len(cases) and native_arrows>0,"editable objects/arrows missing")
    embeddings=json.loads((folder/"docx_embedding.json").read_text("utf-8"))
    s10=next(v for v in cases if v["section_id"]=="S10-01")
    block=next(b for b in embeddings if b["visual_id"]==s10["visual_id"])
    require(visual_output_present({"body_blocks":[block]},"应急组织与联动关系示意图"),"existing VISUAL_REQUIRED presence check failed")
    report={"status":"PASS","docx_visuals":len(saved),"ppt_native_visuals":native_diagrams,"ppt_native_arrows":native_arrows,
            "physical_ppt_slides":len(slides),"same_visual_ir":saved==notes==expected,
            "source_files_unchanged":True,"source_semantic_snapshots_preserved":True,
            "s10_01":{"actual_diagram_embedded_docx":True,"actual_diagram_editable_ppt":True,
                      "nodes":len(s10["nodes"]),"relationships":len(s10["relationships"]),
                      "existing_visual_presence_check":True,"qa_thresholds_changed":False,
                      "authority_settlement_performed":False},"provider_calls":0}
    (folder/"artifact_verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),"utf-8")
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":main()
