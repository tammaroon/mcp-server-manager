"""Secret-safe discovery, catalog persistence and reviewed client config changes."""
from __future__ import annotations

import copy
import difflib
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit, urlunsplit

CLIENTS = {
    'codex': ('Codex', '.codex/config.toml', 'mcp_servers', 'Codex.app'),
    'claude': ('Claude Desktop', 'Library/Application Support/Claude/claude_desktop_config.json', 'mcpServers', 'Claude.app'),
    'cursor': ('Cursor', '.cursor/mcp.json', 'mcpServers', 'Cursor.app'),
    'vscode': ('VS Code', 'Library/Application Support/Code/User/mcp.json', 'servers', 'Visual Studio Code.app'),
}
SENSITIVE = re.compile(r'authorization|cookie|token|secret|password|passwd|api.?key|credential|private.?key', re.I)


class ManagerError(Exception):
    pass


def redact(value, key=''):
    if SENSITIVE.search(key) and key not in ('credential_id', 'credential_set', 'credential_header'):
        return '[REDACTED]'
    if isinstance(value, dict):
        if key.lower() in ('headers', 'http_headers', 'env'):
            return {k: '[REDACTED]' for k in value}
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    if not isinstance(value, str): return value
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED KEY]', value, flags=re.S)
    value = re.sub(r'(?i)\b(Bearer|Basic)\s+[^\s,"\'}]+', r'\1 [REDACTED]', value)
    value = re.sub(r'(?i)((?:authorization|cookie|token|secret|password|passwd|api[_-]?key)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED JWT]', value)
    def clean_url(match):
        try:
            u = urlsplit(match[0])
            host = u.hostname or ''
            if ':' in host: host = '[' + host + ']'
            if u.port: host += ':' + str(u.port)
            return urlunsplit((u.scheme, host, u.path, '[REDACTED]' if u.query else '', ''))
        except ValueError: return '[REDACTED URL]'
    return re.sub(r'https?://[^\s"<>]+', clean_url, value)


def atomic_write(path: Path, data: bytes, *, expected_original=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if expected_original is not None:
            current = path.read_bytes() if path.exists() else b''
            if path.is_symlink() or current != expected_original:
                raise ManagerError('Configuration changed during apply. Preview the change again.')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def read_config(path: Path):
    if not path.exists(): return {}
    if path.stat().st_size > 4 * 1024 * 1024: raise ManagerError('Configuration exceeds 4 MB limit.')
    try:
        data = tomllib.loads(path.read_text()) if path.suffix == '.toml' else json.loads(path.read_text())
        if not isinstance(data, dict): raise ValueError('Expected an object')
        return data
    except (ValueError, OSError) as e:
        raise ManagerError(f'Cannot parse {path.name}; original file preserved. ({type(e).__name__})') from None


def definitions(home, client):
    _, rel, key, _ = CLIENTS[client]
    data = read_config(Path(home) / rel)
    result = data.get(key, {})
    if not isinstance(result, dict): raise ManagerError('Server configuration must be an object.')
    return result


def discovery_identity(config):
    """Identify a launch target without exporting its credentials or arguments."""
    if isinstance(config.get('url'), str) and config['url']:
        value = config['url']
        try:
            url = urlsplit(value)
            host = url.hostname or ''
            if ':' in host: host = '[' + host + ']'
            if url.port is not None: host += ':' + str(url.port)
            # URL userinfo and query remain significant; HTTP fragments do not.
            userinfo = url.netloc.rsplit('@', 1)[0] + '@' if '@' in url.netloc else ''
            value = urlunsplit((url.scheme.lower(), userinfo + host.lower(), url.path.rstrip('/'), url.query, ''))
        except ValueError:
            pass
        target = ('url', value)
    elif isinstance(config.get('command'), str) and config['command']:
        target = ('command', config['command'])
    else:
        return None
    return hashlib.sha256(json.dumps(target).encode()).hexdigest()


def internal_server(name, config):
    normalized = re.sub(r'[^a-z0-9]', '', str(name).lower())
    if normalized in ('noderepl', 'mcpnoderepl', 'computeruse', 'unifiedcomputeruse', 'mcpcomputeruse'):
        return True
    signature = re.compile(r'(?<![a-z0-9])(?:node[-_]repl|(?:unified[-_])?computer[-_]use)(?![a-z0-9])', re.I)
    args = config.get('args', [])
    launch = [config.get('command', '')] + (args if isinstance(args, list) else [])
    return any(isinstance(value, str) and signature.search(value) for value in launch)


def discover(home: Path):
    servers, clients, errors = [], [], []
    equivalent = {}
    for cid, (name, rel, key, app) in CLIENTS.items():
        path = home / rel
        client = {'id': cid, 'name': name, 'config_path': str(path), 'installed': path.exists() or (Path('/Applications') / app).exists() or (home / 'Applications' / app).exists(), 'configured': False, 'bridge_count': 0, 'error': None}
        try:
            defs = definitions(home, cid)
            entry = defs.get('mcp-manager', {})
            if not isinstance(entry, dict): raise ManagerError('The mcp-manager entry must be an object.')
            client['configured'] = bool(entry.get('command')) and 'bridge' in entry.get('args', []) and entry.get('enabled', True) is not False and not entry.get('disabled', False)
            for name, config in defs.items():
                if name == 'mcp-manager' or not isinstance(config, dict) or internal_server(name, config): continue
                sid = hashlib.sha256(f'{cid}:{name}'.encode()).hexdigest()[:12]
                http = bool(config.get('url'))
                transport = 'HTTP' if http else 'stdio'
                supported = bool(config.get('command') or config.get('url')) and config.get('type') != 'sse'
                headers = config.get('http_headers', config.get('headers', {}))
                auth = 'Source credential' if headers or config.get('bearer_token_env_var') or config.get('env_http_headers') else ('Not validated' if http else 'Local process')
                fingerprint = discovery_identity(config) or ('unsupported', cid, name)
                source = {'client': cid, 'path': str(path), 'name': name}
                if fingerprint in equivalent:
                    existing = equivalent[fingerprint]
                    existing['sources'].append(source)
                    if cid not in existing['clients']: existing['clients'].append(cid)
                    continue
                servers.append({'discovery_identity': fingerprint if isinstance(fingerprint, str) else None, 'sources': [source], 'id': sid, 'name': name, 'transport': transport, 'source_client': cid, 'source_path': str(path), 'source_name': name, 'endpoint': redact(config.get('url', config.get('command', 'Unsupported definition'))), 'enabled': config.get('enabled', True) is not False and not config.get('disabled', False), 'supported': supported, 'clients': [cid], 'tool_allowlists': {}, 'auth': auth, 'credential_set': False, 'credential_header': 'Authorization', 'status': 'not_checked' if supported else 'unsupported', 'last_checked': None, 'duration_ms': None, 'tools': [], 'identity': None, 'diagnostic': 'Imported by reference. No connection check has run.' if supported else 'This definition uses an unsupported transport or has no command/URL.'})
                equivalent[fingerprint] = servers[-1]
        except (ManagerError, OSError) as e:
            client['error'] = redact(str(e))
            errors.append(f'{name}: {redact(str(e))}')
        clients.append(client)
    return servers, clients, errors


class Store:
    def __init__(self, state: Path, home: Path):
        self.root, self.home = Path(state), Path(home)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or self.root.stat().st_uid != os.getuid(): raise ManagerError('State directory must be owned by this user and not a symlink.')
        self.root.chmod(0o700)
        self.path = self.root / 'catalog.json'
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text()) if self.path.exists() else {'servers': [], 'settings': {'check_interval': 0}}
        except (OSError, ValueError): raise ManagerError('Catalog is unreadable; original file preserved.') from None
        self.clients, self.errors = [], []

    def save(self):
        atomic_write(self.path, json.dumps(self.data, indent=2).encode())

    def rescan(self):
        with self.lock:
            fresh, self.clients, self.errors = discover(self.home)
            def sources(server):
                return {(v['client'], v['name']) for v in server.get('sources', [
                    {'client': server['source_client'], 'name': server.get('source_name', server['name'])}])}

            def excluded(server):
                config = server.get('definition', {'command': server.get('endpoint', '')})
                if internal_server(server['name'], config) or internal_server(server.get('source_name', ''), config):
                    return True
                for cid, name in sources(server):
                    if internal_server(name, {}): return True
                    if cid not in CLIENTS: continue
                    try:
                        source = definitions(self.home, cid).get(name, {})
                    except (ManagerError, OSError):
                        continue
                    if isinstance(source, dict) and internal_server(name, source): return True
                return False

            def prior_identity(server):
                if server.get('discovery_identity'): return server['discovery_identity']
                endpoint = server.get('endpoint', '')
                # Old catalogs stored redacted URLs. Never treat two redacted queries as equal.
                if not endpoint or endpoint == 'Unsupported definition' or '[REDACTED' in endpoint: return None
                return discovery_identity({'url' if server.get('transport') == 'HTTP' else 'command': endpoint})

            def canonical(records, preferred_id=None):
                # Keep edits and credential references paired with the ID that owns them.
                return min(records, key=lambda prior: (
                    not bool(prior.get('definition')), not bool(prior.get('credential_set')),
                    not bool(prior.get('oauth')), prior['id'] != preferred_id))

            def retain_permissions(server, records):
                # Existing permission decisions are restrictive when legacy duplicates merge.
                server['enabled'] = all(prior['enabled'] for prior in records)
                server['clients'] = sorted(set.intersection(*(set(prior['clients']) for prior in records)))
                combined_rules = {}
                for cid in CLIENTS:
                    lists = [set(prior.get('tool_allowlists', {})[cid]) for prior in records if cid in prior.get('tool_allowlists', {})]
                    if lists: combined_rules[cid] = sorted(set.intersection(*lists))
                server['tool_allowlists'] = combined_rules

            def was_deleted(server):
                for deleted in self.data.get('deleted_servers', []):
                    old_sources = {(v['client'], v['name']) for v in deleted.get('sources', [])}
                    if server['id'] == deleted['id'] or sources(server) & old_sources:
                        return True
                    if server.get('discovery_identity') in deleted.get('identities', []):
                        return True
                return False

            fresh = [server for server in fresh if not was_deleted(server)]
            history = [server for server in self.data['servers'] if not excluded(server)]
            old = {server['id']: server for server in history}
            merged = []
            for server in fresh:
                matches = [prior for prior in history if sources(prior) & sources(server)
                           or (server.get('discovery_identity') and prior_identity(prior) == server['discovery_identity'])]
                if matches:
                    previous = canonical(matches, server['id'])
                    if previous.get('definition') and previous['id'] not in old: continue
                    aliases = server.get('sources', []) + [v for prior in matches for v in prior.get('sources', [])]
                    server['sources'] = list({(v['client'], v['name']): v for v in aliases}.values())
                    if previous.get('definition') and (previous['source_client'], previous.get('source_name', previous['name'])) in sources(server):
                        # Source references in an edited entry retain their credential source while it exists.
                        try:
                            available = definitions(self.home, previous['source_client']).get(previous.get('source_name', previous['name'])) if previous['source_client'] in CLIENTS else None
                        except (ManagerError, OSError):
                            available = None
                        if isinstance(available, dict):
                            for key in ('source_client', 'source_path', 'source_name'):
                                server[key] = previous[key]
                    if previous['id'] in old: server['id'] = previous['id']
                    for prior in matches: old.pop(prior['id'], None)
                    for key in ('last_checked', 'duration_ms', 'tools', 'identity', 'diagnostic', 'status', 'credential_set', 'credential_header', 'definition', 'oauth', 'common_key'):
                        if key in previous: server[key] = copy.deepcopy(previous[key])
                    retain_permissions(server, matches)
                if server.get('definition'):
                    config = server['definition']
                    server.update(transport='HTTP' if config.get('url') else 'stdio', endpoint=previous.get('endpoint', server['endpoint']) if matches else server['endpoint'], supported=True)
                merged.append(server)
            for server in old.values():
                if not server.get('definition'):
                    server.update(status='unsupported', supported=False, diagnostic='Source definition no longer exists. Rediscover after restoring its configuration.')
                merged.append(server)
            # An edited manager target may coincide with another discovered target.
            # Compare literal launch fields only; equal source references can resolve differently.
            groups = {}
            for server in merged:
                if server.get('definition'):
                    config = server['definition']
                    target = config.get('url', config.get('command', ''))
                    identity = discovery_identity(config) if isinstance(target, str) and '${' not in target else None
                else:
                    identity = prior_identity(server)
                groups.setdefault(identity or ('server', server['id']), []).append(server)
            consolidated = []
            for records in groups.values():
                server = canonical(records)
                if len(records) > 1:
                    retain_permissions(server, records)
                    aliases = [v for record in records for v in record.get('sources', [])]
                    server['sources'] = list({(v['client'], v['name']): v for v in aliases}.values())
                consolidated.append(server)
            self.data['servers'] = consolidated
            self.save()

    def server(self, sid):
        for s in self.data['servers']:
            if s['id'] == sid: return s
        raise ManagerError('Unknown server.')

    def update(self, sid, changes):
        with self.lock:
            server = self.server(sid)
            if 'enabled' in changes:
                if not isinstance(changes['enabled'], bool): raise ManagerError('Enabled must be boolean.')
                server['enabled'] = changes['enabled']
            if 'clients' in changes:
                if not isinstance(changes['clients'], list) or any(c not in CLIENTS for c in changes['clients']): raise ManagerError('Unknown client.')
                server['clients'] = sorted(set(changes['clients']))
            if 'tool_allowlists' in changes:
                rules = changes['tool_allowlists']
                if not isinstance(rules, dict) or any(c not in CLIENTS or not isinstance(v, list) or any(not isinstance(t, str) for t in v) for c, v in rules.items()): raise ManagerError('Invalid tool allowlist.')
                server['tool_allowlists'] = rules
            self.save()
            return copy.deepcopy(server)

    def resolve(self, server):
        from server_config import resolve_config
        from credentials import FileCredentials
        if 'definition' not in server and self.server(server['id'])['source_client'] != 'manager':
            config = definitions(self.home, server['source_client']).get(server['source_name'])
            if not isinstance(config, dict): raise ManagerError('Original configuration was removed.')
        return resolve_config(self, server, FileCredentials(self).read)


class ConfigAdapter:
    def __init__(self, home, client, bridge_command):
        if client not in CLIENTS: raise ManagerError('Unsupported client.')
        self.client = client
        self.path = Path(home).resolve() / CLIENTS[client][1]
        self.key = CLIENTS[client][2]
        self.bridge_command = bridge_command

    def _contents(self, remove=False):
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents): raise ManagerError('Symlink config paths are not supported; edit the real config manually.')
        original = self.path.read_bytes() if self.path.exists() else b''
        data = read_config(self.path)
        container = data.get(self.key, {})
        if not isinstance(container, dict): raise ManagerError('Server configuration must be an object.')
        command = {'command': self.bridge_command[0], 'args': self.bridge_command[1:]}
        if self.client == 'vscode': command['type'] = 'stdio'
        if self.path.suffix == '.toml':
            text = original.decode()
            # Retain every untouched byte and remove only the manager's own table tree.
            lines, keep = [], True
            for line in text.splitlines(keepends=True):
                if re.match(r'^\s*\[', line):
                    try:
                        table = tomllib.loads(line + '\n')
                        own = 'mcp-manager' in table.get('mcp_servers', {})
                    except ValueError: own = False
                    keep = not own
                if keep: lines.append(line)
            text = ''.join(lines)
            if not remove:
                text = text.rstrip() + '\n\n[mcp_servers."mcp-manager"]\ncommand = ' + json.dumps(command['command'], ensure_ascii=False) + '\nargs = ' + json.dumps(command['args'], ensure_ascii=False) + '\n'
            tomllib.loads(text)  # Validate output before presenting or applying.
            proposed = text.encode()
        else:
            data.setdefault(self.key, {})
            if remove: data[self.key].pop('mcp-manager', None)
            else: data[self.key]['mcp-manager'] = command
            proposed = (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode()
        return original, proposed

    def preview(self, remove=False):
        original, proposed = self._contents(remove)
        digest = hashlib.sha256(original + b'\0' + proposed).hexdigest()
        # Redact whole changed lines if they contain a header/env object or secret assignment.
        def safe_toml(raw):
            text = raw.decode()
            secrets = []
            def collect(value, sensitive=False):
                if isinstance(value, dict):
                    for k, v in value.items(): collect(v, sensitive or bool(SENSITIVE.search(k)) or k.lower() in ('headers', 'http_headers', 'env'))
                elif isinstance(value, list):
                    for v in value: collect(v, sensitive)
                elif sensitive and isinstance(value, str) and value: secrets.append(value)
            collect(tomllib.loads(text))
            for secret in sorted(set(secrets), key=len, reverse=True):
                text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]').replace(secret, '[REDACTED]')
            return text
        before_text = safe_toml(original) if self.path.suffix == '.toml' else original.decode()
        after_text = safe_toml(proposed) if self.path.suffix == '.toml' else proposed.decode()
        diff = ''.join(difflib.unified_diff(before_text.splitlines(True), after_text.splitlines(True), fromfile=str(self.path), tofile=str(self.path) + ' (proposed)'))
        safe_lines = []
        for line in diff.splitlines():
            if SENSITIVE.search(line) or re.search(r'["\'](?:headers|http_headers|env)["\']\s*:', line):
                safe_lines.append(line[:1] + '  [REDACTED configuration line]')
            else: safe_lines.append(redact(line))
        # JSON reformatting could reveal unknown header/env keys. Show structural safe JSON diff instead.
        if self.path.suffix != '.toml':
            before = json.dumps(redact(json.loads(original or b'{}')), indent=2).splitlines(True)
            after = json.dumps(redact(json.loads(proposed)), indent=2).splitlines(True)
            safe_lines = list(difflib.unified_diff(before, after, fromfile=str(self.path), tofile='Proposed configuration'))
        return {'client': self.client, 'path': str(self.path), 'digest': digest, 'diff': '\n'.join(safe_lines) or 'No changes.', 'remove': remove, 'warning': 'Existing upstream entries are preserved. Restart the client after applying. Disable migrated direct entries in that client to avoid duplicate tools.'}

    def apply(self, digest, remove=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lockpath = self.path.parent / ("." + self.path.name + ".mcp-manager.lock")
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._apply_locked(digest, remove)

    def _apply_locked(self, digest, remove=False):
        original, proposed = self._contents(remove)
        if hashlib.sha256(original + b'\0' + proposed).hexdigest() != digest: raise ManagerError('Configuration changed since review. Preview the change again.')
        backup = None
        if self.path.exists():
            backup = str(self.path) + '.mcp-manager-backup-' + str(time.time_ns())
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f: f.write(original)
        atomic_write(self.path, proposed, expected_original=original)
        return {'backup': backup, 'path': str(self.path)}
