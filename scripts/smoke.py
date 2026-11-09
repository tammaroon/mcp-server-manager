#!/usr/bin/env python3
"""Exercise the packaged bridge/daemon with disposable local fixtures only."""
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
packaged = ROOT / 'build/MCP Server Manager.app/Contents/Resources/backend'
BACKEND = packaged if packaged.exists() else ROOT / 'backend'
sys.path.insert(0, str(BACKEND))
from gateway import rpc, socket_path


def run():
    with tempfile.TemporaryDirectory(prefix='mcp-manager-smoke-') as tmp:
        root = Path(tmp).resolve()
        home, state = root / 'home', root / 'state'
        config = home / '.cursor/mcp.json'
        config.parent.mkdir(parents=True)
        marker = root / 'methods'
        config.write_text(json.dumps({'custom': 'preserve-me', 'mcpServers': {'fixture': {'command': sys.executable, 'args': [str(ROOT / 'tests/fixtures/upstream.py')], 'env': {'METHOD_MARKER': str(marker)}}}}))
        args = [sys.executable, str(BACKEND / 'manager.py'), '--state', str(state), '--home', str(home)]
        daemon = subprocess.Popen(args + ['daemon'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        bridge = None
        try:
            deadline = time.monotonic() + 8
            while not socket_path(state).exists():
                if daemon.poll() is not None: raise RuntimeError('Fixture daemon exited')
                if time.monotonic() > deadline: raise TimeoutError('Fixture daemon did not start')
                time.sleep(.02)
            snapshot = rpc(state, {'action': 'snapshot'})
            sid = snapshot['servers'][0]['id']
            deadline = time.monotonic() + 8
            while snapshot['servers'][0]['status'] == 'not_checked':
                if time.monotonic() > deadline: raise TimeoutError('Startup check did not complete')
                time.sleep(.02)
                snapshot = rpc(state, {'action': 'snapshot'})
            assert snapshot['servers'][0]['status'] == 'reachable'
            assert snapshot['settings']['check_interval'] == 1800
            assert marker.read_text().splitlines() == ['initialize', 'notifications/initialized', 'tools/list'], 'Startup check executed unexpected methods'
            review = rpc(state, {'action': 'preview', 'client': 'cursor'})
            applied = rpc(state, {'action': 'apply', 'client': 'cursor', 'digest': review['digest']})
            assert Path(applied['backup']).stat().st_mode & 0o777 == 0o600
            assert json.loads(config.read_text())['custom'] == 'preserve-me'
            entry = json.loads(config.read_text())['mcpServers']['mcp-manager']
            bridge = subprocess.Popen([entry['command'], *entry['args']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            def exchange(message):
                bridge.stdin.write(json.dumps(message) + '\n'); bridge.stdin.flush()
                # One response at a time: protocol stdout must never contain diagnostics.
                sel = selectors.DefaultSelector(); sel.register(bridge.stdout, selectors.EVENT_READ)
                try:
                    if not sel.select(5): raise TimeoutError('Bridge response timed out')
                    return json.loads(bridge.stdout.readline())
                finally: sel.close()
            initialized = exchange({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}})
            assert initialized['result']['serverInfo']['name'] == 'mcp-manager'
            bridge.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); bridge.stdin.flush()
            tools = exchange({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']
            name = next(t['name'] for t in tools if t['name'].endswith('__echo'))
            response = exchange({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': name, 'arguments': {'text': 'local fixture success'}}})
            assert response['result']['content'][0]['text'] == 'local fixture success'
            rpc(state, {'action': 'update', 'server_id': sid, 'changes': {'clients': []}})
            denied = exchange({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': name, 'arguments': {}}})
            assert denied['error']['code'] == -32602
            daemon.terminate(); daemon.communicate(timeout=5)
            assert bridge.wait(timeout=3) != 0, 'Bridge did not exit on daemon loss'
            print('PASS: packaged discovery → automatic startup check → reviewed config + backup → real stdio bridge → namespaced tool call → immediate revocation → disconnect cleanup')
        finally:
            if bridge is not None:
                if bridge.poll() is None: bridge.terminate()
                bridge.communicate(timeout=5)
            if daemon.poll() is None: daemon.terminate()
            daemon.communicate(timeout=5)

if __name__ == '__main__': run()
