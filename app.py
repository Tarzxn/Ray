"""Small, stateless Render gateway. User jobs and databases stay in Codespaces."""
from __future__ import annotations
import base64
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Response, g, jsonify, redirect, render_template, request
import requests

ROOT = Path(__file__).resolve().parent
SESSION_TTL = 7 * 86400
MAX_WIRE = 64_000_000


# Files in worker/ that belong to the older remote-runner design and are never installed in the Codespace.
WORKER_SKIP = {'runner.py', 'crew_daemon.py', 'setup.py', 'requirements.txt'}


@functools.lru_cache(maxsize=1)
def bundle():
    """The Codespace worker kit: worker/*.py plus its dependency list, zipped the same way every time.

    The bytes are identical on every build, so the Codespace reuses its installed environment until the code changes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        files = [(path.name, path) for path in sorted((ROOT / 'worker').glob('*.py')) if path.name not in WORKER_SKIP]
        files.append(('requirements-cloud-worker.txt', ROOT / 'worker' / 'requirements-cloud.txt'))
        for name, path in files:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes().replace(b'\r\n', b'\n'))
    return buffer.getvalue()



GH_VERSION = '2.103.3'
BOOTSTRAP = '"""Stdlib-only installer sent over SSH. Installation logs contain no credentials."""\nimport base64\nimport hashlib\nimport io\nimport json\nimport os\nfrom pathlib import Path\nimport subprocess\nimport sys\nimport zipfile\n\n\ndef main():\n    packet = json.loads(sys.stdin.buffer.readline(4_000_001))\n    raw = base64.b64decode(packet[\'bundle\'], validate=True)\n    digest = hashlib.sha256(raw).hexdigest()\n    if digest != packet[\'sha256\']: raise ValueError(\'Worker bundle checksum did not match\')\n    root = Path(\'/workspaces/.beam-cloud-runtime\') / digest[:20]\n    root.mkdir(parents=True, exist_ok=True)\n    os.chmod(root.parent, 0o700)\n    def progress(message): print(json.dumps({\'progress\': message}), flush=True)\n    with zipfile.ZipFile(io.BytesIO(raw)) as archive:\n        for name in archive.namelist():\n            target = (root / name).resolve()\n            if not target.is_relative_to(root.resolve()): raise ValueError(\'Invalid bundle path\')\n        archive.extractall(root)\n    python = root / \'venv/bin/python\'\n    marker = root / \'installed\'\n    def install(args):\n        process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)\n        if process.returncode: raise ValueError(\'Worker dependency setup failed. Open the Codespace to check Python, pip, sudo and available storage, then retry.\')\n    if not marker.exists():\n        progress(\'Preparing Python in your Codespace…\')\n        install([\'python3\', \'-m\', \'venv\', str(root / \'venv\')])\n        progress(\'Installing Beam’s document, 3D, browser and MCP libraries…\')\n        install([str(python), \'-m\', \'pip\', \'install\', \'--disable-pip-version-check\', \'-r\', str(root / \'requirements-cloud-worker.txt\')])\n        progress(\'Installing the cloud browser…\')\n        # This runs on the user-selected Codespace, never on Render or their PC.\n        install([str(python), \'-m\', \'playwright\', \'install\', \'--with-deps\', \'chromium\'])\n        marker.write_text(digest)\n    progress(\'Connecting Studio and Crew…\')\n    os.execv(str(python), [str(python), \'-u\', str(root / \'cloud_worker.py\')])\n\n\nif __name__ == \'__main__\':\n    try: main()\n    except Exception as error:\n        print(json.dumps({\'error\': str(error)[:800]}), flush=True)\n        sys.exit(1)\n'


def prepare_tools():
    """Install the official GitHub helper in this app's build folder."""
    import platform
    import tarfile
    from urllib.request import Request, urlopen
    if not shutil.which('ssh'):
        raise ValueError('The server needs an OpenSSH client for Codespaces connections')
    if os.name != 'posix':
        existing = shutil.which('gh')
        if existing: return existing
        raise ValueError('The deployment target is Linux; install GitHub CLI for local Windows development')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if not arch: raise ValueError('GitHub CLI is unavailable for this server architecture')
    folder = ROOT / '.tools'
    tool = folder / ('gh-' + GH_VERSION)
    if tool.is_file(): return str(tool)
    name = 'gh_' + GH_VERSION + '_linux_' + arch + '.tar.gz'
    base = 'https://github.com/cli/cli/releases/download/v' + GH_VERSION + '/'
    def download(filename):
        with urlopen(Request(base + filename, headers={'User-Agent': 'Beam-Cloud'}), timeout=90) as response:
            raw = response.read(32_000_001)
        if len(raw) > 32_000_000: raise ValueError('GitHub helper download exceeds its expected size')
        return raw
    checks = download('gh_' + GH_VERSION + '_checksums.txt').decode()
    expected = next((line.split()[0] for line in checks.splitlines() if line.split()[-1] == name), None)
    raw = download(name)
    if not expected or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('GitHub helper checksum did not match; retry the build')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        member = archive.getmember('gh_' + GH_VERSION + '_linux_' + arch + '/bin/gh')
        if not member.isfile(): raise ValueError('GitHub helper archive has an invalid executable')
        executable = archive.extractfile(member).read()
    folder.mkdir(exist_ok=True)
    temporary = folder / ('gh-download-' + secrets.token_hex(5))
    temporary.write_bytes(executable); temporary.chmod(0o755); temporary.replace(tool)
    return str(tool)

class Transport:
    def __init__(self, identity, payload):
        self.identity = dict(identity)
        self.payload = payload
        self.lock = threading.RLock()
        self.messages = queue.Queue()
        self.process = None
        self.folder = None
        self.status, self.message = 'connecting', 'Starting your Codespace connection…'
        self.last_use = time.monotonic()
        threading.Thread(target=self.start, name='Beam Codespace connection', daemon=True).start()

    def start(self):
        try:
            self.folder = tempfile.TemporaryDirectory(prefix='beam-ssh-')
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'USERPROFILE', 'SystemRoot', 'TEMP', 'TMP', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
            env.update(GH_TOKEN=self.identity['access_token'], GH_HOST='github.com', GH_CONFIG_DIR=self.folder.name,
                GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1', HOME=self.folder.name, USERPROFILE=self.folder.name)
            bootstrap = BOOTSTRAP
            command = 'python3 -u -c ' + shlex.quote(bootstrap)
            self.process = subprocess.Popen([prepare_tools(), 'codespace', 'ssh', '-c', self.identity['codespace'], '--', '-T', '-o', 'BatchMode=yes', command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}))
            def read():
                try:
                    while line := self.process.stdout.readline(MAX_WIRE + 1):
                        if len(line) > MAX_WIRE: break
                        try: self.messages.put(json.loads(line))
                        except ValueError: continue
                finally:
                    self.status, self.message = 'failed', 'The Codespace connection closed. Reconnect to restore your saved workspace.'
                    self.messages.put({'error': self.message})
            threading.Thread(target=read, daemon=True).start()
            self.write({'bundle': base64.b64encode(self.payload).decode(), 'sha256': hashlib.sha256(self.payload).hexdigest()})
            # Installer progress arrives before the persistent bridge accepts init.
            deadline = time.monotonic() + 900
            while True:
                packet = self.messages.get(timeout=max(1, deadline - time.monotonic()))
                if 'progress' in packet: self.message = str(packet['progress'])[:300]; continue
                if packet.get('bridge_ready'):
                    self.write({'operation': 'init', 'owner': str(self.identity['user']['id']), 'codespace': self.identity['codespace'], 'github_token': self.identity['access_token'], 'origin': self.identity.get('public_origin', '')})
                    continue
                if 'error' in packet: raise ValueError(packet['error'])
                if packet.get('result', {}).get('ready'):
                    if packet['result'].get('version') != '0.9.0-cloud':
                        raise ValueError('An older Beam worker is still running. Stop this Codespace in GitHub, then reconnect to Ray.')
                    break
                if time.monotonic() > deadline: raise ValueError('Codespace setup timed out; reconnect to retry')
            self.status, self.message = 'ready', 'Connected. Your workspace is stored in this Codespace.'
        except Exception as error:
            self.status, self.message = 'failed', str(error)[:500]
            self.close()

    def write(self, data):
        self.process.stdin.write((json.dumps(data) + '\n').encode()); self.process.stdin.flush()

    def api(self, path, method='GET', body=None):
        if self.status != 'ready': raise ValueError(self.message)
        with self.lock:
            self.last_use = time.monotonic()
            try:
                self.write({'owner': str(self.identity['user']['id']), 'path': path, 'method': method, 'body': body})
                packet = self.messages.get(timeout=170)
                if 'error' in packet: raise ValueError(packet['error'])
                return packet['result']
            except (BrokenPipeError, OSError, queue.Empty, KeyError):
                self.status, self.message = 'failed', 'Connection interrupted. Reconnect to continue from your saved workspace.'
                self.close(); raise ValueError(self.message) from None

    def close(self):
        if self.process:
            if self.process.poll() is None:
                if os.name == 'posix':
                    try: os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                else: self.process.terminate()
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired: self.process.kill()
            for stream in (self.process.stdin, self.process.stdout):
                try: stream.close()
                except (OSError, ValueError): pass
        if self.folder:
            try: self.folder.cleanup()
            except OSError: pass


class Pool:
    def __init__(self, factory=Transport):
        self.factory, self.entries, self.lock = factory, {}, threading.RLock()
        self.payload = bundle()

    def get(self, identity, start=True):
        if not identity.get('codespace'): return None
        key = (identity['sid'], str(identity['user']['id']), identity['codespace'])
        with self.lock:
            item = self.entries.get(key)
            if item and item.status == 'failed' and start:
                item.close(); self.entries.pop(key); item = None
            if not item and start:
                if len(self.entries) >= 4:
                    oldest = min(self.entries, key=lambda k: self.entries[k].last_use)
                    self.entries.pop(oldest).close()
                item = self.entries[key] = self.factory(identity, self.payload)
            if item: item.last_use = time.monotonic()
            return item

    def close(self, identity):
        with self.lock:
            keys = [k for k in self.entries if k[0] == identity['sid']]
            for key in keys: self.entries.pop(key).close()


def create_app(config=None, pool=None):
    app = Flask(__name__, static_folder=str(ROOT / 'static'), static_url_path='/static', template_folder=str(ROOT / 'templates'))
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        PUBLIC_URL=os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL', ''),
        BEAM_SESSION_KEY=os.environ.get('BEAM_SESSION_KEY', ''),
        GITHUB_CLIENT_ID=os.environ.get('GITHUB_CLIENT_ID', ''), GITHUB_CLIENT_SECRET=os.environ.get('GITHUB_CLIENT_SECRET', ''))
    if config: app.config.update(config)
    secret = app.config['BEAM_SESSION_KEY']
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())) if len(secret) >= 32 else None
    app.pool = pool or Pool()

    def origin():
        value = app.config['PUBLIC_URL'].rstrip('/')
        if not value: raise ValueError('Set PUBLIC_URL to the HTTPS address of your Ray service')
        return value

    def encode(data): return cipher.encrypt(json.dumps(data).encode()).decode()
    def decode(value, ttl=SESSION_TTL):
        try: return json.loads(cipher.decrypt(value.encode(), ttl=ttl))
        except (InvalidToken, ValueError, AttributeError): return None
    def cookie(response, data, name='beam_cloud', ttl=SESSION_TTL):
        response.set_cookie(name, encode(data), max_age=ttl, secure=origin().startswith('https:'), httponly=True, samesite='Lax', path='/')
        return response
    def github(path, token):
        with requests.get('https://api.github.com' + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'}, timeout=(8, 25), allow_redirects=False) as response:
            if response.status_code == 401: raise ValueError('GitHub access expired. Sign out and sign in again.')
            if response.status_code != 200: raise ValueError('GitHub could not complete this request. Check Codespaces permissions and availability.')
            return response.json()

    @app.before_request
    def authenticate():
        missing = []
        if cipher is None: missing.append('BEAM_SESSION_KEY (at least 32 random characters)')
        for key in ('GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET'):
            if not app.config[key]: missing.append(key)
        if not app.config['PUBLIC_URL']: missing.append('PUBLIC_URL or RENDER_EXTERNAL_URL')
        g.setup_missing = missing
        if missing and (request.path == '/' or request.path.startswith('/auth/') or request.path.startswith('/api/')):
            message = 'Add these values in Render → Environment, then redeploy: ' + ', '.join(missing) + '.'
            if request.path.startswith('/api/'): return jsonify(error=message, setup_required=True), 503
            return render_template('login.html', error=message, setup=True), 200 if request.path == '/' else 503
        g.identity = decode(request.cookies.get('beam_cloud'))
        g.refresh_cookie = False
        if request.path.startswith('/api/') or request.path == '/auth/logout':
            if not g.identity: return jsonify(error='Sign in with GitHub to continue', sign_in=True), 401
            if request.method == 'POST':
                if request.headers.get('Origin') != origin(): return jsonify(error='Cross-origin request denied'), 403
                if not secrets.compare_digest(request.headers.get('X-Beam-Token', ''), g.identity['csrf']): return jsonify(error='Your browser session needs to be refreshed'), 403
            if g.identity.get('expires_at', 0) < time.time() + 60:
                if not g.identity.get('refresh_token'): return jsonify(error='Sign in with GitHub again', sign_in=True), 401
                with requests.post('https://github.com/login/oauth/access_token', headers={'Accept': 'application/json'}, data={
                    'client_id': app.config['GITHUB_CLIENT_ID'], 'client_secret': app.config['GITHUB_CLIENT_SECRET'], 'grant_type': 'refresh_token', 'refresh_token': g.identity['refresh_token']}, timeout=(8, 25)) as response:
                    data = response.json()
                if not data.get('access_token'): return jsonify(error='GitHub sign-in expired', sign_in=True), 401
                g.identity.update(access_token=data['access_token'], refresh_token=data.get('refresh_token', g.identity['refresh_token']), expires_at=time.time() + data.get('expires_in', 28800))
                app.pool.close(g.identity); g.refresh_cookie = True

    @app.after_request
    def security(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
        if getattr(g, 'refresh_cookie', False): cookie(response, g.identity)
        return response

    @app.errorhandler(ValueError)
    def bad_request(error): return jsonify(error=str(error)), 400
    @app.errorhandler(requests.RequestException)
    def upstream_error(error): return jsonify(error='The cloud service could not be reached. Retry shortly.'), 502
    @app.errorhandler(413)
    def too_large(error): return jsonify(error='This request exceeds the 8 MB limit'), 413

    @app.get('/healthz')
    def health(): return jsonify(ok=True, configured=not bool(g.setup_missing), name='Ray', version='0.9.0-cloud')

    @app.get('/auth/login')
    def login():
        from urllib.parse import urlencode
        if not app.config['GITHUB_CLIENT_ID'] or not app.config['GITHUB_CLIENT_SECRET']:
            return render_template('login.html', error='The host needs to configure GitHub OAuth. Follow README.md.'), 503
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        url = 'https://github.com/login/oauth/authorize?' + urlencode({'client_id': app.config['GITHUB_CLIENT_ID'], 'redirect_uri': origin() + '/auth/callback',
            'scope': 'read:user codespace', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})
        return cookie(redirect(url), {'state': state, 'verifier': verifier}, 'beam_oauth', 600)

    @app.get('/auth/callback')
    def callback():
        pending = decode(request.cookies.get('beam_oauth'), 600)
        if not pending or not secrets.compare_digest(request.args.get('state', ''), pending['state']): return render_template('login.html', error='Sign-in expired or did not match. Try again.'), 400
        if request.args.get('error'): return render_template('login.html', error='GitHub sign-in was cancelled.'), 400
        with requests.post('https://github.com/login/oauth/access_token', headers={'Accept': 'application/json'}, data={
            'client_id': app.config['GITHUB_CLIENT_ID'], 'client_secret': app.config['GITHUB_CLIENT_SECRET'], 'code': request.args.get('code', ''),
            'redirect_uri': origin() + '/auth/callback', 'code_verifier': pending['verifier']}, timeout=(8, 25)) as response:
            token = response.json()
        if not token.get('access_token'): return render_template('login.html', error='GitHub could not complete sign-in. Try again.'), 400
        profile = github('/user', token['access_token'])
        identity = {'sid': secrets.token_urlsafe(24), 'csrf': secrets.token_urlsafe(32), 'user': {k: profile[k] for k in ('id', 'login')},
            'access_token': token['access_token'], 'refresh_token': token.get('refresh_token'), 'expires_at': time.time() + token.get('expires_in', SESSION_TTL), 'codespace': None, 'public_origin': origin()}
        response = cookie(redirect('/'), identity); response.delete_cookie('beam_oauth', path='/')
        return response

    @app.post('/auth/logout')
    def logout():
        app.pool.close(g.identity)
        response = jsonify(ok=True); response.delete_cookie('beam_cloud', path='/')
        return response

    @app.get('/')
    def index():
        if not g.identity: return render_template('login.html', error='')
        return render_template('index.html', token=g.identity['csrf'], callback_url=origin() + '/auth/google/callback')

    @app.get('/api/account')
    def account(): return jsonify(user=g.identity['user'], codespace=g.identity.get('codespace'), csrf=g.identity['csrf'])

    @app.get('/api/codespaces')
    def computers():
        data = github('/user/codespaces?per_page=100', g.identity['access_token'])
        spaces = [space for space in data.get('codespaces', []) if str(space.get('owner', {}).get('id')) == str(g.identity['user']['id'])]
        return jsonify(computers=[{k: space.get(k) for k in ('name','display_name','state','repository')} for space in spaces])

    @app.post('/api/codespaces/connect')
    def connect():
        import re
        name = request.get_json().get('codespace', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,119}', name): raise ValueError('Choose a Codespace')
        space = github('/user/codespaces/' + name, g.identity['access_token'])
        if str(space.get('owner', {}).get('id')) != str(g.identity['user']['id']): return jsonify(error='Choose a Codespace owned by your GitHub account'), 403
        if space.get('state') not in ('Available', 'Shutdown', 'Starting', 'Created'): raise ValueError('This Codespace is not ready. Open it in GitHub and retry.')
        app.pool.close(g.identity)
        g.identity['codespace'] = name
        item = app.pool.get(g.identity)
        return cookie(jsonify(status=item.status, message=item.message), g.identity)

    @app.get('/api/codespaces/status')
    def status():
        item = app.pool.get(g.identity, start=False)
        if not item and g.identity.get('codespace'): item = app.pool.get(g.identity)
        return jsonify(status=item.status if item else 'disconnected', message=item.message if item else 'Choose your Codespace', codespace=g.identity.get('codespace'))

    @app.get('/auth/google/callback')
    def google_callback():
        if not g.identity: return redirect('/')
        item = app.pool.get(g.identity)
        if not item or item.status != 'ready': return 'Reconnect your Codespace in Beam, then connect Google again.', 409
        answer = item.api('/api/cloud/google/callback', 'POST', {k: request.args.get(k, '') for k in ('state','code','error')})
        return ('Google connected. Return to the Ray tab.' if answer['status'] == 200 else 'Google sign-in did not finish. Return to Beam Cloud and try again.'), answer['status']

    @app.route('/api/<path:path>', methods=['GET', 'POST'])
    def proxy(path):
        if path.startswith(('computer', 'privacy/')): return jsonify(error='Ray runs only in your Codespace'), 404
        item = app.pool.get(g.identity)
        if not item or item.status != 'ready':
            if path == 'state': return jsonify(name='Ray', version='0.9.0-cloud', cloud=True, connected=False, projects=[], sessions=[], active={}, configured=False, theme='dark', effort='high', command_budget=8)
            return jsonify(error='Connect your Codespace first', reconnect=True), 409
        body = request.get_json() if request.method == 'POST' else None
        if request.method == 'POST' and not isinstance(body, dict): raise ValueError('Expected a JSON object')
        if path == 'connections/google/start': body = {**body, 'redirect_uri': origin() + '/auth/google/callback'}
        target = '/api/' + path + ('?' + request.query_string.decode('ascii') if request.query_string else '')
        answer = item.api(target, request.method, body)
        raw = base64.b64decode(answer['data'], validate=True)
        return Response(raw, status=answer['status'], headers=answer.get('headers', {}))

    return app


if __name__ == '__main__':
    import sys
    if '--prepare' in sys.argv:
        prepare_tools()
        print('GitHub connection helper ready.')
    else:
        create_app().run(host='127.0.0.1', port=int(os.environ.get('PORT', '10000')))
else:
    app = create_app()
