"""Per-user Unix-socket daemon, upstream pool and policy-enforced MCP facade."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from catalog import CLIENTS, ConfigAdapter, ManagerError, Store, redact, SENSITIVE
from transport import HTTPSession, StdioSession, MAX_MESSAGE, PROTOCOLS
from oauth import OAuthManager
from common_config import CommonConfig
from credentials import FileCredentials
from server_config import raw_config
from tool_names import ToolNames


def socket_path(state):
    identity = hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:12]
    return Path('/tmp') / f'mcp-manager-{os.getuid()}-{identity}' / 'gateway.sock'


class Gateway:
    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.server_locks = {}
        self.lock = threading.RLock()
        self.bridges = {}
        self.closing = threading.Event()
        self.started = time.time()
        self.last_checked = None
        self.names = ToolNames(store)
        self.credentials = FileCredentials(store)
        self.common = CommonConfig(store)
        self.common_error = None
        self.oauth = OAuthManager(store, self.credentials.read, self.credentials.write, self._oauth_changed)
        self._sync_common()

    def _sync_common(self, strict=False):
        try:
            changed = self.common.sync()
            self.common_error = None
        except ManagerError as e:
            self.common_error = str(e)
            if strict: raise
            return
        for sid in changed: self.restart(sid)

    def _oauth_changed(self, sid):
        self.restart(sid)
        self._sync_common()
        # Release the browser callback while the upstream handshake runs.
        threading.Thread(target=self._check_after_login, args=(sid,), daemon=True).start()

    def _check_after_login(self, sid):
        if self.closing.is_set(): return
        try: self.check(sid)
        except ManagerError: pass  # A server may be deleted before the worker starts.

    def _server_lock(self, sid):
        with self.lock: return self.server_locks.setdefault(sid, threading.RLock())

    def _session(self, server):
        if self.closing.is_set(): raise ManagerError('Gateway is shutting down.')
        raw = raw_config(self.store, server)
        oauth = raw.get('oauth', {})
        headers = {k.lower():v for field in ('headers','http_headers') for k,v in raw.get(field,{}).items()}
        using_oauth = bool(oauth) and headers.get('authorization') == 'Bearer ' + oauth.get('access_token','') and server.get('oauth',{}).get('status') != 'inactive'
        if using_oauth: self.oauth.token(server)
        config = self.store.resolve(server)
        if using_oauth and config.get('url', '').rstrip('/') != server.get('oauth', {}).get('resource', '').rstrip('/'):
            raise ManagerError('Authentication required. This endpoint needs its own sign-in.')
        if not server['supported']: raise ManagerError('Unsupported server definition.')
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        entry = self.sessions.get(server['id'])
        if entry and entry['fingerprint'] == fingerprint: return entry
        self.restart(server['id'])
        session = HTTPSession(config) if config.get('url') else StdioSession(config)
        try:
            session.initialize()
            tools = session.tools()
        except Exception:
            session.close()
            raise
        supplied_credentials = bool(config.get('http_headers') or config.get('headers') or config.get('env_http_headers') or config.get('bearer_token_env_var'))
        entry = {'session': session, 'provided_credentials': supplied_credentials, 'fingerprint': fingerprint, 'tools': tools, 'last_used': time.monotonic()}
        with self.lock:
            if self.closing.is_set():
                session.close()
                raise ManagerError('Gateway is shutting down.')
            self.sessions[server['id']] = entry
        return entry

    def restart(self, sid):
        with self._server_lock(sid):
            if entry := self.sessions.pop(sid, None): entry['session'].close()

    def check(self, sid):
        with self._server_lock(sid):
            server = self.store.server(sid)
            if not server['enabled']:
                self.restart(sid)
                return dict(server, status='disabled')
            start = time.monotonic()
            try:
                self.restart(sid)
                entry = self._session(server)
                server.update(status='reachable', auth=('Daemon authenticated upstream' if server['transport'] == 'HTTP' and entry['provided_credentials'] else 'Not required'), identity=redact(entry['session'].identity), tools=[redact(t) for t in entry['tools']], diagnostic='MCP initialization and tools/list succeeded. No tool was executed.')
            except Exception as e:
                self.restart(sid)
                message = str(e) if isinstance(e, ManagerError) else 'Unexpected upstream failure (' + type(e).__name__ + ').'
                status = 'auth_required' if 'Authentication required' in message or 'credential' in message.lower() else ('unsupported' if not server['supported'] else 'failed')
                server.update(status=status, diagnostic=redact(message), tools=[], identity=None)
                if status == 'auth_required': server['auth'] = 'Authentication required'
            server.update(last_checked=time.time(), duration_ms=round((time.monotonic() - start) * 1000))
            self.last_checked = time.time()
            with self.store.lock: self.store.save()
            return copy.deepcopy(server)

    def start_configured_servers(self):
        """Warm opted-in upstreams once per daemon launch; never execute tools."""
        ids = []
        with self.store.lock:
            for server in self.store.data['servers']:
                if server['enabled'] and server['supported'] and raw_config(self.store, server).get('start_on_launch', False):
                    ids.append(server['id'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self._check_after_login, ids))

    def check_all(self):
        ids = [s['id'] for s in self.store.data['servers'] if s['enabled'] and s['supported']]
        with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(self.check, ids))
        self.last_checked = time.time()
        return self.snapshot()

    def snapshot(self):
        self._sync_common()
        with self.store.lock, self.lock:
            servers = copy.deepcopy(self.store.data['servers'])
            clients = copy.deepcopy(self.store.clients)
            for s in servers:
                s['can_sign_in'] = s['transport'] == 'HTTP' and s['supported']
                s['oauth_status'] = s.get('oauth', {}).get('status')
                s['oauth_error'] = s.get('oauth', {}).get('error')
                if not s['enabled']: s['status'] = 'disabled'
            for c in clients: c['bridge_count'] = sum(v == c['id'] for v in self.bridges.values())
            return {'servers': servers, 'clients': clients, 'errors': self.store.errors + ([self.common_error] if self.common_error else []), 'common_config_path': str(self.common.path), 'settings': self.store.data['settings'], 'daemon': {'status': 'reachable', 'started': self.started, 'last_checked': self.last_checked, 'socket': str(socket_path(self.store.root)), 'bridge_count': len(self.bridges)}, 'version': '0.1.0'}

    def _permitted(self, server, client, tool=None):
        if not server['enabled'] or not server['supported'] or client not in server['clients']: return False
        rules = server.get('tool_allowlists', {})
        return tool is None or client not in rules or tool in rules[client]

    def handle(self, client, message):
        rid = message.get('id')
        method = message.get('method')
        if 'id' not in message: return None
        response = {'jsonrpc': '2.0', 'id': rid}
        try:
            if method in ('tools/list', 'tools/call'): self._sync_common(strict=True)
            if client not in CLIENTS: raise ManagerError('Unknown client identity.')
            if method == 'initialize':
                version = message.get('params', {}).get('protocolVersion')
                result = {'protocolVersion': version if version in PROTOCOLS else PROTOCOLS[0], 'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': {'name': 'mcp-manager', 'title': 'MCP Server Manager', 'version': '0.1.0'}, 'instructions': 'Tools are namespaced by upstream server. Permissions are managed in MCP Server Manager. Reconnect after changing permissions.'}
            elif method == 'ping': result = {}
            elif method == 'tools/list':
                tools = []
                for server in list(self.store.data['servers']):
                    if not self._permitted(server, client): continue
                    with self._server_lock(server['id']):
                        try: server = self.store.server(server['id'])
                        except ManagerError: continue
                        if not self._permitted(server, client): continue
                        try:
                            entry = self._session(server)
                            aliases = self.names.register(server, [tool['name'] for tool in entry['tools']])
                            for tool in entry['tools']:
                                if self._permitted(server, client, tool['name']):
                                    tool = copy.deepcopy(tool)
                                    tool['description'] = '[' + server['name'] + '] ' + tool.get('description', '')
                                    tool['name'] = aliases[tool['name']]
                                    tools.append(tool)
                        except ManagerError:
                            self.check(server['id'])
                result = {'tools': tools}
            elif method == 'tools/call':
                params = message.get('params', {})
                name = params.get('name')
                arguments = params.get('arguments', {})
                if not isinstance(name, str) or not isinstance(arguments, dict): raise ManagerError('Invalid tool call.')
                route = self.names.resolve(name)
                if route is None: raise ManagerError('Tool is unavailable or this client is not allowed to use it.')
                sid, original = route
                with self._server_lock(sid):
                    try: server = self.store.server(sid)
                    except ManagerError: raise ManagerError('Tool is unavailable.') from None
                    if not self._permitted(server, client, original): raise ManagerError('Tool is unavailable or access was revoked.')
                    entry = self._session(server)
                    if not any(tool['name'] == original for tool in entry['tools']): raise ManagerError('Tool is unavailable.')
                    if not self._permitted(self.store.server(sid), client, original): raise ManagerError('Tool is unavailable or access was revoked.')
                    entry['session'].timeout = 60
                    try: result = entry['session'].request('tools/call', {'name': original, 'arguments': arguments})
                    except Exception:
                        self.restart(sid)
                        raise
                    finally: entry['session'].timeout = 10 if server['transport'] == 'HTTP' else 8

            else:
                response['error'] = {'code': -32601, 'message': 'Method not supported by this tools-only gateway.'}
                return response
            response['result'] = result
        except Exception as e:
            response['error'] = {'code': -32602 if isinstance(e, ManagerError) else -32603, 'message': redact(str(e)) if isinstance(e, ManagerError) else 'Internal gateway error.'}
        return response

    def bridge_command(self, client):
        return [sys.executable, str(Path(__file__).with_name('manager.py').resolve()), '--state', str(self.store.root), 'bridge', '--client', client]

    def control(self, request):
        action = request.get('action')
        self._sync_common(strict=action not in ('snapshot', 'common_config'))
        if action == 'common_config': return {'path': str(self.common.path)}
        if action == 'oauth_start':
            sid = request['server_id']
            with self._server_lock(sid): return self.oauth.start(self.store.server(sid))
        if action == 'snapshot': return self.snapshot()
        if action in ('server_config', 'save_server_config'):
            from server_config import editor_config, save_editor_config
            sid = request['server_id']
            with self._server_lock(sid):
                server = self.store.server(sid)
                if action == 'server_config': return editor_config(self.store, server)
                save_editor_config(self.store, server, request.get('config_json'), request.get('revision'))
                self.restart(sid)
            return self.snapshot()
        if action == 'delete':
            if request.get('confirmed') is not True: raise ManagerError('Confirm deletion of this MCP server first.')
            sid = request['server_id']
            with self._server_lock(sid):
                self.common.delete(sid)
                self.restart(sid)
                self.oauth.cancel(sid)
            return self.snapshot()
        if action == 'discover':
            # A rescan closes old sessions so removed/changed definitions cannot stay live.
            for sid in list(self.sessions): self.restart(sid)
            self.store.rescan()
            return self.snapshot()
        if action == 'check':
            if request.get('server_id'): self.check(request['server_id']); return self.snapshot()
            return self.check_all()
        if action == 'update':
            sid = request['server_id']
            with self._server_lock(sid):
                self.store.update(sid, request.get('changes', {}))
                if not self.store.server(sid)['enabled']: self.restart(sid)
            return self.snapshot()
        if action == 'restart':
            self.restart(request['server_id'])
            self.check(request['server_id'])
            return self.snapshot()
        if action in ('preview', 'apply'):
            adapter = ConfigAdapter(self.store.home, request['client'], self.bridge_command(request['client']))
            if action == 'preview': return adapter.preview(request.get('remove', False))
            result = adapter.apply(request['digest'], request.get('remove', False))
            _, self.store.clients, self.store.errors = __import__('catalog').discover(self.store.home)
            return result
        if action == 'settings':
            interval = request.get('check_interval', 0)
            if interval not in (0, 300, 900, 1800, 3600): raise ManagerError('Unsupported check interval.')
            self.store.data['settings']['check_interval'] = interval
            self.store.save()
            return self.snapshot()
        if action == 'add':
            name = request.get('name', '').strip()
            if not name or len(name) > 80: raise ManagerError('Enter a name of up to 80 characters.')
            definition = request.get('definition', {})
            if not isinstance(definition, dict) or set(definition) - {'url', 'command', 'args', 'cwd'}: raise ManagerError('Use only endpoint or command, arguments and working directory. Add credentials in the common configuration.')
            if definition.get('url'):
                # Validates TLS/URL rules without connecting.
                HTTPSession(definition).close()
                if redact(definition['url']) != definition['url']: raise ManagerError('Do not embed credentials or query parameters in the endpoint. Use the common configuration.')
            elif not isinstance(definition.get('command'), str) or not definition['command']: raise ManagerError('Enter a command or URL.')
            if any(SENSITIVE.search(str(a)) for a in definition.get('args', [])): raise ManagerError('Credential arguments are not supported. Use the common configuration.')
            sid = uuid.uuid4().hex[:12]
            server = {'id': sid, 'name': name, 'transport': 'HTTP' if definition.get('url') else 'stdio', 'source_client': 'manager', 'source_path': str(self.store.path), 'source_name': name, 'endpoint': definition.get('url', definition.get('command')), 'enabled': True, 'supported': True, 'clients': [], 'tool_allowlists': {}, 'auth': 'Not validated' if definition.get('url') else 'Local process', 'credential_set': False, 'credential_header': 'Authorization', 'status': 'not_checked', 'last_checked': None, 'duration_ms': None, 'tools': [], 'identity': None, 'diagnostic': 'Added to catalog. Choose allowed clients and check the connection.', 'definition': definition}
            self.store.data['servers'].append(server)
            self.store.save()
            return self.snapshot()
        raise ManagerError('Unknown action.')

    def close(self):
        with self.lock: self.closing.set()
        self.oauth.close()
        for sid in list(self.sessions): self.restart(sid)


def rpc(state, request, timeout=120):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path(state)))
        sock.sendall(json.dumps({'kind': 'control', 'request': request}).encode() + b'\n')
        stream = sock.makefile('rb')
        line = stream.readline(MAX_MESSAGE + 1)
        if len(line) > MAX_MESSAGE: raise ManagerError('Gateway response exceeded limit.')
        result = json.loads(line)
        if not result.get('ok'): raise ManagerError(result.get('error', 'Gateway request failed.'))
        return result['result']


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        gateway = self.server.gateway
        bridge_id = None
        self.connection.settimeout(180)
        # Filesystem permissions are the primary boundary. Verify the peer UID when supported.
        try:
            if hasattr(self.connection, 'getpeereid'):
                uid, _ = self.connection.getpeereid()
                if uid != os.getuid(): return
            first = self.rfile.readline(MAX_MESSAGE + 1)
            if len(first) > MAX_MESSAGE: return
            envelope = json.loads(first)
            if envelope.get('kind') == 'control':
                try: result = {'ok': True, 'result': gateway.control(envelope['request'])}
                except Exception as e: result = {'ok': False, 'error': redact(str(e)) if isinstance(e, ManagerError) else 'Request failed (' + type(e).__name__ + ').'}
                self.send(result)
                return
            if envelope.get('kind') != 'bridge' or envelope.get('client') not in CLIENTS: return
            client = envelope['client']
            bridge_id = uuid.uuid4().hex
            with gateway.lock: gateway.bridges[bridge_id] = client
            self.connection.settimeout(None)
            initialized = False
            while True:
                raw = self.rfile.readline(MAX_MESSAGE + 1)
                if not raw: return
                if len(raw) > MAX_MESSAGE: return
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str): raise ValueError()
                    if message['method'] == 'initialize': initialized = True
                    elif not initialized and 'id' in message:
                        self.send({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32002, 'message': 'Initialize the connection first.'}})
                        continue
                    if result := gateway.handle(client, message): self.send(result)
                except (ValueError, UnicodeError): self.send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON-RPC message.'}})
        except (OSError, ValueError, UnicodeError, BrokenPipeError): pass
        finally:
            if bridge_id:
                with gateway.lock: gateway.bridges.pop(bridge_id, None)

    def send(self, obj):
        payload = json.dumps(obj, separators=(',', ':')).encode() + b'\n'
        if len(payload) > MAX_MESSAGE: payload = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"Response exceeds 2 MB limit."}}\n'
        self.wfile.write(payload)
        self.wfile.flush()


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32


def run_daemon(state, home):
    store = Store(state, home)
    lockfile = open(store.root / 'daemon.lock', 'a')
    os.chmod(lockfile.name, 0o600)
    try: fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: return
    store.rescan()
    gateway = Gateway(store)
    path = socket_path(state)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid(): raise ManagerError('Unsafe socket directory.')
    path.parent.chmod(0o700)
    path.unlink(missing_ok=True)
    old_mask = os.umask(0o177)
    try: server = UnixServer(str(path), RequestHandler)
    finally: os.umask(old_mask)
    server.gateway = gateway
    os.chmod(path, 0o600)
    stop = threading.Event()
    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    def maintenance():
        last = time.monotonic()
        while not stop.wait(10):
            interval = store.data['settings'].get('check_interval', 0)
            if interval and time.monotonic() - last >= interval:
                gateway.check_all(); last = time.monotonic()
    startup = threading.Thread(target=gateway.start_configured_servers, daemon=True)
    startup.start()
    threading.Thread(target=maintenance, daemon=True).start()
    try: server.serve_forever(poll_interval=.1)
    finally:
        stop.set()
        gateway.close()
        startup.join()
        server.server_close()
        path.unlink(missing_ok=True)
        lockfile.close()


def ensure_daemon(state, home):
    try: return rpc(state, {'action': 'snapshot'}, timeout=2)
    except (OSError, ValueError, ManagerError): pass
    # All launches coordinate via flock. A bridge can start the shared daemon when the UI is closed.
    command = [sys.executable, str(Path(__file__).with_name('manager.py')), '--state', str(state), '--home', str(home), 'daemon']
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try: return rpc(state, {'action': 'snapshot'}, timeout=1)
        except (OSError, ValueError, ManagerError): time.sleep(.05)
    raise ManagerError('Gateway could not start. Check state directory permissions and Python 3.11+ availability.')


def run_bridge(state, client):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(socket_path(state)))
        sock.sendall(json.dumps({'kind': 'bridge', 'client': client}).encode() + b'\n')
        failed = threading.Event()
        input_closed = threading.Event()
        def receive():
            try:
                with sock.makefile('rb') as stream:
                    while line := stream.readline(MAX_MESSAGE + 1):
                        if len(line) > MAX_MESSAGE: break
                        sys.stdout.buffer.write(line); sys.stdout.buffer.flush()
            except (OSError, BrokenPipeError): pass
            finally:
                failed.set()
                if not input_closed.is_set(): os._exit(1)
        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        try:
            while line := sys.stdin.buffer.readline(MAX_MESSAGE + 1):
                if len(line) > MAX_MESSAGE: raise ManagerError('Client message exceeds 2 MB limit.')
                if failed.is_set(): raise ManagerError('Shared gateway disconnected. Reconnect the client.')
                sock.sendall(line)
            input_closed.set()
            sock.shutdown(socket.SHUT_WR)
            reader.join(timeout=120)
        except (OSError, BrokenPipeError): raise ManagerError('Shared gateway disconnected.') from None
