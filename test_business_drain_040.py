"""Real SDK/HTTP business cancellation regressions; synthetic offline fixtures only."""
import asyncio
import logging
import threading
import unittest
from unittest.mock import patch

import anyio
from httpx import ASGITransport, AsyncClient

from bridge import AccessVerifier, build_app
import test_auth_040 as auth_fixture
from test_auth_040 import AUTH, CFG, Keys


class BusinessDrain040Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auth_fixture.Auth040Tests.setUpClass()

    async def scenario(self, mode, failure=False, broken_send=False):
        fixture = auth_fixture.Auth040Tests()
        app = build_app(CFG, AccessVerifier(AUTH, Keys(fixture.key.public_key())))
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        scopes, cancellations, responses = [], [], []
        def work(*args, **kwargs):
            started.set()
            try:
                if not release.wait(5):
                    raise AssertionError('test did not release worker')
                if failure:
                    raise RuntimeError('synthetic business failure')
                return {'status': 'accepted'}
            finally:
                finished.set()
        async def wrapped(scope, receive, send):
            async def fail_send(message):
                raise RuntimeError('synthetic transport failure')
            return await app(scope, receive, fail_send if broken_send and scope['path'] == '/mcp' else send)
        headers = {'cf-access-jwt-assertion': fixture.token(),
                   'accept': 'application/json, text/event-stream'}
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'deliver_report', 'arguments': {
                'event_id': '00000000-0000-4000-8000-000000000001',
                'work_id': '00000000-0000-4000-8000-000000000002', 'contents': 'synthetic'}}}
        async with app.app.router.lifespan_context(app.app):
            async with AsyncClient(transport=ASGITransport(app=wrapped), base_url='http://fixture.test') as client:
                async def request():
                    with anyio.CancelScope() as scope:
                        scopes.append(scope)
                        try:
                            responses.append(await client.post('/mcp', headers=headers, json=body))
                        except asyncio.CancelledError:
                            cancellations.append(True)
                            raise
                with patch('bridge.submit_v2', side_effect=work) as submit:
                    task = asyncio.create_task(request())
                    try:
                        with anyio.fail_after(2):
                            while not started.is_set():
                                await anyio.sleep(.001)
                        self.assertEqual(app.in_flight, 1)
                        self.assertEqual(app._auth_active, 0)
                        if mode == 'native':
                            task.cancel()
                        elif mode == 'anyio':
                            scopes[0].cancel()
                        await anyio.sleep(.03)
                        if mode == 'native':
                            task.cancel()  # repeated cancellation must not detach the worker
                            await anyio.sleep(.03)
                        self.assertFalse(finished.is_set())
                        self.assertEqual(app.in_flight, 1)
                        ready = await client.get('/readyz')
                        self.assertEqual(ready.status_code, 200)
                        self.assertEqual(ready.json()['in_flight'], 1)
                        self.assertEqual(app._auth_active, 0)
                        self.assertEqual(submit.call_count, 1)
                    finally:
                        release.set()
                        try:
                            with anyio.fail_after(2):
                                await task
                        except asyncio.CancelledError:
                            if mode != 'native':
                                raise
                        except Exception:
                            if not broken_send:
                                raise
                        with anyio.fail_after(2):
                            while not finished.is_set() or app.in_flight:
                                await anyio.sleep(.001)
                    self.assertEqual(app.in_flight, 0)
                    self.assertEqual(app._auth_active, 0)
                    self.assertEqual(submit.call_count, 1)
                    self.assertEqual((await client.get('/readyz')).json()['in_flight'], 0)
                    if mode != 'none':
                        self.assertTrue(cancellations, 'request cancellation must be delivered')
                        self.assertEqual(responses, [])
                    elif not broken_send:
                        self.assertEqual(responses[0].json()['result'].get('isError', False), failure)

    def test_native_cancel_real_tools_call(self):
        anyio.run(self.scenario, 'native')

    def test_anyio_cancel_real_tools_call(self):
        anyio.run(self.scenario, 'anyio')

    def test_native_cancel_worker_error_cleanup(self):
        anyio.run(self.scenario, 'native', True)

    def test_anyio_cancel_worker_error_cleanup(self):
        anyio.run(self.scenario, 'anyio', True)

    def test_handler_error_cleanup(self):
        anyio.run(self.scenario, 'none', True)

    def test_transport_failure_cleanup(self):
        anyio.run(self.scenario, 'none', False, True)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main()
