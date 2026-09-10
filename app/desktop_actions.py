from __future__ import annotations

import os
import re
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


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def open_run_folder(run_id: str, folder_kind: str = "output") -> dict[str, Any]:
    """Open only a directory belonging to a known MMF run.

    No browser-supplied filesystem path is accepted by this entry point.
    """
    value = str(run_id or "").strip()
    kind = str(folder_kind or "output").strip().lower()
    if not RUN_ID_PATTERN.fullmatch(value) or ".." in value:
        raise PermissionError("无效的任务编号，已阻止打开目录。")
    roots = paths.current()
    run_dir = (roots.runs_dir / value).resolve()
    if not _is_within(run_dir, roots.runs_dir) or not run_dir.is_dir():
        raise FileNotFoundError("输出目录不存在或已被移动。")
    candidates = {
        "output": (roots.output_root / value).resolve(),
        "run": run_dir,
        "artifact": (run_dir / "artifact").resolve(),
    }
    if kind not in candidates:
        raise PermissionError("不允许打开该目录类型。")
    target = candidates[kind]
    allowed_root = roots.output_root if kind == "output" else roots.runs_dir
    if not _is_within(target, allowed_root):
        raise PermissionError("目标目录不在MMF允许的输出范围内。")
    if not target.is_dir():
        raise FileNotFoundError("输出目录不存在或已被移动。")
    try:
        subprocess.Popen(["explorer.exe", str(target)], shell=False)
    except OSError as exc:
        raise OSError("无法启动Windows文件资源管理器，请复制路径后手动打开。") from exc
    return {"ok": True, "run_id": value, "path": str(target), "opened": True, "kind": kind}


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
