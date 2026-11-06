"""The rewrite must remove every direct server without altering other settings."""
import sys
from pathlib import Path
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from codex_migration import manager_only_config, reviewed_change


class CodexMigrationTests(unittest.TestCase):
    command = ['/usr/bin/python3', '/Applications/MCP Manager/bridge.py', '--client', 'codex']

    def rewrite(self, original):
        output = manager_only_config(original, self.command)
        self.assertEqual(tomllib.loads(output.decode())['mcp_servers'], {
            'mcp-manager': {'command': '/usr/bin/python3',
                            'args': ['/Applications/MCP Manager/bridge.py', '--client', 'codex'],
                            'startup_timeout_sec': 60, 'tool_timeout_sec': 120}})
        return output

    def test_removes_disabled_servers_nested_credentials_and_preserves_other_bytes(self):
        before = b'# preferences\nmodel = "gpt-5" # keep spacing\n\n'
        after = b'[projects."/Users/example/project"]\ntrust_level  =  "trusted"\n'
        original = before + b'''[mcp_servers.old]
command = "old"
enabled = false
[mcp_servers.old.env]
API_TOKEN = "private-token"
[mcp_servers.remote]
url = "https://example.invalid"
[mcp_servers.remote.http_headers]
Authorization = "Bearer private-header"
''' + after
        output = self.rewrite(original)
        self.assertTrue(output.startswith(before + after))
        self.assertNotIn(b'private-', output)
        self.assertNotIn(b'[mcp_servers.old', output)

    def test_quoted_and_dotted_names_remove_only_the_root_mcp_tree(self):
        original = b'''model = 'safe'
["mcp_servers" . 'server.with.dots']
command = 'old'
['mcp_servers'."server.with.dots".env]
TOKEN = 'secret'
[projects.'mcp_servers']
note = 'keep me'
["mcp_servers.not-the-root"]
note = 'also keep'
'''
        output = self.rewrite(original)
        self.assertIn(b"[projects.'mcp_servers']\nnote = 'keep me'", output)
        self.assertIn(b'["mcp_servers.not-the-root"]\nnote = \'also keep\'', output)

    def test_root_inline_and_dotted_assignments_are_removed(self):
        for original in [b'mcp_servers = { old = { command = "old", env = { TOKEN = "secret" } } }\nmodel = "keep"\n',
                         b'mcp_servers.old.command = "old"\nmcp_servers.old.env.TOKEN = "secret"\nmodel = "keep"\n',
                         b'[mcp_servers]\nold = { command = "old", enabled = false }\n[features]\nkeep = true\n']:
            with self.subTest(original=original):
                output = self.rewrite(original)
                self.assertNotIn(b'secret', output)
                self.assertNotIn(b'command = "old"', output)

    def test_multiline_non_mcp_values_cannot_be_mistaken_for_headers(self):
        original = b'''note = """keep this
[mcp_servers.fake]
TOKEN = 'part of a note'
"""
values = [
  "[mcp_servers.also-fake]", # quoted text
  {value = "keep"},
]
[mcp_servers.real]
args = [
 "remove",
 "[projects.fake]",
]
[projects.real]
trust_level = "trusted"
'''
        output = self.rewrite(original)
        self.assertTrue(output.startswith(original.split(b'[mcp_servers.real]')[0]))
        self.assertIn(b'[projects.real]\ntrust_level = "trusted"', output)
        self.assertNotIn(b'"remove"', output)

    def test_crlf_and_no_final_newline_are_preserved(self):
        original = b'model = "keep"\r\n[projects.keep]\r\ntrust_level = "trusted"'
        output = self.rewrite(original)
        self.assertTrue(output.startswith(original + b'\r\n'))
        self.assertNotIn(b'\n', output.replace(b'\r\n', b''))

    def test_existing_manager_is_replaced_and_rewrite_is_idempotent(self):
        original = b'[mcp_servers.mcp-manager]\ncommand="outdated"\n[mcp_servers.extra]\ncommand="remove"\n'
        output = self.rewrite(original)
        self.assertEqual(manager_only_config(output, self.command), output)

    def test_rejects_invalid_input_without_echoing_credentials(self):
        for original in [b'model = "unterminated-secret', b'\xff', b'mcp_servers = "private-secret"']:
            with self.subTest(original=original):
                with self.assertRaises(ValueError) as error:
                    manager_only_config(original, self.command)
                self.assertNotIn('secret', str(error.exception))

    def test_rejects_invalid_bridge_command(self):
        for command in [[], [''], ['python', 42], 'python', ['python\x00']]:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    manager_only_config(b'', command)

    def test_unicode_escaped_table_keys_and_array_tables_are_removed(self):
        original = (b'["mcp_\\u0073ervers"."odd#]name"]\ncommand = "old"\n'
                    b'[[mcp_servers.nested.examples]]\nvalue = "remove"\n'
                    b'[projects.keep]\nvalue = nan\n')
        output = self.rewrite(original)
        self.assertIn(b'[projects.keep]\nvalue = nan\n', output)
        self.assertNotIn(b'odd#]name', output)
        self.assertNotIn(b'examples', output)

    def test_unicode_and_escaped_bridge_arguments_round_trip(self):
        command = ['/Applications/Manager \N{SNOWMAN}/python', 'line\nbreak', '\\path', '"quoted"', '']
        output = manager_only_config(b'', command)
        bridge = tomllib.loads(output.decode())['mcp_servers']['mcp-manager']
        self.assertEqual(bridge['command'], command[0])
        self.assertEqual(bridge['args'], command[1:])

    def test_review_rejects_unrelated_edits_and_extra_servers(self):
        original = b'model = "keep"\n'
        output = manager_only_config(original, self.command)
        for proposed in [output.replace(b'model = "keep"', b'model = "changed"'),
                         output + b'[mcp_servers.extra]\ncommand = "bad"\n',
                         output.replace(b'tool_timeout_sec = 120', b'tool_timeout_sec = 1')]:
            with self.subTest(proposed=proposed):
                with self.assertRaises(ValueError):
                    reviewed_change(original, proposed)

    def test_review_never_discloses_any_original_values_or_comments(self):
        original = b'''# private-comment
model = "private-model"
[mcp_servers."private-name"]
url = "https://private-host/?token=private-query"
[mcp_servers."private-name".http_headers]
Authorization = "Bearer private-token"
'''
        output = self.rewrite(original)
        preview = reviewed_change(original, output)
        self.assertIn('mcp-manager', preview)
        self.assertIn('startup_timeout_sec', preview)
        self.assertNotIn('private-', preview)
        self.assertIn('redacted', preview.lower())


if __name__ == '__main__':
    unittest.main()
