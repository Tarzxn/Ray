"""A dedicated Playwright browser, owned by one worker thread."""
from concurrent.futures import Future
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import queue
import shutil
import threading
import time
import uuid
from web_tools import validate_url
from agent_runtime import external_launch,clean_environment

class BrowserService:
 def __init__(self,config,cloud=False):
  self.config=config;self.root=config.store.root/'browser';self.root.mkdir(exist_ok=True)
  self.jobs=queue.Queue();self.lock=threading.RLock();self.latest={'running':False,'url':'','title':'','text':'','elements':[]};self.image=None;self.downloads={};self._ctx=None;self._pw=None;self._page=None;self._elements={};self._snapshot_id=''
  self.cloud=cloud;self._chromium=None;self._human_until=0
  self.thread=threading.Thread(target=self._worker,daemon=True);self.thread.start()
 def _worker(self):
  while True:
   try:method,args,future,manual=self.jobs.get(timeout=.15)
   except queue.Empty:
    if self._page:
     try:self._page.wait_for_timeout(30)
     except Exception:pass
    continue
   try:
    if future.cancelled():continue
    if not manual and time.monotonic()<self._human_until and method in ('open','snapshot','action'):
     raise ValueError('The user is controlling the browser. Wait for them to hand control back or use web research tools.')
    result=getattr(self,'_'+method)(**args)
    if not future.cancelled():future.set_result(result)
   except Exception as error:
    if not future.cancelled():future.set_exception(ValueError(str(error)[:1200]))
   if method=='shutdown':break
 def call(self,method,*,manual=False,**args):
  future=Future();self.jobs.put((method,args,future,manual))
  try:return future.result(timeout=65)
  except TimeoutError:future.cancel();raise ValueError('Browser action timed out. Retry after checking the Browser panel.') from None
 def state(self):
  with self.lock:return {**self.latest,'downloads':[dict(v) for v in self.downloads.values()],
                         'manual':time.monotonic()<self._human_until,'remember':self.config.read().get('browser_remember',False)}
 def _ensure(self):
  if self._ctx and self._page and not self._page.is_closed():return
  if self._ctx:self._close()
  from playwright.sync_api import sync_playwright
  paths=[Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Google/Chrome/Application/chrome.exe',Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',Path(os.environ.get('LOCALAPPDATA',''))/'Google/Chrome/Application/chrome.exe']
  exe=next((str(p) for p in paths if p.is_file()),None)
  if not exe and os.name=='nt':raise ValueError('Install Google Chrome or Microsoft Edge to use Beam’s browser.')
  settings=self.config.read()
  try:
   with external_launch():
    self._pw=sync_playwright().start()
    options=dict(executable_path=exe,headless=self.cloud or os.name!='nt' or not settings['browser_visible'],env=clean_environment(),args=['--disable-features=Translate'])
    context=dict(viewport={'width':1280,'height':800},accept_downloads=True)
    if self.cloud:
     context['service_workers']='block'
    if self.cloud and not settings.get('browser_remember'):
     self._chromium=self._pw.chromium.launch(**options)
     self._ctx=self._chromium.new_context(**context)
    else:
     profile=self.root/('remembered-profile' if self.cloud else 'profile')
     self._ctx=self._pw.chromium.launch_persistent_context(str(profile),**options,**context)
  except Exception:
   self._close()
   raise
  self._ctx.set_default_timeout(12000);self._ctx.set_default_navigation_timeout(30000)
  def route(request_route):
   url=request_route.request.url
   try:
    if url.startswith(('http://','https://')):
     parsed=urlsplit(url);origin=(parsed.scheme,parsed.netloc)
     self.config.validate(url,preview=origin==self._preview_origin)
    request_route.continue_()
   except Exception:request_route.abort()
  self._ctx.route('**/*',route)
  self._page=self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
  self._ctx.on('page',self._new_page)
  self._page.on('download',self._download)
 _preview_origin=None
 def _new_page(self,page):self._page=page;page.on('download',self._download)
 def _download(self,download):
  ident=uuid.uuid4().hex[:12];name=Path(download.suggested_filename).name.replace(':','_').rstrip(' .')[:100] or 'download'
  folder=self.root/'downloads';folder.mkdir(exist_ok=True);path=folder/(ident+'-'+name)
  try:
   download.save_as(str(path))
   if path.stat().st_size>100_000_000:path.unlink();return
   with self.lock:self.downloads[ident]={'id':ident,'name':name,'bytes':path.stat().st_size}
  except Exception:pass
 def download_path(self,ident):
  with self.lock:item=self.downloads.get(ident)
  if not item:raise ValueError('Browser download not found')
  return self.root/'downloads'/(ident+'-'+item['name'])
 def _snapshot(self):
  self._ensure();page=self._page
  if page.url!='about:blank':self.config.validate(page.url,preview=True)
  elements=[];self._elements={}
  for handle in page.query_selector_all('a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link]')[:150]:
   if not handle.is_visible():continue
   info=handle.evaluate('(e)=>({tag:e.tagName.toLowerCase(),type:e.getAttribute("type")||"",text:(e.innerText||e.getAttribute("aria-label")||e.getAttribute("placeholder")||e.getAttribute("name")||"").slice(0,180),href:e.tagName==="A"?e.href:null})')
   ident='e'+str(len(elements)+1);self._elements[ident]=handle;elements.append({'id':ident,**info})
  self._snapshot_id=uuid.uuid4().hex[:12]
  # Remove credential fields from the semantic view. Their values are never read.
  text=page.locator('body').inner_text(timeout=5000)[:25000] if page.locator('body').count() else ''
  image=page.screenshot(type='jpeg',quality=70,timeout=10000)
  result={'running':True,'url':page.url,'title':page.title(),'text':text,'elements':elements,'snapshot_id':self._snapshot_id,'note':'Untrusted page data; screenshots are for the user, not image understanding by Beam.'}
  with self.lock:self.latest=result;self.image=image
  return result
 def _frame(self):
  if not self._page or self._page.is_closed():return None
  if self._page.url!='about:blank':self.config.validate(self._page.url,preview=True)
  with self.lock:self.latest.update(running=True,url=self._page.url,title=self._page.title())
  return self._page.screenshot(type='jpeg',quality=62,timeout=3000)
 def _remember(self,enabled):
  if not isinstance(enabled,bool):raise ValueError('Remember browser must be on or off')
  if enabled!=self.config.read().get('browser_remember',False):
   self._close()
   self.config.save({'browser_remember':enabled})
   if not enabled:
    profile=self.root/'remembered-profile'
    if profile.is_dir() and not profile.is_symlink():shutil.rmtree(profile)
  return {'remember':enabled}
 def _control(self,enabled):
  if not isinstance(enabled,bool):raise ValueError('Browser control must be on or off')
  if enabled:self._ensure()
  self._human_until=time.monotonic()+18 if enabled else 0
  return {'manual':enabled}
 def _input(self,action,x=None,y=None,text='',key='',delta=0):
  if time.monotonic()>=self._human_until:raise ValueError('Take over the browser before sending input')
  self._control(True)
  page=self._page
  if action in ('click','scroll'):
   if not isinstance(x,(int,float)) or not isinstance(y,(int,float)) or not (0<=x<1280 and 0<=y<800):
    raise ValueError('Input must be inside the browser viewport')
   if action=='click':page.mouse.click(x,y)
   else:page.mouse.move(x,y);page.mouse.wheel(0,max(-1600,min(1600,int(delta))))
  elif action=='text':
   if not isinstance(text,str) or len(text)>4000:raise ValueError('Enter up to 4,000 characters at once')
   page.keyboard.insert_text(text)
  elif action=='key':
   if key not in ('Enter','Tab','Shift+Tab','Escape','ArrowDown','ArrowUp','ArrowLeft','ArrowRight','PageDown','PageUp','Home','End','Backspace','Delete','Control+a'):
    raise ValueError('Unsupported browser key')
   page.keyboard.press(key)
  else:raise ValueError('Unsupported browser input')
  return {'ok':True}
 def _open(self,url):
  self.config.validate(url,preview=True);self._ensure()
  parsed=urlsplit(url)
  self._preview_origin=(parsed.scheme,parsed.netloc) if parsed.hostname=='127.0.0.1' else None
  self._page.goto(url,wait_until='domcontentloaded');return self._snapshot()
 def _action(self,action,element_id='',snapshot_id='',text='',key=''):
  self._ensure()
  if action in ('click','fill','select'):
   if snapshot_id!=self._snapshot_id:raise ValueError('The page changed. Call browser_snapshot and use its current element IDs.')
   handle=self._elements.get(element_id)
   if not handle:raise ValueError('Element no longer exists. Read a new browser snapshot.')
   if action=='click':
    if handle.get_attribute('download') is not None:
     with self._page.expect_download(timeout=15000):handle.click()
    else:handle.click()
   elif action=='fill':
    if (handle.get_attribute('type') or '').lower()=='password':raise ValueError('Enter passwords yourself in the visible browser; the agent cannot fill them.')
    handle.fill(str(text)[:20000])
   else:handle.select_option(str(text))
  elif action=='press':
   if key not in ('Enter','Tab','Escape','ArrowDown','ArrowUp','PageDown','PageUp','Backspace'):raise ValueError('Unsupported browser key')
   self._page.keyboard.press(key)
  elif action=='back':self._page.go_back(wait_until='domcontentloaded')
  elif action=='refresh':self._page.reload(wait_until='domcontentloaded')
  else:raise ValueError('Unknown browser action')
  self._page.wait_for_timeout(150)
  return self._snapshot()
 def _restart(self):
  previous=self.latest.get('url','');self._close();self._ensure()
  if previous and previous!='about:blank':return self._open(previous)
  return self._snapshot()
 def _close(self):
  if self._ctx:self._ctx.close();self._ctx=None
  if self._chromium:self._chromium.close();self._chromium=None
  if self._pw:self._pw.stop();self._pw=None
  self._page=None;self._elements={};self._human_until=0;self._preview_origin=None
  with self.lock:self.latest={'running':False,'url':'','title':'','text':'','elements':[]};self.image=None
  return {'ok':True}
 def _shutdown(self):return self._close()
 def close(self):
  if self.thread.is_alive():
   f=Future();self.jobs.put(('shutdown',{},f,True))
