"""Google OAuth for a desktop app, plus Drive and Slides REST tools."""
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlencode,urlparse,parse_qs
import requests

SCOPES=['openid','email','https://www.googleapis.com/auth/drive.readonly','https://www.googleapis.com/auth/presentations']

class GoogleConnection:
 def __init__(self,config):self.config=config;self.lock=threading.RLock();self.auth={'pending':False,'error':''};self.server=None;self.generation=0
 def configure(self,data):
  if isinstance(data,str):data=json.loads(data)
  client=data.get('installed') if isinstance(data,dict) else None
  if not isinstance(client,dict) or not str(client.get('client_id','')).endswith('.apps.googleusercontent.com'):raise ValueError('Import the JSON for a Google OAuth Desktop app client, not a Web application client.')
  self.generation+=1;self.auth={'pending':False,'error':''}
  old=self.config.vault.get('google_client')
  if old and old.get('client_id')!=client['client_id']:self.config.vault.set('google_token',None)
  self.config.vault.set('google_client',{'client_id':client['client_id'],'client_secret':client.get('client_secret','')})
  return self.status()
 def status(self):
  try:client=self.config.vault.get('google_client');token=self.config.vault.get('google_token')
  except ValueError:return {'client_configured':False,'connected':False,'email':'','pending':False,'error':'Saved Google credentials cannot be read on this Windows account. Import and connect again.'}
  return {'client_configured':bool(client),'connected':bool(token),'email':token.get('email','') if token else '',**self.auth}
 def start(self):
  client=self.config.vault.get('google_client')
  if not client:raise ValueError('Import your Google OAuth Desktop client JSON first. Open Google setup instructions in Connections.')
  with self.lock:
   if self.auth.get('pending'):raise ValueError('Google sign-in is already waiting in your browser')
   state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64);challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
   owner=self;generation=self.generation
   class Callback(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
     parsed=urlparse(self.path);params=parse_qs(parsed.query)
     if parsed.path!='/callback' or not hmac.compare_digest(params.get('state',[''])[0],state):
      self.send_response(400);self.end_headers();self.wfile.write(b'Invalid OAuth state. Return to Beam and try again.');return
     try:
      if params.get('error'):raise ValueError('Google sign-in was cancelled or access was denied.')
      code=params.get('code',[''])[0]
      if not code:raise ValueError('Google did not return an authorization code')
      with requests.post('https://oauth2.googleapis.com/token',data={'client_id':client['client_id'],'client_secret':client['client_secret'],'code':code,'code_verifier':verifier,'redirect_uri':redirect,'grant_type':'authorization_code'},timeout=(10,30)) as response:
       if response.status_code!=200:raise ValueError('Google rejected the authorization exchange. Check your Desktop OAuth client and enabled APIs.')
       token=response.json()
      if not token.get('access_token'):raise ValueError('Google returned no access token')
      token['expires_at']=time.time()+int(token.get('expires_in',3600))-60
      with requests.get('https://openidconnect.googleapis.com/v1/userinfo',headers={'Authorization':'Bearer '+token['access_token']},timeout=(10,15)) as profile:
       if profile.status_code==200:token['email']=profile.json().get('email','')
      if generation!=owner.generation:raise ValueError('This sign-in was cancelled; connect again.')
      owner.config.vault.set('google_token',token);owner.auth={'pending':False,'error':''};message='Google is connected to Beam. You can close this window.'
     except Exception as error:
      if generation==owner.generation:owner.auth={'pending':False,'error':str(error)[:500]}
      message='Google connection did not finish. Return to Beam for details.'
     self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(('<!doctype html><title>Beam Google connection</title><h2>'+message+'</h2>').encode())
   server=HTTPServer(('127.0.0.1',0),Callback);server.timeout=.5;self.server=server
   redirect=f'http://127.0.0.1:{server.server_port}/callback'
   url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({'client_id':client['client_id'],'redirect_uri':redirect,'response_type':'code','scope':' '.join(SCOPES),'state':state,'code_challenge':challenge,'code_challenge_method':'S256','access_type':'offline','prompt':'consent'})
   self.auth={'pending':True,'error':''}
   def wait():
    end=time.monotonic()+180
    try:
     while generation==self.generation and self.auth.get('pending') and time.monotonic()<end:server.handle_request()
     if generation==self.generation and self.auth.get('pending'):self.auth={'pending':False,'error':'Google sign-in timed out. Click Connect Google to try again.'}
    finally:
     server.server_close()
     if self.server is server:self.server=None
   threading.Thread(target=wait,daemon=True).start()
   return {'authorization_url':url,'pending':True}
 def token(self):
  with self.lock:
   token=self.config.vault.get('google_token');client=self.config.vault.get('google_client')
   if not token:raise ValueError('Connect Google in Connections first')
   if time.time()>=token.get('expires_at',0):
    if not token.get('refresh_token') or not client:raise ValueError('Google access expired. Reconnect Google.')
    with requests.post('https://oauth2.googleapis.com/token',data={'client_id':client['client_id'],'client_secret':client['client_secret'],'refresh_token':token['refresh_token'],'grant_type':'refresh_token'},timeout=(10,25)) as response:
     if response.status_code!=200:raise ValueError('Google could not refresh access. Reconnect Google in Connections.')
     fresh=response.json()
    if not fresh.get('access_token'):raise ValueError('Google returned no refreshed access token. Reconnect Google.')
    token.update(fresh);token['expires_at']=time.time()+int(fresh.get('expires_in',3600))-60;self.config.vault.set('google_token',token)
   return token['access_token']
 def disconnect(self):
  self.generation+=1
  token=self.config.vault.get('google_token');self.auth={'pending':False,'error':''}
  if token:
   try:requests.post('https://oauth2.googleapis.com/revoke',data={'token':token.get('refresh_token') or token.get('access_token')},timeout=(5,10))
   except requests.RequestException:pass
  self.config.vault.set('google_token',None);return self.status()
 def _request(self,method,url,**kwargs):
  with requests.request(method,url,headers={'Authorization':'Bearer '+self.token()},timeout=(10,40),**kwargs) as response:
   if not response.ok:
    try:message=response.json().get('error',{}).get('message','')[:500]
    except (ValueError,AttributeError):message=''
    raise ValueError(f'Google API returned HTTP {response.status_code}. '+(message or 'Check account permissions, enabled APIs and OAuth scopes.'))
   return response.json()
 @staticmethod
 def presentation_id(value):
  import re
  match=re.search(r'/presentation/d/([a-zA-Z0-9_-]+)',str(value));ident=match.group(1) if match else str(value)
  if not re.fullmatch(r'[a-zA-Z0-9_-]{5,200}',ident):raise ValueError('Use a Google Slides URL or presentation ID')
  return ident
 def files(self,query='',page_token=''):
  q="trashed = false"
  if query:q+=" and name contains '"+str(query).replace('\\','\\\\').replace("'","\\'")[:200]+"'"
  return self._request('GET','https://www.googleapis.com/drive/v3/files',params={'q':q,'fields':'nextPageToken,files(id,name,mimeType,webViewLink,modifiedTime)','pageSize':30,'orderBy':'modifiedTime desc',**({'pageToken':page_token} if page_token else {})})
 def get(self,presentation):
  ident=self.presentation_id(presentation)
  return self._request('GET',f'https://slides.googleapis.com/v1/presentations/{ident}')
 def create(self,title):
  if not str(title).strip() or len(str(title))>200:raise ValueError('Presentation title must contain 1–200 characters')
  result=self._request('POST','https://slides.googleapis.com/v1/presentations',json={'title':str(title)})
  result['url']='https://docs.google.com/presentation/d/'+result['presentationId']+'/edit';return result
 def edit(self,presentation,requests_list,revision):
  ident=self.presentation_id(presentation)
  if not isinstance(requests_list,list) or not 1<=len(requests_list)<=500 or not all(isinstance(r,dict) and len(r)==1 for r in requests_list):raise ValueError('Provide 1–500 Google Slides batchUpdate request objects')
  if not revision:raise ValueError('A presentation revision is required; read the current presentation first')
  return self._request('POST',f'https://slides.googleapis.com/v1/presentations/{ident}:batchUpdate',json={'requests':requests_list,'writeControl':{'requiredRevisionId':revision}})
 def preview(self,presentation,page_id):
  ident=self.presentation_id(presentation)
  import re
  if not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}',page_id):raise ValueError('Invalid slide ID')
  return self._request('GET',f'https://slides.googleapis.com/v1/presentations/{ident}/pages/{page_id}/thumbnail',params={'thumbnailProperties.mimeType':'PNG','thumbnailProperties.thumbnailSize':'MEDIUM'})
