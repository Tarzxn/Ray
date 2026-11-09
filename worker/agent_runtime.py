"""Windows-aware command execution and managed development servers."""
import base64
import ctypes
from contextlib import contextmanager
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

_LAUNCH_LOCK=threading.RLock()


def shell_executable():
    return (shutil.which('pwsh.exe') or str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe')) if os.name=='nt' else '/bin/sh'


def runtime_info():
    python=shutil.which('python.exe' if os.name=='nt' else 'python3')
    return {'platform':sys.platform,'shell':shell_executable(),'python':python,'git':shutil.which('git'),
            'guidance':'Use Windows PowerShell commands: python (not python3), Get-Command, Get-ChildItem, Select-String, $null (not /dev/null). Use start_server for persistent servers, not Start-Process or trailing &.' if os.name=='nt' else 'POSIX shell',
            'test_guidance':'Choose one relevant finite check. Use syntax checks when useful; do not repeat passed checks without a code change or unresolved issue. Prefer Flask test_client and mocked services. Tests must exit; do not run a web server as a foreground test.'}


def validate_command(command):
    if not isinstance(command,str) or not command.strip() or len(command)>12000:raise ValueError('Command is empty or too long')
    if re.search(r'(?is)(Get-Process\b[^|;\n]*\|[^;\n]*Stop-Process|Stop-Process\s+-(?:Name|ProcessName)\b|taskkill\b[^\n]* /im |\b(?:killall|pkill)\s)',command):
        raise ValueError('Do not terminate unrelated processes by name. Use stop_server for a Beam-managed server, or a specific verified PID.')
    if os.name=='nt':
        if re.search(r'/dev/null|\bwhich\s|\bls\s+-[a-z]*[la]|\bpython3\b',command):
            raise ValueError('This tool uses Windows PowerShell. Use python, Get-Command, Get-ChildItem and $null, not Unix shell commands.')
        if re.search(r'\bStart-Process\b|&\s*(?:\n|$)',command,re.I):
            raise ValueError('Use start_server for persistent background processes. run_command is for finite tests and commands.')


def clean_environment():
    env=os.environ.copy()
    if getattr(sys,'frozen',False):
        bundle=str(Path(sys._MEIPASS).resolve()).lower()
        env['PATH']=os.pathsep.join(p for p in env.get('PATH','').split(os.pathsep) if not str(Path(p).resolve()).lower().startswith(bundle))
        for name in ('PYTHONHOME','PYTHONPATH','_MEIPASS2','_PYI_APPLICATION_HOME_DIR','_PYI_ARCHIVE_FILE','_PYI_PARENT_PROCESS_LEVEL'):
            env.pop(name,None)
        env['PYINSTALLER_RESET_ENVIRONMENT']='1'
    env['PYTHONUNBUFFERED']='1';env['PYTHONIOENCODING']='utf-8'
    return env


@contextmanager
def external_launch():
    with _LAUNCH_LOCK:
        reset=os.name=='nt' and getattr(sys,'frozen',False)
        if reset:ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:yield
        finally:
            if reset:ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))


def argv_for(command):
    if os.name!='nt':return [shell_executable(),'-c',command]
    prefix="$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
    encoded=base64.b64encode((prefix+command).encode('utf-16-le')).decode('ascii')
    return [shell_executable(),'-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',encoded]



class WindowsJob:
    """Own only this subprocess tree; no process-name matching or shell taskkill."""
    def __init__(self,process):
        from ctypes import wintypes
        size_t=ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),('min_ws',size_t),('max_ws',size_t),('active',wintypes.DWORD),('affinity',size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',size_t),('job_memory',size_t),('peak_process',size_t),('peak_job',size_t)]
        kernel=ctypes.windll.kernel32
        kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.handle=kernel.CreateJobObjectW(None,None)
        if not self.handle:raise OSError('Cannot create subprocess job')
        info=Extended();info.basic.flags=0x2000
        if not kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            kernel.CloseHandle(self.handle);self.handle=None;raise OSError('Cannot attach subprocess job')
    def close(self):
        if self.handle:
            from ctypes import wintypes
            class Accounting(ctypes.Structure):
                _fields_=[('user',ctypes.c_longlong),('kernel',ctypes.c_longlong),('period_user',ctypes.c_longlong),('period_kernel',ctypes.c_longlong),('faults',wintypes.DWORD),('total',wintypes.DWORD),('active',wintypes.DWORD),('terminated',wintypes.DWORD)]
            kernel=ctypes.windll.kernel32
            kernel.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]
            kernel.TerminateJobObject(self.handle,1)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                info=Accounting()
                if not kernel.QueryInformationJobObject(self.handle,1,ctypes.byref(info),ctypes.sizeof(info),None) or info.active==0:break
                time.sleep(.02)
            kernel.CloseHandle(self.handle);self.handle=None


def launch(command,root,stdout=subprocess.PIPE):
    validate_command(command)
    with external_launch():
        process=subprocess.Popen(argv_for(command),cwd=root,stdout=stdout,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                env=clean_environment(),**({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}))
        if os.name=='nt':
            try:process._beam_job=WindowsJob(process)
            except OSError:process._beam_job=None
        return process


def stop_process(process):
    if not process:return
    job=getattr(process,'_beam_job',None)
    if job:job.close();process._beam_job=None
    if process.poll() is None:
        if os.name=='nt':process.kill()
        else:
            import signal
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass


def run_command(command,root,timeout=120,cancelled=None,on_output=None,on_tick=None):
    cancelled=cancelled or threading.Event()
    timeout=max(1,min(int(timeout),600))
    process=launch(command,root)
    q=queue.Queue();reader_done=threading.Event()
    def reader():
        try:
            while raw:=os.read(process.stdout.fileno(),2048):q.put(raw.decode('utf-8',errors='replace'))
        except (OSError,ValueError):pass
        finally:reader_done.set();q.put(None)
    threading.Thread(target=reader,daemon=True).start()
    start=time.monotonic();last_tick=start;ended=None;killed=None;reason=None;chunks=[];length=0;stream_closed=False
    try:
        while True:
            now=time.monotonic()
            if on_tick and now-last_tick>=1:on_tick(round(now-start));last_tick=now
            if not reason and (cancelled.is_set() or now-start>=timeout):
                reason='Stopped' if cancelled.is_set() else 'Timed out';stop_process(process);killed=now
            if process.poll() is not None:
                ended=ended or now
                if stream_closed:break
            if (killed and now-killed>3) or (ended and now-ended>1.5 and q.empty()):break
            try:chunk=q.get(timeout=.1)
            except queue.Empty:continue
            if chunk is None:
                stream_closed=True
                if process.poll() is not None:break
                continue
            if length<120000:
                chunk=chunk[:120000-length];chunks.append(chunk);length+=len(chunk)
                if on_output:on_output(chunk)
        return {'exit_code':process.poll(),'output':''.join(chunks),'status':reason or 'Finished','duration_seconds':round(time.monotonic()-start,1),'truncated':length>=120000}
    finally:
        stop_process(process)
        # Do not close a pipe from another thread while it is blocked in os.read.
        if not process.stdout.closed and reader_done.is_set():process.stdout.close()


class ManagedServers:
    def __init__(self,data_dir):
        self.directory=Path(data_dir)/'server-logs';self.directory.mkdir(exist_ok=True)
        self.entries={};self.lock=threading.RLock()
    def start(self,command,root,preview_port=None):
        if preview_port is not None and (type(preview_port) is not int or not 1024<=preview_port<=65535):
            raise ValueError('Use an unprivileged TCP preview port between 1024 and 65535')
        ident=uuid.uuid4().hex[:12];log=self.directory/(ident+'.log')
        with log.open('wb') as output:process=launch(command,root,output)
        entry={'id':ident,'command':command,'root':str(root),'log':log,'process':process,'preview_port':preview_port}
        with self.lock:self.entries[ident]=entry
        time.sleep(.15)
        return self.status(ident)
    def status(self,ident):
        with self.lock:entry=self.entries.get(ident)
        if not entry:raise ValueError('Unknown Beam-managed server ID')
        process=entry['process'];raw=entry['log'].read_bytes()[-20000:]
        result={'id':ident,'command':entry['command'],'running':process.poll() is None,'exit_code':process.poll(),'output':raw.decode('utf-8',errors='replace')}
        if result['running'] and entry['preview_port']:
            result['preview_url']=f"http://127.0.0.1:{entry['preview_port']}"
        return result
    def preview_allowed(self,port):
        with self.lock:
            return any(entry['preview_port']==port and entry['process'].poll() is None for entry in self.entries.values())
    def stop(self,ident):
        with self.lock:entry=self.entries.get(ident)
        if not entry:raise ValueError('Unknown Beam-managed server ID')
        stop_process(entry['process']);entry['process'].wait(timeout=10)
        return self.status(ident)
    def close(self):
        for ident in list(self.entries):
            try:self.stop(ident)
            except (ValueError,OSError,subprocess.SubprocessError):pass
