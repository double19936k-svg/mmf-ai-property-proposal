from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AIProvider, normalize_generation, normalize_recommendation, write_json


class MockProvider(AIProvider):
    provider_version = "0.1"

    def health_check(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", True))
        return {
            "configured": enabled,
            "available": enabled,
            "status": "available" if enabled else "not_configured",
            "message": "测试模式可用" if enabled else "未启用",
            "metadata": self.get_metadata(),
        }

    def recommend_knowledge(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=False)
        catalog = request["catalog"]
        result = {
            "recommended_positive": [
                {"ku_id": row["ku_id"], "knowledge_name": f"测试推荐经验 {index + 1}", "summary": row["core_knowledge"], "reason": "用于验证界面与流程，不作为正式方案结论。"}
                for index, row in enumerate(catalog["positive"][:2])
            ],
            "applicable_guardrails": [
                {"ku_id": row["ku_id"], "risk_content": row["core_knowledge"], "reason": "测试模式风险控制示例。"}
                for row in catalog["guardrail"][:2]
            ],
            "missing_information": ["测试模式：请按真实项目补充未确认事实。"],
        }
        metadata = self._task_metadata(request["task_id"], formal_output=False)
        write_json(task_dir / "provider_structured_output.json", result)
        write_json(task_dir / "provider_audit.json", metadata)
        return normalize_recommendation(result, metadata)

    def generate_solution(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=False)
        brief = request["brief"]
        if brief["medium"] == "WORD":
            artifact = {
                "title": f"测试模式｜{brief['scenario']}章节初稿",
                "subtitle": f"{brief['project_name']}｜仅用于流程测试",
                "lead": ["本文件由测试引擎生成，只验证产品流程，不得作为正式物业方案使用。"],
                "sections": [{"heading": "测试内容", "paragraphs": ["已完成项目说明、知识确认、结构化生成与文件输出链路验证。"], "bullets": ["不包含正式结论", "不构成项目承诺"]}],
            }
        else:
            artifact = {
                "title": f"测试模式｜{brief['scenario']}",
                "subtitle": f"{brief['project_name']}｜仅用于流程测试",
                "slides": [{"title": "流程验证", "core_message": "本页不代表正式方案内容", "layout": "overview", "bullets": ["项目说明已接收", "知识确认已完成", "文件生成链可运行"]}],
            }
        result = {
            "artifact": artifact,
            "citation_registry": [{"claim": "当前为流程验证输出", "source_type": "current_project_fact", "source_id": "brief.requirements"}],
            "guardrail_non_use": [],
            "clarification_list": ["测试模式输出不得用于正式业务。"],
        }
        metadata = self._task_metadata(request["task_id"], formal_output=False)
        write_json(task_dir / "provider_structured_output.json", result)
        write_json(task_dir / "provider_audit.json", metadata)
        return normalize_generation(result, metadata)

    def _section_fragment(self, request: dict[str, Any]) -> dict[str, Any]:
        prompt = str(request.get("prompt") or "")
        context = {}
        if "Context Pack：" in prompt:
            try:
                context = json.loads(prompt.rsplit("Context Pack：\n", 1)[1])
            except (json.JSONDecodeError, IndexError, TypeError):
                context = {}
        contract = context.get("section_contract") or {}
        processes = context.get("relevant_process_contracts") or []
        process_text = "；".join(step for row in processes for step in row.get("steps", []))
        must = list(contract.get("must_cover") or ["本节项目化实施逻辑"])
        outputs = list(contract.get("required_outputs") or ["实施记录"])
        filler = "围绕现场任务明确责任界面、执行动作、异常处理、检查方法和成果记录，使服务内容能够被实施、跟踪和复核。"
        target = int(((contract.get("target_words") or {}).get("min") or 400))
        short = bool(request.get("mock_short"))
        body = "；".join(must + outputs + ([process_text] if process_text else [])) + "。"
        if not short:
            body += filler * max(1, target // max(1, len(filler)))
        return {
            "section_id": contract.get("section_id") or request.get("section_id") or "S01-01",
            "title": contract.get("section_title") or "本节",
            "body_blocks": [{"type": "paragraph", "content": body}],
            "tables": [],
            "processes": processes,
            "callouts": [],
            "cross_references": [],
            "claims": [],
            "used_requirement_ids": contract.get("source_requirements") or [],
            "used_ku_ids": [],
            "generation_notes": ["mock_longform_section"],
        }

    def invoke_structured(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=True)
        if request.get("mock_response"):
            result = dict(request["mock_response"])
        elif request.get("generation_mode") == "longform_section" or "section_contract" in str(request.get("prompt") or ""):
            result = self._section_fragment(request)
        else:
            result = {"ok": True}
        metadata = self._task_metadata(
            request["task_id"],
            formal_output=False,
            structured_purpose=request.get("purpose", "generic"),
            finish_reason="stop",
            input_tokens=len(str(request.get("prompt") or "")) // 4,
            output_tokens=len(json.dumps(result, ensure_ascii=False)) // 4,
        )
        write_json(task_dir / "provider_raw_envelope.json", {"mock": True, "payload": result})
        write_json(task_dir / "provider_structured_output.json", result)
        write_json(task_dir / "provider_audit.json", metadata)
        return {**result, "provider_metadata": metadata}
