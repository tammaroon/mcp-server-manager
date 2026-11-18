"""Private, bounded JSON event logs shared safely by daemon and bridge processes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from catalog import CLIENTS, redact
from credentials import file_lock

CHOICES = {
    'mode': {'daemon', 'bridge', 'ensure', 'control'},
    'client': set(CLIENTS),
    'method': {'initialize', 'ping', 'tools/list', 'tools/call'},
    'action': {'snapshot', 'common_config', 'oauth_start', 'server_config', 'save_server_config',
               'delete', 'discover', 'check', 'update', 'restart', 'preview', 'apply', 'settings', 'add'},
    'transport': {'stdio', 'HTTP'},
    'status': {'ok', 'error', 'reachable', 'failed', 'auth_required', 'unsupported', 'disabled', 'pending', 'authorized'},
}
TEXT_FIELDS = {'server_id', 'bridge_id', 'tool', 'socket', 'filename'}
NUMBER_FIELDS = {'count', 'duration_ms', 'errno', 'error_code'}


class EventLog:
    def __init__(self, state, max_bytes=2 * 1024 * 1024, backups=3):
        self.path = Path(state) / 'logs/manager.log'
        self.max_bytes, self.backups = max_bytes, backups

    def write(self, event, *, level='info', error=None, **fields):
        """Accept metadata only. Never format exception messages, locals or MCP payloads."""
        try:
            record = {'time': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                      'pid': os.getpid(), 'level': level if level in ('debug', 'info', 'warning', 'error') else 'info',
                      'event': event if re.fullmatch(r'[a-z_]{1,64}', event) else 'unknown_event'}
            for key, value in fields.items():
                if key in CHOICES:
                    record[key] = value if isinstance(value, str) and value in CHOICES[key] else 'unknown'
                elif key in TEXT_FIELDS and isinstance(value, str): record[key] = redact(value)[:1024]
                elif key in NUMBER_FIELDS and type(value) in (int, float): record[key] = value
            if error is not None:
                record['error_type'] = type(error).__name__
                if isinstance(error, OSError):
                    if isinstance(error.errno, int): record['errno'] = error.errno
                    if isinstance(error.filename, str) and '://' not in error.filename:
                        record['filename'] = redact(error.filename)[:1024]
                if type(getattr(error, 'code', None)) is int: record['error_code'] = error.code
                causes, seen = [], {id(error)}
                cause = error.__cause__ or error.__context__
                while cause is not None and id(cause) not in seen and len(causes) < 4:
                    seen.add(id(cause))
                    details = {'error_type': type(cause).__name__}
                    if type(getattr(cause, 'errno', None)) is int: details['errno'] = cause.errno
                    if type(getattr(cause, 'code', None)) is int: details['error_code'] = cause.code
                    causes.append(details)
                    cause = cause.__cause__ or cause.__context__
                if causes: record['causes'] = causes
                frames, tb = [], error.__traceback__
                while tb is not None:
                    code = tb.tb_frame.f_code
                    frames.append({'file': Path(code.co_filename).name, 'line': tb.tb_lineno, 'function': code.co_name})
                    tb = tb.tb_next
                record['frames'] = frames[-8:]
            data = (json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n').encode()
            if len(data) > self.max_bytes: return False
            directory = self.path.parent
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or directory.stat().st_uid != os.getuid(): return False
            directory.chmod(0o700)
            with file_lock(self.path):
                if self.path.is_symlink(): return False
                if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                    for number in range(self.backups, 0, -1):
                        source = self.path if number == 1 else self.path.with_name(self.path.name + f'.{number - 1}')
                        target = self.path.with_name(self.path.name + f'.{number}')
                        if source.exists(): os.replace(source, target)
                    if not self.backups: self.path.unlink()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'ab') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
            return True
        except Exception:
            # Logging failures must never corrupt protocol stdout or fail an MCP call.
            return False
