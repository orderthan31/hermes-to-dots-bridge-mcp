#!/usr/bin/env python3
"""Opt-in, isolated deployed-image ASGI RPC proof. NEVER a client OAuth receipt.

Host invocation: python planning/run_naver_canaries.py --execute --container NAME
Executes this source on stdin inside the existing container (no files installed).
Does not build, restart, mutate production auth, call workflows, or write Telegram.
"""
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

SEARCH = dict(web='webkr',news='news',blog='blog',cafe='cafearticle',kin='kin',local='local',image='image',encyclopedia='encyc')
MODES = dict(category='categories',category_device='category_device',category_gender='category_gender',category_age='category_age',keyword='keywords',keyword_device='keyword_device',keyword_gender='keyword_gender',keyword_age='keyword_age')
LEGACY = {'deliver_report','request_review','request_task','get_work_status','get_work_updates','get_handoff_result','get_report','send_handoff'}
RELEASE_VERSION = '0.4.0'
RELEASE_IMAGE_REF = 'local/hermes-bridge-mcp:0.4.0'
LABEL = 'ISOLATED DEPLOYED-IMAGE ASGI RPC proof'

REQUIRED_MODULES = frozenset({'bridge.py','hyemi_contracts.py','naver_tools.py','naver_contracts.py','naver_client.py'})
HOST_ARTIFACTS = REQUIRED_MODULES | {'planning/run_naver_canaries.py'}


def validate_manifest(manifest):
    import re
    if type(manifest) is not dict or set(manifest)!={'schema_version','required_modules','host_artifacts','expected_image_id','expected_image_ref'}:
        raise ValueError('invalid manifest fields')
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 2:
        raise ValueError('invalid manifest version')
    modules=manifest.get('required_modules')
    if type(modules) is not dict or set(modules)!=REQUIRED_MODULES:
        raise ValueError('exact required module set missing')
    host=manifest.get('host_artifacts',{})
    if type(host) is not dict or not set(host)<=HOST_ARTIFACTS or 'planning/run_naver_canaries.py' not in host:
        raise ValueError('invalid host artifact set')
    for value in [*modules.values(),*host.values()]:
        if type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value):
            raise ValueError('invalid sha256')
    if any(host[n]!=modules[n] for n in set(host)&REQUIRED_MODULES):
        raise ValueError('conflicting source hashes')
    image=manifest.get('expected_image_id')
    if (type(image) is not str or not re.fullmatch('sha256:[0-9a-f]{64}',image)):
        raise ValueError('invalid image ID')
    if manifest['expected_image_ref'] != RELEASE_IMAGE_REF:
        raise ValueError('invalid release image reference')
    return manifest


def compare_hashes(expected, actual):
    details={name:dict(expected=value,actual=actual.get(name),match=actual.get(name)==value) for name,value in sorted(expected.items())}
    return dict(match=set(actual)==set(expected) and all(v['match'] for v in details.values()),files=details)


def release_success(evidence):
    return bool(evidence.get('ok') and evidence.get('container_probe_exit')==0
                and evidence.get('public_unauthenticated_status')==401
                and all(evidence.get(k) is True for k in ('no_published_ports','network_attached','image_matches_release_tag','approved_source_match','approved_image_match','image_reference_matches'))
                and evidence.get('expected_image_ref')==RELEASE_IMAGE_REF)


def trend_variants():
    # Synthetic test interval: partial weeks AND partial months.
    return [(name,dict(args,start_date='2026-08-05',end_date='2026-09-24',time_unit=unit))
            for name,args in samples().items() if 'trend' in name for unit in ('week','month')]


def samples():
    end=dt.date.today()-dt.timedelta(days=4)
    dates=dict(start_date=(end-dt.timedelta(days=6)).isoformat(),end_date=end.isoformat(),time_unit='date')
    cases={'naver_search_'+name:dict(query='정자동 카페' if name=='local' else '커피') for name in SEARCH}
    cases.update(naver_convert_keyboard_query={'query':'zjavl'},naver_check_adult_query={'query':'coffee'})
    cases['naver_keyword_trend']=dict(dates,keyword_groups=[dict(group_name='커피',keywords=['커피'])])
    # Code verified in utility-fastapi-server/tests/test_naver_api_hub.py (식품).
    for suffix,mode in MODES.items():
        args=dict(dates,category='50000006')
        if mode=='categories': args['category']=[dict(name='식품',param=['50000006'])]
        if mode=='keywords': args['keyword']=[dict(name='커피',param=['커피'])]
        elif mode.startswith('keyword_'): args['keyword']='커피'
        cases['naver_shopping_'+suffix+'_trend']=args
    assert len(cases)==19
    return cases


def unpack(result):
    if result.get('structuredContent') is not None: return result['structuredContent']
    return json.loads(next(item['text'] for item in result['content'] if item['type']=='text'))


def error_summary(result):
    from naver_client import MESSAGES
    error=result.get('structuredContent',{}).get('error',{})
    if error.get('category') in MESSAGES and error.get('message')==MESSAGES[error['category']]:
        return {'isError':True,'error':{'category':error['category'],'message':error['message']}}
    # MCP core errors are sanitized already; allow only short plain errors, never
    # upstream bodies, arbitrary exception strings, URLs, headers or credentials.
    texts=[x.get('text','') for x in result.get('content',[]) if x.get('type')=='text']
    allowed=[text for text in texts if text in set(MESSAGES.values())|{'invalid tool arguments'}]
    return {'isError':True,'sanitized_errors':allowed,'withheld_error_count':len(texts)-len(allowed)}


def inspect_data(name, body, args):
    checks={'object':isinstance(body,dict)}
    if not isinstance(body,dict): return checks,{}
    checks['cache_metadata']=type(body.get('cache_hit')) is bool
    # Core returns a validated flattened evidence object, not utility's envelope.
    data=body
    checks['operation']=body.get('operation')==name
    checks['provider']=body.get('provider')=='NAVER'
    checks['fetched_at']=isinstance(body.get('fetched_at'),str)
    meta={'cache_hit':body.get('cache_hit')}
    if not isinstance(data,dict): return checks,meta
    if name.startswith('naver_search_'):
        checks['source']=body.get('source')==SEARCH[name.removeprefix('naver_search_')]
        checks['query']=body.get('query')==args['query']
        checks['items']=isinstance(data.get('items'),list)
        meta.update(item_count=len(data.get('items',[])),empty=data.get('items')==[],total=data.get('total'))
    elif name=='naver_convert_keyboard_query':
        checks['source']=body.get('source')=='errata'; checks['errata']='errata' in data
        meta['has_correction']=bool(data.get('errata'))
    elif name=='naver_check_adult_query':
        checks['source']=body.get('source')=='adult'; checks['adult']='adult' in data
        meta['adult']=data.get('adult')
    else:
        checks['relative_values']=body.get('relative_values') is True
        from calendar import monthrange
        from naver_contracts import valid_envelope, payload
        from naver_tools import evidence as normalize_evidence
        a,b=dt.date.fromisoformat(args['start_date']),dt.date.fromisoformat(args['end_date'])
        unit=args['time_unit']
        lo=a-dt.timedelta(days=a.weekday()) if unit=='week' else a.replace(day=1) if unit=='month' else a
        hi=b+dt.timedelta(days=6-b.weekday()) if unit=='week' else b.replace(day=monthrange(b.year,b.month)[1]) if unit=='month' else b
        checks['dates']=valid_envelope(args['start_date'],args['end_date'],unit,data.get('startDate'),data.get('endDate'),data.get('timeUnit'))
        checks['independent_envelope']=(data.get('startDate'),data.get('endDate')) in {(a.isoformat(),b.isoformat()),(lo.isoformat(),hi.isoformat())}
        checks['requested_bounds']=data.get('requested_start_date')==args['start_date'] and data.get('requested_end_date')==args['end_date']
        checks['effective_bounds']=data.get('effective_start_date')==data.get('startDate') and data.get('effective_end_date')==data.get('endDate')
        checks['bucket_bounds']=data.get('bucket_start_date')==lo.isoformat() and data.get('bucket_end_date')==hi.isoformat()
        _,request=payload(name,args)
        envelope=dict(cache_hit=body.get('cache_hit'),data={k:data.get(k) for k in ('startDate','endDate','timeUnit','results')})
        if 'mode' in request: envelope['mode']=request['mode']
        try:
            normalize_evidence(name,request,envelope)
            checks['core_validation']=True
        except Exception:
            checks['core_validation']=False
        checks['time_unit']=data.get('timeUnit')==args['time_unit']
        checks['results']=isinstance(data.get('results'),list)
        if name.startswith('naver_shopping_'):
            checks['mode']=body.get('mode')==MODES[name.removeprefix('naver_shopping_').removesuffix('_trend')]
        rows=[row for group in data.get('results',[]) for row in group.get('data',[])]
        checks['ratios']=all(type(row.get('ratio')) in (int,float) and 0<=row['ratio']<=100 for row in rows)
        def valid_point(row):
            try:
                p=dt.date.fromisoformat(row['period'])
                return p.isoformat()==row['period'] and lo<=p<=hi and (unit!='week' or p.weekday()==0) and (unit!='month' or p.day==1)
            except (KeyError,TypeError,ValueError): return False
        checks['periods']=all(valid_point(row) for row in rows)
        meta.update(requested_start_date=args['start_date'],requested_end_date=args['end_date'],
                    effective_start_date=data.get('effective_start_date'),effective_end_date=data.get('effective_end_date'),time_unit=unit)
        meta.update(series_count=len(data.get('results',[])),point_count=len(rows),empty=not rows,start_date=data.get('startDate'),end_date=data.get('endDate'))
    return checks,meta


async def inside(manifest):
    logging.disable(logging.CRITICAL)
    import httpx
    import bridge
    from naver_contracts import payload
    evidence={'proof':LABEL,'authenticated_client_receipt':False,'calls':[],'checks':{},'source_hashes':{}}
    validate_manifest(manifest)
    for name in sorted(REQUIRED_MODULES):
        evidence['source_hashes'][name]=hashlib.sha256(Path('/app',name).read_bytes()).hexdigest()
    evidence['container_source_verification']=compare_hashes(manifest['required_modules'],evidence['source_hashes'])
    evidence['checks']['approved_container_source']=evidence['container_source_verification']['match']
    if not evidence['checks']['approved_container_source']:
        evidence['ok']=False
        return evidence  # Fail before any utility request or key read.
    cfg=bridge.load_config(os.environ.get('HERMES_BRIDGE_MCP_CONFIG','/run/bridge/config.json'))
    keypath=Path(os.environ.get('NAVER_UTILITY_API_KEY_FILE','/run/secrets/utility_api_key'))
    evidence['runtime_key_readable']=os.access(keypath,os.R_OK) and keypath.is_file()
    # This fixture exists only in this independent interpreter. Production
    # AccessVerifier is neither patched nor issued any fabricated credential.
    class OperatorFixtureVerifier:
        ready=True
        def preflight(self, assertion):
            if assertion!='isolated-operator-fixture': raise ValueError('not authorized')
        def verify(self, assertion):
            self.preflight(assertion)
            return {'iss':'https://isolated-operator.invalid','sub':'naver-canary-operator'}
        def warm(self):
            # Synthetic readiness only, never a production JWKS assertion.
            return None
    app=bridge.build_app(cfg,OperatorFixtureVerifier())
    host=cfg['allowed_hosts'][0]
    headers={'Accept':'application/json, text/event-stream','Cf-Access-Jwt-Assertion':'isolated-operator-fixture'}
    # Explicit guards against accidental workflow traffic even if a regression
    # incorrectly wires a NAVER tool to a handoff function.
    def blocked(*a,**kw): raise AssertionError('workflow dispatch forbidden in NAVER canary')
    bridge.dispatch=blocked; bridge.v2_request=blocked; bridge.submit_v2=blocked
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=20) as direct:
        live=await direct.get('http://127.0.0.1:8652/livez')
        ready=await direct.get('http://127.0.0.1:8652/healthz')
        denied=await direct.post('http://127.0.0.1:8652/mcp',headers={'Host':host},json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
        evidence['checks'].update(livez=live.status_code==200,readiness=ready.status_code==200,running_origin_denied=denied.status_code==401)
        evidence['running_origin_denied_status']=denied.status_code
        key=keypath.read_text().strip()
        health=await direct.get('http://utility-fastapi-server:8000/naver/health',headers={'X-API-Key':key})
        evidence['checks']['utility_network_ready']=health.status_code==200 and health.json().get('credentials') is True
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://'+host) as rpc_client:
                async def rpc(method,params):
                    response=await rpc_client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
                    if response.status_code!=200: raise RuntimeError('isolated RPC HTTP failure')
                    return response.json()
                init=await rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='isolated-operator-canary',version=RELEASE_VERSION)))
                evidence['checks']['initialize']='result' in init
                tools=(await rpc('tools/list',{}))['result']['tools']
                names=[t['name'] for t in tools]
                evidence['tool_names']=sorted(names); evidence['tool_count']=len(names)
                cases=samples()
                evidence['checks']['exact_inventory']=len(names)==27 and set(names)==LEGACY|set(cases)
                for name,args in [*cases.items(),*trend_variants()]:
                    entry={'name':name,'variant':args.get('time_unit','default'),'arguments':args,'ok':False}
                    evidence['calls'].append(entry)
                    try:
                        reply=await rpc('tools/call',dict(name=name,arguments=args))
                        result=reply.get('result',{})
                        if result.get('isError') or 'error' in reply:
                            entry.update(error_summary(result)); continue
                        body=unpack(result)
                        checks,meta=inspect_data(name,body,args)
                        entry.update(checks=checks,metadata=meta,ok=all(checks.values()))
                        # Same validated/defaulted utility payload, no public source/mode args.
                        if name in {'naver_search_web','naver_keyword_trend','naver_shopping_category_trend'}:
                            route,request=payload(name,args)
                            compare=await direct.post('http://utility-fastapi-server:8000'+route,json=request,headers={'X-API-Key':key})
                            parity=compare.json() if compare.status_code==200 else {}
                            from naver_tools import evidence as normalize_evidence
                            normalized=normalize_evidence(name,request,parity) if compare.status_code==200 else {}
                            stable=lambda obj:{k:v for k,v in obj.items() if k not in {'fetched_at','cache_hit'}}
                            checks['direct_data_parity']=stable(body)==stable(normalized) and compare.status_code==200
                            checks['direct_cache_metadata']=type(parity.get('cache_hit')) is bool
                            entry['direct_cache_hit']=parity.get('cache_hit')
                            entry['direct_provider_headers']={k:parity.get('data',{}).get(k) for k in ('startDate','endDate','timeUnit')}
                            checks['direct_provider_header_parity']=all(body.get(k)==v for k,v in entry['direct_provider_headers'].items())
                            entry['direct_request']=request
                            checks['direct_metadata_parity']=all(body.get(k)==normalized.get(k) for k in ['operation','source','mode','relative_values','startDate','endDate','timeUnit','query','requested_start_date','requested_end_date','effective_start_date','effective_end_date'])
                            entry['ok']=all(checks.values())
                    except Exception as exc:
                        entry.update(ok=False,exception_type=type(exc).__name__)
    evidence['unique_naver_tool_count']=len({e['name'] for e in evidence['calls']})
    evidence['variant_call_count']=sum(e['variant'] in ('week','month') for e in evidence['calls'])
    evidence['checks']['all_promised_called']=len(evidence['calls'])==37 and {e['name'] for e in evidence['calls']}==set(samples())
    evidence['checks']['all_variants_called']=evidence['variant_call_count']==18 and {(e['name'],e['variant']) for e in evidence['calls'] if e['variant'] in ('week','month')}=={(n,a['time_unit']) for n,a in trend_variants()}
    evidence['ok']=all(evidence['checks'].values()) and all(e['ok'] for e in evidence['calls'])
    return evidence


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Consume real NAVER read quotas; no workflow writes')
    parser.add_argument('--container',help='Exact already-deployed MCP container name or ID')
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'naver-040-operator-evidence.json')
    parser.add_argument('--manifest',type=Path,help='Required parent-approved immutable source manifest')
    parser.add_argument('--manifest-json',help=argparse.SUPPRESS)
    parser.add_argument('--inside',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.inside:
        import asyncio
        try: evidence=asyncio.run(inside(validate_manifest(json.loads(args.manifest_json))))
        except Exception as exc: evidence={'proof':LABEL,'authenticated_client_receipt':False,'ok':False,'exception_type':type(exc).__name__}
        print(json.dumps(evidence,ensure_ascii=False)); return 0 if evidence['ok'] else 1
    if not args.execute or not args.container or not args.manifest: parser.error('--execute, --container and --manifest are required; no deployment is performed')
    evidence={'proof':LABEL,'authenticated_client_receipt':False,'ok':False}
    try:
        manifest_bytes=args.manifest.read_bytes()
        manifest=validate_manifest(json.loads(manifest_bytes))
        evidence['approved_manifest_sha256']=hashlib.sha256(manifest_bytes).hexdigest()
        evidence['approved_manifest']=manifest
        root=Path(__file__).resolve().parent.parent
        expected_host={**manifest['required_modules'],**manifest.get('host_artifacts',{})}
        host_bytes={n:(root/n).read_bytes() for n in expected_host}
        actual_host={n:hashlib.sha256(raw).hexdigest() for n,raw in host_bytes.items()}
        evidence['host_source_verification']=compare_hashes(expected_host,actual_host)
        if not evidence['host_source_verification']['match']: raise ValueError('host source differs from approval')
        inspect=subprocess.run(['docker','inspect',args.container],capture_output=True,text=True,check=True)
        obj=json.loads(inspect.stdout)[0]
        evidence.update(image_id=obj['Image'],image_tag=obj['Config']['Image'],no_published_ports=not any(obj['NetworkSettings'].get('Ports',{}).values()),network_attached='hermes-bridge-edge' in obj['NetworkSettings']['Networks'])
        evidence['expected_image_ref']=manifest['expected_image_ref']
        evidence['image_reference_matches']=obj['Config']['Image'] in {manifest['expected_image_ref'],manifest['expected_image_id']}
        image=subprocess.run(['docker','image','inspect',manifest['expected_image_ref'],'--format','{{.Id}}'],capture_output=True,text=True,check=True)
        evidence['image_matches_release_tag']=image.stdout.strip()==obj['Image']
        evidence['approved_image_match']=manifest['expected_image_id']==obj['Image']
        if not all(evidence[k] for k in ('approved_image_match','image_matches_release_tag','image_reference_matches','no_published_ports','network_attached')): raise ValueError('image differs from approval')
        result=subprocess.run(['docker','exec','-i',args.container,'python','-','--inside','--manifest-json',json.dumps(manifest)],input=host_bytes['planning/run_naver_canaries.py'].decode('utf-8'),capture_output=True,text=True,timeout=900)
        inner=json.loads(result.stdout)
        evidence.update(inner)
        evidence['container_probe_exit']=result.returncode
        evidence['container_source_verification']=compare_hashes(manifest['required_modules'],inner.get('source_hashes',{}))
        evidence['approved_source_match']=evidence['host_source_verification']['match'] and evidence['container_source_verification']['match']
        # Unauthenticated public probe: a redirect is NOT an authenticated receipt.
        import urllib.request, urllib.error
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*a,**kw): return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        request=urllib.request.Request('https://bridge.example.invalid/mcp',data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','User-Agent':'HermesBridgeMCP/'+RELEASE_VERSION})
        try:
            with opener.open(request,timeout=20) as response: status=response.status
        except urllib.error.HTTPError as exc: status=exc.code
        evidence['public_unauthenticated_status']=status
        evidence['ok']=release_success(evidence)
    except Exception as exc:
        evidence.update(ok=False,operator_exception_type=type(exc).__name__)
    evidence['authenticated_client_receipt']=False
    evidence['recorded_at']=dt.datetime.now(dt.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'ok':evidence['ok'],'proof':LABEL,'authenticated_client_receipt':False,'evidence_file':str(args.output)}))
    return 0 if evidence['ok'] else 1

if __name__=='__main__': raise SystemExit(main())
