"""Inspect exported PPTX bytes, source image bindings and native content carriers."""
from __future__ import annotations
import hashlib,io,json,posixpath,re,zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
from PIL import Image
from planning.ppt_service import META,PLACEHOLDER,visible_parts
NS={"a":"http://schemas.openxmlformats.org/drawingml/2006/main","p":"http://schemas.openxmlformats.org/presentationml/2006/main",
    "r":"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "c":"http://schemas.openxmlformats.org/drawingml/2006/chart"}
SERVICE_TONE=json.loads((Path(__file__).parent/"ppt_renderer_v2/service_tone.json").read_text(encoding="utf-8"))
def display_text(s):
    for old,new in SERVICE_TONE:s=str(s).replace(old,new)
    return s
def compact(t):return re.sub(r"\s","",str(t))
def pixel_signature(raw):
    with Image.open(io.BytesIO(raw)) as im:
        rgb=im.convert("RGB")
        return (rgb.size,hashlib.sha256(rgb.tobytes()).hexdigest())
def verify_pptx(path:Path,artifact:dict)->dict:
    path=Path(path);findings=[];pages=[];media=[];missing=[];bindings=[]
    def fail(code,message,**kw):findings.append({"rule_id":code,"severity":"BLOCK","issue":message,**kw})
    def target_path(base,target):
        return target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(base),target))
    def rel_path(name):
        return posixpath.join(posixpath.dirname(name),"_rels",posixpath.basename(name)+".rels")
    def chart_has_workbook(z,name):
        if not name or name not in z.namelist() or rel_path(name) not in z.namelist():return False
        chart=ET.fromstring(z.read(name))
        ext=chart.find("c:externalData",NS)
        series=chart.find(".//c:ser",NS)
        if ext is None or series is None or series.find("c:val/c:numRef/c:f",NS) is None:return False
        rid=ext.get("{"+NS["r"]+"}id")
        for rel in ET.fromstring(z.read(rel_path(name))):
            if rel.get("Id")!=rid or not rel.get("Type","").endswith("/package"):continue
            book_path=target_path(name,rel.get("Target",""))
            if book_path not in z.namelist():return False
            with zipfile.ZipFile(io.BytesIO(z.read(book_path))) as book:
                return book.testzip() is None and {"xl/workbook.xml","xl/worksheets/sheet1.xml"}<=set(book.namelist())
        return False
    try:
        with zipfile.ZipFile(path) as z:
            media_by_path={}
            for name in z.namelist():
                if not name.startswith("ppt/media/") or name.endswith("/"):continue
                raw=z.read(name);record={"path":name,"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"valid":False}
                if not raw:fail("PPT-P-EMPTY-MEDIA","PPT图片媒体为空",media=name)
                else:
                    try:
                        record["_pixels"]=pixel_signature(raw);record["valid"]=True
                    except Exception:fail("PPT-P-BROKEN-MEDIA","PPT图片无法解码",media=name)
                media_by_path[name]=record;media.append(record)
            rels={r.attrib["Id"]:r.attrib["Target"] for r in ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))}
            pres=ET.fromstring(z.read("ppt/presentation.xml"))
            size=pres.find("p:sldSz",NS)
            sw,sh=(int(size.get("cx")),int(size.get("cy"))) if size is not None else (None,None)
            for idx,sid in enumerate(pres.findall("p:sldIdLst/p:sldId",NS),1):
                sp=target_path("ppt/presentation.xml",rels[sid.attrib["{"+NS["r"]+"}id"]])
                root=ET.fromstring(z.read(sp));text="".join(t.text or "" for t in root.findall(".//a:t",NS))
                relname=rel_path(sp)
                slide_rels={r.attrib["Id"]:target_path(sp,r.attrib["Target"]) for r in ET.fromstring(z.read(relname))} if relname in z.namelist() else {}
                images=[];substantive_images=0;editable_charts=0
                for pic in root.findall(".//p:pic",NS):
                    blip=pic.find(".//a:blip",NS);rid=blip.get("{"+NS["r"]+"}embed") if blip is not None else ""
                    mp=slide_rels.get(rid)
                    if mp and media_by_path.get(mp,{}).get("valid"):
                        images.append(mp)
                        extent=pic.find("p:spPr/a:xfrm/a:ext",NS)
                        if extent is not None and sw and sh and int(extent.get("cx"))*int(extent.get("cy"))>=sw*sh*0.075:
                            substantive_images+=1
                    else:fail("PPT-P-IMAGE-BINDING","页面图片未绑定有效的嵌入媒体",page=idx)
                for chart in root.findall(".//c:chart",NS):
                    cp=slide_rels.get(chart.get("{"+NS["r"]+"}id"))
                    if chart_has_workbook(z,cp):editable_charts+=1
                    else:fail("PPT-P-CHART-DATA","原生图表缺少可编辑的数据工作簿",page=idx)
                step_titles=[];diagram_nodes=set();flow_arrows=0;new_renderer=False
                for shape in root.findall(".//p:sp",NS):
                    nv=shape.find("p:nvSpPr/p:cNvPr",NS)
                    name=nv.get("name","") if nv is not None else ""
                    shape_text="".join(t.text or "" for t in shape.findall(".//a:t",NS))
                    if name.startswith("service-step-title-"):step_titles.append(shape_text)
                    if name.startswith("service-evidence-node-title-") and shape_text.strip():diagram_nodes.add(shape_text)
                    if name.startswith("service-flow-"):flow_arrows+=1
                    if name=="service-left-rule":new_renderer=True
                    xfrm=shape.find("p:spPr/a:xfrm",NS)
                    if xfrm is not None and sw and sh:
                        off=xfrm.find("a:off",NS);ext=xfrm.find("a:ext",NS)
                        if off is not None and ext is not None:
                            x,y,cx,cy=[int(v) for v in (off.get("x"),off.get("y"),ext.get("cx"),ext.get("cy"))]
                            if x<0 or y<0 or x+cx>sw+12700 or y+cy>sh+12700:
                                fail("PPT-P-OFF-CANVAS","原生文本框或图形超出页面",page=idx)
                pages.append({"physical_page":idx,"text":text,"picture_count":len(images),"image_paths":images,
                    "step_titles":step_titles,"table_count":len(root.findall(".//a:tbl",NS)),
                    "substantive_images":substantive_images,"editable_charts":editable_charts,
                    "diagram_nodes":len(diagram_nodes),"flow_arrows":flow_arrows,"new_renderer":new_renderer})
                if META.search(text):fail("PPT-P-FINAL-META","实际PPT仍有制图指令",page=idx)
                if PLACEHOLDER.search(text):fail("PPT-P-FINAL-PLACEHOLDER","实际PPT含案例占位",page=idx)
            for slide in artifact.get("slides") or []:
                def expected(value,p):
                    return compact(display_text(value) if p["new_renderer"] else value)
                candidates=[p for p in pages if slide.get("title") and expected(slide["title"],p) in compact(p["text"])]
                matching=[p for p in candidates if all(expected(t,p) in compact(p["text"]) for t in visible_parts(slide))]
                if not matching:
                    missing.append(slide.get("slide_id"));fail("PPT-P-TEXT-LOSS","装配前完整业务文本未在同一最终页面找到",slide_id=slide.get("slide_id"))
                    continue
                page=matching[0]
                if slide.get("layout")=="process" and [compact(t) for t in page["step_titles"]]!=[expected(s["title"],page) for s in slide.get("steps") or []]:
                    fail("PPT-P-FINAL-STEPS","原生步骤标题或顺序与业务步骤不一致",slide_id=slide.get("slide_id"),page=page["physical_page"])
                if slide.get("layout")=="table" and not page["table_count"]:
                    fail("PPT-P-FINAL-TABLE","业务表格未装配为原生表格",slide_id=slide.get("slide_id"))
                if slide.get("image"):
                    raw=Path(slide["image"]).read_bytes();signature=pixel_signature(raw)
                    bound=any(media_by_path[m].get("_pixels")==signature for m in page["image_paths"])
                    bindings.append({"slide_id":slide.get("slide_id"),"physical_page":page["physical_page"],
                        "asset_id":slide.get("image_asset_id"),"source_sha256":hashlib.sha256(raw).hexdigest(),"bound":bound})
                    if not bound:fail("PPT-P-SOURCE-IMAGE","页面配图与来源图片像素身份不符",slide_id=slide.get("slide_id"))
    except Exception as exc:
        fail("PPT-P-FILE-INSPECTION","最终PPT文件检查未完成",error_type=type(exc).__name__)
    source_usage={};image_digests={}
    for slide in artifact.get("slides") or []:
        if not slide.get("image"):continue
        image_path=Path(slide["image"])
        if image_path not in image_digests:
            try:image_digests[image_path]=hashlib.sha256(image_path.read_bytes()).hexdigest()
            except Exception:
                fail("PPT-P-IMAGE-MISSING","页面配图文件无法读取",slide_id=slide.get("slide_id"));continue
        digest=image_digests[image_path]
        source_usage.setdefault(digest,[]).append(slide.get("slide_id"))
        if slide.get("image_source")=="PROJECT_SOURCE" and str(slide.get("chapter_id") or "").startswith(("P02","P03","P04","P05","P06")):
            fail("PPT-P-IMAGE-SCOPE","项目建筑效果图被用于服务章节",slide_id=slide.get("slide_id"))
        if slide.get("image_source")=="AI_GENERATED_SERVICE_ILLUSTRATION" and "服务场景示意" not in str(slide.get("image_caption") or ""):
            fail("PPT-P-SCENE-LABEL","AI服务场景图未标注为服务场景示意",slide_id=slide.get("slide_id"))
    for digest,slide_ids in source_usage.items():
        if len(slide_ids)>2:
            fail("PPT-P-IMAGE-REUSE","同一来源图片在超过两页正文重复使用",slide_ids=slide_ids)
    contract=artifact.get("output_contract") or {}
    minimum=max(1,int(contract.get("min_total_pages") or 15))
    ratio=max(0.30,float(contract.get("min_visual_page_ratio") or contract.get("min_image_page_ratio") or 0.30))
    pictured=[p["physical_page"] for p in pages if p["picture_count"]]
    photos=[p["physical_page"] for p in pages if p["substantive_images"]]
    diagrams=[p["physical_page"] for p in pages if p["diagram_nodes"]>=2 or
              (len(p["step_titles"])>=2 and p["flow_arrows"]==len(p["step_titles"])-1)]
    chart_pages=[p["physical_page"] for p in pages if p["editable_charts"]]
    visual_pages=sorted(set(photos+diagrams+chart_pages))
    if len(pages)<minimum:fail("PPT-P-PAGE-MIN","实际物理页数未达到约定下限")
    new_renderer=any(p["new_renderer"] for p in pages)
    if new_renderer:
        if len(visual_pages)<len(pages)*ratio:fail("PPT-P-VISUAL-RATIO","实际业务图示、图表或有效配图页比例未达到约定下限")
        if len(photos)<len(pages)*float(contract.get("min_photo_page_ratio") or 0):
            fail("PPT-P-IMAGE-RATIO","实际配图页比例未达到明确约定的下限")
    elif len(pictured)<len(pages)*ratio:fail("PPT-P-IMAGE-RATIO","实际含图页比例未达到约定下限")
    for m in media:m.pop("_pixels",None)
    return {"schema_version":"profile-p-final-file-v1","artifact_path":str(path.resolve()),
        "artifact_sha256":hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        "status":"BLOCK" if findings else "PASS","findings":findings,
        "physical_slide_count":len(pages),"content_slides_expected":len(artifact.get("slides") or []),
        "picture_pages":pictured,"picture_page_ratio":round(len(pictured)/max(1,len(pages)),4),
        "substantive_picture_pages":photos,"business_diagram_pages":diagrams,"editable_chart_pages":chart_pages,
        "editable_chart_count":sum(p["editable_charts"] for p in pages),
        "substantive_visual_pages":visual_pages,"substantive_visual_page_ratio":round(len(visual_pages)/max(1,len(pages)),4),
        "media":media,"source_image_bindings":bindings,"missing_content_slide_ids":missing,
        "minimum_total_pages":minimum,**({"minimum_visual_page_ratio":ratio} if new_renderer else {"minimum_image_page_ratio":ratio}),
        "measurement_basis":"PPTX_SLIDE_XML_AND_MEDIA_BYTES",
        "actual_pptx_inspected":not any(f["rule_id"]=="PPT-P-FILE-INSPECTION" for f in findings),
        "visual_render_review":"PENDING",
        "semantic_requirement_acceptance":"PENDING","authority_promoted":False}

