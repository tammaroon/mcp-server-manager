import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from catalog import Store, ManagerError
from transport import StdioSession, HTTPSession
from gateway import Gateway, rpc, socket_path

FIXTURE = str(Path(__file__).parent / 'fixtures/upstream.py')
BACKEND = str(Path(__file__).resolve().parents[1] / 'backend/manager.py')


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.marker = self.root / 'methods'
        config = self.home / '.cursor/mcp.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'mcpServers': {'fixture': {'command': sys.executable, 'args': [FIXTURE], 'env': {'METHOD_MARKER': str(self.marker)}}}}))
        self.store = Store(self.root / 'state', self.home)
        self.store.rescan()
        self.server = self.store.data['servers'][0]
        self.gateway = Gateway(self.store)
    def tearDown(self):
        self.gateway.close()
        self.tmp.cleanup()
    def test_common_config_action_returns_live_shared_file(self):
        result = self.gateway.control({'action': 'common_config'})
        path = Path(result['path'])
        self.assertEqual(path.name, 'mcp.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('fixture', json.loads(path.read_text())['mcpServers'])
        self.assertEqual(self.gateway.snapshot()['common_config_path'], str(path))

    def test_health_check_never_executes_tool(self):
        result = self.gateway.check(self.server['id'])
        self.assertEqual(result['status'], 'reachable')
        self.assertEqual(result['auth'], 'Not required')
        self.assertEqual(result['identity']['name'], 'Fixture')
        self.assertEqual(self.marker.read_text().splitlines(), ['initialize', 'notifications/initialized', 'tools/list'])
    def test_listing_and_calling_enforce_client_allowlist(self):
        self.store.update(self.server['id'], {'tool_allowlists': {'cursor': ['echo']}})
        result = self.gateway.handle('cursor', {'id': 1, 'method': 'tools/list'})
        tools = result['result']['tools']
        self.assertEqual(len(tools), 1)
        name = tools[0]['name']
        self.assertTrue(name.endswith('__echo'))
        call = self.gateway.handle('cursor', {'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': {'text': 'hello'}}})
        self.assertEqual(call['result']['content'][0]['text'], 'hello')
        denied = self.gateway.handle('codex', {'id': 3, 'method': 'tools/call', 'params': {'name': name, 'arguments': {}}})
        self.assertEqual(denied['error']['code'], -32602)
        self.store.update(self.server['id'], {'clients': []})
        revoked = self.gateway.handle('cursor', {'id': 4, 'method': 'tools/call', 'params': {'name': name, 'arguments': {}}})
        self.assertIn('error', revoked)
    def test_disabling_direct_entry_keeps_imported_gateway_available(self):
        path = self.home / '.cursor/mcp.json'
        doc = json.loads(path.read_text())
        doc['mcpServers']['fixture']['disabled'] = True
        path.write_text(json.dumps(doc))
        self.store.rescan()
        result = self.gateway.check(self.server['id'])
        self.assertEqual(result['status'], 'reachable')

    def test_disabled_server_cannot_be_invoked(self):
        self.store.update(self.server['id'], {'enabled': False})
        result = self.gateway.handle('cursor', {'id': 1, 'method': 'tools/list'})
        self.assertEqual(result['result']['tools'], [])
        self.assertFalse(self.marker.exists())
    def test_silent_process_times_out_and_is_reaped(self):
        session = StdioSession({'command': sys.executable, 'args': [FIXTURE, 'silent']}, timeout=.25)
        start = time.monotonic()
        try:
            with self.assertRaises(ManagerError): session.initialize()
        finally: session.close()
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNotNone(session.process.poll())
    def test_stdout_flood_is_bounded(self):
        session = StdioSession({'command': sys.executable, 'args': [FIXTURE, 'flood']}, timeout=.5)
        try:
            with self.assertRaises(ManagerError): session.initialize()
        finally: session.close()
        self.assertIsNotNone(session.process.poll())
    def test_source_removal_keeps_common_owned_connection(self):
        self.gateway.check(self.server['id'])
        (self.home / '.cursor/mcp.json').write_text('{"mcpServers":{}}')
        result = self.gateway.handle('cursor', {'id': 1, 'method': 'tools/list'})
        self.assertTrue(result['result']['tools'])


class DaemonTests(unittest.TestCase):
    def test_real_bridge_handshake_and_socket_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / 'home'
            home.mkdir()
            state = root / 'state'
            daemon = subprocess.Popen([sys.executable, BACKEND, '--state', str(state), '--home', str(home), 'daemon'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not socket_path(state).exists() and time.monotonic() < deadline: time.sleep(.025)
                snap = rpc(state, {'action': 'snapshot'})
                self.assertEqual(snap['servers'], [])
                self.assertEqual(socket_path(state).stat().st_mode & 0o777, 0o600)
                message = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}}) + '\n'
                bridge = subprocess.run([sys.executable, BACKEND, '--state', str(state), 'bridge', '--client', 'codex'], input=message, text=True, capture_output=True, timeout=5)
                self.assertEqual(bridge.returncode, 0, bridge.stderr)
                response = json.loads(bridge.stdout)
                self.assertEqual(response['result']['serverInfo']['name'], 'mcp-manager')
                self.assertEqual(response['result']['capabilities'], {'tools': {'listChanged': False}})
                live_bridge = subprocess.Popen([sys.executable, BACKEND, '--state', str(state), 'bridge', '--client', 'codex'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                live_bridge.stdin.write(message); live_bridge.stdin.flush()
                self.assertEqual(json.loads(live_bridge.stdout.readline())['id'], 1)
                daemon.terminate(); daemon.communicate(timeout=5)
                try:
                    self.assertNotEqual(live_bridge.wait(timeout=2), 0)
                finally:
                    if live_bridge.poll() is None: live_bridge.kill()
                    live_bridge.communicate(timeout=3)
            finally:
                daemon.terminate()
                daemon.communicate(timeout=5)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((req, dict(self.headers)))
                if self.path == '/auth':
                    self.send_response(401); self.end_headers(); return
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', '/mcp'); self.end_headers(); return
                if 'id' not in req:
                    self.send_response(202); self.end_headers(); return
                if req['method'] == 'initialize':
                    result = {'protocolVersion': '2025-11-25', 'serverInfo': {'name': 'HTTP fixture'}, 'capabilities': {'tools': {}}}
                else: result = {'tools': []}
                body = json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}).encode()
                self.send_response(200)
                if self.path == '/sse': body = b'event: message\ndata: ' + body + b'\n\n'
                self.send_header('Content-Type', 'text/event-stream' if self.path == '/sse' else 'application/json')
                self.send_header('Mcp-Session-Id', 'test-session')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.path == '/trickle':
                    try:
                        for byte in body:
                            self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.03)
                    except (BrokenPipeError, ConnectionResetError): pass
                else: self.wfile.write(body)
        self.http = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}'
    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
    def test_http_handshake_preserves_session_and_protocol(self):
        s = HTTPSession({'url': self.base + '/mcp'})
        try:
            s.initialize(); self.assertEqual(s.tools(), [])
        finally: s.close()
        self.assertEqual([r[0]['method'] for r in self.requests], ['initialize', 'notifications/initialized', 'tools/list'])
        headers = {k.lower(): v for k,v in self.requests[-1][1].items()}
        self.assertEqual(headers['mcp-session-id'], 'test-session')
        self.assertEqual(headers['mcp-protocol-version'], '2025-11-25')
    def test_post_sse_handshake_and_listing(self):
        s = HTTPSession({'url': self.base + '/sse'})
        try:
            self.assertEqual(s.initialize()['name'], 'HTTP fixture')
            self.assertEqual(s.tools(), [])
        finally: s.close()

    def test_trickling_response_has_absolute_deadline(self):
        s = HTTPSession({'url': self.base + '/trickle'}, timeout=.2)
        start = time.monotonic()
        try:
            with self.assertRaises(ManagerError): s.initialize()
        finally: s.close()
        self.assertLess(time.monotonic() - start, 1)

    def test_remote_plain_http_is_rejected(self):
        with self.assertRaises(ManagerError): HTTPSession({'url': 'http://remote.example/mcp'})
    def test_redirect_never_forwards_credentials(self):
        s = HTTPSession({'url': self.base + '/redirect', 'headers': {'Authorization': 'Bearer test'}})
        try:
            with self.assertRaises(ManagerError): s.initialize()
        finally: s.close()
        self.assertEqual(len(self.requests), 1)
    def test_common_file_replacement_overrides_source_token_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / '.cursor/mcp.json'
            config.parent.mkdir()
            config.write_text(json.dumps({'mcpServers': {'remote': {'url': self.base + '/mcp', 'http_headers': {'authorization': 'Bearer old-inline'}, 'env_http_headers': {'Authorization': 'OLD_HEADER'}, 'bearer_token_env_var': 'OLD_TOKEN'}}}))
            store = Store(root / 'state', root)
            store.rescan()
            server = store.data['servers'][0]
            gateway = Gateway(store)
            try:
                gateway.common.set_credential(server['id'], 'replacement')
                with patch.dict(os.environ, {'OLD_TOKEN': 'old-token', 'OLD_HEADER': 'Bearer old-header'}):
                    self.assertEqual(gateway.check(server['id'])['status'], 'reachable')
                headers = {k.lower():v for k,v in self.requests[0][1].items()}
                self.assertEqual(headers['authorization'], 'Bearer replacement')
            finally: gateway.close()

    def test_manual_credential_can_replace_missing_oauth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'.cursor/mcp.json';source.parent.mkdir()
            source.write_text(json.dumps({'mcpServers':{'remote':{'url':self.base+'/mcp','oauth':{'access_token':'old-access','expires_at':0},'http_headers':{'Authorization':'Bearer old-access'}}}}))
            store=Store(root/'state',root);store.rescan();gateway=Gateway(store)
            try:
                server=store.data['servers'][0]
                gateway.common.set_credential(server['id'],'replacement')
                self.assertEqual(gateway.check(server['id'])['status'],'reachable')
                self.assertEqual({k.lower():v for k,v in self.requests[0][1].items()}['authorization'],'Bearer replacement')
            finally:gateway.close()

    def test_auth_required_is_distinct(self):
        s = HTTPSession({'url': self.base + '/auth'})
        try:
            with self.assertRaisesRegex(ManagerError, 'Authentication required'): s.initialize()
        finally: s.close()

if __name__ == '__main__': unittest.main()
