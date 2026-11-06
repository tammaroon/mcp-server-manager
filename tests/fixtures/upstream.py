import json
import os
import sys
import time

if len(sys.argv) > 1 and sys.argv[1] == 'silent':
    time.sleep(60)
if len(sys.argv) > 1 and sys.argv[1] == 'flood':
    sys.stdout.write('x' * 2_000_000)
    sys.stdout.flush()
    time.sleep(60)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if marker := os.environ.get('METHOD_MARKER'):
        with open(marker, 'a') as f: f.write(method + '\n')
    if 'id' not in request: continue
    if method == 'initialize':
        result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'Fixture', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'Echo text', 'inputSchema': {'type': 'object'}}, {'name': 'secret', 'inputSchema': {'type': 'object'}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': request['params']['arguments'].get('text', '')}]}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
