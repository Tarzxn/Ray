"""Desktop facade for a persistent, SSH-only Crew coordinator."""
import json,time

PENDING={'queued','running','approval','privacy','waiting_team','waiting_input','stopping'}

class CloudTransferReview(ValueError):
    def __init__(self,categories):
        self.categories=sorted(set(categories))
        super().__init__('Review private details before sending this request to GitHub Codespaces')

def review_transfer(data,content):
    from privacy_guard import detect_sensitive
    findings=[f for f in detect_sensitive(content) if not f.get('intent')]
    if findings and data.get('cloud_transfer_consent') is not True:raise CloudTransferReview(f['category'] for f in findings)

class CloudClient:
    def __init__(self,store,remote):
        self.store=store;self.remote=remote;self.pending=set();self.survey={}
        self.initialize()

    def initialize(self):
        store=self.store
        with store.lock,store.db:
            store.db.executescript('''
            CREATE TABLE IF NOT EXISTS cloud_crew_links(project_id TEXT PRIMARY KEY,codespace TEXT,enabled INTEGER);
            CREATE TABLE IF NOT EXISTS cloud_crew_tasks(id TEXT PRIMARY KEY,project_id TEXT,codespace TEXT,session_id TEXT);
            CREATE TABLE IF NOT EXISTS cloud_crew_cache(id TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS cloud_local_summaries(action_id TEXT PRIMARY KEY,text TEXT,model TEXT);
            ''')

    def link(self,pid):
        with self.store.lock:
            try:row=self.store.db.execute('SELECT * FROM cloud_crew_links WHERE project_id=? AND enabled=1',(pid,)).fetchone()
            except __import__('sqlite3').OperationalError:return None
        return dict(row) if row else None
    def mapping(self,ident,session=False):
        with self.store.lock:
            try:row=self.store.db.execute('SELECT * FROM cloud_crew_tasks WHERE '+('session_id' if session else 'id')+'=?',(ident,)).fetchone()
            except __import__('sqlite3').OperationalError:return None
        return dict(row) if row else None
    def cached(self,key,default=None):
        with self.store.lock:
            row=self.store.db.execute('SELECT payload FROM cloud_crew_cache WHERE id=?',(key,)).fetchone()
        return json.loads(row['payload']) if row else default
    def cache(self,key,data):
        with self.store.lock,self.store.db:self.store.db.execute('INSERT OR REPLACE INTO cloud_crew_cache VALUES (?,?)',(key,json.dumps(data)))
    def remember(self,pid,name,task):
        if task.get('project_id')!=pid:raise ValueError('Cloud task project mismatch')
        with self.store.lock,self.store.db:self.store.db.execute('INSERT OR REPLACE INTO cloud_crew_tasks VALUES (?,?,?,?)',(task['id'],pid,name,task['session_id']))

    def enable(self,pid,agent,crew,consent=None):
        target=self.remote.target(pid)
        if not target:raise ValueError('Connect this project to a Codespace first')
        if crew.has_pending_work() or self.remote.active(None):raise ValueError('Finish active tasks before moving Crew to the cloud')
        key=self.store.key()
        if not key:raise ValueError('Configure the Ollama API key in Settings first')
        local=crew.state(pid);name=target['codespace'];warning='For longer background sessions, review the idle timeout in GitHub Codespaces settings. GitHub’s limits still apply.'
        local['bots']=[crew._bot(b['id']) for b in local['bots']]
        review_transfer(consent or {},{'brief':local['brief'],'profiles':[{k:b[k] for k in ('name','role','instructions','memory')} for b in local['bots']]})
        github=self.remote.cli(['auth','token','--hostname','github.com']).strip()
        self.remote.rpc(pid,'cloud_enable',{'ollama_key':key,'github_token':github,'codespace':name,
            'trusted':bool(target['trusted']),'auto_stop':bool(target['auto_stop']),
            'name':self.store.project(pid)['name'],'bots':local['bots'],'brief':local['brief'],'command_budget':self.store.settings().get('command_budget',8)},timeout=90)
        with self.store.lock,self.store.db:self.store.db.execute('INSERT OR REPLACE INTO cloud_crew_links VALUES (?,?,?)',(pid,name,1))
        self.state(pid,force=True)
        return {'enabled':True,'warning':warning}

    def disable(self,pid):
        self.remote.rpc(pid,'cloud_disable',{},timeout=60)
        with self.store.lock,self.store.db:self.store.db.execute('UPDATE cloud_crew_links SET enabled=0 WHERE project_id=?',(pid,))
        return {'enabled':False}

    def state(self,pid,force=False):
        link=self.link(pid)
        if not link:raise ValueError('Cloud Crew is not enabled')
        name=link['codespace'];key='state:'+pid
        if name in self.remote.paused and not force and time.monotonic()>self.survey.get(name,0):
            self.survey[name]=time.monotonic()+20
            listing=self.remote.list()
            if any(c.get('name')==name and c.get('state')=='Available' for c in listing.get('computers',[])):self.remote.paused.discard(name)
        if name in self.remote.paused and not force:
            data=self.cached(key,{'project_id':pid,'brief':'','bots':[],'tasks':[],'activity':[]})
            return {**data,'cloud':True,'disconnected':True,'working':False}
        try:data=self.remote.rpc_name(name,pid,'cloud_state',{},timeout=45)
        except ValueError:
            self.remote.paused.add(name);self.survey[name]=time.monotonic()+20
            data=self.cached(key)
            if data:return {**data,'cloud':True,'disconnected':True,'working':False}
            raise
        for task in data['tasks']:self.remember(pid,name,task)
        if any(t['status'] in PENDING for t in data['tasks']):self.pending.add(name)
        else:self.pending.discard(name)
        self.cache(key,data)
        if data.get('about_to_stop'):
            self.remote.paused.add(name);self.survey[name]=time.monotonic()+60
        return data

    def task(self,ident,after=0):
        row=self.mapping(ident)
        if not row:raise ValueError('Cloud task not found')
        key='task:'+ident
        if row['codespace'] in self.remote.paused:
            data=self.cached(key)
            if not data:raise ValueError('Reconnect the cloud computer to load this conversation')
            data['events']=[e for e in data['events'] if e['seq']>after];data['run']={**data['run'],'working':False,'label':'Reconnect to update cloud status'}
            data['pending']=None
            return self.with_local_summaries({**data,'disconnected':True})
        data=self.remote.rpc_name(row['codespace'],row['project_id'],'cloud_task',{'task_id':ident,'after':after},timeout=45)
        previous=self.cached(key,{'events':[]})
        combined={e['seq']:e for e in previous['events']}
        combined.update({e['seq']:e for e in data['events']})
        self.cache(key,{**data,'events':[combined[k] for k in sorted(combined)]})
        return self.with_local_summaries(data)

    def with_local_summaries(self,data):
        events=[]
        for event in data['events']:
            events.append(event)
            if event['type']=='privacy_decision' and event.get('choice')=='local':
                with self.store.lock:row=self.store.db.execute('SELECT * FROM cloud_local_summaries WHERE action_id=?',(event['action_id'],)).fetchone()
                if row:events.append({'type':'private_summary','seq':event['seq'],'text':row['text'],'model':row['model']})
        return {**data,'events':events}

    def operation(self,ident,operation,data=None):
        row=self.mapping(ident)
        if not row:raise ValueError('Cloud task not found')
        if operation=='cloud_start_task':
            review_transfer(data or {},{'prompt':(data or {}).get('prompt')})
            data={**(data or {}),'command_budget':self.store.settings().get('command_budget',8)}
        result=self.remote.rpc_name(row['codespace'],row['project_id'],operation,{'task_id':ident,**(data or {})},timeout=60)
        if operation=='cloud_start_task':self.pending.add(row['codespace'])
        return result

    def privacy(self,session_id,data,agent):
        row=self.mapping(session_id,session=True)
        if not row:raise ValueError('Cloud session not found')
        if data.get('choice')=='local':
            material=self.operation(row['id'],'cloud_private_material',{'action_id':data['action_id']})
            model=agent.privacy._model(data.get('model') or self.store.settings().get('privacy_local_model',''))
            summary=agent.privacy._summarize([f for f in material['findings'] if not f.get('intent')],model)
            if summary:
                with self.store.lock,self.store.db:self.store.db.execute('INSERT OR REPLACE INTO cloud_local_summaries VALUES (?,?,?)',(data['action_id'],summary,model))
            data={**data,'model':model,'local_completed':True}
        return self.operation(row['id'],'cloud_privacy',data)

class CrewRouter:
    def __init__(self,local,agent):self.local=local;self.agent=agent;self.remote=agent.remote;self.cloud=self.remote.cloud
    def __getattr__(self,name):return getattr(self.local,name)
    def state(self,pid):return self.cloud.state(pid) if self.cloud.link(pid) else self.local.state(pid)
    def save_bot(self,data):
        pid=data.get('project_id')
        if pid and self.cloud.link(pid):review_transfer(data,{k:data.get(k) for k in ('name','role','instructions','memory')})
        return self.remote.rpc(pid,'cloud_save_bot',data) if pid and self.cloud.link(pid) else self.local.save_bot(data)
    def save_brief(self,pid,brief,consent=None):
        if self.cloud.link(pid):
            review_transfer(consent or {},{'brief':brief})
            return self.remote.rpc(pid,'cloud_save_brief',{'brief':brief})
        return self.local.save_brief(pid,brief)
    def create_task(self,pid,data):
        link=self.cloud.link(pid)
        if not link:return self.local.create_task(pid,data)
        review_transfer(data,{'prompt':data.get('prompt'),'title':data.get('title')})
        task=self.remote.rpc(pid,'cloud_create_task',{**data,'command_budget':self.local.store.settings().get('command_budget',8)});self.cloud.remember(pid,link['codespace'],task);self.cloud.pending.add(link['codespace']);return task
    def task(self,ident,after=0):return self.cloud.task(ident,after) if self.cloud.mapping(ident) else self.local.task(ident,after)
    def start_task(self,ident,prompt=None,consent=None):return self.cloud.operation(ident,'cloud_start_task',{'prompt':prompt,**(consent or {})}) if self.cloud.mapping(ident) else self.local.start_task(ident,prompt)
    def stop_task(self,ident):return self.cloud.operation(ident,'cloud_stop_task') if self.cloud.mapping(ident) else self.local.stop_task(ident)
    def approve(self,ident,action_id,allowed):
        if self.cloud.mapping(ident):return self.cloud.operation(ident,'cloud_approve',{'action_id':action_id,'allowed':allowed})
        return self.agent.approve(self.local.task(ident)['task']['session_id'],action_id,allowed)
    def has_pending_work(self,project_id=None):
        if self.local.has_pending_work(project_id):return True
        with self.local.store.lock:
            links=self.local.store.db.execute('SELECT project_id,codespace FROM cloud_crew_links WHERE enabled=1'+(' AND project_id=?' if project_id else ''),(project_id,) if project_id else ()).fetchall()
        if any(r['codespace'] in self.cloud.pending for r in links):return True
        rows=[{'payload':json.dumps(self.cloud.cached('state:'+r['project_id'],{}))} for r in links]
        return any(any(t['status'] in PENDING for t in json.loads(r['payload']).get('tasks',[])) for r in rows)
    def assert_project_available(self,pid):
        if self.cloud.link(pid):
            state=self.cloud.state(pid,force=True)
            if any(t['status'] in PENDING for t in state['tasks']):raise ValueError('Cloud Crew is using this project. Finish or stop its tasks before starting Studio here.')
        return self.local.assert_project_available(pid)
