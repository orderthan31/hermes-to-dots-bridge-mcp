"""Offline approved-source and canary gate regressions; never execute Docker.
Initial combined RED: 5 tests, 10 errors (6 real envelopes, pagination, 3 helpers).
"""
import copy
import unittest
from planning import run_naver_canaries as canary

class ManifestTests(unittest.TestCase):
    def manifest(self):
        return dict(schema_version=2,required_modules={name:'a'*64 for name in canary.REQUIRED_MODULES},host_artifacts={'planning/run_naver_canaries.py':'b'*64},expected_image_ref='local/hermes-bridge-mcp:0.4.0',expected_image_id='sha256:'+'c'*64)

    def test_valid_and_exact_required_set(self):
        good=self.manifest()
        self.assertEqual(canary.validate_manifest(good),good)
        for bad in [dict(good,schema_version=1),dict(good,required_modules={}),dict(good,required_modules=dict(good['required_modules'],unknown='a'*64))]:
            with self.assertRaises(ValueError): canary.validate_manifest(bad)
        for value in ['', 'z'*64, 'a'*63, None]:
            bad=copy.deepcopy(good); bad['required_modules']['bridge.py']=value
            with self.assertRaises(ValueError): canary.validate_manifest(bad)
        bad=copy.deepcopy(good); del bad['required_modules']['bridge.py']
        with self.assertRaises(ValueError): canary.validate_manifest(bad)

    def test_hashes_and_stale_tag_matching_gate(self):
        manifest=self.manifest(); actual=dict(manifest['required_modules'])
        self.assertTrue(canary.compare_hashes(manifest['required_modules'],actual)['match'])
        actual['naver_tools.py']='b'*64
        self.assertFalse(canary.compare_hashes(manifest['required_modules'],actual)['match'])
        del actual['bridge.py']
        self.assertFalse(canary.compare_hashes(manifest['required_modules'],actual)['match'])
        evidence=dict(ok=True,container_probe_exit=0,public_unauthenticated_status=401,no_published_ports=True,network_attached=True,image_matches_release_tag=True,image_tag='sha256:'+'c'*64,image_reference_matches=True,expected_image_ref='local/hermes-bridge-mcp:0.4.0',approved_source_match=True,approved_image_match=True)
        self.assertTrue(canary.release_success(evidence))
        evidence['approved_source_match']=False
        self.assertFalse(canary.release_success(evidence))

    def test_all_trend_variants(self):
        from naver_contracts import OPERATIONS,payload
        variants=canary.trend_variants()
        self.assertEqual(len(variants),18)
        self.assertEqual({(n,a['time_unit']) for n,a in variants},{(n,u) for n,v in OPERATIONS.items() if v[1]!='/naver/search' for u in ('week','month')})
        for n,a in variants: payload(n,a)

    def test_manifest_pins_and_closed_fields(self):
        valid=dict(self.manifest(),host_artifacts={'planning/run_naver_canaries.py':'b'*64},expected_image_id='sha256:'+'c'*64)
        self.assertEqual(canary.validate_manifest(valid),valid)
        for changes in [dict(host_artifacts={'../private':'a'*64}),dict(host_artifacts={'bridge.py':'b'*64}),dict(expected_image_id='tag'),dict(expected_image_id=None),dict(unreviewed=True),dict(schema_version=True),dict(host_artifacts={}),dict(expected_image_ref='local/hermes-bridge-mcp:0.3.0'),dict(expected_image_ref='sha256:'+'c'*64)]:
            with self.assertRaises(ValueError): canary.validate_manifest(dict(self.manifest(),**changes))

    def run_mocked_main(self, *, stale=False, host_changed=False, script_changed=False, missing=False, pinned_wrong=False, tag_drift=False, tag_launch=False, wrong_ref=False, omit=None, public_status=401):
        # Entirely offline: normal unit mocks, no Docker, keys, config or network.
        import hashlib,json
        from types import SimpleNamespace
        from unittest.mock import patch,Mock
        from pathlib import Path
        h=hashlib.sha256(b'reviewed').hexdigest()
        manifest=dict(schema_version=2,required_modules={n:h for n in canary.REQUIRED_MODULES},host_artifacts={'planning/run_naver_canaries.py':h},expected_image_ref='local/hermes-bridge-mcp:0.4.0',expected_image_id='sha256:'+'c'*64)
        if omit: manifest.pop(omit)
        if missing: del manifest['required_modules']['bridge.py']
        if pinned_wrong: manifest['expected_image_id']='sha256:'+'f'*64
        actual={n:h for n in canary.REQUIRED_MODULES}
        if stale: actual['naver_tools.py']='b'*64
        obj=dict(Image='sha256:'+'c'*64,Config={'Image':'local/hermes-bridge-mcp:0.3.0' if wrong_ref else ('local/hermes-bridge-mcp:0.4.0' if tag_launch else 'sha256:'+'c'*64)},NetworkSettings={'Ports':{},'Networks':{'hermes-bridge-edge':{}}})
        replies=[SimpleNamespace(stdout=json.dumps([obj])),SimpleNamespace(stdout='sha256:'+'d'*64 if tag_drift else obj['Image']),SimpleNamespace(stdout=json.dumps(dict(ok=True,source_hashes=actual)),returncode=0)]
        def read_bytes(path):
            if path.name=='approved.json': return json.dumps(manifest).encode()
            if host_changed and path.name=='naver_tools.py': return b'changed'
            if script_changed and path.name=='run_naver_canaries.py': return b'changed'
            return b'reviewed'
        from urllib.error import HTTPError
        opener=Mock()
        opener.open.side_effect=HTTPError('https://bridge.example.invalid/mcp',public_status,'mock unauthenticated response',{},None)
        with patch.object(canary.sys,'argv',['canary','--execute','--container','test','--manifest','approved.json','--output','evidence.json']),patch.object(Path,'read_bytes',read_bytes),patch.object(Path,'read_text',return_value='reviewed script'),patch.object(Path,'write_text') as write,patch.object(Path,'mkdir'),patch.object(canary.subprocess,'run',side_effect=replies) as docker,patch('urllib.request.build_opener',return_value=opener),patch('builtins.print'):
            code=canary.main()
            recorded=json.loads(write.call_args.args[0])
        return code,recorded,docker,opener

    def test_main_matching_manifest_offline(self):
        code,e,docker,_=self.run_mocked_main()
        self.assertEqual(code,0)
        self.assertTrue(e['approved_source_match'])
        self.assertEqual(set(e['container_source_verification']['files']),canary.REQUIRED_MODULES)
        self.assertFalse(e['authenticated_client_receipt'])
        self.assertEqual(docker.call_count,3)
        self.assertIn('--manifest-json',docker.call_args.args[0])
        self.assertEqual(docker.call_args.kwargs['input'],'reviewed')
        self.assertEqual(docker.call_args_list[1].args[0][3],'local/hermes-bridge-mcp:0.4.0')
        self.assertEqual(self.run_mocked_main(tag_launch=True)[0],0)

    def test_main_public_request_identity_and_strict_401_gate(self):
        for status in (200,301,302,401,403,500):
            with self.subTest(status=status):
                code,e,_,opener=self.run_mocked_main(public_status=status)
                opener.open.assert_called_once()
                request=opener.open.call_args.args[0]
                self.assertEqual(request.get_header('User-agent'),'HermesBridgeMCP/0.4.0')
                self.assertEqual(request.full_url,'https://bridge.example.invalid/mcp')
                self.assertEqual(request.get_method(),'POST')
                self.assertFalse(request.has_header('Authorization'))
                self.assertFalse(request.has_header('Cf-access-jwt-assertion'))
                self.assertEqual(e['public_unauthenticated_status'],status)
                self.assertEqual(code,0 if status==401 else 1)
                self.assertEqual(e['ok'],status==401)
                self.assertFalse(e['authenticated_client_receipt'])

    def test_main_stale_image_despite_matching_tag(self):
        code,e,_,_=self.run_mocked_main(stale=True)
        self.assertEqual(code,1)
        self.assertTrue(e['image_matches_release_tag'])
        self.assertFalse(e['approved_source_match'])
        self.assertFalse(e['container_source_verification']['files']['naver_tools.py']['match'])

    def test_main_bad_approval_never_executes_probe(self):
        for change in ['host_changed','script_changed','missing','pinned_wrong','tag_drift','wrong_ref']:
            with self.subTest(change=change):
                code,e,docker,_=self.run_mocked_main(**{change:True})
                self.assertEqual(code,1)
                self.assertFalse(e['ok'])
                self.assertFalse(any('exec' in call.args[0] for call in docker.call_args_list))
                if change in {'host_changed','script_changed','missing'}: docker.assert_not_called()

    def test_synthetic_captures_and_independent_canary_range_checks(self):
        import json
        from pathlib import Path
        from naver_tools import evidence
        captures=json.loads((Path(__file__).parent/'planning/naver-synthetic-bucket-responses.json').read_text())
        for sample in captures:
            out=evidence(sample['operation'],sample['request'],sample['synthetic_utility_response'])
            checks,_=canary.inspect_data(sample['operation'],out,sample['arguments'])
            self.assertTrue(all(checks.values()),checks)
            for key,value in [('requested_start_date','2000-01-01'),('effective_end_date','2000-01-01'),('bucket_start_date','2000-01-01'),('timeUnit','date'),('startDate','2026-08-04')]:
                broken=dict(out,**{key:value})
                self.assertFalse(all(canary.inspect_data(sample['operation'],broken,sample['arguments'])[0].values()),key)
            broken=copy.deepcopy(out); broken['results'][0]['data'][0]['period']='2026-08-04'
            self.assertFalse(all(canary.inspect_data(sample['operation'],broken,sample['arguments'])[0].values()))

    def test_all_variant_mock_outputs_checked(self):
        from naver_contracts import payload,bucket_bounds
        from naver_tools import evidence
        from test_naver_protocol import backend_response
        for name,args in canary.trend_variants():
            route,request=payload(name,args)
            mock=backend_response(route,request)
            lo,hi=bucket_bounds(request['startDate'],request['endDate'],request['timeUnit'])
            mock['data'].update(startDate=lo,endDate=hi)
            mock['data']['results'][0]['data'][0]['period']=lo
            out=evidence(name,request,mock)
            checks,_=canary.inspect_data(name,out,args)
            self.assertTrue(all(checks.values()),(name,args,checks))


    def test_missing_pins_rejected_before_docker(self):
        for field in ('host_artifacts','expected_image_id','expected_image_ref'):
            code,e,docker,_=self.run_mocked_main(omit=field)
            self.assertEqual(code,1)
            docker.assert_not_called()

class IsolatedInsideTests(unittest.TestCase):
    def test_real_inside_asgi_all_calls_with_only_external_io_mocked(self):
        import asyncio, hashlib, json
        from contextlib import ExitStack
        from pathlib import Path
        from unittest.mock import patch
        import httpx, bridge
        from test_bridge import CFG
        from test_naver_protocol import backend_response
        from naver_contracts import bucket_bounds
        requests=[]
        rpc_requests=[]
        async def record_rpc(request):
            rpc_requests.append(json.loads(request.content))
        original_client=httpx.AsyncClient
        def handler(request):
            requests.append(request)
            if request.url.host=='127.0.0.1':
                return httpx.Response(401 if request.url.path=='/mcp' else 200,json={})
            self.assertEqual(request.url.host,'utility-fastapi-server')
            if request.url.path=='/naver/health':
                return httpx.Response(200,json={'credentials':True})
            body=json.loads(request.content)
            result=backend_response(request.url.path,body)
            if request.url.path!='/naver/search':
                lo,hi=bucket_bounds(body['startDate'],body['endDate'],body['timeUnit'])
                result['data'].update(startDate=lo,endDate=hi)
                result['data']['results'][0]['data'][0]['period']=lo
            return httpx.Response(200,json=result)
        def client(*args,**kwargs):
            if 'transport' not in kwargs: kwargs['transport']=httpx.MockTransport(handler)
            else: kwargs['event_hooks']={'request':[record_rpc]}
            return original_client(*args,**kwargs)
        h=hashlib.sha256(b'reviewed').hexdigest()
        manifest=dict(schema_version=2,required_modules={n:h for n in canary.REQUIRED_MODULES},host_artifacts={'planning/run_naver_canaries.py':h},expected_image_ref='local/hermes-bridge-mcp:0.4.0',expected_image_id='sha256:'+'c'*64)
        with ExitStack() as stack:
            stack.enter_context(patch('bridge.load_config',return_value=copy.deepcopy(CFG)))
            stack.enter_context(patch.object(Path,'read_bytes',return_value=b'reviewed'))
            stack.enter_context(patch.object(Path,'read_text',return_value='synthetic-utility-key'))
            stack.enter_context(patch.object(Path,'is_file',return_value=True))
            stack.enter_context(patch.object(canary.os,'access',return_value=True))
            stack.enter_context(patch('naver_client.load_key',return_value='synthetic-utility-key'))
            stack.enter_context(patch('httpx.AsyncClient',side_effect=client))
            # inside installs its workflow tripwires; restore them after execution.
            for attr in ('dispatch','v2_request','submit_v2'):
                stack.enter_context(patch.object(bridge,attr))
            evidence=asyncio.run(canary.inside(manifest))
        self.assertTrue(evidence['ok'],evidence)
        self.assertFalse(evidence['authenticated_client_receipt'])
        self.assertEqual(evidence['proof'],canary.LABEL)
        self.assertEqual(rpc_requests[0]['method'],'initialize')
        self.assertEqual(rpc_requests[0]['params']['clientInfo']['version'],'0.4.0')
        self.assertEqual(rpc_requests[1]['method'],'tools/list')
        self.assertEqual(sum(r['method']=='tools/call' for r in rpc_requests),37)
        self.assertEqual(len(evidence['calls']),37)
        self.assertEqual(evidence['unique_naver_tool_count'],19)
        self.assertEqual(evidence['variant_call_count'],18)
        self.assertEqual(sum('direct_data_parity' in e['checks'] for e in evidence['calls']),7)
        self.assertEqual(sum(r.method=='POST' and r.url.host=='utility-fastapi-server' for r in requests),44)

    def test_changed_container_source_stops_before_config_or_network(self):
        import asyncio
        from pathlib import Path
        from unittest.mock import patch
        manifest=ManifestTests().manifest()
        with patch.object(Path,'read_bytes',return_value=b'wrong'), patch('bridge.load_config') as cfg, patch('httpx.AsyncClient') as http:
            result=asyncio.run(canary.inside(manifest))
        self.assertFalse(result['ok'])
        self.assertFalse(result['authenticated_client_receipt'])
        cfg.assert_not_called()
        http.assert_not_called()

    def test_fixture_contract_rejects_other_identities_through_real_guard(self):
        # Extract the actual nested fixture, not a duplicated imitation.
        import ast, asyncio, inspect, json
        import bridge, httpx
        tree=ast.parse(inspect.getsource(canary.inside))
        fixture=next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='OperatorFixtureVerifier')
        namespace={}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fixture],type_ignores=[])),'fixture','exec'),namespace)
        verifier=namespace['OperatorFixtureVerifier']()
        entered=[]
        async def inner(scope,receive,send):
            entered.append(dict(scope['state']))
            await bridge.GuardedApp.respond(send,200,{})
        async def run():
            app=bridge.GuardedApp(inner,verifier)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture.invalid') as c:
                for token in ('','wrong','isolated-operator-fixture-extra'):
                    r=await c.post('/mcp',headers={'Cf-Access-Jwt-Assertion':token})
                    self.assertEqual(r.status_code,401)
                self.assertEqual(entered,[])
                self.assertEqual((await c.get('/readyz')).status_code,200)
                r=await c.post('/mcp',headers={'Cf-Access-Jwt-Assertion':'isolated-operator-fixture'})
                self.assertEqual(r.status_code,200)
                self.assertEqual(entered[0]['principal_issuer'],'https://isolated-operator.invalid')
                self.assertEqual(entered[0]['principal_sub'],'naver-canary-operator')
                self.assertEqual(app.in_flight,0)
        asyncio.run(run())
