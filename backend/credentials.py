"""Credentials stored exclusively in the user's private common MCP JSON file."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
from catalog import ManagerError

_locks = {}
_guard = threading.Lock()

@contextmanager
def file_lock(path):
    """Serialize common configuration edits, including nested calls."""
    path = Path(path)
    with _guard:
        lock, local = _locks.setdefault(str(path.resolve()), (threading.RLock(), threading.local()))
    with lock:
        if getattr(local, 'depth', 0):
            local.depth += 1
            try: yield
            finally: local.depth -= 1
            return
        lock_path = path.with_name(path.name + '.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            local.depth = 1
            try: yield
            finally: local.depth = 0
        finally: os.close(descriptor)


def read_document(path):
    if path.is_symlink(): raise ManagerError('Symlink common MCP configuration paths are not supported.')
    if not path.exists(): return {'mcpServers': {}}, b''
    if path.stat().st_size > 4 * 1024 * 1024: raise ManagerError('Common MCP configuration exceeds the 4 MB limit.')
    raw = path.read_bytes()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError()
            result[key] = value
        return result
    try:
        doc = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(doc, dict) or not isinstance(doc.get('mcpServers'), dict): raise ValueError()
        if set(doc) - {'mcpServers', 'credentials'}: raise ValueError()
        if any(not key or not isinstance(value, dict) for key,value in doc['mcpServers'].items()): raise ValueError()
        credentials = doc.get('credentials', {})
        if not isinstance(credentials, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k,v in credentials.items()): raise ValueError()
    except (ValueError, UnicodeError): raise ManagerError('Invalid common MCP configuration; original file preserved.') from None
    return doc, raw


class FileCredentials:
    """OAuth bundle compatibility API; values live in the server's own entry."""
    def __init__(self, store):
        self.store = store
        self.path = Path(store.root) / 'mcp.json'

    def read(self, account):
        with self.store.lock, file_lock(self.path):
            document, _ = read_document(self.path)
            if 'credentials' in document and account in document['credentials']:
                return document['credentials'][account]
            prefix = 'oauth-access:' if account.startswith('oauth-access:') else 'oauth:'
            if not account.startswith(prefix): raise ManagerError('Credentials are stored directly in each MCP server entry.')
            try:
                server = self.store.server(account[len(prefix):])
                bundle = document['mcpServers'][server.get('common_key', server['name'])]['oauth']
                if prefix == 'oauth-access:':
                    value = bundle['access_token']
                    if not isinstance(value, str) or not value: raise ValueError()
                    return value
                return json.dumps(bundle)
            except (KeyError, ValueError, TypeError):
                raise ManagerError('Authentication required. OAuth login is unavailable in this server entry.') from None

    def write(self, account, value):
        if not account.startswith('oauth:'): raise ManagerError('Set credentials directly in the server configuration.')
        try: bundle = json.loads(value)
        except (ValueError, TypeError): raise ManagerError('Invalid OAuth bundle.') from None
        if not isinstance(bundle, dict) or not isinstance(bundle.get('access_token'),str) or not bundle['access_token']:
            raise ManagerError('Invalid OAuth bundle.')
        from common_config import CommonConfig
        sid = account[len('oauth:'):]
        with self.store.lock:
            server = self.store.server(sid)
            CommonConfig(self.store).save_oauth(sid, {**server.get('oauth', {}), **bundle})
