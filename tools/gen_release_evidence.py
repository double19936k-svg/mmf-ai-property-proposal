"""生成 release_evidence.json（V1.1 Release Evidence 要求）。

用法：python tools/gen_release_evidence.py [dist_dir]
默认 dist：MMF_Application/dist/MMF-006E-portable-v0.1
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
DIST = PACKAGE / "dist" / "MMF-006E-portable-v0.1"


def dependency_evidence(dist: Path, manifest: dict) -> tuple[dict, dict]:
    """Read exact pins from the candidate itself; refuse inconsistent Node bytes."""
    requirements = dist / "app/requirements.txt"
    dependencies = {}
    for line in requirements.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        if not match or match[1] in dependencies:
            raise ValueError(f"release evidence requires unique exact Python pins: {line!r}")
        dependencies[match[1]] = match[2]
    package = dist / "app/package.json"
    declared = json.loads(package.read_text(encoding="utf-8-sig")).get("dependencies", {})
    if not declared or not isinstance(declared, dict):
        raise ValueError("release evidence requires declared Node dependencies")
    sources = [requirements, package]
    for name, pin in declared.items():
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", str(pin)):
            raise ValueError(f"release evidence requires an exact Node pin: {name}")
        installed = dist / "app/node_modules" / name / "package.json"
        actual = json.loads(installed.read_text(encoding="utf-8-sig"))
        if actual.get("name") != name or actual.get("version") != pin:
            raise ValueError(f"bundled dependency disagrees with declared pin: {name}")
        dependencies[name] = pin
        sources.append(installed)
    if (manifest.get("ppt_dependencies") or {}).get("version") != dependencies.get("@oai/artifact-tool"):
        raise ValueError("build manifest disagrees with bundled @oai/artifact-tool")
    provenance = {
        str(path.relative_to(dist)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sources
    }
    return dependencies, provenance


def main() -> int:
    dist = Path(sys.argv[1]) if len(sys.argv) > 1 else DIST
    manifest = json.loads((dist / "build_manifest.json").read_text(encoding="utf-8-sig"))
    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(PACKAGE), capture_output=True, text=True).stdout.strip()
    build_git_sha = str(manifest.get("build_git_sha") or "")
    if not git_sha or build_git_sha != git_sha:
        raise SystemExit(f"release evidence refused: build_git_sha={build_git_sha!r} HEAD={git_sha!r}")

    digest = hashlib.sha256()
    count = 0
    for p in sorted(dist.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(dist)).replace("\\", "/")
            # Self-exclusion makes the package digest reproducible across evidence
            # regeneration; the evidence file cannot hash itself.
            if rel == "release_evidence.json":
                continue
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(p.read_bytes())
            count += 1

    dependencies, dependency_sources = dependency_evidence(dist, manifest)
    evidence = {
        "schema_version": "release-evidence-v0.2",
        "git_sha_devtree": git_sha,
        "build_git_sha": build_git_sha,
        "build_id": manifest.get("build_id"),
        "source_version": manifest.get("source_version"),
        "dist_version": manifest.get("dist_version"),
        "source_hash": manifest.get("source_hash"),
        "package_sha256": digest.hexdigest(),
        "package_file_count": count,
        "dependency_manifest": dependencies,
        "dependency_manifest_basis": "exact declarations and bundled Node package metadata; Python installation is not inferred",
        "dependency_manifest_sources_sha256": dependency_sources,
        "runtime_python": {
            "bundled_executable_present": (dist / "runtime/python/Scripts/python.exe").is_file(),
            "installation_entrypoint": "tools/install.ps1",
            "validation": "NOT_EXECUTED_BY_EVIDENCE_GENERATOR",
        },
        "build_timestamp": manifest.get("build_time"),
        "runtime_source": "dist",
        "tests_at_build": int(manifest.get("tests_at_build") or 0),
        "tests_at_build_provenance": manifest.get("tests_at_build_provenance"),
        "certification": "NOT_GRANTED_BY_EVIDENCE_GENERATOR",
        "built_by": "tools/pack_portable.py",
        "note": "本文件记录当前候选的构建身份、包摘要与依赖来源；全量回归、独立 Gate、真实生成及人工验收以各自审查记录为准。",
    }
    out = dist / "release_evidence.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("build_id", "package_sha256", "package_file_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
