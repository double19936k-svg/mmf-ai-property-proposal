"""Product delivery: direct artifact, internal master and external copy stay separately bound."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
TESTS = Path(__file__).resolve().parent
for item in (APP, TESTS):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import app_core
import test_mmf006e_r2_scheduler as fixtures
from providers.mock import MockProvider


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_word_run_keeps_direct_internal_and_external_artifacts(tmp_path):
    runs, runtime = tmp_path / "runs", tmp_path / "runtime"
    runs.mkdir()
    runtime.mkdir()
    brief = {**fixtures._brief(), "provider_name": "mock", "medium": "WORD"}
    selection = {**fixtures._selection(), "knowledge_status": "USER_SELECTED_SOME"}
    run = fixtures._write_run(runs, fixtures._pack(), brief, selection)
    provider = MockProvider({
        "provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-v12",
    })
    manager = SimpleNamespace(get=lambda *args, **kwargs: provider)
    with patch.object(app_core, "RUNS_DIR", runs), \
            patch.object(app_core, "RUNTIME_DIR", runtime), \
            patch.object(app_core, "RUNTIME_ROOT", tmp_path), \
            patch.object(app_core, "_checked", side_effect=lambda value: Path(value)), \
            patch.object(app_core, "_audit_read", return_value=None), \
            patch.object(app_core, "provider_manager", return_value=manager):
        result = app_core.generate_artifact(run.name, ["KU-0001"], {}, auto_repair=False)

    bindings = json.loads((run / "artifact_bindings.json").read_text(encoding="utf-8"))
    direct = run / bindings["product_direct"]["name"]
    internal = run / bindings["internal_master"]["name"]
    assert direct.is_file() and internal.is_file()
    assert bindings["product_direct"]["sha256"] == _sha(direct)
    assert bindings["internal_master"]["sha256"] == _sha(internal)
    assert bindings["internal_master"]["byte_identical_to_product_direct"] is True
    assert bindings["product_direct"]["sha256"] == bindings["internal_master"]["sha256"]

    external = bindings["external_clean"]
    assert external["status"] in {"CLEANED", "NO_CHANGES", "FAILED"}
    if external["name"]:
        assert external["sha256"] == _sha(run / external["name"])
    delivered = bindings["delivered"]
    assert delivered["role"] in {"product_direct", "external_clean"}
    assert delivered["name"] == result["artifact_name"]
    assert (_sha(run / delivered["name"])) == result["artifact_sha256"]
    assert bindings["provider_calls"] == 0

    audit = json.loads((run / "run_audit.json").read_text(encoding="utf-8"))
    assert audit["product_direct_artifact_sha256"] == bindings["product_direct"]["sha256"]
    assert audit["delivered_artifact_name"] == result["artifact_name"]
    assert audit["external_cleanup_provider_calls"] == 0
    if delivered["role"] == "external_clean" and external["status"] == "CLEANED":
        cleanup = json.loads((run / "external_cleanup.json").read_text(encoding="utf-8"))
        assert (cleanup.get("pagination") or {}).get("status") == "REFRESHED"


def _cleanup(status="CLEANED", pagination="REFRESHED", residual=0):
    row = {"status": status, "residual_count": residual}
    if pagination is not None:
        row["pagination"] = {"status": pagination}
    return row


def test_external_copy_requires_refreshed_pagination_and_no_worse_qa():
    eligible = app_core._external_delivery_eligible
    assert eligible(_cleanup(), {"status": "PASS"}, {"status": "PASS"}) is True
    assert eligible(_cleanup(pagination="PARTIAL"), {"status": "PASS"}, {"status": "PASS"}) is False
    assert eligible(_cleanup(pagination="SKIPPED_NO_RENDERER"), {"status": "PASS"}, {"status": "PASS"}) is False
    assert eligible(_cleanup(pagination=None), {"status": "PASS"}, {"status": "PASS"}) is False
    assert eligible(_cleanup(residual=1), {"status": "PASS"}, {"status": "PASS"}) is False
    assert eligible(_cleanup(), {"status": "NEEDS_REVIEW"}, {"status": "PASS"}) is False
    assert eligible(_cleanup(), {"status": "PASS"}, {"status": "NEEDS_REVIEW"}) is True
    assert eligible(_cleanup(), {}, {"status": "PASS"}) is False
    assert eligible(_cleanup(status="FAILED"), {"status": "PASS"}, {"status": "PASS"}) is False
    assert eligible(_cleanup(status="NO_CHANGES", pagination=None), {"status": "PASS"}, {"status": "PASS"}) is True

