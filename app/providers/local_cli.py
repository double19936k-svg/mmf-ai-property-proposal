from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import AIProvider, ProviderError, ProviderUnavailableError, normalize_generation, normalize_recommendation, parse_json_object, write_json


class LocalCLIProvider(AIProvider):
    provider_version = "0.1"

    def _template(self) -> list[str]:
        value = self.config.get("command_template")
        return [str(item) for item in value] if isinstance(value, list) else []

    def health_check(self) -> dict[str, Any]:
        template = self._template()
        configured = bool(self.config.get("enabled", False) and template)
        command = shutil.which(template[0]) if template else None
        available = configured and bool(command)
        return {
            "configured": configured,
            "available": available,
            "status": "available" if available else ("not_configured" if not configured else "unavailable"),
            "message": "可用" if available else ("未配置命令模板" if not configured else "命令不可用"),
            "metadata": self.get_metadata(),
        }

    def _invoke(self, request: dict[str, Any], task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.health_check()["available"]:
            raise ProviderUnavailableError("Local CLI Provider当前不可用。")
        task_dir.mkdir(parents=True, exist_ok=False)
        prompt_path = task_dir / "prompt.txt"
        output_path = task_dir / "provider_output.json"
        prompt_path.write_text(request["system_prompt"] + "\n\n" + request["prompt"], encoding="utf-8")
        replacements = {"prompt_file": str(prompt_path), "output_file": str(output_path), "task_dir": str(task_dir), "task_id": request["task_id"]}
        cmd = [part.format(**replacements) for part in self._template()]
        completed = subprocess.run(
            cmd, cwd=str(task_dir), env=os.environ.copy(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=int(self.config.get("timeout", 300)), shell=False,
        )
        (task_dir / "stdout.log").write_text(completed.stdout or "", encoding="utf-8")
        (task_dir / "stderr.log").write_text(completed.stderr or "", encoding="utf-8")
        if completed.returncode != 0:
            raise ProviderUnavailableError("Local CLI Provider调用失败：" + (completed.stderr or completed.stdout or "")[-800:])
        raw = output_path.read_text(encoding="utf-8-sig") if output_path.exists() else completed.stdout
        try:
            outer = json.loads(raw)
            candidate = outer.get("text", outer) if isinstance(outer, dict) else outer
        except json.JSONDecodeError:
            outer = {"text": raw}
            candidate = raw
        result, repair = parse_json_object(candidate, set(request.get("required_keys", [])))
        metadata = self._task_metadata(request["task_id"], serialization_repair=repair or {"applied": False})
        write_json(task_dir / "provider_raw_envelope.json", outer)
        write_json(task_dir / "provider_structured_output.json", result)
        write_json(task_dir / "provider_audit.json", metadata)
        return result, metadata

    def recommend_knowledge(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return normalize_recommendation(result, metadata)

    def generate_solution(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return normalize_generation(result, metadata)

    def invoke_structured(self, request: dict[str, Any], task_dir: Path) -> dict[str, Any]:
        result, metadata = self._invoke(request, task_dir)
        return {**result, "provider_metadata": metadata}
