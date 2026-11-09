import json,struct,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image
from beam_core import Agent,Run,Store
from cad_templates import model_spec
from enhanced_artifacts import produce_artifact
from artifact_quality import inspect_file
import cad_engine,mesh_engine
from desktop import create_app

class Release4Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.project=self.root/'project';self.project.mkdir();self.store=Store(self.root/'data');self.agent=Agent(self.store);p=self.store.add_project(self.project);self.sid=self.store.create_session(p['id'])['id']
 def tearDown(self):self.agent.integrations.close();self.agent.servers.close();self.store.db.close();self.temp.cleanup()
 def make(self,name,kind,content):
  with patch('cad_engine._render_view',return_value=Image.new('RGB',(64,64))):return produce_artifact(self.project,{'path':name,'kind':kind,'content':content})
 def test_activity_only_reports_actual_running_work(self):
  run=Run();self.agent.runs[self.sid]=run;run.activity('building','Building model');self.assertTrue(self.agent.run_state(self.sid)['working'])
  for status in ('approval','waiting_input','done','failed','needs_attention','stopped'):
   run.status=status;self.assertFalse(self.agent.run_state(self.sid)['working'])
  run.status='running';run.stop();self.assertEqual(self.agent.run_state(self.sid)['status'],'stopping');self.assertFalse(self.agent.run_state(self.sid)['working'])
 def test_dead_worker_cannot_remain_working(self):
  run=Run();worker=threading.Thread(target=lambda:None);worker.start();worker.join();run.worker=worker;self.agent.runs[self.sid]=run;self.assertEqual(self.agent.run_state(self.sid)['status'],'failed')
 def test_worker_setup_error_is_finalized(self):
  run=Run()
  with patch.object(self.agent,'_work',side_effect=RuntimeError('setup failed')):self.agent._work_guarded(self.sid,[],'edit','fake',run)
  self.assertEqual(run.status,'failed');self.assertEqual(self.store.events(self.sid)[-1]['type'],'done')
 def test_ask_user_stops_agent_loop(self):
  class Response:
   status_code=200
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def iter_lines(self):yield json.dumps({'message':{'tool_calls':[{'function':{'name':'ask_user','arguments':{'question':'Which dimensions?','options':['Small','Large']}}}]},'done':True}).encode()
  run=Run()
  with patch('beam_core.requests.post',return_value=Response()) as call:self.agent._work(self.sid,[{'role':'user','content':'build a case'}],'edit','fake',run)
  self.assertEqual(run.status,'waiting_input');self.assertFalse(run.snapshot()['working']);self.assertEqual(call.call_count,1);self.assertTrue(any(e['type']=='question' for e in self.store.events(self.sid)))
 def test_units_convert_without_changing_intended_size(self):
  result=self.make('inch.glb','glb',{'units':'in','tree':{'shape':'box','size':[2,1,.5]},'constraints':{'size_mm':[50.8,25.4,12.7],'tolerance_mm':.001}})
  np.testing.assert_allclose(result['details']['size_mm'],[50.8,25.4,12.7]);self.assertEqual(len(result['details']['dimension_checks']),3)
 def test_large_model_is_not_silently_rescaled(self):
  solid,notes,info=cad_engine.build_program({'shape':'box','size':[1200,20,10]});self.assertEqual(info['size'][0],1200);self.assertFalse(any('rescaled to' in v for v in notes))
 def test_dimension_mismatch_leaves_no_outputs(self):
  with self.assertRaisesRegex(ValueError,'requested'):self.make('bad.stl','stl',{'tree':{'shape':'box','size':[20,10,5]},'constraints':{'size_mm':[30,10,5]}})
  self.assertEqual(list(self.project.iterdir()),[])
 def test_new_presets_match_size_and_components(self):
  for preset,components,size in [('lidded_enclosure',2,[100,70,50]),('planter',1,[90,90,100])]:
   with self.subTest(preset=preset):
    result=self.make(preset+'.stl','stl',model_spec(preset,{}));self.assertEqual(result['details']['components'],components);np.testing.assert_allclose(result['details']['size_mm'],size,atol=.15)
    self.assertTrue((self.project/result['details']['dimension_sheet']).is_file())
 def test_cancelled_build_never_publishes_files(self):
  cancel=threading.Event()
  def progress(phase,label):
   if phase=='validating':cancel.set()
  with self.assertRaisesRegex(ValueError,'stopped'):produce_artifact(self.project,{'path':'cancelled.pdf','kind':'pdf','content':'# A real report'},cancelled=cancel,progress=progress)
  self.assertEqual(list(self.project.iterdir()),[])
 def test_same_basename_office_formats_do_not_conflict(self):
  self.make('report.docx','docx','# A report\n\nComplete contents.')
  self.make('report.pdf','pdf','# A report\n\nComplete contents.')
  self.assertTrue((self.project/'report.docx-quality.json').is_file());self.assertTrue((self.project/'report.pdf-quality.json').is_file())
 def test_a4_documents_preserve_real_page_size(self):
  self.make('a4.pdf','pdf',{'page_size':'A4','markdown':'# A4 report\n\nA complete report.'})
  from pypdf import PdfReader
  self.assertAlmostEqual(float(PdfReader(self.project/'a4.pdf').pages[0].mediabox.width),595.276,places=2)
 def test_enclosure_vents_remove_real_material(self):
  a,_,_=cad_engine.build_program(model_spec('lidded_enclosure',{'vent_count':0}));b,_,_=cad_engine.build_program(model_spec('lidded_enclosure',{'vent_count':6}));self.assertGreater(a.volume()-b.volume(),1000)
 def test_planter_has_actual_drainage_through_the_floor(self):
  solid,_,_=cad_engine.build_program(model_spec('planter',{'drain_count':5}));self.assertEqual(solid.genus(),5)
  fewer,_,_=cad_engine.build_program(model_spec('planter',{'drain_count':1}));self.assertGreater(fewer.volume()-solid.volume(),200)
 def test_assembly_has_bom_and_correct_print_orientation(self):
  result=self.make('case.stl','stl',model_spec('lidded_enclosure',{}));parts=result['details']['assembly_parts'];self.assertEqual(len(parts),2);self.assertAlmostEqual(parts[1]['size_mm'][2],3,places=4);self.assertTrue((self.project/result['details']['bill_of_materials']).read_text('utf-8-sig').startswith('Part,Quantity'))
 def test_glb_preserves_sharp_box_edges_and_material(self):
  result=self.make('red.glb','glb',{'tree':{'shape':'box','size':[20,10,5]},'material':{'color':'#FF0000'}});data=(self.project/'red.glb').read_bytes();n=struct.unpack_from('<I',data,12)[0];doc=json.loads(data[20:20+n]);self.assertEqual(doc['materials'][0]['pbrMetallicRoughness']['baseColorFactor'],[1,0,0,1]);self.assertEqual(doc['accessors'][0]['count'],24);self.assertEqual(mesh_engine.read_glb(self.project/'red.glb').validate()['components'],1)
 def test_quality_changes_curve_tessellation(self):
  draft=self.make('draft.stl','stl',{'quality':'draft','tree':{'shape':'cylinder','radius':30,'height':10}});high=self.make('high.stl','stl',{'quality':'high','tree':{'shape':'cylinder','radius':30,'height':10}});self.assertGreater(high['details']['triangles'],draft['details']['triangles'])
 def test_broken_formula_is_rejected_atomically(self):
  with self.assertRaisesRegex(ValueError,'Broken formula'):self.make('bad.xlsx','xlsx',{'sheets':[{'name':'Data','rows':[['Value'],['=#REF!+1']]}]})
  self.assertEqual(list(self.project.iterdir()),[])
 def test_bad_chart_values_are_rejected(self):
  with self.assertRaisesRegex(ValueError,'numeric'):self.make('bad.xlsx','xlsx',{'sheets':[{'rows':[['Label','Count'],['A','text']],'charts':[{'values':['B']}]}]})
 def test_process_slide_and_theme_are_real_editable_shapes(self):
  result=self.make('flow.pptx','pptx',{'theme':{'accent':'446688'},'slides':[{'layout':'process','title':'How it works','steps':[{'title':'Plan','body':'Choose a useful goal.'},{'title':'Build','body':'Make the output.'},{'title':'Review','body':'Check the result.'}]}]});report=inspect_file(self.project/'flow.pptx');self.assertEqual(report['metrics']['slides'],1);self.assertIn('Choose a useful goal.',' '.join(report['slides'][0]['text']))
 def test_document_preview_contains_tables_and_checked_manifest(self):
  result=self.make('notes.docx','docx',{'title':'Project notes','markdown':'## Checks\n\n| Item | Result |\n|---|---|\n| Export | Passed |'});report=inspect_file(self.project/'notes.docx');self.assertEqual(report['metrics']['tables'],1);self.assertIn('notes.docx-quality.json',result['companions']);self.assertEqual(len(result['quality']['sha256']),64)
 def test_inspect_endpoint_has_no_html_execution(self):
  self.make('deck.pptx','pptx',{'slides':[{'title':'<script>unsafe()</script>','bullets':['Plain data']}]});app=create_app(self.root/'app-data','test');project=app.store.add_project(self.project)
  try:
   r=app.test_client().get('/api/projects/'+project['id']+'/inspect-artifact?path=deck.pptx',headers={'X-Beam-Token':'test'});self.assertEqual(r.status_code,200);self.assertIn('slides',r.json);self.assertEqual(r.mimetype,'application/json')
  finally:app.agent.integrations.close();app.store.db.close()
if __name__=='__main__':unittest.main()
