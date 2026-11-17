"""Small reviewed mappings from frozen Run G CU payloads, with zero providers.

No source files are modified. No raw customer fixture is committed. Mapping
choices are explicit and reviewable, never a generic prose-to-diagram guesser.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from visuals.ir import VERSION, digest, validate, require, pointer
from visuals.scene import compile_scene
from visuals.docx_renderer import add_visual
from visuals.ppt_adapter import visual_slide
from visuals.c4_insertion_contract import production_ppt_insertion_contract


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Sources:
    def __init__(self, root):
        self.root, self.items = Path(root), {}

    def load(self, sid, number):
        cu = f"CU-{sid}-{number:02d}"
        file = f"longform/word/sections/{sid}/cus/{cu}.json"
        raw = (self.root / file).read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
        self.items[cu] = {"source_id": cu, "cu_id": cu, "section_id": sid, "file": file,
                          "source_kind": "GENERATED_PROPOSED_METHOD", "sha256": hashlib.sha256(raw).hexdigest(),
                          "snapshot": value, "snapshot_digest": digest(value)}
        return cu

    def ref(self, cu, path, quote=None):
        raw = pointer(self.items[cu]["snapshot"], path)
        require(isinstance(raw, str), "source reference must locate literal text")
        quote = raw if quote is None else quote
        start = raw.index(quote)
        return {"source_id": cu, "pointer": path, "span": [start, start + len(quote)], "quote": quote}

    def visual(self, id, sid, kind, title, cu_ids, caption=""):
        return {"schema_version": VERSION, "visual_id": id, "section_id": sid, "kind": kind,
                "title": title, "caption": caption, "sources": [copy.deepcopy(self.items[cu]) for cu in cu_ids],
                "nodes": [], "relationships": [], "sequence": [], "roles": [], "responsibilities": [],
                "groups": [], "layout_hints": {}, "mapping_review": {
                    "method": "EXPLICIT_OFFLINE_SOURCE_MAPPING", "approval_claimed": False,
                    "business_independent_gate_required": True}}


def node(id, label, refs, **kwargs):
    return {"id": id, "label": label, "evidence": refs, "reviewed_projection": True, **kwargs}


def edge(id, a, b, label, refs, relation="NEXT", **kwargs):
    return {"id": id, "from": a, "to": b, "label": label, "relation": relation,
            "evidence": refs, "reviewed_projection": True, **kwargs}


def build_cases(source_root):
    s = Sources(source_root)
    org = s.load("S03-01", 1)
    v = s.visual("J-S03-ORG", "S03-01", "ORG_CHART", "项目组织层级与汇报关系", [org],
                 "专业负责人由在册人员兼任，按层级汇报与归口协调")
    refs = [s.ref(org, f"/payload/rows/{i}/{j}") for i, j in [(0, 1), (1, 1), (1, 0), (2, 0)]]
    v["nodes"] = [node("client", "甲方指定对接人", [refs[0]]), node("lead", "项目负责人", [refs[1]]),
                  node("specialists", "专业执行层", [refs[2]], display_text="专业执行层（在册人员兼任负责人）\n工程／秩序维护／前台／室内保洁／后厨"),
                  node("operators", "作业与值守层", [refs[3]], display_text="作业与值守层\n各线当班作业岗位")]
    v["relationships"] = [edge("o1", "lead", "client", "统一对外汇报", [refs[0]], "REPORTS_TO", label_position=[735, 96]),
                          edge("o2", "specialists", "lead", "专业汇总上报", [refs[1]], "REPORTS_TO", label_position=[735, 235]),
                          edge("o3", "operators", "specialists", "本线逐级汇报", [s.ref(org,"/payload/rows/2/1")], "REPORTS_TO", label_position=[735, 391])]
    v["layout_hints"] = {"positions": {"client":[410,0,300,75], "lead":[410,145,300,75],
                                       "specialists":[140,285,840,100], "operators":[320,450,480,90]},
                         "routes": {"o1":[[560,145],[560,75]],"o2":[[560,285],[560,220]],"o3":[[560,450],[560,385]]}}
    cases = [v]

    complaint = s.load("S04-02", 1)
    closure = s.load("S04-02", 10)
    v = s.visual("J-S04-COMPLAINT", "S04-02", "PROCESS_FLOW", "投诉处理与回访关闭流程", [complaint, closure],
                 "未认可进入下一轮处置，关闭前核对台账、签认与凭证")
    labels = ["受理与登记", "首次响应与派单", "过程跟踪", "结果反馈与实效核对", "回访与认可确认", "凭证核验与关闭归档"]
    bindings = [s.ref(complaint,f"/payload/steps/{i}") for i in range(4)] + [s.ref(closure,"/payload/rows/0/2"),s.ref(closure,"/payload/rows/1/2")]
    v["nodes"] = [node(f"c{i}", label, [ref], detail=ref["quote"]) for i, (label,ref) in enumerate(zip(labels,bindings))]
    v["sequence"] = [n["id"] for n in v["nodes"]]
    v["relationships"] = [edge(f"cnext{i}",f"c{i}",f"c{i+1}","顺序推进",[bindings[i],bindings[i+1]]) for i in range(5)]
    v["nodes"].append(node("c6","下一轮处置\n记明未认可缘由",[bindings[4]],detail=bindings[4]["quote"],branch=True))
    v["relationships"][-1].update(label="认可",condition="提出人认可，关闭凭证齐备",label_position=[505,328],label_width=95)
    v["relationships"].append(edge("creturn","c4","c6","未认可",[bindings[4]],"RETURNS_TO",label_position=[50,470],label_width=200))
    v["layout_hints"] = {"positions":{f"c{i}":[20+(i%2)*580,18+(i//2)*155,480,112] for i in range(6)},
                         "routes": {"cnext1":[[1080,74],[1100,74],[1100,152],[260,152],[260,173]],
                                    "cnext3":[[1080,229],[1100,229],[1100,307],[260,307],[260,328]],
                                    "creturn":[[260,440],[260,455],[540,455],[540,470]]}}
    v["layout_hints"]["positions"]["c6"]=[300,470,480,90]
    cases.append(v)

    fault = s.load("S06-02", 2)
    verify = s.load("S06-02", 3)
    v = s.visual("J-S06-FAULT", "S06-02", "PROCESS_FLOW", "工程故障处置与复验关闭流程", [fault,verify],
                 "复验未通过保持工单开启，缺件不关闭；处置条件未齐转计划维修")
    labels = ["受理登记", "到场判断", "定级排序", "处置执行", "复验与运行状态确认", "三件齐备后关闭并回写台账"]
    bindings = [s.ref(fault,f"/payload/steps/{i}") for i in range(4)] + [s.ref(verify,"/payload/items/3"),s.ref(verify,"/payload/items/6")]
    v["nodes"] = [node(f"f{i}", label,[ref],detail=ref["quote"]) for i,(label,ref) in enumerate(zip(labels,bindings))]
    v["nodes"][-1]["evidence"].append(s.ref(verify,"/payload/items/7"))
    v["sequence"] = [n["id"] for n in v["nodes"]]
    v["relationships"] = [edge(f"fnext{i}",f"f{i}",f"f{i+1}","具备条件后推进",[bindings[i],bindings[i+1]]) for i in range(5)]
    v["relationships"].append(edge("freturn","f4","f2","复验未通过，重新定级处置",[s.ref(verify,"/payload/items/4")],"RETURNS_TO",label_position=[170,478],label_width=620))
    v["layout_hints"] = copy.deepcopy(cases[1]["layout_hints"])
    v["layout_hints"]["positions"].pop("c6")
    v["layout_hints"]["positions"] = {k.replace("c","f",1): val for k,val in v["layout_hints"]["positions"].items()}
    v["layout_hints"]["routes"] = {k.replace("c","f",1): val for k,val in v["layout_hints"]["routes"].items()}
    v["layout_hints"]["routes"]["freturn"]=[[20,384],[5,384],[5,229],[20,229]]
    cases.append(v)

    emergency = s.load("S10-01", 1)
    duties = s.load("S10-01", 7)
    activation = s.load("S10-01", 4)
    v = s.visual("J-S10-LINKAGE", "S10-01", "RELATIONSHIP_DIAGRAM", "应急组织与联动关系示意图", [emergency,duties,activation],
                 "项目负责人统一指挥与信息出口，外部联动名单经甲方书面确认")
    chain = s.ref(emergency,"/content", "指挥与协同的责任链为：发现岗位上报专业负责人、专业负责人判级并上报项目负责人、项目负责人向甲方接口报告并对外联动，各专业负责人之间按处置职能横向协同，形成单线上报、横向分工、统一出口的责任网络，避免多头指挥与指令中断。")
    duties_ref = s.ref(emergency,"/content", "项目负责人在应急状态下统一指挥、判级定档、资源调配、对甲方与对外报告、宣布恢复与关闭；专业负责人负责本专业先期处置、技术措施落实与过程记录；发现岗位负责第一时间报告与现场警戒；客户服务岗位负责信息传达与对外口径衔接")
    external = s.ref(activation,"/payload/steps/3")
    v["nodes"] = [node("discover","发现岗位",[chain]),node("specialist","专业负责人",[chain]),
                  node("commander","项目负责人\n现场指挥",[chain,duties_ref]),node("client","甲方接口",[chain]),
                  node("external","外部专业力量\n按甲方渠道协调",[external,chain]),
                  node("service","客户服务岗位\n信息传达与口径衔接",[duties_ref]),
                  node("order","秩序维护\n警戒与疏散",[s.ref(duties,"/payload/rows/1/0")]),
                  node("engineering","工程岗位\n断源与设备操作",[s.ref(duties,"/payload/rows/2/0"),s.ref(duties,"/payload/rows/2/1")]),
                  node("kitchen","后厨与保洁\n断源、清理与围挡",[s.ref(duties,"/payload/rows/3/0"),s.ref(duties,"/payload/rows/3/2")])]
    v["relationships"] = [edge("e1","discover","specialist","上报",[chain],"REPORTS_TO",label_position=[130,317],label_width=170),
                          edge("e2","specialist","commander","判级上报",[chain],"REPORTS_TO",label_position=[155,170],label_width=200),
                          edge("e3","commander","client","向甲方报告",[chain],"REPORTS_TO",label_position=[580,78],label_width=190),
                          edge("e4","commander","external","按甲方渠道联动",[chain,external],"COORDINATES",label_position=[800,95],label_width=300),
                          edge("e5","commander","service","信息与口径衔接",[duties_ref],"COORDINATES",label_position=[575,228],label_width=230),
                          edge("e6","specialist","order","专业分工",[s.ref(emergency,"/payload/steps/3")],"DIRECTS"),
                          edge("e7","specialist","engineering","专业分工",[s.ref(emergency,"/payload/steps/3")],"DIRECTS"),
                          edge("e8","specialist","kitchen","专业分工",[s.ref(emergency,"/payload/steps/3")],"DIRECTS"),
                          edge("e9","order","engineering","断源隔离协同",[s.ref(duties,"/payload/rows/1/3")],"COORDINATES",
                               label_position=[250,508],label_width=185)]
    v["layout_hints"] = {"positions": {"client":[410,0,300,65],"commander":[410,130,300,90],
                                        "external":[825,270,285,90],"service":[410,290,300,90],
                                        "specialist":[20,220,260,75],"discover":[20,385,260,65],
                                        "order":[20,470,230,90],"engineering":[435,470,260,90],"kitchen":[825,470,285,90]},
                         "routes":{"e1":[[100,385],[100,295]],"e2":[[150,220],[150,140],[410,140]],
                                   "e3":[[560,130],[560,65]],"e4":[[710,175],[960,175],[960,270]],
                                   "e5":[[560,220],[560,290]],"e6":[[280,255],[320,255],[320,455],[135,455],[135,470]],
                                   "e7":[[280,265],[340,265],[340,455],[565,455],[565,470]],
                                   "e8":[[280,285],[795,285],[795,420],[968,420],[968,470]],
                                   "e9":[[250,500],[435,500]]}}
    cases.append(v)

    milestone = s.load("S11-01",1)
    v = s.visual("J-S11-MILESTONES", "S11-01", "MILESTONE_GANTT", "实施阶段与里程碑放行关系", [milestone],
                 "阶段位置只表达顺序，日期与试运行周期经甲方书面确认后固化")
    v["time_basis"] = "ORDINAL_PHASES"
    gates = ["进场就绪，甲方书面放行","连续运行满约定周期","试运行结束，遗留事项闭环","履约年度届满，下一周期计划确认"]
    for i in range(4):
        ref=s.ref(milestone,f"/payload/rows/{i}/0")
        v["nodes"].append(node(f"m{i}",ref["quote"] + "\n" + gates[i],
                                [s.ref(milestone,f"/payload/rows/{i}/{c}") for c in range(4)],
                                condition=pointer(s.items[milestone]["snapshot"],f"/payload/rows/{i}/1"),
                                detail=pointer(s.items[milestone]["snapshot"],f"/payload/rows/{i}/2"),
                                responsibility=pointer(s.items[milestone]["snapshot"],f"/payload/rows/{i}/3")))
    v["sequence"]=[n["id"] for n in v["nodes"]]
    v["relationships"]=[edge(f"mnext{i}",f"m{i}",f"m{i+1}","阶段条件满足后推进",v["nodes"][i+1]["evidence"],"DEPENDS_ON") for i in range(3)]
    cases.append(v)

    timeline=copy.deepcopy(v)
    timeline.update(visual_id="J-S11-TIMELINE",kind="TIMELINE",title="实施里程碑时序")
    cases.append(timeline)
    matrix=s.load("S11-01",4)
    v=s.visual("J-S11-RESPONSIBILITIES","S11-01","RESPONSIBILITY_MATRIX_VISUAL","实施阶段责任与确认界面",[matrix],
               "项目负责人、专业负责人、客户服务岗位与甲方接口按事项分工")
    for i in range(4):
        v["roles"].append(node(f"r{i}",pointer(s.items[matrix]["snapshot"],f"/payload/headers/{i+1}"),[s.ref(matrix,f"/payload/headers/{i+1}")]))
    short_labels=["实施启动放行","里程碑台账维护","阶段偏差与延误处置","年度履约评估与衔接"]
    cell_labels=[["提请启动\n签发责任表","确认作业面就绪","留存往来\n建立台账","书面启动意见"],
                 ["核定节点\n与达成标志","更新节点状态","归集阶段成果","书面确认变更"],
                 ["组织升级\n设定整改时限","研判技术成因","登记依赖\n与待确认项","协调重大偏差"],
                 ["主持成果核验\n下周期计划","专业核验意见","归集全周期成果","书面确认计划"]]
    for i in range(4):
        row=node(f"t{i}",short_labels[i],[s.ref(matrix,f"/payload/rows/{i}/0")])
        row["cells"]={f"r{j}":node(f"cell{i}-{j}",cell_labels[i][j],[s.ref(matrix,f"/payload/rows/{i}/{j+1}")],
                                      detail=pointer(s.items[matrix]["snapshot"],f"/payload/rows/{i}/{j+1}")) for j in range(4)}
        v["responsibilities"].append(row)
        v["nodes"].append(node(f"t{i}",short_labels[i],[s.ref(matrix,f"/payload/rows/{i}/0")]))
    cases.append(v)
    for ir in cases:
        ir["evidence"]=[ref for n in ir["nodes"] for ref in n["evidence"]]
        ir["reviewed_projection"]=True
        validate(ir, source_root)
        compile_scene(ir)
    return cases


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--font",default="C:/Windows/Fonts/msyh.ttc")
    args=parser.parse_args()
    source,output=args.source.resolve(),args.output.resolve()
    require(not (source==output or source in output.parents or output in source.parents),"output must be isolated from frozen source")
    # Guard the whole offline fixture/render process, not just a reported flag.
    def disabled(*args,**kwargs):
        raise RuntimeError("RUN_J_NETWORK_DISABLED")
    socket.socket.connect=disabled
    socket.getaddrinfo=disabled
    cases=build_cases(source)
    output.mkdir(parents=True,exist_ok=True)
    dump(output/"visual_ir.json",cases)
    from docx import Document
    from docx.shared import Inches,Pt,RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    doc=Document()
    section=doc.sections[0]
    section.page_width,section.page_height=Inches(8.27),Inches(11.69)
    section.top_margin=section.bottom_margin=Inches(.75)
    section.left_margin=section.right_margin=Inches(.85)
    for name in ("Normal","Heading 1"):
        style=doc.styles[name]
        style.font.name="Microsoft YaHei"
        style.font.size=Pt(11 if name=="Normal" else 16)
        style.font.color.rgb=RGBColor(0,0,0)
        style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"),"Microsoft YaHei")
    embeddings=[]
    for i,ir in enumerate(cases):
        if i: doc.add_page_break()
        doc.add_heading(ir["title"],1)
        doc.add_paragraph(ir["caption"])
        embeddings.append(add_visual(doc,ir,output/"assets",args.font))
        doc.add_paragraph("图示按源方案的岗位与条件表达拟实施方法，具体接口与执行口径按甲方确认意见落实。")
    doc.save(output/"RUN_J_VISUAL_SAMPLES.docx")
    chapters=[{"chapter_id":f"P{i+1:02d}","title":name,"goal":""} for i,name in enumerate(["项目组织","投诉闭环","工程闭环","应急联动","实施计划"])]
    slides=[visual_slide(ir, f"P{min(i+1,5):02d}") for i,ir in enumerate(cases)]
    source_brief=json.loads((source/"brief.json").read_text("utf-8-sig"))
    payload={"brief":{"project_name":source_brief.get("project_name", "物业服务项目"),"scenario":"技术方案"},
             "artifact":{"title":"物业服务组织与实施方案","subtitle":"组织、流程、应急与实施计划",
                         "chapters":chapters,"slides":slides}}
    dump(output/"ppt_content.json",payload)
    contract=production_ppt_insertion_contract(payload["artifact"])
    dump(output/"c4_insertion_plan.json",contract)
    dump(output/"docx_embedding.json",embeddings)
    dump(output/"semantic_validation.json",[validate(ir,source) for ir in cases])
    dump(output/"source_manifest.json",[{"file":s["file"],"sha256":s["sha256"]} for ir in cases for s in ir["sources"]])
    print(json.dumps({"visuals":len(cases),"primary_real_cases":5,"provider_calls":0,"network_disabled":True,
                      "physical_ppt_slides":contract["predicted_physical_slide_count"]}))


if __name__=="__main__":
    main()
