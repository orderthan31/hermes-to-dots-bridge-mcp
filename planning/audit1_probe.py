#!/usr/bin/env python3
"""Opt-in real installed-Caddy privacy test, using ONLY an isolated clone.
No host ports, production mounts, DNS, external requests, reload, or live writes.
Run from project root: python -m planning.audit1_probe
"""
import gzip
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from planning import bluegreen_rollout as bg

KEYS = {'ts', 'bytes_read', 'duration', 'size', 'status', 'endpoint'}
SENTINELS = ['AUDIT1_QUERY_SECRET', 'AUDIT1_BODY_SECRET', 'AUDIT1_COOKIE_SECRET',
             'AUDIT1_BEARER_SECRET', 'AUDIT1_ASSERTION_SECRET', 'audit1-person@example.invalid',
             'AUDIT1_URI_SECRET', 'AUDIT1_AGENT_SECRET', 'AUDIT1_CORRELATION_SECRET',
             'AUDIT1_RESPONSE_SECRET', 'AUDIT1_METHOD_SECRET', 'AUDIT1_HOST_SECRET', 'AUDIT1_TLS_SECRET']


def run(*args, input=None, timeout=60):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[:4]}: {result.stderr.decode(errors="replace")}')
    return result.stdout


def adapt(text):
    # Read-only use of installed production binary; no validation/provisioning here.
    return json.loads(run('docker', 'exec', '-i', 'caddy', 'caddy', 'adapt',
                          '--config', '-', '--adapter', 'caddyfile', input=text.encode()))


def assert_rows(raw, count=None, *, error=False):
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if count is not None:
        assert len(rows) == count, (len(rows), count)
    for token in SENTINELS + ['127.0.0.1', bg.HOST, '/mcp', 'Authorization', 'REDACTED']:
        assert token.lower() not in raw.lower(), token
    for row in rows:
        keys = {'ts', 'duration', 'status'} if error else KEYS
        assert set(row) == keys, row
        if not error:
            assert row['endpoint'] == 'mcp', row
        for key in keys - {'endpoint'}:
            assert type(row[key]) in (int, float) and row[key] >= 0, row
    return rows


def probe(rotation=None):
    if rotation not in (None, 'count', 'age'):
        raise ValueError('unknown rotation fixture')
    info = json.loads(run('docker', 'inspect', 'caddy'))[0]
    version = run('docker', 'exec', 'caddy', 'caddy', 'version').decode().strip()
    assert version.startswith('v2.11.2 '), version
    assert any(m['Destination'] == '/data' and m['Type'] == 'volume' and m['RW'] for m in info['Mounts'])
    image = info['Image']
    candidate = bg.managed_stanza('blue', audit=True)
    config = adapt(candidate)
    logger = config['logging']['logs']['mcp_audit1']
    assert logger['writer'] == {'filename': bg.AUDIT_PATH, 'mode': '0600', 'output': 'file',
                                'roll_keep': 3, 'roll_keep_days': 7, 'roll_size_mb': 5}
    assert logger['include'] == ['http.log.access.mcp_audit1']
    assert 'http.log.access.mcp_audit1' in config['logging']['logs']['default']['exclude']
    error_logger = config['logging']['logs']['mcp_error_audit1']
    assert error_logger['include'] == ['http.log.error.mcp_audit1']
    assert error_logger['writer'] == dict(logger['writer'], filename=bg.AUDIT_ERROR_PATH)
    assert set(config['logging']['logs']['default']['exclude']) == {
        'http.log.access', 'http.log.access.mcp_audit1', 'http.log.error.mcp_audit1'}
    # The logger/prefix is byte-identical. Only isolated listener/upstream differ.
    isolated = (candidate.replace('{\n', '{\n    admin off\n    auto_https off\n', 1).replace(bg.HOST + ' {',
                'http://' + bg.HOST + ':18080 {').replace(
                'hermes-bridge-mcp-blue:8652', '127.0.0.1:18081').replace(
                '    handle /mcp {\n', '    handle /mcp {\n'
                '        @unavailable header X-Probe-Status 599\n        error @unavailable 503\n') + '''
:18081 {
    @health path /readyz
    respond @health 200
    header Set-Cookie "AUDIT1_RESPONSE_SECRET"
    header X-Private "audit1-person@example.invalid"
    @abort header X-Probe-Status 502
    abort @abort
    @ok header X-Probe-Status 200
    respond @ok "AUDIT1_RESPONSE_SECRET" 200
    @error header X-Probe-Status 503
    respond @error "AUDIT1_RESPONSE_SECRET" 503
    respond "AUDIT1_RESPONSE_SECRET" 401
}
http://unrelated.invalid:18080 {
    reverse_proxy 127.0.0.1:19999
}
''')
    isolated += '\n' + candidate.removeprefix(bg.AUDIT_GLOBAL_PREFIX).replace(bg.HOST + ' {',
        'https://' + bg.HOST + ':18443 {\n    tls /audit1/cert.pem /audit1/key.pem').replace(
        'hermes-bridge-mcp-blue:8652', '127.0.0.1:18081').replace(
        '    handle /mcp {\n', '    handle /mcp {\n'
        '        @unavailable header X-Probe-Status 599\n        error @unavailable 503\n')
    # HTTP and TLS share the exact production logger names/policy. Only the
    # clone has the explicit 503-error fixture and unrelated error-control site.
    scratch = Path(os.environ.get('TMPDIR', '/tmp'))
    scratch.mkdir(parents=True, exist_ok=True)
    name = 'audit1-probe-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='audit1-', dir=scratch) as directory:
        folder = Path(directory)
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30',
            '-subj', '/CN=AUDIT1_TLS_SECRET.invalid', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:' + bg.HOST,
            '-keyout', str(folder / 'key.pem'),
            '-out', str(folder / 'cert.pem'))
        (folder / 'Caddyfile').write_text(isolated)
        data_dir = folder / 'data'
        data_dir.mkdir(mode=0o700)
        if rotation:
            for path in (bg.AUDIT_PATH, bg.AUDIT_ERROR_PATH):
                # Pre-existing seed fixtures, not emitted records.
                row = {'ts': 0, 'duration': 0, 'status': 502}
                if path == bg.AUDIT_PATH:
                    row.update(bytes_read=0, size=0, endpoint='mcp')
                fixture = json.dumps(row) + '\n'
                limit = 5 * 1024 * 1024
                seed = (fixture * (limit // len(fixture))).encode()
                seed += b' ' * (limit - len(seed))
                current = data_dir / Path(path).name
                current.write_bytes(seed)
                current.chmod(0o644)  # verify repair too
                offsets = [1, 2, 3, 4, 5] if rotation == 'count' else [1, 8 * 24]
                for hours in offsets:
                    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H-%M-%S.000')
                    archive = data_dir / (current.stem + '-' + stamp + '-size.json.gz')
                    archive.write_bytes(gzip.compress(fixture.encode()))
                    archive.chmod(0o600)
        started = False
        try:
            run('docker', 'run', '-d', '--name', name, '--network', 'none', '--read-only',
                '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE', '--security-opt', 'no-new-privileges',
                '--mount', f'type=bind,src={data_dir},dst=/data',
                '--tmpfs', '/config:rw,noexec,nosuid,mode=0700',
                '--mount', f'type=bind,src={folder},dst=/audit1,readonly',
                '--entrypoint', 'caddy', image, 'run', '--config', '/audit1/Caddyfile', '--adapter', 'caddyfile')
            started = True
            clone = json.loads(run('docker', 'inspect', name))[0]
            assert clone['HostConfig']['NetworkMode'] == 'none'
            assert not clone['HostConfig']['PortBindings']
            assert all(m.get('Source', '').startswith(str(folder)) for m in clone['Mounts'])
            run('docker', 'exec', name, 'caddy', 'validate', '--config', '/audit1/Caddyfile', '--adapter', 'caddyfile')

            transmitted = []
            def request(path='/mcp', method='POST', status=401, host=bg.HOST, tls=False, unrelated=False):
                body = ('AUDIT1_BODY_SECRET client_assertion=AUDIT1_ASSERTION_SECRET '
                        'email=audit1-person@example.invalid')
                data = (f'{method} {path}?token=AUDIT1_QUERY_SECRET HTTP/1.1\r\nHost: {host}\r\n'
                        'Connection: close\r\nAuthorization: Bearer AUDIT1_BEARER_SECRET\r\n'
                        'Cookie: session=AUDIT1_COOKIE_SECRET\r\nUser-Agent: AUDIT1_AGENT_SECRET\r\n'
                        'X-Correlation-ID: AUDIT1_CORRELATION_SECRET\r\n'
                        'Cf-Access-Jwt-Assertion: AUDIT1_ASSERTION_SECRET\r\n'
                        'X-User-Email: audit1-person@example.invalid\r\n'
                        f'X-Probe-Status: {status}\r\nContent-Length: {len(body)}\r\n\r\n{body}')
                # Keep the write side open until the response: BusyBox nc's
                # stdin-EOF half-close can cancel Go's request context early.
                if unrelated:
                    data = ('GET /UNRELATED_CONTROL?fixture=UNRELATED_QUERY HTTP/1.1\r\n'
                            'Host: unrelated.invalid\r\nConnection: close\r\n\r\n')
                else:
                    assert 'Authorization: Bearer AUDIT1_BEARER_SECRET\r\n' in data
                    transmitted.append(data)
                client = ('import socket,ssl,sys; '
                          f's=socket.create_connection(("127.0.0.1",{18443 if tls else 18080}),5); '
                          + ('s=ssl._create_unverified_context().wrap_socket(s,server_hostname="' + bg.HOST + '"); ' if tls else '') +
                          's.sendall(sys.stdin.buffer.read()); '
                          '\nwhile True:\n b=s.recv(65536)\n if not b: break\n sys.stdout.buffer.write(b)\n')
                return run('nsenter', '--target', str(clone['State']['Pid']), '--net',
                           sys.executable, '-c', client, input=data.encode())

            # Bounded startup wait; requests stay in clone's loopback namespace.
            for attempt in range(30):
                try:
                    response = request('/readyz')
                    if response.startswith(b'HTTP/1.1 404'): break
                except RuntimeError:
                    pass
                time.sleep(.1)
            else: raise AssertionError('isolated server not ready')
            # Health checker may run before the mock listener binds; allow its
            # configured 10s interval without sending any extra /mcp request.
            time.sleep(10.2)
            expected = [401, 200, 503, 401, 502, 503, 200, 502, 503]
            fixtures = [401, 200, 503, 401, 502, 599, 200, 502, 599]
            methods = ['POST', 'GET', 'DELETE', 'AUDIT1_METHOD_SECRET', 'POST', 'POST', 'POST', 'POST', 'POST']
            for i, (status, fixture, method) in enumerate(zip(expected, fixtures, methods)):
                response = request(status=fixture, method=method, tls=i >= 6)
                assert response.startswith(f'HTTP/1.1 {status}'.encode()), (rotation, method, status, response)
                if status in (200, 401) or fixture == 503:
                    assert b'AUDIT1_RESPONSE_SECRET' in response
            for path in ['/readyz', '/healthz', '/mcp/', '/mcp/AUDIT1_URI_SECRET', '/AUDIT1_URI_SECRET']:
                assert request(path).startswith(b'HTTP/1.1 404')
            request(host='AUDIT1_HOST_SECRET.invalid')
            def runtime():
                result = subprocess.run(['docker', 'logs', name], capture_output=True, check=True, timeout=10)
                return result.stdout.decode(), result.stderr.decode()

            before_control = runtime()
            for stream in before_control:
                for token in SENTINELS:
                    assert token.lower() not in stream.lower(), ('runtime leak', token)
                assert 'http.log.error.mcp_audit1' not in stream
                assert '"request":' not in stream
            assert request(unrelated=True).startswith(b'HTTP/1.1 502')
            run('docker', 'stop', '--time', '5', name)
            after_control = runtime()
            delta = []
            for before, after in zip(before_control, after_control):
                assert after.startswith(before)
                for token in SENTINELS:
                    assert token.lower() not in after.lower(), ('runtime leak', token)
                delta.extend(json.loads(line) for line in after[len(before):].splitlines() if line.startswith('{'))
            other_errors = [r for r in delta if r.get('logger') == 'http.log.error']
            assert len(other_errors) == 1, delta
            assert other_errors[0]['status'] == 502
            assert other_errors[0]['request']['uri'] == '/UNRELATED_CONTROL?fixture=UNRELATED_QUERY'
            # Raw unrelated fixture is intentionally outside the MCP request-object
            # scan; MCP sentinel scanning still covers both complete streams.
            assert not any(r.get('logger', '').startswith('http.log.access') for r in delta)
            raw = (data_dir / Path(bg.AUDIT_PATH).name).read_text()
            rows = assert_rows(raw, len(expected))
            assert [r['status'] for r in rows] == expected, (rotation, rows, expected)
            error_raw = (data_dir / Path(bg.AUDIT_ERROR_PATH).name).read_text()
            errors = assert_rows(error_raw, 4, error=True)
            assert [r['status'] for r in errors] == [502, 503, 502, 503]
            archive_sets = {}
            expected_archives = 0 if not rotation else 3 if rotation == 'count' else 2
            for path in (bg.AUDIT_PATH, bg.AUDIT_ERROR_PATH):
                stem = Path(path).stem
                for _ in range(50):
                    archives = sorted(data_dir.glob(stem + '-*.gz'))
                    if len(archives) == expected_archives:
                        break
                    time.sleep(.1)
                assert len(archives) == expected_archives, [p.name for p in data_dir.iterdir()]
                if rotation:
                    assert max(len(gzip.decompress(p.read_bytes())) for p in archives) == 5 * 1024 * 1024
                archive_sets[stem] = [p.name for p in archives]
            outputs = []
            for p in sorted(data_dir.rglob('*')):
                if not p.is_file():
                    continue
                content = gzip.decompress(p.read_bytes()) if p.suffix == '.gz' else p.read_bytes()
                for token in SENTINELS:
                    assert token.lower().encode() not in content.lower(), (p.name, token)
                if p.parent != data_dir:
                    continue  # Caddy scratch-only storage metadata is not an audit stream
                assert p.name.endswith('.json') or p.name.endswith('.json.gz'), p.name
                assert_rows(content.decode(), error=p.name.startswith('mcp-error-audit1'))
                assert p.stat().st_mode & 0o777 == 0o600
                outputs.append({'file': p.name, 'mode': '0600', 'bytes': p.stat().st_size,
                                'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
            sent = '\n'.join(transmitted)
            for token in set(SENTINELS) - {'AUDIT1_RESPONSE_SECRET', 'AUDIT1_TLS_SECRET'}:
                assert token in sent, 'fixture never transmitted: ' + token
            return {'rotation_fixture': rotation, 'archive_count': expected_archives,
                    'archive_sets': archive_sets, 'outputs': outputs,
                    'version': version, 'image': image, 'config_sha256': bg.sha(candidate.encode()),
                    'isolated_config_sha256': bg.sha(isolated.encode()), 'writer': logger['writer'],
                    'error_writer': error_logger['writer'], 'error_rows': errors,
                    'mode': '600', 'rows': rows, 'output_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                    'runtime': [{'stream': label, 'bytes': len(value.encode()), 'sha256': bg.sha(value.encode())}
                                for label, value in zip(('stdout', 'stderr'), after_control)],
                    'unrelated_error_preserved': True,
                    'transmitted_authorization': 'Bearer AUDIT1_BEARER_SECRET',
                    'sentinel_scan': 'stdout + stderr + every current/rotated audit file',
                    'isolation': 'network=none; no ports; scratch-only mounts; tmpfs config',
                    'sentinels_absent': True, 'non_mcp_excluded': True}
        except Exception:
            if started:
                logs = subprocess.run(['docker', 'logs', name], capture_output=True, timeout=10)
                print((logs.stdout + logs.stderr).decode(errors='replace'))
            raise
        finally:
            if started:
                run('docker', 'rm', '-f', name)


if __name__ == '__main__':
    print(json.dumps(probe(), indent=2, sort_keys=True))
