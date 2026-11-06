"""Bounded MCP stdio and Streamable HTTP sessions. Health never calls a tool."""
from __future__ import annotations

import json
import http.client
import io
import os
import re
import selectors
import signal
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from catalog import ManagerError

PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_MESSAGE = 2 * 1024 * 1024


def expanded(value):
    if isinstance(value, dict): return {k: expanded(v) for k, v in value.items()}
    if isinstance(value, list): return [expanded(v) for v in value]
    if not isinstance(value, str): return value
    def replace(m):
        name = m[1]
        if name not in os.environ: raise ManagerError('Required environment variable is unavailable: ' + name)
        return os.environ[name]
    value = re.sub(r'\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}', replace, value)
    if '${' in value: raise ManagerError('Interactive client variables are unsupported in the shared daemon.')
    return value


class Session:
    def __init__(self):
        self.sequence = 0
        self.identity = None
        self.capabilities = {}
        self.protocol = PROTOCOLS[0]

    def initialize(self):
        result = self.request('initialize', {'protocolVersion': PROTOCOLS[0], 'capabilities': {}, 'clientInfo': {'name': 'mcp-manager', 'version': '0.1.0'}})
        protocol = result.get('protocolVersion')
        if protocol not in PROTOCOLS: raise ManagerError('Upstream negotiated an unsupported MCP protocol version.')
        self.protocol = protocol
        self.identity = result.get('serverInfo', {})
        self.capabilities = result.get('capabilities', {})
        self.request('notifications/initialized', notification=True)
        return self.identity

    def tools(self):
        if 'tools' not in self.capabilities: return []
        tools, cursor, seen = [], None, set()
        deadline = time.monotonic() + 30
        for _ in range(20):
            if time.monotonic() > deadline: raise ManagerError('Tool listing exceeded its time limit.')
            page = self.request('tools/list', {'cursor': cursor} if cursor else {})
            batch = page.get('tools', [])
            if not isinstance(batch, list): raise ManagerError('Invalid tools/list response.')
            for tool in batch:
                if not isinstance(tool, dict) or not isinstance(tool.get('name'), str): raise ManagerError('Invalid tool definition.')
                if tool['name'] in seen: raise ManagerError('Upstream listed duplicate tool names.')
                seen.add(tool['name'])
                tools.append(tool)
            if len(tools) > 1000: raise ManagerError('Upstream exceeds the 1,000 tool limit.')
            cursor = page.get('nextCursor')
            if not cursor: return tools
        raise ManagerError('Upstream pagination exceeds 20 pages.')

    def request(self, method, params=None, notification=False):
        self.sequence += 1
        message = {'jsonrpc': '2.0', 'method': method}
        if params is not None: message['params'] = params
        if not notification: message['id'] = self.sequence
        response = self.exchange(message)
        if notification: return {}
        if not isinstance(response, dict) or response.get('id') != self.sequence: raise ManagerError('Invalid upstream response identifier.')
        if 'error' in response:
            # Do not persist arbitrary provider error text, which may echo credentials.
            raise ManagerError('Upstream rejected ' + method + ' (code ' + str(response['error'].get('code', 'unknown')) + ').')
        if not isinstance(response.get('result'), dict): raise ManagerError('Invalid upstream result.')
        return response['result']


class StdioSession(Session):
    def __init__(self, config, timeout=8):
        super().__init__()
        self.timeout = timeout
        self.buffer = b''
        self.stderr_bytes = 0
        config = expanded(config)
        command = config.get('command')
        args = config.get('args', [])
        if not isinstance(command, str) or not isinstance(args, list) or any(not isinstance(a, str) for a in args): raise ManagerError('Invalid stdio command.')
        env = {k: v for k, v in os.environ.items() if k in ('HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'SHELL', 'SSH_AUTH_SOCK', 'SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS')}
        env['PATH'] = env.get('PATH', '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
        env.update({k: os.environ[k] for k in config.get('env_vars', []) if k in os.environ})
        configured_env = config.get('env', {})
        if not isinstance(configured_env, dict) or any(not isinstance(v, str) for v in configured_env.values()): raise ManagerError('Environment values must be strings.')
        env.update(configured_env)
        try:
            self.process = subprocess.Popen([command, *args], cwd=config.get('cwd') or None, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        except OSError as e: raise ManagerError('Could not launch local server (' + type(e).__name__ + '). Check command, working directory and executable permissions.') from None
        self.selector = selectors.DefaultSelector()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr): os.set_blocking(pipe.fileno(), False)
        self.selector.register(self.process.stdout, selectors.EVENT_READ, 'stdout')
        self.selector.register(self.process.stderr, selectors.EVENT_READ, 'stderr')

    def _send(self, message, deadline):
        content = json.dumps(message, separators=(',', ':')).encode() + b'\n'
        if len(content) > MAX_MESSAGE: raise ManagerError('MCP request exceeds 2 MB limit.')
        offset = 0
        while offset < len(content):
            try:
                count = os.write(self.process.stdin.fileno(), content[offset:])
                offset += count
            except BlockingIOError:
                if time.monotonic() > deadline: raise ManagerError('Local server stopped accepting input.')
                time.sleep(.005)
            except (OSError, ValueError): raise ManagerError('Local server closed its input.') from None

    def exchange(self, message):
        deadline = time.monotonic() + self.timeout
        self._send(message, deadline)
        if 'id' not in message: return None
        notifications = 0
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if len(line) > MAX_MESSAGE: raise ManagerError('Local server output exceeded the 2 MB limit.')
                try: response = json.loads(line)
                except (ValueError, UnicodeError): raise ManagerError('Local server emitted invalid MCP JSON.') from None
                if not isinstance(response, dict): raise ManagerError('Local server emitted an invalid MCP message.')
                if response.get('id') == message['id'] and 'method' not in response: return response
                notifications += 1
                if notifications > 1000: raise ManagerError('Local server emitted too many messages.')
                if 'method' in response and 'id' in response:
                    self._send({'jsonrpc': '2.0', 'id': response['id'], 'error': {'code': -32601, 'message': 'Client-side requests are not supported by the shared gateway.'}}, deadline)
            if len(self.buffer) > MAX_MESSAGE: raise ManagerError('Local server output exceeded the 2 MB limit.')
            events = self.selector.select(max(0, deadline - time.monotonic()))
            for key, _ in events:
                try: chunk = os.read(key.fd, 65536)
                except BlockingIOError: continue
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.data == 'stdout': raise ManagerError('Local server exited before completing the handshake/request.')
                elif key.data == 'stdout': self.buffer += chunk
                else:
                    self.stderr_bytes += len(chunk)
                    if self.stderr_bytes > MAX_MESSAGE: raise ManagerError('Local server diagnostic output exceeded the 2 MB limit.')
        raise ManagerError('Local server timed out. Its process group was stopped.')

    def close(self):
        try: os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: self.process.wait(timeout=.3)
        except subprocess.TimeoutExpired: pass
        try: os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        self.process.wait(timeout=2)
        self.selector.close()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr): pipe.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class DeadlineReader(io.RawIOBase):
    """Every socket read, including HTTP headers, consumes one absolute budget."""
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
        self.stream = sock.makefile('rb', buffering=0)
    def close(self):
        self.stream.close()
        super().close()
    def readable(self): return True
    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('HTTP deadline exceeded')
        self.sock.settimeout(remaining)
        return self.stream.readinto(buffer)


class DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
    def makefile(self, mode):
        return io.BufferedReader(DeadlineReader(self.sock, self.deadline))


def deadline_opener(deadline):
    def factory(cls):
        def create(host, **kwargs):
            connection = cls(host, **kwargs)
            def response(sock, *args, **kw):
                return http.client.HTTPResponse(DeadlineSocket(sock, deadline), *args, **kw)
            connection.response_class = response
            return connection
        return create
    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, req): return self.do_open(factory(http.client.HTTPConnection), req)
    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req): return self.do_open(factory(http.client.HTTPSConnection), req, context=self._context)
    return urllib.request.build_opener(NoRedirect(), HTTP(), HTTPS())


class HTTPSession(Session):
    def __init__(self, config, timeout=10):
        super().__init__()
        self.timeout = timeout
        config = expanded(config)
        self.url = config.get('url', '')
        try: parsed = urlsplit(self.url)
        except ValueError: raise ManagerError('Invalid upstream URL.') from None
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment: raise ManagerError('Use an HTTPS MCP endpoint without embedded credentials or fragments.')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'): raise ManagerError('Remote servers require HTTPS; HTTP is allowed only on loopback.')
        self.headers = dict(config.get('http_headers', config.get('headers', {})))
        for header, envname in config.get('env_http_headers', {}).items():
            if envname not in os.environ: raise ManagerError('Required header environment variable is unavailable: ' + envname)
            self.headers[header] = os.environ[envname]
        if envname := config.get('bearer_token_env_var'):
            if envname not in os.environ: raise ManagerError('Required token environment variable is unavailable: ' + envname)
            self.headers['Authorization'] = 'Bearer ' + os.environ[envname]
        if any(not isinstance(k, str) or not isinstance(v, str) or '\n' in k + v or '\r' in k + v for k,v in self.headers.items()): raise ManagerError('Invalid upstream headers.')
        self.headers.update({'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        self.session_id = None
        self.opener = urllib.request.build_opener(NoRedirect())

    def exchange(self, message):
        headers = self.headers.copy()
        if self.session_id: headers['Mcp-Session-Id'] = self.session_id
        if message['method'] != 'initialize': headers['MCP-Protocol-Version'] = self.protocol
        request = urllib.request.Request(self.url, data=json.dumps(message).encode(), headers=headers, method='POST')
        deadline = time.monotonic() + self.timeout
        try:
            with deadline_opener(deadline).open(request, timeout=self.timeout) as response:
                if sid := response.headers.get('Mcp-Session-Id'):
                    if len(sid) > 1024 or not all(0x21 <= ord(c) <= 0x7e for c in sid): raise ManagerError('Invalid HTTP session ID.')
                    self.session_id = sid
                if 'id' not in message: return None
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/event-stream' in content_type:
                    total, data, deadline = 0, [], time.monotonic() + self.timeout
                    while time.monotonic() < deadline:
                        line = response.readline(MAX_MESSAGE + 1)
                        total += len(line)
                        if total > MAX_MESSAGE: raise ManagerError('HTTP event stream exceeded 2 MB limit.')
                        if not line: break
                        if line in (b'\n', b'\r\n'):
                            if data:
                                event = json.loads('\n'.join(data)); data = []
                                if isinstance(event, dict) and event.get('id') == message['id']: return event
                        elif line.startswith(b'data:'): data.append(line[5:].decode().strip())
                    raise ManagerError('HTTP event stream ended or timed out without a matching response.')
                body = response.read(MAX_MESSAGE + 1)
                if len(body) > MAX_MESSAGE: raise ManagerError('HTTP response exceeded 2 MB limit.')
                if 'application/json' not in content_type: raise ManagerError('Endpoint is not Streamable HTTP MCP. Legacy SSE is unsupported.')
                return json.loads(body)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (401, 403): raise ManagerError('Authentication required. Add or replace a credential, or sign in through the owning client.') from None
            if 300 <= e.code < 400: raise ManagerError('Upstream redirect rejected. Configure the final trusted endpoint URL.') from None
            if e.code == 404 and self.session_id:
                self.session_id = None
                raise ManagerError('Upstream HTTP session expired. Check again to reconnect.') from None
            raise ManagerError('Upstream HTTP returned status ' + str(e.code) + '.') from None
        except (urllib.error.URLError, TimeoutError, OSError): raise ManagerError('HTTP connection failed or timed out. Check endpoint, TLS trust and network access.') from None
        except (ValueError, UnicodeError): raise ManagerError('Upstream HTTP returned invalid MCP JSON.') from None

    def close(self):
        # Session deletion is optional in MCP. Avoid extra network calls during cleanup.
        self.session_id = None
