from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


APP_VERSION = "0.1.0-alpha"
APP_NAME = "MMF Desktop / Local"
APP_STATUS = "initial_deployable"
BASELINE_AUTHORITY = "MMF-006D R10"
LONGFORM_ORCHESTRATOR_VERSION = "0.1-r1"


def load_build_manifest(package_root: Path | None = None) -> dict[str, Any]:
    root = (package_root or _default_package_root()).resolve()
    path = root / "build_manifest.json"
    if not path.is_file():
        return {
            "runtime_source": "source",
            "build_id": "dev-unpacked",
            "source_version": APP_VERSION,
            "dist_version": APP_VERSION,
            "longform_orchestrator_version": LONGFORM_ORCHESTRATOR_VERSION,
            "source_hash": "",
            "build_time": "",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("runtime_source", "dist")
    data.setdefault("longform_orchestrator_version", LONGFORM_ORCHESTRATOR_VERSION)
    return data


def _default_package_root() -> Path:
    env = os.environ.get("MMF_PACKAGE_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "MMF Desktop"


def user_settings_candidates(package_root: Path) -> list[Path]:
    return [
        Path(os.environ.get("MMF_USER_SETTINGS", "")).expanduser() if os.environ.get("MMF_USER_SETTINGS") else None,
        package_root / "config" / "user_settings.json",
        _local_appdata() / "config" / "user_settings.json",
    ]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class Roots:
    package_root: Path
    app_root: Path
    data_root: Path
    output_root: Path
    config_root: Path
    temp_root: Path
    logs_dir: Path
    runs_dir: Path
    runtime_dir: Path
    static_dir: Path
    templates_dir: Path
    broker_dir: Path
    providers_dir: Path
    grok_bridge_dir: Path
    user_settings_file: Path
    secure_config_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}

    def allowed_roots(self) -> list[Path]:
        return [
            self.package_root,
            self.app_root,
            self.data_root,
            self.output_root,
            self.config_root,
            self.temp_root,
            self.logs_dir,
            self.runs_dir,
            self.runtime_dir,
            self.static_dir,
            self.templates_dir,
            self.broker_dir,
            self.providers_dir,
            self.secure_config_dir,
        ]


_CURRENT: Roots | None = None


def _choose_user_settings_file(package_root: Path) -> Path:
    existing = [path for path in user_settings_candidates(package_root) if path and path.is_file()]
    if existing:
        return existing[0]
    secure = _local_appdata() / "config" / "user_settings.json"
    return secure


def load_user_settings(package_root: Path | None = None) -> dict[str, Any]:
    root = (package_root or _default_package_root()).resolve()
    for path in user_settings_candidates(root):
        if not (path and path.is_file()):
            continue
        data = _read_json(path)
        stored_root = str(data.get("package_root") or "").strip()
        if stored_root:
            try:
                if Path(stored_root).resolve() != root:
                    continue
            except OSError:
                continue
        else:
            # Ignore leftover settings that point at another copy of the app.
            foreign = False
            for key in ("data_root", "output_root", "runs_dir"):
                value = str(data.get(key) or "")
                if value and "mmf006e_cleanroom" in value.lower():
                    foreign = True
                    break
            if foreign:
                continue
        data["_settings_file"] = str(path)
        return data
    return {}


def build_roots(package_root: Path | None = None, settings: dict[str, Any] | None = None) -> Roots:
    pkg = (package_root or _default_package_root()).resolve()
    cfg = dict(settings or load_user_settings(pkg))
    app_root = Path(os.environ.get("MMF_APP_ROOT") or pkg / "app").expanduser().resolve()
    config_root = Path(os.environ.get("MMF_CONFIG_ROOT") or cfg.get("config_root") or pkg / "config").expanduser().resolve()
    data_root = Path(os.environ.get("MMF_DATA_ROOT") or cfg.get("data_root") or pkg).expanduser().resolve()
    output_root = Path(os.environ.get("MMF_OUTPUT_ROOT") or cfg.get("output_root") or pkg / "output").expanduser().resolve()
    temp_root = Path(os.environ.get("MMF_TEMP_ROOT") or cfg.get("temp_root") or pkg / "runtime" / "tmp").expanduser().resolve()
    logs_dir = Path(os.environ.get("MMF_LOGS_DIR") or cfg.get("logs_dir") or pkg / "logs").expanduser().resolve()
    runs_dir = Path(os.environ.get("MMF_RUNS_DIR") or cfg.get("runs_dir") or pkg / "runs").expanduser().resolve()
    runtime_dir = Path(os.environ.get("MMF_RUNTIME_DIR") or data_root / "runtime").expanduser().resolve()
    if runtime_dir == data_root:
        runtime_dir = pkg / "runtime"
    settings_file = Path(cfg.get("_settings_file") or _choose_user_settings_file(pkg)).expanduser().resolve()
    return Roots(
        package_root=pkg,
        app_root=app_root,
        data_root=data_root,
        output_root=output_root,
        config_root=config_root,
        temp_root=temp_root,
        logs_dir=logs_dir,
        runs_dir=runs_dir,
        runtime_dir=runtime_dir,
        static_dir=pkg / "static",
        templates_dir=pkg / "templates",
        broker_dir=pkg / "broker",
        providers_dir=pkg / "providers",
        grok_bridge_dir=pkg / "providers" / "grok_bridge",
        user_settings_file=settings_file,
        secure_config_dir=_local_appdata() / "config",
    )


def reload(package_root: Path | None = None) -> Roots:
    global _CURRENT
    _CURRENT = build_roots(package_root)
    os.environ["MMF_PACKAGE_ROOT"] = str(_CURRENT.package_root)
    os.environ["MMF_APP_ROOT"] = str(_CURRENT.app_root)
    os.environ["MMF_RUNTIME_ROOT"] = str(_CURRENT.package_root)
    os.environ["MMF_DATA_ROOT"] = str(_CURRENT.data_root)
    os.environ["MMF_OUTPUT_ROOT"] = str(_CURRENT.output_root)
    os.environ["MMF_CONFIG_ROOT"] = str(_CURRENT.config_root)
    os.environ["MMF_TEMP_ROOT"] = str(_CURRENT.temp_root)
    os.environ["MMF_LOGS_DIR"] = str(_CURRENT.logs_dir)
    os.environ["MMF_RUNS_DIR"] = str(_CURRENT.runs_dir)
    return _CURRENT


def current() -> Roots:
    global _CURRENT
    if _CURRENT is None:
        reload()
    assert _CURRENT is not None
    return _CURRENT


def ensure_directories(roots: Roots | None = None) -> Roots:
    item = roots or current()
    for path in (
        item.app_root,
        item.data_root,
        item.output_root,
        item.config_root,
        item.temp_root,
        item.logs_dir,
        item.runs_dir,
        item.runtime_dir,
        item.static_dir,
        item.templates_dir,
        item.broker_dir,
        item.providers_dir,
        item.grok_bridge_dir,
        item.secure_config_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return item


PACKAGE_ROOT = _default_package_root()
APP_ROOT = PACKAGE_ROOT / "app"
RUNTIME_ROOT = PACKAGE_ROOT
CONFIG_DIR = PACKAGE_ROOT / "config"
RUNS_DIR = PACKAGE_ROOT / "runs"
RUNTIME_DIR = PACKAGE_ROOT / "runtime"
STATIC_DIR = PACKAGE_ROOT / "static"
ASSETS_DIR = APP_ROOT / "assets"
OUTPUT_ROOT = PACKAGE_ROOT / "output"
LOGS_DIR = PACKAGE_ROOT / "logs"
DATA_ROOT = PACKAGE_ROOT
TEMP_ROOT = PACKAGE_ROOT / "runtime" / "tmp"
SECURE_CONFIG_DIR = _local_appdata() / "config"


def sync_module_aliases() -> Roots:
    roots = current()
    globals().update({
        "PACKAGE_ROOT": roots.package_root,
        "APP_ROOT": roots.app_root,
        "RUNTIME_ROOT": roots.package_root,
        "CONFIG_DIR": roots.config_root,
        "RUNS_DIR": roots.runs_dir,
        "RUNTIME_DIR": roots.runtime_dir,
        "STATIC_DIR": roots.static_dir,
        "ASSETS_DIR": roots.app_root / "assets",
        "OUTPUT_ROOT": roots.output_root,
        "LOGS_DIR": roots.logs_dir,
        "DATA_ROOT": roots.data_root,
        "TEMP_ROOT": roots.temp_root,
        "SECURE_CONFIG_DIR": roots.secure_config_dir,
    })
    return roots


sync_module_aliases()
