#!/usr/bin/env python3
"""LIVE PRIVATE OPERATOR test only; not authenticated client or Hyemi user auth proof.
One submission with a fixed local namespace, GET-only readback, never resend."""
import argparse, datetime, hashlib, json, os, sqlite3, tempfile, time, uuid
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]
BASE='http://172.18.0.1:8651'
# Local administrative fixture identity: never derived from an issuer/sub or ledger.
OPERATOR_NAMESPACE='hermes-bridge-mcp:LOCAL-OPERATOR:report040-canary:v1'
OPERATOR_OWNER=hashlib.sha256(OPERATOR_NAMESPACE.encode()).hexdigest()
CONTENTS='**MCP 보고 전송 확인**\n\n강조와 `inline code`, 이름 있는 링크를 확인하는 운영 테스트야.\n[Telegram 공식 문서](https://core.telegram.org/bots/api)\n\n원문은 기록에 그대로 보관돼. ✅'
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true');parser.add_argument('--output',default='planning/mcp-040-operator-report-canary.json')
    args=parser.parse_args()
    if not args.execute:parser.error('live submission requires --execute')
    out=(ROOT/args.output).resolve()
    if not out.is_relative_to(ROOT/'planning') or out.exists():raise RuntimeError('new planning evidence path required; existing receipt must not be overwritten')
    cfg=json.loads(Path('/root/.config/hyemi-ingress/config.json').read_text())
    channel=cfg['allowed']['hyeji']['home']
    if channel not in ('-1000000002','-1000000002:1'):raise RuntimeError('current General destination mismatch')
    db=sqlite3.connect('file:'+cfg['state_path']+'?mode=ro',uri=True)
    owner=OPERATOR_OWNER; eid=str(uuid.uuid4());wid=str(uuid.uuid4())
    # Fail closed on any collision, including another owner's work. No updates.
    if db.execute('SELECT 1 FROM v2_events WHERE event_id=? OR work_id=?', (eid,wid)).fetchone() or db.execute('SELECT 1 FROM v2_works WHERE work_id=?', (wid,)).fetchone():
        db.close();raise RuntimeError('fresh event and work IDs required; do not resend')
    body={'version':2,'kind':'report','event_id':eid,'work_id':wid,'to':'hyeji','channelid':channel,'contents':CONTENTS,'title':'MCP 0.4.0 보고 전송 확인'}
    record={'recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'event_id':eid,'work_id':wid,'proof':'LIVE PRIVATE OPERATOR test; not authenticated client receipt or Hyemi user auth proof','operator_namespace':OPERATOR_NAMESPACE,'operator_owner':owner,'hyemi_user_auth_proof':False,'retry':False,'destination':channel,'raw_sha256':hashlib.sha256(CONTENTS.encode()).hexdigest(),'authenticated_client_receipt':False,'telegram_ui_verified':False,'submitted':False,'ok':False}
    evidence_error_class=None
    def save(initial=False):
        nonlocal evidence_error_class
        temp=None;fd=None
        try:
            # Serialize before touching disk. The initial O_EXCL claim is stable;
            # updates never truncate it, even if a POST committed ambiguously.
            payload=json.dumps(record,ensure_ascii=False,indent=2)
            if initial:
                fd=os.open(out,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
            else:
                fd,temp=tempfile.mkstemp(prefix='.'+out.name+'.',suffix='.tmp',dir=out.parent)
            stream=os.fdopen(fd,'w',encoding='utf-8');fd=None
            with stream:
                stream.write(payload)
                stream.flush();os.fsync(stream.fileno())
            if not initial:
                os.replace(temp,out);temp=None
            fd=os.open(out.parent,os.O_RDONLY | os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd);fd=None
        except Exception as exc:
            evidence_error_class=type(exc).__name__
            raise
        finally:
            if fd is not None:os.close(fd)
            if temp is not None:os.unlink(temp)
    opener=build_opener(ProxyHandler({}),NoRedirect())
    def call(method,path,data=None):
        if (method,path) not in (('POST','/v2/requests'),('GET','/v2/events/'+eid+'/report'),('GET','/v2/events/'+eid+'/result')):raise RuntimeError('invalid canary path')
        req=Request(BASE+path,method=method,data=None if data is None else json.dumps(data,ensure_ascii=False).encode(),headers={'X-API-Key':cfg['api_key'],'X-Hyemi-Owner':owner,'Content-Type':'application/json','User-Agent':'HermesBridgeMCP/0.4.0 report verification'})
        try:response=opener.open(req,timeout=20)
        except HTTPError as exc:response=exc
        raw=response.read(1048577)
        if len(raw)>1048576:raise RuntimeError('oversized response')
        return response.code,json.loads(raw)
    try:save(initial=True)
    except Exception:
        db.close();raise
    try:
        code,accepted=call('POST','/v2/requests',body)
        record['submission_status']=code;record['accepted']=accepted
        if code!=202 or accepted.get('status')!='accepted' or accepted.get('event_id')!=eid or accepted.get('work_id')!=wid:raise RuntimeError('admission unverified')
        record['submitted']=True;save()
        deadline=time.monotonic()+60
        while True:
            code,result=call('GET','/v2/events/'+eid+'/result')
            if code!=200 or result.get('event_id')!=eid or result.get('work_id')!=wid:raise RuntimeError('result lookup failed')
            delivery=result.get('delivery',[])
            if delivery and all(d['state']=='delivered' and d.get('confirmed_destination') is True for d in delivery):break
            if any(d['state'] in ('failed','unknown','blocked_configuration') for d in delivery) or time.monotonic()>=deadline:raise RuntimeError('delivery failed, unknown, or timed out; do not resend')
            time.sleep(.5)
        code,raw=call('GET','/v2/events/'+eid+'/report')
        record['raw_report_exact']=code==200 and raw==body
        rows=db.execute('SELECT phase,text,sender,state,message_id,destination,rendering FROM v2_outbox WHERE event_id=? ORDER BY id',(eid,)).fetchall()
        plans=[json.loads(r[6]) for r in rows]
        record['delivery']=delivery;record['plans']=plans
        record['named_link_targets']=[e['url'] for p in plans for e in p['entities'] if e['type']=='text_link']
        record['preview_requests']=[p['link_preview_options'] for p in plans if p['link_preview_options']['is_disabled'] is False]
        # WorkflowStore keeps run_id in JSON metadata, not a ledger column.
        # A missing/mismatched row is not evidence of an absent agent run.
        event=db.execute('SELECT owner,work_id,kind,execution_state,metadata,request FROM v2_events WHERE event_id=?',(eid,)).fetchone()
        record['no_agent_run']=bool(event is not None and event[:4]==(owner,wid,'report','finished') and result.get('execution_state')=='finished' and json.loads(event[4]).get('run_id') is None and json.loads(event[5])==body)
        def general(destination):
            return isinstance(destination,dict) and destination.get('chat_id')=='-1000000002' and destination.get('message_thread_id') in (None,1) and destination.get('general') is True
        record['confirmed_general_destination']=bool(rows and len(rows)==len(delivery) and all(general(d.get('verified_destination')) for d in delivery) and all(general(json.loads(r[5])) for r in rows))
        record['ok']=bool(record['confirmed_general_destination'] and record['raw_report_exact'] and record['no_agent_run'] and record['named_link_targets']==['https://core.telegram.org/bots/api'] and len(record['preview_requests'])==1 and all(r[2]=='hyemi' and r[3]=='delivered' for r in rows))
        save();print(json.dumps({k:v for k,v in record.items() if k!='plans'},ensure_ascii=False,indent=2))
        return 0 if record['ok'] else 1
    except Exception as exc:
        record['ok']=False;record['error_class']=type(exc).__name__
        # A failed save is not retried. On transport/readback failure, try one
        # status update, but preserve the original error and IDs if disk fails.
        if evidence_error_class is None:
            try:save()
            except Exception as save_exc:evidence_error_class=type(save_exc).__name__
        print(json.dumps({'ok':False,'event_id':eid,'work_id':wid,'submitted':record['submitted'],'error_class':type(exc).__name__,'evidence_error_class':evidence_error_class,'evidence_updated':evidence_error_class is None,'evidence':str(out),'retry':False},indent=2));return 1
    finally:db.close()
if __name__=='__main__':raise SystemExit(main())
