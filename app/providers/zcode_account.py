from pathlib import Path
from .base import AIProvider,ProviderError,normalize_generation,normalize_recommendation,parse_provider_payload,write_json


def zcode_provider_rows():
    from zcode_account.connection import connection,enabled
    if not enabled():return []
    return [{'provider_name':'zcode_cn','provider_type':'zcode_account','enabled':True,
        'display_name':'ZCode 国内账户（实验）','model':connection().selection.get('model',''),
        'qualification_status':'experimental','test_mode':False,'timeout':240,
        'endpoint_alias':'official_zcode_bigmodel_standalone_cli',
        'billing_note':'使用智谱国内账户权限，额度及适用范围以官方套餐为准；连接检查不试写。'}]


class ZCodeAccountProvider(AIProvider):
    provider_version='0.1-experimental'
    def __init__(self,config,credential_store=None):super().__init__(config,None)
    def prepare_ppt_run(self,run_id,body_pages,call_cap):
        from zcode_account.connection import connection
        return connection().prepare_ppt_run(run_id,body_pages,call_cap)
    def get_metadata(self):
        from zcode_account.connection import connection
        return {**super().get_metadata(),'model':connection().selection.get('model',''),
            'credential_source':'official_zcode_bigmodel_login','transport':'official_zcode_standalone_cli',
            'account_edition':'cn','observed_model':'NOT_AVAILABLE','billable':None,
            'cost_availability':'NOT_AVAILABLE','fallback_used':False}
    def health_check(self):
        from zcode_account.connection import connection
        return {**connection().health(),'metadata':self.get_metadata()}
    def _invoke(self,request,task_dir):
        from zcode_account.connection import connection
        from .openai_compatible import CURRENT_RUN_ID,is_cancelled
        if request.get('purpose')=='capability_probe' or request.get('generation_mode')=='capability_probe':
            raise ProviderError('ZCode连接检查不试写，请单独发起生成。',error_code='ZCODE_EXPLICIT_GENERATION_REQUIRED')
        payload=dict(request);payload['run_id']=payload.get('run_id') or CURRENT_RUN_ID.get() or 'standalone'
        ctl = connection()
        if payload.get('generation_mode') == 'tender_requirement_recognition':
            from tender_intake.zcode_identity_recovery import apply_approved_retry
            payload = apply_approved_retry(payload, ctl)
        from account_tasks import execute
        row=execute('zcode_cn',payload,Path(task_dir),ctl,lambda:is_cancelled(payload['run_id']))
        write_json(Path(task_dir)/'provider_raw_envelope.json',row)
        try:
            if payload.get('generation_mode')=='tender_requirement_recognition':
                from .zcode_recognition import parse_recognition
                result,repair=parse_recognition(payload,row.get('body'))
            else:
                result,repair=parse_provider_payload(payload,row.get('body'),set(payload.get('required_keys') or []))
        except ProviderError as exc:
            write_json(Path(task_dir)/'provider_parse_error.json',{'error_code':exc.error_code,
                'message':str(exc),'request_digest':row.get('digest'),'response_received':True,
                'automatic_resend':False})
            connection().event('response_format_failed',run_id=payload['run_id'],task_id=request.get('task_id'),error_code=exc.error_code)
            raise
        if request.get('generation_mode')=='ppt_slide' and result.get('slide_id')!=request.get('slide_id'):
            raise ProviderError('ZCode返回页号与请求不一致，原答已保留。',error_code='PPT_SLIDE_ID_MISMATCH')
        observed=row.get('observed_selection') or {}
        metadata=self._task_metadata(str(request.get('task_id') or ''),formal_output=True,
            requested_model=row['requested_model'],observed_model=observed.get('modelId','NOT_AVAILABLE'),
            usage=row.get('usage'),cost_availability='NOT_AVAILABLE',finish_reason=row.get('finish_reason'),
            serialization_repair=repair or {'applied':False},request_digest=row['digest'])
        write_json(Path(task_dir)/'provider_structured_output.json',result)
        write_json(Path(task_dir)/'provider_audit.json',metadata)
        connection().event('validated_response',run_id=payload['run_id'],task_id=request.get('task_id'))
        return result,metadata
    def invoke_structured(self,request,task_dir):
        result,metadata=self._invoke(request,task_dir);return {**result,'provider_metadata':metadata}
    def collect_late_response(self,request,task_dir):
        from account_tasks import cached_request
        original=cached_request('zcode_cn',request)
        return self._invoke(original,task_dir) if original else None
    def recommend_knowledge(self,request,task_dir):
        result,metadata=self._invoke(request,task_dir);return normalize_recommendation(result,metadata)
    def generate_solution(self,request,task_dir):
        result,metadata=self._invoke(request,task_dir);return normalize_generation(result,metadata)
