import json
import sys
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt import PyJWK

from bridge import AccessVerifier, build_app, dispatch, get_result

ALLOWED = {
    "hyeji": {"home": "-1000000002", "channels": ["-1000000002", "-1000000002:5"]},
    "hyerin": {"home": "-1000000001", "channels": ["-1000000001"]},
}
CFG = {"_ingress": {"api_key": "test-secret-never-public", "allowed": ALLOWED},
       "ingress_url": "http://172.18.0.1:8651/handoffs",
       "allowed_hosts": ["bridge.example.invalid"]}


class FakeJWKClient:
    def __init__(self, key): self.key = key
    def get_signing_key_from_jwt(self, token):
        class Key:
            def __init__(self, key): self.key = key
        return Key(self.key)


class FakeResponse:
    status = 202
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return b'{"status":"accepted"}'

class FakeResultResponse(FakeResponse):
    status = 200
    def read(self): return '{"status":"agent_replied","reply":"못 했어: 도구 없음"}'.encode()


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = cls.key.public_key()
        cls.auth = {"issuer": "https://myteam.cloudflareaccess.com", "audience": "app-audience",
                    "allowed_emails": ["member@example.test"]}

    def token(self, **changes):
        now = int(time.time())
        payload = {"iss": self.auth["issuer"], "aud": ["app-audience"], "exp": now + 300,
                   "iat": now, "nbf": now, "sub": "one-user", "email": "member@example.test", "type": "app"}
        payload.update(changes)
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "test"})

    def verifier(self): return AccessVerifier(self.auth, FakeJWKClient(self.public))

    def test_valid_access_jwt(self):
        self.assertEqual(self.verifier().verify(self.token())["sub"], "one-user")

    def test_reject_wrong_user_and_audience_and_expired_and_type(self):
        for change in ({"email": "someoneelse@example.test"}, {"aud": ["different"]},
                       {"exp": int(time.time()) - 300}, {"type": "org"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verifier().verify(self.token(**change))

    def test_reject_forged_signature_and_wrong_subject(self):
        forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        token = jwt.encode({"iss": self.auth["issuer"], "aud": "app-audience", "exp": now+300,
                            "iat": now, "nbf": now, "sub": "one-user", "email": "member@example.test",
                            "type": "app"}, forged, algorithm="RS256", headers={"kid": "test"})
        with self.assertRaises(ValueError): self.verifier().verify(token)
        strict = dict(self.auth, allowed_subjects=["another-user"])
        with self.assertRaises(ValueError): AccessVerifier(strict, FakeJWKClient(self.public)).verify(self.token())

    def test_locked_without_cloudflare_config(self):
        self.assertFalse(AccessVerifier({}).ready)
        with self.assertRaises(ValueError): AccessVerifier({}).verify(self.token())

    def test_dispatch_normalizes_and_calls_only_local_ingress(self):
        event_id = str(uuid4())
        with patch("bridge.urlopen", return_value=FakeResponse()) as request:
            result = dispatch(CFG, event_id, "report", "pilot done", channelid="-1000000002:5")
        self.assertEqual(result["status"], "accepted")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, CFG["ingress_url"])
        self.assertEqual(sent.headers["X-api-key"], "test-secret-never-public")
        self.assertEqual(json.loads(sent.data)["channelid"], "-1000000002:5")
        self.assertEqual(json.loads(sent.data)["intent"], "report")
        self.assertNotIn("test-secret", json.dumps(result))

    def test_dispatch_rejects_bad_route_and_noncanonical_id(self):
        with patch("bridge.urlopen") as request:
            for event_id, to, channelid in ((str(uuid4()), "hyerin", "-1000000002:5"),
                                            ("not-uuid", "hyeji", None)):
                with self.assertRaises(ValueError): dispatch(CFG, event_id, "task", "x", to, channelid)
            for bad_intent in ("", "question", "report/task", None):
                with self.assertRaises(ValueError): dispatch(CFG, str(uuid4()), bad_intent, "x")
            request.assert_not_called()

    def test_dispatch_rejects_large_body(self):
        with self.assertRaises(ValueError): dispatch(CFG, str(uuid4()), "task", "x" * 40001)

    def test_result_exact_local_lookup_and_bad_event_rejection(self):
        event_id = str(uuid4())
        with patch("bridge.urlopen", return_value=FakeResultResponse()) as request:
            result = get_result(CFG, event_id)
            self.assertEqual(result["reply"], "못 했어: 도구 없음")
            self.assertEqual(request.call_args.args[0].full_url,
                f"http://172.18.0.1:8651/handoffs/{event_id}/result")
            with self.assertRaises(ValueError): get_result(CFG, "../../../secrets")
            request.assert_called_once()

    def test_protocol_auth_gate_and_tools(self):
        import anyio
        async def run():
            verifier = self.verifier()
            app = build_app(dict(CFG, auth=self.auth), verifier)
            transport = ASGITransport(app=app)
            async with app.app.router.lifespan_context(app.app):
                async with AsyncClient(transport=transport, base_url="http://bridge.example.invalid") as client:
                    live = await client.get("/livez")
                    self.assertEqual(live.status_code, 200)
                    self.assertEqual(live.json(), {"status": "alive"})
                    ready = await client.get("/healthz")
                    self.assertEqual(ready.status_code, 200)
                    self.assertEqual(ready.json(), {"status": "ready"})
                    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0.1"}}}
                    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
                    denied = await client.post("/mcp", json=message, headers=headers)
                    self.assertEqual(denied.status_code, 401)
                    accepted = await client.post("/mcp", json=message,
                        headers=dict(headers, **{"Cf-Access-Jwt-Assertion": self.token()}))
                    self.assertEqual(accepted.status_code, 200, accepted.text[:300])
                    self.assertIn("hermes-bridge", accepted.text)
                    tools = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                        "method": "tools/list", "params": {}},
                        headers=dict(headers, **{"Cf-Access-Jwt-Assertion": self.token()}))
                    self.assertEqual(tools.status_code, 200, tools.text[:300])
                    self.assertIn("send_handoff", tools.text)
                    self.assertIn("get_handoff_result", tools.text)
                    tool = next(t for t in tools.json()["result"]["tools"] if t["name"] == "send_handoff")
                    self.assertIn("intent", tool["inputSchema"]["required"])
                    with patch("bridge.v2_request", return_value={"status": "accepted", "event_id": "test"}) as handoff:
                        called = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 3,
                            "method": "tools/call", "params": {"name": "send_handoff", "arguments": {
                                "event_id": str(uuid4()), "intent": "report", "contents": "pilot report", "to": "hyeji",
                                "channelid": "-1000000002:5"}}},
                            headers=dict(headers, **{"Cf-Access-Jwt-Assertion": self.token()}))
                        self.assertEqual(called.status_code, 200, called.text[:300])
                        self.assertFalse(called.json()["result"]["isError"])
                        self.assertEqual(json.loads(called.json()["result"]["content"][0]["text"])["status"], "migration_required")
                        handoff.assert_not_called()
                        task = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 5,
                            "method": "tools/call", "params": {"name": "send_handoff", "arguments": {
                                "event_id": str(uuid4()), "intent": "task", "contents": "check local status",
                                "to": "hyerin"}}},
                            headers=dict(headers, **{"Cf-Access-Jwt-Assertion": self.token()}))
                        self.assertEqual(task.status_code, 200)
                        self.assertFalse(task.json()["result"]["isError"])
                        self.assertEqual(handoff.call_count, 1)
                        self.assertEqual(handoff.call_args.args[4]['kind'], "task")
                        missing = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 4,
                            "method": "tools/call", "params": {"name": "send_handoff", "arguments": {
                                "event_id": str(uuid4()), "contents": "ambiguous"}}},
                            headers=dict(headers, **{"Cf-Access-Jwt-Assertion": self.token()}))
                        self.assertEqual(missing.status_code, 200)
                        self.assertTrue(missing.json()["result"]["isError"])
                        self.assertEqual(handoff.call_count, 1)
                    with patch("bridge.v2_request", return_value={"status": "agent_replied", "reply": "못 했어"}) as lookup:
                        found = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 6,
                            "method": "tools/call", "params": {"name": "get_handoff_result", "arguments": {
                                "event_id": str(uuid4())}}},
                            headers=dict(headers, **{"Cf-Access-Jwt-Assertion": self.token()}))
                        self.assertEqual(found.status_code, 200)
                        self.assertFalse(found.json()["result"]["isError"])
                        self.assertEqual(json.loads(found.json()["result"]["content"][0]["text"])["reply"], "못 했어")
                        lookup.assert_called_once()
        anyio.run(run)


if __name__ == "__main__": unittest.main()
