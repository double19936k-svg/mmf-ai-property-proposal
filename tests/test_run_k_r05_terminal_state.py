"""Terminal publication races and default deployment roots through production entry points."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from dataclasses import replace

import paths
import server
from generation_lifecycle import run_lock, write_progress
from providers.openai_compatible import bind_run, is_cancelled, unbind_run

class TerminalStateTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="r05_state_")
        self.base = Path(self.work.name)
        self.rid = "r05_" + self.base.name.replace("-", "_")
        self.run = self.base / self.rid
        self.run.mkdir()
        self.roots = patch.object(paths, "current", return_value=replace(paths.current(), runs_dir=self.base, logs_dir=self.base/"logs"))
        self.roots.start()
        self.run.joinpath("tender").mkdir()
        self.write("tender/status.json", {"pack_status": "ready_for_plan", "updated_at": "source_time"})
        self.write("workflow_timing.json", {"status": "completed", "workflow_completed_at": "kept_time"})
        self.write("run_audit.json", {"status": "generation_completed", "generation_finished_at": "kept_time"})

    def tearDown(self):
        unbind_run(self.rid)
        self.roots.stop()
        self.work.cleanup()

    def write(self, name, value):
        self.run.joinpath(name).write_text(json.dumps(value), encoding="utf-8")

    def bytes(self):
        return {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}

    def test_all_terminal_states_are_read_only_on_stop(self):
        for status in ("completed", "generation_completed", "failed", "cancelled"):
            with self.subTest(status=status):
                self.write("generation_status.json", {"status": status, "finished_at": "kept", "elapsed_seconds": 91,
                           "result": {"delivery_status": "NEEDS_REVIEW"}})
                before = self.bytes()
                with patch.object(server, "abort_run_http") as abort:
                    result = server.cancel_generation(self.rid)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["finished_at"], "kept")
                self.assertEqual(self.bytes(), before)
                abort.assert_not_called()

    def test_running_cancel_aborts_only_once_and_preserves_finished_intake(self):
        self.write("generation_status.json", {"status": "running", "started_at": "2026-10-07T00:00:00+08:00"})
        bind_run(self.rid)
        tender = (self.run / "tender/status.json").read_bytes()
        self.assertEqual(server.cancel_generation(self.rid)["status"], "cancelled")
        self.assertTrue(is_cancelled(self.rid))
        after = self.bytes()
        self.assertTrue(server.cancel_generation(self.rid)["idempotent"])
        self.assertEqual(self.bytes(), after)
        self.assertEqual((self.run / "tender/status.json").read_bytes(), tender)

    def test_idle_stop_does_not_create_generation_status(self):
        before = self.bytes()
        self.assertEqual(server.cancel_generation(self.rid)["status"], "not_running")
        self.assertEqual(self.bytes(), before)

    def test_bad_status_is_preserved_and_stop_fails(self):
        (self.run / "generation_status.json").write_text("{BROKEN", encoding="utf-8")
        before = self.bytes()
        with self.assertRaises(json.JSONDecodeError):
            server.cancel_generation(self.rid)
        self.assertEqual(before, self.bytes())

    def test_wrong_service_identity_cannot_close_or_change_business_state(self):
        before = self.bytes()
        with self.assertRaises(server.MMFError):
            server.request_service_shutdown({"process_id": -1})
        self.assertEqual(before, self.bytes())

    def test_delayed_heartbeat_cannot_reopen_completed_job(self):
        self.write("generation_status.json", {"status": "completed", "finished_at": "kept"})
        before = self.bytes()
        self.assertFalse(write_progress(self.run / "generation_status.json",
                         {"run_id": self.rid, "status": "running"}, server.write_json))
        self.assertEqual(before, self.bytes())

    def test_late_stage_records_cost_without_reopening_cancelled_workflow(self):
        from workflow_timing import ensure_workflow, finish_workflow, record_stage_duration
        ensure_workflow(self.run, "generation")
        stopped = finish_workflow(self.run, "cancelled")
        measured = record_stage_duration(self.run, "longform_generation", 12, outcome="failed")
        self.assertEqual(measured["status"], "cancelled")
        self.assertEqual(measured["workflow_completed_at"], stopped["workflow_completed_at"])
        self.assertEqual(measured["stage_timings"]["longform_generation"]["elapsed_seconds"], 12)
        self.assertEqual(ensure_workflow(self.run, "generation")["status"], "processing")

    def test_cancel_wins_before_artifact_publication(self):
        self.write("generation_status.json", {"status": "running"})
        entered, resume = threading.Event(), threading.Event()
        def generate(*args, **kwargs):
            entered.set()
            self.assertTrue(resume.wait(4))
            return {"status": "generation_completed"}
        with patch.object(server, "generate_artifact", side_effect=generate), \
             patch.object(server, "finalize_run") as publish, \
             patch.object(server, "_salvage_first_draft_on_stop", return_value={}):
            worker = threading.Thread(target=server._run_generation_job, args=(self.rid, [], {}))
            worker.start()
            self.assertTrue(entered.wait(4))
            server.cancel_generation(self.rid)
            stopped = self.bytes()
            resume.set()
            worker.join(4)
            self.assertFalse(worker.is_alive())
            publish.assert_not_called()
            self.assertEqual(json.loads((self.run/"generation_status.json").read_text())["status"], "cancelled")
            self.assertEqual(stopped, self.bytes())

    def test_publication_wins_and_late_stop_is_idempotent(self):
        self.write("generation_status.json", {"status": "running"})
        published, release = threading.Event(), threading.Event()
        def finalize(*args):
            published.set()
            self.assertTrue(release.wait(4))
            return {"status": "generation_completed", "delivery_status": "NEEDS_REVIEW"}
        with patch.object(server, "generate_artifact", return_value={"status": "generation_completed"}), \
             patch.object(server, "finalize_run", side_effect=finalize):
            worker = threading.Thread(target=server._run_generation_job, args=(self.rid, [], {}))
            worker.start()
            self.assertTrue(published.wait(4))
            output = []
            stopper = threading.Thread(target=lambda: output.append(server.cancel_generation(self.rid)))
            stopper.start()
            release.set()
            worker.join(4); stopper.join(4)
            self.assertFalse(worker.is_alive() or stopper.is_alive())
            self.assertEqual(output[0]["status"], "completed")
            self.assertTrue(output[0]["idempotent"])
            before = self.bytes()
            server.cancel_generation(self.rid)
            self.assertEqual(before, self.bytes())

class DefaultDeploymentTests(unittest.TestCase):
    def test_default_import_and_http_start_leave_package_immutable(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="r05_default_") as tmp:
            env = {k:v for k,v in os.environ.items() if not k.startswith("MMF_")}
            env.update(MMF_PACKAGE_ROOT=str(repo), MMF_PROVIDER_NETWORK_LOCKDOWN="1",
                       MMF_TEST_PROVIDER_LOCKDOWN="1", MMF_CREDENTIAL_NAMESPACE="r05_default_test",
                       LOCALAPPDATA=tmp, APPDATA=tmp, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            code = r'''
import sys, os, json, threading, urllib.request
from pathlib import Path
root = Path(os.environ["MMF_PACKAGE_ROOT"]).resolve()
def guard(event, args):
    name = args[0] if args else None
    writes = (event == "open" and len(args)>2 and
              (("w" in str(args[1]) or "a" in str(args[1]) or "x" in str(args[1])) or
               isinstance(args[2], int) and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT)))
    writes = writes or event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir"}
    if writes and isinstance(name, (str,bytes,os.PathLike)):
        targets = [name, args[1]] if event == "os.rename" else [name]
        for target in targets:
            if Path(target).resolve().is_relative_to(root):
                raise RuntimeError("PACKAGE_WRITE_REJECTED")
sys.addaudithook(guard)
import server, paths, app_core
from http.server import ThreadingHTTPServer
roots = paths.current()
assert not roots.runtime_dir.is_relative_to(root)
assert not roots.config_root.is_relative_to(root)
assert app_core.STATE_FILE.is_file() and app_core.REVIEW_FILE.is_file()
assert app_core.PROVIDERS.local_path.is_file()
service = ThreadingHTTPServer(("127.0.0.1",0), server.Handler)
worker = threading.Thread(target=service.serve_forever, daemon=True); worker.start()
try:
    for route in ("/api/health", "/api/config"):
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:%s%s"%(service.server_port,route)) as response:
            assert response.status == 200
finally:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        "http://127.0.0.1:%s/api/health"%service.server_port) as response:
        identity = json.load(response)
    request = urllib.request.Request("http://127.0.0.1:%s/api/service/shutdown"%service.server_port,
        data=json.dumps(identity).encode(), headers={"Content-Type":"application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request) as response:
        assert json.load(response)["business_state_modified"] is False
    worker.join(5); assert not worker.is_alive(); service.server_close()
print("DEFAULT_DEPLOYMENT_IMMUTABLE_PASS")
'''
            result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn("DEFAULT_DEPLOYMENT_IMMUTABLE_PASS", result.stdout)

    def test_distinct_packages_get_distinct_default_profiles(self):
        with tempfile.TemporaryDirectory(prefix="r05_profiles_") as tmp, patch.dict(os.environ, {}, clear=True):
            os.environ["LOCALAPPDATA"] = tmp
            a = paths.build_roots(Path(tmp)/"p", {})
            b = paths.build_roots(Path(tmp)/"q", {})
            self.assertNotEqual(a.config_root, b.config_root)
            self.assertNotEqual(a.runs_dir, b.runs_dir)
            self.assertFalse(a.data_root.is_relative_to(a.package_root))

    def test_explicit_writable_roots_are_honored(self):
        with tempfile.TemporaryDirectory(prefix="r05_explicit_") as tmp, patch.dict(os.environ, {}, clear=True):
            os.environ.update(LOCALAPPDATA=tmp, MMF_DATA_ROOT=str(Path(tmp)/"chosen"))
            roots = paths.build_roots(Path(tmp)/"package", {})
            self.assertEqual(roots.data_root, Path(tmp)/"chosen")
            self.assertEqual(roots.runs_dir, roots.data_root/"runs")
