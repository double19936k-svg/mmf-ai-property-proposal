"""MMF-007C Round 4 / P0-A：统一内容预算（Unified Content Budget，指令 §7–§15）。

Round 3 的失败形状已经量化清楚（Run D 304 个 Content Unit 逐条实测）：

    CU 正文（prose face） 262,865 字
    CU 结构 payload       203,778 字   ← 占实际交付字的 43.7%
    计划 target            260,000 字
    实际交付/计划           1.795

也就是说：结构化 Archetype 声明了 rows/columns/steps/items/fields 的**几何**，
但 CU 额度里的 `effective_chars` 只按正文标，表格/清单/表单那些字是**额度之外**
长出来的。每一条 `STANDARD_TABLE` 的实测正文 836 字（正好落在 target 855 附近），
再挂一张 867 字的表 —— 27/27 节全部 `SECTION_OVER_LENGTH`，超上限 75 页。
可减 Padding 只有约 1 页，所以这不是注水，是预算模型少算了一整个维度。

本模块把 §7 点名的全部形态（叙述正文、清单、流程、SOP、检查表、表格、表单模板、
矩阵、流程说明、结构化 payload、表题与备注）折算成同一种货币，并在**规划时**就把
结构成本从所属 Section 额度里扣掉（§9），而不是等 Provider 回来再记账。

货币与标定常数一律带 provenance，实测与估算不混着报：

- 结构单位成本：Run D 实测 P50（`r4_structure_unit_cost_runD.json`），按 5 取整；
- 额度→页：Run D 的账本字（467,292）对同一 Run 的真实渲染技术有效页（395）反标；
- 账本→渲染可见字：同一次 Run 的 345,307 / 467,292，装配与版面压缩都在这条系数里。

红线：不改 ID 体系、不把 Section 等同一次 Provider 请求、不固定 27 节、
不为了压页数删需求或评分证据（那是 reduction_executor 的边界，不是这里的）。
"""
from __future__ import annotations

import re
from typing import Any

UNIFIED_BUDGET_VERSION = "unified-content-budget-v0.1"
BUDGET_GATE_FILE = "pre_generation_budget_gate.json"
MIDRUN_BUDGET_FILE = "budget_midrun_tracking.json"

# ---------------------------------------------------------------- 成本标定 --
# Run D 实测每个结构单位的可见字（P50，逐 archetype 量，304 CU / 166 表；
# 证据：`r4_structure_unit_cost_runD.json`，同一目录下的探针脚本可复算）。
# 表格按"行"计（一行含该 Archetype 声明的全部列），流程按"步"、清单按"项"、表单按"栏"：
#   行：STANDARD_TABLE 107.0 / RESPONSIBILITY_MATRIX 106.0 / RISK_REGISTER 115.0
#       SCHEDULE 102.6 / MATRIX 89.7 / KPI_TABLE 64.8
#   步：SOP 74.4 / PROCESS 66.9 / EMERGENCY_PLAYBOOK 66.0
#   项：CHECKLIST 61.2      栏：FORM_TEMPLATE 67.6
# 取整到 5，并且**同一 Run 的实测均值都高于 P50**（RISK_REGISTER 32.2/格 vs 28.8），
# 所以这条尺子偏松的那一侧是"少排几行"，不会把页数算少。
ARCHETYPE_UNIT_COST_CHARS = {
    "STANDARD_TABLE": 105, "MATRIX": 90, "KPI_TABLE": 65, "RESPONSIBILITY_MATRIX": 105,
    "RISK_REGISTER": 115, "SCHEDULE": 105,
    "SOP": 75, "PROCESS": 70, "EMERGENCY_PLAYBOOK": 70,
    "CHECKLIST": 60, "FORM_TEMPLATE": 70, "NARRATIVE": 0,
}
# 列数变化时的伸缩基准：各 Archetype 的声明列数（RESPONSIBILITY_MATRIX 用 5 列）。
DEFAULT_COLUMNS_FOR_ARCHETYPE = {
    "RESPONSIBILITY_MATRIX": 5, "STANDARD_TABLE": 4, "MATRIX": 4, "KPI_TABLE": 4,
    "RISK_REGISTER": 4, "SCHEDULE": 4,
}
STRUCTURE_UNIT_COST_CHARS = {
    "table_row": 105, "step": 70, "item": 60, "field": 70, "prose": 0,
}
STRUCTURE_COST_BASIS = "MEASURED_RUN_D_P50_304_UNITS"

# 额度→页 / 账本→渲染：同一次 Run D 的两个实测口径互除得到，不是猜的。
#   账本字 467,292 ÷ 真实渲染技术有效页 395 = 1,183.0
#   渲染可见字 345,307 ÷ 账本字 467,292 = 0.739（装配压缩与版面留白都折进这条系数）
LEDGER_CHARS_PER_TECHNICAL_PAGE_RUN_D = 1183.0
LEDGER_TO_RENDER_CHAR_YIELD_RUN_D = 0.739
# 账本字 → 装配产物可见字：Run D 同一次跑的 360,117（document_scale_report 的
# effective_chars）÷ 467,292（content_unit_budget_ledger 的 actual_chars）。
# 事后 QA 的 DOCUMENT_EFFECTIVE_BUDGET 量的是装配产物，规划额度是账本字，
# 不换算就会把"按新额度写满的一篇"读成缺字（LF-300-BUDGET 误报 DEPTH_INCOMPLETE）。
ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D = 0.7707
RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE = 873.6   # artifact_pagination.json 实测
# --- Round 4.1 §7：系数链换到"修复后这条链"上 ------------------------------------
# 两路收益的历史真值保留在下面两个 `*_RUN_D_PRE_FIX` 常量里，但它们**不再参与排产**，
# 因为 Run D 跑在修复前的链上。`governance.artifact_qa.apply_artifact_repairs` 曾让整棵
# fragment 树共用一个"已见字符串"集合，于是 CU 的结构 payload 在审计镜像
# （`content_unit_fragments`）与渲染槽位（`tables`/`processes`）里各存一份时，后走到的那
# 份被判成重复删掉 —— 提交站实测每节 4/5 张表 0 行、3/4 个流程 0 步（R41 证据
# `r41_structure_chain_replay.py`）。把去重域收窄到"每个列表各自"之后，同一批夹具、
# 同一把尺（`visible_chars_of`）、**同一套分桶**（预算把 CHECKLIST 的 `payload.items` 记成
# 结构字，产物侧就必须把 `bullet_group` 记成结构字，否则同一批字两侧不同桶）两侧各量一次，
# 27 节全配对（`r41_route_yield_measurement.py`）：
#   Run D（真实，修复前）  正文路 1.0025  结构路 0.4359  结构占比 0.4367
#   R41（合成，修复后）    正文路 0.8626  结构路 1.0081  结构占比 0.3314
# 结构路从 0.44 跳到 1.01 不是内容变多了，是**已经修掉的损失不再冒充产品规律**；
# 正文路从 1.00 掉到 0.86 是合成夹具里还有成片重复段被本地整备丢掉（LEVEL-4 实测
# `drop_duplicate_paragraph` 41 处），那夹具质量问题在 §10 的重复率门里另外管。
# 模型自检：拿 Run D 自家系数与自家占比回代 466,643 ÷ (892.0/0.7551) = 395.2 页 = 它的
# 真实分页 395（差 0.14%），说明"两路收益 × 每页字数"这条链成立。
RENDER_YIELD_PROSE_RUN_D_PRE_FIX = 1.0025         # Run D 真实正文路（装配段落 ÷ CU 正文额度）
RENDER_YIELD_STRUCTURE_RUN_D_PRE_FIX = 0.4359     # Run D 真实结构路 —— 修复前，被去重删空
RENDER_YIELD_PROSE = 0.8626                       # R41 实测：正文路（丢重复段是主要去向）
RENDER_YIELD_STRUCTURE = 1.0081                   # R41 实测：结构路（表体/清单/流程，修复后）
RUN_D_STRUCTURE_SHARE = 0.4367                    # Run D payload 占实际交付字的比例（历史）
R41_STRUCTURE_SHARE_MEASURED = 0.3314             # 修复后合成链实测结构占比
# 统一预算把结构占比设计在 0.35，修复后同一份计划实测落到 0.3314（修复前只落到 0.1136 ——
# 那正是两路模型不能直接用来判页数的原因）。两路混合因此是 0.9135 产物字/账本字。
#
# **判决用哪把尺**：用文档级直量，不用两路推导。R41 修复后 LEVEL-4 实跑
# 269,646 账本字 ÷ 290 技术页等效 = 930 字/页（测试口径同一次 917.2，两路模型推导 956.3，
# 三者相差 ≤4%）。930 是把"每节固定版面成本、正文去重、表格治理回收"全部折进去的那一个数，
# 而两路模型只在"结构占比与正文去重率和标定那次一致"时才准 —— 70/150 页档实测结构占比与
# 正文重复率都和大文档不同，两路推导在那里就偏低 9～14%（探针 `r41_route_yield_measurement.py`
# 与 `r41_p0_1_ruler*` 三轮实测）。所以：判决走 930 直量尺，两路只作交叉核对留着。
# 对照 Round 4：那时候"两条链都等于 1,183"不是巧合被打破，而是**两条链共用同一个 bug**；
# 修掉之后真实值就是 930，继续拿 1,183 排产会把 300 类排成 352 页越顶（R41 实测）。
PLANNED_STRUCTURE_SHARE = 0.35
MIXED_YIELD_AT_PLANNED_STRUCTURE_SHARE = round(
    (1.0 - PLANNED_STRUCTURE_SHARE) * RENDER_YIELD_PROSE
    + PLANNED_STRUCTURE_SHARE * RENDER_YIELD_STRUCTURE, 4)               # 0.9135，交叉核对用
LEDGER_CHARS_PER_TECHNICAL_PAGE = 930.0
LEDGER_TO_ARTIFACT_VISIBLE_YIELD = round(
    RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE / LEDGER_CHARS_PER_TECHNICAL_PAGE, 4)
PAGE_PROJECTION_BASIS = "MEASURED_R41_POST_FIX_LEVEL4_DOCUMENT_PURCHASE"

TABLE_LIKE_ARCHETYPES = ("STANDARD_TABLE", "MATRIX", "KPI_TABLE", "RESPONSIBILITY_MATRIX",
                         "RISK_REGISTER", "SCHEDULE")
STEP_ARCHETYPES = ("SOP", "PROCESS", "EMERGENCY_PLAYBOOK")
ITEM_ARCHETYPES = ("CHECKLIST",)
FIELD_ARCHETYPES = ("FORM_TEMPLATE",)
STRUCTURED_ARCHETYPES = TABLE_LIKE_ARCHETYPES + STEP_ARCHETYPES + ITEM_ARCHETYPES + FIELD_ARCHETYPES

# §9 要求每张表带 information_role。这里按 Archetype 给角色，不按标题猜。
INFORMATION_ROLE = {
    "STANDARD_TABLE": "SUMMARY_TABLE",
    "SCHEDULE": "PROCESS_TABLE",
    "SOP": "PROCESS_TABLE",
    "PROCESS": "PROCESS_TABLE",
    "EMERGENCY_PLAYBOOK": "PROCESS_TABLE",
    "MATRIX": "CONTROL_MATRIX",
    "KPI_TABLE": "CONTROL_MATRIX",
    "RESPONSIBILITY_MATRIX": "CONTROL_MATRIX",
    "RISK_REGISTER": "CONTROL_MATRIX",
    "CHECKLIST": "CHECKLIST",
    "FORM_TEMPLATE": "FORM_TEMPLATE",
    "NARRATIVE": "NARRATIVE_PROSE",
}
# 一个 CU 的额度里留给"正文面"的比例。结构化 Archetype 的正文是表的说明与口径，
# 表体才是主交付；Run D 实测交付比例约 0.48～0.55，把额度**设计**成表体占多数，
# 正文面被压到"讲清口径"而不是"再讲一遍表"（§12 的同义重复正是从这一路来的）。
PROSE_FACE_SHARE = {
    "NARRATIVE": 1.0,
    "SOP": 0.55, "PROCESS": 0.55, "EMERGENCY_PLAYBOOK": 0.45,
    "CHECKLIST": 0.40, "FORM_TEMPLATE": 0.30,
    "STANDARD_TABLE": 0.40, "MATRIX": 0.45, "KPI_TABLE": 0.45,
    "RESPONSIBILITY_MATRIX": 0.45, "RISK_REGISTER": 0.45, "SCHEDULE": 0.45,
}
MIN_UNITS = {"table": 3, "step": 4, "item": 5, "field": 5}
MAX_UNITS = {"table": 18, "step": 16, "item": 24, "field": 24}
# §10 的参考带（不是硬编码的分配值，实际比例由该节自己的 Archetype 组合算出来）。
NARRATIVE_SHARE_BAND = (0.60, 0.75)
STRUCTURED_SHARE_BAND = (0.20, 0.35)
RESERVE_SHARE_BAND = (0.05, 0.10)
# 投影自身有组合不确定性（结构占比每偏 0.05，页数偏约 5%）。贴着上限通过不叫通过。
CEILING_SAFETY_MARGIN = 0.95


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_structured(archetype: Any) -> bool:
    return str(archetype or "") in STRUCTURED_ARCHETYPES


def structure_kind(archetype: Any) -> str:
    archetype = str(archetype or "")
    if archetype in TABLE_LIKE_ARCHETYPES:
        return "table"
    if archetype in STEP_ARCHETYPES:
        return "step"
    if archetype in ITEM_ARCHETYPES:
        return "item"
    if archetype in FIELD_ARCHETYPES:
        return "field"
    return "prose"


def unit_cost(archetype: Any) -> int:
    """一个结构单位的字成本。表格=一行（含声明列）、流程=一步、清单=一项、表单=一栏。

    标定用的是**整个 payload** 的可见字，所以表题、表注、栏目标签都已经在这条线里
    —— 再单加一项 caption allowance 就是把同一批字数两遍，页数会反过来被低估。
    """
    key = str(archetype or "")
    if key in ARCHETYPE_UNIT_COST_CHARS:
        return ARCHETYPE_UNIT_COST_CHARS[key]
    kind = structure_kind(key)
    return STRUCTURE_UNIT_COST_CHARS.get({"table": "table_row"}.get(kind, kind), 0)


def information_role(archetype: Any) -> str:
    return INFORMATION_ROLE.get(str(archetype or ""), "NARRATIVE_PROSE")


def _columns_for(archetype: Any) -> int:
    return DEFAULT_COLUMNS_FOR_ARCHETYPE.get(str(archetype or ""), 4)


def structure_cost_for_units(archetype: Any, units: int, *, columns: int | None = None) -> int:
    kind = structure_kind(archetype)
    units = max(0, _int(units))
    if kind == "prose" or not units:
        return 0
    cost = float(unit_cost(archetype))
    if kind == "table":
        # 成本标定时用的是该 Archetype 的声明列数；规划改了列数就按列同比伸缩。
        declared = float(_columns_for(archetype))
        cost *= max(1, _int(columns, int(declared))) / max(1.0, declared)
    return int(round(units * cost))


def geometry_from_structure_allowance(archetype: Any, structure_chars: int,
                                      *, columns: int | None = None) -> dict[str, int]:
    """把"这一 CU 能花多少字在结构上"翻译成 Provider 看得懂的几何。"""
    kind = structure_kind(archetype)
    cols = max(1, _int(columns, _columns_for(archetype)))
    if kind == "prose":
        return {"kind": "prose", "units": 0, "columns": 0}
    per_unit = float(unit_cost(archetype))
    if kind == "table":
        per_unit *= cols / max(1.0, float(_columns_for(archetype)))
    units = int(structure_chars // max(1.0, per_unit))
    units = max(MIN_UNITS[kind], min(MAX_UNITS[kind], units))
    return {"kind": kind, "units": units, "columns": cols if kind == "table" else 0}


def unified_unit_budget(archetype: Any, total_chars: int, *,
                        columns: int | None = None) -> dict[str, Any]:
    """一个 Content Unit 的统一额度：结构成本从额度里扣，不在额度之外追加。

    返回的 `effective_chars` 仍然是**该 CU 的全部可见字**（正文面 + 结构体），
    所以既有的批量切分、输出 token 估算、账本判定都不用换货币；
    换掉的是"这个数终于等于 Provider 会写出来的总量"——Run D 里它只等于 0.56 倍。
    """
    total = max(1, _int(total_chars))
    kind = structure_kind(archetype)
    if kind == "prose":
        return {
            "measure": "unified_effective_chars",
            "effective_chars": total,
            "prose_chars": total,
            "structure_chars": 0,
            "structure_kind": "prose",
            "information_role": information_role(archetype),
            "geometry": {"kind": "prose", "units": 0, "columns": 0},
            "structure_unit_cost": 0,
            "estimated_char_cost": 0,
            "estimated_page_cost": 0.0,
            "cost_basis": STRUCTURE_COST_BASIS,
        }
    share = float(PROSE_FACE_SHARE.get(str(archetype or ""), 0.45))
    prose = int(round(total * share))
    structure = total - prose
    geometry = geometry_from_structure_allowance(archetype, structure, columns=columns)
    # 几何被 MIN/MAX 夹住之后，真实结构成本与预留的额度会有差；差额回吐给正文面，
    # 否则夹出来的余量会变成"没人认领的字"，五层守恒就只是账面数字。
    actual_structure = structure_cost_for_units(archetype, geometry["units"],
                                                columns=geometry["columns"] or None)
    prose = max(120, prose + (structure - actual_structure))
    return {
        "measure": "unified_effective_chars",
        "effective_chars": prose + actual_structure,
        "prose_chars": prose,
        "structure_chars": actual_structure,
        "structure_kind": geometry["kind"],
        "information_role": information_role(archetype),
        "geometry": geometry,
        "structure_unit_cost": unit_cost(archetype),
        "estimated_char_cost": actual_structure,
        "estimated_page_cost": round(actual_structure / LEDGER_CHARS_PER_TECHNICAL_PAGE, 3),
        "cost_basis": STRUCTURE_COST_BASIS,
    }


def scale_budget(archetype: Any, total_chars: int, *, columns: int | None = None) -> dict[str, Any]:
    """按新总量重出一条统一额度，几何跟着重算（§14 下一批自动降预算走这条路）。"""
    return unified_unit_budget(archetype, total_chars, columns=columns)


def contract_budget_row(archetype: Any, budget: dict[str, Any]) -> dict[str, Any]:
    """把统一额度落成契约里的 min/target/max 形状。

    `measure` 仍写该 Archetype 的原生量法（table/steps/items/fields），因为这条字段
    是给 Provider 看的"要交什么形状"；新增的 prose_chars / structure_chars 才是额度分配，
    两者一起进契约，模型就不可能再"正文写满额度之后额外追加一张表"。
    """
    kind = structure_kind(archetype)
    geometry = budget.get("geometry") or {}
    units = int(geometry.get("units") or 0)
    columns = int(geometry.get("columns") or 0)
    row = {
        "measure": {"table": "table", "step": "steps", "item": "items",
                    "field": "fields"}.get(kind, "effective_chars"),
        "effective_chars": int(budget.get("effective_chars") or 0),
        "prose_chars": int(budget.get("prose_chars") or 0),
        "structure_chars": int(budget.get("structure_chars") or 0),
        "information_role": str(budget.get("information_role") or information_role(archetype)),
        "structure_unit_cost": int(unit_cost(archetype)),
        "estimated_char_cost": int(budget.get("estimated_char_cost") or 0),
        "estimated_page_cost": float(budget.get("estimated_page_cost") or 0.0),
        "unified": True,
    }
    if kind == "table" and units:
        row["rows"] = units
        row["columns"] = columns or 4
        # 每格下限从"实测行成本 ÷ 列数"推出来，不是拍一个 12：
        # 格子太扁就是 §63 那种占页低信息表，行数砍了也还是空壳。
        row["min_cell_chars"] = max(12, int(round(row["structure_unit_cost"]
                                                  / max(1, row["columns"]) * 0.6)))
    elif kind == "step" and units:
        row["steps"] = units
    elif kind == "item" and units:
        row["items"] = units
    elif kind == "field" and units:
        row["fields"] = units
    return row


# --------------------------------------------------------- Section 级切分 --
def _budget_from_parts(archetype: Any, prose_chars: int, structure_allowance: int,
                       *, columns: int | None = None) -> dict[str, Any]:
    """直接按"正文面多少字、结构最多花多少字"落一条统一额度。

    `unified_unit_budget` 是按 Archetype 的固定正文占比切分的，压结构占比时不能再用它
    （再切一次会把刚压下去的结构又放大回来），但夹出来的零头仍然回吐给正文面，
    否则五层守恒就只剩账面数字。
    """
    kind = structure_kind(archetype)
    prose = max(0, _int(prose_chars))
    if kind == "prose":
        return unified_unit_budget(archetype, prose)
    geometry = geometry_from_structure_allowance(archetype, max(0, _int(structure_allowance)),
                                                 columns=columns)
    actual = structure_cost_for_units(archetype, geometry["units"],
                                      columns=geometry["columns"] or None)
    # MIN_UNITS 夹住的表至少 3 行：压不动的那部分从正文面里扣，不记成"多出来的字"。
    prose = max(120, prose - max(0, actual - _int(structure_allowance)))
    return {
        "measure": "unified_effective_chars",
        "effective_chars": prose + actual,
        "prose_chars": prose,
        "structure_chars": actual,
        "structure_kind": geometry["kind"],
        "information_role": information_role(archetype),
        "geometry": geometry,
        "structure_unit_cost": unit_cost(archetype),
        "estimated_char_cost": actual,
        "estimated_page_cost": round(actual / LEDGER_CHARS_PER_TECHNICAL_PAGE, 3),
        "cost_basis": STRUCTURE_COST_BASIS,
    }


def cap_structure_share(names: list[str], budgets: list[dict[str, Any]], usable: int,
                        cap_share: float = STRUCTURED_SHARE_BAND[1]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """§10：一条节的结构额度不得吃掉超过参考带上沿的字数。

    为什么要在这里硬扣：`LEDGER_CHARS_PER_TECHNICAL_PAGE` 这条页尺子是在**设计混合
    （正文 0.65 / 结构 0.35）**下反标出来的，而结构每账本字只换到 0.3176 个渲染可见字、
    正文换到 1.0943 —— Run D 实测结构占 43.7%，同样 30 万账本字就只买得到 ~260 页而不是
    285 页。规划里排出来的 Archetype 组合偏结构时，必须把多出来的那部分字数还给正文面
    （表还是那张表，只是少排几行），否则门控算出来的页数永远低于页带下沿。
    """
    structure = sum(max(0, _int(row.get("structure_chars"))) for row in budgets)
    cap = int(round(max(0, _int(usable)) * max(0.0, min(1.0, float(cap_share)))))
    if structure <= cap or structure <= 0:
        return budgets, {"structure_cap_chars": cap, "structure_chars_before_cap": structure,
                         "structure_chars_after_cap": structure, "capped": False,
                         "prose_reclaimed_chars": 0}
    factor = cap / float(structure)
    rows: list[dict[str, Any]] = []
    for archetype, budget in zip(names, budgets):
        prose = max(0, _int(budget.get("prose_chars")))
        allowance = int(round(max(0, _int(budget.get("structure_chars"))) * factor))
        if not allowance:
            rows.append(unified_unit_budget(archetype, prose))
            continue
        rows.append(_budget_from_parts(archetype, prose, allowance,
                                       columns=_int((budget.get("geometry") or {}).get("columns")) or None))
    after = sum(max(0, _int(row.get("structure_chars"))) for row in rows)
    return rows, {"structure_cap_chars": cap, "structure_chars_before_cap": structure,
                  "structure_chars_after_cap": after, "capped": True,
                  "prose_reclaimed_chars": structure - after,
                  "cap_share": cap_share,
                  # MIN_UNITS 让一张表至少 3 行：压不下去的那部分如实报出来，不假装到达上限。
                  "residual_over_cap_chars": max(0, after - cap)}


def plan_section_budget(section_budget: int, archetypes: list[Any],
                        *, reserve_share: float = 0.08, iterations: int = 4,
                        structure_share_cap: float = STRUCTURED_SHARE_BAND[1]) -> dict[str, Any]:
    """一条节的额度 → 各 CU 的统一额度 + narrative/structured/reserve 三项（§10/§11）。

    先留出 reserve，再把剩下的摊给 CU；几何夹出来的零头通过定点迭代回收，
    所以 `sum(unit 额度) + reserve == section_budget` 是**构造出来**的，不是事后核对的。
    比例不写死：纯叙述节算不出 structured，结构化重的节算不出 60/35/5 那组数，
    落在 §10 参考带外只在报告里说明，不掰数字 —— 但结构占比超过参考带上沿要扣回来，
    因为页数尺子就是按这条混合标定的（见 `cap_structure_share`）。
    """
    total = max(1, _int(section_budget))
    names = [str(key or "NARRATIVE") for key in (archetypes or [])] or ["NARRATIVE"]
    reserve = int(round(total * max(0.0, min(0.25, float(reserve_share)))))
    usable = max(len(names), total - reserve)
    requested = [usable // len(names)] * len(names)
    # 零头归最后一个 CU：这里必须减"前面那些"，写成一整份 sum() 会把最后一个 CU
    # 直接从 0 起步（几何夹到 MIN_UNITS 只剩几百字），再由定点迭代把那笔字摊给前面的
    # CU —— 于是一条节的最后一单元长期吃不饱、其余单元略微超配。
    requested[-1] = usable - sum(requested[:-1])
    budgets: list[dict[str, Any]] = []
    for index, archetype in enumerate(names):
        budgets.append(unified_unit_budget(archetype, max(1, requested[index])))
    for _ in range(max(1, int(iterations))):
        delivered = sum(_int(row.get("effective_chars")) for row in budgets)
        if delivered <= 0:
            break
        gap = usable - delivered
        if abs(gap) <= max(1, int(round(usable * 0.002))):
            break
        factor = usable / delivered
        for index, archetype in enumerate(names):
            budgets[index] = unified_unit_budget(
                archetype, max(1, int(round(_int(budgets[index].get("effective_chars")) * factor))))
    budgets, cap_report = cap_structure_share(names, budgets, usable, structure_share_cap)
    # 最后一道零头归到最后一个 CU：宁可有几十字对不上，也不要让"守恒"变成近似说法。
    delivered = sum(_int(row.get("effective_chars")) for row in budgets)
    residual = usable - delivered
    if residual and budgets:
        index = len(budgets) - 1
        budgets[index] = _budget_from_parts(
            names[index],
            _int(budgets[index].get("prose_chars")) + residual,
            _int(budgets[index].get("structure_chars")))
    return {"unit_budgets": budgets, "reserve_budget": reserve,
            "residual_recovered": residual, "structure_cap": cap_report}


def section_budget_split(section_total: int, units: list[dict[str, Any]],
                         *, reserve_share: float = 0.08) -> dict[str, Any]:
    """按该节**自己**的 Archetype 组合报出 narrative / structured / reserve（§10）。

    这里只汇总已经排好的 CU 额度；额度本身由 `plan_section_budget` 构造，
    两者用的是同一组数，所以不会出现"切分说有余量、CU 加起来却超"。
    """
    total = max(1, _int(section_total))
    active = units or []
    narrative_cost = sum(max(0, _int(row.get("prose_chars"))) for row in active)
    structured_cost = sum(max(0, _int(row.get("structure_chars"))) for row in active)
    units_total = narrative_cost + structured_cost
    reserve = max(0, total - units_total)
    return {
        "section_total_budget": total,
        "units_planned_budget": units_total,
        "narrative_budget": narrative_cost,
        "structured_budget": structured_cost,
        "reserve_budget": reserve,
        "narrative_share": round(narrative_cost / total, 4),
        "structured_share": round(structured_cost / total, 4),
        "reserve_share": round(reserve / total, 4),
        # 计划出来的比例落在 §10 参考带外时只报事实，不偷偷掰：掰了数字就对不上账。
        "narrative_share_in_band": NARRATIVE_SHARE_BAND[0] <= narrative_cost / total <= NARRATIVE_SHARE_BAND[1],
        "structured_share_in_band": STRUCTURED_SHARE_BAND[0] <= structured_cost / total <= STRUCTURED_SHARE_BAND[1],
        "reserve_share_in_band": RESERVE_SHARE_BAND[0] <= reserve / total <= RESERVE_SHARE_BAND[1],
        "split_basis": "DERIVED_FROM_PLANNED_ARCHETYPE_MIX",
    }


# ------------------------------------------------------------- 五层守恒 --
def build_budget_tree(*, document_target: int, volumes: list[dict[str, Any]],
                      page_targets: tuple[int, int] | list[int] | None = None,
                      chars_per_technical_page: float = LEDGER_CHARS_PER_TECHNICAL_PAGE
                      ) -> dict[str, Any]:
    """Document → Volume → Section → Content Archetype → CU 五层，逐层核对守恒（§8）。

    每一层只允许"由下层求和"或"由上层摊派"两种来源之一，且两个方向都要对得上；
    对不上就把差额与层级报出来，而不是把 `validation` 写成 PASS。
    """
    document = max(1, _int(document_target))
    volume_rows: list[dict[str, Any]] = []
    archetype_roll: dict[str, dict[str, Any]] = {}
    planned_total = 0
    allocated_total = 0
    reserve_total = 0
    for volume in volumes or []:
        sections: list[dict[str, Any]] = []
        volume_total = 0
        for section in volume.get("sections") or []:
            section_units: list[dict[str, Any]] = []
            allocated = 0
            for unit in section.get("content_units") or []:
                budget = unit.get("unified_budget") or {}
                chars = _int(budget.get("effective_chars")) or _int(unit.get("planned_words"))
                allocated += chars
                archetype = str(unit.get("archetype") or "NARRATIVE")
                roll = archetype_roll.setdefault(archetype, {"archetype": archetype, "units": 0,
                                                             "effective_chars": 0, "prose_chars": 0,
                                                             "structure_chars": 0})
                roll["units"] += 1
                roll["effective_chars"] += chars
                roll["prose_chars"] += _int(budget.get("prose_chars"))
                roll["structure_chars"] += _int(budget.get("structure_chars"))
                section_units.append({"content_unit_id": unit.get("content_unit_id"),
                                      "archetype": archetype, "effective_chars": chars})
            # Section 层是"契约给的额度"，CU 层是"实际排出去的额度"，差额就是机动额度（§10）。
            # 只有把两者都带下来，守恒检查才分得清"reserve 没花"和"字丢了一层"。
            section_total = _int(section.get("planned_total_chars")) or allocated
            split = section_budget_split(section_total, [{"archetype": row.get("archetype"),
                                                          "prose_chars": (row.get("unified_budget") or {}).get("prose_chars"),
                                                          "structure_chars": (row.get("unified_budget") or {}).get("structure_chars")}
                                                         for row in section.get("content_units") or []])
            reserve = max(0, section_total - allocated)
            sections.append({"section_id": section.get("section_id"),
                             "section_budget": section_total,
                             "allocated_chars": allocated,
                             "reserve_budget": reserve,
                             "content_units": section_units,
                             "budget_split": split})
            volume_total += section_total
            planned_total += section_total
            allocated_total += allocated
            reserve_total += reserve
        volume_rows.append({"volume_id": volume.get("volume_id"), "volume_budget": volume_total,
                            "sections": sections})
    return {
        "schema_version": UNIFIED_BUDGET_VERSION,
        "measure": "unified_effective_chars",
        "document_budget": document,
        "document_budget_from_pages": page_budget_from_pages(
            page_targets, chars_per_technical_page=chars_per_technical_page),
        "volumes": volume_rows,
        "archetype_budgets": [archetype_roll[key] for key in sorted(archetype_roll)],
        "planned_total_chars": planned_total,
        "allocated_total_chars": allocated_total,
        "reserve_total_chars": reserve_total,
        "conservation": conservation_report(document, volume_rows, archetype_roll, allocated_total,
                                            reserve_total),
        "levels": ("Document", "Volume", "Section", "Content Archetype", "Content Unit"),
    }


def conservation_report(document: int, volumes: list[dict[str, Any]],
                        archetype_roll: dict[str, dict[str, Any]],
                        allocated_total: int, reserve_total: int = 0) -> dict[str, Any]:
    """§8：五层逐层求和核对。每一对的差额都要能指到具体层，不能只报"总数差不多"。"""
    volume_total = sum(_int(row.get("volume_budget")) for row in volumes)
    section_total = sum(_int(section.get("section_budget"))
                        for row in volumes for section in row.get("sections") or [])
    section_allocated = sum(_int(section.get("allocated_chars"))
                            or sum(_int(unit.get("effective_chars"))
                                   for unit in section.get("content_units") or [])
                            for row in volumes for section in row.get("sections") or [])
    archetype_total = sum(_int(row.get("effective_chars")) for row in archetype_roll.values())
    unit_total = sum(_int(unit.get("effective_chars"))
                     for row in volumes for section in row.get("sections") or []
                     for unit in section.get("content_units") or [])
    levels = {
        "document_vs_volume_sum": {"document": document, "volume_sum": volume_total,
                                   "delta": volume_total - document},
        "volume_vs_section_sum": {"volume_sum": volume_total, "section_sum": section_total,
                                  "delta": section_total - volume_total},
        # 第四节额度 = 该节 CU 额度 + 该节机动额度；对不上就是钱在某一层凭空多/少了。
        "section_vs_allocated_plus_reserve": {
            "section_sum": section_total,
            "allocated_plus_reserve": archetype_total + reserve_total,
            "delta": archetype_total + reserve_total - section_total},
        "archetype_vs_unit_sum": {"archetype_sum": archetype_total, "unit_sum": unit_total,
                                  "delta": unit_total - archetype_total},
    }
    worst = max((abs(row["delta"]) for row in levels.values()), default=0)
    tolerance = max(1, int(round(max(section_total, unit_total) * 0.005)))
    return {
        "levels": levels,
        "worst_delta_chars": worst,
        "tolerance_chars": tolerance,
        "conserved": worst <= tolerance,
        "allocated_chars": section_allocated or allocated_total,
        "reserve_chars": reserve_total,
        # 守恒是"每层求和相等"，不是"总数看起来差不多"：差额必须指到具体层。
        "broken_levels": [key for key, row in levels.items() if abs(row["delta"]) > tolerance],
    }


def project_page_mix(prose_chars: int, structure_chars: int,
                     *, chars_per_technical_page: float = RENDER_VISIBLE_CHARS_PER_TECHNICAL_PAGE
                     ) -> dict[str, Any]:
    """按"正文/结构两路收益"投影页数，而不是把全部字当同一种东西。

    Run D 的教训正是这两路收益不同，所以结构占比一动，同样的字数就是不一样的页数；
    这里把混合写清楚，比例错了能一眼看出错在哪一路。两路系数自 R41 起取**修复后这条链**
    的实测值（正文 0.8626 / 结构 1.0081，分桶与预算账一致），Run D 修复前的
    1.0025/0.4359 只作历史保留。这一路现在是**交叉核对**，页数判决用文档级直量尺
    `LEDGER_CHARS_PER_TECHNICAL_PAGE` —— 见上面那段为什么。
    """
    prose = max(0, _int(prose_chars))
    structure = max(0, _int(structure_chars))
    total = prose + structure
    visible = prose * RENDER_YIELD_PROSE + structure * RENDER_YIELD_STRUCTURE
    return {
        "ledger_chars": total,
        "projected_render_visible_chars": int(round(visible)),
        "structure_share": round(structure / total, 4) if total else 0.0,
        "projected_technical_effective_pages": int(round(visible / max(1.0, float(chars_per_technical_page)))),
        "basis": "MEASURED_R41_POST_FIX_TWO_ROUTE_YIELD",
    }


def document_budget_for_pages(page_floor: int, page_ceiling: int,
                              *, planning_pages: int = 0,
                              chars_per_technical_page: float = LEDGER_CHARS_PER_TECHNICAL_PAGE
                              ) -> dict[str, Any]:
    """页数区间 → 文档级账本字额度（§4/§13 要的"总预算"就从这里来）。"""
    per_page = float(chars_per_technical_page or LEDGER_CHARS_PER_TECHNICAL_PAGE)
    floor = _int(page_floor)
    ceiling = _int(page_ceiling)
    planning = _int(planning_pages)
    if not planning and (floor or ceiling):
        planning = int(round((floor + ceiling) / 2))
    return {
        "page_floor": floor,
        "page_ceiling": ceiling,
        "planning_pages": planning,
        "chars_per_technical_page": round(per_page, 1),
        "target_chars_min": int(round(floor * per_page)),
        "target_chars_target": int(round(planning * per_page)),
        "target_chars_max": int(round(ceiling * per_page)),
        "basis": PAGE_PROJECTION_BASIS,
    }


def pages_from_ledger_chars(chars: int, *, chars_per_technical_page: float = LEDGER_CHARS_PER_TECHNICAL_PAGE
                            ) -> int:
    return int(round(max(0, _int(chars)) / max(1.0, float(chars_per_technical_page or 1))))


def artifact_chars_from_ledger(chars: int,
                               *, ledger_to_artifact_yield: float = ARTIFACT_CHARS_PER_LEDGER_CHAR_RUN_D
                               ) -> int:
    """账本字 → 装配产物可见字。

    事后 QA 的 `total_effective_chars` 量的是装配产物，规划额度是账本字（含结构 payload）。
    两者直接相比就等于拿一把 0.77 倍的尺子去量一把 1.0 的尺子 —— 那是 Round 3 货币混用
    的同一个错，只是方向相反：会把按新额度写满的文档判成缺字。
    """
    return int(round(max(0, _int(chars)) * float(ledger_to_artifact_yield or 1.0)))


def page_budget_from_pages(page_targets: tuple[int, int] | list[int] | None,
                           *, chars_per_technical_page: float = LEDGER_CHARS_PER_TECHNICAL_PAGE
                           ) -> dict[str, Any]:
    """页数区间 → 账本字区间（预算与门控共用同一条换算，避免两套尺子）。"""
    targets = list(page_targets or [])
    floor_pages = _int(targets[0] if targets else 0)
    ceiling_pages = _int(targets[1] if len(targets) > 1 else 0)
    per_page = float(chars_per_technical_page or LEDGER_CHARS_PER_TECHNICAL_PAGE)
    return {
        "page_floor": floor_pages,
        "page_ceiling": ceiling_pages,
        "chars_per_technical_page": round(per_page, 1),
        "floor_chars": int(round(floor_pages * per_page)),
        "ceiling_chars": int(round(ceiling_pages * per_page)),
        "basis": PAGE_PROJECTION_BASIS,
    }


def classify_page_state(technical_pages: int, page_targets: tuple[int, int] | list[int] | None
                        ) -> str:
    """§21 双边门：下沿/区间/超页三态，小文档不会被读成超页。"""
    targets = list(page_targets or [])
    floor_pages = _int(targets[0] if targets else 0)
    ceiling_pages = _int(targets[1] if len(targets) > 1 else 0)
    pages = _int(technical_pages)
    if ceiling_pages and pages > ceiling_pages:
        return "OVERSHOOT"
    if floor_pages and pages < floor_pages:
        return "UNDER_LENGTH"
    return "TARGET_RANGE"


# ------------------------------------------------ §11 章节密度画像 --
# 不同章节天然不同密度：讲理念与项目理解的节要靠叙述撑，讲组织/质量/应急的节靠结构撑，
# 表单附件型只许限量出现（§11 最后一行、§63 "低信息表宁可合并转删"）。
# 词表只用于**归类**，不改既有 choose_archetype 的选择顺序，避免把目录逻辑一起改掉。
DENSITY_PROFILE_RULES = (
    ("NARRATIVE_HEAVY", ("项目理解", "服务构想", "服务理念", "总体思路", "需求理解", "重难点",
                         "运营思路", "管理思路", "服务定位", "持续改进", "服务承诺")),
    ("STRUCTURED_HEAVY", ("组织架构", "职责", "责任", "质量控制", "品质", "考核", "应急", "预案",
                          "风险", "指标", "KPI", "台账", "记录")),
    ("FORM_STRICT", ("表单", "记录表", "附件", "清单模板", "填写")),
)
DENSITY_PROFILE_TARGETS = {
    # narrative_share = 该节结构额度之外留给纯叙述 CU 的比例；
    # form_share_cap = 表单类 CU 在该节里的数量上限比例（§11 "严格限量"）。
    "NARRATIVE_HEAVY": {"narrative_share": 0.60, "form_share_cap": 0.10, "table_share_cap": 0.30},
    "BALANCED": {"narrative_share": 0.35, "form_share_cap": 0.15, "table_share_cap": 0.45},
    "STRUCTURED_HEAVY": {"narrative_share": 0.20, "form_share_cap": 0.20, "table_share_cap": 0.55},
    "FORM_STRICT": {"narrative_share": 0.30, "form_share_cap": 0.25, "table_share_cap": 0.40},
}
FORM_LIKE_ARCHETYPES = ("FORM_TEMPLATE",)


def density_profile_for(section: dict[str, Any] | None) -> str:
    text = " ".join([str((section or {}).get("section_title") or ""),
                     str((section or {}).get("purpose") or ""),
                     *[str(item) for item in ((section or {}).get("must_cover") or [])]])
    for profile, keywords in DENSITY_PROFILE_RULES:
        if any(keyword in text for keyword in keywords):
            return profile
    return "BALANCED"


def rebalance_archetype_mix(archetypes: list[Any], *, profile: str) -> dict[str, Any]:
    """按密度画像重排一条节内的 Archetype 组合（§11）。

    只做两件事：把超出 `form_share_cap` 的表单换成叙述、按 `narrative_share`
    把多余的结构 CU 换成叙述；反向不补（不凭空造表），因为多造结构正是上一轮撑页的路径。
    """
    names = [str(key or "NARRATIVE") for key in (archetypes or [])]
    targets = DENSITY_PROFILE_TARGETS.get(profile, DENSITY_PROFILE_TARGETS["BALANCED"])
    count = len(names)
    changed: list[dict[str, Any]] = []
    if not count:
        return {"archetypes": names, "profile": profile, "changes": changed,
                "structure_share": 0.0, "narrative_share": 0.0}
    form_cap = max(0, int(round(count * targets["form_share_cap"])))
    form_seen = 0
    for index, name in enumerate(names):
        if name in FORM_LIKE_ARCHETYPES:
            form_seen += 1
            if form_seen > form_cap:
                names[index] = "NARRATIVE"
                changed.append({"index": index + 1, "from": name, "to": "NARRATIVE",
                                "reason": "FORM_STRICT_SHARE_CAP"})
    structured = sum(1 for name in names if is_structured(name))
    wanted_structured = int(round(count * (1.0 - targets["narrative_share"])))
    # 从后往前把多余的结构 CU 换成叙述：靠后的 CU 通常是同一主题的第二次展开。
    for index in range(count - 1, -1, -1):
        if structured <= wanted_structured:
            break
        if is_structured(names[index]):
            changed.append({"index": index + 1, "from": names[index], "to": "NARRATIVE",
                            "reason": "DENSITY_PROFILE_NARRATIVE_SHARE"})
            names[index] = "NARRATIVE"
            structured -= 1
    return {"archetypes": names, "profile": profile, "changes": changed,
            "structure_share": round(structured / count, 4),
            "narrative_share": round((count - structured) / count, 4)}


# ------------------------------------------------------ §12 语义重复治理 --
_DUP_STRIP = re.compile(r"[\s，。；：、,.!?！？（）()【】\[\]「”“\-—·]+")


def _shingles(text: str, size: int = 12) -> set[str]:
    cleaned = _DUP_STRIP.sub("", str(text or ""))
    if len(cleaned) < size:
        return set()
    return {cleaned[index:index + size] for index in range(len(cleaned) - size + 1)}


def prose_table_duplication(blocks: list[tuple[str, str]], *, min_share: float = 0.35) -> dict[str, Any]:
    """找"正文已经把这件事讲完、表格又整段重讲一遍"的对（§12）。

    允许的是"正文讲原则、表格给操作要点"，所以判定单位是 12 字连续片段的重叠率：
    同一条需求在正文与表体里都出现成片的 12 字重叠，才算重复表达；
    共享一两个术语（"巡检""台账"）远不到这条线。
    """
    rows: list[dict[str, Any]] = []
    prose = [(key, text) for key, text in blocks if str(key).startswith("prose")]
    structure = [(key, text) for key, text in blocks if not str(key).startswith("prose")]
    for prose_key, prose_text in prose:
        prose_set = _shingles(prose_text)
        if not prose_set:
            continue
        for structure_key, structure_text in structure:
            structure_set = _shingles(structure_text)
            if not structure_set:
                continue
            overlap = len(prose_set & structure_set) / min(len(prose_set), len(structure_set))
            if overlap >= min_share:
                rows.append({"prose": prose_key, "structure": structure_key,
                             "overlap_share": round(overlap, 3),
                             "duplicated_chars": int(round(min_share * len(_DUP_STRIP.sub("", structure_text))))})
    return {
        "schema_version": UNIFIED_BUDGET_VERSION,
        "gate": "SEMANTIC_DUPLICATION",
        "pairs": sorted(rows, key=lambda row: -row["overlap_share"])[:40],
        "duplicate_pair_count": len(rows),
        "redundant_chars": sum(row["duplicated_chars"] for row in rows),
        "overlap_share_min": min_share,
        "policy": "PROSE_STATES_PRINCIPLE_TABLE_CARRIES_OPERATIONAL_DETAIL",
    }


# ------------------------------------------------ §13 生成前预算门 --
def pre_generation_budget_gate(tree: dict[str, Any], *, page_targets: tuple[int, int] | list[int] | None,
                               chars_per_technical_page: float = LEDGER_CHARS_PER_TECHNICAL_PAGE
                               ) -> dict[str, Any]:
    """规划总量投到页数上，超出上限就不得进入正式生成（§13）。

    这一道门排在 Provider 调用之前，所以它只读规划产物、不碰任何凭证。
    被挡住时给出**摊派需求**（每个 Section 该降到多少字），而不是让人对着一个
    "BLOCKED" 自己猜怎么改。
    """
    planned = _int((tree or {}).get("planned_total_chars"))
    allocated = _int((tree or {}).get("allocated_total_chars"))
    reserve = _int((tree or {}).get("reserve_total_chars"))
    ruler = page_budget_from_pages(page_targets, chars_per_technical_page=chars_per_technical_page)
    prose_total = sum(_int(row.get("prose_chars")) for row in (tree or {}).get("archetype_budgets") or [])
    structure_total = sum(_int(row.get("structure_chars")) for row in (tree or {}).get("archetype_budgets") or [])
    allocated_total = (prose_total + structure_total) or allocated
    reserve_total = max(0, planned - allocated_total) or _int((tree or {}).get("reserve_total_chars"))
    ruler_chars = ruler["chars_per_technical_page"]
    # 判决走**文档级直量尺**（R41 修复后 LEVEL-4 实测 930 账本字/技术页）：已排额度 → 中心
    # 投影，机动额度全部花掉 → 最坏投影，两条同一把尺，差值就是"机动额度还能长出几页"。
    # 两路收益模型降级为交叉核对：它要求"计划的结构占比 + 正文重复率"都对得上标定那次，
    # 70/150 页档这两条都不成立（实测两把尺在那里分叉 9～14%），分叉本身就是形状变了的告警。
    # 上一轮把它升成判决依据（§7 那次）的前提是"结构占比落不到地上"是 bug —— 修好之后
    # 重新量，才发现它只在 300 页档准，细文档上系统性偏低，所以判决回到直量尺。
    projected_pages = pages_from_ledger_chars(allocated_total, chars_per_technical_page=ruler_chars)
    worst_pages = pages_from_ledger_chars(allocated_total + reserve_total,
                                          chars_per_technical_page=ruler_chars)
    mix = project_page_mix(prose_total, structure_total)
    two_route_pages = int(mix["projected_technical_effective_pages"])
    state = classify_page_state(projected_pages, page_targets)
    worst_state = classify_page_state(worst_pages, page_targets)
    ceiling_chars = _int(ruler.get("ceiling_chars"))
    floor_chars = _int(ruler.get("floor_chars"))
    sections: list[dict[str, Any]] = []
    for volume in (tree or {}).get("volumes") or []:
        for section in volume.get("sections") or []:
            sections.append({"section_id": section.get("section_id"),
                             "volume_id": volume.get("volume_id"),
                             "planned_chars": _int(section.get("section_budget"))})
    reallocation: list[dict[str, Any]] = []
    if worst_state == "OVERSHOOT" and worst_pages and ruler["page_ceiling"]:
        # 摊派系数用"页数比"而不是"字数比上限额度"：投影走的是两路收益，
        # 上限字数走的是混合尺子，两者混在一个算式里就是又造一套口径。
        factor = float(ruler["page_ceiling"]) / float(worst_pages)
        for row in sections:
            target = int(round(row["planned_chars"] * factor))
            reallocation.append({"section_id": row["section_id"], "volume_id": row["volume_id"],
                                 "planned_chars": row["planned_chars"], "allowed_chars": target,
                                 "reduction_ratio": round(1.0 - factor, 4)})
    deficit = max(0, floor_chars - planned) if floor_chars else 0
    conservation_ok = bool((tree or {}).get("conservation", {}).get("conserved"))
    # 页数判定看"已排额度"，能不能进生成还要看"把机动额度花完的最坏情况"：
    # 前者决定会不会写不满，后者决定会不会又跑出 Run D 那种 395 页。
    status = ("BLOCKED_OVER_CEILING" if worst_state == "OVERSHOOT"
              else ("RAISE_PLAN_UNDER_FLOOR" if state == "UNDER_LENGTH" else "PASS"))
    blocked_reasons: list[str] = []
    if status != "PASS":
        blocked_reasons.append(status)
    if not conservation_ok:
        blocked_reasons.append("PLAN_NOT_CONSERVED")
    return {
        "schema_version": UNIFIED_BUDGET_VERSION,
        "gate": "PRE_GENERATION_BUDGET_GATE",
        "status": status,
        "may_enter_generation": status == "PASS" and conservation_ok,
        "blocked_reasons": blocked_reasons,
        "planned_total_chars": planned,
        "allocated_total_chars": allocated_total,
        "reserve_total_chars": reserve_total,
        "projected_technical_effective_pages": projected_pages,
        "worst_case_technical_effective_pages": worst_pages,
        "worst_case_page_state": worst_state,
        "planned_prose_chars": prose_total,
        "planned_structure_chars": structure_total,
        "planned_structure_share": mix.get("structure_share"),
        "projection_method": PAGE_PROJECTION_BASIS,
        "projected_render_visible_chars": int(round(allocated_total
                                                    * LEDGER_TO_ARTIFACT_VISIBLE_YIELD)),
        "ledger_to_artifact_visible_yield": LEDGER_TO_ARTIFACT_VISIBLE_YIELD,
        # 交叉核对：同一份计划拿两路收益模型会投影成多少页。两把尺只在结构占比/正文重复率
        # 与标定一致时重合；分叉超过 15% 就说明这份计划的形状和标定那次不是一回事。
        "two_route_cross_check": {
            "basis": mix.get("basis"),
            "structure_share": mix.get("structure_share"),
            "projected_technical_effective_pages": two_route_pages,
            "projected_render_visible_chars": mix.get("projected_render_visible_chars"),
            "delta_vs_measured_ruler_pages": two_route_pages - projected_pages,
        },
        "page_floor": ruler["page_floor"],
        "page_ceiling": ruler["page_ceiling"],
        "page_state": state,
        "chars_per_technical_page": ruler["chars_per_technical_page"],
        "projection_basis": ruler["basis"],
        "ceiling_chars": ceiling_chars,
        "floor_chars": floor_chars,
        "headroom_chars": max(0, ceiling_chars - planned),
        "headroom_pages": max(0, _int(ruler["page_ceiling"]) - worst_pages),
        "under_floor_chars": deficit,
        # 贴到上限就报出来：投影本身有 ±15% 的组合不确定性，剩 3% 余量不等于安全。
        "at_ceiling_margin": bool(ruler["page_ceiling"]
                                  and worst_pages > ruler["page_ceiling"] * CEILING_SAFETY_MARGIN),
        "ceiling_safety_margin": CEILING_SAFETY_MARGIN,
        "conservation": dict((tree or {}).get("conservation") or {}),
        # 规划层就不守恒时，页数投影无论怎么算都不可信：单独报出来，不并进 status。
        "conservation_ok": conservation_ok,
        "reallocation_demand": reallocation,
        "provider_calls_made": 0,
    }


# ------------------------------------------------------ §14/§15 生成中账 --
def empty_midrun_state(tree: dict[str, Any] | None) -> dict[str, Any]:
    sections: dict[str, dict[str, Any]] = {}
    document_target = 0
    for volume in (tree or {}).get("volumes") or []:
        for section in volume.get("sections") or []:
            split = section.get("budget_split") or {}
            document_target += _int(section.get("section_budget"))
            sections[str(section.get("section_id"))] = {
                "section_id": section.get("section_id"),
                "volume_id": volume.get("volume_id"),
                "section_budget": _int(section.get("section_budget")),
                "narrative_budget": _int(split.get("narrative_budget")),
                "structured_budget": _int(split.get("structured_budget")),
                "reserve_budget": _int(split.get("reserve_budget")),
                "used_narrative_chars": 0,
                "used_table_chars": 0,
                "remaining_narrative_chars": _int(split.get("narrative_budget")),
                "remaining_structured_chars": _int(split.get("structured_budget")),
                "remaining_section_chars": _int(section.get("section_budget")),
                "units_arrived": 0,
                "units_planned": len(section.get("content_units") or []),
            }
    return {
        "schema_version": UNIFIED_BUDGET_VERSION,
        "document_target_chars": document_target,
        "document_used_chars": 0,
        "document_remaining_chars": document_target,
        "sections": sections,
        "batches_observed": 0,
        "adjustments": [],
    }


def observe_batch(state: dict[str, Any], records: list[dict[str, Any]],
                  *, weights: dict[str, float] | None = None,
                  table_governance: dict[str, Any] | None = None) -> dict[str, Any]:
    """每个 Batch/CU 回来就更新四条余量，并就地决定"下一批降不降"（§14）。

    Round 3 是等 27 节全生成完才发现全部超长 —— 这里把发现点提前到第一批之后。
    `weights` 是 Section 权重（评分权重/优先级），只影响 §15 的再平衡方向，
    不影响记账本身。

    §64：`table_governance` 是装配端算好的"这批 CU 的表经过表格治理之后还剩多少字"
    （`longform/cu_assembly.table_governance_accounting` 的输出，按 section_id 索引）。
    被合并掉、被转删的那些字**永远不会渲染**，却已经按 payload 记进 `used_table_chars`；
    不回填，下一批的降额度判决就是在为一个不存在的表格压正文。治理账缺失的节按
    `NOT_OBSERVED` 记账，不猜一个系数。
    """
    active = state or empty_midrun_state(None)
    sections = {str(key): dict(value) for key, value in (active.get("sections") or {}).items()}
    used_by_archetype: dict[str, int] = {}
    for record in records or []:
        sid = str(record.get("section_id") or record.get("parent_section_id") or "")
        row = sections.setdefault(sid, {
            "section_id": sid, "volume_id": record.get("volume_id"), "section_budget": 0,
            "narrative_budget": 0, "structured_budget": 0, "reserve_budget": 0,
            "used_narrative_chars": 0, "used_table_chars": 0, "remaining_narrative_chars": 0,
            "remaining_structured_chars": 0, "remaining_section_chars": 0,
            "units_arrived": 0, "units_planned": 0})
        prose = max(0, _int(record.get("prose_chars")))
        structure = max(0, _int(record.get("structure_chars")))
        if not (prose or structure):
            prose = max(0, _int(record.get("actual_chars")))
        row["used_narrative_chars"] += prose
        row["used_table_chars"] += structure
        row["units_arrived"] += 1
        row["remaining_narrative_chars"] = row["narrative_budget"] - row["used_narrative_chars"]
        row["remaining_structured_chars"] = row["structured_budget"] - row["used_table_chars"]
        row["remaining_section_chars"] = (row["section_budget"] - row["used_narrative_chars"]
                                          - row["used_table_chars"])
        archetype = str(record.get("archetype") or "NARRATIVE")
        used_by_archetype[archetype] = used_by_archetype.get(archetype, 0) + prose + structure
    # §64：表格治理回填 —— 被合并/转删的那部分字不会渲染，不能继续算已用结构额度。
    governance_notes: dict[str, Any] = {}
    for sid, account in sorted((table_governance or {}).items()):
        row = sections.get(str(sid))
        if not isinstance(account, dict) or not isinstance(row, dict):
            continue
        charged = _int(account.get("structure_chars_charged"))
        rendered = _int(account.get("structure_chars_governed"))
        reclaim = max(0, charged - rendered)
        applied = min(reclaim, _int(row.get("used_table_chars")))
        row["used_table_chars"] = _int(row.get("used_table_chars")) - applied
        row["remaining_structured_chars"] = row["structured_budget"] - row["used_table_chars"]
        row["remaining_section_chars"] = (row["section_budget"] - row["used_narrative_chars"]
                                          - row["used_table_chars"])
        note = {
            "structure_chars_charged": charged,
            "structure_chars_governed": rendered,
            "reclaimed_chars": reclaim,
            # 回填只允许往回收，不允许把别的 archetype（SOP/清单）的字挪给表格，
            # 所以 applied 与 reclaim 不等时差额必须可见，不能默默吞掉。
            "applied_chars": applied,
            "unapplied_chars": reclaim - applied,
            "tables_merged": _int(account.get("tables_merged_groups")),
            "tables_dropped": _int(account.get("tables_dropped")),
            "low_information_dropped": _int(account.get("low_information_dropped")),
            "basis": "longform/cu_assembly.table_governance_accounting",
        }
        row["table_governance"] = note
        governance_notes[str(sid)] = note
    document_used = sum(row["used_narrative_chars"] + row["used_table_chars"] for row in sections.values())
    # 局部超长 → 该节下一批自动降额度：用"已用/已规划单元数"外推，而不是等整节跑完。
    adjustments: list[dict[str, Any]] = []
    for sid, row in sorted(sections.items()):
        arrived = row["units_arrived"]
        planned_units = row["units_planned"]
        if not arrived or not planned_units or not row["section_budget"]:
            continue
        spent_share = (row["used_narrative_chars"] + row["used_table_chars"]) / row["section_budget"]
        expected_share = arrived / planned_units
        # 0.15 的容差是给"一个节里 CU 字数天然不均"留的，再大就是真超写。
        if spent_share > expected_share + 0.15:
            remaining_units = max(1, planned_units - arrived)
            remaining_chars = max(0, row["section_budget"] - row["used_narrative_chars"] - row["used_table_chars"])
            per_unit_now = (row["used_narrative_chars"] + row["used_table_chars"]) / arrived
            cap = max(240, int(round(remaining_chars / remaining_units)))
            adjustments.append({
                "section_id": sid,
                "status": "LOCAL_OVER_RUN",
                "spent_share": round(spent_share, 3),
                "expected_share": round(expected_share, 3),
                "observed_chars_per_unit": int(round(per_unit_now)),
                "next_batch_chars_per_unit_cap": cap,
                "factor": round(cap / per_unit_now, 3) if per_unit_now else 1.0,
                "reserve_dip_allowed": cap > per_unit_now and row["reserve_budget"] > 0,
            })
    over_sections = [row["section_id"] for row in sections.values()
                     if row["remaining_section_chars"] < -row["reserve_budget"]]
    under_sections = [row for row in sections.values()
                      if 0 < row["units_planned"] <= row["units_arrived"]
                      and row["remaining_section_chars"] > 0.10 * max(1, row["section_budget"])]
    return {
        **active,
        "sections": sections,
        "document_used_chars": document_used,
        "document_remaining_chars": max(0, _int(active.get("document_target_chars")) - document_used),
        "batches_observed": _int(active.get("batches_observed")) + (1 if records else 0),
        "used_by_archetype": used_by_archetype,
        # §64：文档级的治理回填总量。没有这行账，读者只能从 27 个节里自己加，
        # 而"表被转删了多少字"正是这一轮要能对质的数。
        "table_governance": {
            "sections_observed": len(governance_notes),
            "sections_total": len(sections),
            "status": "OBSERVED" if governance_notes else "NOT_OBSERVED",
            "structure_chars_charged": sum(row["structure_chars_charged"]
                                           for row in governance_notes.values()),
            "structure_chars_governed": sum(row["structure_chars_governed"]
                                            for row in governance_notes.values()),
            "reclaimed_chars": sum(row["reclaimed_chars"] for row in governance_notes.values()),
            "applied_chars": sum(row["applied_chars"] for row in governance_notes.values()),
            "unapplied_chars": sum(row["unapplied_chars"] for row in governance_notes.values()),
            "tables_merged": sum(row["tables_merged"] for row in governance_notes.values()),
            "tables_dropped": sum(row["tables_dropped"] for row in governance_notes.values()),
            # §63/§64：转删是这轮新加的那条腿，文档级总账里必须看得到它自己贡献了多少，
            # 不能只混在 `tables_dropped`（那还包括老的"整张无有效内容"删除）。
            "low_information_dropped": sum(row["low_information_dropped"]
                                           for row in governance_notes.values()),
            "per_section": governance_notes,
        },
        "document_run_rate": round(document_used / max(1, _int(active.get("document_target_chars"))), 4),
        "next_batch_adjustments": adjustments,
        # §15：超预算的节必须先冻结，才谈得上把额度挪给高权重节。
        "frozen_sections": sorted(over_sections),
        "donor_candidates": [
            {"section_id": row["section_id"], "spare_chars": row["remaining_section_chars"],
             "weight": float((weights or {}).get(str(row["section_id"]), 0.0))}
            for row in sorted(under_sections, key=lambda item: -item["remaining_section_chars"])],
        "provider_calls_made": 0,
    }


def mid_run_reallocation(state: dict[str, Any], *, demand: list[dict[str, Any]],
                         max_move_ratio: float = 0.5) -> dict[str, Any]:
    """§15 生成中再平衡：低缺口节把富余额度让给高权重节，超预算节禁止继续扩写。

    `max_move_ratio` 限制一节最多让出自己富余的一部分，避免把某节掏空成短节；
    每一次搬运都记 before/after，事后可对账。
    """
    sections = {str(key): dict(value) for key, value in ((state or {}).get("sections") or {}).items()}
    frozen = set(str(key) for key in (state or {}).get("frozen_sections") or [])
    moves: list[dict[str, Any]] = []
    donors = sorted((row for sid, row in sections.items()
                     if sid not in frozen and row.get("remaining_section_chars", 0) > 0),
                    key=lambda row: -row["remaining_section_chars"])
    for item in demand or []:
        sid = str(item.get("section_id") or "")
        need = max(0, _int(item.get("needed_chars")))
        if not need or sid in frozen:
            continue
        granted = 0
        for donor in donors:
            if donor["section_id"] == sid or granted >= need:
                continue
            available = int(round(donor["remaining_section_chars"] * max_move_ratio))
            give = min(available, need - granted)
            if give <= 0:
                continue
            donor["remaining_section_chars"] -= give
            donor["section_budget"] -= give
            target = sections.setdefault(sid, dict(donor, section_id=sid))
            target["remaining_section_chars"] = target.get("remaining_section_chars", 0) + give
            target["section_budget"] = target.get("section_budget", 0) + give
            granted += give
            moves.append({"from_section": donor["section_id"], "to_section": sid, "chars": give,
                          "reason": str(item.get("reason") or "HIGH_WEIGHT_SCORING_GAP"),
                          "donor_remaining_after": donor["remaining_section_chars"]})
    return {
        "schema_version": UNIFIED_BUDGET_VERSION,
        "gate": "MID_RUN_REALLOCATION",
        "sections": sections,
        "moves": moves,
        "moved_chars": sum(row["chars"] for row in moves),
        "frozen_sections": sorted(frozen),
        "unmet_demand_chars": sum(max(0, _int(item.get("needed_chars"))
                                      - sum(row["chars"] for row in moves if row["to_section"] == str(item.get("section_id"))))
                                  for item in demand or []),
        "provider_calls_made": 0,
    }
