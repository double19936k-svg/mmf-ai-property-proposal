from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
WORKSPACE = PACKAGE.parents[1]
RUNTIME_SRC = WORKSPACE / "06_MVP产品与生成链" / "MMF_Runtime" / "src"
BRIDGE_SRC = WORKSPACE / "06_MVP产品与生成链" / "00_Agent_Infrastructure" / "Grok_Bridge_V0.1"
APP = PACKAGE / "app"
PROVIDERS = PACKAGE / "providers" / "grok_bridge"

COPY_PY = [
    "app_core.py",
    "compliance.py",
    "build_docx.py",
    "build_ppt.mjs",
    "build_ppt_legacy.mjs",
    "requirements.txt",
    "package.json",
]
COPY_DIRS = [
    "providers",
    "governance",
    "tender_intake",
    "longform",
    "planning",
    "ppt_renderer_v2",
    "assets",
]
SKIP_DIR_NAMES = {"__pycache__", "node_modules"}
SKIP_FILE_SUFFIXES = {".pyc", ".log"}
SKIP_FILE_NAMES = {"providers.local.json"}
DEV_SCRIPTS = {
    "analyze_mmf006d_r9_sources.py",
    "analyze_r8_render.py",
    "apply_mmf006d_r7.py",
    "audit_mmf006d_r9_ppt_package.py",
    "build_mmf006d_r10_ppt.py",
    "build_mmf006d_r8.py",
    "invoke_mmf006d_r6_grok.py",
    "invoke_mmf006d_r7_grok.py",
    "invoke_mmf006d_r8_grok.py",
    "invoke_mmf006d_r9_grok.py",
    "qa_mmf006d_r2.py",
    "qa_mmf006d_r3.py",
    "qa_mmf006d_r4.py",
    "qa_mmf006d_r5.py",
    "qa_mmf006d_r6.py",
    "qa_mmf006d_r6_final.py",
    "qa_mmf006d_r7.py",
    "qa_mmf006d_r8.py",
    "qa_mmf006d_r9.py",
    "verify_mmf006d_r10.py",
    "patch_mmf006d_r9_word.py",
    "restore_mmf006d_r9_ppt_media.py",
    "Todd_MMF002_人工验收记录.json",
    "Todd_MMF004_人工验收记录.json",
    "Todd_MMF005_人工验收记录.json",
    "Todd_MMF006A_人工验收记录.json",
    "Todd_MMF006A_人工验收记录_R2.json",
}


def copy_filtered(src: Path, dst: Path) -> None:
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in SKIP_DIR_NAMES or item.name in SKIP_FILE_NAMES:
            continue
        if item.suffix in SKIP_FILE_SUFFIXES:
            continue
        if item.name in DEV_SCRIPTS:
            continue
        copy_filtered(item, dst / item.name)


def patch_app_core(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    old = '''APP_ROOT = Path(os.environ.get("MMF_APP_ROOT") or Path(__file__).resolve().parent).resolve()
_default_runtime_root = APP_ROOT.parent if APP_ROOT.parent.name == "MMF_Runtime" else APP_ROOT
RUNTIME_ROOT = Path(os.environ.get("MMF_RUNTIME_ROOT") or _default_runtime_root).resolve()
ASSETS_DIR = APP_ROOT / "assets"
CONFIG_DIR = APP_ROOT / "config"
RUNS_DIR = RUNTIME_ROOT / "runs"
RUNTIME_DIR = RUNTIME_ROOT / "runtime"
STATIC_DIR = APP_ROOT / "static"
REVIEW_FILE = RUNTIME_DIR / "Todd_MMF004_人工验收记录.json"
STATE_FILE = RUNTIME_DIR / "mmf_state.json"
MMF006A_STATE_FILE = RUNTIME_DIR / "MMF006A_state.json"
ACCESS_AUDIT_FILE = RUNTIME_DIR / "access_audit.jsonl"
'''
    new = '''import paths as _mmf_paths
APP_ROOT = _mmf_paths.APP_ROOT
RUNTIME_ROOT = _mmf_paths.RUNTIME_ROOT
ASSETS_DIR = _mmf_paths.ASSETS_DIR
CONFIG_DIR = _mmf_paths.CONFIG_DIR
RUNS_DIR = _mmf_paths.RUNS_DIR
RUNTIME_DIR = _mmf_paths.RUNTIME_DIR
STATIC_DIR = _mmf_paths.STATIC_DIR
REVIEW_FILE = RUNTIME_DIR / "product_review.json"
STATE_FILE = RUNTIME_DIR / "mmf_state.json"
MMF006A_STATE_FILE = RUNTIME_DIR / "MMF006A_state.json"
ACCESS_AUDIT_FILE = RUNTIME_DIR / "access_audit.jsonl"
'''
    if old not in text:
        raise SystemExit("app_core.py path block not found; refuse to assemble")
    text = text.replace(old, new, 1)
    old_checked = '''def _checked(path: Path) -> Path:
    resolved = path.resolve()
    if not _is_within(resolved, RUNTIME_ROOT):
        raise MMFError(f"Portable运行时拒绝访问MMF_Runtime之外的路径：{resolved.name}")
    forbidden_raw = os.environ.get("MMF_FORBIDDEN_ROOTS", "")
    for value in [item for item in forbidden_raw.split(os.pathsep) if item.strip()]:
        if _is_within(resolved, Path(value)):
            raise MMFError("Clean-room隔离规则阻止了对研发目录的访问。")
    return resolved
'''
    new_checked = '''def _checked(path: Path) -> Path:
    resolved = path.resolve()
    allowed = list(_mmf_paths.current().allowed_roots())
    if not any(_is_within(resolved, root) for root in allowed):
        raise MMFError("当前路径不在应用允许的工作范围内。")
    forbidden_raw = os.environ.get("MMF_FORBIDDEN_ROOTS", "")
    for value in [item for item in forbidden_raw.split(os.pathsep) if item.strip()]:
        if _is_within(resolved, Path(value)):
            raise MMFError("当前环境不允许访问研发目录。")
    return resolved
'''
    if old_checked not in text:
        raise SystemExit("app_core.py _checked block not found; refuse to assemble")
    text = text.replace(old_checked, new_checked, 1)
    old_pm = "PROVIDERS = ProviderManager(APP_ROOT)"
    new_pm = "PROVIDERS = ProviderManager(APP_ROOT, config_dir=CONFIG_DIR)"
    if old_pm not in text:
        raise SystemExit("ProviderManager construction not found")
    text = text.replace(old_pm, new_pm, 1)
    old_err = 'raise MMFError("正式文件生成失败：" + (completed.stderr or completed.stdout or "未知错误")[-800:])'
    new_err = 'raise MMFError("生成失败，请查看运行日志")'
    text = text.replace(old_err, new_err, 1)
    path.write_text(text, encoding="utf-8")


def patch_manager(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    old = '''    def __init__(self, app_root: Path, cache_seconds: int = 30):
        self.app_root = app_root.resolve()
        self.cache_seconds = cache_seconds
        self._lock = threading.RLock()
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.credential_store = CredentialStore("property_ai")
        self.local_path = self.app_root / "config" / "providers.local.json"
        self.example_path = self.app_root / "config" / "providers.example.json"
'''
    new = '''    def __init__(self, app_root: Path, cache_seconds: int = 30, config_dir: Path | None = None):
        self.app_root = app_root.resolve()
        self.cache_seconds = cache_seconds
        self._lock = threading.RLock()
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.credential_store = CredentialStore("property_ai")
        from paths import CONFIG_DIR
        cfg = Path(config_dir or CONFIG_DIR).resolve()
        self.local_path = cfg / "providers.local.json"
        self.example_path = cfg / "providers.example.json"
'''
    if old not in text:
        raise SystemExit("ProviderManager.__init__ not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_bridge(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    old = '''    prohibited_root = Path(r"D:\\YourProject").resolve()
    if workdir == prohibited_root:
        raise TaskValidationError("The whole property-material root cannot be used as working_directory.")
'''
    new = '''    import os
    for raw in [item for item in os.environ.get("MMF_FORBIDDEN_ROOTS", "").split(os.pathsep) if item.strip()]:
        if workdir == Path(raw).resolve():
            raise TaskValidationError("Forbidden workspace root cannot be used as working_directory.")
'''
    if old not in text:
        raise SystemExit("grok_bridge prohibited_root block not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def sanitize_copied_text(root: Path) -> None:
    import re
    user_profile = re.compile(r"(?i)[A-Za-z]:(?:\\\\+|\\+|/)Users(?:\\\\+|\\+|/)")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".py", ".html", ".txt"}:
            continue
        if path.name in {"assemble_release.py", "test_cleanroom_deployment.py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        updated = user_profile.sub("C:\\\\YourProject\\\\", text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def patch_grok(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    old = '''    def _bridge_paths(self) -> tuple[Path, Path]:
        default_dir = Path(__file__).resolve().parents[4] / "00_Agent_Infrastructure" / "Grok_Bridge_V0.1"
        bridge_file = Path(os.path.expandvars(str(self.config.get("bridge_path", default_dir / "grok_bridge.py")))).expanduser()
        config_file = Path(os.path.expandvars(str(self.config.get("bridge_config", bridge_file.parent / "bridge_config.json")))).expanduser()
        return bridge_file.resolve(), config_file.resolve()
'''
    new = '''    def _bridge_paths(self) -> tuple[Path, Path]:
        from paths import PACKAGE_ROOT
        default_dir = PACKAGE_ROOT / "providers" / "grok_bridge"
        bridge_file = Path(os.path.expandvars(str(self.config.get("bridge_path", default_dir / "grok_bridge.py")))).expanduser()
        config_file = Path(os.path.expandvars(str(self.config.get("bridge_config", bridge_file.parent / "bridge_config.json")))).expanduser()
        if not config_file.is_file():
            example = bridge_file.parent / "bridge_config.example.json"
            if example.is_file():
                config_file = example
        return bridge_file.resolve(), config_file.resolve()
'''
    if old not in text:
        raise SystemExit("grok_build._bridge_paths not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def write_manifest() -> dict:
    included = []
    excluded = [
        "R1-R9 historical experiments",
        "MMF-006D R10 Word/PPT authority artifacts",
        "MMF_Sprint_1",
        "QA reports and Todd acceptance records",
        "_project_control",
        "providers.local.json",
        "Todd personal paths",
        "API keys",
        "Grok CLI credentials",
    ]
    for path in PACKAGE.rglob("*"):
        if path.is_file() and "dist" not in path.parts and "__pycache__" not in path.parts:
            included.append(str(path.relative_to(PACKAGE)).replace("\\", "/"))
    payload = {
        "app_name": "MMF Desktop / Local",
        "app_version": "0.1.0-alpha",
        "runtime_version": "MMF-Runtime packaged for 006E",
        "provider_adapters": ["qwen_modelstudio", "grok_build_local_bridge", "kimi_moonshot_preset_only", "mock"],
        "renderer_version": {
            "word": "python-docx build_docx.py",
            "ppt": "ppt_renderer_v2 + @oai/artifact-tool",
        },
        "baseline_authority": "MMF-006D R10",
        "build_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "initial_deployable_candidate",
        "listen": "127.0.0.1",
        "included_components": included,
        "excluded_dev_assets": excluded,
        "notes": [
            "Does not modify R10 Word/PPT",
            "Does not start MMF-007",
            "Grok is optional local bridge",
            "Kimi is not marked available",
        ],
    }
    target = PACKAGE / "release_manifest.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    if not RUNTIME_SRC.is_dir():
        raise SystemExit(f"official runtime not found: {RUNTIME_SRC}")
    APP.mkdir(parents=True, exist_ok=True)
    for name in COPY_PY:
        copy_filtered(RUNTIME_SRC / name, APP / name)
    for name in COPY_DIRS:
        copy_filtered(RUNTIME_SRC / name, APP / name)
    overlay_mock = PACKAGE / "tools" / "overlays" / "mock.py"
    if overlay_mock.is_file():
        shutil.copy2(overlay_mock, APP / "providers" / "mock.py")
    PROVIDERS.mkdir(parents=True, exist_ok=True)
    copy_filtered(BRIDGE_SRC / "grok_bridge.py", PROVIDERS / "grok_bridge.py")
    copy_filtered(BRIDGE_SRC / "task_schema.json", PROVIDERS / "task_schema.json")
    example = PROVIDERS / "bridge_config.example.json"
    if example.is_file() and not (PROVIDERS / "bridge_config.json").is_file():
        shutil.copy2(example, PROVIDERS / "bridge_config.json")
    patch_app_core(APP / "app_core.py")
    patch_manager(APP / "providers" / "manager.py")
    patch_grok(APP / "providers" / "grok_build.py")
    patch_bridge(PROVIDERS / "grok_bridge.py")
    sanitize_copied_text(APP)
    sanitize_copied_text(PROVIDERS)
    shutil.copy2(APP / "requirements.txt", PACKAGE / "requirements.txt")
    readme = PACKAGE / "docs" / "README_用户使用说明.md"
    if readme.is_file():
        shutil.copy2(readme, PACKAGE / "README_用户使用说明.md")
    manifest = write_manifest()
    print(json.dumps({"status": "assembled", "files": len(manifest["included_components"])}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
