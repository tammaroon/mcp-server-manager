import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from catalog import Store,ManagerError
from tool_names import ToolNames

class ToolNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state',self.root)
        self.jira={'id':'internal-1','name':'jira'}
        self.store.data['servers']=[self.jira]
        self.names=ToolNames(self.store)
    def tearDown(self):self.tmp.cleanup()
    def test_readable_name_maps_back_to_internal_identity(self):
        names=self.names.register(self.jira,['search_issues'])
        self.assertEqual(names,{'search_issues':'jira__search_issues'})
        self.assertEqual(self.names.resolve('jira__search_issues'),('internal-1','search_issues'))
    def test_friendly_server_normalization(self):
        logs={'id':'logs-id','name':'signal-desk-logs'}
        chrome={'id':'chrome-id','name':'io.github.ChromeDevTools/chrome-devtools-mcp'}
        self.store.data['servers'] += [logs,chrome]
        self.assertEqual(self.names.register(logs,['search_logs'])['search_logs'],'signal_desk_logs__search_logs')
        self.assertEqual(self.names.register(chrome,['list_pages'])['list_pages'],'chrome_devtools__list_pages')
    def test_server_collisions_have_stable_numeric_suffixes(self):
        twin={'id':'internal-2','name':'JIRA'};self.store.data['servers'].append(twin)
        self.assertEqual(self.names.register(self.jira,['search'])['search'],'jira__search')
        self.assertEqual(self.names.register(twin,['search'])['search'],'jira_2__search')
        self.store.data['servers'].reverse();self.store.save()
        restarted=ToolNames(Store(self.store.root,self.root))
        self.assertEqual(restarted.register(twin,['search'])['search'],'jira_2__search')
    def test_tool_sanitization_and_truncation_suffix_only_on_collision(self):
        long='very_long_tool_name_'*6
        mapping=self.names.register(self.jira,['get.issue','get_issue',long+'a',long+'b'])
        self.assertEqual(mapping['get.issue'],'jira__get_issue')
        self.assertEqual(mapping['get_issue'],'jira__get_issue_2')
        self.assertTrue(mapping[long+'b'].endswith('_2'))
        self.assertEqual(len(set(mapping.values())),4)
        for name in mapping.values():
            self.assertLessEqual(len(name),64);self.assertRegex(name,r'^[A-Za-z0-9_-]+$')
    def test_existing_names_survive_rename_and_new_colliding_tool(self):
        self.names.register(self.jira,['get_issue'])
        self.jira['name']='Renamed Jira'
        mapping=self.names.register(self.jira,['get.issue','get_issue'])
        self.assertEqual(mapping['get_issue'],'jira__get_issue')
        self.assertEqual(mapping['get.issue'],'jira__get_issue_2')
    def test_deleted_server_names_are_not_reassigned(self):
        self.names.register(self.jira,['search'])
        replacement={'id':'new-server','name':'jira'};self.store.data['servers']=[replacement]
        self.assertEqual(self.names.register(replacement,['search'])['search'],'jira_2__search')
        self.assertEqual(self.names.resolve('jira__search'),('internal-1','search'))
    def test_persist_failure_does_not_publish_new_names(self):
        before=json.loads(json.dumps(self.store.data))
        with patch.object(self.store,'save',side_effect=ManagerError('Failed persistence')):
            with self.assertRaises(ManagerError):self.names.register(self.jira,['search'])
        self.assertEqual(self.store.data,before)
        self.assertIsNone(self.names.resolve('jira__search'))
