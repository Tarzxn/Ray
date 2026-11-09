"""Codespace-owned application state. Nothing in this module runs on Render."""
from __future__ import annotations
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet
from flask import jsonify, request
from beam_core import Agent, Store

VERSION = '0.9.0-cloud'
WORKSPACES = Path('/workspaces')
PRIVATE_NAMES = {'.beam-cloud', '.beam-crew', '.beam-worker', '.beam-projects', '.beam-cloud-runtime'}
GRACE_SECONDS = 120


class CloudStore(Store):
    cloud = True

    def __init__(self, directory):
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == 'posix': os.chmod(folder, 0o700)
        keyfile = folder / 'vault.key'
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream: stream.write(Fernet.generate_key())
        self.cipher = Fernet(keyfile.read_bytes())
        super().__init__(directory)
        with self.lock, self.db:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS cloud_paused(session_id TEXT PRIMARY KEY, mode TEXT, effort TEXT, crew_task TEXT);
              CREATE TABLE IF NOT EXISTS cloud_drafts(id TEXT PRIMARY KEY, text TEXT);
            ''')

    def protect_secret(self, value, decrypt=False):
        return self.cipher.decrypt(value.encode()).decode() if decrypt else self.cipher.encrypt(value.encode()).decode()

    def key(self):
        saved = self.settings().get('ollama_key')
        return self.protect_secret(saved, True) if saved else ''

    def add_project(self, raw):
        path = Path(raw).resolve(strict=True)
        base = WORKSPACES.resolve()
        if not path.is_relative_to(base) or path == base or any(p in PRIVATE_NAMES or p.startswith('.beam-') for p in path.relative_to(base).parts):
            raise ValueError('Choose a project folder inside /workspaces, outside Beam’s private storage')
        return super().add_project(path)


class CloudAgent(Agent):
    privacy_choices = ['cloud', 'stop']

    def __init__(self, store):
        super().__init__(store)
        self.integrations.config.preview_allowed = self.servers.preview_allowed

    def start(self, ident, prompt, mode='ask', effort='high', **kwargs):
        kwargs['extra_context'] = kwargs.get('extra_context', '') + (
            '\nYou run inside the user’s GitHub Codespace. All commands, files, browser, MCP and generation tools execute here. '
            'Use Linux shell syntax. Studio and Crew are private to this GitHub account and can continue independently of the web page. '
            'Pause for questions, approvals and sensitive information. Never use local Ollama or invent user consent. '
            'Beam’s private application data and credentials are not project inputs; do not access .beam-cloud or .beam-crew folders.')
        return super().start(ident, prompt, mode, effort, **kwargs)

    def choose_privacy(self, ident, action_id, choice, model=None):
        if choice not in self.privacy_choices: raise ValueError('Choose cloud processing for these details or stop the task')
        return super().choose_privacy(ident, action_id, choice, None)


class Lifecycle:
    def __init__(self, app, stop_computer=None):
        self.app = app
        self.tabs = {}
        self.lock = threading.RLock()
        self.changed = time.monotonic()
        self.closing = False
        self.closed = threading.Event()
        self.stop_computer = stop_computer or self.github_stop
        self.thread = threading.Thread(target=self.loop, name='Beam cloud presence', daemon=True)
        self.thread.start()

    def presence(self, tab, leaving=False):
        if not isinstance(tab, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', tab): raise ValueError('Invalid browser tab identity')
        with self.lock:
            if self.closing: raise ValueError('The Codespace is pausing; reconnect once it stops')
            if leaving: self.tabs.pop(tab, None)
            else: self.tabs[tab] = time.monotonic() + GRACE_SECONDS
            self.changed = time.monotonic()
        return self.state()

    def state(self):
        with self.app.store.lock:
            rows = [dict(row) for row in self.app.store.db.execute('SELECT * FROM cloud_paused')]
        return {'background_work': self.app.store.settings().get('background_work', False), 'paused': rows,
                'closing': self.closing, 'grace_seconds': GRACE_SECONDS, 'storage': 'Codespace'}

    def pause(self):
        app = self.app
        # Freeze scheduling before cancelling workers; persist resumable records
        # before asking GitHub to stop. No cancelled tool is silently rerun.
        with app.crew.lock, app.agent.lock:
            tasks = app.crew._rows("SELECT * FROM crew_tasks WHERE status IN ('queued','running','approval','privacy','waiting_team','stopping')")
            with app.store.lock, app.store.db:
                for task in tasks:
                    app.store.db.execute('INSERT OR REPLACE INTO cloud_paused VALUES (?,?,?,?)',
                        (task['session_id'], task['mode'], task['effort'], task['id']))
                crew_sessions = {t['session_id'] for t in tasks}
                for ident, run in list(app.agent.runs.items()):
                    if ident not in crew_sessions and run.snapshot()['status'] in ('running','approval','privacy','stopping'):
                        app.store.db.execute('INSERT OR REPLACE INTO cloud_paused VALUES (?,?,?,?)',
                            (ident, run.permission_mode, run.effort, ''))
                for task in tasks:
                    app.store.db.execute("UPDATE crew_tasks SET status='interrupted',stop_requested=1,next_prompt='' WHERE id=?", (task['id'],))
            for run in list(app.agent.runs.values()):
                if run.snapshot()['status'] in ('running','approval','privacy','stopping'):
                    run.cancelled.set(); run.decision.set(); run.privacy_event.set()
                    threading.Thread(target=run.stop, daemon=True).start()
        deadline = time.monotonic() + 20
        for run in list(app.agent.runs.values()):
            if run.worker: run.worker.join(max(0, deadline - time.monotonic()))
        app.agent.servers.close()
        app.agent.integrations.close()
        with app.store.lock: app.store.db.execute('PRAGMA wal_checkpoint(FULL)')

    def shutdown(self):
        with self.lock:
            if self.closing: return
            self.closing = True
        try:
            self.pause()
            self.stop_computer()
            self.closed.set()
        except Exception:
            # Keep retrying shutdown if GitHub is temporarily unavailable. The
            # task records have already been saved and execution is cancelled.
            with self.lock: self.closing = False; self.changed = time.monotonic() - GRACE_SECONDS
            raise

    def github_stop(self):
        import requests
        identity = json.loads((self.app.store.root / 'owner.json').read_text())
        token = self.app.agent.integrations.config.vault.get('cloud_github_token')
        if not token: raise ValueError('Reconnect GitHub so Beam can stop this Codespace')
        with requests.Session() as session:
            session.trust_env = False
            response = session.post('https://api.github.com/user/codespaces/' + identity['codespace'] + '/stop',
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'}, timeout=(5, 20))
            response.raise_for_status()

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self.tabs = {k: v for k, v in self.tabs.items() if v > now}
            should_stop = not self.tabs and now - self.changed >= GRACE_SECONDS and not self.closing
        if should_stop and not self.app.store.settings().get('background_work', False): self.shutdown()

    def loop(self):
        while not self.closed.wait(5):
            try: self.tick()
            except Exception: pass


def configure(app):
    from cloud_google import CloudGoogle
    app.agent.integrations.google = CloudGoogle(app.agent.integrations.config)
    lifecycle = app.lifecycle = Lifecycle(app)
    original_state = app.view_functions['state']

    @app.before_request
    def cloud_boundary():
        if request.path.startswith('/api/computer') or request.path.startswith('/api/privacy/'):
            return jsonify(error='Ray only connects to your Codespace'), 404
        if lifecycle.closing and request.method != 'GET': return jsonify(error='Pausing this Codespace. Reconnect to resume.'), 409

    def state():
        data = original_state().get_json()
        return jsonify(**{**data, 'name': 'Ray', 'version': VERSION, 'cloud': True, **lifecycle.state()})
    app.view_functions['state'] = state

    original_settings = app.view_functions['settings']
    def settings():
        data = request.get_json()
        if 'background_work' in data and not isinstance(data['background_work'], bool): raise ValueError('Background work must be true or false')
        result = original_settings()
        if 'background_work' in data:
            with app.store.lock:
                saved = app.store.settings(); saved['background_work'] = data['background_work']
                temp = app.store.settings_path.with_suffix('.tmp'); temp.write_text(json.dumps(saved)); temp.replace(app.store.settings_path)
        return result
    app.view_functions['settings'] = settings

    def workspace():
        path = WORKSPACES / 'beam-workspace'; path.mkdir(exist_ok=True)
        return jsonify(app.store.add_project(path))
    app.view_functions['workspace'] = workspace

    @app.get('/api/cloud/folders')
    def folders():
        path = Path(request.args.get('path', str(WORKSPACES))).resolve(strict=True)
        base = WORKSPACES.resolve()
        if not path.is_relative_to(base) or any(p.startswith('.') for p in path.relative_to(base).parts): raise ValueError('Choose a folder under /workspaces')
        children = [{'name': p.name, 'path': str(p)} for p in sorted(path.iterdir())
                    if p.is_dir() and not p.is_symlink() and not p.name.startswith('.') and p.name not in {'node_modules','venv','__pycache__'}]
        return jsonify(path=str(path), parent=str(path.parent) if path != base else None, folders=children, selectable=path != base)

    @app.post('/api/cloud/presence')
    def presence():
        data = request.get_json()
        return jsonify(lifecycle.presence(data.get('tab'), data.get('leaving') is True))

    @app.post('/api/cloud/leave')
    def leave():
        if request.get_json().get('background') is True:
            if not app.store.settings().get('background_work', False): raise ValueError('Enable background work in Settings first')
            return jsonify(ok=True, background=True)
        threading.Thread(target=lifecycle.shutdown, daemon=True).start()
        return jsonify(ok=True, pausing=True)

    @app.post('/api/cloud/resume')
    def resume():
        ident = request.get_json().get('session_id')
        with app.store.lock:
            row = app.store.db.execute('SELECT * FROM cloud_paused WHERE session_id=?', (ident,)).fetchone()
        if not row: raise ValueError('Choose a paused task')
        run = app.agent.runs.get(ident)
        if run and run.worker and run.worker.is_alive(): raise ValueError('The previous worker is still stopping')
        if row['crew_task']: app.crew.start_task(row['crew_task'])
        else:
            app.crew.assert_project_available(app.store.session(ident)['project_id'])
            app.agent.start(ident, 'Resume the paused task. Review the saved conversation and current files. Verify interrupted actions before retrying them.', row['mode'], row['effort'])
        with app.store.lock, app.store.db: app.store.db.execute('DELETE FROM cloud_paused WHERE session_id=?', (ident,))
        return jsonify(ok=True)

    @app.route('/api/cloud/drafts', methods=['GET', 'POST'])
    def drafts():
        with app.store.lock:
            if request.method == 'POST':
                data = request.get_json(); ident = str(data.get('id', ''))
                if not re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', ident) or not isinstance(data.get('text'), str) or len(data['text']) > 50000: raise ValueError('Invalid draft')
                with app.store.db: app.store.db.execute('INSERT OR REPLACE INTO cloud_drafts VALUES (?,?)', (ident, data['text']))
                return jsonify(ok=True)
            return jsonify(drafts={r['id']: r['text'] for r in app.store.db.execute('SELECT * FROM cloud_drafts')})

    @app.route('/api/cloud/ui', methods=['GET', 'POST'])
    def ui_state():
        with app.store.lock:
            saved = app.store.settings()
            if request.method == 'POST':
                data = request.get_json(); project = data.get('project'); session = data.get('session')
                if project: app.store.project(project)
                if session and app.store.session(session)['project_id'] != project: raise ValueError('Session belongs to another project')
                if data.get('mode') not in ('studio', 'crew'): raise ValueError('Choose Studio or Crew')
                saved['cloud_ui'] = {'project': project, 'session': session, 'mode': data['mode']}
                temp = app.store.settings_path.with_suffix('.tmp'); temp.write_text(json.dumps(saved)); temp.replace(app.store.settings_path)
            return jsonify(saved.get('cloud_ui', {}))

    def google_start():
        return jsonify(app.agent.integrations.google.start(request.get_json().get('redirect_uri', '')))
    app.view_functions['google_start'] = google_start

    @app.post('/api/cloud/google/callback')
    def google_callback():
        return jsonify(app.agent.integrations.google.finish(request.get_json()))
