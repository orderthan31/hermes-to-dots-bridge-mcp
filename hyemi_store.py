"""Durable owner-scoped v2 workflow ledger (stdlib SQLite).

Only v2_* tables are created. Public reads take owner first and hide internal
run/payload/API metadata. Worker methods are trusted in-process interfaces.
The exclusive delivery worker calls reconcile_outbox on startup; constructing
read handles never mutates active claims. Unknown sends never auto-replay.
queued_events returns the oldest unfinished execution per session (active rows
included). Reports are raw storage, never executable jobs, and are excluded.
Each call uses a separate transaction/connection; instances are thread-safe.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from hyemi_contracts import (ContractError, canonical_json, canonical_uuid, integer,
                             session_key, text, validate_request, validate_result)

class StoreConflict(ValueError): pass
class NotFound(KeyError): pass

TERMINAL = {'finished','failed','cancelled'}
STATES = {'queued','starting','running','unknown',*TERMINAL}
FIELDS = {'run_id','payload','api_identity','api_url','attempted_at','last_upstream_seq','error',
          'started_at','finished_at','updated_at','poll_after','attempt_count'}

class WorkflowStore:
    def __init__(self, state_path):
        self.path = str(Path(state_path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS v2_works (
              owner TEXT NOT NULL, work_id TEXT NOT NULL, recipient TEXT NOT NULL,
              role TEXT NOT NULL, latest_event_id TEXT NOT NULL, revision INTEGER NOT NULL,
              PRIMARY KEY(owner,work_id));
            CREATE TABLE IF NOT EXISTS v2_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
              owner TEXT NOT NULL, work_id TEXT NOT NULL, kind TEXT NOT NULL,
              session_id TEXT NOT NULL, request TEXT NOT NULL,
              execution_state TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
              last_upstream_seq INTEGER NOT NULL DEFAULT 0, result TEXT,
              created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS v2_event_work ON v2_events(owner,work_id,id);
            CREATE TABLE IF NOT EXISTS v2_updates (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
              kind TEXT NOT NULL, data TEXT NOT NULL, upstream_seq INTEGER,
              created_at REAL NOT NULL, UNIQUE(event_id,upstream_seq));
            CREATE TABLE IF NOT EXISTS v2_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
              phase TEXT NOT NULL, text TEXT NOT NULL, sender TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', message_id TEXT, error TEXT,
              created_at REAL NOT NULL, UNIQUE(event_id,phase));
            CREATE TABLE IF NOT EXISTS v2_queries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
              work_id TEXT NOT NULL, operation TEXT NOT NULL, cursor INTEGER,
              created_at REAL NOT NULL);
            ''')
            if 'destination' not in {row['name'] for row in db.execute('PRAGMA table_info(v2_outbox)')}:
                db.execute('ALTER TABLE v2_outbox ADD COLUMN destination TEXT')
            if 'rendering' not in {row['name'] for row in db.execute('PRAGMA table_info(v2_outbox)')}:
                db.execute('ALTER TABLE v2_outbox ADD COLUMN rendering TEXT')
            # Claims are reconciled by the single worker at startup, not by readers.
            db.execute("UPDATE v2_outbox SET state='delivered' WHERE state='sent'")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction: db.commit()
        except BaseException:
            if db.in_transaction: db.rollback()
            raise
        finally: db.close()

    @staticmethod
    def _event(db,eid,owner=None):
        canonical_uuid(eid)
        row=db.execute('SELECT * FROM v2_events WHERE event_id=?',(eid,)).fetchone()
        if row is None or (owner is not None and row['owner'] != owner): raise NotFound('not_found')
        return row

    @staticmethod
    def _work(db,owner,wid):
        text(owner,'owner'); canonical_uuid(wid)
        row=db.execute('SELECT * FROM v2_works WHERE owner=? AND work_id=?',(owner,wid)).fetchone()
        if row is None: raise NotFound('not_found')
        return row

    @staticmethod
    def _internal(row):
        value=dict(row); value['request']=json.loads(value['request'])
        value.update(json.loads(value.pop('metadata')))
        value['result']=json.loads(value['result']) if value['result'] is not None else None
        return value

    def admit(self,owner,request):
        text(owner,'owner'); r=validate_request(request); encoded=canonical_json(r)
        eid,wid=r['event_id'],r['work_id']
        with self._db() as db:
            old=db.execute('SELECT * FROM v2_events WHERE event_id=?',(eid,)).fetchone()
            if old:
                if old['owner'] != owner or old['request'] != encoded: raise StoreConflict('event_id content or owner conflict')
                return dict(event_id=eid,work_id=wid,status='duplicate',session_id=old['session_id'])
            # Freeze the complete presentation before admitting anything. Duplicates
            # return above, preserving legacy/current plans and all delivery states.
            plan = None
            if r['kind'] == 'report':
                from hyemi_formatting import render_report, render_discord
                plan = (render_discord if r.get('platform') == 'discord' else render_report)(r['contents'])
            work=db.execute('SELECT * FROM v2_works WHERE owner=? AND work_id=?',(owner,wid)).fetchone()
            if work:
                if work['recipient'] != r['to']: raise StoreConflict('work recipient is immutable')
                if r['kind'] != 'report' and work['role'] != r['kind']: raise StoreConflict('work execution role is immutable')
                if r['kind']=='review' and r['revision'] <= work['revision']: raise StoreConflict('review revision must increase')
            if 'previous_event_id' in r:
                prev=db.execute('SELECT * FROM v2_events WHERE event_id=?',(r['previous_event_id'],)).fetchone()
                if not prev or prev['owner'] != owner or prev['work_id'] != wid or prev['kind'] != 'review':
                    raise StoreConflict('previous_event_id must refer to this review work')
            sid=session_key(owner,wid,r['to'],r['kind'])
            state='finished' if r['kind']=='report' else 'queued'
            db.execute('INSERT INTO v2_events(event_id,owner,work_id,kind,session_id,request,execution_state,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (eid,owner,wid,r['kind'],sid,encoded,state,time.time()))
            if not work:
                db.execute('INSERT INTO v2_works VALUES(?,?,?,?,?,?)',(owner,wid,r['to'],r['kind'],eid,r.get('revision',0)))
            elif r['kind'] != 'report' or work['role']=='report':
                db.execute('UPDATE v2_works SET latest_event_id=?,revision=? WHERE owner=? AND work_id=?',(eid,r.get('revision',0),owner,wid))
            db.execute('INSERT INTO v2_updates(event_id,kind,data,created_at) VALUES(?,?,?,?)',
                       (eid,'accepted',canonical_json({'status':'accepted'}),time.time()))
            if r['kind']=='report':
                for index,chunk in enumerate(plan):
                    self._enqueue(db,eid,'report:'+str(index),chunk['text'],'hyemi',chunk)
            return dict(event_id=eid,work_id=wid,status='accepted',session_id=sid)

    def event(self,owner,eid):
        text(owner,'owner')
        with self._db() as db: return self._internal(self._event(db,eid,owner))

    def work_status(self,owner,wid):
        with self._db() as db:
            w=self._work(db,owner,wid); e=self._event(db,w['latest_event_id'],owner)
            result=json.loads(e['result']) if e['result'] else {}
            return dict(work_id=wid,to=w['recipient'],kind=w['role'],revision=w['revision'],latest_event_id=w['latest_event_id'],
                        execution_state=e['execution_state'],availability='available' if e['result'] or e['kind']=='report' else 'pending',
                        raw_report_available=e['kind']=='report',
                        outcome=result.get('outcome'),completion_signal=result.get('completion_signal'))

    def updates(self,owner,wid,after_seq=0,limit=50):
        integer(after_seq,'after_seq'); integer(limit,'limit',1,100)
        with self._db() as db:
            self._work(db,owner,wid)
            rows=db.execute('''SELECT u.* FROM v2_updates u JOIN v2_events e ON e.event_id=u.event_id
              WHERE e.owner=? AND e.work_id=? AND u.seq>? ORDER BY u.seq LIMIT ?''',(owner,wid,after_seq,limit+1)).fetchall()
            entries=[dict(seq=r['seq'],event_id=r['event_id'],kind=r['kind'],data=json.loads(r['data']),created_at=r['created_at']) for r in rows[:limit]]
            return dict(updates=entries,next_seq=entries[-1]['seq'] if entries else after_seq,has_more=len(rows)>limit)

    def report(self,owner,eid):
        value=self.event(owner,eid)
        if value['kind'] != 'report': raise NotFound('not_found')
        return value['request']

    def result(self,owner,eid):
        text(owner,'owner')
        with self._db() as db:
            e=self._event(db,eid,owner)
            value=json.loads(e['result']) if e['result'] is not None else {}
            delivery=[dict(id=r['id'],phase=r['phase'],state=r['state'],message_id=r['message_id'],
                           verified_destination=json.loads(r['destination']) if r['destination'] else None,
                           confirmed_destination=r['state']=='delivered' and r['destination'] is not None)
                      for r in db.execute('SELECT * FROM v2_outbox WHERE event_id=? ORDER BY id',(eid,))]
            request=json.loads(e['request'])
            if request.get('platform') == 'discord':
                from hyemi_contracts import DISCORD_BOTS, DISCORD_GUILD
                value.update(platform='discord',logical_author='hyemi' if e['kind']=='report' else request['to'],
                             transport_sender='hyemi',sender_bot_id=DISCORD_BOTS[request['to']],
                             channel_id=request['channelid'],guild_id=DISCORD_GUILD)
            if e['kind']=='report':
                value.update(contents=request['contents'],raw_report_available=True)
            return dict(value,event_id=eid,work_id=e['work_id'],execution_state=e['execution_state'],
                        availability='available' if e['result'] is not None or e['kind']=='report' else 'pending',delivery=delivery)

    def queued_events(self):
        with self._db() as db:
            rows=db.execute('''SELECT e.* FROM v2_events e WHERE e.kind!='report'
              AND e.execution_state NOT IN ('finished','failed','cancelled') AND NOT EXISTS (
                SELECT 1 FROM v2_events p WHERE p.session_id=e.session_id AND p.id<e.id
                AND p.execution_state NOT IN ('finished','failed','cancelled')) ORDER BY e.id''').fetchall()
            return [self._internal(r) for r in rows]

    def append_update(self,eid,kind,data,upstream_seq=None):
        text(kind,'kind',100); encoded=canonical_json(data)
        if upstream_seq is not None: integer(upstream_seq,'upstream_seq',1)
        with self._db() as db:
            e=self._event(db,eid)
            if upstream_seq is not None:
                old=db.execute('SELECT * FROM v2_updates WHERE event_id=? AND upstream_seq=?',(eid,upstream_seq)).fetchone()
                if old:
                    if old['kind'] != kind or old['data'] != encoded: raise StoreConflict('upstream sequence content conflict')
                    return old['seq']
                if upstream_seq <= e['last_upstream_seq']: raise StoreConflict('upstream sequence moved backwards')
            seq=db.execute('INSERT INTO v2_updates(event_id,kind,data,upstream_seq,created_at) VALUES(?,?,?,?,?)',(eid,kind,encoded,upstream_seq,time.time())).lastrowid
            if upstream_seq is not None: db.execute('UPDATE v2_events SET last_upstream_seq=? WHERE event_id=?',(upstream_seq,eid))
            return seq

    def set_execution(self,eid,state,**fields):
        if not isinstance(state,str) or state not in STATES: raise ContractError('invalid execution state')
        if set(fields)-FIELDS: raise ContractError('unsupported execution metadata')
        if 'last_upstream_seq' in fields: integer(fields['last_upstream_seq'],'last_upstream_seq')
        if 'attempt_count' in fields: integer(fields['attempt_count'],'attempt_count')
        with self._db() as db:
            e=self._event(db,eid)
            current=e['execution_state']
            if current in TERMINAL and state != current: raise StoreConflict('terminal execution is immutable')
            if state == 'queued' and current != 'queued': raise StoreConflict('attempted execution cannot be requeued')
            if state == 'starting' and current not in {'queued','starting'}: raise StoreConflict('attempted execution requires reconciliation')
            if e['kind']=='report' and state != 'finished': raise StoreConflict('reports have no execution')
            if state=='finished' and e['kind']!='report' and e['result'] is None:
                raise StoreConflict('finished execution requires a result')
            if state in {'starting','running'}:
                earlier=db.execute("SELECT 1 FROM v2_events WHERE session_id=? AND id<? AND execution_state NOT IN ('finished','failed','cancelled')",(e['session_id'],e['id'])).fetchone()
                if earlier: raise StoreConflict('session has an unfinished predecessor')
            cursor=fields.pop('last_upstream_seq',e['last_upstream_seq'])
            if cursor < e['last_upstream_seq']: raise StoreConflict('cursor cannot decrease')
            metadata=json.loads(e['metadata']); metadata.update(fields)
            db.execute('UPDATE v2_events SET execution_state=?,metadata=?,last_upstream_seq=? WHERE event_id=?',(state,canonical_json(metadata),cursor,eid))

    def finish(self,eid,result,telegram_phase=None):
        """Persist immutable result and optional final delivery in one transaction."""
        with self._db() as db:
            e=self._event(db,eid)
            if e['kind']=='report': raise StoreConflict('reports retain raw data without model results')
            validate_result(result,e['kind']); encoded=canonical_json(result)
            target=result['outcome'] if result['outcome'] in {'failed','cancelled'} else 'finished'
            if e['execution_state'] in TERMINAL and e['execution_state'] != target:
                raise StoreConflict('terminal execution is immutable')
            if e['result'] is not None and e['result'] != encoded: raise StoreConflict('finished result is immutable')
            if e['kind']=='review' and e['result'] is None:
                classification=self._review_classification(result)
                if classification is not None:
                    known,active=self._finding_registry(db,e)
                    ids,resolved,remaining,new=classification
                    if resolved | remaining != active or new & known:
                        raise StoreConflict('review finding continuity conflict')
            db.execute('UPDATE v2_events SET result=?,execution_state=? WHERE event_id=?',(encoded,target,eid))
            if telegram_phase is not None:
                text(telegram_phase,'telegram_phase',80)
                from hyemi_delivery import split_text
                request=json.loads(e['request']); sender=request['to']
                if request.get('platform') == 'discord':
                    from hyemi_formatting import render_discord
                    for index,chunk in enumerate(render_discord(result['telegram_report'])):
                        self._enqueue(db,eid,telegram_phase+':'+str(index),chunk['text'],sender,chunk)
                else:
                    for index,chunk in enumerate(split_text(result['telegram_report'])):
                        self._enqueue(db,eid,telegram_phase+':'+str(index),chunk,sender)

    def result_for_internal(self,eid):
        with self._db() as db:
            e=self._event(db,eid)
            return json.loads(e['result']) if e['result'] is not None else None

    @staticmethod
    def _review_classification(result):
        """Only complete validated classifications advance authoritative state."""
        detail=result['hyemi_result']
        keys={'findings','resolved','remaining','new'}
        # Legacy summary-only records and executor failures do not erase findings.
        if not keys.intersection(detail): return None
        metadata=detail.get('review_metadata')
        execution_error=detail.get('error') or (
            metadata.get('error') if isinstance(metadata,dict) else None)
        if result['outcome'] in {'failed','cancelled'} or (
                result['outcome']=='unverified' and execution_error):
            return None
        if not keys.issubset(detail): raise StoreConflict('incomplete review classifications')
        groups=[]
        for key in ('resolved','remaining','new'):
            values=detail[key]
            if not isinstance(values,list) or any(not isinstance(v,str) or not v.strip() for v in values):
                raise StoreConflict('invalid review finding IDs')
            if len(values)!=len(set(values)): raise StoreConflict('duplicate review finding IDs')
            groups.append(set(values))
        resolved,remaining,new=groups
        findings=detail['findings']; ids=set()
        if not isinstance(findings,list): raise StoreConflict('invalid review findings')
        for finding in findings:
            if not isinstance(finding,dict) or any(not isinstance(finding.get(k),str) or not finding[k].strip()
                    for k in ('id','location','issue','reason','improvement','resolution_criteria')):
                raise StoreConflict('invalid review finding detail')
            if finding['id'] in ids: raise StoreConflict('duplicate review finding IDs')
            ids.add(finding['id'])
        if resolved & remaining or resolved & new or remaining & new or ids != remaining | new:
            raise StoreConflict('inconsistent review classifications')
        if result['outcome']=='passed_in_scope' and ids: raise StoreConflict('passed review has active findings')
        if result['outcome']=='changes_required' and not ids: raise StoreConflict('changes require findings')
        return ids,resolved,remaining,new

    def _finding_registry(self,db,before):
        """Full durable ledger, not the bounded prompt window; old DBs need no backfill."""
        known,active=set(),set()
        rows=db.execute("SELECT result FROM v2_events WHERE owner=? AND work_id=? AND kind='review' AND id<? AND result IS NOT NULL ORDER BY id",
                        (before['owner'],before['work_id'],before['id']))
        for row in rows:
            try:
                result=json.loads(row['result']); validate_result(result,'review')
                classification=self._review_classification(result)
            except (ValueError,KeyError,TypeError):
                continue  # Historical malformed failures are not authoritative.
            if classification is None: continue
            ids,resolved,remaining,new=classification
            if resolved | remaining != active or new & known:
                # Reserve observed IDs even in an inconsistent legacy record,
                # but never let it erase the last valid active classification.
                known.update(ids | resolved | remaining | new)
                continue
            known.update(ids | resolved | remaining | new); active=ids
        return known,active

    def review_history(self,owner,work_id,before_event_id,max_entries=12):
        integer(max_entries,'max_entries',1,100)
        with self._db() as db:
            self._work(db,owner,work_id)
            before=self._event(db,before_event_id,owner)
            if before['work_id']!=work_id or before['kind']!='review': raise NotFound('not_found')
            rows=db.execute("SELECT * FROM v2_events WHERE owner=? AND work_id=? AND kind='review' AND id<? ORDER BY id DESC LIMIT ?",
                            (owner,work_id,before['id'],max_entries)).fetchall()
            history=[]
            for row in reversed(rows):
                entry=self._internal(row)
                entry['version']=entry['request']['version']
                entry['revision']=entry['request']['revision']
                entry['hyemi_result']=entry['result']['hyemi_result'] if entry['result'] else None
                history.append(entry)
            if history:
                known,active=self._finding_registry(db,before)
                history[-1]['known_finding_ids']=sorted(known)
                history[-1]['active_finding_ids']=sorted(active)
            return history

    def _enqueue(self,db,eid,phase,content,sender,rendering=None):
        text(phase,'phase',100); text(content,'text',nonempty=False)
        if not content: raise ContractError('empty outbox text')
        e=self._event(db,eid)
        expected='hyemi' if e['kind']=='report' else json.loads(e['request'])['to']
        if sender!=expected: raise ContractError('sender does not match event role')
        old=db.execute('SELECT * FROM v2_outbox WHERE event_id=? AND phase=?',(eid,phase)).fetchone()
        if old:
            if (old['text'],old['sender']) != (content,sender): raise StoreConflict('outbox content conflict')
            return old['id']
        return db.execute('INSERT INTO v2_outbox(event_id,phase,text,sender,created_at,rendering) VALUES(?,?,?,?,?,?)',
                          (eid,phase,content,sender,time.time(),canonical_json(rendering) if rendering is not None else None)).lastrowid

    def enqueue_telegram(self,eid,phase,text,sender,rendering=None):
        with self._db() as db: return self._enqueue(db,eid,phase,text,sender,rendering)

    def outbox_pending(self):
        """Credential readiness must be checked before claiming these rows."""
        with self._db() as db:
            rows=db.execute("SELECT o.*,e.owner,e.work_id,e.request FROM v2_outbox o JOIN v2_events e ON e.event_id=o.event_id WHERE o.state IN ('pending','blocked_configuration') ORDER BY o.id")
            result=[]
            for row in rows:
                item=dict(row); request=json.loads(item.pop('request'))
                item['channelid']=request['channelid']; item['channel']=request['channelid']
                result.append(item)
            return result

    @staticmethod
    def _phase_order(phase):
        family,sep,index=phase.rpartition(':')
        return (family,int(index)) if sep and index.isdigit() else (phase,None)

    def claim_outbox(self,id):
        integer(id,'outbox id',1)
        with self._db() as db:
            row=db.execute('SELECT * FROM v2_outbox WHERE id=?',(id,)).fetchone()
            if row is None: raise NotFound('not_found')
            family,index=self._phase_order(row['phase'])
            if index is not None:
                for prior in db.execute('SELECT phase,state FROM v2_outbox WHERE event_id=?',(row['event_id'],)):
                    pf,pi=self._phase_order(prior['phase'])
                    if pf==family and pi is not None and pi<index and prior['state']!='delivered': return False
            return bool(db.execute("UPDATE v2_outbox SET state='sending',error=NULL WHERE id=? AND state IN ('pending','blocked_configuration')",(id,)).rowcount)

    def reconcile_outbox(self):
        """Only call after acquiring exclusive worker ownership at startup."""
        with self._db() as db:
            return db.execute("UPDATE v2_outbox SET state='unknown',error='worker_interrupted' WHERE state='sending'").rowcount

    def mark_blocked(self,id,error='sender_not_configured'):
        return self.finish_outbox(id,'blocked_configuration',error=error)

    def finish_outbox(self,id,state,message_id=None,error=None,destination=None):
        integer(id,'outbox id',1)
        if state=='sent': state='delivered'
        if state not in {'delivered','failed','unknown','blocked_configuration'}: raise ContractError('invalid outbox completion state')
        if message_id is not None and (isinstance(message_id,bool) or not isinstance(message_id,(str,int))): raise ContractError('invalid message_id')
        if error is not None: text(error,'error',nonempty=False)
        mid=str(message_id) if message_id is not None else None
        with self._db() as db:
            row=db.execute('SELECT * FROM v2_outbox WHERE id=?',(id,)).fetchone()
            if row is None: raise NotFound('not_found')
            proof=None
            if state=='delivered':
                from hyemi_delivery import verified_destination
                event=self._event(db,row['event_id']); request=json.loads(event['request'])
                expected='hyemi' if event['kind']=='report' else request['to']
                if isinstance(destination,dict) and set(destination)=={'chat_id','message_thread_id','general'}:
                    chat=destination['chat_id']
                    try:
                        if isinstance(chat,str) and str(int(chat))==chat and type(destination['general']) is bool:
                            proof=verified_destination(request['channelid'],int(chat),destination['message_thread_id'])
                    except (ValueError,TypeError): pass
                    if proof!=destination: proof=None
                if request.get('platform') == 'discord':
                    from hyemi_contracts import snowflake
                    from hyemi_delivery import verified_discord_destination
                    # Validate the original value: coercion is not Discord proof.
                    try:
                        snowflake(message_id)
                    except ContractError:
                        proof=None
                    else:
                        proof=verified_discord_destination(request, destination, expected)
                if proof is None or row['sender']!=expected or mid is None or not mid.isascii() or not mid.isdigit() or int(mid)<=0:
                    state='unknown'; error=request.get('platform','telegram')+'_destination_unconfirmed'; proof=None
            encoded=canonical_json(proof) if proof is not None else None
            if (row['state'],row['message_id'],row['error'],row['destination'])==(state,mid,error,encoded): return
            allowed={'sending'}
            if state=='blocked_configuration': allowed|={'pending','blocked_configuration'}
            if row['state'] not in allowed: raise StoreConflict('delivery requires a claim; terminal delivery is immutable')
            db.execute('UPDATE v2_outbox SET state=?,message_id=?,error=?,destination=? WHERE id=?',(state,mid,error,encoded,id))

    def retry_failed_outbox(self,id):
        """Trusted admin only: retry a definite failure, never an unknown send."""
        integer(id,'outbox id',1)
        with self._db() as db:
            if not db.execute('SELECT 1 FROM v2_outbox WHERE id=?',(id,)).fetchone(): raise NotFound('not_found')
            return bool(db.execute("UPDATE v2_outbox SET state='pending',error=NULL WHERE id=? AND state='failed'",(id,)).rowcount)

    def record_query(self,owner,wid,operation,cursor=None):
        if operation not in {'status','work_status','updates','report','result','event'}: raise ContractError('invalid query operation')
        if cursor is not None: integer(cursor,'cursor')
        with self._db() as db:
            self._work(db,owner,wid); now=time.time()
            id=db.execute('INSERT INTO v2_queries(owner,work_id,operation,cursor,created_at) VALUES(?,?,?,?,?)',(owner,wid,operation,cursor,now)).lastrowid
            return dict(id=id,work_id=wid,operation=operation,cursor=cursor,at=now,meaning='authenticated_lookup')
