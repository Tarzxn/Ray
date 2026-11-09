import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from beam_core import Run
from browser_tools import BrowserService
from desktop import create_app
from model_pipeline import build_files
import mesh_engine
import models


class RayTests(unittest.TestCase):
    def test_auto_selects_by_effort_and_only_uses_available_models(self):
        with patch.object(models, 'live_ids', return_value={'gpt-oss:20b','qwen3.5:397b','glm-5.2'}):
            self.assertEqual(models.route('build a server', 'low', 'fixture'), 'gpt-oss:20b')
            self.assertEqual(models.route('build a server', 'high', 'fixture'), 'qwen3.5:397b')
            self.assertEqual(models.route('build a server', 'max', 'fixture'), 'glm-5.2')

    def test_always_approve_requires_review_for_sensitive_tool_args(self):
        with TemporaryDirectory() as folder:
            app = create_app(Path(folder), cloud=True)
            try:
                run = Run()
                with patch.object(app.agent, 'request_approval', return_value=False) as approval, \
                     patch.object(app.agent, '_execute_tool', return_value={'ok':True}) as execute:
                    self.assertTrue(app.agent.execute('test', run, Path(folder), 'auto', 'web_search', {'query':'Python docs'})['ok'])
                    args = {'query':'Find pat@example.com'}
                    self.assertTrue(app.agent.execute('test', run, Path(folder), 'auto', 'web_search', args)['denied'])
                    approval.assert_called_once_with('test',run,'web_search',{'privacy_categories':['Email address']})
                    self.assertEqual(execute.call_count, 1)
            finally:
                app.lifecycle.closed.set()
                app.crew.close()
                app.store.db.close()

    def test_manual_browser_lease_blocks_agent_and_accepts_user_input(self):
        with TemporaryDirectory() as folder:
            config = Mock(store=SimpleNamespace(root=Path(folder)))
            config.read.return_value = {}
            browser = BrowserService(config, cloud=True)
            browser._ensure = Mock()
            browser._page = Mock(url='about:blank')
            browser._page.is_closed.return_value = False
            browser._page.screenshot.return_value = b'jpeg'
            try:
                with self.assertRaisesRegex(ValueError, 'Take over'):
                    browser.call('input', manual=True, action='click', x=10, y=20)
                browser.call('control', manual=True, enabled=True)
                with self.assertRaisesRegex(ValueError, 'user is controlling'):
                    browser.call('snapshot')
                browser.call('input', manual=True, action='click', x=10, y=20)
                browser.call('input', manual=True, action='text', text='hello')
                browser._page.mouse.click.assert_called_once_with(10,20)
                browser._page.keyboard.insert_text.assert_called_once_with('hello')
                self.assertEqual(browser.call('frame', manual=True), b'jpeg')
                browser.call('control', manual=True, enabled=False)
                self.assertFalse(browser.state()['manual'])
            finally:
                browser.close()
                browser.thread.join(2)

    def test_private_preview_requires_a_registered_port_and_browser_context(self):
        with TemporaryDirectory() as folder:
            app = create_app(Path(folder), cloud=True)
            config = app.agent.integrations.config
            try:
                config.preview_allowed = lambda port: port == 8080
                self.assertEqual(config.validate('http://127.0.0.1:8080', preview=True), 'http://127.0.0.1:8080')
                for url, preview in [('http://127.0.0.1:8080', False), ('http://127.0.0.1:8888', True)]:
                    with self.assertRaises(ValueError):
                        config.validate(url, preview=preview)
            finally:
                app.lifecycle.closed.set()
                app.crew.close()
                app.store.db.close()

    def test_named_assembly_exports_and_rejects_interference(self):
        parts = [{'shape':'box','size':[10,10,5]}, {'shape':'box','size':[10,10,5],'position':[11,0,0]}]
        spec = {
            'tree':{'op':'union','children':parts},
            'allow_multiple':True,
            'constraints':{'max_size_mm':[22,None,None]},
            'assembly':{'parts':['base','lid'],'exploded_view':True,'export_individual_parts':True,
                        'checks':[{'a':'base','b':'lid','min_gap_mm':0.8}]},
        }
        with TemporaryDirectory() as folder:
            target = Path(folder)/'assembly.stl'
            build_files(target,spec)
            report = json.loads(target.with_name('assembly-validation.json').read_text('utf-8'))
            self.assertEqual(report['assembly_checks'][0]['overlap_mm3'],0)
            self.assertEqual(len(report['assembly_parts']),2)
            exploded = mesh_engine.read_glb(Path(folder)/report['exploded_view']).validate(connected=False)
            self.assertEqual(exploded['components'],2)
            self.assertGreater(exploded['size_mm'][0],report['size_mm'][0])
            parts[1]['position'][0] = 8
            with self.assertRaisesRegex(ValueError, 'Fit check failed'):
                build_files(Path(folder)/'collision.stl',spec)
