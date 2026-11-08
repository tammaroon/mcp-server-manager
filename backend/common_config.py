"""Canonical per-server MCP JSON, with a non-secret catalog mirror."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import threading
from catalog import ManagerError, atomic_write, discovery_identity, redact
from credentials import file_lock, read_document
from server_config import all_refs, digest, inline_legacy_config, safe_definition, source_config, validate


class CommonConfig:
    def __init__(self, store):
        self.store = store
        self.path = Path(store.root) / 'mcp.json'
        self.lock = threading.RLock()

    @staticmethod
    def _keys(servers, prior):
        active = {s['id'] for s in servers}
        keys = {sid: key for sid, key in prior.items() if sid in active}
        occupied = set(keys.values())
        for server in servers:
            if server['id'] in keys: continue
            key = server['name'] or server['id']
            while key in occupied: key += '-' + server['id']
            keys[server['id']] = key
            occupied.add(key)
        return keys

    @staticmethod
    def _mirror(server, config, key, reset=False):
        candidate = copy.deepcopy(server)
        candidate.update(common_key=key, definition=safe_definition(config), credential_set=False,
                         transport='HTTP' if config.get('url') else 'stdio', supported=True,
                         endpoint=redact(config.get('url', config.get('command', ''))))
        oauth = config.get('oauth', {})
        if oauth:
            metadata = {k: copy.deepcopy(v) for k,v in oauth.items() if k in ('client_id','issuer','resource','token_endpoint','scope','expires_at','status','error')}
            metadata.setdefault('resource', config.get('url'))
            metadata.setdefault('status', 'authorized')
            headers = {k.lower():v for field in ('headers','http_headers') for k,v in config.get(field, {}).items()}
            if headers.get('authorization') != 'Bearer ' + oauth.get('access_token', ''):
                metadata['status'] = 'inactive'
            if not reset and server.get('oauth',{}).get('status') in ('pending','failed'):
                metadata.update(status=server['oauth']['status'],error=server['oauth'].get('error'))
            candidate['oauth'] = metadata
        elif not reset and server.get('oauth',{}).get('status') in ('pending','failed'):
            candidate['oauth'] = copy.deepcopy(server['oauth'])
        else: candidate.pop('oauth', None)
        if reset:
            candidate.update(status='not_checked', last_checked=None, duration_ms=None, tools=[], identity=None,
                             auth='Signed in' if candidate.get('oauth',{}).get('status') in ('authorized','imported') else 'Not validated' if config.get('url') else 'Local process',
                             diagnostic='Manager configuration saved. Check the connection to validate it.')
        return candidate

    @staticmethod
    def _tombstone(data, server, config):
        identities = {server.get('discovery_identity'), discovery_identity(config)}
        data.setdefault('deleted_servers', []).append({
            'id':server['id'], 'identities':sorted(i for i in identities if isinstance(i,str)),
            'sources':[{'client':v['client'],'name':v['name']} for v in server.get('sources',[])],
        })

    def _replace_data(self, data):
        servers = {s['id']:s for s in self.store.data['servers']}
        replacement = copy.deepcopy(data)
        for index, server in enumerate(replacement['servers']):
            if server['id'] in servers:
                existing = servers[server['id']]
                existing.clear(); existing.update(server)
                replacement['servers'][index] = existing
        self.store.data.clear(); self.store.data.update(replacement)

    def _commit(self, data, document, original):
        keys = data['common_config']['keys']
        data['common_config'] = {'keys':keys, 'hashes':{key:digest(value) for key,value in document['mcpServers'].items()}}
        proposed = (json.dumps(document, indent=2, ensure_ascii=False) + '\n').encode()
        before = copy.deepcopy(self.store.data)
        changed = data != before
        same_file = bool(original) and json.loads(original) == document
        # The canonical file must survive a process interruption before metadata
        # is saved. On restart its values can rebuild the non-secret mirror.
        if same_file:
            if self.path.is_symlink() or self.path.read_bytes() != original: raise ManagerError('Common MCP configuration changed during apply; original file preserved.')
            self.path.chmod(0o600)
        else: atomic_write(self.path, proposed, expected_original=original)
        try:
            if changed:
                self._replace_data(data)
                self.store.save()
        except Exception:
            self._replace_data(before)
            if not same_file:
                if original: atomic_write(self.path, original, expected_original=proposed)
                elif not self.path.is_symlink() and self.path.read_bytes() == proposed: self.path.unlink()
            raise

    def sync(self):
        with self.store.lock, self.lock, file_lock(self.path):
            return self._sync_locked()

    def _sync_locked(self):
        document, original = read_document(self.path)
        previous = self.store.data.get('common_config', {})
        keys = self._keys(self.store.data['servers'], previous.get('keys', {}))
        unknown = set(document['mcpServers']) - set(keys.values())
        if unknown: raise ManagerError('Unknown common MCP server key. Add servers through the manager; original file preserved.')
        staged = copy.deepcopy(self.store.data)
        changed = set()
        keep = []
        migrated = {'mcpServers': {}}
        for server in staged['servers']:
            sid = server['id']; key = keys[sid]
            config = document['mcpServers'].get(key)
            if config is None:
                if server.get('common_key') and key in previous.get('hashes', {}):
                    self._tombstone(staged, server, {})
                    changed.add(sid)
                    keys.pop(sid, None)
                    continue
                config = copy.deepcopy(server['definition'] if 'definition' in server else source_config(self.store, server))
            already_inline = key in document['mcpServers'] and 'credentials' not in document and not all_refs(config)
            if not already_inline and (not server.get('common_key') or 'credentials' in document):
                config = inline_legacy_config(self.store, server, config, document)
            validate(config)
            if all_refs(config): raise ManagerError('Enter credentials directly in each server entry; original common configuration preserved.')
            reset = key in previous.get('hashes', {}) and previous['hashes'][key] != digest(config)
            candidate = self._mirror(server, config, key, reset=reset)
            keep.append(candidate)
            migrated['mcpServers'][key] = config
            if reset: changed.add(sid)
        staged['servers'] = keep
        staged['common_config'] = {'keys':keys}
        self._commit(staged, migrated, original)
        return changed

    def save_server(self, sid, config, revision=None):
        validate(config)
        if all_refs(config): raise ManagerError('Enter credential values directly in each server entry.')
        with self.store.lock, self.lock, file_lock(self.path):
            self._sync_locked()
            server = self.store.server(sid)
            document, original = read_document(self.path)
            key = server['common_key']
            if revision is not None and digest(document['mcpServers'][key]) != revision:
                raise ManagerError('Configuration changed since it was loaded. Reset to reload it before saving.')
            document['mcpServers'][key] = copy.deepcopy(config)
            staged = copy.deepcopy(self.store.data)
            for index, record in enumerate(staged['servers']):
                if record['id'] == sid: staged['servers'][index] = self._mirror(record, config, key, reset=True)
            self._commit(staged, document, original)

    def save_oauth(self, sid, bundle):
        with self.store.lock, self.lock, file_lock(self.path):
            self._sync_locked()
            server = self.store.server(sid)
            document, _ = read_document(self.path)
            config = document['mcpServers'][server['common_key']]
            revision = digest(config)
            config['oauth'] = copy.deepcopy(bundle)
            for field in ('headers','http_headers','env_http_headers'):
                if field in config: config[field] = {k:v for k,v in config[field].items() if k.lower()!='authorization'}
            config.pop('bearer_token_env_var',None)
            config.setdefault('http_headers',config.pop('headers',{}))['Authorization'] = 'Bearer ' + bundle['access_token']
            self.save_server(sid, config, revision)

    def delete(self, sid):
        with self.store.lock, self.lock, file_lock(self.path):
            self._sync_locked()
            server = self.store.server(sid)
            document, original = read_document(self.path)
            staged = copy.deepcopy(self.store.data)
            staged['servers'] = [s for s in staged['servers'] if s['id'] != sid]
            key = staged['common_config']['keys'].pop(sid)
            self._tombstone(staged, server, document['mcpServers'].pop(key))
            self._commit(staged, document, original)
