# -*- coding: utf-8 -*-
"""MMF-007C Round 4.3 §4–§8：Technical Scoring 的唯一 canonical Authority。

为什么单独一个模块：生成入口（`longform.orchestrator`）与终态落盘
（`longform.run_state`）必须消费**同一个**评分校验结果（任务书 §11）。放成
orchestrator 的私有函数的话，run_state 要 import orchestrator 才用得上，而
orchestrator 已经在 import run_state —— 那会成环，然后就是"两边各写一套"，
正是 §9 抓到的那个"入口 BLOCK、finalizer PASS"的双真值。本模块只 import
`planning.*`，两边单向依赖它，物理上不给第二套判据留位置。

Authority 关系（§4/§5，本轮的核心）：

  canonical 输入 = `09_scoring_matrix.json`（矩阵）+ `08_document_hierarchy.json`
  （计划清单，用来核对引用）+ `tender/requirement_pack.json`（招标抽取，用来算分母）。
  `pre_generation_scoring_mapping_gate.json` 与矩阵里嵌的那份门结论都只是**派生的声称**：
  它们可以解释"上一次是谁把门判成什么样的"，永远不能替代对 canonical 输入的重新验证。

因此本模块给出的唯一布尔 `may_enter_generation` 只在三种情形为真：

  PASS                    —— canonical 输入齐全、引用完整性按**当前**计划清单重算通过，
                             且**适用主题宇宙 == 已映射主题宇宙**（Round 4.4 §2）；
  NOT_APPLICABLE          —— 分母由事实算出等于 0（没有适用技术实质评分主题）；
  SCORING_MAPPING_NOT_PLANNED —— 这次入口根本没有 canonical 输入可言（无规划、无抽取）。

其余一律 False，且每一种 False 都有自己的名字：MISSING / UNREADABLE / MALFORMED /
GATE_NOT_EMBEDDED / GATE_INCOMPLETE / GATE_UNKNOWN_VERDICT / GATE_STALE /
GATE_CONTRADICTORY / FALSE_NOT_APPLICABLE / PLAN_UNVERIFIABLE /
BLOCKED_SCORING_MAPPING_INCOMPLETE / TECHNICAL_SCORING_LEAK /
TOPIC_COVERAGE_INCOMPLETE / TOPIC_NOT_IN_UNIVERSE / TOPIC_UNIVERSE_CONTRADICTORY /
TOPIC_UNIVERSE_UNVERIFIABLE / TOPIC_UNIVERSE_PLANNING_MISMATCH。

Round 4.4 §2/§3 补的那一半：引用合法只证明"已经存在的记录落点是对的"，不证明
"该映射的主题都映射了"。所以 PASS 还要求 `expected == mapped`，且**按主题身份集合**比，
不按条数比（两条记录映射同一个主题，冒充不了两个主题的覆盖）。主题身份、别名与去重
一律复用规划链那把尺（`topic_key` / `topic_similarity` / `build_scoring_taxonomy`），
这里不另起第二套归一化。§4 的三层输入（抽取 / 矩阵 / 记录）必须真的被消费：
期望宇宙按既有 precedence 取"过了 Technical-Only 闸的矩阵行"，抽取层比它宽的那些主题
必须能被现有分类器解释成"判成了商务/计价/程序…"，解释不了的分歧按矛盾处理 —— 不猜。

§7 的另一半同样重要：合法 N/A 与合法 Clean 都不能被误杀。N/A 的凭据是**算出来的 0**，
不是"某个文件写着 NOT_APPLICABLE"，也不是"拿不到清单"；反过来，完整覆盖 + 引用齐 +
QA PASS + 实测页在带内，Clean 必须仍然拿得到（本轮为此专门留了一条真规划链正例）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from planning.hierarchy import _planned_section_ids, _planned_unit_ids
from planning.scoring_taxonomy import (
    MIN_MERGE_TOPIC_CHARS,
    SCORING_MAPPING_GATE_FILE,
    TOPIC_MERGE_SIMILARITY,
    build_scoring_taxonomy,
    is_non_response_row,
    pre_generation_scoring_mapping_gate,
    topic_key,
    topic_similarity,
)

# canonical 输入的名字只在这里写一次。
CANONICAL_MATRIX_FILE = "09_scoring_matrix.json"
CANONICAL_PLAN_FILE = "08_document_hierarchy.json"
CANONICAL_EXTRACTION_FILES = ("tender/requirement_pack.json", "requirement_pack.json")
EMBEDDED_GATE_KEY = "pre_generation_scoring_mapping_gate"

# 「这一单该不该有评分映射」的判据来源：**规划跑过**（分层树在场）就够了，
# 与"矩阵文件在不在"无关 —— 反过来用"文件不在"推"没有评分主题"就是 fail-open。
SCORING_PLANNING_EVIDENCE_FILES = (CANONICAL_PLAN_FILE, CANONICAL_MATRIX_FILE,
                                   SCORING_MAPPING_GATE_FILE)
SCORING_GATE_VERDICTS = {"PASS", "NOT_APPLICABLE", "BLOCKED_SCORING_MAPPING_INCOMPLETE",
                         "TECHNICAL_SCORING_LEAK"}
SCORING_GATE_REQUIRED_FIELDS = ("status", "may_enter_generation", "substantive_topic_total",
                                "mapped_total", "unmapped_total")
# 允许进生成、也允许自称干净完成的结论。注意这里没有"派生文件写了 PASS 所以放行"这一档。
SCORING_AUTHORITY_PERMITTED = {"PASS", "NOT_APPLICABLE", "SCORING_MAPPING_NOT_PLANNED"}

# 挡住 COMPLETED_CLEAN 时写给终态的原因码（任务书 §10 要求逐档点名，不许糊成一个
# "SCORING_FAILED"）。这张表在 Authority 这一侧，不在 finalizer 那一侧 —— 落盘端只照抄，
# 不自己翻译（§11：不建第二套判据）。
SCORING_AUTHORITY_CLEAN_BLOCKERS = {
    "SCORING_MAPPING_ARTIFACT_MISSING": "SCORING_AUTHORITY_MISSING",
    "SCORING_MAPPING_ARTIFACT_UNREADABLE": "SCORING_AUTHORITY_UNREADABLE",
    "SCORING_MAPPING_ARTIFACT_MALFORMED": "SCORING_AUTHORITY_MALFORMED",
    "SCORING_MAPPING_GATE_NOT_EMBEDDED": "SCORING_AUTHORITY_INCOMPLETE",
    "SCORING_MAPPING_GATE_INCOMPLETE": "SCORING_AUTHORITY_INCOMPLETE",
    "SCORING_MAPPING_GATE_MALFORMED": "SCORING_AUTHORITY_MALFORMED",
    "SCORING_MAPPING_GATE_UNKNOWN_VERDICT": "SCORING_AUTHORITY_UNKNOWN",
    "SCORING_MAPPING_GATE_STALE": "SCORING_AUTHORITY_STALE",
    "SCORING_MAPPING_GATE_CONTRADICTORY": "SCORING_AUTHORITY_CONTRADICTORY",
    "SCORING_MAPPING_FALSE_NOT_APPLICABLE": "SCORING_AUTHORITY_FALSE_NOT_APPLICABLE",
    "SCORING_MAPPING_RECORDS_NOT_PERSISTED": "SCORING_AUTHORITY_MISSING",
    "SCORING_MAPPING_PLAN_UNVERIFIABLE": "SCORING_AUTHORITY_MISSING",
    "BLOCKED_SCORING_MAPPING_INCOMPLETE": "SCORING_AUTHORITY_INVALID_REFERENCE",
    "TECHNICAL_SCORING_LEAK": "SCORING_AUTHORITY_BLOCKED",
    # Round 4.4 §2/§3：主题覆盖完整性。名字各自点名一件事，不许糊成 "SCORING_FAILED"。
    "SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE": "SCORING_AUTHORITY_TOPIC_COVERAGE_INCOMPLETE",
    "SCORING_MAPPING_TOPIC_NOT_IN_UNIVERSE": "SCORING_AUTHORITY_TOPIC_NOT_IN_UNIVERSE",
    "SCORING_MAPPING_TOPIC_UNIVERSE_CONTRADICTORY": "SCORING_AUTHORITY_TOPIC_UNIVERSE_CONTRADICTORY",
    "SCORING_MAPPING_TOPIC_UNIVERSE_UNVERIFIABLE": "SCORING_AUTHORITY_TOPIC_UNIVERSE_UNVERIFIABLE",
    # Round 4.5：一条记录必须绑定到**恰好一个** canonical 主题身份才许参与覆盖判定。
    "SCORING_MAPPING_TOPIC_IDENTITY_AMBIGUOUS": "SCORING_AUTHORITY_TOPIC_IDENTITY_AMBIGUOUS",
    "SCORING_MAPPING_TOPIC_IDENTITY_MISMATCH": "SCORING_AUTHORITY_TOPIC_IDENTITY_MISMATCH",
    # MMF008A4-A1：Authority 过图现算的宇宙与矩阵自己落盘的 canonical 身份集逐身份对不上。
    "SCORING_MAPPING_TOPIC_UNIVERSE_PLANNING_MISMATCH":
        "SCORING_AUTHORITY_TOPIC_UNIVERSE_PLANNING_MISMATCH",
}
SCORING_AUTHORITY_UNKNOWN_BLOCKER = "SCORING_AUTHORITY_UNKNOWN"

MATRIX_PRESENT = "PRESENT"
MATRIX_MISSING = "MISSING"
MATRIX_UNREADABLE = "UNREADABLE"
MATRIX_MALFORMED = "MALFORMED"


def _load(path: Path) -> tuple[Any, str]:
    """读一份 JSON，并把"为什么没读到"具名返回 —— 不许用 `{}` 冒充"没有这件事"。"""
    if not path.is_file():
        return None, MATRIX_MISSING
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), ""
    except (OSError, ValueError, TypeError):
        return None, MATRIX_UNREADABLE


def _load_object(path: Path) -> tuple[dict[str, Any], str]:
    value, state = _load(path)
    if state:
        return {}, state
    if not isinstance(value, dict):
        return {}, MATRIX_MALFORMED
    return value, ""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "UNREADABLE"


def _header_shaped_record(row: dict[str, Any]) -> bool:
    """表尾合计/总计/小计/总分。旧矩阵可能把它记成技术主题，对账时不占宇宙名额。"""
    return bool(is_non_response_row(
        row.get("label") or row.get("topic") or row.get("display_topic") or row.get("scoring_topic") or ""))


def _topic_identity(row: dict[str, Any]) -> str:
    """一个 **canonical 宇宙成员**（矩阵行 / 分类器现算出的记录 / 抽取项）的主题身份。

    §3 明写不要另建第二套主题归一化，也不要拿原始文本直接比。规划链写进
    `canonical_scoring_records` 的记录本身就带 `topic_key`；老工件或手写矩阵没带时，
    用同一个 `topic_key()` 从主题名/标签现算 —— 同一个函数，不是第二套规则。

    **这个函数只回答"宇宙里有哪几个身份"，不回答"一条落盘记录声称自己是谁"**：
    后者若先信 stored `topic_key`，正是 Round 4.5 要关掉的那道伪造
    （`record.topic = X` 配 `record.topic_key = A`，X 就冒充了 A）。记录侧走
    `_bind_record_identity()`，身份只从记录自己的主题文本现算。
    """
    stored = str(row.get("topic_key") or row.get("scoring_topic_key") or "")
    if stored:
        return stored
    return topic_key(row.get("topic") or row.get("label") or "")


def _same_topic_identity(left: str, right: str) -> bool:
    """两个主题身份是不是同一件事：同一个 key，或过 canonical 去重阈值。

    度量与阈值都从规划链引（`topic_similarity` / `TOPIC_MERGE_SIMILARITY` /
    `MIN_MERGE_TOPIC_CHARS`），本轮一个字都没调 —— 改的是**这个判定被允许用在哪**：
    它只回答"这段文本是哪个主题"，不再回答"这些主题被覆盖了没"。
    """
    if not left or not right:
        return False
    if left == right:
        return True
    return (topic_similarity(left, right) >= TOPIC_MERGE_SIMILARITY
            and min(len(left), len(right)) >= MIN_MERGE_TOPIC_CHARS)


def _topic_covered(key: str, expected: set[str]) -> bool:
    """宇宙之间的归属判定（§4 三层对账用）。落盘记录的覆盖不走这里。"""
    return any(_same_topic_identity(key, other) for other in expected)


# 一条记录带来的**身份承载**主题文本字段。Round 4.6 §3 的字段语义契约：
# `label` 是原始全名，`topic` / `scoring_topic` 是**不截断**的 canonical 主题文本
# （`scoring_taxonomy.canonical_topic_text`），三者与 `topic_key` 同源，所以同一条记录
# 不该再出现两套互相矛盾的主题文本。展示用的截断形叫 `display_topic`
# （`scoring_taxonomy.display_topic` = 身份文本的前缀），**不在身份线索里** ——
# 让 Authority 去猜"这段大概是被截断的"就是 §3 明令禁止的那条路。
RECORD_TOPIC_TEXT_FIELDS = ("label", "topic", "scoring_topic")
RECORD_PRESENTATION_FIELDS = ("display_topic", "scoring_topic_display")


def _record_topic_sources(row: dict[str, Any]) -> dict[str, str]:
    """把记录自己的每条身份线索各算一份 key；stored `topic_key` 单列，只用来对账。

    stored key 与现算结果不一致 ⇒ 记录跟自己矛盾（`MISMATCH`）。本轮修的是**生产侧**：
    规划链以前把展示形写进 `topic`，长主题（>TOPIC_DISPLAY_CHARS）于是同一条记录里
    两套文本并存、现算 key 与自报 key 差出一截（Run E 的 SCN-001 实测相似度 0.6812）。
    修后身份字段承载完整主题文本，"文本 ↔ 自报 key"这条对账仍然一个字没放松：
    `topic=X` 配 `topic_key=A` 照样 `MISMATCH`。
    """
    sources: dict[str, str] = {}
    for name in RECORD_TOPIC_TEXT_FIELDS:
        text = str(row.get(name) or "").strip()
        if text:
            sources[name] = topic_key(text)
    stored = str(row.get("topic_key") or row.get("scoring_topic_key") or "").strip()
    if stored:
        sources["stored_topic_key"] = stored
    return sources


def _bind_record_identity(row: dict[str, Any], index: int,
                          expected: set[str]) -> dict[str, Any]:
    """一条映射记录 → **恰好一个** canonical 主题身份，或者一个具名的绑定失败。

    Round 4.5 的不变量：绑定必须是"记录 → 唯一主题"的函数，不是"主题 ↔ 记录"的
    多对多关系。做法是拿记录的每条身份线索（`label` / `topic` / `scoring_topic`，
    以及它自报的 `topic_key`）到期望宇宙里各问一次"这是哪个主题"：

      * 各线索都命中同一个身份 ⇒ `BOUND` —— 只有这种记录允许参与覆盖判定；
      * 任一线索命中**多个** canonical 身份 ⇒ `AMBIGUOUS`。A-B 不过合并阈值而 C 与
        A、B 都过（非传递桥）时，C 命中两个；"从中挑一个"不叫解决歧义，所以不挑；
      * 线索之间落点不一致，含 stored key 与现算结果不符 ⇒ `MISMATCH`；
      * 全部线索都命中**零个** ⇒ `OUT_OF_UNIVERSE`（宇宙外多出来的主题）；
      * 记录压根没有主题文本 ⇒ `IDENTITY_ABSENT`，这份宇宙就核不了。
    """
    sources = _record_topic_sources(row)
    text_sources = {name: key for name, key in sources.items()
                    if name != "stored_topic_key"}
    resolutions = {name: sorted(other for other in expected
                                if _same_topic_identity(key, other))
                   for name, key in sources.items()}
    candidates = {identity for hit in resolutions.values() for identity in hit}
    binding = {
        "record": str(row.get("canonical_scoring_id") or row.get("record_id")
                      or row.get("scoring_item_id") or f"#{index}"),
        "identity_sources": sources,
        "identity_resolutions": resolutions,
    }
    if not text_sources:
        binding["status"] = "IDENTITY_ABSENT"
        return binding
    if any(len(hit) > 1 for hit in resolutions.values()):
        binding["status"] = "AMBIGUOUS"
        binding["candidates"] = sorted(candidates)
        return binding
    if len(candidates) > 1 or (candidates and any(not hit for hit in resolutions.values())):
        binding["status"] = "MISMATCH"
        binding["resolved"] = sorted(candidates)
        return binding
    if not candidates:
        binding["status"] = "OUT_OF_UNIVERSE"
        binding["recomputed_topic_key"] = (text_sources.get("label")
                                           or next(iter(text_sources.values())))
        return binding
    binding["status"] = "BOUND"
    binding["identity"] = next(iter(candidates))
    return binding


def _canonical_scoring_inputs(run_dir: Path) -> dict[str, Any]:
    """canonical 抽取层输入一次读齐：pack 本体、它的评分项、以及**建包时算出的那张评分表结构图**。

    图必须在这里被消费掉：规划链的期望宇宙是"行 → 图 → 可响应叶子 → 折叠成唯一主题"算出来的，
    Authority 若把图丢掉，同一批行就换成按文本相似度折叠的另一把尺——正式链实测多要若干主题，
    因为无图时它们折不到叶子上，也就没有表格那一格的文本可供范围门与第二维判定。

    `scoring_items` 是**空表**与**根本没有这份 pack**是两件事（前者是"算出来 0 个主题"的凭据，
    后者是"无从核实"），所以 `pack_present` / `items_are_list` 两格单独带出来，不在这里合并。
    """
    for name in CANONICAL_EXTRACTION_FILES:
        candidate = run_dir / name
        if not candidate.is_file():
            continue
        value, state = _load(candidate)
        if state:
            return {"pack": None, "items": [], "items_are_list": False, "graph": None,
                    "graph_state": f"PACK_{state}", "gaps": [f"CANONICAL_EXTRACTION_{state}"],
                    "file": name}
        if not isinstance(value, dict):
            return {"pack": None, "items": [], "items_are_list": False, "graph": None,
                    "graph_state": "PACK_MALFORMED",
                    "gaps": ["CANONICAL_EXTRACTION_MALFORMED"], "file": name}
        raw_items = value.get("scoring_items")
        items = [row for row in (raw_items or []) if isinstance(row, dict)]
        graph = value.get("scoring_graph")
        if not isinstance(graph, dict):
            graph_state = "ABSENT_FROM_PACK"
        elif graph.get("status") == "DERIVED":
            graph_state = "DERIVED"
        else:
            graph_state = str(graph.get("status") or "MALFORMED")
        return {"pack": value, "items": items, "items_are_list": isinstance(raw_items, list),
                "graph": graph if graph_state == "DERIVED" else None,
                "graph_state": graph_state, "gaps": [], "file": name}
    return {"pack": None, "items": [], "items_are_list": False, "graph": None,
            "graph_state": "PACK_MISSING", "gaps": ["CANONICAL_EXTRACTION_MISSING"],
            "file": CANONICAL_EXTRACTION_FILES[0]}


def _universe_ruler(graph: dict[str, Any] | None) -> str:
    """期望宇宙这次用的是哪把尺——只有一处判定，名字随图的在场与否变。"""
    return ("CANONICAL_SCORING_GRAPH_LEAF_FOLDING" if graph
            else "ROW_TEXT_SIMILARITY_NO_SCORING_GRAPH")


def canonical_topic_coverage(run_dir: Path, matrix: dict[str, Any],
                             records: list[dict[str, Any]]) -> dict[str, Any]:
    """§2/§3/§4 + Round 4.5 + MMF008A4-A1：期望宇宙 = **规划链那把尺**现算出来的宇宙。

    三层输入都要真的被消费掉，而不是只被读出来：
      * `CANONICAL_MATRIX_ROWS`：矩阵行过 canonical 分类器之后的主题宇宙；
      * `CANONICAL_EXTRACTION_SCORING_ITEMS`：抽取层（gate 之前，天然更宽）；
      * `RECORDS_BOUND`：已落盘映射、且**逐条绑定到唯一 canonical 身份**的那部分主题集。

    MMF008A4-A1 关掉的是前一层的一个裂缝：这一支以前调 `build_scoring_taxonomy(rows)`
    **不带图**，于是"行 → 评分表结构图 → 可响应叶子 → 折叠成唯一主题"这条规划链到最后一层
    被换成"行 → 按文本相似度折叠"。同一批行两套答案，且无图时折不到叶子就读不到表格那一格
    的文本，范围门与第二维双双失明——正式链实测 Authority 比规划多要若干主题，门被自己造的
    幽灵主题挡死。
    现在图与抽取项都从 canonical pack 现读，两层输入过**同一把**带图的尺；图建不出来
    （NOT_DERIVABLE / 老产物不带图）时照旧按文本折叠，但**具名**说清用的是哪把尺
    （`expected_ruler`），并拿矩阵自己落盘的 canonical 身份集做逐身份对账
    （`planning_consistency`）：两把尺在同一张图下对不上，就是矩阵的行与它的记录互相矛盾，
    fail-closed，不静默换成其中任何一份读数。

    期望宇宙的 precedence 用项目**已有**的那一条：矩阵是过闸之后的 canonical 评分工件，
    所以行宇宙在场就以它为准；行不在场才退回抽取层。两层都没有 ⇒ 宇宙不可核实，
    按不放行处理（"没有清单可查"从来不等"查过了"）。
    抽取比矩阵宽本身不是缺陷（gate 判成商务/计价/程序的主题就不该进技术宇宙），
    所以那部分分歧要能被现有分类器**解释**；解释不了的分歧（矩阵整行丢了）才是矛盾。

    Round 4.5 改掉的那一半：`missing` 以前是"对每个期望主题找任意一条相似记录"，
    那是多对多 —— 一条记录与两个主题都相似就能同时顶掉两个主题。现在记录先各自
    绑定，`missing = expected - bound` 是纯集合差：一条记录最多贡献一个身份，
    绑不上的（歧义 / 冒充 / 宇宙外 / 无文本）一条都不算，重复记录也不再加覆盖。
    """
    rows = [row for row in (matrix.get("rows") or []) if isinstance(row, dict)]
    records = [row for row in records if not _header_shaped_record(row)]
    inputs = _canonical_scoring_inputs(run_dir)
    graph = inputs["graph"]
    layers: dict[str, int] = {}
    expected: set[str] = set()
    expected_basis = ""
    accounted: set[str] = set()
    composition: dict[str, dict[str, Any]] = {}
    if rows:
        taxonomy = build_scoring_taxonomy(rows, None, graph)
        expected = {_topic_identity(item) for item in taxonomy["canonical_scoring_records"]}
        expected.discard("")
        accounted = {_topic_identity(row) for row in taxonomy["rows"]} - {""}
        layers["CANONICAL_MATRIX_ROWS"] = len(expected)
        expected_basis = "CANONICAL_MATRIX_ROWS"
        # 宇宙不只是一批身份，还带着第二维的组成：哪一类主题占技术正文覆盖率、值多少分。
        # 这一格由 Authority 自己那一次**带图**的分类产出来，不在别处另算一遍。
        composition = {str(name): {"topics": int(cell.get("topics") or 0),
                                   "weight_points": round(float(cell.get("weight_points") or 0.0), 2),
                                   "counts_toward_technical_fulfillment":
                                       bool(cell.get("counts_toward_gate"))}
                       for name, cell in ((taxonomy.get("denominator") or {})
                                          .get("composition_by_response_dimension") or {}).items()}
    extraction = {_topic_identity(row)
                  for row in build_scoring_taxonomy(inputs["items"], None,
                                                    graph)["canonical_scoring_records"]}
    extraction.discard("")
    if extraction:
        layers["CANONICAL_EXTRACTION_SCORING_ITEMS"] = len(extraction)
        if not expected_basis:
            expected, expected_basis = set(extraction), "CANONICAL_EXTRACTION_SCORING_ITEMS"
    bindings = [_bind_record_identity(row, index, expected)
                for index, row in enumerate(records)]
    bound = {row["identity"] for row in bindings if row["status"] == "BOUND"}
    identity_absent = any(row["status"] == "IDENTITY_ABSENT" for row in bindings)
    ambiguous = [row for row in bindings if row["status"] == "AMBIGUOUS"]
    mismatched = [row for row in bindings if row["status"] == "MISMATCH"]
    layers["RECORDS_BOUND"] = len(bound)
    missing = sorted(expected - bound)
    extra = sorted({row["recomputed_topic_key"] for row in bindings
                    if row["status"] == "OUT_OF_UNIVERSE"})
    duplicates = sorted({identity for identity in bound
                         if sum(1 for row in bindings if row.get("identity") == identity) > 1})
    unexplained = sorted(key for key in extraction - expected if key not in accounted
                         and not _topic_covered(key, accounted))
    # 逐身份对账：Authority 现算的宇宙 vs 矩阵自己落盘的 canonical 记录身份集。
    # 两边都过同一张图时才**可比**，所以对不上就是矛盾（矩阵的行与它自己的记录互不认账），
    # 而不是"换一份读数"。没有图时两边本来就不是同一把尺折出来的，只报数不判案。
    planning = {_topic_identity(row) for row in records}
    planning.discard("")
    return {"expected": expected, "mapped": bound, "bindings": bindings,
            "ambiguous": ambiguous, "mismatched": mismatched,
            "expected_basis": expected_basis, "layers": layers,
            "missing": missing, "extra": extra, "duplicates": duplicates,
            "unexplained_extraction_topics": unexplained,
            "explained_extraction_topic_total": len(extraction) - len(unexplained)
            - len(extraction & expected),
            "universe_verifiable": bool(expected_basis) and not identity_absent,
            "record_identity_absent": identity_absent,
            "expected_ruler": _universe_ruler(graph),
            "scoring_graph_state": inputs["graph_state"],
            "composition": composition,
            "respondable_total": sum(cell["topics"] for cell in composition.values()
                                     if cell["counts_toward_technical_fulfillment"]),
            "respondable_weight_points": round(sum(cell["weight_points"] for cell in
                                                   composition.values()
                                                   if cell["counts_toward_technical_fulfillment"]),
                                               2),
            "planning_consistency": {
                "applies": bool(expected_basis == "CANONICAL_MATRIX_ROWS" and graph),
                "reason": ("" if expected_basis == "CANONICAL_MATRIX_ROWS" and graph else
                           ("NO_SCORING_GRAPH_IN_CANONICAL_INPUTS" if expected_basis
                            == "CANONICAL_MATRIX_ROWS" else "NO_MATRIX_ROW_UNIVERSE")),
                "planning_identity_total": len(planning),
                "authority_identity_total": len(expected),
                "identities_equal": planning == expected,
                "only_in_authority": sorted(expected - planning),
                "only_in_planning": sorted(planning - expected),
                "lineage_source_fields_null": sum(
                    1 for row in records
                    if isinstance(row.get("rubric_lineage"), dict)
                    and not (row["rubric_lineage"].get("source_chapter")
                             or row["rubric_lineage"].get("source_locator")
                             or row["rubric_lineage"].get("source_table_id"))),
                "record_total": len(records)}}


def _topic_facts(coverage: dict[str, Any]) -> dict[str, Any]:
    """§3 要求把 expected/mapped/missing/extra 显式写进每一次读数（不只写个数）。

    Round 4.5 再加一格：绑定失败的每一笔也要看得见 —— 是哪条记录、它的每条身份线索
    各命中了哪些 canonical 身份。否则后来的人只知道"挡住了"，不知道被哪一步挡住。
    """
    return {"topic_expected_count": len(coverage["expected"]),
            "topic_mapped_count": len(coverage["mapped"]),
            "topic_missing_ids": coverage["missing"],
            "topic_extra_ids": coverage["extra"],
            "topic_duplicate_ids": coverage["duplicates"],
            "topic_universe_layers": coverage["layers"],
            "topic_expected_basis": coverage["expected_basis"],
            "topic_universe_verifiable": coverage["universe_verifiable"],
            "topic_unexplained_extraction_topics": coverage["unexplained_extraction_topics"],
            # MMF008A4-A1：期望宇宙是**哪把尺**算出来的，与它是否逐身份等于矩阵自己落盘的
            # canonical 身份集。个数相等而身份不同不叫对上，所以两边差集原样报出来。
            "topic_universe_ruler": coverage["expected_ruler"],
            "topic_scoring_graph_state": coverage["scoring_graph_state"],
            "topic_universe_planning_consistency": coverage["planning_consistency"],
            # 宇宙的第二维组成：占不占技术正文覆盖率、值多少分，逐类并列（条数尺 + 分值尺）。
            "topic_universe_composition": coverage["composition"],
            "topic_respondable_total": coverage["respondable_total"],
            "topic_respondable_weight_points": coverage["respondable_weight_points"],
            "topic_record_binding_counts": {
                name: sum(1 for row in coverage["bindings"] if row["status"] == name)
                for name in ("BOUND", "AMBIGUOUS", "MISMATCH", "OUT_OF_UNIVERSE",
                             "IDENTITY_ABSENT")},
            "topic_identity_ambiguous_records": [
                {"record": row["record"], "candidates": row["candidates"]}
                for row in coverage["ambiguous"]][:10],
            "topic_identity_mismatched_records": [
                {"record": row["record"], "identity_sources": row["identity_sources"],
                 "identity_resolutions": row["identity_resolutions"]}
                for row in coverage["mismatched"]][:10]}
def _topic_total(rows: list[dict[str, Any]], graph: dict[str, Any] | None = None) -> int:
    """一批评分行里有多少个**唯一技术实质评分主题**：交给 canonical 分类器数。

    这里绝不自己写第二套分类或去重（§11）：用的就是规划期那份
    `build_scoring_taxonomy`，并且**与期望宇宙同一把尺** —— 图在场就必须带上图。
    N/A 的分母与覆盖率的宇宙若是两把尺，"这里到底有没有适用主题"就永远只有一个答案
    是认真的（MMF008A4-A1 前：宇宙 43、记录 39，两边各自成立却互相不认账）。
    """
    if not rows:
        return 0
    return len(build_scoring_taxonomy(rows, None, graph)["canonical_scoring_records"])


def canonical_technical_topic_total(run_dir: Path,
                                    matrix: dict[str, Any] | None = None) -> dict[str, Any]:
    """从 canonical 抽取里**算**出技术实质评分主题数，作为 N/A 的唯一凭据（§7）。

    三个来源各算一次，取其中最大的"已被事实证明"的数：任何一个源证明"这里有主题"，
    就足以推翻"没有适用主题"的声明。等不等不做要求（老矩阵的行字段完整度不一），
    但并列报出来，分歧看得见。
    """
    run_dir = Path(run_dir)
    sources: dict[str, Any] = {}
    inputs = _canonical_scoring_inputs(run_dir)
    graph = inputs["graph"]
    gaps: list[str] = list(inputs["gaps"])
    if isinstance(matrix, dict) and matrix:
        records = [row for row in (matrix.get("canonical_scoring_records") or [])
                   if isinstance(row, dict)]
        rows = [row for row in (matrix.get("rows") or []) if isinstance(row, dict)]
        sources["CANONICAL_MATRIX_RECORDS"] = len(records)
        sources["CANONICAL_MATRIX_ROWS"] = _topic_total(rows, graph)
    if inputs["items_are_list"]:
        sources["CANONICAL_EXTRACTION_SCORING_ITEMS"] = _topic_total(inputs["items"], graph)
    elif inputs["pack"]:
        gaps.append("CANONICAL_EXTRACTION_WITHOUT_SCORING_ITEMS")
    total = max(sources.values()) if sources else None
    basis = next((name for name, value in sources.items() if value == total), "") if sources else ""
    return {"total": total, "basis": basis, "sources": sources, "gaps": sorted(set(gaps)),
            "graph_state": inputs["graph_state"], "proven": bool(sources)}


def _declared_gate(matrix: dict[str, Any], derived_gate: dict[str, Any],
                   derived_gate_state: str) -> dict[str, Any]:
    """把"派生文件声称了什么"读出来备查，不参与放行判断。

    声称的形状完全不受信：这里对类型逐项兜住（一句 `status: "PASS"` 字符串、
    一个 `"PASS"` 整份文件都出现过），但兜住只意味着"看得见它说了什么"，
    不意味着"它说了就算"。
    """
    embedded = matrix.get(EMBEDDED_GATE_KEY) if isinstance(matrix, dict) else None
    embedded_row = embedded if isinstance(embedded, dict) else {}
    derived_row = derived_gate if isinstance(derived_gate, dict) else {}
    return {
        "embedded_present": embedded is not None,
        "embedded_shape_ok": isinstance(embedded, dict),
        "embedded_status": str(embedded_row.get("status") or ""),
        "embedded_substantive_topic_total": embedded_row.get("substantive_topic_total"),
        "derived_file_state": derived_gate_state or "PRESENT",
        "derived_file_status": str(derived_row.get("status") or (derived_gate or "")),
        "derived_file_substantive_topic_total": derived_row.get("substantive_topic_total"),
        "derived_file_honored": False,
    }


def _reading(status: str, *, may_enter: bool, reason: str, source: str = "",
             gate: dict[str, Any] | None = None, expected: bool | None = None,
             extras: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 Authority 的读数写成一份**不可误读**的对象（§8）。

    `may_enter_generation` 与 `status` 必须同时在场：调用方只许读布尔，不再有机会
    把"空对象"当成"没有评分项"。一个坏形状的门结论也不能把读数器自己弄崩 ——
    派生文件写成一串、写成数组都出现过，这里对类型兜住，兜住之后照样是"不放行"。
    """
    row = dict(gate) if isinstance(gate, dict) else {}
    row.update({
        "status": status,
        "may_enter_generation": bool(may_enter),
        "gate_status": str(row.get("status") or "") if isinstance(gate, dict) else str(gate or ""),
        "reason": reason,
        "source": source,
        "scoring_mapping_expected": (None if expected is None else bool(expected)),
        "fail_closed": not may_enter,
        "verdict_basis": ("RECOMPUTED_FROM_CANONICAL_INPUTS" if may_enter and status == "PASS"
                          else "DERIVED_FROM_CANONICAL_INPUTS"),
        "clean_blockers": ([] if may_enter else [
            SCORING_AUTHORITY_CLEAN_BLOCKERS.get(status, SCORING_AUTHORITY_UNKNOWN_BLOCKER)]),
    })
    row.update(extras or {})
    return row


def _validate_declared_gate(gate: object, records_total: int, source: str,
                            expected: bool) -> dict[str, Any]:
    """门的形状/自洽性不合法 ⇒ 一律 fail-closed，绝不回退成"跳过这道门"。"""
    if not isinstance(gate, dict):
        return _reading("SCORING_MAPPING_GATE_MALFORMED", may_enter=False, expected=expected,
                        reason=f"{source} 里的 {EMBEDDED_GATE_KEY} 不是对象"
                               f"（读到 {type(gate).__name__}）：门的结论不可读就是没确立",
                        source=source)
    missing = [field for field in SCORING_GATE_REQUIRED_FIELDS if field not in gate]
    if missing:
        return _reading("SCORING_MAPPING_GATE_INCOMPLETE", may_enter=False, expected=expected,
                        reason=f"{source} 里的门缺必备字段 {missing}：宁可停，不按缺省值放行",
                        gate=gate, source=source)
    verdict = str(gate.get("status") or "")
    if verdict not in SCORING_GATE_VERDICTS:
        return _reading("SCORING_MAPPING_GATE_UNKNOWN_VERDICT", may_enter=False, expected=expected,
                        reason=f"{source} 里的门结论 {verdict!r} 不在词表 "
                               f"{sorted(SCORING_GATE_VERDICTS)} 内",
                        gate=gate, source=source)
    if not isinstance(gate.get("may_enter_generation"), bool):
        return _reading("SCORING_MAPPING_GATE_MALFORMED", may_enter=False, expected=expected,
                        reason=f"{source} 里的 may_enter_generation 不是布尔值",
                        gate=gate, source=source)
    if int(gate.get("substantive_topic_total") or 0) != int(records_total):
        return _reading("SCORING_MAPPING_GATE_STALE", may_enter=False, expected=expected,
                        reason=f"{source} 里的门按 {gate.get('substantive_topic_total')} 个唯一实质"
                               f"主题评估，矩阵现在却有 {records_total} 个：门是旧读数，"
                               f"不能当本轮结论用",
                        gate=gate, source=source)
    allowed = bool(gate.get("may_enter_generation"))
    if allowed and verdict in {"BLOCKED_SCORING_MAPPING_INCOMPLETE", "TECHNICAL_SCORING_LEAK"}:
        return _reading("SCORING_MAPPING_GATE_CONTRADICTORY", may_enter=False, expected=expected,
                        reason=f"{source} 里 status={verdict} 却写着 may_enter_generation=true",
                        gate=gate, source=source)
    if allowed and verdict == "NOT_APPLICABLE" and records_total > 0:
        return _reading("SCORING_MAPPING_GATE_CONTRADICTORY", may_enter=False, expected=expected,
                        reason=f"{source} 里有 {records_total} 个唯一实质技术主题，"
                               f"结论却写着 NOT_APPLICABLE",
                        gate=gate, source=source)
    if not allowed:
        return _reading(verdict, may_enter=False, expected=expected, gate=gate, source=source,
                        reason=str((gate.get("blocked_reasons") or [""])[0] or verdict))
    return _reading(verdict, may_enter=allowed, expected=expected, gate=gate, source=source,
                    reason="")


def _plan_reference_ids(run_dir: Path) -> tuple[list[str], list[str], str]:
    """从盘上的分层树里取出"计划里真实存在"的节与内容单元清单（引用对账的依据）。"""
    hierarchy_path = run_dir / CANONICAL_PLAN_FILE
    if not hierarchy_path.is_file():
        return [], [], "PLAN_ARTIFACT_MISSING"
    hierarchy, state = _load_object(hierarchy_path)
    if state:
        return [], [], f"PLAN_ARTIFACT_{state}"
    return (_planned_section_ids(hierarchy), _planned_unit_ids(hierarchy), "")


def canonical_scoring_authority(run_dir: Path) -> dict[str, Any]:
    """Technical Scoring 的唯一权威读数。生成入口与终态落盘都只读这一个函数。

    非抛出、纯磁盘读取、每次现算：不缓存、不继承上一轮结论、也不接受任何派生文件
    自报的 PASS。返回的 `may_enter_generation` 就是"能不能进生成"与"能不能自称
    COMPLETED_CLEAN"的**同一个**判据（§11：一个结果两处消费，不设第二个布尔）。
    """
    run_dir = Path(run_dir)
    expected = any((run_dir / name).is_file() for name in SCORING_PLANNING_EVIDENCE_FILES)
    matrix_path = run_dir / CANONICAL_MATRIX_FILE
    matrix, matrix_state = _load_object(matrix_path) if matrix_path.is_file() else ({}, MATRIX_MISSING)
    if not matrix_state:
        matrix_state = MATRIX_PRESENT
    derived_gate, derived_gate_state = _load_object(run_dir / SCORING_MAPPING_GATE_FILE)
    if not (run_dir / SCORING_MAPPING_GATE_FILE).is_file():
        derived_gate_state = MATRIX_MISSING
    topics = canonical_technical_topic_total(run_dir, matrix=matrix)
    declared = _declared_gate(matrix, derived_gate, derived_gate_state)
    records = [row for row in (matrix.get("canonical_scoring_records") or [])
               if isinstance(row, dict)]
    coverage = canonical_topic_coverage(run_dir, matrix, records)
    identity = {CANONICAL_MATRIX_FILE: (_sha256(matrix_path) if matrix_path.is_file() else "ABSENT"),
                CANONICAL_PLAN_FILE: _sha256(run_dir / CANONICAL_PLAN_FILE)}
    common = {"canonical_topic_total": topics["total"], "canonical_topic_basis": topics["basis"],
              "canonical_topic_sources": topics["sources"], "canonical_topic_gaps": topics["gaps"],
              "canonical_matrix_state": matrix_state, "declared": declared,
              "verified_against": identity, "scoring_authority_identity": "|".join(
                  f"{key}={value}" for key, value in sorted(identity.items())),
              **_topic_facts(coverage)}

    def decide(status: str, *, may_enter: bool, reason: str, source: str,
               gate: dict[str, Any] | None = None, extras: dict[str, Any] | None = None):
        return _reading(status, may_enter=may_enter, reason=reason, source=source, gate=gate,
                        expected=expected, extras={**common, **(extras or {})})

    # ---- 1. canonical 矩阵读不出来：无论派生文件写着什么，一律停（§5）
    if matrix_state == MATRIX_UNREADABLE:
        return decide("SCORING_MAPPING_ARTIFACT_UNREADABLE", may_enter=False, source=matrix_path.name,
                      reason=f"{CANONICAL_MATRIX_FILE} 存在但读不出来（JSON 损坏或不可读）："
                             f"这“不是”“没有评分主题”，派生门文件的结论不能替它说话")
    if matrix_state == MATRIX_MALFORMED:
        return decide("SCORING_MAPPING_ARTIFACT_MALFORMED", may_enter=False, source=matrix_path.name,
                      reason=f"{CANONICAL_MATRIX_FILE} 顶层不是对象")

    proven_total = topics["total"]

    # ---- 2. canonical 矩阵不在场：派生门文件**不是**授权来源（§4/§5，Blocker B1）
    if matrix_state == MATRIX_MISSING:
        if proven_total == 0:
            if declared["derived_file_substantive_topic_total"] or declared["embedded_substantive_topic_total"]:
                return decide("SCORING_MAPPING_GATE_CONTRADICTORY", may_enter=False,
                              source=SCORING_MAPPING_GATE_FILE,
                              reason=f"canonical 抽取算出 0 个适用技术实质主题，"
                                     f"派生门文件却按 "
                                     f"{declared['derived_file_substantive_topic_total']} 个评估："
                                     f"两份读数对不上，按不放行处理")
            return decide("NOT_APPLICABLE", may_enter=True, source=CANONICAL_EXTRACTION_FILES[0],
                          reason="分母由 canonical 抽取算出为 0（合法 N/A，与文件坏了严格不同）")
        if proven_total and proven_total > 0:
            status = ("SCORING_MAPPING_FALSE_NOT_APPLICABLE"
                      if declared["derived_file_status"] == "NOT_APPLICABLE"
                      else "SCORING_MAPPING_ARTIFACT_MISSING")
            return decide(status, may_enter=False, source=SCORING_MAPPING_GATE_FILE,
                          reason=f"canonical 抽取里有 {proven_total} 个唯一实质技术评分主题"
                                 f"（分母来源 {topics['basis']}），却没有 "
                                 f"{CANONICAL_MATRIX_FILE}：该有而没有，"
                                 f"任何派生门文件自报 PASS 都不构成放行凭据")
        if expected:
            return decide("SCORING_MAPPING_ARTIFACT_MISSING", may_enter=False,
                          source=CANONICAL_MATRIX_FILE,
                          reason="这一单跑过规划却没有评分矩阵/映射门，且 canonical 抽取也读不到，"
                                 "无法证明「没有评分主题」：该有而没有，不得按“没有评分项”放行")
        return decide("SCORING_MAPPING_NOT_PLANNED", may_enter=True, source="NOT_PRESENT",
                      reason="本次入口没有 canonical 输入在场（既无规划产物也无评分抽取）："
                             "无评分映射可言，但这“不是”“门已通过”")

    # ---- 3. canonical 矩阵在场，却没有 canonical 记录：先问事实，再问声明（§6/§7）
    if not records:
        if proven_total and proven_total > 0:
            # 两种"没有记录"要分开命名，否则名字会指控一个并没发生过的声明：
            #   ① 管道算过、把 records 落成空表，或某份门文件写着 NOT_APPLICABLE ——
            #      这是在**声称**"没有适用主题"，而事实说有 ⇒ FALSE_NOT_APPLICABLE；
            #   ② 老矩阵（Run D 那一份 R3 时代产物）连 `canonical_scoring_records` 这个键
            #      都没有、也没嵌过门 ⇒ 它没做过任何声称，只是 canonical 映射从未落盘，
            #      命名成 RECORDS_NOT_PERSISTED。两条都不放行，但只有 ① 是"说谎"。
            embedded_row = matrix.get(EMBEDDED_GATE_KEY)
            embedded_row = embedded_row if isinstance(embedded_row, dict) else {}
            claimed_na = ("canonical_scoring_records" in matrix
                          or str(embedded_row.get("status") or "") == "NOT_APPLICABLE"
                          or declared["derived_file_status"] == "NOT_APPLICABLE")
            return decide("SCORING_MAPPING_FALSE_NOT_APPLICABLE" if claimed_na
                          else "SCORING_MAPPING_RECORDS_NOT_PERSISTED",
                          may_enter=False, source=matrix_path.name,
                          extras={"clean_blockers": [
                              SCORING_AUTHORITY_CLEAN_BLOCKERS.get(
                                  "SCORING_MAPPING_FALSE_NOT_APPLICABLE" if claimed_na
                                      else "SCORING_MAPPING_RECORDS_NOT_PERSISTED",
                                  SCORING_AUTHORITY_UNKNOWN_BLOCKER),
                              # §5：expected>0 而一条都没映射，本质就是覆盖不完整 ——
                              # 名字要说清它同时是这两件事，但不重复写同一个码。
                              "SCORING_AUTHORITY_TOPIC_COVERAGE_INCOMPLETE"]},
                          reason=f"canonical 抽取算出 {proven_total} 个唯一实质技术评分主题"
                                 f"（来源 {topics['basis']}），"
                                 + ("矩阵把 canonical 记录算成了空表：" if claimed_na else
                                    "矩阵里根本没有 canonical 记录这一段（老工件），")
                                 + "N/A 必须是算出来的 0，不是空值或一个词："
                                   f"适用主题 {len(coverage['expected'])} 个、已映射 0 个")
        embedded = matrix.get(EMBEDDED_GATE_KEY)
        if isinstance(embedded, dict) and int(embedded.get("substantive_topic_total") or 0) > 0:
            return decide("SCORING_MAPPING_GATE_STALE", may_enter=False, source=matrix_path.name,
                          gate=embedded,
                          reason=f"嵌在矩阵里的门按 "
                                 f"{embedded.get('substantive_topic_total')} 个主题评估，"
                                 f"矩阵里却一条 canonical 记录都没有")
        return decide("NOT_APPLICABLE", may_enter=True, source=matrix_path.name,
                      reason="矩阵在场，且 canonical 抽取与矩阵行都算不出适用技术实质主题"
                             "（合法 N/A，与文件损坏不同）")

    consistency = coverage["planning_consistency"]
    if consistency["applies"] and not consistency["identities_equal"]:
        # 这一档排在**任何派生声称**之前：矩阵的行与它自己落盘的记录互不认账时，
        # "覆盖齐不齐"这道题本身就还没定义，先回答它等于挑一份读数当分母。
        return decide("SCORING_MAPPING_TOPIC_UNIVERSE_PLANNING_MISMATCH", may_enter=False,
                      source=matrix_path.name,
                      extras={"topic_universe_planning_consistency": consistency},
                      reason=f"同一张评分表结构图下，Authority 按矩阵行现算出的主题宇宙有 "
                             f"{consistency['authority_identity_total']} 个身份，"
                             f"矩阵自己落盘的 canonical 记录却是 {consistency['planning_identity_total']} 个："
                             f"多出来 {consistency['only_in_authority'][:5]}、"
                             f"少了 {consistency['only_in_planning'][:5]}。"
                             f"规划链与校验链必须数同一批主题（尺 {coverage['expected_ruler']}），"
                             f"两边对不上就是矩阵的行与它自己的记录互不认账，"
                             f"不得挑其中一份当分母")

    # ---- 4. 有主题：门的结论必须**现在**按当前计划清单重算，嵌入的 PASS 只是声称
    if EMBEDDED_GATE_KEY not in matrix:
        return decide("SCORING_MAPPING_GATE_NOT_EMBEDDED", may_enter=False, source=matrix_path.name,
                      reason=f"矩阵里有 {len(records)} 个唯一实质技术主题，却没评估过生成前映射门："
                             f"门没跑不等于门通过")
    validated = _validate_declared_gate(matrix[EMBEDDED_GATE_KEY], len(records),
                                        matrix_path.name, expected)
    content_block = str(validated.get("status") or "") in {
        "BLOCKED_SCORING_MAPPING_INCOMPLETE", "TECHNICAL_SCORING_LEAK"}
    # 嵌入的未通过只在计划树还在时重算：分类器修正后，表尾合计不再挡生成。
    # 计划树不在场就沿用原结论，不能改口成「计划不可核」。
    if not validated["may_enter_generation"] and not (
            content_block and (run_dir / CANONICAL_PLAN_FILE).is_file()):
        return decide(str(validated["status"]), may_enter=False, source=matrix_path.name,
                      gate=matrix[EMBEDDED_GATE_KEY], reason=str(validated["reason"]))
    sections, units, plan_gap = _plan_reference_ids(run_dir)
    if plan_gap:
        return decide("SCORING_MAPPING_PLAN_UNVERIFIABLE", may_enter=False, source=CANONICAL_PLAN_FILE,
                      gate=matrix[EMBEDDED_GATE_KEY],
                      reason=f"门的结论说可以进生成，却拿不到分层树来核对引用（{plan_gap}）："
                             f"无清单可查不等于查过了")
    recomputed = pre_generation_scoring_mapping_gate(
        matrix, planned_unit_ids=units, planned_section_ids=sections)
    if not recomputed.get("may_enter_generation"):
        invalid_sections = sorted({str(item) for defect in (recomputed.get("unmapped_records") or [])
                                   for item in (defect.get("invalid_sections") or [])})
        invalid_units = sorted({str(item) for defect in (recomputed.get("unmapped_records") or [])
                                for item in (defect.get("invalid_content_units") or [])})
        blockers = [SCORING_AUTHORITY_CLEAN_BLOCKERS.get(str(recomputed.get("status")),
                                                         SCORING_AUTHORITY_UNKNOWN_BLOCKER)]
        if (invalid_sections or invalid_units) and "SCORING_AUTHORITY_INVALID_REFERENCE" not in blockers:
            # 引用查无此处时，光说"没映射完"不够：必须单点出 INVALID_REFERENCE，
            # 但也不能把同一个原因码写两遍（同一个状态里重复的名字会让人以为挡住了两次）。
            blockers.append("SCORING_AUTHORITY_INVALID_REFERENCE")
        return decide(str(recomputed.get("status") or "SCORING_MAPPING_GATE_CONTRADICTORY"),
                      may_enter=False, source=matrix_path.name, gate=recomputed,
                      extras={"clean_blockers": blockers,
                              "invalid_reference": {"sections": invalid_sections,
                                                    "content_units": invalid_units}},
                      reason=f"{matrix_path.name} 里嵌的门结论与按**当前**计划清单重算的结果不一致："
                             f"重算判 {recomputed.get('status')}，"
                             f"未映射主题 {recomputed.get('unmapped_total')} 个，"
                             f"查无此节 {invalid_sections}，查无此单元 {invalid_units}")
    divergence = (str(matrix[EMBEDDED_GATE_KEY].get("status") or "") != str(recomputed.get("status")))
    # ---- 5. Round 4.4 §2/§3：引用合法只证明"已存在的记录落点是对的"，不证明"该映射的
    # 主题都映射了"。PASS 还要求 expected 主题身份集 == mapped 主题身份集。
    # 这一档必须排在引用重算**之后**：落点本身就是幽灵时，先报那个更具体的原因。
    if not coverage["universe_verifiable"]:
        return decide("SCORING_MAPPING_TOPIC_UNIVERSE_UNVERIFIABLE", may_enter=False,
                      source=matrix_path.name, gate=recomputed,
                      reason=f"有 {len(records)} 条 canonical 记录，却核不出适用主题宇宙"
                             f"（宇宙来源 {coverage['expected_basis'] or 'NONE'}"
                             + ("，且有记录没有主题身份（topic_key 为空）"
                                if coverage["record_identity_absent"] else "")
                             + "）：核不了覆盖不等于覆盖齐了")
    if coverage["mismatched"]:
        return decide("SCORING_MAPPING_TOPIC_IDENTITY_MISMATCH", may_enter=False,
                      source=matrix_path.name, gate=recomputed,
                      extras={"declared_verdict_divergence": divergence},
                      reason=f"{len(coverage['mismatched'])} 条 canonical 记录绑定不到唯一主题身份："
                             f"记录自报的 topic_key 与按记录自己的主题文本现算的结果不一致"
                             f"（{[row['record'] for row in coverage['mismatched'][:5]]}）。"
                             f"身份只能由主题文本决定，stored key 不得覆盖原始 topic 文本")
    if coverage["ambiguous"]:
        return decide("SCORING_MAPPING_TOPIC_IDENTITY_AMBIGUOUS", may_enter=False,
                      source=matrix_path.name, gate=recomputed,
                      extras={"declared_verdict_divergence": divergence},
                      reason=f"{len(coverage['ambiguous'])} 条 canonical 记录同时落在 "
                             f"{len({i for row in coverage['ambiguous'] for i in row['candidates']})} "
                             f"个 canonical 主题身份上"
                             f"（{[(row['record'], row['candidates']) for row in coverage['ambiguous'][:3]]}）："
                             f"一条记录只能映射一个主题，多个候选里挑一个不叫解决歧义，"
                             f"一条记录顶两个主题就是非传递桥")
    if coverage["unexplained_extraction_topics"]:
        return decide("SCORING_MAPPING_TOPIC_UNIVERSE_CONTRADICTORY", may_enter=False,
                      source=CANONICAL_EXTRACTION_FILES[0], gate=recomputed,
                      reason=f"抽取层有 {len(coverage['unexplained_extraction_topics'])} 个主题"
                             f"既不在矩阵的技术主题宇宙里、也无法由现有分类器解释"
                             f"（{coverage['unexplained_extraction_topics'][:5]}）："
                             f"两层 canonical 输入自己就对不上，不能只凭记录放行")
    if coverage["missing"]:
        return decide("SCORING_MAPPING_TOPIC_COVERAGE_INCOMPLETE", may_enter=False,
                      source=matrix_path.name, gate=recomputed,
                      extras={"declared_verdict_divergence": divergence},
                      reason=f"适用技术实质主题 {len(coverage['expected'])} 个"
                             f"（宇宙来源 {coverage['expected_basis']}），"
                             f"已映射 {len(coverage['mapped'])} 个，"
                             f"缺 {len(coverage['missing'])} 个：{coverage['missing'][:5]}。"
                             f"已有的 {len(records)} 条记录引用都合法，"
                             f"但“引用合法”不等于“主题都覆盖了”"
                             + (f"；另有同一主题被记了多条 {coverage['duplicates'][:5]}"
                                if coverage["duplicates"] else ""))
    if coverage["extra"]:
        return decide("SCORING_MAPPING_TOPIC_NOT_IN_UNIVERSE", may_enter=False,
                      source=matrix_path.name, gate=recomputed,
                      extras={"declared_verdict_divergence": divergence},
                      reason=f"canonical 记录里出现 {len(coverage['extra'])} 个不属于适用主题宇宙"
                             f"（{coverage['expected_basis']}）的主题身份 {coverage['extra'][:5]}："
                             f"多出来的映射同样是没对上，不能当加分项")
    return decide(str(recomputed.get("status")), may_enter=True, source=matrix_path.name,
                  gate=recomputed,
                  extras={"declared_verdict_divergence": divergence},
                  reason="引用完整性已按当前 canonical 计划清单重算通过"
                         f"（{recomputed.get('mapped_total')}/{recomputed.get('substantive_topic_total')} "
                         f"个唯一实质技术主题有真实落点），且主题覆盖完整："
                         f"expected == mapped == {len(coverage['expected'])} 个"
                         f"（宇宙来源 {coverage['expected_basis']}）")
