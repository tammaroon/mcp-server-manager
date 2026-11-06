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
from server_config import editor_config,save_editor_config,validate
FIXTURE=str(Path(__file__).parent/'fixtures/upstream.py')

class StartupServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.marker=self.root/'requests'
        source=self.root/'.cursor/mcp.json';source.parent.mkdir()
        source.write_text(json.dumps({'mcpServers':{'logs':{'command':sys.executable,'args':[FIXTURE],'env':{'METHOD_MARKER':str(self.marker)}}}}))
        self.store=Store(self.root/'state',self.root);self.store.rescan();self.gateway=Gateway(self.store);self.server=self.store.data['servers'][0]
    def tearDown(self):self.gateway.close();self.tmp.cleanup()
    def configure(self,value):
        view=editor_config(self.store,self.server);config=json.loads(view['config_json']);config['start_on_launch']=value
        save_editor_config(self.store,self.server,json.dumps(config),view['revision'])
    def test_start_on_launch_warms_and_keeps_a_shared_connection_without_tool_calls(self):
        self.configure(True)
        self.gateway.start_configured_servers()
        self.assertEqual(self.server['status'],'reachable')
        self.assertIn(self.server['id'],self.gateway.sessions)
        self.assertEqual(self.marker.read_text().splitlines(),['initialize','notifications/initialized','tools/list'])
        self.assertNotIn('start_on_launch',self.store.resolve(self.server))
    def test_default_and_disabled_servers_are_not_launched(self):
        self.gateway.start_configured_servers();self.assertFalse(self.marker.exists())
        self.configure(True);self.store.update(self.server['id'],{'enabled':False})
        self.gateway.start_configured_servers();self.assertFalse(self.marker.exists())
    def test_startup_flag_is_boolean(self):
        with self.assertRaises(ManagerError):validate({'command':sys.executable,'start_on_launch':'true'})

    def test_shutdown_during_startup_cannot_publish_a_live_session(self):
        self.configure(True)
        arrived=threading.Event();release=threading.Event();closed=threading.Event()
        class BlockedSession:
            identity={'name':'blocked fixture'}
            def __init__(self,config):pass
            def initialize(self):arrived.set();release.wait(3)
            def tools(self):return []
            def close(self):closed.set()
        with patch('gateway.StdioSession',BlockedSession):
            worker=threading.Thread(target=self.gateway.start_configured_servers);worker.start()
            try:
                self.assertTrue(arrived.wait(3))
                self.gateway.close()
            finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertNotIn(self.server['id'],self.gateway.sessions)
        self.assertTrue(closed.is_set())
