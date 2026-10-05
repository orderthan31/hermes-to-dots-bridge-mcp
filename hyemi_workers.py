"""Bounded workflow execution, safe SSE progress and independent delivery.

Only this process owns orchestration; SQLite owns session ordering and delivery
claims. A lost review process is finalized as interrupted, never paid twice.
Upstream seq N is stored as N+1 (zero is the ledger's initial cursor).
"""
from __future__ import annotations
import hashlib
import json
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from hyemi_delivery import SenderTokens, TelegramTransport, NoRedirect, _secure_read, split_text
from hyemi_review import ReviewExecutor
from hyemi_contracts import validate_result
from hyemi_notifications import notification_result, render_progress


def served_provenance(answer):
    """Copy only actual Runs API status provenance, never request/model output hints.

    Wire shape verified in installed gateway/platforms/api_server_runs.py:
    runtime={provider,model,route_source,requested?,model_lock?}, usage counters.
    Unknown keys (including source/URLs/headers/secrets) are not provenance.
    """
    def identifier(value, limit):
        if (isinstance(value, str) and 0 < len(value) <= limit
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@+ -]*', value)
                and '://' not in value and not value.lower().startswith(('bearer ', 'sk-', 'sk_'))):
            return value
        return None
    metadata = {}
    run_id = answer.get('run_id')
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        return metadata
    runtime = answer.get('runtime')
    if isinstance(runtime, dict):
        safe = {}
        for key, limit in (('provider',80), ('model',256), ('route_source',64), ('model_lock',32)):
            value = identifier(runtime.get(key), limit)
            if value is not None: safe[key] = value
        # Requested IDs are labelled separately and never stand in for served IDs.
        requested = runtime.get('requested')
        if isinstance(requested, dict):
            selected = {k: identifier(requested.get(k), limit) for k, limit in (('provider',80),('model',256))}
            selected = {k:v for k,v in selected.items() if v is not None}
            if selected: safe['requested'] = selected
        if safe: metadata['runtime'] = dict(safe, run_id=run_id)
    usage = answer.get('usage')
    if isinstance(usage, dict):
        safe = {k:usage[k] for k in ('input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_write_tokens')
                if type(usage.get(k)) is int and 0 <= usage[k] <= 2**63-1}
        if safe: metadata['usage'] = dict(safe, run_id=run_id)
    return metadata

TARGETS = {'hyeji': ('http://127.0.0.1:8662/v1/runs', '/root/.hermes/.env'),
           'hyerin': ('http://127.0.0.1:8663/v1/runs', '/root/.hermes/profiles/hyerin/.env')}
RUN_ID = re.compile(r'run_[0-9a-f]{32}\Z')
MAX_JSON = 262144
MAX_FRAME = 65536
MAX_STREAM = 1048576
OUTCOMES = {'completed','failed','blocked','cancelled','interrupted','unverified'}
INSTRUCTIONS = '''당신은 실제 작업을 수행하는 담당자입니다. contents의 작업 목표와 acceptance_criteria를 기준으로 도구를 사용해 수행하고 결과를 검증합니다.
의미 있는 진행 상황과 확인된 중간 결과를 commentary 메시지로 한국어로 보고합니다. 승인 또는 추가 입력이 필요한 상태는 명확히 설명합니다.
최종 출력은 JSON 객체입니다: {"outcome":"completed|failed|blocked|cancelled|interrupted|unverified","hyemi_result":{"summary":"요약","completed_steps":[],"evidence":[],"result_links":[],"limitations":[],"next_actions":[]},"telegram_report":"한국어 사용자용 결과 보고","completion_signal":true}.
completed는 실제 실행 증거로 모든 수락 기준을 검증한 경우이며 completion_signal은 이 경우만 true입니다. 미확인 사항과 실패는 해당 outcome과 limitations로 명시합니다. Telegram 전달은 워크플로의 별도 전달 단계가 담당합니다.'''


def failure(code, outcome='unverified'):
    return dict(outcome=outcome, completion_signal=False,
                hyemi_result=dict(summary='작업 완료를 확인할 수 없습니다.',completed_steps=[],evidence=[],
                    result_links=[],limitations=[code],next_actions=[],error={'code':code}),
                telegram_report='작업 상태: '+outcome+' · '+code)


def parse_task_result(raw):
    try:
        if not isinstance(raw,str) or len(raw.encode())>MAX_JSON: raise ValueError()
        stripped=raw.strip()
        fenced=re.fullmatch(r'```(?:json)?\s*\n([\s\S]*?)\n```',stripped)
        r=json.loads(fenced[1] if fenced else stripped)
        if type(r) is not dict or set(r)!={'outcome','hyemi_result','telegram_report','completion_signal'}: raise ValueError()
        if r['outcome'] not in OUTCOMES or type(r['completion_signal']) is not bool: raise ValueError()
        d=r['hyemi_result']
        if not isinstance(d,dict) or not isinstance(d.get('summary'),str) or not d['summary'].strip(): raise ValueError()
        if not isinstance(r['telegram_report'],str) or not r['telegram_report'].strip(): raise ValueError()
        for key in ('completed_steps','evidence','result_links','limitations','next_actions'):
            if not isinstance(d.get(key),list): raise ValueError()
        if r['outcome']=='completed':
            if not r['completion_signal'] or not d['completed_steps'] or not d['evidence']: raise ValueError()
            if any(not isinstance(e,(str,dict)) or not e for e in d['evidence']): raise ValueError()
        elif r['completion_signal']: raise ValueError()
        return validate_result(r,'task')
    except (ValueError,TypeError,KeyError): return failure('format_failed')


def normalize_review(r):
    if not isinstance(r,dict) or r.get('outcome') not in {'changes_required','passed_in_scope','unverified'}:
        return failure('review_format_failed')
    detail=r.get('hyemi_result')
    if not isinstance(detail,dict) or not isinstance(r.get('telegram_report'),str) or not r['telegram_report'].strip():
        return failure('review_format_failed')
    detail=dict(detail)
    detail['review_metadata']={k:v for k,v in r.items() if k not in {'hyemi_result','telegram_report','outcome','completion_signal'}}
    verified=r['outcome']=='passed_in_scope' and bool(detail.get('verified_scope')) and not detail.get('findings') and not detail.get('remaining') and not detail.get('new') and not r.get('error')
    outcome='unverified' if r['outcome']=='passed_in_scope' and not verified else r['outcome']
    result=dict(outcome=outcome,hyemi_result=detail,telegram_report=r['telegram_report'],completion_signal=bool(verified))
    try: return validate_result(result,'review')
    except (ValueError,TypeError,KeyError): return failure('review_format_failed')


def request_json(req):
    with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=10) as response:
        data=response.read(MAX_JSON+1)
        if len(data)>MAX_JSON: raise ValueError('response_limit')
        value=json.loads(data)
        if not isinstance(value,dict): raise ValueError('response_shape')
        return response.status,value


class WorkflowWorkers:
    def __init__(self,config,store,legacy_ingress):
        self.config,self.store,self.legacy_ingress=config,store,legacy_ingress
        self.transport=TelegramTransport(SenderTokens(config))
        from hyemi_delivery import DiscordTransport, DiscordTokens
        self.discord_transport=DiscordTransport(DiscordTokens(), getattr(legacy_ingress,'config',{}).get('allowed',{}))
        self.reviewer=ReviewExecutor(config)
        self.limit=max(1,min(16,int(config.get('workflow_workers',4))))
        self._jobs=ThreadPoolExecutor(max_workers=self.limit,thread_name_prefix='hyemi-work')
        self._streams=ThreadPoolExecutor(max_workers=self.limit,thread_name_prefix='hyemi-progress')
        self._delivery=ThreadPoolExecutor(max_workers=1,thread_name_prefix='hyemi-outbox')
        self._active={}; self._stream_active={}; self._stream_after={}; self._outbox=None
        self._lock=threading.Lock(); self._stop=threading.Event(); self._thread=None
        self._seen_status={}; self._heartbeat={}; self._gaps=set(); self._scheduled={}
        # This object is the deployment's exclusive worker owner, not a read handle.
        self.store.reconcile_outbox()

    def _api_target(self,recipient):
        expected=TARGETS.get(recipient)
        target=getattr(self.legacy_ingress,'config',{}).get('api_targets',{}).get(recipient,{})
        url=target.get('url','').rstrip('/')
        if not url.endswith('/v1/runs'): url+='/v1/runs'
        if expected is None or (url,target.get('env_file'))!=expected: raise ValueError('api_configuration_invalid')
        path=Path(expected[1])
        if str(path.resolve())!=str(path): raise ValueError('api_configuration_invalid')
        keys=[]
        for line in _secure_read(path).splitlines():
            line=line.strip()
            if line.startswith('export '): line=line[7:]
            name,sep,value=line.partition('=')
            if sep and name.strip()=='API_SERVER_KEY':
                values=shlex.split(value,comments=True)
                if len(values)==1: keys.append(values[0])
        if len(keys)!=1 or not keys[0] or any(c in keys[0] for c in '\r\n'): raise ValueError('api_configuration_invalid')
        return url,keys[0]

    def _finish(self,row,result):
        # Render BEFORE the transaction: stored result and outbox are identical.
        self.store.finish(row['event_id'],notification_result(row,result),telegram_phase='final')

    def _enqueue(self,row,phase,text):
        text=render_progress(row,phase,text)
        if row['request'].get('platform') == 'discord':
            from hyemi_formatting import render_discord
            for index,chunk in enumerate(render_discord(text)):
                self.store.enqueue_telegram(row['event_id'],f'{phase}:{index}',chunk['text'],row['request']['to'],chunk)
        else:
            for index,chunk in enumerate(split_text(text)):
                self.store.enqueue_telegram(row['event_id'],f'{phase}:{index}',chunk,row['request']['to'])

    def _notice(self,row,phase,text):
        self._enqueue(row,phase,text)

    def reconcile(self):
        if self._stop.is_set() or not self._lock.acquire(False): return
        try:
            for eid,f in list(self._active.items()):
                if f.done():
                    # Exceptions carry no public URLs/credentials; next cycle reconciles persisted state.
                    try: f.result()
                    except Exception: pass
                    del self._active[eid]
            for eid,f in list(self._stream_active.items()):
                if f.done():
                    try: f.result()
                    except Exception: pass
                    del self._stream_active[eid]
                    self._stream_after[eid]=time.monotonic()+.1
            if self._outbox is None or self._outbox.done():
                self._outbox=self._delivery.submit(self._deliver)
            rows=sorted(self.store.queued_events(),key=lambda r:self._scheduled.get(r['event_id'],0))
            for row in rows:
                eid=row['event_id']
                if eid in self._active: continue
                if len(self._active)>=self.limit: break
                self._scheduled[eid]=time.monotonic()
                if row['kind']=='review':
                    if row['execution_state']!='queued':
                        self._finish(row,failure('review_process_interrupted'))
                        continue
                    self.store.set_execution(eid,'running',started_at=time.time())
                    self._active[eid]=self._jobs.submit(self._review,row)
                else:
                    if row.get('run_id') and eid not in self._stream_active and len(self._stream_active)<self.limit and time.monotonic()>=self._stream_after.get(eid,0):
                        self._stream_active[eid]=self._streams.submit(self._stream,row)
                    self._active[eid]=self._jobs.submit(self._task,row)
        finally: self._lock.release()

    def _review(self,row):
        eid=row['event_id']
        try:
            self._notice(row,'started','검토를 시작했습니다. 제출 내용과 검토 기준을 확인합니다.')
            history=self.store.review_history(row['owner'],row['work_id'],eid,max_entries=12)
            def progress(value):
                # Executor lifecycle only; content remains in structured result.
                self.store.append_update(eid,'review.status',{'status':'running'})
                self._notice(row,'review:progress','검토 진행 중입니다. 제출 내용과 기준을 대조하고 있습니다.')
            result=self.reviewer.run(row['request'],row['session_id'],history=history,progress_callback=progress)
            self._finish(row,normalize_review(result))
        except Exception:
            self._finish(row,failure('review_execution_failed'))

    def _task(self,row):
        eid=row['event_id']
        try: url,key=self._api_target(row['request']['to'])
        except Exception:
            if row.get('attempted_at') is not None or row.get('run_id'):
                self.store.set_execution(eid,row['execution_state'],error='api_configuration_unavailable')
            else:
                self._finish(row,failure('api_configuration_unavailable','failed'))
            return
        identity=hashlib.sha256(key.encode()).hexdigest()
        if row.get('api_url',url)!=url or row.get('api_identity',identity)!=identity:
            self._finish(row,failure('routing_configuration_changed')); return
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}
        if not row.get('run_id'):
            now=time.time(); attempted=row.get('attempted_at')
            if attempted is not None and now-attempted>=3500:
                self.store.set_execution(eid,'unknown',error='admission_unknown_expired'); return
            payload=row.get('payload') or json.dumps(dict(session_id=row['session_id'],input=json.dumps(row['request'],ensure_ascii=False),instructions=INSTRUCTIONS),ensure_ascii=False,separators=(',',':'))
            self.store.set_execution(eid,'starting' if row['execution_state']=='queued' else row['execution_state'],
                payload=payload,api_url=url,api_identity=identity,attempted_at=attempted if attempted is not None else now)
            headers['Idempotency-Key']=eid
            try:
                code,answer=request_json(Request(url,data=payload.encode(),headers=headers,method='POST'))
                runid=answer.get('run_id')
                if code!=202 or not isinstance(runid,str) or not RUN_ID.fullmatch(runid): raise ValueError('admission_shape')
            except HTTPError as exc:
                if 400<=exc.code<500: self._finish(row,failure('admission_http_'+str(exc.code),'failed'))
                else: self.store.set_execution(eid,'unknown',error='admission_unknown')
                return
            except Exception:
                self.store.set_execution(eid,'unknown',error='admission_unknown'); return
            self.store.set_execution(eid,'running',run_id=runid,started_at=now,error=None)
            self._notice(row,'started','작업 요청을 접수하고 실행을 시작했습니다. 확인된 진행 상황과 결과를 전달합니다.')
            return
        if not RUN_ID.fullmatch(row['run_id']): self._finish(row,failure('invalid_run_id')); return
        try:
            code,answer=request_json(Request(url+'/'+row['run_id'],headers=headers))
            if code!=200 or answer.get('run_id')!=row['run_id']: raise ValueError('status_shape')
        except HTTPError as exc:
            if exc.code==404:
                self._gap(row,'run_unavailable')
                self._finish(row,failure('upstream_run_unavailable','interrupted'))
            elif 400<=exc.code<500: self._finish(row,failure('poll_http_'+str(exc.code),'failed'))
            return
        except Exception: return
        status=answer.get('status')
        allowed={'queued','running','waiting_for_approval','waiting_for_input','completed','failed','cancelled','interrupted','blocked'}
        if status not in allowed: return
        if self._seen_status.get(eid)!=status:
            self.store.append_update(eid,'run.status',{'status':status}); self._seen_status[eid]=status
            if status in {'waiting_for_approval','waiting_for_input','blocked'}:
                self._notice(row,'status:'+status,'작업에 승인 또는 추가 입력이 필요합니다. 상태: '+status)
        now=time.monotonic()
        if now-self._heartbeat.get(eid,now)<60: self._heartbeat.setdefault(eid,now)
        else:
            self.store.append_update(eid,'heartbeat',{'status':status}); self._heartbeat[eid]=now
        if status in {'completed','failed','cancelled','interrupted'}:
            result=parse_task_result(answer.get('output')) if status=='completed' else failure('run_'+status,status)
            # Model-authored runtime/usage are not evidence of who served the run.
            result['hyemi_result'].pop('runtime',None)
            result['hyemi_result'].pop('usage',None)
            result['hyemi_result'].update(served_provenance(answer))
            self._finish(row,result)

    def _gap(self,row,reason):
        marker=(row['event_id'],reason)
        if marker not in self._gaps:
            self.store.append_update(row['event_id'],'progress_gap',{'reason':reason,'past_progress':'unavailable'})
            self._gaps.add(marker)

    def _stream(self,row):
        try:
            url,key=self._api_target(row['request']['to'])
            if url!=row.get('api_url') or hashlib.sha256(key.encode()).hexdigest()!=row.get('api_identity'): return
            runid=row.get('run_id','')
            if not RUN_ID.fullmatch(runid): return
            headers={'Authorization':'Bearer '+key,'Accept':'text/event-stream'}
            cursor=row.get('last_upstream_seq',0)
            if cursor: headers['Last-Event-ID']=str(cursor-1)
            req=Request(url+'/'+runid+'/events',headers=headers)
            with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=3) as response:
                consumed=0; frame=[]; size=0; deadline=time.monotonic()+10
                while not self._stop.is_set() and time.monotonic()<deadline:
                    line=response.readline(MAX_FRAME+1)
                    if not line: break
                    consumed+=len(line); size+=len(line)
                    if len(line)>MAX_FRAME or size>MAX_FRAME or consumed>MAX_STREAM:
                        self._gap(row,'stream_limit'); return
                    if line in (b'\n',b'\r\n'):
                        self._frame(row,frame); frame=[]; size=0
                    else: frame.append(line)
        except HTTPError as exc:
            if exc.code in {404,410}: self._gap(row,'stream_unavailable_after_restart')
        except Exception:
            # Reconnect with last durably consumed cursor; never log raw exceptions.
            return

    def _frame(self,row,lines):
        data=[]; ident=None
        for line in lines:
            if line.startswith(b'data:'): data.append(line[5:].strip())
            elif line.startswith(b'id:'): ident=line[3:].strip()
        if not data: return
        try: event=json.loads(b'\n'.join(data))
        except (ValueError,UnicodeError): self._gap(row,'invalid_stream_frame'); return
        if not isinstance(event,dict): return
        kind=event.get('event')
        if kind=='replay.truncated': self._gap(row,'replay_truncated'); return
        seq=event.get('seq')
        if type(seq) is not int or not 0<=seq<2**63-1: return
        if ident is not None and ident!=str(seq).encode(): self._gap(row,'cursor_mismatch'); return
        eid=row['event_id']; current=self.store.event(row['owner'],eid)
        if seq+1<=current.get('last_upstream_seq',0): return
        if seq>current.get('last_upstream_seq',0): self._gap(row,'sequence_gap')
        safe=None
        if kind=='message.interim' and isinstance(event.get('text'),str) and event['text'].strip():
            safe={'text':event['text']}
        elif kind in {'tool.started','tool.completed'} and isinstance(event.get('tool'),str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',event['tool']):
            safe={'tool':event['tool']}
            if kind=='tool.completed': safe['error']=event.get('error') is True
        elif kind in {'approval.request','approval.resolved','input.required','run.blocked','run.completed','run.failed','run.cancelled','run.interrupted','run.started'}:
            safe={'status':kind}
        if safe is not None:
            # append_update atomically records the safe event and advances mapped cursor.
            self.store.append_update(eid,kind,safe,upstream_seq=seq+1)
            if kind=='message.interim': self._enqueue(row,'progress:'+str(seq),safe['text'])
            elif kind in {'approval.request','input.required','run.blocked'}:
                self._enqueue(row,'progress:'+str(seq),'작업에 승인 또는 추가 입력이 필요합니다. 상태: '+kind)
        else:
            # Consumed private/unsupported frames only advance the cursor, never store their data.
            self.store.set_execution(eid,current['execution_state'],last_upstream_seq=seq+1)

    def _deliver(self):
        for row in self.store.outbox_pending():
            if self._stop.is_set(): return
            try:
                event=self.store.event(row['owner'],row['event_id'])
                request=event['request']
                from hyemi_contracts import authorize_destination, ContractError
                try:
                    authorize_destination(request, getattr(self.legacy_ingress,'config',{}).get('allowed',{}))
                    authorized=True
                except ContractError: authorized=False
                discord=request.get('platform') == 'discord'
                transport=self.discord_transport if discord else self.transport
                sender='hyemi' if discord else row['sender']
                expected='hyemi' if event['kind']=='report' else request['to']
                if not authorized or row['sender']!=expected:
                    self.store.mark_blocked(row['id'],error='delivery_routing_configuration_invalid'); continue
                if not transport.ready(sender):
                    self.store.mark_blocked(row['id']); continue
                # Serialize raw chunks after any uncertain/failed predecessor, including restarts.
                if row['phase'].startswith('report:'):
                    previous=self.store.result(row['owner'],row['event_id'])['delivery']
                    if any(d['id']<row['id'] and d['phase'].startswith('report:') and d['state'] not in {'delivered','sent'} for d in previous): continue
                if not self.store.claim_outbox(row['id']): continue
                try:
                    options = {}
                    if (event['kind'] == 'report' or discord) and row.get('rendering') is not None:
                        options['rendering'] = json.loads(row['rendering'])
                    if discord: options.update(recipient=request['to'],logical_author=row['sender'])
                    result=transport.send(sender,row['channelid'],row['text'],**options)
                except Exception:
                    self.store.finish_outbox(row['id'],'unknown',error='delivery_unknown'); continue
                if result.state=='blocked_configuration': self.store.mark_blocked(row['id'])
                else: self.store.finish_outbox(row['id'],result.state,message_id=result.message_id,error=result.error,
                                              destination=getattr(result,'destination',None))
            except Exception:
                # A claim not finalized is recovered as unknown on restart, not resent.
                continue

    def start(self):
        if self._thread and self._thread.is_alive(): return
        if self._stop.is_set(): raise RuntimeError('workers_closed')
        def loop():
            while not self._stop.is_set():
                try: self.reconcile()
                except Exception: pass
                self._stop.wait(.5)
        self._thread=threading.Thread(target=loop,name='hyemi-reconcile',daemon=True); self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=2)
        self._jobs.shutdown(wait=True,cancel_futures=True)
        self._streams.shutdown(wait=True,cancel_futures=True)
        self._delivery.shutdown(wait=True,cancel_futures=True)
