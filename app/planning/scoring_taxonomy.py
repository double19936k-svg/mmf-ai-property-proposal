"""P0-B（Round 4 §24–§37）：评分项十类预分类、唯一实质主题去重与规范评分记录。

要解决的问题：Run D 把"评分表里有多少行"当成"技术标要回答多少件事"。同一件事在
招标评分表里出现三次，就占三个分母；表头行、分值档次行也被当成待响应项。结果是
覆盖率既不能反映真实缺口，也不能指导补写。

本模块只做**分类与登记**，不生成内容、不改片段、不碰凭证：
- 十类预分类：只有 TECHNICAL_SUBSTANTIVE 进入覆盖率分母，其余一律登记不响应。
- 分母按 unique substantive topic 计，不按原始行数；重复行折叠进 canonical 记录。
- 每条 canonical 记录带 planned section / planned CU / evidence contract，
  生成前由 `pre_generation_scoring_mapping_gate` 检查，缺任何一项不得进正式生成。

MMF008A-A 补上**第二维**（`scoring_responsiveness`）：第一维答"这行属于招标文件哪一块"，
第二维答"技术标正文要怎样才算回答了它"。Run F 实测的错位就在这一格——
"项目管理团队人员构成""组织架构中每个成员的角色/阶段/工作内容"与"省级银行类似项目的
服务经验""是否覆盖所有物业需求""技术及服务部分 40分"在第一维里都是
`TECHNICAL_INCLUDED -> TECHNICAL_SUBSTANTIVE`，于是要么被要求拿个人资历与项目合同到正文里
举证（那本来属于资格/商务文件），要么被一句都找不到的招标原句当要素去搜（真实写出的岗位表
读成未覆盖）。两维**相乘**才决定一条主题进不进技术标完成度的账。

类数、状态数、维数都从本模块的常量推导，不得在任何调用方写死条数（§36 硬编码禁令）。
"""
from __future__ import annotations

import re
from typing import Any

from .content_engineering import (
    TECHNICAL_SCOPE_CLASSES,
    TECHNICAL_SCOPE_DISPOSITION,
    is_excluded_technical_scope,
)
from .scoring_evidence import (
    label_concepts_with_segmentation,
    normalize_text,
    rubric_for,
)
from .scoring_graph import (  # noqa: E402
    RESPONSE_TEXT_FROM_TABLE, leaf_by_id, response_text_for_item, rubric_lineage,
    rubric_lineage_index)
from .scoring_responsiveness import (
    CROSS_FILE_PROTECTED_EVIDENCE,
    FULFILLMENT_REQUIRED_DIMENSIONS,
    TECHNICAL_ACCEPTANCE_DIMENSIONS,
    MAY_NOT_BIND_TECHNICAL_RESPONSE_CU,
    RESPONSIVENESS_VERSION,
    RESPONSE_DIMENSION_DISPOSITION,
    RESPONSE_DIMENSIONS,
    TECHNICAL_BODY_ANONYMOUS_STRUCTURE,
    WHOLE_DOCUMENT_RUBRIC,
    record_requires_body_response,
    record_requires_technical_acceptance,
    responsiveness_fields,
)

SCORING_TAXONOMY_VERSION = "technical-scoring-taxonomy-v0.3"

# §24 十类：技术实质 / 技术参考 / 两类非响应表行 / 去重折叠 / 五类范围排除。
SCORING_CLASSES: tuple[str, ...] = (
    "TECHNICAL_SUBSTANTIVE",
    "TECHNICAL_CONTEXTUAL",
    "SCORING_HEADER",
    "SCORING_FORMULA",
    "DUPLICATE_TOPIC",
    "COMMERCIAL_TERMS",
    "PRICING",
    "QUALIFICATION_CREDENTIAL",
    "BIDDER_CREDENTIAL",
    "PROCEDURAL",
)

SCORING_CLASS_DISPOSITION: dict[str, str] = {
    "TECHNICAL_SUBSTANTIVE": "TECHNICAL_DENOMINATOR",
    "TECHNICAL_CONTEXTUAL": "TECHNICAL_NOT_SCORED",
    "SCORING_HEADER": "NON_RESPONSE_ROW",
    "SCORING_FORMULA": "NON_RESPONSE_ROW",
    "DUPLICATE_TOPIC": "MERGED_INTO_CANONICAL",
    "COMMERCIAL_TERMS": "EXCLUDED_FROM_TECHNICAL_BID",
    "PRICING": "EXCLUDED_FROM_TECHNICAL_BID",
    "QUALIFICATION_CREDENTIAL": "EXCLUDED_FROM_TECHNICAL_BID",
    "BIDDER_CREDENTIAL": "EXCLUDED_FROM_TECHNICAL_BID",
    "PROCEDURAL": "EXCLUDED_FROM_TECHNICAL_BID",
}

DENOMINATOR_SCORING_CLASS = "TECHNICAL_SUBSTANTIVE"
EXCLUDED_SCORING_DISPOSITION = "EXCLUDED_FROM_TECHNICAL_BID"
# 分母口径的字面量只在这里定义一次：门控、矩阵摘要、终态读数与报告读的都是它。
#
# MMF008A-A：第一维（评分归属）之上还要过第二维（技术正文可响应性），所以这个字符串
# 改口为"唯一技术实质**且正文可响应**主题"。旧字面量不是被删掉，而是降级成对照值
# （`COVERAGE_DENOMINATOR_LEGACY_LABEL`）：按旧尺子量出来的覆盖率在新链路上读成
# NOT_MEASURED，不得授权 COMPLETED_CLEAN——换尺子必须换名字，同名的两把尺早晚会串。
COVERAGE_DENOMINATOR = "UNIQUE_TECHNICAL_RESPONDABLE_TOPICS"
COVERAGE_DENOMINATOR_LEGACY_LABEL = "UNIQUE_TECHNICAL_SUBSTANTIVE_TOPICS"
# 十类第一维口径下的全量主题数：与上面那条并列报出，换分母不藏数字。
COVERAGE_DENOMINATOR_ALL_CLASSES = "UNIQUE_TECHNICAL_SUBSTANTIVE_TOPICS_ALL_RESPONSE_CLASSES"
SCORING_MAPPING_GATE_FILE = "pre_generation_scoring_mapping_gate.json"

# 七个范围类别里，TECHNICAL_* 决定"是不是技术内容"，四類 *_EXCLUDED 决定"归哪类排除"。
# 映射表是 §24 十类与 TECHNICAL_ONLY_SCOPE_GATE 唯一的接缝：范围判定仍然只有一处真值。
SCOPE_CLASS_TO_SCORING_CLASS: dict[str, str] = {
    "TECHNICAL_INCLUDED": "TECHNICAL_SUBSTANTIVE",
    "TECHNICAL_REFERENCE": "TECHNICAL_CONTEXTUAL",
    "COMMERCIAL_EXCLUDED": "COMMERCIAL_TERMS",
    "PRICING_EXCLUDED": "PRICING",
    "QUALIFICATION_EXCLUDED": "QUALIFICATION_CREDENTIAL",
    "BIDDER_CREDENTIAL_EXCLUDED": "BIDDER_CREDENTIAL",
    "LEGAL_PROCEDURAL_EXCLUDED": "PROCEDURAL",
}

# §26 五个状态。NEARBY_ONLY 不是 fulfilled，也不许改名回"部分覆盖"。
SCORING_STATUSES: tuple[str, ...] = (
    "FULFILLED",
    "PARTIAL",
    "MISSING",
    "NEARBY_ONLY",
    "NOT_APPLICABLE",
)
FULFILLED_SCORING_STATUS = "FULFILLED"

# canonical 记录从头部行**照抄**的第二维字段：分类只在行上算一次，
# 主题折叠（DUPLICATE_TOPIC）不重算，否则同一件事会因为选了不同的头行而换类别。
# 清单由产出方自己给（对空标签跑一次取键名），不在这里手抄——手抄的清单会与新字段走散。
RESPONSIVENESS_RECORD_FIELDS: tuple[str, ...] = tuple(responsiveness_fields(""))

# ---- 评分矩阵行的 schema 契约（MMF008A3-A2）---------------------------------
# 下游读矩阵行是**直读**，不是 `.get`：`build_scoring_matrix` 的 coverage_summary 读
# `row["must_respond"]` 与 `row["response_status"]`，批量规划读 `row["section_ids"]`，
# 验收回填读 `row["fulfillment"]`。所以每一条行——包括**抽取里一行都没有、只由评分表
# 结构补出来的派生叶子**——都必须在生产端把这些字段一次配齐。
#
# 为什么不在消费端补默认值：`row.get("must_respond", False)` 会把"生产端根本没算这一格"
# 读成"这一格判定为 False"，也就是把缺 schema 洗成一个判定结果——分母因此悄悄变小，
# 而盘面上看不出任何异常。Run F 的正式链正是在 `LEAF-TAB-00004-R14`（图上成立、抽取漏抽
# 的那片叶子）上缺 `must_respond` 而 KeyError；缺字段本身只是症状，真正的缺陷是
# **派生叶子与抽取行没有共用同一份行 schema**。
SCORING_ROW_REQUIRED_FIELDS: tuple[str, ...] = (
    # 身份与来源（`source` 是评分项在招标文件里的落点：`rubric_lineage` 的章节/表格/段落
    # 三格都从它读，缺它就只剩一个 anchor_basis 名字，来源语义在矩阵这一跳就断了）
    "scoring_item_id", "label", "scoring_leaf_id", "max_score", "must_respond",
    "must_respond_basis", "source",
    # 范围门快照（唯一真值仍在 TECHNICAL_ONLY_SCOPE_GATE，这里只是行上那份）
    "scope_class", "scope_disposition",
    # 落点：需求引用 → 计划章节 → 计划正文单元 → 证据合同
    "requirement_ids", "section_ids", "primary_section_id", "planned_response_cu",
    "planned_evidence", "planned_response_words", "mapping_source", "missing_reason",
    # 分类与去重（第一维 + 折叠后的类别）
    "scoring_class", "scoring_class_after_dedup", "duplicate_of",
    # 状态与验收
    "response_status", "fulfillment",
)

# 派生叶子必须**自己算出来**的字段：这些不许从兄弟行借值，也不许留中性占位——
# 借了就是把别一条评分项的来源当成这一条的来源。
_DERIVED_COMPUTED_FIELDS: tuple[str, ...] = (
    "scoring_item_id", "label", "scoring_leaf_id", "must_respond", "must_respond_basis",
    "scoring_class", "scoring_class_source", "scoring_class_after_dedup", "response_status",
    "source",
)

# 其余契约字段的中性值。"这一格对派生叶子尚不存在"与"这一格没生产"必须是两件事，
# 所以中性值随 `derived_schema_fill` 一起落盘，读的人看得见哪些格子是中性填的。
_DERIVED_NEUTRAL_FIELDS: dict[str, Any] = {
    "max_score": None,
    "scope_class": None, "scope_disposition": None,
    "requirement_ids": [], "section_ids": [], "primary_section_id": None,
    "planned_response_cu": [], "planned_evidence": {}, "planned_response_words": 0,
    "mapping_source": None, "missing_reason": None,
    "duplicate_of": None, "entity_contract": {},
}


def leaf_source(leaf: dict[str, Any]) -> dict[str, Any]:
    """评分叶子自己的来源格子（表 id、表格序号、页码、章节路径）。

    为什么由图现算而不是留空：漏抽叶子**确实在招标表格里占一格**，那是它整个身份的依据；
    行上不带来源，`rubric_lineage` 就把这一格读成 null，来源语义到矩阵这一跳就断了。
    与抽取行同一形状（`source_section` / `source_paragraph_or_table` / `table_id`），
    但只带图确实知道的字段，并具名 `source_derivation` —— 表格结构与抽取记录是两件事，
    不能拿结构推出来的定位冒充抽取器记录过的原文位置。
    """
    locator = dict(leaf.get("locator") or {})
    heading_path = [str(node) for node in (locator.get("heading_path") or [])
                    if str(node or "").strip()]
    table_index = locator.get("table_index")
    return {
        "source_section": " / ".join(heading_path) or None,
        "heading_path": heading_path,
        "table_id": locator.get("table_id") or None,
        "source_page": locator.get("page_no"),
        "source_paragraph_or_table": (f"table:{table_index}"
                                      if table_index is not None else None),
        "source_derivation": "SCORING_TABLE_LEAF_LOCATOR",
    }


def derived_scoring_leaf_row(leaf: dict[str, Any], *, consensus: tuple[str, str] | None,
                             sibling_fields: set[str] | None = None
                             ) -> dict[str, Any]:
    """造一条**完整的**派生评分叶子行（表上成立、抽取里一行都没有的漏抽主题）。

    与抽取行同构：`SCORING_ROW_REQUIRED_FIELDS` 一格不缺，兄弟行上有而契约未列的字段
    也一并补齐（补的是什么在 `derived_schema_fill` 里点名）。这样 consumer 可以继续直读，
    不必为派生叶子另写一条宽容的读法。

    `consensus` 是同表/同卷次兄弟行的第一维类别；`None` 表示问不到兄弟行，这一条的类别
    留给调用方按**叶子自己的那一格文本**现算（同一台分类器，不是另一把尺）。

    `must_respond` 与抽取行同规则：评分表上这一格是一项**评分事项**，所以要响应；
    "要不要占技术正文覆盖率"是第二维的事，不在这里改第一格的口径。
    """
    leaf_id = str(leaf.get("leaf_id") or "")
    scoring_class, class_source = consensus or ("", "NO_LANDED_SIBLING_ROW")
    row: dict[str, Any] = {
        "scoring_item_id": f"LEAF-{leaf_id}",
        "label": str(leaf.get("leaf_text") or leaf.get("topic") or ""),
        "scoring_leaf_id": leaf_id,
        "source": leaf_source(leaf),
        "weight_numeric": leaf.get("weight"),
        "must_respond": True,
        "must_respond_basis": "DERIVED_SCORING_TABLE_LEAF",
        "scoring_class": scoring_class,
        "scoring_class_source": class_source,
        "scoring_class_after_dedup": DENOMINATOR_SCORING_CLASS,
        "response_status": "MISSING_EXTRACTION",
        "fulfillment": {"status": "NOT_GENERATED", "effective_chars": 0, "evidence_found": False,
                        "response_section": None, "response_cu": [], "evidence_text": [],
                        "artifact_location": [], "last_validated_at": None},
        "extraction_gap": True,
    }
    neutral = sorted(_DERIVED_NEUTRAL_FIELDS)
    for key, value in _DERIVED_NEUTRAL_FIELDS.items():
        row[key] = list(value) if isinstance(value, list) else value
    extra = sorted({str(key) for key in (sibling_fields or set())
                    if key not in row and key not in _DERIVED_COMPUTED_FIELDS})
    for key in extra:
        row[key] = None
    row["derived_schema_fill"] = {"neutral": neutral, "sibling_only": extra}
    return row


def scoring_row_schema_violations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """哪些行缺了下游必读字段。生产端自检用：非空就不该继续往下走。"""
    violations = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        missing = [key for key in SCORING_ROW_REQUIRED_FIELDS if key not in row]
        if missing:
            violations.append({"scoring_item_id": str(row.get("scoring_item_id") or ""),
                               "missing_fields": missing})
    return violations


def must_respond_of(item: dict[str, Any]) -> tuple[bool, str]:
    """`must_respond` 的唯一读法：pack 写过就照抄，没写过就按"评分事项默认要响应"推，
    并且**把这一格是推出来的**写在 basis 里。

    与 `.get("must_respond", False)` 的区别是要点名的：默认 False 会把"生产端没算这一格"
    读成"判定为不必响应"，分母因此悄悄变小且盘面无痕；这里推出来的是 True，
    并且 `must_respond_basis` 随行落盘，复核的人看得见哪几条的凭据弱一档。
    """
    if "must_respond" in item:
        return bool(item.get("must_respond")), "PACK_SCORING_ITEM"
    return True, "INFERRED_SCORING_ITEM_DEFAULT"


# 主题归一：评分噪声去掉后剩下的才是"这件事叫什么"。
_SCORE_SPAN = re.compile(
    r"[（(][^（）()]{0,24}?(?:满分|分值|得分|权重|分)[^（）()]{0,12}[）)]"
    r"|\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?\s*分(?:值)?"
    r"|\d+(?:\.\d+)?\s*分(?!钟|值)"
)
_LEADING_ENUM = re.compile(
    r"^(?:[一二三四五六七八九十百]+、|\d+(?:[.．、]|\s*[.．]\s*\d+)*[、.．]?|（[一二三四五六七八九十\d]{1,3}）|\([0-9a-zA-Z]{1,3}\)|[①-⑳])+"
)
_BOILERPLATE_PREFIX = re.compile(
    r"^(?:技术评分要求|技术标评分要求|评分要求|评审要求|评审因素|评分因素|评分标准|评审标准|投标人|我司)+"
)
_CONFIRM_NOTE = re.compile(r"[（(【\[]\s*待?投标人确认[^】\]）)]*[】\])]")
_HEADER_ONLY = re.compile(
    r"^(?:合计|总分|小计|总计|说明|备注|注|评分|分值|得分|评审|评分办法|评分标准|评审标准|评分细则|"
    r"评审细则|评分说明|评审说明|评分表|评审表|技术评分|商务评分|资信评分|综合评分|价格评分|技术标评分|"
    r"商务标评分|资信标评分|综合标评分|评价要素|评审因素|评分因素|评分项目|评审项目)$"
)
# 档次/公式行：本身是"怎么打分"的规则，不是一条要写进技术标的响应。
_FORMULA_MARK = re.compile(r"扣分|得分|得\d+分|按.{0,8}(?:比例|权重|档次).{0,6}计算|加权|优{1}[^，,。;；]{0,3}分|档次")
_TECHNICAL_SUBJECT_HINT = re.compile(
    r"服务|管理|方案|制度|流程|预案|措施|标准|规范|巡检|维保|保洁|绿化|秩序|安防|应急|客服|"
    r"人员|培训|质量|环境|设备|设施|投诉|回访|考核|档案|交接|移交|节能|能耗"
)
MIN_MERGE_TOPIC_CHARS = 6
TOPIC_MERGE_SIMILARITY = 0.85
TOPIC_DISPLAY_CHARS = 60


def scoring_classes() -> tuple[str, ...]:
    return SCORING_CLASSES


def scoring_statuses() -> tuple[str, ...]:
    return SCORING_STATUSES


def strip_scoring_noise(label: Any) -> str:
    """去掉分值、权重、序号与"评分要求"前缀，剩下真正的事项名。"""
    text = str(label if label is not None else "")
    for _ in range(2):
        # 两遍：招标表里常写成"（【待投标人确认】…）"，批注套了外层括号，一遍剥不干净。
        text = _CONFIRM_NOTE.sub(" ", text)
    text = re.sub(r"[（(]\s*[）)]", " ", text)
    text = _SCORE_SPAN.sub(" ", text)
    text = _LEADING_ENUM.sub(" ", text, count=1)
    text = _BOILERPLATE_PREFIX.sub(" ", text, count=1)
    return re.sub(r"\s+", " ", text).strip()


def topic_key(label: Any) -> str:
    """主题指纹：只留字母数字与汉字，用来判"这两行说的是同一件事"。"""
    return normalize_text(re.sub(r"[【】\[\]]", "", strip_scoring_noise(label)))


def canonical_topic_text(label: Any) -> str:
    """**身份承载**的主题文本：去掉分值噪声、但**不截断**。

    Round 4.6 §3 的字段语义契约：带身份的字段（`topic` / `scoring_topic`）只能装这一份，
    因为它和 `topic_key(label)` 是同一个来源，落盘记录自报的 key 与按记录自己的主题文本
    现算的 key 才会是同一个身份。
    """
    return strip_scoring_noise(label) or normalize_text(label)


def display_topic(label: Any) -> str:
    """**只用于展示**的主题名：`canonical_topic_text` 的前缀，不参与任何身份判定。

    截断是展示层的决定，所以它必须是身份文本的**前缀**（同一把归一化，不是第二套）；
    Round 4.6 之前这里被直接写进 `topic`，长主题（>TOPIC_DISPLAY_CHARS）就在同一条记录里
    造成两套互相矛盾的主题文本，真实链 Run E 的 SCN-001 就是这么被生成前门挡住的。
    """
    return canonical_topic_text(label)[:TOPIC_DISPLAY_CHARS]


def topic_similarity(left: str, right: str) -> float:
    """字符二元组 Jaccard：中文没有词边界，二元组是最不坏的主题近似度量。"""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    def bigrams(text: str) -> set[str]:
        return {text[index:index + 2] for index in range(len(text) - 1)} or {text}
    a, b = bigrams(left), bigrams(right)
    union = a | b
    return round(len(a & b) / len(union), 4) if union else 0.0


def is_non_response_row(label: Any) -> str | None:
    """表头行与档次/公式行的识别。

    判据刻意收紧：只有去掉分值噪声之后**仍然短**（≤12 字）且没有技术主题词的行才降级。
    放宽这一条会把真实评分项踢出分母 —— 那等于用分类刷覆盖率。
    """
    core = topic_key(label)
    if not core or re.fullmatch(r"[\d.%~—-]+", core):
        return "SCORING_HEADER"
    if len(core) <= 12 and not _TECHNICAL_SUBJECT_HINT.search(core):
        if _HEADER_ONLY.match(core):
            return "SCORING_HEADER"
        if _FORMULA_MARK.search(core) or re.search(r"^\d+(?:\.\d+)?%$", core):
            return "SCORING_FORMULA"
    return None


def classify_scoring_row(item: dict[str, Any], *, scope_class: str | None = None) -> str:
    """一条评分行 → 十类之一。范围判定仍以 TECHNICAL_ONLY_SCOPE_GATE 为准。"""
    return classify_scoring_row_with_source(item, scope_class=scope_class)[0]


def classify_scoring_row_with_source(
    item: dict[str, Any],
    *,
    scope_class: str | None = None,
) -> tuple[str, str]:
    """返回 (scoring_class, class_source)。

    本函数**不自己判范围**：范围类别只认 TECHNICAL_ONLY_SCOPE_GATE 一处（§23）。
    门禁没给出类别时按旧口径当作技术实质项，并把 `SCOPE_GATE_ABSENT_ASSUMED_TECHNICAL`
    写在 class_source 上 —— 缺席的门禁不能让评分项悄悄掉出分母。
    """
    label = item.get("label") or item.get("text") or ""
    non_response = is_non_response_row(label)
    # 表头/档次行要在范围映射之前判：这类行常常被范围分类当成 TECHNICAL_INCLUDED，
    # 于是"技术评分（30分）"这样一行就成了一个没人能响应的分母项。
    if non_response:
        return non_response, "NON_RESPONSE_ROW_SHAPE"
    name = str(scope_class or "")
    if not name:
        return "TECHNICAL_SUBSTANTIVE", "SCOPE_GATE_ABSENT_ASSUMED_TECHNICAL"
    if name in SCOPE_CLASS_TO_SCORING_CLASS:
        return SCOPE_CLASS_TO_SCORING_CLASS[name], "TECHNICAL_ONLY_SCOPE_GATE"
    # 门禁给出的是旧口径类别名（requirement_class / tender_content_class）时按处置回退。
    if is_excluded_technical_scope(name):
        return "PROCEDURAL", "TECHNICAL_ONLY_SCOPE_GATE"
    return "TECHNICAL_SUBSTANTIVE", "TECHNICAL_ONLY_SCOPE_GATE"


def row_scoring_classification(row: dict[str, Any]) -> tuple[str, str]:
    """矩阵行 → (第一维类别, 类别来源)。

    规划期给行打第二维、分类器给 canonical 记录打类别，必须走这同一个入口拿第一维类别：
    两处各搭一份输入字段，迟早会因为少带一个 key 而分出两种结果。
    """
    return classify_scoring_row_with_source({
        "label": row.get("label"),
        "text": row.get("label"),
        **{key: row.get(key) for key in ("requirement_type", "requirement_domain", "domain",
                                         "tender_content_class", "source")},
    }, scope_class=row.get("scope_class"))


def row_scoring_class(row: dict[str, Any]) -> str:
    return row_scoring_classification(row)[0]


def scoring_class_disposition(scoring_class: str) -> str:
    return SCORING_CLASS_DISPOSITION.get(str(scoring_class or ""), "TECHNICAL_NOT_SCORED")


def is_denominator_class(scoring_class: str) -> bool:
    return str(scoring_class or "") == DENOMINATOR_SCORING_CLASS


def evidence_contract_for(
    label: Any,
    planned_evidence: dict[str, Any] | None = None,
    *,
    responsiveness_class: str = "",
) -> dict[str, Any]:
    """§32 的 evidence_contract：规划期写下、生成后按同一份合同验收。

    要素词表直接取 `scoring_evidence.label_concepts` / 命名 rubric —— 规划期与验收期
    共用同一个分词器，否则合同上写的要素和实际去找的要素不是同一批词。

    第二维只改**证据形状**，不改严格程度：`TECHNICAL_BODY_ANONYMOUS_STRUCTURE` 必须落在
    结构化载体上（岗位/角色/阶段/工作内容表），一段"我们将配备精干团队"的散文不算回答；
    反过来它也永远不要求个人身份材料。三个不进分母的类别（证明材料 / 整本质量 /
    条款与标题）合同照写，只是用来记账与复核，不当正文的验收题。
    """
    text = str(label or "")
    planned = planned_evidence or {}
    rubric = rubric_for(text)
    if responsiveness_class == WHOLE_DOCUMENT_RUBRIC:
        return {'contract_version': SCORING_TAXONOMY_VERSION,
                'responsiveness_class': responsiveness_class,
                'rubric_id': 'GLOBAL_SERVICE_PLAN', 'acceptance_object': 'WHOLE_DOCUMENT',
                'concepts': ['服务域覆盖', '项目适配', '结构完整', '可执行责任与记录'],
                'min_concepts': 4, 'require_structured': False,
                'concept_source': 'WHOLE_DOCUMENT_QA', 'identity_material_required': False}
    if rubric:
        concepts: dict[str, tuple[str, ...]] = dict(rubric["concepts"])
        min_concepts = int(rubric["min_concepts"])
        require_structured = bool(rubric["require_structured"])
        rubric_id = str(rubric["rubric_id"])
        source = "NAMED_RUBRIC"
        segmentation = "NAMED_RUBRIC_CONCEPTS"
    else:
        concepts, segmentation = label_concepts_with_segmentation(text)
        min_concepts = 2 if len(concepts) >= 2 else 1
        require_structured = bool(planned.get("tables") or planned.get("processes"))
        rubric_id = "LABEL_DERIVED"
        source = "SCORING_LABEL_TOKENS"
    if str(responsiveness_class or "") == TECHNICAL_BODY_ANONYMOUS_STRUCTURE:
        require_structured = True
    return {
        "contract_version": SCORING_TAXONOMY_VERSION,
        "responsiveness_class": str(responsiveness_class or ""),
        "rubric_id": rubric_id,
        "concept_source": source,
        "concept_segmentation": segmentation,
        "concepts": list(concepts),
        "concept_synonyms": {key: list(values) for key, values in concepts.items()},
        "min_concepts": min_concepts,
        "require_structured": require_structured,
        "identity_material_required": False,
        "forbidden_shortcuts": [
            "COUNT_NEARBY_TEXT_AS_FULFILLED",
            "ADD_COMMERCIAL_CONTENT_FOR_COVERAGE",
            "ADD_BLANK_ATTACHMENT_PAGE_FOR_COVERAGE",
        ] + ([] if str(responsiveness_class or "") != CROSS_FILE_PROTECTED_EVIDENCE
             else ["PULL_PROOF_MATERIAL_INTO_TECHNICAL_BODY"]),
    }


def _weight_of(item: dict[str, Any]) -> tuple[float | None, str]:
    for key, source in (("weight_numeric", "pack_weight_numeric"),
                        ("weight", "pack_weight"),
                        ("max_score", "pack_max_score"),
                        ("score", "pack_score")):
        value = item.get(key)
        if isinstance(value, (int, float)) and value:
            return float(value), source
        if isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value)
            if match:
                return float(match.group()), source
    match = _SCORE_SPAN.search(str(item.get("label") or ""))
    if match:
        digits = re.search(r"\d+(?:\.\d+)?", match.group())
        if digits:
            return float(digits.group()), "label_score_span"
    return None, "unknown"


def _record_contract(head: dict[str, Any]) -> dict[str, Any]:
    """canonical 记录的证据合同：优先沿用规划期那份，形状不合才重建。

    "形状不合"只有一种情况——行上的合同是在**没有第二维**的时候写下的（旧矩阵、
    旧规划产物），或缺了本次分类所需的 `responsiveness_class`。这时按同一函数重建，
    而不是在这里另写一套分词：合同永远只有 `evidence_contract_for` 一个产地。
    """
    stored = head.get("entity_contract") or {}
    cls = str(head.get("responsiveness_class") or "")
    # 只在两种情况下按同一个产地重打合同：① 行上根本没有合同（沿用旧口径的兜底）；
    # ② 合同是第二维之前写的（带要素、但没有 `responsiveness_class` 标记）。
    # **要素为空的合同一律原样保留**：门要挡的就是那份空合同，在这里替它补齐等于
    # 把"没写合同"洗成"合同没问题"（`test_missing_evidence_contract_blocks_generation`
    # 就是这条边界的反例）。
    if not stored or (stored.get("concepts") and stored.get("responsiveness_class") != cls):
        return evidence_contract_for(head.get("label"), head.get("planned_evidence"),
                                     responsiveness_class=cls)
    return stored


def _unit_index(units: list[dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        index.setdefault(str(unit.get("section_id") or unit.get("parent_section_id") or ""), []).append(unit)
    return index


def _unit_matches_topic(unit: dict[str, Any], key: str) -> bool:
    for field in ("title", "purpose", "intent"):
        candidate = topic_key(unit.get(field))
        if not candidate:
            continue
        if key in candidate or candidate in key:
            return True
        if topic_similarity(key, candidate) >= 0.5:
            return True
    return False


def bind_planned_response_cu(
    source_ids: list[str],
    requirement_refs: list[str],
    planned_sections: list[str],
    normalized_topic: str,
    units_by_section: dict[str, list[dict[str, Any]]],
) -> tuple[list[str], dict[str, Any]]:
    """planned_response_cu 必须真绑生成计划：先按评分项 id，再按需求引用，最后按主题。"""
    ids = {str(item) for item in source_ids if item}
    refs = {str(item) for item in requirement_refs if item}
    explicit: list[str] = []
    by_requirement: list[str] = []
    by_section: list[str] = []
    by_topic: list[str] = []
    key = str(normalized_topic or "")
    candidates: list[tuple[str, dict[str, Any]]] = [
        (str(section_id), unit)
        for section_id in planned_sections
        for unit in (units_by_section.get(str(section_id)) or [])
    ]
    for section_id, unit in candidates:
        cu_id = str(unit.get("content_unit_id") or "")
        if not cu_id:
            continue
        by_section.append(cu_id)
        if ids & {str(item) for item in (unit.get("scoring_item_ids") or [])}:
            explicit.append(cu_id)
        elif refs & {str(item) for item in (unit.get("requirement_ids") or [])}:
            by_requirement.append(cu_id)
        elif key and _unit_matches_topic(unit, key):
            by_topic.append(cu_id)
    # 绑定优先级就是可信度：显式 scoring_item_ids > 需求引用 > 主题匹配。
    # 走到"无绑定"就要门控挡住，而不是把整节 CU 都塞进 planned_response_cu 装作有响应。
    for bound, basis in ((explicit, "EXPLICIT_SCORING_ITEM_IDS"),
                         (by_requirement, "REQUIREMENT_REF_LINK"),
                         (by_topic, "TOPIC_MATCH_IN_PLANNED_SECTION")):
        if bound:
            return sorted(set(bound)), {
                "basis": basis,
                "explicit_scoring_id_units": sorted(set(explicit)),
                "requirement_ref_units": sorted(set(by_requirement)),
                "topic_matched_units": sorted(set(by_topic)),
                "units_in_planned_sections": sorted(set(by_section)),
            }
    return [], {
        "basis": "UNBOUND",
        "explicit_scoring_id_units": [],
        "requirement_ref_units": [],
        "topic_matched_units": [],
        "units_in_planned_sections": sorted(set(by_section)),
    }


def unit_matches(unit: dict[str, Any], topic: str) -> bool:
    return bool(topic) and _unit_matches_topic(unit, topic_key(topic))


LEAF_LINK_CELL = "SAME_SCORING_TABLE_ROW"
LEAF_LINK_REQUIREMENT = "REQUIREMENT_LINKED_TO_UNIQUE_SCORING_TABLE_LEAF"


def _leaf_groups(substantive: list[dict[str, Any]],
                 leaves: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把技术实质行落到叶子上：先按表格行，再按需求关联，两路都写得出凭据。

    第二条路是为了"同一件事换了个地方再说一遍"：需求书里那句"组织架构中应明确列出每个管理
    成员的角色、参与阶段…"不在评分表里，但它与评分表第 8 行引用**同一条需求**（REQ-1008），
    所以它是那 8 分主题的另一种说法，不是第三件独立的事。关联只在**唯一**叶子成立时合并；
    一条需求牵到两个以上叶子时不猜，那行继续自己成题（宁可分母偏大，不可静默合并）。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for row in substantive:
        leaf_id = str(row.get("scoring_leaf_id") or "")
        if leaves and leaf_id in leaves:
            groups.setdefault(leaf_id, []).append(row)
        else:
            loose.append(row)
    if not loose:
        return groups
    requirement_to_leaves: dict[str, set[str]] = {}
    for leaf_id, bucket in groups.items():
        for row in bucket:
            for ref in row.get("requirement_ids") or []:
                requirement_to_leaves.setdefault(str(ref), set()).add(leaf_id)
    for row in loose:
        refs = {str(ref) for ref in (row.get("requirement_ids") or []) if ref}
        candidates = {leaf_id for ref in refs for leaf_id in requirement_to_leaves.get(ref, set())}
        if len(candidates) == 1:
            leaf_id = next(iter(candidates))
            row["linked_leaf_basis"] = LEAF_LINK_REQUIREMENT
            row["linked_leaf_requirement_refs"] = sorted(
                ref for ref in refs if leaf_id in requirement_to_leaves.get(ref, set()))
            groups.setdefault(leaf_id, []).append(row)
    return groups


def _volume_consensus(leaf: dict[str, Any], substantive: list[dict[str, Any]],
                      leaves: dict[str, dict[str, Any]]) -> tuple[str, str] | None:
    """漏抽叶子该按哪一类进不进分母：问**同一张表、同一卷次里已经落地的行**，不在这另判范围。

    评分表里"技术部分"这一卷已经有被判成技术实质的行，那一卷里另一行没抽出来的主题同样
    是该卷的技术主题（同一张表、同一卷次、同一评分口径）。反之"商务部分"里的漏行不会
    因为这条规则被凭空升成技术主题。判定仍然只引用第一维既有的类别，不新增第三种范围真值。

    两档作用域**同表优先**：一张评分表就是同一套评分口径，而"名称与标准挤在一格"的紧凑表
    常常根本没有卷次列。只按卷次问，这种表在表上一行都没抽出来时会拿不到共识，于是**整张表
    从分母里消失**（既不算覆盖也不算漏抽）——正是本函数要关掉的那个洞的反面。两档都问不到时
    返回 None，交回叶子自己的那一格文本走同一条第一维分类（与抽取行进分类器的路数相同），
    而不是在这里凭空造一个类别。
    """
    own_table = str((leaf.get("locator") or {}).get("table_id") or "")
    own_volume = str(leaf.get("volume") or "")
    by_table: dict[str, int] = {}
    by_volume: dict[str, int] = {}
    for row in substantive:
        other = leaves.get(str(row.get("scoring_leaf_id") or ""))
        if other is None:
            continue
        name = str(row.get("scoring_class") or "")
        if own_table and str((other.get("locator") or {}).get("table_id") or "") == own_table:
            by_table[name] = by_table.get(name, 0) + 1
        if own_volume and str(other.get("volume") or "") == own_volume:
            by_volume[name] = by_volume.get(name, 0) + 1
    for tally, basis in ((by_table, "SCORING_TABLE_CELL_CONSENSUS"),
                         (by_volume, "SCORING_TABLE_VOLUME_CONSENSUS")):
        if not tally:
            continue
        best = max(tally.items(), key=lambda item: (item[1], item[0] == DENOMINATOR_SCORING_CLASS))[0]
        return best, basis
    return None


def build_scoring_taxonomy(
    rows: list[dict[str, Any]],
    units: list[dict[str, Any]] | None = None,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """给评分矩阵行打十类标签、按**唯一可独立响应主题**去重、产出 canonical 记录。

    `rows` 是 `build_scoring_matrix` 已经映射好章节与需求引用的行：本函数不再重新
    路由，只负责"这行算不算一项技术响应、和哪一行是同一件事"。
    `units` 是分层树里的 Content Unit；缺席时（例如手写矩阵）退回行自带的
    `planned_response_cu`，并把绑定口径标成 ROW_PLANNED_CU_FALLBACK，不冒充真绑计划。
    `graph` 是 `scoring_graph.build_scoring_graph` 的产物（评分表单元格结构）。有图时
    **主题身份 = 叶子**：同一叶子的多条抽取行折成一条记录（"项目管理团队（8分）"不再算
    三件事），分类读叶子文本（"服务保障能力"不再因为只剩半句评价语而丢 5 分），
    图上成立而抽取里一行都没有的叶子（漏抽）照样成为一条记录并具名 `extraction_gap`。
    没有图（旧产物、手写矩阵、评分表解析不出来）时才退回上一轮的按文本相似度折叠，
    并把每条记录的身份来源写清楚，两种口径不混在一格里。
    """
    leaves = leaf_by_id(graph) if (graph or {}).get("status") == "DERIVED" else {}
    lineage_index = rubric_lineage_index(graph)
    units_by_section = _unit_index(units)
    row_fallback = units is None

    def annotate(row: dict[str, Any], *, forced_class: tuple[str, str] | None = None
                 ) -> dict[str, Any]:
        item = dict(row)
        scoring_class, class_source = forced_class or row_scoring_classification(item)
        item["scoring_class"] = scoring_class
        item["scoring_class_source"] = class_source
        item["scoring_class_disposition"] = scoring_class_disposition(scoring_class)
        # 折叠之后的类别也在**生产端**落定：不进分母的行（资格、资信、价格…）从来走不到下面
        # 那两段折叠逻辑，若这里不配平，下游读 `row["scoring_class_after_dedup"]` 就会在
        # 某一类行上 KeyError —— 而消费端为了躲这一脚写的 `.get(..., 默认)` 正是本轮明令
        # 禁止的写法（少一格要在生产端炸，不许在消费端兜）。中性值取"没被折叠，仍是自己"。
        item.setdefault("scoring_class_after_dedup", scoring_class)
        item.setdefault("duplicate_of", None)
        item["scoring_topic"] = canonical_topic_text(item.get("label"))
        item["scoring_topic_display"] = display_topic(item.get("label"))
        item["scoring_topic_key"] = topic_key(item.get("label"))
        # `must_respond` 与其凭据同源同处配齐（`must_respond_of` 是唯一读法）：这一格在
        # pack/hierarchy 那边写过就照抄，没写过就按"评分事项默认要响应"推并具名。
        # 只补**没说过话**的行：派生叶子已经带着 `DERIVED_SCORING_TABLE_LEAF` 这格更具体的
        # 凭据，用它盖掉等于把"这一条从哪来"抹平——补格子不是重新判定。
        if "must_respond_basis" not in item:
            item["must_respond"], item["must_respond_basis"] = must_respond_of(item)
        item["weight_if_known"], item["weight_source"] = _weight_of(item)
        # 第二维：与第一维正交，只看"技术正文能不能、该不该回答它"。
        # 读的那一格由 `scoring_graph` 一处决定：能落到叶子就读"事项名 + 本行评分标准"，
        # 落不到才读打平标签——两处各挑一份文本迟早分叉。
        structural_text, text_basis = response_text_for_item(item, leaves)
        leaf = leaves.get(str(item.get("scoring_leaf_id") or ""))
        if leaf is not None:
            item["scoring_table_topic"] = str(leaf.get("topic") or "")
            item["tender_locator"] = dict(leaf.get("locator") or {})
            # 分值以**表格那一格**为准：抽取器从打平标签里抓到的第一个"N分"经常是
            # 卷次总分或某一档的分（"技术部分（40分）| 项目管理团队（8分）"抓成 40），
            # 它不是这一件事的分值。落在叶子上就用叶子的分值，抓取值原样留在
            # `weight_scraped_from_text` 里可查——覆盖它不等于丢掉它。
            structural = leaf.get("weight")
            if structural is not None:
                scraped = item.get("weight_if_known")
                if scraped is not None and abs(float(scraped) - float(structural)) > 1e-9:
                    item["weight_scraped_from_text"] = float(scraped)
                    item["weight_scraped_source"] = str(item.get("weight_source") or "")
                item["weight_if_known"] = float(structural)
                item["weight_source"] = str(leaf.get("weight_basis") or "scoring_table_cell")
        item.update(responsiveness_fields(item.get("label"), scoring_class=scoring_class,
                                         structural_text=structural_text, text_basis=text_basis,
                                         lineage=rubric_lineage(
                                             item, graph, leaves=leaves, index=lineage_index)))
        return item

    annotated: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        annotated.append(annotate(row))

    substantive = [row for row in annotated if is_denominator_class(row["scoring_class"])]
    canonical: list[dict[str, Any]] = []
    leaf_groups = _leaf_groups(substantive, leaves)
    groups: list[list[dict[str, Any]]] = []
    group_meta: list[dict[str, Any]] = []
    for leaf_id, bucket in leaf_groups.items():
        for position, row in enumerate(bucket):
            if position:
                row["duplicate_of"] = bucket[0].get("scoring_item_id")
                row["duplicate_similarity"] = 1.0
                row["duplicate_basis"] = "SAME_SCORING_TABLE_ROW" if not row.get("linked_leaf_basis") \
                    else row["linked_leaf_basis"]
                row["scoring_class_after_dedup"] = "DUPLICATE_TOPIC"
                row["scoring_class_disposition_after_dedup"] = SCORING_CLASS_DISPOSITION["DUPLICATE_TOPIC"]
            else:
                row["duplicate_of"] = None
                row["scoring_class_after_dedup"] = DENOMINATOR_SCORING_CLASS
        groups.append(bucket)
        group_meta.append({"identity_basis": "SCORING_TABLE_CELL", "leaf_id": leaf_id,
                           "extraction_gap": False})
    folded = {id(row) for bucket in leaf_groups.values() for row in bucket}
    for row in substantive:
        if id(row) in folded:
            continue
        key = row["scoring_topic_key"]
        placed = False
        if key:
            for group in groups:
                head = group[0]
                similar = topic_similarity(head["scoring_topic_key"], key)
                if (similar >= TOPIC_MERGE_SIMILARITY
                        and min(len(head["scoring_topic_key"]), len(key)) >= MIN_MERGE_TOPIC_CHARS):
                    group.append(row)
                    row["duplicate_of"] = head["scoring_item_id"]
                    row["duplicate_similarity"] = similar
                    row["scoring_class_after_dedup"] = "DUPLICATE_TOPIC"
                    row["scoring_class_disposition_after_dedup"] = SCORING_CLASS_DISPOSITION["DUPLICATE_TOPIC"]
                    placed = True
                    break
        if not placed:
            groups.append([row])
            group_meta.append({"identity_basis": "ROW_TEXT_SIMILARITY" if leaves
                               else "NO_SCORING_TABLE_GRAPH", "leaf_id": None,
                               "extraction_gap": False})
            row["duplicate_of"] = None
            row["scoring_class_after_dedup"] = DENOMINATOR_SCORING_CLASS

    # 结构上是一条可响应主题、抽取里却一行都没有：这不是"没有这条"，是**漏抽**。
    # 它必须进分母（否则漏抽反而把覆盖率刷高），并带上 `extraction_gap` 让人看得见。
    # 卷次/同表的定性沿用已经落地的兄弟行（范围门仍是唯一真值），问不到兄弟行才用它自己
    # 那一格文本走同一条分类；这里不另立一处判范围。
    for leaf_id, leaf in leaves.items():
        if leaf_id in leaf_groups:
            continue
        if leaf.get("attached_scoring_ids"):
            # 抽取里有行、只是那些行被判成"非技术实质/由别处举证/整本质量"：那是判定结果，
            # 不是漏抽，不能用卷次共识把已判过的行翻案。卷次共识只服务**一行都没抽出来**的叶子。
            continue
        own_label = str(leaf.get("leaf_text") or leaf.get("topic") or "")
        # 合计/总计/小计/总分是表尾，不是漏抽的技术事项。同表兄弟行几乎全是技术实质，
        # 卷次共识会把这一格继承成可响应主题，然后因为凑不出证据词把整场生成挡在门外。
        # 这种格子按自己的文本走表头分类，留在行账里供复核，不进分母。
        if is_non_response_row(own_label):
            gap_row = annotate(derived_scoring_leaf_row(
                leaf, consensus=None,
                sibling_fields={key for row in annotated for key in row}))
            annotated.append(gap_row)
            continue
        consensus = _volume_consensus(leaf, substantive, leaves)
        # 两档共识都问不到（这张表一行都没抽出来）时**不再整条丢掉**：漏抽叶子拿自己那一格
        # 文本走同一条第一维分类，与抽取行进分类器的路数完全相同。少这一档，紧凑表（没有
        # 卷次列）会连"漏抽"这格都留不下，表上的分值就这样从分母里静默消失。
        # 派生叶子与抽取行**共用同一份行 schema**（`SCORING_ROW_REQUIRED_FIELDS`）：
        # 下游读的是直读，少一格就是正式链中间 KeyError（上一轮 `LEAF-TAB-00004-R14`）。
        gap_row = annotate(derived_scoring_leaf_row(
            leaf, consensus=consensus,
            sibling_fields={key for row in annotated for key in row}),
            forced_class=consensus)
        annotated.append(gap_row)
        substantive.append(gap_row)
        leaf_groups[leaf_id] = [gap_row]
        groups.append([gap_row])
        group_meta.append({"identity_basis": "SCORING_TABLE_CELL", "leaf_id": leaf_id,
                           "extraction_gap": True})

    for index, (group, meta) in enumerate(zip(groups, group_meta), start=1):
        head = group[0]
        leaf = leaves.get(str(meta.get("leaf_id") or ""))
        source_ids = [str(row.get("scoring_item_id")) for row in group
                      if row.get("scoring_item_id") and not str(row.get("scoring_item_id")).startswith("LEAF-")]
        requirement_refs = sorted({str(ref) for row in group for ref in (row.get("requirement_ids") or []) if ref})
        planned_sections: list[str] = []
        for row in group:
            for section_id in ([row.get("primary_section_id")] if row.get("primary_section_id") else []) + list(row.get("section_ids") or []):
                if section_id and section_id not in planned_sections:
                    planned_sections.append(str(section_id))
        # 重复行的分值是同一件事的分值，取最大值而不是求和：求和会把去重省下的分又造出来。
        weighted = [(float(row["weight_if_known"]), str(row.get("weight_source") or "unknown"))
                    for row in group if row.get("weight_if_known") is not None]
        # 有叶子时分值直接取**表格里那一格**：行上的 `weight_numeric` 是抽取器从文本里
        # 抓的第一个"N分"，抓到的常常是某一档的分（"优得6-8分"抓成 6），不是本项分值。
        structural_weight = leaf.get("weight") if leaf else None
        if row_fallback:
            planned_cu = sorted({str(cu) for row in group for cu in (row.get("planned_response_cu") or [])})
            binding = {
                "basis": "ROW_PLANNED_CU_FALLBACK" if planned_cu else "UNBOUND",
                "explicit_scoring_id_units": [],
                "requirement_ref_units": [],
                "topic_matched_units": [],
                "units_in_planned_sections": planned_cu,
            }
        else:
            # 主题线索优先用评分表的事项名（"项目管理团队"），它比打平行文本短且准；
            # 没有图才退回头行的 topic_key。
            planned_cu, binding = bind_planned_response_cu(
                source_ids, requirement_refs, planned_sections,
                topic_key(leaf["topic"]) if leaf and leaf.get("topic")
                else str(head.get("scoring_topic_key") or ""),
                units_by_section)
        record = {
            "canonical_scoring_id": f"SCN-{index:03d}",
            "source_ids": source_ids,
            "evaluation_row_ids": [str(row["scoring_item_id"]) for row in group if row.get("scoring_item_id")],
            "topic": head.get("scoring_topic") or "",
            # `topic` 是**身份承载**字段：装的是不截断的 canonical 主题文本，与 `topic_key`
            # 同源。展示用的截断形单列在 `display_topic` 里，Round 4.6 §3 的字段语义契约。
            "display_topic": head.get("scoring_topic_display") or "",
            # 原始标签必须随记录走：`scoring_topic` 是**去掉分值噪声之后**的事项名，
            # "优势2分并提出管理建议2分"这类信息整段被剥掉了。种类识别只看 topic 就会
            # 永远认不出"项目优势或管理建议"这一类主题（Run D SCR-041 实测）。
            "label": head.get("label") or "",
            "topic_key": head.get("scoring_topic_key") or "",
            "scoring_class": DENOMINATOR_SCORING_CLASS,
            "weight_if_known": (float(structural_weight) if structural_weight is not None
                                else (max(value for value, _ in weighted) if weighted else None)),
            "weight_source": (str(leaf.get("weight_basis") or "scoring_table_cell")
                              if structural_weight is not None
                              else (max(weighted)[1] if weighted else "unknown")),
            "requirement_refs": requirement_refs,
            "planned_response_section": planned_sections[0] if planned_sections else None,
            "planned_response_sections": planned_sections,
            "planned_response_cu": planned_cu,
            "cu_binding": binding,
            "evidence_contract": _record_contract(head),
            "dedup": {
                "duplicate_source_ids": source_ids[1:],
                "rows_in_topic": len(group),
                "merge_basis": "UNIQUE_SUBSTANTIVE_TOPIC" if len(group) > 1 else "SINGLE_ROW",
                "merge_similarity_threshold": TOPIC_MERGE_SIMILARITY,
            },
            "response_status": head.get("response_status"),
            # 主题身份的来路：表格单元格 / 文本相似度 / 没有图。三种口径同盘可见，
            # 下一轮复核不必猜"这条是按哪把尺折出来的"。
            "identity_basis": meta["identity_basis"],
            "scoring_leaf_id": meta.get("leaf_id"),
            "scoring_table_topic": (leaf or {}).get("topic") or "",
            "parent_group": (leaf or {}).get("parent_group"),
            "volume": (leaf or {}).get("volume") or "",
            "tender_locator": (leaf or {}).get("locator") or {},
            # 表上写着这一项有分值、抽取里却一行都没有 → 漏抽，不是"没有这条"。
            "extraction_gap": bool(meta.get("extraction_gap")),
            "structural_weight_basis": (leaf or {}).get("weight_basis"),
        }
        # 第二维从头部行照抄（不在这里重算）：主题折叠后"这件事叫什么"由头行决定，
        # 类别也必须由同一份标签决定，否则换一条重复行当头部就会换一套分类。
        record.update({key: head.get(key) for key in RESPONSIVENESS_RECORD_FIELDS})
        canonical.append(record)

    by_source = {str(cu): record["canonical_scoring_id"]
                 for record in canonical for cu in record.get("evaluation_row_ids", record["source_ids"])}
    for row in substantive:
        row["canonical_scoring_id"] = by_source.get(str(row.get("scoring_item_id") or ""))

    counts: dict[str, int] = {name: 0 for name in SCORING_CLASSES}
    dimension_counts: dict[str, int] = {name: 0 for name in RESPONSE_DIMENSIONS}
    respondable: list[dict[str, Any]] = []
    # 分母的**组成**（两把尺并列）：主题条数与主题分值。上一轮只报条数，于是"丢了 5 分
    # 与 3 分两条主题"和"少了一条 1 分的表头行"读起来是同一个 -2。
    composition: dict[str, dict[str, Any]] = {
        name: {"topics": 0, "weight_points": 0.0, "topic_ids": [],
               "counts_toward_gate": name in TECHNICAL_ACCEPTANCE_DIMENSIONS}
        for name in RESPONSE_DIMENSIONS}
    for record in canonical:
        dimension = str(record.get("responsiveness_class") or "")
        dimension_counts[dimension] = dimension_counts.get(dimension, 0) + 1
        bucket = composition.setdefault(dimension, {"topics": 0, "weight_points": 0.0, "topic_ids": [],
                                                    "counts_toward_gate": False})
        bucket["topics"] += 1
        bucket["weight_points"] += float(record.get("weight_if_known") or 0.0)
        bucket["topic_ids"].append(str(record.get("canonical_scoring_id") or ""))
        if record_requires_technical_acceptance(record):
            respondable.append(record)
    identity_basis_counts: dict[str, int] = {}
    for record in canonical:
        name = str(record.get("identity_basis") or "")
        identity_basis_counts[name] = identity_basis_counts.get(name, 0) + 1
    sources: dict[str, int] = {}
    for row in annotated:
        name = str(row.get("scoring_class_after_dedup") or row.get("scoring_class"))
        counts[name] = counts.get(name, 0) + 1
        source = str(row.get("scoring_class_source") or "")
        sources[source] = sources.get(source, 0) + 1
    # 生产端自检：本函数**自己造出来**的行（派生叶子）缺任何一格下游必读字段就不许流出去。
    # 只对合成行抛：抽自招标的行由 `build_scoring_matrix` 生产，那里的契约由正式链测试对表，
    # 在这里替别人的产品抛错等于把两处 schema 混成一处。
    synthesized = [row for row in annotated if row.get("extraction_gap")]
    violations = scoring_row_schema_violations(synthesized)
    if violations:
        detail = "; ".join("{} 缺 {}".format(row["scoring_item_id"], ",".join(row["missing_fields"]))
                           for row in violations)
        raise ValueError("派生评分叶子行缺下游必读字段（生产端未配齐 schema）：" + detail)
    return {
        "schema_version": SCORING_TAXONOMY_VERSION,
        "row_schema_contract": {
            "required_fields": list(SCORING_ROW_REQUIRED_FIELDS),
            "checked_rows": len(synthesized),
            "violations": violations,
        },
        "allowed_scoring_classes": list(SCORING_CLASSES),
        "allowed_scoring_statuses": list(SCORING_STATUSES),
        "allowed_response_dimensions": list(RESPONSE_DIMENSIONS),
        "responsiveness_version": RESPONSIVENESS_VERSION,
        "disposition_by_class": dict(SCORING_CLASS_DISPOSITION),
        "disposition_by_response_dimension": dict(RESPONSE_DIMENSION_DISPOSITION),
        "fulfillment_required_response_dimensions": list(FULFILLMENT_REQUIRED_DIMENSIONS),
        "scope_class_to_scoring_class": dict(SCOPE_CLASS_TO_SCORING_CLASS),
        "scope_classes_gated": list(TECHNICAL_SCOPE_CLASSES),
        "scope_disposition_reference": dict(TECHNICAL_SCOPE_DISPOSITION),
        "rows": annotated,
        "canonical_scoring_records": canonical,
        "class_counts": counts,
        "class_source_counts": sources,
        "response_dimension_counts": dimension_counts,
        "denominator": {
            "basis": COVERAGE_DENOMINATOR,
            "class": DENOMINATOR_SCORING_CLASS,
            "canonical_total": len(canonical),
            # 第二维：分母只算"技术正文可响应"的那部分主题，其余三类照旧登记不响应。
            # 旧口径（十类第一维全量）作为并列字段一起给，换分母不藏数字。
            "respondable_total": len(respondable),
            "respondable_ids": [str(record.get("canonical_scoring_id") or "")
                                for record in respondable],
            "not_respondable_total": len(canonical) - len(respondable),
            "all_classes_basis": COVERAGE_DENOMINATOR_ALL_CLASSES,
            "raw_substantive_rows": len(substantive),
            "merged_duplicate_rows": len(substantive) - len(canonical),
            "raw_scoring_rows": len(annotated),
            "rows_not_in_denominator": len(annotated) - len(substantive),
            # 组成而不是总数：每个类别各报条数与分值，进不进门的标志跟着类别走。
            "composition_by_response_dimension": composition,
            "respondable_weight_points": round(sum(float(record.get("weight_if_known") or 0.0)
                                                   for record in respondable), 4),
            "canonical_weight_points": round(sum(float(record.get("weight_if_known") or 0.0)
                                                 for record in canonical), 4),
            # 身份口径的可见性：按表格单元格折出来的主题有多少、按文本折叠的有多少、
            # 表上写着有值而抽取里一行都没有（漏抽）的是哪几条。
            "identity_basis_counts": identity_basis_counts,
            "extraction_gap_ids": [str(record.get("canonical_scoring_id") or "")
                                   for record in canonical if record.get("extraction_gap")],
            "scoring_graph_status": (graph or {}).get("status") or "ABSENT",
            "scoring_graph_basis": (graph or {}).get("basis") or "NO_SCORING_TABLE_CELL_STRUCTURE",
        },
    }


def canonical_status(source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一个主题的多行证据合成一个 §26 状态。NEARBY_ONLY 不算 fulfilled。"""
    fulfillments = [row.get("fulfillment") or {} for row in source_rows]
    statuses = [str(row.get("status") or "") for row in fulfillments]
    if FULFILLED_SCORING_STATUS in statuses:
        status = "FULFILLED"
    elif "PARTIAL" in statuses:
        status = "PARTIAL"
    elif "NEARBY_ONLY" in statuses:
        status = "NEARBY_ONLY"
    elif any(item in {"MISSING", "NOT_GENERATED"} for item in statuses):
        status = "MISSING"
    elif not statuses or all(item in {"", "NOT_GENERATED"} for item in statuses):
        status = "NOT_APPLICABLE"
    else:
        status = "MISSING"
    sections: list[str] = []
    units: list[str] = []
    for row in fulfillments:
        located = [str(sid) for sid in (row.get("response_sections") or []) if sid]
        if not located and row.get("response_section"):
            located = [str(row["response_section"])]
        sections.extend(located)
        units.extend(str(cu) for cu in (row.get("response_cu") or []))
    return {
        "status": status,
        "source_statuses": statuses,
        "counts_rows": len(source_rows),
        "effective_chars": max((int(row.get("effective_chars") or 0) for row in fulfillments), default=0),
        "response_sections": sorted(set(sections)),
        "response_cu": sorted(set(units)),
        "evidence_found": any(bool(row.get("evidence_found")) for row in fulfillments),
        "entity_backed": any(bool(row.get("entity_backed")) for row in fulfillments),
    }


def pre_generation_scoring_mapping_gate(
    matrix: dict[str, Any],
    *,
    planned_unit_ids: list[str] | None = None,
    planned_section_ids: list[str] | None = None,
    planned_unit_sections: dict[str, str] | None = None,
) -> dict[str, Any]:
    """§34：生成前映射门。缺 planned section / planned CU / evidence contract 的实质
    评分主题不得进入正式 300-Class 生成 —— 计划阶段就没落点的事，写完也不会有证据。

    MMF008A-A：这一格门现在查的是**两维相乘**之后的落点义务。第一维留下的技术实质主题里，
    只有第二维判为"正文可写"的两类（可生成正文 / 匿名组织设计）必须有 planned section 与
    planned CU；另外三类（证明材料在别的文件 / 整本质量口径 / 条款与程序语）不落点，
    但**必须**出现在 `non_respondable_registered` 与 `denominator_shrink_watch` 里，
    并且反过来查一条：它们一旦被派生成 Technical Response 内容单元，就是泄漏
    （为了覆盖率往技术正文里塞资格证明材料）。少掉一条主题可以，少掉一条账不行。

    R41 §23 再收紧一格：给了 `planned_unit_ids`（生成计划里真实存在的内容单元清单）时，
    绑定还必须**指得到人**——写着 `planned_response_cu` 却在计划里查无此单元，同样是缺陷。
    这一条防的是"自动映射自己造出一个不存在的落点来把门骗过去"。

    Final Gate §5（Blocker B）把这格门补齐成**引用完整性**门，两处旧写法都不留：
      ① 过去只看 `planned_response_section` 非空，不验它在不在计划里 —— 写着
         `S99-99` 这种查无此节的 ID 照样 PASS；
      ② CU 过去判的是"绑定集合与计划清单**有交集**"，于是 `[有效, 无效]` 混着也能过 ——
         一个有效 CU 掩护一个幽灵落点。现在要求**声明为正式落点的每一个**都在计划里
         （§5.3："不要使用 any(valid) == PASS"）。
    没有清单可对照时（`planned_*_ids is None`）不猜"有效"，只在 `rules` 里具名声明
    "引用未对账"，让读门的人知道这一版门查到了哪一步。
    """
    records = [row for row in (matrix or {}).get("canonical_scoring_records") or [] if isinstance(row, dict)]
    known_units = ({str(item) for item in planned_unit_ids} if planned_unit_ids is not None else None)
    known_sections = ({str(item) for item in planned_section_ids}
                      if planned_section_ids is not None else None)
    defects: list[dict[str, Any]] = []
    leaks: list[dict[str, Any]] = []
    registered_not_required: list[dict[str, Any]] = []

    def _header_topic(record: dict[str, Any]) -> bool:
        return bool(is_non_response_row(
            record.get("label") or record.get("topic") or record.get("display_topic") or ""))

    for record in records:
        # 已经落盘的旧矩阵可能把"合计"记成技术实质（同表共识继承）。主题文本本身是表头时，
        # 不再向它要证据合同，也不把当时派生出的内容单元当成商务内容泄漏。
        header_topic = _header_topic(record)
        body_required = False if header_topic else record_requires_body_response(record)
        if not body_required:
            # 第二维把它请出了"正文实体响应"的账，但它必须继续可见：类别、命中标记词、
            # 由谁判、证明材料登记在哪，逐条列出来给人复核。静默消失才是作弊。
            entry = {
                "canonical_scoring_id": record.get("canonical_scoring_id"),
                "topic": record.get("display_topic") or record.get("topic"),
                "source_ids": record.get("source_ids") or [],
                "responsiveness_class": record.get("responsiveness_class"),
                "responsiveness_class_source": record.get("responsiveness_class_source"),
                "matched_markers": (record.get("responsiveness_evidence") or {}).get("matched_markers") or {},
                "response_requirement": record.get("response_requirement"),
                "judged_by_whole_document_dimensions": list(
                    record.get("judged_by_whole_document_dimensions") or []),
                "protected_evidence_terms": list(record.get("protected_evidence_terms") or []),
                "must_respond": bool(record.get("must_respond")),
                "weight_if_known": record.get("weight_if_known"),
                "planned_response_cu": [str(item) for item in (record.get("planned_response_cu") or [])],
                "mapping_basis": record.get("mapping_basis"),
            }
            registered_not_required.append(entry)
            if (matrix.get('response_point_contract_version')
                    and record.get('responsiveness_class') == WHOLE_DOCUMENT_RUBRIC
                    and not record.get('global_acceptance_plan')):
                defects.append({'canonical_scoring_id': record.get('canonical_scoring_id'),
                                'topic': record.get('topic'), 'missing': ['GLOBAL_ACCEPTANCE_PLAN']})
            # 反过来的滥用同样要挡：一条"证明材料在资格文件里"的主题被派生出一个
            # Technical Response 内容单元，就是拿覆盖率当理由往技术正文里塞资格材料。
            if (str(record.get("mapping_basis") or "") == "DERIVED_TECHNICAL_RESPONSE_CU"
                    and not header_topic):
                leaks.append({
                    "canonical_scoring_id": record.get("canonical_scoring_id"),
                    "scoring_class": record.get("scoring_class"),
                    "responsiveness_class": record.get("responsiveness_class"),
                    "section_ids": list(record.get("planned_response_sections") or []),
                    "planned_response_cu": [str(item) for item in (record.get("planned_response_cu") or [])],
                    "leak_kind": "TECHNICAL_BODY_RESPONSE_DERIVED_FOR_NON_RESPONDABLE_TOPIC",
                })
            continue
        missing: list[str] = []
        declared_sections = [str(item) for item in (
            [record.get("planned_response_section")] if record.get("planned_response_section")
            else []) + list(record.get("planned_response_sections") or []) if str(item)]
        if not declared_sections:
            missing.append("PLANNED_SECTION")
        invalid_sections = (sorted(set(declared_sections) - known_sections)
                            if known_sections is not None else [])
        if invalid_sections:
            missing.append("PLANNED_SECTION_NOT_IN_PLAN")
        bound = [str(item) for item in (record.get("planned_response_cu") or [])]
        if not bound:
            missing.append("PLANNED_CONTENT_UNIT")
        invalid_units = (sorted(set(bound) - known_units)
                         if known_units is not None else [])
        if invalid_units:
            missing.append("PLANNED_CONTENT_UNIT_NOT_IN_PLAN")
        contract = record.get("evidence_contract") or {}
        if not (contract.get("concepts") or []) or int(contract.get("min_concepts") or 0) < 1:
            missing.append("EVIDENCE_CONTRACT")
        if contract.get('source_obligation_conflicts'):
            missing.append('SOURCE_OBLIGATION_CONFLICT')
        if matrix.get('response_point_contract_version'):
            points = record.get('planning_response_points') or []
            if not points or any(not p.get('expected_final_evidence_location')
                                 or set(contract.get('concepts') or []) - set(p.get('required_concepts') or [])
                                 or p.get('content_unit_id') not in bound
                                 or p.get('section_id') not in declared_sections
                                 or p.get('topic_id') != record.get('canonical_scoring_id')
                                 or (planned_unit_sections is not None and planned_unit_sections.get(str(p.get('content_unit_id'))) != p.get('section_id')) for p in points):
                missing.append('EXPLICIT_RESPONSE_POINT')
        if missing:
            defects.append({
                "canonical_scoring_id": record.get("canonical_scoring_id"),
                "topic": record.get("topic"),
                "source_ids": record.get("source_ids") or [],
                "missing": missing,
                "responsiveness_class": record.get("responsiveness_class"),
                "responsiveness_class_source": record.get("responsiveness_class_source"),
                "declared_sections": sorted(set(declared_sections)),
                "invalid_sections": invalid_sections,
                "declared_content_units": sorted(set(bound)),
                "invalid_content_units": invalid_units,
                "cu_binding_basis": (record.get("cu_binding") or {}).get("basis"),
                "mapping_basis": record.get("mapping_basis"),
                "mapping_confidence": record.get("mapping_confidence"),
            })
    # 范围排除类一旦被规划出落点，就是 Technical-Only 泄漏：评分表里的报价项
    # 绝不能因为"提高覆盖率"而进技术正文。
    for row in (matrix or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        disposition = scoring_class_disposition(str(row.get("scoring_class") or ""))
        if disposition == EXCLUDED_SCORING_DISPOSITION and (
                row.get("section_ids") or row.get("planned_response_cu")
                or row.get("response_status") in {"PLANNED", "DERIVED_PLANNED"}):
            leaks.append({
                "scoring_item_id": row.get("scoring_item_id"),
                "scoring_class": row.get("scoring_class"),
                "section_ids": row.get("section_ids") or [],
                "planned_response_cu": row.get("planned_response_cu") or [],
            })
    total = len(records)
    respondable = [record for record in records
                   if record_requires_body_response(record) and not _header_topic(record)]
    respondable_total = len(respondable)
    rows_by_id = {str(row.get("scoring_item_id") or ""): row
                  for row in (matrix or {}).get("rows") or [] if isinstance(row, dict)}
    # 分母由 TECHNICAL_ONLY_SCOPE_GATE 给，分类器不自己判范围 —— 但"招标写着必须响应/
    # 有分值，范围门却判成只做参考"的条目一旦离开分母，覆盖率就会悄悄变好。逐条列出来
    # 交给复核，不改门的状态：这是范围分类器的账，不能靠评分门替它下结论。
    shrink_watch: list[dict[str, Any]] = []
    for row in (matrix or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("scoring_class") or "") != "TECHNICAL_CONTEXTUAL":
            continue
        if not (row.get("must_respond") or row.get("weight_if_known") is not None):
            continue
        shrink_watch.append({
            "scoring_item_id": row.get("scoring_item_id"),
            # 这份清单是给人复核的，用展示形；身份判定一律走 `scoring_topic` / `scoring_topic_key`。
            "topic": row.get("scoring_topic_display") or row.get("scoring_topic"),
            "scope_class": row.get("scope_class"),
            "scoring_class_source": row.get("scoring_class_source"),
            "must_respond": bool(row.get("must_respond")),
            "weight_if_known": row.get("weight_if_known"),
            "watch_axis": "RESPONSE_CLASS_FIRST_DIMENSION",
        })
    # MMF008A-A 第二维的同一条账：一条主题离开"正文实体响应"的账，必须在这里点名。
    # 招标写着必须响应、或带分值，却被第二维分到证明材料/整本质量/不响应三类的，
    # 逐条列出来交人工复核 —— 分类是给人看的判据，不是给机器吞掉的开关。
    for entry in registered_not_required:
        source_rows = [rows_by_id[str(sid)] for sid in entry.get("source_ids") or []
                       if str(sid) in rows_by_id]
        must_respond = any(bool(row.get("must_respond")) for row in source_rows)
        weights = [row.get("weight_if_known") for row in source_rows
                   if row.get("weight_if_known") is not None]
        if not (must_respond or weights or entry.get("planned_response_cu")):
            continue
        shrink_watch.append({
            "canonical_scoring_id": entry.get("canonical_scoring_id"),
            "scoring_item_id": (entry.get("source_ids") or [None])[0],
            "topic": entry.get("topic"),
            "scoring_class": (source_rows[0].get("scoring_class") if source_rows else None),
            "responsiveness_class": entry.get("responsiveness_class"),
            "responsiveness_class_source": entry.get("responsiveness_class_source"),
            "matched_markers": entry.get("matched_markers"),
            "must_respond": must_respond,
            "weight_if_known": (max(weights) if weights else None),
            "planned_response_cu": entry.get("planned_response_cu"),
            "watch_axis": "RESPONSIVENESS_CLASS_SECOND_DIMENSION",
        })
    status = ("BLOCKED_SCORING_MAPPING_INCOMPLETE" if defects
              else ("TECHNICAL_SCORING_LEAK" if leaks
                    else ("NOT_APPLICABLE" if not respondable_total else "PASS")))
    reasons: list[str] = []
    if defects:
        reasons.append(
            f"{len(defects)}/{respondable_total} 个正文可响应的唯一技术实质评分主题缺 "
            + "、".join(sorted({str(field) for defect in defects for field in defect["missing"]}))
        )
    if leaks:
        reasons.append(
            f"{len(leaks)} 个范围排除类评分项被规划出落点（"
            + "、".join(sorted({str(leak.get("scoring_class") or "") for leak in leaks}))
            + "）：不得为提高覆盖率把商务/报价/资信内容写进技术正文"
        )
    return {
        "schema_version": SCORING_TAXONOMY_VERSION,
        "responsiveness_version": RESPONSIVENESS_VERSION,
        "evaluated_stage": "PRE_GENERATION",
        "status": status,
        "may_enter_generation": not defects and not leaks,
        "blocked_reasons": reasons,
        "planned_coverage": (matrix or {}).get("coverage_summary") or {},
        # 两个总数并列：`substantive_topic_total` 仍是第一维十类口径下的唯一实质主题数
        # （R46 的 topic identity 账走这个数），`respondable_topic_total` 才是本次门
        # 检查落点用的那一份。少了后者，读门的人无法知道分母为什么变小。
        "substantive_topic_total": total,
        "respondable_topic_total": respondable_total,
        'technical_acceptance_topic_total': sum(record_requires_technical_acceptance(r) for r in records),
        'whole_document_acceptance_topic_total': sum(r.get('responsiveness_class') == WHOLE_DOCUMENT_RUBRIC for r in records),
        "not_respondable_topic_total": total - respondable_total,
        "mapped_total": respondable_total - len(defects),
        "unmapped_total": len(defects),
        "unmapped_records": defects,
        "non_respondable_registered": registered_not_required,
        "non_respondable_registered_total": len(registered_not_required),
        "response_dimension_counts": {
            name: sum(1 for record in records
                      if str(record.get("responsiveness_class") or "") == name)
            for name in RESPONSE_DIMENSIONS
        },
        "excluded_class_planned_leaks": leaks,
        "denominator_shrink_watch": shrink_watch,
        "denominator_shrink_watch_total": len(shrink_watch),
        "denominator_shrink_watch_second_dimension_total": sum(
            1 for item in shrink_watch
            if str(item.get("watch_axis") or "") == "RESPONSIVENESS_CLASS_SECOND_DIMENSION"),
        # Run D 实测：真实评分包里 must_respond 常常整表为真，只按它列清单会有噪声；
        # 带明确分值的那几条才是真正"丢了就丢分"的，所以两个数分开报。
        "denominator_shrink_watch_scored_total": sum(1 for item in shrink_watch
                                                      if item["weight_if_known"] is not None),
        "rules": {
            "denominator_class": DENOMINATOR_SCORING_CLASS,
            "denominator_authority": "TECHNICAL_ONLY_SCOPE_GATE",
            "denominator_basis": COVERAGE_DENOMINATOR,
            # 第二维：哪些类别要落点、哪些类别禁止落点，两条都写进门禁产物本身。
            "denominator_response_dimensions": list(FULFILLMENT_REQUIRED_DIMENSIONS),
            "dimensions_requiring_body_binding": list(FULFILLMENT_REQUIRED_DIMENSIONS),
            "dimensions_forbidden_technical_response_cu": list(MAY_NOT_BIND_TECHNICAL_RESPONSE_CU),
            "required_fields": ["planned_response_section", "planned_response_cu", "evidence_contract"],
            "planned_unit_must_exist_in_plan": known_units is not None,
            "planned_section_must_exist_in_plan": known_sections is not None,
            # §5.3：引用完整性是"每一个声明的落点都要对得上"，不是"有一个对得上就行"。
            "every_declared_target_must_exist_not_any_of_them": True,
            "references_verified_against_plan": (known_units is not None
                                                 and known_sections is not None),
            "excluded_classes_may_not_be_planned": True,
            "never_write_commercial_content_for_coverage": True,
            "never_pull_proof_material_into_technical_body_for_coverage": True,
            "identity_material_never_required_in_technical_body": True,
            "whole_document_rubric_not_forced_into_a_content_unit": True,
        },
    }
