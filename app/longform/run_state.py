"""MMF-007 Blocker 3（Round 2）：RUN_STATE_AUTHORITY —— 唯一终态派生。

Round 2 修复（Codex 复核）：
- FACTS 与 DERIVED 分离：各模块只存自己的事实（checkpoint 存 gaps、volume
  progress 存 artifact 路径、run_audit 存 QA/交付决策），终态（delivery_status /
  internal_phase / public_phase）只能由 derive_run_state(facts) 统一派生；
- 不变量 STATE-01~06 每条真实交叉读取 ≥2 个状态源（authority facts、checkpoint、
  generation_status history、report/run_audit、volume plan、磁盘 artifact），
  禁止自比较或硬编码 PASS；
- TERMINAL_BLOCKED 必须分类 ACTIVE / RESOLVED / STALE（resolved_at / resolved_by /
  superseded_by），不得残留裸字符串；
- Round 4 §44–§49：Volume 中间 DOCX 定为 **B（Checkpoint / Debug 辅助）**。
  Final = COMPLETED_CLEAN 当且仅当：Final Artifact 存在 + QA 合法 + checkpoint 无 gaps +
  无 failed units + B 语义四条内容证据齐（分卷 Section 齐 / CU 齐 / 装配输入齐 / 最终件谱系齐）
  + 无 provisional（深度待补）Section；分卷 DOCX 落了几份只作为恢复能力读数，不再决定终态。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .cu_state import SECTION_DRAFTABLE_STATES, SECTION_PASS_STATES, SECTION_PROVISIONAL, SECTION_DEPTH_INCOMPLETE, SECTION_TECHNICAL_FAILED
from .hierarchical import VOLUME_ARTIFACT_CONTRACT, atomic_write_json
from .provisional_settlement import settle_provisional_sections
from .scoring_authority import canonical_scoring_authority
from planning.scoring_taxonomy import COVERAGE_DENOMINATOR
from planning.unified_budget import classify_page_state

STATE_SCHEMA = "run-state-authority-v0.3-single-writer"
PASS_SECTIONS = SECTION_PASS_STATES
# PROVISIONAL / DEPTH_INCOMPLETE 是"正文已到达、深度待补"，既不算完成也不算失败；
# TECHNICAL_FAILED_SECTION 必须继续算失败，不得把技术失败藏进警告里。
DRAFTABLE_SECTIONS = SECTION_DRAFTABLE_STATES


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -------------------------------------------- Round 4.1 §24–§29 + Round 4.7：QA authority --
# 旧写法是 `final_qa in {"PASS", "NOT_APPLICABLE", None}` —— 把"**没有这份报告**"和
# "报告说通过"当成同一件事，于是缺最终 QA 也能派生成 COMPLETED_CLEAN（Codex 复现过）。
# 现在先把报告读成一个具名状态，判决只认这个状态；读不到、字段缺、JSON 坏、词表外的值
# 全都各自有名有姓，绝不默认成 PASS。
#
# Round 4.7 把"到底哪一份才是终态凭据"也写成一判据，而不是靠文件名猜：
#   `artifact_qa_report.json`  **渲染前 / 装配期** QA —— 对 assembled 内容量的，
#     生产里只在 `app_core` 装配那一步写一次，之后渲染、分页刷新、DOCX 修复都不重写它；
#   `final_artifact_qa.json`   **渲染后** QA —— 对**真实交付 DOCX** 逐字节量的
#     （它内部先跑同一套 `evaluate_artifact` 规则，再加标题 / 承诺 / 原生 TOC /
#     真实表格 / 请求账，见 `governance.final_artifact_qa`）。
# 对已经产出最终 Artifact、正要进终态的那场 Run，Clean 的 QA 凭据必须是后者；
# 前者继续在场，作为过程质量读数（看得见，但不覆盖最终 Artifact QA）。
PRE_RENDER_QA_FILE = "artifact_qa_report.json"
FINAL_ARTIFACT_QA_FILE = "final_artifact_qa.json"
WORD_ARTIFACT_SUFFIXES = {".docx"}
QA_GOVERNS_FINAL_ARTIFACT = "FINAL_ARTIFACT_QA"
QA_GOVERNS_PRE_RENDER = "PRE_RENDER_QA_NOT_SUPERSEDED"
QA_PASS = "QA_PASS"
QA_REVIEW = "QA_REVIEW_REQUIRED"
QA_BLOCK = "QA_BLOCK"
QA_NOT_APPLICABLE = "QA_NOT_APPLICABLE"
QA_NOT_MEASURED = "QA_NOT_MEASURED"
QA_UNKNOWN = "QA_UNKNOWN"
# 上游有三套 QA 词表：`governance.artifact_qa`（PASS / AUTO_REPAIR / BLOCK）、
# `governance.final_artifact_qa`（PASS / WARNING / NEEDS_REVIEW / BLOCK）与
# `delivery_state`（PASS / PASS_AFTER_REPAIR / WARNINGS_REMAIN / REVIEW_REQUIRED /
# CRITICAL_CONTENT_RISK）。三边都收进这一张表，**没有默认值**：表外的值一律 QA_UNKNOWN。
FINAL_QA_STATE_BY_STATUS = {
    "PASS": QA_PASS,
    "PASS_AFTER_REPAIR": QA_PASS,          # classify_fail_soft 只在 issues 已清空时才给这个值
    "NOT_APPLICABLE": QA_NOT_APPLICABLE,
    "AUTO_REPAIR": QA_REVIEW,
    "NEEDS_REVIEW": QA_REVIEW,
    "WARNINGS_REMAIN": QA_REVIEW,
    "REVIEW_REQUIRED": QA_REVIEW,
    "WARNING": QA_REVIEW,                  # final_artifact_qa 的"有警告、无 BLOCK"档
    "FAIL": QA_BLOCK,
    "BLOCK": QA_BLOCK,
    "CRITICAL_CONTENT_RISK": QA_BLOCK,
}
QA_CLEAN_ELIGIBLE_STATES = {QA_PASS, QA_NOT_APPLICABLE}
FINAL_QA_NOT_MEASURED = "FINAL_QA_NOT_MEASURED"
FINAL_QA_BLOCKED = "FINAL_QA_BLOCKED"
FINAL_QA_UNKNOWN_STATUS = "FINAL_QA_UNKNOWN_STATUS"
# Round 4.7：交付件已在场，但那份渲染后 QA 不是**对它**量的 —— 这是一条"没量"，
# 不是一个新的状态词（状态仍是 QA_NOT_MEASURED，缺陷名进 reason 通道，与
# `FINAL_QA_UNKNOWN_STATUS:<词>` 同一个既有模式）。
FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT = "FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT"

# ---- Round 4.1 Final Gate §4（Blocker A）：产品页带是 Clean 的必要条件 ----------------
# 过去这里只挡 `OVERSHOOT` 一个方向，于是 244 页（`UNDER_LENGTH`）与"根本没量过页数"
# 都能拿到 COMPLETED_CLEAN —— Codex 的 A1 反例走的就是这条路。三态一律进同一把尺：
# 只有**实测落在产品带内**才是 Clean 的必要条件，投影不是实测（§4.2 "不得把 Projection
# 当真实分页"）。挡下来的每一条都带自己的名字，不糊成同一个 NEEDS_REVIEW。
PAGE_BAND_UNDER_LENGTH = "PAGE_BAND_UNDER_LENGTH"
PAGE_BAND_OVERSHOOT = "PAGE_BAND_OVERSHOOT"
PAGE_BAND_NOT_MEASURED = "PAGE_BAND_NOT_MEASURED"
MEASURED_PAGE_BAND_BASES = {"MEASURED_FINAL_RENDER", "MEASURED_PAGE_CLASSIFICATION", "PPTX_SLIDE_XML"}
# 最终产物还没落位时，"上一轮写过 Clean"不是这一轮的凭据（§6.2 Blocker C）。
FINAL_ARTIFACT_NOT_LANDED = "FINAL_ARTIFACT_NOT_LANDED"


def _qa_report_reading(run_dir: Path, file_name: str) -> dict[str, Any]:
    """把一份 QA 报告读成具名状态：报告缺席 / 读不出 / 字段缺 / 词表外，各自有名。

    MISSING / UNREADABLE / FIELD_MISSING 三者分开设 reason：运维看到
    `FINAL_QA_NOT_MEASURED` 时要知道是"没跑"还是"跑了没写状态"，这两件事修法相反。
    """
    path = Path(run_dir) / file_name
    if not path.is_file():
        return {"status": None, "state": QA_NOT_MEASURED,
                "missing_reason": "FINAL_QA_REPORT_MISSING", "source": file_name}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"status": None, "state": QA_NOT_MEASURED,
                "missing_reason": "FINAL_QA_REPORT_UNREADABLE", "source": file_name}
    status = raw.get("status") if isinstance(raw, dict) else None
    if status is None:
        return {"status": None, "state": QA_NOT_MEASURED,
                "missing_reason": "FINAL_QA_STATUS_FIELD_MISSING", "source": file_name}
    state = FINAL_QA_STATE_BY_STATUS.get(str(status))
    if state is None:
        return {"status": status, "state": QA_UNKNOWN,
                "missing_reason": f"FINAL_QA_UNKNOWN_STATUS:{status}", "source": file_name,
                "raw": raw if isinstance(raw, dict) else {}}
    return {"status": status, "state": state, "missing_reason": "", "source": file_name,
            "raw": raw if isinstance(raw, dict) else {}}


def _landed_final_artifact(run_dir: Path, suffixes: set[str] | None = None) -> dict[str, Any] | None:
    """交付目录里最新那份真实产物（路径 / 字节 / sha256）。全场只在这一处算它。

    STATE-04d 的谱系读数与 Round 4.7 的 QA 绑定读的是同一个源、同一把 sha256 ——
    不允许出现"终态算一份哈希、QA 再算另一份"的第二套身份。
    `suffixes` 只是同一个源上的一个过滤（"最新那份 Word 产物"），不是第二条算哈希的路。

    平票必须能确定性打破：真实现场里有 `artifact/` 同时留着两份同名不同秒的 DOCX、
    整目录被复制过导致 mtime 全部相同的那一场（tender_20260909_183128_8e0a4d），
    只按 mtime 取最大会挑到**旧**那一份，把"QA 绑的是新交付件"读成"QA 过期"。
    交付件名里带 `%Y%m%d_%H%M%S`，所以 (mtime, 文件名) 这个键在时间轴上是单调的：
    mtime 更新者胜，同 mtime 时后一秒渲染的名字胜。
    """
    artifact_dir = Path(run_dir) / "artifact"
    wanted = suffixes or {".docx", ".pptx"}
    files = [p for p in artifact_dir.glob("*")
             if p.suffix in wanted and p.stat().st_size > 0] \
        if artifact_dir.is_dir() else []
    if not files:
        return None
    latest = max(files, key=lambda p: (p.stat().st_mtime, p.name))
    return {"path": str(latest), "name": latest.name, "bytes": latest.stat().st_size,
            "sha256": _sha256(latest)}


def canonical_final_qa_authority(run_dir: Path) -> dict[str, Any]:
    """唯一 QA authority：两份报告都读，但**谁做终态凭据**由"真实交付件在不在场"决定。

    规则只有一条（任务书 Round 4.7"修复原则"）：最终 Word Artifact 已落位的 Run，
    Clean 的 QA 凭据必须是对那份 Artifact 量的 `final_artifact_qa.json`；
    渲染前那份 `artifact_qa_report.json` 仍在读数里（过程质量），但不再覆盖它。

    绑定判据复用现场已有字段，不新建第二套 SHA 体系：报告自报的 `sha256` 与
    `artifact_path` 的文件名，必须对得上**当前落位**那份产物的现算 sha256 与文件名，
    且 `actual_docx_inspected` 为真。对不上（旧产物、被回滚、被换过、没真检）
    就是"没对当前交付件量过"—— 状态回到 `QA_NOT_MEASURED`，缺陷名写进 reason。

    交付件不是 Word（如纯 PPT）或还没落位时，没有"对最终 DOCX 的 QA"可消费，
    凭据仍是渲染前那一份；后者本身是否成立另说，产物没落位那条由
    `FINAL_ARTIFACT_NOT_LANDED` 单独挡。
    """
    run_dir = Path(run_dir)
    pre_render = _qa_report_reading(run_dir, PRE_RENDER_QA_FILE)
    final = _qa_report_reading(run_dir, FINAL_ARTIFACT_QA_FILE)
    landed = _landed_final_artifact(run_dir)
    # "最终 Word 产物已落位"用的是**同一个**落位读数的 Word 投影，不是第二条哈希路径：
    # 只要 artifact/ 里有 .docx，就必须有对它量的那份 QA 才能谈 Clean —— 与
    # `classify_terminal_block` / STATE-04d 里 `medium == "WORD"` 那条同源同物。
    landed_word = _landed_final_artifact(run_dir, WORD_ARTIFACT_SUFFIXES)
    brief = _read_json(run_dir / "brief.json") or {}
    assembly = _read_json(run_dir / "artifact_content.json") or {}
    ppt_profile = str(brief.get("medium")).upper() == "PPT" and (assembly.get("artifact") or {}).get("profile_p_contract") == "profile-p-service-v1"
    landed_ppt = _landed_final_artifact(run_dir, {".pptx"}) if ppt_profile else None
    required = bool(landed_word or landed_ppt)

    governing = final if required else pre_render
    identity = ""
    bound_target = landed_word or landed_ppt or landed
    binding: dict[str, Any] = {"required_by_landed_word_artifact": required,
                               "landed_artifact": bound_target["name"] if bound_target else None,
                               "landed_sha256": bound_target["sha256"] if bound_target else None}
    if landed_ppt and not landed_word:
        raw = final.get("raw", {})
        declared_sha = str(raw.get("artifact_sha256") or "")
        declared_name = Path(str(raw.get("artifact_path") or "")).name
        defects = []
        if declared_sha != bound_target["sha256"]: defects.append("SHA256_MISMATCH")
        if declared_name != bound_target["name"]: defects.append("ARTIFACT_NAME_MISMATCH")
        if raw.get("actual_pptx_inspected") is not True: defects.append("ACTUAL_PPTX_NOT_INSPECTED")
        binding.update(declared_sha256=declared_sha, declared_artifact_name=declared_name,
                       actual_pptx_inspected=raw.get("actual_pptx_inspected"),
                       bound_to_landed_artifact=not defects,binding_defects=defects)
        if defects:
            governing = {"status":final["status"],"state":QA_NOT_MEASURED,
                         "missing_reason":"FINAL_PPT_QA_NOT_BOUND:"+"+".join(defects),
                         "source":FINAL_ARTIFACT_QA_FILE}
        identity = f"{bound_target['name']}={bound_target['sha256'][:16]}"
    elif required:
        declared_sha = str(final.get("raw", {}).get("sha256") or "")
        declared_path = str(final.get("raw", {}).get("artifact_path") or "")
        declared_name = Path(declared_path).name if declared_path else ""
        inspected = final.get("raw", {}).get("actual_docx_inspected")
        mismatches = []
        if final["state"] in QA_CLEAN_ELIGIBLE_STATES:
            if not declared_sha:
                mismatches.append("SHA256_FIELD_ABSENT")
            elif declared_sha != bound_target["sha256"]:
                mismatches.append("SHA256_MISMATCH")
            if not declared_name:
                mismatches.append("ARTIFACT_PATH_FIELD_ABSENT")
            elif declared_name != bound_target["name"]:
                mismatches.append("ARTIFACT_NAME_MISMATCH")
            if inspected is not True:
                mismatches.append("ACTUAL_DOCX_NOT_INSPECTED")
        binding.update({
            "declared_sha256": declared_sha or None,
            "declared_artifact_name": declared_name or None,
            "actual_docx_inspected": inspected,
            "bound_to_landed_artifact": not mismatches,
            "binding_defects": mismatches})
        if mismatches:
            governing = {"status": final["status"], "state": QA_NOT_MEASURED,
                         "missing_reason": (FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT
                                            + ":" + "+".join(mismatches)),
                         "source": FINAL_ARTIFACT_QA_FILE}
        identity = f"{bound_target['name']}={bound_target['sha256'][:16]}"
    return {
        "final_qa": governing["status"],
        "final_qa_state": governing["state"],
        "final_qa_missing_reason": governing["missing_reason"],
        "final_qa_source": governing["source"],
        "final_qa_governs": QA_GOVERNS_FINAL_ARTIFACT if required else QA_GOVERNS_PRE_RENDER,
        "final_qa_artifact_identity": identity,
        "final_qa_binding": binding,
        "pre_render_qa_status": pre_render["status"],
        "pre_render_qa_state": pre_render["state"],
        "pre_render_qa_missing_reason": pre_render["missing_reason"],
        "final_artifact_qa_status": final["status"],
        "final_artifact_qa_state": final["state"],
        "final_artifact_qa_missing_reason": final["missing_reason"],
    }


def _page_band_facts(run_dir: Path) -> dict[str, Any]:
    """§21 双边门的交付端事实：页数一律取**实测**，两个实测源按新→旧优先。

    - `artifact_pagination.json`：最终交付件真实分页后逐页分类出来的技术有效页（最权威）；
    - `document_scale_report.json`：初稿逐页分类 + 当时的页带（没有最终分页时的退路）。

    两个都没有页数实测时返回 NOT_MEASURED：不得用字数投影冒充"页数在带内"，
    也不得因为量不到就把 OVERSHOOT 说成没有发生。
    """
    brief = _read_json(run_dir / "brief.json") or {}
    assembly = _read_json(run_dir / "artifact_content.json") or {}
    if str(brief.get("medium")).upper() == "PPT" and (assembly.get("artifact") or {}).get("profile_p_contract") == "profile-p-service-v1":
        report = _read_json(run_dir / "final_artifact_qa.json") or {}
        artifact = Path(str(report.get("artifact_path") or ""))
        landed = _landed_final_artifact(run_dir, {".pptx"})
        measured = None
        if landed and artifact.is_file() and artifact.resolve().is_relative_to(run_dir.resolve()):
            if artifact.resolve() == Path(landed["path"]).resolve() and landed["sha256"] == report.get("artifact_sha256"):
                import zipfile
                import xml.etree.ElementTree as ET
                try:
                    with zipfile.ZipFile(artifact) as z:
                        measured = len(ET.fromstring(z.read("ppt/presentation.xml")).findall("{http://schemas.openxmlformats.org/presentationml/2006/main}sldIdLst/*"))
                except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
                    measured = None
        minimum = int(((assembly.get("artifact") or {}).get("output_contract") or {}).get("min_total_pages") or 15)
        state = "TARGET_RANGE" if measured is not None and measured >= minimum else "UNDER_LENGTH" if measured is not None else "NOT_MEASURED"
        return {"page_band": state, "page_band_pages": measured, "page_band_source": "FINAL_PPTX_BYTES",
                "page_band_basis": "PPTX_SLIDE_XML" if measured is not None else "NOT_MEASURED",
                "page_band_targets": [minimum], "page_band_reported": state,
                "pagination_technical_pages": 0, "scale_report_technical_pages": 0}
    scale_report = _read_json(run_dir / "document_scale_report.json") or {}
    targets = [int(value or 0) for value in (scale_report.get("page_targets") or [])]
    page_report = scale_report.get("page_report") or {}
    scale_pages = int(page_report.get("technical_effective_pages") or 0)
    pagination = _read_json(run_dir / "artifact_pagination.json") or {}
    pagination_pages = int(pagination.get("technical_effective_pages") or 0)
    pages, source = ((pagination_pages, "FINAL_ARTIFACT_PAGINATION") if pagination_pages
                     else (scale_pages, "FIRST_DRAFT_PAGE_CLASSIFICATION") if scale_pages
                     else (0, "NOT_MEASURED"))
    # 投影与实测分得开：初稿那份 page_report 在没有逐页分类时是按字/页投出来的，
    # 把投影写成"实测页在带内"就是本轮反复踩过的"量错口径当判据"。
    basis = ("MEASURED_FINAL_RENDER" if source == "FINAL_ARTIFACT_PAGINATION"
             else ("MEASURED_PAGE_CLASSIFICATION" if page_report.get("measured")
                   else "PROJECTED_FROM_CHARS") if source == "FIRST_DRAFT_PAGE_CLASSIFICATION"
             else "NOT_MEASURED")
    governing = (classify_page_state(pages, targets) if pages and max(targets or [0]) > 0
                 else "NOT_MEASURED")
    return {
        "page_band": governing,
        "page_band_pages": pages,
        "page_band_source": source,
        "page_band_basis": basis,
        "page_band_targets": targets,
        "page_band_reported": str(scale_report.get("page_band") or "NOT_MEASURED"),
        "pagination_technical_pages": pagination_pages,
        "scale_report_technical_pages": scale_pages,
    }


def _scoring_gate_facts(run_dir: Path) -> dict[str, Any]:
    """§35/§36 覆盖率端的交付事实：只认 canonical 唯一实质主题口径的评分覆盖率。

    三态分开，不能混：
    - NOT_APPLICABLE：这场招标根本没有技术评分实质主题（或没生成评分矩阵）——
      没有东西要覆盖，覆盖率不成立，但也不许写成"覆盖率高所以干净"；
    - BELOW_GATE：有主题、有实测覆盖率但 `< min` → 不得 COMPLETED_CLEAN；
    - NOT_MEASURED：有主题却没跑覆盖率回填（`scoring_gate` 缺失或 measured=False），
      或者那份覆盖率不是按唯一实质主题口径量的 —— 尺子不对就等于没量，
      同样不得 COMPLETED_CLEAN。
    """
    matrix = _read_json(run_dir / "09_scoring_matrix.json") or {}
    gate = matrix.get("scoring_gate") or {}
    summary = matrix.get("coverage_summary") or {}
    records = matrix.get("canonical_scoring_records") or []
    coverage = gate.get("technical_scoring_coverage")
    denominator = str(gate.get("coverage_denominator")
                      or summary.get("coverage_denominator") or "NOT_MEASURED")
    if not matrix or (not records and not gate):
        state = "NOT_APPLICABLE"
    elif gate.get("measured") and isinstance(coverage, (int, float)) \
            and denominator == COVERAGE_DENOMINATOR:
        state = "COVERAGE_OK" if gate.get("completed_clean_allowed") else "BELOW_GATE"
    else:
        # 有主题、却没有按唯一实质主题口径量出来的覆盖率 —— 尺子不对就等于没量：
        # 按行口径的 0.34 与主题口径的 0.87 是两个世界，不能拿旧尺子的读数放行。
        state = "NOT_MEASURED"
    binding_state = "LEGACY_NOT_BOUND"
    report_path = run_dir / "final_scoring_report.json"
    if report_path.is_file() or matrix.get("final_artifact_scoring_binding"):
        from planning.scoring_final_evidence import final_scoring_binding_matches, final_scoring_matrix_matches
        reading = _read_json(report_path) or {}
        landed = _landed_final_artifact(run_dir, {".docx"})
        if reading.get("status") == "NOT_APPLICABLE" and not matrix.get("rows") and not records:
            binding_state = "NOT_APPLICABLE"
        elif (landed and (run_dir / "09_scoring_matrix.json").is_file()
              and reading.get("status") == "VERIFIED"
              and reading.get("may_claim_final_scoring") is True
              and reading.get("artifact_sha256") == landed["sha256"]
              and final_scoring_matrix_matches(reading, run_dir / "09_scoring_matrix.json")
              and final_scoring_binding_matches(matrix, Path(landed["path"]), artifact_sha256=landed["sha256"])):
            binding_state = "BOUND"
        else:
            binding_state = "FAILED_OR_STALE"
            state, coverage = "NOT_MEASURED", None
    return {
        "scoring_gate_state": state,
        "scoring_artifact_binding_state": binding_state,
        "scoring_technical_coverage": coverage if isinstance(coverage, (int, float)) else None,
        "scoring_legacy_row_coverage": gate.get("legacy_row_coverage"),
        "scoring_coverage_denominator": denominator,
        "scoring_canonical_total": int(summary.get("canonical_scoring_total") or len(records)),
        "scoring_min_coverage": gate.get("min_technical_scoring_coverage"),
    }


def _scoring_authority_facts(run_dir: Path) -> dict[str, Any]:
    """Round 4.3 §9–§11：终态落盘与生成入口消费**同一个** canonical Scoring Authority。

    Codex 的 C10 反例抓到的正是这里缺的一根轴：页带有 STATE-08、最终 QA 有 STATE-07，
    评分却只看"覆盖率量到多少"—— 覆盖率是**写完之后**的账，而"这份评分映射现在还算不算数"
    是 canonical 输入与当前计划清单的账。本函数不复制任何 Topic→Section/CU 算法（§11），
    只是调用 `scoring_authority.canonical_scoring_authority`：它每次现算、不缓存，
    派生门文件自报的 PASS 在它面前不是凭据。
    """
    reading = canonical_scoring_authority(run_dir)
    blockers = [str(name) for name in (reading.get("clean_blockers") or [])]
    if not blockers and not reading.get("may_enter_generation"):
        blockers = [f"SCORING_AUTHORITY_{reading.get('status')}"]
    invalid = reading.get("invalid_reference") or {}
    return {
        "scoring_authority_status": str(reading.get("status") or "UNKNOWN"),
        "scoring_authority_may_enter_generation": bool(reading.get("may_enter_generation")),
        "scoring_authority_canonical_topic_total": reading.get("canonical_topic_total"),
        "scoring_authority_topic_basis": str(reading.get("canonical_topic_basis") or ""),
        "scoring_authority_matrix_state": str(reading.get("canonical_matrix_state") or "UNKNOWN"),
        "scoring_authority_reason": str(reading.get("reason") or ""),
        "scoring_authority_clean_blockers": blockers,
        "scoring_authority_invalid_reference": {
            "sections": list(invalid.get("sections") or []),
            "content_units": list(invalid.get("content_units") or [])},
        "scoring_authority_identity": str(reading.get("scoring_authority_identity") or ""),
    }


def _volume_contract_facts(run_dir: Path) -> dict[str, Any]:
    """§44–§49 B 语义的交付证据：分卷内容齐 / CU 齐 / 装配输入齐 / 最终件谱系齐。

    这里刻意**不**看 `volumes/Vxx.docx` 是否存在：分卷 DOCX 按本轮决策只是 Resume 锚点
    （见 `hierarchical.VOLUME_ARTIFACT_CONTRACT_RECORD`），拿它当交付判据就是
    "代码按 B 跑、状态按 A 判"，Run D 实测正是因此把一次完整交付记成 STATE-04 FAIL。
    每条事实都交叉两个源：Volume Plan（应有清单）vs 磁盘 fragment / CU 审计 / 初稿装配记录。
    """
    plan = _read_json(run_dir / "11_volume_generation_plan.json") or {}
    volumes = [v for v in (plan.get("volumes") or []) if str(v.get("volume_id") or "")]
    expected_sections = {sid for v in volumes for sid in (v.get("section_ids") or [])}
    expected_units = {uid for v in volumes for uid in (v.get("content_unit_ids") or [])}

    fragment_sections = {p.parent.name for p in
                         (run_dir / "longform" / "word" / "sections").glob("*/fragment.json")}
    cu_audit = _read_json(run_dir / "content_unit_generation_audit.json") or {}
    persisted_units = {str(u) for u in (cu_audit.get("persisted_content_unit_ids") or [])}
    # 装配输入的权威是引擎自己落的第一稿清单（`persist_first_draft_artifact`），
    # 根目录 `first_draft.json` 只是 app_core 对它的投影。只读投影会把"没走 app_core"的
    # 链路报成 27 条装配缺口 —— 那是测量工具读错了源，不是交付缺了内容。
    engine_draft = _read_json(run_dir / "longform" / "word" / "first_draft_artifact.json") or {}
    first_draft = _read_json(run_dir / "first_draft.json") or {}
    section_states: dict[str, Any] = {}
    assembly_input_source = "NO_ASSEMBLY_INPUT_MANIFEST"
    for candidate, name in ((engine_draft, "ENGINE_FIRST_DRAFT_MANIFEST"),
                            (first_draft, "APP_CORE_FIRST_DRAFT_PROJECTION")):
        if candidate.get("section_states"):
            section_states = candidate["section_states"]
            assembly_input_source = name
            break
    drafted = {sid for sid, status in section_states.items() if str(status) in DRAFTABLE_SECTIONS}
    has_assembly_manifest = assembly_input_source != "NO_ASSEMBLY_INPUT_MANIFEST"

    section_gaps = sorted(expected_sections - fragment_sections)
    unit_gaps = sorted(expected_units - persisted_units)
    # 清单没在场就是"读不到装配输入"，不是"每条 Section 都缺"：把 27 条编成缺口
    # 会让报告指着一个不存在的问题去找根因。
    assembly_gaps = sorted(expected_sections - drafted) if has_assembly_manifest else []
    final_content = run_dir / "artifact_content.json"
    if not cu_audit:
        cu_audit_state = "AUDIT_ABSENT"
    elif cu_audit.get("persisted_content_unit_ids") is None:
        cu_audit_state = "AUDIT_WITHOUT_ID_LIST"
    else:
        cu_audit_state = "ID_LIST_PRESENT"
    return {
        "volume_artifact_contract": VOLUME_ARTIFACT_CONTRACT,
        "volume_plan_state": "VOLUME_PLAN_PRESENT" if volumes else "NO_VOLUME_PLAN",
        # 没有任何 Word 侧证据 = 这不是长文交付（例如纯 PPT），Word 侧证据不适用；
        # 但只要有一点证据在场，四项就必须逐项齐 —— 不适用与没做到是两回事。
        "word_side_evidence_present": bool(volumes or fragment_sections or drafted),
        "volume_plan_total": len(volumes),
        "volume_sections_expected": len(expected_sections),
        "volume_sections_with_fragment": len(expected_sections & fragment_sections),
        "volume_sections_complete": bool(expected_sections) and not section_gaps,
        "volume_section_gaps": section_gaps,
        "volume_content_units_expected": len(expected_units),
        "volume_content_units_persisted": len(expected_units & persisted_units),
        "volume_content_units_complete": bool(expected_units) and not unit_gaps
        and cu_audit_state == "ID_LIST_PRESENT",
        "volume_content_unit_gaps": unit_gaps,
        "volume_content_unit_audit_state": cu_audit_state,
        # 守恒：分卷是 Section 的分组，不应重复也不应漏（与 §8 预算守恒同构的交叉核对）。
        "volume_grouping_conservation_holds": sum(len(v.get("section_ids") or []) for v in volumes)
        == len(expected_sections),
        "assembly_inputs_complete": has_assembly_manifest and bool(expected_sections) and not assembly_gaps,
        "assembly_input_state": ("SECTION_STATES_PRESENT" if has_assembly_manifest else "NO_ASSEMBLY_INPUT_MANIFEST"),
        "assembly_input_source": assembly_input_source,
        "assembly_input_gaps": assembly_gaps,
        "assembly_input_section_total": len(drafted),
        "first_draft_status": str(first_draft.get("status") or "NOT_PRESENT"),
        "final_artifact_content_present": bool(final_content.is_file() and final_content.stat().st_size > 0),
    }


# ---------------------------------------------------------------- FACTS 采集
def collect_facts(run_dir: Path, *, medium: str | None = None, draft_delivery: str | None = None) -> dict[str, Any]:
    """从各模块事实记录采集 FACTS。每个来源只声明自己知道的事实。"""
    run_dir = Path(run_dir)

    volume_plan = _read_json(run_dir / "11_volume_generation_plan.json") or {}
    expected_volumes = [str(v.get("volume_id") or "") for v in volume_plan.get("volumes") or []]

    volumes_dir = run_dir / "volumes"
    artifact_paths: dict[str, str] = {}
    for vid in expected_volumes:
        docx = volumes_dir / f"{vid}.docx"
        if docx.is_file() and docx.stat().st_size > 0:
            artifact_paths[vid] = str(docx)

    completed_sections: list[str] = []
    draft_sections: list[str] = []
    failed_units: list[str] = []
    provisional_records: list[dict[str, Any]] = []
    for status_path in (run_dir / "longform" / "word" / "sections").glob("*/status.json"):
        sid = status_path.parent.name
        status = (_read_json(status_path) or {}).get("status")
        if status in PASS_SECTIONS:
            completed_sections.append(sid)
        elif status in DRAFTABLE_SECTIONS:
            draft_sections.append(sid)
            if status in {SECTION_PROVISIONAL, SECTION_DEPTH_INCOMPLETE}:
                # 原因结算要读的是节内那份门禁记录：`provisional` 块与 must_cover /
                # length_status 同源同盘，不在这里另算一套深度。
                gate = _read_json(status_path.parent / "governance.json") or {}
                provisional_records.append({
                    "section_id": sid, "status": status, "gate": gate,
                    "provisional": gate.get("provisional") or {}})
        elif status in {"FAILED_SECTION", "FAILED", SECTION_TECHNICAL_FAILED}:
            failed_units.append(sid)
    # provisional 不再只是"列表非空"：每一节带着**为什么 provisional** 进终态判定。
    # 深度缺口是真的缺一块正文，还是长度规则/跨文件举证造成的规则效应，两类的效力不同。
    provisional_settlement = settle_provisional_sections(
        provisional_records,
        (_read_json(run_dir / "09_scoring_matrix.json") or {}).get("canonical_scoring_records") or [])

    checkpoint = _read_json(run_dir / "checkpoint" / "state.json") or {}
    checkpoint_gaps = bool(
        checkpoint.get("failed_batches")
        or checkpoint.get("pending_batches")
        or checkpoint.get("failed_units")
        or checkpoint.get("retry_queue")
    )

    final_artifact = _landed_final_artifact(run_dir)

    qa_facts = canonical_final_qa_authority(run_dir)

    consistency_report = _read_json(run_dir / "consistency_issues.json") or {}
    history = _read_json(run_dir / "generation_status.json") or {}
    report_audit = _read_json(run_dir / "run_audit.json") or {}

    reconcile_failed = _read_json(run_dir / "run_state_reconcile_failed.json")
    technical_failure = _read_json(run_dir / "technical_failure.json")
    return {
        "medium": medium,
        "reconcile_failed": bool(reconcile_failed and reconcile_failed.get("status") == "RUN_STATE_RECONCILE_FAILED"),
        "expected_volumes": expected_volumes,
        "completed_volumes": sorted(artifact_paths.keys()),
        "artifact_paths": artifact_paths,
        "completed_sections": sorted(completed_sections),
        "provisional_sections": sorted(draft_sections),
        # 同一份 provisional 名单，两份读数并列：`provisional_sections` 是**这轮有哪些节
        # 停在 provisional**（历史口径，一个字节没改），`provisional_blocking_sections`
        # 是其中"确实缺一块正文"的那部分（终态门只消费这一份）。
        "provisional_blocking_sections": sorted(provisional_settlement["blocking_section_ids"]),
        "provisional_settlement": provisional_settlement,
        "failed_units": sorted(failed_units),
        "checkpoint_gaps": checkpoint_gaps,
        "final_artifact_exists": bool(final_artifact),
        "final_artifact": final_artifact,
        **qa_facts,
        "technical_failure": bool(technical_failure) or (bool(failed_units) and not (completed_sections or draft_sections)),
        "technical_failure_detail": technical_failure,
        "consistency_issue_count": len(consistency_report.get("issues") or []),
        "consistency_status": consistency_report.get("status"),
        "draft_delivery": draft_delivery,
        "history_delivery": (history.get("result") or {}).get("delivery_status") or history.get("delivery_status"),
        "audit_delivery": report_audit.get("delivery_status"),
        "checkpoint_ref": {k: checkpoint.get(k) for k in ("schema_version", "status") if checkpoint.get(k)},
        **_page_band_facts(run_dir),
        **_scoring_gate_facts(run_dir),
        **_scoring_authority_facts(run_dir),
        **_volume_contract_facts(run_dir),
        "reduction_executed": bool((_read_json(run_dir / "overshoot_reduction_execution.json")
                                    or {}).get("actions_executed")),
    }


# ---------------------------------------------------------------- DERIVED 派生
def derive_run_state(facts: dict[str, Any]) -> dict[str, Any]:
    """唯一终态派生函数。其它模块不得自行写 COMPLETED_CLEAN / TERMINAL_BLOCKED 终态。"""
    final_exists = bool(facts.get("final_artifact_exists"))
    final_qa = facts.get("final_qa")
    # §24–§26：判决只认具名状态。`None` / 词表外的值 / 报告缺失一律**不是**通过，
    # 也不默认成通过 —— 过去 `final_qa in {"PASS", "NOT_APPLICABLE", None}` 就是在这里
    # 把"没有最终 QA"读成"最终 QA 通过"。
    final_qa_state = str(facts.get("final_qa_state") or QA_NOT_MEASURED)
    final_qa_reason = str(facts.get("final_qa_missing_reason") or "")
    qa_blocked = False
    gaps = bool(facts.get("checkpoint_gaps"))
    failed = facts.get("failed_units") or []
    consistency_status = str(facts.get("consistency_status") or "")
    consistency_issues = int(facts.get("consistency_issue_count") or 0)
    expected = set(facts.get("expected_volumes") or [])
    completed = set(facts.get("completed_volumes") or [])
    # §44–§49：交付判据换成 B 语义的四条内容证据。`volumes/Vxx.docx` 落了几份只是
    # Resume 锚点的磁盘事实（`volume_checkpoint_docx_done`），报告里照旧看得见，但不再决定终态 ——
    # 让"代码按 B 跑、状态按 A 判"的分裂回到一处（Run D：交付件完整却只渲染 2/7 分卷）。
    volume_checkpoint_docx_done = (expected <= completed) if expected else False
    word_side = bool(facts.get("word_side_evidence_present"))
    volume_inputs_complete = (
        bool(facts.get("volume_sections_complete"))
        and bool(facts.get("volume_content_units_complete"))
        and bool(facts.get("assembly_inputs_complete"))
        and bool(facts.get("volume_grouping_conservation_holds"))
        and bool(facts.get("final_artifact_content_present"))
        if word_side else True)
    # 正文已到达、深度待补（PROVISIONAL / DEPTH_INCOMPLETE）过去只是**顺带**被"这一卷没渲染
    # 成 DOCX"挡住；选 B 之后那层意外保护就没了，必须在这里显式写出来 —— 否则"定 B"等于偷偷放宽。
    provisional = sorted(str(s) for s in (facts.get("provisional_sections") or []))
    # 挡 Clean 的那一份是**带原因的子集**：正文确实缺一块的节继续挡（PROVISIONAL_SECTIONS
    # 这道门一个字没删），只因长度规则或"举证不在技术正文"而停在 provisional 的节
    # 改由各自该承重的那道门具名（超编 → 页带 / OVERSHOOT；整件产物 QA → FINAL_QA_BLOCKED）。
    provisional_blocking = sorted(str(s) for s in
                                  (facts.get("provisional_blocking_sections") or []))
    draft = facts.get("draft_delivery")
    page_band = str(facts.get("page_band") or "NOT_MEASURED")
    page_band_basis = str(facts.get("page_band_basis") or "NOT_MEASURED")
    scoring_state = str(facts.get("scoring_gate_state") or "NOT_APPLICABLE")
    # Round 4.3 §10：canonical Scoring Authority 是 Clean 的第 4 条必要条件。
    # 读的是入口那一份同一个结果（§11）：`may_enter_generation` 为假 ⇒ 既不能进生成，
    # 也不能在落盘时自称干净完成 —— 不允许再出现"入口 BLOCK、finalizer PASS"。
    # 这里刻意用 `is not True`：没采集过这道事实（None）同样不给 Clean，
    # 磁盘交叉核对（STATE-09）随后会独立再算一次，两处都放行才叫真放行。
    scoring_authority_may_enter = facts.get("scoring_authority_may_enter_generation")
    scoring_authority_status = str(facts.get("scoring_authority_status")
                                   or "SCORING_AUTHORITY_NOT_COLLECTED")
    scoring_authority_blocks_clean = scoring_authority_may_enter is not True
    # §4（Blocker A）：产品页带三态里只有"实测落在带内"允许进 Clean。挡下来的原因具名，
    # 且**不新增第二套终态判据** —— 读的还是 `_page_band_facts` 那一个源、
    # `classify_page_state` 那一把尺。
    page_band_blocks_clean = (page_band != "TARGET_RANGE"
                             or page_band_basis not in MEASURED_PAGE_BAND_BASES)
    page_band_reason = {"UNDER_LENGTH": PAGE_BAND_UNDER_LENGTH,
                        "OVERSHOOT": PAGE_BAND_OVERSHOOT}.get(page_band) or PAGE_BAND_NOT_MEASURED

    if facts.get("reconcile_failed"):
        # Round 3：State Authority reconcile 失败 → 不得 COMPLETED_CLEAN
        internal_phase = "COMPLETED_WITH_REVIEW"
        delivery = "NEEDS_REVIEW"
    elif facts.get("technical_failure"):
        internal_phase = "INCOMPLETE"
        delivery = "TECHNICAL_FAILED"
    elif not final_exists:
        # §6.2（Blocker C）：最终产物还没落位时**绝不**继承 draft 的 COMPLETED_CLEAN。
        # 旧写法是 `delivery = draft`，而 `run_layout` 把上一份 authority 的
        # `delivery_status` 原样当 draft 传回来 —— 于是"上一轮写过 Clean"本身
        # 就成了这一轮 Clean 的凭据，绕过了最终 QA 与页带。Clean 只能由落盘后的
        # 可验证输入重新派生（finalize 在产物落位后再算一次，正常交付不受影响）。
        internal_phase = "INCOMPLETE"
        delivery = (draft if draft in {"NEEDS_REVIEW", "TECHNICAL_FAILED"}
                    else "TECHNICAL_FAILED")
    elif consistency_status == "CONSISTENCY_CHECK_FAILED" or consistency_issues > 0:
        internal_phase = "COMPLETED_WITH_REVIEW"
        delivery = "NEEDS_REVIEW"
    elif gaps or failed:
        internal_phase = "COMPLETED_WITH_GAPS"
        delivery = "NEEDS_REVIEW"
    elif final_qa_state == QA_BLOCK:
        # §28：最终 QA 判 BLOCK 是一篇**有内容、但没过内容门**的交付。它既不能进 Clean，
        # 也不能和"待复核""没量过"混成同一个状态：文件仍然交付（AGENTS.md 的
        # "局部失败不得隐藏完整文件"），但 public 面从此不是 READY。
        internal_phase = "COMPLETED_WITH_BLOCKERS"
        delivery = "NEEDS_REVIEW"
        qa_blocked = True   # public_phase 走 BLOCKED，见下面 §27 单一真值那一段
    elif final_qa_state == QA_REVIEW:
        internal_phase = "COMPLETED_WITH_REVIEW"
        delivery = "NEEDS_REVIEW"
    elif final_qa_state in {QA_NOT_MEASURED, QA_UNKNOWN}:
        # §25/§26：没量过 / 量出了词表外的值 —— 具名状态，绝不默认成通过。
        internal_phase = "COMPLETED_UNVERIFIED"
        delivery = "NEEDS_REVIEW"
    elif final_qa_state in {QA_PASS, QA_NOT_APPLICABLE} and volume_inputs_complete:
        # §4/§21：产品页带是双向硬门 —— 超上限、低于下沿、以及"这台机器没量出页数/只有投影"
        # 三种情形都不可能"干净完成"。Round 3 把 UNDER_LENGTH 交给 `DOCUMENT_SCALE_BELOW_TARGET`
        # 分级、不在终态挡，代价就是 244 页也能自称 Clean；本轮把它收回同一条判据：
        # **量不到不等于在带内**，与评分门那条 `SCORING_GATE_NOT_MEASURED` 同一个口径。
        if page_band_blocks_clean:
            internal_phase = "COMPLETED_WITH_REVIEW"
            delivery = "NEEDS_REVIEW"
        elif scoring_state in {"BELOW_GATE", "NOT_MEASURED"}:
            # §35/§36：技术评分覆盖率是唯一实质主题口径上的硬门。低于下限当然不行；
            # "有评分主题却从没回填过覆盖率"也不行 —— 量不到不等于覆盖得好。
            # 这里绝不会反过来去补商务内容撑覆盖率：那条路在评分门里已经写死了
            # commercial_content_for_coverage_forbidden。
            internal_phase = "COMPLETED_WITH_REVIEW"
            delivery = "NEEDS_REVIEW"
        elif scoring_authority_blocks_clean:
            # Round 4.3 §9/§10：评分映射/门结论现在站不住 ⇒ 覆盖率再高也不是干净完成。
            # 判据来自入口那一份 Authority（`_scoring_authority_facts`），这里只是消费，
            # 不重写任何映射算法（§11）。
            internal_phase = "COMPLETED_WITH_REVIEW"
            delivery = "NEEDS_REVIEW"
        elif provisional_blocking:
            # "正文已到、深度待补"不是干净完成：这一档必须交人判，且不靠"某卷没渲染成
            # DOCX"这种顺带效果来挡（§44–§49 定 B 之后那层保护就不存在了）。
            # 本轮改的是**判什么**：具名原因里只有 REAL_CONTENT_DEFICIENCY 有这个效力，
            # 规则效应与"举证本来不在技术正文"两类继续登记、继续报出，但不冒充内容缺口。
            internal_phase = "COMPLETED_WITH_REVIEW"
            delivery = "NEEDS_REVIEW"
        else:
            internal_phase = "COMPLETED"
            delivery = "COMPLETED_CLEAN"
    else:
        internal_phase = "COMPLETED_WITH_REVIEW"
        delivery = "NEEDS_REVIEW"

    # 具名阻塞项：哪一道门在挡这次交付，必须写在派生结果里，而不是让人对着
    # 一个 "NEEDS_REVIEW" 反推。顺序 = 判据从上到下的顺序。
    final_qa_blockers: list[str] = []
    if final_qa_state == QA_BLOCK:
        final_qa_blockers.append(FINAL_QA_BLOCKED)
    elif final_qa_state in {QA_NOT_MEASURED, QA_UNKNOWN}:
        final_qa_blockers.append(FINAL_QA_NOT_MEASURED if final_qa_state == QA_NOT_MEASURED
                                 else FINAL_QA_UNKNOWN_STATUS)
    if final_qa_reason.startswith(FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT):
        # 交付件在场、那份 QA 却不是对它量的（旧产物 / 被回滚 / 被换过 / 没真检）——
        # 具名出场，否则六面上只看得见一个说不清原因的"没量过"。
        final_qa_blockers.append(FINAL_QA_NOT_BOUND_TO_LANDED_ARTIFACT)
    if page_band_blocks_clean and final_exists:
        final_qa_blockers.append(page_band_reason)
    if not final_exists:
        final_qa_blockers.append(FINAL_ARTIFACT_NOT_LANDED)
    if scoring_state in {"BELOW_GATE", "NOT_MEASURED"} and final_exists:
        final_qa_blockers.append(f"SCORING_GATE_{scoring_state}")
    if scoring_authority_blocks_clean and final_exists:
        # 具名原因由 Authority 那一侧给出（§10 的 MISSING/UNREADABLE/MALFORMED/INCOMPLETE/
        # STALE/CONTRADICTORY/INVALID_REFERENCE/FALSE_NOT_APPLICABLE/UNKNOWN/BLOCK 逐档点名），
        # 落盘端不自己翻译一遍，免得两处词表分叉。
        authority_blockers = [str(name) for name in
                              (facts.get("scoring_authority_clean_blockers") or [])]
        final_qa_blockers.extend(authority_blockers or [f"SCORING_AUTHORITY_{scoring_authority_status}"])
    if provisional_blocking and final_exists:
        # 门名保持 `PROVISIONAL_SECTIONS` 不变（历史读数与既有反例都钉着它），
        # 后面追加具名原因：读的人不必再反推"是哪一节、因为什么在挡"。
        final_qa_blockers.append("PROVISIONAL_SECTIONS(" + ",".join(provisional_blocking) + ")")
    if facts.get("reconcile_failed"):
        final_qa_blockers.append("RUN_STATE_RECONCILE_FAILED")
    if gaps or failed:
        final_qa_blockers.append("CHECKPOINT_GAPS_OR_FAILED_UNITS")
    if final_exists and not volume_inputs_complete and bool(facts.get("word_side_evidence_present")):
        final_qa_blockers.append("VOLUME_INPUTS_INCOMPLETE")
    if delivery != "COMPLETED_CLEAN" and not final_qa_blockers:
        # 不允许出现"挡住了却说不清被谁挡"：兜底也要具名。
        final_qa_blockers.append("DELIVERY_NOT_CLEAN")

    if draft == "TECHNICAL_FAILED" and delivery != "TECHNICAL_FAILED" and not final_exists:
        delivery = "TECHNICAL_FAILED"
        internal_phase = "INCOMPLETE"

    public_phase = "READY" if delivery in {"COMPLETED_CLEAN", "NEEDS_REVIEW"} else "BLOCKED"
    return {
        "internal_phase": internal_phase,
        "delivery_status": delivery,
        # §27 单一真值：public 面仍然按"文件能不能下载"说话（AGENTS.md：局部失败不得隐藏
        # 完整文件），但**内容门是否被挡住**必须在这同一个对象里具名出场，
        # 否则一个 READY 会把"最终 QA 判 BLOCK"和"最终 QA 通过"读成同一件事。
        "public_phase": public_phase,
        "content_gate_blocked": bool(qa_blocked
                                     or final_qa_state in {QA_NOT_MEASURED, QA_UNKNOWN}
                                     or (page_band_blocks_clean and final_exists)
                                     or (scoring_authority_blocks_clean and final_exists)),
        "final_qa_state": final_qa_state,
        "final_qa_status": final_qa,
        "final_qa_missing_reason": final_qa_reason,
        # Round 4.7：写清这次 QA 结论是**按哪一份报告**下的、绑的是哪一份交付件 ——
        # 没有这两格，"QA_PASS"读起来仍然分不清是渲染前过了还是真实交付件过了。
        "final_qa_source": str(facts.get("final_qa_source") or ""),
        "final_qa_governs": str(facts.get("final_qa_governs") or ""),
        "final_qa_artifact_identity": str(facts.get("final_qa_artifact_identity") or ""),
        "final_qa_binding_defects": list((facts.get("final_qa_binding") or {}).get(
            "binding_defects") or []),
        "pre_render_qa_state": str(facts.get("pre_render_qa_state") or QA_NOT_MEASURED),
        "final_artifact_qa_state": str(facts.get("final_artifact_qa_state") or QA_NOT_MEASURED),
        "final_qa_blockers": final_qa_blockers,
        # 分卷 DOCX 是 Checkpoint（B），所以这里两份口径并列：交付判据是内容证据，
        # 磁盘渲染份数只作恢复能力读数。
        "volume_artifact_contract": str(facts.get("volume_artifact_contract") or "NOT_RECORDED"),
        "volume_inputs_complete": volume_inputs_complete,
        "volume_checkpoint_docx_done": volume_checkpoint_docx_done,
        "volume_checkpoint_docx_total": len(sorted(completed)),
        "volume_checkpoint_docx_expected_total": len(sorted(expected)),
        "provisional_section_total": len(provisional),
        # 两份读数并列，不互相替换：总数回答"有多少节停在 provisional"，
        # 具名原因子集回答"其中哪些是缺一块正文、因此有挡 Clean 的效力"。
        "provisional_blocking_section_total": len(provisional_blocking),
        "provisional_blocking_sections": provisional_blocking,
        "provisional_reason_code_counts": dict(
            ((facts.get("provisional_settlement") or {}).get("reason_code_counts") or {})),
        # 页带随终态一起出场：判定是谁量的、量了几页，否则"COMPLETED_CLEAN"读起来
        # 像是量过页数，而实际上可能是这台机器渲染不出分页（NOT_MEASURED）。
        "page_band": page_band,
        "page_band_source": str(facts.get("page_band_source") or "NOT_MEASURED"),
        "page_band_basis": str(facts.get("page_band_basis") or "NOT_MEASURED"),
        "page_band_pages": int(facts.get("page_band_pages") or 0),
        # 覆盖率门与页带门并列出场：写了谁在挡、按哪个分母量的、量到多少，
        # 否则"NEEDS_REVIEW"读起来像是页数问题，而实际上是评分覆盖没到线。
        "scoring_gate_state": scoring_state,
        "scoring_artifact_binding_state": facts.get("scoring_artifact_binding_state", "LEGACY_NOT_BOUND"),
        "scoring_technical_coverage": facts.get("scoring_technical_coverage"),
        "scoring_coverage_denominator": str(facts.get("scoring_coverage_denominator") or "NOT_MEASURED"),
        "scoring_canonical_total": int(facts.get("scoring_canonical_total") or 0),
        # Round 4.3 §10/§12：canonical Scoring Authority 与页带、最终 QA 并列出场。
        # `scoring_authority_identity` 记的是"这次结论是按哪一份 09/08 算出来的"，
        # STATE-09 拿它与盘上现状交叉：换了矩阵、换了计划树，旧结论就不再是凭据。
        "scoring_authority_status": scoring_authority_status,
        "scoring_authority_may_enter_generation": bool(scoring_authority_may_enter),
        "scoring_authority_canonical_topic_total": facts.get(
            "scoring_authority_canonical_topic_total"),
        "scoring_authority_topic_basis": str(facts.get("scoring_authority_topic_basis") or ""),
        "scoring_authority_matrix_state": str(facts.get("scoring_authority_matrix_state") or ""),
        "scoring_authority_identity": str(facts.get("scoring_authority_identity") or ""),
    }


# ------------------------------------------------- TERMINAL_BLOCKED 分类
def classify_terminal_block(ppt_status: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    if str(ppt_status.get("public_phase") or "") != "TERMINAL_BLOCKED":
        return {"classification": "NOT_PRESENT"}
    if facts.get("final_artifact_exists") and facts.get("medium") == "WORD":
        return {
            "classification": "STALE",
            "resolved_at": _now(),
            "resolved_by": "RUN_STATE_AUTHORITY",
            "superseded_by": f"final_artifact:{(facts.get('final_artifact') or {}).get('path')}",
        }
    return {"classification": "ACTIVE"}


# ---------------------------------------------------------------- 不变量
def evaluate_invariants(state: dict[str, Any], run_dir: Path) -> dict[str, str]:
    """每条不变量交叉读取 ≥2 个真实状态源。"""
    run_dir = Path(run_dir)
    facts = state["facts"]
    derived = state["derived"]

    # STATE-01：authority(派生) vs longform/ppt/status.json(投影源)
    ppt_status = _read_json(run_dir / "longform" / "ppt" / "status.json") or {}
    active_terminal = ppt_status.get("public_phase") == "TERMINAL_BLOCKED"
    final_done = bool(facts.get("final_artifact_exists"))
    s01 = "PASS" if not (final_done and active_terminal) else "FAIL"

    # STATE-02：磁盘 volume docx(源1) vs authority.completed_volumes(源2)
    volumes_dir = run_dir / "volumes"
    disk_valid = sorted(p.stem for p in volumes_dir.glob("*.docx") if p.stat().st_size > 0) if volumes_dir.is_dir() else []
    auth_vols = sorted(facts.get("completed_volumes") or [])
    s02 = "PASS" if disk_valid == auth_vols else "FAIL"

    # STATE-03：Volume Plan(源1).generation_status vs 磁盘 artifact(源2)
    plan = _read_json(run_dir / "11_volume_generation_plan.json") or {}
    disk_vids = set(disk_valid)
    s03 = "PASS"
    for volume in plan.get("volumes") or []:
        vid = str(volume.get("volume_id") or "")
        if vid in disk_vids and volume.get("generation_status") == "PLANNED":
            s03 = "FAIL"

    # STATE-04（§44–§49 定 B 后的新语义）：Final Artifact 在场 ⇒ 四条**内容**证据齐。
    # 旧写法是"expected ⊆ 磁盘 volumes/*.docx"，那是把 Resume 锚点当交付判据：Run D
    # 完整交付（27 Section / 304 CU 全部落盘）只因只渲染了 2/7 份分卷 DOCX 而记 FAIL。
    # 现在每条仍交叉两个源，且不再有"删掉这条就绿了"的可能 —— 它比原来查得更多。
    word_side = bool(facts.get("word_side_evidence_present"))
    s04a = s04b = s04c = s04d = "NOT_APPLICABLE"
    if final_done and word_side:
        s04a = "PASS" if facts.get("volume_sections_complete") else "FAIL"
        s04b = "PASS" if facts.get("volume_content_units_complete") else "FAIL"
        s04c = "PASS" if facts.get("assembly_inputs_complete") else "FAIL"
        lineage = facts.get("final_artifact") or {}
        s04d = "PASS" if (lineage.get("sha256")
                          and facts.get("final_artifact_content_present")
                          and int(lineage.get("bytes") or 0) > 0) else "FAIL"

    # STATE-05：authority.delivery(源1) == history(源2) == report run_audit(源3)
    non_null = {
        str(s) for s in (
            derived.get("delivery_status"),
            facts.get("history_delivery"),
            facts.get("audit_delivery"),
        ) if s not in {None, ""}
    }
    s05 = "PASS" if len(non_null) <= 1 else "FAIL"

    # STATE-06：checkpoint.completed_sections(源1) ⊆ authority.completed∪provisional(源2)
    checkpoint = _read_json(run_dir / "checkpoint" / "state.json") or {}
    ck_sections = set(checkpoint.get("completed_sections") or [])
    auth_sections = set(facts.get("completed_sections") or []) | set(facts.get("provisional_sections") or [])
    s06 = "PASS" if ck_sections <= auth_sections else "FAIL"

    # STATE-07（Round 4.1 §24–§27 + Round 4.7）：任何一面写着 COMPLETED_CLEAN，另一面就不许写着
    # "没量过"、也不许那份量的是**另一份产物**。这条不读派生结果自己跟自己比 ——
    # 它重新走一遍同一个 canonical QA authority（交付件已在场时读的就是
    # `final_artifact_qa.json` 那一份，并且再核一次它对不对得上当前落位的字节），
    # 与 authority 的 delivery 交叉核对；报告缺席、字段缺失、词表外的值、绑旧产物都算 FAIL。
    # 这正是 Codex / Grok 先后复现出来的那一类：终态读着一份与交付件无关的"通过"。
    disk_qa = canonical_final_qa_authority(run_dir)
    clean_claimed = str(derived.get("delivery_status") or "") == "COMPLETED_CLEAN"
    s07 = "PASS"
    if clean_claimed and disk_qa["final_qa_state"] not in QA_CLEAN_ELIGIBLE_STATES:
        s07 = "FAIL"
    if clean_claimed and str(derived.get("final_qa_governs") or "") != str(
            disk_qa.get("final_qa_governs") or ""):
        # 派生时"该读哪一份"的答案与现在盘上的答案不一致 —— 那是同一条 authority
        # 在两次运行里说了两套话（例如交付件在派生之后被换掉），不能自称干净完成。
        s07 = "FAIL"
    if clean_claimed and str(derived.get("final_qa_artifact_identity") or "") != str(
            disk_qa.get("final_qa_artifact_identity") or ""):
        # 绑的是哪份交付件的哈希 —— 不一致就是"QA 过后文件又变了"（回滚、重渲染、换名）。
        s07 = "FAIL"
    if clean_claimed and not derived.get("volume_inputs_complete"):
        s07 = "FAIL"
    if clean_claimed and int(derived.get("provisional_blocking_section_total") or 0) > 0:
        # 自称干净却仍有"缺一块正文"的 provisional 节 → FAIL。规则效应那类不再触发这条，
        # 但两层的**总数**仍原样报在 `provisional_section_total` 里，没有从读数中删除。
        s07 = "FAIL"
    # 反向也查：盘上那份**做数的** QA 明明写着 BLOCK，authority 却说干净 —— 那是同一个
    # 洞的另一半。渲染前报告说什么不再决定这条（Round 4.7 Q3：它对的是装配期的内容，
    # 真实交付件的问题由渲染后那份重新量一遍；把已修好的旧结论永久当成否决票，
    # 等于拿一份永不重写的历史读数冒充"对当前产物的判断"）。
    if clean_claimed and str(disk_qa.get("final_qa") or "") == "BLOCK":
        s07 = "FAIL"

    # STATE-08（Final Gate §4，Blocker A）：与 STATE-07 同一条思路，但换一根轴 ——
    # 任何一面写着 COMPLETED_CLEAN，就得在**盘上的页数实测**里对得上：
    # 重新读一次分页/画像文件与页带目标，带外或量不到都不许自称干净完成。
    # 这条刻意不读 derived 里的 page_band（那是同一次派生的产物），只跟磁盘交叉。
    s08 = "PASS"
    if clean_claimed:
        disk_band = _page_band_facts(run_dir)
        if (str(disk_band.get("page_band") or "") != "TARGET_RANGE"
                or str(disk_band.get("page_band_basis") or "") not in MEASURED_PAGE_BAND_BASES):
            s08 = "FAIL"

    # STATE-09（Round 4.3 §9/§10/§12）：写着 COMPLETED_CLEAN，就得在**盘上现在的** canonical
    # 评分输入前站得住。这条刻意不读 derived 里那份读数（那是同一次派生的产物，自己跟自已比
    # 等于没查），而是与 STATE-07/08 同样重新读一次磁盘再算一遍：
    #   ① 入口 BLOCK 的那些形状（幽灵落点、矩阵缺失、假 N/A…）在这里同样 FAIL；
    #   ② 派生之后 09/08 被换过（identity 不一致）也 FAIL —— 这就是 §12 要的
    #      "Clean 针对当前 Artifact / Current Plan"，用的是盘上 SHA，不新建 provenance 系统。
    s09 = "PASS"
    if clean_claimed:
        disk_authority = canonical_scoring_authority(run_dir)
        if not disk_authority.get("may_enter_generation"):
            s09 = "FAIL"
        elif str(disk_authority.get("scoring_authority_identity") or "") != str(
                derived.get("scoring_authority_identity") or ""):
            s09 = "FAIL"

    return {
        "STATE-01_final_vs_active_terminal": s01,
        "STATE-02_disk_volumes_vs_authority": s02,
        "STATE-03_plan_status_vs_disk": s03,
        "STATE-04a_final_implies_volume_sections_complete": s04a,
        "STATE-04b_final_implies_content_units_complete": s04b,
        "STATE-04c_final_implies_assembly_inputs_complete": s04c,
        "STATE-04d_final_artifact_lineage_complete": s04d,
        "STATE-05_delivery_across_authority_history_audit": s05,
        "STATE-06_checkpoint_sections_subset_authority": s06,
        "STATE-07_clean_implies_final_qa_measured_and_passed": s07,
        "STATE-08_clean_implies_measured_pages_in_product_band": s08,
        "STATE-09_clean_implies_canonical_scoring_authority_valid": s09,
    }


def _update_json(path: Path, updater) -> None:
    current = _read_json(path)
    if not isinstance(current, dict):
        return
    updated = updater(current)
    atomic_write_json(path, updated)


def guard_clean_claim(state: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Final Gate §6.3（Blocker C）：Clean 必须先过守卫，才允许被投影与落盘。

    旧行为是"先把 COMPLETED_CLEAN 投到六面、再算不变式把 STATE-07 报个红"——
    红只进报告，产品状态不回改，于是 `COMPLETED_CLEAN + STATE-07 FAIL` 能同时落盘。
    这里把顺序反过来：**投影之前**先复核，守卫不成立就地降级并留下具名记录
    （`CLEAN_GATE_INVARIANT_FAILED`），落盘前再复核一次，因此不存在"写出去再报警"。

    这不是第二套终态判据：派生仍然只有 `derive_run_state` 一处，本函数只做一件事 ——
    拒绝把 Clean 交给投影/落盘，理由是磁盘交叉核对（STATE-07/08/09）不认。
    """
    invariants = evaluate_invariants(state, run_dir)
    state["invariants"] = invariants
    failing = sorted(key for key, value in invariants.items()
                     if key.startswith(("STATE-07", "STATE-08", "STATE-09")) and value == "FAIL")
    derived = state.get("derived") or {}
    if failing and str(derived.get("delivery_status") or "") == "COMPLETED_CLEAN":
        # 挡住 Clean 的那道门必须**跟着降级一起出场**，否则六面黑板上只剩一个
        # 说不清原因的 NEEDS_REVIEW。名字仍由 Authority 那一侧提供（§11）。
        blockers = ["CLEAN_GATE_INVARIANT_FAILED"]
        if any(key.startswith("STATE-09") for key in failing):
            authority = canonical_scoring_authority(run_dir)
            blockers.extend(str(name) for name in (authority.get("clean_blockers") or [])
                            if str(name).startswith("SCORING_AUTHORITY_"))
        state["derived"] = {
            **derived,
            "delivery_status": "NEEDS_REVIEW",
            "internal_phase": "COMPLETED_WITH_REVIEW",
            "content_gate_blocked": True,
            "final_qa_blockers": sorted({*(derived.get("final_qa_blockers") or []), *blockers}),
        }
        state["clean_gate_downgrade"] = {
            "failing_invariants": failing,
            "rule": "COMPLETED_CLEAN 必须与 STATE-07（最终 QA 量过且通过）、"
                    "STATE-08（实测页数落在产品带内）与 STATE-09（canonical Scoring "
                    "Authority 对盘上当前的 09/08 仍然成立）同时成立",
            "guarded_at": _now(),
        }
    return state


def project_authority_state(run_dir: Path, state: dict[str, Any]) -> None:
    """Project the derived terminal state to every display/audit surface.

    These files are projections only; none is allowed to derive a terminal
    delivery value independently.
    """
    run_dir = Path(run_dir)
    derived = state["derived"]
    delivery = derived["delivery_status"]
    public_phase = derived["public_phase"]
    internal_phase = derived["internal_phase"]
    projection = {
        "delivery_status": delivery,
        "public_phase": public_phase,
        "internal_phase": internal_phase,
        # §27：单一真值不只是"别互相矛盾"，还要求每一面都能说出**为什么**是这个状态。
        # 只写 delivery_status 的投影会让 run_audit 与 checkpoint 变成"看得见结果、
        # 看不见判据"的两块黑板，所以把最终 QA 的具名状态与阻塞项一起投过去。
        "final_qa_state": derived.get("final_qa_state"),
        "final_qa_missing_reason": derived.get("final_qa_missing_reason") or "",
        "final_qa_blockers": list(derived.get("final_qa_blockers") or []),
        "content_gate_blocked": bool(derived.get("content_gate_blocked")),
        "run_state_authority": STATE_SCHEMA,
        "run_state_authority_updated_at": state["generated_at"],
    }

    for audit_path in (run_dir / "run_audit.json", run_dir / "report" / "run_audit.json"):
        _update_json(audit_path, lambda row: {**row, **projection})

    checkpoint_path = run_dir / "checkpoint" / "state.json"
    _update_json(checkpoint_path, lambda row: {**row, **projection})

    # 进度面与计划面也带完整投影：`authority_*` 两个键名是它们原有的读法，保留兼容，
    # 但"为什么是这个交付状态"（具名最终 QA + 阻塞项）必须在这两面上同样看得见，
    # 否则 §27 的单一真值就漏了这两块黑板。
    progress_path = run_dir / "volume_progress.json"
    _update_json(progress_path, lambda row: {
        **row, **projection,
        "authority_delivery_status": delivery, "authority_internal_phase": internal_phase})

    plan_path = run_dir / "11_volume_generation_plan.json"
    _update_json(plan_path, lambda row: {
        **row, **projection,
        "authority_delivery_status": delivery, "authority_internal_phase": internal_phase})

    status_path = run_dir / "generation_status.json"
    def update_status(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row.get("result") or {})
        result.update(projection)
        return {**row, **projection, "result": result}
    _update_json(status_path, update_status)


def record_reconcile_failure(run_dir: Path, exc: BaseException) -> dict[str, Any]:
    failure = {
        "status": "RUN_STATE_RECONCILE_FAILED",
        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        "delivery_downgrade": "NEEDS_REVIEW",
        "recorded_at": _now(),
    }
    atomic_write_json(Path(run_dir) / "run_state_reconcile_failed.json", failure)
    return failure


def finalize_delivery_state(run_dir: Path, *, medium: str | None = None, draft_delivery: str | None = None) -> dict[str, Any]:
    """Non-throwing formal finalization boundary.

    A reconcile failure becomes an authoritative fact and is immediately
    projected as NEEDS_REVIEW.  Later QA cannot erase this fact.
    """
    run_dir = Path(run_dir)
    try:
        return reconcile_run_state(run_dir, medium=medium, draft_delivery=draft_delivery)
    except Exception as exc:
        record_reconcile_failure(run_dir, exc)
        facts = collect_facts(run_dir, medium=medium, draft_delivery=draft_delivery)
        derived = derive_run_state(facts)
        state = {
            "schema_version": STATE_SCHEMA,
            "run_dir": str(run_dir),
            "generated_at": _now(),
            "medium": medium,
            "facts": facts,
            "derived": derived,
            "reconciled_fixes": [],
            "terminal_block": None,
            "authority_error": facts.get("reconcile_failed"),
        }
        guard_clean_claim(state, run_dir)
        project_authority_state(run_dir, state)
        state["facts"] = collect_facts(run_dir, medium=medium, draft_delivery=draft_delivery)
        state["derived"] = derive_run_state(state["facts"])
        guard_clean_claim(state, run_dir)
        atomic_write_json(run_dir / "run_state.json", state)
        return state


# ---------------------------------------------------------------- 权威入口
def reconcile_run_state(run_dir: Path, *, medium: str | None = None, draft_delivery: str | None = None) -> dict[str, Any]:
    """权威对账入口：采集 FACTS → 同步投影 → 派生终态 → 不变量 → 落盘 run_state.json。"""
    run_dir = Path(run_dir)
    fixes: list[str] = []
    facts = collect_facts(run_dir, medium=medium, draft_delivery=draft_delivery)

    # --- 投影同步（改文件后重采事实）---
    plan_path = run_dir / "11_volume_generation_plan.json"
    plan = _read_json(plan_path)
    if isinstance(plan, dict):
        changed = False
        for volume in plan.get("volumes") or []:
            vid = str(volume.get("volume_id") or "")
            docx = run_dir / "volumes" / f"{vid}.docx"
            if docx.is_file() and docx.stat().st_size > 0 and volume.get("generation_status") in {"PLANNED", "GENERATING", None}:
                prior = volume.get("generation_status")
                volume["generation_status"] = "COMPLETED"
                volume["artifact_path"] = str(docx)
                changed = True
                fixes.append(f"{vid}: {prior}→COMPLETED（artifact 已落盘，STATE-03 同步）")
        if changed:
            atomic_write_json(plan_path, plan)

    progress_path = run_dir / "volume_progress.json"
    progress = _read_json(progress_path)
    if isinstance(progress, dict) and progress.get("last_valid_volume_artifact") and not Path(progress["last_valid_volume_artifact"]).is_file():
        progress["last_valid_volume_artifact"] = None
        fixes.append("volume_progress: 清除失效 last_valid 引用")
        atomic_write_json(progress_path, progress)

    ppt_status_path = run_dir / "longform" / "ppt" / "status.json"
    ppt_status = _read_json(ppt_status_path) or {}
    block_class = classify_terminal_block(ppt_status, facts)
    if block_class["classification"] == "STALE":
        ppt_status["public_phase"] = "STALE_TERMINAL_BLOCK_RESOLVED"
        ppt_status["terminal_block"] = block_class
        atomic_write_json(ppt_status_path, ppt_status)
        fixes.append(f"PPT TERMINAL_BLOCKED→{block_class['classification']}（superseded_by final artifact）")

    facts = collect_facts(run_dir, medium=medium, draft_delivery=draft_delivery)
    derived = derive_run_state(facts)
    state = {
        "schema_version": STATE_SCHEMA,
        "run_dir": str(run_dir),
        "generated_at": _now(),
        "medium": medium,
        "facts": facts,
        "derived": derived,
        "reconciled_fixes": fixes,
        "terminal_block": block_class if block_class.get("classification") != "NOT_PRESENT" else None,
    }
    guard_clean_claim(state, run_dir)          # 投影之前：Clean 必须先过守卫（§6.3）
    project_authority_state(run_dir, state)
    facts = collect_facts(run_dir, medium=medium, draft_delivery=draft_delivery)
    state["facts"] = facts
    state["derived"] = derive_run_state(facts)
    guard_clean_claim(state, run_dir)          # 落盘之前：同一把尺再来一次
    atomic_write_json(run_dir / "run_state.json", state)
    return state
