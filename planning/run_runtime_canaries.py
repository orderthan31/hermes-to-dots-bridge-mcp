#!/usr/bin/env python3
"""Operator diagnostics through the deployed Docker bridge's real business API.

This is NOT a Cloudflare-authenticated ChatGPT client receipt. Public JWT/protocol
checks remain separate. Uses configured credentials internally; prints no secrets.
"""
import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / 'planning/runtime-canary-receipts.json'
CONTAINER = 'hermes-bridge-mcp-hermes-bridge-mcp-1'
OWNER = hashlib.sha256(b'hyemi-v2-operator-diagnostics').hexdigest()
CHILD = '''import json,sys
from bridge import load_config,submit_v2,lookup_v2
cfg=load_config('/run/bridge/config.json')
r=json.load(sys.stdin)
try:
 if r['op']=='submit':
  result=submit_v2(cfg,r['owner'],**r['arguments'])
 else:
  result=lookup_v2(cfg,r['owner'],**r['arguments'])
 print(json.dumps(result,ensure_ascii=False,allow_nan=False))
except Exception as e:
 print(json.dumps({'diagnostic_error':type(e).__name__})); sys.exit(2)
'''


def rpc(op, **arguments):
    call = subprocess.run(['docker','exec','-i',CONTAINER,'python','-c',CHILD],
        input=json.dumps({'op':op,'owner':OWNER,'arguments':arguments},ensure_ascii=False),
        text=True,capture_output=True,timeout=30)
    if call.returncode:
        raise RuntimeError('deployed_bridge_call_failed')
    result=json.loads(call.stdout)
    if not isinstance(result,dict): raise RuntimeError('deployed_bridge_shape_failed')
    return result


def read_receipt():
    if RECEIPT.exists(): return json.loads(RECEIPT.read_text())
    return {'surface':'deployed Docker business API → private ingress → ledger/executor/Telegram',
            'cloudflare_authenticated_client_receipt':False,'entries':[]}


def save(receipt):
    RECEIPT.parent.mkdir(parents=True,exist_ok=True)
    tmp=RECEIPT.with_suffix('.tmp')
    tmp.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)); tmp.chmod(0o600); tmp.replace(RECEIPT)


def record(receipt,label,kind,recipient,channel,contents,work_id=None,**fields):
    found=next((e for e in receipt['entries'] if e['label']==label),None)
    if found:
        if found['state'] != 'prepared': return found
        entry=found
    else:
        entry={'label':label,'kind':kind,'recipient':recipient,'channel':channel,
               'event_id':str(uuid.uuid4()),'work_id':work_id or str(uuid.uuid4()),
               'contents':contents,'fields':fields,'state':'prepared'}
        receipt['entries'].append(entry); save(receipt)  # survive ambiguous submission
    entry['admission']=rpc('submit',kind=entry['kind'],event_id=entry['event_id'],work_id=entry['work_id'],
        contents=entry['contents'],to=entry['recipient'],channelid=entry['channel'],**entry['fields'])
    entry['state']='submitted'; save(receipt)
    return entry


def inspect(receipt):
    for entry in receipt['entries']:
        entry['result']=rpc('lookup',category='events',identifier=entry['event_id'],suffix='/result')
        entry['work']=rpc('lookup',category='works',identifier=entry['work_id'])
        entry['updates']=rpc('lookup',category='works',identifier=entry['work_id'],suffix='/updates',limit=100)
        if entry['kind']=='report':
            report=rpc('lookup',category='events',identifier=entry['event_id'],suffix='/report')
            entry['raw_preserved']=report.get('contents')==entry['contents']
    save(receipt)
    return receipt


def summaries(receipt):
    rows=[]
    for e in receipt['entries']:
        r=e.get('result',{})
        rows.append({'label':e['label'],'event_id':e['event_id'],'work_id':e['work_id'],
          'availability':r.get('availability'),'execution_state':r.get('execution_state'),
          'outcome':r.get('outcome'),'completion_signal':r.get('completion_signal'),
          'deliveries':[{k:d.get(k) for k in ('phase','sender','state','message_id','verified_destination','destination')} for d in r.get('delivery',[])],
          'raw_preserved':e.get('raw_preserved'),
          'runtime':r.get('hyemi_result',{}).get('runtime'),
          'backend':r.get('hyemi_result',{}).get('backend') or r.get('hyemi_result',{}).get('review_metadata',{}).get('backend')})
    return rows


def main():
    p=argparse.ArgumentParser(); p.add_argument('stage',choices=['reports','review1','review2','tasks','inspect'])
    p.add_argument('--wait',type=int,default=0); args=p.parse_args(); receipt=read_receipt()
    if args.stage=='reports':
        # Synthetic destinations only; adapt explicitly to private policy before use.
        targets=[('hyeji','-1000000002','Recipient A general'),('hyeji','-1000000002:101','Topic A'),
                 ('hyerin','-1000000001','Recipient B general'),('hyerin','-1000000001:102','Topic B'),
                 ('hyerin','-1000000001:103','Topic C')]
        for to,channel,title in targets:
            record(receipt,'report-'+title,'report',to,channel,
                   '혜미 MCP 연결 테스트 — '+title+'\n원문 그대로 전달됐는지 확인하는 테스트 보고야. 😀',title='MCP 연결 확인')
    elif args.stage=='review1':
        record(receipt,'review-1','review','hyeji','-1000000002','검토할 수식: 1+1=3',
               title='MCP 산술 검토 연결 확인',revision=1,criteria='산술 정확성')
    elif args.stage=='review2':
        prior=next(e for e in receipt['entries'] if e['label']=='review-1')
        if prior.get('result',{}).get('outcome')!='changes_required': raise RuntimeError('first_review_not_verified')
        record(receipt,'review-2','review','hyeji','-1000000002','검토할 수식: 1+1=2',
               work_id=prior['work_id'],title='MCP 산술 검토 연결 확인',revision=2,criteria='산술 정확성',
               changes='합계를 3에서 2로 수정했습니다.',previous_event_id=prior['event_id'])
    elif args.stage=='tasks':
        for recipient,channel in [('hyeji','-1000000002'),('hyerin','-1000000001')]:
            record(receipt,'task-'+recipient,'task',recipient,channel,
                '읽기 전용 연결 확인 작업입니다. 진행 시작을 commentary로 알리고, terminal 또는 read_file 도구로 /proc/sys/kernel/hostname 파일을 실제 읽어 주세요. 확인한 값을 증거에 담고 최종 결과를 요청된 JSON 구조로 보고해 주세요.',
                title='MCP 실제 도구 실행 연결 확인',acceptance_criteria='실제 도구로 /proc/sys/kernel/hostname을 읽고 확인값과 사용 도구를 evidence에 기록한다. 파일 변경 없이 검증을 마치고 한국어 결과 보고를 작성한다.')
    deadline=time.monotonic()+max(0,min(args.wait,480))
    while True:
        inspect(receipt)
        complete=all(e.get('result',{}).get('availability')=='available' and e.get('result',{}).get('delivery') and
            all(d['state']=='delivered' for d in e['result']['delivery']) for e in receipt['entries'])
        if complete or time.monotonic()>=deadline: break
        time.sleep(2)
    print(json.dumps({'receipts':str(RECEIPT),'summaries':summaries(receipt)},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
