"""Official standalone CLI owns authentication. Desktop app-server requires a host overlay."""
from __future__ import annotations
import json
import os
import queue
import subprocess
import threading
import time
import tempfile
from contextlib import contextmanager
from .protocol import ProtocolError,identity,installation,spawn,spawn_prompt

APPROVED_CLI_SHA='97490d0f06a7a225d54d47817acf4b4859cdb268b59f2490caaeda982bef063a'
# Both installed builds have had their bare /goal read-only branch reviewed.
REVIEWED_CLI_VERSIONS={APPROVED_CLI_SHA:'0.17.0',
    '395307aba9b59ba6a099f4941b073ec7a8b53fe4154e4e75af65b3703d2e6d36':'0.17.1',
    # Desktop 3.17.2.9661: CLI still reports 0.17.1, with a rebuilt bundle.
    'd9afd58d694f98a5279bf548cdc7646413c125762e86730605a6071f9e0346d6':'0.17.1',
    # Desktop post-2026-11-12 update: CLI still reports 0.17.1, with a rebuilt bundle.
    '26785f28ab8fb9f8b0f3821f2bf71d294f4d0b8bf28108785292f86c7e14090d':'0.17.1'}
DOMESTIC_PROVIDER='account:bigmodel-individual-coding-plan'
DENIED_TOOLS='Bash Read Write Edit MultiEdit Glob Grep LS WebFetch WebSearch Task Agent TodoWrite TodoRead AskUserQuestion ToolSearch NotebookEdit BrowserUse CreateWorkflow RunWorkflow CreateOffPeakTask EnterPlanMode ExitPlanMode TaskOutput TaskStop ReadSessionContext'.split()


def verified_installation():
    row=identity()
    if row['cli_sha256'] not in REVIEWED_CLI_VERSIONS:
        raise ProtocolError('本机ZCode版本尚未适配，请更新本产品后重新检查连接；重复登录无法修复版本问题。',code='ZCODE_VERSION_UNREVIEWED')
    row['cli_version']=REVIEWED_CLI_VERSIONS[row['cli_sha256']]
    return row


def configured_selection(account):
    # This is only the new experiment's token-free public selection file, written
    # by official login. Never inspect credentials.json or the desktop user's home.
    path=account/'data/.zcode/v2/provider_config.json'
    try:selection=json.loads(path.read_text(encoding='utf-8'))['config']['defaultModelSelection']
    except (OSError,ValueError,KeyError,TypeError):return None
    if not isinstance(selection,dict) or selection.get('providerId')!=DOMESTIC_PROVIDER:return None
    _,_,builtin=installation()
    rules=json.loads(builtin.read_text(encoding='utf-8'))['config']['providerConfigRules']['providerRules']
    provider=next((r.get('config',{}) for r in rules if r.get('providerId')==DOMESTIC_PROVIDER),{})
    if selection.get('modelId') not in provider.get('builtinModelIds',[]):return None
    return {k:selection[k] for k in ('providerId','modelId','options') if k in selection}


def domestic_model_ids():
    _,_,builtin=installation()
    rules=json.loads(builtin.read_text(encoding='utf-8'))['config']['providerConfigRules']['providerRules']
    provider=next((r.get('config',{}) for r in rules if r.get('providerId')==DOMESTIC_PROVIDER),{})
    return [m for m in provider.get('builtinModelIds',[]) if isinstance(m,str)]


def set_configured_selection(account, selection):
    """Change only this product's token-free public default, never desktop credentials."""
    verified_installation()
    if selection.get('providerId')!=DOMESTIC_PROVIDER or selection.get('modelId') not in domestic_model_ids():
        raise ProtocolError('该模型不在官方国内账户内置目录中。')
    path=account/'data/.zcode/v2/provider_config.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    data['config']['defaultModelSelection']={k:selection[k] for k in ('providerId','modelId','options') if k in selection}
    pending=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,suffix='.tmp',delete=False) as f:
            pending=f.name;json.dump(data,f,ensure_ascii=False,indent=2)
        os.replace(pending,path);pending=None
    finally:
        if pending:os.unlink(pending)
    if configured_selection(account)!=data['config']['defaultModelSelection']:
        raise ProtocolError('模型选择未保存成功，请重新检查。')


def _atomic_config(path,data):
    pending=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,suffix='.tmp',delete=False) as f:
            pending=f.name;json.dump(data,f,ensure_ascii=False,indent=2)
        os.replace(pending,path);pending=None
    finally:
        if pending:os.unlink(pending)


@contextmanager
def request_options(account,params):
    """The standalone CLI has no per-prompt options flag; use its owned public layer.

    Its registry defaults to the *last* reasoning level (max for GLM-5.3).
    Restore the original public file byte-for-byte when our temporary layer is intact.
    Never touch credentials or the user's desktop profile.
    """
    selection=dict(params['selection']);maximum=params.get('maxOutputTokens')
    effort=params.get('reasoningEffort')
    explicit=effort in ('low','high','max') and selection['modelId'] in ('GLM-5.3','GLM-5.3-Flash')
    if maximum is None and not explicit:
        yield;return
    path=account/'data/.zcode/v2/provider_config.json';original=path.read_bytes()
    data=json.loads(original);config=data['config']
    if explicit:selection={**selection,'options':{**selection.get('options',{}),'reasoningLevel':effort}}
    config['defaultModelSelection']=selection
    if maximum is not None:
        maximum=min(16384,max(1,int(maximum)))
        rules=config.setdefault('modelConfigRules',{'providerModelRules':[],'manualProviderModelRules':[]})
        # Preserve all existing public model overrides; only overlay the selected pair.
        key={'providerId':selection['providerId'],'modelId':selection['modelId']}
        if any(all(r.get(k)==v for k,v in key.items()) for r in rules.get('manualProviderModelRules',[])):
            raise ProtocolError('所选账户模型存在手动规则，无法安全应用请求预算；本次尚未发送。',code='ZCODE_NOT_DISPATCHED')
        rows=rules.setdefault('providerModelRules',[])
        matched=[r for r in rows if all(r.get(k)==v for k,v in key.items())]
        if len(matched)>1:raise ProtocolError('所选模型存在重复规则，本次尚未发送。',code='ZCODE_NOT_DISPATCHED')
        rule=matched[0] if matched else {**key,'config':{}}
        if not matched:rows.append(rule)
        spec=rule.setdefault('config',{}).setdefault('optionSpecs',{}).setdefault('maxOutputTokens',{})
        spec['max']=min(maximum,int(spec.get('max') or maximum))
    _atomic_config(path,data)
    try:yield
    finally:
        if json.loads(path.read_bytes())!=data:
            raise ProtocolError('执行中公开配置被外部修改，未覆盖外部更改；请检查连接。')
        # Atomic restoration also preserves original formatting and unrelated fields.
        with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.tmp',delete=False) as f:
            f.write(original);pending=f.name
        os.replace(pending,path)


class Client:
    def __init__(self,account):
        self.account=account;self.workspace={};self.process=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=4)

    def inspect(self):
        verified_installation()
        try:receipt=json.loads((self.account/'mmf_official_login_receipt.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):receipt={}
        selection=configured_selection(self.account)
        if receipt.get('provider')!='bigmodel' or receipt.get('cli_sha256') not in REVIEWED_CLI_VERSIONS or not selection:
            return {'models':[],'model_requests':0,'model_catalog_availability':'NOT_AVAILABLE'}
        # Official command-center routes bare /goal to readTarget only. It does not
        # submitPrompt, set a goal, generate a title, or invoke an inference provider.
        self.process=spawn(self.account,['--prompt','/goal','--mode','plan','--json','--workflow-mode','disabled'])
        try:out,_=self.process.communicate(timeout=40)
        except subprocess.TimeoutExpired:self.close();raise ProtocolError('独立CLI状态检查超时，未发送模型请求。')
        if self.process.returncode or len(out)>1024*1024:raise ProtocolError('独立CLI状态检查未完成。')
        try:row=json.loads(out)
        except ValueError:raise ProtocolError('独立CLI状态响应无法识别。')
        if not isinstance(row,dict) or set(row)-{'sessionId','traceId','response'} or not isinstance(row.get('response'),str):
            raise ProtocolError('只读控制命令返回了意外响应；未把它当作连接成功。')
        return {'models':[{'ref':selection if m==selection['modelId'] else {'providerId':DOMESTIC_PROVIDER,'modelId':m},
            'label':m,'provider_label':'智谱国内账户（官方内置模型）',
            'disabled_reason':'','availability_source':'official_builtin_configuration_not_remote_quota'} for m in domestic_model_ids()],
            'current':selection,'model_requests':0,'model_catalog_availability':'NOT_AVAILABLE',
            'official_login_completed_at':receipt.get('completed_at')}

    def ensure_dispatchable(self):
        """The new CLI admits a model only after its own account entitlement check.

        /goal never selects a model, so a green connection check can still exit
        before any prompt is sent. /model is handled before inference.
        """
        verified_installation()
        self.process=spawn(self.account,['--prompt','/model','--mode','plan','--json','--workflow-mode','disabled','--toolsearch-mode','off'])
        try:out,err=self.process.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            self.close();raise ProtocolError('国内账户模型确认超时，未发送生成请求。')
        if 'Select a model before sending a message' in (err or ''):
            raise ProtocolError('本机新版 ZCode 还不能使用已保存的国内账户，本次尚未发送。请点击「网页登录 / 重新授权」，完成后再检查连接。',code='ZCODE_LOGIN_REQUIRED')
        if self.process.returncode or not out.strip():
            raise ProtocolError('独立CLI尚未确认国内账户模型，未发送生成请求。')

    def call(self,method,params,timeout=240,cancelled=lambda:False,progress=None):
        if method!='workspace/generateText':raise ProtocolError('独立CLI不支持此操作。')
        verified_installation()
        if configured_selection(self.account)!=params.get('selection'):
            raise ProtocolError('启动前模型配置发生变化，本次尚未发送。请重新检查连接后重试。',code='ZCODE_NOT_DISPATCHED')
        with request_options(self.account,params):
            try:
                self.last_result=self._generate(params,timeout,cancelled,progress)
                return self.last_result
            finally:self.close()

    def _generate(self,params,timeout,cancelled,progress):
        prompt=params.get('prompt')
        if not isinstance(prompt,str) or not prompt.strip():raise ProtocolError('任务正文为空。')
        args=['--prompt',prompt,'--mode','plan','--output-format','stream-json',
            '--workflow-mode','disabled','--toolsearch-mode','off','--disallowed-tools',*DENIED_TOOLS]
        if params.get('nativeSessionId'):
            import re
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',params['nativeSessionId']):raise ProtocolError('会话编号无效，未派发。',code='ZCODE_NOT_DISPATCHED')
            args.extend(['--resume',params['nativeSessionId']])
        self.process=spawn_prompt(self.account,args)
        frames=queue.Queue(maxsize=1024);failed=[];stderr_text=[];activity={'events_received':0,'terminal_received':False,
            'reasoning_events':0,'text_events':0,'turns_completed':0,'tool_events':0,'tool_names':[],'session_id':None}
        def read():
            try:
                while True:
                    line=self.process.stdout.readline(4*1024*1024+1)
                    if not line:break
                    if len(line)>4*1024*1024 or not line.endswith('\n'):raise ValueError()
                    frame=json.loads(line)
                    if not isinstance(frame,dict):raise ValueError()
                    activity['events_received']+=1
                    if isinstance(frame.get('sessionId'),str):activity['session_id']=frame['sessionId']
                    kind=(frame.get('payload') or {}).get('kind') if isinstance(frame.get('payload'),dict) else None
                    if kind=='reasoning_delta':activity['reasoning_events']+=1
                    if kind=='text_delta':activity['text_events']+=1
                    if kind=='tool_call':
                        activity['tool_events']+=1
                        name=(frame.get('payload') or {}).get('toolName')
                        if isinstance(name,str) and name in DENIED_TOOLS and name not in activity['tool_names']:activity['tool_names'].append(name)
                    if frame.get('type')=='turn.completed':activity['turns_completed']+=1
                    # No thinking, tool payloads or private runtime events enter logs.
                    if frame.get('type')=='result':activity['terminal_received']=True;frames.put_nowait(frame)
            except Exception:failed.append(True)
        def drain():
            while True:
                chunk=self.process.stderr.read(4096)
                if not chunk:break
                if sum(len(part) for part in stderr_text)<4000:stderr_text.append(chunk[:4000])
        threading.Thread(target=read,daemon=True).start();stderr_thread=threading.Thread(target=drain,daemon=True);stderr_thread.start()
        started=time.monotonic();end=started+timeout;terminal=None;last_progress=-10
        while time.monotonic()<end:
            elapsed=int(time.monotonic()-started)
            if progress and elapsed-last_progress>=5:
                progress({**activity,'elapsed_seconds':elapsed,'timeout_seconds':timeout});last_progress=elapsed
            if cancelled():self.close();raise ProtocolError('任务已取消，未自动重发。')
            if failed:raise ProtocolError('官方CLI输出格式异常，未自动重发。')
            try:terminal=frames.get(timeout=.2);break
            except queue.Empty:
                if self.process.poll() is not None:
                    try:terminal=frames.get(timeout=.5)
                    except queue.Empty:
                        stderr_thread.join(1)
                        if 'Select a model before sending a message' in ''.join(stderr_text):
                            raise ProtocolError('本机新版 ZCode 还不能使用已保存的国内账户，本次尚未发送。请点击「网页登录 / 重新授权」，完成后再继续。',code='ZCODE_LOGIN_REQUIRED')
                        raise ProtocolError('官方CLI未返回终态，未自动重发。')
                    break
        if terminal is None:raise ProtocolError(f'ZCode等待{int(timeout)}秒仍未返回完整结果，原请求状态需核对；未自动重发。',code='ZCODE_RESPONSE_UNKNOWN')
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:raise ProtocolError('ZCode尚未结束，原请求状态需核对。')
        if progress:progress({**activity,'elapsed_seconds':int(time.monotonic()-started),'timeout_seconds':timeout})
        return {'text':terminal.get('response'),'selection':None,'usage':terminal.get('usage'),
            'tool_event_count':activity['tool_events'],'tool_names':list(activity['tool_names']),
            'session_id':terminal.get('sessionId'),'status':(terminal.get('projection') or {}).get('status'),
            'process_exit_code':self.process.returncode,'transport':'official_standalone_cli',
            'observed_model_availability':'NOT_AVAILABLE',
            'requested_reasoning_effort':params.get('reasoningEffort'),
            'requested_max_output_tokens':params.get('maxOutputTokens')}
