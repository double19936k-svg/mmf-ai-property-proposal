from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import paths


RUN_FOLDERS = ("input", "plan", "provider_result", "artifact", "report", "log")


def ensure_run_layout(run_dir: Path) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    mapping = {name: run_dir / name for name in RUN_FOLDERS}
    for folder in mapping.values():
        folder.mkdir(parents=True, exist_ok=True)
    return mapping


def _copy_if_exists(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if target.exists():
            return
        shutil.copytree(source, target, dirs_exist_ok=True)
        return
    if not target.exists():
        shutil.copy2(source, target)


def finalize_run(run_id: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    roots = paths.current()
    run_dir = roots.runs_dir / run_id
    folders = ensure_run_layout(run_dir)
    copies = {
        folders["input"]: ["brief.json", "generation_input.json"],
        folders["plan"]: ["knowledge_selection.json"],
        folders["report"]: [
            "compliance_report.json",
            "commitment_provenance_report.json",
            "artifact_qa_report.json",
            "run_audit.json",
            "generation_status.json",
        ],
        folders["log"]: ["artifact_build.stdout.log", "artifact_build.stderr.log"],
    }
    for target, names in copies.items():
        for name in names:
            _copy_if_exists(run_dir / name, target / name)
    for child in run_dir.iterdir():
        if child.is_dir() and child.name.startswith("provider_"):
            _copy_if_exists(child, folders["provider_result"] / child.name)
        if child.suffix.lower() in {".docx", ".pptx"}:
            _copy_if_exists(child, folders["artifact"] / child.name)
    output_dir = roots.output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    word_path = ""
    ppt_path = ""
    for artifact in folders["artifact"].glob("*"):
        if artifact.suffix.lower() not in {".docx", ".pptx"}:
            continue
        dest = output_dir / artifact.name
        if artifact.resolve() != dest.resolve():
            shutil.copy2(artifact, dest)
        if artifact.suffix.lower() == ".docx":
            word_path = str(dest)
        else:
            ppt_path = str(dest)
    payload = dict(result or {})
    payload.update({
        "run_id": run_id,
        "run_dir": str(run_dir),
        "output_dir": str(output_dir),
        "word_path": word_path,
        "ppt_path": ppt_path,
        "artifact_dir": str(folders["artifact"]),
        "log_dir": str(folders["log"]),
    })
    return payload
