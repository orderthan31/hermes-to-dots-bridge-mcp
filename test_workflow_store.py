"""Task A regression suite. Run: python -m unittest test_workflow_store -v."""
import concurrent.futures
import os
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from hyemi_contracts import ContractError, canonical_uuid, normalize_request, owner_id, session_key
from hyemi_store import WorkflowStore, StoreConflict, NotFound

ALLOWED = {'hyeji': {'home': '-1001', 'channels': ['-1002']}, 'hyerin': {'home': '-2001', 'channels': []}}
def request(kind='task', **kw):
    value = dict(version=2, kind=kind, event_id=str(uuid4()), work_id=str(uuid4()), to='hyeji', channelid='-1001', contents='원문\n exact ')
    if kind == 'task': value['acceptance_criteria'] = 'real tool evidence'
    if kind == 'review': value.update(revision=1, criteria='precise improvements')
    value.update(kw)
    return normalize_request(value, ALLOWED)

class Contracts(unittest.TestCase):
    def test_strict(self):
        r=request()
        for patch in ({'version':True},{'contents':''},{'contents':'가'*14000},{'extra':1},{'channelid':'other'},{'event_id':r['event_id'].upper()}, {'title':'x'*201}, {'acceptance_criteria':False}):
            with self.subTest(patch=str(patch)[:80]), self.assertRaises(ContractError): normalize_request(dict(r, **patch), ALLOWED)
        with self.assertRaises(ContractError): request('review', revision=True)
        with self.assertRaises(ContractError): request('report', criteria='bad')
        alias=request(to='hyelin', channelid='-2001')
        self.assertEqual(alias['to'],'hyerin')
        self.assertEqual(canonical_uuid(r['event_id']),r['event_id'])
    def test_identity(self):
        self.assertNotEqual(owner_id('ab','c'),owner_id('a','bc'))
        w=str(uuid4()); a=owner_id('issuer','one'); b=owner_id('issuer','two')
        keys={session_key(a,w,'hyeji','task'),session_key(b,w,'hyeji','task'),session_key(a,w,'hyerin','task'),session_key(a,w,'hyeji','review')}
        self.assertEqual(len(keys),4)

class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=self.tmp.name+'/state.sqlite'; self.s=WorkflowStore(self.path); self.owner=owner_id('i','s')
    def tearDown(self): self.tmp.cleanup()
    def add(self,kind='task',**kw):
        r=request(kind,**kw); self.s.admit(self.owner,r); return r
    def test_duplicate_conflict_isolation(self):
        r=self.add(); self.assertEqual(self.s.admit(self.owner,r)['status'],'duplicate')
        for owner, value in [(self.owner,dict(r,contents='changed')),('other',r)]:
            with self.assertRaises(StoreConflict): self.s.admit(owner,value)
        for fn,args in [(self.s.event,(r['event_id'],)),(self.s.work_status,(r['work_id'],)),(self.s.updates,(r['work_id'],)),(self.s.result,(r['event_id'],)),(self.s.report,(r['event_id'],)),(self.s.record_query,(r['work_id'],'status'))]:
            with self.assertRaises(NotFound): fn('other',*args)
    def test_concurrent_retry(self):
        r=request()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            states=list(pool.map(lambda _:self.s.admit(self.owner,r)['status'],range(16)))
        self.assertEqual(states.count('accepted'),1); self.assertEqual(states.count('duplicate'),15)
    def test_report_attaches_without_changing_task(self):
        r=self.add(); report=self.add('report',work_id=r['work_id'], revision=1)
        self.assertEqual(self.s.report(self.owner,report['event_id']),report)
        status=self.s.work_status(self.owner,r['work_id'])
        self.assertEqual(status['kind'],'task'); self.assertEqual(status['latest_event_id'],r['event_id'])
        self.assertNotEqual(self.s.event(self.owner,r['event_id'])['session_id'],self.s.event(self.owner,report['event_id'])['session_id'])
        only=self.add('report')
        with self.assertRaises(StoreConflict): self.add(work_id=only['work_id'])
    def test_continuity_history(self):
        old=self.add('review'); newer=self.add('review',work_id=old['work_id'],revision=2,previous_event_id=old['event_id'],changes='fixed')
        self.s=WorkflowStore(self.path)
        self.assertEqual(self.s.event(self.owner,old['event_id'])['session_id'],self.s.event(self.owner,newer['event_id'])['session_id'])
        self.s.finish(newer['event_id'],outcome('review','passed_in_scope'))
        self.s.finish(old['event_id'],outcome('review','changes_required'))
        self.assertEqual(self.s.work_status(self.owner,old['work_id'])['outcome'],'passed_in_scope')
        self.assertEqual(self.s.result(self.owner,old['event_id'])['outcome'],'changes_required')
        with self.assertRaises(StoreConflict): self.add('review',work_id=old['work_id'],revision=2)
        with self.assertRaises(StoreConflict): self.add('review',work_id=old['work_id'],revision=3,previous_event_id=str(uuid4()))
        with self.assertRaises(StoreConflict): self.add('review',work_id=old['work_id'],revision=3,to='hyerin',channelid='-2001')
    def test_progress_atomic_cursor_and_limits(self):
        r=self.add(); eid=r['event_id']
        self.s.append_update(eid,'progress',{'text':'one'},upstream_seq=1)
        self.s.append_update(eid,'progress',{'text':'one'},upstream_seq=1)
        self.s.append_update(eid,'progress',{'text':'two'},upstream_seq=2)
        self.assertEqual(self.s.event(self.owner,eid)['last_upstream_seq'],2)
        page=self.s.updates(self.owner,r['work_id'],limit=1)
        self.assertEqual(len(page['updates']),1); self.assertTrue(page['has_more'])
        self.assertEqual(len(self.s.updates(self.owner,r['work_id'],page['next_seq'])['updates']),2)
        for kwargs in ({'limit':0},{'limit':101},{'after_seq':-1},{'after_seq':True}):
            with self.assertRaises((ValueError,ContractError)): self.s.updates(self.owner,r['work_id'],**kwargs)
    def test_outbox_restart_and_independent_result(self):
        r=self.add(); eid=r['event_id']; oid=self.s.enqueue_telegram(eid,'result','hello','hyeji')
        self.assertEqual(oid,self.s.enqueue_telegram(eid,'result','hello','hyeji'))
        self.assertTrue(self.s.claim_outbox(oid)); self.assertFalse(self.s.claim_outbox(oid))
        self.s=WorkflowStore(self.path)
        self.assertEqual(self.s.result(self.owner,eid)['delivery'][0]['state'],'sending')
        self.s.reconcile_outbox()
        self.assertEqual(self.s.outbox_pending(),[])
        self.assertEqual(self.s.result(self.owner,eid)['delivery'][0]['state'],'unknown')
        self.s.finish(eid,outcome())
        with self.assertRaises(StoreConflict): self.s.finish_outbox(oid,'failed',error='network')
        result=self.s.result(self.owner,eid)
        self.assertEqual(result['availability'],'available'); self.assertEqual(result['delivery'][0]['state'],'unknown')
        self.assertEqual(result['outcome'],'completed'); self.assertEqual(self.s.queued_events(),[])
    def test_serial_queue_and_query_receipt(self):
        a=self.add(); b=self.add(work_id=a['work_id'])
        self.assertEqual([x['event_id'] for x in self.s.queued_events()],[a['event_id']])
        self.s.set_execution(a['event_id'],'running',run_id='run-1',api_identity='hyeji')
        self.assertEqual(self.s.queued_events()[0]['execution_state'],'running')
        receipt=self.s.record_query(self.owner,a['work_id'],'status')
        self.assertEqual(receipt['meaning'],'authenticated_lookup'); self.assertNotIn('read',receipt)
        self.s.finish(a['event_id'],outcome())
        self.assertEqual(self.s.queued_events()[0]['event_id'],b['event_id'])
    def test_execution_does_not_requeue_uncertain_attempt(self):
        r=self.add(); eid=r['event_id']
        self.s.set_execution(eid,'starting',payload={'contents':'safe'},attempted_at=1)
        self.s.set_execution(eid,'unknown',error='response lost')
        with self.assertRaises(StoreConflict): self.s.set_execution(eid,'queued')
        self.s.set_execution(eid,'running',run_id='reconciled')
        with self.assertRaises(StoreConflict): self.s.set_execution(eid,'starting')
    def test_result_requires_structured_hyemi_result(self):
        r=self.add()
        with self.assertRaises(ContractError):
            self.s.finish(r['event_id'],dict(hyemi_result='unparsed text',telegram_report='hello',outcome='success',completion_signal=True))
    def test_same_work_other_owner_session_isolated(self):
        r=self.add(); other=dict(r,event_id=str(uuid4()))
        admitted=self.s.admit('other-owner',other)
        self.assertNotEqual(admitted['session_id'],self.s.event(self.owner,r['event_id'])['session_id'])
        self.assertEqual(self.s.work_status('other-owner',r['work_id'])['latest_event_id'],other['event_id'])
    def test_conflicting_progress_rolls_back(self):
        r=self.add(); eid=r['event_id']
        seq=self.s.append_update(eid,'progress',{'a':1},upstream_seq=1)
        with self.assertRaises(StoreConflict): self.s.append_update(eid,'progress',{'a':2},upstream_seq=1)
        self.assertEqual(self.s.event(self.owner,eid)['last_upstream_seq'],1)
        self.assertEqual(self.s.updates(self.owner,r['work_id'])['next_seq'],seq)
        self.assertEqual(len(self.s.updates(self.owner,r['work_id'])['updates']),2)
    def test_permissions_legacy_untouched(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777,0o600)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE legacy (value TEXT)'); db.execute("INSERT INTO legacy VALUES ('unchanged')")
        WorkflowStore(self.path)
        with sqlite3.connect(self.path) as db: self.assertEqual(db.execute('SELECT value FROM legacy').fetchone()[0],'unchanged')

def outcome(kind='task', status='completed'):
    return dict(hyemi_result={'summary':'검증 결과', 'evidence':['실제 테스트 통과']},
                telegram_report='검증 결과입니다', outcome=status,
                completion_signal=status in {'completed','passed_in_scope'})

class SpecRepairs(unittest.TestCase):
    setUp=Ledger.setUp
    tearDown=Ledger.tearDown
    add=Ledger.add
    def test_verified_delivery_receipt(self):
        r=self.add(); oid=self.s.enqueue_telegram(r['event_id'],'final','내용','hyeji')
        self.s.claim_outbox(oid)
        self.s.finish_outbox(oid,'delivered',message_id=42)
        receipt=self.s.result(self.owner,r['event_id'])['delivery'][0]
        self.assertEqual(receipt['state'],'unknown')
        self.assertFalse(receipt['confirmed_destination'])
        self.assertFalse(self.s.retry_failed_outbox(oid))
        r=self.add(); oid=self.s.enqueue_telegram(r['event_id'],'final','내용','hyeji')
        self.s.claim_outbox(oid)
        proof=dict(chat_id='-1001',message_thread_id=None,general=True)
        self.s.finish_outbox(oid,'delivered',message_id=42,destination=proof)
        receipt=WorkflowStore(self.path).result(self.owner,r['event_id'])['delivery'][0]
        self.assertTrue(receipt['confirmed_destination'])
        self.assertEqual(receipt['verified_destination'],proof)

    @staticmethod
    def finding_result(new=(), resolved=(), remaining=()):
        result=outcome('review','changes_required' if new or remaining else 'passed_in_scope')
        result['hyemi_result'].update(findings=[dict(id=f,location='문장',issue='오류',reason='이유',improvement='수정',resolution_criteria='해결') for f in (*remaining,*new)],
                                     new=list(new),resolved=list(resolved),remaining=list(remaining))
        return result

    def test_mismatched_proof_and_legacy_receipts(self):
        for proof in [dict(chat_id='-999',message_thread_id=None,general=True),
                      dict(chat_id='-1001',message_thread_id=99,general=False),
                      dict(chat_id='-1001',message_thread_id=None,general=1)]:
            r=self.add(); oid=self.s.enqueue_telegram(r['event_id'],'final','내용','hyeji')
            self.s.claim_outbox(oid)
            self.s.finish_outbox(oid,'delivered',message_id=1,destination=proof)
            receipt=self.s.result(self.owner,r['event_id'])['delivery'][0]
            self.assertEqual(receipt['state'],'unknown'); self.assertIsNone(receipt['verified_destination'])
            self.assertFalse(self.s.retry_failed_outbox(oid))
        # Simulate a pre-proof database and reopen through the real migration.
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE v2_outbox DROP COLUMN destination')
            db.execute("UPDATE v2_outbox SET state='sent'")
        self.s=WorkflowStore(self.path)
        receipt=self.s.result(self.owner,r['event_id'])['delivery'][0]
        self.assertEqual(receipt['state'],'delivered')
        self.assertFalse(receipt['confirmed_destination'])
        self.assertIsNone(receipt['verified_destination'])

    def test_scope_unverified_findings_are_durable_and_reports_excluded(self):
        first=self.add('review'); result=self.finding_result(new=['F1'])
        result.update(outcome='unverified',completion_signal=False)
        self.s.finish(first['event_id'],result)
        self.add('report',work_id=first['work_id'],contents='F99 arbitrary report')
        other=self.add('review'); self.s.finish(other['event_id'],self.finding_result(new=['OTHER']))
        current=self.add('review',work_id=first['work_id'],revision=2)
        history=self.s.review_history(self.owner,first['work_id'],current['event_id'])
        self.assertEqual(history[-1]['known_finding_ids'],['F1'])
        self.assertEqual(history[-1]['active_finding_ids'],['F1'])
        self.s.finish(current['event_id'],self.finding_result(remaining=['F1'],new=['F2']))

    def test_all_round_finding_registry_and_isolation(self):
        first=self.add('review'); wid=first['work_id']
        self.s.finish(first['event_id'],self.finding_result(new=['F1']))
        prev=first
        for revision in range(2,15):
            r=self.add('review',work_id=wid,revision=revision)
            self.s.finish(r['event_id'],self.finding_result(new=['F'+str(revision)],resolved=['F'+str(revision-1)]))
            prev=r
        current=self.add('review',work_id=wid,revision=15)
        self.s=WorkflowStore(self.path)
        history=self.s.review_history(self.owner,wid,current['event_id'])
        self.assertEqual(len(history),12)
        self.assertIn('F1',history[-1]['known_finding_ids'])
        self.assertEqual(history[-1]['active_finding_ids'],['F14'])
        with self.assertRaises(StoreConflict):
            self.s.finish(current['event_id'],self.finding_result(new=['F1'],resolved=['F14']),telegram_phase='final')
        self.assertIsNone(self.s.result_for_internal(current['event_id']))
        self.assertEqual(self.s.outbox_pending(),[])
        with self.assertRaises(StoreConflict):
            self.s.finish(current['event_id'],self.finding_result(new=['F15'],resolved=['F13']))
        self.s.finish(current['event_id'],self.finding_result(new=['F15'],resolved=['F14']))
        other=request('review',work_id=wid); self.s.admit('other',other)
        self.s.finish(other['event_id'],self.finding_result(new=['F1']))

    def test_failed_review_preserves_active_registry(self):
        from hyemi_contracts import normalize_executor_result
        from hyemi_review import failure
        first=self.add('review'); self.s.finish(first['event_id'],self.finding_result(new=['F1']))
        failed=self.add('review',work_id=first['work_id'],revision=2)
        self.s.finish(failed['event_id'],normalize_executor_result(failure('format_failed'),'review'))
        current=self.add('review',work_id=first['work_id'],revision=3)
        with self.assertRaises(StoreConflict):
            self.s.finish(current['event_id'],self.finding_result(new=['F2']))
        self.s.finish(current['event_id'],self.finding_result(resolved=['F1']))

    def test_completion_contract(self):
        r=self.add('review')
        bad=outcome('review','changes_required'); bad['completion_signal']=True
        with self.assertRaises(ContractError): self.s.finish(r['event_id'],bad)
        self.s.finish(r['event_id'],outcome('review','changes_required'))
        self.assertFalse(self.s.result(self.owner,r['event_id'])['completion_signal'])

    def test_terminal_guards(self):
        r=self.add()
        with self.assertRaises(StoreConflict): self.s.set_execution(r['event_id'],'finished')
        for terminal in ('failed','cancelled'):
            r=self.add(); self.s.set_execution(r['event_id'],terminal)
            with self.assertRaises(StoreConflict): self.s.finish(r['event_id'],outcome())
            self.s.finish(r['event_id'],outcome(status=terminal))
            self.s.finish(r['event_id'],outcome(status=terminal))
            self.assertEqual(self.s.event(self.owner,r['event_id'])['execution_state'],terminal)

    def test_atomic_final_and_blocked(self):
        r=self.add(); eid=r['event_id']; result=outcome()
        self.s.finish(eid,result,telegram_phase='final')
        rows=self.s.outbox_pending(); self.assertEqual(len(rows),1)
        row=rows[0]; self.assertEqual(row['channelid'],r['channelid'])
        self.assertEqual(row['owner'],self.owner); self.assertEqual(row['sender'],'hyeji')
        self.s.mark_blocked(row['id'])
        self.assertEqual(self.s.outbox_pending()[0]['state'],'blocked_configuration')
        self.assertEqual(self.s.result(self.owner,eid)['availability'],'available')
        self.assertTrue(self.s.claim_outbox(row['id']))
        self.s.finish_outbox(row['id'],'delivered',message_id=1,destination=dict(chat_id='-1001',message_thread_id=None,general=True))
        self.s.finish(eid,result,telegram_phase='final')
        self.assertEqual(self.s.outbox_pending(),[])

    def test_report_lossless_availability_and_order(self):
        r=self.add('report',contents='x'*4000+' '*4000+'끝')
        eid=r['event_id']; rows=self.s.outbox_pending()
        self.assertEqual(''.join(x['text'] for x in rows),r['contents'])
        self.assertEqual(self.s.result(self.owner,eid)['contents'],r['contents'])
        self.assertTrue(self.s.result(self.owner,eid)['raw_report_available'])
        self.assertEqual(self.s.work_status(self.owner,r['work_id'])['availability'],'available')
        with self.assertRaises(ContractError): self.s.enqueue_telegram(eid,'wrong','내용','hyeji')
        self.assertFalse(self.s.claim_outbox(rows[1]['id']))
        self.assertTrue(self.s.claim_outbox(rows[0]['id']))
        self.s.finish_outbox(rows[0]['id'],'failed')
        self.assertFalse(self.s.claim_outbox(rows[1]['id']))
        self.assertNotIn(rows[0]['id'],[x['id'] for x in self.s.outbox_pending()])

    def test_atomic_finish_rollback(self):
        r=self.add(); eid=r['event_id']
        self.s.enqueue_telegram(eid,'final:0','다른 내용','hyeji')
        with self.assertRaises(StoreConflict): self.s.finish(eid,outcome(),telegram_phase='final')
        self.assertIsNone(self.s.result_for_internal(eid))
        self.assertEqual(self.s.event(self.owner,eid)['execution_state'],'queued')

    def test_no_false_completion_or_empty_evidence(self):
        from hyemi_contracts import validate_result
        for status in ('failed','blocked','cancelled','interrupted','unverified'):
            bad=outcome(status=status); bad['completion_signal']=True
            with self.assertRaises(ContractError): validate_result(bad,'task')
        for evidence in (None,[],[' '],{},[{}],False):
            bad=outcome(); bad['hyemi_result']['evidence']=evidence
            with self.assertRaises(ContractError): validate_result(bad,'task')
        bad=outcome(); bad['telegram_report']=' '
        with self.assertRaises(ContractError): validate_result(bad,'task')

    def test_actual_executor_failure_adapter(self):
        from hyemi_contracts import normalize_executor_result
        from hyemi_review import failure
        raw=failure('format_failed')
        result=normalize_executor_result(raw,'review')
        self.assertEqual(set(result),{'hyemi_result','telegram_report','outcome','completion_signal'})
        self.assertEqual(result['hyemi_result']['error'],raw['error'])
        self.assertFalse(result['completion_signal'])
        r=self.add('review'); self.s.finish(r['event_id'],result,telegram_phase='final')
        self.assertEqual(self.s.result_for_internal(r['event_id']),result)

    def test_actual_executor_success_adapter(self):
        import json
        from hyemi_contracts import normalize_executor_result
        from hyemi_review import parse_result, make_job
        from test_review_executor import good, REQUEST
        raw=parse_result(json.dumps(good('passed_in_scope')),
                         make_job(dict(REQUEST,contents='1+1=2'),'test'))
        result=normalize_executor_result(raw,'review')
        self.assertTrue(result['completion_signal'])
        r=self.add('review'); self.s.finish(r['event_id'],result,telegram_phase='final')
        self.assertEqual(self.s.result_for_internal(r['event_id']),result)

    def test_lossless_executor_success_adapter(self):
        from hyemi_contracts import normalize_executor_result
        raw=dict(outcome('review','passed_in_scope'),kind='review',backend={'version':'custom'},
                 verified_scope=['텍스트'],limitations=['외부 미검증'],criterion_checks=[{'passed':True}])
        raw['completion_signal']='검토 범위 내 통과'
        result=normalize_executor_result(raw,'review')
        self.assertTrue(result['completion_signal'])
        for key in ('kind','backend','verified_scope','limitations','criterion_checks'):
            self.assertEqual(result['hyemi_result'][key],raw[key])

    def test_history_owner_bound_and_accepted_once(self):
        a=self.add('review'); self.s.finish(a['event_id'],outcome('review','changes_required'))
        b=self.add('review',work_id=a['work_id'],revision=2,previous_event_id=a['event_id'])
        self.s.admit(self.owner,a)
        history=WorkflowStore(self.path).review_history(self.owner,a['work_id'],b['event_id'])
        self.assertEqual(len(history),1); self.assertEqual(history[0]['request'],a)
        self.assertEqual(history[0]['hyemi_result']['summary'],'검증 결과')
        with self.assertRaises(NotFound): self.s.review_history('other',a['work_id'],b['event_id'])
        updates=self.s.updates(self.owner,a['work_id'])['updates']
        self.assertEqual(len([x for x in updates if x['event_id']==a['event_id'] and x['kind']=='accepted']),1)

if __name__=='__main__': unittest.main()
