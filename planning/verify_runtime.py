#!/usr/bin/env python3
"""Read-back verification of the exact deployed target; no credentials printed."""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

ROOT=Path(__file__).resolve().parents[1]
NAME='hermes-bridge-mcp-hermes-bridge-mcp-1'
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*_): return None


def main():
    info=json.loads(subprocess.check_output(['docker','inspect',NAME],text=True))[0]
    expected=json.loads(subprocess.check_output(['docker','image','inspect','local/hermes-bridge-mcp:0.2.0'],text=True))[0]['Id']
    state=info['State']; network=info['NetworkSettings']
    result={'container':NAME,'image':info['Image'],'expected_image':expected,
      'image_matches':info['Image']==expected,'running':state['Running'],
      'health':state.get('Health',{}).get('Status'),'restarts':info['RestartCount'],
      'oom_killed':state['OOMKilled'],'published_ports':network.get('Ports',{}),
      'read_only_root':info['HostConfig']['ReadonlyRootfs'],
      'all_capabilities_dropped':'ALL' in info['HostConfig'].get('CapDrop',[])}
    probe=subprocess.run(['docker','exec',NAME,'python','-c',
      "import bridge,json; from urllib.request import urlopen; print(json.dumps({'health':json.load(urlopen('http://127.0.0.1:8652/healthz',timeout=3))}))"],text=True,capture_output=True,timeout=10)
    result['container_readiness']=json.loads(probe.stdout) if probe.returncode==0 else {'status':'failed'}
    hashes={}
    for file in ('bridge.py','hyemi_contracts.py'):
        cp=subprocess.run(['docker','exec',NAME,'python','-c',
          "import hashlib; print(hashlib.sha256(open('/app/"+file+"','rb').read()).hexdigest())"],text=True,capture_output=True,timeout=10)
        actual=cp.stdout.strip()
        hashes[file]={'local':hashlib.sha256((ROOT/file).read_bytes()).hexdigest(),'container':actual,'matches':actual==hashlib.sha256((ROOT/file).read_bytes()).hexdigest()}
    result['source_hashes']=hashes
    result['ingress_service']=subprocess.check_output(['systemctl','--user','show','hyemi-ingress.service',
      '--property=ActiveState,SubState,ExecMainStatus,MainPID'],text=True).strip().splitlines()
    cfg=json.loads(Path('/root/.config/hyemi-ingress/config.json').read_text())
    owner=hashlib.sha256(b'hyemi-v2-operator-diagnostics').hexdigest()
    client=build_opener(ProxyHandler({}),NoRedirect())
    request=Request('http://172.18.0.1:8651/healthz',headers={'X-API-Key':cfg['api_key'],'X-Hyemi-Owner':owner})
    with client.open(request,timeout=5) as r: result['private_ingress_readiness']=json.load(r)
    codes={}
    for path in ('/mcp','/v2/requests','/webhooks/hyemi'):
        try:
            with client.open(Request('https://bridge.example.invalid'+path,headers={'User-Agent':'HermesBridgeMCP/2.0','Accept':'application/json'}),timeout=15) as r: codes[path]=r.status
        except HTTPError as e: codes[path]=e.code
    result['public_unauthenticated_http']=codes
    valid=(result['image_matches'] and result['running'] and result['health']=='healthy' and result['restarts']==0
      and not result['oom_killed'] and not result['published_ports'] and result['read_only_root']
      and result['all_capabilities_dropped'] and all(x['matches'] for x in hashes.values())
      and result['private_ingress_readiness'].get('protocol')==2 and codes['/mcp']==401)
    result['pass']=valid
    path=ROOT/'planning/runtime-verification.json'; path.write_text(json.dumps(result,ensure_ascii=False,indent=2)); path.chmod(0o600)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not valid: raise SystemExit(1)


if __name__=='__main__': main()
