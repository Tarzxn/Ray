import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from beam_core import Agent, Run, Store, project_path, run_command, walk_files, protect_secret
from desktop import create_app
from enhanced_artifacts import produce_artifact


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.project=self.root/'project';self.project.mkdir()
        self.app=create_app(self.root/'data','test-token');self.client=self.app.test_client()
        self.headers={'X-Beam-Token':'test-token'}
        self.p=self.app.store.add_project(self.project)
        self.s=self.app.store.create_session(self.p['id'])['id']
        self.agent=self.app.agent
    def tearDown(self):
        self.app.store.db.close();self.temp.cleanup()
    def request(self,path,data=None):
        return self.client.get('/api/'+path,headers=self.headers) if data is None else self.client.post('/api/'+path,json=data,headers=self.headers)
    def execute(self,name,args,mode='edit',run=None):
        return self.agent.execute(self.s,run or Run(),self.project,mode,name,args)
    def test_loopback_token_origin_and_host_guards(self):
        self.assertEqual(self.client.get('/api/state').status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={**self.headers,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers=self.headers,base_url='http://evil.example').status_code,403)
        self.assertEqual(self.request('state').status_code,200)
    def test_no_key_or_private_reasoning_in_state(self):
        with patch.object(self.app.store,'key',return_value='a-secret'):
            response=self.request('state')
        self.assertNotIn(b'a-secret',response.data)
        self.app.store.save_messages(self.s,[{'role':'assistant','thinking':'private-reasoning','content':'visible'}])
        self.assertNotIn(b'private-reasoning',self.request('sessions/'+self.s).data)
    def test_traversal_credentials_and_devices(self):
        for path in ['../outside.txt','C:/outside.txt','file.txt:stream','.env','.env.test','src/secret.key','.git/config','NUL.txt']:
            with self.subTest(path=path),self.assertRaises(ValueError):project_path(self.project,path)
        (self.project/'.env').write_text('PRIVATE',encoding='utf-8')
        (self.project/'code.py').write_text('print(1)',encoding='utf-8')
        self.assertEqual([r['path'] for r in walk_files(self.project)],['code.py'])
    def test_plan_mode_blocks_all_mutations(self):
        for name,args in [('write_file',{'path':'x.txt','content':'x'}),('edit_file',{'path':'x.txt','old_text':'x','new_text':'y'}),('run_command',{'command':'echo no'}),('generate_artifact',{'path':'x.pdf','kind':'pdf','content':'# no'})]:
            with self.subTest(name=name),self.assertRaises(ValueError):self.execute(name,args,'plan')
        self.assertEqual(list(self.project.iterdir()),[])
    def test_edit_ambiguity_and_exact_match(self):
        path=self.project/'file.py';path.write_text('x=1\nx=1\n',encoding='utf-8')
        with self.assertRaises(ValueError):self.execute('edit_file',{'path':'file.py','old_text':'x=1','new_text':'x=2'})
        result=self.execute('edit_file',{'path':'file.py','old_text':'x=1\nx=1','new_text':'x=2\nx=1'})
        self.assertTrue(result['changed']);self.assertEqual(path.read_text(),'x=2\nx=1\n')
    def test_approval_deny_never_writes(self):
        run=Run();self.agent.runs[self.s]=run
        box={}
        worker=threading.Thread(target=lambda:box.update(self.execute('write_file',{'path':'new.txt','content':'hello'},'ask',run)))
        worker.start()
        for _ in range(100):
            if run.pending:break
            time.sleep(.01)
        self.assertFalse((self.project/'new.txt').exists())
        self.agent.approve(self.s,run.pending['action_id'],False)
        worker.join(3)
        self.assertTrue(box['denied']);self.assertFalse((self.project/'new.txt').exists())
    def test_approved_edit_detects_concurrent_human_change(self):
        path=self.project/'file.txt';path.write_text('old',encoding='utf-8')
        def approval(*args):path.write_text('human',encoding='utf-8');return True
        with patch.object(self.agent,'request_approval',side_effect=approval),self.assertRaisesRegex(ValueError,'changed'):
            self.execute('write_file',{'path':'file.txt','content':'new'},'ask')
        self.assertEqual(path.read_text(),'human')
    def test_editor_conflict_protects_external_edits(self):
        (self.project/'x.txt').write_text('human',encoding='utf-8')
        r=self.request(f"projects/{self.p['id']}/file",{'path':'x.txt','original':'old','content':'new'})
        self.assertEqual(r.status_code,409);self.assertEqual((self.project/'x.txt').read_text(),'human')
    def test_html_preview_does_not_execute_in_app_origin(self):
        (self.project/'page.html').write_text('<script>evil()</script>',encoding='utf-8')
        self.assertEqual(self.request(f"projects/{self.p['id']}/preview?path=page.html").status_code,400)
    def test_durable_sessions_survive_store_reopen(self):
        self.app.store.emit(self.s,'assistant',text='Saved')
        other=Store(self.root/'data')
        self.assertEqual(other.events(self.s)[0]['text'],'Saved');other.db.close()
    def test_command_capture_and_timeout(self):
        result=run_command('Write-Output "beam-ok"' if os.name=='nt' else 'echo beam-ok',self.project)
        self.assertEqual(result['exit_code'],0);self.assertIn('beam-ok',result['output'])
        result=run_command('Start-Sleep -Seconds 5' if os.name=='nt' else 'sleep 5',self.project,1)
        self.assertEqual(result['status'],'Timed out')
    def test_mocked_agent_reads_edits_and_finishes_with_beam(self):
        (self.project/'app.py').write_text('value = 1\n',encoding='utf-8')
        responses=[{'content':'I will inspect the file.','tool_calls':[{'function':{'name':'read_file','arguments':{'path':'app.py'}}}]},
                   {'content':'I found the value.','tool_calls':[{'function':{'name':'edit_file','arguments':{'path':'app.py','old_text':'value = 1','new_text':'value = 2'}}}]},
                   {'content':'Updated the value to 2.'}]
        class Response:
            status_code=200
            def __init__(self,message):self.message=message
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_lines(self):yield json.dumps({'message':self.message,'done':True,'prompt_eval_count':100,'eval_count':20}).encode()
            def close(self):pass
        with patch.object(self.app.store,'key',return_value='test'),patch('beam_core.requests.post',side_effect=[Response(r) for r in responses]) as posted:
            run=self.agent.start(self.s,'Change value to 2','edit')
            for _ in range(300):
                if run.status=='done':break
                time.sleep(.01)
        self.assertEqual(run.status,'done');self.assertEqual((self.project/'app.py').read_text(),'value = 2\n')
        self.assertEqual(posted.call_args_list[0].kwargs['json']['model'],'beam')
        self.assertTrue(any(e['type']=='change' for e in self.app.store.events(self.s)))
        self.assertEqual(self.app.store.session(self.s)['messages'][-1]['content'],'Updated the value to 2.')
    def test_stop_between_calls_keeps_valid_history(self):
        run=Run();run.cancelled.set()
        self.agent._work(self.s,[{'role':'user','content':'Stop'}],'ask','test',run)
        self.assertEqual(run.status,'stopped')
    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_dpapi_roundtrip(self):
        encoded=protect_secret('test-secret');self.assertNotIn('test-secret',encoded);self.assertEqual(protect_secret(encoded,True),'test-secret')
    def test_xlsx_has_native_chart_formats_and_real_numbers(self):
        from openpyxl import load_workbook
        produce_artifact(self.project,{'path':'report.xlsx','kind':'xlsx','content':{'sheets':[{'name':'Sales','rows':[['Month','Revenue'],['Jan',100],['Feb',120]],'formats':{'B':'£#,##0.00'},'charts':[{'type':'bar','labels':'A','values':['B'],'title':'Sales'}]}]}})
        wb=load_workbook(self.project/'report.xlsx');ws=wb.active
        self.assertEqual(len(ws._charts),1);self.assertEqual(ws['B2'].value,100);self.assertEqual(ws['B2'].number_format,'£#,##0.00');wb.close()
    def test_pptx_metrics_comparison_and_native_chart(self):
        from pptx import Presentation
        content={'slides':[{'layout':'metrics','title':'Results','metrics':[{'value':'42','label':'Completed tasks'}]},
                           {'layout':'two-column','title':'Compare','left':{'title':'A','bullets':['One']},'right':{'title':'B','bullets':['Two']}},
                           {'layout':'chart','title':'Counts','chart':{'labels':['A','B'],'series':[{'name':'Count','values':[2,3]}]}}]}
        produce_artifact(self.project,{'path':'deck.pptx','kind':'pptx','content':content})
        deck=Presentation(self.project/'deck.pptx');self.assertEqual(len(deck.slides),3)
        self.assertTrue(any(s.has_chart for s in deck.slides[2].shapes))
    def test_invalid_generated_chart_is_atomic(self):
        with self.assertRaises(ValueError):produce_artifact(self.project,{'path':'bad.pptx','kind':'pptx','content':[{'layout':'chart','chart':{'labels':['a'],'series':[{'values':[1,2]}]}}]})
        self.assertEqual(list(self.project.iterdir()),[])
    def test_agent_artifact_emits_success_manifest(self):
        result=self.execute('generate_artifact',{'path':'notes.pdf','kind':'pdf','content':'# Notes\n\nA complete test file.'})
        self.assertEqual(result['kind'],'pdf')
        event=self.app.store.events(self.s)[-1]
        self.assertEqual(event['type'],'artifact');self.assertEqual(event['kind'],'pdf')

    def test_workbook_level_chart_is_not_silently_ignored(self):
        result=self.execute('generate_artifact',{'path':'root-chart.xlsx','kind':'xlsx','content':{'sheets':[{'name':'Data','rows':[['Name','Value'],['A',3],['B',5]]}],'charts':[{'type':'bar','labels':'A','values':['B']}]}})
        self.assertEqual(result['details']['charts'],1)
        self.assertEqual(result['warnings'],[])

    def test_nested_zip_paths_cannot_escape(self):
        with self.assertRaises(ValueError):produce_artifact(self.project,{'path':'bundle.zip','kind':'zip','content':[{'path':'../escape.txt','kind':'text','content':'bad'}]})

if __name__=='__main__':unittest.main()
