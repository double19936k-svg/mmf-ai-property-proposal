"""MMF-007C Round 3 §45/§46：低信息增量（Generic / Padding）度量。

§47 的反注水门（`content_engineering.content_quality_report`）看的是**重复**：完全重复、
近重复、结构化低增量复述、Canonical Fact 复述。Run C 404 页真实交付件实测这四项全为 0
（`r3_p1_generic_padding_runC.json`），但抽样里仍有相当一部分句子"任何物业项目都能照抄"
—— 那类句子一个字都不重复，重复检测天然看不见。本模块补的就是这一维。

判据是两个正交轴，任一单轴都不足以判注水：

  A 可核查载体：这句话能不能落到一个可查的数字 / 岗位 / 设备部位 / 记录载体 / 时点上；
  B 技术实质：这句话是不是在说本项目的物业作业 —— 词表直接复用 `page_calibration` 的
    `TECHNICAL_PAGE_TERMS` 与 `EXCLUDED_SCOPE_PATTERNS`，全产品一把尺子。

只有 **A=0 且 B=0**（或整句一半以上在复述同一批 Canonical Fact）的句子才计为低增量；
套话密度只用于把 HIGH_VALUE 降为 USABLE，不参与判注水（"确保""严格"出现在"每2小时巡检一次并签字"这种硬句子里很正常）。

§46 的另一半：削减候选按"信息增量最低"排序（`targeted_reduction_plan`），页数不足时
补的是结构化载体（`longform/capability_gaps.py`），不是把叙述重复一遍。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from planning.content_engineering import _walk_strings
from planning.page_calibration import EXCLUDED_SCOPE_PATTERNS, TECHNICAL_PAGE_TERMS

PADDING_SCHEMA_VERSION = "content-padding-quality-gate-v0.1"
# 词表迭代号与报告结构分开：结构没变、判据词变了，报告里的占比就不能跨版本对照。
PADDING_LEXICON_VERSION = "padding-lexicon-v0.9"

HIGH_VALUE = "HIGH_VALUE"
USABLE = "USABLE"
GENERIC_PADDING = "GENERIC_PADDING"
BUCKETS = (HIGH_VALUE, USABLE, GENERIC_PADDING)

# A 轴：可核查载体。数**种类**不数次 —— 一段话里写三个"电梯"只算一类，
# 否则长段落能靠同族词把密度刷上去。
TECHNICAL_ANCHOR_PATTERNS: dict[str, re.Pattern[str]] = {
    "quant": re.compile(
        r"\d+(?:[.,]\d+)?\s*(?:%|％|小时|分钟|个月|天|日|周|月|年|次|遍|轮|名|人|人次|层|米|平方米|㎡"
        r"|台|部|套|处|项|个|张|条|组|批|窗口|通道|℃|度|倍|字|号|号楼|栋|幢|单元|室|户|岗|班)"
        r"|每\s*(?:班|日|天|周|月|季|年|两|三|数|次)|按\s*(?:日|周|月|季|年|班|次)"
        r"|逐\s*(?:日|条|台|件|项|张|处)|24\s*小时|7\s*[×x]\s*24"
        # 中文数词同样给出可核查量："指派一人专职疏导""每类抽足三件""三轮签注"。
        r"|[一二三四五六七八九十百两]+\s*(?:个|次|人|件|轮|级|类|处|项|班|道|遍|层|台|部|张|条|组|名)"),
    "role": re.compile(
        r"项目经理|主管|领班|班长|组长|秩序维护|安保|保安|客服|保洁|绿化|工程维修|维修工|弱电"
        r"|电梯司机|驾驶员|管理员|作业员|技术员|网格员|片区负责人|文员|工程师|负责人|接口人"
        r"|值班员|前台|工程岗|作业岗|专员|调度|岗位人员|岗位"),
    "asset": re.compile(
        r"电梯|扶梯|配电|水泵|消防|监控|门禁|道闸|摄像|屋面|屋顶|外墙|幕墙|地下车库|车库|大堂"
        r"|门厅|卫生间|楼道|走廊|通道|大厅|会议室|机房|发电机|空调|新风|照明|标识|景观|垃圾"
        r"|污水|管网|管井|弱电井|点位|设备房|泳池|露台|门窗|玻璃|地毯|墙面|地面|桌面|办公设备"
        r"|设备|设施|器材|工具|物料|药剂"
        # 环境品质属性是可测的东西，不是形容词。
        r"|气味|异味|湿滑|光洁|积尘|积水|污渍|温湿度|温度|湿度|亮度|噪声|噪音|清洁度|完好率"),
    "record": re.compile(
        r"记录表|台账|签到|巡检单|工单|派单|日志|登记表|核查表|检查表|验收单|交接记录|培训记录"
        r"|演练记录|留痕|归档|签字|确认单|照片|影像|清单|明细表|统计表|考核表|报告|记录|卡片|检查卡"),
    "standard": re.compile(
        r"不低于|不少于|不超过|以内|控制在|达到|合格率|及时率|闭环率|响应时间|响应时限|时限"
        r"|标准是|要求为|≥|≤|分值|权重|判定标准|验收标准|质量标准|允许偏差|达标|合格"),
    "place": re.compile(r"[\u4e00-\u9fa5]{2,6}(?:市|区|县|路|街|大道|广场)\d*\D|^[\u4e00-\u9fa5]{2,6}号"),
    # v0.6 两族来自 Run C 30 条等距人工定标（`r3_p1_padding_sample_audit_runC.json`）：
    # 被误判成注水的 20 条全是"餐具无油迹""填表时点与作业时点当日或次日"这类现场条目，
    # 缺的就是"能不能指着它去现场看"的名词与时点。
    # "品类/品牌/物资/工装"曾经进来过，人工定标里那句"只登记品类与管理要求，
    # 不列明未确认的品牌、数量与价格信息"因此被判成有效内容 —— 它谈的是文件里写什么，
    # 不是现场有什么，已收回。
    "object": re.compile(
        r"餐具|油污|油迹|水痕|破损|缺口|清洁剂|除味剂|稀释|配比|容器|标签|材质|出入口|封箱|物品|件数"
        r"|外观|申报|开箱|学历|毕业证书|专业栏|证照|原件|复印件|保险|扣分|整改单|返工|重做|剔除"
        r"|预警|状态代码|工况|备用|停机|检修|故障|抢修|到场|维保|供应单位|紧急号码|演练|预案|判据"
        r"|现象|接应|张贴|发放|告知|载体|基准日|封条|警戒|隔离带|编号栏|读数|倍率|折算|用量|车牌"
        r"|图纸|积压量|持证|外委"
        # v0.8：第三轮定标（`r3_p1_padding_sample_audit3_runC.json`，从 v0.7 命中的 59 条里等距抽 30）
        # 剩下的 6 条假阳性分两类现场名词：处置动作（不移物/暂停放行/报带班人）与记录完整性字段
        # （补录标记、事后涂改、口径卡随身、标准生效日期、挂账天数）。
        # 『跟踪表』在这一版收回：它落在 N30"招标文本未量化条目已登记跟踪表"上，
        # 说的是文件里的台账而不是现场，把一条定标为注水的条目放成了有效内容。
        r"|放行|拦停|争执|带班|开验|法定检验|补录|涂改|随身|生效日期|天数"
        # v0.9：第四轮不再抽样，把 v0.8 命中集合剩下的 22 条逐条定标（全量金标）。
        # 抽样在按章节排序的列表上偏了：第三轮 30 条里 18 条落在第 1/2/4 章的文件自查密集区，
        # 漏在样本外的那半边全是现场条目，全量定标把 v0.8 的注水命中率从 96.4 % 打回 75.5 %。
        # 两类缺失名词：设备运行观察现象与复测字段（温升、异响、同一测点复测、观察期），
        # 服务与交付记录字段和现场巡检做法（回访人、核验意见、恢复状态、动线与观感面、成果目录与报送件）。
        # 全量金标复量：FN 仍为 0，命中集合 55 → 45 条，精确率 93.0 %
        # （`r3_p1_padding_lexicon_v09_census_check.py`）。
        r"|温升|异响|振动|渗漏|泄漏|参数漂移|误动|拒动|测点|复测|观察期"
        r"|回访人|反馈方式|核验意见|恢复状态|报送件|成果目录|密级|签发人|菜谱|食堂|三餐"
        r"|动线|观感|背向面|班前|施工车辆|偏差条目|影响面|回填"
        r"|发生时间|持续时长|影响范围|遗留问题|隐患|点位|面积|频次表|交接班记录"),
    "when": re.compile(
        r"当日|次日|当班|在岗|离岗|交接班|时段|时刻|时点|当期|当场|即时|按期|超时|提前|滞后|触发时"),
    # v0.7：取证与记录动作。第二轮定标（`r3_p1_padding_sample_audit2_runC.json`）里
    # 剩下的假阳性几乎都栽在"登记/署名/复核/取证/挂账/汇总"上 —— 这些是能去现场查的事。
    "act": re.compile(
        r"核对|校核|查验|抽查|抽检|盘查|清点|签认|签收|签署|签注|署名|划改|登记|记入|计入|挂账|销项"
        r"|报审|受理|转办|跟踪|复核|复查|复验|回访|取证|汇总|统计|旁站|认定|上报|交底|交接|闭环"),
}
# 动作的对象是**文件本身**时，取证动词不算现场实质：N03/N08/N10/N30 那类"表述是否统一、
# 本卷回引是否一致、招标文本未量化条目是否登记"用的正是同一批动词，定标结论都是注水。
# 因此这一族命中时只作废 `act`，其余载体（数量、时点、现场对象）照计。
# 『标注』『口径』看着太贪心，但把它们摘掉实测过（`r3_p1_padding_lexicon_v08_check_runC.json` C1）：
# 一起放掉的"证据标注核对…是否加待确认标注""人员更换响应核对…保留原文口径"都是文件自查，
# 换 1 条真内容要付 2 条注水，不摘。M19"标注天数"、M24"判定口径卡随身"改由现场名词救回。
DOCUMENT_SELF_CHECK = re.compile(
    r"正文|本卷|本表|本节|全册|章节|小标题|专章|回引|句式|表述|写法|口径|标注|备选|落点"
    r"|招标文件|招标文本|澄清|递交|装订|份数|售价|编号栏|技术标|商务响应|商务渠道|商务事项|待投标人确认"
    r"|自查|一致性|统一为|不重复列举|不作展开|不进入本章")
HEDGE = re.compile(
    r"视情况|按需|酌情|适当|原则上|必要时|相关|有关|相应|其他|其它|各类|一定|较为|相对"
    r"|尽量|尽可能|切实|不断|持续|进一步|大力|积极|认真|严格|确保|保证|保障|加强|提高|提升"
    r"|完善|规范|高质量|高水平|高标准")
CANONICAL_FACT = re.compile(
    r"(?:项目名称|项目地点|建筑面积|总建筑面积|合同期限|预算|最高限价)[^，。；\n]{0,30}")

FACT_SHARE_MAX = 0.50           # 一半以上在复述同一批事实 → 无信息增量
HEDGE_PER_1000_MAX = 3.0        # 套话密度只降级，不判注水
MIN_NARRATIVE_CHARS = 24        # 与 §47 那把尺子同一条线
SHORT_ITEM_CHARS = 40
# 复核线上限：0.20 取自指令 §45 的 Codex 抽样基线（Generic/Padding 21%），含义是
# "不得比抽样时更差"；字数占比另给一条 0.10，因为长句真内容会把条数占比稀释掉。
PADDING_ITEM_SHARE_REVIEW = 0.20
PADDING_CHAR_SHARE_REVIEW = 0.10
# §46 点名的结构化载体。补页只能往这些形态上加，它们与"重复 Narrative"是两条路。
STRUCTURED_CARRIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "SOP_PROCESS": re.compile(r"SOP|作业指导|操作流程|处置流程|服务流程|工艺|步骤|程序|预案"),
    "CHECKLIST": re.compile(r"检查表|核查表|巡检表|点检|对照表|清单"),
    "RISK_REGISTER": re.compile(r"风险"),
    "KPI": re.compile(r"考核|指标|KPI|评分|满意度|绩效"),
    "RESPONSIBILITY_MATRIX": re.compile(r"职责|责任|分工|RACI|岗位设置|人员配置"),
    "STANDARD": re.compile(r"标准|规范|要求|参数|限值|口径"),
    "FORM": re.compile(r"记录表|登记表|台账|表单|申请表|交接单|确认单|反馈单|签到"),
    "SCHEDULE": re.compile(r"计划|进度|排班|频次|时间安排|甘特|节点"),
}
_NARRATIVE_MARKERS = (".paragraphs[", ".body_blocks[")
_STRUCTURED_MARKERS = (".bullets[", ".processes[", ".callouts[")


def classify_text(text: str) -> dict[str, Any]:
    """一条句子的分桶结果。所有判据都是常量，桶位可被样本反查、可被反驳。"""
    value = str(text or "").strip()
    chars = len(value)
    if not chars:
        return {"chars": 0, "anchors": [], "anchor_kinds": 0, "technical_term_hits": 0,
                "excluded_scope_hit_total": 0, "fact_char_share": 0.0, "hedge_per_1000": 0.0,
                "bucket": USABLE, "reasons": ["EMPTY"]}
    anchors = [name for name, pattern in TECHNICAL_ANCHOR_PATTERNS.items() if pattern.search(value)]
    doc_check = bool(DOCUMENT_SELF_CHECK.search(value))
    if doc_check and "act" in anchors:
        # 动作指向文件本身（"本卷回引句式逐句比对并登记检查人"）时不计现场实质。
        anchors = [name for name in anchors if name != "act"]
    technical_terms = len(TECHNICAL_PAGE_TERMS.findall(value))
    scope_hits = sum(len(pattern.findall(value)) for pattern in EXCLUDED_SCOPE_PATTERNS)
    fact_share = sum(len(match.group(0)) for match in CANONICAL_FACT.finditer(value)) / chars
    hedge_per_1000 = len(HEDGE.findall(value)) * 1000.0 / chars
    reasons: list[str] = []
    fact_restate = fact_share >= FACT_SHARE_MAX
    if (not anchors and technical_terms == 0) or fact_restate:
        # 两轴同时为 0，或整句一半以上在复述同一批事实 —— 两条判据都要记名：
        # 只报前一条会把"事实复述"这一类从理由直方图里吞掉，削减时无从下手。
        bucket = GENERIC_PADDING
        if not anchors and technical_terms == 0:
            reasons = ["BID_PROCESS_BOILERPLATE"] if scope_hits else ["NO_ANCHOR_NO_TECHNICAL_TERM"]
            if doc_check:
                # 直方图里单独记一笔：这一类是文件自我校核，归 §0 范围治理处理，不是作业描述写薄了。
                reasons.append("DOCUMENT_SELFCHECK")
        if fact_restate:
            reasons.append("CANONICAL_FACT_RESTATEMENT")
    elif not anchors:
        bucket, reasons = USABLE, ["TECHNICAL_TERM_ONLY_NO_CHECKABLE_ANCHOR"]
    elif len(anchors) >= 2 and ("record" in anchors or "quant" in anchors or "standard" in anchors) \
            and hedge_per_1000 < HEDGE_PER_1000_MAX:
        bucket, reasons = HIGH_VALUE, [f"ANCHORS:{'+'.join(sorted(anchors))}"]
    else:
        bucket = USABLE
        if len(anchors) < 2:
            reasons.append("SINGLE_ANCHOR_ONLY")
        if hedge_per_1000 >= HEDGE_PER_1000_MAX:
            reasons.append("HEDGE_DENSITY_OVER")
        reasons.insert(0, f"ANCHORS:{'+'.join(sorted(anchors))}")
    return {"chars": chars, "anchors": sorted(anchors), "anchor_kinds": len(anchors),
            "technical_term_hits": technical_terms, "excluded_scope_hit_total": scope_hits,
            "fact_char_share": round(fact_share, 4), "hedge_per_1000": round(hedge_per_1000, 3),
            "bucket": bucket, "reasons": reasons}


def _shares(rows: list[dict[str, Any]]) -> dict[str, Any]:
    items = len(rows) or 1
    chars = sum(row["chars"] for row in rows) or 1
    by_bucket_items = Counter(row["bucket"] for row in rows)
    by_bucket_chars: dict[str, int] = defaultdict(int)
    for row in rows:
        by_bucket_chars[row["bucket"]] += row["chars"]
    return {
        "items": len(rows),
        "chars": sum(row["chars"] for row in rows),
        "items_by_bucket": {key: by_bucket_items[key] for key in BUCKETS},
        "chars_by_bucket": {key: by_bucket_chars[key] for key in BUCKETS},
        "item_share": {key: round(by_bucket_items[key] / items, 4) for key in BUCKETS},
        "char_share": {key: round(by_bucket_chars[key] / chars, 4) for key in BUCKETS},
    }


def measure_payload(payload: Any) -> dict[str, Any]:
    """对任意 CU 正文 / artifact 片段算低增量占比。纯本地，零 Provider 调用。"""
    rows = []
    for _path, text in _walk_strings(payload if isinstance(payload, (dict, list)) else {"value": payload}):
        stripped = str(text or "").strip()
        if len(stripped) >= MIN_NARRATIVE_CHARS:
            rows.append(classify_text(stripped))
    padding = [row for row in rows if row["bucket"] == GENERIC_PADDING]
    padding_item_share = round(len(padding) / (len(rows) or 1), 4)
    padding_char_share = round(sum(row["chars"] for row in padding)
                               / (sum(row["chars"] for row in rows) or 1), 4)
    return {
        "items": len(rows),
        "chars": sum(row["chars"] for row in rows),
        "low_gain_items": len(padding),
        "low_gain_chars": sum(row["chars"] for row in padding),
        "padding_item_share": padding_item_share,
        "padding_char_share": padding_char_share,
        "reason_histogram": dict(Counter(reason for row in padding for reason in row["reasons"])),
        "shares": _shares(rows),
    }


def carriers_present(texts: Iterable[Any]) -> dict[str, int]:
    """一批标题 / 表名里出现了几类 §46 结构载体。"""
    counts: Counter[str] = Counter()
    for text in texts or []:
        value = str(text or "")
        for name, pattern in STRUCTURED_CARRIER_PATTERNS.items():
            if pattern.search(value):
                counts[name] += 1
    return {name: counts.get(name, 0) for name in STRUCTURED_CARRIER_PATTERNS}


def content_padding_report(artifact: dict[str, Any] | None, project_type: str = "") -> dict[str, Any]:
    """整篇交付件的 §45 报告。上限只到 NEEDS_REVIEW —— §38 那条口径：比例过高要复核，
    但不因为一把度量尺子把整篇判死。"""
    walked = [(path, str(text).strip()) for path, text in _walk_strings(artifact or {}) if str(text).strip()]
    narrative, structured, other = [], [], []
    for path, text in walked:
        if len(text) < MIN_NARRATIVE_CHARS:
            continue
        row = classify_text(text)
        row["path"] = path
        if any(marker in path for marker in _NARRATIVE_MARKERS):
            narrative.append(row)
        elif any(marker in path for marker in _STRUCTURED_MARKERS):
            structured.append(row)
        else:
            other.append(row)
    all_rows = narrative + structured + other
    shares = _shares(all_rows)
    padding_items = [row for row in all_rows if row["bucket"] == GENERIC_PADDING]
    review = (shares["item_share"][GENERIC_PADDING] > PADDING_ITEM_SHARE_REVIEW
              or shares["char_share"][GENERIC_PADDING] > PADDING_CHAR_SHARE_REVIEW)
    per_section: dict[str, dict[str, int]] = defaultdict(lambda: {"items": 0, "low_gain_items": 0,
                                                                  "chars": 0, "low_gain_chars": 0})
    for row in all_rows:
        match = re.match(r"artifact\.sections\[(\d+)\]", row["path"])
        key = f"sections[{match.group(1)}]" if match else "document"
        bucket = per_section[key]
        bucket["items"] += 1
        bucket["chars"] += row["chars"]
        if row["bucket"] == GENERIC_PADDING:
            bucket["low_gain_items"] += 1
            bucket["low_gain_chars"] += row["chars"]
    worst = sorted(((dict(values) | {"path": key,
                                      "low_gain_item_share": round(values["low_gain_items"] / (values["items"] or 1), 4)})
                    for key, values in per_section.items() if values["low_gain_items"]),
                   key=lambda item: -item["low_gain_chars"])[:10]
    return {
        "schema_version": PADDING_SCHEMA_VERSION,
        "lexicon_version": PADDING_LEXICON_VERSION,
        "status": "NEEDS_REVIEW" if review else "PASS",
        "project_type": str(project_type or ""),
        **shares,
        "narrative": _shares(narrative),
        "structured": _shares(structured),
        "other_surfaces": _shares(other),
        "low_gain_items": len(padding_items),
        "low_gain_chars": sum(row["chars"] for row in padding_items),
        "reason_histogram": dict(Counter(reason for row in padding_items for reason in row["reasons"])),
        "review_thresholds": {"padding_item_share": PADDING_ITEM_SHARE_REVIEW,
                              "padding_char_share": PADDING_CHAR_SHARE_REVIEW},
        "measurement_basis": "MEASURED_LOCAL_TEXT",
        "policy": "§45：低增量=无可核查载体且无技术主题词的句子，或 Canonical Fact 复述；"
                  "§47 的重复检测继续独立生效，两条门互不顶替。比例过高只到 NEEDS_REVIEW。",
        "reduction_priority": "§22/§45：超页时先删 low_gain_item_share 最高的节，"
                              "其次才是字数超预算的节。",
        "worst_surfaces": worst,
        # 只给定位用的路径，不抄客户原文：这份报告会进运行目录，路径足够让人翻到那一条。
        "low_gain_paths": [row["path"] for row in padding_items[:40]],
    }
