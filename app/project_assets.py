"""Local project PDF assets: explicit source bytes and reviewed image labels.

No paid provider or VLM is called. Unclassified images stay in the manifest and
are never guessed into a building page. All paths are inside the owning run.
"""
from __future__ import annotations
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any
from PIL import Image
from pypdf import PdfReader

KINDS={"aerial","layout_map","tower","villa","research","production","source_reference"}
SCENE_TOPICS={"handover","move_in","fitout","reception","logistics","maintenance","energy","cleaning","amenities","leasing","quality","seasonal","gate","concierge"}
SCENE_PREFIX="服务场景示意"
def _sha(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def _sealed(run_dir:Path)->bool:
    return (run_dir/"artifact_content.json").exists() or (run_dir/"generation_input.json").exists()
def save_project_pdf(run_dir:Path, filename:str, data:bytes, annotations:list[dict[str,Any]]|None=None)->dict[str,Any]:
    if Path(filename).suffix.lower()!=".pdf" or not 0<len(data)<=40*1024*1024:
        raise ValueError("请上传不超过40MB的项目PDF。")
    if _sealed(run_dir):
        raise ValueError("已生成实例的图件保持封存，请建立新实例。")
    docsha=_sha(data); root=run_dir/"project_assets"/docsha
    path=run_dir/"project_assets/manifest.json"
    if path.exists():
        previous=json.loads(path.read_text(encoding="utf-8"))
        if previous.get("source_sha256")!=docsha:
            raise ValueError("本实例已绑定另一份项目PDF，请建立新实例或显式合并资产。")
    root.mkdir(parents=True,exist_ok=True)
    source=root/"source.pdf"
    if not source.exists():source.write_bytes(data)
    annotations=annotations or []
    labels={}
    for row in annotations:
        kind=row.get("kind")
        if kind not in KINDS:raise ValueError("图件分类无效。")
        labels[(int(row["page"]),int(row["image"]))]=row
    entries=[]; reader=PdfReader(io.BytesIO(data))
    if len(reader.pages)>300:raise ValueError("项目参考PDF超过300页，请精简后上传。")
    for n,page in enumerate(reader.pages,1):
        for i,item in enumerate(page.images,1):
            raw=item.data
            try:
                im=Image.open(io.BytesIO(raw)); w,h=im.size; im.verify()
            except Exception as exc:
                raise ValueError(f"第{n}页第{i}张图无法解码") from exc
            if min(w,h)<64:continue
            label=labels.get((n,i),{})
            suffix=Path(item.name).suffix.lower()
            if suffix not in {".png",".jpg",".jpeg"}:suffix=".png"
            name=f"p{n:03d}_{i:02d}{suffix}"
            target=root/name
            if suffix==".png" and not raw.startswith(b"\x89PNG"):
                converted=Image.open(io.BytesIO(raw)).convert("RGB"); buf=io.BytesIO();converted.save(buf,format="PNG"); raw=buf.getvalue()
            target.write_bytes(raw)
            kind=label.get("kind","source_reference")
            entries.append({"asset_id":f"{docsha[:12]}-p{n:03d}-{i:02d}","path":str(target.relative_to(run_dir)),
                "sha256":_sha(raw),"width":w,"height":h,"source_file":filename,"source_sha256":docsha,
                "source_page":n,"source_image":i,"kind":kind,"classification":"REVIEWED_SOURCE_LABEL" if label else "NEEDS_CLASSIFICATION",
                "image_class":label.get("image_class","项目来源参考图"),
                "caption":label.get("caption",f"{label.get('image_class','项目来源参考图')} · 简介第{n}页"),
                "source_authority":"USER_SUPPLIED_REFERENCE","verified_current_site_photo":False})
    manifest={"schema_version":"project-pdf-assets-v1","source_sha256":docsha,"source_filename":filename,
        "source_path":str(source.relative_to(run_dir)),"images":entries,"provider_calls":0,
        "unclassified_count":sum(e["classification"]=="NEEDS_CLASSIFICATION" for e in entries)}
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return manifest


def verified_image(run_dir:Path,asset_id:str)->Path:
    manifest=json.loads((run_dir/"project_assets/manifest.json").read_text(encoding="utf-8"))
    row=next((r for r in manifest["images"] if r["asset_id"]==asset_id),None)
    if row is None:raise ValueError("图件不存在。")
    target=(run_dir/row["path"]).resolve()
    if not target.is_relative_to(run_dir.resolve()) or _sha(target.read_bytes())!=row["sha256"]:
        raise ValueError("图件身份或路径核验失败。")
    return target

def display_caption(entry:dict[str,Any])->str:
    """Short visible label; exact source page and original caption stay in manifest."""
    caption=re.sub(r"\s*[·｜|]\s*(?:项目)?简介第\s*\d+\s*页\s*$", "", str(entry.get("caption") or ""))
    suffix="项目简介示意图" if entry.get("kind")=="layout_map" else "项目简介效果图" if "效果图" in str(entry.get("image_class") or caption) else "项目简介来源图"
    return f"{caption} · {suffix}" if caption else suffix


def relabel_project_pdf(run_dir:Path,annotations:list[dict[str,Any]])->dict[str,Any]:
    manifest=json.loads((run_dir/"project_assets/manifest.json").read_text(encoding="utf-8"))
    source=(run_dir/manifest["source_path"]).resolve()
    if not source.is_relative_to(run_dir.resolve()):
        raise ValueError("来源文件路径无效。")
    raw=source.read_bytes()
    if _sha(raw)!=manifest["source_sha256"]:raise ValueError("来源PDF身份已变化。")
    return save_project_pdf(run_dir,manifest["source_filename"],raw,annotations)

def save_service_scene(run_dir:Path,topic:str,data:bytes,*,caption:str,generation:dict[str,Any]|None=None, _during_generation:bool=False)->dict[str,Any]:
    """Register one generated service-scene illustration with byte identity.

    These are AI illustrations, never site photos: the caption carries the
    mandatory 服务场景示意 label and the manifest records generation provenance.
    """
    if topic not in SCENE_TOPICS:raise ValueError("服务场景主题不在允许清单内。")
    if not 0<len(data)<=12*1024*1024:raise ValueError("服务场景图片大小无效。")
    if _sealed(run_dir) and (not _during_generation or (run_dir/"artifact_content.json").exists()):
        raise ValueError("已生成实例的图件保持封存，请建立新实例。")
    try:
        im=Image.open(io.BytesIO(data));w,h=im.size;im.verify()
    except Exception as exc:raise ValueError("服务场景图片无法解码") from exc
    if min(w,h)<512:raise ValueError("服务场景图片分辨率不足。")
    root=run_dir/"project_assets/scenes";root.mkdir(parents=True,exist_ok=True)
    manifest_path=root/"manifest.json"
    manifest=json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"schema_version":"service-scene-assets-v1","scenes":[]}
    target=root/f"{topic}.png"
    if not data.startswith(b"\x89PNG"):
        buf=io.BytesIO();Image.open(io.BytesIO(data)).convert("RGB").save(buf,format="PNG");data=buf.getvalue()
    target.write_bytes(data)
    label=caption if SCENE_PREFIX in caption else f"{SCENE_PREFIX} · {caption}"
    entry={"scene_id":f"scene-{topic}","topic":topic,"path":str(target.relative_to(run_dir)),"sha256":_sha(data),
        "width":w,"height":h,"caption":label,"image_class":SCENE_PREFIX,
        "authority":"AI_GENERATED_SERVICE_ILLUSTRATION","verified_current_site_photo":False,
        "generation":dict(generation or {}),"human_visual_review":"PENDING"}
    manifest["scenes"]=[row for row in manifest.get("scenes") or [] if row.get("topic")!=topic]+[entry]
    if _during_generation:
        manifest["provider_calls"] = int(manifest.get("provider_calls") or 0) + 1
    else:
        manifest.setdefault("provider_calls",0)
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return manifest


def verified_scene(run_dir:Path,scene_id:str)->Path:
    manifest=json.loads((run_dir/"project_assets/scenes/manifest.json").read_text(encoding="utf-8"))
    row=next((r for r in manifest.get("scenes") or [] if r["scene_id"]==scene_id),None)
    if row is None:raise ValueError("服务场景图不存在。")
    target=(run_dir/row["path"]).resolve()
    if not target.is_relative_to(run_dir.resolve()) or _sha(target.read_bytes())!=row["sha256"]:
        raise ValueError("服务场景图身份或路径核验失败。")
    return target


def attach_service_scenes(run_dir:Path,artifact:dict[str,Any], *, scene_policy=None)->dict[str,Any]:
    """Bind registered AI service scenes to their planned modules pages."""
    path=run_dir/"project_assets/scenes/manifest.json"
    manifest=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"scenes":[]}
    choices={}
    for row in manifest.get("scenes") or []:
        target=(run_dir/row["path"]).resolve()
        if not target.is_relative_to(run_dir.resolve()) or not target.is_file() or _sha(target.read_bytes())!=row["sha256"]:
            raise ValueError("服务场景图身份或路径核验失败。")
        choices[row["topic"]]={**row,"absolute_path":str(target)}
    assignments=[];requested=[]
    for index,slide in enumerate(artifact.get("slides") or []):
        topic=slide.get("scene_topic")
        if not topic:continue
        requested.append(topic)
        entry=choices.get(topic)
        if not entry or slide.get("layout")!="modules" or slide.get("image"):
            continue
        slide.update(image=entry["absolute_path"],image_caption=entry["caption"],image_width=entry["width"],
            image_height=entry["height"],image_asset_id=entry["scene_id"],image_source="AI_GENERATED_SERVICE_ILLUSTRATION",
            image_side="left" if sum(1 for s in (artifact.get("slides") or [])[:index] if s.get("image"))%2==0 else "right")
        assignments.append({"slide_id":slide.get("slide_id"),"scene_id":entry["scene_id"],"topic":topic,
            "sha256":entry["sha256"],"caption":entry["caption"],"verified_current_site_photo":False})
    from account_delivery import read
    attempts=read(run_dir/"images/account_scene_requests.json")
    audit={"schema_version":"service-scene-assignment-v1","source_manifest":str(path),"assignments":assignments,
        "requested_topics":requested,"missing_topics":sorted(set(requested)-set(choices)),
        "provider_calls":max(int(manifest.get("provider_calls") or 0),sum(
            1 for row in attempts.values() if isinstance(row,dict) and row.get("state") in {"registered","returned","unusable"}
        )), "request_attempts":len(attempts), "picture_mode":scene_policy,
        "status":"ASSIGNED_REQUIRES_RENDER_CHECK" if assignments else "NO_SCENES_ASSIGNED"}
    image_dir=run_dir/"images";image_dir.mkdir(parents=True,exist_ok=True)
    (image_dir/"scene_audit.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
    return audit


def attach_project_images(run_dir:Path,artifact:dict[str,Any], *, fill_scenes=None, scene_policy=None)->dict[str,Any]:
    path=run_dir/"project_assets/manifest.json"
    manifest=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"images":[]}
    choices={}
    for row in manifest.get("images") or []:
        target=(run_dir/row["path"]).resolve()
        if not target.is_relative_to(run_dir.resolve()) or not target.is_file() or _sha(target.read_bytes())!=row["sha256"]:
            raise ValueError("项目图片身份或路径边界核验失败。")
        if row.get("classification")=="REVIEWED_SOURCE_LABEL":choices.setdefault(row["kind"],{**row,"absolute_path":str(target)})
    assignments=[]
    for index,slide in enumerate(artifact.get("slides") or []):
        entry=choices.get(slide.get("image_topic"))
        if entry and slide.get("layout")=="modules" and not slide.get("image"):
            slide.update(image=entry["absolute_path"],image_caption=display_caption(entry),
                         image_width=entry["width"],image_height=entry["height"],image_asset_id=entry["asset_id"],
                         image_source="PROJECT_SOURCE",
                         image_side="left" if sum(1 for s in (artifact.get("slides") or [])[:index] if s.get("image"))%2==0 else "right")
            assignments.append({"slide_id":slide.get("slide_id"),"asset_id":entry["asset_id"],"source_page":entry["source_page"],"sha256":entry["sha256"]})
    aerial=choices.get("aerial")
    if aerial:
        artifact["images"]={"cover":aerial["absolute_path"],"cover_caption":display_caption(aerial),
                            "cover_sha256":aerial["sha256"],"cover_asset_id":aerial["asset_id"]}
    if fill_scenes is not None:
        fill_scenes(run_dir,artifact)
    scene_audit=attach_service_scenes(run_dir,artifact,scene_policy=scene_policy)
    audit={"schema_version":"project-image-assignment-v1","source_manifest":str(path),"assignments":assignments,
        "cover_asset_id":aerial["asset_id"] if aerial else None,"classified_images":len(choices),
        "provider_calls":scene_audit["provider_calls"],"status":"ASSIGNED_REQUIRES_FINAL_RENDER_CHECK" if assignments else "MISSING_REVIEWED_ASSETS",
        "unclassified_count":manifest.get("unclassified_count",0),"service_scenes":scene_audit}
    image_dir=run_dir/"images";image_dir.mkdir(parents=True,exist_ok=True)
    (image_dir/"image_audit.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
    return audit

