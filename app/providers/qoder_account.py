"""Optional official account provider; original queue provider remains independent."""
from __future__ import annotations

from pathlib import Path

from .base import (AIProvider, ProviderError, normalize_generation, normalize_recommendation,
    parse_provider_payload, write_json)


def account_provider_rows() -> list[dict]:
    from qoder_account.connection import connection, enabled
    if not enabled():
        return []
    return [{"provider_name": "qoder_account", "provider_type": "qoder_account", "enabled": True,
        "display_name": connection().brand["label"] + "账户（实验）", "model": connection().selection.get("model", ""),
        "qualification_status": "experimental", "test_mode": False,
        "endpoint_alias": connection().brand["transport"], "timeout": 240,
        "billing_note": "通过Qoder官方登录使用账户额度；实际消耗以Qoder记录为准。"}]


class QoderAccountProvider(AIProvider):
    provider_version = "0.1-experimental"

    def __init__(self, config, credential_store=None):
        super().__init__(config, None)

    def prepare_ppt_run(self, run_id, body_pages, call_cap):
        from qoder_account.connection import connection
        return connection().prepare_ppt_run(run_id, body_pages, call_cap)

    def get_metadata(self):
        from qoder_account.connection import connection
        brand = connection().brand
        return {**super().get_metadata(), "model": connection().selection.get("model", ""),
            "credential_source": brand["credential_source"], "transport": brand["transport"], "account_edition": brand["id"],
            "observed_model": "NOT_AVAILABLE", "billable": None,
            "cost_availability": "NOT_AVAILABLE", "fallback_used": False}

    def health_check(self):
        from qoder_account.connection import connection
        return {**connection().health(), "metadata": self.get_metadata()}

    def _invoke(self, request: dict, task_dir: Path):
        from qoder_account.connection import connection, terminal_failure
        from .openai_compatible import CURRENT_RUN_ID, is_cancelled
        # Health/capability inspections are expressly non-generating on this route.
        if request.get("purpose") == "capability_probe" or request.get("generation_mode") == "capability_probe":
            raise ProviderError("连接检查不进行模型试写，请单独发起生成。", error_code="QODER_EXPLICIT_GENERATION_REQUIRED")
        payload = dict(request)
        payload["run_id"] = payload.get("run_id") or CURRENT_RUN_ID.get() or "standalone"
        from account_tasks import execute
        row = execute('qoder_account',payload,Path(task_dir),connection(),lambda:is_cancelled(payload['run_id']))
        if row.get("is_error") or row.get("stop_reason") == "max_tokens":
            raise terminal_failure(row)
        if payload.get('generation_mode') == 'tender_requirement_recognition':
            from .account_recognition import parse_recognition
            result, repair = parse_recognition('qoder_account', payload, row.get('body'))
        else:
            result, repair = parse_provider_payload(payload, row.get("body"), set(payload.get("required_keys") or []))
        if request.get("generation_mode") == "ppt_slide" and result.get("slide_id") != request.get("slide_id"):
            raise ProviderError("Qoder返回的页号与请求不一致，原答已保留。", error_code="PPT_SLIDE_ID_MISMATCH")
        models = list((row.get("model_usage") or {}).keys())
        metadata = self._task_metadata(str(request.get("task_id") or ""), formal_output=True,
            structured_purpose=request.get("purpose", "generic"), serialization_repair=repair or {"applied": False},
            requested_model=row.get("requested_model"), observed_model=models[0] if len(models) == 1 else "NOT_AVAILABLE",
            session_id=row.get("session_id"), sdk_num_turns=row.get("num_turns"),
            usage=row.get("usage"), model_usage=row.get("model_usage"),
            total_credits=row.get("total_credits"), total_cost_usd=row.get("total_cost_usd"),
            cost_availability="available" if row.get("total_credits") is not None or row.get("total_cost_usd") is not None else "NOT_AVAILABLE",
            finish_reason=row.get("stop_reason") or row.get("subtype"), request_digest=row["digest"])
        write_json(Path(task_dir) / "provider_raw_envelope.json", row)
        write_json(Path(task_dir) / "provider_structured_output.json", result)
        write_json(Path(task_dir) / "provider_audit.json", metadata)
        connection().event("validated_response", run_id=payload["run_id"], task_id=request.get("task_id"),
            slide_id=request.get("slide_id"), total_credits=row.get("total_credits"))
        return result, metadata

    def invoke_structured(self, request, task_dir):
        result, metadata = self._invoke(request, task_dir)
        return {**result, "provider_metadata": metadata}

    def collect_late_response(self,request,task_dir):
        from account_tasks import cached_request
        original=cached_request('qoder_account',request)
        return self._invoke(original,task_dir) if original else None

    def recommend_knowledge(self, request, task_dir):
        result, metadata = self._invoke(request, task_dir)
        return normalize_recommendation(result, metadata)

    def generate_solution(self, request, task_dir):
        result, metadata = self._invoke(request, task_dir)
        return normalize_generation(result, metadata)
