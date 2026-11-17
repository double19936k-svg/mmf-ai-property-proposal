"""MMF-007 Provider Benchmark 收集器：从 run 目录提取统一指标行。

用法：runtime/dev_python/Scripts/python.exe tools/benchmark_collector.py <run_dir> [<run_dir> ...]
输出：每行一个 JSON（stdout），字段对齐总纲第八节 Benchmark 指标。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def collect(run_dir: Path) -> dict | None:
    audit = _load(run_dir / "run_audit.json")
    if not audit:
        return None
    matrix = _load(run_dir / "02_requirement_section_matrix.json") or {}
    scoring = _load(run_dir / "09_scoring_matrix.json") or {}
    ta = audit.get("token_accounting") or {}
    row = {
        "run_id": audit.get("run_id"),
        "provider": audit.get("provider_name"),
        "model": audit.get("model"),
        "speed_profile": audit.get("speed_profile"),
        "medium": (audit.get("project_brief") or {}).get("medium"),
        "project": (audit.get("project_brief") or {}).get("project_name"),
        "total_processing_seconds": audit.get("total_processing_seconds"),
        "generation_seconds": audit.get("generation_elapsed_seconds"),
        "planning_seconds": audit.get("planning_seconds"),
        "repair_seconds": audit.get("repair_seconds"),
        "initial_calls": audit.get("INITIAL_GENERATION_CALLS"),
        "continuation_calls": audit.get("CONTINUATION_CALLS"),
        "repair_calls": audit.get("SECTION_REPAIR_CALLS"),
        "retry_calls": audit.get("RETRY_CALLS"),
        "total_provider_calls": audit.get("TOTAL_PROVIDER_CALLS"),
        "call_reduction_ratio": audit.get("INITIAL_CALL_REDUCTION_RATIO"),
        "max_concurrency": audit.get("MAX_OBSERVED_CONCURRENCY"),
        "input_tokens": ta.get("input_tokens") or audit.get("input_tokens"),
        "output_tokens": ta.get("output_tokens") or audit.get("output_tokens"),
        "logical_sections": audit.get("logical_section_count"),
        "generation_batches": audit.get("generation_batch_count"),
        "repair_budget_used": audit.get("REPAIR_BUDGET_USED"),
        "delivery_status": audit.get("delivery_status"),
        "final_artifact_qa": audit.get("final_artifact_qa_status"),
        "review_count": audit.get("review_count"),
        "requirement_coverage": matrix.get("coverage_summary"),
        "scoring_coverage": scoring.get("coverage_summary"),
        "build_id": audit.get("build_id"),
        "runtime_source": audit.get("runtime_source"),
        "created_at": audit.get("created_at"),
    }
    artifact = audit.get("artifact_display_path")
    row["artifact"] = artifact
    return row


def main() -> None:
    rows = []
    for arg in sys.argv[1:]:
        row = collect(Path(arg))
        if row:
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False))
        else:
            print(json.dumps({"error": "no run_audit", "run_dir": arg}, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
