"""Offline edge-normalizing Discord model; no live behavior claim or sends."""
import copy
import json
import unittest
from unittest.mock import patch

import test_dual_platform as fixtures
from hyemi_delivery import split_text, utf16_units
from hyemi_formatting import discord_text, render_discord
from hyemi_store import WorkflowStore

GUARD = '\u200b'
SOURCE = ' \t공백 보존 수정 전송 확인 v2\n' + '😀 body <@123> \u200b\n' * 220 + '\t \u3000\n'


def legacy_render(text):
    return [dict(platform='discord', text=p,
                 allowed_mentions={'parse': [], 'replied_user': False})
            for p in split_text(discord_text(text), 2000)]


class RenderingTests(unittest.TestCase):
    def assert_preserved(self, source):
        pieces = split_text(discord_text(source), 1998)
        plans = render_discord(source)
        self.assertEqual(plans, render_discord(source))
        self.assertEqual(len(pieces), len(plans))
        recovered = []
        for piece, plan in zip(pieces, plans):
            left, right = piece[:1].isspace(), piece[-1:].isspace()
            expected = (GUARD if left else '') + piece + (GUARD if right else '')
            self.assertEqual(plan, dict(platform='discord', text=expected,
                                       allowed_mentions={'parse': [], 'replied_user': False}))
            self.assertLessEqual(utf16_units(plan['text']), 2000)
            self.assertNotIn('<@', plan['text'])
            self.assertEqual(plan['text'].strip(), plan['text'])
            # Remove only source-proven inserted positions, NOT literal U+200B.
            recovered.append(plan['text'][int(left):len(plan['text'])-int(right)])
        self.assertEqual(''.join(recovered), discord_text(source))

    def test_ascii_unicode_whitespace_and_emoji_boundaries(self):
        for edge in (' ', '\n', '\t', '\r\n', '\v', '\f', '\u0085', '\u00a0', '\u1680', '\u2000', '\u2028', '\u2029', '\u202f', '\u205f', '\u3000'):
            for body in ('x', 'x'*1997, 'x'*1998, '😀'*999, '😀'*1000, 'x'*1997+'😀', '😀'*2300):
                with self.subTest(edge=repr(edge), length=len(body)):
                    self.assert_preserved(edge + body + edge)

    def test_whitespace_only_intermediate_chunks(self):
        self.assert_preserved('start' + ' '*6000 + 'end')
        self.assert_preserved(' \t\n'*2000)

    def test_literal_guards_and_mention_breaks_not_removed(self):
        for source in (GUARD, GUARD+' '+GUARD, ' '+GUARD+' ',
                       '<@123> <@&123> <@!123> '+GUARD,
                       'x'*1997+'<@123> '+GUARD, SOURCE):
            with self.subTest(source_length=len(source)):
                self.assert_preserved(source)

    def test_invalid_input_stays_rejected(self):
        for source in ('', None, 7, '\ud800'):
            with self.subTest(source=repr(source)), self.assertRaises((ValueError, TypeError, AttributeError, UnicodeError)):
                render_discord(source)

    def test_seeded_unicode_preservation(self):
        import random
        rng = random.Random(731)
        alphabet = 'ab 😀\t\n\u3000\u00a0\u200b<@&123>'
        for _ in range(30):
            self.assert_preserved(''.join(rng.choice(alphabet) for _ in range(rng.randrange(1, 10000))))

    def test_no_whitespace_does_not_add_guards(self):
        self.assertEqual(render_discord('plain')[0]['text'], 'plain')
        self.assert_preserved('x'*2000)


class CanaryWhitespaceTests(unittest.TestCase):
    # Reuse only real-store/main fixture plumbing, not inherited test methods.
    setUp = fixtures.CanaryTests.setUp
    dedicated_token = fixtures.CanaryTests.dedicated_token
    private_open = fixtures.CanaryTests.private_open
    invoke = fixtures.CanaryTests.invoke

    def external(self, req):
        code, value = fixtures.CanaryTests.external(self, req)
        if '/messages' in req.full_url:
            value = copy.deepcopy(value)
            value['content'] = value['content'].strip()
            if req.get_method() == 'POST':
                self.messages[value['id']] = value
        return code, value

    def rows(self):
        with self.store._db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM v2_outbox WHERE event_id=? ORDER BY id', (self.body['event_id'],))]

    def assert_no_replay(self):
        before = self.rows()
        self.store = WorkflowStore(self.store.path)
        from hyemi_workers import WorkflowWorkers
        from types import SimpleNamespace
        worker = WorkflowWorkers({}, self.store, SimpleNamespace(config=self.cfg))
        self.addCleanup(worker.close)
        with patch('hyemi_formatting.render_discord', side_effect=AssertionError('must not re-render')):
            self.store.admit(self.canary.OPERATOR_OWNER, self.body)
            with patch.object(worker.discord_transport, 'send') as send:
                worker._deliver()
                send.assert_not_called()
        self.assertEqual(before, self.rows())

    def test_edge_stripping_model_real_main_worker_exact_success(self):
        with patch.object(self.canary, 'CONTENTS', SOURCE):
            self.assertEqual(self.invoke(), 0)
            receipt = json.loads(self.output.read_text())
            self.assertTrue(receipt['raw_report_exact'])
            self.assertTrue(receipt['discord_get_verified'])
            self.assertGreater(self.discord_gets, 1)
            self.assertEqual(self.store.report(self.canary.OPERATOR_OWNER, self.body['event_id'])['contents'], SOURCE)
            self.assertEqual(self.store.result(self.canary.OPERATOR_OWNER, self.body['event_id'])['contents'], SOURCE)
            self.assert_no_replay()
            self.assertEqual(self.invoke(reconcile=True), 0)
            self.assertEqual(self.posts, 1)

    def test_actual_v2_canary_contents_through_normalizing_model(self):
        self.assertIn('공백 보존 수정 전송 확인 v2', self.canary.CONTENTS)
        self.assertTrue(self.canary.CONTENTS[-1].isspace())
        self.assertEqual(self.invoke(), 0)
        self.assertGreater(self.discord_gets, 1)
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.store.report(self.canary.OPERATOR_OWNER, self.body['event_id'])['contents'], self.canary.CONTENTS)

    def test_old_discord_plans_all_states_preserved_on_restart_and_dedupe(self):
        for state in ('pending', 'blocked_configuration', 'sending', 'unknown', 'failed', 'delivered'):
            with self.subTest(state=state):
                body = fixtures.request(contents=SOURCE)
                with patch('hyemi_formatting.render_discord', side_effect=legacy_render):
                    self.store.admit(self.canary.OPERATOR_OWNER, body)
                self.body = body
                # Fixture-only state setup; no production database is opened.
                with self.store._db() as db:
                    db.execute('UPDATE v2_outbox SET state=? WHERE event_id=?', (state, body['event_id']))
                before = self.rows()
                with patch('hyemi_formatting.render_discord', side_effect=AssertionError('no re-render')):
                    self.store = WorkflowStore(self.store.path)
                    self.store.admit(self.canary.OPERATOR_OWNER, body)
                self.assertEqual(before, self.rows())
                self.assertEqual([json.loads(row['rendering']) for row in before], legacy_render(SOURCE))

    def test_baseline_unknown_old_frozen_plan_remains_unreplayed(self):
        with patch.object(self.canary, 'CONTENTS', SOURCE), patch('hyemi_formatting.render_discord', side_effect=legacy_render):
            self.assertEqual(self.invoke(), 1)
        rows = self.rows()
        self.assertEqual(rows[0]['state'], 'unknown')
        self.assertTrue(all(row['state'] == 'pending' for row in rows[1:]))
        self.assertEqual([json.loads(r['rendering']) for r in rows], legacy_render(SOURCE))
        self.assert_no_replay()
        with patch.object(self.canary, 'CONTENTS', SOURCE):
            self.assertEqual(self.invoke(reconcile=True), 1)
        self.assertEqual(self.posts, 1)
        self.assertEqual(rows, self.rows())

    def test_old_pending_plan_delivers_exact_frozen_bytes_not_rerender(self):
        original_deliver = self.w._deliver
        with patch.object(self.canary, 'CONTENTS', 'old '+ 'x'*2100), patch('hyemi_formatting.render_discord', side_effect=legacy_render), patch.object(self.w, '_deliver'):
            # Invoke would poll; admit directly through the real store instead.
            body = fixtures.request(contents='old '+ 'x'*2100)
            self.store.admit(self.canary.OPERATOR_OWNER, body)
        self.body = body
        before = [(r['text'], r['rendering']) for r in self.rows()]
        with patch('hyemi_formatting.render_discord', side_effect=AssertionError('frozen only')):
            original_deliver()
        self.assertEqual(before, [(r['text'], r['rendering']) for r in self.rows()])
        self.assertTrue(all(r['state'] == 'delivered' for r in self.rows()))

    def test_post_content_corruption_still_unknown_and_blocks_remainder(self):
        original = self.external
        mutations = [lambda s: 'wrong body', lambda s: s.replace('body ', 'body', 1),
                     lambda s: s.replace(GUARD, '', 1), lambda s: s.replace(GUARD, '\u2060', 1)]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                def bad(req):
                    code, value = original(req)
                    if req.get_method() == 'POST' and '/messages' in req.full_url:
                        value = dict(value, content=mutate(value['content']))
                    return code, value
                self.posts = 0
                with patch.object(self.canary, 'CONTENTS', SOURCE), patch('hyemi_delivery.request_json', side_effect=bad):
                    self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.rows()[0]['state'], 'unknown')
                self.assertTrue(all(r['state'] == 'pending' for r in self.rows()[1:]))
                self.assert_no_replay()
                self.output.unlink()

    def test_get_exact_proof_rejects_body_guard_and_identity_changes(self):
        original = self.external
        mutations = [lambda v: dict(v, content='wrong'),
                     lambda v: dict(v, content=v['content'].replace('body ', 'body', 1)),
                     lambda v: dict(v, content=v['content'].replace(GUARD, '', 1)),
                     lambda v: dict(v, id='000123'),
                     lambda v: dict(v, channel_id=fixtures.OTHER),
                     lambda v: dict(v, author={'id':'123', 'bot':True}),
                     lambda v: dict(v, guild_id='123')]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                def bad(req):
                    code, value = original(req)
                    if req.get_method() == 'GET' and '/messages/' in req.full_url:
                        value = mutate(value)
                    return code, value
                self.posts = 0
                with patch.object(self.canary, 'CONTENTS', SOURCE), patch('hyemi_delivery.request_json', side_effect=bad):
                    self.assertEqual(self.invoke(), 1)
                    self.assertFalse(json.loads(self.output.read_text())['ok'])
                    # Helper failure is not a recovery write to transport states.
                    self.assertEqual(self.invoke(reconcile=True), 1)
                self.assertEqual(self.posts, 1)
                self.output.unlink()


if __name__ == '__main__':
    unittest.main()
