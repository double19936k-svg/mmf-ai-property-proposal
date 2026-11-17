"""MMF-007 规模分级验证（Synthetic Provider，ENGINEERING_PIPELINE_VERIFIED 口径）。

LEVEL 1: 70页级 / LEVEL 2: 150页级 / LEVEL 3: 120～150 页带里硬排 200 页（§13 负例）/
LEVEL 4: 300页级。
口径（Round 4.1 §7 之后只剩这一把尺子）：页数 → 文档额度与页数投影都用**同一条直量尺**
`LEDGER_CHARS_PER_TECHNICAL_PAGE`（Run D 467,292÷395=1,183.0，合成 282,807÷239=1,183.3
交叉验证），产物页数用**渲染可见字**尺 `RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE`。
旧的 `1 页 = 900 字` 是渲染口径却被拿去定计划额度；Round 4 又用"计划结构占比 0.35"
倒推出 1,062.2 当尺子 —— 两条都不是直量，后者比直量小 10.2%，正是 300 类合成
投影 273 页、实测 244 页的那 29 页。两路收益模型降级为门控里的 `two_route_diagnostic`。
全部本地执行，禁止真实 Provider（conftest 守卫）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import unittest

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).parent))

import test_mmf006e_r21_q01_q04 as r21  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from longform.factory import visible_text  # noqa: E402
from longform.orchestrator import generate_longform  # noqa: E402
from planning.planner import PlanningError  # noqa: E402
from planning.budget_control import BUDGET_LEDGER_FILE  # noqa: E402
from planning.unified_budget import (  # noqa: E402
    BUDGET_GATE_FILE,
    LEDGER_CHARS_PER_TECHNICAL_PAGE,
    MIDRUN_BUDGET_FILE,
    RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE,
)

APP_ROOT = APP


def _scale_evidence_dir() -> Path:
    """规模分级证据的落点：源码树写进项目记录树，便携包内跑测试不得在 dist 旁边造树。

    旧实现写死 `parents[2]/MMF_Project_Records/MMF-007/codex_final_closure_evidence`：
    一是治理同步后该目录已迁到 `03_Review_and_QA/` 下（旧路径每次跑测试就被复活一次），
    二是便携包内 `parents[2]` 落在 `dist/`，于是每轮 dist E2E 都在包旁边长出
    `dist/MMF_Project_Records/`（Round 1 收口时按"只移动不删除"清理过一份）。

    `MMF_SCALE_EVIDENCE_DIR` 给便携包一侧显式落点：被测包的 package_sha256 覆盖包内每一个
    文件，测试产物写进包里就会让"被验收的包"和"被跑的包"不再是同一份字节。
    """
    override = os.environ.get("MMF_SCALE_EVIDENCE_DIR")
    if override:
        return Path(override)
    records = (
        Path(__file__).resolve().parents[2] / "MMF_Project_Records" / "MMF-007"
        / "03_Review_and_QA" / "codex_final_closure_evidence"
    )
    if records.parent.is_dir():
        return records
    return Path(__file__).resolve().parents[1] / "runtime" / "codex_final_closure_evidence"


# 每个 LEVEL 用哪一档产品规模：页带由 `document_scale` 自己推导（§4），测试不再另定一套
# "多少字算一页"。LEVEL-3 是唯一没有对应页带的历史档位 —— 200 页在 120～150 页带里
# 属于超计划，正好用来当 §13 的负例（计划总和超文档上限必须先重新分配，不得进生成）。
_LEVEL_SCALE = {
    70: "regular_solution",
    150: "detailed_technical_solution",
    200: "detailed_technical_solution",
    300: "large_technical_tender",
}

# Round 4.1 §4：**工程回归地板**与**产品验收带**是两件事，必须分开具名。
#   产品验收带 = 该档正式交付必须落在的页数区间（硬门，来自 `document_scale` 自己那一档）；
#   工程地板   = 只防"断崖式回退"（装配丢节、链级静默丢失那一类），比硬门下沿低一截。
# 300 类过去用 `_assert_level(ev, 200)`：200 比硬门下沿 250 低 50 页，于是 244 页
# （实测低于产品门）照样测成通过 —— 那是把地板当门用。现在每一级都必须同时过两条线。
PRODUCT_ACCEPTANCE_BAND = {
    70: (50, 70),
    150: (120, 150),
    300: (250, 320),
}
ENGINEERING_REGRESSION_FLOOR = {
    70: 45,
    150: 100,
    300: 250,     # 300 类的地板直接抬到硬门下沿：低于 250 页不回退，只可能是缺陷
}
# §8 的推荐中心：给真实 LibreOffice/WPS 分页误差留余量的**期望值**，不是硬门。
# 差额按具名字段报出来（`recommended_center_hit`），不写进断言，也不许被读成"已过中心"。
SYNTHETIC_RECOMMENDED_CENTER = {
    70: (55, 66),
    150: (128, 142),
    300: (270, 300),
}
# 投影尺的低估包络 —— 按档实测，不是拍的：
#   R41 定尺（930 账本字/技术页）后的实测  投影→实测  低估
#     70 档  55 → 67 页  +21.8%   （2 次一致）
#    150 档 125 → 146 页  +16.8%   （2 次一致）
#    300 档 264 → 286 页  +8.3%    （3 次一致）
# 为什么分档：每节都有一份不随字数缩小的固定版面成本（节题、间距、表题/表下说明），
# 文档越薄这份占比越大，所以同一把"字→页"直量尺在薄文档上系统性偏低。要收掉它，
# 需要**排产时**就知道节数（`planner._scale_profile` 是一张静态表，看不到节数），
# 那是一项独立的改造，已登记为 Run E 条目，不在本轮里顺手加参数。
# 这里先钉住两件事：方向不得反过来（高估 = 削减会被过早放行），以及低估不得越过
# 实测包络 —— 包络放宽就等于承认尺子又漂了。
PROJECTION_UNDER_PREDICTION_ENVELOPE = {
    70: 0.25,
    150: 0.20,
    300: 0.12,
}
# 高估只允许在测量噪声内（页等效是整数除法，1 页 ≈ 0.4%）
PROJECTION_OVER_PREDICTION_TOLERANCE = 0.02


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def _write_scale_evidence(evidence: dict) -> Path:
    evidence_dir = _scale_evidence_dir()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"scale_{evidence['page_equivalent_target']}p_evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def scale_evidence_path(page_equivalent: int) -> Path:
    return _scale_evidence_dir() / f"scale_{page_equivalent}p_evidence.json"


def _gate_view(gate: dict) -> dict:
    view = {key: gate.get(key) for key in (
        "status", "may_enter_generation", "blocked_reasons",
        "projected_technical_effective_pages", "worst_case_technical_effective_pages",
        "page_floor", "page_ceiling", "planned_total_chars", "planned_structure_share",
        "provider_calls_made")}
    view["reallocation_sections"] = len(gate.get("reallocation_demand") or [])
    return view


def _blocked_scale_evidence(label: str, page_equivalent: int, scale: str, run: Path,
                            factor_state: dict) -> dict:
    """§13 挡下的一级：证据要能说明"为什么挡、挡在 Provider 调用之前"。"""
    gate = _read_json(run / BUDGET_GATE_FILE)
    return {
        "level": label,
        "page_equivalent_target": page_equivalent,
        "document_scale": scale,
        "planned_factor": factor_state.get("factor"),
        "outcome": "BLOCKED_BEFORE_GENERATION",
        "budget_gate": _gate_view(gate),
        "provider_calls": 0,
        "generation_started": (run / "content_unit_generation_audit.json").is_file(),
        "midrun_tracking_written": (run / MIDRUN_BUDGET_FILE).is_file(),
        "docx_rendered": False,
        "verification_class": "SYNTHETIC_MOCK_PROVIDER",
    }


def _scale_run(page_equivalent: int, tmp: Path, label: str, *, plan_pages: int = 0) -> dict:
    """跑一个按页数缩放的完整长文生成，返回证据 dict。

    默认**不动预算**：`document_scale` 自己按页带推导计划额度（§4），这一档要多少字
    由产品算，不由测试乘。只有显式给 `plan_pages` 时才改写章节内容预算 —— 用来造
    "计划总量超过自己页带上限"的 §13 负例（LEVEL-3）。

    旧的 `factor = 页等效 × 900 / 41000 × 1.4` 是把渲染口径（900 字/页）拿去定计划额度
    （账本口径 1,062 字/页），于是每一级都超计划 1.2～1.7 倍；§13 门控一生效，
    这条路就在第一次 Provider 调用之前被挡住，四个 LEVEL 全灭。
    """
    scale = _LEVEL_SCALE[page_equivalent]
    import planning.planner as planner_mod

    patched = None
    if plan_pages:
        original_budget = planner_mod.build_content_budget
        factor_state: dict[str, float] = {}

        def scaled_budget(outline, matrix, *args, **kwargs):
            budget = original_budget(outline, matrix, *args, **kwargs)
            totals = budget.get("totals") or {}
            base = int(totals.get("target_words_target") or 0)
            factor = (max(1.0, plan_pages * LEDGER_CHARS_PER_TECHNICAL_PAGE / base)
                      if base else 1.0)
            factor_state["factor"] = factor
            for row in budget.get("chapters", []):
                row["target_words_min"] = int(row["target_words_min"] * factor)
                row["target_words_target"] = int(row["target_words_target"] * factor)
                row["target_words_max"] = int(row["target_words_max"] * factor)
            for field in ("target_words_min", "target_words_target", "target_words_max"):
                if field in totals:
                    totals[field] = sum(int(row[field]) for row in budget.get("chapters", []))
            for words_field, chars_field in (
                ("target_words_min", "target_effective_chars_min"),
                ("target_words_target", "target_effective_chars_target"),
                ("target_words_max", "target_effective_chars_max"),
            ):
                if chars_field in totals:
                    totals[chars_field] = totals.get(words_field)
            budget["scale_note"] = (f"{label}: x{factor:.2f} "
                                    f"(计划 {plan_pages} 页 × {LEDGER_CHARS_PER_TECHNICAL_PAGE} 账本字)")
            return budget

        planner_mod.build_content_budget = scaled_budget
        patched = (planner_mod, original_budget, factor_state)
    try:
        pack = r21._complex_pack()
        brief = {**r21._brief(), "project_type": "产业园综合体", "scenario": "完整物业服务方案",
                 "document_scale": scale}
        run = r21._write_run(tmp, pack, brief, r21._selection())
        provider = MockProvider({"provider_name": "mock", "provider_type": "mock", "enabled": True, "model": "mock-scale"})
        started = time.monotonic()
        result = generate_longform(
            run_dir=run,
            provider=provider,
            provider_name="mock",
            brief=brief,
            selection=r21._selection(),
            selected_ids=["KU-0001"],
            task_mode="full_longform",
            require_section_min=True,
            speed_profile="fast",
        )
        elapsed = round(time.monotonic() - started, 1)
    except PlanningError:
        # §13 负例：计划超上限必须在生成前被挡下。把"挡住了、且零 Provider 调用"写成
        # 证据，而不是让这一级从证据目录里消失。
        if patched:
            _write_scale_evidence(_blocked_scale_evidence(label, page_equivalent, scale, run,
                                                          patched[2]))
        raise
    finally:
        if patched:
            patched[0].build_content_budget = patched[1]

    artifact = result["generated"]["artifact"]
    text = visible_text(artifact)
    effective_chars = len(text)
    sections_total = len(artifact.get("sections") or [])
    checkpoint = json.loads((run / "checkpoint" / "state.json").read_text(encoding="utf-8"))
    hierarchy = json.loads((run / "08_document_hierarchy.json").read_text(encoding="utf-8-sig")) if (run / "08_document_hierarchy.json").is_file() else {}
    volume_plan = json.loads((run / "11_volume_generation_plan.json").read_text(encoding="utf-8-sig")) if (run / "11_volume_generation_plan.json").is_file() else {}
    plan_cus = sum(len(v.get("content_unit_ids") or []) for v in volume_plan.get("volumes") or [])
    cu_audit = json.loads((run / "content_unit_generation_audit.json").read_text(encoding="utf-8-sig"))
    batches_file = run / "generation_batches.json"
    batch_count = len(json.loads(batches_file.read_text(encoding="utf-8-sig")).get("batches", [])) if batches_file.is_file() else (result.get("word") or {}).get("generation_batch_count")

    # 真实 DOCX（生产装配器）
    content_path = run / "scale_content.json"
    content_path.write_text(json.dumps({"brief": brief, "artifact": artifact}, ensure_ascii=False), encoding="utf-8")
    docx_path = run / f"scale_{page_equivalent}p.docx"
    render = subprocess.run(
        [sys.executable, str(APP_ROOT / "build_docx.py"), str(content_path), str(docx_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    docx_ok = render.returncode == 0 and docx_path.is_file() and docx_path.stat().st_size > 10000

    gate = _read_json(run / BUDGET_GATE_FILE)
    midrun = _read_json(run / MIDRUN_BUDGET_FILE)
    ledger_totals = _read_json(run / BUDGET_LEDGER_FILE).get("totals") or {}
    evidence = {
        "level": label,
        "page_equivalent_target": page_equivalent,
        "document_scale": scale,
        "effective_chars": effective_chars,
        # 产物页数用渲染口径的实测尺子；900 字/页是 Round 4 之前的旧口径，留着会把
        # 每一次规模验证都读成"页数是计划的 1.18 倍"。
        "chars_per_technical_page": RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE,
        "page_equivalent_actual": round(effective_chars / RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE),
        "planned_ledger_chars_per_page": LEDGER_CHARS_PER_TECHNICAL_PAGE,
        "budget_gate": _gate_view(gate),
        "midrun_batches_observed": (midrun.get("observed") or {}).get("batches_observed"),
        "midrun_next_batch_caps": len(midrun.get("next_batch_chars_per_unit_cap") or {}),
        "midrun_provider_calls": midrun.get("provider_calls_made"),
        "budget_ledger": ledger_totals,
        "sections": sections_total,
        "generation_seconds": elapsed,
        "assembly_seconds": (result.get("word") or {}).get("artifact_seconds"),
        "checkpoint_schema": checkpoint.get("schema_version"),
        "volumes": len(volume_plan.get("volumes") or []),
        "chapters": len(hierarchy.get("chapters") or []),
        "sections_planned": len(hierarchy.get("sections") or []),
        "content_units_planned": len(hierarchy.get("content_units") or []),
        "content_units_in_volume_plan": plan_cus,
        "content_units_completed": len(checkpoint.get("completed_content_units") or []),
        "content_units_sent": cu_audit.get("sent"),
        "content_units_provider_received": cu_audit.get("provider_received"),
        "content_units_returned": cu_audit.get("returned"),
        "content_units_persisted": cu_audit.get("completed"),
        "content_units_legacy_fallback": cu_audit.get("legacy_fallback"),
        "generation_batches": batch_count,
        "provider_mock_calls": (result.get("word") or {}).get("provider_calls_total"),
        "completed_volumes": checkpoint.get("completed_volumes"),
        "docx_rendered": docx_ok,
        "docx_bytes": docx_path.stat().st_size if docx_path.is_file() else 0,
        "docx_path": str(docx_path),
        "verification_class": "SYNTHETIC_MOCK_PROVIDER",
    }
    _write_scale_evidence(evidence)
    return evidence


class ScaleLevelTests(unittest.TestCase):
    def _assert_level(self, evidence: dict, page_equivalent: int) -> None:
        """一条级必须同时过三条线：真实 DOCX 在场、产品硬门带内、工程地板不塌。"""
        floor, ceiling = PRODUCT_ACCEPTANCE_BAND[page_equivalent]
        actual = evidence["page_equivalent_actual"]
        self.assertTrue(evidence["docx_rendered"], "必须产出真实 DOCX")
        # 产品验收带（§4 硬门）。这一条就是过去缺的那一条：244 页当时只被
        # `>= 200` 拦，量出来低于 250 也照样绿。
        self.assertGreaterEqual(
            actual, floor,
            f"实测 {actual} 页低于 {page_equivalent} 档产品硬门下沿 {floor} 页")
        self.assertLessEqual(
            actual, ceiling,
            f"实测 {actual} 页超过 {page_equivalent} 档产品硬门上限 {ceiling} 页")
        self.assertGreaterEqual(actual, ENGINEERING_REGRESSION_FLOOR[page_equivalent])
        # 实测必须与门控投影对得上账。这里钉的是**方向**，不是一个对称百分比：
        #   · 投影**低估**（projected < actual）才是危险方向 —— 门控以为还留着余量，
        #     实际已经把文档顶越 320 上沿（R41 抓到的 352 页就是这么来的）。
        #   · 投影**高估**只会让门过早拒绝一份本来没问题的计划，是保守侧。
        # 低估的允许幅度按档实测给出（见 `PROJECTION_UNDER_PREDICTION_ENVELOPE` 为什么分档）。
        projected = evidence["budget_gate"]["projected_technical_effective_pages"]
        drift = (actual - projected) / float(projected or 1)
        self.assertGreaterEqual(
            drift, -PROJECTION_OVER_PREDICTION_TOLERANCE,
            f"投影 {projected} 页比实测 {actual} 页高出 {-drift:.1%} —— 判决尺又在高估页数，"
            "门控会按到不了的页数放行削减")
        self.assertLessEqual(
            abs(drift), PROJECTION_UNDER_PREDICTION_ENVELOPE[page_equivalent],
            f"实测 {actual} 页 vs 投影 {projected} 页：低估 {drift:.1%} 超过该档实测包络 "
            f"{PROJECTION_UNDER_PREDICTION_ENVELOPE[page_equivalent]:.0%}")
        self.assertGreater(evidence["sections"], 0)
        self.assertEqual(evidence["checkpoint_schema"], "longform-checkpoint-v0.3-hierarchical")
        # §13：进到生成的每一级，计划本身必须落在自己那一档的页带内，且门控零调用
        gate = evidence["budget_gate"]
        self.assertEqual(gate["status"], "PASS", gate)
        self.assertTrue(gate["may_enter_generation"], gate)
        self.assertEqual(gate["provider_calls_made"], 0)
        self.assertGreaterEqual(gate["projected_technical_effective_pages"], gate["page_floor"])
        self.assertLessEqual(gate["worst_case_technical_effective_pages"], gate["page_ceiling"])
        # §10：结构额度不得超过参考带上沿（页数尺子就标定在这条混合上）
        self.assertLessEqual(gate["planned_structure_share"], 0.35 + 1e-9)
        # §7/§14：账本分两路记账，正文 + 结构 == 实际字数量得上
        ledger = evidence["budget_ledger"]
        self.assertEqual(ledger["prose_chars"] + ledger["structure_chars"],
                         ledger["actual_chars"])
        self.assertGreater(ledger["content_units_arrived"], 0)
        self.assertEqual(evidence["midrun_provider_calls"], 0)
        self.assertGreater(evidence["midrun_batches_observed"] or 0, 0,
                           "§14：生成中预算账必须在生成过程中就被写过")

    def test_level_1_70_pages(self):
        ev = _scale_run(70, Path(tempfile.mkdtemp()), "LEVEL-1")
        self._assert_level(ev, 70)
        self.assertEqual(ev["document_scale"], "regular_solution")

    def test_level_2_150_pages(self):
        ev = _scale_run(150, Path(tempfile.mkdtemp()), "LEVEL-2")
        self._assert_level(ev, 150)
        self.assertEqual(ev["document_scale"], "detailed_technical_solution")

    def test_level_3_200_pages(self):
        """§13 负例：200 页的计划排进 120～150 页的规模 → 挡在第一次 Provider 调用之前。

        这一档在 Round 4 之前是"乘 4.6 倍预算假装 200 页"；统一预算与页带对齐之后，
        这种计划正是 §13 要拦的那一种：计划总和 > 文档上限，必须先重新分配。
        """
        with self.assertRaises(PlanningError) as raised:
            _scale_run(200, Path(tempfile.mkdtemp()), "LEVEL-3", plan_pages=200)
        message = str(raised.exception)
        self.assertIn("BLOCKED_OVER_CEILING", message)
        self.assertIn("未发起任何 Provider 调用", message)
        evidence = json.loads(scale_evidence_path(200).read_text(encoding="utf-8"))
        self.assertEqual(evidence["outcome"], "BLOCKED_BEFORE_GENERATION")
        self.assertEqual(evidence["provider_calls"], 0)
        self.assertFalse(evidence["generation_started"], "被挡住的一级不得留下生成痕迹")
        self.assertFalse(evidence["midrun_tracking_written"])
        gate = evidence["budget_gate"]
        self.assertGreater(gate["worst_case_technical_effective_pages"], gate["page_ceiling"])
        self.assertGreater(gate["reallocation_sections"], 0, "§13 要求给出逐节重新分配需求")

    def test_level_4_300_pages(self):
        ev = _scale_run(300, Path(tempfile.mkdtemp()), "LEVEL-4")
        # §4/§5：300 类的通过门是产品硬门 250～320，不再是"地板 200 页"。
        self._assert_level(ev, 300)
        self.assertEqual(ev["document_scale"], "large_technical_tender")
        # §23：合成 300-Class 的投影必须落在 250～320 硬带内
        self.assertGreaterEqual(ev["budget_gate"]["page_floor"], 250)
        self.assertLessEqual(ev["budget_gate"]["page_ceiling"], 320)
        # §8 推荐中心（270～300）与实测的差额按**具名字段**留在证据里，不写成断言：
        # 差额来源是文档额度里那 7.51% 的机动额度在合成链上没被花掉（本轮不改机动额度语义）。
        center_low, center_high = SYNTHETIC_RECOMMENDED_CENTER[300]
        actual = ev["page_equivalent_actual"]
        ev["recommended_center_band"] = [center_low, center_high]
        ev["recommended_center_hit"] = bool(center_low <= actual <= center_high)
        _write_scale_evidence(ev)
        self.assertGreaterEqual(actual, PRODUCT_ACCEPTANCE_BAND[300][0])


if __name__ == "__main__":
    unittest.main()
