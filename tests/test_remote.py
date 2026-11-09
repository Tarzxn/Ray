import base64, hashlib, importlib.util, io, json, os, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
from beam_core import Agent, Run, Store
from desktop import create_app
from remote_tools import RemoteComputers, execute_remote

spec=importlib.util.spec_from_file_location('beam_test_worker',Path(__file__).parents[1]/'worker/runner.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.local=self.root/'local';self.local.mkdir();self.store=Store(self.root/'data');self.agent=Agent(self.store)
        self.pid=self.store.add_project(self.local)['id'];self.sid=self.store.create_session(self.pid)['id']
        self.remote=self.agent.remote
    def tearDown(self):
        for run in self.agent.runs.values():run.stop()
        self.remote.close();self.agent.servers.close();self.agent.integrations.close();self.store.db.close();self.temp.cleanup()
    def mapped(self,trusted=True):
        with self.store.lock,self.store.db:self.store.db.execute('INSERT OR REPLACE INTO remote_projects VALUES (?,?,?,?)',(self.pid,'test-codespace-123',int(trusted),1))
        return self.remote.target(self.pid)
    def run_object(self,trusted=True):
        run=Run();run.remote=self.mapped(trusted);return run
    def test_remote_tools_never_execute_or_edit_local_project(self):
        run=self.run_object();calls=[]
        def rpc(name,pid,op,args,**kwargs):
            calls.append((op,args));return {'expected':'digest','diff':'new','path':'remote.py','bytes':4}
        with patch.object(self.remote,'rpc_name',side_effect=rpc),patch.object(self.agent,'_execute_tool') as local:
            self.agent.execute(self.sid,run,self.local,'edit','write_file',{'path':'remote.py','content':'pass'})
        self.assertEqual([x[0] for x in calls],['prepare_edit','commit_edit']);local.assert_not_called();self.assertFalse((self.local/'remote.py').exists())
    def test_remote_connection_failure_has_no_local_fallback(self):
        run=self.run_object()
        with patch.object(self.remote,'rpc_name',side_effect=ValueError('disconnected')),patch('beam_core.run_command') as local:
            with self.assertRaisesRegex(ValueError,'disconnected'):self.agent.execute(self.sid,run,self.local,'edit','run_command',{'command':'python3 --version'})
        local.assert_not_called()
    def test_trusted_remote_commands_skip_prompt_and_use_linux(self):
        run=self.run_object()
        with patch.object(self.remote,'rpc_name',return_value={'exit_code':0,'status':'Finished'}) as rpc,patch.object(self.agent,'request_approval') as approval:
            self.agent.execute(self.sid,run,self.local,'edit','run_command',{'command':'python3 -m unittest'})
        approval.assert_not_called();self.assertEqual(run.command_count,1);self.assertEqual(rpc.call_args.args[2],'run_command')
    def test_untrusted_ask_plan_and_sensitive_command_boundaries(self):
        for mode,trusted,command,should_ask in [('ask',True,'python3 --version',True),('edit',False,'python3 --version',True),('edit',True,'git push',True),('edit',True,'sudo apt update',True)]:
            with self.subTest(mode=mode,trusted=trusted,command=command):
                run=self.run_object(trusted)
                with patch.object(self.remote,'rpc_name') as rpc,patch.object(self.agent,'request_approval',return_value=False) as approval:
                    result=self.agent.execute(self.sid,run,self.local,mode,'run_command',{'command':command})
                self.assertTrue(result['denied']);approval.assert_called_once();rpc.assert_not_called()
        with patch.object(self.remote,'rpc_name') as rpc,self.assertRaisesRegex(ValueError,'Plan mode'):
            self.agent.execute(self.sid,self.run_object(),self.local,'plan','run_command',{'command':'echo no'})
        rpc.assert_not_called()
    def test_remote_unsupported_tools_cannot_launch_local_browser(self):
        with patch.object(self.agent.integrations.browser,'call') as browser,self.assertRaisesRegex(ValueError,'not supported'):
            self.agent.execute(self.sid,self.run_object(),self.local,'edit','browser_open',{'url':'https://example.com'})
        browser.assert_not_called()
    def test_rpc_uses_structured_stdin_not_interpolated_shell_input(self):
        command='echo "$SECRET"; touch arbitrary.txt'
        with patch.object(self.remote,'gh',return_value='gh'),patch.object(self.remote,'_stream',return_value={}) as stream:
            self.remote.rpc_name('test-codespace-123',self.pid,'run_command',{'command':command})
        argv,payload=stream.call_args.args[:2]
        self.assertNotIn(command,' '.join(argv));self.assertEqual(json.loads(payload)['arguments']['command'],command)
        with self.assertRaises(ValueError):self.remote.rpc_name('--evil',self.pid,'status',{})
    def test_transport_protocol_and_cancellation(self):
        script="import sys,json; r=json.loads(sys.stdin.readline()); print(json.dumps({'event':'terminal','text':'remote output'}),flush=True); print(json.dumps({'event':'result','result':{'ok':True}}),flush=True)"
        events=[];result=self.remote._stream([sys.executable,'-u','-c',script],b'{}\n',5,on_event=events.append)
        self.assertTrue(result['ok']);self.assertEqual(events[0]['text'],'remote output')
        cancelled=threading.Event();script="import sys,json; sys.stdin.readline(); sys.stdin.read(); print(json.dumps({'event':'result','result':{}}),flush=True)"
        timer=threading.Timer(.15,cancelled.set);timer.start()
        with self.assertRaisesRegex(ValueError,'stopped'):self.remote._stream([sys.executable,'-u','-c',script],b'{}\n',5,cancelled)
        timer.join()
    def test_cancellation_cannot_block_on_large_stdin_upload(self):
        cancelled=threading.Event();timer=threading.Timer(.2,cancelled.set);timer.start();start=time.monotonic()
        with self.assertRaisesRegex(ValueError,'stopped'):
            self.remote._stream([sys.executable,'-u','-c','import time; time.sleep(60)'],b'x'*1000000,5,cancelled)
        timer.join();self.assertLess(time.monotonic()-start,5)
    def test_browser_login_exposes_device_code_but_no_credentials(self):
        script="import sys; sys.stdin.readline(); print('Copy code: ABCD-1234',flush=True); print('credential must remain private',flush=True)"
        def spawn(args,**kwargs):
            self.assertIn('--web',args);self.assertIn('codespace',args)
            return subprocess.Popen([sys.executable,'-u','-c',script],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        with patch.object(self.remote,'gh',return_value='helper'),patch.object(self.remote,'spawn',side_effect=spawn):
            self.remote.login();limit=time.monotonic()+5
            while self.remote.auth['status']=='waiting' and time.monotonic()<limit:time.sleep(.01)
        self.assertEqual(self.remote.auth['status'],'done');self.assertEqual(self.remote.auth['code'],'ABCD-1234')
        self.assertNotIn('credential',json.dumps(self.remote.state()))
    def test_upload_preview_is_scoped_and_excludes_credentials(self):
        self.mapped();(self.local/'note.txt').write_text('safe');(self.local/'.env').write_text('SECRET=private')
        result=self.remote.preview_upload(self.pid,self.local,[{'path':'note.txt'},{'path':'.env'}])
        self.assertEqual([f['path'] for f in result['files']],['note.txt'])
        with patch.object(self.remote,'rpc',return_value={'files':1}) as rpc:self.remote.upload(self.pid,result['token'])
        self.assertEqual(rpc.call_args.args[1],'upload')
        with self.assertRaises(ValueError):self.remote.upload(self.pid,result['token'])
    def test_idle_stop_only_targets_opted_in_inactive_computer(self):
        self.mapped();self.remote.closed.set();self.remote.thread.join(2)
        self.remote.closed=unittest.mock.Mock();self.remote.closed.wait.side_effect=[False,True]
        self.remote.last_use['test-codespace-123']=time.monotonic()-400
        with patch.object(self.remote,'stop') as stop:self.remote._idle_loop()
        stop.assert_called_once_with('test-codespace-123')
        self.remote.closed.wait.side_effect=[False,True];self.remote.active=lambda _:True
        with patch.object(self.remote,'stop') as stop:self.remote._idle_loop()
        stop.assert_not_called()
    def test_target_not_saved_until_worker_version_verified(self):
        with patch.object(self.remote,'rpc_name',return_value={'ready':True,'version':'wrong'}),self.assertRaisesRegex(ValueError,'version'):
            self.remote.configure(self.pid,{'codespace':'test-codespace-123'})
        self.assertIsNone(self.remote.target(self.pid))
    def test_switch_uses_one_activation_and_remembers_codespace(self):
        with patch.object(self.remote,'rpc_name',return_value={'ready':True,'version':'0.7.0','files':[{'path':'remote.txt','bytes':4}]}) as rpc:
            connected=self.remote.configure(self.pid,{'codespace':'test-codespace-123','trusted':True,'auto_stop':False})
        self.assertEqual(rpc.call_count,1);self.assertEqual(rpc.call_args.args[2],'activate')
        self.assertEqual(connected['files'][0]['path'],'remote.txt')
        local=self.remote.configure(self.pid,{'target':'local'})
        self.assertIsNone(local['target']);self.assertEqual(local['saved_target']['codespace'],'test-codespace-123')
        with patch.object(self.remote,'rpc_name',return_value={'ready':True,'version':'0.7.0','files':[]}) as rpc:
            restored=self.remote.configure(self.pid,{'codespace':local['saved_target']['codespace'],'trusted':True,'auto_stop':False})
        self.assertEqual(rpc.call_count,1);self.assertEqual(restored['target']['codespace'],'test-codespace-123')
    def test_cli_environment_keeps_model_credentials_off_connection_helper(self):
        with patch.dict(os.environ,{'OLLAMA_API_KEY':'private','GH_TOKEN':'global-token'}):env=self.remote.environment()
        self.assertNotIn('OLLAMA_API_KEY',env);self.assertNotIn('GH_TOKEN',env);self.assertEqual(env['GH_CONFIG_DIR'],str(self.store.root/'github'))
    def test_worker_rejects_escape_credentials_and_stale_edits(self):
        root=self.root/'remote';root.mkdir()
        for raw in ('../out.txt','/etc/passwd','.env','nested/secret.pem','node_modules/a'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):worker.path_in(root,raw)
        args={'path':'a.py','content':'print(1)'};prepared=worker.edit(root,args,False)
        (root/'a.py').write_text('print(2)')
        with self.assertRaisesRegex(ValueError,'changed'):worker.edit(root,{**args,'expected':prepared['expected']},True)
        with self.assertRaises(SyntaxError):worker.edit(root,{'path':'bad.py','content':'def !'},False)
    def test_worker_file_edit_upload_and_artifact_are_real(self):
        with patch.object(worker,'BASE',self.root/'remote-base'):
            def call(op,args=None):return worker.dispatch({'operation':op,'project_id':self.pid,'arguments':args or {}})
            prepared=call('prepare_edit',{'path':'hello.py','content':'print("hello")'})
            call('commit_edit',{'path':'hello.py','content':'print("hello")','expected':prepared['expected']})
            self.assertIn('hello',call('read_file',{'path':'hello.py'})['content'])
            call('upload',{'files':[{'path':'notes.md','data':base64.b64encode(b'# Notes').decode()}]})
            self.assertEqual(base64.b64decode(call('download',{'path':'notes.md'})['data']),b'# Notes')
            result=call('generate_artifact',{'path':'report.docx','kind':'docx','content':'# Worker report\n\nGenerated on the worker.'})
            self.assertTrue((worker.project(self.pid)/result['path']).is_file())
    def test_invalid_bulk_upload_publishes_no_files(self):
        with patch.object(worker,'BASE',self.root/'remote-base'):
            with self.assertRaises(ValueError):worker.dispatch({'operation':'upload','project_id':self.pid,'arguments':{'files':[{'path':'ok.txt','data':'b2s='},{'path':'../escape','data':'eA=='}]}})
            self.assertFalse((worker.project(self.pid)/'ok.txt').exists())
    def test_remote_results_still_pass_local_privacy_guard_before_cloud(self):
        self.mapped();ip='10.33.44.55';marker='LOCAL_ONLY_DERIVED_RESULT';payloads=[]
        class Response:
            status_code=200
            def __init__(self,message):self.message=message
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def close(self):pass
            def iter_lines(self):yield json.dumps({'message':self.message,'done':True}).encode()
        def post(url,**kwargs):
            payloads.append(kwargs['json'])
            return Response({'tool_calls':[{'function':{'name':'read_file','arguments':{'path':'network.txt'}}}]} if len(payloads)==1 else {'content':'Done.'})
        with patch.object(self.store,'key',return_value='fixture'),patch('beam_core.requests.post',side_effect=post),patch.object(self.remote,'rpc_name',return_value={'content':ip+'.'}),patch.object(self.agent.privacy,'_model',return_value='local'),patch.object(self.agent.privacy,'_summarize',return_value=marker):
            run=self.agent.start(self.sid,'Read the project file.','edit','low')
            until=time.monotonic()+5
            while not run.privacy_pending and time.monotonic()<until:time.sleep(.01)
            self.assertEqual(len(payloads),1);self.agent.choose_privacy(self.sid,run.privacy_pending['action_id'],'local','local');run.worker.join(5)
        self.assertEqual(run.status,'done');self.assertEqual(len(payloads),2)
        self.assertNotIn(ip,json.dumps(payloads));self.assertNotIn(marker,json.dumps(payloads));self.assertTrue(any(e['type']=='private_summary' for e in self.store.events(self.sid)))
        self.assertEqual(payloads[0]['model'],'beam');self.assertNotIn('browser_open',[t['function']['name'] for t in payloads[0]['tools']])

class RemoteApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        folder=self.root/'project';folder.mkdir();self.app=create_app(self.root/'data','remote-test-token')
        self.pid=self.app.store.add_project(folder)['id'];self.client=self.app.test_client();self.headers={'X-Beam-Token':'remote-test-token'}
    def tearDown(self):
        self.app.crew.close();self.app.agent.remote.close();self.app.agent.integrations.close();self.app.agent.servers.close();self.app.store.db.close();self.temp.cleanup()
    def test_remote_settings_require_local_token_and_origin(self):
        self.assertEqual(self.client.get('/api/computer').status_code,403)
        self.assertEqual(self.client.post('/api/computer/login',headers={**self.headers,'Origin':'https://evil.example'},json={}).status_code,403)
        self.assertEqual(self.client.post('/api/computer/target',headers=self.headers,json=[]).status_code,400)
    def test_connection_install_target_and_local_return_contract(self):
        with patch.object(self.app.agent.remote,'rpc_name',return_value={'ready':True,'version':'0.7.0','files':[{'path':'cloud.py','bytes':10}]}) as rpc:
            response=self.client.post('/api/computer/target',headers=self.headers,json={'project_id':self.pid,'codespace':'fixture-codespace','trusted':True,'auto_stop':True})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['target']['trusted'],1)
        self.assertEqual(response.json['files'][0]['path'],'cloud.py');self.assertEqual(rpc.call_args.args[2],'activate')
        result=self.client.get('/api/computer?project_id='+self.pid,headers=self.headers).json
        self.assertEqual(result['target']['codespace'],'fixture-codespace')
        response=self.client.post('/api/computer/target',headers=self.headers,json={'project_id':self.pid,'target':'local'})
        self.assertEqual(response.status_code,200);self.assertIsNone(response.json['target']);self.assertEqual(response.json['saved_target']['codespace'],'fixture-codespace')
    def test_remote_editor_checks_stale_contents_before_commit(self):
        with self.app.store.lock,self.app.store.db:self.app.store.db.execute('INSERT INTO remote_projects VALUES (?,?,?,?)',(self.pid,'fixture-codespace',0,0))
        with patch.object(self.app.agent.remote,'rpc',return_value={'expected':hashlib.sha256(b'newer').hexdigest(),'diff':''}) as rpc:
            response=self.client.post('/api/projects/'+self.pid+'/file',headers=self.headers,json={'path':'app.py','original':'old','content':'pass'})
        self.assertEqual(response.status_code,409);self.assertEqual(rpc.call_count,1)
    def test_git_inspector_does_not_wake_stopped_codespace(self):
        with self.app.store.lock,self.app.store.db:self.app.store.db.execute('INSERT INTO remote_projects VALUES (?,?,?,?)',(self.pid,'fixture-codespace',0,0))
        self.app.agent.remote.paused.add('fixture-codespace')
        with patch.object(self.app.agent.remote,'rpc') as rpc:
            result=self.client.get('/api/projects/'+self.pid+'/git',headers=self.headers)
            diff=self.client.get('/api/projects/'+self.pid+'/git?diff=1',headers=self.headers)
        self.assertTrue(result.json['paused']);self.assertTrue(diff.json['paused']);rpc.assert_not_called()
    def test_background_credential_storage_requires_explicit_consent(self):
        with patch.object(self.app.agent.remote.cloud,'enable',return_value={'enabled':True}) as enable:
            denied=self.client.post('/api/computer/cloud-crew',headers=self.headers,json={'project_id':self.pid,'enabled':True})
            self.assertEqual(denied.status_code,400);enable.assert_not_called()
            allowed=self.client.post('/api/computer/cloud-crew',headers=self.headers,json={'project_id':self.pid,'enabled':True,'credential_consent':True})
            self.assertEqual(allowed.status_code,200);enable.assert_called_once()
    def test_private_transfer_response_exposes_categories_without_original_text(self):
        from cloud_client import CloudTransferReview
        with patch.object(self.app.crew,'create_task',side_effect=CloudTransferReview(['IP address'])):
            result=self.client.post('/api/crew/tasks',headers=self.headers,json={'project_id':self.pid,'prompt':'10.33.45.67'})
        self.assertEqual(result.status_code,400);self.assertTrue(result.json['cloud_transfer_review']);self.assertNotIn('10.33.45.67',result.get_data(as_text=True))
    def test_active_task_blocks_computer_change(self):
        sid=self.app.store.create_session(self.pid)['id'];self.app.agent.runs[sid]=Run()
        response=self.client.post('/api/computer/target',headers=self.headers,json={'project_id':self.pid,'target':'local'})
        self.assertEqual(response.status_code,400)

if __name__=='__main__':unittest.main()
