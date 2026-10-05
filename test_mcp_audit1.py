"""Offline audit capture tests: synthetic inputs only; no live services."""
import asyncio
import json
import logging
import time
import unittest
from datetime import datetime
from unittest.mock import patch

import anyio
import jwt
import bridge
import test_auth_040 as fixture
import test_tool_dispatch_drain_040 as drain_fixture


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
    def emit(self, record):
        self.records.append(record)


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.Auth040Tests.setUpClass()

    def setUp(self):
        self.f = fixture.Auth040Tests()
        self.capture = Capture()
        self.old_disable = logging.root.manager.disable
        self.old_level = bridge.LOG.level
        logging.disable(logging.NOTSET)
        bridge.LOG.setLevel(logging.INFO)
        bridge.LOG.addHandler(self.capture)
        self.addCleanup(logging.disable, self.old_disable)
        self.addCleanup(bridge.LOG.setLevel, self.old_level)
        self.addCleanup(bridge.LOG.removeHandler, self.capture)

    def record(self, status, outcome, auth, reason):
        records = self.capture.records
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertIsNone(r.exc_info)
        self.assertIsNone(r.stack_info)
        self.assertEqual(r.args, ())
        text = r.getMessage()
        self.assertLess(len(text), 768)
        for secret in ['SECRET_', 'fixture-sub', 'fixture@example.test', 'fixture-audience', 'cloudflareaccess', 'jwt-assertion']:
            self.assertNotIn(secret, text)
        row = json.loads(text)
        self.assertEqual(set(row), {'event', 'timestamp', 'request_id', 'method', 'path', 'status', 'duration_ms', 'auth_outcome', 'auth_reason', 'outcome'})
        self.assertEqual(row['event'], 'mcp_request')
        self.assertEqual(row['path'], '/mcp')
        self.assertRegex(row['request_id'], r'^[0-9a-f]{32}$')
        self.assertTrue(row['timestamp'].endswith('Z'))
        self.assertIsNotNone(datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).tzinfo)
        self.assertGreaterEqual(row['duration_ms'], 0)
        self.assertEqual((row['status'], row['outcome'], row['auth_outcome'], row['auth_reason']), (status, outcome, auth, reason))
        self.capture.records.clear()
        return row

    def test_real_auth_classification_and_privacy(self):
        async def run():
            cases = [(None, 401, 'missing', 0), ('SECRET_bad', 401, 'invalid', 0),
                     (self.f.token({'exp': int(time.time())-300}), 401, 'expired', 0),
                     (self.f.token(key=self.f.other), 401, 'signature', 1),
                     (self.f.token({'aud': 'SECRET_audience'}), 401, 'audience', 0),
                     (self.f.token({'iss': 'SECRET_issuer'}), 401, 'issuer', 0),
                     (self.f.token({'email': 'SECRET_email', 'sub': 'SECRET_sub'}), 401, 'invalid', 0),
                     (self.f.token({'extra': 'SECRET_claim'}), 200, 'verified', 1)]
            ids = []
            for token, status, reason, calls in cases:
                keys = fixture.Keys(self.f.key.public_key())
                app = self.f.app(keys)
                response = await self.f.request(app, token, '/mcp?SECRET_query=SECRET_uri')
                self.assertEqual(response.status_code, status)
                self.assertNotIn('www-authenticate', response.headers)
                self.assertNotIn('retry-after', response.headers)
                self.assertEqual(keys.calls, calls)
                row = self.record(status, 'completed', 'accepted' if status == 200 else 'rejected', reason)
                ids.append(row['request_id'])
            self.assertEqual(len(set(ids)), len(ids))
            keys = fixture.Keys(self.f.key.public_key(), jwt.PyJWKClientConnectionError('SECRET_exception'))
            response = await self.f.request(self.f.app(keys), self.f.token())
            self.assertEqual(response.headers['retry-after'], '3')
            self.assertNotIn('www-authenticate', response.headers)
            self.record(503, 'completed', 'unavailable', 'unavailable')
        anyio.run(run)

    def test_no_body_or_header_map_capture_and_method_allowlist(self):
        async def run():
            app = self.f.app()
            messages = []
            async def send(message): messages.append(message)
            async def receive(): raise AssertionError('audit must not read body SECRET_body')
            scope = {'type': 'http', 'path': '/mcp', 'method': 'SECRET_method', 'query_string': b'SECRET_query',
                     'headers': [(b'authorization', b'SECRET_bearer'), (b'cookie', b'SECRET_cookie'),
                                 (b'cf-ray', b'SECRET_ray'), (b'x-request-id', b'SECRET_id'),
                                 (b'cf-access-jwt-assertion', b'\xffSECRET_assertion')]}
            await app(scope, receive, send)
            self.assertEqual(messages[0]['status'], 401)
            self.assertEqual(self.record(401, 'completed', 'rejected', 'invalid')['method'], 'OTHER')
        anyio.run(run)

    def test_transport_status_and_exception_preservation(self):
        async def run():
            for started in [False, True]:
                error = RuntimeError('SECRET_handler_exception')
                async def inner(scope, receive, send):
                    if started: await bridge.GuardedApp.respond(send, 202, {'SECRET_result': 'SECRET_body'})
                    raise error
                app = self.f.app(inner=inner)
                with self.assertRaises(RuntimeError) as caught:
                    await self.f.request(app, self.f.token())
                self.assertIs(caught.exception, error)
                self.assertEqual(app.in_flight, 0)
                self.record(202 if started else None, 'failed', 'accepted', 'verified')
            async def send(message): raise RuntimeError('SECRET_send')
            app = self.f.app()
            with self.assertRaises(RuntimeError):
                await app({'type': 'http', 'path': '/mcp', 'headers': []}, None, send)
            self.record(None, 'failed', 'rejected', 'missing')
        anyio.run(run)

    def test_readiness_noise_and_unconfigured_busy(self):
        async def run():
            app = self.f.app()
            for path in ['/livez', '/healthz', '/readyz', '/SECRET_path']:
                await self.f.request(app, path=path)
            self.assertEqual(self.capture.records, [])
            app._auth_active = app._auth_workers
            self.assertEqual((await self.f.request(app, self.f.token())).status_code, 503)
            self.record(503, 'completed', 'unavailable', 'unavailable')
            app.verifier = bridge.AccessVerifier({})
            self.assertEqual((await self.f.request(app)).status_code, 503)
            self.record(503, 'completed', 'unavailable', 'unavailable')
        anyio.run(run)

    def test_native_cancel_audit_with_still_running_real_sdk_tool(self):
        # This existing real SDK fixture asserts HTTP exit while tool retains
        # drain, then waits for tool cleanup and verifies in_flight reaches zero.
        runner = drain_fixture.ToolDispatchDrain040Tests()
        anyio.run(runner.scenario, 'native', 'success', 'async')
        self.record(None, 'cancelled', 'accepted', 'verified')

    def test_logging_faults_never_replace_response_failure_or_cancel(self):
        async def run():
            for fault in [RuntimeError('SECRET_logger'), asyncio.CancelledError('SECRET_logger')]:
                with patch.object(bridge.LOG, 'info', side_effect=fault):
                    for token, status in [(None, 401), (self.f.token(), 200)]:
                        self.assertEqual((await self.f.request(self.f.app(), token)).status_code, status)
                    keys = fixture.Keys(self.f.key.public_key(), jwt.PyJWKClientConnectionError('SECRET_dependency'))
                    self.assertEqual((await self.f.request(self.f.app(keys), self.f.token())).status_code, 503)
                    for error in [RuntimeError('SECRET_original'), asyncio.CancelledError('SECRET_original')]:
                        async def inner(scope, receive, send): raise error
                        app = self.f.app(inner=inner)
                        with self.assertRaises(type(error)) as caught:
                            await self.f.request(app, self.f.token())
                        self.assertIs(caught.exception, error)
                        self.assertEqual(app.in_flight, 0)
        anyio.run(run)

    def test_anyio_cancel_audit_with_still_running_real_sdk_tool(self):
        runner = drain_fixture.ToolDispatchDrain040Tests()
        anyio.run(runner.scenario, 'anyio', 'success', 'sync')
        self.record(None, 'cancelled', 'accepted', 'verified')

    def test_audit_setup_and_serialization_faults_are_inert(self):
        original = json.dumps
        def broken_json(obj, *args, **kwargs):
            if isinstance(obj, dict) and obj.get('event') == 'mcp_request':
                raise RuntimeError('SECRET_serialization')
            return original(obj, *args, **kwargs)
        async def run():
            for target, fault in [('bridge._MCPAudit', RuntimeError('SECRET_setup')),
                                  ('bridge.json.dumps', broken_json)]:
                with patch(target, side_effect=fault):
                    for token, status in [(None, 401), (self.f.token(), 200)]:
                        app = self.f.app()
                        self.assertEqual((await self.f.request(app, token)).status_code, status)
                        self.assertEqual(app.in_flight, 0)
            self.assertEqual(self.capture.records, [])
        anyio.run(run)

    def test_body_send_failure_retains_started_status(self):
        async def run():
            app = self.f.app()
            error = RuntimeError('SECRET_body_send')
            async def send(message):
                if message['type'] == 'http.response.body': raise error
            with self.assertRaises(RuntimeError) as caught:
                await app({'type':'http', 'path':'/mcp', 'headers':[]}, None, send)
            self.assertIs(caught.exception, error)
            self.record(401, 'failed', 'rejected', 'missing')
        anyio.run(run)

    def test_cancelled_auth_is_not_claimed_verified(self):
        import threading
        async def run():
            started, release = threading.Event(), threading.Event()
            keys = fixture.Keys(self.f.key.public_key())
            original = keys.get_signing_key_from_jwt
            def block(token):
                started.set()
                if not release.wait(3): raise AssertionError('worker not released')
                return original(token)
            keys.get_signing_key_from_jwt = block
            app = self.f.app(keys)
            task = asyncio.create_task(self.f.request(app, self.f.token()))
            try:
                with anyio.fail_after(2):
                    while not started.is_set(): await anyio.sleep(.001)
                    task.cancel()
                    await anyio.sleep(.01)
                    self.assertFalse(task.done())
                    self.assertEqual(app.in_flight, 1)
                    self.assertEqual(self.capture.records, [])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(app.in_flight, 0)
            self.assertEqual(keys.calls, 1)
            self.record(None, 'cancelled', 'not_completed', 'not_completed')
        anyio.run(run)

    def test_logger_failure_during_real_sdk_cancel_does_not_release_tool(self):
        runner = drain_fixture.ToolDispatchDrain040Tests()
        with patch.object(bridge.LOG, 'info', side_effect=RuntimeError('SECRET_logger')):
            anyio.run(runner.scenario, 'native', 'success', 'sync')
        self.assertEqual(self.capture.records, [])

    def test_real_sdk_transport_status_and_body_privacy(self):
        from httpx import ASGITransport, AsyncClient
        async def run():
            app = bridge.build_app(fixture.CFG, bridge.AccessVerifier(fixture.AUTH, fixture.Keys(self.f.key.public_key())))
            async with app.app.router.lifespan_context(app.app):
                async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture.test') as client:
                    for method, body in [('POST', {'jsonrpc':'2.0','id':'SECRET_rpc_id','method':'tools/list','params':{}}),
                                         ('POST', {'jsonrpc':'2.0','id':'SECRET_rpc_id','method':'SECRET_rpc_method','params':{'SECRET_arguments':'SECRET_body'}}),
                                         ('DELETE', None)]:
                        response = await client.request(method, '/mcp?SECRET_query', json=body,
                            headers={'cf-access-jwt-assertion': self.f.token(), 'accept':'application/json, text/event-stream'})
                        self.record(response.status_code, 'completed', 'accepted', 'verified')
        anyio.run(run)


if __name__ == '__main__':
    unittest.main()
