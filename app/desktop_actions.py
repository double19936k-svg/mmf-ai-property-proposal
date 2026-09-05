from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import paths
from user_errors import classify


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _allowed(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    roots = paths.current()
    if any(_is_within(resolved, root) for root in roots.allowed_roots()):
        return resolved
    raise PermissionError("目标路径不在应用允许的工作范围内")


def open_path(path: str | Path, folder: bool = False) -> dict[str, Any]:
    target = _allowed(Path(path))
    if folder:
        if target.is_file():
            target = target.parent
        if not target.is_dir():
            raise FileNotFoundError("文件夹不存在")
    elif not target.exists():
        raise FileNotFoundError("文件不存在")
    os.startfile(str(target))  # noqa: S606 - local desktop helper
    return {"ok": True, "path": str(target), "opened": True, "kind": "folder" if folder else "file"}


def safe_open(path: str | Path, folder: bool = False) -> dict[str, Any]:
    try:
        return open_path(path, folder=folder)
    except Exception as exc:
        payload = classify(exc)
        payload["ok"] = False
        return payload


def pick_folder(title: str = "选择文件夹", start_path: str = "") -> dict[str, Any]:
    start = str(start_path or "").strip()
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = $env:MMF_PICK_TITLE; "
        "$d.ShowNewFolderButton = $true; "
        "if ($env:MMF_PICK_START -and (Test-Path -LiteralPath $env:MMF_PICK_START)) { $d.SelectedPath = $env:MMF_PICK_START }; "
        "[System.Windows.Forms.Application]::EnableVisualStyles(); "
        "$r = $d.ShowDialog(); "
        "if ($r -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::Out.Write($d.SelectedPath) }"
    )
    env = os.environ.copy()
    env["MMF_PICK_TITLE"] = title or "选择文件夹"
    env["MMF_PICK_START"] = start
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
        shell=False,
    )
    selected = (completed.stdout or "").strip()
    if not selected:
        return {"ok": False, "cancelled": True, "path": ""}
    path = Path(selected).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "cancelled": False, "path": str(path.resolve())}
