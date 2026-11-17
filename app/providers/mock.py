from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AIProvider, normalize_generation, normalize_recommendation, write_json


def _current_batch_id_from_prompt(prompt: str) -> str | None:
    text = str(prompt or "")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("CURRENT_BATCH_ID") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip().split()[0].strip("\"'")
            return value or None
    return None


# Round 4.1 §5/§6：合成夹具的 CU 正文必须**逐条不同**。旧实现把同一句 filler 重复到目标
# 字数，于是一节内 CU 之间的句级重复率实测 **0.935**，而 Run D（真实 Provider）同口径实测
# **0.001**（27 节 4,557 条长句去重后 4,552 条）。装配期的本地去重因此把合成链约四分之一的
# 正文剥掉，剥掉的比例随"每节 CU 条数"变化（70 类 2.9 条/节几乎不触发、300 类 11.8 条/节
# 剥掉 25%）—— 这等于让"账本字 → 页"的产率随规模漂移，一把页数尺子不可能同时量准三档。
# 夹具只用中性的运营措辞：不写"确保/保证/承诺"，避免夹具替投标人造承诺（§40 承诺门）。
# 可见文本里也**不出现 CU id**：把 `CU-S05-03-02` 印进正文等于让规模验证跑在一条
# "内部标识泄漏"的假象上（那条链路 QA 本来就要拦）。唯一性靠"条目序号 + 该条自己的主题
# +  facet/动作/载体/检查 四元组"组合保证。
CU_FACETS = ("任务分解", "责任到岗", "时序安排", "物料与场地准备", "记录载体", "检查点设置",
             "异常口径", "接口交接", "复盘改进", "交底培训", "旁站与巡检", "整改闭环")
CU_ACTIONS = ("落实到岗到人", "按班次核对", "随现场条件变化同步调整", "与相关方确认后执行",
              "当日录入当日核销", "按分级机制上报", "在检查点复核有效性", "纳入日周月节奏")
CU_RECORDS = ("作业台账", "巡查记录单", "交底单", "复核单", "整改通知单", "交接清单")
CU_CHECKS = ("日检", "周检", "专项检查", "班组自检", "旁站检查", "留痕抽查", "闭环复核")
CU_ROLES = ("岗位人员", "专业负责人", "品质主管", "项目经理", "班组长")

# 结构单元的实测密度（同一份探针在 Run D 真实产物上逐单元量出来的，不是估的）：
#   表行 82–99 字/行（四列 → 每格约 23 字）、流程步 47–53 字、清单项 52 字
# 旧夹具每格只写 4–5 个字（"事项1/按计划执行/形成记录/复核关闭"），于是**计划按 105 字/行
# 排结构额度、夹具只交 30 字/行** —— 实测结构占比从计划的 0.31 掉到 0.12。两路收益模型
# 正是按占比换算页数的，占比对不上时任何一把页数尺都不可能同时量准 70/150/300 三档。
RUN_D_ROW_CELL_CHARS = 23
RUN_D_STEP_CHARS = 50
RUN_D_ITEM_CHARS = 52
CU_CLAUSES = ("落实到岗到人", "按班次核对", "与相关方确认后执行", "形成可追溯记录",
              "按分级机制上报", "在检查点复核", "纳入日周月节奏", "同步更新台账")


def _unit_text(cu_id: str, index: int, topic: str, chars: int) -> str:
    """一条**与其他条不重复**、且长度贴着实测密度的合成结构单元文本。"""
    facet = CU_FACETS[index % len(CU_FACETS)]
    parts = [f"{facet}第{index + 1}项：围绕{topic}"]
    step = 0
    while sum(len(part) for part in parts) < max(8, int(chars)) and step < 12:
        parts.append(CU_ACTIONS[(index // len(CU_FACETS) + step) % len(CU_ACTIONS)])
        step += 1
    return "，".join(parts)


def _payload_chars(value: Any) -> int:
    if isinstance(value, str):
        return len("".join(value.split()))
    if isinstance(value, dict):
        return sum(_payload_chars(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_payload_chars(item) for item in value)
    return 0


def _cu_body(heading: str, cu_id: str, topic: str, chars: int) -> str:
    """按给定的正文字数产出**逐条不重复**的合成正文（确定性，无随机）。"""
    parts = [heading]
    index = 0
    while sum(len(part) for part in parts) < max(0, int(chars)) and index < 400:
        facet = CU_FACETS[index % len(CU_FACETS)]
        action = CU_ACTIONS[(index // len(CU_FACETS) + index) % len(CU_ACTIONS)]
        record = CU_RECORDS[(index * 2 + len(topic)) % len(CU_RECORDS)]
        check = CU_CHECKS[(index * 3) % len(CU_CHECKS)]
        role = CU_ROLES[(index + 1) % len(CU_ROLES)]
        parts.append(f"{facet}第{index + 1}项：围绕{topic}，{action}，由{role}形成{record}，"
                     f"以{check}确认闭环。")
        index += 1
    return "".join(parts)


class MockProvider(AIProvider):
    provider_version = "0.1"

    def __init__(self, config: dict[str, Any], credential_store: Any = None):
        super().__init__(config, credential_store)
        self.received_content_unit_ids: list[str] = []
        self.received_requests: list[dict[str, Any]] = []

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
        required_tables = list(contract.get("required_tables") or [])
        filler = "围绕现场任务明确责任界面、执行动作、异常处理、检查方法和成果记录，使服务内容能够被实施、跟踪和复核。"
        target = int(((contract.get("target_words") or {}).get("min") or 400))
        short = bool(request.get("mock_short"))
        body = "；".join(must + outputs + ([process_text] if process_text else [])) + "。"
        if not short:
            body += filler * max(1, target // max(1, len(filler)))
        # Blocker 4：Content Unit 真实执行——按收到的 CU 契约产出 per-CU fragments，
        # 并记录接收的 CU ID（供审计 planned/sent/generated/completed 集合比对）。
        cu_contracts = request.get("content_unit_contracts") or context.get("content_unit_contracts") or []
        cu_execution = request.get("content_unit_execution") or {}
        content_unit_fragments = []
        for index, cu in enumerate(cu_contracts):
            cu_id = str(cu.get("content_unit_id") or "")
            if not cu_id:
                continue
            self.received_content_unit_ids.append(cu_id)
            # The mock models the same CU contract as a real provider.  The first
            # CU carries the Section-level hard coverage contract; subsequent CUs
            # elaborate their own intent.  This prevents the Section QA gate from
            # passing on fabricated section-level fallback text.
            section_contract_suffix = ""
            if index == 0:
                section_contract_suffix = (
                    f"本节必须覆盖：{'；'.join(str(value) for value in must)}。"
                    f"本节应形成：{'；'.join(str(value) for value in outputs)}。"
                    + (f"具体流程包括：{process_text}。" if process_text else "")
                )
            budget = cu.get("target_budget") or 900
            target_chars = int((budget.get("effective_chars") if isinstance(budget, dict) else budget) or 900)
            archetype = str(cu.get("archetype") or "NARRATIVE")
            cu_text = f"{cu.get('intent') or cu.get('title') or '内容单元'}：围绕{cu.get('must_cover') or ['既定要点']}形成可执行内容，按目标信息量组织动作、记录与检查。{section_contract_suffix}"
            payload = None
            # 主题用"、"而不是"；"连接：分号在中文里是句读，夹具把它放进一个短语里会让
            # 该短语的后半段在所有 CU 里变成同一句（重复率量出来会虚高）。
            cu_topic = "、".join(str(value) for value in
                                 (cu.get("must_cover") or [cu.get("title") or "既定要点"]))
            if archetype in {"STANDARD_TABLE", "MATRIX", "KPI_TABLE", "RESPONSIBILITY_MATRIX", "RISK_REGISTER", "SCHEDULE"}:
                row_count = max(4, int((budget.get('rows') if isinstance(budget, dict) else 4) or 4))
                payload = {"title": cu.get("title") or "实施管理表", "columns": ["事项", "责任动作", "输出记录", "检查要求"],
                           "rows": [[_unit_text(cu_id, row * 4 + cell, cu_topic, RUN_D_ROW_CELL_CHARS)
                                     for cell in range(4)]
                                    for row in range(1, row_count + 1)],
                           "notes": "表内要求结合现场条件执行。"}
            elif archetype in {"SOP", "PROCESS", "EMERGENCY_PLAYBOOK"}:
                step_count = max(5, int((budget.get('steps') if isinstance(budget, dict) else 5) or 5))
                payload = {"purpose": cu.get("intent") or "形成可执行闭环", "scope": "本项目相关服务范围", "roles": ["项目负责人", "专业负责人", "执行岗位"], "prerequisites": ["确认任务边界", "准备记录载体"], "steps": [_unit_text(cu_id, 100 + row, cu_topic, RUN_D_STEP_CHARS) for row in range(1, step_count + 1)], "exceptions": ["异常事项按项目机制分级升级"], "records": ["执行记录", "复核记录"], "acceptance": ["动作完成且记录可追溯"]}
            elif archetype == "CHECKLIST":
                item_count = max(6, int((budget.get('items') if isinstance(budget, dict) else 6) or 6))
                payload = {"title": cu.get("title") or "检查清单", "items": [_unit_text(cu_id, 200 + row, cu_topic, RUN_D_ITEM_CHARS) for row in range(1, item_count + 1)]}
            elif archetype == "FORM_TEMPLATE":
                fields = [f"字段{row}" for row in range(1, max(6, int((budget.get('fields') if isinstance(budget, dict) else 6) or 6)) + 1)]
                # §61：合成夹具按 `FORM_PAYLOAD_SPEC` 给全九要素 —— 夹具缺要素会让
                # "表单在真实链路里能凑齐九项"这条测试永远测不到（它只测缺项）。
                payload = {"form_name": cu.get("title") or "业务记录表", "purpose": "记录执行过程并支持追溯", "fields": fields,
                           "field_rules": {field: "当场填写，与实际作业记录一致" for field in fields},
                           "required_fields": fields[:4], "approval": "专业负责人复核",
                           "frequency": "按班次记录，异常即时记录",
                           "storage_location": "项目部资料室归档，同步上传公司档案系统",
                           "retention": "按项目档案要求保存",
                           "review": "专业负责人逐条复核，品质主管抽查"}
            # Fault/smoke fixtures use mock_short to exercise the real
            # under-length continuation path.  Keep every requested CU in the
            # response (so the CU contract itself remains valid), but do not
            # let structured payload cells accidentally make a deliberately
            # short Section look complete.
            if short:
                cu_text = f"{cu.get('title') or '内容单元'}：待补充。"
                payload = None
            # 正文与结构 payload 合起来才是一条 CU 的交付量：旧的"正文单独补到 0.8×额度"
            # 在 payload 只有三十字时会让 CU 落在额度之下、在 payload 达标时又让它超额度。
            payload_chars = _payload_chars(payload) if payload else 0
            prose_chars = max(int(target_chars * 0.45), target_chars - payload_chars)
            content_unit_fragments.append({
                "content_unit_id": cu_id,
                "parent_section_id": contract.get("section_id"),
                "archetype": archetype,
                "content": cu_text if short else _cu_body(cu_text, cu_id, cu_topic, prose_chars),
                "payload": payload,
                "effective_chars": len(cu_text),
            })
        result = {
            "section_id": contract.get("section_id") or request.get("section_id") or "S01-01",
            "title": contract.get("section_title") or "本节",
            "body_blocks": [{"type": "paragraph", "content": body}],
            "content_unit_fragments": content_unit_fragments,
            "content_unit_execution": {
                "mode": cu_execution.get("mode") or "SECTION_GRANULAR",
                "received_content_unit_ids": [c.get("content_unit_id") for c in cu_contracts if c.get("content_unit_id")],
            },
            "tables": [
                {
                    "title": str(item.get("purpose") or "实施检查表") if isinstance(item, dict) else str(item),
                    "headers": ["检查事项", "责任动作", "记录方式"],
                    "rows": [["现场任务", "按计划执行并复核", "形成可追溯记录"]],
                }
                for item in (required_tables or [{"purpose": "实施检查表"}])
            ],
            "processes": processes,
            "callouts": [],
            "cross_references": [],
            "claims": [],
            "used_requirement_ids": contract.get("source_requirements") or [],
            "used_ku_ids": [],
            "generation_notes": ["mock_longform_section"],
        }
        return result

    def _batch_fragments(self, request: dict[str, Any]) -> dict[str, Any]:
        prompt = str(request.get("prompt") or "")
        payload = {}
        if "Context Pack：" in prompt:
            try:
                payload = json.loads(prompt.rsplit("Context Pack：\n", 1)[1])
            except (json.JSONDecodeError, IndexError, TypeError):
                payload = {}
        rows = payload.get("sections") or []
        requested = payload.get("requested_section_ids") or request.get("section_ids") or []
        sections = []
        drop = set(request.get("mock_drop_sections") or [])
        fail = set(request.get("mock_fail_sections") or [])
        for row in rows:
            contract = row.get("section_contract") or {}
            sid = contract.get("section_id")
            if sid in drop:
                continue
            fake = dict(request)
            fake["prompt"] = "Context Pack：\n" + json.dumps(row, ensure_ascii=False)
            fake["mock_short"] = bool(request.get("mock_short") or sid in fail)
            fake["content_unit_contracts"] = row.get("content_unit_contracts") or []
            fake["content_unit_execution"] = row.get("content_unit_execution") or {}
            sections.append(self._section_fragment(fake))
        if not sections and requested:
            for sid in requested:
                if sid in drop:
                    continue
                fake = dict(request)
                fake["section_id"] = sid
                sections.append(self._section_fragment(fake))
        batch_id = _current_batch_id_from_prompt(prompt) or request.get("batch_id") or "GB-mock"
        return {"batch_id": batch_id, "sections": sections}

    def invoke_structured(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        task_dir.mkdir(parents=True, exist_ok=True)
        self.received_requests.append({
            "task_id": request.get("task_id"),
            "generation_mode": request.get("generation_mode"),
            "section_ids": list(request.get("section_ids") or ([request.get("section_id")] if request.get("section_id") else [])),
            "content_unit_ids": [
                str(row.get("content_unit_id"))
                for row in (request.get("content_unit_contracts") or [])
                if isinstance(row, dict) and row.get("content_unit_id")
            ],
            "content_unit_contracts": [
                dict(row)
                for row in (request.get("content_unit_contracts") or [])
                if isinstance(row, dict)
            ],
        })
        if request.get("mock_rate_limit"):
            from .base import ProviderError
            raise ProviderError("429 rate limit", error_code="RATE_LIMIT")
        if request.get("mock_latency_error"):
            from .base import ProviderError
            raise ProviderError("timed out", error_code="TIMEOUT")
        if request.get("mock_response"):
            result = dict(request["mock_response"])
        elif request.get("generation_mode") == "longform_batch" or "一次生成多个Word Section" in str(request.get("prompt") or ""):
            result = self._batch_fragments(request)
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
