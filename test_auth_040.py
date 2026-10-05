"""Offline regression coverage; all keys and identities are synthetic."""
import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from bridge import AccessVerifier, GuardedApp, build_app

AUTH = dict(issuer='https://fixture.cloudflareaccess.com', audience='fixture-audience',
            allowed_emails=['fixture@example.test'], allowed_subjects=['fixture-sub'])
CFG = dict(allowed_hosts=['fixture.test'], _ingress=dict(api_key='synthetic', allowed={}))


class Keys:
    def __init__(self, key, failure=None):
        self.key, self.failure, self.calls = key, failure, 0
    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        if self.failure: raise self.failure
        return SimpleNamespace(key=self.key)
    def get_signing_keys(self):
        self.calls += 1
        if self.failure: raise self.failure
        return [SimpleNamespace(key=self.key, algorithm_name='RS256', key_type='RSA', key_id='fixture')]


class Auth040Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    def token(self, changes=None, key=None, headers=None):
        now = int(time.time())
        claims = dict(iss=AUTH['issuer'], aud=AUTH['audience'], exp=now+300,
                      iat=now, nbf=now, sub='fixture-sub', email='fixture@example.test', type='app')
        claims.update(changes or {})
        return jwt.api_jws.encode(json.dumps(claims).encode(), key or self.key, algorithm='RS256', headers=headers or {'kid':'fixture'})
    def app(self, keys=None, inner=None, **kwargs):
        self.inner_calls = 0
        async def default(scope, receive, send):
            self.inner_calls += 1
            await GuardedApp.respond(send, 200, {'status':'ok'})
        return GuardedApp(inner or default, AccessVerifier(AUTH, keys or Keys(self.key.public_key())), **kwargs)
    async def request(self, app, token=None, path='/mcp', method='GET'):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture.test') as c:
            return await c.request(method, path, headers={} if token is None else {'cf-access-jwt-assertion':token})
    def test_outage_503_sanitized_no_oauth_signal(self):
        async def run():
            keys = Keys(self.key.public_key(), jwt.PyJWKClientConnectionError('PRIVATE https://secret.test/token'))
            app = self.app(keys)
            for method in ['GET','POST','DELETE']:
                r = await self.request(app, self.token(), method=method)
                self.assertEqual(r.status_code,503)
                self.assertEqual(r.json(),{'status':'temporarily_unavailable'})
                self.assertEqual(r.headers['retry-after'],'3')
                self.assertNotIn('www-authenticate',r.headers)
                self.assertNotIn('PRIVATE',r.text)
                self.assertNotIn('oauth',r.text.lower())
            self.assertEqual(self.inner_calls,0)
            self.assertEqual(app.in_flight,0)
        anyio.run(run)
    def test_invalid_tokens_401_before_network_even_during_outage(self):
        async def run():
            keys = Keys(self.key.public_key(), jwt.PyJWKClientConnectionError('offline'))
            app = self.app(keys)
            tokens = ['', 'not-a-jwt', 'a.b.c', self.token(headers={'kid':'fixture','alg':'RS256'})+'.extra',
                      jwt.encode({'sub':'x'},'synthetic-key-that-is-at-least-32-bytes',algorithm='HS256',headers={'kid':'fixture'}),
                      self.token(headers={'kid':''}), self.token().rsplit('.',1)[0]+'.!']
            tokens += [self.token(c) for c in [dict(email=[]),dict(email=None),dict(sub=[]),dict(sub=4),
                      dict(iss=[]),dict(aud={}),dict(type=[]),dict(email='other@example.test'),
                      dict(sub='other'),dict(iss='https://other.cloudflareaccess.com'),dict(aud='other'),
                      dict(exp=int(time.time())-300),dict(nbf=int(time.time())+300)]]
            for token in tokens:
                r=await self.request(app,token)
                self.assertEqual(r.status_code,401)
            self.assertEqual(keys.calls,0)
            self.assertEqual(self.inner_calls,0)
        anyio.run(run)
    def test_bad_signature_and_missing_key_remain_401(self):
        async def run():
            app=self.app()
            self.assertEqual((await self.request(app,self.token(key=self.other))).status_code,401)
            app=self.app(Keys(self.key.public_key(),jwt.PyJWKClientError('no matching kid')))
            self.assertEqual((await self.request(app,self.token())).status_code,401)
            self.assertEqual(self.inner_calls,0)
        anyio.run(run)
    def test_readyz_actual_keys_and_no_sticky_ready(self):
        async def run():
            keys=Keys(self.key.public_key()); app=self.app(keys)
            r=await self.request(app,path='/readyz')
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.json(),{'status':'ready','in_flight':0})
            self.assertEqual(keys.calls,1)
            keys.failure=jwt.PyJWKClientConnectionError('private failure')
            r=await self.request(app,path='/readyz')
            self.assertEqual(r.status_code,503)
            self.assertEqual(r.json(),{'status':'not_ready','in_flight':0})
            self.assertEqual((await self.request(app,path='/livez')).status_code,200)
            self.assertEqual((await self.request(app,path='/healthz')).status_code,200)
            self.assertEqual(self.inner_calls,0)
        anyio.run(run)
    def test_readyz_requires_rs256_signing_key(self):
        async def run():
            keys=Keys(self.key.public_key()); app=self.app(keys)
            for value in [[],[SimpleNamespace(algorithm_name='ES256',key_type='EC',key_id='ec',key='wrong')]]:
                with patch.object(keys,'get_signing_keys',return_value=value):
                    self.assertEqual((await self.request(app,path='/readyz')).status_code,503)
        anyio.run(run)
    def test_warmup_never_bypasses_signature_validation(self):
        async def run():
            app=self.app()
            self.assertEqual((await self.request(app,path='/readyz')).status_code,200)
            self.assertEqual((await self.request(app,self.token(key=self.other))).status_code,401)
            self.assertEqual((await self.request(app,self.token())).status_code,200)
            self.assertEqual(self.inner_calls,1)
        anyio.run(run)
    def test_auth_bounded_off_loop_and_inflight_through_inner(self):
        async def run():
            started=threading.Event(); release=threading.Event(); inner_started=anyio.Event(); inner_release=anyio.Event()
            keys=Keys(self.key.public_key()); original=keys.get_signing_key_from_jwt
            def block(token):
                started.set(); release.wait(2); return original(token)
            keys.get_signing_key_from_jwt=block
            async def inner(scope,receive,send):
                inner_started.set(); await inner_release.wait(); await GuardedApp.respond(send,200,{'status':'ok'})
            app=self.app(keys,inner,auth_workers=1)
            async with anyio.create_task_group() as group:
                group.start_soon(self.request,app,self.token())
                with anyio.fail_after(1):
                    while not started.is_set(): await anyio.sleep(.001)
                    self.assertEqual(app.in_flight,1)
                    self.assertEqual((await self.request(app,path='/livez')).status_code,200)
                    busy=await self.request(app,self.token())
                    self.assertEqual(busy.status_code,503)
                    self.assertEqual(app.in_flight,1)
                    ready=await self.request(app,path='/readyz')
                    self.assertEqual(ready.status_code,503)
                    self.assertEqual(ready.json()['in_flight'],1)
                    release.set(); await inner_started.wait()
                    self.assertEqual(app.in_flight,1)
                    inner_release.set()
            self.assertEqual(app.in_flight,0)
        anyio.run(run)
    def test_inflight_finally_on_handler_and_send_failure(self):
        async def run():
            async def broken(scope,receive,send): raise RuntimeError('synthetic handler')
            app=self.app(inner=broken)
            with self.assertRaises(RuntimeError): await self.request(app,self.token())
            self.assertEqual(app.in_flight,0)
            async def send(message): raise RuntimeError('synthetic send')
            with self.assertRaises(RuntimeError): await app({'type':'http','path':'/mcp','headers':[]},None,send)
            self.assertEqual(app.in_flight,0)
        anyio.run(run)
    def test_native_cancellation_retains_admission_and_drain(self):
        async def run():
            started=threading.Event(); release=threading.Event()
            keys=Keys(self.key.public_key()); original=keys.get_signing_key_from_jwt
            def block(token):
                started.set(); release.wait(2); return original(token)
            keys.get_signing_key_from_jwt=block
            app=self.app(keys,auth_workers=1)
            request=asyncio.create_task(self.request(app,self.token()))
            try:
                with anyio.fail_after(1):
                    while not started.is_set(): await anyio.sleep(.001)
                    request.cancel(); await anyio.sleep(.01)
                    self.assertFalse(request.done())
                    self.assertEqual(app.in_flight,1)
                    self.assertEqual((await self.request(app,self.token())).status_code,503)
                    self.assertEqual((await self.request(app,'invalid')).status_code,401)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await request
            self.assertEqual(app.in_flight,0)
            self.assertEqual(app._auth_active,0)
            self.assertEqual(self.inner_calls,0)
            self.assertEqual((await self.request(app,self.token())).status_code,200)
        anyio.run(run)
    def test_anyio_cancellation_does_not_abandon_worker(self):
        async def run():
            started=threading.Event(); release=threading.Event(); scope_box=[]
            keys=Keys(self.key.public_key()); original=keys.get_signing_key_from_jwt
            def block(token):
                started.set(); release.wait(2); return original(token)
            keys.get_signing_key_from_jwt=block
            app=self.app(keys,auth_workers=1)
            async def request():
                with anyio.CancelScope() as scope:
                    scope_box.append(scope)
                    await self.request(app,self.token())
            async with anyio.create_task_group() as group:
                group.start_soon(request)
                with anyio.fail_after(1):
                    while not started.is_set(): await anyio.sleep(.001)
                    scope_box[0].cancel(); await anyio.sleep(.01)
                    self.assertEqual(app.in_flight,1)
                    self.assertEqual(app._auth_active,1)
                    release.set()
            self.assertEqual(app.in_flight,0)
            self.assertEqual(app._auth_active,0)
            self.assertEqual(self.inner_calls,0)
        anyio.run(run)
    def test_pyjwt_cache_expiry_outage_rotation_and_recovery(self):
        from io import BytesIO
        from urllib.error import URLError
        jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid='fixture',use='sig',alg='RS256')
        client=jwt.PyJWKClient(AUTH['issuer']+'/cdn-cgi/access/certs',timeout=3,lifespan=3600)
        verifier=AccessVerifier(AUTH,client)
        data=json.dumps({'keys':[jwk]}).encode()
        with patch('jwt.jwks_client.urllib.request.urlopen',return_value=BytesIO(data)) as fetch:
            verifier.warm()
            self.assertEqual(verifier.verify(self.token())['sub'],'fixture-sub')
            fetch.assert_called_once()
        with patch('jwt.jwks_client.urllib.request.urlopen',side_effect=URLError('synthetic outage')) as fetch:
            # Unexpired cache remains usable, but invalid signatures never do.
            self.assertEqual(verifier.verify(self.token())['sub'],'fixture-sub')
            with self.assertRaises(ValueError): verifier.verify(self.token(key=self.other))
            fetch.assert_not_called()
            # Expiry uses installed PyJWT's monotonic TTL, not a second cache.
            with patch('jwt.jwk_set_cache.time.monotonic',return_value=time.monotonic()+3601):
                from bridge import AuthUnavailable
                with self.assertRaises(AuthUnavailable): verifier.warm()
                with self.assertRaises(AuthUnavailable): verifier.verify(self.token())
        rotated=dict(jwk,kid='rotated')
        with patch('jwt.jwks_client.urllib.request.urlopen',return_value=BytesIO(json.dumps({'keys':[rotated]}).encode())) as fetch:
            self.assertEqual(verifier.verify(self.token(headers={'kid':'rotated'}))['sub'],'fixture-sub')
            fetch.assert_called_once()
    def test_unconfigured_readyz_and_rejected_send_counted(self):
        async def run():
            app=self.app()
            async def send(message):
                self.assertEqual(app.in_flight,1)
            await app({'type':'http','path':'/mcp','headers':[(b'cf-access-jwt-assertion',b'\xff')]},None,send)
            self.assertEqual(app.in_flight,0)
            app.verifier=AccessVerifier({})
            self.assertEqual((await self.request(app,path='/livez')).status_code,200)
            self.assertEqual((await self.request(app,path='/healthz')).status_code,503)
            r=await self.request(app,path='/readyz')
            self.assertEqual(r.status_code,503)
            self.assertEqual(r.json(),{'status':'not_ready','in_flight':0})
            self.assertEqual((await self.request(app,self.token())).status_code,503)
            self.assertEqual(self.inner_calls,0)
        anyio.run(run)
    def test_report_metadata_visible_in_protocol(self):
        async def run():
            app=build_app(CFG,AccessVerifier(AUTH,Keys(self.key.public_key())))
            async with app.app.router.lifespan_context(app.app):
                async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture.test') as c:
                    r=await c.post('/mcp',headers={'cf-access-jwt-assertion':self.token(),'accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
                    tool=next(t for t in r.json()['result']['tools'] if t['name']=='deliver_report')
                    for text in [tool['description'],tool['inputSchema']['properties']['contents']['description']]:
                        for phrase in ['messenger','concise','job','**bold**','http','code']:
                            self.assertIn(phrase,text)
                    self.assertFalse(tool['inputSchema']['additionalProperties'])
        anyio.run(run)

if __name__=='__main__': unittest.main()
