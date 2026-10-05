"""Shared real MCP dispatch ownership; only synthetic keys/external I/O are mocked."""
import asyncio
import json
import logging
import threading
import unittest
from unittest.mock import patch

import anyio
import httpx
from httpx import ASGITransport, AsyncClient

import bridge
import test_auth_040 as auth_fixture
from test_auth_040 import AUTH, CFG, Keys
from test_naver_protocol import backend_response


class ToolDispatchDrain040Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auth_fixture.Auth040Tests.setUpClass()

    async def scenario(self, mode='native', outcome='success', family='naver',
                       broken_send=False, late=False, cancel_tool=False):
        fixture = auth_fixture.Auth040Tests()
        servers = []
        server_class = bridge.StrictMCPServer

        def make_server(*args, **kwargs):
            server = server_class(*args, **kwargs)
            servers.append(server)
            return server

        with patch('bridge.StrictMCPServer', side_effect=make_server):
            app = bridge.build_app(CFG, bridge.AccessVerifier(AUTH, Keys(fixture.key.public_key())))
        server = servers[0]
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closing, closed, close_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        http_done, dispatch_waiting, dispatch_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        thread_started, thread_release, thread_finished = threading.Event(), threading.Event(), threading.Event()
        calls, tool_tasks, scopes, cancellations, responses = [], [], [], [], []
        dispatch_tasks, dispatch_scopes = [], []
        if not cancel_tool:
            close_release.set()

        async def work(request):
            # Ordinary cancellable transport, not a fake NaverClient.request.
            calls.append(request)
            tool_tasks.append(asyncio.current_task())
            started.set()
            try:
                await release.wait()
                if outcome == 'transport':
                    raise httpx.ReadError('synthetic transport failure', request=request)
                if outcome == 'backend':
                    return httpx.Response(502, json={'detail': 'synthetic backend failure'})
                return httpx.Response(200, json=backend_response(request.url.path, json.loads(request.content)))
            finally:
                finished.set()

        class Transport(httpx.MockTransport):
            async def aclose(self):
                closing.set()
                await close_release.wait()
                await super().aclose()
                closed.set()

        original_client = httpx.AsyncClient

        def external_client(*args, **kwargs):
            if 'transport' not in kwargs:
                kwargs['transport'] = Transport(work)
            return original_client(*args, **kwargs)

        # Register AFTER build_app: no enumeration/per-family decorator can cover
        # this future tool. It does not request Context or implement drain logic.
        if family == 'async':
            @server.tool()
            async def future_async_tool() -> dict:
                calls.append('future_async')
                tool_tasks.append(asyncio.current_task())
                started.set()
                try:
                    await release.wait()
                    if outcome != 'success':
                        raise RuntimeError('synthetic future failure')
                    return {'status': 'complete'}
                finally:
                    finished.set()
                    closing.set()
                    await close_release.wait()
                    closed.set()
            name, arguments = 'future_async_tool', {}
        elif family == 'sync':
            @server.tool()
            def future_sync_tool() -> dict:
                calls.append('future_sync')
                thread_started.set()
                try:
                    if not thread_release.wait(5):
                        raise AssertionError('test did not release sync tool')
                    if outcome != 'success':
                        raise RuntimeError('synthetic future failure')
                    return {'status': 'complete'}
                finally:
                    thread_finished.set()
            name, arguments = 'future_sync_tool', {}
        else:
            name, arguments = 'naver_search_web', {'query': 'synthetic'}

        # Pause immediately before the real shared dispatch to exercise an SDK
        # task scheduled too late, after HTTP ownership already reached zero.
        original_dispatch = server.call_tool
        async def observed_dispatch(*args, **kwargs):
            dispatch_tasks.append(asyncio.current_task())
            with anyio.CancelScope() as scope:
                dispatch_scopes.append(scope)
                return await original_dispatch(*args, **kwargs)

        async def delayed_dispatch(*args, **kwargs):
            dispatch_waiting.set()
            await dispatch_release.wait()
            try:
                return await original_dispatch(*args, **kwargs)
            finally:
                closed.set()
        server.call_tool = delayed_dispatch if late else observed_dispatch

        async def wrapped(scope, receive, send):
            async def fail_send(message):
                raise RuntimeError('synthetic ASGI send failure')
            return await app(scope, receive, fail_send if broken_send and scope['path'] == '/mcp' else send)

        headers = {'cf-access-jwt-assertion': fixture.token(), 'accept': 'application/json, text/event-stream'}
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}}
        async with app.app.router.lifespan_context(app.app):
            async with AsyncClient(transport=ASGITransport(app=wrapped), base_url='http://fixture.test') as client:
                async def request():
                    try:
                        with anyio.CancelScope() as scope:
                            scopes.append(scope)
                            try:
                                responses.append(await client.post('/mcp', headers=headers, json=body))
                            except asyncio.CancelledError:
                                cancellations.append(True)
                                raise
                    finally:
                        http_done.set()

                with patch('httpx.AsyncClient', side_effect=external_client), patch('naver_client.load_key', return_value='synthetic-key'):
                    task = asyncio.create_task(request())
                    try:
                        with anyio.fail_after(2):
                            if late:
                                await dispatch_waiting.wait()
                            elif family == 'sync':
                                while not thread_started.is_set():
                                    await anyio.sleep(.001)
                            else:
                                await started.wait()
                        self.assertEqual(app.in_flight, 1)
                        self.assertEqual(app._auth_active, 0)
                        if mode == 'native':
                            task.cancel()
                        elif mode == 'anyio':
                            scopes[0].cancel()
                        if mode != 'none':
                            with anyio.fail_after(2):
                                await http_done.wait()
                            task.cancel()  # repeat native cancellation after transport exit
                        if late:
                            self.assertEqual(app.in_flight, 0)
                            release.set()
                            dispatch_release.set()
                            with anyio.fail_after(2):
                                await closed.wait()
                            self.assertEqual(calls, [], 'late SDK task must not resurrect drained work')
                        else:
                            self.assertFalse(thread_finished.is_set() if family == 'sync' else finished.is_set())
                            self.assertEqual(app.in_flight, 1)
                            ready = await client.get('/readyz')
                            self.assertEqual(ready.status_code, 200)
                            self.assertEqual(ready.json()['in_flight'], 1)
                            self.assertEqual(app._auth_active, 0)
                            self.assertEqual(len(calls), 1)
                            if cancel_tool:
                                if family == 'sync':
                                    if cancel_tool == 'anyio':
                                        dispatch_scopes[0].cancel()
                                    else:
                                        dispatch_tasks[0].cancel()
                                    self.assertEqual((await client.get('/readyz')).json()['in_flight'], 1)
                                    if cancel_tool != 'anyio':
                                        dispatch_tasks[0].cancel()  # repeated real SDK cancellation
                                    self.assertEqual((await client.get('/readyz')).json()['in_flight'], 1)
                                    self.assertFalse(thread_finished.is_set())
                                    self.assertFalse(dispatch_tasks[0].done())
                                else:
                                    await self.cancel_async_tool(tool_tasks[0], closing, finished, closed, app, client)

                    finally:
                        release.set()
                        thread_release.set()
                        close_release.set()
                        dispatch_release.set()
                        try:
                            with anyio.fail_after(2):
                                await task
                        except asyncio.CancelledError:
                            if mode == 'none' and not cancel_tool:
                                raise
                        except Exception:
                            if not broken_send:
                                raise
                        with anyio.fail_after(2):
                            if family == 'sync' and not late:
                                while not thread_finished.is_set():
                                    await anyio.sleep(.001)
                            else:
                                await closed.wait()
                            while app.in_flight:
                                await anyio.sleep(.001)
                    self.assertEqual(app.in_flight, 0)
                    self.assertEqual(app._auth_active, 0)
                    self.assertEqual(len(calls), 0 if late else 1)
                    self.assertEqual((await client.get('/readyz')).json()['in_flight'], 0)
                    if mode != 'none':
                        self.assertTrue(cancellations)
                        self.assertEqual(responses, [])
                    elif not broken_send and not cancel_tool:
                        self.assertEqual(responses[0].json()['result'].get('isError', False), outcome != 'success')

    async def cancel_async_tool(self, tool_task, closing, finished, closed, app, client):
        # Cancel the ACTUAL SDK tool task, not just HTTP. Its ordinary async
        # finally/aclose must finish before drain ownership can be released.
        tool_task.cancel()
        with anyio.fail_after(2):
            await closing.wait()
        self.assertTrue(finished.is_set())
        self.assertFalse(closed.is_set())
        self.assertEqual(app.in_flight, 1)
        self.assertEqual((await client.get('/readyz')).json()['in_flight'], 1)

    def test_naver_native_cancel_success(self):
        anyio.run(self.scenario, 'native')

    def test_naver_anyio_cancel_success(self):
        anyio.run(self.scenario, 'anyio')

    def test_naver_native_cancel_backend_failure(self):
        anyio.run(self.scenario, 'native', 'backend')

    def test_naver_anyio_cancel_backend_failure(self):
        anyio.run(self.scenario, 'anyio', 'backend')

    def test_naver_native_cancel_transport_failure(self):
        anyio.run(self.scenario, 'native', 'transport')

    def test_naver_anyio_cancel_transport_failure(self):
        anyio.run(self.scenario, 'anyio', 'transport')

    def test_naver_success(self):
        anyio.run(self.scenario, 'none')

    def test_naver_backend_error_cleanup(self):
        anyio.run(self.scenario, 'none', 'backend')

    def test_naver_transport_error_cleanup(self):
        anyio.run(self.scenario, 'none', 'transport')

    def test_naver_asgi_send_failure_cleanup(self):
        anyio.run(self.scenario, 'none', 'success', 'naver', True)

    def test_naver_actual_sdk_task_cancel_waits_for_client_close(self):
        anyio.run(self.scenario, 'native', 'success', 'naver', False, False, True)

    def test_future_async_native_cancel(self):
        anyio.run(self.scenario, 'native', 'success', 'async')

    def test_future_async_anyio_cancel(self):
        anyio.run(self.scenario, 'anyio', 'success', 'async')

    def test_future_async_error_cleanup(self):
        anyio.run(self.scenario, 'native', 'backend', 'async')

    def test_future_async_actual_sdk_task_cancel_cleanup(self):
        anyio.run(self.scenario, 'native', 'success', 'async', False, False, True)

    def test_future_sync_native_cancel(self):
        anyio.run(self.scenario, 'native', 'success', 'sync')

    def test_future_sync_anyio_cancel(self):
        anyio.run(self.scenario, 'anyio', 'success', 'sync')

    def test_future_sync_error_cleanup(self):
        anyio.run(self.scenario, 'native', 'backend', 'sync')

    def test_future_sync_actual_sdk_native_cancel(self):
        anyio.run(self.scenario, 'native', 'success', 'sync', False, False, True)

    def test_future_sync_actual_sdk_anyio_cancel(self):
        anyio.run(self.scenario, 'anyio', 'success', 'sync', False, False, 'anyio')

    def test_future_sync_actual_sdk_cancel_error_cleanup(self):
        anyio.run(self.scenario, 'native', 'backend', 'sync', False, False, True)

    def test_late_naver_dispatch_refused(self):
        anyio.run(self.scenario, 'native', 'success', 'naver', False, True)

    def test_late_future_async_dispatch_refused(self):
        anyio.run(self.scenario, 'native', 'success', 'async', False, True)

    def test_late_future_sync_dispatch_refused(self):
        anyio.run(self.scenario, 'native', 'success', 'sync', False, True)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main()
