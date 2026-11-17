"""P0-2 技术评分实体证据判定（Round 3 §24/§25）。

"附近有文字就算覆盖"不是覆盖：一个技术评分项要判 FULFILLED，必须能在最终 Word 里
指出响应 Section、响应 Content Unit、逐字证据片段与产物位置，并且评分项自身要求的
成果要素达到阈值。本模块只做判定，不生成内容，也不修改片段。
"""
from __future__ import annotations

import re
from typing import Any
from .scoring_retrieval import visible_values, select_evidence
from .scoring_context import deadline_terms

EVIDENCE_CONTRACT_VERSION = "technical-scoring-entity-evidence-v0.3"

# 单元级实体门槛：低于该长度的片段只算"提及"，不足以承载一条评分响应。
SUBSTANTIVE_UNIT_CHARS = 320
# 一个**必答要素**的最短长度：两字词（"时间""规范""角色"）在几十万字正文里几乎必然出现，
# 用它当证据等于不设证据。分类与路由仍可看两字名词，验收只认 ≥3 字的要素。
MIN_CONCEPT_CHARS = 3
# 证据摘录窗口：取命中词前后各若干字，够人工回查到段落。
EVIDENCE_SNIPPET_CHARS = 90

# §27：技术评分实体覆盖率低于该值不得判 COMPLETED_CLEAN。
TECHNICAL_SCORING_GATE_MIN = 0.80
# §26：本轮点名必须补齐的四项。
NAMED_FOCUS_RUBRICS = (
    "EXIT_HANDOVER",
    "SERVICE_GUARANTEE_SYSTEM",
    "EMERGENCY_DEPLOYMENT_RESPONSE",
    "PROJECT_MANAGEMENT_SUGGESTION",
)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value if value is not None else ""))


# ---------------------------------------------------------------------------
# MMF008A-A：评分标签的两种话——「要写哪件事」与「这件事按什么口径打分」
# ---------------------------------------------------------------------------
# 为什么单独立一节：`label_concepts` 过去把整句评分指令切下来当要素去找，于是
# "组织架构中应明确列出投标人每个管理成员在项目中的角色" 成了 25 个字的"要素"。
# 没有一份技术标会逐字复述招标文件的打分句，所以真实存在的岗位表照样读成
# NEARBY_ONLY —— Run F 的 SCN-003/005/008 三条实测就是这么丢分的。
# 证据合同只能写在**事项名词**上；评价口径留给第二维与整体文档 QA 去判。
#
# 刻意不建同义词表：把"人员结构"近义成"编制/岗位序列"会直接放大覆盖率，
# 那是拿尺子换刻度。宁可少算，多出来的缺口由生成去补，不是由分词去消。

# 修饰/评价用词：出现即说明这个分句讲的是"写成什么样、怎么打分"，不是"写哪件事"。
EVALUATION_MARKERS: tuple[str, ...] = (
    "是否", "应明确", "应列出", "应提供", "应提交", "应说明", "须提交", "需提交", "应当",
    "综合考虑", "根据", "依据", "按照", "结合", "针对本项目", "针对各", "视方案", "视是否",
    "进行评分", "评分", "评审", "打分", "得分", "满分", "分值", "权重", "优得", "一般得",
    "较差得", "横向比较", "纵向比较", "比较", "档次", "优中选优", "符合实际", "符合要求",
    "满足要求", "可操作性", "合理可行", "完备", "科学", "清晰", "详实", "规范合理", "完整",
    "合理", "可行", "迅速", "简单", "容易", "及时", "有力", "有效开展", "实际要求", "落地容易",
)
# 条款/程序语：主语是甲乙双方的权利义务或投标程序，技术正文不"响应"它。
CONTRACT_PROCEDURE_MARKERS: tuple[str, ...] = (
    "有权终止", "单方解除", "解除合同", "终止合同", "不再续签", "不续签", "违约责任",
    "违约金", "赔偿", "扣减服务费", "扣除服务费", "扣分", "罚款", "废标", "投标无效",
    "投标保证金", "履约保证金", "密封", "装订", "正本", "副本", "营业执照", "资质等级",
)
# 证明材料语：这些事实在技术标正文里**写不出来**，只能由资格/商务文件证明。
PROOF_MATERIALED_MARKERS: tuple[str, ...] = (
    "类似项目", "同类项目", "类似业绩", "业绩", "案例", "物业服务经验", "相关经验",
    "管理经验", "从业年限", "中标通知书", "合同扫描件", "履约证明", "验收证明",
    "荣誉证书", "获奖", "表彰", "信用证书", "认证证书", "财务状况", "纳税",
)
# 匿名组织设计语：允许以岗位/角色/阶段/职责的匿名方式在正文里回答，
# 但**不得**要求投标人把个人身份材料写进技术正文。
ANONYMOUS_ORG_MARKERS: tuple[str, ...] = (
    "组织架构", "组织机构", "组织体系", "岗位设置", "岗位配置", "岗位序列", "定岗", "定员",
    "人员构成", "人员结构", "班子", "梯队", "每个管理成员", "管理成员", "参与阶段",
    "参与时间", "工作内容", "角色", "职责分工", "职责边界", "编制",
)
# 整体文档质量语：评的是整本合同，不是某一节里的一个实体。
WHOLE_DOCUMENT_RUBRIC_MARKERS: tuple[str, ...] = (
    "覆盖所有", "覆盖全部", "所有物业需求", "全部需求", "内容结构", "结构清晰",
    "整体方案", "通篇", "全文", "各章节", "完整性", "条理",
)
# 个人身份材料：技术标的证据合同**永远不得**把它们列为必答要素
# （招标把这类材料放在资格/资信文件里，正文复述反而构成偏误）。
IDENTITY_MATERIAL_TERMS: tuple[str, ...] = (
    "身份证", "姓名", "学历", "学位", "职称证书", "资格证书", "荣誉证书", "社保证明",
    "劳动合同", "履历", "简历", "个人业绩", "注册证书", "执业资格",
)
# 一个"事项名词"的上限长度：超过就说明它还是一个从句，没被剥成事项名。
MAX_TOPIC_NOUN_CHARS = 12
_MODIFIER_HEAD = re.compile(r"^(?:应|须|需|应当|不得|禁止|可以|能够)?(?:明确|列出|提供|提交|说明|描述|体现|覆盖|满足|符合|达到|完成)")
_PREPOSITION_HEAD = re.compile(r"^(?:为|对|向|由|在|从|关于|针对|依据|根据|按照|结合|拟投入本?项目的?)")
_ENUM_PREFIX = re.compile(r"^(?:\d+[、.．)]+|[（(]\d+[）)])")
_DE_TAIL_HEAD = re.compile(r"^(?:了|的|地)")


def _evaluation_marked(text: str) -> bool:
    return any(marker in text for marker in EVALUATION_MARKERS)


def _core_noun(segment: str) -> str:
    """定中结构取中心语：「投标人拟投入本项目的管理团队人员构成情况」→「人员构成」。"""
    text = _ENUM_PREFIX.sub("", str(segment or "")).strip()
    text = _PREPOSITION_HEAD.sub("", text)
    text = _MODIFIER_HEAD.sub("", text)
    parts = [part for part in re.split(r"的|地", text) if part]
    if len(parts) > 1:
        text = parts[-1]
    text = re.sub(r"(?:情况|情形|等方面|等相关|相关|方面|等)$", "", text)
    text = _DE_TAIL_HEAD.sub("", text)
    return text.strip(" ，,。；;：:、")


def split_scoring_label(label: Any) -> dict[str, Any]:
    """把一条评分标签切成「事项名词 / 评价口径 / 证明材料 / 身份材料」四堆。

    返回的每个清单都是**分类凭据**，会随记录落盘：读的人要能在原始标签里指着
    某几个字说"就是这几个字让它离开分母的"。分不出名词不等于不用负责 ——
    `matter_nouns` 为空时 `responsiveness` 分类器仍然要给出具名类别。
    """
    text = re.sub(r"[（(][^（）()]*?(?:满分|分值|得分|权重|分)[^（）()]*?[）)]", " ", str(label or ""))
    text = re.sub(r"\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?\s*分(?:值)?", " ", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*分(?!钟)", " ", text)
    nouns: list[str] = []
    evaluation: list[str] = []
    for raw in re.split(r"[、，,。；;：:|/\s]+", text):
        for segment in [raw, *[piece for piece in re.split(r"及与|以及|并且|及|与|和", raw)]]:
            segment = segment.strip(" 。；:：、")
            if len(segment) < 2 or segment in evaluation:
                continue
            noun = _core_noun(segment)
            if (not noun or len(noun) > MAX_TOPIC_NOUN_CHARS or _evaluation_marked(noun)
                    or _evaluation_marked(segment) and noun == segment):
                if segment not in evaluation:
                    evaluation.append(segment)
                continue
            if noun not in nouns:
                nouns.append(noun)
    joined = " ".join(nouns) + " " + " ".join(evaluation)
    return {
        "matter_nouns": nouns,
        "evaluation_phrases": evaluation,
        "proof_material_terms": [term for term in PROOF_MATERIALED_MARKERS if term in joined],
        "anonymous_org_terms": [term for term in ANONYMOUS_ORG_MARKERS if term in joined],
        "whole_document_terms": [term for term in WHOLE_DOCUMENT_RUBRIC_MARKERS if term in joined],
        "contract_procedure_terms": [term for term in CONTRACT_PROCEDURE_MARKERS if term in joined],
        "identity_material_terms": [term for term in IDENTITY_MATERIAL_TERMS if term in joined],
    }


# §26 点名的四项 + 常见同类项。concepts 为"要素 → 可接受同义词"，
# min_concepts 是判实体齐备所需的要素个数（不是关键词个数）。
SCORING_ENTITY_RUBRICS: tuple[dict[str, Any], ...] = (
    {
        'rubric_id': 'STAFF_GUARANTEE', 'match': r'人员保障|岗位人员.*保障|各岗位人员.*保障',
        'concepts': {
            '岗位覆盖': ('人员配置', '岗位', '编制', '排班'),
            '储备补位': ('备用', '替岗', '顶岗', '补位', '替班'),
            '培训计划': ('培训', '带岗', '岗前', '补训'),
            '考勤履约': ('考勤', '在岗', '考核', '履约'),
            '技能核验': ('技能', '核验', '独立作业', '交底'),
        }, 'min_concepts': 5, 'require_structured': True,
    },
    {
        'rubric_id': 'MANAGEMENT_ADVANTAGE', 'match': r'管理.*优势|管理优势',
        'concepts': {
            '项目适配': ('本项目', '办公楼', '职工食堂', '金融办公'),
            '具体机制': ('时间窗口', '书面接口', '独立复核', '交叉复核', '分级', '唯一牵头', '一事一档'),
            '实施方法': ('实施', '执行', '交底', '安排', '核查', '复核'),
            '可追溯成果': ('台账', '记录', '清单', '签认', '核对单'),
            '优势作用': ('避免', '减少', '降低', '防止', '保障', '连续性'),
        }, 'min_concepts': 5, 'require_structured': False,
    },
    {
        'rubric_id': 'MANAGEMENT_TEAM', 'match': r'管理\s*团队|人员构成|人员结构',
        'concepts': {
            '岗位组织': ('项目主管', '项目负责人', '组织架构', '岗位构成', '专业负责人'),
            '职责汇报': ('职责', '汇报', '牵头', '指挥', '责任边界'),
            '配置到岗': ('人员配置', '编制', '排班', '在岗', '值守', '常驻现场'),
            '能力管理': ('技能', '培训', '核验', '考核', '交底', '任职要求'),
        }, 'min_concepts': 4, 'require_structured': True,
    },
    {
        # R41 §19：退出移交必须要求"退出流程 / 资料 / 资产 / 权限 / 问题清单 / 人员撤场 /
        # 过渡 / 确认"八项。八项全部列进合同，且 `min_concepts` 就等于项数 —— 合同上写着
        # 八项、验收只找四项，那这张合同就是装饰。
        "rubric_id": "EXIT_HANDOVER",
        "match": r"退出|移交|退场",
        "concepts": {
            "退出流程": ("退出流程", "退出安排", "退出计划", "退场流程", "退出机制", "交接步骤", "移交流程"),
            "资料移交": ("资料移交", "档案移交", "文件移交", "文档移交", "资料归档", "档案归档", "移交清单"),
            "资产设备": ("设备移交", "设施移交", "固定资产", "资产移交", "设备清单", "器具移交", "备件移交"),
            "权限交回": ("权限交回", "权限收回", "账号移交", "门禁授权", "系统权限", "钥匙移交"),
            "问题清单": ("问题清单", "遗留问题", "尾项清单", "整改清单", "未完事项", "责任岗位"),
            "人员撤场": ("人员撤场", "撤场", "退场安排", "人员离场", "分批撤离"),
            "过渡安排": ("过渡安排", "过渡期", "平稳过渡", "衔接方案", "移交双方"),
            "双方确认": ("双方确认", "移交确认", "验收签字", "签收", "书面确认"),
        },
        "min_concepts": 8,
        "require_structured": True,
    },
    {
        "rubric_id": "SERVICE_GUARANTEE_SYSTEM",
        "match": r"服务保障体系|保障体系|服务保障",
        "concepts": {
            "体系构成": ("保障体系", "体系构成", "组织架构", "分层", "三级"),
            "人员保障": ("人员保障", "备用人员", "应急补位", "顶岗", "人力储备"),
            "物资装备保障": ("物资保障", "装备", "物料", "库", "工装"),
            "制度与流程保障": ("制度", "流程", "规程", "标准"),
            "监督与考核": ("考核", "检查", "督导", "抽查", "评审"),
            "投诉与改进": ("投诉", "整改", "持续改进", "回访"),
        },
        "min_concepts": 4,
        "require_structured": True,
    },
    {
        "rubric_id": "EMERGENCY_DEPLOYMENT_RESPONSE",
        "match": r"应急|突发|响应速度|抢险|救援",
        "concepts": {
            "响应时限": ("响应时限", "响应时间", "分钟内", "小时内", "到场时限", "限时"),
            "到场与部署": ("到场", "部署", "值守", "岗位布置", "布点", "力量配置"),
            "处置流程": ("处置流程", "处置步骤", "响应流程", "处置程序", "先期处置"),
            "应急物资": ("应急物资", "物资清单", "器材", "储备"),
            "救援队伍": ("救援队伍", "应急队伍", "抢险队", "小组", "班组"),
            "演练与复盘": ("演练", "复盘", "培训", "评估"),
        },
        "min_concepts": 4,
        "require_structured": True,
    },
    {
        "rubric_id": "PROJECT_MANAGEMENT_SUGGESTION",
        "match": r"管理建议|建议",
        "concepts": {
            "针对本项目": ("本项目", "本标段", "结合项目"),
            "问题或风险识别": ("风险", "问题", "难点", "隐患", "短板"),
            "改进建议": ("建议", "优化", "改进措施", "提升"),
            "实施路径": ("实施", "落地", "步骤", "阶段", "计划"),
            "责任与节点": ("责任", "岗位", "节点", "时限", "频次"),
            "效果衡量": ("指标", "考核", "衡量", "验证", "成效"),
        },
        "min_concepts": 4,
        "require_structured": True,
    },
)


def artifact_evidence_index(
    fragments: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], dict[str, int]]:
    """一次遍历建立三份索引：CU 证据、结构化文字面、表格数量。"""
    unit_index = _unit_records(fragments)
    structured_by_section: dict[str, str] = {}
    table_count_by_section: dict[str, int] = {}
    for section_id, fragment in (fragments or {}).items():
        if not isinstance(fragment, dict):
            continue
        structured_by_section[str(section_id)] = _structured_text(fragment)
        table_count_by_section[str(section_id)] = len(fragment.get("tables") or [])
    return unit_index, structured_by_section, table_count_by_section


def rubric_for(label: str) -> dict[str, Any] | None:
    text = normalize_text(label)
    for rubric in SCORING_ENTITY_RUBRICS:
        if re.search(str(rubric["match"]), text):
            return rubric
    return None


def _coarse_label_tokens(label: Any) -> list[str]:
    """抽不出事项名词时的兜底要素：退回旧的粗粒度分词，宁可找得严，也不能没有合同。

    要素表为空会让 §34 的生成前门直接判 `EVIDENCE_CONTRACT` 缺失，把一整场生成挡死 ——
    "这一条我们没能拆出事项名词"是**可见性**问题，不该变成**可生成性**问题。
    """
    cleaned = re.sub(r"[（(][^）)]*[分值分][）)]", " ", str(label or ""))
    cleaned = re.sub(r"^(技术评分要求|评分要求|评审因素|评分因素)[:：]?", " ", cleaned)
    coarse = [tok for tok in re.split(r"[、，,。；;：:|/]|\s+", cleaned) if len(normalize_text(tok)) >= 3]
    tokens: list[str] = []
    for token in coarse:
        normalized = normalize_text(token)
        tokens.append(normalized)
        for piece in re.split(r"及与|以及|并且|及|与|和", normalized):
            piece = normalize_text(piece)
            if len(piece) >= 3 and piece not in tokens:
                tokens.append(piece)
    return tokens[:6]


def label_concepts_with_segmentation(label: Any) -> tuple[dict[str, tuple[str, ...]], str]:
    """(要素表, 分词口径)。口径随合同一起落盘，读数的人能看见用的是哪一把尺。

    - `TOPIC_NOUNS`：MMF008A-A 的主口径，要素 = 从评分标签里剥出来的**事项名词**，
      评价口径（"是否清晰详实合理""优得6-8分""进行评分"）不当要素——正文里不会有这句话。
    - `COARSE_LABEL_TOKENS_NO_NOUN`：一个名词都没剥出来时的兜底，退回旧粗粒度分词。
      这条路径上的主题仍然留在分母里（见 `scoring_responsiveness` 的
      `NO_MATTER_NOUN_ASSUMED_GENERATABLE`），只是验收时找的词更宽。

    刻意**不建同义词表**：把"人员结构"近义成"编制/岗位序列"会直接放大覆盖率，那是换尺子
    不是修尺子。宁可少算，缺口由生成去补。个人身份材料（学历/职称/证书/姓名…）一律
    不进必答要素：技术标按匿名岗位口径回答即可。

    要素最短 3 字（分类用的名词仍是 2 字）：像"时间""规范"这种两字词在一份 30 万字的
    正文里几乎必然命中，拿它当"这条评分项被回答了"的证据等于没有证据——
    Run F 回放里 SCN-003 就是靠 `阶段`+`时间` 两个泛词被判成 FULFILLED 的。
    """
    nouns = [normalize_text(noun) for noun in split_scoring_label(label)["matter_nouns"]]
    kept = [noun for noun in nouns if len(noun) >= MIN_CONCEPT_CHARS
            and not any(term in noun for term in IDENTITY_MATERIAL_TERMS)]
    if kept:
        return {noun: (noun,) for noun in dict.fromkeys(kept[:6])}, "TOPIC_NOUNS"
    fallback = [tok for tok in _coarse_label_tokens(label)
                if len(tok) >= MIN_CONCEPT_CHARS and not _evaluation_marked(tok)
                and not any(term in tok for term in IDENTITY_MATERIAL_TERMS)]
    return {tok: (tok,) for tok in dict.fromkeys(fallback)}, \
        "COARSE_LABEL_TOKENS_NO_NOUN"


def label_concepts(label: Any) -> dict[str, tuple[str, ...]]:
    """没有命名 rubric 时的要素表：见 `label_concepts_with_segmentation`。"""
    return label_concepts_with_segmentation(label)[0]


def _structured_text(fragment: dict[str, Any]) -> str:
    """表格与流程的文字面：实体响应必须落到结构化载体上，纯叙述不算。"""
    parts: list[str] = []
    for table in fragment.get("tables") or []:
        if not isinstance(table, dict):
            continue
        parts.extend([str(table.get("title") or ""), str(table.get("notes") or "")])
        parts.extend(str(col) for col in table.get("columns") or [])
        for row in table.get("rows") or []:
            parts.extend(str(cell) for cell in row if str(cell or "").strip())
    for process in fragment.get("processes") or []:
        if not isinstance(process, dict):
            continue
        parts.append(str(process.get("title") or ""))
        parts.extend(str(step) for step in process.get("steps") or [])
        parts.append(str(process.get("roles") or ""))
        parts.append(str(process.get("trigger") or ""))
        parts.append(str(process.get("records") or ""))
    return normalize_text("".join(parts))


def _unit_records(fragments: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把每个 Section 的到达片段摊成 CU 级证据记录（含 planned id 与产物位置）。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for section_id, fragment in (fragments or {}).items():
        if not isinstance(fragment, dict):
            continue
        units: list[dict[str, Any]] = []
        rows = fragment.get("content_unit_fragments") or []
        for position, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            text = normalize_text(row.get("content"))
            payload = row.get('payload') or {}
            payload_text = normalize_text(visible_values(payload))
            units.append({
                "position": position,
                "section_id": str(section_id),
                "assembled_content_unit_id": str(row.get("content_unit_id") or f"CU-{section_id}-{position:02d}"),
                "archetype": str(row.get("archetype") or "NARRATIVE"),
                "chars": len(text),
                "text": text + payload_text,
                "structured_text": normalize_text(row.get('final_structured_text')) if row.get('final_artifact_locations') else payload_text if (payload.get('rows') or payload.get('steps') or payload.get('items') or payload.get('fields')) else '',
                "substantive": len(text) >= SUBSTANTIVE_UNIT_CHARS,
                "artifact_location": (
                    ';'.join(row['final_artifact_locations']) if row.get('final_artifact_locations') else
                    f"word/sections/{section_id}/fragment.json#content_unit_fragments[{position - 1}]"
                ),
            })
        if not units:
            blocks = fragment.get("body_blocks") or []
            text = normalize_text("".join(str(block.get("content") if isinstance(block, dict) else block) for block in blocks))
            units.append({
                "position": 1,
                "section_id": str(section_id),
                "assembled_content_unit_id": f"CU-{section_id}-01",
                "archetype": "ASSEMBLED_SECTION",
                "chars": len(text),
                "text": text,
                "substantive": len(text) >= SUBSTANTIVE_UNIT_CHARS,
                "artifact_location": f"word/sections/{section_id}/fragment.json#body_blocks",
            })
        structured = _structured_text(fragment)
        if structured:
            # 表格与流程本身就是产物里的实体证据，给它一条可定位的记录而不是另开一套判据。
            table_cells = sum(
                len(table.get("rows") or []) for table in fragment.get("tables") or [] if isinstance(table, dict))
            process_steps = sum(
                len(process.get("steps") or []) for process in fragment.get("processes") or [] if isinstance(process, dict))
            units.append({
                "position": len(units) + 1,
                "section_id": str(section_id),
                "assembled_content_unit_id": f"STRUCTURED-{section_id}",
                "archetype": "STRUCTURED",
                "chars": len(structured),
                "text": structured,
                "structured_text": structured,
                # 有实际行/步的结构化载体本身就算实体，不看字数；空壳表由 P0-4 治理。
                "substantive": (len(structured) >= SUBSTANTIVE_UNIT_CHARS
                                or table_cells >= 2 or process_steps >= 2),
                "artifact_location": f"word/sections/{section_id}/fragment.json#tables+processes",
            })
        index[str(section_id)] = units
    return index


def _locate(concepts, units, structured, rubric_id=''):
    """Compatibility entry; candidate selection has one implementation."""
    hits, missed, _ = select_evidence(concepts, units, rubric_id)
    return hits, missed


def judge_scoring_row(
    row: dict[str, Any],
    unit_index: dict[str, list[dict[str, Any]]],
    structured_by_section: dict[str, str],
    table_count_by_section: dict[str, int],
) -> dict[str, Any]:
    """返回一条评分项的 fulfillment 块（含 §24 五要素）。"""
    section_ids = [str(sid) for sid in (row.get("section_ids") or []) if sid]
    planned = row.get("planned_evidence") or {}
    if row.get("scope_disposition") == "EXCLUDED":
        return {
            "status": "EXCLUDED_FROM_TECHNICAL_BID",
            "scope_class": row.get("scope_class"),
            "response_section": None,
            "response_cu": [],
            "evidence_text": [],
            "artifact_location": [],
            "effective_chars": 0,
            "evidence_found": False,
            "entity_backed": False,
            "last_validated_at": None,
        }
    if not section_ids:
        return {
            "status": "MISSING",
            # 未规划与"规划了但产物查无文字"必须可区分：前者是预算/路由问题，后者是写作问题。
            "missing_kind": "NOT_PLANNED",
            "reason": row.get("missing_reason") or "无响应位置",
            "response_section": None,
            "response_cu": [],
            "evidence_text": [],
            "artifact_location": [],
            "effective_chars": 0,
            "evidence_found": False,
            "entity_backed": False,
            "last_validated_at": None,
        }

    units = [unit for sid in section_ids for unit in unit_index.get(sid) or []]
    structured = "".join(structured_by_section.get(sid) or "" for sid in section_ids)
    chars = sum(unit["chars"] for unit in units)

    rubric = rubric_for(str(row.get("label") or ""))
    contract = row.get("entity_contract") or row.get("evidence_contract") or {}
    if rubric:
        # Search all actual carriers. The plan is a preference and an audit dimension,
        # not permission to miss valid evidence elsewhere in the document.
        units = [u for section_units in unit_index.values() for u in section_units]
        chars = sum(unit['chars'] for unit in units)
    if rubric:
        concepts = dict(rubric["concepts"])
        min_concepts = int(rubric["min_concepts"])
        require_structured = bool(rubric["require_structured"])
        rubric_id = str(rubric["rubric_id"])
        concept_source = "NAMED_RUBRIC"
    elif contract.get("concepts"):
        # §32 的本意：规划期写下的那份合同就是生成后验收用的那份合同。过去这里重新
        # 分词一次，等于同一件事两把尺子——分类器改了口径，验收还在按旧口径找词。
        synonyms = contract.get("concept_synonyms") or {}
        concepts = {str(name): tuple([str(name)] + [str(item) for item in (synonyms.get(name) or [])])
                    for name in contract.get("concepts") or []}
        min_concepts = int(contract.get("min_concepts") or (2 if len(concepts) >= 2 else 1))
        require_structured = bool(contract.get("require_structured"))
        rubric_id = str(contract.get("rubric_id") or "PLAN_OF_RECORD_CONTRACT")
        concept_source = str(contract.get("concept_source") or "PLAN_OF_RECORD_CONTRACT")
    else:
        concepts, segmentation = label_concepts_with_segmentation(str(row.get("label") or ""))
        min_concepts = 2 if len(concepts) >= 2 else 1
        require_structured = bool(planned.get("tables") or planned.get("processes"))
        rubric_id = "LABEL_DERIVED"
        concept_source = "LABEL_REDERIVED_AT_JUDGEMENT"
    if rubric is None and not contract.get("concepts"):
        # 只在"合同缺席"时才现分一次词，并把口径写进读数：有合同就一定按合同验。
        concept_segmentation = segmentation
    else:
        concept_segmentation = str(contract.get("concept_segmentation") or "")

    if rubric and rubric_id == 'EXIT_HANDOVER':
        bindings = [b for b in contract.get('source_obligations') or []
                    if (b.get('source') or {}).get('para_id') and b.get('text')]
        if bindings:
            concepts['合同退出期限'] = deadline_terms(bindings)
            min_concepts = int(rubric['min_concepts']) + 1

    hits, missed, candidates = select_evidence(concepts, units, rubric_id,
                                             row.get('planned_response_cu_bound') or row.get('planned_response_cu') or [],
                                             require_structured=require_structured,
                                             source_obligations=contract.get('source_obligations') or [])
    entity_hits = [hit for hit in hits if hit["substantive"]]
    structured_hits = [hit for hit in entity_hits if hit["structured_backed"]]
    entity_backed = (
        len(entity_hits) >= min_concepts
        and (not require_structured or len(structured_hits) >= 1)
        and chars > 0
        and not contract.get('source_obligation_conflicts')
    )

    if chars <= 0:
        status = "MISSING"
    elif entity_backed:
        status = "FULFILLED"
    elif len(hits) >= 1:
        status = "PARTIAL"
    else:
        # §25 的判据：节里有字数、但一条评分要素都定位不到 —— 这就是"附近有文字"。
        # §26 统一后的五个状态之一，字面量不得再改成 NEARBY_TEXT_ONLY。
        status = "NEARBY_ONLY"

    return {
        "status": status,
        "missing_kind": "EMPTY_ARTIFACT" if status == "MISSING" else None,
        "rubric_id": rubric_id,
        "concept_source": concept_source,
        "concept_segmentation": concept_segmentation,
        # 只回显，不参与判定：判定用哪份合同由 `entity_contract` 决定，这一维决定的是
        # "这条主题算不算技术正文的账"（见 `hierarchy.update_scoring_fulfillment`）。
        "responsiveness_class": str(row.get("responsiveness_class") or ""),
        "concepts_required": min_concepts,
        "concepts_total": len(concepts),
        "concepts_found": [hit["concept"] for hit in hits],
        "concepts_missing": missed,
        "contract_threshold_met": entity_backed,
        "all_concepts_satisfied": len(entity_hits) == len(concepts),
        "substantive_concepts_found": len(entity_hits),
        "acceptance_scope": "CURRENT_ENTITY_CONTRACT_THRESHOLD_ONLY",
        "concept_gap_note": "达到当前契约门限不代表全部概念满足" if entity_backed and missed else "",
        'evidence_candidates': candidates,
        'selected_evidence': hits,
        'rejected_evidence': [c for c in candidates if not c['selected']],
        'missing_dimensions': [] if entity_backed else ([c for c in concepts if c not in {h['concept'] for h in entity_hits}]
                               + (['STRUCTURED_CARRIER'] if require_structured and not structured_hits else [])
                               + (['SOURCE_OBLIGATION_CONFLICT'] if contract.get('source_obligation_conflicts') else [])),
        "response_section": section_ids[0],
        "response_sections": section_ids,
        "response_cu": sorted({hit["content_unit_id"] for hit in hits if hit["content_unit_id"]}),
        "evidence_text": [
            {"concept": hit["concept"], "content_unit_id": hit["content_unit_id"],
             "text": hit["evidence_text"]} for hit in hits[:min_concepts + 2]
        ],
        "artifact_location": [
            {"section_id": hit["section_id"], "content_unit_id": hit["content_unit_id"],
             "path": hit["artifact_location"]} for hit in hits[:min_concepts + 2]
        ],
        "effective_chars": chars,
        "evidence_found": bool(hits),
        "entity_backed": entity_backed,
        "structured_hits": len(structured_hits),
        "tables": sum(int(table_count_by_section.get(sid) or 0) for sid in section_ids),
        "last_validated_at": None,
    }
