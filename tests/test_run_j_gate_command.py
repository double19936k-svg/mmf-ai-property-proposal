"""Regression for the real PowerShell 5.1 parse/exclusion failure from OBS-1."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@pytest.mark.skipif(not SHELL.is_file(),reason="PowerShell 5.1 is a Windows review dependency")
@pytest.mark.parametrize("defect",["invalid_json","empty_exclusions","duplicate_exclusions","missing_test"])
def test_invalid_manifest_stops_before_any_output_or_test(tmp_path,defect):
    manifest = tmp_path / "invalid.json"
    if defect == "invalid_json":
        manifest.write_text('{"中文":',encoding="utf-8")
    else:
        excluded = [] if defect == "empty_exclusions" else ["tests/test_run_j_visuals.py"]*20
        if defect == "missing_test": excluded = [f"tests/test_missing_{i}.py" for i in range(20)]
        manifest.write_text(json.dumps({"regression":{"excluded_generation_files":excluded}}),encoding="utf-8")
    output = ROOT / "runtime/run_j/agent_outputs/command_regression" / tmp_path.name / defect
    result = subprocess.run([str(SHELL),"-NoProfile","-ExecutionPolicy","Bypass","-File",str(ROOT/"tools/run_j_gate_checks.ps1"),
        "-Manifest",str(manifest),"-OutputDirectory",str(output),"-Mode","Offline"],
        capture_output=True,timeout=30)
    assert result.returncode != 0
    expected = {"invalid_json":b"ConvertFrom-Json","empty_exclusions":b"Expected exactly 20",
                "duplicate_exclusions":b"Expected exactly 20","missing_test":b"Invalid or missing excluded test"}
    assert expected[defect] in result.stdout + result.stderr, "Must fail at manifest validation, not shell startup"
    assert not output.exists(), "A rejected plan must never reach a test launcher or create outputs"
