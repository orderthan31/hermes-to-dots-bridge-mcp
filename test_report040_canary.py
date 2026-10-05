"""Offline helper-main regressions against the actual WorkflowStore schema.

Only the config read, helper root/argv and private HTTP opener are replaced.
The opener uses real admission, rendering, delivery transitions and readback;
no live config, state, credentials, Telegram or external network is accessed.
"""
import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyemi_store import WorkflowStore
from planning import run_report040_canary as canary


class Report040CanaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='report040-q1-', dir=None)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'planning').mkdir()
        self.output = self.root / 'planning/receipt.json'
        self.store = WorkflowStore(self.root / 'ledger.sqlite')
        self.owner = 'a' * 64
        self.config = {'api_key': 'offline-fixture-only', 'state_path': self.store.path,
                       'allowed': {'hyeji': {'home': '-1000000002'}}}
        # A genuine existing owner is required by the helper's preflight.
        self.seed = {'version': 2, 'kind': 'report', 'event_id': str(uuid.uuid4()),
                     'work_id': str(uuid.uuid4()), 'to': 'hyeji',
                     'channelid': '-1000000002', 'contents': 'offline seed'}
        self.store.admit(self.owner, self.seed)
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
                        patch('sys.argv', ['run_report040_canary.py', '--execute', '--output', 'planning/receipt.json']),
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
            before = json.loads(self.output.read_text())
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
                    self.store.finish_outbox(row['id'], 'delivered', message_id=41,
                        destination={'chat_id': '-1000000002', 'message_thread_id': None, 'general': True})
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
                # Restore the seed-only owner set for each independent invocation.
                with self.store._db() as db:
                    db.execute('DELETE FROM v2_events WHERE event_id!=?', (self.seed['event_id'],))
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

    def test_existing_output_refused_before_config_or_post(self):
        self.output.write_text('original evidence')
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read config')):
            with self.assertRaisesRegex(RuntimeError, 'new planning evidence path required'):
                canary.main()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.output.read_text(), 'original evidence')

    def test_execute_flag_required(self):
        with patch('sys.argv', ['run_report040_canary.py']), contextlib.redirect_stderr(io.StringIO()):
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

    def test_ambiguous_owner_refused_before_post(self):
        other = dict(self.seed, event_id=str(uuid.uuid4()), work_id=str(uuid.uuid4()))
        self.store.admit('b' * 64, other)
        with self.assertRaisesRegex(RuntimeError, 'unambiguous existing authenticated owner required'):
            canary.main()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
