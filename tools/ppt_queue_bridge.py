"""Persistent transcript return + deadline closeout, independent of parent chat turns."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from providers.qoder_ppt_bridge import PPTBridge, read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["claim", "sweep", "serve"])
    parser.add_argument("--instance", required=True, type=Path)
    parser.add_argument("--worker", choices=["w1", "w2"])
    parser.add_argument("--closeout-script", type=Path)
    args = parser.parse_args()
    bridge = PPTBridge(args.instance)
    if args.action == "claim":
        print(json.dumps(bridge.claim(args.worker), ensure_ascii=False))
        return 0
    if args.action == "sweep":
        print(json.dumps({"returned": bridge.sweep(), **bridge.health()}, ensure_ascii=False))
        return 0
    if args.closeout_script is None or not args.closeout_script.is_file() or not args.closeout_script.resolve().is_relative_to(bridge.root):
        raise ValueError("serve requires a tested closeout script saved inside this instance")
    guard_lock = bridge.state / "guard.lock"
    descriptor = os.open(guard_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    proc = None
    try:
        while True:
            bridge.sweep()
            state = bridge.health()
            rid = read(bridge.root / "runtime/current_run.json").get("run_id")
            status = read(bridge.root / "runs" / str(rid or "_") / "generation_status.json").get("status")
            ready = status in {"completed", "generation_partial", "failed", "cancelled", "technical_failed"}
            # Give the bounded generator up to 30s to flush its final checkpoints.
            cutoff = time.time() >= bridge.contract["generation_stop_epoch"] + 30
            if (ready or cutoff) and proc is None:
                if cutoff:
                    (bridge.q / "STOP").write_text("GENERATION_CUTOFF\n", encoding="utf-8")
                log = (bridge.state / "closeout.log").open("w", encoding="utf-8")
                proc = subprocess.Popen([sys.executable, "-B", str(args.closeout_script)], cwd=bridge.root,
                                        stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                log.close()
                bridge.event("CLOSEOUT_STARTED", pid=proc.pid, reason="terminal" if ready else "generation_cutoff")
            if proc is not None and proc.poll() is not None:
                bridge.event("CLOSEOUT_FINISHED", returncode=proc.returncode)
                return proc.returncode
            if state["status"] == "DEADLINE":
                (bridge.q / "STOP").write_text("WALL_CLOCK_LIMIT\n", encoding="utf-8")
                bridge.event("WALL_CLOCK_LIMIT", closeout_finished=False)
                return 2
            time.sleep(1)
    finally:
        guard_lock.unlink(missing_ok=True)
        if proc is not None and proc.poll() is None:
            # Only our own closeout child; never stop a user's Office/Qoder process.
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
