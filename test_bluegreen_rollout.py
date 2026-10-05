"""Offline operator tests; no subprocess or network is used."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from planning import bluegreen_rollout as bg

BASE = 'other.example {\n    reverse_proxy other:1234\n}\n\nbridge.example.invalid {\n    handle /mcp {\n        reverse_proxy hermes-bridge-mcp:8652\n    }\n    handle {\n        respond 404\n    }\n}\n'

class SyntaxTests(unittest.TestCase):
    def test_only_mcp_changes_and_roundtrip(self):
        new = bg.retarget(BASE, 'blue')
        self.assertEqual(bg.active_slot(new), 'blue')
        self.assertTrue(new.startswith(BASE.split('bridge.example.invalid')[0]))
        self.assertIn('stream_close_delay 5m', new)
        self.assertNotIn('lb_try', new)
        self.assertEqual(bg.retarget(new, 'legacy'), BASE)

    def test_reject_ambiguous_or_unrecognized(self):
        for text in [BASE + BASE, BASE.replace('/mcp', '/oops'), BASE.replace('hermes-bridge-mcp:8652', 'evil:8652'), BASE.replace('reverse_proxy hermes', 'header X x\n        reverse_proxy hermes')]:
            with self.subTest(text=text), self.assertRaises(bg.Refused):
                bg.active_slot(text)

    def test_audit1_exact_generator_and_all_slot_roundtrips(self):
        for slot in bg.SERVICES:
            for audited in (False, True):
                with self.subTest(slot=slot, audited=audited):
                    managed = bg.managed_stanza(slot, audit=audited)
                    text = ((bg.AUDIT_GLOBAL_PREFIX if audited else '') + BASE.split(bg.HOST)[0] +
                            managed.removeprefix(bg.AUDIT_GLOBAL_PREFIX) + '\ntrailer.example {\n    respond 204\n}\n')
                    self.assertEqual(bg.active_slot(text), slot)
                    self.assertEqual(bg.stanza(text).group('audit') or '', bg.AUDIT_PREFIX if audited else '')
                    for target in bg.SERVICES:
                        changed = bg.retarget(text, target)
                        self.assertEqual(bg.active_slot(changed), target)
                        self.assertEqual(bg.retarget(changed, slot), text)
                        self.assertEqual(changed.replace(bg.stanza(changed).group(), ''), text.replace(bg.stanza(text).group(), ''))

    def test_audit1_candidate_is_idempotent_and_preserves_routes(self):
        audited = bg.with_audit(BASE)
        self.assertEqual(bg.with_audit(audited), audited)
        self.assertEqual(audited.removeprefix(bg.AUDIT_GLOBAL_PREFIX).replace(bg.AUDIT_PREFIX, ''), BASE)
        self.assertEqual(bg.with_audit(bg.retarget(BASE, 'blue')), bg.retarget(audited, 'blue'))

    def test_audit1_only_exact_prefix_accepted(self):
        audited = bg.with_audit(BASE)
        variants = [audited + BASE, audited + audited,
                    audited.replace('request delete', 'request>headers delete'),
                    audited.replace('mode 0600', 'mode 0644'),
                    audited.replace('roll_keep 3', 'roll_keep 30'),
                    audited.replace('not path /mcp', 'not path /mcp*'),
                    audited.replace('log_append endpoint mcp', 'log_append endpoint {uri}'),
                    audited.replace(bg.AUDIT_PREFIX, bg.AUDIT_PREFIX * 2),
                    audited.replace('    handle /mcp', '    header X-Unsafe raw\n    handle /mcp'),
                    audited.replace('mcp-audit1.json', 'other.json')]
        for text in variants:
            with self.subTest(text=text), self.assertRaises(bg.Refused):
                bg.retarget(text, 'green')
        with self.assertRaises(bg.Refused): bg.managed_stanza('unknown')

    def test_in_place_keeps_inode_and_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'Caddyfile'
            p.write_text(BASE)
            p.chmod(0o640)
            before = p.stat()
            bg.write_in_place(p, b'new')
            self.assertEqual(p.stat().st_ino, before.st_ino)
            self.assertEqual(p.stat().st_mode, before.st_mode)
            self.assertEqual(p.read_bytes(), b'new')

class FakeIO:
    def __init__(self, text=BASE):
        self.text = text
        self.live = self.adapt(text)
        self.events = []
        self.fail_reload = False
        self.bad_mounted = False
        self.containers = {'legacy': 'legacy-id'}
        self.counts = [0]
    def adapt(self, text): return {'adapted': text}
    def admin(self): return self.live
    def mounted(self): return b'bad' if self.bad_mounted else self.text.encode()
    def validate(self, text): self.events.append('validate')
    def reload(self, text):
        self.events.append('reload')
        self.live = self.adapt(text)
        if self.fail_reload: raise RuntimeError('response lost')
    def restore(self, config):
        self.events.append('restore')
        self.live = config
    def container(self, slot, required=True):
        value = self.containers.get(slot)
        if not value and required: raise bg.Refused('missing container')
        return value
    def start(self, slot, manifest):
        self.events.append('start:' + slot)
        self.containers[slot] = slot + '-id'
    def gate(self, slot, manifest): self.events.append('gate:' + slot)
    def source_gate(self, manifest): self.events.append('source_gate')
    def probe(self, slot): self.events.append('probe:' + slot)
    def in_flight(self, slot):
        result = self.counts[0]
        if len(self.counts)>1: self.counts.pop(0)
        return result
    def stop(self, slot, cid): self.events.append('stop:' + slot)
    def boundary(self): return {'status':401, 'error':None, 'latency_ms':1, 'time':0}

class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / 'Caddyfile'
        self.file.write_text(BASE)
        self.io = FakeIO(BASE)
        self.op = bg.Operator(self.io, self.file, self.root / 'audit', interval=.001, samples=2)
        original = bg.write_in_place
        def persist(path, data):
            original(path, data)
            self.io.text = data.decode()
        self.persist = patch.object(bg, 'write_in_place', side_effect=persist)
        self.persist.start()
    def tearDown(self):
        self.persist.stop()
        self.tmp.cleanup()
    def test_staging_does_not_touch_active_or_proxy(self):
        self.op.stage('blue', {})
        self.assertIn('start:blue', self.io.events)
        self.assertNotIn('reload', self.io.events)
        self.assertEqual(self.file.read_text(), BASE)
        for slot in ['legacy', 'blue']:
            with self.assertRaises(bg.Refused): self.op.stage(slot, {})
    def test_stage_failure_keeps_original(self):
        self.io.probe = lambda slot: (_ for _ in ()).throw(bg.Refused('not ready'))
        with self.assertRaises(bg.Refused): self.op.stage('green', {})
        self.assertEqual(self.file.read_text(), BASE)
        self.assertEqual(self.io.live, self.io.adapt(BASE))
        self.assertNotIn('stop:legacy', self.io.events)
    def test_promote_and_exact_rollback(self):
        self.io.containers['blue'] = 'blue-id'
        journal = self.op.promote('blue', {})
        self.assertEqual(bg.active_slot(self.file.read_text()), 'blue')
        self.assertEqual(self.io.live, self.io.adapt(self.file.read_text()))
        self.assertIn('gate:blue', self.io.events)
        self.assertEqual(len(json.loads((journal / 'monitor.json').read_text())), 2)
        self.op.rollback(journal)
        self.assertEqual(self.file.read_text(), BASE)
        self.assertEqual(self.io.live, self.io.adapt(BASE))
    def test_response_loss_restores_exact_full_config(self):
        self.io.containers['blue'] = 'blue-id'
        self.io.fail_reload = True
        with self.assertRaises(RuntimeError): self.op.promote('blue', {})
        self.assertIn('restore', self.io.events)
        self.assertEqual(self.io.live, self.io.adapt(BASE))
        self.assertEqual(self.file.read_text(), BASE)
        self.assertNotIn('stop:legacy', self.io.events)
    def test_drift_blocks_before_mutation(self):
        self.io.live = {'drift': True}
        with self.assertRaises(bg.Refused): self.op.stage('blue', {})
        self.assertNotIn('start:blue', self.io.events)
    def test_manifest_gate_failure_blocks_promotion(self):
        self.io.containers['blue'] = 'blue-id'
        self.io.gate = lambda *args: (_ for _ in ()).throw(bg.Refused('image mismatch'))
        with self.assertRaises(bg.Refused): self.op.promote('blue', {})
        self.assertNotIn('reload', self.io.events)
    def test_legacy_and_timeout_retain_old(self):
        self.io.text = bg.retarget(BASE, 'blue')
        self.file.write_text(self.io.text)
        self.io.live = self.io.adapt(self.io.text)
        self.assertFalse(self.op.drain('legacy', timeout=.01, grace=.001, stop=True))
        self.io.containers['green'] = 'green-id'
        self.io.counts = [1]
        self.assertFalse(self.op.drain('green', timeout=.003, grace=.001, stop=True))
        self.assertNotIn('stop:green', self.io.events)
    def test_direct_origin_monitor_keeps_sni_and_no_retry(self):
        result = type('Result', (), {'stdout': b'401', 'stderr': b'', 'returncode': 0})()
        with patch.object(bg.subprocess, 'run', return_value=result) as run:
            rounds = bg.DockerIO('caddy', '/etc/caddy/Caddyfile', self.root).boundary()
        self.assertEqual({s['kind'] for s in rounds}, {'public_edge', 'direct_origin'})
        calls = [c.args[0] for c in run.call_args_list]
        origin = next(c for c in calls if '--resolve' in c)
        edge = next(c for c in calls if '--resolve' not in c)
        self.assertIn(bg.HOST + ':443:127.0.0.1', origin)
        for cmd in (origin, edge):
            self.assertEqual(cmd[-1], 'https://' + bg.HOST + '/mcp')
            self.assertEqual(cmd[cmd.index('--retry') + 1], '0')
            self.assertIn('POST', cmd)
            self.assertNotIn('--insecure', cmd)
            self.assertFalse(any('Authorization' in token for token in cmd))

    def test_origin_failure_even_edge_401_rolls_back(self):
        self.io.containers['blue'] = 'blue-id'
        self.io.boundary = lambda: [{'kind': 'public_edge', 'status': 401, 'error': None}, {'kind': 'direct_origin', 'status': 401 if self.io.live == self.io.adapt(BASE) else 502, 'error': None}]
        with self.assertRaises(bg.Refused): self.op.promote('blue', {})
        self.assertIn('restore', self.io.events)
        self.assertEqual(self.io.live, self.io.adapt(BASE))
        self.assertEqual(self.file.read_text(), BASE)

    def test_rollback_failure_restores_pre_rollback_state(self):
        self.io.containers['blue'] = 'blue-id'
        journal = self.op.promote('blue', {})
        after = self.file.read_text()
        original_restore = self.io.restore
        calls = []
        def fail_once(config):
            original_restore(config)
            calls.append(config)
            if len(calls) == 1: raise RuntimeError('rollback response lost')
        self.io.restore = fail_once
        with self.assertRaises(RuntimeError): self.op.rollback(journal)
        self.assertEqual(self.file.read_text(), after)
        self.assertEqual(self.io.live, self.io.adapt(after))

    def test_stable_zero_required_and_active_never_stopped(self):
        self.io.containers['blue'] = 'blue-id'
        self.io.counts = [0, 1, 0, 0, 0, 0, 0, 0]
        self.assertTrue(self.op.drain('blue', timeout=.1, grace=.003, stop=True))
        self.assertIn('stop:blue', self.io.events)
        with self.assertRaises(bg.Refused): self.op.drain('legacy', timeout=.1, grace=.001, stop=True)

class Audit1OperatorTests(OperatorTests):
    """Run the complete existing operator contract with the audited prefix."""
    def setUp(self):
        self.base_patch = patch(__name__ + '.BASE', bg.with_audit(BASE))
        self.base_patch.start()
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            self.base_patch.stop()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(bg, 'ROOT', self.root)
        self.root_patch.start()
        hashes = {}
        for file in bg.REVIEW_FILES:
            path = self.root / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('reviewed:' + file)
            hashes[file] = bg.sha(path.read_bytes())
        self.image_id = 'sha256:' + '1' * 64
        self.manifest = {'schema': 1, 'reviewer': 'offline fixture', 'reviewed_at': 'fixture',
                         'image_ref': 'local/test:0.4.0', 'image_id': self.image_id,
                         'source_sha256': hashes,
                         'runtime_sha256': {'/app/' + f: hashes[f] for f in bg.IMAGE_FILES}}
        self.manifest['runtime_sha256']['/app/hyemi_ingress.py'] = '2' * 64
        self.io = bg.DockerIO('caddy', '/etc/caddy/Caddyfile', self.root)
        self.io.container = lambda *a, **k: 'candidate-id'
        self.running_image = self.image_id
        self.runtime = dict(self.manifest['runtime_sha256'])
        def run(*cmd, **kwargs):
            if cmd[:3] == ('docker', 'image', 'inspect'):
                return json.dumps([{'Id': self.image_id}]).encode()
            if cmd[:2] == ('docker', 'inspect'):
                return json.dumps([{'Id': 'candidate-id', 'Image': self.running_image, 'State': {'Running': True}}]).encode()
            if cmd[:2] == ('docker', 'exec'):
                return json.dumps(self.runtime).encode()
            raise AssertionError(cmd)
        self.io.run = run
    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()
    def test_exact_reviewed_image_and_runtime_bytes(self):
        self.io.gate('blue', self.manifest)
        self.runtime['/app/bridge.py'] = '3' * 64
        with self.assertRaises(bg.Refused): self.io.gate('blue', self.manifest)
    def test_running_image_mismatch_and_source_drift(self):
        self.running_image = 'sha256:' + '3' * 64
        with self.assertRaises(bg.Refused): self.io.gate('blue', self.manifest)
        self.running_image = self.image_id
        (self.root / 'bridge.py').write_text('unreviewed edit')
        with self.assertRaises(bg.Refused): self.io.gate('blue', self.manifest)
    def test_missing_reviewed_files_and_secret_paths_refused(self):
        self.manifest['runtime_sha256']['/run/secrets/key'] = '4' * 64
        with self.assertRaises(bg.Refused): self.io.source_gate(self.manifest)
        self.manifest['runtime_sha256'].pop('/run/secrets/key')
        self.manifest['source_sha256'].pop('Dockerfile')
        with self.assertRaises(bg.Refused): self.io.source_gate(self.manifest)
    def test_manifest_exact_byte_digest(self):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(self.manifest))
        digest = bg.sha(path.read_bytes())
        self.assertEqual(bg.load_manifest(path, digest), self.manifest)
        path.write_text(path.read_text() + ' ')
        with self.assertRaises(bg.Refused): bg.load_manifest(path, digest)
    def test_private_probe_no_auth_redirect_or_retry(self):
        def run(*cmd, **kwargs):
            code = cmd[-1]
            compile(code, '<private-probe>', 'exec')
            self.assertIn('NoRedirect', code)
            self.assertIn("method='POST'", code)
            self.assertNotIn('Authorization', code)
            self.assertNotIn('retry', code)
            return b'{"status":401,"body":"missing auth"}'
        self.io.run = run
        self.assertEqual(self.io.private('blue', '/mcp', 'POST')['status'], 401)

class ComposeTests(unittest.TestCase):
    def test_opt_in_slots_keep_legacy_hardening(self):
        import yaml
        config = yaml.safe_load((bg.ROOT / 'compose.yaml').read_text())
        legacy = config['services']['hermes-bridge-mcp']
        self.assertEqual(legacy['image'], 'local/hermes-bridge-mcp:0.3.0')
        self.assertNotIn('profiles', legacy)
        for slot in ('blue', 'green'):
            candidate = config['services']['hermes-bridge-mcp-' + slot]
            self.assertEqual(candidate['profiles'], [slot])
            self.assertNotIn('ports', candidate)
            self.assertNotIn('build', candidate)
            self.assertEqual({k: v for k, v in candidate.items() if k not in ('profiles', 'image')},
                             {k: v for k, v in legacy.items() if k not in ('build', 'image')})

if __name__ == '__main__': unittest.main()
