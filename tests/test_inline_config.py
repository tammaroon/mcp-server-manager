import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from catalog import Store,ManagerError
from gateway import Gateway
from server_config import editor_config,save_editor_config

class InlineConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        source=self.home/'.cursor/mcp.json';source.parent.mkdir()
        source.write_text(json.dumps({'mcpServers':{'remote':{'url':'https://example.test/mcp','http_headers':{'Authorization':'Bearer initial-secret'}}}}))
        self.store=Store(self.home/'state',self.home);self.store.rescan();self.server=self.store.data['servers'][0]
        self.gateway=Gateway(self.store)
    def tearDown(self):self.gateway.close();self.tmp.cleanup()
    def test_headers_live_directly_in_entry_and_editor_without_catalog_copy(self):
        self.gateway.snapshot();doc=json.loads(self.gateway.common.path.read_text())
        self.assertEqual(set(doc),{'mcpServers'})
        self.assertEqual(doc['mcpServers']['remote']['http_headers']['Authorization'],'Bearer initial-secret')
        self.assertEqual(json.loads(editor_config(self.store,self.server)['config_json']),doc['mcpServers']['remote'])
        self.assertNotIn('initial-secret',self.store.path.read_text())
        self.assertNotIn('initial-secret',json.dumps(self.gateway.snapshot()))
    def test_save_and_external_edit_survive_restart_without_credential_references(self):
        self.gateway.snapshot();view=editor_config(self.store,self.server)
        config=json.loads(view['config_json']);config['http_headers']['Authorization']='Bearer replacement-secret'
        save_editor_config(self.store,self.server,json.dumps(config),view['revision'])
        doc=json.loads(self.gateway.common.path.read_text())
        self.assertNotIn('${credential:',json.dumps(doc))
        doc['mcpServers']['remote']['http_headers']['Authorization']='Bearer external-secret'
        self.gateway.common.path.write_text(json.dumps(doc));self.gateway.snapshot()
        restarted=Store(self.store.root,self.home)
        self.assertEqual(restarted.resolve(restarted.server(self.server['id']))['http_headers']['Authorization'],'Bearer external-secret')
        self.assertNotIn('external-secret',self.store.path.read_text())
    def test_legacy_credential_section_is_inlined_and_removed(self):
        self.server['definition']={'url':'https://example.test/mcp','http_headers':{'Authorization':'${credential:config:'+self.server['id']+':old}'}}
        self.store.save()
        self.gateway.common.path.write_text(json.dumps({'mcpServers':{'remote':self.server['definition']},'credentials':{'config:'+self.server['id']+':old':'Bearer migrated-secret'}}))
        self.gateway.snapshot();doc=json.loads(self.gateway.common.path.read_text())
        self.assertEqual(set(doc),{'mcpServers'})
        self.assertEqual(doc['mcpServers']['remote']['http_headers']['Authorization'],'Bearer migrated-secret')
        self.assertNotIn('migrated-secret',self.store.path.read_text())

    def test_legacy_manual_credential_ignores_missing_overridden_oauth(self):
        sid=self.server['id']
        self.server.pop('common_key',None)
        self.server.update(credential_set=True,credential_header='Authorization',definition={'url':'https://example.test/mcp','headers':{'Authorization':'${credential:oauth-access:'+sid+'}'}})
        self.store.save()
        self.gateway.common.path.write_text(json.dumps({'mcpServers':{'remote':self.server['definition']},'credentials':{sid:'manual-secret'}}))
        self.gateway.snapshot()
        self.assertEqual(self.store.resolve(self.server)['http_headers']['Authorization'],'Bearer manual-secret')
        self.assertNotIn('credentials',json.loads(self.gateway.common.path.read_text()))

    def test_legacy_oauth_migration_preserves_refresh_and_client_metadata(self):
        sid=self.server['id'];self.server.pop('common_key',None)
        self.server.update(definition={'url':'https://example.test/mcp','http_headers':{'Authorization':'${credential:oauth-access:'+sid+'}'}},oauth={'status':'imported','client_id':'client-fixture','resource':'https://example.test/mcp','token_endpoint':'https://example.test/token'})
        self.store.save()
        bundle={'access_token':'migrated-access','refresh_token':'migrated-refresh','expires_at':12345,'original_token_response':{'id_token':'migrated-id'}}
        self.gateway.common.path.write_text(json.dumps({'mcpServers':{'remote':self.server['definition']},'credentials':{'oauth:'+sid:json.dumps(bundle)}}))
        self.gateway.snapshot();doc=json.loads(self.gateway.common.path.read_text())
        oauth=doc['mcpServers']['remote']['oauth']
        self.assertEqual(oauth['refresh_token'],'migrated-refresh')
        self.assertEqual(oauth['client_id'],'client-fixture')
        self.assertEqual(oauth['original_token_response']['id_token'],'migrated-id')
        self.assertNotIn('migrated-access',self.store.path.read_text())
        self.assertNotIn('migrated-refresh',self.store.path.read_text())
        self.assertNotIn('credentials',doc)

    def _preserve_editor_change_during(self, operation):
        self.gateway.snapshot()
        original=self.gateway.common.save_server
        def intervening_edit(sid,config,revision=None):
            doc=json.loads(self.gateway.common.path.read_text())
            doc['mcpServers']['remote']['url']='https://edited.example/mcp'
            self.gateway.common.path.write_text(json.dumps(doc))
            return original(sid,config,revision)
        with patch.object(self.gateway.common,'save_server',side_effect=intervening_edit):
            with self.assertRaisesRegex(ManagerError,'changed'):operation()
        self.assertEqual(json.loads(self.gateway.common.path.read_text())['mcpServers']['remote']['url'],'https://edited.example/mcp')

    def test_oauth_save_preserves_intervening_external_edits(self):
        self._preserve_editor_change_during(lambda:self.gateway.common.save_oauth(self.server['id'],{'access_token':'new-access','refresh_token':'new-refresh'}))

    def test_invalid_oauth_types_preserve_file_and_report_validation_error(self):
        self.gateway.snapshot();path=self.gateway.common.path
        original=json.loads(path.read_text())
        for invalid in (None,123,[],True):
            doc=json.loads(json.dumps(original));doc['mcpServers']['remote']['oauth']={'access_token':invalid}
            text=json.dumps(doc);path.write_text(text)
            snapshot=self.gateway.snapshot()
            self.assertTrue(snapshot['errors'])
            self.assertEqual(path.read_text(),text)

    def test_migration_keeps_an_inactive_oauth_login_with_manual_header(self):
        sid=self.server['id'];self.server.pop('common_key',None)
        self.server.update(definition={'url':'https://example.test/mcp','headers':{'Authorization':'Bearer manual-secret'}},oauth={'status':'inactive','client_id':'client-fixture','resource':'https://example.test/mcp'})
        self.store.save()
        bundle={'access_token':'inactive-access','refresh_token':'inactive-refresh','expires_at':12345}
        self.gateway.common.path.write_text(json.dumps({'mcpServers':{'remote':self.server['definition']},'credentials':{'oauth:'+sid:json.dumps(bundle)}}))
        self.gateway.snapshot();entry=json.loads(self.gateway.common.path.read_text())['mcpServers']['remote']
        self.assertEqual(entry['oauth']['refresh_token'],'inactive-refresh')
        self.assertEqual(entry['oauth']['status'],'inactive')
        self.assertEqual(entry['headers']['Authorization'],'Bearer manual-secret')

    def test_file_is_durable_before_catalog_commit_for_restart_recovery(self):
        self.gateway.snapshot();view=editor_config(self.store,self.server)
        config=json.loads(view['config_json']);config['http_headers']['Authorization']='Bearer recovered-secret'
        with patch.object(self.store,'save',side_effect=SystemExit('Simulated process interruption')):
            with self.assertRaises(SystemExit):save_editor_config(self.store,self.server,json.dumps(config),view['revision'])
        restarted=Store(self.store.root,self.home);Gateway(restarted).close()
        self.assertEqual(restarted.resolve(restarted.server(self.server['id']))['http_headers']['Authorization'],'Bearer recovered-secret')
        self.assertNotIn('recovered-secret',restarted.path.read_text())

    def test_legacy_manual_migration_recovers_after_file_commit_interruption(self):
        from common_config import CommonConfig
        sid=self.server['id'];self.server.pop('common_key',None)
        self.server.update(credential_set=True,credential_header='Authorization',definition={'url':'https://example.test/mcp','http_headers':{'Authorization':'${credential:'+sid+'}'}})
        self.store.save()
        self.gateway.common.path.write_text(json.dumps({'mcpServers':{'remote':self.server['definition']},'credentials':{sid:'legacy-manual-secret'}}))
        with patch.object(self.store,'save',side_effect=SystemExit('Simulated migration interruption')):
            with self.assertRaises(SystemExit):self.gateway.common.sync()
        restarted=Store(self.store.root,self.home)
        CommonConfig(restarted).sync()
        self.assertEqual(restarted.resolve(restarted.server(sid))['http_headers']['Authorization'],'Bearer legacy-manual-secret')
        self.assertNotIn('credentials',json.loads(self.gateway.common.path.read_text()))
