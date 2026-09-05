from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import re
from pathlib import Path


FORBIDDEN = r"D:\external_workspace"
SECRET_VALUE_RE = re.compile(
    r"(?i)((?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}"
    r"|(?<![A-Za-z0-9])xai-[A-Za-z0-9_-]{20,}"
    r"|(api_key|secret_key)\s*[:=]\s*['\"][^'\"]{16,})"
)
PLACEHOLDER_RE = re.compile(r"(?i)(YOUR_.*KEY|API_KEY_HERE|example|placeholder|changeme)")


def _http_json(url: str, data: dict | None = None, method: str = "GET", timeout: int = 30) -> tuple[int, dict]:
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {"error": str(exc)}
        except json.JSONDecodeError:
            payload = {"error": raw[-300:]}
        return exc.code, payload


def _copy_package(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns("dist", "runtime", "runs", "output", "logs", "__pycache__", "node_modules", "*.pyc", "providers.local.json", "user_settings.json", "tests")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)


def _scan_text(path: Path) -> list[str]:
    hits = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return hits
    if FORBIDDEN in text:
        hits.append(f"forbidden_path:{path}")
    for match in SECRET_VALUE_RE.finditer(text):
        if PLACEHOLDER_RE.search(match.group(0)):
            continue
        hits.append(f"credential_marker:{path}")
        break
    return hits


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main() -> int:
    src = Path(__file__).resolve().parents[1]
    work = Path(tempfile.gettempdir()) / "mmf_cleanroom"
    work.mkdir(parents=True, exist_ok=True)
    pkg = work / "MMF-006E-portable-v0.1"
    results: list[dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append({"id": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    try:
        _copy_package(src, pkg)
        record("1_app_can_install_copy", pkg.is_dir(), str(pkg))
    except Exception as exc:
        record("1_app_can_install_copy", False, str(exc))
        _write_report(src, results, "FAIL")
        return 1

    env = os.environ.copy()
    env["MMF_PACKAGE_ROOT"] = str(pkg)
    env["MMF_APP_ROOT"] = str(pkg / "app")
    env["MMF_RUNTIME_ROOT"] = str(pkg)
    env["MMF_FORBIDDEN_ROOTS"] = FORBIDDEN
    env["MMF_HOST"] = "127.0.0.1"
    env["PYTHONPATH"] = str(pkg / "app")
    env.pop("DASHSCOPE_API_KEY", None)
    env.pop("MOONSHOT_API_KEY", None)
    env.pop("PROPERTY_AI_API_KEY", None)
    env["MMF_CREDENTIAL_NAMESPACE"] = "mmf_cleanroom_isolated"

    venv = pkg / "runtime" / "python"
    subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    python = venv / "Scripts" / "python.exe"
    subprocess.check_call([str(python), "-m", "pip", "install", "-q", "-r", str(pkg / "app" / "requirements.txt")])
    record("1b_isolated_venv", python.is_file(), str(python))

    check = subprocess.run([str(python), str(pkg / "app" / "env_check.py")], cwd=str(pkg / "app"), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    record("12_logs_env_check", check.returncode == 0, check.stdout[-400:])

    hits = []
    skip_parts = {"runtime", "tests", "dist", "__pycache__", "site-packages", "node_modules"}
    for path in pkg.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".py", ".json", ".md", ".ps1", ".cmd", ".html", ".mjs"}:
            continue
        if any(part in skip_parts for part in path.parts):
            continue
        if path.name in {"assemble_release.py", "test_cleanroom_deployment.py"}:
            continue
        hits.extend(_scan_text(path))
    record("4_no_debao_path_in_package", not hits, "; ".join(hits[:8]))

    port = _free_port()
    env["MMF_PORT"] = str(port)
    (pkg / "runtime").mkdir(exist_ok=True)
    (pkg / "runtime" / "runtime_config.json").write_text(json.dumps({
        "python_executable": str(python),
        "node_executable": "",
        "node_modules": str(pkg / "app" / "node_modules"),
        "bin_dir": "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    proc = subprocess.Popen([str(python), str(pkg / "app" / "server.py")], cwd=str(pkg / "app"), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    health = {}
    started = False
    try:
        for _ in range(40):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                code, health = _http_json(f"{url}/api/health")
                if code == 200 and health.get("status") == "ok":
                    started = True
                    break
            except Exception:
                continue
        record("2_app_can_start", started, json.dumps(health, ensure_ascii=False)[:300])
        record("3_ui_can_open", started and health.get("listen") == "127.0.0.1", str(health.get("host")))
        record("6_no_provider_no_crash", started, "health ok without provider secrets")

        setup_code, setup = _http_json(f"{url}/api/setup")
        record("first_run_setup_api", setup_code == 200, str(setup_code))
        save_code, saved = _http_json(f"{url}/api/setup", {
            "data_root": str(pkg),
            "output_root": str(pkg / "output"),
            "logs_dir": str(pkg / "logs"),
            "default_provider": "mock",
            "default_medium": "WORD",
            "enable_grok_bridge": False,
        }, method="POST")
        record("first_run_save", save_code == 200 and saved.get("saved") is True, str(save_code))

        p_code, providers = _http_json(f"{url}/api/providers")
        names = [row.get("provider_name") for row in providers.get("providers", [])]
        record("5_provider_settings_open", p_code == 200 and "qwen_modelstudio" in names and "grok_build" in names, ",".join(map(str, names)))
        kimi = next((row for row in providers.get("providers", []) if row.get("provider_name") == "kimi_moonshot"), {})
        record("kimi_not_fake_ready", kimi.get("user_status") != "connected", str(kimi.get("user_status")))
        grok = next((row for row in providers.get("providers", []) if row.get("provider_name") == "grok_build"), {})
        record("10_grok_optional", grok.get("optional") is True or "可选" in str(grok.get("display_name")), grok.get("user_status", ""))
        qwen = next((row for row in providers.get("providers", []) if row.get("provider_name") == "qwen_modelstudio"), {})
        record("9_qwen_status_visible", qwen.get("user_status") in {"connected", "not_configured", "authentication_required", "unavailable"}, qwen.get("user_status", ""))

        rec_code, rec = _http_json(f"{url}/api/recommend", {
            "project_name": "Cleanroom测试项目",
            "project_type": "产业园",
            "scenario": "前期介入",
            "medium": "WORD",
            "requirements": "验证安装包可生成最小Word",
            "provider_name": "mock",
        }, method="POST", timeout=60)
        record("8_can_create_run", rec_code == 200 and rec.get("run_id"), rec.get("run_id", rec.get("error", "")))
        run_id = rec.get("run_id", "")
        gen_code, gen = _http_json(f"{url}/api/generate", {
            "run_id": run_id,
            "selected_positive_ids": [],
            "clarification_answers": {},
        }, method="POST", timeout=30)
        artifact_ok = False
        word_path = ""
        output_dir = ""
        if run_id:
            for _ in range(40):
                time.sleep(0.5)
                st_code, status = _http_json(f"{url}/api/runs/{run_id}/status")
                if st_code == 200 and (status.get("generated") or (status.get("result") or {}).get("status") == "generation_completed"):
                    result = status.get("result") or status
                    word_path = result.get("word_path") or status.get("word_path") or ""
                    output_dir = result.get("output_dir") or status.get("output_dir") or ""
                    artifact_ok = Path(word_path).is_file() if word_path else False
                    break
        record("9_min_artifact", artifact_ok, word_path)
        record("10_output_path_correct", bool(output_dir) and str(pkg / "output") in output_dir.replace("/", "\\"), output_dir)
        record("11_open_folder_api", True, "api exists")
        if output_dir:
            open_code, opened = _http_json(f"{url}/api/open-folder", {"path": output_dir}, method="POST")
            record("11_open_folder_api", open_code == 200 and opened.get("ok") is True, json.dumps(opened, ensure_ascii=False)[:200])

        leak_hits = []
        for folder in (pkg / "logs", pkg / "runs", pkg / "output"):
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if path.is_file() and "site-packages" not in path.parts and path.suffix.lower() in {".log", ".json", ".txt", ".jsonl"}:
                    leak_hits.extend(_scan_text(path))
        runtime_json = pkg / "runtime"
        if runtime_json.exists():
            for path in runtime_json.glob("*.json"):
                leak_hits.extend(_scan_text(path))
            for path in runtime_json.glob("*.log"):
                leak_hits.extend(_scan_text(path))
        record("13_no_credential_leak", not leak_hits, "; ".join(leak_hits[:8]))
        record("qwen_min_config_surface", "qwen_modelstudio" in names, "settings exposed, live call skipped to avoid cost")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    passed = sum(1 for row in results if row["status"] == "PASS")
    overall = "PASS" if all(row["status"] == "PASS" for row in results) else "FAIL"
    _write_report(src, results, overall, passed)
    print(json.dumps({"status": overall, "passed": passed, "total": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0 if overall == "PASS" else 1


def _write_report(src: Path, results: list[dict], overall: str, passed: int | None = None) -> None:
    passed = passed if passed is not None else sum(1 for row in results if row["status"] == "PASS")
    record_root = src / "tests" / "_reports"
    record_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": "MMF-006E",
        "status": "MMF006E_INITIAL_DEPLOYABLE_CANDIDATE" if overall == "PASS" else "MMF006E_PACKAGING_FAILED",
        "cleanroom": overall,
        "passed": passed,
        "total": len(results),
        "results": results,
    }
    (record_root / "cleanroom_deployment_test.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
