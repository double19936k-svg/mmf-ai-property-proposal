"""Content-Unit 输出长度标定 —— 全项目唯一一把"这批 CU 会交付多少"的尺。

为什么要单独一个模块：`batch_planner` 过去用 `max_budget.effective_chars × 1.05` 当预测，
那是"预算上限再放 5%"，不是"实测交付量"。Run D 304 个 CU 的实测是 交付/预算上限 ≈ 0.87，
于是每个 CU 被系统性高估约 1.2 倍，一个 8192-token 的口子只装得下 5 个 CU，
27 节 304 个 CU 被撑成 72 次调用。标定与预测分家以后，改预测只看这一处。

口径边界（写给下一个读代码的人）：
* Provider 的真实 token 用量在 Run D 是 `token_provenance=estimate`（`len(json)//4`），
  对中文系统性低估，**不能**拿来反推容量；所以这里全部用"精确测量的有效字符"作单位，
  再按 1 字符 ≤ 1 token 的最坏情形折算成 token。这个折算方向是保守的。
* **点估计用 ratio-of-sums（Σ交付 / Σ预算），不用分位数之比的中位数。**
  容量问的是"一批加起来交付多少字符"，加和的正确估计量就是加和之比。
  同一份数据里 median-of-ratios = 0.9043 而 ratio-of-sums = 0.8742：后者才是
  对"和"的无偏陈述；用前者等于在每个 CU 上重复一次"取上分位"的保守。
  每个 CU 自己的 p50/p75/p95 仍然保留，但它们回答的是"单个 CU 长什么样"，
  只用于"这一个 CU 单独装不装得下硬上限"的告警。
* **尾部保护只算一次**：Provider 侧 `safe_output_budget` 已经打了 0.72（8192→5898，
  留 38.9% 余量）。把逐 CU 的 p95 再加起来当批次尾部，是第二次加同一层保守——
  而且数学上站不住：分位数不可加，7 个 CU 同时落在各自 p95 的概率趋零。
  Run D 的批次级实测支持这一条：52 个有 CU 证据的调用里，
  Σ交付/Σ预算 最大只有 0.992（p95 0.9754），0 次截断。
  所以批次尾部用**实测批次放大系数**（`call_tail_amplification`），不用 Σp95。
"""
from __future__ import annotations

from typing import Any

# 中文字符在最坏分词下 ≈ 1 token；结构性字段另有 CU_FRAMING_TOKENS 记账。
TOKENS_PER_CHAR_WORST_CASE = 1.0
CU_FRAMING_TOKENS = 48
# 单 CU 的最小预测：低于它的批次里"多塞一个 CU"几乎不花钱，留余量反而更省调用。
MIN_CU_OUTPUT_TOKENS = 220

# 点估计口径 = 加和之比；p95 = 单 CU 尾部，只用于"这一个 CU 自己就顶穿硬上限"的告警。
PLANNING_BASIS = "aggregate"
TAIL_GUARD_BASIS = "p95"

_CALIBRATION_PROVENANCE = {
    "measured_from_run": "tender_20260922_005351_b6002a",
    "evidence_file": (
        "04_Real_Run_Evidence/MMF007C_R4_20260922/r4_batch_capacity_runD.json"
    ),
    "content_units_measured": 304,
    "provider_calls_observed": 72,
    "truncated_calls_observed": 0,
    "measured_at": "2026-09-22",
    "unit": "effective_chars / max_budget.effective_chars",
    "token_dimension": "NOT_INDEPENDENTLY_VERIFIABLE",
    "planning_basis": PLANNING_BASIS,
    "single_unit_tail_basis": TAIL_GUARD_BASIS,
    "sum_delivered_chars": 272655,
    "sum_planned_max_chars": 311893,
    "why_aggregate_not_median_of_ratios": (
        "容量问的是一批加起来交付多少，加和之比（0.8742）才是对「和」的陈述；"
        "分位数之比的中位数（0.9043）高估了 3.4%，等于在每个 CU 上重复一次取上分位。"
        "均值 0.8749 这次与加和之比接近，但仍不是同一个量：它按 CU 等权，"
        "而预算大的 CU 在实际交付里权重大。"
    ),
    "why_not_sum_of_per_cu_p95": (
        "分位数不可加。批次尾部改用 52 个调用的实测批次口径：Σ交付/Σ点估计 的最大值 1.1089"
        "（见 call_level_delivery_calibration）。Provider 侧 0.72 安全系数（放大系数 1.3889）"
        "仍然是绑定项、也是本轮唯一一次尾部保护，不再叠加第二层。"
    ),
}

# archetype 标定表。`aggregate` = Σ交付/Σ预算（点估计用这一列）；
# p50/p75/p95 是该 archetype 内**单个 CU** 的交付/预算上分之比的分位数（只作告警与诊断）。
# sample_units < 10 的只登记、不参与预测，避免用 2 个样本给整个项目定尺。
ARCHETYPE_DELIVERY_CALIBRATION: dict[str, dict[str, float]] = {
    "CHECKLIST": {"aggregate": 0.8779, "p50": 0.8967, "p75": 0.9454, "p95": 0.9913, "sample_units": 63},
    "STANDARD_TABLE": {"aggregate": 0.8561, "p50": 0.8983, "p75": 0.932, "p95": 0.993, "sample_units": 48},
    "MATRIX": {"aggregate": 0.835, "p50": 0.877, "p75": 0.9223, "p95": 0.9849, "sample_units": 36},
    "FORM_TEMPLATE": {"aggregate": 0.8053, "p50": 0.8851, "p75": 0.9285, "p95": 0.9759, "sample_units": 35},
    "RESPONSIBILITY_MATRIX": {"aggregate": 0.8715, "p50": 0.913, "p75": 0.9445, "p95": 0.997, "sample_units": 25},
    "SOP": {"aggregate": 0.9673, "p50": 0.9689, "p75": 0.9874, "p95": 1.2587, "sample_units": 23},
    "PROCESS": {"aggregate": 0.9612, "p50": 0.9448, "p75": 0.9859, "p95": 1.1637, "sample_units": 22},
    "NARRATIVE": {"aggregate": 0.8862, "p50": 0.9043, "p75": 0.9586, "p95": 1.0, "sample_units": 21},
    "RISK_REGISTER": {"aggregate": 0.8275, "p50": 0.8874, "p75": 0.9662, "p95": 0.9779, "sample_units": 13},
    "SCHEDULE": {"aggregate": 0.8812, "p50": 0.8981, "p75": 0.9468, "p95": 0.9906, "sample_units": 10},
    # 样本不足（<10）：只登记，不参与预测。
    "EMERGENCY_PLAYBOOK": {"aggregate": 1.0087, "p50": 0.9574, "p75": 0.994, "p95": 1.275, "sample_units": 6},
    "KPI_TABLE": {"aggregate": 0.8269, "p50": 0.6867, "p75": 0.9677, "p95": 0.9677, "sample_units": 2},
}

POOLED_DELIVERY_CALIBRATION = {
    "aggregate": 0.8742, "p50": 0.9043, "p75": 0.9583, "p95": 1.0, "sample_units": 304,
}
MIN_SAMPLES_FOR_ARCHETYPE_RULE = 10

# 批次级（一次 Provider 调用）实测。52 个调用有逐次 CU 证据，覆盖 216 个 CU。
# 两个口径要分清：
#   delivered_over_budget_*  —— 交付 / Σ单CU预算上限：说明"预算本身有多准"。
#   delivered_over_prediction_* —— 交付 / 规划器点估计（含每 CU 的结构开销）：
#   这才是批次尾部的直接测量，`call_tail_amplification` 用它。
CALL_LEVEL_DELIVERY_CALIBRATION = {
    "sample_calls": 52,
    "content_units_covered": 216,
    "delivered_over_budget_p50": 0.9053,
    "delivered_over_budget_p95": 0.9754,
    "delivered_over_budget_max": 0.992,
    "delivered_over_prediction_p50": 0.9957,
    "delivered_over_prediction_p95": 1.0217,
    "delivered_over_prediction_max": 1.1089,
    "calls_under_predicted": 21,
    "calls_over_predicted": 31,
    "delivered_chars_p50": 4606,
    "delivered_chars_p95": 4976,
    "delivered_chars_max_non_truncating": 5178,
    "truncated_calls_in_sample": 0,
    "note": (
        "样本只覆盖 Run D 的一部分调用（另有 20 次调用的 CU 证据未持久化），"
        "所以 max 是「本样本内最大」，不是「总体最大」；批次尾部系数按 1 字符≤1 token 折算。"
        "点估计在批次级已经几乎无偏（p50 0.9957），偏差主要来自单 CU 之间的相互抵消。"
    ),
}


def calibration_provenance() -> dict[str, Any]:
    return dict(_CALIBRATION_PROVENANCE)


def call_level_calibration() -> dict[str, Any]:
    return dict(CALL_LEVEL_DELIVERY_CALIBRATION)


def delivery_ratio(archetype: Any, basis: str = PLANNING_BASIS) -> float:
    """返回该 archetype 的 实测交付 / 预算上限 比值；样本不足或未知一律用 POOLED。"""
    row = ARCHETYPE_DELIVERY_CALIBRATION.get(str(archetype or "").strip().upper())
    if not row or int(row.get("sample_units") or 0) < MIN_SAMPLES_FOR_ARCHETYPE_RULE:
        row = POOLED_DELIVERY_CALIBRATION
    return float(row.get(basis) or row[PLANNING_BASIS])


def call_tail_amplification() -> float:
    """批次交付相对**点估计**的最坏放大系数（实测）。

    直接取批次级 Σ交付/Σ预测 的最大观测值（Run D 52 次有 CU 证据的调用里最大 1.1089），
    而不是拿 Σ交付/Σ预算 的最大值除以 pooled 比值推导：后者 0.992/0.8742=1.1347 混了两个
    口径（分子是同一批内容的实测、分母是整个项目的平均尺），偏保守但不是这一批的实测。
    推导值仍留在 `call_level_derived_amplification_for_reference` 里备查。
    """
    worst = float(CALL_LEVEL_DELIVERY_CALIBRATION["delivered_over_prediction_max"])
    return round(worst, 4) if worst > 0 else 1.0


def call_level_derived_amplification_for_reference() -> float:
    """Σ交付/Σ预算 最大值 ÷ pooled 比值 —— 只作对照，不参与判决。"""
    pooled = float(POOLED_DELIVERY_CALIBRATION[PLANNING_BASIS])
    worst = float(CALL_LEVEL_DELIVERY_CALIBRATION["delivered_over_budget_max"])
    return round(worst / pooled, 4) if pooled > 0 else 1.0


def batch_output_ceiling_tokens(provider_output_limit: int) -> int:
    """点估计允许达到的批次数值上限：实测批次最坏放大后仍不得顶穿 Provider 硬上限。

    注意它通常**不**是绑定项——`safe_output_budget` 的 0.72（放大系数 1.389）已经更紧。
    留着是为了说清两件事：尾部保护只在这里发生一次；以及谁将来把 0.72 调大，
    会先在这里被实测系数挡住，而不是等到真实跑出现截断才知道。
    """
    limit = int(provider_output_limit or 0)
    if limit <= 0:
        return 0
    return max(1, int(limit / call_tail_amplification()))


def _planned_chars(contract: dict[str, Any]) -> int:
    for key in ("max_budget", "target_budget"):
        budget = contract.get(key)
        if isinstance(budget, dict) and budget.get("effective_chars"):
            return int(budget["effective_chars"])
    return int(contract.get("planned_words") or 0)


def predicted_cu_output_tokens(contract: dict[str, Any]) -> int:
    """一次 Provider 调用里，这个 CU 预计要吃掉的输出 token（点估计 = ratio-of-sums）。"""
    chars = _planned_chars(contract)
    if chars <= 0:
        return MIN_CU_OUTPUT_TOKENS
    ratio = delivery_ratio(contract.get("archetype"), PLANNING_BASIS)
    return max(MIN_CU_OUTPUT_TOKENS, int(chars * ratio * TOKENS_PER_CHAR_WORST_CASE) + CU_FRAMING_TOKENS)


def worst_case_cu_output_tokens(contract: dict[str, Any]) -> int:
    """单 CU 的 p95 口径。只用来标"这一个 CU 自己就装不进硬上限"，不参与合批判决。"""
    chars = _planned_chars(contract)
    if chars <= 0:
        return predicted_cu_output_tokens(contract)
    ratio = delivery_ratio(contract.get("archetype"), TAIL_GUARD_BASIS)
    return max(MIN_CU_OUTPUT_TOKENS, int(chars * ratio * TOKENS_PER_CHAR_WORST_CASE) + CU_FRAMING_TOKENS)


def calibration_status() -> dict[str, Any]:
    """规划结果里原样带出标定来历：谁测的、多少样本、哪一个口径、token 维度可不可验证。"""
    return {
        "schema_version": "cu-output-calibration-v0.2",
        "provenance": calibration_provenance(),
        "planning_basis": PLANNING_BASIS,
        "pooled": dict(POOLED_DELIVERY_CALIBRATION),
        "archetype_rules": {
            name: {**row, "applied": int(row.get("sample_units") or 0) >= MIN_SAMPLES_FOR_ARCHETYPE_RULE}
            for name, row in sorted(ARCHETYPE_DELIVERY_CALIBRATION.items())
        },
        "min_samples_for_archetype_rule": MIN_SAMPLES_FOR_ARCHETYPE_RULE,
        "call_level": call_level_calibration(),
        "call_tail_amplification": call_tail_amplification(),
        "call_tail_amplification_basis": "delivered_over_prediction_max (batch-level, measured)",
        "call_level_derived_amplification_for_reference": call_level_derived_amplification_for_reference(),
        "framing_tokens_per_cu": CU_FRAMING_TOKENS,
        "tokens_per_char_worst_case": TOKENS_PER_CHAR_WORST_CASE,
    }
