"""Visual-only contract, source safety, source preservation and C4 parity."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile
from xml.etree import ElementTree as ET

from docx import Document
import pytest

from visuals.ir import VERSION, VisualContractError, digest, validate
from visuals.scene import compile_scene
from visuals.docx_renderer import embed_ir
from visuals.ppt_adapter import visual_slide
from visuals.c4_insertion_contract import production_ppt_insertion_contract


def test_exported_native_text_honors_shared_lines_without_default_insets(ir,tmp_path):
    node = os.environ.get("RUN_J_NODE")
    modules = os.environ.get("RUNTIME_NODE_MODULES")
    if not node or not modules:
        pytest.skip("native text export needs the declared Run J visual runtime")
    root = Path(__file__).resolve().parents[1]
    scene = compile_scene(ir)
    script = tmp_path/"native_text.mjs"
    out = tmp_path/"native_text.pptx"
    script.write_text('import {createRequire} from "node:module";import {pathToFileURL} from "node:url";\n'+
        'import {renderVisualScene} from '+json.dumps((root/"app/ppt_renderer_v2/visuals.mjs").as_uri())+';\n'+
        'const entry=createRequire(import.meta.url).resolve("@oai/artifact-tool",{paths:[process.env.RUNTIME_NODE_MODULES]});\n'+
        'const {Presentation,PresentationFile}=await import(pathToFileURL(entry).href);\n'+
        'const p=Presentation.create({slideSize:{width:1280,height:720}});\n'+
        'renderVisualScene(p.slides.add(),{title:"流程",visual_scene:'+json.dumps(scene,ensure_ascii=False)+'},1,1);\n'+
        'await (await PresentationFile.exportPptx(p)).save('+json.dumps(str(out))+');\n',encoding="utf-8")
    result = subprocess.run([node,str(script)],capture_output=True,text=True,encoding="utf-8",timeout=60)
    assert result.returncode == 0,result.stderr
    ns = {"p":"http://schemas.openxmlformats.org/presentationml/2006/main",
          "a":"http://schemas.openxmlformats.org/drawingml/2006/main"}
    with zipfile.ZipFile(out) as z:
        xml = ET.fromstring(z.read("ppt/slides/slide1.xml"))
    labels = {s.find("p:nvSpPr/p:cNvPr",ns).get("name"):s for s in xml.findall("p:cSld/p:spTree/p:sp",ns)}
    for primitive in scene["primitives"]:
        if primitive["type"] != "box": continue
        shape = labels["label:"+primitive["id"]]
        body = shape.find("p:txBody/a:bodyPr",ns)
        assert body.get("wrap") == "none"
        assert all(body.get(attr) == "0" for attr in ("lIns","rIns","tIns","bIns"))
        assert ["".join(p.itertext()) for p in shape.findall("p:txBody/a:p",ns)] == primitive["lines"]


@pytest.fixture
def ir(tmp_path):
    source = {"content_unit_id": "CU-TEST-01", "payload": {"steps": ["受理登记", "复验签认", "关闭归档"]}}
    raw = json.dumps(source, ensure_ascii=False).encode()
    (tmp_path / "source.json").write_bytes(raw)
    refs = [{"source_id":"SRC", "pointer":f"/payload/steps/{i}", "quote":text, "span":[0,len(text)]}
            for i,text in enumerate(source["payload"]["steps"])]
    return {"schema_version":VERSION,"visual_id":"TEST","section_id":"TEST","kind":"PROCESS_FLOW","title":"办理流程",
            "sources":[{"source_id":"SRC","cu_id":"CU-TEST-01","source_kind":"GENERATED_PROPOSED_METHOD",
                        "file":"source.json","sha256":hashlib.sha256(raw).hexdigest(),"snapshot":source,"snapshot_digest":digest(source)}],
            "nodes":[{"id":f"n{i}","label":text,"evidence":[refs[i]]} for i,text in enumerate(source["payload"]["steps"])],
            "relationships":[{"id":f"e{i}","from":f"n{i}","to":f"n{i+1}","relation":"NEXT","label":"顺序",
                              "reviewed_projection":True,"evidence":[refs[i],refs[i+1]]} for i in range(2)],
            "sequence":["n0","n1","n2"],"roles":[],"responsibilities":[],"groups":[],"layout_hints":{},
            "evidence":refs,"reviewed_projection":True}


def test_exact_original_and_portable_binding(ir,tmp_path):
    assert validate(ir,tmp_path)["source_verification"] == "ORIGINAL_FILES"
    assert validate(ir)["source_verification"] == "PORTABLE_SNAPSHOTS"
    (tmp_path/"source.json").write_text('{}')
    with pytest.raises(VisualContractError,match="changed source file"):
        validate(ir,tmp_path)


def test_tender_evidence_can_bind_non_cu_document(ir,tmp_path):
    source=ir["sources"][0]
    source["source_kind"]="TENDER"
    source["snapshot"].pop("content_unit_id")
    source["snapshot"]["tender_id"]="SRC"
    source["identity_pointer"]="/tender_id"
    raw=json.dumps(source["snapshot"],ensure_ascii=False).encode()
    source["sha256"]=hashlib.sha256(raw).hexdigest()
    source["snapshot_digest"]=digest(source["snapshot"])
    (tmp_path/"source.json").write_bytes(raw)
    assert validate(ir,tmp_path)["status"]=="PASS"


def test_connector_cannot_cross_unrelated_business_node(ir):
    ir["layout_hints"]={"positions":{"n0":[0,0,200,100],"n1":[500,0,200,100],"n2":[250,0,200,100]},
                        "routes":{"e0":[[200,50],[500,50]]}}
    with pytest.raises(VisualContractError,match="crosses"):
        compile_scene(ir)


@pytest.mark.parametrize("change",[
    lambda v:v["nodes"][0].pop("evidence"),
    lambda v:v["nodes"][0]["evidence"][0].update(quote="新增指挥部门"),
    lambda v:v["nodes"][0]["evidence"][0].update(pointer="/payload/missing"),
    lambda v:v["relationships"][0].update(to="ghost"),
    lambda v:v["nodes"].append(copy.deepcopy(v["nodes"][0])),
    lambda v:v.update(sequence=["n0","n1"]),
    lambda v:v["nodes"][0].update(display_text="三十分钟完成"),
    lambda v:v["sources"][0].update(source_kind="GUESSED"),
    lambda v:v["sources"][0]["snapshot"]["payload"].update(steps=["changed"]),
    lambda v:v["relationships"][0].pop("evidence"),
    lambda v:v["relationships"][0].update(relation="UNSUPPORTED_BUSINESS_RELATION"),
    lambda v:v["relationships"][0].update(to="n0"),
])
def test_refuses_unsupported_or_lossy_semantics(ir,change):
    change(ir)
    with pytest.raises(VisualContractError):
        validate(ir)


def test_source_traversal_refused(ir,tmp_path):
    ir["sources"][0]["file"]="../source.json"
    with pytest.raises(VisualContractError,match="escapes"):
        validate(ir,tmp_path)


def test_org_chart_requires_single_root_and_acyclic_reporting(ir):
    ir.update(kind="ORG_CHART",sequence=[])
    for edge in ir["relationships"]: edge["relation"]="REPORTS_TO"
    validate(ir)
    ir["relationships"][1].update(**{"from":"n1","to":"n0"})
    with pytest.raises(VisualContractError,match="cycle"):
        validate(ir)


def test_no_prose_guessing_for_graph_layout(ir):
    ir.update(kind="RELATIONSHIP_DIAGRAM",sequence=[])
    with pytest.raises(VisualContractError,match="explicit positions"):
        compile_scene(ir)


def test_long_labels_block_instead_of_truncation(ir):
    ir["nodes"][0].update(label="很长的业务标签"*100,reviewed_projection=True)
    with pytest.raises(VisualContractError,match="cannot fit without loss"):
        compile_scene(ir)


def test_overlapping_and_outside_page_geometry_blocked(ir):
    ir["layout_hints"]["positions"]={n["id"]:[0,0,480,120] for n in ir["nodes"]}
    with pytest.raises(VisualContractError,match="overlapping"):
        compile_scene(ir)
    ir["layout_hints"]["positions"]["n0"]=[1100,0,480,120]
    with pytest.raises(VisualContractError,match="outside"):
        compile_scene(ir)


def test_timeline_and_ordinal_gantt_never_guess_dates(ir):
    ir["kind"]="TIMELINE"
    assert compile_scene(ir)["semantic_ir"]["kind"]=="TIMELINE"
    ir.update(kind="MILESTONE_GANTT",time_basis="ORDINAL_PHASES")
    for n in ir["nodes"]:
        n.update(condition=n["evidence"][0]["quote"])
    scene=compile_scene(ir)
    assert "阶段顺序（非日历工期）" in "".join("".join(p.get("lines",[])) for p in scene["primitives"])
    ir["time_basis"]="CONFIRMED_DATES"
    for n in ir["nodes"]:n.update(start="2026-10-01",end="2026-10-02")
    with pytest.raises(VisualContractError,match="dates must be literal"):
        validate(ir)


def test_full_semantics_survive_docx_and_ppt_adapter(ir,tmp_path):
    doc=Document()
    embed_ir(doc,ir)
    path=tmp_path/"visual.docx"
    doc.save(path)
    with zipfile.ZipFile(path) as package:
        saved=json.loads(ET.fromstring(package.read("customXml/visual1.xml")).text)
        assert saved==ir
    slide=visual_slide(ir,"P02")
    assert slide["visual_scene"]==compile_scene(ir)
    assert slide["visual_scene"]["semantic_ir"]==saved
    assert digest(saved)==slide["visual_scene"]["semantic_digest"]


@pytest.mark.parametrize("ids,chapters,expected",[
    (["P01"],["P01"],3),(["P01","P02"],["P01","P02"],5),
    (["P01","P02","P02","P01"],["P01","P02"],7),
    (["P02","P03"],["P02","P03"],6),(["P99"],["P01"],3),
    (["P01"],["P01","P02"],3),([""] ,["P01"],3)])
def test_frozen_c4_insertion_contract(ids,chapters,expected):
    artifact={"slides":[{"slide_id":f"V{i}","chapter_id":c} for i,c in enumerate(ids)],
              "chapters":[{"chapter_id":c} for c in chapters]}
    result=production_ppt_insertion_contract(artifact)
    assert result["predicted_physical_slide_count"]==expected
    assert result["footer_total_pages_js"]==expected-1
    assert result["sidecar_specific_rules"] is False


def test_actual_production_insertion_and_native_arrow_direction(ir,tmp_path):
    node=os.environ.get("RUN_J_NODE")
    if not node:
        pytest.skip("set RUN_J_NODE for native PPT contract execution")
    root=Path(__file__).resolve().parents[1]
    scene=compile_scene(ir)
    source=(root/"app/build_ppt.mjs").read_text("utf-8")
    # Run the actual production loop in a VM. Only drawing/export APIs are stubs.
    start=source.index("  const brief = payload.brief")
    end=source.index("  const previewIndex = [];")
    body=source[start:end]+"\nreturn {events,footer:totalPages};"
    script=tmp_path/"oracle.mjs"
    module=(root/"app/ppt_renderer_v2/visuals.mjs").as_uri()
    script.write_text('import assert from "node:assert/strict";\nimport {renderVisualScene} from '+json.dumps(module)+';\n'+
      'const scene='+json.dumps(scene,ensure_ascii=False)+';\n'+
      'let options=[]; const slide={background:{},shapes:{add:()=>({text:{}}),connect:(a,b,o)=>options.push(o)},speakerNotes:{textFrame:{setText:()=>{}}}};\n'+
      # Fake facade's text assignment must still expose text.style.
      'slide.shapes.add=()=>{let v={style:{}};return {set text(s){v.value=s},get text(){return v}}};\n'+
      'renderVisualScene(slide,{title:"流程",visual_scene:scene},2,3);\n'+
      'assert(options.some(o=>o.tail?.type==="triangle"));assert(options.every(o=>!o.head));\n'+
      'const changed=structuredClone(scene);changed.primitives[0].points[0][0]+=1;\n'+
      'assert.throws(()=>renderVisualScene(slide,{visual_scene:changed},2,3),/scene digest/);\n'+
      'const code='+json.dumps(body)+';\n'+
      'const payload={brief:{},artifact:{slides:[{slide_id:"a",chapter_id:"P01",layout:"visual",visual_scene:scene},{slide_id:"b",chapter_id:"P02",layout:"visual",visual_scene:scene},{slide_id:"c",chapter_id:"P02"}],chapters:[{chapter_id:"P01"},{chapter_id:"P02"}]}};\n'+
      'const events=[]; const draw=(role)=>(...args)=>events.push(role);\n'+
      'const Presentation={create:()=>({slides:{add:()=>({})}})};\n'+
      'const names=["payload","Presentation","W","H","configureTheme","runIdFromInput","inputPath","renderCover","renderDivider","routeSlide","renderVisualScene","renderModules","renderProcess","renderTable","renderOverview","renderClosing","events"];\n'+
      'const args=[payload,Presentation,1280,720,()=>{},()=>"run","input",draw("cover"),draw("divider"),()=>({component_id:"SK-03"}),draw("visual"),draw("modules"),draw("process"),draw("table"),draw("overview"),draw("closing"),events];\n'+
      'const result=new Function(...names,code)(...args);\n'+
      'assert.deepEqual(events,["cover","visual","divider","visual","overview","closing"]);assert.equal(result.footer,5);\n',encoding="utf-8")
    result=subprocess.run([node,str(script)],capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
