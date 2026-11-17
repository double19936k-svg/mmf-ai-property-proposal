from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
import os
import stat
import subprocess
import sys
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
DIST = PACKAGE / "dist" / "MMF-006E-portable-v0.1"
LONGFORM_ORCHESTRATOR_VERSION = "0.1-r2.7"
PRESERVE_DIST_DIRS = ("runtime", "output", "config", "runs", "logs")
LOCAL_CONFIG_NAMES = {"providers.local.json", "user_settings.json"}
REQUIRED = (
    "app/planning/ppt_service.py",
    "app/project_assets.py",
    "app/ppt_final_verify.py",
    "tests/test_ppt_service_contract.py",
    "app/filesystem_paths.py",
    "app/windows_paths.mjs",
    "app/planning/planner.py",
    "app/planning/content_engineering.py",
    "app/planning/canonical.py",
    "app/planning/manual_brief.py",
    "tests/test_manual_brief_ingestion.py",
    "app/longform/factory.py",
    "app/longform/orchestrator.py",
    "app/longform/batch_planner.py",
    "app/longform/cu_audit.py",
    "app/longform/scheduler.py",
    "app/longform/reasoning.py",
    "app/longform/eta.py",
    "app/longform/run_state.py",
    "app/workflow_timing.py",
    "app/providers/capability.py",
    "app/providers/rate_limit.py",
    "app/providers/capability_probe.py",
    "app/providers/token_usage.py",
    "app/governance/longform_qa.py",
    "app/governance/artifact_qa.py",
    "app/governance/commitment_provenance.py",
    "app/governance/final_artifact_qa.py",
    "app/governance/toc_qa.py",
    "app/governance/text_sanitize.py",
    "app/governance/customer_hygiene.py",
    "app/tender_intake/pack_builder.py",
    "app/tender_intake/models.py",
    "app/tender_intake/confirmation.py",
    "app/app_core.py",
    "app/run_layout.py",
    "app/providers/mock.py",
    "app/workflow_policy.py",
    "app/recovery_orchestrator.py",
    "app/delivery_state.py",
    "app/qa_severity.py",
    "app/longform/repair_policy.py",
    "app/longform/envelope.py",
    "app/longform/local_normalize.py",
    "app/longform/cu_state.py",
    "app/longform/depth_supplement.py",
    "app/longform/supplement_registry.py",
    "app/r07_table_layout.py",
    "tests/test_run_k_r07_table_layout.py",
    "tests/test_run_k_r07_supplement_preservation.py",
    "tests/test_run_k_r07_source_projection.py",
    "app/longform/document_repair.py",
    "app/governance/document_qa.py",
    # MMF-007C Round 3：P0-1/P0-2/P0-4/P0-5/P1 五块新门都落在这几个模块里，
    # 少带任何一个，dist 里的 import 就会在第一次真实运行时炸。
    "app/governance/table_qa.py",
    "app/planning/budget_control.py",
    "app/planning/page_calibration.py",
    "app/planning/scoring_evidence.py",
    "app/planning/content_padding.py",
    "app/providers/qoder_internal.py",
    "app/providers/network_lockdown.py",
    "tools/qoder_bridge.py",
    "tests/test_mmf007c_qa_pipeline.py",
    "tests/test_mmf007c_page_calibration.py",
    "tests/test_mmf007c_qa_precision.py",
    "tests/test_mmf007c_qoder_internal_bridge.py",
    # MMF-007C Round 3 新增的门禁测试：随包一起验，缺一条就说明 dist 少带了东西。
    "tests/test_mmf007c_table_density.py",
    "tests/test_mmf007c_form_table_governance.py",
    "tests/test_mmf007c_bidder_evidence_boundary.py",
    "tests/test_mmf007c_content_padding_ruler.py",
    "tests/test_mmf007c_technical_only_scope.py",
    "tests/test_mmf007c_depth_supplement.py",
    "tests/test_mmf007c_late_response.py",
    "tests/test_mmf007c_throughput.py",
    "tests/test_mmf007c_toc_pagination.py",
    "app/server.py",
    "static/index.html",
    "app/package.json",
    "app/check_ppt_runtime.mjs",
    "providers/grok_bridge/grok_bridge.py",
    "启动MMF.cmd",
    "tools/launch_mmf.ps1",
    "tests/test_mmf006e_r241_workflow_timing.py",
    "tests/test_mmf006e_r24_artifact_qa.py",
    "tests/test_mmf006e_r242_live_artifact_closure.py",
    "tests/test_mmf006e_r243_provider_recognition.py",
    "tests/test_mmf006e_r2_stability_freeze.py",
    "tests/test_mmf006e_r25_recovery_hygiene.py",
    "tests/test_mmf006e_r26_fail_soft_delivery.py",
    "tests/test_mmf006e_r27_fast_performance.py",
    "tests/test_mmf007_final_closure.py",
    "tests/test_mmf007_production_faults.py",
    "tests/test_mmf007b_content_engineering.py",
    "tests/test_mmf007b_real_run_fix.py",
    "tests/test_image_provider_endpoints.py",
    # MMF-007C Round 4 / 4.1：评分映射与状态权威这四条链的门禁模块。少带任何一个，
    # dist 里的 import 就会在第一次真实运行时炸；`tests/conftest.py` 更是测试侧
    # 网络禁令唯一的落点（R41 §30 之后它引用的是产品那份判定），漏带等于整包失锁。
    "app/planning/hierarchy.py",
    "app/planning/scoring_taxonomy.py",
    "app/planning/scoring_mapping.py",
    "app/planning/unified_budget.py",
    "app/planning/scope_gated_requirements.py",
    "tests/conftest.py",
    "tests/test_mmf007a_hierarchy.py",
    "tests/test_mmf007c_scoring_taxonomy.py",
    "tests/test_mmf007c_scope_gated_requirements.py",
    "tests/test_mmf007c_unified_budget.py",
    "tests/test_mmf007c_reduction_executor.py",
    "tests/test_mmf007c_r4_final_state_contract.py",
    "tests/test_mmf007c_r4_table_governance_budget.py",
    "tests/test_mmf007c_r41_scoring_auto_mapping.py",
    "tests/test_mmf007c_r41_final_qa_fail_closed.py",
    "tests/test_mmf007c_r41_page_calibration.py",
    "tests/test_mmf007c_r41_structure_survival.py",
    "tests/test_mmf007c_r41_network_lockdown_single_ruler.py",
    "tools/mmf007b_synthetic_validation.py",
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
    ".git",          # Git 安全整改：主仓 .git 不得进入发布包
    ".pytest_cache",
    ".pack_preserve_r22",
    "migration_backup",
    # Frozen reviewer dispatch instructions contain machine-local paths and are
    # development evidence, rather than a portable runtime interface.
    "RUN_J_ZCODE_INDEPENDENT_GATE.md",
)


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        st = path.lstat()
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _safe_rmtree(path: Path, expected: Path) -> None:
    resolved = path.resolve()
    if resolved != expected.resolve():
        raise SystemExit(f"pack refused: rmtree target {resolved} != {expected}")
    if _is_reparse_point(path) or _is_reparse_point(resolved):
        raise SystemExit(f"pack refused: refusing recursive delete of reparse/junction {path}")
    shutil.rmtree(resolved)


def _refuse_running_dist(dist: Path) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for name in ("server.pid", "watchdog.pid"):
        file = dist / "runtime" / name
        if not file.is_file():
            continue
        try:
            pid = int(file.read_text(encoding="utf-8-sig").strip())
        except (ValueError, OSError):
            continue
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 5:
                raise SystemExit("pack refused: unable to confirm MMF process stopped")
            continue
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259:
                raise SystemExit("pack refused: stop the running MMF before rebuilding; dist is unchanged")
        finally:
            kernel.CloseHandle(handle)


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


def _measure_tests_at_build() -> int:
    """构建清单里的 `tests_at_build` 必须当场量出来，不能抄上一份。

    Round 2/Round 3 各有三次构建因为"建完又改这个数字"被作废（见
    MMF-007_review_bundle.json 的 invalidated_builds）—— 字面量就是那个根因。
    必须在任何破坏性步骤之前量：collect 失败时 dist 必须还是原样。
    """
    venv_python = PACKAGE / "runtime" / "dev_python" / "Scripts" / "python.exe"
    interpreter = str(venv_python) if venv_python.is_file() else sys.executable
    collect_temp = Path(os.environ.get("MMF_PACK_COLLECT_EVIDENCE_DIR") or
                        PACKAGE / "runtime" / "scratch_pack_collect")
    check = subprocess.run(
        [interpreter, "-m", "pytest", "tests", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--basetemp", str(collect_temp)],
        cwd=str(PACKAGE), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    if check.returncode != 0:
        raise SystemExit("pack refused: test collection failed; existing dist was not changed\n"
                         + (check.stdout or "")[-2000:] + (check.stderr or "")[-2000:])
    tail = [line for line in (check.stdout or "").splitlines() if line.strip()]
    for line in reversed(tail):
        marker = line.find(" tests collected")
        if marker >= 0:
            count = int(line[:marker].strip().split()[-1])
            break
    else:
        collected = [line for line in tail if "::" in line and not line.startswith((".", "E", "W"))]
        if not collected:
            raise SystemExit("pack refused: test collection reported no count; existing dist was not changed")
        count = len(collected)
    if count <= 0:
        raise SystemExit("pack refused: test collection produced 0 tests; existing dist was not changed")
    return count


def main() -> None:
    dist = DIST.resolve()
    expected = (PACKAGE / "dist" / "MMF-006E-portable-v0.1").resolve()
    if dist != expected:
        raise SystemExit(f"pack refused: resolved dist {dist} != {expected}")
    if _is_reparse_point(DIST) or _is_reparse_point(expected):
        raise SystemExit("pack refused: dist path is a junction/reparse point")
    _refuse_running_dist(DIST)
    node = shutil.which("node")
    if not node:
        raise SystemExit("pack refused: Node is required to validate the bundled PPT runtime")
    ppt_source = None
    for candidate in (PACKAGE / "app/node_modules", DIST / "app/node_modules"):
        if not candidate.is_dir() or _is_reparse_point(candidate):
            continue
        check = subprocess.run([node, str(PACKAGE / "app/check_ppt_runtime.mjs"), str(candidate)], capture_output=True, timeout=30)
        if check.returncode == 0:
            ppt_source = candidate
            break
    if ppt_source is None:
        raise SystemExit("pack refused: complete loadable PPT dependencies are required; existing dist was not changed")
    tests_at_build = _measure_tests_at_build()
    preserve_root = PACKAGE / "dist" / ".pack_preserve_r22"
    if preserve_root.exists():
        raise SystemExit("pack refused: previous recovery backup exists; restore/preserve it before retrying")
    preserved: dict[str, Path] = {}
    ppt_backup = preserve_root / "ppt_node_modules"
    shutil.copytree(ppt_source, ppt_backup)
    if dist.exists():
        for name in PRESERVE_DIST_DIRS:
            src = dist / name
            if src.exists():
                dest = preserve_root / name
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                preserved[name] = dest
        _safe_rmtree(dist, expected)
    cache_dir = PACKAGE / "app" / "__pycache__"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    source_hash = _hash_files(PACKAGE)
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(PACKAGE),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")
    build_id = f"MMF006E-R2.7-{source_hash[:12]}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
        if path.name in LOCAL_CONFIG_NAMES:
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
    for name, src in preserved.items():
        dest = DIST / name
        if name in {"runtime", "output"}:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        elif name == "config":
            dest.mkdir(exist_ok=True)
            for item in src.iterdir():
                # Preserve only machine-local user choices.  Restoring the
                # whole old config directory would overwrite the newly built
                # providers.example.json and silently discard new providers.
                if item.name not in LOCAL_CONFIG_NAMES:
                    continue
                target = dest / item.name
                if item.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
        elif name in {"runs", "logs"}:
            dest.mkdir(exist_ok=True)
            for item in src.iterdir():
                target = dest / item.name
                if item.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(item, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(item, target)
    shutil.copytree(ppt_backup, DIST / "app/node_modules")
    ppt_check = subprocess.run([node, str(DIST / "app/check_ppt_runtime.mjs"), str(DIST / "app/node_modules")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if ppt_check.returncode != 0:
        raise SystemExit("pack failed PPT import check; preserved data retained in .pack_preserve_r22")
    if preserve_root.exists():
        shutil.rmtree(preserve_root)
    build_manifest = {
        "source_version": "0.1.1-alpha",
        "source_hash": source_hash,
        "build_time": built_at,
        "dist_version": "0.1.1-alpha-r2.7-fast-perf",
        "public_package_excludes_local_config": True,
        "longform_orchestrator_version": LONGFORM_ORCHESTRATOR_VERSION,
        "build_id": build_id,
        "build_git_sha": git_sha,
        "runtime_source": "dist",
        "tests_at_build": tests_at_build,
        "tests_at_build_provenance": "measured by pytest --collect-only -q on the source tree during this pack run",
        "ppt_dependencies": {"status": "PASS", "version": json.loads(ppt_check.stdout).get("version"), "bundled": True},
        "included_longform": list(REQUIRED),
    }
    (DIST / "build_manifest.json").write_text(json.dumps(build_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # The final build must not dirty the committed source tree.  The authoritative
    # build manifest lives in production_dist; the source-tree manifest is retained
    # as historical metadata and is not rewritten after the final commit.
    manifest_path = PACKAGE / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    manifest.update({
        "portable_path": str(DIST),
        "packaged_at": built_at,
        "app_version": "0.1.1-alpha",
        "baseline_authority": "MMF-006D R10",
        "build_id": build_id,
        "source_hash": source_hash,
        "longform_orchestrator_version": LONGFORM_ORCHESTRATOR_VERSION,
    })
    (DIST / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "packed", "path": str(DIST), "build_id": build_id, "source_hash": source_hash}, ensure_ascii=False))


if __name__ == "__main__":
    main()
