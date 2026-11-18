#!/usr/bin/env python3
"""CLI entry point. stdout contains JSON only; bridge mode contains MCP only."""
import json
from pathlib import Path
import sys
import threading
sys.dont_write_bytecode = True
from catalog import CLIENTS, ManagerError, redact
from event_log import EventLog


def main():
    import argparse
    parser = argparse.ArgumentParser(description='MCP Server Manager local gateway')
    parser.add_argument('--state', type=Path, default=Path.home() / 'Library/Application Support/MCP Server Manager')
    parser.add_argument('--home', type=Path, default=Path.home())
    sub = parser.add_subparsers(dest='mode', required=True)
    sub.add_parser('daemon')
    sub.add_parser('ensure')
    sub.add_parser('control')
    bridge = sub.add_parser('bridge')
    bridge.add_argument('--client', choices=list(CLIENTS), required=True)
    args = parser.parse_args()
    log = EventLog(args.state)
    threading.excepthook = lambda event: log.write('thread_failed', level='error', error=event.exc_value, mode=args.mode)
    if args.mode in ('daemon', 'bridge'): log.write('process_started', mode=args.mode)
    try:
        from gateway import ensure_daemon, rpc, run_bridge, run_daemon
        if args.mode == 'daemon': run_daemon(args.state.resolve(), args.home.resolve())
        elif args.mode == 'bridge':
            ensure_daemon(args.state, args.home)
            run_bridge(args.state, args.client)
        elif args.mode == 'ensure': print(json.dumps({'ok': True, 'result': ensure_daemon(args.state, args.home)}))
        else:
            request = json.loads(sys.stdin.buffer.read(1024 * 1024))
            print(json.dumps({'ok': True, 'result': rpc(args.state, request)}))
    except Exception as e:
        log.write('process_failed', level='error', error=e, mode=args.mode)
        error = redact(str(e)) if isinstance(e, ManagerError) else 'Gateway failed (' + type(e).__name__ + ').'
        if args.mode in ('control', 'ensure'): print(json.dumps({'ok': False, 'error': error}))
        else: print(error, file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
