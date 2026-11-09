import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image
from beam_core import Agent,Run,Store,EFFORTS
from agent_runtime import ManagedServers,run_command,validate_command,clean_environment
from cad_templates import model_spec
from enhanced_artifacts import produce_artifact
import cad_engine

class UpgradeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.project=self.root/'project';self.project.mkdir();self.store=Store(self.root/'data');self.agent=Agent(self.store);self.p=self.store.add_project(self.project);self.sid=self.store.create_session(self.p['id'])['id']
 def tearDown(self):self.agent.servers.close();self.store.db.close();self.temp.cleanup()
 def execute(self,name,args):return self.agent.execute(self.sid,Run(),self.project,'edit',name,args)
 def test_effort_setting_persists_across_store_reopen(self):
  self.store.save_settings({'effort':'max'})
  reopened=Store(self.store.root);self.assertEqual(reopened.settings()['effort'],'max');reopened.db.close()
 def test_invalid_python_never_saved(self):
  with self.assertRaisesRegex(ValueError,'syntax error'):self.execute('write_file',{'path':'app.py','content':'f"{ {\'done\': 1 }"'})
  self.assertFalse((self.project/'app.py').exists())
 def test_missing_project_is_rejected(self):
  self.project.rmdir()
  with patch.object(self.store,'key',return_value='test'),self.assertRaises(ValueError):self.agent.start(self.sid,'do work')
 def test_effort_budgets_increase(self):
  levels=[EFFORTS[x] for x in ['low','medium','high','max']]
  self.assertEqual([c['steps'] for c in levels],sorted(c['steps'] for c in levels));self.assertGreater(levels[-1]['tokens'],levels[0]['tokens']);self.assertFalse(levels[0]['think']);self.assertTrue(levels[-1]['think'])
 def test_live_request_uses_selected_effort(self):
  class Response:
   status_code=200
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def iter_lines(self):yield json.dumps({'message':{'content':'Done'},'done':True}).encode()
  for effort in ('low','max'):
   run=Run();run.effort=effort
   with patch('beam_core.requests.post',return_value=Response()) as call:self.agent._work(self.sid,[{'role':'user','content':'say done'}],'plan','test',run)
   payload=call.call_args.kwargs['json'];self.assertEqual(payload['options']['num_predict'],EFFORTS[effort]['tokens']);self.assertEqual(payload['think'],EFFORTS[effort]['think'])
 def test_repeated_failure_pauses(self):
  class Response:
   status_code=200
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def iter_lines(self):yield json.dumps({'message':{'content':'','tool_calls':[{'function':{'name':'read_file','arguments':{'path':'missing.txt'}}}]},'done':True}).encode()
  run=Run()
  with patch('beam_core.requests.post',side_effect=lambda *a,**kw:Response()) as call:self.agent._work(self.sid,[{'role':'user','content':'test'}],'plan','test',run)
  self.assertEqual(call.call_count,3);self.assertEqual(run.status,'needs_attention')
 @unittest.skipUnless(os.name=='nt','Windows shell')
 def test_unix_commands_and_global_kills_are_rejected(self):
  for command in ['python3 --version','which ollama','ls -la','python app.py 2>/dev/null','Get-Process python | Stop-Process -Force','Start-Process python app.py']:
   with self.subTest(command=command),self.assertRaises(ValueError):validate_command(command)
 def test_managed_server_logs_and_stop(self):
  command='python -u -c "import time; print(123,flush=True); time.sleep(30)"'
  server=self.agent.servers.start(command,self.project)
  for i in range(60):
   status=self.agent.servers.status(server['id'])
   if '123' in status['output']:break
   time.sleep(.05)
  self.assertTrue(status['running']);self.assertIn('123',status['output'])
  self.assertFalse(self.agent.servers.stop(server['id'])['running'])
 def test_generated_boolean_aliases_recover(self):
  for spec in [{'shape':'union','children':[{'shape':'box','size':[10,10,10]}]}, {'boolean':'difference','base':{'shape':'box','size':[10,10,10]},'cutters':[{'shape':'cylinder','radius':1,'height':14}]}]:
   solid,_,_=cad_engine.build_program({'tree':spec});self.assertGreater(solid.volume(),0)
 def test_presets_match_dimensions_and_closed_topology(self):
  for preset,size in [('enclosure',[100,70,50]),('bracket',[100,70,50]),('vase',[70,70,120]),('finger_joint_box',[100,70,50])]:
   with self.subTest(preset=preset),patch('cad_engine._render_view',return_value=Image.new('RGB',(32,32))):
    result=produce_artifact(self.project,{'path':preset+'.stl','kind':'stl','content':model_spec(preset,{})})
    details=result['details'];self.assertTrue(details['watertight']);self.assertEqual(details['components'],5 if preset=='finger_joint_box' else 1)
    for actual,expected in zip(details['size_mm'],size):self.assertAlmostEqual(actual,expected,places=3)
 def test_missing_dimensions_do_not_silently_use_defaults(self):
  with self.assertRaisesRegex(ValueError,'depth'):self.execute('generate_model',{'path':'bad.stl','preset':'finger_joint_box','parameters':{'width':84,'height':48,'wall':3}})
 def test_finger_box_has_separate_flat_panel_exports(self):
  with patch('cad_engine._render_view',return_value=Image.new('RGB',(32,32))):r=produce_artifact(self.project,{'path':'box.stl','kind':'stl','content':model_spec('finger_joint_box',{})})
  import zipfile
  self.assertEqual(len(r['details']['assembly_parts']),5)
  with zipfile.ZipFile(self.project/r['details']['parts_archive']) as archive:self.assertEqual(len(archive.namelist()),10)
  for part in r['details']['assembly_parts']:self.assertAlmostEqual(part['size_mm'][2],3,places=3)
 def test_invalid_wall_is_rejected(self):
  with self.assertRaises(ValueError):model_spec('enclosure',{'wall':12,'width':30})
 def test_generate_model_emits_real_manifest(self):
  with patch('cad_engine._render_view',return_value=Image.new('RGB',(32,32))):r=self.execute('generate_model',{'path':'case.stl','preset':'enclosure','parameters':{'width':80,'depth':70,'height':50,'wall':3}})
  self.assertEqual(r['details']['size_mm'][0],80);self.assertEqual(self.store.events(self.sid)[-1]['type'],'artifact')
 def test_frozen_environment_is_cleaned(self):
  import sys
  from unittest.mock import patch
  with patch.object(sys,'frozen',True,create=True),patch.object(sys,'_MEIPASS',str(self.root/'bundle'),create=True),patch.dict(os.environ,{'PATH':str(self.root/'bundle'/'pywin32_system32')+os.pathsep+'C:/Windows','PYTHONHOME':'wrong'}):
   env=clean_environment();self.assertNotIn('bundle',env['PATH']);self.assertNotIn('PYTHONHOME',env)

if __name__=='__main__':unittest.main()
