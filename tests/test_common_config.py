import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from catalog import ManagerError, Store
from common_config import CommonConfig
from server_config import editor_config, save_editor_config


class CommonConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / '.cursor/mcp.json'
        self.source.parent.mkdir()
        self.source.write_text(json.dumps({'mcpServers': {
            'jira': {'url': 'https://example.com/mcp', 'headers': {'Authorization': 'Bearer original-secret'}},
            'local': {'command': '/bin/cat', 'env': {'API_TOKEN': 'local-secret'}},
        }}))
        self.original = self.source.read_bytes()
        self.store = Store(self.root / 'state', self.root)
        self.store.rescan()
        self.common = CommonConfig(self.store)
        self.server = next(s for s in self.store.data['servers'] if s['name'] == 'jira')

    def tearDown(self):
        self.tmp.cleanup()

    def document(self):
        return json.loads(self.common.path.read_text())

    def external(self, doc):
        self.common.path.write_text(json.dumps(doc))

    def test_generates_private_common_file_with_actual_values_in_each_entry(self):
        self.common.sync()
        self.assertEqual(self.common.path, self.store.root / 'mcp.json')
        self.assertEqual(self.document(), {'mcpServers': {
            'jira': {'url': 'https://example.com/mcp', 'headers': {'Authorization': 'Bearer original-secret'}},
            'local': {'command': '/bin/cat', 'env': {'API_TOKEN': 'local-secret'}},
        }})
        self.assertEqual(stat.S_IMODE(self.common.path.stat().st_mode), 0o600)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertNotIn('original-secret', self.store.path.read_text())
        self.assertNotIn('local-secret', self.store.path.read_text())

    def test_catalog_persists_common_ownership_and_only_hashes_for_baseline(self):
        self.common.sync()
        self.assertEqual(self.store.server(self.server['id'])['common_key'], 'jira')
        baseline = json.dumps(self.store.data.get('common_config', {}))
        for value in ('original-secret', 'local-secret', 'https://example.com/mcp', '/bin/cat'):
            self.assertNotIn(value, baseline)
        restarted = Store(self.store.root, self.root)
        self.assertEqual(restarted.resolve(restarted.server(self.server['id']))['headers']['Authorization'], 'Bearer original-secret')

    def test_external_edit_preserves_permissions_ids_and_original_source(self):
        self.common.sync()
        self.store.update(self.server['id'], {'clients': ['claude'], 'tool_allowlists': {'claude': ['safe']}})
        doc = self.document()
        doc['mcpServers']['jira']['url'] = 'https://changed.example/mcp'
        self.external(doc)
        self.assertIn(self.server['id'], self.common.sync())
        result = self.store.server(self.server['id'])
        self.assertEqual(result['clients'], ['claude'])
        self.assertEqual(result['tool_allowlists'], {'claude': ['safe']})
        config = self.store.resolve(result)
        self.assertEqual(config['headers']['Authorization'], 'Bearer original-secret')
        self.assertEqual(config['url'], 'https://changed.example/mcp')
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.common.sync(), set())

    def test_external_secret_stays_in_its_entry_and_resolves_after_restart(self):
        self.common.sync()
        doc = self.document()
        doc['mcpServers']['jira']['headers']['Authorization'] = 'Bearer replacement-secret'
        self.external(doc)
        self.assertIn(self.server['id'], self.common.sync())
        self.assertEqual(self.document()['mcpServers']['jira']['headers']['Authorization'], 'Bearer replacement-secret')
        self.assertNotIn('credentials', self.document())
        self.assertNotIn('replacement-secret', self.store.path.read_text())
        restarted = Store(self.store.root, self.root)
        CommonConfig(restarted).sync()
        self.assertEqual(restarted.resolve(restarted.server(self.server['id']))['headers']['Authorization'], 'Bearer replacement-secret')

    def test_malformed_and_unknown_edits_preserve_file_and_catalog(self):
        self.common.sync()
        baseline = self.store.path.read_bytes()
        for text in ('{', '{"mcpServers": []}', '{"mcpServers":{"new":{"command":"/bin/cat"}}}', '{"mcpServers":{"jira":{"url":NaN}}}', '{"mcpServers":{},"mcpServers":{}}'):
            self.common.path.write_text(text)
            with self.assertRaises(ManagerError):
                self.common.sync()
            self.assertEqual(self.common.path.read_text(), text)
            self.assertEqual(self.store.path.read_bytes(), baseline)

    def test_invalid_later_entry_cannot_partially_apply_earlier_edit(self):
        self.common.sync()
        baseline = copy.deepcopy(self.store.data)
        doc = self.document()
        doc['mcpServers']['jira']['headers']['Authorization'] = 'Bearer replacement-secret'
        doc['mcpServers']['local']['args'] = 'bad'
        self.external(doc)
        original = self.common.path.read_bytes()
        with self.assertRaises(ManagerError):
            self.common.sync()
        self.assertEqual(self.store.data, baseline)
        self.assertEqual(self.common.path.read_bytes(), original)

    def test_external_removal_deletes_server_and_persists_through_rediscovery(self):
        self.common.sync()
        sid = self.server['id']
        doc = self.document()
        del doc['mcpServers']['jira']
        self.external(doc)
        self.assertIn(sid, self.common.sync())
        with self.assertRaises(ManagerError):
            self.store.server(sid)
        self.store.rescan()
        self.common.sync()
        self.assertNotIn('jira', self.document()['mcpServers'])
        restarted = Store(self.store.root, self.root)
        restarted.rescan()
        CommonConfig(restarted).sync()
        self.assertNotIn(sid, {s['id'] for s in restarted.data['servers']})
        self.assertNotIn('jira', self.document()['mcpServers'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_imported_entry_ignores_later_source_edits(self):
        self.common.sync()
        self.source.write_bytes(self.original.replace(b'original-secret', b'new-source-secret').replace(b'https://example.com/mcp', b'https://source.example/mcp'))
        self.store.rescan()
        self.common.sync()
        self.assertEqual(self.document()['mcpServers']['jira']['url'], 'https://example.com/mcp')
        self.assertEqual(self.document()['mcpServers']['jira']['headers']['Authorization'], 'Bearer original-secret')
        self.assertEqual(self.store.resolve(self.store.server(self.server['id']))['url'], 'https://example.com/mcp')

    def test_app_saves_write_entry_immediately_and_preserve_other_entries(self):
        self.common.sync()
        before = self.document()['mcpServers']['local']
        view = editor_config(self.store, self.server)
        save_editor_config(self.store, self.server, '{"command":"/bin/echo"}', view['revision'])
        self.assertEqual(self.document()['mcpServers']['jira'], {'command': '/bin/echo'})
        self.assertEqual(self.document()['mcpServers']['local'], before)
        self.common.sync()
        self.assertEqual(self.document()['mcpServers']['jira'], {'command': '/bin/echo'})

    def test_external_secret_edit_invalidates_loaded_editor_revision(self):
        self.common.sync()
        view = editor_config(self.store, self.server)
        doc = self.document()
        doc['mcpServers']['jira']['headers']['Authorization'] = 'Bearer changed-outside'
        self.external(doc)
        original = self.common.path.read_bytes()
        with self.assertRaisesRegex(ManagerError, 'changed|stale'):
            save_editor_config(self.store, self.server, view['config_json'], view['revision'])
        self.assertEqual(self.common.path.read_bytes(), original)

    def test_semantic_noop_preserves_user_formatting(self):
        self.common.sync()
        formatted = json.dumps(self.document(), separators=(',', ':'))
        self.common.path.write_text(formatted)
        self.assertEqual(self.common.sync(), set())
        self.assertEqual(self.common.path.read_text(), formatted)

    def test_edit_after_restart_keeps_id_permissions_and_common_ownership(self):
        self.common.sync()
        self.store.update(self.server['id'], {'clients': ['cursor'], 'tool_allowlists': {'cursor': ['safe']}})
        doc = self.document()
        doc['mcpServers']['jira']['url'] = 'https://changed.example/mcp'
        self.external(doc)
        store = Store(self.store.root, self.root)
        self.assertIn(self.server['id'], CommonConfig(store).sync())
        result = store.server(self.server['id'])
        self.assertEqual(store.resolve(result)['url'], 'https://changed.example/mcp')
        self.assertEqual(result['clients'], ['cursor'])
        self.assertEqual(result['tool_allowlists'], {'cursor': ['safe']})

    def test_duplicate_names_keep_stable_keys_after_order_or_name_changes(self):
        twin = copy.deepcopy(self.server)
        twin.update(id='another-id', source_client='manager', definition={'command': '/bin/cat'})
        self.store.data['servers'].append(twin)
        self.common.sync()
        before = self.document()
        self.assertEqual(set(before['mcpServers']), {'jira', 'jira-another-id', 'local'})
        self.store.data['servers'].reverse()
        self.store.server(self.server['id'])['name'] = 'Renamed'
        self.common.sync()
        self.assertEqual(self.document(), before)
        CommonConfig(Store(self.store.root, self.root)).sync()
        self.assertEqual(self.document(), before)

    def test_symlink_is_rejected_without_touching_target(self):
        target = self.root / 'target.json'
        target.write_text('{"mcpServers":{}}')
        self.common.path.symlink_to(target)
        with self.assertRaisesRegex(ManagerError, 'symlink|Symlink'):
            self.common.sync()
        self.assertEqual(target.read_text(), '{"mcpServers":{}}')

    def test_failed_catalog_commit_rolls_back_memory_and_preserves_external_file(self):
        self.common.sync()
        baseline = copy.deepcopy(self.store.data)
        persisted = self.store.path.read_bytes()
        doc = self.document()
        doc['mcpServers']['jira']['headers']['Authorization'] = 'Bearer replacement-secret'
        self.external(doc)
        original = self.common.path.read_bytes()
        with patch.object(self.store, 'save', side_effect=OSError('fixture catalog save failure')):
            with self.assertRaises((ManagerError, OSError)):
                self.common.sync()
        self.assertEqual(self.store.data, baseline)
        self.assertEqual(self.store.path.read_bytes(), persisted)
        self.assertEqual(self.common.path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
