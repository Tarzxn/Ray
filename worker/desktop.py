"""Beam Windows desktop entry point. Run desktop.py or the packaged Beam.exe."""
from __future__ import annotations
import argparse
import ctypes
from contextlib import closing
import json
import logging
import mimetypes
import io
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time

from flask import Flask, jsonify, request, send_file
from werkzeug.serving import make_server
from beam_core import Agent, Store, MODEL, git_info, project_path, read_text, walk_files
from crew import Crew
from cloud_client import CrewRouter



def create_app(directory, token=None, cloud=False):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 8*1024*1024
    app.config['BEAM_TOKEN'] = token or secrets.token_urlsafe(32)
    if cloud:
        from cloud_runtime import CloudStore, CloudAgent
        store = CloudStore(directory)
        agent = CloudAgent(store)
    else:
        store = Store(directory)
        agent = Agent(store)
    local_crew = Crew(store, agent)
    crew = CrewRouter(local_crew,agent)
    app.store, app.agent, app.crew = store, agent, crew

    @app.before_request
    def local_only():
        agent.integrations.config.blocked_origins.add(request.host_url.rstrip('/'))
        if request.remote_addr not in ('127.0.0.1','::1',None):
            return jsonify(error='Local connections only'),403
        if request.host.split(':')[0] not in ('127.0.0.1','localhost'):
            return jsonify(error='Invalid host'),403
        if request.headers.get('Origin') and request.headers['Origin'] != request.host_url.rstrip('/'):
            return jsonify(error='Cross-origin access denied'),403
        if request.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('X-Beam-Token',''),app.config['BEAM_TOKEN']):
            return jsonify(error='Invalid local session token'),403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        response.headers['Cache-Control']='no-store'
        return response

    @app.errorhandler(ValueError)
    def bad_value(error):
        from cloud_client import CloudTransferReview
        if isinstance(error,CloudTransferReview):return jsonify(error=str(error),cloud_transfer_review=True,categories=error.categories),400
        return jsonify(error=str(error)),400
    @app.errorhandler(OSError)
    def io_error(error): return jsonify(error=str(error)),400
    @app.errorhandler(__import__('requests').RequestException)
    def network_error(error):return jsonify(error='The network request failed. Check the connection, URL and service credentials.'),502
    @app.errorhandler(413)
    def too_large(error): return jsonify(error='Request exceeds the 8 MB limit'),413

    @app.get('/api/state')
    def state():
        try:
            key_ready=bool(store.key())
        except ValueError:
            key_ready=False
        return jsonify(name='Beam',version='0.7.0',model=MODEL,configured=key_ready,theme=store.settings().get('theme','dark'),effort=store.settings().get('effort','high'),selected_model=store.settings().get('model','beam'),reflection=store.settings().get('reflection','auto'),crew_parallel=store.settings().get('crew_parallel',1),command_budget=store.settings().get('command_budget',8),
                       projects=store.projects(),sessions=[s for s in store.sessions() if s['id'] not in crew.session_ids()],
                       active={k:agent.run_state(k)['status'] for k in list(agent.runs) if k not in crew.session_ids() and agent.run_state(k)['status'] in ('running','approval','privacy','stopping')})

    @app.get('/api/models')
    def models_list():
        import models as model_catalog
        settings=store.settings()
        try:key=store.key()
        except ValueError:key=''
        selected=settings.get('model','auto' if cloud else 'beam')
        catalog=model_catalog.catalog(key,selected)
        if cloud and selected != 'auto':
            catalog['models'].insert(0,model_catalog.meta('auto'))
        return jsonify(**catalog,selected=selected,reflection=settings.get('reflection','auto'),crew_parallel=settings.get('crew_parallel',1),
                       efforts={name:{'reflect':model_catalog.reflect_rounds(name,settings)} for name in ('low','medium','high','max')})

    @app.post('/api/settings')
    def settings():
        with store.lock:store.save_settings(request.get_json())
        return jsonify(ok=True)

    @app.post('/api/connection')
    def connection():
        import requests
        key=store.key()
        if not key: raise ValueError('Add an Ollama Cloud API key first')
        # Authenticated, minimal inference checks actual model access instead of the public tags endpoint.
        try:
            response=requests.post('https://ollama.com/api/chat',headers={'Authorization':'Bearer '+key},
                json={'model':MODEL,'messages':[{'role':'user','content':'Reply with OK.'}],'stream':False,'think':False,'options':{'num_predict':8}},timeout=(15,60))
            if response.status_code != 200:
                raise ValueError(f'Ollama returned HTTP {response.status_code}. Check your key, model access and quota.')
            response.raise_for_status()
            return jsonify(ok=True,message='Connected to Beam on Ollama Cloud')
        except requests.RequestException:
            raise ValueError('Could not connect to Ollama Cloud. Check your internet connection.') from None

    @app.post('/api/workspace')
    def workspace():
        path=store.root/'workspace';path.mkdir(exist_ok=True)
        return jsonify(store.add_project(path))

    @app.get('/api/connections')
    def connections():return jsonify(agent.integrations.state())

    @app.post('/api/connections')
    def connection_settings():return jsonify(agent.integrations.save_settings(request.get_json()))

    @app.post('/api/web/search')
    def web_search():
        data=request.get_json();return jsonify(agent.integrations.web.search(data.get('query',''),data.get('max_results',5)))

    @app.get('/api/browser')
    def browser_state():return jsonify(agent.integrations.browser.state())

    @app.post('/api/browser')
    def browser_control():
        data=request.get_json();method=data.get('method','snapshot')
        supported = ('open','snapshot','action','restart','close')
        if cloud:supported += ('control','input','remember')
        if method not in supported:raise ValueError('Unsupported browser operation')
        args=data.get('arguments',{})
        if not isinstance(args,dict) or 'manual' in args:raise ValueError('Invalid browser arguments')
        return jsonify(agent.integrations.browser.call(method,manual=True,**args))

    if cloud:
        @app.get('/api/browser/frame')
        def browser_frame():
            data=agent.integrations.browser.call('frame',manual=True)
            return send_file(io.BytesIO(data),mimetype='image/jpeg') if data else ('',204)

    @app.get('/api/browser/image')
    def browser_image():
        browser=agent.integrations.browser
        with browser.lock:data=browser.image
        if not data:raise ValueError('No browser screenshot yet')
        return send_file(io.BytesIO(data),mimetype='image/jpeg')

    @app.post('/api/connections/google/client')
    def google_client():return jsonify(agent.integrations.google.configure(request.get_json()))

    @app.post('/api/connections/google/start')
    def google_start():return jsonify(agent.integrations.google.start())

    @app.post('/api/connections/google/disconnect')
    def google_disconnect():return jsonify(agent.integrations.google.disconnect())

    @app.get('/api/google/files')
    def google_files():return jsonify(agent.integrations.google.files(request.args.get('query',''),request.args.get('page_token','')))

    @app.get('/api/google/presentation')
    def google_presentation():return jsonify(agent.integrations.google.get(request.args['id']))

    @app.post('/api/google/presentation')
    def google_create():return jsonify(agent.integrations.google.create(request.get_json()['title']))

    @app.get('/api/google/thumbnail')
    def google_thumbnail():
        import requests
        from urllib.parse import urlparse
        result=agent.integrations.google.preview(request.args['id'],request.args['page'])
        url=result.get('contentUrl','');parsed=urlparse(url)
        if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.googleusercontent.com'):raise ValueError('Google returned an unsupported preview host')
        with requests.get(url,timeout=(10,20),stream=True) as response:
            response.raise_for_status();data=bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data)>5000000:raise ValueError('Slide preview is too large')
        return send_file(io.BytesIO(data),mimetype='image/png')

    @app.post('/api/connections/mcp')
    def mcp_save():return jsonify(agent.integrations.mcp.save(request.get_json()))

    @app.post('/api/connections/mcp/<ident>/<action>')
    def mcp_action(ident,action):
        if action=='connect':return jsonify(agent.integrations.mcp.call(ident,'list'))
        if action=='disconnect':agent.integrations.mcp.disconnect(ident);return jsonify(ok=True)
        if action=='remove':return jsonify(agent.integrations.mcp.remove(ident))
        raise ValueError('Unknown MCP connection action')

    @app.post('/api/projects')
    def projects(): return jsonify(store.add_project(request.get_json()['path']))

    @app.post('/api/sessions')
    def new_session(): return jsonify(store.create_session(request.get_json()['project_id']))

    @app.get('/api/sessions/<ident>')
    def session(ident):
        result=store.session(ident)
        result.pop('messages',None) # private reasoning/tool context stays on the local backend
        run=agent.runs.get(ident)
        return jsonify(**result,**agent.run_state(ident),pending=run.pending if run else None,privacy_pending=getattr(run,'privacy_pending',None) if run else None)

    @app.get('/api/sessions/<ident>/events')
    def events(ident):
        store.session(ident)
        run=agent.runs.get(ident)
        return jsonify(events=store.events(ident,max(0,int(request.args.get('after',0)))),**agent.run_state(ident),privacy_pending=getattr(run,'privacy_pending',None) if run else None)

    @app.post('/api/sessions/<ident>/run')
    def start(ident):
        data=request.get_json()
        with agent.lock:
            if ident in crew.session_ids():raise ValueError('Start Crew tasks from the Crew workspace')
            crew.assert_project_available(store.session(ident)['project_id'])
            agent.start(ident,str(data.get('prompt','')),data.get('mode','ask'),data.get('effort','high'),model=data.get('model') or None)
        return jsonify(ok=True,**agent.run_state(ident))

    @app.post('/api/sessions/<ident>/stop')
    def stop(ident):
        if ident in crew.session_ids():raise ValueError('Stop Crew tasks from the Crew workspace')
        store.session(ident)
        run=agent.runs.get(ident)
        if run: threading.Thread(target=run.stop,daemon=True).start()
        return jsonify(ok=True)

    @app.post('/api/sessions/<ident>/approve')
    def approve(ident):
        data=request.get_json()
        agent.approve(ident,data['action_id'],data.get('allowed') is True)
        return jsonify(ok=True)

    def crew_data():
        data=request.get_json(silent=True)
        if not isinstance(data,dict):raise ValueError('Expected a JSON object')
        return data

    @app.get('/api/computer')
    def computer_state():return jsonify(agent.remote.state(request.args.get('project_id')))

    @app.post('/api/computer/login')
    def computer_login():
        if local_crew.has_pending_work():raise ValueError('Finish or stop Crew tasks before changing the GitHub connection')
        return jsonify(agent.remote.login())

    @app.get('/api/computer/list')
    def computer_list():return jsonify(agent.remote.list())

    @app.post('/api/computer/install')
    def computer_install():return jsonify(agent.remote.install(crew_data().get('codespace')))

    @app.post('/api/computer/target')
    def computer_target():
        data=crew_data()
        if crew.has_pending_work(data.get('project_id')):raise ValueError('Finish or stop this project’s Crew tasks before changing its computer')
        return jsonify(agent.remote.configure(data.get('project_id'),data))

    @app.post('/api/computer/cloud-crew')
    def computer_cloud_crew():
        data=crew_data();ident=data.get('project_id')
        with agent.lock:
            if data.get('enabled') is True:
                if data.get('credential_consent') is not True:raise ValueError('Confirm storing cloud credentials before enabling Cloud Crew')
                return jsonify(agent.remote.cloud.enable(ident,agent,local_crew,data))
            if data.get('enabled') is False:return jsonify(agent.remote.cloud.disable(ident))
        raise ValueError('Choose enable or disconnect')

    @app.post('/api/computer/upload-preview')
    def computer_upload_preview():
        ident=crew_data().get('project_id');folder=Path(store.project(ident)['path'])
        return jsonify(agent.remote.preview_upload(ident,folder,walk_files(folder)))

    @app.post('/api/computer/upload')
    def computer_upload():
        if crew.has_pending_work() or agent.remote.active(None):raise ValueError('Finish active tasks before copying local files')
        data=crew_data();return jsonify(agent.remote.upload(data.get('project_id'),data.get('token')))

    @app.post('/api/computer/stop')
    def computer_stop():return jsonify(agent.remote.stop(crew_data().get('codespace')))

    @app.post('/api/computer/resume')
    def computer_resume():return jsonify(agent.remote.resume(crew_data().get('project_id')))

    @app.get('/api/privacy/models')
    def privacy_models():return jsonify(agent.privacy.local_models())

    @app.post('/api/privacy/settings')
    def privacy_settings():
        data=crew_data()
        model=data.get('model','')
        if not isinstance(model,str) or len(model)>200 or any(c in model for c in ('\n','\r','\x00')):raise ValueError('Choose a valid local Ollama model')
        with store.lock:
            settings=store.settings();settings['privacy_local_model']=model.strip()
            temp=store.settings_path.with_suffix('.tmp');temp.write_text(json.dumps(settings),'utf-8');temp.replace(store.settings_path)
        return jsonify(ok=True,model=model.strip())

    @app.post('/api/sessions/<ident>/privacy')
    def privacy_choice(ident):
        data=crew_data()
        if agent.remote.cloud.mapping(ident,session=True):return jsonify(agent.remote.cloud.privacy(ident,data,agent))
        store.session(ident)
        agent.choose_privacy(ident,data.get('action_id',''),data.get('choice',''),data.get('model'))
        return jsonify(ok=True)

    @app.get('/api/crew')
    def crew_state():return jsonify(crew.state(request.args.get('project_id','')))

    @app.post('/api/crew/bots')
    def crew_save_bot():return jsonify(crew.save_bot(crew_data()))

    @app.post('/api/crew/brief')
    def crew_save_brief():
        data=crew_data()
        return jsonify(crew.save_brief(data.get('project_id',''),data.get('brief',''),data))

    @app.post('/api/crew/tasks')
    def crew_create_task():
        data=crew_data()
        with agent.lock:return jsonify(crew.create_task(data.get('project_id',''),data))

    @app.get('/api/crew/tasks/<ident>')
    def crew_task(ident):return jsonify(crew.task(ident,max(0,int(request.args.get('after',0)))))

    @app.post('/api/crew/tasks/<ident>/start')
    def crew_start_task(ident):
        data=crew_data()
        with agent.lock:return jsonify(crew.start_task(ident,data.get('prompt'),data))

    @app.post('/api/crew/tasks/<ident>/stop')
    def crew_stop_task(ident):return jsonify(crew.stop_task(ident))

    @app.post('/api/crew/tasks/<ident>/approve')
    def crew_approve_task(ident):
        data=crew_data()
        crew.approve(ident,data.get('action_id',''),data.get('allowed') is True)
        return jsonify(ok=True)


    def root(ident): return Path(store.project(ident)['path'])

    @app.get('/api/projects/<ident>/inspect-artifact')
    def inspect_generated_file(ident):
        if agent.remote.target(ident):return jsonify(agent.remote.rpc(ident,'inspect_artifact',{'path':request.args['path']}))
        from artifact_quality import inspect_file
        return jsonify(inspect_file(project_path(root(ident),request.args['path'])))

    @app.get('/api/projects/<ident>/files')
    def files(ident):
        store.project(ident)
        return jsonify(agent.remote.files(ident) if agent.remote.target(ident) else {'files':walk_files(root(ident))})

    @app.get('/api/projects/<ident>/file')
    def file_read(ident):
        if agent.remote.target(ident):return jsonify(agent.remote.rpc(ident,'file',{'path':request.args['path']}))
        path=project_path(root(ident),request.args['path'])
        return jsonify(path=request.args['path'],content=read_text(path))

    @app.post('/api/projects/<ident>/file')
    def file_write(ident):
        data=request.get_json()
        if agent.remote.target(ident):
            prepared=agent.remote.rpc(ident,'prepare_edit',{'path':data['path'],'content':data['content']})
            import hashlib
            if prepared['expected']!=hashlib.sha256(data.get('original','').encode()).hexdigest():return jsonify(error='This remote file changed. Reopen it before saving.'),409
            return jsonify(agent.remote.rpc(ident,'commit_edit',{'path':data['path'],'content':data['content'],'expected':prepared['expected']}))
        path=project_path(root(ident),data['path'])
        current=read_text(path) if path.exists() else ''
        if current != data.get('original',''):
            return jsonify(error='This file changed outside the editor. Reopen it before saving.'),409
        content=str(data['content'])
        if len(content.encode('utf-8'))>1_000_000: raise ValueError('File exceeds 1 MB')
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_name(path.name+'.beam-save-'+secrets.token_hex(4))
        try:
            raw=path.read_bytes() if path.exists() else b''
            output=content.replace('\r\n','\n')
            if b'\r\n' in raw and b'\n' not in raw.replace(b'\r\n',b''): output=output.replace('\n','\r\n')
            temp.write_bytes((b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+output.encode('utf-8'))
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.get('/api/projects/<ident>/git')
    def git(ident):
        target=agent.remote.target(ident)
        if target:
            if target['codespace'] in agent.remote.paused:return jsonify(output='',paused=True,error='Resume the computer in Project computer to inspect Git changes.')
            return jsonify(agent.remote.rpc(ident,'git_diff' if request.args.get('diff')=='1' else 'git_status'))
        return jsonify(git_info(root(ident),request.args.get('diff')=='1'))

    @app.get('/api/projects/<ident>/download')
    def download(ident):
        if agent.remote.target(ident):return send_file(io.BytesIO(agent.remote.download(ident,request.args['path'])),as_attachment=True,download_name=Path(request.args['path']).name)
        path=project_path(root(ident),request.args['path'])
        return send_file(path,as_attachment=True,download_name=path.name)

    @app.get('/api/projects/<ident>/preview')
    def preview(ident):
        if agent.remote.target(ident):
            suffix=Path(request.args['path']).suffix.lower()
            if suffix not in ('.png','.jpg','.jpeg','.webp','.gif','.pdf'):raise ValueError('Preview supports PDF and raster images')
            return send_file(io.BytesIO(agent.remote.download(ident,request.args['path'])),mimetype=mimetypes.guess_type(request.args['path'])[0])
        path=project_path(root(ident),request.args['path'])
        # Active HTML/SVG stays in the text editor. Never execute generated pages in the app origin.
        if path.suffix.lower() not in ('.png','.jpg','.jpeg','.webp','.gif','.pdf'):
            raise ValueError('Preview supports PDF and raster images. Open other files in the editor.')
        return send_file(path,mimetype=mimetypes.guess_type(path.name)[0],as_attachment=False)

    if cloud:
        from cloud_runtime import configure
        configure(app)
    return app


class NativeBridge:
    def __init__(self, app): self._app,self._window=app,None
    def choose_folder(self):
        import webview
        selected=self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not selected: return None
        return self._app.store.add_project(selected[0])
    def save_copy(self,project_id,relative):
        import webview
        remote=self._app.agent.remote.target(project_id)
        source=Path(relative) if remote else project_path(self._app.store.project(project_id)['path'],relative)
        if not remote and not source.is_file(): raise ValueError('File not found')
        selected=self._window.create_file_dialog(webview.FileDialog.SAVE,save_filename=source.name)
        if selected:
            if remote:Path(selected[0]).write_bytes(self._app.agent.remote.download(project_id,relative))
            else:shutil.copy2(source,selected[0])
            return {'saved':True,'path':selected[0]}
        return {'saved':False}
    def open_github_login(self):
        import webbrowser
        webbrowser.open('https://github.com/login/device')
        return True

    def import_history(self):
        import webview
        import sqlite3
        with self._app.store.lock:
            if self._app.store.db.execute('SELECT 1 FROM cloud_crew_links WHERE enabled=1 LIMIT 1').fetchone():raise ValueError('Disconnect Cloud Crew in Project computer before importing history so its credentials and links are not orphaned')
        if self._app.crew.has_pending_work() or any(r.status in ('running','approval','privacy','stopping') for r in self._app.agent.runs.values()):raise ValueError('Stop Code and Crew tasks before importing history')
        selected=self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not selected:return None
        source=Path(selected[0])
        if (source/'BeamData').is_dir():source=source/'BeamData'
        database=source/'beam.sqlite3'
        if not database.is_file():raise ValueError('Choose a BeamData folder or the portable Beam folder containing it')
        store=self._app.store
        if database.resolve()==(store.root/'beam.sqlite3').resolve():raise ValueError('This is already the current data folder')
        with closing(sqlite3.connect('file:'+database.resolve().as_posix()+'?mode=ro',uri=True)) as old:
            tables={r[0] for r in old.execute('SELECT name FROM sqlite_master WHERE type="table"')}
            if not {'projects','sessions','events'}.issubset(tables):raise ValueError('Not a compatible Beam history database')
            with self._app.crew.lock, self._app.agent.lock, store.lock:
                if self._app.crew.has_pending_work() or any(r.worker and r.worker.is_alive() for r in self._app.agent.runs.values()):raise ValueError('Stop Code and Crew tasks before importing history')
                with closing(sqlite3.connect(store.root/('before-import-'+str(int(time.time()))+'.sqlite3'))) as backup:store.db.backup(backup)
                old.backup(store.db)
                self._app.agent.runs.clear()
                self._app.crew.initialize()
                self._app.agent.remote.initialize()
        settings=source/'settings.json'
        if settings.is_file():
            saved=json.loads(settings.read_text('utf-8'));current=store.settings()
            for key in ('theme','ollama_key'):
                if key in saved and key not in current:current[key]=saved[key]
            store.settings_path.write_text(json.dumps(current),'utf-8')
        return {'ok':True,'sessions':len(store.sessions())}

    def import_google_client(self):
        import webview
        selected=self._window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=False,file_types=('Google client JSON (*.json)',))
        if not selected:return None
        path=Path(selected[0])
        if path.stat().st_size>100000:raise ValueError('This file is too large to be an OAuth client JSON')
        return self._app.agent.integrations.google.configure(json.loads(path.read_text('utf-8-sig')))
    def connect_google(self):
        import webbrowser
        result=self._app.agent.integrations.google.start()
        webbrowser.open(result['authorization_url'])
        return {'pending':True}

    def show_project(self,project_id):
        path=self._app.store.project(project_id)['path']
        if os.name=='nt': os.startfile(path)
        return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--headless',action='store_true',help='Local browser development server')
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--data-dir')
    parser.add_argument('--token-file',help='Development only: write local API token to this file')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--self-test-output')
    args=parser.parse_args()
    if args.self_test:
        from enhanced_artifacts import self_test
        try:report=self_test()
        except Exception:
            import traceback
            report={'ok':False,'error':traceback.format_exc()}
        result=json.dumps(report)
        if args.self_test_output: Path(args.self_test_output).write_text(result,'utf-8')
        elif sys.stdout: print(result)
        if not report['ok']:raise SystemExit(1)
        return
    portable=Path(sys.executable).parent/'portable.flag' if getattr(sys,'frozen',False) else None
    default_data=portable.parent/'BeamData' if portable and portable.exists() else Path(os.environ.get('LOCALAPPDATA',Path.home()))/'Beam'
    directory=Path(args.data_dir or os.environ.get('BEAM_DATA_DIR') or default_data)
    directory.mkdir(parents=True,exist_ok=True)
    instance_lock=None
    if os.name=='nt' and not args.headless:
        import msvcrt
        instance_lock=(directory/'beam.lock').open('a+b')
        instance_lock.seek(0);instance_lock.write(b'0');instance_lock.flush();instance_lock.seek(0)
        try:msvcrt.locking(instance_lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            handle=ctypes.windll.user32.FindWindowW(None,'Beam')
            if handle:ctypes.windll.user32.ShowWindow(handle,9);ctypes.windll.user32.SetForegroundWindow(handle)
            return
    logging.basicConfig(filename=directory/'beam.log',level=logging.WARNING,format='%(asctime)s %(levelname)s %(message)s')
    app=create_app(directory)
    server=make_server('127.0.0.1',args.port,app,threaded=True)
    url=f'http://127.0.0.1:{server.server_port}'
    if args.token_file: Path(args.token_file).write_text(app.config['BEAM_TOKEN'],'utf-8')
    threading.Thread(target=server.serve_forever,daemon=True).start()
    if args.headless:
        print(url,flush=True) if sys.stdout else None
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: pass
        finally:
            app.agent.remote.close()
            app.crew.close()
            app.agent.servers.close()
            app.agent.integrations.close()
            server.shutdown()
        return
    import webview
    if os.name=='nt':ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Beam.Desktop')
    api=NativeBridge(app)
    window=webview.create_window('Beam',url,js_api=api,width=1440,height=940,min_size=(1050,700),background_color='#262624',text_select=True)
    api._window=window
    def close():
        app.agent.remote.close()
        app.crew.close()
        for run in list(app.agent.runs.values()):
            threading.Thread(target=run.stop,daemon=True).start()
        app.agent.servers.close()
        threading.Thread(target=app.agent.integrations.close,daemon=True).start()
        server.shutdown()
    window.events.closed += close
    webview.start(gui='edgechromium' if os.name=='nt' else None,private_mode=False,storage_path=str(directory/'webview'))


if __name__=='__main__':
    try:
        main()
    except Exception:
        logging.exception('Beam startup failed')
        if not sys.stdout:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'Beam could not start. See %LOCALAPPDATA%\\Beam\\beam.log for details. Microsoft Edge WebView2 Runtime is required.','Beam',0x10)
        else:
            raise
        sys.exit(1)
