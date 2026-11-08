import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from catalog import Store, ManagerError
from credentials import FileCredentials
from oauth import OAuthManager
from server_config import editor_config,save_editor_config


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests = []
        self.received_headers = []
        requests = self.requests
        received_headers = self.received_headers
        self.token_gate={'block_next':False,'arrived':threading.Event(),'release':threading.Event()}
        token_gate=self.token_gate
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, data, code=200, headers=None):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                for k,v in (headers or {}).items(): self.send_header(k,v)
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                base = f'http://127.0.0.1:{self.server.server_port}'
                if self.path == '/mcp': self.reply({},401,{'WWW-Authenticate': f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource", scope="tools:read"'})
                elif self.path == '/.well-known/oauth-protected-resource': self.reply({'resource': base+'/mcp', 'authorization_servers':[base]})
                elif self.path == '/.well-known/oauth-authorization-server': self.reply({'issuer':base,'authorization_endpoint':base+'/authorize','token_endpoint':base+'/token','registration_endpoint':base+'/register','token_endpoint_auth_methods_supported':['none'],'code_challenge_methods_supported':['S256']})
                else: self.reply({},404)
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                payload = json.loads(raw) if 'application/json' in self.headers['Content-Type'] else {k:v[0] for k,v in urllib.parse.parse_qs(raw.decode()).items()}
                requests.append((self.path,payload))
                received_headers.append((self.path, dict(self.headers)))
                if self.path == '/register': self.reply({'client_id':'fixture-client','token_endpoint_auth_method':'none'})
                elif self.path == '/token':
                    if token_gate['block_next']:
                        token_gate['block_next']=False;token_gate['arrived'].set();token_gate['release'].wait(5)
                    self.reply({'access_token':'fixture-access-secret','refresh_token':'fixture-refresh-secret','token_type':'Bearer','expires_in':3600})
                elif self.path == '/mcp':
                    if payload.get('method') == 'initialize': result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'OAuth fixture'}}
                    elif payload.get('method') == 'tools/list': result={'tools':[]}
                    else: result={}
                    self.reply({'jsonrpc':'2.0','id':payload.get('id'),'result':result})
                else: self.reply({},404)
        self.http = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.http.serve_forever,daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}'
        path=self.root/'.cursor/mcp.json';path.parent.mkdir()
        path.write_text(json.dumps({'mcpServers': {'webexapis': {'url':self.base+'/mcp'}}}))
        self.store=Store(self.root/'state',self.root);self.store.rescan();self.server=self.store.data['servers'][0]
        self.credentials=FileCredentials(self.store);self.changed=[]
        self.oauth=OAuthManager(self.store, self.credentials.read, self.credentials.write, self.changed.append)
    def tearDown(self):
        self.token_gate['release'].set();self.oauth.close();self.http.shutdown();self.http.server_close();self.thread.join();self.tmp.cleanup()
    def callback(self, started, **changes):
        params={k:v[0] for k,v in urllib.parse.parse_qs(urllib.parse.urlsplit(started['authorization_url']).query).items()}
        query={'state':params['state'],'code':'fixture-code'};query.update(changes)
        url=params['redirect_uri']+'?'+urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=5) as response: response.read()
        return params
    def test_browser_login_uses_pkce_and_keeps_tokens_out_of_catalog(self):
        started=self.oauth.start(self.server)
        self.assertEqual(self.server['oauth']['status'],'pending')
        auth=self.callback(started)
        self.assertEqual(self.server['oauth']['status'],'authorized')
        self.assertEqual(auth['resource'],self.base+'/mcp')
        token=next(payload for path,payload in self.requests if path=='/token')
        challenge=base64.urlsafe_b64encode(hashlib.sha256(token['code_verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(challenge,auth['code_challenge'])
        self.assertEqual(token['redirect_uri'],auth['redirect_uri'])
        self.assertEqual(token['code'],'fixture-code')
        self.assertEqual(self.server['definition']['oauth'],'${common:/oauth}')
        text=self.store.path.read_text()
        for secret in ('fixture-access-secret','fixture-refresh-secret','fixture-code',auth['state'],token['code_verifier']): self.assertNotIn(secret,text)
        self.assertEqual(self.oauth.token(self.server),'fixture-access-secret')
        self.assertEqual(self.changed,[self.server['id']])
        common=json.loads((self.store.root/'mcp.json').read_text())
        self.assertNotIn('credentials',common)
        saved=common['mcpServers']['webexapis']['oauth']
        self.assertEqual(saved['access_token'],'fixture-access-secret')
        self.assertEqual(saved['refresh_token'],'fixture-refresh-secret')
        restarted=Store(self.store.root,self.root)
        credentials=FileCredentials(restarted)
        oauth=OAuthManager(restarted,credentials.read,credentials.write,lambda _:None)
        try:
            self.assertEqual(oauth.token(restarted.server(self.server['id'])),'fixture-access-secret')
            self.assertEqual(restarted.resolve(restarted.server(self.server['id']))['http_headers']['Authorization'],'Bearer fixture-access-secret')
        finally:oauth.close()
    def test_bad_state_does_not_exchange_code_or_finish_pending_flow(self):
        started=self.oauth.start(self.server)
        with self.assertRaises(urllib.error.HTTPError) as caught:self.callback(started,state='wrong-state')
        caught.exception.close()
        self.assertEqual(self.server['oauth']['status'],'pending')
        self.assertNotIn('/token',[p for p,_ in self.requests])
        self.callback(started)
        self.assertEqual(self.server['oauth']['status'],'authorized')
    def test_expired_token_is_refreshed_for_the_same_resource(self):
        started=self.oauth.start(self.server);self.callback(started)
        account='oauth:'+self.server['id']
        bundle=json.loads(self.credentials.read(account));bundle['expires_at']=0;self.credentials.write(account,json.dumps(bundle))
        self.assertEqual(self.oauth.token(self.server),'fixture-access-secret')
        refreshed=self.requests[-1][1]
        self.assertEqual(refreshed['grant_type'],'refresh_token')
        self.assertEqual(refreshed['refresh_token'],'fixture-refresh-secret')
        self.assertEqual(refreshed['resource'],self.base+'/mcp')
    def test_provider_denial_clears_pending_login(self):
        started=self.oauth.start(self.server);self.callback(started,error='access_denied')
        self.assertEqual(self.server['oauth']['status'],'failed')
        self.assertNotIn('/token',[p for p,_ in self.requests])
    def test_changed_resource_cannot_reuse_existing_oauth_token(self):
        started=self.oauth.start(self.server);self.callback(started)
        path=self.store.root/'mcp.json';doc=json.loads(path.read_text())
        doc['mcpServers']['webexapis']['url']=self.base+'/different-resource'
        path.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ManagerError,'endpoint changed'):self.oauth.token(self.server)

    def test_login_can_restart_when_previous_file_credential_is_missing(self):
        started=self.oauth.start(self.server);self.callback(started)
        view=editor_config(self.store,self.server);config=json.loads(view['config_json'])
        config.pop('oauth')
        save_editor_config(self.store,self.server,json.dumps(config),view['revision'])
        next_login=self.oauth.start(self.server)
        self.assertTrue(next_login['authorization_url'])
        self.assertEqual(self.server['oauth']['status'],'pending')

    def test_browser_login_recovers_when_manual_credential_is_missing(self):
        self.server.update(credential_set=True,credential_header='Authorization')
        login=self.oauth.start(self.server)
        self.assertTrue(login['authorization_url'])
        self.assertEqual(self.server['oauth']['status'],'pending')

    def test_gateway_uses_completed_oauth_login_upstream(self):
        from gateway import Gateway
        started=self.oauth.start(self.server);self.callback(started)
        gateway=Gateway(self.store)
        try:self.assertEqual(gateway.check(self.server['id'])['status'],'reachable')
        finally:gateway.close()
        headers=next(h for path,h in self.received_headers if path=='/mcp')
        self.assertEqual(headers.get('Authorization'),'Bearer fixture-access-secret')

    def test_gateway_checks_connection_automatically_after_successful_login(self):
        from gateway import Gateway
        gateway=Gateway(self.store)
        try:
            started=gateway.oauth.start(self.server)
            self.callback(started)
            deadline=time.monotonic()+3
            while self.server.get('last_checked') is None and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(self.server['status'],'reachable')
            self.assertIsNotNone(self.server['last_checked'])
            self.assertEqual(self.server['oauth']['status'],'authorized')
            methods=[payload.get('method') for path,payload in self.requests if path=='/mcp']
            self.assertEqual(methods,['initialize','notifications/initialized','tools/list'])
        finally: gateway.close()

    def test_gateway_does_not_check_connection_after_denied_login(self):
        from gateway import Gateway
        gateway=Gateway(self.store)
        try:
            started=gateway.oauth.start(self.server)
            self.callback(started,error='access_denied')
            self.assertEqual(self.server['oauth']['status'],'failed')
            self.assertIsNone(self.server['last_checked'])
            self.assertNotIn('/mcp',[path for path,_ in self.requests])
        finally: gateway.close()

    def test_deleting_server_cancels_its_pending_browser_login(self):
        from gateway import Gateway
        gateway=Gateway(self.store)
        try:
            started=gateway.oauth.start(self.server)
            sid=self.server['id']
            gateway.control({'action':'delete','server_id':sid,'confirmed':True})
            self.assertNotIn(sid,gateway.oauth.pending)
            with self.assertRaises(urllib.error.URLError):self.callback(started)
            with self.assertRaises(ManagerError):self.credentials.read('oauth:'+sid)
            self.assertNotIn('/token',[path for path,_ in self.requests])
        finally:gateway.close()

    def test_discovery_during_sign_in_updates_current_catalog_record(self):
        started=self.oauth.start(self.server)
        sid=self.server['id']
        self.store.rescan()
        self.assertIsNot(self.server,self.store.server(sid))
        self.callback(started)
        current=self.store.server(sid)
        self.assertEqual(current['oauth']['status'],'authorized')
        self.assertEqual(current['definition']['oauth'],'${common:/oauth}')

    def test_replaced_inflight_callback_cannot_commit_or_remove_new_flow(self):
        first=self.oauth.start(self.server)
        self.token_gate['block_next']=True
        outcome={}
        def old_callback():
            try:self.callback(first);outcome['code']=200
            except urllib.error.HTTPError as e:outcome['code']=e.code;e.close()
        thread=threading.Thread(target=old_callback);thread.start()
        self.assertTrue(self.token_gate['arrived'].wait(3))
        second=self.oauth.start(self.server)
        self.token_gate['release'].set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.server(self.server['id'])['oauth']['status'],'pending')
        self.assertIn(self.server['id'],self.oauth.pending)
        self.callback(second)
        self.assertEqual(self.store.server(self.server['id'])['oauth']['status'],'authorized')

    def test_browser_disconnect_still_closes_callback_listener(self):
        started=self.oauth.start(self.server)
        auth={k:v[0] for k,v in urllib.parse.parse_qs(urllib.parse.urlsplit(started['authorization_url']).query).items()}
        redirect=urllib.parse.urlsplit(auth['redirect_uri'])
        flow=self.oauth.pending[self.server['id']]
        self.token_gate['block_next']=True
        sock=socket.create_connection((redirect.hostname,redirect.port),timeout=3)
        query=urllib.parse.urlencode({'state':auth['state'],'code':'fixture-code'})
        sock.sendall(('GET '+redirect.path+'?'+query+' HTTP/1.1\r\nHost: '+redirect.netloc+'\r\nConnection: close\r\n\r\n').encode())
        try:
            self.assertTrue(self.token_gate['arrived'].wait(3))
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));sock.close()
            self.token_gate['release'].set()
            deadline=time.monotonic()+3
            while flow['server'].fileno()!=-1 and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(flow['server'].fileno(),-1)
            self.assertNotIn(self.server['id'],self.oauth.pending)
        finally:
            sock.close();self.token_gate['release'].set()
            if flow['server'].fileno()!=-1:flow['server'].shutdown();flow['server'].server_close()

    def test_imported_codex_login_discovers_refresh_endpoint_lazily(self):
        self.server['oauth']={'status':'imported','client_id':'fixture-client','resource':self.base+'/mcp','issuer':self.base}
        self.credentials.write('oauth:'+self.server['id'],json.dumps({'access_token':'old-access','refresh_token':'imported-refresh','expires_at':0}))
        token=self.oauth.token(self.server)
        self.assertEqual(token,'fixture-access-secret')
        self.assertEqual(self.server['oauth']['status'],'authorized')
        self.assertEqual(self.requests[-1][1]['refresh_token'],'imported-refresh')
        self.assertNotIn('/register',[path for path,_ in self.requests])

    def test_unavailable_imported_provider_keeps_its_stored_login(self):
        self.server['oauth']={'status':'imported','client_id':'fixture-client','resource':self.base+'/mcp','issuer':self.base}
        account='oauth:'+self.server['id']
        original=json.dumps({'access_token':'old-access','refresh_token':'imported-refresh','expires_at':0})
        self.credentials.write(account,original)
        with patch('oauth.discovery',side_effect=ManagerError('Provider unavailable')):
            with self.assertRaises(ManagerError):self.oauth.token(self.server)
        self.assertEqual(json.loads(self.credentials.read(account))['refresh_token'],json.loads(original)['refresh_token'])
        self.assertEqual(self.server['oauth']['status'],'imported')

    def test_start_again_cancels_old_callback_listener(self):
        first=self.oauth.start(self.server)
        second=self.oauth.start(self.server)
        self.assertNotEqual(first['authorization_url'],second['authorization_url'])
        self.callback(second)
        self.assertEqual(self.server['oauth']['status'],'authorized')

if __name__=='__main__':unittest.main()
