"""Editable manager-owned MCP entries with opaque source/file credential references."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re
from catalog import ManagerError, SENSITIVE, definitions, redact
from transport import HTTPSession

REFERENCE = re.compile(r'^\$\{(source|credential|common):(.+)\}$')
ENV_REFERENCE = re.compile(r'^\$\{env:[A-Za-z_][A-Za-z0-9_]*\}$')
IGNORED = {'enabled', 'disabled', 'gallery', 'version'}
FIELDS = {'type', 'url', 'command', 'args', 'cwd', 'env', 'env_vars', 'headers', 'http_headers', 'env_http_headers', 'bearer_token_env_var', 'startup_timeout_sec', 'tool_timeout_sec', 'oauth', 'start_on_launch'}


def pointer(path):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in path)


def source_config(store, server):
    if server['source_client'] == 'manager': return {}
    config = definitions(store.home, server['source_client']).get(server['source_name'], {})
    return config if isinstance(config, dict) else {}


def raw_config(store, server):
    if server.get('common_key'):
        from credentials import read_document
        document, _ = read_document(store.root / 'mcp.json')
        config = document['mcpServers'].get(server['common_key'])
        if not isinstance(config, dict): raise ManagerError('The server entry is missing from the common MCP configuration.')
    else:
        config = server['definition'] if 'definition' in server else source_config(store, server)
    return copy.deepcopy({k: v for k, v in config.items() if k not in IGNORED})


def sensitive(path, value, root):
    if path and path[0] == 'env_http_headers': return False
    if any(p in ('headers', 'http_headers', 'env') for p in path): return True
    if path and SENSITIVE.search(str(path[-1])) and path[-1] != 'bearer_token_env_var': return True
    if len(path) == 2 and path[0] == 'args' and isinstance(path[1], int):
        if isinstance(value, str) and (value.startswith(('--header=', '--headers=', '--http-header=')) or value.startswith('-H') and len(value) > 2): return True
        if path[1] > 0:
            flag = str(root.get('args', [])[path[1] - 1])
            if flag in ('--header', '--headers', '--http-header', '-H') or SENSITIVE.search(flag): return True
    return isinstance(value, str) and redact(value) != value


def map_values(value, visit, path=()):
    if isinstance(value, dict): return {k: map_values(v, visit, (*path, k)) for k, v in value.items()}
    if isinstance(value, list): return [map_values(v, visit, (*path, i)) for i, v in enumerate(value)]
    return visit(value, path)


def all_refs(value):
    refs = set()
    def visit(v, _):
        if isinstance(v, str) and REFERENCE.fullmatch(v): refs.add(v)
        return v
    map_values(value, visit)
    return refs


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_definition(config):
    """Keep only opaque pointers in the non-secret catalog mirror."""
    def hide(value, path):
        if isinstance(value, str) and not ENV_REFERENCE.fullmatch(value) and sensitive(path, value, config):
            return '${common:' + pointer(path) + '}'
        return value
    result = map_values(config, hide)
    if 'oauth' in result: result['oauth'] = '${common:/oauth}'
    return result


def inline_legacy_config(store, server, config, document):
    """Resolve the old file/source references once, before removing their section."""
    original = copy.deepcopy(config)
    source = source_config(store, server) if any(r.startswith('${source:') for r in all_refs(config)) else {}
    credentials = document.get('credentials', {})
    def value_at(root, name):
        result = root
        for part in name.lstrip('/').split('/'):
            key = part.replace('~1', '/').replace('~0', '~')
            if result is root and key in ('headers', 'http_headers') and key not in result:
                key = 'headers' if key == 'http_headers' else 'http_headers'
            result = result[int(key)] if isinstance(result, list) else result[key]
        return result
    def materialize(value, path):
        match = REFERENCE.fullmatch(value) if isinstance(value, str) else None
        if not match: return value
        kind, name = match.groups()
        try:
            if kind == 'source': result = value_at(source, name)
            elif kind == 'common': result = value_at(config, name)
            elif name.startswith('oauth-access:'):
                result = json.loads(credentials['oauth:' + name[len('oauth-access:'):]])['access_token']
            else: result = credentials[name]
            if not isinstance(result, str): raise ValueError()
            if kind == 'credential' and name in (server['id'], 'oauth-access:' + server['id']) and path and str(path[-1]).lower() == 'authorization' and not result.lower().startswith(('bearer ', 'basic ')):
                result = 'Bearer ' + result
            return result
        except (KeyError, ValueError, TypeError, IndexError):
            raise ManagerError('A previous credential reference is unavailable; original common configuration preserved.') from None
    header = server.get('credential_header', 'Authorization') if server.get('credential_set') else None
    config = copy.deepcopy(config)
    if header:
        for field in ('headers', 'http_headers', 'env_http_headers'):
            if field in config: config[field] = {k:v for k,v in config[field].items() if k.lower() != header.lower()}
        if header.lower() == 'authorization': config.pop('bearer_token_env_var', None)
    result = map_values(config, materialize)
    if server.get('credential_set'):
        header = server.get('credential_header', 'Authorization')
        try: secret = credentials[server['id']]
        except KeyError: raise ManagerError('A previous credential is unavailable; original common configuration preserved.') from None
        for field in ('headers', 'http_headers', 'env_http_headers'):
            if field in result: result[field] = {k: v for k, v in result[field].items() if k.lower() != header.lower()}
        if header.lower() == 'authorization':
            result.pop('bearer_token_env_var', None)
            if not secret.lower().startswith(('bearer ', 'basic ')): secret = 'Bearer ' + secret
        result.setdefault('http_headers', result.pop('headers', {}))[header] = secret
    oauth_reference = '${credential:oauth-access:' + server['id'] + '}'
    oauth_account = 'oauth:' + server['id']
    manual_override = bool(header and header.lower() == 'authorization')
    if oauth_account in credentials:
        try: bundle = json.loads(credentials[oauth_account])
        except (ValueError, TypeError): raise ManagerError('The previous OAuth login is unavailable; original common configuration preserved.') from None
        if not isinstance(bundle,dict): raise ManagerError('The previous OAuth login is invalid; original common configuration preserved.')
        result['oauth'] = {**server.get('oauth', {}), **bundle, **result.get('oauth', {})}
        if manual_override or oauth_reference not in all_refs(original): result['oauth']['status'] = 'inactive'
    return {k: v for k, v in result.items() if k not in IGNORED}


def editor_config(store, server):
    if not server.get('common_key'):
        from common_config import CommonConfig
        CommonConfig(store).sync()
        server = store.server(server['id'])
    config = raw_config(store, server)
    return {'config_json': json.dumps(config, indent=2, ensure_ascii=False), 'revision': digest(config)}


def validate(config):
    if not isinstance(config, dict): raise ManagerError('Enter one MCP server configuration as a JSON object.')
    if set(config) - FIELDS: raise ManagerError('Unsupported MCP configuration field. Use url or command, args, cwd, env, headers, or environment references.')
    if 'start_on_launch' in config and not isinstance(config['start_on_launch'], bool): raise ManagerError('start_on_launch must be boolean.')
    if 'oauth' in config and not isinstance(config['oauth'], dict): raise ManagerError('oauth must be a JSON object.')
    if 'oauth' in config:
        for key in ('access_token', 'refresh_token', 'client_id', 'resource', 'token_endpoint', 'issuer', 'scope', 'status', 'error'):
            if key not in config['oauth']: continue
            value = config['oauth'][key]
            if value is None and key in ('refresh_token', 'error'): continue
            if not isinstance(value, str) or key == 'access_token' and not value: raise ManagerError('OAuth ' + key + ' must be a string.')
        if 'expires_at' in config['oauth']:
            value=config['oauth']['expires_at']
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value): raise ManagerError('OAuth expires_at must be a finite number.')
    if bool(config.get('url')) == bool(config.get('command')): raise ManagerError('Specify exactly one of url or command.')
    if config.get('type') not in (None, 'stdio', 'http', 'streamable-http'): raise ManagerError('Supported transport types are stdio and http.')
    for field in ('url', 'command', 'cwd', 'bearer_token_env_var'):
        if field in config and (not isinstance(config[field], str) or not config[field].strip()): raise ManagerError(field + ' must be a nonempty string.')
    for field in ('args', 'env_vars'):
        if field in config and (not isinstance(config[field], list) or any(not isinstance(v, str) for v in config[field])): raise ManagerError(field + ' must be an array of strings.')
    for field in ('headers', 'http_headers', 'env_http_headers', 'env'):
        if field in config and (not isinstance(config[field], dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k,v in config[field].items())): raise ManagerError(field + ' must map names to string values.')
    for field in ('headers', 'http_headers', 'env_http_headers'):
        if any(not re.fullmatch(r'[A-Za-z0-9!#$%&\'*+.^_`|~-]+', k) or '\n' in v or '\r' in v for k,v in config.get(field, {}).items()): raise ManagerError('Invalid HTTP header name or value.')
    for field in ('startup_timeout_sec', 'tool_timeout_sec'):
        if field in config and (isinstance(config[field], bool) or not isinstance(config[field], (int, float)) or not 0 < config[field] <= 600): raise ManagerError(field + ' must be between 0 and 600.')
    if config.get('url') and not REFERENCE.fullmatch(config['url']) and not ENV_REFERENCE.fullmatch(config['url']): HTTPSession({'url': config['url']}).close()
    if config.get('url') and config.get('type') == 'stdio' or config.get('command') and config.get('type') in ('http', 'streamable-http'): raise ManagerError('Transport type does not match the url or command.')


def save_editor_config(store, server, text, revision, write_secret=None, delete_secret=None):
    if not isinstance(text, str) or len(text.encode()) > 512 * 1024: raise ManagerError('Configuration must be JSON text under 512 KB.')
    try: config = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except json.JSONDecodeError as e: raise ManagerError(f'Invalid JSON at line {e.lineno}, column {e.colno}.') from None
    except ValueError: raise ManagerError('JSON numbers must be finite.') from None
    validate(config)
    if all_refs(config): raise ManagerError('Enter credential values directly in this server entry; credential references are no longer supported.')
    from common_config import CommonConfig
    CommonConfig(store).save_server(server['id'], config, revision)


def resolve_config(store, server, read_secret=None):
    config = raw_config(store, server)
    if all_refs(config):
        from credentials import read_document
        document, _ = read_document(store.root / 'mcp.json')
        config = inline_legacy_config(store, server, config, document)
    config.pop('oauth', None)
    config.pop('start_on_launch', None)
    return config
