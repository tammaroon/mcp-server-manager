import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from catalog import Store,ManagerError
from gateway import Gateway
from server_config import editor_config, save_editor_config

class DeleteServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.source=self.home/'.cursor/mcp.json';self.source.parent.mkdir()
        self.source.write_text(json.dumps({'mcpServers':{'fixture':{'command':sys.executable,'args':[str(Path(__file__).parent/'fixtures/upstream.py')]},'other':{'url':'https://other.example/mcp'}}}))
        self.original=self.source.read_bytes()
        self.store=Store(self.home/'state',self.home);self.store.rescan()
        self.server=next(s for s in self.store.data['servers'] if s['name']=='fixture')
        self.gateway=Gateway(self.store);self.gateway.snapshot()
    def tearDown(self):self.gateway.close();self.tmp.cleanup()
    def delete(self,**extra):return self.gateway.control({'action':'delete','server_id':self.server['id'],**extra})
    def test_delete_requires_explicit_confirmation(self):
        before=self.store.path.read_bytes()
        with self.assertRaisesRegex(ManagerError,'Confirm'):self.delete()
        self.assertEqual(self.store.path.read_bytes(),before)
    def test_delete_closes_connection_removes_owned_credentials_and_stays_deleted(self):
        sid=self.server['id'];self.gateway.check(sid)
        view=editor_config(self.store,self.server);config=json.loads(view['config_json'])
        config['env']={'FIXTURE_SECRET':'config-secret'}
        save_editor_config(self.store,self.server,json.dumps(config),view['revision'])
        result=self.delete(confirmed=True)
        self.assertNotIn(sid,self.gateway.sessions)
        self.assertNotIn(sid,[s['id'] for s in result['servers']])
        doc=json.loads(self.gateway.common.path.read_text())
        self.assertEqual(set(doc['mcpServers']),{'other'})
        self.assertNotIn('credentials',doc)
        self.assertNotIn('config-secret',json.dumps(doc))
        self.assertEqual(self.source.read_bytes(),self.original)
        restarted=Store(self.store.root,self.home);restarted.rescan()
        self.assertEqual([s['name'] for s in restarted.data['servers']],['other'])
    def test_queued_tool_listing_cannot_reopen_a_deleted_server(self):
        sid=self.server['id']
        other=next(s for s in self.store.data['servers'] if s['id']!=sid)
        self.store.update(other['id'],{'clients':[]})
        selected=threading.Event();resume=threading.Event();result={}
        permitted=self.gateway._permitted
        def pause(server,client,tool=None):
            answer=permitted(server,client,tool)
            if server['id']==sid and not selected.is_set():
                selected.set();resume.wait(3)
            return answer
        def listing():result.update(self.gateway.handle('cursor',{'id':1,'method':'tools/list'}))
        with patch.object(self.gateway,'_permitted',side_effect=pause):
            worker=threading.Thread(target=listing);worker.start()
            try:
                self.assertTrue(selected.wait(3))
                self.delete(confirmed=True)
            finally:resume.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get('result',{}).get('tools'),[])
        self.assertNotIn(sid,self.gateway.sessions)

    def test_deleted_target_does_not_return_under_a_new_source_name(self):
        self.delete(confirmed=True)
        document=json.loads(self.source.read_text())
        document['mcpServers']['renamed']=document['mcpServers'].pop('fixture')
        self.source.write_text(json.dumps(document))
        self.store.rescan()
        self.assertEqual([s['name'] for s in self.store.data['servers']],['other'])
        self.gateway._check_after_login(self.server['id'])
        self.assertNotIn(self.server['id'],self.gateway.sessions)

    def test_failed_file_write_preserves_server_credentials_and_catalog(self):
        before=copy.deepcopy(self.store.data)
        file_before=self.gateway.common.path.read_bytes()
        with patch('common_config.atomic_write',side_effect=ManagerError('File changed')):
            with self.assertRaisesRegex(ManagerError,'File changed'):self.delete(confirmed=True)
        self.assertEqual(self.store.data,before)
        self.assertEqual(self.gateway.common.path.read_bytes(),file_before)
    def test_delete_preserves_credentials_in_other_server_entry(self):
        other=next(s for s in self.store.data['servers'] if s['id']!=self.server['id'])
        view=editor_config(self.store,other);config=json.loads(view['config_json'])
        config['http_headers']={'Authorization':'Bearer other-secret'}
        save_editor_config(self.store,other,json.dumps(config),view['revision'])
        self.delete(confirmed=True)
        self.assertEqual(self.store.resolve(other)['http_headers']['Authorization'],'Bearer other-secret')
        self.assertEqual(set(json.loads(self.gateway.common.path.read_text())),{'mcpServers'})

if __name__=='__main__':unittest.main()
