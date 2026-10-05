"""Exercise the production HTTP adapter over a real loopback socket."""
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from hyemi_http import WorkflowIngress
from hyemi_store import WorkflowStore
from hyemi_server import make_server

ALLOWED = {'hyeji': {'home': '-1000000002', 'channels': ['-1000000002']}}
OWNER = 'a' * 64
OTHER = 'b' * 64


class Legacy:
    config = {'api_key': 'fixture-key', 'allowed': ALLOWED}
    def result(self, key, eid):
        return 200, {'status': 'legacy', 'event_id': eid}
    def handoff(self, key, raw):
        raise AssertionError('v2 must not run legacy agents')
    process = handoff


class ProductionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.temp.name) / 'ledger.db')
        self.workflow = WorkflowIngress(Legacy.config, self.store)
        self.server = make_server(('127.0.0.1', 0), Legacy(), self.workflow)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.client = build_opener(ProxyHandler({}))
    def tearDown(self):
        self.server.shutdown(); self.thread.join(timeout=3); self.server.server_close()
        self.temp.cleanup()
    def call(self, method, path, body=None, key='fixture-key', owner=OWNER, content_type='application/json'):
        headers = {'X-API-Key': key, 'X-Hyemi-Owner': owner, 'Content-Type': content_type}
        raw = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = Request(self.base + path, data=raw, method=method, headers=headers)
        try:
            with self.client.open(req, timeout=3) as reply:
                return reply.status, json.load(reply)
        except HTTPError as exc:
            return exc.code, json.load(exc)
    def request(self):
        return {'version': 2, 'kind': 'report', 'event_id': str(uuid.uuid4()),
                'work_id': str(uuid.uuid4()), 'to': 'hyeji',
                'channelid': '-1000000002', 'contents': '원문 😀\n줄바꿈 그대로'}
    def test_live_socket_admission_retry_and_owner_scoped_read(self):
        obj = self.request()
        code, admitted = self.call('POST', '/v2/requests', obj)
        self.assertEqual(code, 202); self.assertEqual(admitted['status'], 'accepted')
        self.assertEqual(self.call('POST', '/v2/requests', obj)[1]['status'], 'duplicate')
        code, report = self.call('GET', '/v2/events/' + obj['event_id'] + '/report')
        self.assertEqual(code, 200); self.assertEqual(report['contents'], obj['contents'])
        self.assertEqual(self.call('GET', '/v2/events/' + obj['event_id'] + '/report', owner=OTHER)[0], 404)
        self.assertEqual(self.call('GET', '/v2/works/' + obj['work_id'] + '/updates?limit=1')[0], 200)
        self.assertEqual(self.store.queued_events(), [])
    def test_authentication_precedes_json_and_media_checks(self):
        self.assertEqual(self.call('POST', '/v2/requests', b'{broken', key='wrong')[0], 401)
        self.assertEqual(self.call('POST', '/v2/requests', b'{broken', owner='forged')[0], 401)
        self.assertEqual(self.call('POST', '/v2/requests', b'{}', content_type='text/plain')[0], 415)
    def test_closed_route_conflict_and_input_errors(self):
        obj = self.request(); self.call('POST', '/v2/requests', obj)
        obj['contents'] = 'changed'
        self.assertEqual(self.call('POST', '/v2/requests', obj)[0], 409)
        self.assertEqual(self.call('POST', '/v2/requests', b'{broken')[0], 400)
        self.assertEqual(self.call('GET', '/v2/works/' + obj['work_id'] + '/updates?limit=101')[0], 400)
        self.assertEqual(self.call('GET', '/arbitrary')[0], 404)
    def test_unassigned_legacy_not_exposed(self):
        eid = str(uuid.uuid4())
        self.assertEqual(self.call('GET', '/handoffs/' + eid + '/result')[0], 404)
        self.assertEqual(self.call('GET', '/healthz', key='')[0], 401)
        self.assertEqual(self.call('GET', '/healthz')[1]['status'], 'ready')
    def test_owner_headers_and_transfer_encoding_are_unambiguous(self):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.putrequest('POST', '/v2/requests')
        conn.putheader('X-API-Key', 'fixture-key'); conn.putheader('X-Hyemi-Owner', OWNER)
        conn.putheader('X-Hyemi-Owner', OTHER); conn.putheader('Content-Length', '2')
        conn.putheader('Content-Type', 'application/json'); conn.endheaders(b'{}')
        self.assertEqual(conn.getresponse().status, 400); conn.close()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.putrequest('POST', '/v2/requests'); conn.putheader('X-API-Key', 'fixture-key')
        conn.putheader('X-Hyemi-Owner', OWNER); conn.putheader('Transfer-Encoding', 'chunked')
        conn.putheader('Content-Length', '2'); conn.putheader('Content-Type', 'application/json')
        conn.endheaders(b'{}'); self.assertEqual(conn.getresponse().status, 400); conn.close()


if __name__ == '__main__':
    unittest.main()
