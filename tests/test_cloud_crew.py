"""Cloud coordinator and avatar boundaries. All model/transport replies are fixtures."""
import base64,io,json,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from beam_core import Agent,Run,Store
from crew import Crew
from crew_avatar import avatar
from cloud_client import CrewRouter,CloudTransferReview,review_transfer
sys.path.insert(0,str(Path(__file__).parents[1]/'worker'))
import runner
from crew_daemon import Service

def until(fn):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        result=fn()
        if result:return result
        time.sleep(.02)
    raise AssertionError('Background job did not reach expected state')

class Response:
    status_code=200
    def __init__(self,message):self.message=message
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def close(self):pass
    def iter_lines(self):yield json.dumps({'message':self.message,'done':True}).encode()

def tool(name,**args):return {'function':{'name':name,'arguments':args}}

class AvatarTests(unittest.TestCase):
    def test_shapes_and_uploads_validate_and_strip_metadata(self):
        self.assertEqual(avatar({'shape':'heart','color':'#aabbcc','eyes':'round'})['shape'],'heart')
        from PIL import Image,PngImagePlugin
        data=io.BytesIO();meta=PngImagePlugin.PngInfo();meta.add_text('Private','location')
        Image.new('RGB',(300,120),'red').save(data,format='PNG',pnginfo=meta)
        result=avatar({'kind':'image','image':'data:image/png;base64,'+base64.b64encode(data.getvalue()).decode()})
        with Image.open(io.BytesIO(base64.b64decode(result['image'].split(',')[1]))) as picture:
            self.assertEqual(picture.size,(256,256));self.assertNotIn('Private',picture.info)
        for value in ({'kind':'image','image':'https://example.com/a.png'},{'kind':'image','image':'data:image/svg+xml;base64,AAAA'},{'shape':'<script>'}):
            with self.subTest(value=value),self.assertRaises(ValueError):avatar(value)
    def test_profile_avatar_migrates_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);agent=Agent(store);crew=Crew(store,agent)
            self.assertNotEqual(crew._bot('lead')['avatar']['shape'],crew._bot('engineer')['avatar']['shape'])
            saved=crew.save_bot({'id':'researcher','avatar':{'shape':'flower','color':'#aabbcc','eyes':'sleepy'}})
            crew.close();crew=Crew(store,agent)
            self.assertEqual(crew._bot('researcher')['avatar'],saved['avatar'])
            crew.close();agent.remote.close();agent.servers.close();agent.integrations.close();store.db.close()
    def test_private_cloud_transfer_requires_explicit_decision(self):
        for data in ({'prompt':'My IP is 10.23.45.67'},{'memory':'Contact me at owner@example.com'}):
            with self.assertRaises(CloudTransferReview):review_transfer({},data)
            review_transfer({'cloud_transfer_consent':True},data)
        review_transfer({}, {'prompt':'Research network performance'})

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.previous_base=runner.BASE
        self.service=Service(self.root/'service',self.root/'projects')
        local=self.root/'local';local.mkdir();self.store=Store(self.root/'desktop');self.agent=Agent(self.store);self.local=Crew(self.store,self.agent);self.router=CrewRouter(self.local,self.agent)
        self.pid=self.store.add_project(local)['id'];self.cloud=self.agent.remote.cloud;self.remote=self.agent.remote
        with self.store.lock,self.store.db:self.store.db.execute('INSERT INTO remote_projects VALUES (?,?,?,?)',(self.pid,'fixture-codespace',1,1))
        self.transport=patch.object(self.remote,'rpc_name',side_effect=lambda name,pid,op,args,**kw:self.call(op,args,pid));self.transport.start()
    def call(self,op,args=None,pid=None):return self.service.dispatch({'project_id':pid or self.pid,'operation':op,'arguments':args or {}})
    def enable(self):
        with patch.object(self.store,'key',return_value='ollama-test-key'),patch.object(self.remote,'cli',return_value='github-test-token'):
            return self.cloud.enable(self.pid,self.agent,self.local)
    def tearDown(self):
        self.transport.stop();self.local.close();self.agent.remote.close();self.agent.servers.close();self.agent.integrations.close();self.store.db.close()
        self.service.close()
        for run in self.service.agent.runs.values():
            if run.worker:run.worker.join(4)
        self.service.store.db.close();runner.BASE=self.previous_base;self.temp.cleanup()
    def test_enable_state_and_credentials_stay_outside_task_folder(self):
        self.enable();state=self.router.state(self.pid)
        self.assertTrue(state['cloud']);self.assertTrue(state['trusted']);self.assertTrue(self.remote.target(self.pid)['cloud_crew'])
        self.assertNotIn('ollama-test-key',json.dumps(state));self.assertNotIn('github-test-token',json.dumps(state))
        self.assertFalse(self.service.credentials.is_relative_to(runner.project(self.pid)))
        self.cloud.disable(self.pid);self.assertFalse(self.service.credentials.exists());self.assertFalse(self.cloud.link(self.pid))
    def test_cloud_task_continues_after_desktop_services_close(self):
        self.enable();entered=threading.Event();release=threading.Event();payloads=[]
        def post(url,**kw):
            payloads.append(kw['json']);entered.set();release.wait(5)
            return Response({'tool_calls':[tool('write_file',path='report.txt',content='background output')]} if len(payloads)==1 else {'content':'Complete.'})
        with patch('beam_core.requests.post',side_effect=post):
            task=self.router.create_task(self.pid,{'prompt':'Write a plain report.','bot_id':'writer','mode':'edit','effort':'low'})
            self.assertTrue(entered.wait(5));self.local.close();self.remote.close();release.set()
            until(lambda:self.call('cloud_task',{'task_id':task['id']})['task']['status']=='done')
        self.assertEqual((runner.project(self.pid)/'report.txt').read_text(),'background output')
        self.assertFalse((self.root/'local/report.txt').exists())
        self.assertEqual(payloads[0]['model'],'beam');names=[t['function']['name'] for t in payloads[0]['tools']]
        self.assertNotIn('browser_open',names);self.assertNotIn('mcp_call',names);self.assertNotIn('slides_get',names)
    def test_task_ownership_and_active_disconnect_are_checked(self):
        self.enable();task=self.router.create_task(self.pid,{'prompt':'Inspect','start':False})
        other='b'*32;self.call('cloud_enable',{'ollama_key':'fixture','github_token':'fixture','codespace':'fixture-codespace'},other)
        with self.assertRaisesRegex(ValueError,'another project'):self.call('cloud_task',{'task_id':task['id']},other)
        self.service.crew._update(task['id'],status='waiting_input')
        with self.assertRaisesRegex(ValueError,'Finish or stop'):self.cloud.disable(self.pid)
    def test_sensitive_prompt_stays_desktop_before_codespace_transfer(self):
        self.enable()
        with self.assertRaises(CloudTransferReview):self.router.create_task(self.pid,{'prompt':'My IP is 10.33.55.77'})
        self.assertEqual(self.call('cloud_state')['tasks'],[])
    def test_switching_local_restores_local_tools_and_cloud_cannot_silently_follow(self):
        self.enable()
        with self.assertRaisesRegex(ValueError,'Disconnect Cloud Crew'):self.remote.configure(self.pid,{'target':'local'})
        self.cloud.disable(self.pid);self.remote.configure(self.pid,{'target':'local'})
        sid=self.store.create_session(self.pid)['id'];result=self.agent.execute(sid,Run(),self.root/'local','edit','write_file',{'path':'local.txt','content':'on PC'})
        self.assertTrue(result['changed']);self.assertTrue((self.root/'local/local.txt').exists());self.assertFalse((runner.project(self.pid)/'local.txt').exists())
    def test_disconnected_cache_has_no_working_indicator_or_pending_action(self):
        self.enable();task=self.router.create_task(self.pid,{'prompt':'Inspect','start':False})
        self.cloud.task(task['id']);self.remote.paused.add('fixture-codespace');self.cloud.survey['fixture-codespace']=time.monotonic()+60
        state=self.cloud.state(self.pid);detail=self.cloud.task(task['id'])
        self.assertTrue(state['disconnected']);self.assertTrue(detail['disconnected']);self.assertFalse(detail['run']['working']);self.assertIsNone(detail['pending'])
    def test_cloud_privacy_waits_and_local_summary_is_never_uploaded(self):
        self.enable();ip='10.33.45.67';marker='THIS_SUMMARY_STAYS_ON_PC';payloads=[]
        (runner.project(self.pid)/'network.txt').write_text(ip)
        def post(url,**kw):
            payloads.append(kw['json']);return Response({'tool_calls':[tool('read_file',path='network.txt')]} if len(payloads)==1 else {'content':'Done.'})
        with patch('beam_core.requests.post',side_effect=post):
            task=self.router.create_task(self.pid,{'prompt':'Read the project network file.','bot_id':'engineer','mode':'edit','effort':'low'})
            detail=until(lambda:(d if (d:=self.call('cloud_task',{'task_id':task['id']}))['task']['status']=='privacy' else None))
            self.assertFalse(detail['run']['working']);self.assertEqual(len(payloads),1)
            pending=detail['pending'];self.assertIsNotNone(pending)
            with patch.object(self.agent.privacy,'_model',return_value='local'),patch.object(self.agent.privacy,'_summarize',return_value=marker):
                self.cloud.privacy(task['session_id'],{'action_id':pending['action_id'],'choice':'local','model':'local'},self.agent)
            until(lambda:self.call('cloud_task',{'task_id':task['id']})['task']['status']=='done')
        self.assertNotIn(ip,json.dumps(payloads));self.assertNotIn(marker,json.dumps(payloads))
        remote_events=self.call('cloud_task',{'task_id':task['id']})['events'];self.assertNotIn(marker,json.dumps(remote_events))
        self.assertIn(marker,json.dumps(self.cloud.task(task['id'])['events']))
    def test_incremental_cached_events_do_not_duplicate_or_lose_cursor(self):
        self.enable();task=self.router.create_task(self.pid,{'prompt':'Inspect','start':False});sid=task['session_id']
        self.service.store.emit(sid,'notice',text='first');first=self.cloud.task(task['id']);cursor=first['events'][-1]['seq']
        self.service.store.emit(sid,'notice',text='second');second=self.cloud.task(task['id'],cursor)
        self.assertEqual([e['text'] for e in second['events']],['second'])
        cached=self.cloud.cached('task:'+task['id']);self.assertEqual(len({e['seq'] for e in cached['events']}),len(cached['events']))

if __name__=='__main__':unittest.main()
