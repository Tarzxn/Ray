"""The Render gateway (app.py): pages, static assets, sign-in rules and the Codespace worker kit."""
import ast
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import time
import unittest
import zipfile

from cryptography.fernet import Fernet

import app as gateway

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'x' * 40
CONFIG = {'BEAM_SESSION_KEY': SECRET, 'GITHUB_CLIENT_ID': 'id', 'GITHUB_CLIENT_SECRET': 'secret', 'PUBLIC_URL': 'https://beam.test'}


class FakePool:
    def __init__(self):
        self.closed = []

    def get(self, identity, start=True):
        return None

    def close(self, identity):
        self.closed.append(identity)


def signed_in_cookie():
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest()))
    identity = {'user': {'id': 7, 'login': 'tester'}, 'access_token': 'token', 'csrf': 'csrf-token',
                'expires_at': time.time() + 3600, 'refresh_token': 'r'}
    return cipher.encrypt(json.dumps(identity).encode()).decode()


class PageTests(unittest.TestCase):
    def setUp(self):
        self.app = gateway.create_app(CONFIG, FakePool())
        self.client = self.app.test_client()

    def signed_in(self):
        self.client.set_cookie('beam_cloud', signed_in_cookie())
        return self.client

    def test_health_reports_configured(self):
        data = self.client.get('/healthz').get_json()
        self.assertTrue(data['ok'] and data['configured'])

    def test_missing_setup_shows_a_page_not_a_crash(self):
        client = gateway.create_app({'BEAM_SESSION_KEY': '', 'GITHUB_CLIENT_ID': '', 'GITHUB_CLIENT_SECRET': '', 'PUBLIC_URL': ''}, FakePool()).test_client()
        self.assertFalse(client.get('/healthz').get_json()['configured'])
        page = client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'BEAM_SESSION_KEY', page.data)
        self.assertEqual(client.get('/api/state').status_code, 503)

    def test_signed_out_visitors_get_the_login_page_and_no_api(self):
        self.assertIn(b'GitHub', self.client.get('/').data)
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        self.assertEqual(self.client.post('/api/settings', json={}).status_code, 401)

    def test_signed_in_visitors_get_studio_with_every_script(self):
        page = self.signed_in().get('/').get_data(as_text=True)
        for script in ('app.js', 'beam-models.js'):
            self.assertIn('/static/' + script, page)
        self.assertIn('csrf-token', page)

    def test_posts_need_the_matching_origin_and_csrf_token(self):
        client = self.signed_in()
        self.assertEqual(client.post('/api/settings', json={}, headers={'Origin': 'https://evil.example', 'X-Beam-Token': 'csrf-token'}).status_code, 403)
        self.assertEqual(client.post('/api/settings', json={}, headers={'Origin': 'https://beam.test', 'X-Beam-Token': 'wrong'}).status_code, 403)

    def test_security_headers_block_inline_and_third_party_scripts(self):
        response = self.client.get('/healthz')
        policy = response.headers['Content-Security-Policy']
        self.assertIn("script-src 'self'", policy)
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

    def test_every_asset_a_template_references_is_served(self):
        seen = 0
        for template in (ROOT / 'templates').glob('*.html'):
            for url in re.findall(r'(?:src|href)="(/static/[^"]+)"', template.read_text(encoding='utf-8')):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, template.name + ' references ' + url)
                seen += 1
        self.assertGreaterEqual(seen, 3)

    def test_templates_do_not_reference_removed_front_ends(self):
        for template in (ROOT / 'templates').glob('*.html'):
            text = template.read_text(encoding='utf-8')
            self.assertNotIn('desktop_static', text)
            self.assertNotIn('cloud_static', text)

    def test_the_page_scripts_only_call_routes_the_gateway_can_relay(self):
        script = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8') + (ROOT / 'static' / 'beam-models.js').read_text(encoding='utf-8')
        self.assertIn("api('models')", script)
        self.assertIn('api(', script)


class WorkerKitTests(unittest.TestCase):
    def kit(self):
        gateway.bundle.cache_clear()
        return zipfile.ZipFile(io.BytesIO(gateway.bundle()))

    def test_kit_has_the_entry_point_and_dependency_list_the_bootstrap_installs(self):
        names = self.kit().namelist()
        self.assertIn('cloud_worker.py', names)
        self.assertIn('requirements-cloud-worker.txt', names)
        self.assertIn('models.py', names)

    def test_kit_never_ships_tests_ui_or_remote_runner_files(self):
        for name in self.kit().namelist():
            self.assertTrue(name.endswith('.py') or name == 'requirements-cloud-worker.txt', name)
            self.assertNotIn(name, ('runner.py', 'crew_daemon.py', 'setup.py'))

    def test_kit_bytes_are_identical_every_build_so_codespaces_reuse_their_install(self):
        first = hashlib.sha256(self.kit() and gateway.bundle()).hexdigest()
        gateway.bundle.cache_clear()
        self.assertEqual(first, hashlib.sha256(gateway.bundle()).hexdigest())

    def test_every_first_party_import_inside_the_kit_resolves_inside_the_kit(self):
        archive = self.kit()
        inside = {n[:-3] for n in archive.namelist() if n.endswith('.py')}
        outside = {p.stem for p in (ROOT / 'worker').glob('*.py')} - inside
        for name in archive.namelist():
            if not name.endswith('.py'):
                continue
            tree = ast.parse(archive.read(name).decode('utf-8'))
            for node in ast.walk(tree):
                modules = [a.name.split('.')[0] for a in node.names] if isinstance(node, ast.Import) else \
                          [node.module.split('.')[0]] if isinstance(node, ast.ImportFrom) and node.module else []
                for module in modules:
                    self.assertNotIn(module, outside, name + ' imports ' + module + ', which is not shipped to the Codespace')

    def test_worker_requirements_list_the_libraries_the_kit_needs(self):
        text = self.kit().read('requirements-cloud-worker.txt').decode()
        for package in ('Flask', 'requests', 'cryptography', 'playwright', 'mcp', 'python-docx', 'manifold3d'):
            self.assertIn(package.lower(), text.lower())
        self.assertNotIn('-r ', text)   # one self-contained list; the installer reads only this file


if __name__ == '__main__':
    unittest.main()
