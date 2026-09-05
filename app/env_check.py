from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import paths
from user_errors import USER_MESSAGES, sanitize


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _which(name: str) -> str:
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else ""


def _find_node() -> str:
    env = os.environ.get("RUNTIME_NODE", "").strip()
    if env and Path(env).is_file():
        return str(Path(env).resolve())
    for name in ("node", "node.exe"):
        found = _which(name)
        if found and "WindowsApps" not in found:
            return found
    home = Path.home()
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "nodejs" / "node.exe",
        home / "AppData" / "Local" / "Programs" / "nodejs" / "node.exe",
        home / "AppData" / "Local" / "Programs" / "node" / "node.exe",
        Path(r"C:\nodejs\node.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return ""


def _find_npm() -> str:
    found = _which("npm") or _which("npm.cmd")
    if found:
        return found
    node = _find_node()
    if node:
        sibling = Path(node).parent / "npm.cmd"
        if sibling.is_file():
            return str(sibling)
    return ""


def _persist_node(node: str, node_modules: Path) -> None:
    cfg = paths.current().runtime_dir / "runtime_config.json"
    data: dict[str, Any] = {}
    if cfg.is_file():
        try:
            loaded = json.loads(cfg.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    if node:
        data["node_executable"] = node
        data["node_modules"] = str(node_modules)
        data["bin_dir"] = str(Path(node).parent)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.environ["RUNTIME_NODE"] = node
        os.environ["RUNTIME_NODE_MODULES"] = str(node_modules)
        os.environ["RUNTIME_BIN_DIR"] = str(Path(node).parent)


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mmf_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _port_in_use(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _our_service_running(host: str, port: int) -> bool:
    try:
        request = urllib.request.Request(f"http://{host}:{port}/api/health", method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") == "ok" and "MMF" in str(payload.get("app", ""))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return False


def _port_available(host: str, port: int) -> tuple[bool, str]:
    if not _port_in_use(host, port):
        return True, f"端口 {port} 可用"
    if _our_service_running(host, port):
        return True, f"端口 {port} 已由当前MMF使用"
    return False, USER_MESSAGES["port_in_use"]


def _python_ok() -> dict[str, Any]:
    version = sys.version_info
    ok = version.major == 3 and version.minor >= 10
    return {
        "id": "python_runtime",
        "label": "Python运行环境",
        "ok": ok,
        "user": "Python已就绪" if ok else USER_MESSAGES["python_missing"],
        "detail": sys.version.split()[0],
        "path": sys.executable,
    }


def _import_ok(module: str, label: str) -> dict[str, Any]:
    try:
        imported = __import__(module)
        version = getattr(imported, "__version__", "ok")
        return {"id": module, "label": label, "ok": True, "user": f"{label}已安装", "detail": str(version)}
    except Exception as exc:
        return {"id": module, "label": label, "ok": False, "user": f"{label}未安装，请重新运行首次安装", "detail": sanitize(str(exc), 120)}


def _node_ok() -> dict[str, Any]:
    node = _find_node()
    if not node:
        return {
            "id": "node_runtime",
            "label": "Node运行环境",
            "ok": False,
            "optional": True,
            "user": USER_MESSAGES["node_missing"],
            "detail": "PPT生成需要Node；WORD生成不受影响",
            "path": "",
        }
    return {
        "id": "node_runtime",
        "label": "Node运行环境",
        "ok": True,
        "optional": True,
        "user": "Node已就绪，可用于PPT生成",
        "detail": node,
        "path": node,
    }


def _artifact_tool_ok(node_modules: Path) -> dict[str, Any]:
    package = node_modules / "@oai" / "artifact-tool" / "package.json"
    if package.is_file():
        try:
            version = json.loads(package.read_text(encoding="utf-8")).get("version", "")
        except json.JSONDecodeError:
            version = "present"
        return {"id": "ppt_renderer", "label": "PPT组件", "ok": True, "optional": True, "user": "PPT生成组件已就绪", "detail": version}
    return {
        "id": "ppt_renderer",
        "label": "PPT组件",
        "ok": False,
        "optional": True,
        "user": "PPT生成组件未安装。可先使用WORD；安装Node后重新运行首次安装。",
        "detail": "missing @oai/artifact-tool",
    }


def _grok_status(enable_requested: bool = False) -> dict[str, Any]:
    roots = paths.current()
    bridge = roots.grok_bridge_dir / "grok_bridge.py"
    config = roots.grok_bridge_dir / "bridge_config.json"
    grok = shutil.which("grok") or str(Path.home() / ".grok" / "bin" / "grok.exe")
    grok_exists = Path(grok).is_file() if grok else False
    if not bridge.is_file():
        status = "unavailable"
        user = USER_MESSAGES["grok_bridge_unavailable"]
    elif not grok_exists:
        status = "unavailable"
        user = "未检测到本机Grok CLI。Grok为可选功能，不影响使用千问等API引擎。"
    elif not enable_requested:
        status = "not_configured"
        user = "已检测到本机Grok，但尚未在首次配置中启用。"
    else:
        status = "connected" if config.is_file() else "not_configured"
        user = "本机Grok Bridge可用" if status == "connected" else "请在AI引擎设置中完成Grok Bridge配置"
    return {
        "id": "grok_bridge",
        "label": "Local Grok Bridge（可选）",
        "ok": True,
        "optional": True,
        "status": status,
        "user": user,
        "cli_detected": grok_exists,
        "bridge_present": bridge.is_file(),
        "path": "",
    }


def run_environment_check(host: str = "127.0.0.1", port: int = 3050, assume_self_listening: bool = False) -> dict[str, Any]:
    roots = paths.ensure_directories()
    settings = paths.load_user_settings(roots.package_root)
    node_modules = Path(os.environ.get("RUNTIME_NODE_MODULES") or roots.app_root / "node_modules")
    node = _find_node()
    if node:
        _persist_node(node, node_modules)
    if assume_self_listening:
        port_ok, port_msg = True, f"端口 {port} 已由当前MMF使用"
    else:
        port_ok, port_msg = _port_available(host, port)
    checks = [
        _python_ok(),
        _import_ok("docx", "Word生成组件"),
        _import_ok("pypdf", "PDF读取组件"),
        _node_ok(),
        _artifact_tool_ok(node_modules),
        {"id": "host_bind", "label": "监听地址", "ok": host == "127.0.0.1", "user": "仅本机访问" if host == "127.0.0.1" else "拒绝非本机监听", "detail": host},
        {"id": "port", "label": "服务端口", "ok": port_ok, "optional": True, "user": port_msg if not port_ok else f"端口 {port} 可用", "detail": str(port)},
        {"id": "output_writable", "label": "输出目录", "ok": _writable(roots.output_root), "user": "输出目录可写" if _writable(roots.output_root) else USER_MESSAGES["output_not_writable"], "detail": str(roots.output_root)},
        {"id": "runs_writable", "label": "任务目录", "ok": _writable(roots.runs_dir), "user": "任务目录可写" if _writable(roots.runs_dir) else USER_MESSAGES["permission_denied"], "detail": str(roots.runs_dir)},
        {"id": "logs_writable", "label": "日志目录", "ok": _writable(roots.logs_dir), "user": "日志目录可写" if _writable(roots.logs_dir) else USER_MESSAGES["permission_denied"], "detail": str(roots.logs_dir)},
        {"id": "config_present", "label": "配置文件", "ok": (roots.config_root / "app.json").is_file(), "user": "应用配置已就绪" if (roots.config_root / "app.json").is_file() else "缺少应用配置", "detail": str(roots.config_root / "app.json")},
        _grok_status(bool(settings.get("enable_grok_bridge"))),
        {"id": "broker", "label": "本机桌面助手", "ok": (roots.broker_dir / "user_session_broker.py").is_file(), "user": "可打开成品文件夹", "detail": "local_open_folder"},
    ]
    blocking = [item for item in checks if not item.get("ok") and not item.get("optional")]
    status = "PASS" if not blocking else "FAIL"
    report = {
        "schema_version": "mmf-desktop-env-check-v0.1",
        "status": status,
        "app_version": paths.APP_VERSION,
        "checked_at": now_iso(),
        "host": host,
        "port": port,
        "roots": roots.as_dict(),
        "checks": checks,
        "user_summary": [item["user"] for item in checks],
        "blocking": [item["id"] for item in blocking],
    }
    target = roots.runtime_dir / "environment_check.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_redact_home(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _redact_home(value: Any) -> Any:
    home = str(Path.home())
    if isinstance(value, dict):
        return {key: _redact_home(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_home(item) for item in value]
    if not isinstance(value, str) or not home:
        return value
    alt = home.replace("\\", "/")
    pattern = re.compile(re.escape(home) + "|" + re.escape(alt), re.IGNORECASE)
    return pattern.sub("%USERPROFILE%", value)


def user_facing_check(report: dict[str, Any] | None = None) -> dict[str, Any]:
    data = report or run_environment_check()
    hidden = {"runs_writable", "logs_writable"}
    return {
        "status": "就绪" if data["status"] == "PASS" else "需要处理",
        "items": [
            {"label": item["label"], "ok": bool(item.get("ok")), "message": item.get("user", "")}
            for item in data.get("checks", [])
            if item.get("id") not in hidden
        ],
        "blocking": data.get("blocking", []),
    }


if __name__ == "__main__":
    import json as _json
    report = run_environment_check()
    print(_json.dumps({"status": report["status"], "blocking": report["blocking"]}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "PASS" else 1)
