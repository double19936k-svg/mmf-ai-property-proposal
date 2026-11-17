"""MMF008A-A / MMF008A2-A：评分项的第二维——「技术正文可响应性」。

第一维（`scoring_taxonomy.SCORING_CLASSES`）回答的是"这一行属于招标文件的哪一块"。
Run F 实测证明它不足以决定"技术标正文要不要回答它、能不能靠正文里的证据回答它"：
`TECHNICAL_INCLUDED -> TECHNICAL_SUBSTANTIVE` 一条边把四种完全不同的东西压进同一个分母——

    项目管理团队构成、组织架构中的角色/阶段/工作内容（**匿名组织设计**）
    省级政府部门/省级银行类似项目的物业服务经验（**证明材料在资格文件里**）
    是否覆盖所有物业需求、结构是否清晰详实（**整本合同的质量**）
    合同期内甲方可终止、卷次标题与计分规则（**条款语与部次标题**）

四者都要求投标人在正文里"举出实体证据"，于是真实写出的岗位表与责任矩阵全部读成
NEARBY_ONLY，而证明材料本来就不该出现在技术标里。第二维与第一维**正交**：
第一维不改、范围判定仍只认 TECHNICAL_ONLY_SCOPE_GATE 一处，本模块只补一问——
这条评分项要怎样才算被技术标回答。

六条类别各自的处置（`RESPONSE_DIMENSION_DISPOSITION`）：
- `TECHNICAL_BODY_DIRECT`            服务保障/退出移交/应急预案/管理模式/操作程序/培训计划
                                     ——正文里必须有实体证据，进覆盖率。
- `TECHNICAL_BODY_ANONYMOUS_STRUCTURE` 匿名岗位、组织、职责、阶段、工作内容
                                     ——进覆盖率，但证据必须是**结构化载体**，
                                     且**永远不要求**个人身份材料。
- `CROSS_FILE_PROTECTED_EVIDENCE`    相关但证据=个人资历/真实项目经验/合同/证书/履约证明
                                     ——缺正文证据**不得**扣技术标完成度，
                                     也**不得**为凑覆盖率把证明材料拉进技术正文。
- `WHOLE_DOCUMENT_RUBRIC`            需求覆盖/完整性/结构清晰/合理可行
                                     ——由整体文档 QA 的既有维度判，不硬造一个 CU
                                     去对应招标原句。
- `CONTRACTUAL_CONTEXT`              合同期内可终止、商务/价格条款、履约承诺
                                     ——评的是承诺与合同约束，不进技术正文分母。
- `HEADER_FORMULA_NON_RESPONSE`      部次标题/计分规则/程序性套语
                                     ——这份表压根没在问一件可响应的事，登记不响应。

`classify_responsiveness` 的入参是**一条主题的可读文本**。上一轮传的是打平后的评分行标签，
所以"服务保障能力"这一行只剩评分标准的残段（"…全面性、可行性、及时性等方面横向比较进行打分"），
事项名与分值在表格的**别的列**里，分类器再准也看不到；本轮改由 `scoring_graph` 交出
`leaf_text`（事项名 + 本行评分标准全文）作为入参。分类判据本身没有加一个项目专有词。

三条不许踩的线（本轮任务书 §A/§B）：不为把覆盖率变绿而硬改分母；不把人员相关项一律
排除；不排匿名组织设计。所以每条记录的分类都必须带**可指认的凭据**（命中标记词 +
事项名词 + 评价口径），并由 `pre_generation_scoring_mapping_gate` 的
`denominator_shrink_watch` 逐条列出来交人工复核——类别是账，不是开关。
"""
from __future__ import annotations

import re
from typing import Any

from .scoring_evidence import split_scoring_label

RESPONSIVENESS_VERSION = "technical-scoring-responsiveness-v0.2"

TECHNICAL_BODY_DIRECT = "TECHNICAL_BODY_DIRECT"
TECHNICAL_BODY_ANONYMOUS_STRUCTURE = "TECHNICAL_BODY_ANONYMOUS_STRUCTURE"
CROSS_FILE_PROTECTED_EVIDENCE = "CROSS_FILE_PROTECTED_EVIDENCE"
WHOLE_DOCUMENT_RUBRIC = "WHOLE_DOCUMENT_RUBRIC"
CONTRACTUAL_CONTEXT = "CONTRACTUAL_CONTEXT"
HEADER_FORMULA_NON_RESPONSE = "HEADER_FORMULA_NON_RESPONSE"

# §A 六类。上一轮的第五类 `NON_RESPONSE_OR_CONTEXT` 把两件相反的事混在了一格里：
# "技术及服务部分 40分"是**卷次标题/计分规则**（这份表压根没有在问一件事），
# "合同期内甲方可无条件终止"是**合同约束语**（它问的是商务承诺，不是技术正文）。
# 压在一格时，两者都不能被派生成内容单元、都不进分母，看不出区别；拆开后每一类
# 各自的处置写得出名目，人工复核才知道哪条是因为什么不算。
RESPONSE_DIMENSIONS: tuple[str, ...] = (
    TECHNICAL_BODY_DIRECT,
    TECHNICAL_BODY_ANONYMOUS_STRUCTURE,
    CROSS_FILE_PROTECTED_EVIDENCE,
    WHOLE_DOCUMENT_RUBRIC,
    CONTRACTUAL_CONTEXT,
    HEADER_FORMULA_NON_RESPONSE,
)

RESPONSE_DIMENSION_DISPOSITION: dict[str, str] = {
    TECHNICAL_BODY_DIRECT: "TECHNICAL_BODY_ENTITY_RESPONSE",
    TECHNICAL_BODY_ANONYMOUS_STRUCTURE: "TECHNICAL_BODY_ANONYMOUS_STRUCTURE",
    CROSS_FILE_PROTECTED_EVIDENCE: "PROOF_LIVES_OUTSIDE_TECHNICAL_BODY",
    WHOLE_DOCUMENT_RUBRIC: "JUDGED_BY_WHOLE_DOCUMENT_QA",
    CONTRACTUAL_CONTEXT: "CONTRACT_or_COMMERCIAL_NOT_TECHNICAL_BODY",
    HEADER_FORMULA_NON_RESPONSE: "NO_ENTITY_RESPONSE_REQUIRED",
}

# 只有这两类进技术标完成度的账（分母的**语义**，不是一个可以调的数）。
FULFILLMENT_REQUIRED_DIMENSIONS: tuple[str, ...] = (
    TECHNICAL_BODY_DIRECT,
    TECHNICAL_BODY_ANONYMOUS_STRUCTURE,
)
TECHNICAL_ACCEPTANCE_DIMENSIONS = (*FULFILLMENT_REQUIRED_DIMENSIONS, WHOLE_DOCUMENT_RUBRIC)
# 这四类不得被派生出一个 Technical Response 内容单元：派生=把证明材料/整本质量/合同语
# 强行摊派成正文里的一篇稿子，Run F 的 CU-S01-01-13 就是这么长出来的。
MAY_NOT_BIND_TECHNICAL_RESPONSE_CU: tuple[str, ...] = (
    CROSS_FILE_PROTECTED_EVIDENCE,
    WHOLE_DOCUMENT_RUBRIC,
    CONTRACTUAL_CONTEXT,
    HEADER_FORMULA_NON_RESPONSE,
)

# ---- 评分血缘关（MMF008A3-A1）-----------------------------------------------
# 只有"要写进技术正文"这两类需要血缘背书：把它们之外的类别往下压没有意义（本来就不进分母），
# 把它们往上抬也不归这一关管。名单直接复用进分母那两条，避免出现第二份口径。
LINEAGE_GATED_DIMENSIONS: tuple[str, ...] = FULFILLMENT_REQUIRED_DIMENSIONS
# 两条降级各自的具名理由：来源不是评分章 / 来源是评分章但不是表格行也不是分值行。
RULE_LINEAGE_CONTRACT_CLAUSE = "NOT_ANCHORED_CONTRACT_CLAUSE_LINEAGE"
RULE_LINEAGE_RUBRIC_CHAPTER_HEADER = "NOT_ANCHORED_RUBRIC_CHAPTER_NON_CELL"

# 整体文档质量类**必须**指向的既有维度名（`app/governance/document_qa.py` 的 dimensions）。
# 这里是引用不是新增：这些维度本来就在算，第二维只是说明"这条评分项归它们管"。
WHOLE_DOCUMENT_QA_DIMENSIONS: dict[str, tuple[str, ...]] = {
    WHOLE_DOCUMENT_RUBRIC: (
        "REQUIREMENT_COVERAGE", "SCORING_COVERAGE", "STRUCTURED_SHARE", "ARTIFACT_INTEGRITY",
    ),
}

# 第一维里"本来就不要求正文响应"的类别（表头行、档次/公式行、程序性套语）。
DIM1_NON_RESPONSE_CLASSES = ("SCORING_HEADER", "SCORING_FORMULA", "PROCEDURAL")
DIM1_CREDENTIAL_CLASSES = ("QUALIFICATION_CREDENTIAL", "BIDDER_CREDENTIAL")
# 商务与价格条款评的是承诺与合同约束，不是技术正文里的一篇稿子。
DIM1_CONTRACT_CLASSES = ("COMMERCIAL_TERMS", "PRICING")

# 部次标题：剥掉分值噪声之后只剩"某某部分"，它不是一件事，是一个卷次的名字。
_VOLUME_HEADING = re.compile(
    r"^(?:第?[一二三四五六七八九十0-9]+(?:部分|卷|册|篇|章|标段)?)?"
    r"(?:技术|商务|资信|资格|综合|价格|服务)(?:及?服务|及?商务|及?技术)?部分$"
)
# 文档结构词当"事项名词"用没有意义：招标书的卷次名不是要在正文里举证实体的事。
_DOCUMENT_STRUCTURE_NOUN = re.compile(
    r"^(?:第?[一二三四五六七八九十0-9]+)?(?:技术|商务|资信|资格|综合|价格|服务)?"
    r"(?:及?服务|及?商务|及?技术)?(?:部分|篇|章|节|卷|册|标题|目录|标书)$"
    r"|^(?:技术|商务|资信|正文|章节|篇目)$"
)
# 整体文档质量语不在这里另立一份：`split_scoring_label` 的 `whole_document_terms`
# 是唯一词表来源，分类器只读它，不再自己写一条正则去"再认一次"。


def response_dimensions() -> tuple[str, ...]:
    return RESPONSE_DIMENSIONS


def response_disposition(responsiveness_class: str) -> str:
    return RESPONSE_DIMENSION_DISPOSITION.get(str(responsiveness_class or ""), "TECHNICAL_BODY_ENTITY_RESPONSE")


def counts_toward_technical_fulfillment(responsiveness_class: str) -> bool:
    """这条主题是否要进"技术标实体覆盖率"的账。

    判定只看类别，不看条数、不看分值：把分值或 `must_respond` 加进来，就等于
    "分高的才算技术实质"，那是另一种刷分母。
    """
    return str(responsiveness_class or "") in FULFILLMENT_REQUIRED_DIMENSIONS


def record_requires_body_response(record: dict[str, Any]) -> bool:
    """读一条行/canonical 记录要不要在正文里被回答；**缺第二维字段时按"要回答"处理**。

    这条默认值是防篡改的：一份没有跑过本模块分类的矩阵（旧产物、手写夹具、别的
    版本落盘的文件）不能因为"没标类别"就整批掉出分母 —— 那等于用漏标来刷覆盖率。
    """
    item = record if isinstance(record, dict) else {}
    if "counts_toward_technical_fulfillment" in item:
        return bool(item.get("counts_toward_technical_fulfillment"))
    return counts_toward_technical_fulfillment(
        str(item.get("responsiveness_class") or TECHNICAL_BODY_DIRECT))


def record_requires_technical_acceptance(record: dict[str, Any]) -> bool:
    # Global technical rubrics remain in coverage but never require a single response CU.
    return record_requires_body_response(record) or record.get('responsiveness_class') == WHOLE_DOCUMENT_RUBRIC


def may_bind_technical_response_cu(responsiveness_class: str) -> bool:
    return str(responsiveness_class or "") not in MAY_NOT_BIND_TECHNICAL_RESPONSE_CU


def classify_responsiveness(
    label: Any,
    *,
    scoring_class: str = "",
    split: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """一条评分行 → (responsiveness_class, rule_id, evidence)。

    次序就是判据的优先级，写在明面上：先认第一维已经定性过的行，再认条款语与部次标题，
    然后才在"有事项可写"的技术行里分匿名组织设计 / 证明材料 / 可生成正文。
    证明材料排在匿名组织设计**之后**：招标在技术部分写的"人员构成、素质及相关经验"
    确实要正文回答（岗位表），不能因为出现"经验"二字就整条踢出分母。
    """
    facts = split or split_scoring_label(label)
    nouns = [str(item) for item in facts.get("matter_nouns") or []
             if not _DOCUMENT_STRUCTURE_NOUN.match(str(item or ""))]
    evidence: dict[str, Any] = {
        "schema_version": RESPONSIVENESS_VERSION,
        "matter_nouns": list(nouns),
        "evaluation_phrases": list(facts.get("evaluation_phrases") or []),
        "matched_markers": {},
        "identity_material_terms": list(facts.get("identity_material_terms") or []),
    }
    name = str(scoring_class or "")
    if name in DIM1_NON_RESPONSE_CLASSES:
        return HEADER_FORMULA_NON_RESPONSE, "DIM1_NON_RESPONSE_ROW", evidence
    if name in DIM1_CREDENTIAL_CLASSES:
        evidence["matched_markers"]["scoring_class"] = name
        # 第一维已经把它定性成"投标人资格/资信材料"，第二维据此不落技术正文；但**保护哪些词**
        # 仍要逐条记下。少了这一行，走 DIM1 这条路进来的行就没有 `protected_evidence_terms`，
        # "不要为了覆盖率把证明材料拽进技术正文"这条约束在最该生效的那一类上反而不成立。
        evidence["protected_evidence_terms"] = sorted({
            *[item for item in (facts.get("proof_material_terms") or []) if item],
            *[item for item in (facts.get("identity_material_terms") or []) if item]})
        return CROSS_FILE_PROTECTED_EVIDENCE, "DIM1_CREDENTIAL_CLASS", evidence
    if name in DIM1_CONTRACT_CLASSES:
        evidence["matched_markers"]["scoring_class"] = name
        return CONTRACTUAL_CONTEXT, "DIM1_CONTRACT_CLASS", evidence

    text = re.sub(r"\s+", "", str(label or ""))
    # 分值先按"数字+分"整段去掉再剥分隔符：把 `分` 当成单字符删掉会把"部分"削成"部"，
    # 部次标题就再也认不出来了（第一版就犯了这个错，SCN-007 漏成技术实质项）。
    stripped = re.sub(r"\d+(?:\.\d+)?\s*分(?:值)?", "", text)
    stripped = re.sub(r"[|｜/、，,。；;：:()（）\[\]【】\s]", "", stripped)
    contract_terms = [item for item in facts.get("contract_procedure_terms") or [] if item]
    if contract_terms:
        evidence["matched_markers"]["contract_procedure"] = contract_terms
        return CONTRACTUAL_CONTEXT, "CONTRACT_OR_PROCEDURE_CLAUSE", evidence
    if _VOLUME_HEADING.match(stripped):
        evidence["matched_markers"]["volume_heading"] = [stripped]
        return HEADER_FORMULA_NON_RESPONSE, "VOLUME_HEADING_WITHOUT_MATTER", evidence

    if (facts.get('whole_document_terms') and re.search('覆盖所有|覆盖全部|所有物业需求|全部需求', text)
            and re.search('内容结构|结构清晰|详实|整体方案|全文', text)):
        evidence['matched_markers']['whole_document'] = list(facts['whole_document_terms'])
        evidence['judged_by'] = list(WHOLE_DOCUMENT_QA_DIMENSIONS[WHOLE_DOCUMENT_RUBRIC])
        return WHOLE_DOCUMENT_RUBRIC, 'WHOLE_DOCUMENT_QUALITY_RUBRIC', evidence

    org_terms = [item for item in facts.get("anonymous_org_terms") or [] if item]
    if org_terms:
        evidence["matched_markers"]["anonymous_org"] = org_terms
        # 同一条评分项里点名的证明材料随记录一起登记：它决定"正文不要写什么"，
        # 不决定"这条不算技术项"。
        evidence["protected_evidence_terms"] = [
            item for item in (facts.get("proof_material_terms") or []) if item]
        return TECHNICAL_BODY_ANONYMOUS_STRUCTURE, "ANONYMOUS_ORG_DESIGN_TOPIC", evidence

    proof_terms = [item for item in facts.get("proof_material_terms") or [] if item]
    identity_terms = [item for item in facts.get("identity_material_terms") or [] if item]
    whole_terms = [item for item in facts.get("whole_document_terms") or [] if item]
    # "正文里写得出来吗"要看**整条标签**，不只看抽出来的名词：名词抽取偶尔会一个不剩
    # （措辞太口语），此时如果标签里明明写着"应急预案/培训计划"，就把这条留在分母里，
    # 而不是顺手判成"整本质量"或"证明材料"。三个非分母类别一律受这根守卫约束 ——
    # 抽取失败不能变成少写正文的理由。
    # 「人员」紧挨着「履历 / 简历」时，点的是要另附的身份材料，不是人员构成或方案。
    # 技术事项词表里有「人员」，若照单全收，这条会被当成正文可写；证据合同又把履历词整段剔除，
    # 要素表变空，生成前门就以缺证据合同把整场写稿挡住。人员构成、配置方案仍留在分母里。
    matter_text = stripped
    body_nouns = list(nouns)
    if identity_terms:
        matter_text = re.sub(r"人员(?=履历|简历)", "", stripped)
        body_nouns = []
        for noun in nouns:
            if not any(term in noun for term in identity_terms):
                body_nouns.append(noun)
                continue
            cleaned = noun
            for term in identity_terms:
                cleaned = cleaned.replace(term, "")
            cleaned = re.sub(r"人员$", "", cleaned)
            if cleaned and _TECHNICAL_MATTER.search(cleaned):
                body_nouns.append(cleaned)
    technical_matter = _has_technical_matter(body_nouns) or bool(_TECHNICAL_MATTER.search(matter_text))
    evidence["technical_matter_present"] = technical_matter
    if (proof_terms or identity_terms) and not technical_matter:
        if proof_terms:
            evidence["matched_markers"]["proof_only"] = proof_terms
        if identity_terms:
            evidence["matched_markers"]["identity_material"] = identity_terms
        evidence["protected_evidence_terms"] = list(dict.fromkeys([*proof_terms, *identity_terms]))
        source = "PROOF_ONLY_TOPIC" if proof_terms else "IDENTITY_MATERIAL_ONLY_TOPIC"
        return CROSS_FILE_PROTECTED_EVIDENCE, source, evidence
    if whole_terms and not technical_matter:
        # 评的是整本合同的质量：交给 `document_qa` 已有的四个维度，不硬造一个 CU
        # 去对应招标原句（"根据是否覆盖所有物业需求"不是一篇文章的标题）。
        evidence["matched_markers"]["whole_document"] = whole_terms
        evidence["judged_by"] = list(WHOLE_DOCUMENT_QA_DIMENSIONS[WHOLE_DOCUMENT_RUBRIC])
        return WHOLE_DOCUMENT_RUBRIC, "WHOLE_DOCUMENT_QUALITY_RUBRIC", evidence

    if not nouns:
        # 抽不出事项名词：这**不是**"这条不用正文回答"的证据，只说明措辞把抽取器绕过去了
        # （"技术部分须提交项目应急预案，评分要求整体方案设计规范合理…"就是一个名词都剥不
        # 出来、但明明在要一篇预案的行）。所以降级到整本质量仍然要有正面凭据：
        # 标签里带技术事项词（预案/方案/培训/巡检…）时一律留在正文可响应类，
        # 并把"没抽到名词"这个失败原因写在 source 上供人复核。
        # 抽取失败绝不能变成少写正文的理由。
        if whole_terms and not technical_matter:
            evidence["matched_markers"]["whole_document"] = whole_terms
            evidence["judged_by"] = list(WHOLE_DOCUMENT_QA_DIMENSIONS[WHOLE_DOCUMENT_RUBRIC])
            return WHOLE_DOCUMENT_RUBRIC, "NO_MATTER_NOUN_WHOLE_DOCUMENT_QUALITY", evidence
        evidence["matched_markers"]["no_matter_noun"] = list(facts.get("evaluation_phrases") or [])[:6]
        evidence["technical_matter_in_label"] = technical_matter
        return TECHNICAL_BODY_DIRECT, "NO_MATTER_NOUN_ASSUMED_GENERATABLE", evidence

    evidence["matched_markers"]["matter_nouns"] = list(nouns)
    if proof_terms:
        evidence["protected_evidence_terms"] = proof_terms
    return TECHNICAL_BODY_DIRECT, "TECHNICAL_MATTER_NOUN", evidence


# 可生成正文的技术事项指认：这些词出现说明"正文里写得出来、也验得出"。
_TECHNICAL_MATTER = re.compile(
    r"方案|预案|措施|制度|流程|规程|程序|标准|规范|体系|机制|岗位|人员|培训|演练|巡检|"
    r"维保|保洁|绿化|秩序|安防|应急|客服|投诉|回访|考核|档案|交接|移交|节能|能耗|设备|"
    r"设施|环境|质量|频次|时限|响应|职责|界面|台账|清单|建议|设想|组织"
)


def _has_technical_matter(nouns: list[str]) -> bool:
    return any(_TECHNICAL_MATTER.search(noun) for noun in nouns)


def _apply_rubric_lineage_gate(responsiveness: str, rule_id: str, evidence: dict[str, Any],
                               lineage: dict[str, Any]) -> tuple[str, str]:
    """文本判成"正文要回答它"之后，还要有**评分表上的落点**才成立为一条独立技术主题。

    只往**不算义务**的方向走一档，且只在三条正面证据全缺时生效（叶子、表格血缘、
    评分章节 + 自带分值）。反过来说：任何一条在评分表上指得出来的行都不会被这一关降级，
    所以它不可能被用来悄悄缩分母——真要缩，得先把表上那一格抹掉。

    降级到哪一类看**它从哪来**：来源章节不是评分表所在章 ⇒ 这句话是标书别处的条款在
    引用已承诺的内容（合同语境）；来源章节就是评分表所在章却既不是表格行也没有分值
    ⇒ 那是卷次标题/计分规则一类的的话，不是一件可响应的事。
    """
    if responsiveness not in LINEAGE_GATED_DIMENSIONS or lineage.get("anchored", True):
        return responsiveness, rule_id
    evidence["matched_markers"]["rubric_lineage"] = {
        "leaf_id": lineage.get("leaf_id"),
        "source_table_id": lineage.get("source_table_id"),
        "source_chapter": lineage.get("source_chapter"),
        "source_locator": lineage.get("source_locator"),
        "own_weight": lineage.get("own_weight"),
        "anchor_basis": lineage.get("anchor_basis"),
    }
    evidence["lineage_gate"] = {
        "text_only_class": responsiveness,
        "text_only_rule": rule_id,
        "reason": "文字像技术要求不足以立项：这一项在评分表上指不出来，也没有自己的分值。",
    }
    if lineage.get("chapter_lineage"):
        return HEADER_FORMULA_NON_RESPONSE, RULE_LINEAGE_RUBRIC_CHAPTER_HEADER
    return CONTRACTUAL_CONTEXT, RULE_LINEAGE_CONTRACT_CLAUSE


def responsiveness_fields(
    label: Any,
    *,
    scoring_class: str = "",
    structural_text: str = "",
    text_basis: str = "FLATTENED_ROW_LABEL",
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """给一行/一条记录打上第二维字段（含分类凭据）。

    唯一写出这些字段的地方就在这里：分类器、矩阵行、canonical 记录、门控与映射读的都是
    这一份输出，不在任何调用方再算一次。

    `label` 是这条主题**展示与承载身份**的名字；`structural_text` 是判"正文怎么回答它"时
    实际要读的那一格（评分表里的 事项名 + 本行评分标准全文）。两者不同时给，分类器就只能
    看见打平后的残段——上一轮 5 分/3 分/2 分的丢失是这么来的。没给 `structural_text`
    就退回读 `label`，并把读的是哪一份写在 `responsiveness_input_basis` 上，不装作看过表。

    `lineage` 是这条评分项的**评分血缘**（`scoring_graph.rubric_lineage`）。文本判成
    "正文要回答它"之后还要过这一关：一条既不在评分表上、也不在评分表所在章节、
    自己也没有分值的句子，就算措辞完全是技术语，也不是一条独立的评分义务——它多半是
    合同条款在引用投标人已经承诺过的内容。这一关只对**给了血缘**的调用生效（正式链），
    没给就不假装判过（`rubric_lineage` 为 None，文本判定原样成立）。
    """
    read_text = str(structural_text or label or "")
    split = split_scoring_label(read_text)
    responsiveness, rule_id, evidence = classify_responsiveness(
        read_text, scoring_class=scoring_class, split=split)
    text_only_class, text_only_rule = responsiveness, rule_id
    if lineage is not None:
        responsiveness, rule_id = _apply_rubric_lineage_gate(
            responsiveness, rule_id, evidence, lineage)
    body_required = counts_toward_technical_fulfillment(responsiveness)
    return {
        "responsiveness_class": responsiveness,
        "responsiveness_class_version": RESPONSIVENESS_VERSION,
        "responsiveness_class_source": rule_id,
        # 文本判定与血缘判定分两格写：看得见"文字像技术要求"这一步得到了什么，
        # 也看得见它为什么最终不算一条独立技术主题——降类不是把前一步抹掉。
        "responsiveness_text_only_class": text_only_class,
        "responsiveness_text_only_rule": text_only_rule,
        "rubric_lineage": dict(lineage) if lineage else None,
        "responsiveness_evidence": evidence,
        "responsiveness_input_text": read_text,
        "responsiveness_input_basis": text_basis if structural_text else "FLATTENED_ROW_LABEL",
        "responsiveness_disposition": response_disposition(responsiveness),
        "counts_toward_technical_fulfillment": body_required,
        "requires_technical_body_response": body_required,
        "may_bind_technical_response_cu": may_bind_technical_response_cu(responsiveness),
        "response_requirement": ("TECHNICAL_BODY_ENTITY_RESPONSE" if body_required
                                 else RESPONSE_DIMENSION_DISPOSITION[responsiveness]),
        "judged_by_whole_document_dimensions": list(
            WHOLE_DOCUMENT_QA_DIMENSIONS.get(responsiveness) or []),
        "protected_evidence_terms": list(evidence.get("protected_evidence_terms") or []),
        "identity_material_not_required": True,
        "matter_nouns": list(split["matter_nouns"]),
        "evaluation_phrases": list(split["evaluation_phrases"]),
    }
