import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch,Mock
from urllib.parse import parse_qs,urlparse
import urllib.request
import urllib.error
from beam_core import Agent,Run,Store
from command_policy import reusable_check
from desktop import create_app
from web_tools import ConnectionConfig,WebTools,validate_url,public_get
from google_tools import GoogleConnection

class Response:
 def __init__(self,data=None,status=200,headers=None,body=b''):
  self.data=data or {};self.status_code=status;self.ok=status<400;self.headers=headers or {};self.encoding='utf-8';self.body=body;self.closed=False
 def __enter__(self):return self
 def __exit__(self,*args):self.close()
 def close(self):self.closed=True
 def json(self):return self.data
 def raise_for_status(self):
  if not self.ok:raise ValueError('HTTP '+str(self.status_code))
 def iter_content(self,size):yield self.body

class IntegrationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.project=self.root/'project';self.project.mkdir();self.store=Store(self.root/'data');self.agent=Agent(self.store);self.config=self.agent.integrations.config;self.google=self.agent.integrations.google;self.p=self.store.add_project(self.project);self.sid=self.store.create_session(self.p['id'])['id']
 def tearDown(self):
  self.agent.integrations.close();self.agent.servers.close();time.sleep(.05);self.store.db.close();self.temp.cleanup()
 def execute(self,name,args,run=None,mode='edit'):
  return self.agent.execute(self.sid,run or Run(),self.project,mode,name,args)
 def client(self):self.google.configure({'installed':{'client_id':'test.apps.googleusercontent.com','client_secret':'client-secret'}})
 def test_project_overview_and_file_sensitive_reuse(self):
  p=self.project/'app.py';p.write_text('a=1');run=Run()
  result=self.execute('inspect_project',{},run);self.assertIn('app.py',result['entry_points']);self.assertIn('runtime',result)
  self.assertTrue(self.execute('inspect_project',{},run)['cached'])
  p.write_text('a=123');self.assertNotIn('cached',self.execute('inspect_project',{},run))
 def test_passed_check_not_rerun_until_file_change(self):
  run=Run();args={'command':'python -m unittest discover'}
  with patch.object(self.agent,'request_approval',return_value=True) as approval,patch('beam_core.run_command',return_value={'exit_code':0,'status':'Finished','output':'OK'}) as command:
   self.execute('run_command',args,run);self.assertTrue(self.execute('run_command',args,run)['cached']);self.assertEqual(command.call_count,1);self.assertEqual(approval.call_count,1);self.assertEqual(run.command_count,1)
   (self.project/'app.py').write_text('x=1');self.execute('run_command',args,run);self.assertEqual(command.call_count,2)
 def test_mutating_commands_are_never_reused(self):
  run=Run()
  with patch.object(self.agent,'request_approval',return_value=True),patch('beam_core.run_command',return_value={'exit_code':0,'status':'Finished'}) as command:
   for _ in range(2):self.execute('run_command',{'command':'Write-Output hello'},run)
   self.assertEqual(command.call_count,2)
  for text in ('npm run test -- --update','python -m unittest; Write-Output hi','python -c "print(1)"'):
   self.assertFalse(reusable_check(text))
 def test_failed_check_is_not_cached(self):
  run=Run()
  with patch.object(self.agent,'request_approval',return_value=True),patch('beam_core.run_command',return_value={'exit_code':1,'status':'Finished'}) as command:
   for _ in range(2):self.execute('run_command',{'command':'python -m unittest'},run)
   self.assertEqual(command.call_count,2)
 def test_command_limit_stops_before_approval(self):
  run=Run();run.command_count=run.command_budget
  with patch.object(self.agent,'request_approval') as approval,self.assertRaisesRegex(ValueError,'budget reached'):
   self.execute('run_command',{'command':'Write-Output hi'},run)
  approval.assert_not_called();self.assertEqual(run.outcome,'needs_attention')
 def test_budget_settings_are_bounded(self):
  self.store.save_settings({'command_budget':4});self.assertEqual(self.store.settings()['command_budget'],4)
  for value in (0,41):
   with self.assertRaises(ValueError):self.store.save_settings({'command_budget':value})
 def test_dedicated_web_does_not_spend_command_budget(self):
  run=Run()
  with patch.object(self.agent.integrations.web,'search',return_value={'results':[]}):self.execute('web_search',{'query':'docs'},run)
  self.assertEqual(run.command_count,0)
 def test_plan_blocks_external_writes(self):
  for name,args in [('browser_action',{'action':'click'}),('slides_create',{'title':'Deck'}),('slides_update',{'presentation':'abcde','requests':[{}],'revision':'r'}),('mcp_call',{'server':'x','tool':'a','arguments':{}}),('save_browser_download',{'id':'a','path':'a.txt'})]:
   with self.subTest(name=name),self.assertRaisesRegex(ValueError,'Plan mode'):self.execute(name,args,mode='plan')
 def test_external_writes_always_require_approval(self):
  with patch.object(self.agent,'request_approval',return_value=False) as approval:
   self.assertTrue(self.execute('slides_create',{'title':'Deck'})['denied']);approval.assert_called_once()
 def test_local_and_unsafe_urls_rejected(self):
  for url in ('file:///c:/secret','https://user:pw@example.com','http://127.0.0.1','http://[::1]/','http://192.168.1.2','http://localhost','http://169.254.169.254'):
   with self.subTest(url=url),self.assertRaises(ValueError):validate_url(url)
  self.assertEqual(validate_url('http://localhost:1234',True),'http://localhost:1234')
 def test_beam_control_url_still_blocked_when_local_allowed(self):
  self.config.save({'browser_allow_local':True});self.config.blocked_origins.add('http://127.0.0.1:1234')
  with self.assertRaisesRegex(ValueError,'control interface'):self.config.validate('http://localhost:1234/api/state')
 def test_redirect_revalidation_blocks_private_target(self):
  response=Response(status=302,headers={'Location':'http://127.0.0.1/secret'})
  with patch('web_tools.requests.get',return_value=response),patch('web_tools.socket.getaddrinfo',return_value=[(None,None,None,None,('1.1.1.1',443))]),self.assertRaises(ValueError):public_get('https://example.com')
  self.assertTrue(response.closed)
 def test_web_page_filters_scripts_and_resolves_links(self):
  raw=b'<title>Test</title><script>secret()</script><h1>Hello</h1><a href="/docs">Docs</a>'
  with patch('web_tools.public_get',return_value=('https://example.com',raw,'text/html','utf-8')):result=self.agent.integrations.web.open('https://example.com')
  self.assertNotIn('secret()',result['text']);self.assertEqual(result['links'][0]['url'],'https://example.com/docs')
 def test_search_reuses_recent_results(self):
  with patch('ddgs.DDGS') as provider:
   provider.return_value.text.return_value=[{'title':'Docs','href':'https://example.com','body':'Source'}]
   self.agent.integrations.web.search('test');self.assertTrue(self.agent.integrations.web.search('test')['cached']);provider.return_value.text.assert_called_once()
 def test_credentials_are_encrypted_and_not_public(self):
  self.agent.integrations.save_settings({'search_key':'test-secret-search-key'})
  state=json.dumps(self.agent.integrations.state());self.assertNotIn('test-secret-search-key',state)
  self.assertNotIn('test-secret-search-key',self.config.vault.path.read_text());self.assertEqual(self.config.vault.get('search_key'),'test-secret-search-key')
 def test_google_requires_desktop_client(self):
  with self.assertRaisesRegex(ValueError,'Desktop'):self.google.configure({'web':{'client_id':'a'}})
  with self.assertRaisesRegex(ValueError,'Import'):self.google.start()
 def test_google_client_replacement_clears_old_token(self):
  self.client();self.config.vault.set('google_token',{'access_token':'old'});self.google.configure({'installed':{'client_id':'next.apps.googleusercontent.com'}});self.assertFalse(self.google.status()['connected'])
 def test_google_pkce_callback_state_and_encrypted_tokens(self):
  self.client();result=self.google.start();params=parse_qs(urlparse(result['authorization_url']).query);redirect=params['redirect_uri'][0]
  self.assertEqual(params['code_challenge_method'],['S256']);self.assertIn('drive.readonly',' '.join(params['scope']))
  try:urllib.request.urlopen(redirect+'?code=x&state=invalid',timeout=3)
  except urllib.error.HTTPError as error:error.close()
  else:self.fail('Invalid OAuth state was accepted')
  self.assertTrue(self.google.status()['pending'])
  with patch('google_tools.requests.post',return_value=Response({'access_token':'access-secret','refresh_token':'refresh-secret','expires_in':3600})) as exchange,patch('google_tools.requests.get',return_value=Response({'email':'tester@example.com'})):
   urllib.request.urlopen(redirect+'?code=test-code&state='+params['state'][0],timeout=3).close()
  verifier=exchange.call_args.kwargs['data']['code_verifier'];challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=');self.assertEqual(challenge,params['code_challenge'][0]);self.assertTrue(self.google.status()['connected']);self.assertNotIn('access-secret',self.config.vault.path.read_text())
 def test_cancelled_google_exchange_never_saves_token(self):
  self.client();result=self.google.start();params=parse_qs(urlparse(result['authorization_url']).query)
  def exchange(*args,**kwargs):
   self.google.disconnect();return Response({'access_token':'cancelled-token','expires_in':3600})
  with patch('google_tools.requests.post',side_effect=exchange),patch('google_tools.requests.get',return_value=Response()):urllib.request.urlopen(params['redirect_uri'][0]+'?code=test&state='+params['state'][0],timeout=3).close()
  self.assertFalse(self.google.status()['connected'])
 def test_google_refresh_preserves_refresh_token(self):
  self.client();self.config.vault.set('google_token',{'access_token':'expired','refresh_token':'refresh-secret','expires_at':0})
  with patch('google_tools.requests.post',return_value=Response({'access_token':'fresh','expires_in':3600})):self.assertEqual(self.google.token(),'fresh')
  self.assertEqual(self.config.vault.get('google_token')['refresh_token'],'refresh-secret')
 def test_slides_update_requires_revision_and_sends_write_control(self):
  with self.assertRaisesRegex(ValueError,'revision'):self.google.edit('abcde',[{'createSlide':{}}],'')
  with patch.object(self.google,'_request',return_value={}) as call:self.google.edit('https://docs.google.com/presentation/d/abcde/edit',[{'createSlide':{}}],'revision-2')
  self.assertEqual(call.call_args.kwargs['json']['writeControl'],{'requiredRevisionId':'revision-2'})
 def test_google_file_search_quotes_are_escaped(self):
  with patch.object(self.google,'_request',return_value={}) as call:self.google.files("Sam's deck")
  self.assertIn("Sam\\'s deck",call.call_args.kwargs['params']['q'])
 def test_mcp_config_keeps_bearer_and_environment_encrypted(self):
  hub=self.agent.integrations.mcp;entry=hub.save({'name':'Remote','transport':'http','url':'https://example.com/mcp','token':'mcp-secret-token'})
  self.assertNotIn('mcp-secret-token',json.dumps(hub.public()));self.assertNotIn('mcp-secret-token',self.config.path.read_text());self.assertEqual(self.config.vault.get('mcp-'+entry['id'])['token'],'mcp-secret-token')
  with self.assertRaisesRegex(ValueError,'encrypted'):hub.save({'name':'Bad','url':'https://example.com/mcp?api_key=secret'})
 def test_api_connections_workspace_and_private_guards(self):
  app=create_app(self.root/'api-data','test-token');client=app.test_client();headers={'X-Beam-Token':'test-token'}
  try:
   self.assertEqual(client.get('/api/connections').status_code,403)
   response=client.post('/api/connections',headers=headers,json={'search_key':'private-search-secret'});self.assertEqual(response.status_code,200);self.assertNotIn(b'private-search-secret',response.data)
   response=client.post('/api/workspace',headers=headers,json={});self.assertEqual(response.status_code,200);self.assertTrue(Path(response.json['path']).is_dir())
   self.assertEqual(client.post('/api/browser',headers=headers,json={'method':'shutdown'}).status_code,400)
  finally:app.agent.integrations.close();app.store.db.close()

if __name__=='__main__':unittest.main()
