"""Local ZCode execution contract. No inference, credentials or quality overrides."""
import json
from pathlib import Path
from providers.base import ProviderError,write_json

DEFAULT_GENERATION_CALLS=8
MAX_PPT_CALLS=96

def prepare_ppt_policy(jobs:Path,run_id:str,model:str,pages:int,call_cap:int):
    if not (1<=pages<=70 and pages<=call_cap<=MAX_PPT_CALLS):
        raise ProviderError('PPT调用计划超出支持范围，未发送请求。',error_code='ZCODE_PLAN_INVALID')
    jobs.mkdir(parents=True,exist_ok=True);path=jobs/'.ppt_policy'
    if (jobs/'.model').exists() and (jobs/'.model').read_text(encoding='utf-8')!=model:
        raise ProviderError('任务绑定的模型与当前选择不同，未改变原任务。',error_code='ZCODE_PLAN_IDENTITY')
    if path.exists():
        saved=json.loads(path.read_text(encoding='utf-8'))
        if (saved.get('run_id'),saved.get('model'),saved.get('body_pages'),saved.get('page_call_cap'))!=(run_id,model,pages,call_cap):
            raise ProviderError('PPT接续计划与原预算不一致，未扩大旧预算。',error_code='ZCODE_PLAN_IDENTITY')
        return saved
    rows=[json.loads(p.read_text(encoding='utf-8')) for p in jobs.glob('*.json') if not p.name.endswith('.answer.json')]
    # A missing policy plus existing page markers is the old Word fallback, not a finished plan.
    prior=sum(1 for r in rows if r.get('status')!='not_dispatched' and r.get('budget_scope') in (None,'generation') and (r.get('task_id') or r.get('generation_mode')))
    if prior+call_cap>MAX_PPT_CALLS:
        raise ProviderError('PPT计划加既有调用超过本轮96次上限，未发送请求。',error_code='ZCODE_PLAN_INVALID')
    result={'version':1,'run_id':run_id,'model':model,'body_pages':pages,'page_call_cap':call_cap,
            'prior_generation_calls':prior,'generation_call_cap':prior+call_cap,'parallelism':1,'legacy':False}
    write_json(path,result);return result

def generation_call_cap(jobs:Path):
    path=jobs/'.ppt_policy'
    if not path.exists():return DEFAULT_GENERATION_CALLS
    row=json.loads(path.read_text(encoding='utf-8'))
    cap=row.get('generation_call_cap')
    if not isinstance(cap,int) or not 1<=cap<=MAX_PPT_CALLS:
        raise ProviderError('PPT调用预算记录无效，未发送请求。',error_code='ZCODE_PLAN_INVALID')
    return cap

def partial_execution_issues(run_dir:Path):
    """Explain an incomplete deck using execution evidence, not downstream QA symptoms."""
    messages=[]
    for path in sorted((run_dir/'longform/ppt/slides').glob('*/audit.json')):
        try:attempts=json.loads(path.read_text(encoding='utf-8')).get('attempts') or []
        except (OSError,ValueError,AttributeError):continue
        for row in attempts:
            text=str(row.get('error') or '')
            if '8次' in text or 'ZCODE_RUN_BUDGET'==row.get('native_error_code'):
                label='本任务原调用次数已用尽；已完成页保留，旧预算未自动扩大。'
            elif '未发送' in text or row.get('invoked') is False:
                label='部分页面在排队或准备时用完时间，尚未提交模型。'
            elif '未返回完整结果' in text or row.get('native_error_code')=='ZCODE_RESPONSE_UNKNOWN':
                label='部分调用未收到完整结果，已有请求记录保留，未自动重发。'
            elif '配置' in text:
                label='调用期间模型配置校验未通过，原记录已保留。'
            elif text:label='部分页面的模型调用或结果接收未完成。'
            else:continue
            if label not in messages:messages.append(label)
    return [{'label':text} for text in messages] or [{'label':'正文尚未全部生成，已生成内容已保存。'}]
