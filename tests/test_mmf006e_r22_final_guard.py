import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from providers.capability_probe import observe_from_health
from longform.eta import append_latency_sample,load_budget_history_samples,sample_eligible_for_eta,history_key

class FinalGuards(unittest.TestCase):
    def test_scheduler_settings_not_sent_to_vendor_api(self):
        from providers.openai_compatible import OpenAICompatibleProvider
        provider=OpenAICompatibleProvider({'provider_name':'fixture','provider_type':'openai_compatible','enabled':True,'base_url':'https://example.invalid/v1','model':'fixture','extra_options':{'rpm':3,'tpm':900,'latency_budget':{'soft':10},'soft_latency_budget':10,'hard_timeout':40,'supported_reasoning_levels':['low']}})
        captured=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps({'choices':[{'message':{'content':'{"probe":true,"ok":true}'}}],'usage':{'prompt_tokens':1,'completion_tokens':1}}).encode()
        def send(request,timeout):
            captured.append(json.loads(request.data))
            return Response()
        with tempfile.TemporaryDirectory() as tmp,patch.object(provider,'_configured',return_value=True),patch.object(provider,'_api_key',return_value=('synthetic-test-key','fixture')),patch('providers.openai_compatible._http_open',side_effect=send):
            provider.invoke_structured({'task_id':'probe','generation_mode':'capability_probe','system_prompt':'fixture','prompt':'fixture','max_tokens':128,'timeout_seconds':30},Path(tmp))
        for field in ('rpm','tpm','latency_budget','soft_latency_budget','hard_timeout','supported_reasoning_levels'):
            self.assertNotIn(field,captured[0])
        self.assertEqual(captured[0]['max_tokens'],128)

    def provider(self, output=None, error=None):
        calls=[]
        def invoke(request, directory):
            calls.append(request)
            if error: raise error
            return output or {'probe':True,'ok':True}
        return SimpleNamespace(config={'provider_name':'fixture','provider_type':'openai_compatible','enabled':True,'model':'fixture'},invoke_structured=invoke),calls

    def test_passive_status_never_generates(self):
        provider,calls=self.provider()
        with tempfile.TemporaryDirectory() as tmp:
            row=observe_from_health(provider,{'available':True},store_path=Path(tmp)/'obs.json')
            self.assertEqual(calls,[])
            self.assertEqual(row['probe_kind'],'health_only')

    def test_explicit_probe_is_small_and_contract_checked(self):
        provider,calls=self.provider({'wrong':True})
        with tempfile.TemporaryDirectory() as tmp:
            row=observe_from_health(provider,{'available':True},force=True,store_path=Path(tmp)/'obs.json')
            self.assertEqual(row['status'],'OUTPUT_CONTRACT_ERROR')
            self.assertEqual(calls[0]['max_tokens'],128)
            self.assertEqual(calls[0]['timeout_seconds'],30)
            self.assertEqual(calls[0]['tools'],[])

    def test_explicit_probe_auth_failure_not_pass(self):
        provider,calls=self.provider(error=RuntimeError('login required'))
        with tempfile.TemporaryDirectory() as tmp:
            row=observe_from_health(provider,{'available':True},force=True,store_path=Path(tmp)/'obs.json')
            self.assertEqual(row['status'],'AUTH_REQUIRED')

    def test_finalized_budget_samples_do_not_pollute_document_eta(self):
        row={'schema_version':'latency-history-v2','history_id':'one','provider':'mock','model':'fixture','mode':'balanced','stage':'draft','sample_scope':'provider_attempt','batch_size':2,'success':True,'eligible_for_eta':True,'elapsed_seconds':10,'size_bucket':'full_20_plus'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'history.jsonl'
            append_latency_sample(path,row)
            self.assertEqual(len(load_budget_history_samples(path,provider='mock',model='fixture',mode='balanced',stage='draft',batch_size=2)),1)
            self.assertEqual(load_budget_history_samples(path,provider='mock',model='fixture',mode='balanced',stage='draft',batch_size=1),[])
            self.assertFalse(sample_eligible_for_eta(row,history_key('mock','fixture','balanced','draft',27)))

    def test_terminal_writer_produces_budget_sample_only_after_pass(self):
        import app_core
        performance={'this_run_calls':1,'repair_reasoning':'not_invoked','token_accounting':{'input_tokens':3,'output_tokens':4},'provider_attempt_samples':[{'elapsed_seconds':2,'stage':'draft','batch_size':2,'model':'fixture','input_tokens':3,'output_tokens':4}]}
        result={'word':{'this_run_calls':1,'logical_section_count':2,'generation_batch_count':1},'performance':performance,'capability':{'effective_settings':{'model':'fixture'}}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(app_core,'RUNTIME_DIR',Path(tmp)):
            app_core._record_terminal_latency(run_id='a',brief={},provider_name='mock',longform_result=result,status='generation_completed',eligible=True,actual_time=3,qa_seconds=1,build_id='fixture')
            rows=[json.loads(s) for s in (Path(tmp)/'latency_history.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([r['sample_scope'] for r in rows],['full_document','provider_attempt'])
            app_core._record_terminal_latency(run_id='b',brief={},provider_name='mock',longform_result=result,status='compliance_blocked',eligible=False,actual_time=3,qa_seconds=1,build_id='fixture')
            rows=[json.loads(s) for s in (Path(tmp)/'latency_history.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows),3)
            self.assertFalse(rows[-1]['eligible_for_eta'])

if __name__=='__main__': unittest.main()
