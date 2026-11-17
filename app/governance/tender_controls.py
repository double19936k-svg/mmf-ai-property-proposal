"""Immutable, located tender controls used by planning and the final DOCX gate."""
from __future__ import annotations
import hashlib,json,re
from pathlib import Path


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def extract_controls(extraction):
    staffing=[]
    for table in extraction.get('tables',[]):
        columns=table.get('columns') or []
        headings=' '.join(table.get('heading_path') or [])
        if len(columns)<2 or '岗位' not in str(columns[0]) or not re.search(r'人员配置|人数',str(columns[1])):
            continue
        if re.search(r'投标文件格式|报价|工资',headings):
            continue
        roles={};total=None;invalid=False
        for row in table.get('rows') or []:
            if len(row)<2:continue
            role=compact(row[0]);raw=compact(row[1])
            if not role or not raw:continue
            if not re.fullmatch(r'\d+',raw):invalid=True;continue
            if re.search(r'合计|总计',role):total=int(raw)
            elif role in roles and roles[role]!=int(raw):invalid=True
            else:roles[role]=int(raw)
        if roles and not invalid and (total is None or total==sum(roles.values())):
            staffing.append({'roles':roles,'minimum_staffing':sum(roles.values()),'total_explicit':total is not None,'unit':'person','status':'source_literal','source':{k:table.get(k) for k in ['file_id','table_id','table_index','heading_path']}})
    signatures={json.dumps(x['roles'],ensure_ascii=False,sort_keys=True) for x in staffing}
    staff=staffing[0] if len(signatures)==1 else {'status':'conflict' if staffing else 'unknown','minimum_staffing':None,'roles':{},'candidates':staffing}
    checks=[]
    criteria=[('HEALTH_CERT',r'健康证|健康证明',r'健康证|健康证明'),('RAW_COOKED_SEPARATION',r'生熟|生食物.*熟食物',r'生熟|生食.{0,30}熟食'),('SEPARATE_UTENSILS',r'刀具',r'刀具'),('KITCHEN_UNIFORM',r'工作服',r'工作服'),('PERSONAL_HYGIENE',r'长指甲|必须洗手',r'指甲|洗手')]
    paragraphs=extraction.get('paragraphs') or []
    for key,source_pattern,output_pattern in criteria:
        sources=[{k:p.get(k) for k in ['file_id','para_id','paragraph_index','heading_path','text']} for p in paragraphs if re.search(source_pattern,str(p.get('text') or '')) and not any('投标文件格式' in str(h) for h in p.get('heading_path') or [])]
        if sources:checks.append({'key':key,'output_pattern':output_pattern,'sources':sources})
    from planning.content_engineering import is_service_personnel_requirement
    for p in paragraphs:
        text = str(p.get('text') or '')
        if not is_service_personnel_requirement({'text':text}):
            continue
        if not any('技术' in str(h) for h in p.get('heading_path') or []):
            continue
        literals = [m.group() for pattern in [r'年龄\s*\d+\s*岁(?:以下|以上|以内)', r'[一二三四五六七八九十\d]+年以上[^，。；]{0,24}工作经验', r'厨师等级证书|电工上岗证'] for m in re.finditer(pattern,text)]
        for literal in literals:
            checks.append({'key':'SERVICE_PERSONNEL_' + hashlib.sha256(literal.encode()).hexdigest()[:12], 'output_pattern':re.escape(literal), 'sources':[{k:p.get(k) for k in ['file_id','para_id','paragraph_index','heading_path','text']}]})
    contacts=[]
    for i,p in enumerate(paragraphs):
        text=str(p.get('text') or '')
        context=' '.join(str(x.get('text') or '') for x in paragraphs[max(0,i-2):i+1])
        if re.search(r'招标代理|开标|投标文件.*递交',context):
            for m in re.finditer(r'(?:地址|地点|递交至)[：:\s]*([^。；\n]+)',text):
                val=m.group(1).strip()
                if val and len(val)>6:contacts.append({'value':val,'file_id':p.get('file_id'),'para_id':p.get('para_id')})
    for table in extraction.get('tables') or []:
        for row in table.get('rows') or []:
            text='\n'.join(map(str,row))
            for m in re.finditer(r'招标代理机构地址[：:]\s*([^\n。；]+)',text):contacts.append({'value':m.group(1).strip(),'file_id':table.get('file_id'),'table_id':table.get('table_id')})
    return {'schema_version':'source-controls-v1','staffing':staff,'critical_service_requirements':checks,'contact_locations_not_property_facts':contacts}


def controls_from_run(run_dir):
    root=Path(run_dir);p=root/'tender/extraction.json'
    if not p.is_file():return None
    extraction=json.loads(p.read_text(encoding='utf-8-sig'));verified=set()
    for f in extraction.get('files') or []:
        names=[f.get('original_filename'),Path(str(f.get('stored_relative_path') or '')).name]
        for name in filter(None,names):
            source=(root/'tender/originals'/name).resolve()
            if source.is_relative_to((root/'tender/originals').resolve()) and source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest()==f.get('sha256'):
                verified.add(f.get('file_id'));break
    if extraction.get('files') and not verified:
        return {'source_verification_failed':True}
    if extraction.get('files'):
        extraction={**extraction,**{k:[v for v in extraction.get(k,[]) if v.get('file_id') in verified] for k in ['paragraphs','tables']}}
    return extract_controls(extraction)


def final_control_findings(blocks,controls):
    if not controls:return []
    findings=[]
    def add(rule,issue,**details):findings.append({'rule_id':rule,'path':'final_docx','severity':'BLOCK','issue':issue,**details})
    if controls.get('source_verification_failed'):
        add('SOURCE_CONTROL_BINDING_INVALID','原始招标文件身份校验失败，不能认证来源事实');return findings
    text='\n'.join(str(b.get('text') or '') for b in blocks)
    staff=controls.get('staffing') or {}
    if staff.get('status')=='source_literal':
        for role,count in staff['roles'].items():
            pattern=re.escape(role)+r'[^\d\n]{0,22}'+str(count)+r'(?!\d)'
            if not re.search(pattern,text):add('SOURCE_STAFFING_OMITTED',f'来源岗位配置未落入最终正文：{role}{count}人',source=staff['source'],role=role,expected=count)
        if re.search(r'(?:最低人员配置|人员最低配置|人员配置)[^。\n]{0,35}(?:未给出|未提供|取值不明|仍为待确认)',text):add('KNOWN_STAFFING_MARKED_UNKNOWN','已有来源定员表，成品仍把配置写成未知')
    for check in controls.get('critical_service_requirements') or []:
        if not re.search(check['output_pattern'],text):add('SOURCE_SERVICE_REQUIREMENT_OMITTED',f"原文明确服务要求在终稿缺失：{check['key']}",source=check['sources'][0],requirement_key=check['key'])
    for source in controls.get('contact_locations_not_property_facts') or []:
        val=compact(source['value'])
        for b in blocks:
            line=compact(b.get('text'))
            if val in line and re.search(r'坐落|座落|本项目地址|项目名称与坐落',line) and not re.search(r'并非|不是|不得作为|不作为',line):
                add('CONTACT_ADDRESS_AS_PROPERTY','将代理机构或开标地址声明为物业坐落位置',source=source,artifact_location=b.get('path'));break
    return findings
