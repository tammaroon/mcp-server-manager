"""Readable, persistent tool aliases; upstream identities stay internal."""
from __future__ import annotations
import copy
import re
from catalog import ManagerError

MAX_NAME = 64
MAX_SERVER_ALIAS = 24
VALID = re.compile(r'^[A-Za-z0-9_-]+$')


def server_slug(name):
    # Reverse-DNS package names often finish with a useful path component.
    slug = re.sub(r'[^a-z0-9]+', '_', name.rsplit('/', 1)[-1].lower()).strip('_')
    slug = re.sub(r'^mcp_|_mcp$', '', slug)
    return slug or 'server'


def unique(base, limit, occupied):
    base = base[:limit].rstrip('_-') or 'tool'
    candidate = base
    number = 2
    while candidate in occupied:
        suffix = '_' + str(number)
        candidate = base[:limit-len(suffix)].rstrip('_-') + suffix
        number += 1
    return candidate


class ToolNames:
    def __init__(self, store):
        self.store = store

    def _state(self):
        state = self.store.data.get('tool_names', {'servers': {}, 'tools': {}})
        if not isinstance(state, dict) or not isinstance(state.get('servers'), dict) or not isinstance(state.get('tools'), dict):
            raise ManagerError('Stored tool aliases are invalid.')
        aliases = list(state['servers'].values())
        if any(not isinstance(v, str) or not VALID.fullmatch(v) or len(v) > MAX_SERVER_ALIAS for v in aliases) or len(aliases) != len(set(aliases)):
            raise ManagerError('Stored server aliases are invalid.')
        for sid, tools in state['tools'].items():
            if sid not in state['servers'] or not isinstance(tools, dict): raise ManagerError('Stored tool aliases are invalid.')
            values = list(tools.values())
            budget = MAX_NAME - len(state['servers'][sid]) - 2
            if any(not isinstance(k,str) or not k for k in tools) or any(not isinstance(v,str) or not VALID.fullmatch(v) or len(v)>budget for v in values) or len(values)!=len(set(values)):
                raise ManagerError('Stored tool aliases are invalid.')
        return state

    def register(self, server, originals):
        """Reserve all upstream names before applying a client's tool filters."""
        if any(not isinstance(name, str) or not name for name in originals): raise ManagerError('Upstream advertised an invalid tool name.')
        with self.store.lock:
            current = self._state()
            staged = copy.deepcopy(current)
            if not any(s['id'] == server['id'] for s in self.store.data['servers']): raise ManagerError('Unknown server.')
            occupied = set(staged['servers'].values())
            # Allocate every known namespace independently of permission/list order.
            for record in sorted(self.store.data['servers'], key=lambda s: (server_slug(s['name']), s['id'])):
                sid = record['id']
                if sid not in staged['servers']:
                    alias = unique(server_slug(record['name']), MAX_SERVER_ALIAS, occupied)
                    staged['servers'][sid] = alias
                    occupied.add(alias)
            sid = server['id']
            alias = staged['servers'][sid]
            tools = staged['tools'].setdefault(sid, {})
            occupied = set(tools.values())
            for original in sorted(set(originals)):
                if original not in tools:
                    base = re.sub(r'[^A-Za-z0-9_-]+', '_', original).strip('_') or 'tool'
                    tool_alias = unique(base, MAX_NAME-len(alias)-2, occupied)
                    tools[original] = tool_alias
                    occupied.add(tool_alias)
            if staged != current or 'tool_names' not in self.store.data:
                previous = self.store.data.get('tool_names')
                self.store.data['tool_names'] = staged
                try: self.store.save()
                except Exception:
                    if previous is None: self.store.data.pop('tool_names', None)
                    else: self.store.data['tool_names'] = previous
                    raise
            return {original: alias + '__' + tools[original] for original in originals}

    def resolve(self, exposed):
        with self.store.lock:
            state = self._state()
            for sid, alias in state['servers'].items():
                prefix = alias + '__'
                if not exposed.startswith(prefix): continue
                suffix = exposed[len(prefix):]
                for original, tool_alias in state['tools'].get(sid, {}).items():
                    if suffix == tool_alias: return sid, original
        return None
