import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from catalog import Store,ManagerError
from credentials import FileCredentials
from gateway import Gateway

class FileCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        source=self.root/'.cursor/mcp.json';source.parent.mkdir()
        source.write_text(json.dumps({'mcpServers':{'fixture':{'url':'https://example.test/mcp'}}}))
        self.store=Store(self.root/'state',self.root);self.store.rescan()
        self.gateway=Gateway(self.store);self.server=self.store.data['servers'][0]
        self.credentials=self.gateway.credentials;self.account='oauth:'+self.server['id']
    def tearDown(self):self.gateway.close();self.tmp.cleanup()
    def test_oauth_bundle_lives_directly_in_entry_and_survives_restart(self):
        self.credentials.write(self.account,json.dumps({'access_token':'access-secret','refresh_token':'refresh-secret'}))
        doc=json.loads(self.gateway.common.path.read_text())
        self.assertNotIn('credentials',doc)
        self.assertEqual(doc['mcpServers']['fixture']['oauth']['refresh_token'],'refresh-secret')
        self.assertEqual(doc['mcpServers']['fixture']['http_headers']['Authorization'],'Bearer access-secret')
        restarted=Store(self.store.root,self.root)
        self.assertEqual(FileCredentials(restarted).read('oauth-access:'+self.server['id']),'access-secret')
        self.assertNotIn('access-secret',self.store.path.read_text())
        self.assertEqual(stat.S_IMODE(self.gateway.common.path.stat().st_mode),0o600)
    def test_removing_oauth_keeps_server_and_other_fields(self):
        self.credentials.write(self.account,json.dumps({'access_token':'access-secret','refresh_token':'refresh-secret'}))
        self.credentials.delete(self.account)
        doc=json.loads(self.gateway.common.path.read_text())
        self.assertEqual(doc['mcpServers']['fixture']['url'],'https://example.test/mcp')
        self.assertNotIn('oauth',doc['mcpServers']['fixture'])
        with self.assertRaises(ManagerError):self.credentials.read(self.account)
    def test_external_manual_header_keeps_refresh_bundle_and_deactivates_oauth(self):
        self.credentials.write(self.account,json.dumps({'access_token':'access-secret','refresh_token':'refresh-secret'}))
        doc=json.loads(self.gateway.common.path.read_text())
        doc['mcpServers']['fixture']['http_headers']['Authorization']='Bearer replacement-secret'
        self.gateway.common.path.write_text(json.dumps(doc));self.gateway.snapshot()
        self.assertEqual(self.store.resolve(self.server)['http_headers']['Authorization'],'Bearer replacement-secret')
        self.assertEqual(self.server['oauth']['status'],'inactive')
        self.assertEqual(json.loads(self.credentials.read(self.account))['refresh_token'],'refresh-secret')
        self.assertNotIn('replacement-secret',self.store.path.read_text())
    def test_invalid_file_and_symlinks_are_preserved(self):
        path=self.gateway.common.path;path.write_text('{')
        with self.assertRaises(ManagerError):self.credentials.write(self.account,'{"access_token":"unused"}')
        self.assertEqual(path.read_text(),'{')
        path.unlink();target=self.root/'outside';target.write_text('{}');path.symlink_to(target)
        with self.assertRaises(ManagerError):self.credentials.write(self.account,'{"access_token":"unused"}')
        self.assertEqual(target.read_text(),'{}')
