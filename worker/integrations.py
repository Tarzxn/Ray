"""Connection services shared by the agent and desktop controls."""
from pathlib import Path
import shutil
from web_tools import ConnectionConfig,WebTools
from google_tools import GoogleConnection

class Integrations:
 def __init__(self,store):self.config=ConnectionConfig(store);self.web=WebTools(self.config);self.google=GoogleConnection(self.config);self._browser=None;self._mcp=None
 @property
 def browser(self):
  if self._browser is None:
   from browser_tools import BrowserService
   self._browser=BrowserService(self.config,cloud=getattr(self.config.store,'cloud',False))
  return self._browser
 @property
 def mcp(self):
  if self._mcp is None:
   from mcp_tools import McpHub
   self._mcp=McpHub(self.config)
  return self._mcp
 def state(self):
  settings=self.config.public();settings['google']=self.google.status()
  settings['mcp']=self._mcp.public() if self._mcp else [{**c,'connected':False,'tool_count':0} for c in settings['mcp']]
  settings['browser']=self._browser.state() if self._browser else {'running':False,'url':'','title':'','text':'','elements':[],'downloads':[]}
  return settings
 def summary(self):
  return {'search_provider':self.config.read()['search_provider'],'google_connected':self.google.status()['connected'],'mcp_servers':[{'id':c['id'],'name':c['name']} for c in self.config.read()['mcp']]}
 def save_settings(self,data):
  updates={}
  if data.get('search_provider') in ('free','tavily'):updates['search_provider']=data['search_provider']
  for key in ('browser_visible','browser_allow_local'):
   if key in data:updates[key]=bool(data[key])
  if data.get('search_key'):self.config.vault.set('search_key',str(data['search_key']).strip())
  if data.get('clear_search_key'):self.config.vault.set('search_key',None)
  self.config.save(updates);return self.state()
 def close(self):
  if self._browser:self._browser.close()
  if self._mcp:self._mcp.close()
  self.google.auth['pending']=False
