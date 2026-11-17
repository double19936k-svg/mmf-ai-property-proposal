from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit
from providers.base import ProviderError, write_json
from .protocol import ProtocolError, identity, spawn
from .standalone import Client, set_configured_selection, configured_selection, verified_installation


def enabled():return os.environ.get('MMF_ZCODE_ACCOUNT_ENABLED','').lower() in ('1','true')


def open_login_page(url):
    webbrowser.open(url)


def trusted_login_url(url):
    try:
        p=urlsplit(url)
        return (p.scheme=='https' and p.hostname in ('zcode.z.ai','bigmodel.cn','open.bigmodel.cn')
            and not p.username and not p.password and p.port in (None,443))
    except ValueError:return False


class Connection:
    def __init__(self,root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.account=Path(os.environ.get('MMF_ZCODE_ACCOUNT_HOME') or self.root/'account').resolve()
        self.lock=threading.RLock();self.execution_lock=threading.Lock();self.control_busy=False
        self.process=None;self.suspended=False;self.events=[];self.sequence=0
        self.state={'status':'not_connected','message':'尚未连接ZCode国内账户','models':[],
            'reason_code':'ZCODE_LOGIN_REQUIRED','next_action':'login','login_stage':'not_connected','login_expires_at':None,
            'login_url':'','connection_check_model_requests':0,'quota_availability':'NOT_AVAILABLE',
            'verification_scope':'official_login_and_standalone_cli_saved_selection','model_generation_verified':False,
            'model_catalog_availability':'NOT_AVAILABLE'}
        try:self.selection=json.loads((self.root/'selection.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):self.selection={}
        try:self.model_preferences=json.loads((self.root/'model_preferences.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):self.model_preferences={'glm53_enabled':True}

    def event(self,phase,**fields):
        with self.lock:
            self.sequence+=1
            self.events.append({'sequence':self.sequence,'time':time.time(),'phase':phase,**fields})
            self.events=self.events[-200:]

    def snapshot(self):
        with self.lock:
            progress={}
            for p in (self.root/'jobs').glob('*/*.json'):
                if p.name.endswith('.answer.json'):continue
                try:r=json.loads(p.read_text(encoding='utf-8'))
                except (OSError,ValueError):continue
                v=progress.setdefault(r.get('run_id','standalone'),{'admitted':0,'returned':0})
                v['admitted']+=1;v['returned']+=int(r.get('status')=='returned')
            return {'enabled':enabled(),**self.state,'edition':'cn','product_label':'ZCode 国内版',
                'connecting':self.control_busy,'suspended':self.suspended,'executing':self.execution_lock.locked(),
                'selected_model':self.selection.get('model',''),'events':list(self.events),
                'glm53_enabled':self.model_preferences.get('glm53_enabled',True),
                'run_progress':progress,'max_jobs_per_run':8,'max_recognition_jobs_per_run':48,'full_word_ppt_verified':False}

    def _check(self):
        with Client(self.account) as client:
            row=client.inspect()
            if row['models'] and hasattr(client,'ensure_dispatchable'):client.ensure_dispatchable()
        models=[]
        for model in row['models']:
            if model['disabled_reason']:continue
            if model['ref'].get('modelId')=='GLM-5.3' and not self.model_preferences.get('glm53_enabled',True):continue
            ref=model['ref'];model['id']=ref['providerId']+'/'+ref['modelId'];models.append(model)
        with self.lock:
            if self.suspended:return
            # Official login can reset its public default. Restore only the
            # enabled model the user already selected in this product.
            if any(m['id']==self.selection.get('model') for m in models):
                self._ensure_selected_model()
            self.state.update(models=models,login_url='',checked_at=time.time(),installation=identity(),
                official_login_completed_at=row.get('official_login_completed_at'),
                reason_code=None if models else 'ZCODE_LOGIN_REQUIRED',next_action='none' if models else 'login',
                login_stage='connected' if models else 'not_connected',login_expires_at=None,
                status='ready' if models else 'not_connected',
                message='国内账户已授权，独立CLI检查通过；当前显示官方保存的模型配置，额度和真实生成待验证。' if models else '尚未取得国内账户的有效登录记录，请完成官方登录。')
        self.event('connection_checked',model_count=len(models),model_requests=0)

    def _ensure_selected_model(self):
        desired=self.selection.get('ref')
        if not desired:return
        previous=configured_selection(self.account)
        if previous!=desired:
            set_configured_selection(self.account,desired)
            write_json(self.root/'last_model_sync.json',{'time':time.time(),'previous_model':(previous or {}).get('modelId'),
                'selected_model':desired['modelId'],'model_requests':0})
            self.event('saved_model_restored',model=desired['modelId'],model_requests=0)

    def start_connect(self,login=False):
        if not enabled():raise ProviderError('ZCode连接未启用。',error_code='ZCODE_DISABLED')
        with self.lock:
            if self.control_busy or self.execution_lock.locked():return self.snapshot()
            self.control_busy=True;self.suspended=False
            self.state.update(status='checking',message='正在检查ZCode国内账户…',login_url='',
                reason_code=None,next_action='wait',login_stage='checking',login_expires_at=None)
        threading.Thread(target=self._connect,args=(login,),daemon=True).start()
        return self.snapshot()

    def _connect(self,login):
        try:
            try:self._check()
            except ProtocolError as exc:
                if not login or getattr(exc,'code',None)!='ZCODE_LOGIN_REQUIRED':raise
            if not login:return
            # A browser click on login is an explicit reauthorization request,
            # including when the saved account still appears connected.
            with self.lock:self.state.update(status='authorizing',message='正在准备官方登录页，请稍候…',
                login_stage='preparing',next_action='wait',login_expires_at=time.time()+300)
            process=spawn(self.account,['login','bigmodel','--no-browser'])
            self.process=process
            notes=[]
            def consume(stream):
                for line in iter(lambda:stream.readline(65536),''):
                    if len(''.join(notes))<2000:notes.append(line[:500])
                    for url in re.findall(r'https://[^\s<>"\x1b]+',line):
                        url=url.rstrip("',);\\")
                        if trusted_login_url(url):
                            with self.lock:
                                self.state.update(status='authorizing',login_url=url,login_stage='awaiting_authorization',
                                    next_action='open_login',message='已打开智谱官方登录页。完成授权后返回本页，连接会自动检查。')
                            self.event('awaiting_official_login',provider='bigmodel')
                            open_login_page(url)
            readers=[threading.Thread(target=consume,args=(s,),daemon=True) for s in (process.stdout,process.stderr)]
            for t in readers:t.start()
            try:code=process.wait(timeout=315)
            except subprocess.TimeoutExpired:process.kill();process.wait();raise ProtocolError('官方登录等待已超时，请点击“网页登录 / 重新授权”重试。',code='ZCODE_LOGIN_TIMEOUT')
            for t in readers:t.join(timeout=2)
            if self.suspended:return
            if code:
                detail=''.join(notes)
                if 'credential storage is unavailable' in detail.lower():
                    raise ProtocolError('官方登录无法写入本机凭据，登录页尚未打开。请重新点击“网页登录 / 重新授权”。',code='ZCODE_LOGIN_INCOMPLETE')
                raise ProtocolError('官方授权未完成，请点击“网页登录 / 重新授权”重试，并确认使用智谱国内账户。',code='ZCODE_LOGIN_INCOMPLETE')
            with self.lock:self.state['official_login_completed_at']=time.time()
            write_json(self.account/'mmf_official_login_receipt.json',{'provider':'bigmodel',
                'completed_at':self.state['official_login_completed_at'],'cli_sha256':identity()['cli_sha256'],
                'evidence':'official_login_process_exit_0'})
            self.event('official_login_completed',provider='bigmodel')
            self._check()
        except Exception as e:
            with self.lock:
                if self.suspended:return
                code=getattr(e,'code',None) or 'ZCODE_CONNECTION_FAILED'
                self.state.update(status='error',login_url='',models=[],reason_code=code,
                    next_action='update' if code=='ZCODE_VERSION_UNREVIEWED' else 'login' if code=='ZCODE_LOGIN_REQUIRED' else 'retry',
                    login_stage='failed',login_expires_at=None,
                    message=str(e) if isinstance(e,ProtocolError) else '连接检查未完成，请先点击“检查连接”；仍失败时可在本页重新授权。')
            self.event('connection_failed')
        finally:
            with self.lock:self.control_busy=False;self.process=None

    def select_model(self,model):
        with self.lock:
            if self.control_busy or self.execution_lock.locked():raise ProviderError('请等待当前操作结束。',error_code='ZCODE_BUSY')
            selected=next((m for m in self.state['models'] if m['id']==model),None)
            if self.state['status']!='ready' or not selected:raise ProviderError('请先连接并选择官方目录中的国内账户模型。',error_code='ZCODE_MODEL_UNAVAILABLE')
            set_configured_selection(self.account,selected['ref'])
            self.selection={'model':model,'ref':selected['ref']};write_json(self.root/'selection.json',self.selection)
        self.event('model_selected',model=selected['ref']['modelId'])
        return self.snapshot()

    def disconnect(self):
        with self.lock:
            if self.execution_lock.locked():raise ProviderError('请先停止当前生成任务。',error_code='ZCODE_BUSY')
            self.suspended=True
            if self.process and self.process.poll() is None:self.process.terminate()
            self.state.update(status='not_connected',models=[],login_url='',reason_code='ZCODE_CONNECTION_DISABLED',
                next_action='check',login_stage='not_connected',login_expires_at=None,
                message='已取消登录或停用本产品连接。原模型选择保留，可点击“检查连接”恢复。')
        return self.snapshot()

    def set_model_preferences(self, glm53_enabled):
        if not isinstance(glm53_enabled,bool):raise ProviderError('启用选项必须为是或否。',error_code='ZCODE_INVALID_MODEL_PREFERENCE')
        with self.lock:
            if self.control_busy or self.execution_lock.locked():raise ProviderError('请等待当前操作结束。',error_code='ZCODE_BUSY')
            if not glm53_enabled and self.selection.get('model','').endswith('/GLM-5.3'):
                raise ProviderError('请先选择GLM-5.3-Flash，再停用GLM-5.3。',error_code='ZCODE_MODEL_IN_USE')
            self.model_preferences={'glm53_enabled':glm53_enabled}
            write_json(self.root/'model_preferences.json',self.model_preferences)
        return self.start_connect(login=False)

    def health(self):
        with self.lock:
            model=self.selection.get('model','')
            ready=enabled() and self.state['status']=='ready' and not self.suspended and not self.control_busy and any(m['id']==model for m in self.state['models'])
            return {'configured':bool(model),'available':ready,'status':'ready' if ready else 'not_ready',
                'message':self.state['message'],'verification_scope':self.state['verification_scope']}

    def execute(self,request,task_dir,cancelled=lambda:False):
        from providers.openai_compatible import GenerationCancelled
        if not self.health()['available']:raise ProviderError('请先连接ZCode国内账户并选择模型。',error_code='ZCODE_NOT_READY')
        rid=str(request.get('run_id') or 'standalone');tid=str(request.get('task_id') or '')
        queued_at=time.monotonic();queued=False
        while not self.execution_lock.acquire(timeout=.2):
            if not queued:self.event('queued',run_id=rid,task_id=tid);queued=True
            if cancelled():raise GenerationCancelled('已停止等待ZCode。')
            if request.get('generation_deadline_epoch') and time.time()>=float(request['generation_deadline_epoch']):
                raise ProviderError('本轮总时间已到，排队中的内容尚未发送。',error_code='ZCODE_NOT_DISPATCHED')
        try:
            with self.lock:
                if self.control_busy:raise ProviderError('连接检查正在进行，本次尚未发送。',error_code='ZCODE_NOT_DISPATCHED')
            if queued:self.event('dispatch_ready',run_id=rid,task_id=tid,queue_seconds=round(time.monotonic()-queued_at,2))
            digest=hashlib.sha256(json.dumps({'request':request,'model':self.selection},ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
            jobs=self.root/'jobs'/hashlib.sha256(rid.encode()).hexdigest()[:24];jobs.mkdir(parents=True,exist_ok=True)
            slot=str(request.get('request_id') or request.get('job_id') or tid or Path(task_dir).resolve())
            key=hashlib.sha256(slot.encode()).hexdigest()[:24]
            record=jobs/(key+'.json');answer=jobs/(key+'.answer.json')
            if request.get('generation_mode')=='ppt_slide' and request.get('slide_id'):
                sid=str(request['slide_id'])
                for p in jobs.glob('*.json'):
                    if p.name.endswith('.answer.json'):continue
                    prior=json.loads(p.read_text(encoding='utf-8'))
                    same=prior.get('slide_id')==sid or f'-{sid}-A' in str(prior.get('task_id',''))
                    if same and prior.get('status') not in ('not_dispatched','returned'):
                        raise ProviderError('此页已有结果未知的ZCode调用，未作为新尝试重发；原记录和已完成页保留。',error_code='ZCODE_NO_AUTOMATIC_RESEND')
            if request.get('generation_mode')=='tender_requirement_recognition' and '-segment-' in tid:
                parent=tid.rsplit('-segment-',1)[0]
                for p in jobs.glob('*.json'):
                    if p.name.endswith('.answer.json'):continue
                    prior=json.loads(p.read_text(encoding='utf-8'))
                    if prior.get('task_id')==parent and prior.get('status') not in ('not_dispatched','returned'):
                        raise ProviderError('原父块请求结果未知，未拆成新请求重发；请保留原任务并核对。',error_code='ZCODE_NO_AUTOMATIC_RESEND')
            model_lock=jobs/'.model'
            if model_lock.exists() and model_lock.read_text(encoding='utf-8')!=self.selection['model']:
                original=model_lock.read_text(encoding='utf-8').rsplit('/',1)[-1]
                raise ProviderError(f'此任务已绑定{original}；切换模型后请新建识别任务，原记录保留且不自动重发。',error_code='ZCODE_MODEL_LOCKED')
            if record.exists():
                previous=json.loads(record.read_text(encoding='utf-8'))
                if previous.get('digest')!=digest:raise ProviderError('请求身份与原内容不一致，未重新派发。',error_code='REQUEST_IDENTITY_MISMATCH')
                if previous.get('status')=='returned' and answer.exists():return json.loads(answer.read_text(encoding='utf-8'))
                if previous.get('status')!='not_dispatched':
                    raise ProviderError('本次请求已有记录且结果不明，未自动重发；已返回内容保留。',error_code='ZCODE_NO_AUTOMATIC_RESEND')
            mode=request.get('generation_mode')
            scope='recognition' if mode=='tender_requirement_recognition' else 'recommendation' if mode=='knowledge_recommendation' else 'generation'
            existing=[json.loads(p.read_text(encoding='utf-8')) for p in jobs.glob('*.json') if not p.name.endswith('.answer.json')]
            def counts_for(r):
                if r.get('status')=='not_dispatched':return False
                if r.get('budget_scope')=='recommendation' or r.get('generation_mode')=='knowledge_recommendation' or str(r.get('task_id') or '').endswith('-recommend'):
                    return scope=='recommendation'
                return r.get('budget_scope') in (None,scope)
            used=sum(counts_for(r) for r in existing)
            from .policy import generation_call_cap
            maximum=2 if scope=='recommendation' else 48 if mode=='tender_requirement_recognition' else generation_call_cap(jobs)
            if scope=='generation' and not (jobs/'.ppt_policy').exists():
                from account_budget import word_cap
                maximum=word_cap(jobs,rid,self.selection['model'])
            if used>=maximum:raise ProviderError(f'本轮ZCode已达到{maximum}次请求上限，已有内容保留。',error_code='ZCODE_RUN_BUDGET')
            if cancelled():raise GenerationCancelled('任务已取消。')
            budget=240
            if request.get('generation_mode')=='tender_requirement_recognition':
                budget=min(600,max(1,float(request.get('timeout_seconds') or 600)))
            if request.get('generation_mode')=='ppt_slide' and request.get('response_budget_seconds'):
                remaining=min(budget,float(request['response_budget_seconds']),float(request['generation_deadline_epoch'])-time.time())
            else:remaining=min(budget,float(request.get('response_deadline_epoch') or time.time()+budget)-time.time())
            if remaining<=0:raise ProviderError('本轮时间预算已到，未发送ZCode请求。',error_code='PPT_TIME_BUDGET')
            try:
                self._ensure_selected_model()
                verified_installation()
            except Exception as exc:
                self.event('not_dispatched',run_id=rid,task_id=tid)
                if getattr(exc,'code',None)=='ZCODE_VERSION_UNREVIEWED':
                    raise ProviderError(str(exc),error_code='ZCODE_NOT_DISPATCHED') from exc
                raise ProviderError('启动前未能恢复网页中选择的模型，本次尚未发送。请检查连接后重试。',error_code='ZCODE_NOT_DISPATCHED') from exc
            Path(task_dir).mkdir(parents=True,exist_ok=True)
            if not model_lock.exists():model_lock.write_text(self.selection['model'],encoding='utf-8')
            prior=json.loads(record.read_text(encoding='utf-8')) if record.is_file() else {}
            history=list(prior.get('pre_send_history') or [])
            if prior.get('status')=='not_dispatched':history.append({k:prior.get(k) for k in ('status','error_code','error','time')})
            base={'run_id':rid,'task_id':tid,'digest':digest,'pre_send_history':history,'budget_scope':scope}
            if request.get('generation_mode')=='ppt_slide':base['slide_id']=request.get('slide_id')
            write_json(record,{**base,'status':'admitted','time':time.time()})
            self.event('submitted',run_id=rid,task_id=tid)
            prompt=str(request.get('system_prompt') or '')+'\n\n'+str(request.get('prompt') or json.dumps(request,ensure_ascii=False,default=str))
            try:
                def report_progress(row):
                    write_json(Path(task_dir)/'zcode_stream_progress.json',row)
                    self.event('working',run_id=rid,task_id=tid,**row)
                with Client(self.account) as client:
                    self.event('working',run_id=rid,task_id=tid)
                    result=client.call('workspace/generateText',{'workspace':client.workspace,'selection':self.selection['ref'],
                        'prompt':prompt,'tools':[],'querySource':'mmf-zcode-account','maxOutputTokens':min(16384,max(1,int(request.get('max_tokens') or 16384))),
                        'reasoningEffort':request.get('reasoning_effort'),
                        'operationId':digest,**({'nativeSessionId':request['native_session_id']} if request.get('native_session_id') else {})},timeout=remaining,cancelled=cancelled,progress=report_progress)
                if isinstance(result,dict) and result.get('tool_event_count'):
                    raise ProtocolError('ZCode返回了计划或工具调用，未作为正文接收；原记录和已完成子块保留。',code='ZCODE_UNEXPECTED_TOOL_RESULT')
                if not isinstance(result,dict) or not isinstance(result.get('text'),str) or not result['text'].strip():raise ProtocolError('ZCode已结束本次调用，但未返回正文；已完成子块保留，请查看本页停止原因。',code='ZCODE_EMPTY_RESPONSE')
                if result.get('toolCalls'):raise ProtocolError('官方接口返回了未允许的工具调用。')
                envelope={'body':result['text'],'requested_model':self.selection['ref']['modelId'],
                    'observed_selection':result.get('selection'),'usage':result.get('usage'),
                    'finish_reason':result.get('finishReason'),'cost_availability':'NOT_AVAILABLE','digest':digest,
                    'session_id':result.get('session_id'),'transport':result.get('transport'),
                    'observed_model_availability':result.get('observed_model_availability','available' if result.get('selection') else 'NOT_AVAILABLE'),
                    'requested_reasoning_effort':result.get('requested_reasoning_effort'),
                    'requested_max_output_tokens':result.get('requested_max_output_tokens')}
                write_json(Path(task_dir)/'zcode_raw_response.json',envelope)
                if result.get('process_exit_code',0) or result.get('status') in ('error','failed','interrupted','cancelled'):
                    raise ProtocolError('ZCode返回了未完成状态，原答已保留。')
                if result.get('selection') and any(result['selection'].get(k)!=self.selection['ref'].get(k) for k in ('providerId','modelId')):
                    raise ProtocolError('官方回传的模型标识与所选国内模型不一致，原答已保留。')
                write_json(answer,envelope);write_json(record,{**base,'status':'returned','time':time.time()})
                self.event('returned',run_id=rid,task_id=tid,text_characters=len(result['text']))
                return envelope
            except Exception as e:
                final=getattr(locals().get('client'),'last_result',None)
                if isinstance(final,dict) and isinstance(final.get('text'),str):
                    write_json(Path(task_dir)/'zcode_terminal_response_preserved.json',final)
                # A bundle update can race the preflight. Client.call checks the
                # version before spawn_prompt, so this rejection is also unsent.
                native=getattr(e,'code',None)
                not_sent=native in {'ZCODE_NOT_DISPATCHED','ZCODE_VERSION_UNREVIEWED','ZCODE_LOGIN_REQUIRED'}
                write_json(record,{**base,'status':'not_dispatched' if not_sent else 'unknown_or_failed',
                    'time':time.time(),'error_code':native,'error':str(e) if isinstance(e,ProtocolError) else 'request incomplete'})
                self.event('not_dispatched' if not_sent else 'failed_or_unknown',run_id=rid,task_id=tid)
                if cancelled():raise GenerationCancelled('已停止本轮ZCode生成，已有内容保留；在途状态需核对。') from e
                code=native if native=='ZCODE_LOGIN_REQUIRED' else 'ZCODE_NOT_DISPATCHED' if not_sent else 'ZCODE_RESPONSE_UNKNOWN' if native=='ZCODE_RESPONSE_UNKNOWN' else 'ZCODE_REQUEST_INCOMPLETE'
                raise ProviderError(str(e) if isinstance(e,ProtocolError) else 'ZCode本次请求未完成，已有内容保留；未自动重发。',error_code=code) from e
        finally:self.execution_lock.release()

    def prepare_ppt_run(self,run_id,body_pages,call_cap):
        from .policy import prepare_ppt_policy
        if not isinstance(run_id,str) or not run_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in run_id):
            raise ProviderError('PPT任务编号无效，未发送请求。',error_code='ZCODE_PLAN_INVALID')
        jobs=self.root/'jobs'/hashlib.sha256(run_id.encode()).hexdigest()[:24]
        with self.lock:return prepare_ppt_policy(jobs,run_id,self.selection.get('model',''),body_pages,call_cap)


_instances={};_guard=threading.Lock()
def connection():
    from paths import current
    root=current().runtime_dir/'zcode_cn_account'
    with _guard:
        if str(root) not in _instances:_instances[str(root)]=Connection(root)
        return _instances[str(root)]
