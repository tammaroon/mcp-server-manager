import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from catalog import Store, ManagerError
from gateway import Gateway


class ServerConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / '.cursor/mcp.json'
        self.source.parent.mkdir()
        self.source.write_text(json.dumps({'mcpServers': {'jira': {
            'url': 'https://example.com/mcp',
            'headers': {'Authorization': 'Bearer original-secret'},
        }}}))
        self.original = self.source.read_bytes()
        self.store = Store(self.root / 'state', self.root)
        self.store.rescan()
        self.server = self.store.data['servers'][0]
        self.gateway = Gateway(self.store)

    def tearDown(self):
        self.gateway.close()
        self.tmp.cleanup()

    def editor(self):
        return self.gateway.control({'action': 'server_config', 'server_id': self.server['id']})

    def save(self, config, revision):
        return self.gateway.control({'action': 'save_server_config', 'server_id': self.server['id'], 'config_json': json.dumps(config), 'revision': revision})

    def document(self):
        return json.loads(self.gateway.common.path.read_text())

    def test_editor_loads_actual_common_entry_values(self):
        result = self.editor()
        doc = json.loads(result['config_json'])
        self.assertEqual(doc, {'url': 'https://example.com/mcp', 'headers': {'Authorization': 'Bearer original-secret'}})
        self.assertEqual(self.document()['mcpServers']['jira'], doc)
        self.assertNotIn('credentials', self.document())
        self.assertNotIn('original-secret', self.store.path.read_text())

    def test_save_preserves_ids_permissions_original_source_and_restart_resolution(self):
        view = self.editor()
        doc = json.loads(view['config_json'])
        doc['url'] = 'https://changed.example/mcp'
        self.store.update(self.server['id'], {'clients': ['claude'], 'tool_allowlists': {'claude': ['safe']}})
        self.save(doc, view['revision'])
        self.assertEqual(self.document()['mcpServers']['jira'], doc)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.store.rescan()
        result = self.store.server(self.server['id'])
        self.assertEqual(self.store.resolve(result), doc)
        self.assertEqual(result['endpoint'], 'https://changed.example/mcp')
        self.assertEqual(result['clients'], ['claude'])
        self.assertEqual(result['tool_allowlists'], {'claude': ['safe']})
        restarted = Store(self.store.root, self.root)
        self.assertEqual(restarted.resolve(restarted.server(self.server['id'])), doc)

    def test_replacement_secret_is_directly_in_entry_and_not_catalog_or_snapshot(self):
        view = self.editor()
        doc = json.loads(view['config_json'])
        doc['headers']['Authorization'] = 'Bearer replacement-secret'
        self.save(doc, view['revision'])
        self.assertEqual(self.document()['mcpServers']['jira']['headers']['Authorization'], 'Bearer replacement-secret')
        self.assertNotIn('credentials', self.document())
        self.assertNotIn('${credential:', self.gateway.common.path.read_text())
        self.assertNotIn('replacement-secret', self.store.path.read_text())
        self.assertNotIn('replacement-secret', json.dumps(self.gateway.snapshot()))
        self.assertEqual(json.loads(self.editor()['config_json'])['headers']['Authorization'], 'Bearer replacement-secret')
        restarted = Store(self.store.root, self.root)
        self.assertEqual(restarted.resolve(restarted.server(self.server['id']))['headers']['Authorization'], 'Bearer replacement-secret')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_catalog_baseline_keeps_hashes_without_duplicate_actual_config(self):
        self.editor()
        metadata = json.dumps(self.store.data.get('common_config', {}))
        self.assertNotIn('original-secret', self.store.path.read_text())
        self.assertNotIn('https://example.com/mcp', metadata)
        self.assertNotIn('Authorization', metadata)
        self.assertEqual(self.store.server(self.server['id'])['common_key'], 'jira')

    def test_invalid_schema_and_stale_edits_preserve_valid_configuration(self):
        view = self.editor()
        initial_catalog = self.store.path.read_bytes()
        initial_common = self.gateway.common.path.read_bytes()
        for doc in ([], {}, {'url': 'https://example.com', 'command': 'cat'}, {'command': 'cat', 'args': 'not-array'}, {'url': 'https://example.com', 'headers': {'Authorization': 4}}):
            with self.assertRaises(ManagerError):
                self.save(doc, view['revision'])
            self.assertEqual(self.store.path.read_bytes(), initial_catalog)
            self.assertEqual(self.gateway.common.path.read_bytes(), initial_common)
        doc = json.loads(view['config_json'])
        doc['url'] = 'https://other.example/mcp'
        self.save(doc, view['revision'])
        committed = self.gateway.common.path.read_bytes()
        with self.assertRaisesRegex(ManagerError, 'changed|stale'):
            self.save({'url': 'https://stale.example/mcp'}, view['revision'])
        self.assertEqual(self.gateway.common.path.read_bytes(), committed)

    def test_invalid_json_has_safe_location_error(self):
        view = self.editor()
        with self.assertRaisesRegex(ManagerError, 'line'):
            self.gateway.control({'action': 'save_server_config', 'server_id': self.server['id'], 'config_json': '{"url": "sensitive"', 'revision': view['revision']})

    def test_external_secret_edit_conflicts_with_open_editor(self):
        view = self.editor()
        doc = self.document()
        doc['mcpServers']['jira']['headers']['Authorization'] = 'Bearer externally-edited'
        self.gateway.common.path.write_text(json.dumps(doc))
        external = self.gateway.common.path.read_bytes()
        edited = json.loads(view['config_json'])
        edited['url'] = 'https://other.example/mcp'
        with self.assertRaisesRegex(ManagerError, 'changed|stale'):
            self.save(edited, view['revision'])
        self.assertEqual(self.gateway.common.path.read_bytes(), external)
        self.assertNotIn('externally-edited', self.store.path.read_text())

    def test_source_edits_do_not_change_imported_config_or_invalidate_its_editor(self):
        view = self.editor()
        self.source.write_bytes(self.original.replace(b'original-secret', b'changed-source-secret'))
        self.store.rescan()
        doc = json.loads(view['config_json'])
        doc['url'] = 'https://changed.example/mcp'
        self.save(doc, view['revision'])
        effective = self.store.resolve(self.store.server(self.server['id']))
        self.assertEqual(effective['headers']['Authorization'], 'Bearer original-secret')
        self.assertEqual(self.document()['mcpServers']['jira'], doc)

    def test_local_config_edit_is_used_by_gateway_and_clears_previous_health(self):
        view = self.editor()
        self.server.update(status='reachable', last_checked=123, tools=[{'name': 'old'}])
        fixture = str(Path(__file__).parent / 'fixtures/upstream.py')
        result = self.save({'command': sys.executable, 'args': [fixture]}, view['revision'])
        updated = next(s for s in result['servers'] if s['id'] == self.server['id'])
        self.assertEqual(updated['transport'], 'stdio')
        self.assertEqual(updated['status'], 'not_checked')
        self.assertEqual(updated['tools'], [])
        self.assertIsNone(updated['last_checked'])
        self.assertEqual(self.gateway.check(updated['id'])['status'], 'reachable')

    def test_managed_definition_survives_source_removal(self):
        view = self.editor()
        original_config = json.loads(view['config_json'])
        self.source.unlink()
        self.store.rescan()
        self.assertEqual(self.store.resolve(self.store.server(self.server['id'])), original_config)
        self.save({'command': '/bin/cat'}, view['revision'])
        self.store.rescan()
        self.assertEqual(self.store.resolve(self.store.server(self.server['id'])), {'command': '/bin/cat'})

    def test_edited_shared_entry_does_not_duplicate_after_sources_diverge(self):
        codex = self.root / '.codex/config.toml'
        codex.parent.mkdir()
        codex.write_text('[mcp_servers.jira]\nurl = "https://example.com/mcp"\n[mcp_servers.jira.http_headers]\nAuthorization = "Bearer original-secret"\n')
        self.store.rescan()
        self.server = self.store.data['servers'][0]
        view = self.editor()
        self.save({'command': '/bin/cat'}, view['revision'])
        self.source.write_bytes(self.original.replace(b'original-secret', b'source-changed'))
        for _ in range(2):
            self.store.rescan()
            self.assertEqual(len(self.store.data['servers']), 1)
            self.assertEqual(self.store.resolve(self.store.server(self.server['id'])), {'command': '/bin/cat'})

    def test_imported_headers_survive_equivalent_source_failover(self):
        codex = self.root / '.codex/config.toml'
        codex.parent.mkdir()
        codex.write_text('[mcp_servers.jira]\nurl = "https://example.com/mcp"\n[mcp_servers.jira.http_headers]\nAuthorization = "Bearer original-secret"\n')
        self.store.rescan()
        self.server = self.store.data['servers'][0]
        view = self.editor()
        expected = json.loads(view['config_json'])
        codex.unlink()
        self.store.rescan()
        self.assertEqual(self.store.resolve(self.store.server(self.server['id'])), expected)

    def test_header_arguments_and_environment_are_direct_values_only_in_common_file(self):
        config = {'command': '/usr/bin/true', 'args': ['--header', 'X-Custom: original-opaque-value', '-H', 'Other: second-opaque-value', '--header=Inline: third-opaque-value'], 'env': {'API_TOKEN': 'actual-env-secret'}}
        self.gateway.close()
        self.source.write_text(json.dumps({'mcpServers': {'jira': config}}))
        self.store = Store(self.root / 'header-argument-state', self.root)
        self.store.rescan()
        self.server = self.store.data['servers'][0]
        self.gateway = Gateway(self.store)
        view = self.editor()
        self.assertEqual(json.loads(view['config_json']), config)
        self.assertEqual(self.document()['mcpServers']['jira'], config)
        for value in ('original-opaque-value', 'second-opaque-value', 'third-opaque-value', 'actual-env-secret'):
            self.assertNotIn(value, self.store.path.read_text())
        edited = json.loads(view['config_json'])
        edited['args'][1] = 'X-Custom: replacement-opaque-value'
        edited['env']['API_TOKEN'] = 'replacement-env-secret'
        self.save(edited, view['revision'])
        self.assertEqual(self.document()['mcpServers']['jira'], edited)
        self.assertEqual(self.store.resolve(self.store.server(self.server['id'])), edited)
        self.assertNotIn('replacement-opaque-value', self.store.path.read_text())
        self.assertNotIn('replacement-env-secret', self.store.path.read_text())

    def test_failed_catalog_commit_rolls_back_editor_save_and_keeps_original_file(self):
        view = self.editor()
        catalog = self.store.path.read_bytes()
        common = self.gateway.common.path.read_bytes()
        edited = json.loads(view['config_json'])
        edited['headers']['Authorization'] = 'Bearer replacement-secret'
        with patch.object(self.store, 'save', side_effect=OSError('fixture catalog save failure')):
            with self.assertRaises((ManagerError, OSError)):
                self.save(edited, view['revision'])
        self.assertEqual(self.gateway.common.path.read_bytes(), common)
        self.assertEqual(self.store.path.read_bytes(), catalog)
        self.assertEqual(self.store.resolve(self.store.server(self.server['id']))['headers']['Authorization'], 'Bearer original-secret')
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
