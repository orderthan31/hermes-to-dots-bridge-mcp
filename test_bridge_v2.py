import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from types import SimpleNamespace
from test_bridge import CFG
import test_bridge as legacy
from hyemi_contracts import owner_id
from hyemi_store import WorkflowStore
from hyemi_http import WorkflowIngress
from bridge import principal_owner

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkflowStore(Path(self.tmp.name)/'state.db')
        self.api = WorkflowIngress(CFG['_ingress'], self.store)
        self.owner = owner_id('issuer','subject')
        self.key = CFG['_ingress']['api_key']
        self.raw = dict(version=2,kind='report',event_id=str(uuid4()),work_id=str(uuid4()),to='hyeji',channelid=CFG['_ingress']['allowed']['hyeji']['home'],contents='hello 😀\n'*1000)
    def test_report_delivery_and_scoped_reads(self):
        result = self.api.submit(self.key,self.owner,json.dumps(self.raw).encode())
        self.assertEqual(result['status'],'accepted')
        self.assertNotIn('session_id',result)
        rows = self.store.outbox_pending()
        self.assertEqual(''.join(r['text'] for r in rows),self.raw['contents'])
        self.assertTrue(all(r['sender']=='hyemi' for r in rows))
        self.assertEqual([r['phase'] for r in rows],['report:'+str(i) for i in range(len(rows))])
        self.assertEqual(self.store.queued_events(),[])
        self.api.submit(self.key,self.owner,json.dumps(self.raw).encode())
        self.assertEqual(len(self.store.outbox_pending()),len(rows))
        path='/v2/events/'+self.raw['event_id']+'/report'
        self.assertEqual(self.api.get(self.key,self.owner,path)['contents'],self.raw['contents'])
        with self.assertRaises(KeyError): self.api.get(self.key,'a'*64,path)
        with self.store._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM v2_queries').fetchone()[0],1)
    def test_all_reads_owner_filtered_and_tasks_queued(self):
        self.api.submit(self.key,self.owner,json.dumps(self.raw).encode())
        eid,wid=self.raw['event_id'],self.raw['work_id']
        for path in ['/v2/works/'+wid,'/v2/works/'+wid+'/updates?after_seq=0&limit=50','/v2/events/'+eid+'/result','/v2/events/'+eid+'/report']:
            self.assertIsInstance(self.api.get(self.key,self.owner,path),dict)
            with self.assertRaises(KeyError): self.api.get(self.key,'b'*64,path)
        for kind,extra in [('review',dict(revision=1,criteria='review this')),('task',dict(acceptance_criteria='done'))]:
            raw=dict(self.raw,event_id=str(uuid4()),work_id=str(uuid4()),kind=kind,**extra)
            self.api.submit(self.key,self.owner,json.dumps(raw).encode())
        self.assertEqual({e['kind'] for e in self.store.queued_events()},{'review','task'})
        with self.store._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM v2_queries').fetchone()[0],4)

    def test_auth_first_and_input_rejection(self):
        for key,owner in [('bad',self.owner),(self.key,'bad')]:
            with self.assertRaises(PermissionError): self.api.submit(key,owner,b'\xff')
            with self.assertRaises(PermissionError): self.api.get(key,owner,'%%%')
        for raw in [b'\xff',b'[]',b'{',b'{"version":2,"version":2}']:
            with self.assertRaises(ValueError): self.api.submit(self.key,self.owner,raw)
        for suffix in ['/updates?limit=101','/updates?after_seq=-1','/updates?limit=1&limit=2','?x=1','/updates?limit=01','/updates?limit=%31']:
            with self.assertRaises(ValueError): self.api.get(self.key,self.owner,'/v2/works/'+self.raw['work_id']+suffix)
    def test_missing_context_fails_closed(self):
        from mcp.server.mcpserver import Context
        for context in [None,Context(),SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={'headers':[(b'x-hyemi-owner',self.owner.encode())]})))]:
            with self.assertRaises(PermissionError): principal_owner(context)

class TransportTests(unittest.TestCase):
    def test_bounded_fixed_owner_transport_and_sanitized_error(self):
        from bridge import v2_request, MAX_RESPONSE, NoRedirect, LOCAL_OPENER
        from io import BytesIO
        from urllib.error import URLError
        from urllib.request import ProxyHandler
        owner=owner_id('issuer','subject')
        response=BytesIO(b'{"status":"accepted","session_id":"internal"}')
        with patch.object(LOCAL_OPENER,'open',return_value=response) as opened:
            self.assertEqual(v2_request(CFG,owner,'POST','/v2/requests',{}),{'status':'accepted'})
            request=opened.call_args.args[0]
            self.assertEqual(request.full_url,'http://172.18.0.1:8651/v2/requests')
            self.assertEqual(request.get_header('X-hyemi-owner'),owner)
        self.assertFalse(any(isinstance(h,ProxyHandler) and h.proxies for h in LOCAL_OPENER.handlers))
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'http://evil'))
        for response in [BytesIO(b'x'*(MAX_RESPONSE+1)),BytesIO(b'[]')]:
            with patch.object(LOCAL_OPENER,'open',return_value=response):
                with self.assertRaisesRegex(RuntimeError,'^local workflow unavailable$'):
                    v2_request(CFG,owner,'GET','/v2/works/x')
        with patch.object(LOCAL_OPENER,'open',side_effect=URLError('secret credential')):
            with self.assertRaisesRegex(RuntimeError,'^local workflow unavailable$'):
                v2_request(CFG,owner,'GET','/v2/works/x')

    def test_legacy_fallback_requires_preassigned_owner(self):
        from bridge import lookup_v2
        owner=owner_id('issuer','subject'); eid=str(uuid4())
        with patch('bridge.v2_request',return_value={'status':'not_found'}) as read:
            result=lookup_v2(CFG,owner,'events',eid,'/result')
            self.assertEqual(result['legacy_access'],'unassigned')
            self.assertEqual(read.call_count,1)
        with patch('bridge.v2_request',side_effect=[{'status':'not_found'},{'reply':'old'}]) as read:
            self.assertEqual(lookup_v2(dict(CFG,legacy_owner_id=owner),owner,'events',eid,'/result'),{'reply':'old'})
            self.assertEqual(read.call_args.args[3],'/handoffs/'+eid+'/result')

class ProtocolV2Tests(unittest.TestCase):
    setUpClass = classmethod(legacy.BridgeTests.setUpClass.__func__)
    token = legacy.BridgeTests.token
    verifier = legacy.BridgeTests.verifier
    def test_three_way_protocol(self):
        import anyio
        from httpx import ASGITransport,AsyncClient
        from bridge import build_app
        async def run():
            app=build_app(CFG,self.verifier())
            async with app.app.router.lifespan_context(app.app):
                async with AsyncClient(transport=ASGITransport(app=app),base_url='http://bridge.example.invalid') as c:
                    headers={'Accept':'application/json, text/event-stream','Cf-Access-Jwt-Assertion':self.token(),'X-Hyemi-Owner':'a'*64}
                    async def rpc(method,params):
                        return (await c.post('/mcp',headers=headers,json=dict(jsonrpc='2.0',id=1,method=method,params=params))).json()
                    await rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='test',version='1')))
                    tools=(await rpc('tools/list',{}))['result']['tools']
                    for t in tools:
                        self.assertFalse(set(t['inputSchema'].get('properties',{})) & {'ctx','owner','session_id','url','token','api_key'})
                    for name,extra in [('deliver_report',{}),('request_review',dict(revision=1,criteria='check')),('request_task',dict(acceptance_criteria='done'))]:
                        args=dict(event_id=str(uuid4()),work_id=str(uuid4()),contents='body',**extra)
                        with patch('bridge.v2_request',return_value={'status':'accepted'}) as send:
                            result=await rpc('tools/call',dict(name=name,arguments=args))
                            self.assertFalse(result['result']['isError'],result)
                            self.assertEqual(send.call_args.args[1],owner_id(self.auth['issuer'],'one-user'))
                            for field in ['owner','session_id','url','ctx','api_key']:
                                forged=await rpc('tools/call',dict(name=name,arguments=dict(args,**{field:'forged'})))
                                self.assertTrue(forged['result']['isError'],forged)
                            self.assertEqual(send.call_count,1)
        anyio.run(run)

if __name__=='__main__': unittest.main()
