"""Signed-fixture JWT protocol integration; no real utility or handoff calls."""
import copy
import json
import unittest
from unittest.mock import patch, AsyncMock
import anyio
import httpx
import bridge
import test_bridge as legacy
from hyemi_contracts import owner_id

def backend_response(route, body):
    if route=='/naver/search':
        source=body['source']
        data={source:'커피' if source=='errata' else '0'} if source in {'errata','adult'} else dict(total=1,start=body['start'],display=1,lastBuildDate='fixture',items=[dict(title='커피',link='https://example.test',description='fixture')])
        return dict(cache_hit=False,source=source,query=body['query'],data=data)
    data={key:body[key] for key in ['startDate','endDate','timeUnit']}
    row=dict(period=body['startDate'],ratio=50.0)
    dimension=body.get('mode','').rsplit('_',1)[-1]
    if dimension in {'device','gender','age'}: row['group']={'device':'pc','gender':'f','age':'20'}[dimension]
    data['results']=[dict(title='커피',data=[row])]
    result=dict(cache_hit=True,relative_values=True,data=data)
    if 'mode' in body: result['mode']=body['mode']
    return result

SEARCH = dict(web='webkr', news='news', blog='blog', cafe='cafearticle', kin='kin', local='local', image='image', encyclopedia='encyc')
MODES = dict(category='categories', category_device='category_device', category_gender='category_gender', category_age='category_age', keyword='keywords', keyword_device='keyword_device', keyword_gender='keyword_gender', keyword_age='keyword_age')
LEGACY = {'deliver_report','request_review','request_task','get_work_status','get_work_updates','get_handoff_result','get_report','send_handoff'}

def samples():
    result = {'naver_search_'+name: {'query':'정자동 카페' if name=='local' else '커피'} for name in SEARCH}
    result.update(naver_convert_keyboard_query={'query':'zjavl'},naver_check_adult_query={'query':'coffee'})
    dates = dict(start_date='2026-09-20',end_date='2026-09-26',time_unit='date')
    result['naver_keyword_trend'] = dict(dates,keyword_groups=[dict(group_name='커피',keywords=['커피'])])
    for suffix, mode in MODES.items():
        args = dict(dates,category='50000006')
        if mode=='categories': args['category']=[dict(name='식품',param=['50000006'])]
        if mode=='keywords': args['keyword']=[dict(name='커피',param=['커피'])]
        elif mode.startswith('keyword_'): args['keyword']='커피'
        result['naver_shopping_'+suffix+'_trend']=args
    return result

class NaverProtocolTests(unittest.TestCase):
    setUpClass=classmethod(legacy.BridgeTests.setUpClass.__func__)
    token=legacy.BridgeTests.token
    verifier=legacy.BridgeTests.verifier

    def run_rpc(self, check, *, unguarded=False):
        async def run():
            app=bridge.build_app(legacy.CFG,self.verifier())
            async with app.app.router.lifespan_context(app.app):
                target=app.app if unguarded else app
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=target),base_url='http://bridge.example.invalid') as c:
                    headers={'Accept':'application/json, text/event-stream','Cf-Access-Jwt-Assertion':self.token()}
                    async def rpc(method,params,override=None):
                        r=await c.post('/mcp',headers=headers if override is None else override,json=dict(jsonrpc='2.0',id=1,method=method,params=params))
                        return r
                    init=await rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='naver-test',version='1')))
                    self.assertEqual(init.status_code,200)
                    await check(rpc)
        anyio.run(run)

    def test_synthetic_week_month_envelopes_through_signed_mcp(self):
        from pathlib import Path
        from naver_contracts import payload
        captures=json.loads((Path(__file__).parent/'planning/naver-synthetic-bucket-responses.json').read_text())
        async def check(rpc):
            with patch('naver_client.NaverClient.request',new_callable=AsyncMock) as backend:
                for sample in captures:
                    backend.return_value=sample['synthetic_utility_response']
                    result=(await rpc('tools/call',dict(name=sample['operation'],arguments=sample['arguments']))).json()['result']
                    self.assertFalse(result['isError'],result)
                    data=result['structuredContent']
                    self.assertEqual(json.loads(result['content'][0]['text']),data)
                    self.assertEqual(backend.call_args.args[1:],payload(sample['operation'],sample['arguments']))
                    self.assertEqual(data['requested_start_date'],sample['arguments']['start_date'])
                    self.assertEqual(data['requested_end_date'],sample['arguments']['end_date'])
                    for key in ['startDate','endDate','timeUnit','results']:
                        self.assertEqual(data[key],sample['synthetic_utility_response']['data'][key])
                    self.assertEqual(data['effective_start_date'],data['startDate'])
                    self.assertEqual(data['effective_end_date'],data['endDate'])
                self.assertEqual(backend.call_count,6)
        self.run_rpc(check)

    def test_exact_inventory_closed_schema_and_annotations(self):
        async def check(rpc):
            tools=(await rpc('tools/list',{})).json()['result']['tools']
            self.assertEqual({t['name'] for t in tools},LEGACY|set(samples()))
            self.assertEqual(len(tools),27)
            def closed(node):
                if isinstance(node,dict):
                    if node.get('type')=='object': self.assertIs(node.get('additionalProperties'),False,node)
                    for value in node.values(): closed(value)
                elif isinstance(node,list):
                    for value in node: closed(value)
            for tool in tools:
                if tool['name'] not in samples(): continue
                closed(tool['inputSchema'])
                self.assertFalse(set(tool['inputSchema']['properties']) & {'source','mode','owner','session','url','path','key','header','ctx','token','context'})
                self.assertIs(tool['annotations']['readOnlyHint'],True)
                self.assertIs(tool['annotations']['destructiveHint'],False)
                self.assertIs(tool['annotations']['idempotentHint'],True)
                self.assertIs(tool['annotations']['openWorldHint'],True)
        self.run_rpc(check)

    def test_auth_rejected_before_backend(self):
        async def check(rpc):
            for token in ['', 'invalid',self.token(email='wrong@example.test'),self.token(aud='wrong'),self.token(iss='https://wrong.cloudflareaccess.com')]:
                headers={'Accept':'application/json, text/event-stream','Cf-Access-Jwt-Assertion':token}
                r=await rpc('tools/call',dict(name='naver_search_web',arguments={'query':'coffee'}),headers)
                self.assertEqual(r.status_code,401)
        with patch('naver_client.NaverClient.request',new_callable=AsyncMock) as backend:
            self.run_rpc(check)
            backend.assert_not_called()

    def test_invalid_arguments_never_reach_backend(self):
        async def check(rpc):
            with patch('bridge.dispatch') as handoff,patch('bridge.v2_request') as workflow,patch('naver_client.NaverClient.request',new_callable=AsyncMock) as backend:
                for name,args in samples().items():
                    bads=[dict(args,**{field:'forged'}) for field in ['source','mode','owner','url','token','key','context','ctx','session','path','header']]
                    if 'query' in args: bads += [dict(args,query=' '),dict(args,query=123)]
                    if name.startswith('naver_search_'): bads += [dict(args,display='2'),dict(args,display=True),dict(args,display=2.0)]
                    if 'trend' in name: bads += [dict(args,ages=[20])]
                    for field in ['keyword_groups','category','keyword']:
                        if isinstance(args.get(field),list):
                            bad=copy.deepcopy(args); bad[field][0]['unknown']='bad'; bads.append(bad)
                            bad=copy.deepcopy(args)
                            nested='keywords' if field=='keyword_groups' else 'param'
                            bad[field][0][nested]=[123]; bads.append(bad)
                    for bad in bads:
                        r=(await rpc('tools/call',dict(name=name,arguments=bad))).json()
                        self.assertTrue(r['result']['isError'],(name,bad,r))
                handoff.assert_not_called(); workflow.assert_not_called(); backend.assert_not_called()
        self.run_rpc(check)

    def test_every_valid_tool_payload_and_no_handoff(self):
        async def check(rpc):
            from naver_contracts import payload
            async def response(owner,route,body):
                self.assertEqual(owner,owner_id(self.auth['issuer'],'one-user'))
                return backend_response(route,body)
            with patch('naver_client.NaverClient.request',side_effect=response) as backend,patch('bridge.dispatch') as handoff,patch('bridge.v2_request') as workflow:
                for name,args in samples().items():
                    result=(await rpc('tools/call',dict(name=name,arguments=args))).json()['result']
                    self.assertFalse(result['isError'],(name,result))
                    data=result['structuredContent']
                    self.assertEqual(data['operation'],name)
                    self.assertEqual(backend.call_args.args[1:],payload(name,args))
                    route,sent=backend.call_args.args[1:]
                    if name.startswith('naver_search_'):
                        self.assertEqual(route,'/naver/search')
                        self.assertEqual(sent['source'],SEARCH[name.removeprefix('naver_search_')])
                        self.assertEqual(sent['start'],1)
                    elif name.startswith('naver_shopping_'):
                        self.assertEqual(route,'/naver/shopping-trend')
                        self.assertEqual(sent['mode'],MODES[name.removeprefix('naver_shopping_').removesuffix('_trend')])
                    elif name=='naver_keyword_trend':
                        self.assertEqual(route,'/naver/keyword-trend')
                        self.assertEqual(sent['keywordGroups'][0]['groupName'],'커피')
                    else:
                        self.assertEqual(sent,dict(query=args['query'],source='errata' if name=='naver_convert_keyboard_query' else 'adult'))
                    if 'trend' in name:
                        self.assertIs(data['relative_values'],True)
                        self.assertEqual(data['startDate'],args['start_date'])
                        self.assertEqual(data['results'][0]['data'][0]['ratio'],50.0)
                    elif name.startswith('naver_search_'):
                        self.assertEqual(data['returned_count'],1)
                        self.assertEqual(data['items'][0]['title'],'커피')
                self.assertEqual(backend.call_count,19)
                handoff.assert_not_called(); workflow.assert_not_called()
        self.run_rpc(check)

    def test_missing_guard_principal_rejected_before_backend(self):
        async def check(rpc):
            with patch('naver_client.NaverClient.request',new_callable=AsyncMock) as backend:
                result=(await rpc('tools/call',dict(name='naver_search_web',arguments={'query':'coffee'}))).json()['result']
                self.assertTrue(result['isError'])
                backend.assert_not_called()
        self.run_rpc(check,unguarded=True)

    def test_missing_key_does_not_disable_workflow_tools(self):
        async def check(rpc):
            from naver_client import NaverError
            from uuid import uuid4
            with patch('naver_client.load_key',side_effect=NaverError('configuration')),patch('bridge.v2_request',return_value={'status':'not_found'}) as workflow:
                failed=(await rpc('tools/call',dict(name='naver_search_web',arguments={'query':'coffee'}))).json()['result']
                self.assertTrue(failed['isError'])
                called=(await rpc('tools/call',dict(name='get_work_status',arguments={'work_id':str(uuid4())}))).json()['result']
                self.assertFalse(called['isError'])
                workflow.assert_called_once()
        self.run_rpc(check)

class CanaryHarnessTests(unittest.TestCase):
    def test_all_canary_samples_match_real_contracts_and_output(self):
        from planning.run_naver_canaries import samples as canary_samples, inspect_data
        from naver_contracts import OPERATIONS,payload
        from naver_tools import evidence
        cases=canary_samples()
        self.assertEqual(set(cases),set(OPERATIONS))
        self.assertEqual(len(cases),19)
        for name,args in cases.items():
            route,body=payload(name,args)
            normalized=evidence(name,body,backend_response(route,body))
            checks,meta=inspect_data(name,normalized,args)
            self.assertTrue(all(checks.values()),(name,checks))
            broken=dict(normalized,cache_hit='true')
            self.assertFalse(all(inspect_data(name,broken,args)[0].values()))

    def test_canary_error_summary_never_echoes_unknown_errors(self):
        from planning.run_naver_canaries import error_summary
        from naver_client import MESSAGES
        result=error_summary({'structuredContent':{'error':{'category':'provider','message':MESSAGES['provider']}}})
        self.assertEqual(result['error']['message'],MESSAGES['provider'])
        result=error_summary({'content':[{'type':'text','text':'sensitive opaque value'}]})
        self.assertEqual(result['sanitized_errors'],[])
        self.assertEqual(result['withheld_error_count'],1)

if __name__=='__main__': unittest.main()
