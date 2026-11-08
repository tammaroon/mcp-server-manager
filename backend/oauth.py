"""MCP OAuth discovery and native browser sign-in with PKCE and a loopback callback."""
from __future__ import annotations
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from catalog import ManagerError
from transport import deadline_opener
from server_config import raw_config, resolve_config


def origin(url):
    p=urllib.parse.urlsplit(url)
    return p.scheme.lower()+'://'+p.netloc.lower()


def loopback(host):
    if host == 'localhost': return True
    try: return ipaddress.ip_address(host or '').is_loopback
    except ValueError: return False


def safe_url(url, resource):
    if not isinstance(url,str) or len(url)>8192: raise ManagerError('The OAuth provider returned an invalid endpoint.')
    try: p=urllib.parse.urlsplit(url)
    except ValueError: raise ManagerError('The OAuth provider returned an invalid endpoint.') from None
    local=loopback(urllib.parse.urlsplit(resource).hostname)
    if not p.hostname or p.username or p.password or p.fragment or p.scheme not in ('https','http') or p.scheme=='http' and not (local and loopback(p.hostname)) or loopback(p.hostname) and not local:
        raise ManagerError('OAuth endpoints require HTTPS; local HTTP is allowed only for a local MCP server.')
    return url


def json_request(url, resource, payload=None, form=False):
    safe_url(url,resource)
    data=None if payload is None else (urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode())
    headers={'Accept':'application/json'}
    if data is not None: headers['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
    request=urllib.request.Request(url,data=data,headers=headers)
    try:
        with deadline_opener(time.monotonic()+10).open(request,timeout=10) as response:
            body=response.read(1024*1024+1)
            if len(body)>1024*1024: raise ManagerError('OAuth response exceeds the size limit.')
            result=json.loads(body)
            if not isinstance(result,dict): raise ValueError()
            return result
    except urllib.error.HTTPError as e:
        code=e.code;e.close()
        raise ManagerError('OAuth endpoint returned HTTP '+str(code)+'.') from None
    except (ValueError,UnicodeError): raise ManagerError('OAuth endpoint returned invalid JSON.') from None
    except (OSError,urllib.error.URLError): raise ManagerError('OAuth connection failed or timed out.') from None


def discovery(resource):
    safe_url(resource,resource)
    p=urllib.parse.urlsplit(resource);base=origin(resource)
    challenge=''
    try:
        with deadline_opener(time.monotonic()+10).open(urllib.request.Request(resource,headers={'Accept':'application/json'}),timeout=10) as response:
            response.read(1)
    except urllib.error.HTTPError as e:
        challenge=e.headers.get('WWW-Authenticate','');e.close()
    except (OSError,urllib.error.URLError): raise ManagerError('Could not reach the MCP server to discover sign-in.') from None
    metadata_match=re.search(r'\bresource_metadata\s*=\s*(?:"([^"]+)"|([^,\s]+))',challenge,re.I)
    candidates=[metadata_match[1] or metadata_match[2]] if metadata_match else [base+'/.well-known/oauth-protected-resource'+p.path.rstrip('/'),base+'/.well-known/oauth-protected-resource']
    protected=None
    for url in dict.fromkeys(candidates):
        try: protected=json_request(url,resource);break
        except ManagerError: pass
    if not protected: raise ManagerError('This MCP server does not publish supported OAuth sign-in metadata.')
    canonical=protected.get('resource')
    if not isinstance(canonical,str) or canonical.rstrip('/')!=resource.rstrip('/'): raise ManagerError('OAuth resource metadata does not match this MCP server.')
    issuers=protected.get('authorization_servers')
    if not isinstance(issuers,list) or not issuers or not isinstance(issuers[0],str): raise ManagerError('The MCP server does not identify an OAuth provider.')
    issuer=safe_url(issuers[0],resource).rstrip('/');i=urllib.parse.urlsplit(issuer);authbase=origin(issuer)
    candidates=[authbase+'/.well-known/oauth-authorization-server'+i.path,authbase+'/.well-known/openid-configuration'+i.path,issuer+'/.well-known/openid-configuration',authbase+'/.well-known/oauth-authorization-server',authbase+'/.well-known/openid-configuration']
    metadata=None
    for url in dict.fromkeys(candidates):
        try:
            value=json_request(url,resource)
            if value.get('issuer','').rstrip('/')!=issuer: continue
            metadata=value;break
        except ManagerError: pass
    if not metadata: raise ManagerError('The OAuth provider did not publish valid authorization metadata.')
    for field in ('authorization_endpoint','token_endpoint','registration_endpoint'):
        if not metadata.get(field): raise ManagerError('This provider requires a pre-registered OAuth client; automatic browser sign-in is unavailable.')
        safe_url(metadata[field],resource)
    if 'S256' not in metadata.get('code_challenge_methods_supported',[]): raise ManagerError('The OAuth provider does not advertise required PKCE S256 support.')
    if 'none' not in metadata.get('token_endpoint_auth_methods_supported',['client_secret_basic']): raise ManagerError('This OAuth provider requires a confidential client registration.')
    scope_match=re.search(r'\bscope\s*=\s*"([^"]*)"',challenge,re.I)
    scopes=protected.get('scopes_supported',[])
    scope=scope_match[1] if scope_match else ' '.join(s for s in scopes if isinstance(s,str)) if isinstance(scopes,list) else ''
    if len(scope)>8192 or any(c in scope for c in '\r\n'): raise ManagerError('OAuth scope metadata is invalid.')
    return metadata,canonical,scope


class ReplacedLogin(Exception):
    pass


class OAuthManager:
    def __init__(self,store,read_secret,write_secret,on_changed):
        self.store,self.read_secret,self.write_secret,self.on_changed=store,read_secret,write_secret,on_changed
        self.pending={};self.lock=threading.RLock()
        for server in store.data['servers']:
            if server.get('oauth',{}).get('status') == 'pending': self._failed(server,'The previous sign-in was interrupted. Open the login screen to try again.')

    def _resource(self,server):
        # Discover/restart login without reading a missing or expired Authorization secret.
        endpoint=raw_config(self.store,server).get('url')
        projection={**server,'definition':{'url':endpoint},'credential_set':False}
        return resolve_config(self.store,projection).get('url')

    def start(self,server):
        sid=server['id']
        if server['transport']!='HTTP': raise ManagerError('Browser sign-in is available for HTTP MCP servers.')
        resource=self._resource(server)
        metadata,resource,scope=discovery(resource)
        with self.lock:
            if old:=self.pending.pop(sid,None): self._close_flow(old)
        state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
        manager=self
        class Callback(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                parsed=urllib.parse.urlsplit(self.path)
                expected='127.0.0.1:'+str(self.server.server_port)
                if parsed.path!='/oauth/callback' or self.headers.get('Host')!=expected or len(self.path)>16384:
                    self.send_error(400);return
                query=urllib.parse.parse_qs(parsed.query)
                values=query.get('state',[])
                if len(values)!=1 or not values[0].isascii() or len(values[0])>256 or not secrets.compare_digest(values[0],state): self.send_error(400,'Invalid sign-in state');return
                with manager.lock:
                    current=manager.pending.get(sid)
                    if current is not flow: self.send_error(410);return
                    if current.get('completing'): self.send_error(409);return
                    current['completing']=True
                success=False;status_code=200
                try:
                    if query.get('error'): raise ManagerError('Sign-in was canceled or denied. Open the login screen to try again.')
                    codes=query.get('code',[])
                    if len(codes)!=1 or not codes[0] or len(codes[0])>8192: raise ManagerError('The login callback did not contain an authorization code.')
                    token=json_request(metadata['token_endpoint'],resource,{'grant_type':'authorization_code','code':codes[0],'redirect_uri':redirect,'client_id':client_id,'code_verifier':verifier,'resource':resource},form=True)
                    with manager.lock:
                        if manager.pending.get(sid) is not flow: raise ReplacedLogin()
                        manager._save_token(server,token,{'client_id':client_id,'resource':resource,'token_endpoint':metadata['token_endpoint'],'scope':scope})
                        manager.pending.pop(sid,None)
                    success=True
                    try: manager.on_changed(sid)
                    except Exception: pass  # The saved login remains valid if a UI/session refresh fails.
                except ReplacedLogin:
                    status_code=410
                except Exception as e:
                    with manager.lock:
                        if manager.pending.get(sid) is flow:
                            manager._failed(server,str(e) if isinstance(e,ManagerError) else 'Sign-in could not complete. Please try again.')
                            manager.pending.pop(sid,None)
                finally:
                    body=("Sign-in complete. You can close this tab and return to MCP Server Manager." if success else "Sign-in could not complete. Return to MCP Server Manager for details.").encode()
                    try:
                        self.send_response(status_code);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'none'");self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                    except OSError: pass
                    finally: manager._close_flow(flow)
        http=ThreadingHTTPServer(('127.0.0.1',0),Callback);http.daemon_threads=True
        redirect=f'http://127.0.0.1:{http.server_port}/oauth/callback'
        flow={'server':http,'timer':None,'state':state,'url':None}
        try:
            registration={'client_name':'MCP Server Manager','redirect_uris':[redirect],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','application_type':'native'}
            if scope: registration['scope']=scope
            client=json_request(metadata['registration_endpoint'],resource,registration)
            client_id=client.get('client_id')
            if not isinstance(client_id,str) or not client_id or client.get('token_endpoint_auth_method','none')!='none': raise ManagerError('The OAuth provider did not register a public native client.')
            challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            params={'response_type':'code','client_id':client_id,'redirect_uri':redirect,'state':state,'code_challenge':challenge,'code_challenge_method':'S256','resource':resource}
            if scope: params['scope']=scope
            separator='&' if urllib.parse.urlsplit(metadata['authorization_endpoint']).query else '?'
            url=metadata['authorization_endpoint']+separator+urllib.parse.urlencode(params)
            flow['url']=url
            with self.lock: self.pending[sid]=flow
            with self.store.lock:
                server['oauth']={**server.get('oauth',{}),'status':'pending','error':None}
                self.store.save()
            threading.Thread(target=http.serve_forever,kwargs={'poll_interval':.05},daemon=True).start()
            def expire():
                with self.lock:
                    if self.pending.get(sid) is not flow or flow.get('completing'): return
                    self.pending.pop(sid,None)
                self._failed(server,'Sign-in timed out. Open the login screen to try again.');self._close_flow(flow)
            timer=threading.Timer(300,expire);timer.daemon=True;flow['timer']=timer;timer.start()
            return {'authorization_url':url}
        except Exception:
            http.server_close()
            with self.lock: self.pending.pop(sid,None)
            raise

    def _save_token(self,server,token,metadata):
        access=token.get('access_token')
        if not isinstance(access,str) or not access or token.get('token_type','').lower()!='bearer': raise ManagerError('The OAuth provider did not return a Bearer access token.')
        duration=token.get('expires_in',3600)
        if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0: raise ManagerError('The OAuth provider returned invalid token expiry metadata.')
        bundle={'access_token':access,'refresh_token':token.get('refresh_token'),'expires_at':time.time()+min(duration,365*86400)}
        with self.store.lock:
            # Discovery replaces dictionaries: always commit against the current stable ID.
            server=self.store.server(server['id'])
            configured=self._resource(server)
            if not isinstance(configured,str) or configured.rstrip('/')!=metadata['resource'].rstrip('/'):
                raise ManagerError('The server endpoint changed during sign-in. Open the login screen again.')
            info={**metadata,'status':'authorized','error':None,'expires_at':bundle['expires_at']}
            self.write_secret('oauth:'+server['id'],json.dumps({**info,**bundle}))

    def token(self,server):
        info=server.get('oauth',{})
        if info.get('status') not in ('authorized','imported'): raise ManagerError('Authentication required. Open the login screen to sign in again.')
        configured=self._resource(server)
        if not isinstance(configured,str) or configured.rstrip('/')!=info.get('resource','').rstrip('/'):
            raise ManagerError('Authentication required. The server endpoint changed; sign in to this endpoint again.')
        try: bundle=json.loads(self.read_secret('oauth:'+server['id']))
        except Exception: raise ManagerError('Authentication required. The OAuth credential is unavailable; open the login screen again.') from None
        if bundle.get('expires_at',0)>time.time()+60: return bundle['access_token']
        refresh=bundle.get('refresh_token')
        if not isinstance(refresh,str) or not refresh: raise ManagerError('Authentication required. The OAuth session expired; open the login screen again.')
        if not info.get('token_endpoint'):
            try:
                metadata,resource,scope=discovery(configured)
            except ManagerError:
                raise ManagerError('The upstream OAuth metadata is unavailable. The imported sign-in is preserved; retry when the provider is reachable.') from None
            info={**info,'token_endpoint':metadata['token_endpoint'],'resource':resource,'scope':info.get('scope') or scope}
        try:
            token=json_request(info['token_endpoint'],info['resource'],{'grant_type':'refresh_token','refresh_token':refresh,'client_id':info['client_id'],'resource':info['resource']},form=True)
            token.setdefault('refresh_token',refresh)
            self._save_token(server,token,{k:v for k,v in info.items() if k not in ('status','error','expires_at')})
            return token['access_token']
        except ManagerError:
            self._failed(server,'Your sign-in expired. Open the login screen to sign in again.')
            raise ManagerError('Authentication required. Open the login screen to sign in again.') from None

    def _failed(self,server,message):
        with self.store.lock:
            try: server=self.store.server(server['id'])
            except ManagerError: return
            server['oauth']={**server.get('oauth',{}),'status':'failed','error':message}
            self.store.save()

    def _close_flow(self,flow):
        if timer:=flow.get('timer'):timer.cancel()
        flow['server'].shutdown();flow['server'].server_close()

    def cancel(self, sid):
        with self.lock:
            flow = self.pending.pop(sid, None)
        if flow: self._close_flow(flow)

    def close(self):
        with self.lock:
            flows=list(self.pending.values());self.pending.clear()
        for flow in flows:self._close_flow(flow)
