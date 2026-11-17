from __future__ import annotations
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path


class ProtocolError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def installation():
    cli = Path(os.environ.get('MMF_ZCODE_CLI') or r'C:\Program Files\ZCode\resources\glm\zcode.cjs').resolve()
    builtin = cli.parents[1] / 'config/provider/zcode-builtin.json'
    node = os.environ.get('MMF_ZCODE_NODE') or shutil.which('node')
    if not cli.is_file() or not builtin.is_file() or not node or not Path(node).is_file():
        raise ProtocolError('未找到完整的官方ZCode安装或Node运行环境。')
    return str(node), cli, builtin


def credential_secret_file(account: Path) -> Path:
    """Local key for the official CLI's encrypted store. Never the desktop credential vault."""
    path = account/'secure'/'credential-secret'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and 32 <= path.stat().st_size <= 4096:
        return path
    path.write_text(secrets.token_hex(32), encoding='utf-8')
    return path


def environment(account: Path):
    account = account.resolve()
    for part in ('home', 'data', 'temp', 'workspace', 'roaming', 'local'):
        (account / part).mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('ZCODE_', 'ZAI_', 'BIGMODEL_', 'QODER_', 'QODERCN_')) or key == 'NODE_OPTIONS' or re.search(r'(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)$', key, re.I):
            env.pop(key, None)
    _, _, builtin = installation()
    env.update(HOME=str(account/'home'), USERPROFILE=str(account/'home'),
        ZCODE_DATA_BASE_DIR=str(account/'data'), ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=str(builtin),
        ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=str(account/'data/.zcode/v2/provider_config.json'),
        ZCODE_CREDENTIAL_SECRET_FILE=str(credential_secret_file(account)),
        APPDATA=str(account/'roaming'), LOCALAPPDATA=str(account/'local'),
        TEMP=str(account/'temp'), TMP=str(account/'temp'), NO_COLOR='1')
    config = account/'home/.zcode/cli/config.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({'features':dict.fromkeys(('mcp','subagent','memory','skill'),False),
        'memory':{'use':False},'plugins':{'enabled':False},'skills':{'enabled':False},
        'hooks':{'enabled':False},'ui':{'locale':'zh-CN'}}), encoding='utf-8')
    return env


def spawn(account: Path, args):
    node, cli, _ = installation()
    env = environment(account)
    return subprocess.Popen([node,str(cli),*args,'--cwd',str(account/'workspace')],
        cwd=account/'workspace',env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def spawn_prompt(account: Path, args):
    """Supply the same official CLI args over stdin before vendor code starts."""
    node,cli,_=installation();env=environment(account)
    payload=json.dumps([*args,'--cwd',str(account/'workspace')],ensure_ascii=False)
    if len(payload.encode('utf-8'))>8*1024*1024:raise ProtocolError('本次请求超过连接传输上限，未截断或发送。')
    process=subprocess.Popen([node,str(Path(__file__).with_name('stdin_cli.cjs')),str(cli)],
        cwd=account/'workspace',env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        process.stdin.write(payload);process.stdin.close()
    except Exception:
        process.kill();process.wait();raise
    return process


class Client:
    """Bounded official NDJSON transport. No agent prompts, shell tools or credential reads."""
    def __init__(self, account: Path):
        self.account = account
        self.process = spawn(account, ['app-server'])
        self.queue = queue.Queue(maxsize=2048)
        self.failed = False
        self.sequence = 0
        self.workspace = {'workspacePath':str((account/'workspace').resolve()),'workspaceKey':'mmf_zcode_cn'}
        threading.Thread(target=self._read,daemon=True).start()
        threading.Thread(target=self._drain_errors,daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(4*1024*1024+1)
                if not line:break
                if len(line)>4*1024*1024 or not line.endswith('\n'):raise ProtocolError('协议响应过大。')
                row = json.loads(line)
                if not isinstance(row,dict):raise ProtocolError('协议响应无效。')
                self.queue.put_nowait(row)
        except Exception:
            self.failed = True

    def _drain_errors(self):
        # Official diagnostic output can contain private account detail; never persist it.
        while self.process.stderr.read(4096):pass

    def send(self, row):
        payload=json.dumps(row,ensure_ascii=False)
        if len(payload)>4*1024*1024:raise ProtocolError('请求超过连接大小限制。')
        self.process.stdin.write(payload+'\n');self.process.stdin.flush()

    def call(self, method, params, timeout=45, cancelled=lambda:False):
        self.sequence+=1;rid='mmf-'+str(self.sequence)
        self.send({'id':rid,'method':method,'params':params})
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if cancelled():raise ProtocolError('任务已取消；本次请求不会自动重发。')
            if self.failed:raise ProtocolError('官方接口响应格式异常；本次请求不会自动重发。')
            try:row=self.queue.get(timeout=.2)
            except queue.Empty:
                if self.process.poll() is not None:raise ProtocolError('官方ZCode接口已退出。')
                continue
            if row.get('method') and 'id' in row:
                # Optional runtime preferences fall back to vendor defaults. All other
                # reverse interactions also fail closed; no permission is ever granted.
                self.send({'id':row['id'],'error':{'code':-32601,'message':'Interactive tools are unavailable on this text-only connection'}})
            elif row.get('id')==rid:
                if row.get('error'):raise ProtocolError('官方接口未完成本次操作（代码%s）。' % row['error'].get('code','unknown'))
                if 'result' not in row:raise ProtocolError('官方接口缺少结果。')
                return row['result']
        raise ProtocolError('官方接口响应超时；本次请求不会自动重发。')

    def inspect(self):
        # Empty session creation has title generation explicitly disabled. No prompt,
        # connectivity-test method, model invocation or existing history is requested.
        row=self.call('session/create',{'workspace':self.workspace,'mode':'plan',
            'titleGenerationEnabled':False,'mcpServers':[],'toolAllowlist':[]})
        sid=(row.get('session') or {}).get('sessionId')
        settings=(row.get('settings') or {}).get('model') or {}
        models=[]
        for item in settings.get('available') or []:
            ref=item.get('ref')
            if not isinstance(ref,dict):continue
            if ref.get('providerId') != 'account:bigmodel-individual-coding-plan':continue
            ref={k:ref[k] for k in ('providerId','modelId','options') if k in ref}
            # Only account-owned domestic model references can be admitted. Exact
            # provider classification is verified against the official registry ref.
            models.append({'ref':ref,'label':str(item.get('label') or '')[:160],
                'provider_label':str(item.get('providerLabel') or '')[:160],
                'disabled_reason':str(item.get('disabledReason') or '')[:240]})
        if sid:self.call('session/close',{'sessionId':sid},timeout=15)
        return {'models':models,'current':settings.get('current'), 'model_requests':0}

    def close(self):
        try:self.process.stdin.close()
        except OSError:pass
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def identity():
    node,cli,_=installation()
    return {'cli_path':str(cli),'cli_sha256':hashlib.sha256(cli.read_bytes()).hexdigest(),'node':node}
