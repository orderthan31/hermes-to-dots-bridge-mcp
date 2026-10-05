#!/usr/bin/env python3
"""Private authenticated HTTP ingress and lifecycle for durable v2 workflows.

The public OAuth MCP remains in Docker. Local orchestration uses the fixed host
Runs APIs and server-side credential files. This process exposes no public port.
"""
from __future__ import annotations
import hmac
import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hyemi_http import MAX_BODY, WorkflowIngress
from hyemi_store import NotFound, StoreConflict, WorkflowStore

LOG = logging.getLogger('hyemi-workflow-server')


def make_server(address, legacy, workflow):
    class Handler(BaseHTTPRequestHandler):
        timeout = 15
        def credentials(self):
            if (len(self.headers.get_all('X-API-Key', [])) != 1 or
                    len(self.headers.get_all('X-Hyemi-Owner', [])) != 1):
                raise ValueError('ambiguous authentication headers')
            key = self.headers.get('X-API-Key', '')
            owner = self.headers.get('X-Hyemi-Owner', '')
            workflow.authenticate(key, owner)
            return key, owner
        def do_GET(self):
            try:
                if self.path == '/healthz':
                    # Health is private and uses the same authenticated boundary.
                    self.credentials()
                    return self.respond(200, {'status': 'ready', 'protocol': 2})
                if self.path.startswith('/v2/'):
                    key, owner = self.credentials()
                    return self.respond(200, workflow.get(key, owner, self.path))
                if self.path.startswith('/handoffs/') and self.path.endswith('/result'):
                    key, owner = self.credentials()
                    if workflow.config.get('legacy_owner_id') != owner:
                        return self.respond(404, {'status': 'not_found', 'legacy_access': 'unassigned'})
                    import re
                    match = re.fullmatch(r'/handoffs/([0-9a-f-]{36})/result', self.path)
                    if match:
                        return self.respond(*legacy.result(key, match[1]))
                self.respond(404, {'status': 'not_found'})
            except Exception as exc:
                self.error(exc)
        def do_POST(self):
            try:
                if self.path != '/v2/requests':
                    return self.respond(404, {'status': 'not_found'})
                key, owner = self.credentials()  # authenticate before reading body
                if self.headers.get_all('Transfer-Encoding'):
                    return self.respond(400, {'status': 'invalid', 'error': 'transfer encoding'})
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                    return self.respond(411 if not lengths else 400, {'status': 'invalid_length'})
                size = int(lengths[0])
                if size < 1 or size > MAX_BODY:
                    return self.respond(413, {'status': 'invalid', 'error': 'body size'})
                media = self.headers.get_all('Content-Type', [])
                if len(media) != 1 or media[0].split(';', 1)[0].strip().lower() != 'application/json':
                    return self.respond(415, {'status': 'unsupported_media_type'})
                body = self.rfile.read(size)
                if len(body) != size:
                    return self.respond(400, {'status': 'invalid_body'})
                admitted = workflow.submit(key, owner, body)
                self.respond(202 if admitted['status'] == 'accepted' else 200, admitted)
            except Exception as exc:
                self.error(exc)
        def error(self, exc):
            if isinstance(exc, PermissionError):
                self.respond(401, {'status': 'unauthorized'})
            elif isinstance(exc, NotFound):
                self.respond(404, {'status': 'not_found'})
            elif isinstance(exc, StoreConflict):
                self.respond(409, {'status': 'conflict'})
            elif isinstance(exc, (ValueError, UnicodeError, OverflowError)):
                self.respond(400, {'status': 'invalid'})
            else:
                LOG.error('request failed kind=%s', type(exc).__name__)
                self.respond(503, {'status': 'unavailable'})
        def respond(self, code, obj):
            raw = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers(); self.wfile.write(raw)
            self.close_connection = True
        def log_message(self, *_):
            pass  # paths and untrusted content do not belong in operational logs
    return ThreadingHTTPServer(address, Handler)


def main():
    import fcntl
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    config_path = Path(os.environ.get('HYEMI_INGRESS_CONFIG', '/root/.config/hyemi-ingress/config.json'))
    config = json.loads(config_path.read_text())
    address = config.get('listen', ['172.18.0.1', 8651])
    if address not in (['172.18.0.1', 8651], ['127.0.0.1', 8651]):
        raise ValueError('private ingress bind required')
    from hyemi_ingress import Ingress
    from hyemi_workers import WorkflowWorkers
    state = Path(config['state_path'])
    lock_path = state.with_suffix(state.suffix + '.workflow.lock')
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # claim before store recovery
    legacy = Ingress(config, state)
    store = WorkflowStore(state)
    workers = WorkflowWorkers(config, store, legacy)
    workflow = WorkflowIngress(config, store, workers)
    server = make_server(tuple(address), legacy, workflow)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    legacy.start(); workers.start()
    LOG.info('private workflow ingress ready protocol=2')
    try:
        server.serve_forever()
    finally:
        workers.close(); legacy.close(); server.server_close(); os.close(lock)


if __name__ == '__main__':
    main()
