from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import paths
from filesystem_paths import filesystem_path


RUN_FOLDERS = ("input", "plan", "provider_result", "artifact", "report", "log")


def ensure_run_layout(run_dir: Path) -> dict[str, Path]:
    filesystem_path(run_dir).mkdir(parents=True, exist_ok=True)
    mapping = {name: run_dir / name for name in RUN_FOLDERS}
    for folder in mapping.values():
        filesystem_path(folder).mkdir(parents=True, exist_ok=True)
    return mapping


def _copy_if_exists(source: Path, target: Path) -> None:
    source, target = filesystem_path(source), filesystem_path(target)
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
            "qa_review_summary.json",
            # §44：待投标人确认事项的内部随件汇总，与 qa_review_summary 同一层。
            "technical_review_summary.json",
            "last_valid_artifact.json",
        ],
        folders["log"]: ["artifact_build.stdout.log", "artifact_build.stderr.log"],
    }
    for target, names in copies.items():
        for name in names:
            _copy_if_exists(run_dir / name, target / name)
    for child in run_dir.iterdir():
        if child.is_dir() and child.name.startswith("provider_") and child.name not in RUN_FOLDERS:
            _copy_if_exists(child, folders["provider_result"] / child.name)
        if child.suffix.lower() in {".docx", ".pptx"}:
            _copy_if_exists(child, folders["artifact"] / child.name)
    output_dir = roots.output_root / run_id
    filesystem_path(output_dir).mkdir(parents=True, exist_ok=True)
    word_path = ""
    ppt_path = ""
    for artifact in folders["artifact"].glob("*"):
        if artifact.suffix.lower() not in {".docx", ".pptx"}:
            continue
        dest = output_dir / artifact.name
        if artifact.resolve() != dest.resolve():
            shutil.copy2(filesystem_path(artifact), filesystem_path(dest))
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
    # Scoring is bound to a path as well as bytes. Measure the delivery carrier
    # after copying; a binding to the run-root source is stale in artifact/.
    if (run_dir / "final_scoring_report.json").is_file():
        from longform.run_state import _landed_final_artifact
        from app_core import _refresh_final_scoring_delivery, read_json, write_json
        landed = _landed_final_artifact(run_dir, {".docx"})
        if landed:
            qa_path = run_dir / "final_artifact_qa.json"
            qa = read_json(qa_path, "final_artifact_qa") if qa_path.is_file() else {"findings": []}
            refreshed = _refresh_final_scoring_delivery(run_dir, Path(landed["path"]), qa)
            if qa_path.is_file():
                # Copies are byte-identical; this does not assert a new QA run.
                if qa.get("sha256") == landed["sha256"]:
                    refreshed["artifact_path"] = landed["path"]
                write_json(qa_path, refreshed)
    # Formal finalization boundary.  The authority derives the terminal state
    # after the artifact has landed, then projects it back to the returned/UI
    # payload and every persisted status surface.
    from longform.run_state import finalize_delivery_state
    authority = finalize_delivery_state(
        run_dir,
        medium=payload.get("medium"),
        draft_delivery=payload.get("delivery_status"),
    )
    derived = authority["derived"]
    payload.update({
        "delivery_status": derived["delivery_status"],
        "qa_state": derived["delivery_status"],
        "public_phase": derived["public_phase"],
        "internal_phase": derived["internal_phase"],
        "run_state_authority": authority.get("schema_version"),
    })
    return payload
