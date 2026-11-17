import hashlib,json,time
from types import SimpleNamespace
import pytest
import paths
import account_tasks as a
from providers.base import ProviderError,write_json
from zcode_account import connection as mod
from zcode_account.protocol import ProtocolError
from test_zcode_account_connection import ctl,request

@pytest.fixture
def views(tmp_path,monkeypatch):
    roots=SimpleNamespace(runs_dir=tmp_path/'runs',runtime_dir=tmp_path/'runtime',output_root=tmp_path/'out')
    roots.runs_dir.mkdir();monkeypatch.setattr(paths,'current',lambda:roots)
    def task(run_id,state='content_ready',**kw):
        key=hashlib.sha256(run_id.encode()).hexdigest()[:32]
        row={'key':key,'run_id':run_id,'provider':'zcode_cn','task_id':run_id+'-tender-CHK-1',
             'state':state,'turn':1,'session_id':'synthetic','owner':a.INSTANCE,**kw}
        a.put(key,row);write_json(a.folder(key)/'request.json',{'run_id':run_id,'task_id':row['task_id']})
        return row
    return roots,task

def test_hidden_history_and_removed_run_do_not_leak_into_task_listing(views):
    roots,task=views
    for rid in ['current','hidden']:(roots.runs_dir/rid).mkdir()
    task('current');task('hidden');task('deleted')
    write_json(roots.runs_dir/'hidden/history_hidden.json',{'hidden':True})
    assert [r['run_id'] for r in a.list_tasks()]==['current']
    assert a.list_tasks('hidden')==[] and a.list_tasks('deleted')==[]
    assert (a.folder(hashlib.sha256(b'deleted').hexdigest()[:32])/'state.json').exists()

@pytest.mark.parametrize('operation',['answer','cancel','continue'])
def test_hidden_run_actions_cannot_recreate_or_dispatch(views,operation):
    roots,task=views;row=task('deleted','waiting_for_user',nonce='nonce',question={'kind':'facts','question':'synthetic'})
    with pytest.raises(ProviderError) as error:
        if operation=='answer':a.answer(row['key'],'nonce','synthetic')
        elif operation=='cancel':a.cancel(row['key'])
        else:a.continue_workflow(row['key'],lambda *args:pytest.fail('must not resume hidden run'))
    assert error.value.error_code=='ACCOUNT_RUN_HIDDEN' and not (roots.runs_dir/'deleted').exists()

def test_saved_recovery_does_not_recreate_a_deleted_run(views,monkeypatch):
    roots,task=views;row=task('deleted')
    monkeypatch.setattr(mod,'connection',lambda:pytest.fail('must not load account for removed run'))
    a._recover(row['key'],lambda *args:pytest.fail('must not restart workflow'))
    assert not (roots.runs_dir/'deleted').exists()

def test_version_rejection_before_admission_is_definitely_unsent(ctl,tmp_path,monkeypatch):
    c,calls=ctl
    def reject():raise ProtocolError('版本尚未适配',code='ZCODE_VERSION_UNREVIEWED')
    monkeypatch.setattr(mod,'verified_installation',reject)
    with pytest.raises(ProviderError) as error:c.execute(request(),tmp_path/'task')
    assert error.value.error_code=='ZCODE_NOT_DISPATCHED' and not calls
    assert not list((c.root/'jobs').rglob('*.json'))

def test_update_race_before_spawn_keeps_unsent_ledger_and_explicit_retry(ctl,tmp_path,monkeypatch):
    c,calls=ctl;original=mod.Client
    class Race(original):
        def call(self,*args,**kw):raise ProtocolError('版本更新',code='ZCODE_VERSION_UNREVIEWED')
    monkeypatch.setattr(mod,'Client',Race)
    with pytest.raises(ProviderError) as error:c.execute(request(),tmp_path/'task')
    assert error.value.error_code=='ZCODE_NOT_DISPATCHED' and not calls
    markers=[p for p in (c.root/'jobs').rglob('*.json') if not p.name.endswith('.answer.json')]
    assert len(markers)==1 and json.loads(markers[0].read_text())['status']=='not_dispatched'
    monkeypatch.setattr(mod,'Client',original)
    c.execute(request(),tmp_path/'retry');assert len(calls)==1
    assert json.loads(markers[0].read_text())['pre_send_history'][0]['status']=='not_dispatched'

def test_user_can_continue_known_unsent_step_without_replaying_on_list(views):
    roots,task=views;(roots.runs_dir/'current').mkdir()
    row=task('current','needs_attention',error_code='ZCODE_NOT_DISPATCHED',turn=0)
    calls=[]
    listed=a.list_tasks('current');assert listed[0]['retry_available'] and not calls
    a.continue_workflow(row['key'],lambda row,request:calls.append(request))
    end=time.monotonic()+2
    while not calls and time.monotonic()<end:time.sleep(.01)
    assert len(calls)==1

def test_coordinator_only_allows_explicit_retry_of_definitely_unsent(views,tmp_path):
    roots,_=views;(roots.runs_dir/'current').mkdir()
    request={'run_id':'current','task_id':'synthetic','required_keys':['ok'],'prompt':'synthetic'}
    class C:
        root=tmp_path/'ctl';selection={'model':'synthetic'};calls=0
        def execute(self,*args):
            self.calls+=1
            if self.calls==1:raise ProviderError('未发送',error_code='ZCODE_NOT_DISPATCHED')
            return {'body':'{"ok":true}','session_id':'synthetic'}
        def event(self,*args,**kw):pass
    c=C()
    with pytest.raises(ProviderError):a.execute('zcode_cn',request,tmp_path/'task',c)
    assert c.calls==1 and a.list_tasks()[0]['retry_available']
    a.execute('zcode_cn',request,tmp_path/'retry',c)
    assert c.calls==2 and a.list_tasks()[0]['state']=='content_ready'
