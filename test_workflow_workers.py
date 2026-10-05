"""Local SQLite + real loopback HTTP orchestration checks; no live services."""
import json
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyemi_store import WorkflowStore, StoreConflict
from hyemi_delivery import SendResult, split_text
from hyemi_workers import WorkflowWorkers, parse_task_result


def request(kind='task', **kw):
    r=dict(version=2, kind=kind, event_id=str(uuid.uuid4()), work_id=str(uuid.uuid4()),
           to='hyeji', channelid='-100:2', contents='Perform local fixture work')
    if kind=='task': r['acceptance_criteria']='Fixture checked'
    if kind=='review': r.update(revision=1, criteria='Check text')
    r.update(kw)
    return r


def final():
    return dict(outcome='completed', completion_signal=True, telegram_report='검증 완료',
                hyemi_result=dict(summary='Checked', completed_steps=['checked fixture'],
                evidence=['fixture test passed'], result_links=[], limitations=[], next_actions=[]))


class HTTPFixture(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.posts.append((self.headers.get('Idempotency-Key'), data))
        self.send_response(self.server.post_status); self.end_headers()
        self.wfile.write(json.dumps({'run_id':'run_'+'a'*32}).encode())
    def do_GET(self):
        if self.path.endswith('/events'):
            self.server.cursors.append(self.headers.get('Last-Event-ID'))
            self.send_response(self.server.stream_status); self.end_headers()
            if self.server.stream_status==200: self.wfile.write(self.server.frames)
        else:
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(dict(run_id='run_'+'a'*32,status=self.server.status,
                                            output=self.server.output, **getattr(self.server,'metadata',{}))).encode())


class WorkersTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=WorkflowStore(Path(self.tmp.name)/'ledger.db')
        self.http=ThreadingHTTPServer(('127.0.0.1',0),HTTPFixture)
        self.http.posts=[]; self.http.cursors=[]; self.http.status='running'
        self.http.output=json.dumps(final()); self.http.post_status=202
        self.http.frames=b''; self.http.stream_status=200
        self.server_thread=threading.Thread(target=self.http.serve_forever,daemon=True); self.server_thread.start()
        self.workers=WorkflowWorkers({},self.store,SimpleNamespace(config={'allowed':{'hyeji':{'channels':['-100:2']},'hyerin':{'channels':['-100:2']}}}))
        self.workers._api_target=lambda recipient:(f'http://127.0.0.1:{self.http.server_port}/v1/runs','fixture-key')
        self.sent=[]
        self.workers.transport=SimpleNamespace(ready=lambda sender:False, send=self.send)
    def send(self,sender,channel,text,**kwargs):
        self.sent.append((sender,channel,text)); return SendResult('failed',error='fixture_rejection')
    def tearDown(self):
        self.workers.close(); self.http.shutdown(); self.http.server_close(); self.tmp.cleanup()
    def spin(self, predicate, timeout=4):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            self.workers.reconcile()
            if predicate(): return
            time.sleep(.015)
        self.fail('condition timed out')
    def admit(self,r): self.store.admit('owner',r); return r['event_id']
    def test_report_missing_bot_never_runs(self):
        r=request('report'); eid=self.admit(r)
        self.store.enqueue_telegram(eid,'report:0',r['contents'],'hyemi')
        self.spin(lambda:self.store.result('owner',eid)['delivery'][0]['state']=='blocked_configuration')
        self.assertEqual(self.http.posts,[]); self.assertEqual(self.sent,[])
    def test_task_progress_survives_failed_delivery_and_seq_zero(self):
        r=request(); eid=self.admit(r)
        events=[dict(event='message.interim',seq=0,text='자료 확인 완료'),
                dict(event='tool.started',seq=1,tool='terminal',preview='SECRET',arguments='SECRET'),
                dict(event='reasoning.available',seq=2,text='SECRET')]
        self.http.frames=b''.join(('id: '+str(e['seq'])+'\ndata: '+json.dumps(e)+'\n\n').encode() for e in events)
        self.workers.transport.ready=lambda sender:True
        self.spin(lambda:len(self.store.updates('owner',r['work_id'])['updates'])>=2)
        self.spin(lambda:len(self.http.cursors)>1)
        self.assertIn('2',self.http.cursors)
        self.http.status='completed'
        self.spin(lambda:self.store.result('owner',eid)['availability']=='available')
        result=self.store.result('owner',eid)
        self.assertEqual(result['outcome'],'completed')
        self.assertEqual(len(self.http.posts),1)
        updates=self.store.updates('owner',r['work_id'])['updates']
        self.assertEqual(sum(u['kind']=='message.interim' for u in updates),1)
        self.assertNotIn('SECRET',json.dumps(updates))
    def test_malformed_final_is_unverified_without_rerun(self):
        r=request(); eid=self.admit(r); self.http.status='completed'; self.http.output='not JSON'
        self.spin(lambda:self.store.result('owner',eid)['availability']=='available')
        for _ in range(5): self.workers.reconcile()
        result=self.store.result('owner',eid)
        self.assertEqual(result['outcome'],'unverified'); self.assertFalse(result['completion_signal'])
        self.assertEqual(len(self.http.posts),1)
    def test_partial_report_unknown_stops_later_chunks(self):
        r=request('report',contents='x'*9000); eid=self.admit(r)
        for i,text in enumerate(split_text(r['contents'])): self.store.enqueue_telegram(eid,f'report:{i}',text,'hyemi')
        self.workers.transport.ready=lambda sender:True
        def send(*args,**kwargs): self.sent.append(args); return SendResult('unknown',error='timeout')
        self.workers.transport.send=send
        self.spin(lambda:len(self.sent)==1)
        time.sleep(.05)
        for _ in range(5): self.workers.reconcile(); time.sleep(.02)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.store.result('owner',eid)['delivery'][0]['state'],'unknown')
    def test_restart_missing_stream_gap_and_blocked_input(self):
        r=request(); self.admit(r); self.http.stream_status=404; self.http.status='waiting_for_input'
        self.spin(lambda:any(u['kind']=='progress_gap' for u in self.store.updates('owner',r['work_id'])['updates']))
        self.spin(lambda:any(u['kind']=='run.status' and u['data']['status']=='waiting_for_input' for u in self.store.updates('owner',r['work_id'])['updates']))
        self.assertEqual(len(self.http.posts),1)
    def test_review_round_history_and_stale_version(self):
        calls=[]
        def review(req,sid,history=None,progress_callback=None):
            calls.append(history)
            return dict(kind='review',outcome='passed_in_scope',hyemi_result={'summary':'검토','verified_scope':['contents']},telegram_report='검토 완료',completion_signal=True,limitations=['text only'])
        self.workers.reviewer.run=review
        first=request('review'); eid=self.admit(first)
        self.spin(lambda:self.store.result('owner',eid)['availability']=='available')
        second=request('review',work_id=first['work_id'],revision=2,previous_event_id=eid)
        secondid=self.admit(second)
        self.spin(lambda:self.store.result('owner',secondid)['availability']=='available')
        self.assertEqual(calls[0],[]); self.assertEqual(len(calls[1]),1)
        with self.assertRaises(StoreConflict): self.admit(request('review',work_id=first['work_id'],revision=2))
        self.assertEqual(self.http.posts,[])
    def test_parallel_jobs_serial_same_session(self):
        first=request(); self.admit(first)
        second=request(work_id=first['work_id']); self.admit(second)
        other=request(to='hyerin'); self.admit(other)
        self.spin(lambda:len(self.http.posts)==2)
        self.assertEqual({p[0] for p in self.http.posts},{first['event_id'],other['event_id']})
    def test_final_delivery_is_atomic_obligation(self):
        r=request(); eid=self.admit(r); self.http.status='completed'
        self.spin(lambda:self.store.result('owner',eid)['availability']=='available')
        final_rows=[d for d in self.store.result('owner',eid)['delivery'] if d['phase'].startswith('final:')]
        self.assertEqual(len(final_rows),1)

    def test_admission_http_failure_is_typed_and_not_retried(self):
        r=request(); eid=self.admit(r); self.http.post_status=403
        self.spin(lambda:self.store.result('owner',eid)['availability']=='available')
        self.assertEqual(self.store.result('owner',eid)['outcome'],'failed')
        for _ in range(3): self.workers.reconcile()
        self.assertEqual(len(self.http.posts),1)

    def test_expired_unknown_admission_never_posts(self):
        r=request(); eid=self.admit(r)
        self.store.set_execution(eid,'unknown',attempted_at=time.time()-3601,payload='{}')
        self.spin(lambda:self.store.event('owner',eid).get('error')=='admission_unknown_expired')
        self.assertEqual(self.http.posts,[])

    def test_interrupted_review_never_reruns(self):
        r=request('review'); eid=self.admit(r); self.store.set_execution(eid,'running')
        def forbidden(*args,**kw): self.fail('paid review replayed')
        self.workers.reviewer.run=forbidden
        self.spin(lambda:self.store.result('owner',eid)['availability']=='available')
        result=self.store.result('owner',eid)
        self.assertEqual(result['outcome'],'unverified')
        self.assertIn('review_process_interrupted',result['hyemi_result']['limitations'])
        self.assertTrue(any(d['phase']=='final:0' for d in result['delivery']))

    def test_restart_claim_becomes_unknown_and_is_not_resent(self):
        r=request('report'); eid=self.admit(r)
        oid=self.store.enqueue_telegram(eid,'report:0',r['contents'],'hyemi')
        self.assertTrue(self.store.claim_outbox(oid))
        self.workers.close()
        self.workers=WorkflowWorkers({},self.store,SimpleNamespace(config={'allowed':{'hyeji':{'channels':['-100:2']},'hyerin':{'channels':['-100:2']}}}))
        self.workers.transport=SimpleNamespace(ready=lambda sender:True,send=self.send)
        self.workers.reconcile(); time.sleep(.05)
        self.assertEqual(self.store.result('owner',eid)['delivery'][0]['state'],'unknown')
        self.assertEqual(self.sent,[])

    def test_api_credentials_are_independent_of_bot_credentials(self):
        ingress=SimpleNamespace(config={'api_targets':{'hyeji':{'url':'http://127.0.0.1:8662/v1/runs','env_file':'/root/.hermes/.env'}}})
        original=self.workers.legacy_ingress; self.workers.legacy_ingress=ingress
        try:
            with patch('hyemi_workers._secure_read',return_value='API_SERVER_KEY=fixture_only\nTELEGRAM_BOT_TOKEN=invalid\n'):
                self.assertEqual(WorkflowWorkers._api_target(self.workers,'hyeji'),('http://127.0.0.1:8662/v1/runs','fixture_only'))
        finally: self.workers.legacy_ingress=original

    def test_replay_truncated_and_frame_limit_are_explicit(self):
        r=request(); self.admit(r)
        self.http.frames=b'data: {"event":"replay.truncated"}\n\n'+b'data: '+b'x'*65537+b'\n\n'
        self.spin(lambda:len([u for u in self.store.updates('owner',r['work_id'])['updates'] if u['kind']=='progress_gap'])>=2)
        reasons={u['data']['reason'] for u in self.store.updates('owner',r['work_id'])['updates'] if u['kind']=='progress_gap'}
        self.assertEqual(reasons,{'replay_truncated','stream_limit'})

    def test_slow_review_does_not_block_task_or_delivery(self):
        release=threading.Event()
        def review(*args,**kw): release.wait(2); return {'invalid':True}
        self.workers.reviewer.run=review
        review_request=request('review'); self.admit(review_request)
        report=request('report'); reportid=self.admit(report)
        self.store.enqueue_telegram(reportid,'report:0',report['contents'],'hyemi')
        self.workers.transport.ready=lambda sender:True
        task=request(); self.admit(task)
        try:
            self.spin(lambda:bool(self.http.posts) and bool(self.sent))
            self.assertEqual(self.store.event('owner',review_request['event_id'])['execution_state'],'running')
        finally: release.set()

    def test_unknown_admission_reuses_exact_payload_and_key(self):
        r=request(); eid=self.admit(r); self.http.post_status=503
        self.spin(lambda:self.store.event('owner',eid)['execution_state']=='unknown')
        persisted=self.store.event('owner',eid)['payload']
        self.http.post_status=202
        self.spin(lambda:self.store.event('owner',eid).get('run_id'))
        self.assertGreaterEqual(len(self.http.posts),2)
        self.assertTrue(all(k==eid and value==json.loads(persisted) for k,value in self.http.posts))

    def test_missing_bot_after_run_admission_does_not_block_polling(self):
        r=request(); eid=self.admit(r)
        self.spin(lambda:self.store.event('owner',eid).get('run_id'))
        self.workers.transport.ready=lambda sender:False
        self.http.status='completed'
        self.spin(lambda:self.store.result('owner',eid)['availability']=='available')
        self.assertEqual(self.store.result('owner',eid)['outcome'],'completed')
        self.assertEqual(len(self.http.posts),1)

    def test_changed_delivery_allowlist_blocks_provider(self):
        r=request('report'); eid=self.admit(r)
        self.store.enqueue_telegram(eid,'report:0',r['contents'],'hyemi')
        self.workers.legacy_ingress.config['allowed']['hyeji']['channels']=[]
        self.workers.transport.ready=lambda sender:True
        self.spin(lambda:self.store.result('owner',eid)['delivery'][0]['state']=='blocked_configuration')
        self.assertEqual(self.sent,[])

    def test_exact_json_fence_is_supported_but_prose_is_not(self):
        raw=json.dumps(final())
        self.assertEqual(parse_task_result('```json\n'+raw+'\n```')['outcome'],'completed')
        self.assertEqual(parse_task_result('Here is the output:\n'+raw)['outcome'],'unverified')

    def test_minimal_review_has_authoritative_notification(self):
        r=request('review',title='권한 검토',revision=3)
        eid=self.admit(r)
        self.workers.reviewer.run=lambda *a,**k: dict(outcome='passed_in_scope',
            hyemi_result={'summary':'검토','verified_scope':['contents']},telegram_report='검토 완료')
        self.workers._review(self.store.event('owner',eid))
        result=self.store.result('owner',eid)
        text=result['telegram_report']
        for expected in ('권한 검토',r['work_id'],eid,'review3','상태','확인한 내용','문제점','변경 사항','해결','잔존','신규','차단 요인','다음 행동','전체 소스'):
            self.assertIn(expected,text)
        self.assertEqual(result['hyemi_result']['notification']['model_telegram_report'],'검토 완료')
        chunks=[d['text'] for d in self.store.outbox_pending() if d['phase'].startswith('final:')]
        self.assertEqual(''.join(chunks),text)

    def test_actual_served_runtime_and_usage_survive_terminal_states(self):
        for status in ('completed','failed','interrupted','cancelled'):
            with self.subTest(status=status):
                r=request(); eid=self.admit(r)
                self.http.status=status
                self.http.metadata={'model':'requested-not-served',
                    'runtime':{'provider':'fallback-provider','model':'served-model',
                        'route_source':'model_routes','source':'SECRET','api_key':'SECRET',
                        'requested':{'model':'requested-model','provider':'primary','headers':'SECRET'}},
                    'usage':{'input_tokens':123,'output_tokens':45,'total_tokens':168,
                        'cache_read_tokens':10,'cache_write_tokens':2,'headers':'SECRET'}}
                self.workers._task(self.store.event('owner',eid))
                self.workers._task(self.store.event('owner',eid))
                detail=self.store.result('owner',eid)['hyemi_result']
                self.assertEqual(detail['runtime']['model'],'served-model')
                self.assertEqual(detail['runtime']['provider'],'fallback-provider')
                self.assertEqual(detail['runtime']['route_source'],'model_routes')
                self.assertEqual(detail['runtime']['run_id'],'run_'+'a'*32)
                self.assertEqual(detail['usage']['input_tokens'],123)
                self.assertNotIn('SECRET',json.dumps(detail))

    def test_delivery_forwards_verified_destination_proof(self):
        r=request('report'); eid=self.admit(r)
        self.store.enqueue_telegram(eid,'report:0',r['contents'],'hyemi')
        proof={'chat_id':'-100','message_thread_id':2,'general':False}
        self.workers.transport=SimpleNamespace(ready=lambda sender:True,
            send=lambda *args,**kwargs:SendResult('delivered',message_id=51,destination=proof))
        self.workers._deliver()
        receipt=self.store.result('owner',eid)['delivery'][0]
        self.assertEqual(receipt['state'],'delivered')
        self.assertEqual(receipt['verified_destination'],proof)

    def test_model_runtime_is_not_used_when_actual_metadata_absent(self):
        r=request(); eid=self.admit(r)
        output=final()
        output['hyemi_result'].update(runtime={'model':'made-up'},usage={'total_tokens':999})
        self.http.output=json.dumps(output); self.http.status='completed'
        self.workers._task(self.store.event('owner',eid))
        self.workers._task(self.store.event('owner',eid))
        detail=self.store.result('owner',eid)['hyemi_result']
        self.assertNotIn('runtime',detail)
        self.assertNotIn('usage',detail)

    def test_completed_requires_evidence(self):
        value=final(); value['hyemi_result']['evidence']=[]
        self.assertEqual(parse_task_result(json.dumps(value))['outcome'],'unverified')

if __name__=='__main__': unittest.main()
