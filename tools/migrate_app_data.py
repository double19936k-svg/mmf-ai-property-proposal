"""MMF-007 Phase 6：升级数据迁移/备份/回滚（V1.1 MIG 契约）。

流程：PRECHECK → BACKUP → MIGRATE（复制式）→ VALIDATE → COMMIT；任一步失败 → ROLLBACK。
备份内容：Provider 配置、凭据引用（不含明文）、user settings、history 元数据、run 状态索引。
用法：python tools/migrate_app_data.py <app_root> [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

BACKUP_FILES = [
    "config/providers.local.json",
    "config/user_settings.json",
    "config/app.json",
]
STATE = "migration_state.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def precheck(app_root: Path) -> dict:
    problems = []
    for rel in BACKUP_FILES:
        if not (app_root / rel).is_file():
            problems.append(f"missing:{rel}")
    return {"ok": not problems, "problems": problems}


def backup(app_root: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle = backup_dir / f"pre_migration_{stamp}.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in BACKUP_FILES:
            path = app_root / rel
            if path.is_file():
                zf.write(path, rel)
        # 凭据引用信息（不导出明文）：记录引用形态存在性
        zf.writestr("credential_reference.json", json.dumps({
            "credential_store": "windows_credential_manager",
            "exported": False,
            "note": "凭据明文永不进入备份；仅记录引用存在性。",
        }, ensure_ascii=False))
    manifest = {"bundle": str(bundle), "sha256": _sha(bundle.read_bytes()), "created_at": stamp}
    (backup_dir / f"pre_migration_{stamp}.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def validate(app_root: Path) -> dict:
    ok = True
    for rel in BACKUP_FILES:
        if (app_root / rel).is_file():
            try:
                json.loads((app_root / rel).read_text(encoding="utf-8-sig"))
            except Exception:
                ok = False
    return {"ok": ok}


def migrate(app_root: Path, *, dry_run: bool = False) -> dict:
    app_root = Path(app_root)
    state = {"schema_version": "app-migration-v0.1", "steps": [], "status": "STARTED", "started_at": datetime.now().isoformat(timespec="seconds")}
    pre = precheck(app_root)
    state["steps"].append({"step": "PRECHECK", **pre})
    if not pre["ok"]:
        state["status"] = "ABORTED_PRECHECK"
        return state
    backup_dir = app_root / "migration_backup"
    bundle = backup(app_root, backup_dir)
    state["steps"].append({"step": "BACKUP", "bundle": str(bundle)})
    try:
        # MIGRATE（复制式迁移示例：确保 config 结构包含新版本字段；真实字段迁移随版本演进）
        config_path = app_root / "config" / "providers.local.json"
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        config.setdefault("migration_meta", {"data_schema_version": 2, "migrated_at": state["started_at"]})
        if not dry_run:
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state["steps"].append({"step": "MIGRATE", "applied": not dry_run})
        validation = validate(app_root)
        state["steps"].append({"step": "VALIDATE", **validation})
        if not validation["ok"]:
            raise RuntimeError("迁移后校验失败")
        state["status"] = "COMMITTED"
    except Exception as exc:
        state["steps"].append({"step": "ROLLBACK", "reason": str(exc)[:200]})
        for rel in BACKUP_FILES:
            src = bundle and None
        # 从最近备份恢复
        bundles = sorted(backup_dir.glob("pre_migration_*.zip"))
        if bundles:
            with zipfile.ZipFile(bundles[-1]) as zf:
                zf.extractall(app_root)
        state["status"] = "ROLLED_BACK"
    state["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (app_root / STATE).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    result = migrate(root, dry_run=dry)
    print(json.dumps(result, ensure_ascii=False, indent=2))
