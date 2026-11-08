import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import catalog as m


class CatalogFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.codex = self.home / '.codex/config.toml'
        self.codex.parent.mkdir()
        self.codex.write_text('model = "custom"\n[mcp_servers.logs]\ncommand = "/bin/cat"\nenabled = false\n[mcp_servers.jira]\nurl = "https://jira.example/mcp"\n[mcp_servers.jira.http_headers]\nAuthorization = "Bearer supersecret"\n')
    def tearDown(self): self.tmp.cleanup()


class CatalogTests(CatalogFixture, unittest.TestCase):
    def test_discovery_preserves_disabled_and_never_exports_secrets(self):
        servers, clients, errors = m.discover(self.home)
        self.assertEqual(len(servers), 2)
        self.assertFalse(next(s for s in servers if s['name'] == 'logs')['enabled'])
        self.assertNotIn('supersecret', json.dumps(servers))
        self.assertEqual(next(s for s in servers if s['name'] == 'jira')['auth'], 'Source credential')
    def test_redacts_nested_headers_urls_and_bearers(self):
        value = {'headers': {'X-Custom': 'hidden'}, 'url': 'https://u:pass@host/mcp?key=hidden', 'message': 'Authorization: Bearer abc.def.ghi password=hunter2'}
        safe = json.dumps(m.redact(value))
        for secret in ('hidden', 'hunter2', 'abc.def.ghi', 'pass@'):
            self.assertNotIn(secret, safe)
    def test_store_does_not_reenable_on_rediscovery(self):
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        sid = next(s['id'] for s in store.data['servers'] if s['name'] == 'jira')
        store.update(sid, {'enabled': False, 'clients': []})
        store.rescan()
        self.assertFalse(store.server(sid)['enabled'])
        self.assertEqual(store.server(sid)['clients'], [])
        self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)
    def test_equivalent_definitions_are_one_upstream_with_shared_clients(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'jira': {'type': 'http', 'url': 'https://jira.example/mcp', 'headers': {'Authorization': 'Bearer supersecret'}}}}))
        servers, _, _ = m.discover(self.home)
        jira = [s for s in servers if s['name'] == 'jira']
        self.assertEqual(len(jira), 1)
        self.assertEqual(jira[0]['clients'], ['codex', 'cursor'])
        self.assertEqual(len(jira[0]['sources']), 2)

    def test_permissions_survive_primary_source_removal(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'jira': {'url': 'https://jira.example/mcp', 'headers': {'Authorization': 'Bearer supersecret'}}}}))
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        original = next(s for s in store.data['servers'] if s['name'] == 'jira')
        store.update(original['id'], {'clients': []})
        self.codex.write_text('')
        store.rescan()
        jira = [s for s in store.data['servers'] if s['name'] == 'jira']
        self.assertEqual(len(jira), 1)
        self.assertEqual(jira[0]['id'], original['id'])
        self.assertEqual(jira[0]['clients'], [])

    def test_same_url_with_different_credentials_merges_to_codex_source(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'jira': {'url': 'https://jira.example/mcp', 'headers': {'Authorization': 'Bearer DIFFERENT'}}}}))
        servers, _, _ = m.discover(self.home)
        jira = [s for s in servers if s['name'] == 'jira']
        self.assertEqual(len(jira), 1)
        self.assertEqual(jira[0]['source_client'], 'codex')
        self.assertEqual(jira[0]['clients'], ['codex', 'cursor'])
        self.assertEqual(len(jira[0]['sources']), 2)
        self.assertNotIn('DIFFERENT', json.dumps(servers))

    def test_url_identity_normalizes_host_scheme_and_trailing_slash(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {
            'same-jira': {'url': 'HTTPS://JIRA.EXAMPLE/mcp/'},
            'different-path': {'url': 'https://jira.example/other'},
            'different-query': {'url': 'https://jira.example/mcp?project=other'},
        }}))
        servers, _, _ = m.discover(self.home)
        jira = next(s for s in servers if s['name'] == 'jira')
        self.assertEqual(jira['clients'], ['codex', 'cursor'])
        self.assertEqual(len(servers), 4)

    def test_same_command_merges_despite_arguments_environment_and_credentials(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'other-local': {
            'command': '/bin/cat', 'args': ['another-server.py'], 'env': {'TOKEN': 'different-secret'},
        }}}))
        servers, _, _ = m.discover(self.home)
        local = [s for s in servers if s['transport'] == 'stdio']
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0]['name'], 'logs')
        self.assertEqual(local[0]['clients'], ['codex', 'cursor'])
        self.assertFalse(local[0]['enabled'])

    def test_internal_servers_are_excluded_by_name_and_command_signature(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        ignored = {
            'node_repl': {'command': '/bin/repl'},
            'node-repl': {'command': '/bin/repl2'},
            'Computer Use': {'url': 'https://internal.example/mcp'},
            'computer_use': {'command': '/bin/cua'},
            'renamed-node': {'command': '/usr/bin/node', 'args': ['/bundle/node_repl/server.mjs']},
            'renamed-computer': {'command': '/bundle/unified-computer-use/bin/server'},
        }
        cursor.write_text(json.dumps({'mcpServers': ignored}))
        original = cursor.read_bytes()
        servers, _, errors = m.discover(self.home)
        self.assertEqual({s['name'] for s in servers}, {'logs', 'jira'})
        self.assertEqual(errors, [])
        self.assertEqual(cursor.read_bytes(), original)

    def test_rescan_removes_previously_cataloged_internal_servers(self):
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        base = next(s for s in store.data['servers'] if s['name'] == 'jira')
        for index, (name, definition) in enumerate([
            ('node_repl', None),
            ('Computer Use', {'url': 'https://internal.example/mcp'}),
            ('renamed-internal', {'command': '/usr/bin/node', 'args': ['/bundle/node-repl/server.mjs']}),
        ]):
            internal = copy.deepcopy(base)
            internal.update(id='internal-' + str(index), name=name, source_name=name, sources=[])
            if definition: internal['definition'] = definition
            store.data['servers'].append(internal)
        store.rescan()
        self.assertEqual({s['name'] for s in store.data['servers']}, {'logs', 'jira'})
        self.assertNotIn('internal-', store.path.read_text())

    def test_rescan_consolidates_legacy_duplicates_with_restrictive_permissions(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'jira-copy': {'url': 'https://jira.example/mcp', 'headers': {'Authorization': 'Bearer DIFFERENT'}}}}))
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        jira = next(s for s in store.data['servers'] if s['name'] == 'jira')
        duplicate = copy.deepcopy(jira)
        jira.update(sources=jira['sources'][:1], clients=['codex', 'cursor'], tool_allowlists={'codex': ['read', 'write']})
        duplicate.update(id='legacy-cursor', source_client='cursor', source_name='jira-copy',
                         sources=[{'client': 'cursor', 'path': str(cursor), 'name': 'jira-copy'}],
                         clients=['cursor'], enabled=False, tool_allowlists={'codex': ['read']})
        store.data['servers'].append(duplicate)
        original_id = jira['id']
        for _ in range(2):
            store.rescan()
            records = [s for s in store.data['servers'] if s['transport'] == 'HTTP']
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['id'], original_id)
            self.assertEqual(records[0]['clients'], ['cursor'])
            self.assertEqual(records[0]['tool_allowlists'], {'codex': ['read']})
            self.assertFalse(records[0]['enabled'])

    def test_rescan_keeps_manager_edited_duplicate_and_its_stable_credential_id(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'jira-copy': {'url': 'https://jira.example/mcp'}}}))
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        jira = next(s for s in store.data['servers'] if s['name'] == 'jira')
        managed = copy.deepcopy(jira)
        jira['sources'] = jira['sources'][:1]
        managed.update(id='managed-existing', source_client='cursor', source_name='jira-copy',
                       sources=[{'client': 'cursor', 'path': str(cursor), 'name': 'jira-copy'}],
                       endpoint='https://edited.example/mcp', definition={'url': 'https://edited.example/mcp'},
                       credential_set=True, credential_header='X-Token', clients=[], tool_allowlists={'cursor': []})
        store.data['servers'].append(managed)
        store.rescan()
        records = [s for s in store.data['servers'] if s['transport'] == 'HTTP']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['id'], 'managed-existing')
        self.assertEqual(records[0]['definition'], {'url': 'https://edited.example/mcp'})
        self.assertEqual(records[0]['endpoint'], 'https://edited.example/mcp')
        self.assertTrue(records[0]['credential_set'])
        self.assertEqual(records[0]['credential_header'], 'X-Token')
        self.assertEqual(records[0]['clients'], [])

    def test_rescan_merges_missing_legacy_source_by_endpoint(self):
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        jira = next(s for s in store.data['servers'] if s['name'] == 'jira')
        duplicate = copy.deepcopy(jira)
        duplicate.pop('discovery_identity', None)
        duplicate.update(id='removed-source', source_client='cursor', source_name='removed-jira',
                         sources=[{'client': 'cursor', 'name': 'removed-jira', 'path': '/fixture/missing'}], clients=[])
        store.data['servers'].append(duplicate)
        store.rescan()
        records = [s for s in store.data['servers'] if s['transport'] == 'HTTP']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['clients'], [])

    def test_rescan_collapses_effective_manager_target_with_another_import(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'other': {'url': 'https://edited.example/mcp'}}}))
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        jira = next(s for s in store.data['servers'] if s['name'] == 'jira')
        sid = jira['id']
        jira.update(definition={'url': 'https://edited.example/mcp'}, endpoint='https://edited.example/mcp',
                    credential_set=True, oauth={'state': 'connected', 'token_account': 'oauth:' + sid})
        for _ in range(2):
            store.rescan()
            records = [s for s in store.data['servers'] if s['transport'] == 'HTTP']
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['id'], sid)
            self.assertEqual(records[0]['definition'], {'url': 'https://edited.example/mcp'})
            self.assertEqual(records[0]['clients'], [])
            self.assertTrue(records[0]['credential_set'])
            self.assertEqual(records[0]['oauth'], {'state': 'connected', 'token_account': 'oauth:' + sid})
            self.assertEqual({source['client'] for source in records[0]['sources']}, {'codex', 'cursor'})

    def test_reference_targets_do_not_merge_without_resolving_their_sources(self):
        cursor = self.home / '.cursor/mcp.json'
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({'mcpServers': {'other': {'url': 'https://other.example/mcp'}}}))
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        for server in store.data['servers']:
            if server['transport'] == 'HTTP': server['definition'] = {'url': '${source:/url}'}
        store.rescan()
        self.assertEqual(len([s for s in store.data['servers'] if s['transport'] == 'HTTP']), 2)

    def test_missing_launch_targets_are_not_deduplicated_as_a_command(self):
        self.codex.write_text('[mcp_servers.first]\nenabled = true\n[mcp_servers.second]\nenabled = true\n')
        store = m.Store(self.home / 'state', self.home)
        store.rescan()
        self.assertEqual({s['name'] for s in store.data['servers']}, {'first', 'second'})
        self.assertTrue(all(not s['supported'] for s in store.data['servers']))

    def test_malformed_reserved_entry_is_a_discovery_error(self):
        self.codex.write_text('[mcp_servers]\nmcp-manager = "invalid"\n')
        servers, clients, errors = m.discover(self.home)
        self.assertTrue(errors)
        self.assertIsNotNone(next(c for c in clients if c['id'] == 'codex')['error'])

    def test_invalid_config_visible_as_discovery_error(self):
        self.codex.write_text('[bad toml')
        servers, clients, errors = m.discover(self.home)
        self.assertEqual(servers, [])
        self.assertTrue(errors)


class ConfigTests(CatalogFixture, unittest.TestCase):
    def adapter(self, client='codex'):
        return m.ConfigAdapter(self.home, client, ['/usr/bin/python3', '/safe/manager.py', 'bridge', '--client', client])
    def test_toml_add_preserves_original_definitions_and_backs_up(self):
        adapter = self.adapter()
        original = self.codex.read_bytes()
        review = adapter.preview()
        self.assertNotIn('supersecret', json.dumps(review))
        result = adapter.apply(review['digest'])
        import tomllib
        doc = tomllib.loads(self.codex.read_text())
        self.assertEqual(doc['model'], 'custom')
        self.assertFalse(doc['mcp_servers']['logs']['enabled'])
        self.assertEqual(doc['mcp_servers']['mcp-manager']['args'][-1], 'codex')
        self.assertEqual(Path(result['backup']).read_bytes(), original)
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o777, 0o600)
    def test_stale_preview_is_rejected(self):
        adapter = self.adapter()
        preview = adapter.preview()
        self.codex.write_text(self.codex.read_text() + '\n# edited\n')
        with self.assertRaisesRegex(m.ManagerError, 'changed'):
            adapter.apply(preview['digest'])
    def test_edit_during_apply_is_not_overwritten(self):
        from unittest.mock import patch
        adapter = self.adapter()
        review = adapter.preview()
        original_write = m.atomic_write
        import catalog
        def external_edit_then_write(path, data, **kwargs):
            self.codex.write_text(self.codex.read_text() + '\n# concurrent edit\n')
            return original_write(path, data, **kwargs)
        with patch.object(catalog, 'atomic_write', side_effect=external_edit_then_write):
            with self.assertRaises(m.ManagerError): adapter.apply(review['digest'])
        self.assertIn('# concurrent edit', self.codex.read_text())

    def test_invalid_config_never_overwritten(self):
        self.codex.write_text('invalid = =')
        with self.assertRaises(m.ManagerError): self.adapter().preview()
        self.assertEqual(self.codex.read_text(), 'invalid = =')
    def test_json_preserves_unknown_keys(self):
        path = self.home / '.cursor/mcp.json'
        path.parent.mkdir()
        path.write_text('{"custom":7,"mcpServers":{"keep":{"command":"cat"}}}')
        adapter = self.adapter('cursor')
        adapter.apply(adapter.preview()['digest'])
        result = json.loads(path.read_text())
        self.assertEqual(result['custom'], 7)
        self.assertIn('keep', result['mcpServers'])
    def test_symlink_config_refuses_apply(self):
        target = self.home / 'target.toml'
        self.codex.rename(target)
        self.codex.symlink_to(target)
        with self.assertRaises(m.ManagerError): self.adapter().preview()
    def test_toml_preview_redacts_arbitrary_header_names(self):
        self.codex.write_text('[mcp_servers.jira]\nurl = "https://example.com"\n[mcp_servers.jira.http_headers]\nX-Custom = "opaque-sensitive-value"\n')
        preview = self.adapter().preview()
        self.assertNotIn('opaque-sensitive-value', json.dumps(preview))

    def test_existing_manager_entry_is_replaced_not_duplicated(self):
        adapter = self.adapter()
        adapter.apply(adapter.preview()['digest'])
        adapter.apply(adapter.preview()['digest'])
        self.assertEqual(self.codex.read_text().count('[mcp_servers."mcp-manager"]'), 1)

if __name__ == '__main__': unittest.main()
