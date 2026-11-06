"""Pure, fail-closed rewriting of Codex TOML for a manager-only MCP setup.

No files or credentials are accessed here. Callers own backup, review, atomic
publication, and ensuring that bridge command arguments do not contain secrets.
"""
from __future__ import annotations

import difflib
import json
import math
import tomllib


def _parse(raw: bytes) -> tuple[str, dict]:
    try:
        text = raw.decode('utf-8')
        return text, tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError):
        # TOML exception messages can include credential-bearing input.
        raise ValueError('Invalid UTF-8 TOML configuration; original preserved.') from None


def _statements(text: str):
    """Yield complete TOML statements, preserving every character.

    Parsing remains tomllib's job. This scanner only finds newlines outside
    strings, arrays, and inline tables, so apparent headers in multiline values
    cannot change the section being removed.
    """
    start = i = depth = 0
    while i < len(text):
        char = text[i]
        if char in "\"'":
            quote = char
            multiline = text.startswith(quote * 3, i)
            i += 3 if multiline else 1
            while i < len(text):
                if quote == '"' and text[i] == '\\':
                    i += 2
                    continue
                if text[i] == quote:
                    if not multiline:
                        i += 1
                        break
                    end = i
                    while end < len(text) and text[end] == quote:
                        end += 1
                    if end - i >= 3:
                        i = end
                        break
                    i = end
                    continue
                i += 1
            continue
        if char == '#':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
            continue
        if char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        elif char == '\n' and depth == 0:
            yield text[start:i + 1]
            start = i + 1
        i += 1
    if start < len(text):
        yield text[start:]


def _without_mcp(document: dict) -> dict:
    return {key: value for key, value in document.items() if key != 'mcp_servers'}


def _same_value(left, right) -> bool:
    # A valid unrelated setting may contain TOML nan, which isn't equal to itself.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and math.isnan(left):
        return math.isnan(right)
    return left == right


def _bridge_definition(bridge_command: list[str]) -> dict:
    if (not isinstance(bridge_command, list) or not bridge_command
            or any(not isinstance(value, str) or '\x00' in value for value in bridge_command)
            or not bridge_command[0].strip()):
        raise ValueError('Bridge command must be a nonempty list of strings with a valid executable.')
    return {'command': bridge_command[0], 'args': bridge_command[1:],
            'startup_timeout_sec': 60, 'tool_timeout_sec': 120}


def manager_only_config(original: bytes, bridge_command: list[str]) -> bytes:
    """Replace the entire root MCP tree with exactly one manager bridge.

    Non-MCP statements retain their original UTF-8 bytes and line endings.
    Root inline/dotted assignments, quoted table keys, and nested MCP tables are
    supported. Unsupported syntax fails closed and never returns partial output.
    ValueError messages deliberately exclude source text and credential values.
    """
    definition = _bridge_definition(bridge_command)
    text, parsed = _parse(original)
    if 'mcp_servers' in parsed and not isinstance(parsed['mcp_servers'], dict):
        raise ValueError('Root mcp_servers must be a TOML table; original preserved.')
    kept = []
    in_mcp = False
    in_table = False
    try:
        for statement in _statements(text):
            meaningful = statement.lstrip(' \t\r\n')
            if meaningful.startswith('['):
                header = tomllib.loads(statement)
                in_mcp = next(iter(header)) == 'mcp_servers'
                in_table = True
            elif not in_table and meaningful and not meaningful.startswith('#'):
                # A standalone assignment reveals the decoded first dotted key,
                # including quoted/escaped keys and root inline-table syntax.
                assignment = tomllib.loads(statement)
                if 'mcp_servers' in assignment:
                    continue
            if not in_mcp:
                kept.append(statement)
        retained = ''.join(kept)
        if not _same_value(tomllib.loads(retained), _without_mcp(parsed)):
            raise ValueError('Cannot safely preserve all non-MCP settings; original preserved.')
        newline = '\r\n' if '\r\n' in text else '\n'
        if retained and not retained.endswith('\n'):
            retained += newline
        if retained and not retained.endswith(newline * 2):
            retained += newline
        # JSON basic strings/arrays are valid TOML for these string-only values.
        section = newline.join([
            '[mcp_servers.mcp-manager]',
            'command = ' + json.dumps(definition['command'], ensure_ascii=False),
            'args = ' + json.dumps(definition['args'], ensure_ascii=False),
            'startup_timeout_sec = 60',
            'tool_timeout_sec = 120',
            '',
        ])
        output = (retained + section).encode('utf-8')
        _, rewritten = _parse(output)
        if (rewritten.get('mcp_servers') != {'mcp-manager': definition}
                or not _same_value(_without_mcp(rewritten), _without_mcp(parsed))):
            raise ValueError('Cannot safely produce a manager-only configuration; original preserved.')
        return output
    except (tomllib.TOMLDecodeError, StopIteration, IndexError, UnicodeError):
        raise ValueError('Unsupported TOML configuration format; original preserved.') from None


def reviewed_change(original: bytes, proposed: bytes) -> str:
    """Return a unified structural diff that contains no source values/comments.

    Unrelated configuration is omitted entirely. Existing server names, command
    arguments, URLs, comments, and all values are hidden, including proposed
    bridge paths. The proposed file must preserve other settings and contain
    exactly the expected manager bridge before it can be previewed.
    """
    _, before = _parse(original)
    _, after = _parse(proposed)
    servers = before.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('Root mcp_servers must be a TOML table; original preserved.')
    expected = after.get('mcp_servers')
    if not isinstance(expected, dict) or set(expected) != {'mcp-manager'}:
        raise ValueError('Preview requires exactly one manager MCP server.')
    manager = expected['mcp-manager']
    if (not isinstance(manager, dict) or not isinstance(manager.get('args'), list)
            or manager != _bridge_definition([manager.get('command'), *manager['args']])
            or not _same_value(_without_mcp(before), _without_mcp(after))):
        raise ValueError('Preview requires a valid manager bridge and unchanged non-MCP settings.')
    old = [f'# {len(servers)} existing MCP entries; names and values redacted\n']
    new = [
        '[mcp_servers.mcp-manager]\n',
        'command = "<redacted>"\n',
        'args = ["<redacted>"]\n',
        'startup_timeout_sec = 60\n',
        'tool_timeout_sec = 120\n',
    ]
    return ''.join(difflib.unified_diff(old, new, fromfile='Codex MCP (redacted)',
                                        tofile='Codex manager bridge (redacted)'))
