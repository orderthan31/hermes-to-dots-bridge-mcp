#!/usr/bin/env python3
"""Parent-operated live report canary: one submission, readback, never resend."""
import argparse, datetime, hashlib, json, os, re, sqlite3, time, uuid
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]
BASE='http://172.18.0.1:8651'
CONTENTS='**MCP 보고 전송 확인**\n\n강조와 `inline code`, 이름 있는 링크를 확인하는 운영 테스트야.\n[Telegram 공식 문서](https://core.telegram.org/bots/api)\n\n원문은 기록에 그대로 보관돼. ✅'
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true');parser.add_argument('--output',default='planning/mcp-040-report-canary.json')
    args=parser.parse_args()
    if not args.execute:parser.error('live submission requires --execute')
    out=(ROOT/args.output).resolve()
    if not out.is_relative_to(ROOT/'planning') or out.exists():raise RuntimeError('new planning evidence path required; existing receipt must not be overwritten')
    cfg=json.loads(Path('/root/.config/hyemi-ingress/config.json').read_text())
    channel=cfg['allowed']['hyeji']['home']
    if channel not in ('-1000000002','-1000000002:1'):raise RuntimeError('current General destination mismatch')
    db=sqlite3.connect('file:'+cfg['state_path']+'?mode=ro',uri=True)
    owners=[r[0] for r in db.execute('SELECT DISTINCT owner FROM v2_events')]
    if len(owners)!=1 or not re.fullmatch('[0-9a-f]{64}',owners[0]):raise RuntimeError('unambiguous existing authenticated owner required')
    owner=owners[0]; eid=str(uuid.uuid4());wid=str(uuid.uuid4())
    body={'version':2,'kind':'report','event_id':eid,'work_id':wid,'to':'hyeji','channelid':channel,'contents':CONTENTS,'title':'MCP 0.4.0 보고 전송 확인'}
    record={'recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'event_id':eid,'work_id':wid,'proof':'LIVE private workflow/report delivery; not ChatGPT OAuth receipt','destination':channel,'raw_sha256':hashlib.sha256(CONTENTS.encode()).hexdigest(),'authenticated_client_receipt':False,'telegram_ui_verified':False,'submitted':False,'ok':False}
    def save():
        out.write_text(json.dumps(record,ensure_ascii=False,indent=2));os.chmod(out,0o600)
    opener=build_opener(ProxyHandler({}),NoRedirect())
    def call(method,path,data=None):
        if path not in ('/v2/requests','/v2/events/'+eid+'/report','/v2/events/'+eid+'/result'):raise RuntimeError('invalid canary path')
        req=Request(BASE+path,method=method,data=None if data is None else json.dumps(data,ensure_ascii=False).encode(),headers={'X-API-Key':cfg['api_key'],'X-Hyemi-Owner':owner,'Content-Type':'application/json','User-Agent':'HermesBridgeMCP/0.4.0 report verification'})
        try:response=opener.open(req,timeout=20)
        except HTTPError as exc:response=exc
        raw=response.read(1048577)
        if len(raw)>1048576:raise RuntimeError('oversized response')
        return response.code,json.loads(raw)
    save()
    try:
        code,accepted=call('POST','/v2/requests',body)
        record['submission_status']=code;record['accepted']=accepted
        if code!=202 or accepted.get('status')!='accepted' or accepted.get('event_id')!=eid or accepted.get('work_id')!=wid:raise RuntimeError('admission unverified')
        record['submitted']=True;save()
        deadline=time.monotonic()+60
        while True:
            code,result=call('GET','/v2/events/'+eid+'/result')
            if code!=200:raise RuntimeError('result lookup failed')
            delivery=result.get('delivery',[])
            if delivery and all(d['state']=='delivered' and d.get('confirmed_destination') is True for d in delivery):break
            if any(d['state'] in ('failed','unknown','blocked_configuration') for d in delivery) or time.monotonic()>=deadline:raise RuntimeError('delivery failed, unknown, or timed out; do not resend')
            time.sleep(.5)
        code,raw=call('GET','/v2/events/'+eid+'/report')
        record['raw_report_exact']=code==200 and raw.get('contents')==CONTENTS and raw.get('event_id')==eid and raw.get('work_id')==wid
        rows=db.execute('SELECT phase,text,sender,state,message_id,destination,rendering FROM v2_outbox WHERE event_id=? ORDER BY id',(eid,)).fetchall()
        plans=[json.loads(r[6]) for r in rows]
        record['delivery']=delivery;record['plans']=plans
        record['named_link_targets']=[e['url'] for p in plans for e in p['entities'] if e['type']=='text_link']
        record['preview_requests']=[p['link_preview_options'] for p in plans if p['link_preview_options']['is_disabled'] is False]
        # WorkflowStore keeps run_id in JSON metadata, not a ledger column.
        # A missing/mismatched row is not evidence of an absent agent run.
        event=db.execute('SELECT owner,work_id,kind,execution_state,metadata FROM v2_events WHERE event_id=?',(eid,)).fetchone()
        record['no_agent_run']=bool(event is not None and event[:4]==(owner,wid,'report','finished') and result.get('execution_state')=='finished' and json.loads(event[4]).get('run_id') is None)
        record['ok']=bool(record['raw_report_exact'] and record['no_agent_run'] and record['named_link_targets']==['https://core.telegram.org/bots/api'] and len(record['preview_requests'])==1 and all(r[2]=='hyemi' and r[3]=='delivered' for r in rows))
        save();print(json.dumps({k:v for k,v in record.items() if k!='plans'},ensure_ascii=False,indent=2))
        return 0 if record['ok'] else 1
    except Exception as exc:
        record['error_class']=type(exc).__name__;save()
        print(json.dumps({'ok':False,'event_id':eid,'submitted':record['submitted'],'error_class':type(exc).__name__,'evidence':str(out),'retry':False},indent=2));return 1
    finally:db.close()
if __name__=='__main__':raise SystemExit(main())
