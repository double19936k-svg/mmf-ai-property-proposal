"""Profile-P contract for manual property-service proposals.

Planner methods are proposals, never additions to the frozen knowledge corpus.
All output evidence is tied to actual slide IDs, not Word section identifiers.
"""
from __future__ import annotations

import copy
import re
from typing import Any

VERSION = "profile-p-service-v1"
META = re.compile(r"横向箭头|反馈箭头|依次连接|按时间顺序排列|每个里程碑对应|卡片(?:标题|布局)|左图右文|上图下文|绘制.{0,8}(?:箭头|流程图)")
PLACEHOLDER = re.compile(r"案例占位|占位待补充|取得可核验案例资料后再补充|等待真实案例来源")
GROUPS = (
    ("tower", "产业大厦", r"产业大厦|总部大楼|商务大厦", "客梯与就餐高峰、大堂环境、配送与办公客流协调"),
    ("villa", "企业独栋", r"企业独栋|独栋办公", "企业专属联系人、加班与接待、共用与企业自用区域界面"),
    ("research", "研发中试", r"研发中试|中试车间|研发楼", "设备尺寸与运输重量、荷载和用电排水的专业复核、装修试运行邻户沟通"),
    ("production", "标准生产", r"标准生产|标准车间|生产厂房", "装卸与货梯预约、消防通道、公共机电维护、计划停机窗口协商"),
)
# topic, title, owner question, layout, service-scene topic, project-image topic.
# Each page answers one business question; repeated themes are merged instead of
# being split into principle/flow/record/summary pages.
CHAPTERS = (
 ("P01","项目理解与服务界面",[
  ("project_overview","项目规模、配套与使用特征","以关键指标和使用特征概括项目，不重复封面鸟瞰图","modules",None,None),
  ("customer_groups","不同客户群体的服务重点","区分园区管理方、办公研发企业、生产企业和配套经营方的关注点与对应服务，不复述楼宇参数","table",None,None),
  ("layout_zones","功能分区与服务界面","公共区域、生产区域和配套区域分别由谁服务、界面如何衔接","modules",None,"layout_map"),
 ]),
 ("P02","接管与企业入驻",[
  ("handover","承接查验与交付筹备","资料保修、分专业现场查验和试运行复查分别做什么、留下什么结果","modules","handover",None),
  ("first_90_days","接管后首90天建议计划","以接管启动为相对起点，分段说明摸底、整改与复盘工作；可随实际进度调整","table",None,None),
  ("move_in","企业入驻服务","从提前沟通到联合交接、入驻跟进，企业实际经历哪些环节、由谁对接","modules","move_in",None),
  ("fitout","装修与设备进场管理","装修前复核、施工巡查、设备运输保护和完工查验如何分工","modules","fitout",None),
  ("staffing","服务组织与岗位安排","项目负责人及企业、工程、秩序、环境岗位分别承担什么、如何交接","table",None,None),
 ]),
 ("P03","客户服务与安全秩序",[
  ("service_reception","企业接待与入驻咨询","企业首次到访、日常咨询和入驻前沟通分别由谁接待、如何转交","modules","reception",None),
  ("repair_flow","报修受理与进度沟通","从企业报修到结果确认，每一步谁负责、企业如何获知进度","process",None,None),
  ("complaint_levels","投诉分级与升级处理","一般投诉、重复投诉和影响使用的投诉分别如何承接和升级","table",None,None),
  ("access","人员与访客出入","员工、来访和外来作业分别如何核验、引导与结束授权","modules","gate",None),
  ("logistics","货运与装卸组织","到场前如何预约、现场如何组织、结束后由谁恢复；荷载不等于电梯载荷","modules","logistics",None),
  ("emergency","消防与典型事件处置","火情、停水停电、电梯困人和积水分别有哪些先期动作与专业联动","table",None,None),
 ]),
 ("P04","工程运行与公共环境",[
  ("maintenance","工程巡检与预防性维护","巡检看什么、专业维保如何分工、计划检修如何减少对企业使用的影响","modules","maintenance",None),
  ("facilities","共用设施维护分工","供配电、给排水、电梯、消防和建筑设施按移交范围分别维护什么","table",None,None),
  ("faults","设备异常处置与恢复","异常确认、影响控制、专业处置、恢复验证和重复故障分析由谁完成","process",None,None),
  ("energy","能源与公共费用管理","分区读数、运行调整和投入评估如何形成可解释的公共费用依据","modules","energy",None),
  ("cleaning","公共环境与保洁场景","大堂高峰、生产公共区遗撒、雨天防滑和绿化废弃物分别如何安排","modules","cleaning",None),
  ("amenities","生活与商务配套服务","餐饮、公寓酒店和集中来访的公共环境、秩序由谁协作","modules","amenities",None),
 ]),
 ("P05","企业服务与园区协作",[
  ("enterprise_services","企业专项服务菜单","按需求提供会务、室内清洁、办公维护等可选服务，说明范围及另行确认条件","table",None,None),
  ("leasing_support","招商参观与活动保障","参观路线、集中来访、活动用电和现场恢复如何组织","modules","concierge",None),
  ("enterprise_contact","企业沟通与园区协同","企业走访、共同议事和产业需求转交运营方分别如何落地","modules",None,None),
  ("digital_phases","智慧化应用分期建议","先核对既有系统，再按受理、设备和扩展需求分阶段评估","table",None,None),
 ]),
 ("P06","品质与持续运营",[
  ("quality","品质检查与结果验证","现场问题、企业感受和整改结果如何互相验证，而不是只检查表单","modules","quality",None),
  ("seasonal","四季养护与节假日安排","按季节风险、节假日和企业生产节奏分别安排作业与沟通","modules","seasonal",None),
  ("cost_procurement","成本控制与采购管理","按实际需求排班、比较维保采购全周期成本、说明主要支出；不虚构人数、价格或节省比例","modules",None,None),
  ("vacancy","租户变化与空置区域管理","空置巡查、企业扩租、退场搬运和授权更新分别如何衔接","modules",None,None),
  ("clarifications","服务范围与待确认事项","集中列出人员、时段、责任范围及专业资料，由谁确认、影响哪些安排","table",None,None),
 ]),
)

def enabled(pack: dict[str, Any]) -> bool:
    return pack.get("ppt_service_contract") == VERSION

def reference_text(pack: dict[str, Any]) -> str:
    return "\n".join(str(r.get("text") or "") for r in pack.get("reference_material") or [])

def building_profiles(pack: dict[str, Any]) -> list[dict[str, Any]]:
    refs = pack.get("reference_material") or []
    profiles = []
    for kind, label, pattern, actions in GROUPS:
        for ref in refs:
            text = str(ref.get("text") or "")
            # Ignore the project overview and use a detailed product heading if present.
            matches = list(re.finditer(pattern, text))
            match = next((m for m in matches if re.search(r"[#＃]|号|层数", text[m.start():m.start()+90])), None)
            if not match:
                continue
            end = next((m.start() for m in re.finditer(r"(?m)^\s*(?:第\d+页|[●]\s*)", text)
                        if m.start() > match.start()+8), len(text))
            excerpt = text[match.start():min(end, match.start()+650)].strip()
            profiles.append({"kind":kind,"label":label,"source_reference_id":ref.get("reference_id"),
                             "source_excerpt":excerpt,"source_span_in_reference":{"start":match.start(),"end":min(end,match.start()+650)},
                             "proposed_actions":actions,"authority":"USER_SUPPLIED_REFERENCE_NOT_CURRENT_SITE_VERIFIED"})
            break
    return profiles

CHAPTER_GOALS = {
    "P01": "园区特点、客户需求与各类楼宇的服务重点",
    "P02": "承接查验、首期计划、企业入驻与装修管理",
    "P03": "企业接待、报修投诉、人员货运与事件响应",
    "P04": "专业维护、设备异常、能源费用与公共环境",
    "P05": "专项服务、招商配合、企业沟通与智慧应用",
    "P06": "品质检查、季节养护、采购成本与服务范围",
}
EXPECTED_STEPS = {"repair_flow": 4, "faults": 5}
PROJECT_IMAGE_CHAPTERS = {"P01"}
# 正文页数按 PPT 自己的三档，不从 Word 页数折算。
PPT_PAGE_BANDS = ((15, 20), (30, 40), (50, 70))
CORE_TOPICS = {
    "project_overview", "customer_groups", "layout_zones",
    "handover", "first_90_days", "move_in", "staffing",
    "service_reception", "repair_flow", "complaint_levels", "emergency",
    "maintenance", "facilities", "faults", "cleaning",
    "enterprise_services", "enterprise_contact",
    "quality", "clarifications",
}
# 第二档补到 30–40 页，第三档补到 50–70 页。每条都是独立业务问题，不把同一主题拆成原则/记录/总结。
EXTRA_TOPICS = (
    ("P02", 2, "public_interface", "公共与企业自用界面", "共用设施、企业专属区域和专业维保分别由谁负责、如何交接", "modules"),
    ("P03", 2, "night_cover", "夜间与节假日值守", "夜间和节假日的秩序、工程值班以及企业紧急联络如何安排", "table"),
    ("P03", 2, "elevator_split", "客梯与货梯分流", "客流高峰、货运预约和装修运输分别走哪部电梯、由谁现场协调", "modules"),
    ("P04", 2, "waste_stream", "废弃物与消杀安排", "生活垃圾、生产废弃物和消杀作业如何分类定时清运，并减少对办公和生产的影响", "modules"),
    ("P04", 2, "fee_explain", "公共费用说明", "分区读数、公摊范围和异常用量由谁核对、如何向企业说明", "table"),
    ("P03", 2, "auth_update", "人员与授权变更", "入职、离职、访客权限和作业许可变更由谁受理、何时生效", "modules"),
    ("P01", 3, "traffic_flow", "人车流线与装卸口", "员工、访客、货车分别从哪里进出，高峰时如何避免互相占用", "modules"),
    ("P01", 3, "shared_peaks", "共享空间与高峰使用", "大堂、食堂、会议室和装卸区在高峰时段由谁组织、如何错峰", "table"),
    ("P02", 3, "trade_checklist", "分专业查验清单", "建筑、机电、消防和保洁分别查什么、问题交给谁复查", "table"),
    ("P02", 3, "vacant_watch", "未入驻区域看护", "空置房屋、未启用机房和公共通道如何巡查、发现异常向谁报告", "modules"),
    ("P03", 3, "escalation_path", "企业联络与升级路径", "日常联系人、超时未处理事项和影响生产的事项分别找谁", "table"),
    ("P03", 3, "repeat_repair", "重复报修回看", "同一部位再次报修时如何核对上次处理、是否升级到专业维保", "modules"),
    ("P03", 3, "work_permit", "外来作业许可", "外来施工和检测人员如何申请、由谁陪同、结束后如何收回授权", "modules"),
    ("P04", 3, "outage_plan", "停水停电先期安排", "计划停电停水前通知谁、现场如何防护、恢复后由谁确认", "table"),
    ("P04", 3, "rain_response", "雨天防滑与积水", "入口、坡道和装卸区在降雨时由谁处理积水、如何向企业提示", "modules"),
    ("P04", 3, "fire_lane", "消防通道与疏散", "消防车道、疏散门和占用物料由谁每日查看、发现堵塞如何当场处理", "modules"),
    ("P04", 3, "fitout_waste", "装修垃圾与运输时段", "装修垃圾何时清运、走哪条路线、谁在现场维持通道", "modules"),
    ("P05", 3, "indoor_boundary", "室内保洁与公共保洁界面", "企业室内、公共走廊和卫生间分别由谁清洁、交界处如何衔接", "table"),
    ("P05", 3, "visit_route", "参观路线与现场恢复", "参观线路、临时用电和活动结束后的恢复由谁安排", "modules"),
    ("P05", 3, "system_check", "既有系统核对", "门禁、监控和能耗系统先核对现状，再决定是否分期补充", "table"),
    ("P05", 3, "lighting_energy", "公共照明与运行时段", "公共照明、空调和景观用电按使用时段如何开关、由谁复核", "modules"),
    ("P06", 3, "visit_log", "企业走访记录", "走访看什么、问题如何转给责任岗位、下次如何核对是否解决", "table"),
    ("P06", 3, "season_switch", "季节性设备切换", "供冷供热和防汛设备在换季时由谁检查、如何通知受影响企业", "modules"),
    ("P06", 3, "buy_boundary", "采购与维保界面", "日常物料、专项维保和紧急外委分别由谁提出、如何验收", "table"),
    ("P06", 3, "move_out", "退场搬运与恢复", "企业退场时搬运时段、门禁收回和房屋恢复由谁组织", "modules"),
    ("P06", 3, "scope_change", "服务范围变更", "新增或取消服务时由谁确认范围、从何时开始执行", "modules"),
    ("P03", 3, "callback", "投诉回访", "处理完成后由谁回访、企业仍不满意时如何升级", "modules"),
    ("P06", 3, "spot_check", "品质抽查样本", "抽查覆盖哪些区域、不合格项如何限期整改并复核", "table"),
    ("P06", 3, "year_plan", "年度养护计划", "年度养护、节前检查和重大活动保障如何排期并告知企业", "modules"),
    ("P04", 3, "lift_rescue", "电梯困人协同", "困人后谁先到场防护、谁联系维保、恢复后如何向被困人员说明", "modules"),
    ("P02", 3, "key_handover", "钥匙与资料移交", "图纸、钥匙、保修资料和账号由谁清点、缺项如何补交", "table"),
)


def _ppt_band(slide_band: tuple[int, int], explicit: int) -> tuple[int, int]:
    low, high = slide_band
    if explicit and explicit > high:
        for band in PPT_PAGE_BANDS:
            if explicit <= band[1]:
                low, high = band
                break
        else:
            low = high = explicit
    if explicit:
        low = max(low, explicit)
        if low > high:
            high = low
    return low, high


def _take_across_chapters(group: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in group:
        chapter = row["chapter_id"]
        if chapter not in buckets:
            order.append(chapter)
            buckets[chapter] = []
        buckets[chapter].append(row)
    picked: set[int] = set()
    while len(picked) < count and any(buckets[chapter] for chapter in order):
        for chapter in order:
            if buckets[chapter] and len(picked) < count:
                picked.add(id(buckets[chapter].pop(0)))
    return [row for row in group if id(row) in picked]


def _fit_slide_band(slides: list[dict[str, Any]], low: int, high: int) -> list[dict[str, Any]]:
    keep: set[int] = set()
    count = 0
    for tier in (1, 2, 3):
        group = [row for row in slides if int(row.get("depth_tier") or 2) == tier]
        if not group:
            continue
        if count >= low and count + len(group) > high:
            break
        room = high - count
        if len(group) <= room:
            keep.update(id(row) for row in group)
            count += len(group)
            if count >= low:
                break
        else:
            need = max(0, low - count)
            take = _take_across_chapters(group, min(len(group), max(need, room)))
            keep.update(id(row) for row in take)
            break
    chosen = [row for row in slides if id(row) in keep]
    return chosen or slides[:high]


def build_plan(pack: dict[str, Any], word_outline: list[dict[str, Any]], slide_band: tuple[int, int] = (15, 20)) -> dict[str, Any]:
    profiles = building_profiles(pack)
    reqs = [r for r in pack.get("requirements") or [] if r.get("confirmation_status") != "REJECTED"]
    request_text="\n".join(r.get("normalized_requirement", "") for r in reqs)
    meetings_excluded=bool(re.search(r"(?:不含|不提供|不包括|无需)(?:承担|提供)?会务",request_text))
    chapters=[]; slides=[]
    for cid, title, definitions in CHAPTERS:
        definitions=list(definitions)
        if meetings_excluded:
            definitions=[(topic,name,purpose.replace("会务、", ""),layout,scene,image) for topic,name,purpose,layout,scene,image in definitions if topic!="meetings"]
        if cid=="P01":
            definitions += [(f"building_{p['kind']}", p["label"]+"服务重点", p["proposed_actions"],"modules",None,p["kind"]) for p in profiles]
            if re.search(r"销售|租赁|出租", reference_text(pack)):
                definitions += [("ownership","租售并存的管理安排","公共规则统一，产权方和实际使用企业分别联络；区分共用维护与自用专项","modules",None,None)]
        tier_of = {topic: 1 if topic in CORE_TOPICS or topic.startswith("building_") or topic == "ownership" else 2 for topic, *_rest in definitions}
        for _chapter, tier, topic, name, purpose, layout in EXTRA_TOPICS:
            if _chapter != cid or (topic == "meetings" and meetings_excluded):
                continue
            definitions.append((topic, name, purpose, layout, None, None))
            tier_of[topic] = tier
        for topic, name, purpose, layout, scene, image in definitions:
            group=next((p for p in profiles if topic=="building_"+p["kind"]),None)
            image_topic=image if image else (group["kind"] if group else None)
            slides.append({"slide_id":f"SL-{len(slides)+1:03d}","chapter_id":cid,"slide_title":name,
                "slide_role":"process" if layout=="process" else "detail",
                "core_message":purpose,"supporting_points":[purpose],
                "layout_intent":layout,"required_visual":layout,"component_family_hint":layout,
                "content_source":"SOURCE_REFERENCES_AND_CONDITIONAL_PLANNER_METHOD",
                "source_requirements":[r["requirement_id"] for r in reqs],
                "source_knowledge":[],"evidence_requirement":"SOURCE_OR_EXPLICIT_PROPOSAL",
                "profile_p_contract":VERSION,"topic_id":topic,"building_profile":group,
                "ppt_topic_owner":None,"topic_mode":"SECTION_SPECIFIC",
                "expected_steps":EXPECTED_STEPS.get(topic),
                "expected_modules":3 if scene else None,
                "image_topic":image_topic if cid in PROJECT_IMAGE_CHAPTERS else None,
                "scene_topic":scene,"depth_tier":tier_of.get(topic, 2)})
        chapters.append({"chapter_id":cid,"chapter_title":title,"narrative_goal":CHAPTER_GOALS[cid],
            "key_messages":[],"target_slide_count":0,
            "source_requirements":[r["requirement_id"] for r in reqs],"priority":"high","case_source_required":False})
    stated = [int(item) for row in reqs for item in re.findall(r"不少于\s*(\d+)\s*页", row.get("normalized_requirement") or "")]
    low, high = _ppt_band(slide_band, max(stated) if stated else 0)
    slides = _fit_slide_band(slides, low, high)
    for index, row in enumerate(slides, 1):
        row["slide_id"] = f"SL-{index:03d}"
    kept = {row["chapter_id"] for row in slides}
    chapters = [block for block in chapters if block["chapter_id"] in kept]
    for block in chapters:
        owned = [row for row in slides if row["chapter_id"] == block["chapter_id"]]
        block["key_messages"] = [row["slide_title"] for row in owned]
        block["target_slide_count"] = len(owned)
    return {"schema_version":VERSION,"profile_p_contract":VERSION,"chapter_blocks":chapters,"slide_storyboard":slides,
        "building_profiles":profiles,"presentation_narrative":{"story":"项目差异、接管入驻、客户服务、专业运营、企业协作、持续运营",
            "principles":["每页回答业主的具体问题","来源事实与实施建议分开","不复制历史案例或未知承诺","同一主题只在一页展开"]},
        "narrative_budget":[{"chapter_id":c["chapter_id"],"slide_count":c["target_slide_count"],
            "narrative_weight":round(c["target_slide_count"]/len(slides),3)} for c in chapters],
        "coverage_summary":{"items":[{"requirement_id":r["requirement_id"],"coverage_type":"PLANNED_REQUIRES_FINAL_EVIDENCE",
            "slide_ids":[s["slide_id"] for s in slides]} for r in reqs],"not_present":0,
            "final_coverage":"NOT_ASSESSED"},
        "source_trace_policy":{"frontend_req_id_visible":False,"backend_trace_required_for_confirmed_numeric_scope_and_sla":True},
        "layout_diversity_summary":{"slide_role_count":2,"component_family_count":3,"warnings":[]},
        "planner_independence":{"word_chapter_titles":[c.get("chapter_title") for c in word_outline],
            "ppt_chapter_titles":[c["chapter_title"] for c in chapters],"identical_chapter_sequence":False,
            "ppt_section_to_word_section_one_to_one":False},
        "ppt_topic_ownership":[],"execution_policy":{"profile":"bounded_90min_v1","parallelism":2,"wall_seconds":5400,"generation_reserve_seconds":1200,"max_total_pages":80,"max_page_attempts":2},
        "output_contract":{"min_total_pages":low,"target_slides_max":high,"min_visual_page_ratio":0.30,
            "counting":"physical pages including cover, navigation, dividers and closing; report content separately",
            "depth_basis":"page information completeness, topic coverage and visible evidence; no Word word-count target"}}

def prompt(context: dict[str, Any], repair: list[str] | None = None, *, text_budget: int | None = None) -> dict[str,str]:
    import json
    slide=context["slide"]
    contract={"headline":"完整业务标题","subheadline":"","key_message":"本页给业主的主要信息",
        "content_blocks":[{"title":"独立生成完整短标题","description":"具体业务安排"}],
        "visual_data":{"layout_type":slide.get("layout_intent"),"flow_direction":"horizontal",
            "steps":[{"title":"业务步骤","body":"具体动作、协作和结果"}],
            "table":{"columns":["阶段或对象","服务安排","企业获知或阶段成果"],"rows":[]}},
        "component_intent":slide.get("layout_intent"),"source_trace":[],
        "slide_id":slide["slide_id"],"speaker_note_optional":"","generation_notes":[]}
    rules=[
        "每页回答slide.core_message中的业务问题，明确对象、动作、协作与使用结果。不得用台账或复盘套话代替服务安排。",
        "汇报页写安排和结果。不写“物业不代作结论”“物业只做现场秩序维护”“待甲方核定”。材料里已经出现的投资、面积、业态和协调要求要写进对应页，不另造材料里没有的数字。",
        "有多业态时，在对应服务页区分共用区域和专用区域，写明触发条件、交接方和实施结果。材料明确列出的常规物业、综合设施和增值服务要对应到实际对象；机房、精密生产等专业区域只写物业可承担的外围保障与交接，技术动作以专业条件为准。",
        "一页只讲一个业务主题，直接进入实质安排；不得把同一主题拆成原则、流程、记录表、总结等多页，也不得与已生成页面重复同一组服务动作。",
        "跨页不得重复使用同一句正文或同一组模块标题；工程方案中的建议只在确有条件时使用，不要把“建议”作为每段正文的统一开头。",
        "语言自然完整，宁可删去次要并列信息，不省略必要虚词或拼造短语；不用分承担、缺项成清单、集中到时等电报腔。直接写动作与结果，角色称谓仅在责任容易混淆时保留。",
        "正文以150—190汉字为软目标，不含页面标题、图注和页脚。模块正文通常42—55字，表格每格通常25—35字；流程按步骤完整性分配篇幅。不为凑下限加话，不为压字数删关键条件；容量硬上限保持原契约。",
        "共性人员、时段、费用等待确认项集中到clarifications主题，不逐段添加免责尾句。本页安全、权属、费用或额外委托条件仍须可读。普通服务写具体处理安排，不用无依据时效数值替代专业内容。",
        "流程图只用于顺序或分工会改变结果的主题，如企业报修、设备异常；其他主题用模块或表格说清对象、分工和结果，不套用五步闭环。",
        "项目建筑效果图只用于项目概况、功能分区和楼宇差异页；服务章节的配图只表达服务场景，不承载业务文字，正文必须能独立说明安排。",
        "headline、步骤和模块title独立生成完整业务词语，建议4—12字；不得从正文截取前N字，也不得输出制图说明。",
        "layout_type、flow_direction属于结构元数据。可见title/body/description中不得出现横向箭头、依次连接、按时间顺序排列等制图指令。",
        "流程页在visual_data.steps分别列出每个业务步骤，3—5步，每步有完整title和body；不要用elements写图形描述。",
        "表格页在visual_data.table列出完整columns和rows，3—6行；模块页content_blocks为2—4项，每项说明具体措施。有配图的模块页优先2—3项，每项正文不超过60字；4项时每项不超过42字。",
        "宏观面积、区位等参数集中在项目概况页，其他页面只在解释措施确有必要时引用；不要求每页加数字。",
        "结合building_profile说明楼宇差异；楼面荷载、电梯额定载荷、运输路线与设备尺寸分别依专业资料复核，不互相推导。",
        *(["本页配服务场景示意图，content_blocks只写2—3项，每项正文不超过56字；关键业务信息全部写在可编辑正文中，图内不承载标签。"] if slide.get("scene_topic") else []),
        "未知人数、时段、价格、设备能力、系统现状和承诺指标不可编造。不默认添加10分钟、24小时或百分比。",
        "资料未给出的共用/专有划分、发电机与临时供电能力不可断言。层高不是卸货净高，设备数量不是运输能力。物业协调设计或有资质单位核定专业条件，不代作结论。5#楼图文租售口径不一致时明确差异待核，不择一作定论。",
        "火情、困人及设备异常只概述报警联动、现场防护和专业处置职责，不给未限定资质人员写断电停梯、设备复位等操作指令；紧急风险不能以预约协商代替必要风险控制。",
        "方法明确写为建议或有条件适用。关键范围和实施条件放在可读正文中，不用微小脚注隐藏；待确认事项集中说明。",
        "first_90_days以接管启动为相对起点，分1—15、16—45、46—90天建议阶段，各列具体工作和成果，保留按实际进度调整条件。",
        "不输出案例占位；无服务商资料不编资质业绩；历史案例只有输入明确提供且标明历史参考时才可使用。",
        "不读文件、不联网、不调用工具，只返回当前一页JSON。保留全部要求字段。"
    ]
    if text_budget is not None:
        rules.insert(0, f"本页可见文字总预算为{text_budget}个文字单位，首次生成即须遵守，建议控制在{max(1,text_budget-30)}单位内并预留图注空间。"
            "每个汉字算1单位，连续英文或数字串算1单位。标题headline、主旨key_message，以及当前版式全部模块标题与正文、步骤标题与正文、表头与单元格共同计入总量。"
            "先给标题与主旨预留篇幅，再分配各项内容；用简洁完整的业务表达，合并重复解释。"
            "保留规划要求的全部步骤、对象、具体措施和关键实施条件；不截断句子，不删必要内容，不把关键条件移入speaker_note_optional或generation_notes。")
    if repair:rules+=repair
    return {"system":"你是物业服务方案策划。根据显式输入为业主编写本页可执行、有来源边界的服务建议。只返回JSON。",
        "prompt":"业务内容契约：\n"+"\n".join(rules)+"\n结构示例（按当前版式只填对应结构）：\n"+
                 json.dumps(contract,ensure_ascii=False)+"\nContext Pack:\n"+json.dumps(context,ensure_ascii=False)}

def polish_service_text(value: str) -> str:
    """Known grammatical typos only; keep scope conditions and raw payload intact."""
    for old,new in (("按界面分承担","按责任界面分别承担"),
                    ("缺项成清单","将缺项整理成清单"),("集中到时","集中到访时")):
        value=value.replace(old,new)
    return value


def render_data(payload: dict[str,Any], plan: dict[str,Any]) -> dict[str,Any]:
    """No character slicing or inference of missing business steps."""
    visual=payload.get("visual_data") or {}
    layout=str(visual.get("layout_type") or plan.get("layout_intent") or "modules")
    result={"profile_p_contract":VERSION,"layout":layout,"chapter_id":plan.get("chapter_id",""),
            "slide_id":plan.get("slide_id"),"topic_id":plan.get("topic_id"),"source_trace":copy.deepcopy(payload.get("source_trace") or []),
            "building_kind":(plan.get("building_profile") or {}).get("kind"),"image_topic":plan.get("image_topic"),
            "scene_topic":plan.get("scene_topic"),"expected_steps":plan.get("expected_steps")}
    if layout=="process":
        raw=visual.get("steps") or payload.get("steps") or []
        result["steps"]=[{"title":str(x.get("title") or ""),"body":polish_service_text(str(x.get("body") or ""))} for x in raw if isinstance(x,dict)]
    elif layout=="table":
        result["table"]=copy.deepcopy(visual.get("table") or payload.get("table") or {})
        result["table"]["rows"]=[[polish_service_text(str(c)) for c in row] for row in result["table"].get("rows",[])]
    else:
        result["layout"]="modules"
        result["modules"]=[{"title":str(x.get("title") or ""),"body":polish_service_text(str(x.get("description") or x.get("body") or ""))}
                          for x in payload.get("content_blocks") or [] if isinstance(x,dict)]
    return result

def visible_parts(slide: dict[str,Any]) -> list[str]:
    parts=[str(slide.get("title") or slide.get("headline") or ""),str(slide.get("core_message") or slide.get("key_message") or "")]
    layout=slide.get("layout")
    if layout=="process":
        parts += [str(x.get(k) or "") for x in slide.get("steps") or [] for k in ("title","body")]
    elif layout=="table":
        table=slide.get("table") or {}
        parts += [str(x) for x in table.get("columns") or []]
        parts += [str(x) for row in table.get("rows") or [] for x in row]
    elif layout=="modules":
        parts += [str(x.get(k) or "") for x in slide.get("modules") or [] for k in ("title","body")]
    else:
        parts += [str(x) for x in slide.get("bullets") or []]
    if slide.get("image_caption"): parts.append(str(slide["image_caption"]))
    return [p for p in parts if p.strip()]

def issues(slide: dict[str,Any]) -> list[dict[str,str]]:
    findings=[]
    def add(code,message,severity="BLOCK"):findings.append({"rule_id":code,"severity":severity,"issue":message})
    text="\n".join(visible_parts(slide))
    if META.search(text):add("PPT-P-META","制图或排版指令进入可见业务正文")
    if PLACEHOLDER.search(text):add("PPT-P-PLACEHOLDER","页面包含案例空占位")
    if not str(slide.get("title") or slide.get("headline") or "").strip():add("PPT-P-TITLE","缺少页面标题")
    layout=slide.get("layout")
    items=slide.get("steps") if layout=="process" else slide.get("modules") if layout=="modules" else None
    if layout in {"process","modules"}:
        if layout=="process" and slide.get("expected_steps") and len(items or [])!=slide["expected_steps"]:
            add("PPT-P-STEP-COUNT","实际步骤数与规划不同，请复核必要动作是否完整","NEEDS_REVIEW")
        maximum=5 if layout=="process" else 4
        if not isinstance(items,list) or not 2<=len(items)<=maximum:add("PPT-P-CONTENT","实际步骤或服务安排数量不适合当前版式")
        for item in items or []:
            title=str(item.get("title") or "")
            if not title.strip():
                add("PPT-P-NODE-TITLE","步骤或模块缺少标题")
            elif len(title)>16 or title.endswith(("的","与","及","—","：","、")):
                add("PPT-P-NODE-TITLE","步骤或模块标题需编辑复核；保留已有业务内容","NEEDS_REVIEW")
            if not str(item.get("body") or "").strip():add("PPT-P-NODE-BODY","步骤或模块缺少具体业务动作")
    if layout=="table":
        table=slide.get("table") or {}; cols=table.get("columns") or []; rows=table.get("rows") or []
        if not cols or not rows or any(len(r)!=len(cols) for r in rows):add("PPT-P-TABLE","表格缺列、缺行或行列不闭合")
    if re.search(r"(?:kg|公斤|[tT吨])\s*/\s*[㎡m²]+.{0,30}(?:匹配|确定).{0,10}电梯",text):
        add("PPT-P-PARAMETER","楼面荷载被直接用于推导电梯能力")
    if re.search(r"不因小故障中断|保证.{0,8}不中断|零故障|绝不发生",text):add("PPT-P-PROMISE","缺乏依据的绝对运行承诺")
    if "超期未改自动提醒" in text and not re.search(r"如|若|拟|建议|具备",text):add("PPT-P-SYSTEM","未注明实施条件的现有系统能力")
    if slide.get("image_source")=="AI_GENERATED_SERVICE_ILLUSTRATION":
        caption=str(slide.get("image_caption") or "")
        if "服务场景示意" not in caption:add("PPT-P-SCENE-LABEL","AI服务场景图未标注为服务场景示意")
        if re.search(r"实拍|现场照片|既有设备|真实业绩|实际案例",caption):add("PPT-P-SCENE-CLAIM","服务场景示意图被写成现场或业绩证据")
    if slide.get("image_source")=="PROJECT_SOURCE" and str(slide.get("chapter_id") or "").startswith(("P02","P03","P04","P05","P06")):
        add("PPT-P-IMAGE-SCOPE","项目建筑效果图被用于服务章节")
    return findings


GENERIC_STEP = re.compile(r"^(?:受理|登记|建档|建台账|建立台账|派单|处置|跟踪|反馈|回访|确认|归档|闭环|复盘|总结|改进|回单)$")
ADVICE_START = re.compile(r"^(?:建议|宜|可考虑)")
def _norm_text(value: Any) -> str:
    return re.sub(r"[\s，。；、,.;：:！？!?…—\-]", "", str(value or ""))


def deck_issues(artifact: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """跨页重复与空泛流程检查；只读已装配的可见块。"""
    findings: list[dict[str, Any]] = []; warnings: list[dict[str, Any]] = []
    bodies: dict[str, set[str]] = {}; slide_titles: dict[str, set[str]] = {}
    module_titles: dict[str, set[str]] = {}; all_bodies: list[tuple[str, str]] = []
    for slide in artifact.get("slides") or []:
        sid = str(slide.get("slide_id") or "")
        title = _norm_text(slide.get("title") or slide.get("headline"))
        if len(title) >= 5:
            slide_titles.setdefault(title, set()).add(sid)
        layout = slide.get("layout")
        items = slide.get("steps") if layout == "process" else slide.get("modules") if layout == "modules" else []
        for item in items or []:
            node_title = _norm_text(item.get("title"))
            if len(node_title) >= 4:
                module_titles.setdefault(sid, set()).add(node_title)
            body = _norm_text(item.get("body"))
            if len(body) >= 14:
                bodies.setdefault(body, set()).add(sid); all_bodies.append((sid, str(item.get("body") or "")))
        if layout == "table":
            for row in (slide.get("table") or {}).get("rows") or []:
                for cell in row:
                    body = _norm_text(cell)
                    if len(body) >= 14:
                        bodies.setdefault(body, set()).add(sid); all_bodies.append((sid, str(cell)))
        if layout == "process":
            step_titles = [_norm_text(item.get("title")) for item in items or []]
            generic = sum(bool(GENERIC_STEP.fullmatch(value)) for value in step_titles)
            if len(step_titles) >= 3 and generic / len(step_titles) >= 0.6:
                findings.append({"rule_id": "PPT-P-PROCESS-CLICHE", "severity": "NEEDS_REVIEW", "slide_id": sid,
                    "issue": "流程页由通用闭环词构成，未体现本项目的具体对象与分工"})
    for text, sids in bodies.items():
        if len(sids) >= 2:
            findings.append({"rule_id": "PPT-P-DUPLICATE", "severity": "NEEDS_REVIEW", "slide_ids": sorted(sids),
                "issue": "跨页重复使用同一句可见业务正文", "sample": text[:40]})
    for text, sids in slide_titles.items():
        if len(sids) >= 2:
            findings.append({"rule_id": "PPT-P-DUPLICATE-TITLE", "severity": "NEEDS_REVIEW", "slide_ids": sorted(sids),
                "issue": "跨页重复使用同一业务标题", "sample": text[:40]})
    page_ids = sorted(module_titles)
    for left_index, left in enumerate(page_ids):
        for right in page_ids[left_index + 1:]:
            shared = module_titles[left] & module_titles[right]
            if len(shared) >= 2:
                findings.append({"rule_id": "PPT-P-DUPLICATE-TITLE", "severity": "NEEDS_REVIEW", "slide_ids": [left, right],
                    "issue": "两页重复使用同一组模块标题", "sample": "、".join(sorted(shared))[:40]})
    if len(all_bodies) >= 8:
        advice = sum(bool(ADVICE_START.match(text)) for _, text in all_bodies)
        if advice / len(all_bodies) >= 0.5:
            warnings.append({"rule_id": "PPT-P-ADVICE-DENSITY", "severity": "WARNING",
                "issue": "过半正文以建议开头，服务安排被写成泛化建议", "advice_bodies": advice, "total_bodies": len(all_bodies)})
    return findings, warnings

def payload_issues(payload:dict[str,Any],plan:dict[str,Any])->list[dict[str,str]]:
    slide={"title":payload.get("headline"),"core_message":payload.get("key_message"),**render_data(payload,plan)}
    findings=issues(slide)
    if plan.get("slide_id") and payload.get("slide_id") != plan["slide_id"]:
        findings.append({"rule_id":"PPT-P-SLIDE-ID","severity":"BLOCK","issue":"模型返回页号与当前规划页不一致"})
    return findings

def content_qa(artifact:dict[str,Any],plan:dict[str,Any])->dict[str,Any]:
    slides=artifact.get("slides") or []; expected=plan.get("slide_storyboard") or []
    by_id={s.get("slide_id"):s for s in slides}
    findings=[]
    for s in slides:
        findings += [{**f,"slide_id":s.get("slide_id")} for f in issues(s)]
    deck_findings,deck_warnings=deck_issues(artifact)
    findings += deck_findings
    missing=[p["slide_id"] for p in expected if p["slide_id"] not in by_id]
    for sid in missing:findings.append({"rule_id":"PPT-P-MISSING","severity":"BLOCK","slide_id":sid,"issue":"规划页面未到达成稿"})
    blocked=set()
    for f in findings:
        if f.get("severity") != "BLOCK":
            continue
        blocked.add(f.get("slide_id")); blocked.update(f.get("slide_ids") or [])
    # This is trace completeness only, not semantic confirmation of every MUST.
    trace=[{"requirement_id":r["requirement_id"],"planned_slide_ids":r["slide_ids"],
            "arrived_slide_ids":[sid for sid in r["slide_ids"] if sid in by_id],
            "semantic_status":"PENDING_REVIEW"} for r in plan.get("coverage_summary",{}).get("items",[])]
    status="BLOCK" if any(f.get("severity")=="BLOCK" for f in findings) else "NEEDS_REVIEW"
    return {"schema_version":VERSION,"status":status,"findings":findings,"warnings":deck_warnings,"missing_slide_ids":missing,
        "planned_content_slides":len(expected),"arrived_content_slides":len(slides),
        "complete_content_slides":sum(not any(f.get("severity")=="BLOCK" for f in issues(s)) and s.get("slide_id") not in blocked for s in slides),
        "requirement_trace":trace,"semantic_coverage":"PENDING_REVIEW",
        "word_page_and_character_gates":{"status":"NOT_APPLICABLE","reason":"Profile-P uses final slide evidence, not Word section/word-count depth"},
        "final_media_page_measurement":"PENDING_RENDER","authority_promoted":False}

