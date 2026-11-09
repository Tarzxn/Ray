"""Private Unix-socket service plus an SSH JSON bridge. No public worker port."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time

DATA = Path('/workspaces/.beam-cloud')
HERE = Path(__file__).resolve().parent
MAX_WIRE = 64_000_000


class Service:
    def __init__(self, root=DATA):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.app = None

    def dispatch(self, data):
        owner = str(data.get('owner', ''))
        if not re.fullmatch(r'[1-9][0-9]{0,19}', owner): raise ValueError('A GitHub account is required')
        with self.lock:
            identity_file = self.root / 'owner.json'
            existing = json.loads(identity_file.read_text()) if identity_file.exists() else None
            if existing and existing['owner'] != owner: raise ValueError('This cloud workspace belongs to a different GitHub account')
            if data.get('operation') == 'init':
                name = data.get('codespace', '')
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,119}', name): raise ValueError('Invalid Codespace identity')
                self.root.mkdir(parents=True, exist_ok=True)
                if os.name == 'posix': os.chmod(self.root, 0o700)
                if existing and existing['codespace'] != name: raise ValueError('Codespace identity changed')
                identity_file.write_text(json.dumps({'owner': owner, 'codespace': name, 'schema': 1}))
                if not self.app:
                    from desktop import create_app
                    self.app = create_app(self.root, cloud=True)
                token = data.get('github_token', '')
                if not isinstance(token, str) or not 10 <= len(token) <= 2000: raise ValueError('GitHub connection token is missing')
                self.app.agent.integrations.config.vault.set('cloud_github_token', token)
                origin = data.get('origin', '')
                if origin:
                    from urllib.parse import urlparse
                    parsed = urlparse(origin)
                    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password: raise ValueError('Invalid Beam Cloud origin')
                    self.app.agent.integrations.config.blocked_origins.add(origin.rstrip('/'))
                from cloud_runtime import VERSION
                return {'ready': True, 'version': VERSION, 'storage': 'Codespace'}
            if not existing or not self.app: raise ValueError('Connect the Codespace first')
            app = self.app
        path, method = data.get('path', ''), data.get('method', 'GET')
        if not isinstance(path, str) or not path.startswith('/api/') or '\\' in path or '\x00' in path or len(path) > 5000 or method not in ('GET', 'POST'): raise ValueError('Invalid cloud API request')
        with app.test_client() as client:
            response = client.open(path, method=method, json=data.get('body') if method == 'POST' else None,
                headers={'X-Beam-Token': app.config['BEAM_TOKEN']}, base_url='http://localhost', environ_base={'REMOTE_ADDR': '127.0.0.1'})
            raw = response.get_data()
            if len(raw) > 40_000_000: raise ValueError('File exceeds the 40 MB transfer limit')
            headers = {k: v for k, v in response.headers if k.lower() in ('content-type', 'content-disposition')}
            return {'status': response.status_code, 'headers': headers, 'data': base64.b64encode(raw).decode()}

    def close(self):
        if self.app:
            self.app.lifecycle.closed.set()
            self.app.crew.close(); self.app.agent.servers.close(); self.app.agent.integrations.close(); self.app.agent.remote.close()


def call(data):
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    address = str(DATA / 'web.sock')
    def connect():
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); connection.settimeout(180)
        try: connection.connect(address); return connection
        except OSError: connection.close(); raise
    try: connection = connect()
    except OSError:
        import fcntl
        with (DATA / 'launch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try: connection = connect()
            except OSError:
                Path(address).unlink(missing_ok=True)
                with (DATA / 'service.log').open('ab') as log:
                    subprocess.Popen([sys.executable, str(HERE / 'cloud_worker.py'), '--serve'], stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True, close_fds=True)
                deadline = time.monotonic() + 40
                while True:
                    try: connection = connect(); break
                    except OSError:
                        if time.monotonic() > deadline: raise ValueError('Beam’s cloud service did not start; reconnect to retry')
                        time.sleep(.15)
    with connection:
        connection.sendall((json.dumps(data) + '\n').encode())
        with connection.makefile('rb') as stream: raw = stream.readline(MAX_WIRE + 1)
    if len(raw) > MAX_WIRE: raise ValueError('Worker response too large')
    answer = json.loads(raw)
    if answer.get('error'): raise ValueError(answer['error'])
    return answer['result']


def main():
    if '--serve' not in sys.argv:
        print(json.dumps({'bridge_ready': True}), flush=True)
        for line in sys.stdin.buffer:
            try:
                if len(line) > MAX_WIRE: raise ValueError('Request too large')
                result = {'result': call(json.loads(line))}
            except Exception as error: result = {'error': str(error)[:800]}
            print(json.dumps(result), flush=True)
        return
    DATA.mkdir(parents=True, exist_ok=True); os.chmod(DATA, 0o700); os.umask(0o077)
    import fcntl
    instance_lock = (DATA / 'daemon.lock').open('a')
    try: fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: return
    service = Service()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                raw = self.rfile.readline(MAX_WIRE + 1)
                if len(raw) > MAX_WIRE: raise ValueError('Request too large')
                answer = {'result': service.dispatch(json.loads(raw))}
            except Exception as error: answer = {'error': str(error)[:800]}
            self.wfile.write((json.dumps(answer) + '\n').encode())
    class Server(socketserver.ThreadingUnixStreamServer): daemon_threads = True
    address = str(DATA / 'web.sock')
    Path(address).unlink(missing_ok=True)
    with Server(address, Handler) as server:
        os.chmod(address, 0o600)
        try: server.serve_forever()
        finally: service.close()


if __name__ == '__main__': main()
