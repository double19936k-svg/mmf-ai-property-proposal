"""MMF-007 Phase 4/5/7：Provider 治理、数据外发披露、验收就绪门（V1.1 新增要求）。

- Provider 真实可用状态分层：installed / reachable / authenticated / quota_available /
  model_available / effective_config——"能连 API"≠"Provider 可用"；
- 错误分类禁止把网络故障误报为登录（AUTH_FAILED 仅限明确认证拒绝）；
- 数据外发披露：provider_data_disclosure 结构（只记录范围与 ID，不记录客户原文）；
- 验收就绪门：工程完整性全部满足才允许 READY_FOR_TODD_REVIEW，Todd 只审业务。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

SECRET_PATTERNS = re.compile(
    r"sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|api[_-]?key\s*[=:：]\s*\S+|Authorization\s*:",
    re.I,
)


def provider_status_layers(*, app_installed: bool, health: dict[str, Any] | None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """把健康检查结果分解为 V1.1 要求的独立状态层。

    health: 现有 health_check() 产物（configured/available/status/credential_source）。
    未经探测的层一律 unknown，不得假报可用。
    """
    health = health or {}
    configured = bool(health.get("configured"))
    reachable = health.get("status") in {"available"} if configured else False
    credential_source = str(health.get("credential_source") or "none")
    authenticated = reachable and credential_source not in {"none", "environment:missing"}
    quota_available = reachable  # 无独立额度探测时不得高于 reachable；真实额度探测留待 Provider 支持
    model_available = reachable and bool((config or {}).get("model"))
    return {
        "installed": bool(app_installed),
        "reachable": bool(reachable),
        "authenticated": bool(authenticated) if configured else False,
        "quota_available": {"status": "ASSUMED_FROM_REACHABLE" if quota_available else "UNKNOWN"},
        "model_available": bool(model_available),
        "effective_config": {
            "model": (config or {}).get("model"),
            "provider_type": (config or {}).get("provider_type"),
            "endpoint_alias": (config or {}).get("endpoint_alias"),
        },
        "configured": configured,
        "credential_source": credential_source,
        "note": "quota 层当前不可独立探测，标记 ASSUMED_FROM_REACHABLE；不得据此承诺额度。",
    }


def provider_data_disclosure(
    *,
    provider_name: str,
    model: str,
    sent_file_names: list[str],
    sent_requirement_ids: list[str],
    sent_knowledge_ids: list[str],
    redaction_applied: bool,
) -> dict[str, Any]:
    """外发披露结构：只记录范围与标识，禁止记录完整客户原文。"""
    return {
        "schema_version": "provider-data-disclosure-v0.1",
        "provider": provider_name,
        "model": model,
        "sent_file_names": list(sent_file_names),
        "sent_extracted_sections": "structured requirement items（编号+文本范围，不含整份原文）",
        "sent_requirement_ids": list(sent_requirement_ids)[:200],
        "sent_knowledge_ids": list(sent_knowledge_ids),
        "redaction_applied": bool(redaction_applied),
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "本地运行不等于数据不外发；本结构用于运行留痕与用户告知。",
    }


def scan_log_for_secrets(text: str) -> list[str]:
    """Log Hygiene：返回命中的疑似秘密模式（供 CI/清理使用）。"""
    hits = []
    for match in SECRET_PATTERNS.finditer(text or ""):
        hits.append(match.group(0)[:24] + "…")
    return hits


def acceptance_ready_gate(
    *,
    service_healthy: bool,
    ui_reachable: bool,
    links_valid: bool,
    artifact_exists: bool,
    artifact_downloadable: bool,
    open_folder_works: bool,
    internal_qa_complete: bool,
    repair_queue_settled: bool,
    production_runtime_known: bool,
) -> dict[str, Any]:
    """ACCEPTANCE_READY_GATE：全部工程项满足才 READY_FOR_TODD_REVIEW。

    未达标时返回 NOT_READY + 缺项清单；工程排错不得转嫁为 Todd 的人工验收任务。
    """
    checks = {
        "service_healthy": bool(service_healthy),
        "ui_reachable": bool(ui_reachable),
        "links_valid": bool(links_valid),
        "artifact_exists": bool(artifact_exists),
        "artifact_downloadable": bool(artifact_downloadable),
        "open_folder_works": bool(open_folder_works),
        "internal_qa_complete": bool(internal_qa_complete),
        "repair_queue_settled": bool(repair_queue_settled),
        "production_runtime_known": bool(production_runtime_known),
    }
    missing = [name for name, ok in checks.items() if not ok]
    ready = not missing
    return {
        "schema_version": "acceptance-ready-gate-v0.1",
        "checks": checks,
        "missing": missing,
        "status": "READY_FOR_TODD_REVIEW" if ready else "NOT_READY",
        "todd_scope": ["业务价值", "关键事实", "客户表达", "PPT视觉"],
        "engineering_scope": [name for name in checks],
        "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
