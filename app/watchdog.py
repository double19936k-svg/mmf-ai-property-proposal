from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def _port_open(host: str, port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _merge_no_proxy(env: dict[str, str]) -> None:
    extra = ("localhost", "127.0.0.1", "::1")
    current = []
    for key in ("NO_PROXY", "no_proxy"):
        current.extend(part.strip() for part in str(env.get(key) or "").split(",") if part.strip())
    merged = []
    seen = set()
    for item in [*current, *extra]:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    value = ",".join(merged)
    env["NO_PROXY"] = value
    env["no_proxy"] = value


def main() -> None:
    app_dir = Path(__file__).resolve().parent
    package_root = app_dir.parent
    logs_dir = package_root / "logs"
    runtime_dir = package_root / "runtime"
    logs_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    server = app_dir / "server.py"
    log_path = logs_dir / "watchdog.log"
    pid_path = runtime_dir / "watchdog.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    python = sys.executable
    env = os.environ.copy()
    env["MMF_PACKAGE_ROOT"] = str(package_root)
    env["MMF_APP_ROOT"] = str(app_dir)
    env["PYTHONPATH"] = str(app_dir)
    grok_bin = Path.home() / ".grok" / "bin"
    if grok_bin.is_dir():
        env["PATH"] = str(grok_bin) + os.pathsep + env.get("PATH", "")
    if not env.get("HTTPS_PROXY") and not env.get("HTTP_PROXY"):
        for port in (7897, 7890, 10809, 10808, 6152, 8888):
            if _port_open("127.0.0.1", port):
                proxy = f"http://127.0.0.1:{port}"
                env["HTTP_PROXY"] = proxy
                env["HTTPS_PROXY"] = proxy
                env["ALL_PROXY"] = proxy
                break
    _merge_no_proxy(env)
    host = env.get("MMF_HOST", "127.0.0.1")
    port = int(env.get("MMF_PORT", "3050"))
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    while True:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{stamp} starting server\n")
        out = open(runtime_dir / "launcher_server.log", "a", encoding="utf-8")
        err = open(runtime_dir / "launcher_server_error.log", "a", encoding="utf-8")
        proc = None
        code = 1
        try:
            proc = subprocess.Popen(
                [python, str(server)],
                cwd=str(app_dir),
                env=env,
                stdout=out,
                stderr=err,
                creationflags=creationflags,
            )
            (runtime_dir / "server.pid").write_text(str(proc.pid), encoding="utf-8")
            saw_listen = False
            silent = 0
            while True:
                result = proc.poll()
                if result is not None:
                    code = result
                    break
                if _port_open(host, port):
                    saw_listen = True
                    silent = 0
                elif saw_listen:
                    silent += 1
                    if silent >= 8:
                        with log_path.open("a", encoding="utf-8") as log:
                            log.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} port {port} lost; restarting server\n")
                        proc.kill()
                        try:
                            code = proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            code = 1
                        break
                time.sleep(1)
        finally:
            out.close()
            err.close()
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{stamp} server exited code={code}; restarting\n")
        time.sleep(1)


if __name__ == "__main__":
    main()
