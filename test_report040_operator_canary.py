"""Offline helper-main regressions against the actual WorkflowStore schema.

Only the config read, helper root/argv and private HTTP opener are replaced.
The opener uses real admission, rendering, delivery transitions and readback;
no live config, state, credentials, Telegram or external network is accessed.
"""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyemi_store import WorkflowStore
from planning import run_report040_operator_canary as canary


class Report040OperatorCanaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='report040-operator-', dir=None)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'planning').mkdir()
        self.output = self.root / 'planning/receipt.json'
        self.store = WorkflowStore(self.root / 'ledger.sqlite')
        self.owner = canary.OPERATOR_OWNER
        self.config = {'api_key': 'offline-fixture-only', 'state_path': self.store.path,
                       'allowed': {'hyeji': {'home': '-1000000002'}}}
        # Existing client owners must never be selected or mutated.
        self.seed = {'version': 2, 'kind': 'report', 'event_id': str(uuid.uuid4()),
                     'work_id': str(uuid.uuid4()), 'to': 'hyeji',
                     'channelid': '-1000000002', 'contents': 'offline seed'}
        self.store.admit('a' * 64, self.seed)
        self.other = dict(self.seed, event_id=str(uuid.uuid4()), work_id=str(uuid.uuid4()), contents='second client raw')
        self.store.admit('b' * 64, self.other)
        self.before_clients = self.client_snapshot()
        self.calls = []
        self.mode = 'success'
        self.mutate_row = None
        self.stdout = io.StringIO()
        original_read = Path.read_text

        def config_read(path, *args, **kwargs):
            if str(path) == '/root/.config/hyemi-ingress/config.json':
                return json.dumps(self.config)
            if not path.resolve().is_relative_to(self.root):
                raise AssertionError('unexpected read outside scratch fixture')
            return original_read(path, *args, **kwargs)

        for patcher in (patch.object(canary, 'ROOT', self.root),
                        patch('sys.argv', ['run_report040_operator_canary.py', '--execute', '--output', 'planning/receipt.json']),
                        patch.object(Path, 'read_text', config_read),
                        patch.object(canary, 'build_opener', return_value=SimpleNamespace(open=self.open))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def open(self, req, timeout):
        self.assertEqual(timeout, 20)
        self.assertTrue(req.full_url.startswith(canary.BASE + '/v2/'))
        self.assertEqual(req.get_header('X-hyemi-owner'), self.owner)
        method = req.get_method()
        path = req.full_url[len(canary.BASE):]
        self.calls.append((method, path))
        if method == 'POST':
            self.assertEqual(path, '/v2/requests')
            self.assertEqual(sum(m == 'POST' for m, _ in self.calls), 1)
            body = json.loads(req.data)
            # IDs and an initial receipt must exist BEFORE the ambiguous operation.
            self.reservation_bytes = self.output.read_bytes()
            before = json.loads(self.reservation_bytes)
            self.assertEqual(before['event_id'], body['event_id'])
            self.assertEqual(before['work_id'], body['work_id'])
            self.assertFalse(before['submitted'])
            accepted = self.store.admit(self.owner, body)
            self.body = body
            if self.mode == 'ambiguous':
                raise TimeoutError('acceptance response lost after real scratch admission')
            for row in self.store.outbox_pending():
                if row['event_id'] == body['event_id']:
                    self.assertTrue(self.store.claim_outbox(row['id']))
                    state = self.mode if self.mode in ('failed', 'unknown', 'blocked_configuration') else 'delivered'
                    self.store.finish_outbox(row['id'], state, message_id=41,
                        destination={'chat_id': '-999' if self.mode == 'wrong_destination' else '-1000000002',
                                     'message_thread_id': None, 'general': True})
            if self.mode == 'run_id':
                self.store.set_execution(body['event_id'], 'finished', run_id='offline-agent-run')
            if self.mode == 'null_run_id':
                self.store.set_execution(body['event_id'], 'finished', run_id=None)
            code, value = 202, accepted
        else:
            self.assertEqual(method, 'GET')
            eid = self.body['event_id']
            if path == '/v2/events/' + eid + '/result':
                code, value = 200, self.store.result(self.owner, eid)
                if self.mode == 'unfinished_result':
                    value['execution_state'] = 'running'
            else:
                self.assertEqual(path, '/v2/events/' + eid + '/report')
                code, value = 200, self.store.report(self.owner, eid)
                if self.mutate_row:
                    with self.store._db() as db:
                        self.mutate_row(db, eid)
        encoded = json.dumps(value).encode()
        return SimpleNamespace(code=code, read=lambda limit: encoded[:limit])

    def invoke(self):
        with contextlib.redirect_stdout(self.stdout):
            status = canary.main()
        return status, json.loads(self.output.read_text())

    def test_success_real_schema_absent_run_id_no_agent(self):
        status, receipt = self.invoke()
        self.assertEqual(status, 0, receipt)
        self.assertTrue(receipt['ok'])
        self.assertTrue(receipt['no_agent_run'])
        self.assertTrue(receipt['raw_report_exact'])
        self.assertEqual(self.calls, [('POST', '/v2/requests'),
            ('GET', '/v2/events/' + receipt['event_id'] + '/result'),
            ('GET', '/v2/events/' + receipt['event_id'] + '/report')])
        event = self.store.event(self.owner, receipt['event_id'])
        self.assertEqual(event['execution_state'], 'finished')
        self.assertNotIn('run_id', event)
        self.assertEqual(self.store.queued_events(), [])
        self.assertEqual(self.store.report(self.owner, receipt['event_id']), self.body)
        self.assertEqual(receipt['work_id'], self.body['work_id'])
        self.assertEqual(receipt['named_link_targets'], ['https://core.telegram.org/bots/api'])
        self.assertEqual(receipt['preview_requests'], [{'is_disabled': False, 'url': 'https://core.telegram.org/bots/api'}])
        self.assertTrue(all(d['state'] == 'delivered' and d['confirmed_destination'] for d in receipt['delivery']))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertFalse(receipt['authenticated_client_receipt'])
        self.assertFalse(receipt['telegram_ui_verified'])

    def test_explicit_null_run_id_is_no_agent(self):
        self.mode = 'null_run_id'
        status, receipt = self.invoke()
        self.assertEqual(status, 0, receipt)
        self.assertTrue(receipt['no_agent_run'])

    def test_present_run_id_rejected_despite_finished_public_result(self):
        self.mode = 'run_id'
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertFalse(receipt['ok'])
        self.assertFalse(receipt['no_agent_run'])
        self.assertNotIn('error_class', receipt)
        self.assertEqual(self.store.event(self.owner, receipt['event_id'])['run_id'], 'offline-agent-run')

    def test_unfinished_public_result_rejected(self):
        self.mode = 'unfinished_result'
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertFalse(receipt['no_agent_run'])

    def test_missing_actual_event_is_not_null_run_proof(self):
        self.mutate_row = lambda db, eid: db.execute('DELETE FROM v2_events WHERE event_id=?', (eid,))
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertFalse(receipt['no_agent_run'])

    def test_actual_event_identity_kind_and_state_must_match(self):
        for column, value in [('owner', 'b' * 64), ('work_id', str(uuid.uuid4())),
                              ('kind', 'task'), ('execution_state', 'running')]:
            with self.subTest(column=column):
                self.output.unlink(missing_ok=True)
                self.calls.clear()
                self.mutate_row = lambda db, eid: db.execute(
                    'UPDATE v2_events SET ' + column + '=? WHERE event_id=?', (value, eid))
                # Remove only previous operator attempts, preserving both client seeds.
                with self.store._db() as db:
                    db.execute('DELETE FROM v2_events WHERE event_id NOT IN (?,?)',
                               (self.seed['event_id'], self.other['event_id']))
                status, receipt = self.invoke()
                self.assertEqual(status, 1)
                self.assertFalse(receipt['no_agent_run'])

    def test_invalid_metadata_fails_closed(self):
        self.mutate_row = lambda db, eid: db.execute('UPDATE v2_events SET metadata=? WHERE event_id=?', ('not-json', eid))
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertFalse(receipt['ok'])
        self.assertEqual(receipt['error_class'], 'JSONDecodeError')

    def test_ambiguous_post_persists_ids_never_retries_or_replays(self):
        self.mode = 'ambiguous'
        status, receipt = self.invoke()
        self.assertEqual(status, 1)
        self.assertFalse(receipt['ok'])
        self.assertFalse(receipt['submitted'])  # Acceptance unverified, NOT no side effect.
        self.assertEqual(receipt['error_class'], 'TimeoutError')
        self.assertEqual(receipt['event_id'], self.body['event_id'])
        self.assertEqual(receipt['work_id'], self.body['work_id'])
        self.assertEqual(self.store.report(self.owner, receipt['event_id']), self.body)
        self.assertEqual(self.calls, [('POST', '/v2/requests')])
        self.assertFalse(json.loads(self.stdout.getvalue())['retry'])
        before = self.output.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'existing receipt must not be overwritten'):
            self.invoke()
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(self.calls, [('POST', '/v2/requests')])

    @contextlib.contextmanager
    def receipt_fault(self, stage, active):
        """Persistent failures on real helper I/O, not fabricated store results."""
        original_fdopen = canary.os.fdopen
        original_dump = json.dump
        original_dumps = json.dumps
        original_fsync = canary.os.fsync
        original_replace = canary.os.replace
        original_mkstemp = canary.tempfile.mkstemp
        self.fault_hits = 0
        self.temp_modes = []

        def fail():
            self.fault_hits += 1
            raise OSError('SECRET filesystem failure must not escape to console')

        def is_receipt(value):
            return isinstance(value, dict) and 'operator_namespace' in value

        def dump(value, stream, *args, **kwargs):
            # Also targets the old destructive json.dump implementation for RED.
            if stage in ('serialize', 'write') and active() and is_receipt(value):
                fail()
            return original_dump(value, stream, *args, **kwargs)

        def dumps(value, *args, **kwargs):
            if stage == 'serialize' and active() and is_receipt(value):
                fail()
            return original_dumps(value, *args, **kwargs)

        fixture = self
        class Stream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def fileno(self): return self.stream.fileno()
            def write(self, value):
                if stage == 'write' and active():
                    self.stream.write(value[:12])  # Genuine partial sibling write.
                    fail()
                return self.stream.write(value)
            def flush(self):
                if stage == 'flush' and active(): fail()
                return self.stream.flush()

        def fdopen(fd, *args, **kwargs):
            import os
            fixture.temp_modes.append(os.fstat(fd).st_mode & 0o777)
            return Stream(original_fdopen(fd, *args, **kwargs))

        def fsync(fd):
            import os, stat
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
            if active() and ((stage == 'fsync' and not is_dir) or
                             (stage == 'directory_fsync' and is_dir)):
                fail()
            return original_fsync(fd)

        def replace(src, dst):
            self.assertEqual(Path(src).parent, self.output.parent)
            self.assertEqual(Path(dst), self.output)
            self.assertEqual(Path(src).stat().st_mode & 0o777, 0o600)
            if stage == 'replace' and active(): fail()
            return original_replace(src, dst)

        def mkstemp(*args, **kwargs):
            if stage == 'create' and active(): fail()
            return original_mkstemp(*args, **kwargs)

        with patch.object(canary.os, 'fdopen', fdopen), \
             patch.object(json, 'dump', dump), patch.object(json, 'dumps', dumps), \
             patch.object(canary.os, 'fsync', fsync), \
             patch.object(canary.tempfile, 'mkstemp', mkstemp), \
             patch.object(canary.os, 'replace', replace):
            yield

    def assert_failed_update_preserves_ids(self, stage, mode='ambiguous', final=False):
        self.mode = mode
        active = (lambda: any(path.endswith('/report') for _, path in self.calls)) if final else (lambda: hasattr(self, 'body'))
        error = None
        with self.receipt_fault(stage, active), contextlib.redirect_stdout(self.stdout):
            try:
                status = canary.main()
            except Exception as exc:
                error = type(exc).__name__
        # Check real commit and POST count even in the RED destructive implementation.
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
        self.assertEqual(self.store.report(self.owner, self.body['event_id']), self.body)
        self.assertEqual(self.store.event(self.owner, self.body['event_id'])['work_id'], self.body['work_id'])
        self.assertTrue(self.output.read_bytes(), 'committed POST lost original durable IDs: zero-byte receipt')
        receipt = json.loads(self.output.read_text())
        self.assertEqual(receipt['event_id'], self.body['event_id'])
        self.assertEqual(receipt['work_id'], self.body['work_id'])
        if stage != 'directory_fsync' and not final:
            self.assertEqual(self.output.read_bytes(), self.reservation_bytes)
        self.assertIsNone(error, 'persistent error-save failure must be handled')
        self.assertEqual(status, 1)
        console = json.loads(self.stdout.getvalue())
        self.assertFalse(console['ok'])
        self.assertFalse(console['retry'])
        self.assertEqual(console['event_id'], self.body['event_id'])
        self.assertEqual(console['work_id'], self.body['work_id'])
        self.assertEqual(console['error_class'], 'TimeoutError' if mode == 'ambiguous' else 'OSError')
        self.assertEqual(console['evidence_error_class'], 'OSError')
        self.assertFalse(console['evidence_updated'])
        self.assertNotIn('SECRET', self.stdout.getvalue())
        self.assertEqual(self.fault_hits, 1, 'no persistence retry after a failed update')
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(mode == 0o600 for mode in self.temp_modes))
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])
        self.assertEqual(self.client_snapshot(), self.before_clients)
        before = self.output.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'existing receipt must not be overwritten'):
            canary.main()
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)
        if final:
            self.assertTrue(receipt['submitted'])
            self.assertFalse(receipt['ok'])

    def test_qop1_ambiguous_persistent_write(self):
        self.assert_failed_update_preserves_ids('write')

    def test_qop1_acknowledged_persistent_write(self):
        self.assert_failed_update_preserves_ids('write', mode='success')

    def test_qop1_ambiguous_serialize(self):
        self.assert_failed_update_preserves_ids('serialize')

    def test_qop1_ambiguous_flush(self):
        self.assert_failed_update_preserves_ids('flush')

    def test_qop1_ambiguous_file_fsync(self):
        self.assert_failed_update_preserves_ids('fsync')

    def test_qop1_ambiguous_pre_replace(self):
        self.assert_failed_update_preserves_ids('replace')

    def test_qop1_acknowledged_serialize(self):
        self.assert_failed_update_preserves_ids('serialize', mode='success')

    def test_qop1_acknowledged_flush(self):
        self.assert_failed_update_preserves_ids('flush', mode='success')

    def test_qop1_acknowledged_file_fsync(self):
        self.assert_failed_update_preserves_ids('fsync', mode='success')

    def test_qop1_acknowledged_pre_replace(self):
        self.assert_failed_update_preserves_ids('replace', mode='success')

    def test_qop1_final_evidence_write_failure_not_success(self):
        self.assert_failed_update_preserves_ids('write', mode='success', final=True)

    def test_qop1_directory_fsync_failure_is_not_durable_success(self):
        self.assert_failed_update_preserves_ids('directory_fsync', mode='success')

    def test_qop1_sibling_creation_failure(self):
        self.assert_failed_update_preserves_ids('create')

    def test_qop1_initial_exclusive_claim_race_zero_post(self):
        original_open = canary.os.open
        claimed = b'concurrent original receipt'
        def race(path, flags, *args, **kwargs):
            if Path(path) == self.output and flags & canary.os.O_EXCL:
                self.output.write_bytes(claimed)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(canary.os, 'open', race):
            with self.assertRaises(FileExistsError): canary.main()
        self.assertEqual(self.output.read_bytes(), claimed)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client_snapshot(), self.before_clients)

    def test_qop1_success_fsync_replace_order_and_modes(self):
        import os, stat
        original_fsync, original_replace = os.fsync, os.replace
        sequence = []
        def fsync(fd):
            sequence.append('directory_fsync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file_fsync')
            return original_fsync(fd)
        def replace(src, dst):
            sequence.append('replace')
            self.assertEqual(Path(src).parent, self.output.parent)
            self.assertEqual(Path(src).stat().st_mode & 0o777, 0o600)
            old, new = json.loads(self.output.read_text()), json.loads(Path(src).read_text())
            self.assertEqual((old['event_id'], old['work_id']), (new['event_id'], new['work_id']))
            return original_replace(src, dst)
        def transport(req, timeout):
            if req.get_method() == 'POST': sequence.append('POST')
            return self.open(req, timeout)
        with patch.object(canary.os, 'fsync', fsync), patch.object(canary.os, 'replace', replace), \
             patch.object(canary, 'build_opener', return_value=SimpleNamespace(open=transport)):
            status, receipt = self.invoke()
        self.assertEqual(status, 0, receipt)
        self.assertEqual(sequence, ['file_fsync', 'directory_fsync', 'POST',
            'file_fsync', 'replace', 'directory_fsync', 'file_fsync', 'replace', 'directory_fsync'])
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_qop1_initial_reservation_failure_zero_post(self):
        for stage in ('serialize', 'write', 'flush', 'fsync', 'directory_fsync'):
            with self.subTest(stage=stage):
                self.output.unlink(missing_ok=True)
                with self.receipt_fault(stage, lambda: True):
                    with self.assertRaises(OSError):
                        canary.main()
                self.assertEqual(self.calls, [])
                self.assertEqual(self.client_snapshot(), self.before_clients)
                self.assertTrue(all(mode == 0o600 for mode in self.temp_modes))
                self.assertEqual([p for p in self.output.parent.iterdir() if p != self.output], [])

    def test_existing_output_refused_before_config_or_post(self):
        self.output.write_text('original evidence')
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read config')):
            with self.assertRaisesRegex(RuntimeError, 'new planning evidence path required'):
                canary.main()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.output.read_text(), 'original evidence')

    def test_execute_flag_required(self):
        with patch('sys.argv', ['run_report040_operator_canary.py']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                canary.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.output.exists())

    def test_fixed_general_destination_required(self):
        self.config['allowed']['hyeji']['home'] = '-999:2'
        with self.assertRaisesRegex(RuntimeError, 'General destination mismatch'):
            canary.main()
        self.assertEqual(self.calls, [])

    def client_snapshot(self):
        with self.store._db() as db:
            rows = {table: [dict(r) for r in db.execute(query)] for table, query in {
                'events': "SELECT * FROM v2_events WHERE owner IN ('" + 'a'*64 + "','" + 'b'*64 + "') ORDER BY id",
                'works': "SELECT * FROM v2_works WHERE owner IN ('" + 'a'*64 + "','" + 'b'*64 + "') ORDER BY owner,work_id",
                'outbox': "SELECT * FROM v2_outbox WHERE event_id IN (SELECT event_id FROM v2_events WHERE owner != '" + self.owner + "') ORDER BY id",
                'updates': "SELECT * FROM v2_updates WHERE event_id IN (SELECT event_id FROM v2_events WHERE owner != '" + self.owner + "') ORDER BY seq",
            }.items()}
        return json.dumps(rows, sort_keys=True).encode()

    def test_multi_owner_success_without_impersonation_or_mutation(self):
        status, receipt = self.invoke()
        self.assertEqual(status, 0, receipt)
        self.assertEqual(canary.OPERATOR_OWNER, hashlib.sha256(
            b'hermes-bridge-mcp:LOCAL-OPERATOR:report040-canary:v1').hexdigest())
        self.assertNotIn(self.owner, ('a'*64, 'b'*64))
        self.assertEqual(self.client_snapshot(), self.before_clients)
        self.assertEqual(hashlib.sha256(self.client_snapshot()).hexdigest(),
                         hashlib.sha256(self.before_clients).hexdigest())
        self.assertEqual(self.store.report('a'*64, self.seed['event_id']), self.seed)
        self.assertEqual(self.store.report('b'*64, self.other['event_id']), self.other)
        with self.store._db() as db:
            self.assertEqual(db.execute('SELECT DISTINCT owner FROM v2_events').fetchall().__len__(), 3)
        self.assertEqual(receipt['operator_owner'], self.owner)
        self.assertFalse(receipt['hyemi_user_auth_proof'])
        self.assertIn('LIVE PRIVATE OPERATOR', receipt['proof'])

    def test_failed_delivery_no_resend(self):
        self.mode = 'failed'
        self.assert_terminal_delivery()

    def test_unknown_delivery_no_resend(self):
        self.mode = 'unknown'
        self.assert_terminal_delivery()

    def test_blocked_delivery_no_resend(self):
        self.mode = 'blocked_configuration'
        self.assert_terminal_delivery()

    def assert_terminal_delivery(self):
        status, receipt = self.invoke()
        self.assertEqual(status, 1, receipt)
        self.assertFalse(receipt['ok'])
        self.assertFalse(receipt['retry'])
        self.assertEqual([m for m, _ in self.calls], ['POST', 'GET'])
        self.assertEqual(self.client_snapshot(), self.before_clients)
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertEqual([m for m, _ in self.calls], ['POST', 'GET'])

    def test_wrong_destination_real_store_downgrades_to_unknown(self):
        self.mode = 'wrong_destination'
        self.assert_terminal_delivery()
        with self.store._db() as db:
            row = db.execute('SELECT state FROM v2_outbox WHERE event_id=?',
                             (self.body['event_id'],)).fetchone()
        self.assertEqual(row['state'], 'unknown')

    def test_corrupt_stored_destination_rejected(self):
        def mutate(db, eid):
            db.execute('UPDATE v2_outbox SET destination=? WHERE event_id=?',
                       (json.dumps({'chat_id': '-999', 'message_thread_id': None, 'general': True}), eid))
        self.mutate_row = mutate
        status, receipt = self.invoke()
        self.assertEqual(status, 1, receipt)
        self.assertFalse(receipt['confirmed_general_destination'])
        self.assertFalse(receipt['ok'])

    def test_config_identity_claims_ignored(self):
        self.config.update(owner='a'*64, issuer='external-issuer', sub='customer-sub')
        status, receipt = self.invoke()
        self.assertEqual(status, 0, receipt)
        self.assertEqual(receipt['operator_owner'], canary.OPERATOR_OWNER)
        self.assertNotIn(self.config['api_key'], self.stdout.getvalue() + self.output.read_text())
        self.assertEqual(self.client_snapshot(), self.before_clients)

    def test_stored_request_mismatch_rejected(self):
        def mutate(db, eid):
            altered = dict(self.body, contents='not the exact sample')
            db.execute('UPDATE v2_events SET request=? WHERE event_id=?', (json.dumps(altered), eid))
        self.mutate_row = mutate
        status, receipt = self.invoke()
        self.assertEqual(status, 1, receipt)
        self.assertFalse(receipt['no_agent_run'])

    def test_owner_override_not_supported(self):
        with patch('sys.argv', ['canary', '--execute', '--owner', 'a'*64]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                canary.main()
        self.assertFalse(self.calls)

    def test_general_thread_one_accepted(self):
        self.config['allowed']['hyeji']['home'] = '-1000000002:1'
        status, receipt = self.invoke()
        self.assertEqual(status, 0, receipt)


if __name__ == '__main__':
    unittest.main()
