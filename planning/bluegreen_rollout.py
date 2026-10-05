#!/usr/bin/env python3
"""Explicit operator-only blue/green rollout. Importing this module has no side effects.
No tokens are read, no authenticated requests are sent, and no request is retried.
"""
import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
HOST = 'bridge.example.invalid'
SERVICES = {s: 'hermes-bridge-mcp' + ('' if s == 'legacy' else '-' + s) for s in ('legacy', 'blue', 'green')}
IMAGE_FILES = ('bridge.py', 'hyemi_contracts.py', 'naver_tools.py', 'naver_contracts.py', 'naver_client.py', 'requirements.txt')
REVIEW_FILES = IMAGE_FILES + ('Dockerfile', 'compose.yaml', 'planning/bluegreen_rollout.py', 'test_bluegreen_rollout.py', 'planning/bluegreen-operational-handoff.md')
UA = 'Hermes-Bridge-Rollout-Monitor/0.4 (unauthenticated operator boundary check)'
OPTIONS = ' {\n            health_uri /readyz\n            health_status 200\n            health_interval 10s\n            health_timeout 5s\n            stream_close_delay 5m\n        }'
# Versioned, exact managed prefix: do not broaden this to arbitrary Caddy nodes.
# Re-review the output schema and rerun audit1's real-binary probe on upgrades.
AUDIT_PATH = '/data/mcp-audit1.json'
AUDIT_ERROR_PATH = '/data/mcp-error-audit1.json'
AUDIT_GLOBAL_PREFIX = '''{
    log default {
        exclude http.log.error.mcp_audit1 http.log.access
    }
    log mcp_error_audit1 {
        include http.log.error.mcp_audit1
        output file /data/mcp-error-audit1.json {
            mode 0600
            roll_size 5MiB
            roll_keep 3
            roll_keep_for 168h
        }
        format filter {
            wrap json {
                time_format unix_seconds_float
                duration_format seconds
                message_key ""
                level_key ""
                name_key ""
                caller_key ""
                stacktrace_key ""
            }
            fields {
                request delete
                resp_headers delete
                user_id delete
                error delete
                err_id delete
                err_trace delete
            }
        }
    }
}
'''
AUDIT_PREFIX = '''    log mcp_audit1 {
        output file /data/mcp-audit1.json {
            mode 0600
            roll_size 5MiB
            roll_keep 3
            roll_keep_for 168h
        }
        format filter {
            wrap json {
                time_format unix_seconds_float
                duration_format seconds
                message_key ""
                level_key ""
                name_key ""
                caller_key ""
                stacktrace_key ""
            }
            fields {
                request delete
                resp_headers delete
                user_id delete
                error delete
                err_id delete
                err_trace delete
            }
        }
    }
    @audit1_non_mcp not path /mcp
    log_skip @audit1_non_mcp
    log_append endpoint mcp
'''
PATTERN = re.compile(r'(?m)^bridge\.example\.invalid \{\n(?P<audit>' + re.escape(AUDIT_PREFIX) + r')?    handle /mcp \{\n        reverse_proxy (?P<target>hermes-bridge-mcp(?:-blue|-green)?:8652)(?P<options>' + re.escape(OPTIONS) + r')?\n    \}\n    handle \{\n        respond 404\n    \}\n\}(?=\n|$)')

class Refused(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def stanza(text):
    matches = list(PATTERN.finditer(text))
    if len(matches) != 1 or len(re.findall(r'(?m)^bridge\.example\.invalid\s*\{', text)) != 1:
        raise Refused('MCP stanza is absent, ambiguous, or not the exact managed form')
    m = matches[0]
    has_global = text.startswith(AUDIT_GLOBAL_PREFIX)
    if text.count(AUDIT_GLOBAL_PREFIX) != int(has_global):
        raise Refused('audit1 global prefix must appear exactly once at the start or not at all')
    if bool(m.group('audit')) != has_global:
        raise Refused('audit1 requires the exact paired global and site prefixes')
    if has_global:
        _audit_unmanaged_guard(text[len(AUDIT_GLOBAL_PREFIX):].replace(AUDIT_PREFIX, '', 1))
    return m


def _audit_unmanaged_guard(text):
    # Baseline has no global options or access logging. Never merge arbitrary
    # globals, imports, snippets or log policies: those need independent review.
    # Also reject compact/indented directives, not only column-zero spellings.
    clean = '\n'.join(line.split('#', 1)[0] for line in text.splitlines())
    if (re.search(r'(?m)^\s*\{', clean) or
            re.search(r'(^|[\s{}])(import|log|log_skip|log_append|log_name)\b', clean) or
            re.search(r'(?m)^\s*\(', clean)):
        raise Refused('unsupported global/import/snippet/logging configuration for audit1')


def managed_stanza(slot, *, audit=False):
    """Return a candidate only; never read or mutate live configuration."""
    if slot not in SERVICES:
        raise Refused('unknown slot')
    return ((AUDIT_GLOBAL_PREFIX if audit else '') + HOST + ' {\n' + (AUDIT_PREFIX if audit else '') +
            '    handle /mcp {\n        reverse_proxy ' + SERVICES[slot] + ':8652' +
            ('' if slot == 'legacy' else OPTIONS) +
            '\n    }\n    handle {\n        respond 404\n    }\n}')


def with_audit(text):
    """Offline, idempotent insertion; preserve every existing byte otherwise."""
    m = stanza(text)
    if m.group('audit'):
        return text
    _audit_unmanaged_guard(text)
    insertion = m.start() + len(HOST + ' {\n')
    return AUDIT_GLOBAL_PREFIX + text[:insertion] + AUDIT_PREFIX + text[insertion:]


def active_slot(text):
    target = stanza(text).group('target')
    return next(s for s, name in SERVICES.items() if target == name + ':8652')


def retarget(text, slot):
    if slot not in SERVICES:
        raise Refused('unknown slot')
    m = stanza(text)
    replacement = SERVICES[slot] + ':8652' + ('' if slot == 'legacy' else OPTIONS)
    return text[:m.start('target')] + replacement + text[m.end('options') if m.group('options') else m.end('target'):]


def write_in_place(path, data):
    """Do not replace bind-mounted inode, ownership, or mode."""
    with open(path, 'r+b') as out:
        out.seek(0)
        out.write(data)
        out.truncate()
        out.flush()
        os.fsync(out.fileno())


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)


class DockerIO:
    def __init__(self, caddy, mounted_path, audit):
        self.caddy, self.mounted_path, self.audit = caddy, mounted_path, audit

    def run(self, *args, input=None, env=None):
        result = subprocess.run(list(args), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60, check=False, env=env)
        if result.returncode:
            raise Refused('command failed: ' + ' '.join(args[:5]) + ': ' + result.stderr.decode(errors='replace')[:2000])
        return result.stdout

    def compose(self, *args, env=None):
        return self.run('docker', 'compose', '-f', str(ROOT / 'compose.yaml'), *args, env=env)

    def container(self, slot, required=True):
        ids = self.compose('ps', '-a', '-q', SERVICES[slot]).decode().split()
        if len(ids) > 1 or (required and len(ids) != 1):
            raise Refused('expected exactly one container: ' + slot)
        return ids[0] if ids else None

    def inspect_container(self, slot):
        obj = json.loads(self.run('docker', 'inspect', self.container(slot)))[0]
        if not obj['State']['Running']:
            raise Refused('slot not running: ' + slot)
        return obj

    def source_gate(self, manifest):
        if manifest.get('schema') != 1 or not manifest.get('reviewer') or not manifest.get('reviewed_at'):
            raise Refused('manifest must record independent approval')
        expected = manifest.get('source_sha256', {})
        if not set(REVIEW_FILES).issubset(expected):
            raise Refused('manifest omits required reviewed files')
        for relative, digest in expected.items():
            p = (ROOT / relative).resolve()
            if not p.is_relative_to(ROOT) or not p.is_file() or sha(p.read_bytes()) != digest:
                raise Refused('reviewed source mismatch: ' + relative)
        image_id = manifest.get('image_id', '')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise Refused('exact Docker image ID required')
        image = json.loads(self.run('docker', 'image', 'inspect', manifest['image_ref']))[0]
        if image['Id'] != image_id:
            raise Refused('image reference drift')
        expected_runtime = { '/app/' + f: expected[f] for f in IMAGE_FILES }
        runtime = manifest.get('runtime_sha256', {})
        if any(runtime.get(p) != d for p, d in expected_runtime.items()) or not re.fullmatch('[0-9a-f]{64}', runtime.get('/app/hyemi_ingress.py', '')):
            raise Refused('runtime manifest omits exact build bytes or mounted ingress bytes')
        if set(runtime) != set(expected_runtime) | {'/app/hyemi_ingress.py'}:
            raise Refused('runtime manifest must not include credentials or unknown files')

    def gate(self, slot, manifest):
        self.source_gate(manifest)
        info = self.inspect_container(slot)
        if info['Image'] != manifest['image_id']:
            raise Refused('running container image ID differs from reviewed image')
        paths = list(manifest['runtime_sha256'])
        code = 'import hashlib,json,pathlib; print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in ' + repr(paths) + '}))'
        actual = json.loads(self.run('docker', 'exec', info['Id'], 'python', '-c', code))
        if actual != manifest['runtime_sha256']:
            raise Refused('running bytes differ from reviewed source/ingress bytes')

    def start(self, slot, manifest):
        env = dict(os.environ, **{'HERMES_BRIDGE_' + slot.upper() + '_IMAGE': manifest['image_id']})
        self.compose('--profile', slot, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', SERVICES[slot], env=env)

    def private(self, slot, path, method='GET'):
        # Requests originate on the Docker network using the actual service DNS.
        # No auth/header credentials, no redirects, no retry on response loss.
        code = '''import json,urllib.request,urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
r=urllib.request.Request(URL,method=METHOD,data=(b'{}' if METHOD=='POST' else None),headers={'User-Agent':UA,'Content-Type':'application/json'})
try:
 response=opener.open(r,timeout=10)
except urllib.error.HTTPError as e: response=e
print(json.dumps({'status':response.code,'body':response.read(65536).decode()}))
'''.replace('URL', repr('http://' + SERVICES[slot] + ':8652' + path)).replace('METHOD', repr(method)).replace('UA', repr(UA))
        # The target container's own Python provides a dependency-free probe.
        return json.loads(self.run('docker', 'exec', self.container(slot), 'python', '-c', code))

    def probe(self, slot):
        self.inspect_container(slot)
        if self.private(slot, '/healthz')['status'] != 200:
            raise Refused('healthz failed')
        if slot != 'legacy':
            self.in_flight(slot)  # read actual warmed JWKS readiness, not config health
        if self.private(slot, '/mcp', 'POST')['status'] != 401:
            raise Refused('missing-auth POST must return origin 401')

    def in_flight(self, slot):
        response = self.private(slot, '/readyz')
        obj = json.loads(response['body'])
        value = obj.get('in_flight')
        if response['status'] != 200 or obj.get('status') != 'ready' or type(value) is not int or value < 0:
            raise Refused('readyz must be ready with a nonnegative integer in_flight')
        return value

    def admin(self):
        return json.loads(self.run('docker', 'exec', self.caddy, 'wget', '-qO-', 'http://127.0.0.1:2019/config/'))

    def mounted(self):
        return self.run('docker', 'exec', self.caddy, 'cat', self.mounted_path)

    @contextlib.contextmanager
    def candidate(self, data, suffix):
        token = 'hermes-rollout-' + uuid.uuid4().hex + suffix
        local = self.audit / token
        local.write_bytes(data)
        local.chmod(0o600)
        remote = '/tmp/' + token
        self.run('docker', 'cp', str(local), self.caddy + ':' + remote)
        try:
            yield remote
        finally:
            # A cleanup failure must not hide a reload/rollback failure.
            try: self.run('docker', 'exec', self.caddy, 'rm', '-f', remote)
            except Exception: pass
            local.unlink(missing_ok=True)

    def adapt(self, text):
        return json.loads(self.run('docker', 'exec', '-i', self.caddy, 'caddy', 'adapt', '--config', '-', '--adapter', 'caddyfile', input=text.encode()))

    def validate(self, text):
        version = self.run('docker', 'exec', self.caddy, 'caddy', 'version').decode()
        if not version.startswith('v2.11.2 '):
            raise Refused('review required for a different Caddy version: ' + version)
        with self.candidate(text.encode(), '.Caddyfile') as path:
            self.run('docker', 'exec', self.caddy, 'caddy', 'validate', '--config', path, '--adapter', 'caddyfile')

    def reload(self, text):
        with self.candidate(text.encode(), '.Caddyfile') as path:
            self.run('docker', 'exec', self.caddy, 'caddy', 'reload', '--config', path, '--adapter', 'caddyfile')

    def restore(self, config):
        with self.candidate(canonical(config), '.json') as path:
            self.run('docker', 'exec', self.caddy, 'caddy', 'validate', '--config', path)
            self.run('docker', 'exec', self.caddy, 'caddy', 'reload', '--config', path)

    def boundary(self):
        def request(kind):
            started = time.monotonic()
            sample = {'time': time.time(), 'kind': kind, 'status': None, 'error': None}
            cmd = ['curl', '--silent', '--show-error', '--noproxy', '*', '--max-time', '10', '--connect-timeout', '3',
                   '--retry', '0', '--output', '/dev/null', '--write-out', '%{http_code}', '--user-agent', UA,
                   '--request', 'POST', '--header', 'Content-Type: application/json', '--data', '{}']
            if kind == 'direct_origin':
                cmd += ['--resolve', HOST + ':443:127.0.0.1']
            cmd += ['https://' + HOST + '/mcp']
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=12, check=False)
                sample['status'] = int(result.stdout) if result.stdout.strip().isdigit() else None
                if result.returncode: sample['error'] = result.stderr.decode(errors='replace')[:2000]
            except Exception as exc:
                sample['error'] = str(exc)
            sample['latency_ms'] = round((time.monotonic() - started) * 1000, 3)
            return sample
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(request, ('public_edge', 'direct_origin')))

    def stop(self, slot, cid):
        if self.container(slot) != cid:
            raise Refused('container changed during drain')
        self.run('docker', 'stop', '--time', '30', cid)
        info = json.loads(self.run('docker', 'inspect', cid))[0]
        if info['State']['Running']:
            raise Refused('stop did not stop exact container')


class Operator:
    def __init__(self, io, caddyfile, audit, interval=1, samples=30):
        self.io, self.file, self.audit = io, Path(caddyfile), Path(audit)
        self.interval, self.samples = interval, samples
        if not math.isfinite(interval) or interval <= 0 or samples < 2 or samples > 3600:
            raise Refused('monitor must be finite, positive, and have at least two rounds')
        self.audit.mkdir(mode=0o700, parents=True, exist_ok=True)

    def inspect(self):
        text = self.file.read_text()
        slot = active_slot(text)
        live = self.io.admin()
        if self.io.mounted() != text.encode() or live != self.io.adapt(text):
            raise Refused('host/mounted Caddyfile/admin config drift; reconcile manually')
        return text, live, slot

    def journal(self, action, **fields):
        path = self.audit / (action + '-' + uuid.uuid4().hex)
        path.mkdir(mode=0o700)
        save(path / 'state.json', {'action': action, 'time': time.time(), **fields})
        return path

    def stage(self, slot, manifest):
        text, live, active = self.inspect()
        if slot == 'legacy' or slot == active or self.io.container(slot, required=False):
            raise Refused('stage only an inactive EMPTY blue/green slot; preserve retained containers')
        self.io.source_gate(manifest)
        journal = self.journal('stage', slot=slot, before_sha256=sha(text.encode()), manifest=manifest)
        try:
            self.io.start(slot, manifest)
            self.io.gate(slot, manifest)
            self.io.probe(slot)
            after, after_live, _ = self.inspect()
            if after != text or after_live != live:
                raise Refused('proxy changed concurrently during staging')
            save(journal / 'result.json', {'status': 'staged', 'container': self.io.container(slot)})
        except Exception as exc:
            save(journal / 'result.json', {'status': 'failed', 'error': str(exc), 'policy': 'no proxy change or old-slot stop attempted; retain candidate for inspection'})
            raise
        return journal

    def verify(self, text, live):
        if self.file.read_bytes() != text.encode() or self.io.mounted() != text.encode() or self.io.admin() != live:
            raise Refused('exact host/mounted/admin readback failed')

    def restore(self, text, live):
        # Full original JSON, not an approximation of a reversed text edit.
        self.io.restore(live)
        write_in_place(self.file, text.encode())
        self.verify(text, live)

    @contextlib.contextmanager
    def monitor(self, journal):
        rounds = []
        def sample():
            try: value = self.io.boundary()
            except Exception as exc: value = {'status': None, 'error': str(exc), 'time': time.time()}
            rounds.append(value)
            save(journal / 'monitor.json', rounds)
        sample()  # synchronous before mutation, then concurrent samples across reload
        def loop():
            for _ in range(self.samples - 2):
                time.sleep(self.interval)
                sample()
        worker = threading.Thread(target=loop, daemon=True)
        worker.start()
        try: yield rounds
        finally:
            worker.join()
            sample()  # mandatory after readback / restoration, even on slow reload

    def promote(self, slot, manifest):
        before, old_live, active = self.inspect()
        if slot not in ('blue', 'green') or slot == active:
            raise Refused('promotion needs inactive blue/green slot')
        self.io.gate(slot, manifest)
        self.io.probe(slot)
        old_id, new_id = self.io.container(active), self.io.container(slot)
        after = retarget(before, slot)
        self.io.validate(after)
        new_live = self.io.adapt(after)
        journal = self.journal('promote', old_slot=active, new_slot=slot, old_container=old_id, new_container=new_id, manifest=manifest)
        (journal / 'before.Caddyfile').write_text(before)
        (journal / 'after.Caddyfile').write_text(after)
        save(journal / 'before.json', old_live)
        save(journal / 'after.json', new_live)
        save(journal / 'hashes.json', {'before_file': sha(before.encode()), 'after_file': sha(after.encode()), 'before_live': sha(canonical(old_live)), 'after_live': sha(canonical(new_live))})
        attempted_reload = False
        with self.monitor(journal) as rounds:
            try:
                baseline = rounds[0] if isinstance(rounds[0], list) else [rounds[0]]
                if not baseline or any(s.get('status') != 401 or s.get('error') for s in baseline):
                    raise Refused('boundary baseline failed; no proxy mutation attempted')
                self.verify(before, old_live)
                self.io.gate(slot, manifest)
                self.io.probe(slot)
                if self.io.container(slot) != new_id or self.io.container(active) != old_id:
                    raise Refused('container changed before promotion')
                attempted_reload = True
                self.io.reload(after)
                write_in_place(self.file, after.encode())
                self.verify(after, new_live)
                self.io.probe(slot)
                save(journal / 'result.json', {'status': 'promoted_pending_monitor', 'old_slot_retained': True})
            except Exception as exc:
                if not attempted_reload:
                    save(journal / 'result.json', {'status': 'precondition_failed_no_mutation', 'error': str(exc)})
                    raise
                try:
                    self.restore(before, old_live)
                    status = 'failed_restored'
                except Exception as rollback_error:
                    save(journal / 'result.json', {'status': 'ROLLBACK_FAILED', 'error': str(exc), 'rollback_error': str(rollback_error)})
                    raise Refused('promotion and rollback failed; inspect journal ' + str(journal)) from rollback_error
                save(journal / 'result.json', {'status': status, 'error': str(exc)})
                raise
        bad = [s for r in rounds for s in (r if isinstance(r, list) else [r]) if s.get('status') != 401 or s.get('error')]
        if bad:
            try:
                self.restore(before, old_live)
            except Exception as exc:
                save(journal / 'result.json', {'status': 'BOUNDARY_ROLLBACK_FAILED', 'failed_samples': len(bad), 'error': str(exc)})
                raise Refused('boundary failed and restore failed; inspect ' + str(journal)) from exc
            save(journal / 'result.json', {'status': 'boundary_failed_restored', 'failed_samples': len(bad)})
            raise Refused('boundary monitor failed; original config restored; see ' + str(journal))
        self.verify(after, new_live)
        save(journal / 'result.json', {'status': 'promoted', 'old_slot_retained': True, 'config_readback_verified': True,
                                       'boundary_rounds': len(rounds), 'authenticated_receipt': 'not_verified'})
        return journal

    def rollback(self, journal):
        journal = Path(journal)
        state = json.loads((journal / 'state.json').read_text())
        before = (journal / 'before.Caddyfile').read_text()
        after = (journal / 'after.Caddyfile').read_text()
        old_live = json.loads((journal / 'before.json').read_text())
        new_live = json.loads((journal / 'after.json').read_text())
        hashes = json.loads((journal / 'hashes.json').read_text())
        if hashes != {'before_file': sha(before.encode()), 'after_file': sha(after.encode()), 'before_live': sha(canonical(old_live)), 'after_live': sha(canonical(new_live))}:
            raise Refused('rollback artifacts are corrupt')
        self.verify(after, new_live)
        if self.io.container(state['old_slot']) != state['old_container'] or self.io.container(state['new_slot']) != state['new_container']:
            raise Refused('rollback container identity changed; manual review required')
        self.io.probe(state['old_slot'])
        rollback = self.journal('rollback', source=str(journal))
        with self.monitor(rollback) as rounds:
            try:
                self.restore(before, old_live)
                self.io.probe(state['old_slot'])
            except Exception as exc:
                try:
                    self.restore(after, new_live)
                except Exception as recovery:
                    save(rollback / 'result.json', {'status': 'ROLLBACK_RECOVERY_FAILED', 'error': str(exc), 'recovery_error': str(recovery)})
                    raise Refused('rollback and recovery failed; inspect ' + str(rollback)) from recovery
                save(rollback / 'result.json', {'status': 'rollback_failed_new_target_restored', 'error': str(exc)})
                raise
        bad = [s for r in rounds for s in (r if isinstance(r, list) else [r]) if s.get('status') != 401 or s.get('error')]
        save(rollback / 'result.json', {'status': 'rolled_back' if not bad else 'rolled_back_boundary_degraded', 'failed_samples': len(bad), 'both_containers_retained': True})
        if bad:
            raise Refused('original config restored but boundary checks failed; inspect ' + str(rollback))
        return rollback

    def drain(self, slot, timeout=900, grace=300, stop=False):
        if not math.isfinite(timeout) or not math.isfinite(grace) or timeout <= 0 or grace <= 0 or grace > timeout:
            raise Refused('require 0 < grace <= timeout')
        _, _, active = self.inspect()
        if slot == active:
            raise Refused('never drain active slot')
        cid = self.io.container(slot)
        journal = self.journal('drain', slot=slot, container=cid, timeout=timeout, grace=grace, stop=stop)
        if slot == 'legacy':
            save(journal / 'result.json', {'status': 'retained', 'reason': 'legacy lacks in_flight; no precise drain can be proved; manual grace/rollback retention required'})
            return False
        deadline, zero_since = time.monotonic() + timeout, None
        samples = []
        while time.monotonic() < deadline:
            _, _, current = self.inspect()
            if current == slot or self.io.container(slot) != cid:
                raise Refused('active target/container changed during drain')
            try: count = self.io.in_flight(slot)
            except Exception as exc:
                count = None
                samples.append({'time': time.time(), 'error': str(exc)})
            samples.append({'time': time.time(), 'in_flight': count})
            save(journal / 'samples.json', samples)
            if count == 0:
                if zero_since is None: zero_since = time.monotonic()
                if time.monotonic() - zero_since >= grace:
                    if stop:
                        _, _, current = self.inspect()
                        if current == slot or self.io.in_flight(slot) != 0:
                            raise Refused('final drain check failed')
                        self.io.stop(slot, cid)
                    save(journal / 'result.json', {'status': 'stopped' if stop else 'drained_retained'})
                    return True
            else: zero_since = None
            time.sleep(self.interval)
        save(journal / 'result.json', {'status': 'timeout_retained'})
        return False


def load_manifest(path, digest):
    data = Path(path).read_bytes()
    if not re.fullmatch('[0-9a-f]{64}', digest) or sha(data) != digest:
        raise Refused('reviewed manifest digest mismatch')
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caddy', required=True, help='exact Caddy container name or ID')
    parser.add_argument('--caddyfile', default='/opt/bridge-edge/Caddyfile')
    parser.add_argument('--mounted-path', default='/etc/caddy/Caddyfile')
    parser.add_argument('--audit-dir', default=str(ROOT / 'planning/rollout-audit'))
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--execute', action='store_true', help='explicitly authorize stage/promote/rollback/stop')
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('inspect')
    for action in ('stage', 'promote'):
        sub = subs.add_parser(action)
        sub.add_argument('slot', choices=['blue', 'green'])
        sub.add_argument('--manifest', required=True)
        sub.add_argument('--reviewed-sha256', required=True)
    sub = subs.add_parser('rollback')
    sub.add_argument('journal')
    sub = subs.add_parser('drain')
    sub.add_argument('slot', choices=list(SERVICES))
    sub.add_argument('--timeout', type=float, default=900)
    sub.add_argument('--grace', type=float, default=300)
    sub.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    if (args.command in ('stage', 'promote', 'rollback') or getattr(args, 'stop', False)) and not args.execute:
        parser.error('mutation requires --execute; inspect or non-stopping drain is safe')
    audit = Path(args.audit_dir)
    audit.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(audit, 0o700)
    # One lock independent of audit-dir prevents competing instances of this tool.
    with open(ROOT / 'planning/.bluegreen-rollout.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        io = DockerIO(args.caddy, args.mounted_path, audit)
        op = Operator(io, args.caddyfile, audit, args.interval, args.samples)
        if args.command == 'inspect':
            text, live, slot = op.inspect()
            result = {'active_slot': slot, 'container': io.container(slot), 'file_sha256': sha(text.encode()), 'live_sha256': sha(canonical(live))}
        elif args.command in ('stage', 'promote'):
            manifest = load_manifest(args.manifest, args.reviewed_sha256)
            result = {'journal': str(getattr(op, args.command)(args.slot, manifest))}
        elif args.command == 'rollback':
            result = {'journal': str(op.rollback(args.journal))}
        else:
            result = {'drained': op.drain(args.slot, args.timeout, args.grace, args.stop)}
        print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
