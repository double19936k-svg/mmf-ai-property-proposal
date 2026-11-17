"""Read-only integrity verification of the local Run J visual gate packet.

This is implementation-author evidence, not an independent gate approval.
No generation, provider, network connection or artifact mutation is performed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL).decode("utf-8").strip()


def main():
    parser = argparse.ArgumentParser()
    current = ROOT / "runtime/run_j/fix_v2/RUN_J_FINAL_FREEZE.json"
    parser.add_argument("--manifest", type=Path, default=current if current.exists() else ROOT / "runtime/run_j/RUN_J_FINAL_FREEZE.json")
    args = parser.parse_args()
    packet = json.loads(args.manifest.read_text(encoding="utf-8"))
    require(packet["schema_version"] == "run-j-visual-freeze-v1", "unsupported freeze packet")
    require(packet["independent_gate"]["status"] == "PENDING_ZCODE", "freeze cannot self-approve independent gate")
    require(git("rev-parse", "HEAD") == packet["head"], "HEAD differs from frozen review target")
    require(git("branch", "--show-current") == "run-j-visual", "wrong branch")
    require(not git("status", "--porcelain"), "tracked/untracked source changes after freeze")
    require(git("merge-base", packet["base"], packet["head"]) == packet["base"], "wrong baseline")
    require(not git("rev-list", "--merges", packet["base"] + ".." + packet["head"]), "Run J contains a merge")
    changed = git("diff", "--name-only", packet["base"], packet["head"]).splitlines()
    require(changed == packet["changed_modules"], "changed-file boundary differs")
    for module in packet["tested_implementation_modules"]:
        require(not git("diff", packet["tested_implementation_head"], packet["head"], "--", module),
                "tested visual implementation changed: " + module)
    for record in packet["frozen_files"]:
        path = (ROOT / record["file"]).resolve()
        require(path.is_relative_to(ROOT), "freeze file escapes Run J")
        require(path.is_file() and sha256(path) == record["sha256"], "frozen file mismatch: " + record["file"])
    source_root = Path(packet["source_root"]).resolve()
    for record in packet["original_source_files"]:
        path = (source_root / record["file"]).resolve()
        require(path.is_relative_to(source_root), "source locator escapes frozen source")
        require(path.is_file() and sha256(path) == record["sha256"], "original source mismatch: " + record["file"])
    for record in packet["runtime_dependencies"]:
        path = Path(record["file"])
        require(path.is_file() and sha256(path) == record["sha256"], "runtime/font mismatch: " + str(path))
    sys.path.insert(0, str(ROOT / "app"))
    from visuals.ir import validate
    cases = json.loads((ROOT / packet["visual_ir_file"]).read_text(encoding="utf-8"))
    require(len(cases) == 7, "real-control count changed")
    results = [validate(case, source_root) for case in cases]
    require({case["section_id"] for case in cases} == {"S03-01", "S04-02", "S06-02", "S10-01", "S11-01"},
            "five real tender controls changed")
    require(packet["provider_calls"] == 0, "unexpected provider-call declaration")
    print(json.dumps({"status": "FREEZE_INTEGRITY_PASS", "head": packet["head"],
                      "frozen_files": len(packet["frozen_files"]), "original_source_files": len(packet["original_source_files"]),
                      "visuals_validated": len(results), "provider_calls_this_verification": 0,
                      "independent_gate": "PENDING_ZCODE"}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print("FREEZE_INTEGRITY_FAIL: " + str(exc), file=sys.stderr)
        sys.exit(2)
