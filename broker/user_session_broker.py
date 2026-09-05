from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class BrokerContext:
    def __init__(self, token: str, allowed_roots: list[Path]):
        self.token = token
        self.allowed_roots = [path.resolve() for path in allowed_roots]

    def _allowed(self, raw: str) -> Path:
        path = Path(raw).expanduser().resolve()
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return path
            except ValueError:
                continue
        raise PermissionError("目标不在允许打开的目录内")

    def open_folder(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = str(payload.get("path") or payload.get("artifact_key") or "")
        path = self._allowed(raw)
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            raise FileNotFoundError("文件夹不存在")
        os.startfile(str(path))
        return {"path": str(path), "opened": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "MMFDesktopBroker/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_json(404, {"ok": False, "error": "NOT_FOUND"})
            return
        self.send_json(200, {"ok": True, "service": "MMF_DESKTOP_BROKER", "version": "0.1"})

    def do_POST(self) -> None:
        if self.path != "/action":
            self.send_json(404, {"ok": False, "error": "NOT_FOUND"})
            return
        if self.headers.get("X-MMF-Broker-Token", "") != self.server.context.token:  # type: ignore[attr-defined]
            self.send_json(403, {"ok": False, "error": "UNAUTHORIZED"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if payload.get("action") != "open_allowed_folder":
                raise ValueError("当前仅支持打开允许的文件夹")
            result = self.server.context.open_folder(payload)  # type: ignore[attr-defined]
            self.send_json(200, {"ok": True, "result": result})
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": "BROKER_ERROR", "message": str(exc)[-300:]})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    allowed = [Path(item) for item in args.allowed_root] or [Path(os.environ.get("MMF_PACKAGE_ROOT", ".")).resolve()]
    token = secrets.token_urlsafe(36)
    context = BrokerContext(token, allowed)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.context = context  # type: ignore[attr-defined]
    host, port = server.server_address
    write_json(Path(args.session_file), {
        "schema_version": "mmf-desktop-broker-v0.1",
        "status": "WORKING",
        "host": host,
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "started_at": now_iso(),
        "actions": ["open_allowed_folder"],
    })
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
