"""Launch the opt-in experiment with its own writable profile, without changing old launchers."""
from __future__ import annotations
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--sdk-python", default=sys.executable)
    parser.add_argument("--account-home", type=Path)
    parser.add_argument("--edition", choices=("global", "cn"), default="global")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    profile = args.profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    # A second click reopens this exact live instance instead of sharing writable state.
    try:
        previous=json.loads((profile/'LAUNCH.json').read_text(encoding='utf-8'))
        previous_url=str(previous.get('url') or '')
        from urllib.parse import urlsplit
        parsed=urlsplit(previous_url)
        if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and Path(previous.get('source','')).resolve()==root:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(previous_url+'api/health',timeout=2) as response:
                health=json.load(response)
            if (health.get('process_id')==previous.get('pid')
                    and Path(health.get('runs_root','')).resolve()==profile/'runs'
                    and Path(health.get('runtime_root','')).resolve()==root):
                if previous.get('edition','global') != args.edition:
                    raise RuntimeError('该目录正在运行另一版Qoder，请先关闭该实验实例或使用独立目录。')
                if not args.no_browser:webbrowser.open(previous_url)
                print(json.dumps({**previous,'reused_existing_instance':True},ensure_ascii=False))
                return
    except (OSError,ValueError,KeyError):
        pass
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("MMF_", "QODER_", "QODERCN_")) or key in {"PYTHONHOME", "NODE_OPTIONS"} or re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)$", key, re.I):
            env.pop(key, None)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env.update(MMF_HOST="127.0.0.1", MMF_PORT=str(port), MMF_PACKAGE_ROOT=str(root),
        MMF_APP_ROOT=str(root/'app'), MMF_QODER_ACCOUNT_ENABLED="1", MMF_QODER_ACCOUNT_PYTHON=args.sdk_python,
        MMF_QODER_ACCOUNT_EDITION=args.edition,
        MMF_CREDENTIAL_NAMESPACE="mmf_qoder_account_experiment",
        MMF_USER_SETTINGS=str(profile/'settings.json'),
        PYTHONPATH=str(root/'app'), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
        LOCALAPPDATA=str(profile/'local'), APPDATA=str(profile/'roaming'),
        TEMP=str(profile/'temp'), TMP=str(profile/'temp'), TMPDIR=str(profile/'temp'),
        NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1")
    for name, sub in (("DATA_ROOT","data"),("CONFIG_ROOT","config"),("RUNTIME_DIR","runtime"),
        ("TEMP_ROOT","work"),("OUTPUT_ROOT","output"),("RUNS_DIR","runs"),("LOGS_DIR","logs"),
        ("BROKER_DIR","broker"),("SECURE_CONFIG_DIR","secure"),("RUNTIME_ROOT","runtime-root")):
        env['MMF_'+name] = str(profile/sub)
        (profile/sub).mkdir(exist_ok=True)
    (profile/'temp').mkdir(exist_ok=True)
    if args.account_home:
        env['MMF_QODER_ACCOUNT_HOME'] = str(args.account_home.resolve())
    settings = profile/'settings.json'
    if not settings.exists():
        settings.write_text(json.dumps({'first_run_completed':True,'default_provider':'qwen_modelstudio',
            'output_root':str(profile/'output'),'enable_grok_bridge':False}),encoding='utf-8')
    for name in ('app.json','providers.example.json'):
        import shutil
        if not (profile/'config'/name).exists():
            shutil.copyfile(root/'config'/name, profile/'config'/name)
    with (profile/'server.log').open('a',encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable,'-B',str(root/'app/server.py')],cwd=root,env=env,
            stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    url=f'http://127.0.0.1:{port}/'
    actual_pid=None
    for attempt in range(40):
        if process.poll() is not None:
            raise RuntimeError('实验服务未启动，请查看专用目录server.log。')
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url+'api/health',timeout=1) as response:
                health=json.load(response)
            if Path(health.get('runtime_root','')).resolve()!=root or Path(health.get('runs_root','')).resolve()!=profile/'runs':
                raise RuntimeError('实验服务身份不符。')
            actual_pid=health['process_id'];break
        except (OSError,ValueError):
            time.sleep(.25)
    if actual_pid is None:
        raise RuntimeError('实验服务启动超时，请查看专用目录server.log。')
    row={'pid':actual_pid,'launcher_process_id':process.pid,'url':url,'port':port,'source':str(root),'profile':str(profile),'edition':args.edition,
        'model_calls_on_launch':0,'sdk_python':args.sdk_python,'old_services_touched':False}
    (profile/'LAUNCH.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
    if not args.no_browser:
        webbrowser.open(row['url'])
    print(json.dumps(row,ensure_ascii=False))


if __name__=='__main__':main()
