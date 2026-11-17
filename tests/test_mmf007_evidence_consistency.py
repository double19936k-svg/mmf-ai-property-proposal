"""EVIDENCE-01：Review Bundle 与 release_evidence 必须一致。"""
import json
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
WORKSPACE = PACKAGE.parent.parent
# 工作区治理同步后权威记录已迁至 MMF_Project_Records；索引目录下的旧路径继续兼容。
BUNDLE_CANDIDATES = [
    WORKSPACE / "00_项目管理与索引" / "MMF-007_review_bundle.json",
    WORKSPACE / "06_MVP产品与生成链" / "MMF_Project_Records" / "MMF-007" / "00_Current_Authority" / "MMF-007_review_bundle.json",
]
EVIDENCE = PACKAGE / "dist" / "MMF-006E-portable-v0.1" / "release_evidence.json"


def _bundle_path() -> Path:
    for candidate in BUNDLE_CANDIDATES:
        if candidate.is_file():
            return candidate
    # 判"这棵树根本不带权威记录"要用记录树本身是否存在，不能拿 release_evidence.json 当标志：
    # 那份文件是打包**之后**由 gen_release_evidence 生成的，刚构建完的 dist 里它还不存在，
    # 于是对账会在源码侧该报错的地方报错、在该跳过的时候跳过不了。
    if not (WORKSPACE / "06_MVP产品与生成链" / "MMF_Project_Records").is_dir():
        pytest.skip("production_dist 不含项目记录树，Review Bundle 对账在源码侧执行")
    raise AssertionError(
        "MMF-007 review bundle 不存在，已查找：" + " | ".join(str(row) for row in BUNDLE_CANDIDATES)
    )


def test_bundle_matches_release_evidence():
    bundle = json.loads(_bundle_path().read_text(encoding="utf-8"))
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert bundle["build_id"] == evidence["build_id"], (
        f"review bundle 记录的是 {bundle['build_id']}，当前 production_dist 是 {evidence['build_id']}；"
        "重新构建 dist 后必须同步更新权威记录"
    )
    assert bundle["production_dist_hash"] == evidence["package_sha256"]
    assert bundle["final_git_sha"] == evidence["git_sha_devtree"]
