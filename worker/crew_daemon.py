"""Persistent Crew coordinator, private to the Codespace account via Unix socket.

Disconnecting the desktop closes an SSH client, not this process. Provider idle
and maximum-lifetime limits still apply. No public HTTP port is opened.
"""
import json, os, re, socket, socketserver, subprocess, sys, threading, time
from pathlib import Path

HERE=Path(__file__).resolve().parent
DATA=Path('/workspaces/.beam-crew')
SOCKET=DATA/'crew.sock'

def call(request,timeout=60):
    DATA.mkdir(parents=True,exist_ok=True);os.chmod(DATA,0o700)
    def connect():
        s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(timeout)
        try:s.connect(str(SOCKET));return s
        except OSError:s.close();raise
    try:connection=connect()
    except OSError:
        # A short-lived lock protects concurrent first connections.
        import fcntl
        with (DATA/'launch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:connection=connect()
            except OSError:
                if SOCKET.exists():SOCKET.unlink()
                with (DATA/'service.log').open('ab') as log:
                    subprocess.Popen([sys.executable,str(HERE/'crew_daemon.py')],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
                deadline=time.monotonic()+30
                while True:
                    try:connection=connect();break
                    except OSError:
                        if time.monotonic()>deadline:raise ValueError('Cloud Crew did not start. Reinstall the worker and check its setup.')
                        time.sleep(.15)
    with connection:
        connection.sendall((json.dumps(request)+'\n').encode())
        with connection.makefile('rb') as stream:raw=stream.readline(64000001)
    if len(raw)>64000000:raise ValueError('Cloud Crew response is too large')
    answer=json.loads(raw)
    if 'error' in answer:raise ValueError(answer['error'])
    return answer['result']

class Service:
    def __init__(self,directory=None,base=None):
        from beam_core import Agent,Store
        from crew import Crew
        from privacy_guard import PrivacyGuard,PrivacyPause,detect_sensitive
        import runner
        self.root=Path(directory or DATA);self.root.mkdir(parents=True,exist_ok=True);os.chmod(self.root,0o700)
        self.credentials=self.root/'credentials.json';self.store=Store(self.root);self.lock=threading.RLock()
        self.last_work=time.monotonic();self.about_to_stop=False
        if base is not None:runner.BASE=Path(base)
        self.runner=runner
        with self.store.lock,self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS cloud_runtime_projects(project_id TEXT PRIMARY KEY,enabled INTEGER,trusted INTEGER,auto_stop INTEGER,codespace TEXT)')
        service=self
        class DesktopPrivate(PrivacyGuard):
            def __init__(self,store):super().__init__(store);self.local=threading.local()
            def prepare(self,payload,run,checkpoint,emit=None):
                run.private_material=detect_sensitive(payload);self.local.run=run
                return super().prepare(payload,run,checkpoint,emit)
            def _model(self,selected):
                if not getattr(self.local.run,'desktop_private_done',False):raise PrivacyPause('Choose local processing from the desktop app. Private details remain paused.')
                return selected
            def _summarize(self,findings,model):
                # Actual local summaries stay on the desktop. Only confirmation
                # arrives here so the originals can be redacted before cloud AI.
                self.local.run.desktop_private_done=False
                return None
        class CloudAgent(Agent):
            def start(self,ident,prompt,mode='ask',effort='high',**kwargs):
                pid=self.store.session(ident)['project_id']
                if not service.enabled(pid):raise ValueError('Enable Cloud Crew for this project first')
                kwargs['extra_context']=kwargs.get('extra_context','')+'\nThis Crew runs independently inside the Linux Codespace, including after the desktop closes, while the provider keeps this machine running. Use bash and python3. Commands use a restricted task account. Desktop Google, MCP and browser sessions are not connected here. Pause for approvals, private information or essential questions; do not invent a desktop response.'
                if service.trusted(pid) and mode=='edit':kwargs['extra_context']+='\nOrdinary task commands are already authorized for this Codespace. Do not request separate approval for routine builds and checks. Privilege and publishing actions still need approval.'
                kwargs['disabled_tools']={'browser_open','browser_snapshot','browser_action','save_browser_download','mcp_call','mcp_list_tools','slides_get','slides_create','slides_update','slides_thumbnail','google_files','start_server','server_status','stop_server'}
                service.last_work=time.monotonic();service.about_to_stop=False
                return super().start(ident,prompt,mode,effort,**kwargs)
            def request_approval(self,ident,run,name,args,diff=''):
                pid=self.store.session(ident)['project_id']
                if name=='run_command' and service.trusted(pid) and getattr(run,'permission_mode','ask')=='edit':
                    command=args.get('command','')
                    if not re.search(r'(?i)\b(sudo|su|ssh|scp|mount|umount)\b|\bgit\s+push\b|\brm\s+[^\n]*-[^\n]*r|\b(?:curl|wget)\b',command):return True
                return super().request_approval(ident,run,name,args,diff)
            def execute(self,ident,run,root,mode,name,args):
                if 'path' in args:runner.path_in(Path(root),args['path'])
                return super().execute(ident,run,root,mode,name,args)
            def _execute_tool(self,ident,run,root,mode,name,args,edit_context=('', '', '')):
                if name in {'list_files','read_file','search_files','inspect_project','runtime_info'}:
                    return runner.dispatch({'project_id':self.store.session(ident)['project_id'],'operation':name,'arguments':args})
                if name=='run_command':
                    run.command_count+=1;self.store.emit(ident,'command_stats',count=run.command_count,budget=run.command_budget,reused=False)
                    return runner.command(Path(root),args,run.cancelled,on_output=lambda chunk:self.store.emit(ident,'terminal',text=chunk))
                return super()._execute_tool(ident,run,root,mode,name,args,edit_context)
        self.agent=CloudAgent(self.store);self.agent.privacy=DesktopPrivate(self.store)
        self.store.key=lambda:self.secrets().get('ollama_key','')
        self.crew=Crew(self.store,self.agent)
        self.closed=threading.Event();threading.Thread(target=self.idle,daemon=True).start()

    def secrets(self):
        try:return json.loads(self.credentials.read_text('utf-8'))
        except (OSError,ValueError):return {}
    def rows(self):
        with self.store.lock:return [dict(r) for r in self.store.db.execute('SELECT * FROM cloud_runtime_projects WHERE enabled=1')]
    def enabled(self,pid):return any(r['project_id']==pid for r in self.rows())
    def trusted(self,pid):return any(r['project_id']==pid and r['trusted'] for r in self.rows())
    def busy(self):return self.crew.has_pending_work()

    def doing_work(self):
        if self.crew._rows("SELECT id FROM crew_tasks WHERE status IN ('running','queued','stopping') LIMIT 1"):return True
        for lease in (self.root/'leases').glob('*.json'):
            try:
                if json.loads(lease.read_text())['until']>time.time():return True
            except (OSError,ValueError,KeyError):pass
        return False

    def dispatch(self,request):
        pid=request['project_id'];op=request['operation'];data=request.get('arguments',{})
        root=self.runner.project(pid)
        if op in ('cloud_enable','cloud_create_task','cloud_start_task'):
            with self.store.lock:self.store.save_settings({'command_budget':data.get('command_budget',8)})
        if op=='cloud_enable':
            key=data.get('ollama_key','');github=data.get('github_token','');name=data.get('codespace','')
            if not isinstance(key,str) or not key or len(key)>1000 or not isinstance(github,str) or not github or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9-]{0,119}',name):raise ValueError('Cloud credentials or Codespace identity are missing')
            temporary=self.credentials.with_suffix('.new')
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:json.dump({'ollama_key':key,'github_token':github},f)
            os.chmod(temporary,0o600);temporary.replace(self.credentials)
            with self.store.lock,self.store.db:
                previous=self.store.db.execute('SELECT * FROM cloud_runtime_projects WHERE project_id=?',(pid,)).fetchone()
                self.store.db.execute('INSERT OR IGNORE INTO projects VALUES (?,?,?)',(pid,str(root),str(data.get('name','Workspace'))[:120]))
                self.store.db.execute('INSERT OR REPLACE INTO cloud_runtime_projects VALUES (?,?,?,?,?)',(pid,1,int(data.get('trusted',False)),int(data.get('auto_stop',False)),name))
            if previous is None:
                for bot in data.get('bots',[]):
                    ident=bot.get('id','')
                    if not re.fullmatch('[A-Za-z0-9_-]{1,64}',ident):continue
                    with self.store.lock,self.store.db:
                        self.store.db.execute('INSERT OR IGNORE INTO crew_bots(id,name,role,instructions,memory,color,archived,updated,avatar) VALUES (?,?,?,?,?,?,?,?,?)',(ident,'Member','Custom','','','#bd9773',0,time.time(),''))
                    self.crew.save_bot(bot)
                self.crew.save_brief(pid,str(data.get('brief',''))[:20000])
            self.last_work=time.monotonic();self.about_to_stop=False
            return {'enabled':True}
        if not self.enabled(pid):raise ValueError('Cloud Crew is not enabled for this project')
        if op=='cloud_state':
            state=self.crew.state(pid)
            for task in state['tasks']:task['cloud']=True
            return {**state,'cloud':True,'trusted':self.trusted(pid),'busy':self.busy(),'about_to_stop':self.about_to_stop}
        if op=='cloud_save_bot':return self.crew.save_bot(data)
        if op=='cloud_save_brief':return self.crew.save_brief(pid,data['brief'])
        if op=='cloud_config':
            if self.busy():raise ValueError('Finish active cloud tasks before changing permissions')
            with self.store.lock,self.store.db:self.store.db.execute('UPDATE cloud_runtime_projects SET trusted=?,auto_stop=? WHERE project_id=?',(int(data.get('trusted',False)),int(data.get('auto_stop',False)),pid))
            return {'ok':True}
        if op=='cloud_create_task':return {**self.crew.create_task(pid,data),'cloud':True}
        ident=data.get('task_id')
        if ident:
            row=self.crew._row(ident)
            if row['project_id']!=pid:raise ValueError('Task belongs to another project')
        if op=='cloud_task':
            detail=self.crew.task(ident,max(0,int(data.get('after',0))));detail['task']['cloud']=True;return detail
        if op=='cloud_start_task':return self.crew.start_task(ident,data.get('prompt'))
        if op=='cloud_stop_task':return self.crew.stop_task(ident)
        if op=='cloud_approve':
            self.agent.approve(row['session_id'],data['action_id'],data.get('allowed') is True);return {'ok':True}
        if op in ('cloud_private_material','cloud_privacy'):
            run=self.agent.runs.get(row['session_id'])
            if not run or not run.privacy_pending or run.privacy_pending['action_id']!=data.get('action_id'):raise ValueError('This privacy checkpoint is no longer pending')
            if op=='cloud_private_material':return {'findings':getattr(run,'private_material',[])}
            if data.get('choice')=='local':
                if data.get('local_completed') is not True:raise ValueError('Complete local processing on the desktop first')
                run.desktop_private_done=True
            self.agent.choose_privacy(row['session_id'],data['action_id'],data['choice'],data.get('model'));return {'ok':True}
        if op=='cloud_disable':
            if self.busy():raise ValueError('Finish or stop cloud tasks before disconnecting Cloud Crew')
            with self.store.lock,self.store.db:self.store.db.execute('UPDATE cloud_runtime_projects SET enabled=0 WHERE project_id=?',(pid,))
            if not self.rows():self.credentials.unlink(missing_ok=True)
            return {'enabled':False}
        raise ValueError('Unknown Cloud Crew operation')

    def idle(self):
        while not self.closed.wait(15):
            try:
                if self.doing_work():self.last_work=time.monotonic();continue
                rows=self.rows()
                if not rows or not all(r['auto_stop'] for r in rows) or time.monotonic()-self.last_work<300:continue
                self.about_to_stop=True
                self.closed.wait(5)
                if self.doing_work():self.about_to_stop=False;continue
                import requests
                token=self.secrets().get('github_token','')
                if not token:continue
                name=rows[0]['codespace']
                with requests.Session() as session:
                    session.trust_env=False
                    session.post('https://api.github.com/user/codespaces/'+name+'/stop',headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'},timeout=(5,15),allow_redirects=False).raise_for_status()
                self.closed.set()
            except Exception:
                self.about_to_stop=False;self.last_work=time.monotonic()

    def close(self):
        self.closed.set();self.crew.close();self.agent.remote.close();self.agent.servers.close();self.agent.integrations.close()

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir');parser.add_argument('--project-base');args=parser.parse_args()
    global DATA,SOCKET
    if args.data_dir:DATA=Path(args.data_dir);SOCKET=DATA/'crew.sock'
    DATA.mkdir(parents=True,exist_ok=True);os.chmod(DATA,0o700);os.umask(0o002)
    service=Service(DATA,args.project_base)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                raw=self.rfile.readline(32000001)
                if len(raw)>32000000:raise ValueError('Request too large')
                answer={'result':service.dispatch(json.loads(raw))}
            except Exception as error:answer={'error':str(error)[:1500]}
            self.wfile.write((json.dumps(answer)+'\n').encode())
    class Server(socketserver.ThreadingUnixStreamServer):daemon_threads=True
    if SOCKET.exists():SOCKET.unlink()
    with Server(str(SOCKET),Handler) as server:
        os.chmod(SOCKET,0o600)
        try:server.serve_forever()
        finally:service.close()

if __name__=='__main__':main()
