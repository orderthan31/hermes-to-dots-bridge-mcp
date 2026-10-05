"""Offline direct-report rendering and durable delivery regression tests."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from hyemi_http import WorkflowIngress
from hyemi_store import WorkflowStore
from hyemi_workers import WorkflowWorkers
from hyemi_delivery import TelegramTransport, utf16_units
from test_workflow_workers import request


class ReportIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.store = WorkflowStore(self.path)
        self.config = {'api_key': 'fixture', 'allowed': {'hyeji': {'home': '-100:2', 'channels': ['-100:2']}}}
        self.owner = 'a' * 64
        self.ingress = WorkflowIngress(self.config, self.store)
        self.workers = WorkflowWorkers({}, self.store, SimpleNamespace(config=self.config))
        tokens = Mock(); tokens.get.return_value = '123:FAKE'
        self.workers.transport = TelegramTransport(tokens)
    def tearDown(self):
        self.workers.close(); self.tmp.cleanup()
    def submit(self, source):
        r = request('report', contents=source)
        self.ingress.submit('fixture', self.owner, json.dumps(r).encode())
        return r
    def test_real_path_raw_ledger_entities_preview_no_agent(self):
        url = 'https://example.com/a?utm_source=original&x=2#frag'
        r = self.submit('😀 **제목** [원문](' + url + ') `a<b`')
        self.assertEqual(self.store.report(self.owner, r['event_id']), r)
        answer = (200, {'ok': True, 'result': {'message_id': 1, 'chat': {'id': -100}, 'message_thread_id': 2}})
        with patch('hyemi_delivery.request_json', return_value=answer) as api, patch.object(self.workers, '_task') as task, patch.object(self.workers.reviewer, 'run') as review:
            self.workers._deliver()
            payload = json.loads(api.call_args.args[0].data)
            self.assertEqual(payload['text'], '😀 제목 원문 a<b')
            self.assertEqual(payload['entities'], [{'type': 'bold', 'offset': 3, 'length': 2}, {'type': 'text_link', 'offset': 6, 'length': 2, 'url': url}, {'type': 'code', 'offset': 9, 'length': 3}])
            self.assertEqual(payload['link_preview_options'], {'is_disabled': False, 'url': url})
            self.assertNotIn('parse_mode', payload)
            self.assertNotIn('disable_web_page_preview', payload)
            self.assertEqual(self.store.queued_events(), [])
            self.ingress.submit('fixture', self.owner, json.dumps(r).encode())
            self.workers._deliver(); self.assertEqual(api.call_count, 1)
            task.assert_not_called(); review.assert_not_called()
        receipt = self.store.result(self.owner, r['event_id'])['delivery'][0]
        self.assertTrue(receipt['confirmed_destination'])
    def test_restart_and_duplicate_preserve_frozen_plan(self):
        r = self.submit('x' * 3995 + '[label](https://example.com/?a=1#z)' + '😀' * 2200)
        old = self.store.outbox_pending()
        self.assertGreater(len(old), 1)
        self.store = WorkflowStore(self.path)
        self.ingress = WorkflowIngress(self.config, self.store)
        with patch('hyemi_formatting.render_report', side_effect=AssertionError('duplicate rerender')):
            self.ingress.submit('fixture', self.owner, json.dumps(r).encode())
        self.assertEqual(self.store.outbox_pending(), old)

    def test_legacy_schema_states_and_raw_plan_never_reparsed(self):
        # Reconstruct the pre-rendering schema and real old rows, not mocks.
        self.workers.close()
        records = []
        for state in ('pending', 'sending', 'failed', 'unknown', 'delivered'):
            r = self.submit('**legacy** [original](https://example.com/?keep=1#f)')
            with self.store._db() as db:
                db.execute('UPDATE v2_outbox SET text=?,state=?,rendering=NULL WHERE event_id=?', (r['contents'], state, r['event_id']))
            records.append((r, state))
        with self.store._db() as db:
            db.execute('ALTER TABLE v2_outbox DROP COLUMN rendering')
        self.store = WorkflowStore(self.path)
        self.ingress = WorkflowIngress(self.config, self.store)
        with patch('hyemi_formatting.render_report', side_effect=AssertionError('legacy rerender')):
            for r, state in records:
                self.assertEqual(self.ingress.submit('fixture', self.owner, json.dumps(r).encode())['status'], 'duplicate')
                self.assertEqual(self.store.result(self.owner, r['event_id'])['delivery'][0]['state'], state)
        self.workers = WorkflowWorkers({}, self.store, SimpleNamespace(config=self.config))
        tokens = Mock(); tokens.get.return_value = '123:FAKE'
        self.workers.transport = TelegramTransport(tokens)
        answer = (200, {'ok': True, 'result': {'message_id': 1, 'chat': {'id': -100}, 'message_thread_id': 2}})
        with patch('hyemi_delivery.request_json', return_value=answer) as api:
            self.workers._deliver(); self.workers._deliver()
            self.assertEqual(api.call_count, 1)
            payload = json.loads(api.call_args.args[0].data)
            self.assertEqual(payload['text'], records[0][0]['contents'])
            self.assertNotIn('entities', payload)
            self.assertTrue(payload['disable_web_page_preview'])
        for r, state in records:
            actual = self.store.result(self.owner, r['event_id'])['delivery']
            self.assertEqual(len(actual), 1)
            self.assertEqual(actual[0]['state'], {'pending': 'delivered', 'sending': 'unknown'}.get(state, state))
            self.assertEqual(self.store.report(self.owner, r['event_id']), r)

    def test_new_failed_unknown_and_sending_stop_replay_after_restart(self):
        for state in ('failed', 'unknown', 'sending'):
            with self.subTest(state=state):
                r = self.submit('**' + 'x'*4100 + '** [link](https://example.com/)')
                row = next(x for x in self.store.outbox_pending() if x['event_id'] == r['event_id'])
                self.store.claim_outbox(row['id'])
                if state != 'sending': self.store.finish_outbox(row['id'], state, error='fixture')
                self.workers.close()
                self.store = WorkflowStore(self.path)
                self.ingress = WorkflowIngress(self.config, self.store)
                self.workers = WorkflowWorkers({}, self.store, SimpleNamespace(config=self.config))
                tokens = Mock(); tokens.get.return_value = '123:FAKE'
                self.workers.transport = TelegramTransport(tokens)
                self.ingress.submit('fixture', self.owner, json.dumps(r).encode())
                with patch('hyemi_delivery.request_json') as api:
                    self.workers._deliver(); api.assert_not_called()
                rows = self.store.result(self.owner, r['event_id'])['delivery']
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]['state'], 'unknown' if state == 'sending' else state)
                self.assertEqual(rows[1]['state'], 'pending')

    def test_report_payload_preserves_routing_guard_and_destination_proof(self):
        r = self.submit('[link](https://example.com)')
        self.config['allowed']['hyeji'] = {'channels': []}
        with patch('hyemi_delivery.request_json') as api:
            self.workers._deliver(); api.assert_not_called()
        self.assertEqual(self.store.result(self.owner, r['event_id'])['delivery'][0]['state'], 'blocked_configuration')
        self.config['allowed']['hyeji'] = {'channels': ['-100:2']}
        with patch('hyemi_delivery.request_json', return_value=(200, {'ok': True, 'result': {'message_id': 2, 'chat': {'id': -999}, 'message_thread_id': 2}})) as api:
            self.workers._deliver(); self.workers._deliver()
            self.assertEqual(api.call_count, 1)
        self.assertEqual(self.store.result(self.owner, r['event_id'])['delivery'][0]['state'], 'unknown')

    def test_no_partial_admission_on_render_or_enqueue_failure(self):
        from hyemi_store import NotFound
        for target in ('hyemi_formatting.render_report', 'hyemi_store.WorkflowStore._enqueue'):
            r = request('report', contents='**atomic**')
            with patch(target, side_effect=ValueError('fixture')):
                with self.assertRaises(ValueError): self.ingress.submit('fixture', self.owner, json.dumps(r).encode())
            with self.assertRaises(NotFound): self.store.event(self.owner, r['event_id'])
            self.assertEqual(self.store.outbox_pending(), [])

    def test_multiple_clickable_links_one_preview_and_exact_offsets_on_wire(self):
        source = '😀 [one](https://example.com/one?a=1#x) ' + 'a'*3990 + ' [two](https://example.org/two?b=2#y)'
        r = self.submit(source)
        answer = (200, {'ok': True, 'result': {'message_id': 1, 'chat': {'id': -100}, 'message_thread_id': 2}})
        with patch('hyemi_delivery.request_json', return_value=answer) as api:
            self.workers._deliver()
        bodies = [json.loads(c.args[0].data) for c in api.call_args_list]
        self.assertEqual(len(bodies), 2)
        self.assertEqual(sum(not b['link_preview_options']['is_disabled'] for b in bodies), 1)
        links = [(e['url'], b['text'].encode('utf-16-le')[2*e['offset']:2*(e['offset']+e['length'])].decode('utf-16-le')) for b in bodies for e in b['entities'] if e['type']=='text_link']
        self.assertEqual(links, [('https://example.com/one?a=1#x', 'one'), ('https://example.org/two?b=2#y', 'two')])
        self.assertEqual(self.store.report(self.owner, r['event_id'])['contents'], source)


class RendererTest(unittest.TestCase):
    def render(self, source):
        from hyemi_formatting import render_report
        return render_report(source)
    def test_utf16_boundaries_and_link_integrity(self):
        chunks = self.render('😀' * 1998 + '[named😀](https://example.com/a?x=1#f)' + '**' + '😀' * 4001 + '**')
        self.assertEqual(''.join(c['text'] for c in chunks), '😀' * 1998 + 'named😀' + '😀' * 4001)
        links = []
        for c in chunks:
            self.assertLessEqual(utf16_units(c['text']), 4000)
            raw = c['text'].encode('utf-16-le')
            for e in c['entities']:
                part = raw[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le')
                if e['type'] == 'text_link': links.append(part)
        self.assertEqual(links, ['named😀'])
        self.assertEqual(sum(not c['link_preview_options']['is_disabled'] for c in chunks), 1)
    def test_plain_malformed_unsafe_and_long_link_fallback(self):
        for s in ['plain <b>HTML</b> _unsupported_', '**unclosed', '`unclosed', '[x](javascript:alert(1))', '[x](https://user:pass@example.com/)', '[x](https://example.com/\n)', '[' + 'a'*4001 + '](https://example.com)']:
            with self.subTest(source=s[:80]):
                chunks = self.render(s)
                self.assertEqual(''.join(c['text'] for c in chunks), s)
                self.assertTrue(all(not c['entities'] for c in chunks))
                self.assertTrue(all(c['link_preview_options']['is_disabled'] for c in chunks))
    def test_private_preview_suppressed_public_selected(self):
        for host in ['127.0.0.1', '[::1]', '10.1.2.3', 'localhost', 'service.local', '2130706433', '127.1']:
            chunks = self.render('[local](http://' + host + '/) [public](https://example.com/a)')
            self.assertEqual(chunks[0]['link_preview_options'], {'is_disabled': False, 'url': 'https://example.com/a'})
    def test_url_validation_and_original_encoded_targets(self):
        from hyemi_formatting import valid_url, preview_safe
        for bad in ['javascript:alert(1)', 'file:///etc/passwd', 'https:///missing', 'https://a:pass@example.com/', 'https://example.com:99999/', 'https://[::1]evil/', 'https://example.com/%0aevil', 'https://example.com/\x00', 'https://bad_host/', 'https://example.com:/', 'https://example.com\\@evil.com']:
            with self.subTest(bad=bad): self.assertFalse(valid_url(bad))
        raw_ipv6 = self.render('http://[::1]/ https://example.com/a')
        self.assertEqual([e['type'] for e in raw_ipv6[0]['entities']], ['url', 'url'])
        self.assertEqual(raw_ipv6[0]['link_preview_options']['url'], 'https://example.com/a')
        original = 'https://example.com/a%20b?q=%22hello%22&utm=keep#original'
        self.assertTrue(valid_url(original))
        self.assertEqual(self.render('[source](' + original + ')')[0]['entities'][0]['url'], original)
        for private in ['http://[::ffff:127.0.0.1]/', 'http://169.254.169.254/', 'http://224.0.0.1/', 'http://[ff02::1]/', 'http://0x7f000001/', 'http://127.000.0.1/']:
            self.assertFalse(preview_safe(private))

    def test_entity_budget_and_all_outputs_validate(self):
        from hyemi_formatting import validate_chunk
        for source in ['**x** ' * 1000, '```\n' + '😀'*8001 + '\n```', '[' * 100000 + ']', 'plain😀' * 3000]:
            for chunk in self.render(source):
                validate_chunk(chunk['text'], chunk)
                self.assertLessEqual(len(chunk['entities']), 100)
                self.assertLessEqual(utf16_units(chunk['text']), 4000)

    def test_corrupt_plan_never_reaches_api(self):
        import copy
        plan = self.render('😀 [source](https://example.com)')[0]
        tokens = Mock(); tokens.get.return_value = '123:FAKE'
        cases = []
        bad = copy.deepcopy(plan); bad['entities'][0]['offset'] = 1; cases.append(bad)
        bad = copy.deepcopy(plan); bad['link_preview_options']['url'] = 'http://127.0.0.1/'; cases.append(bad)
        bad = copy.deepcopy(plan); bad['version'] = 999; cases.append(bad)
        bad = copy.deepcopy(plan); bad['entities'][0]['url'] = 'javascript:alert(1)'; cases.append(bad)
        with patch('hyemi_delivery.request_json') as api:
            for bad in cases:
                self.assertEqual(TelegramTransport(tokens).send('hyemi', '-100:2', bad['text'], rendering=bad).state, 'failed')
            self.assertEqual(TelegramTransport(tokens).send('hyeji', '-100:2', plan['text'], rendering=plan).state, 'failed')
            api.assert_not_called()

    def test_code_escape_raw_urls(self):
        chunks = self.render('\\*literal\\* `**raw**`\n```python\nx < y\n```\nhttps://example.com/a?utm=1#z')
        self.assertEqual(''.join(c['text'] for c in chunks), '*literal* **raw**\nx < y\n\nhttps://example.com/a?utm=1#z')
        self.assertEqual([e['type'] for e in chunks[0]['entities']], ['code', 'pre', 'url'])

if __name__ == '__main__': unittest.main()
