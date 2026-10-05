"""Opt-in installed-Caddy tests: AUDIT1_CADDY_PROBE=1 /usr/bin/python3 -m unittest test_audit1_caddy -v.
Uses read-only production adapt plus temporary network=none clones only.
"""
import json
import os
from pathlib import Path
import unittest
from planning import bluegreen_rollout as bg
from planning.audit1_probe import adapt, assert_rows, probe


class AuditSchemaTests(unittest.TestCase):
    def test_named_error_policy_is_exact_and_conflicts_refused(self):
        text = bg.managed_stanza('blue', audit=True)
        self.assertTrue(text.startswith(bg.AUDIT_GLOBAL_PREFIX))
        self.assertIn('exclude http.log.error.mcp_audit1 http.log.access', bg.AUDIT_GLOBAL_PREFIX)
        for conflict in ('{\n    debug\n}\n', 'import other.conf\n',
                         'other.example {\n    log\n}\n',
                         '  {\n debug\n}\n', 'other.example { log }\n',
                         '(shared) { respond 200 }\n'):
            with self.subTest(conflict=conflict), self.assertRaises(bg.Refused):
                bg.with_audit(conflict + bg.managed_stanza('blue'))
        for bad in (text.replace(bg.AUDIT_GLOBAL_PREFIX, ''),
                    text.replace('http.log.error.mcp_audit1', 'http.log.error'),
                    text + bg.AUDIT_GLOBAL_PREFIX,
                    bg.managed_stanza('blue') + '\n' + bg.AUDIT_GLOBAL_PREFIX,
                    bg.AUDIT_GLOBAL_PREFIX + bg.managed_stanza('blue')):
            with self.subTest(bad=bad), self.assertRaises(bg.Refused):
                bg.retarget(bad, 'green')

    def test_closed_output_schema_rejects_unknown_strings_and_missing_fields(self):
        row = {'ts': 1, 'bytes_read': 0, 'duration': .01, 'size': 0, 'status': 401, 'endpoint': 'mcp'}
        assert_rows(json.dumps(row), 1)
        for change in ({'request': {}}, {'raw': 'private'}, {'duration': 'private'},
                       {'endpoint': '/mcp'}, {'ts': True}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                assert_rows(json.dumps(dict(row, **change)), 1)


@unittest.skipUnless(os.environ.get('AUDIT1_CADDY_PROBE') == '1', 'explicit isolated Docker probe opt-in required')
class InstalledCaddyTests(unittest.TestCase):
    def test_old_audited_all_slots_adapt_and_preserve_other_routes(self):
        before = Path('/opt/bridge-edge/Caddyfile').read_text()
        baseline_sha = bg.sha(before.encode())  # caller-provided baseline, not historical evidence
        candidate = bg.with_audit(before)
        self.assertEqual(candidate.removeprefix(bg.AUDIT_GLOBAL_PREFIX).replace(bg.AUDIT_PREFIX, ''), before)
        old = adapt(before)
        new = adapt(candidate)
        self.assertFalse(old.get('logging'))  # inspected baseline: no global log policy to overwrite
        for server in old['apps']['http']['servers'].values():
            self.assertNotIn('logs', server)  # no pre-existing access fallback
        self.assertEqual(set(new['logging']['logs']['default']['exclude']), {
            'http.log.access', 'http.log.access.mcp_audit1', 'http.log.error.mcp_audit1'})
        # Other virtual-host route trees must be semantically unchanged too.
        def unrelated(config):
            return [route for server in config['apps']['http']['servers'].values()
                    for route in server.get('routes', [])
                    if not any(bg.HOST in m.get('host', []) for m in route.get('match', []))]
        self.assertEqual(unrelated(new), unrelated(old))
        for audit in (False, True):
            for slot in bg.SERVICES:
                with self.subTest(audit=audit, slot=slot):
                    text = bg.managed_stanza(slot, audit=audit)
                    result = adapt(text)
                    self.assertEqual(bg.active_slot(text), slot)
                    self.assertEqual('mcp_audit1' in result.get('logging', {}).get('logs', {}), audit)
                    self.assertIn(bg.SERVICES[slot] + ':8652', json.dumps(result))
        self.assertEqual(bg.sha(Path('/opt/bridge-edge/Caddyfile').read_bytes()), baseline_sha)

    def test_real_output_and_size_count_age_rotation(self):
        for rotation, archives in ((None, 0), ('count', 3), ('age', 2)):
            with self.subTest(rotation=rotation):
                result = probe(rotation)
                self.assertEqual(result['archive_count'], archives)
                self.assertEqual(len(result['rows']), 9)
                self.assertEqual([r['status'] for r in result['error_rows']], [502, 503, 502, 503])
                self.assertTrue(result['unrelated_error_preserved'])
                self.assertEqual(result['transmitted_authorization'], 'Bearer AUDIT1_BEARER_SECRET')
                self.assertEqual({r['stream'] for r in result['runtime']}, {'stdout', 'stderr'})
                self.assertEqual(len(result['outputs']), 2 + 2 * archives)
                self.assertTrue(result['sentinels_absent'])
                self.assertTrue(result['non_mcp_excluded'])


if __name__ == '__main__':
    unittest.main()
