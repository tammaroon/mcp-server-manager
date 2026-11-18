import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from catalog import Store, ManagerError
from gateway import Gateway

ROOT = Path(__file__).resolve().parents[1]


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        source = self.root / '.cursor/mcp.json'
        source.parent.mkdir()
        self.secret = 'opaque-value-never-for-log-output'
        source.write_text(json.dumps({'mcpServers': {'fixture': {
            'command': sys.executable, 'args': [str(ROOT / 'tests/fixtures/upstream.py')],
            'env': {'API_TOKEN': self.secret},
        }}}))
        self.store = Store(self.state, self.root)
        self.store.rescan()
        self.gateway = Gateway(self.store)
        self.server = self.store.data['servers'][0]
        self.path = self.state / 'logs/manager.log'

    def tearDown(self):
        self.gateway.close()
        self.tmp.cleanup()

    def records(self):
        self.assertTrue(self.path.exists(), 'Gateway events must be persisted')
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_health_checks_and_tool_calls_log_metadata_without_payloads(self):
        self.gateway.check(self.server['id'])
        self.gateway.handle('cursor', {'id': 1, 'method': 'tools/list'})
        result = self.gateway.handle('cursor', {'id': self.secret, 'method': 'tools/call', 'params': {
            'name': 'fixture__echo', 'arguments': {'text': self.secret},
        }})
        self.assertEqual(result['result']['content'][0]['text'], self.secret)
        events = self.records()
        check = next(e for e in events if e['event'] == 'check_finished')
        self.assertEqual(check['status'], 'reachable')
        self.assertEqual(check['server_id'], self.server['id'])
        call = next(e for e in events if e['event'] == 'mcp_request' and e['method'] == 'tools/call')
        self.assertEqual(call['client'], 'cursor')
        self.assertEqual(call['status'], 'ok')
        self.assertGreaterEqual(call['duration_ms'], 0)
        self.assertNotIn(self.secret, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_unexpected_error_preserves_stack_location_without_error_text(self):
        with patch('gateway.StdioSession', side_effect=RuntimeError(self.secret)):
            self.gateway.check(self.server['id'])
        failed = next(e for e in self.records() if e['event'] == 'check_failed')
        self.assertEqual(failed['error_type'], 'RuntimeError')
        self.assertTrue(failed['frames'])
        self.assertNotIn(self.secret, self.path.read_text())

    def test_failed_tool_result_is_logged_as_an_error_without_its_content(self):
        self.gateway.handle('cursor', {'id': 1, 'method': 'tools/list'})
        session = self.gateway.sessions[self.server['id']]['session']
        failed = {'isError': True, 'content': [{'type': 'text', 'text': self.secret}]}
        with patch.object(session, 'request', return_value=failed):
            result = self.gateway.handle('cursor', {'id': 2, 'method': 'tools/call', 'params': {'name': 'fixture__echo', 'arguments': {}}})
        self.assertTrue(result['result']['isError'])
        call = next(e for e in self.records() if e['event'] == 'mcp_request' and e['method'] == 'tools/call')
        self.assertEqual(call['status'], 'error')
        self.assertNotIn(self.secret, self.path.read_text())

    def test_wrapped_errors_keep_cause_types_and_codes_without_messages(self):
        from urllib.error import HTTPError
        try:
            try:
                raise HTTPError('https://example.test/' + self.secret, 401, self.secret, {}, None)
            except HTTPError as cause:
                cause.close()
                raise ManagerError(self.secret) from None
        except ManagerError as error:
            self.assertTrue(hasattr(self.gateway, 'log'), 'Gateway needs a persistent event log')
            self.gateway.log.write('wrapped_failure', level='error', error=error)
        failed = next(e for e in self.records() if e['event'] == 'wrapped_failure')
        self.assertEqual(failed.get('causes'), [{'error_type': 'HTTPError', 'error_code': 401}])
        self.assertNotIn(self.secret, self.path.read_text())

    def test_cli_records_missing_socket_without_polluting_json_stdout(self):
        state = self.root / 'offline'
        result = subprocess.run([sys.executable, str(ROOT / 'backend/manager.py'), '--state', str(state), 'control'],
                                input='{"action":"snapshot"}', capture_output=True, text=True, timeout=5)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertFalse(json.loads(result.stdout)['ok'])
        path = state / 'logs/manager.log'
        self.assertTrue(path.exists(), 'CLI startup failures must be persisted')
        events = [json.loads(line) for line in path.read_text().splitlines()]
        error = next(e for e in events if e['event'] == 'rpc_failed')
        self.assertEqual(error['error_type'], 'FileNotFoundError')
        self.assertEqual(error['errno'], 2)
        self.assertTrue(error['socket'].endswith('gateway.sock'))
        self.assertEqual(result.stderr, '')

    def test_rotation_keeps_bounded_valid_records_and_the_latest_event(self):
        self.assertTrue(hasattr(self.gateway, 'log'), 'Gateway needs a persistent event log')
        log = type(self.gateway.log)(self.root / 'rotating', max_bytes=512, backups=2)
        for number in range(40):
            self.assertTrue(log.write('rotation_test', count=number))
        files = list(log.path.parent.glob('manager.log*'))
        files = [p for p in files if p.suffix != '.lock']
        self.assertLessEqual(len(files), 3)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 512)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines()))
        self.assertEqual(json.loads(log.path.read_text().splitlines()[-1])['count'], 39)

    def test_concurrent_processes_append_complete_records_without_losing_events(self):
        self.assertTrue(hasattr(self.gateway, 'log'), 'Gateway needs a persistent event log')
        state = self.root / 'parallel'
        code = "from event_log import EventLog; import sys; log=EventLog(sys.argv[1]); [log.write('parallel_test', count=n) for n in range(40)]"
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend')}
        workers = [subprocess.Popen([sys.executable, '-c', code, str(state)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, stderr)
            self.assertEqual(stdout, b'')
        events = [json.loads(line) for line in (state / 'logs/manager.log').read_text().splitlines()]
        self.assertEqual(len(events), 160)
        self.assertEqual(len({e['pid'] for e in events}), 4)

    def test_logging_failure_does_not_break_a_healthy_mcp_check(self):
        self.assertTrue(hasattr(self.gateway, 'log'), 'Gateway needs a persistent event log')
        bad = self.root / 'unwritable'
        bad.mkdir()
        (bad / 'logs').write_text('a file blocks log directory creation')
        self.gateway.log = type(self.gateway.log)(bad)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.gateway.check(self.server['id'])['status'], 'reachable')
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')

    def test_arbitrary_fields_and_unknown_protocol_methods_cannot_leak_payloads(self):
        self.assertTrue(hasattr(self.gateway, 'log'), 'Gateway needs a persistent event log')
        self.gateway.log.write('privacy_test', headers={'Authorization': self.secret}, arguments=self.secret, result=self.secret)
        self.gateway.handle('cursor', {'id': 1, 'method': self.secret})
        self.assertNotIn(self.secret, self.path.read_text())
