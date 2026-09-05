from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
DIST = PACKAGE / "dist" / "MMF-006E-portable-v0.1"
LONGFORM_ORCHESTRATOR_VERSION = "0.1-r1"
REQUIRED = (
    "app/planning/planner.py",
    "app/planning/canonical.py",
    "app/longform/factory.py",
    "app/longform/orchestrator.py",
    "app/providers/capability.py",
    "app/governance/longform_qa.py",
    "app/app_core.py",
    "启动MMF.cmd",
    "tools/launch_mmf.ps1",
)
IGNORE = shutil.ignore_patterns(
    "dist",
    "runtime",
    "runs",
    "output",
    "logs",
    "__pycache__",
    "node_modules",
    "*.pyc",
    "providers.local.json",
    "user_settings.json",
)


def _hash_files(root: Path) -> str:
    digest = hashlib.sha256()
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {"dist", "runtime", "runs", "output", "logs", "__pycache__", "node_modules"} for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".json", ".html", ".ps1", ".cmd", ".mjs", ".md"}:
            continue
        files.append(path)
    for path in sorted(files, key=lambda item: str(item.relative_to(root)).replace("\\", "/").lower()):
        rel = str(path.relative_to(root)).replace("\\", "/")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    cache_dir = PACKAGE / "app" / "__pycache__"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    source_hash = _hash_files(PACKAGE)
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")
    build_id = f"MMF006E-R1.1-{source_hash[:12]}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.copytree(PACKAGE, DIST, ignore=IGNORE)
    for leftover in (
        DIST / "config" / "providers.local.json",
        DIST / "config" / "user_settings.json",
    ):
        leftover.unlink(missing_ok=True)
    missing = [item for item in REQUIRED if not (DIST / item).is_file()]
    if missing:
        raise SystemExit("pack refused: missing " + ", ".join(missing))
    dist_core = (DIST / "app" / "app_core.py").read_text(encoding="utf-8")
    if "generate_longform(" not in dist_core:
        raise SystemExit("pack refused: dist app_core.py does not call generate_longform")
    if "provider.generate_solution(request" in dist_core:
        raise SystemExit("pack refused: dist still one-shot generate_solution in generate_artifact")
    bridge_cfg = DIST / "providers" / "grok_bridge" / "bridge_config.json"
    if bridge_cfg.is_file():
        cfg = json.loads(bridge_cfg.read_text(encoding="utf-8-sig"))
        cfg["grok_executable"] = ""
        bridge_cfg.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    skip_names = {"assemble_release.py", "test_cleanroom_deployment.py"}
    home = Path.home()
    needles = {
        str(home).lower(),
        str(home).replace("\\", "\\\\").lower(),
        home.as_posix().lower(),
        ("/users/" + home.name).lower(),
        ("\\users\\" + home.name).lower(),
    }
    leaks = []
    for path in DIST.rglob("*"):
        if not path.is_file() or path.name in skip_names:
            continue
        if path.suffix.lower() not in {".json", ".py", ".md", ".html", ".txt", ".ps1", ".cmd", ".mjs"}:
            continue
        if any(part in {"runtime", "runs", "output", "logs", "__pycache__", "site-packages"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if any(needle and needle in text for needle in needles):
            leaks.append(str(path.relative_to(DIST)))
    if leaks:
        raise SystemExit("pack refused: personal path leaked in " + ", ".join(leaks[:8]))
    (DIST / "runtime").mkdir(exist_ok=True)
    (DIST / "runs").mkdir(exist_ok=True)
    (DIST / "output").mkdir(exist_ok=True)
    (DIST / "logs").mkdir(exist_ok=True)
    (DIST / "config").mkdir(exist_ok=True)
    build_manifest = {
        "source_version": "0.1.0-alpha",
        "source_hash": source_hash,
        "build_time": built_at,
        "dist_version": "0.1.0-alpha-r1.1",
        "longform_orchestrator_version": LONGFORM_ORCHESTRATOR_VERSION,
        "build_id": build_id,
        "runtime_source": "dist",
        "included_longform": list(REQUIRED),
    }
    (DIST / "build_manifest.json").write_text(json.dumps(build_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (PACKAGE / "build_manifest.json").write_text(json.dumps({**build_manifest, "runtime_source": "source", "last_packed_dist": str(DIST)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path = PACKAGE / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    manifest.update({
        "portable_path": str(DIST),
        "packaged_at": built_at,
        "app_version": "0.1.0-alpha",
        "baseline_authority": "MMF-006D R10",
        "build_id": build_id,
        "source_hash": source_hash,
        "longform_orchestrator_version": LONGFORM_ORCHESTRATOR_VERSION,
    })
    (DIST / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "packed", "path": str(DIST), "build_id": build_id, "source_hash": source_hash}, ensure_ascii=False))


if __name__ == "__main__":
    main()
