"""Copy the last product PPT, export PDF and record honest gaps within the run clock."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from planning.ppt_delivery import completion_state
from providers.qoder_ppt_bridge import read
from providers.base import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--soffice", type=Path)
    args = parser.parse_args()
    root = args.instance.resolve()
    contract = read(root / "RUN_CONTRACT.json")
    if not contract.get("deadline_epoch"):
        raise ValueError("Missing current run clock")
    rid = str(read(root / "runtime/current_run.json").get("run_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rid):
        raise ValueError("Missing current isolated run identity")
    run = root / "runs" / rid
    out = root / "deliverables"
    out.mkdir(exist_ok=True)
    completion = completion_state(run)
    audit = read(run / "run_audit.json")
    name = Path(str(audit.get("artifact_display_name") or "_")).name
    candidates = [run / name, run / "artifact" / name]
    candidates += sorted(run.glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    candidates += sorted((run / "artifact").glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    source = next((p for p in candidates if p.is_file() and p.suffix.lower() == ".pptx"), None)
    report = {"run_id": rid, "ppt_completion": completion, "native_run_verified": False,
              "deadline_epoch": contract["deadline_epoch"], "external_paid_api_calls": 0,
              "content_qa": read(run / "ppt_content_qa.json"), "final_artifact_qa": read(run / "final_artifact_qa.json"),
              "human_visual_review": "PENDING", "quality_thresholds_changed": False}
    if source is None:
        report["status"] = "NO_PRODUCT_ARTIFACT"
        write_json(out / "CLOSEOUT_RESULT.json", report)
        return 1
    with zipfile.ZipFile(source) as z:
        if z.testzip():
            raise ValueError("Invalid product PPTX")
        pages = len([n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)])
    complete = completion is not None and completion["complete"]
    label = "产品稿" if complete else "部分稿"
    pptx = out / f"物业服务方案_{label}_{pages}页.pptx"
    shutil.copy2(source, pptx)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert hashlib.sha256(pptx.read_bytes()).hexdigest() == digest
    report.update(source_artifact=str(source), artifact_sha256=digest, physical_pages=pages,
                  pptx=str(pptx), status="PPT_SAVED_PDF_PENDING")
    write_json(out / "CLOSEOUT_RESULT.json", report)
    try:
        seconds_left = contract["deadline_epoch"] - time.time()
        if seconds_left <= 60:
            raise TimeoutError("Insufficient time left for export and review; original PPT has been saved")
        executable = str(args.soffice or os.environ.get("MMF_LIBREOFFICE_EXE") or shutil.which("soffice") or "")
        if not executable or not Path(executable).is_file():
            raise FileNotFoundError("LibreOffice executable must be supplied during preflight")
        export = root / "runtime/ppt_export"
        export.mkdir(exist_ok=True)
        carrier = export / "deck.pptx"
        shutil.copy2(pptx, carrier)
        profile = root / "runtime/lo_profile"
        profile.mkdir(exist_ok=True)
        proc = subprocess.run([executable, "-env:UserInstallation=" + profile.as_uri(), "--headless", "--convert-to", "pdf:impress_pdf_Export",
                               "--outdir", str(export), str(carrier)], capture_output=True, timeout=min(180, seconds_left - 60),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (export / "export.log").write_bytes(proc.stdout + proc.stderr)
        pdf_source = export / "deck.pdf"
        if proc.returncode or not pdf_source.is_file():
            raise RuntimeError("PDF export failed; inspect runtime/ppt_export/export.log")
        from pypdf import PdfReader
        reader = PdfReader(pdf_source)
        if len(reader.pages) != pages:
            raise ValueError("PDF/PPT physical page count mismatch")
        rows = [{"page": i, "text_chars": len(page.extract_text() or ""), "visual_review": "PENDING"} for i, page in enumerate(reader.pages, 1)]
        pdf = pptx.with_suffix(".pdf")
        shutil.copy2(pdf_source, pdf)
        report.update(pdf=str(pdf), pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), pages=rows,
                      status="EXPORTED_REVIEW_PENDING" if complete else "PARTIAL_EXPORTED_REVIEW_PENDING")
        table = ["| 页码 | PDF文本字符 | 视觉复核 |", "|---|---|---|"]
        table += [f"| {r['page']} | {r['text_chars']} | 待逐页检查 |" for r in rows]
        (out / "逐页检查表.md").write_text("# 自动导出检查\n\n此表不代表视觉验收通过。还需查看每页渲染，核查越界、图片变形、重复及图文对应。\n\n" + "\n".join(table) + "\n", encoding="utf-8")
    except Exception as exc:
        report.update(status="PPT_SAVED_EXPORT_FAILED", error=f"{type(exc).__name__}: {exc}")
    report.update(finished_epoch=time.time(), elapsed_from_instance_seconds=round(time.time() - contract.get("instance_created_epoch", contract["deadline_epoch"] - 5400), 3))
    report["within_90min_at_export"] = time.time() <= contract["deadline_epoch"]
    report["whole_delivery_90min_accepted"] = False  # Export alone is not review/acceptance.
    write_json(out / "CLOSEOUT_RESULT.json", report)
    note = (f"# 交付说明\n\n状态：{report['status']}。{pages}个物理页；{label}。\n\n"
            f"正文完成情况：{json.dumps(completion, ensure_ascii=False)}。\n\n"
            "PPT为产品输出的原字节副本。内容QA和最终文件QA见CLOSEOUT_RESULT.json，未修改质量门。自动导出完成不等于整套90分钟验收通过，视觉与业务复核仍待执行。\n")
    (out / "交付说明.md").write_text(note, encoding="utf-8")
    write_json(out / "文件清单.sha256.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file() and p.name != "文件清单.sha256.json"})
    return 0 if report["status"].endswith("EXPORTED_REVIEW_PENDING") else 1


if __name__ == "__main__":
    raise SystemExit(main())
