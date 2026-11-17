"""PPT 离线快速迭代：复用已完成 run 的逐页 payload，重建 artifact_content 并渲染。

用法：runtime/dev_python/Scripts/python.exe tools/ppt_offline_render.py <run_id> [--no-images]
产物：runs/<run_id>/offline_rebuild/ 下的 pptx 与逐页 PNG（_qa_ppt_offline/）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from longform.final_assembly import _strip_scoring, build_ppt_slide_data  # noqa: E402

CHAPTER_PROMPTS = {
    "P01": "现代高端商务写字楼外立面与入口大堂，专业礼宾服务，商务摄影风格，冷色调，无文字",
    "P02": "现代化物业运营指挥中心，多屏监控大屏与专业管理团队，商务摄影风格，冷色调，无文字",
    "P03": "高端写字楼前台礼宾服务场景，专业客服人员，明亮整洁，商务摄影风格，无文字",
    "P04": "专业安保人员在商务园区巡逻，秩序井然，商务纪实摄影，冷色调，无文字",
    "P05": "物业工程人员在设备机房进行专业巡检维护，工业质感，商务摄影，无文字",
    "P06": "现代园区绿化与环境养护场景，整洁明亮，商务摄影风格，自然光，无文字",
    "P07": "项目管理团队在会议室进行服务品质复盘汇报，专业协作氛围，商务摄影，无文字",
}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run_id = args[0]
    with_images = "--no-images" not in sys.argv
    run_dir = ROOT / "runs" / run_id
    brief = json.loads((run_dir / "brief.json").read_text(encoding="utf-8-sig"))
    plan = json.loads((run_dir / "06_ppt_presentation_plan.json").read_text(encoding="utf-8-sig"))
    storyboard = {row["slide_id"]: row for row in plan.get("slide_storyboard") or []}

    slides = []
    for index, path in enumerate(sorted((run_dir / "longform" / "ppt" / "slides").glob("*/payload.json"))):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        data = build_ppt_slide_data(payload, storyboard.get(path.parent.name), index)
        slides.append({
            "slide_id": path.parent.name,
            "title": _strip_scoring(str(payload.get("headline") or "")) or path.parent.name,
            "core_message": _strip_scoring(str(payload.get("key_message") or "")),
            **data,
        })

    plan_blocks = plan.get("chapter_blocks") or []
    artifact = {
        "title": str(brief.get("project_name") or "本项目"),
        "subtitle": "物业服务方案汇报",
        "chapters": [
            {"chapter_id": row.get("chapter_id"), "title": row.get("chapter_title") or row.get("chapter_id"), "goal": row.get("narrative_goal") or ""}
            for row in plan_blocks
        ],
        "slides": slides,
    }

    if with_images:
        from app_core import provider_manager

        image_dir = run_dir / "images"
        existing = sorted(image_dir.glob("image_*.png")) + sorted(image_dir.glob("image_*.jpg"))
        cover_path = str(existing[0]) if existing else ""
        chapter_paths = existing[1:]
        if cover_path and chapter_paths:
            print("复用已有图片:", len(existing), "张")
            images = {"cover": str(cover_path), "by_chapter": {}}
            chapter_ids = sorted({str(s.get("chapter_id") or "") for s in slides if s.get("chapter_id") and s.get("chapter_id") != "P01"})
            for cid, img_path in zip(chapter_ids[:5], chapter_paths[:5]):
                images["by_chapter"][cid] = str(img_path)
        else:
            try:
                image_provider = provider_manager().get("qwen_image", require_available=False)
            except Exception:
                image_provider = None
            images: dict = {}
            if image_provider is not None:
                try:
                    result = image_provider.generate_image(
                        "现代高端商务楼宇物业服务方案汇报封面背景，玻璃幕墙写字楼与专业服务团队剪影，深蓝绿色调，留白构图，商务摄影，无文字无水印",
                        image_dir, size="1024*1024",
                    )
                    images["cover"] = result["path"]
                    print("封面图 OK:", result["path"])
                except Exception as exc:
                    print("封面图失败:", str(exc)[:120])
                by_chapter: dict[str, str] = {}
                chapter_ids = sorted({str(s.get("chapter_id") or "") for s in slides if s.get("chapter_id") and s.get("chapter_id") != "P01"})
                for cid in chapter_ids[:5]:
                    try:
                        result = image_provider.generate_image(CHAPTER_PROMPTS.get(cid) or CHAPTER_PROMPTS["P02"], image_dir, size="720*1280")
                        by_chapter[cid] = result["path"]
                        print(f"章节图 {cid} OK")
                    except Exception as exc:
                        print(f"章节图 {cid} 失败:", str(exc)[:100])
                if by_chapter:
                    images["by_chapter"] = by_chapter
        if images:
            artifact["images"] = images

    out_dir = run_dir / "offline_rebuild"
    out_dir.mkdir(parents=True, exist_ok=True)
    content_path = out_dir / "content.json"
    content_path.write_text(json.dumps({"brief": brief, "artifact": artifact}, ensure_ascii=False), encoding="utf-8")

    runtime = json.loads((ROOT / "runtime" / "runtime_config.json").read_text(encoding="utf-8-sig"))
    import os

    env = {**os.environ,
           "RUNTIME_NODE": runtime["node_executable"],
           "RUNTIME_NODE_MODULES": runtime["node_modules"],
           "RUNTIME_BIN_DIR": runtime["bin_dir"]}
    qa_dir = out_dir / "qa"
    pptx_path = out_dir / f"{run_id}_offline.pptx"
    completed = subprocess.run(
        [runtime["node_executable"], "build_ppt.mjs", str(content_path), str(pptx_path), str(qa_dir)],
        cwd=str(ROOT / "app"), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    print("render rc:", completed.returncode)
    if completed.returncode != 0:
        print(completed.stderr[-1500:])
        return 1
    manifest = qa_dir / "image_manifest.json"
    if manifest.is_file():
        fix = subprocess.run(
            [runtime["python_executable"] or sys.executable, str(ROOT / "tools" / "fix_pptx_media.py"), str(pptx_path), str(manifest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        print("media fix:", (fix.stdout or fix.stderr).strip()[:200])
    print("PPTX:", pptx_path)
    print("PNG 目录:", qa_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
