from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MEDIA_TYPES = {
    ".pdf": "pdf_text_layer",
    ".docx": "docx",
    ".txt": "txt",
    ".md": "markdown",
}
CLASSIFICATIONS = {
    "PROJECT_SPECIFIC",
    "GENERIC_REQUIREMENT",
    "POTENTIAL_BOILERPLATE",
    "CONFLICT_OR_AMBIGUOUS",
}
MANDATORY_LEVELS = {"MUST", "SHOULD", "INFO", "UNKNOWN"}
CONFIRMATION_STATUSES = {
    "UNCONFIRMED",
    "CONFIRMED",
    "REJECTED_AS_NOT_APPLICABLE",
    "EDITED",
    "DEFERRED",
}
CANONICAL_FACT_KEYS = {
    "project_name",
    "project_type",
    "location",
    "gross_area",
    "managed_area",
    "building_count",
    "building_functions",
    "contract_duration",
    "service_start_date",
    "service_end_date",
    "service_scope_summary",
    "excluded_scope",
}
SERVICE_SCOPE_KEYS = {"included", "excluded", "deprioritized", "conditional"}


class TenderError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def safe_filename(filename: str) -> str:
    name = str(filename or "").strip()
    if not name or Path(name).name != name or ".." in name or any(x in name for x in ("/", "\\", ":")):
        raise TenderError("EXTRACT_IO", "文件名不安全，已拒绝保存。")
    cleaned = re.sub(r"[\x00-\x1f<>\"|?*]", "_", name).strip(" .")
    if not cleaned:
        raise TenderError("EXTRACT_IO", "文件名无效。")
    return cleaned[:180]


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise TenderError("EXTRACT_IO", "路径越界，已拒绝访问。") from exc
    return resolved


def validate_extraction(value: dict[str, Any]) -> None:
    required = {"schema_version", "extraction_id", "run_id", "files", "outline", "pages", "paragraphs", "tables", "warnings", "processing_mode"}
    missing = required - set(value)
    if missing:
        raise TenderError("EXTRACT_IO", f"Extraction结构缺失：{sorted(missing)}")
    for key in ("files", "outline", "pages", "paragraphs", "tables", "warnings"):
        if not isinstance(value[key], list):
            raise TenderError("EXTRACT_IO", f"Extraction字段{key}必须是数组。")
    if not value["files"]:
        raise TenderError("EXTRACT_EMPTY_BODY", "没有可解析文件。")
    for page in value["pages"]:
        if not {"file_id", "page_no", "text", "char_count", "likely_scan", "outline_node_id"} <= set(page):
            raise TenderError("EXTRACT_IO", "PDF页面定位结构不完整。")


def validate_pack_shape(value: dict[str, Any]) -> None:
    required = {
        "schema_version", "pack_id", "run_id", "status", "source_registry", "project_facts", "service_scope",
        "explicit_requirements", "mandatory_requirements", "service_standards", "staffing_requirements",
        "service_hours", "sla_kpi", "facility_requirements", "handover_requirements", "security_requirements",
        "environment_requirements", "engineering_requirements", "customer_service_requirements",
        "commercial_or_assessment_requirements", "contract_or_duration_requirements", "exclusions", "ambiguities",
        "conflicts", "potential_boilerplate", "clarification_items", "requirements", "scoring_items", "confirmation",
    }
    missing = required - set(value)
    if missing:
        raise TenderError("UNDERSTAND_SCHEMA_INVALID", f"Requirement Pack缺失字段：{sorted(missing)}")
    if value.get("schema_version") != "tender-requirement-pack-v0.1":
        raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Requirement Pack版本不正确。")
    if set(value.get("project_facts", {})) - CANONICAL_FACT_KEYS:
        raise TenderError("UNDERSTAND_SCHEMA_INVALID", "project_facts出现未受控字段。")
    if set(value.get("service_scope", {})) - SERVICE_SCOPE_KEYS:
        raise TenderError("UNDERSTAND_SCHEMA_INVALID", "service_scope出现未受控字段。")
    for requirement in value.get("requirements", []):
        keys = {"requirement_id", "normalized_requirement", "requirement_type", "mandatory_level", "confidence", "classification", "conflict_group", "sources", "confirmation_status"}
        if not keys <= set(requirement):
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Requirement结构不完整。")
        if requirement["classification"] not in CLASSIFICATIONS or requirement["mandatory_level"] not in MANDATORY_LEVELS:
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Requirement枚举值无效。")
        if requirement["confirmation_status"] not in CONFIRMATION_STATUSES:
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Requirement确认状态无效。")
        if not isinstance(requirement["sources"], list) or not requirement["sources"]:
            raise TenderError("UNDERSTAND_SCHEMA_INVALID", "每条Requirement必须保留来源。")
        for source in requirement["sources"]:
            if not source.get("file_id") or not str(source.get("source_excerpt", "")).strip():
                raise TenderError("UNDERSTAND_SCHEMA_INVALID", "Source Trace缺失文件或原文。")
