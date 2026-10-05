"""Offline NAVER tests. Initial RED: test_inventory, ModuleNotFoundError (1 test).
Run: PYTHONPATH=. python -m unittest -q test_naver_tools
No live network, real credentials, or service mutations.
"""
import asyncio
import copy
import inspect
import json
import unittest
from unittest.mock import patch, AsyncMock
import httpx
from pydantic import ValidationError
from mcp.server.mcpserver import MCPServer
from naver_contracts import OPERATIONS, payload, periods
from naver_client import Admission, NaverClient, NaverError, MAX_BYTES, BASE_URL, load_key
from naver_tools import register_naver_tools, evidence, FIELDS

OWNER = 'a'*64
CANARY = 'secret-canary-do-not-return'

def arguments(name):
    model, route, mode = OPERATIONS[name]
    if route == '/naver/search':
        return {'query':'  test   query  '}
    args = dict(start_date='2024-01-01', end_date='2024-01-02', time_unit='date')
    if route == '/naver/keyword-trend':
        args['keyword_groups'] = [{'group_name':'test', 'keywords':['coffee']}]
    elif mode == 'categories':
        args['category'] = [{'name':'test', 'param':['50000000']}]
    else:
        args['category'] = '50000000'
        if mode == 'keywords':
            args['keyword'] = [{'name':'test', 'param':['coffee']}]
        elif mode.startswith('keyword_'):
            args['keyword'] = 'coffee'
    return args


def response_for(body):
    source = body.get('source')
    if source:
        if source == 'adult':
            data = {'adult':'0'}
        elif source == 'errata':
            data = {'errata':''}
        else:
            data = dict(items=[], total=0, start=body['start'], display=0,
                        lastBuildDate='Thu, 01 Oct 2026 10:00:00 +0900')
        return dict(source=source, query=body['query'], cache_hit=False, data=data)
    data = {k:body[k] for k in ('startDate','endDate','timeUnit')}
    data['results'] = []
    result = dict(cache_hit=True, relative_values=True, data=data)
    if 'mode' in body:
        result['mode'] = body['mode']
    return result

class InventoryTests(unittest.TestCase):
    def test_inventory(self):
        self.assertEqual(len(OPERATIONS), 19)
        self.assertEqual(sum(v[1]=='/naver/search' for v in OPERATIONS.values()),10)

    def test_fixed_mapping_independent(self):
        search = {'web':'webkr','encyclopedia':'encyc','news':'news','blog':'blog','cafe':'cafearticle','kin':'kin','local':'local','image':'image'}
        expected = {'naver_search_'+name:('/naver/search',source) for name,source in search.items()}
        expected.update(naver_convert_keyboard_query=('/naver/search','errata'),
                        naver_check_adult_query=('/naver/search','adult'),
                        naver_keyword_trend=('/naver/keyword-trend',None),
                        naver_shopping_category_trend=('/naver/shopping-trend','categories'),
                        naver_shopping_keyword_trend=('/naver/shopping-trend','keywords'))
        for kind in ('category','keyword'):
            for dim in ('device','gender','age'):
                expected['naver_shopping_'+kind+'_'+dim+'_trend']=('/naver/shopping-trend',kind+'_'+dim)
        self.assertEqual({k:v[1:] for k,v in OPERATIONS.items()},expected)

    def test_schema_public_and_closed(self):
        server = MCPServer('test')
        register_naver_tools(server, {'base_url':'http://evil.invalid'}, lambda ctx: OWNER)
        forbidden = {'ctx','owner','url','path','source','mode','header','key','session'}
        for name in OPERATIONS:
            tool = server._tool_manager.get_tool(name)
            schema = tool.parameters
            self.assertFalse(forbidden & schema['properties'].keys(), name)
            self.assertIs(schema['additionalProperties'],False)
            self.assertTrue(tool.annotations.read_only_hint)
            for definition in schema.get('$defs',{}).values():
                if definition.get('type')=='object':
                    self.assertIs(definition.get('additionalProperties'),False)
        self.assertNotIn('sort',server._tool_manager.get_tool('naver_search_web').parameters['properties'])
        self.assertNotIn('start',server._tool_manager.get_tool('naver_search_local').parameters['properties'])
        self.assertEqual(set(server._tool_manager.get_tool('naver_check_adult_query').parameters['properties']),{'query'})

    def test_strict_integer_scalars(self):
        for field in ('display','start'):
            for value in (True,False,1.0,'1',float('nan'),float('inf'),0,-1):
                with self.subTest(field=field,value=value), self.assertRaises(ValidationError):
                    payload('naver_search_web',{'query':'q',field:value})

    def test_query_bounds(self):
        for value in ('', '  ', 'q'*201, 123, None, True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                payload('naver_search_web',{'query':value})
        self.assertEqual(payload('naver_search_web',{'query':' a\n b '})[1]['query'],'a b')

    def test_unsupported_fields(self):
        for name in OPERATIONS:
            for field in ('url','owner','ctx','source','mode','api_key','session'):
                with self.subTest(name=name,field=field), self.assertRaises(ValidationError):
                    payload(name,{**arguments(name),field:'evil'})
        for name, field, value in [('naver_search_web','sort','sim'),('naver_search_encyclopedia','sort','sim'),('naver_search_local','start',1),('naver_check_adult_query','display',1),('naver_convert_keyboard_query','sort','sim')]:
            with self.assertRaises(ValidationError):
                payload(name,{**arguments(name),field:value})

    def test_pagination_and_sort_bounds(self):
        for name, field, value in [('naver_search_web','display',31),('naver_search_web','start',1001),('naver_search_local','display',6),('naver_search_local','sort','sim'),('naver_search_news','sort','point'),('naver_search_image','filter','evil')]:
            with self.assertRaises(ValidationError):
                payload(name,{**arguments(name),field:value})
        self.assertEqual(payload('naver_search_web',{'query':'q','display':30,'start':1000})[1]['start'],1000)
        self.assertEqual(payload('naver_search_kin',{'query':'q','sort':'point'})[1]['sort'],'point')

    def test_nested_unknown_and_strict(self):
        name='naver_keyword_trend'
        for group in ({'group_name':'x','keywords':['a'],'extra':1},{'group_name':7,'keywords':['a']},{'group_name':'x','keywords':[1]},{'group_name':'x','keywords':[' ']},{'group_name':' ','keywords':['x']},{'groupName':'x','keywords':['x']}):
            with self.assertRaises(ValidationError):
                payload(name,{**arguments(name),'keyword_groups':[group]})

    def test_group_limits(self):
        for n in (0,6):
            with self.assertRaises(ValidationError):
                payload('naver_keyword_trend',{**arguments('naver_keyword_trend'),'keyword_groups':[{'group_name':'a','keywords':['a']}]*n})
        for n in (0,21):
            with self.assertRaises(ValidationError):
                payload('naver_keyword_trend',{**arguments('naver_keyword_trend'),'keyword_groups':[{'group_name':'a','keywords':['a']*n}]})
        for field in ('group_name','keywords'):
            group={'group_name':'x','keywords':['x']}
            group[field] = 'x'*101 if field=='group_name' else ['x'*101]
            with self.assertRaises(ValidationError):
                payload('naver_keyword_trend',{**arguments('naver_keyword_trend'),'keyword_groups':[group]})

    def test_shopping_pairs(self):
        for name,field in [('naver_shopping_category_trend','category'),('naver_shopping_keyword_trend','keyword')]:
            limit = 3 if field=='category' else 5
            for pairs in ([],[{'name':'x','param':['x']}]* (limit+1),[{'name':'x','param':['x'],'unknown':1}],[{'name':'x','param':[5]}],[{'name':' ','param':['x']}]):
                with self.assertRaises(ValidationError):
                    payload(name,{**arguments(name),field:pairs})
        with self.assertRaises(ValidationError):
            payload('naver_shopping_keyword_trend',{**arguments('naver_shopping_keyword_trend'),'keyword':[{'name':'x','param':['x','y']}]})

    def test_ages_and_demographics(self):
        for name,valid,bad in [('naver_keyword_trend',['1','11'],['20']),('naver_shopping_category_trend',['10','60'],['1'])]:
            payload(name,{**arguments(name),'ages':valid,'device':'pc','gender':'f'})
            for extra in ({'ages':bad},{'ages':[10]},{'device':'mobile'},{'gender':True}):
                with self.assertRaises(ValidationError):
                    payload(name,{**arguments(name),**extra})

    def test_dates_and_period_bounds(self):
        name='naver_keyword_trend'
        for extra in ({'start_date':'2024-1-01'},{'start_date':'2024-02-30'},{'start_date':'2024-01-01T00:00:00Z'},{'start_date':'2015-12-31'},{'start_date':'2024-01-03'},{'end_date':'2025-01-01'}):
            with self.assertRaises(ValidationError):
                payload(name,{**arguments(name),**extra})
        payload(name,{**arguments(name),'end_date':'2024-12-31'})
        payload(name,{**arguments(name),'start_date':'2016-01-01','end_date':'2026-01-01','time_unit':'month'})
        payload(name,{**arguments(name),'start_date':'2016-01-01','end_date':'2020-01-01','time_unit':'week'})
        with self.assertRaises(ValidationError):
            payload(name,{**arguments(name),'start_date':'2016-01-01','end_date':'2026-01-01','time_unit':'week'})
        name='naver_shopping_category_trend'
        with self.assertRaises(ValidationError):
            payload(name,{**arguments(name),'start_date':'2017-07-31'})
        self.assertEqual(periods('2024-01-07','2024-01-08','week'),2)

class EvidenceTests(unittest.TestCase):
    def test_source_specific_fields_and_whitelist(self):
        for name,(_,route,source) in OPERATIONS.items():
            if source not in FIELDS: continue
            _,body=payload(name,arguments(name)); response=response_for(body)
            item={key:'provider-value' for key in FIELDS[source].split()}
            item.update(api_key=CANARY, raw_body=CANARY, price=999)
            response['data'].update(items=[item],total=50,display=1)
            response['secret']=CANARY
            out=evidence(name,body,response)
            self.assertEqual(set(out['items'][0]),set(FIELDS[source].split()))
            self.assertNotIn(CANARY,json.dumps(out))
            self.assertEqual(out['returned_count'],1)
            self.assertIn('not search volume',out['total_semantics'])

    def test_trend_series_and_whitelist(self):
        for name,(_,route,mode) in OPERATIONS.items():
            if route=='/naver/search': continue
            _,body=payload(name,arguments(name)); response=response_for(body)
            row={'period':'2024-01-01','ratio':0, 'secret':CANARY}
            if mode and '_' in mode:
                row['group']={'device':'pc','gender':'m','age':'20'}[mode.rsplit('_',1)[-1]]
            response['data']['results']=[{'title':'test','data':[row],'secret':CANARY}]
            out=evidence(name,body,response)
            self.assertEqual(out['returned_point_count'],1)
            self.assertNotIn(CANARY,json.dumps(out))
            self.assertTrue(out['relative_values'])

    def test_invalid_search_envelopes(self):
        name='naver_search_news'; _,body=payload(name,arguments(name))
        for change in ({'source':'blog'},{'query':'other'},{'cache_hit':'false'},{'data':{}},{'data':None}):
            response=response_for(body); response.update(change)
            with self.assertRaises(NaverError): evidence(name,body,response)
        for change in ({'start':0},{'start':2},{'total':True},{'total':-1},{'display':31},{'items':[{'title':'x','link':'x'}]*11},{'items':[{'title':'x'}]},{'items':None}):
            response=response_for(body); response['data'].update(change)
            with self.assertRaises(NaverError): evidence(name,body,response)

    def test_invalid_trend_data(self):
        name='naver_keyword_trend'; _,body=payload(name,arguments(name))
        for row in ({'period':'2024-01-01','ratio':float('nan')},{'period':'2024-01-01','ratio':float('inf')},{'period':'2024-01-01','ratio':True},{'period':'2024-01-01','ratio':'1'},{'period':'2024-01-01','ratio':101},{'period':'2024-02-01','ratio':5},{'period':'garbage','ratio':1}):
            response=response_for(body); response['data']['results']=[{'title':'x','data':[row]}]
            with self.assertRaises(NaverError): evidence(name,body,response)
        for rows in ([{'period':'2024-01-01','ratio':1}]*2,[{'period':'2024-01-01','ratio':1}]*367):
            response=response_for(body); response['data']['results']=[{'title':'x','data':rows}]
            with self.assertRaises(NaverError): evidence(name,body,response)

    def test_period_alignment_and_count(self):
        name='naver_keyword_trend'
        _,body=payload(name,{**arguments(name),'time_unit':'month','end_date':'2024-02-28'})
        response=response_for(body)
        response['data']['results']=[{'title':'test','data':[{'period':'2024-01-02','ratio':1}]}]
        with self.assertRaises(NaverError): evidence(name,body,response)

    def test_six_age_series_366_points_each(self):
        from datetime import date, timedelta
        name='naver_shopping_category_age_trend'
        _,body=payload(name,{**arguments(name),'end_date':'2024-12-31'})
        response=response_for(body)
        rows=[{'period':(date(2024,1,1)+timedelta(days=n)).isoformat(),'ratio':50,'group':age}
              for n in range(366) for age in ('10','20','30','40','50','60')]
        response['data']['results']=[{'title':'test','category':['50000000'],'data':rows}]
        self.assertEqual(evidence(name,body,response)['returned_point_count'],2196)
        response['data']['results'][0]['data'].append(rows[0])
        with self.assertRaises(NaverError): evidence(name,body,response)

    def test_response_string_bound(self):
        name='naver_search_news'; _,body=payload(name,arguments(name)); response=response_for(body)
        response['data'].update(items=[{'title':'x'*8193,'link':'x'}],display=1)
        with self.assertRaises(NaverError): evidence(name,body,response)

    def test_result_wire_limit(self):
        from naver_tools import as_result
        with self.assertRaises(NaverError) as ctx:
            as_result({'value':'x'*140000})
        self.assertEqual(ctx.exception.category,'size')

    def test_error_wire_flags(self):
        from naver_tools import as_result
        out=as_result({'error':{'category':'provider'}},True).model_dump(by_alias=True)
        self.assertIs(out['isError'],True)
        self.assertEqual(out['structuredContent'],{'error':{'category':'provider'}})

    def test_support_results(self):
        name='naver_check_adult_query'; _,body=payload(name,arguments(name)); response=response_for(body)
        response['data']['adult']='yes'
        with self.assertRaises(NaverError): evidence(name,body,response)
        name='naver_convert_keyboard_query'; _,body=payload(name,arguments(name))
        self.assertEqual(evidence(name,body,response_for(body))['errata'],'')

class SecretTests(unittest.TestCase):
    def test_key_file_validation(self):
        import io
        for raw in (b'',b'  ',b'x'*4097,b'bad\nheader',b'\xff'):
            with patch('naver_client.Path.open',return_value=io.BytesIO(raw)):
                with self.assertRaises(NaverError) as ctx: load_key()
                self.assertEqual(ctx.exception.category,'configuration')
        with patch('naver_client.Path.open',return_value=io.BytesIO(b' test-key\n')):
            self.assertEqual(load_key(),'test-key')

    def test_server_file_override_only(self):
        import io
        with patch.dict('os.environ',{'NAVER_UTILITY_API_KEY_FILE':'/test-only-key'},clear=True), patch('naver_client.Path.open',return_value=io.BytesIO(b'test-key')) as opened:
            self.assertEqual(load_key(),'test-key')
            opened.assert_called_once_with('rb')

class AdmissionTests(unittest.TestCase):
    def test_per_owner_and_expiry(self):
        now=[100.0]; admission=Admission(lambda:now[0])
        for _ in range(60): admission.admit(OWNER)
        with self.assertRaises(NaverError) as ctx: admission.admit(OWNER)
        self.assertEqual(ctx.exception.category,'rate')
        admission.admit('b'*64)
        now[0]=160.0; admission.admit('c'*64)
        self.assertEqual(admission.owners,{'c'*64:1})

    def test_global_bookkeeping_bound(self):
        admission=Admission(lambda:0.0)
        for n in range(120): admission.admit(f'{n:064x}')
        for n in range(120,1000):
            with self.assertRaises(NaverError): admission.admit(f'{n:064x}')
        self.assertEqual(len(admission.owners),120)
        self.assertEqual(len(admission.events),120)

    def test_owner_validation(self):
        for owner in ('client','a'*63,'g'*64,None,1):
            with self.assertRaises(NaverError) as ctx: Admission().admit(owner)
            self.assertEqual(ctx.exception.category,'auth')

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, client=None):
        real=httpx.AsyncClient
        def factory(**kwargs):
            self.assertIs(kwargs['trust_env'],False)
            self.assertIs(kwargs['follow_redirects'],False)
            self.assertEqual(kwargs['timeout'].connect,3.0)
            return real(transport=httpx.MockTransport(handler),**kwargs)
        with patch('naver_client.load_key',return_value='test-key'), patch('naver_client.httpx.AsyncClient',side_effect=factory):
            return await (client or NaverClient()).request(OWNER,'/naver/search',{'source':'news','query':'q'})

    async def test_fixed_transport_payload(self):
        def handler(request):
            self.assertEqual(str(request.url),BASE_URL+'/naver/search')
            self.assertEqual(request.method,'POST')
            self.assertEqual(request.headers['X-API-Key'],'test-key')
            self.assertNotIn('X-Hyemi-Owner',request.headers)
            self.assertEqual(json.loads(request.content),{'source':'news','query':'q'})
            return httpx.Response(200,json={'ok':True})
        self.assertEqual(await self.request(handler),{'ok':True})

    async def test_statuses_sanitized_no_retry(self):
        for status,category in [(301,'provider'),(307,'provider'),(401,'auth'),(403,'auth'),(429,'rate'),(400,'validation'),(422,'validation'),(500,'provider'),(503,'configuration')]:
            calls=[]
            def handler(request):
                calls.append(request)
                return httpx.Response(status,headers={'location':'https://evil.invalid'},text=CANARY)
            with self.assertRaises(NaverError) as ctx:
                await self.request(handler)
            self.assertEqual(ctx.exception.category,category)
            self.assertNotIn(CANARY,str(ctx.exception))
            self.assertEqual(len(calls),1)

    async def test_invalid_json_and_nonfinite(self):
        for data in (CANARY,'[]','null','{"a": NaN}','{"a": Infinity}','{"a":1e999}','['*5000):
            with self.assertRaises(NaverError) as ctx:
                await self.request(lambda request:httpx.Response(200,content=data))
            self.assertEqual(ctx.exception.category,'invalid-response')
            self.assertNotIn(CANARY,str(ctx.exception))

    async def test_echoed_private_values_rejected(self):
        for value in ('test-key', BASE_URL, '/run/secrets/utility_api_key'):
            with self.assertRaises(NaverError) as ctx:
                await self.request(lambda request:httpx.Response(200,json={'data':{'title':value}}))
            self.assertEqual(ctx.exception.category,'invalid-response')
            self.assertNotIn(value,str(ctx.exception))

    async def test_network_exception_safe(self):
        def handler(request): raise httpx.ConnectError(CANARY)
        with self.assertRaises(NaverError) as ctx: await self.request(handler)
        self.assertEqual(ctx.exception.category,'provider')
        self.assertNotIn(CANARY,str(ctx.exception))

    async def test_stream_size_limit(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(40): yield b'x'*8192
        with self.assertRaises(NaverError) as ctx:
            await self.request(lambda request:httpx.Response(200,stream=Stream()))
        self.assertEqual(ctx.exception.category,'size')

    async def test_timeout_and_total_deadline(self):
        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200,json={})
        client=NaverClient(); client.deadline=0.01
        with self.assertRaises(NaverError) as ctx: await self.request(slow,client)
        self.assertEqual(ctx.exception.category,'timeout')
        self.assertEqual(client.semaphore._value,4)
        def timeout(request): raise httpx.ReadTimeout(CANARY)
        with self.assertRaises(NaverError) as ctx: await self.request(timeout)
        self.assertEqual(ctx.exception.category,'timeout')
        self.assertNotIn(CANARY,str(ctx.exception))

    async def test_route_request_bounds(self):
        with patch('naver_client.load_key') as key:
            for route,body in [('https://evil.invalid',{}),('/naver/health',{}),('/naver/search',{'q':'x'*40000}),('/naver/search',{'q':float('nan')})]:
                with self.assertRaises(NaverError): await NaverClient().request(OWNER,route,body)
            key.assert_not_called()

    async def test_missing_key_lazy(self):
        server=MCPServer('test')
        with patch('naver_client.Path.open',side_effect=FileNotFoundError(CANARY)):
            client=register_naver_tools(server,{},lambda ctx:OWNER)
            result=await server._tool_manager.get_tool('naver_search_news').fn(query='q')
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['category'],'configuration')
        self.assertNotIn(CANARY,str(result))

    async def test_concurrency_and_cancellation(self):
        active=0; peak=0; ready=asyncio.Event(); release=asyncio.Event()
        async def handler(request):
            nonlocal active,peak
            active+=1; peak=max(peak,active)
            if active==4: ready.set()
            try:
                await release.wait()
                return httpx.Response(200,json={})
            finally: active-=1
        client=NaverClient(); real=httpx.AsyncClient
        with patch('naver_client.load_key',return_value='test'),patch('naver_client.httpx.AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),**kw)):
            tasks=[asyncio.create_task(client.request(OWNER,'/naver/search',{})) for _ in range(8)]
            await asyncio.wait_for(ready.wait(),1)
            tasks[0].cancel()
            with self.assertRaises(asyncio.CancelledError): await tasks[0]
            release.set()
            await asyncio.gather(*tasks[1:])
        self.assertEqual(peak,4)
        self.assertEqual(client.semaphore._value,4)
        self.assertEqual(active,0)

    async def test_deadline_includes_json_decode(self):
        import time
        real_loads=json.loads
        def slow_loads(data):
            time.sleep(0.03)
            return real_loads(data)
        client=NaverClient(); client.deadline=0.02
        with patch('naver_client.json.loads',side_effect=slow_loads):
            with self.assertRaises(NaverError) as ctx:
                await self.request(lambda request:httpx.Response(200,content=b'{}'),client)
        self.assertEqual(ctx.exception.category,'timeout')

    async def test_deadline_includes_queue(self):
        client=NaverClient(); client.deadline=0.01
        for _ in range(4): await client.semaphore.acquire()
        with patch('naver_client.load_key') as key:
            with self.assertRaises(NaverError) as ctx: await client.request(OWNER,'/naver/search',{})
            self.assertEqual(ctx.exception.category,'timeout'); key.assert_not_called()
        for _ in range(4): client.semaphore.release()

class EchoBoundaryTests(unittest.IsolatedAsyncioTestCase):
    KEYS = ('fixture-plain-key', 'fixture-quote-"-key',
            'fixture-backslash-\\-key', 'fixture-both-"-\\-key')
    LOCATIONS = ('title', 'link', 'description', 'unknown', 'nested', 'dictionary-key')

    def response(self):
        _, body = payload('naver_search_news', {'query':'q'})
        response = response_for(body)
        response['data'].update(total=1, display=1, items=[{
            'title':'한글 "quoted" \\ title', 'link':'https://example.test/문서',
            'description':'日本語 café 😀 /public/data/'}])
        response['unknown'] = {'일반 키': ['benign \\ " text', 1, True, None]}
        return response

    async def call_handler(self, key, response, escaped):
        import io
        wire = json.dumps(response, ensure_ascii=False)
        if escaped:
            # Escape every decoded character, including dictionary keys and slashes.
            def encode(value):
                if isinstance(value, str):
                    return '"' + ''.join('\\u%04x' % ord(c) if ord(c) < 65536
                                         else json.dumps(c)[1:-1] for c in value) + '"'
                if isinstance(value, dict):
                    return '{' + ','.join(encode(k)+':'+encode(v) for k,v in value.items()) + '}'
                if isinstance(value, list):
                    return '[' + ','.join(map(encode, value)) + ']'
                return json.dumps(value)
            wire = encode(response)
        self.assertEqual(json.loads(wire), response)
        real = httpx.AsyncClient
        calls = []
        def upstream(request):
            calls.append(request)
            self.assertEqual(request.headers['X-API-Key'], key)
            self.assertEqual(str(request.url), BASE_URL+'/naver/search')
            return httpx.Response(200, content=wire.encode('utf-8'))
        server = MCPServer('offline-echo-regression')
        register_naver_tools(server, {}, lambda ctx: OWNER)
        with patch('naver_client.Path.open', side_effect=lambda *a, **kw: io.BytesIO(key.encode('ascii'))), \
             patch('naver_client.httpx.AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(upstream), **kw)):
            result = await server._tool_manager.get_tool('naver_search_news').fn(query='q')
        self.assertEqual(len(calls), 1)
        return result

    def inject(self, response, location, value):
        if location in ('title', 'link', 'description'):
            response['data']['items'][0][location] = value
        elif location == 'unknown':
            response['unknown'] = value
        elif location == 'nested':
            response['unknown'] = {'outer': [0, {'inner': [value]}]}
        else:
            response['unknown'] = {'outer': [{value: 'benign'}]}

    def assert_rejected(self, result, key):
        expected = {'error': {'category':'invalid-response',
                              'message':'NAVER provider returned an invalid response.'}}
        wire = result.model_dump(by_alias=True)
        # Check both result forms even when the old implementation returns success.
        with self.subTest(surface='structured'):
            self.assertEqual(wire['structuredContent'], expected)
        with self.subTest(surface='text'):
            self.assertEqual(json.loads(wire['content'][0]['text']), expected)
        with self.subTest(surface='serialized'):
            serialized = result.model_dump_json(by_alias=True)
            for private in (key, BASE_URL, 'utility-fastapi-server', '/run/secrets/'):
                self.assertNotIn(private, serialized)
                self.assertNotIn(json.dumps(private)[1:-1], serialized)
        with self.subTest(surface='isError'):
            self.assertIs(wire['isError'], True)

    async def test_credential_echoes_fail_closed(self):
        for key in self.KEYS:
            for location in self.LOCATIONS:
                for embedded in (False, True):
                    for escaped in (False, True):
                        with self.subTest(key=key, location=location, embedded=embedded, escaped=escaped):
                            response = self.response()
                            self.inject(response, location, 'prefix '+key+' suffix' if embedded else key)
                            self.assert_rejected(await self.call_handler(key, response, escaped), key)

    async def test_private_markers_fail_closed(self):
        key = self.KEYS[-1]
        for marker in (BASE_URL, 'utility-fastapi-server', '/run/secrets/'):
            for location in self.LOCATIONS:
                for embedded in (False, True):
                    for escaped in (False, True):
                        with self.subTest(marker=marker, location=location, embedded=embedded, escaped=escaped):
                            response = self.response()
                            self.inject(response, location, 'prefix '+marker+' suffix' if embedded else marker)
                            self.assert_rejected(await self.call_handler(key, response, escaped), key)

    async def test_benign_special_character_keys_preserve_provider_data(self):
        for key in self.KEYS:
            for escaped in (False, True):
                with self.subTest(key=key, escaped=escaped):
                    response = self.response()
                    result = await self.call_handler(key, response, escaped)
                    wire = result.model_dump(by_alias=True)
                    self.assertIs(wire['isError'], False)
                    self.assertEqual(wire['structuredContent']['items'], response['data']['items'])
                    self.assertEqual(json.loads(wire['content'][0]['text']), wire['structuredContent'])
                    self.assertNotIn('unknown', wire['structuredContent'])


class WrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_first_missing_context(self):
        def owner(ctx):
            if ctx is None: raise PermissionError(CANARY)
            return OWNER
        server=MCPServer('test'); client=register_naver_tools(server,{},owner)
        with patch.object(client,'request',new_callable=AsyncMock) as request:
            result=await server._tool_manager.get_tool('naver_search_news').fn(query=None)
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content['error']['category'],'auth')
            request.assert_not_called()
            self.assertNotIn(CANARY,str(result))

    async def test_wrapper_validation_is_error(self):
        server=MCPServer('test'); client=register_naver_tools(server,{},lambda ctx:OWNER)
        with patch.object(client,'request',new_callable=AsyncMock) as request:
            result=await server._tool_manager.get_tool('naver_search_news').fn(query='q',display=True)
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content['error']['category'],'validation')
            request.assert_not_called()

    async def test_wrapper_exception_sanitization(self):
        server=MCPServer('test'); client=register_naver_tools(server,{},lambda ctx:OWNER)
        with patch.object(client,'request',side_effect=RuntimeError(CANARY)):
            result=await server._tool_manager.get_tool('naver_search_news').fn(query='q')
            self.assertTrue(result.is_error)
            self.assertNotIn(CANARY,str(result))

# A separately counted async test for every operation exercises real SDK registration,
# context injection, strict nested argument parsing, fixed payload, and empty evidence.
def operation_test(name):
    async def test(self):
        marker=object()
        def owner(ctx):
            self.assertIs(ctx,marker)
            return OWNER
        server=MCPServer('test'); client=register_naver_tools(server,{},owner)
        args=arguments(name); route,body=payload(name,args)
        with patch.object(client,'request',new_callable=AsyncMock,return_value=response_for(body)) as request:
            tool=server._tool_manager.get_tool(name)
            tool.fn_metadata.arg_model.model_validate(args,strict=True,extra='forbid')
            result=await tool.run(args,marker)
            request.assert_awaited_once_with(OWNER,route,body)
        self.assertFalse(result.is_error)
        out=result.structured_content
        self.assertEqual(out['operation'],name)
        self.assertIn('fetched_at',out)
        self.assertIn('cache_hit',out)
        if route=='/naver/search':
            self.assertEqual(body['source'],OPERATIONS[name][2])
            self.assertEqual(out['query'],'test query')
            if body['source']=='local': self.assertEqual(body['start'],1)
            if body['source'] not in ('adult','errata'):
                self.assertEqual(out['items'],[]); self.assertEqual(out['returned_count'],0)
        else:
            self.assertNotIn('start_date',body)
            self.assertEqual(body['startDate'],'2024-01-01')
            if route=='/naver/keyword-trend':
                self.assertEqual(body['keywordGroups'],[{'groupName':'test','keywords':['coffee']}])
            else: self.assertEqual(body['mode'],OPERATIONS[name][2])
            self.assertEqual(out['results'],[])
    return test

for _name in OPERATIONS:
    setattr(WrapperTests,'test_operation_'+_name,operation_test(_name))


class RevisionEvidenceTests(unittest.TestCase):
    def test_calendar_edges_and_strict_envelope_pairs(self):
        from naver_contracts import bucket_bounds
        cases=[('naver_keyword_trend','2016-01-01','2016-01-03','week','2015-12-28','2016-01-03'),
               ('naver_shopping_category_trend','2017-08-01','2017-08-02','week','2017-07-31','2017-08-06'),
               ('naver_keyword_trend','2024-02-13','2024-02-27','month','2024-02-01','2024-02-29'),
               ('naver_keyword_trend','2023-12-31','2024-01-02','week','2023-12-25','2024-01-07'),
               ('naver_keyword_trend','2023-12-31','2024-01-02','month','2023-12-01','2024-01-31'),
               ('naver_keyword_trend','2024-02-29','2024-03-01','date','2024-02-29','2024-03-01')]
        for name,start,end,unit,lo,hi in cases:
            with self.subTest(start=start,unit=unit):
                _,body=payload(name,dict(arguments(name),start_date=start,end_date=end,time_unit=unit))
                self.assertEqual(bucket_bounds(start,end,unit),(lo,hi))
                for left,right in {(start,end),(lo,hi)}:
                    r=response_for(body); r['data'].update(startDate=left,endDate=right,results=[dict(title='x',data=[dict(period=lo,ratio=1)])])
                    out=evidence(name,body,r)
                    self.assertEqual((out['startDate'],out['endDate']),(left,right))
                    self.assertEqual((out['bucket_start_date'],out['bucket_end_date']),(lo,hi))
                if start!=lo and end!=hi:
                    for left,right in [(start,hi),(lo,end)]:
                        r=response_for(body); r['data'].update(startDate=left,endDate=right)
                        with self.assertRaises(NaverError): evidence(name,body,r)
                for change in [dict(startDate=None),dict(endDate=True),dict(startDate='2024-2-01'),dict(endDate='2024-02-30'),dict(startDate='2024-01-01T00:00:00Z'),dict(startDate='2020-01-01',endDate='2020-01-02'),dict(timeUnit='other')]:
                    r=response_for(body); r['data'].update(change)
                    with self.assertRaises(NaverError): evidence(name,body,r)
                from datetime import date,timedelta
                for period in [(date.fromisoformat(lo)-timedelta(days=1)).isoformat(),(date.fromisoformat(hi)+timedelta(days=1)).isoformat()]:
                    r=response_for(body); r['data']['results']=[dict(title='x',data=[dict(period=period,ratio=1)])]
                    with self.assertRaises(NaverError): evidence(name,body,r)

    def test_synthetic_bucket_envelopes(self):
        from pathlib import Path
        samples=json.loads((Path(__file__).parent/'planning/naver-synthetic-bucket-responses.json').read_text())
        self.assertEqual(len(samples),6)
        for sample in samples:
            with self.subTest(operation=sample['operation'],unit=sample['arguments']['time_unit']):
                name=sample['operation']; _,body=payload(name,sample['arguments'])
                self.assertEqual(body,sample['request'])
                out=evidence(name,body,sample['synthetic_utility_response'])
                self.assertEqual(out['startDate'],sample['synthetic_utility_response']['data']['startDate'])
                self.assertEqual(out['endDate'],sample['synthetic_utility_response']['data']['endDate'])
                self.assertEqual(out['requested_start_date'],body['startDate'])
                self.assertEqual(out['requested_end_date'],body['endDate'])
                self.assertEqual(out['effective_start_date'],out['startDate'])
                self.assertEqual(out['effective_end_date'],out['endDate'])
                self.assertEqual(out['results'],sample['synthetic_utility_response']['data']['results'])

    def test_local_default_display_one_never_advances(self):
        name='naver_search_local'; _,body=payload(name,{'query':'q'})
        response=response_for(body); response['data'].update(items=[dict(title='x',link='x')],display=1,total=999999)
        p=evidence(name,body,response)['pagination']
        self.assertFalse(p['advancing_supported'])
        self.assertEqual((p['returned_start'],p['returned_end']),(1,1))
        self.assertEqual(p['remaining_indexed_matches'],999998)
        self.assertIsNone(p['next_start']); self.assertIsNone(p['next_arguments'])

    def test_pagination_navigation(self):
        for name in [n for n in OPERATIONS if n.startswith('naver_search_')]:
            local=name=='naver_search_local'
            for start,count,total in [(1,5,1000000),(1,2,100),(1,0,100),(1,5,5),(995,5,1000000),(996,5,1000000),(1000,5,1000000)]:
                if local and start!=1: continue
                args=dict(query='q',display=5)
                if not local: args['start']=start
                _,body=payload(name,args); response=response_for(body)
                response['data'].update(items=[dict(title='x',link='x')]*count,display=count,total=total)
                out=evidence(name,body,response); p=out['pagination']
                self.assertEqual(p['allowed_start_min'],1)
                self.assertEqual(p['allowed_start_max'],1 if local else 1000)
                self.assertEqual(p['allowed_display_max'],5 if local else 30)
                self.assertEqual(p['returned_start'],start if count else None)
                self.assertEqual(p['returned_end'],start+count-1 if count else None)
                expected=not local and count==5 and start+count<=1000 and start+count<=total
                self.assertEqual(p['next_start'],start+count if expected else None)
                if expected:
                    _,suggested=payload(name,p['next_arguments'])
                    self.assertEqual(suggested,dict(body,start=start+count))
                    self.assertFalse(set(p['next_arguments']) & {'source','mode','ctx','owner'})
                else: self.assertIsNone(p['next_arguments'])
        name='naver_search_web'; _,body=payload(name,dict(query='q',start=1000,display=30))
        response=response_for(body); response['data'].update(items=[dict(title='x',link='x')]*30,display=30,total=999999)
        out=evidence(name,body,response)
        self.assertEqual(out['pagination']['returned_end'],1029)
        self.assertIsNone(out['pagination']['next_start'])

if __name__ == '__main__': unittest.main()
