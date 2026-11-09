"""GitHub Codespaces transport. GitHub CLI owns authentication and SSH keys."""
from __future__ import annotations
import base64, io, json, os, queue, re, shlex, shutil, subprocess, sys
import threading, time, uuid
from pathlib import Path
from agent_runtime import clean_environment, WindowsJob, stop_process, external_launch

VERSION='0.7.0'
REMOTE_DIR='/workspaces/.beam-worker/0.7.0'
RESOURCE=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))
OPERATIONS={'list_files','read_file','search_files','write_file','edit_file','run_command',
            'git_status','git_diff','generate_artifact','generate_model','inspect_artifact',
            'runtime_info','inspect_project'}
BLOCKED={'start_server','server_status','stop_server','browser_open','browser_snapshot',
         'browser_action','save_browser_download','mcp_call','mcp_list_tools'}

def safe_name(name):
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,119}',name):
        raise ValueError('Choose a Codespace from Refresh computers')
    return name

class RemoteComputers:
    def __init__(self,store):
        self.store=store;self.lock=threading.RLock();self.mutation_lock=threading.RLock();self.last_use={};self.paused=set();self.files_cache={}
        self.uploads={};self.auth_process=None
        self.job={'status':'idle','log':''};self.auth={'status':'idle','code':'','url':'https://github.com/login/device'}
        self.closed=threading.Event();self.active=lambda _:False
        with store.lock,store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS remote_projects(project_id TEXT PRIMARY KEY,codespace TEXT,trusted INTEGER,auto_stop INTEGER)')
            store.db.execute('CREATE TABLE IF NOT EXISTS remote_saved_projects(project_id TEXT PRIMARY KEY,codespace TEXT,trusted INTEGER,auto_stop INTEGER)')
            self.paused={r['codespace'] for r in store.db.execute('SELECT codespace FROM remote_projects')}
        from cloud_client import CloudClient
        self.cloud=CloudClient(store,self)
        self.thread=threading.Thread(target=self._idle_loop,daemon=True);self.thread.start()

    def initialize(self):
        """Restore connection tables after a legacy database import."""
        with self.store.lock,self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS remote_projects(project_id TEXT PRIMARY KEY,codespace TEXT,trusted INTEGER,auto_stop INTEGER)')
            self.store.db.execute('CREATE TABLE IF NOT EXISTS remote_saved_projects(project_id TEXT PRIMARY KEY,codespace TEXT,trusted INTEGER,auto_stop INTEGER)')
            self.paused={r['codespace'] for r in self.store.db.execute('SELECT codespace FROM remote_projects')}
        self.last_use.clear();self.files_cache.clear();self.uploads.clear();self.cloud.pending.clear();self.cloud.initialize()

    def gh(self):
        bundled=RESOURCE/'worker/github/gh.exe'
        if os.name=='nt' and bundled.is_file():return str(bundled)
        path=shutil.which('gh.exe' if os.name=='nt' else 'gh')
        if not path and os.name=='nt':
            candidate=Path(os.environ.get('ProgramFiles','C:/Program Files'))/'GitHub CLI/gh.exe'
            if candidate.is_file():path=str(candidate)
        if not path:raise ValueError('The GitHub connection helper is missing. Reinstall this version of Beam.')
        return path

    def environment(self):
        env=clean_environment()
        for key in list(env):
            if key in {'GH_TOKEN','GITHUB_TOKEN','OLLAMA_API_KEY'} or key.endswith(('_API_KEY','_API_TOKEN')):env.pop(key,None)
        env.update(GH_CONFIG_DIR=str(self.store.root/'github'),GH_HOST='github.com',GH_PROMPT_DISABLED='1',GH_NO_UPDATE_NOTIFIER='1',NO_COLOR='1')
        return env

    def spawn(self,args,own_tree=True,allow_prompts=False,**kwargs):
        env=self.environment()
        if allow_prompts:env.pop('GH_PROMPT_DISABLED',None)
        with external_launch():
            process=subprocess.Popen(args,env=env,**self.launch_options(),**kwargs)
        if os.name=='nt' and own_tree:
            try:process._beam_job=WindowsJob(process)
            except OSError:process._beam_job=None
        return process

    def login(self):
        if self.active(None):raise ValueError('Finish or stop active tasks before changing the GitHub connection')
        with self.lock:
            if self.auth['status']=='waiting':return dict(self.auth)
            self.auth={'status':'waiting','code':'','url':'https://github.com/login/device'}
        def authenticate():
            try:
                process=self.spawn([self.gh(),'auth','login','--hostname','github.com','--git-protocol','https','--web','--scopes','codespace','--skip-ssh-key'],own_tree=False,allow_prompts=True,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                self.auth_process=process
                watchdog=threading.Timer(900,lambda:process.terminate() if process.poll() is None else None);watchdog.daemon=True;watchdog.start()
                process.stdin.write(b'\n');process.stdin.flush();captured='';start=time.monotonic()
                # Device authorization expires at GitHub. Only its short code is
                # exposed in the UI; tokens and CLI output never enter a chat.
                while True:
                    byte=process.stdout.read(1)
                    if not byte:break
                    captured=(captured+byte.decode('utf-8','replace'))[-4000:]
                    code=re.search(r'\b[A-Z0-9]{4}-[A-Z0-9]{4}\b',captured)
                    if code:self.auth['code']=code.group()
                    if time.monotonic()-start>900:process.terminate();break
                process.wait(timeout=10);process.stdin.close();process.stdout.close()
                watchdog.cancel()
                self.auth['status']='done' if process.returncode==0 else 'failed'
            except Exception:self.auth['status']='failed'
        threading.Thread(target=authenticate,daemon=True).start();return dict(self.auth)

    @staticmethod
    def launch_options():
        return {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}

    def cli(self,args,timeout=30):
        process=self.spawn([self.gh(),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:output,_=process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(process);process.communicate();raise ValueError('GitHub connection timed out. Check your connection and try again.') from None
        finally:stop_process(process)
        if process.returncode:raise ValueError('GitHub could not complete the request. Choose Connect GitHub again and approve Codespaces access. No local fallback was used.')
        return output.decode('utf-8','replace')

    def list(self):
        try:
            rows=json.loads(self.cli(['codespace','list','--json','name,displayName,state,machineName,repository']))
            return {'available':True,'computers':rows}
        except ValueError as error:return {'available':False,'computers':[],'error':str(error)}

    def target(self,project_id):
        with self.store.lock:
            try:row=self.store.db.execute('SELECT * FROM remote_projects WHERE project_id=?',(project_id,)).fetchone()
            except __import__('sqlite3').OperationalError:
                # Legacy history imports can replace the database.
                with self.store.db:self.store.db.execute('CREATE TABLE IF NOT EXISTS remote_projects(project_id TEXT PRIMARY KEY,codespace TEXT,trusted INTEGER,auto_stop INTEGER)')
                row=None
        if not row:return None
        result=dict(row);result['cloud_crew']=bool(self.cloud.link(project_id));return result

    def state(self,project_id=None):
        target=self.target(project_id) if project_id else None
        saved=None
        if project_id:
            with self.store.lock:
                row=self.store.db.execute('SELECT * FROM remote_saved_projects WHERE project_id=?',(project_id,)).fetchone()
                saved=dict(row) if row else None
        return {'target':target,'paused':bool(target and target['codespace'] in self.paused),
                'saved_target':saved,'setup':dict(self.job),'auth':dict(self.auth),
                'cli_installed':bool((RESOURCE/'worker/github/gh.exe').is_file() or shutil.which('gh'))}

    def configure(self,project_id,data):
        with self.mutation_lock:return self._configure(project_id,data)

    def _configure(self,project_id,data):
        self.store.project(project_id)
        cloud=self.cloud.link(project_id)
        if cloud:
            if data.get('target')=='local' or data.get('codespace')!=cloud['codespace']:raise ValueError('Disconnect Cloud Crew before changing its computer')
            state=self.cloud.state(project_id,force=True)
            if state.get('busy'):raise ValueError('Finish or stop cloud tasks before changing their permissions')
        if self.active(None):raise ValueError('Finish or stop active Studio and Crew tasks before changing the computer')
        if data.get('target')=='local':
            with self.store.lock,self.store.db:
                active=self.store.db.execute('SELECT * FROM remote_projects WHERE project_id=?',(project_id,)).fetchone()
                if active:self.store.db.execute('INSERT OR REPLACE INTO remote_saved_projects VALUES (?,?,?,?)',(project_id,active['codespace'],active['trusted'],active['auto_stop']))
                self.store.db.execute('DELETE FROM remote_projects WHERE project_id=?',(project_id,))
            return self.state(project_id)
        name=safe_name(data.get('codespace'))
        # One activation request verifies the worker and returns the initial file
        # tree, avoiding a second SSH connection immediately after switching.
        info=self.rpc_name(name,project_id,'activate',{},timeout=60)
        if info.get('version')!=VERSION or not info.get('ready'):raise ValueError('Install this version of the Beam worker first')
        for key in ('trusted','auto_stop'):
            if not isinstance(data.get(key,False),bool):raise ValueError(key+' must be true or false')
        if cloud:self.rpc_name(name,project_id,'cloud_config',{'trusted':data.get('trusted',False),'auto_stop':data.get('auto_stop',False)})
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO remote_projects VALUES (?,?,?,?)',(project_id,name,int(data.get('trusted',False)),int(data.get('auto_stop',False))))
            self.store.db.execute('INSERT OR REPLACE INTO remote_saved_projects VALUES (?,?,?,?)',(project_id,name,int(data.get('trusted',False)),int(data.get('auto_stop',False))))
        self.files_cache[project_id]=info.get('files',[])
        return {**self.state(project_id),'files':self.files_cache[project_id]}

    def _stream(self,argv,payload,timeout,cancelled=None,on_event=None):
        process=self.spawn(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        lines=queue.Queue();errors=[]
        written=threading.Event()
        def write_request():
            try:process.stdin.write(payload);process.stdin.flush()
            except (OSError,ValueError):lines.put({'event':'error','message':'Connection closed before the request reached the worker'})
            finally:written.set()
        def read_out():
            try:
                while True:
                    line=process.stdout.readline(64000001)
                    if not line:break
                    if len(line)>64000000:lines.put({'event':'error','message':'Worker response exceeds the transfer limit'});break
                    try:lines.put(json.loads(line))
                    except ValueError:continue
            finally:lines.put(None)
        def read_err():
            while process.stderr.read(8192):pass  # Never expose SSH credentials or remote startup noise.
        reader=threading.Thread(target=read_out,daemon=True);reader.start()
        err_reader=threading.Thread(target=read_err,daemon=True);err_reader.start()
        writer=threading.Thread(target=write_request,daemon=True);writer.start()
        started=time.monotonic();answer=None;failure=None;stop_time=None
        try:
            while True:
                stopped=cancelled is not None and cancelled.is_set()
                if stopped or time.monotonic()-started>timeout:
                    if stop_time is None:
                        stop_time=time.monotonic()
                        if written.is_set():process.stdin.close()
                        else:stop_process(process)
                    if time.monotonic()-stop_time>12:break
                try:item=lines.get(timeout=.15)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():break
                    continue
                if item is None:break
                if item.get('event')=='result':answer=item['result'];break
                if item.get('event')=='error':failure=item.get('message','Remote worker failed');break
                if on_event:on_event(item)
            if cancelled is not None and cancelled.is_set():raise ValueError('Remote task stopped')
            if stop_time is not None:raise ValueError('Remote request timed out; the worker was asked to stop')
            if failure:raise ValueError(str(failure)[:1500])
            if answer is None:raise ValueError('The remote worker disconnected. Check Codespaces is available and the worker is installed. Execution did not fall back to this PC.')
            return answer
        finally:
            if not written.is_set():stop_process(process)
            writer.join(2)
            if not process.stdin.closed:
                try:process.stdin.close()
                except OSError:pass
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:stop_process(process);process.wait(timeout=5)
            stop_process(process)
            process.stdout.close();process.stderr.close();reader.join(1);err_reader.join(1)

    def rpc_name(self,name,project_id,operation,arguments,timeout=180,cancelled=None,on_event=None):
        safe_name(name)
        if not re.fullmatch('[a-f0-9]{32}',project_id):raise ValueError('Invalid project identity')
        command=f'{REMOTE_DIR}/venv/bin/python {REMOTE_DIR}/runner.py'
        argv=[self.gh(),'codespace','ssh','-c',name,'--','-T','-o','BatchMode=yes',command]
        payload=(json.dumps({'project_id':project_id,'operation':operation,'arguments':arguments})+'\n').encode()
        if len(payload)>32000000:raise ValueError('Remote request exceeds 32 MB')
        self.paused.discard(name);self.last_use[name]=time.monotonic()
        try:return self._stream(argv,payload,timeout,cancelled,on_event)
        finally:self.last_use[name]=time.monotonic()

    def rpc(self,project_id,operation,arguments=None,**kwargs):
        target=self.target(project_id)
        if not target:raise ValueError('This project uses the local computer')
        return self.rpc_name(target['codespace'],project_id,operation,arguments or {},**kwargs)

    def files(self,project_id):
        target=self.target(project_id)
        if target['codespace'] in self.paused:return {'files':self.files_cache.get(project_id,[]),'paused':True}
        result=self.rpc(project_id,'list_files');self.files_cache[project_id]=result['files'];return result

    def download(self,project_id,path):
        target=self.target(project_id)
        if target['codespace'] in self.paused:raise ValueError('Resume the computer before opening a remote file')
        return base64.b64decode(self.rpc(project_id,'download',{'path':path})['data'],validate=True)

    def stop(self,name):
        safe_name(name)
        if self.active(name):raise ValueError('Stop the active Beam task before stopping its computer')
        with self.store.lock:links=self.store.db.execute('SELECT project_id FROM cloud_crew_links WHERE enabled=1 AND codespace=?',(name,)).fetchall()
        for link in links:
            if self.cloud.state(link['project_id'],force=True).get('busy'):raise ValueError('Finish or stop Cloud Crew tasks before stopping this computer')
        self.cli(['codespace','stop','-c',name],timeout=40);self.paused.add(name)
        return {'ok':True,'paused':True}

    def resume(self,project_id):return self.rpc(project_id,'status',timeout=60)

    def preview_upload(self,project_id,root,entries):
        if not self.target(project_id):raise ValueError('Connect this project to a Codespace first')
        rows=[];total=0
        for entry in entries:
            original=root/entry['path'];p=original.resolve()
            if original.is_symlink() or not p.is_relative_to(root.resolve()):continue
            if any(part.lower().startswith('.env') or part.lower() in {'credentials.json','id_rsa','id_ed25519','.npmrc','.pypirc'} or part.lower().endswith(('.pem','.key','.pfx')) for part in p.relative_to(root.resolve()).parts):continue
            raw=p.read_bytes();total+=len(raw)
            if len(rows)>=500 or total>20000000:raise ValueError('Choose a smaller local project: upload limit is 500 files / 20 MB')
            rows.append({'path':entry['path'],'data':base64.b64encode(raw).decode(),'bytes':len(raw)})
        token=uuid.uuid4().hex
        self.uploads={token:(project_id,self.target(project_id)['codespace'],rows,time.monotonic())}
        return {'token':token,'files':[{'path':r['path'],'bytes':r['bytes']} for r in rows],'bytes':total}

    def upload(self,project_id,token):
        value=self.uploads.pop(token,None)
        if not value or value[0]!=project_id or time.monotonic()-value[3]>300:raise ValueError('Refresh the upload preview before copying files')
        if self.target(project_id)['codespace']!=value[1]:raise ValueError('Computer changed. Refresh the upload preview.')
        return self.rpc(project_id,'upload',{'files':value[2]},timeout=180)

    def install(self,name):
        safe_name(name)
        if self.active(name):raise ValueError('Finish active tasks on this computer before reinstalling its worker')
        with self.store.lock:links=self.store.db.execute('SELECT project_id FROM cloud_crew_links WHERE codespace=? AND enabled=1',(name,)).fetchall()
        for link in links:
            if self.cloud.state(link['project_id'],force=True).get('busy'):raise ValueError('Finish or stop Cloud Crew before reinstalling its worker')
        with self.lock:
            if self.job['status']=='running':raise ValueError('Worker installation is already running')
            self.job={'status':'running','log':'Uploading worker and installing its Python dependencies…'}
        def setup():
            try:
                archive=(RESOURCE/'worker/worker.zip').read_bytes()
                # The uploaded archive contains only application sources and public requirements.
                script="import sys,io,zipfile,pathlib,subprocess,venv; p=pathlib.Path('"+REMOTE_DIR+"'); p.mkdir(parents=True,exist_ok=True); z=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())); assert all('/' not in n and '\\\\' not in n and n not in ('.','..') for n in z.namelist()); z.extractall(p); venv.EnvBuilder(with_pip=True).create(p/'venv'); subprocess.check_call([str(p/'venv/bin/python'),'-m','pip','install','--disable-pip-version-check','-r',str(p/'requirements.txt')]); subprocess.check_call([str(p/'venv/bin/python'),str(p/'setup.py')])"
                command='python3 -c '+shlex.quote(script)
                process=self.spawn([self.gh(),'codespace','ssh','-c',name,'--','-T','-o','BatchMode=yes',command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:process.communicate(input=archive,timeout=1200)
                finally:
                    stop_process(process)
                    process.communicate()
                if process.returncode:raise ValueError('Worker setup failed. Reconnect GitHub and check that this Codespace supports SSH and Python. See the setup guide.')
                self.job={'status':'done','log':'Worker installed. Select Use Codespace to connect this project.'}
                self.last_use[name]=time.monotonic()
            except Exception as error:self.job={'status':'failed','log':str(error)[:1500]}
        threading.Thread(target=setup,daemon=True).start();return dict(self.job)

    def _idle_loop(self):
        while not self.closed.wait(15):
            try:
                if self.job['status']=='running':continue
                with self.store.lock:rows=self.store.db.execute('SELECT DISTINCT codespace FROM remote_projects WHERE auto_stop=1').fetchall()
                for row in rows:
                    name=row['codespace']
                    with self.store.lock:
                        if self.store.db.execute('SELECT 1 FROM cloud_crew_links WHERE codespace=? AND enabled=1',(name,)).fetchone():continue
                    if name in self.paused or name not in self.last_use or self.active(name):continue
                    if time.monotonic()-self.last_use[name]>=300:self.stop(name)
            except __import__('sqlite3').ProgrammingError:
                break
            except Exception:
                if self.closed.is_set():break

    def close(self):
        self.closed.set()
        p=self.auth_process
        if p and p.poll() is None:p.terminate()

def execute_remote(agent,ident,run,project_id,mode,name,args):
    """Keep the same permission and event contract as local tools."""
    if name in BLOCKED:
        raise ValueError('This tool is not supported on the remote computer. Remote tasks cannot launch a browser, MCP server or persistent server on your PC. Use web_search/web_open for research, or switch the project to Local for desktop connections.')
    mutation=name in {'write_file','edit_file','run_command','generate_artifact','generate_model'}
    if mutation and mode=='plan':raise ValueError('Plan mode allows inspection only')
    if name=='run_command' and run.command_count>=run.command_budget:
        run.outcome='needs_attention';raise ValueError('Command budget reached. Review progress before continuing.')
    def rpc(operation,arguments=None):
        return agent.remote.rpc_name(run.remote['codespace'],project_id,operation,arguments or {},
            timeout=max(180,min(640,int(args.get('timeout_seconds',120))+40)) if name=='run_command' else 360,
            cancelled=run.cancelled,on_event=lambda e:agent.store.emit(ident,'terminal',text=e.get('text','')) if e.get('event')=='terminal' else None)
    prepared=None;diff=''
    if name in {'write_file','edit_file'}:
        prepared=rpc('prepare_edit',args);diff=prepared['diff']
    command=args.get('command','')
    # Trusted mode is consent to this Codespace's account permissions. Obvious
    # external publishing/privilege commands still receive an explicit prompt.
    sensitive=bool(re.search(r'(?i)\b(sudo|su|ssh|scp|mount|umount)\b|\bgit\s+push\b|\brm\s+[^\n]*-[^\n]*r|\b(?:curl|wget)\b',command))
    automatic=mode=='auto' or (name=='run_command' and mode=='edit' and run.remote['trusted'] and not sensitive)
    if mutation and mode!='auto' and (mode=='ask' or name=='run_command' and not automatic):
        display={**args,'execution':'GitHub Codespaces · '+run.remote['codespace']}
        if not agent.request_approval(ident,run,name,display,diff):return {'denied':True,'message':'Action declined. Do not retry by another route.'}
    if run.cancelled.is_set():raise ValueError('Task stopped')
    run.activity('building' if name.startswith('generate_') else 'tool','Remote · '+name.replace('_',' '))
    if name in {'write_file','edit_file'}:
        result=rpc('commit_edit',{**args,'expected':prepared['expected']})
        agent.store.emit(ident,'change',path=args['path'],diff=diff)
    else:
        if name=='run_command':
            run.command_count+=1;agent.store.emit(ident,'command_stats',count=run.command_count,budget=run.command_budget,reused=False)
        result=rpc(name,args)
        if name.startswith('generate_'):agent.store.emit(ident,'artifact',**result)
    return result
