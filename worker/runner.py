"""Beam SSH worker. One JSON request on stdin; JSON events on stdout.

No listening network port. Commands run inside the GitHub Codespace, never
on Beam's computer. File tools are confined to a managed project directory.
"""
import ast, base64, difflib, hashlib, json, os, re, selectors, signal
import subprocess, sys, threading, time, uuid, tempfile, shlex
from pathlib import Path

BASE = Path('/workspaces/.beam-projects')
SKIP = {'.git','.venv','venv','node_modules','__pycache__','.beam-browser'}
SECRET = {'.npmrc','.pypirc','credentials.json','id_rsa','id_ed25519'}
MAX_FILE = 1000000

def emit(kind, **values):
    print(json.dumps({'event':kind, **values}), flush=True)

def project(ident):
    if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):
        raise ValueError('Invalid project identity')
    BASE.mkdir(parents=True,exist_ok=True)
    root=BASE/ident
    root.mkdir(exist_ok=True)
    if root.is_symlink() or not root.resolve().is_relative_to(BASE.resolve()):
        raise ValueError('Unsafe project root')
    if os.name=='posix' and str(BASE)=='/workspaces/.beam-projects':
        import grp
        os.chown(root,-1,grp.getgrnam('beamtask').gr_gid);os.chmod(root,0o2770)
    return root.resolve()

def path_in(root, raw):
    if not isinstance(raw,str) or not raw or '\\' in raw or '\x00' in raw:
        raise ValueError('Use a relative project path')
    parts=Path(raw).parts
    if Path(raw).is_absolute() or '..' in parts or any(p in SKIP or p in SECRET or p.startswith('.env') or p.endswith(('.key','.pem')) for p in parts):
        raise ValueError('Path is outside the workspace or contains credentials')
    target=(root/raw).resolve()
    if not target.is_relative_to(root) or target==root:raise ValueError('Unsafe path')
    resolved_parts=target.relative_to(root).parts
    if any(p in SKIP or p in SECRET or p.startswith('.env') or p.endswith(('.key','.pem')) for p in resolved_parts):
        raise ValueError('Resolved path contains credentials or dependencies')
    return target

def files(root):
    result=[]
    for folder,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in SKIP and not (Path(folder)/d).is_symlink())
        for name in sorted(names):
            relative=(Path(folder)/name).relative_to(root).as_posix()
            try:
                p=path_in(root,relative)
                if (Path(folder)/name).is_symlink():continue
                result.append({'path':relative,'bytes':p.stat().st_size,'size':p.stat().st_size})
            except (ValueError,OSError):continue
            if len(result)>=3000:return result
    return result

def text(p):
    if p.stat().st_size>MAX_FILE:raise ValueError('File exceeds the 1 MB text limit')
    raw=p.read_bytes()
    if b'\x00' in raw[:8192]:raise ValueError('Binary file: use download')
    return raw.decode('utf-8-sig')

def edit(root,args,commit):
    target=path_in(root,args['path']);old=text(target) if target.exists() else ''
    if 'old_text' in args:
        if not args['old_text'] or old.count(args['old_text'])!=1:raise ValueError('old_text must match exactly once')
        new=old.replace(args['old_text'],args['new_text'],1)
    else:new=str(args['content'])
    if len(new.encode())>MAX_FILE:raise ValueError('File exceeds the 1 MB edit limit')
    if target.suffix=='.py':ast.parse(new,filename=args['path'])
    if target.suffix=='.json':json.loads(new)
    diff=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile=args['path'],tofile=args['path']))
    digest=hashlib.sha256(old.encode()).hexdigest()
    if commit:
        if args.get('expected')!=digest:raise ValueError('File changed while waiting. Read it again before saving.')
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(target.name+'.beam-'+uuid.uuid4().hex)
        try:temporary.write_text(new,encoding='utf-8');temporary.replace(target)
        finally:temporary.unlink(missing_ok=True)
    return {'path':args['path'],'bytes':len(new.encode()),'diff':diff[:60000],'expected':digest,'changed':commit}

def command(root,args,cancelled,on_output=None):
    cmd=args.get('command','')
    if not isinstance(cmd,str) or not cmd.strip() or len(cmd)>12000:raise ValueError('Invalid command')
    timeout=max(1,min(600,int(args.get('timeout_seconds',120))))
    env=os.environ.copy()
    for key in list(env):
        if key in {'GH_TOKEN','GITHUB_TOKEN','OLLAMA_API_KEY','GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN'} or key.endswith(('_API_KEY','_API_TOKEN')):env.pop(key,None)
    import pwd
    account=pwd.getpwnam('beamtask')
    if account.pw_uid==0:raise ValueError('Restricted worker account is not configured')
    argv=['sudo','-n','-u','beamtask','--','env','PATH='+env.get('PATH','/usr/bin:/bin'),'HOME='+account.pw_dir,'bash','-c',cmd]
    proc=subprocess.Popen(argv,cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    output=[];total=0;start=time.monotonic();status='Finished'
    selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
    try:
        while True:
            if cancelled.is_set() or time.monotonic()-start>timeout:
                status='Stopped' if cancelled.is_set() else 'Timed out';break
            ready=selector.select(.15)
            for key,_ in ready:
                data=os.read(key.fileobj.fileno(),8192)
                if not data:selector.unregister(key.fileobj);continue
                chunk=data.decode('utf-8','replace')
                if total<120000:
                    chunk=chunk[:120000-total];output.append(chunk);total+=len(chunk)
                    if on_output:on_output(chunk)
                    else:emit('terminal',text=chunk)
            if proc.poll() is not None and not selector.get_map():break
    finally:
        selector.close()
        if status!='Finished':
            if proc.poll() is None:os.killpg(proc.pid,signal.SIGTERM)
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        proc.stdout.close()
    return {'status':status,'exit_code':proc.returncode,'output':''.join(output),'execution':'GitHub Codespaces'}

def dispatch(request,cancelled=None):
    cancelled=cancelled or threading.Event();name=request['operation'];args=request.get('arguments',{})
    if name.startswith('cloud_'):
        from crew_daemon import call
        return call(request)
    if name in ('status','activate'):
        import pwd
        try:ready=pwd.getpwnam('beamtask').pw_uid!=0
        except KeyError:ready=False
        result={'ready':ready,'version':'0.7.0','platform':sys.platform,'shell':'bash','sandbox':'GitHub Codespace · restricted task user','python':'python3','workspace':str(BASE)}
        if name=='activate':result['files']=files(project(request['project_id']))
        return result
    root=project(request['project_id'])
    if name=='upload':
        rows=args.get('files',[])
        if not isinstance(rows,list) or len(rows)>500:raise ValueError('Upload is limited to 500 files')
        decoded=[];total=0
        for row in rows:
            target=path_in(root,row['path']);raw=base64.b64decode(row['data'],validate=True);total+=len(raw)
            if total>20000000:raise ValueError('Upload exceeds 20 MB')
            decoded.append((target,raw))
        with tempfile.TemporaryDirectory(dir=root,prefix='.beam-transfer-') as temporary:
            for i,(target,raw) in enumerate(decoded):(Path(temporary)/str(i)).write_bytes(raw)
            for i,(target,raw) in enumerate(decoded):
                if cancelled.is_set():raise ValueError('Upload stopped')
                target.parent.mkdir(parents=True,exist_ok=True);(Path(temporary)/str(i)).replace(target)
        return {'files':len(decoded),'bytes':total}
    if name=='fingerprint':
        digest=hashlib.sha256()
        for row in files(root):
            q=path_in(root,row['path']);digest.update((row['path']+str(q.stat().st_mtime_ns)+str(q.stat().st_size)).encode())
        return {'fingerprint':digest.hexdigest()}
    if name=='list_files':return {'files':files(root),'limit':3000}
    if name in ('file','read_file'):
        content=text(path_in(root,args['path']))
        if name=='file':return {'path':args['path'],'content':content}
        lines=content.splitlines();start=max(1,int(args.get('start_line',1)));end=min(len(lines),int(args.get('end_line',start+499)),start+999)
        return {'path':args['path'],'total_lines':len(lines),'content':'\n'.join(f'{i+1}: {lines[i]}' for i in range(start-1,end))[:65000]}
    if name=='download':
        p=path_in(root,args['path'])
        if p.stat().st_size>40000000:raise ValueError('Download exceeds 40 MB; split the output')
        return {'path':args['path'],'data':base64.b64encode(p.read_bytes()).decode()}
    if name=='search_files':
        query=str(args['query']).lower()
        if not query:raise ValueError('Enter search text')
        matches=[]
        for row in files(root):
            try:
                for i,line in enumerate(text(path_in(root,row['path'])).splitlines(),1):
                    if query in line.lower():matches.append({'path':row['path'],'line':i,'text':line[:400]})
                    if len(matches)>=80:return {'matches':matches,'truncated':True}
            except (ValueError,OSError,UnicodeError):continue
        return {'matches':matches}
    if name in ('prepare_edit','commit_edit'):return edit(root,args,name=='commit_edit')
    if name=='run_command':return command(root,args,cancelled)
    if name in ('git_status','git_diff'):
        return command(root,{'command':'git --no-optional-locks -c core.fsmonitor=false -c safe.directory='+shlex.quote(str(root))+' '+('diff --no-ext-diff --no-textconv HEAD --' if name=='git_diff' else 'status --short --branch'),'timeout_seconds':20},cancelled)
    if name in ('runtime_info','inspect_project'):
        return {'platform':'linux','shell':'bash','python':'python3','files':files(root)[:200],
                'guidance':'Commands run inside the GitHub Codespace as a separate task user in a persistent managed folder. Use python3, bash and POSIX commands. Use git -c safe.directory=<project root> when Git requests an ownership exception for this shared folder. Use finite checks and a project .venv for dependencies.'}
    if name in ('generate_model','generate_artifact'):
        from enhanced_artifacts import produce_artifact
        if name=='generate_model':
            from cad_templates import model_spec
            item={'path':args['path'],'kind':'glb' if args['path'].endswith('.glb') else 'stl','content':model_spec(args['preset'],args.get('parameters',{}),require_dimensions=True)}
        else:item=args
        path_in(root,item['path'])
        return produce_artifact(root,item,cancelled=cancelled)
    if name=='inspect_artifact':
        from artifact_quality import inspect_file
        return inspect_file(path_in(root,args['path']))
    raise ValueError('Unsupported remote operation')

def main():
    os.umask(0o002)
    raw=sys.stdin.buffer.readline(32000001)
    if len(raw)>32000000:raise ValueError('Request is too large')
    request=json.loads(raw);cancelled=threading.Event()
    def watch():
        sys.stdin.buffer.read(1);cancelled.set()
    threading.Thread(target=watch,daemon=True).start()
    lease=None
    if not request['operation'].startswith('cloud_') and request['operation']!='status':
        directory=Path('/workspaces/.beam-crew/leases')
        if directory.parent.exists():
            directory.mkdir(exist_ok=True);lease=directory/(str(os.getpid())+'.json');lease.write_text(json.dumps({'until':time.time()+660}))
    try:result=dispatch(request,cancelled)
    finally:
        if lease:lease.unlink(missing_ok=True)
    emit('result',result=result)

if __name__=='__main__':
    try:main()
    except Exception as error:emit('error',message=str(error)[:1500]);sys.exit(1)
