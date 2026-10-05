#!/usr/bin/env python3
"""Separately reviewed LIVE operator canary. One POST; recovery is GET-only.

Not a client OAuth receipt or a human UI receipt. No configuration is changed.
Requires explicit --execute and one target. Never choose a new receipt to retry
an uncertain submission; use --reconcile with the original receipt instead.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Same fixed legacy dependency used by the host executor and existing tests.
# Project modules above remain staging-local, never imported from production.
import bridge
import hyemi_delivery as delivery
from hyemi_contracts import canonical_uuid, normalize_request, DISCORD_BOTS, DISCORD_GUILD
from hyemi_formatting import render_discord, render_report

OPERATOR_NAMESPACE = 'hermes-bridge-mcp:LOCAL-OPERATOR:dual-platform-canary:v1'
OPERATOR_OWNER = hashlib.sha256(OPERATOR_NAMESPACE.encode()).hexdigest()
TARGETS = {
    'Blog': ('hyeji', '100000000000000006'),
    'SecondBrain': ('hyeji', '100000000000000007'),
    'Parenting': ('hyerin', '100000000000000008'),
    'GeneralJobs': ('hyerin', '100000000000000009'),
    'DeveloperJobs': ('hyerin', '100000000000000010'),
    'Hotdeals': ('hyerin', '100000000000000011'),
    'HyejiTelegramGeneral': ('hyeji', None),
}
# Deliberately crosses a Discord chunk boundary; preserves whitespace/Markdown.
CONTENTS = ('**공백 보존 수정 전송 확인 v2**\nHyemi 작성 · 전용 Hyemi 봇 전달 테스트\n'
            '다른 봇 호출 방지: <@100000000000000004> <@100000000000000005> <@&123>\n'
            '`inline code` [공식 문서](https://discord.com/developers/docs/intro)\n'
            + '유니코드 확인 😀 · ' * 180 + '\n원문 보존 확인.  \n')


def load_config():
    # Fixed operator-only configuration; never accepts a client owner override.
    return json.loads(delivery._secure_read(Path('/root/.config/hyemi-ingress/config.json')))


def serialize_receipt(record):
    return json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)


def save_receipt(out, record, initial=False):
    """Exclusive reservation, then same-directory atomic durable replacement."""
    payload = serialize_receipt(record)
    fd = None
    temp = None
    try:
        if initial:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        else:
            fd, temp = tempfile.mkstemp(prefix='.'+out.name+'.', suffix='.tmp', dir=out.parent)
        stream = os.fdopen(fd, 'w', encoding='utf-8')
        fd = None
        with stream:
            if stream.write(payload) != len(payload):
                raise OSError('incomplete receipt write')
            stream.flush()
            os.fsync(stream.fileno())
        if not initial:
            os.replace(temp, out)
            temp = None
        fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temp is not None:
            os.unlink(temp)


def verify_discord(request, rows):
    token = delivery.DiscordTokens().get('hyemi')
    headers = {'Authorization': 'Bot '+token, 'User-Agent': 'HermesBridgeMCP operator verification'}
    base = 'https://discord.com/api/v10/channels/'+request['channelid']
    code, channel = delivery.request_json(Request(base, headers=headers))
    if code != 200 or channel.get('id') != request['channelid'] or channel.get('guild_id') != DISCORD_GUILD:
        raise ValueError('Discord channel readback mismatch')
    for row in rows:
        delivery_id = row['message_id']
        from hyemi_contracts import snowflake
        snowflake(delivery_id)
        code, message = delivery.request_json(Request(base+'/messages/'+delivery_id, headers=headers))
        if (code != 200 or message.get('id') != delivery_id or message.get('content') != row['text']
                or message.get('channel_id') != request['channelid']
                or type(message.get('author')) is not dict
                or message['author'].get('id') != DISCORD_BOTS[request['to']]
                or message['author'].get('bot') is not True
                or message.get('guild_id', DISCORD_GUILD) != DISCORD_GUILD):
            raise ValueError('Discord message readback mismatch')


def verify(cfg, db, body, record):
    eid, wid = body['event_id'], body['work_id']
    def get(path):
        return bridge.v2_request({'_ingress': cfg}, OPERATOR_OWNER, 'GET', path)
    deadline = time.monotonic() + 60
    while True:
        result = get('/v2/events/'+eid+'/result')
        if result.get('event_id') != eid or result.get('work_id') != wid:
            raise ValueError('exact result unavailable; never resend')
        entries = result.get('delivery', [])
        if entries and all(d['state'] == 'delivered' and d.get('confirmed_destination') is True for d in entries):
            break
        if (any(d['state'] in {'unknown', 'failed', 'blocked_configuration'} for d in entries)
                or time.monotonic() >= deadline):
            raise ValueError('delivery unverified; never resend')
        time.sleep(.5)
    raw = get('/v2/events/'+eid+'/report')
    work = get('/v2/works/'+wid)
    if (raw != body or result.get('contents') != body['contents'] or result.get('execution_state') != 'finished'
            or work.get('work_id') != wid or work.get('latest_event_id') != eid
            or work.get('to') != body['to'] or work.get('kind') != 'report'
            or work.get('execution_state') != 'finished'):
        raise ValueError('exact report/work readback mismatch')
    event = db.execute('SELECT * FROM v2_events WHERE event_id=?', (eid,)).fetchone()
    if (event is None or event['owner'] != OPERATOR_OWNER or event['work_id'] != wid
            or event['kind'] != 'report' or event['execution_state'] != 'finished'
            or json.loads(event['request']) != body or event['result'] is not None
            or any(json.loads(event['metadata']).get(k) is not None for k in ('run_id', 'payload', 'attempted_at'))):
        raise ValueError('exact no-agent ledger proof failed')
    ledger_work = db.execute('SELECT * FROM v2_works WHERE owner=? AND work_id=?', (OPERATOR_OWNER, wid)).fetchone()
    if (ledger_work is None or ledger_work['latest_event_id'] != eid
            or ledger_work['recipient'] != body['to'] or ledger_work['role'] != 'report'):
        raise ValueError('exact work ledger proof failed')
    rows = db.execute('SELECT * FROM v2_outbox WHERE event_id=? ORDER BY id', (eid,)).fetchall()
    discord = body.get('platform') == 'discord'
    plans = (render_discord if discord else render_report)(body['contents'])
    if len(rows) != len(entries) or len(rows) != len(plans):
        raise ValueError('chunk count mismatch')
    for index, (row, entry, plan) in enumerate(zip(rows, entries, plans)):
        proof = json.loads(row['destination']) if row['destination'] else None
        if (row['phase'] != 'report:'+str(index) or row['sender'] != 'hyemi'
                or row['state'] != 'delivered' or json.loads(row['rendering']) != plan
                or row['text'] != plan['text'] or row['id'] != entry['id']
                or entry['phase'] != row['phase'] or entry['message_id'] != row['message_id']
                or entry.get('verified_destination') != proof):
            raise ValueError('exact delivery ledger mismatch')
        if discord:
            if delivery.verified_discord_destination(body, proof, 'hyemi') is None:
                raise ValueError('Discord proof mismatch')
        else:
            chat, _, topic = body['channelid'].partition(':')
            expected = delivery.verified_destination(body['channelid'], int(chat),
                                                     proof.get('message_thread_id') if isinstance(proof, dict) else None)
            if proof != expected or expected is None:
                raise ValueError('Telegram General proof mismatch')
    if discord:
        verify_discord(body, rows)
    record.update(ok=True, raw_report_exact=True, no_agent_run=True,
                  verified_chunks=len(rows), discord_get_verified=discord,
                  delivery=entries, reconciled=not record['submitted'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--reconcile', action='store_true', help='GET-only recovery of original receipt')
    parser.add_argument('--target', choices=TARGETS, required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error('operator authorization requires --execute')
    record = None
    db = None
    reserved = False
    evidence_failed = False
    try:
        out = ROOT / args.output
        if out.is_symlink() or not out.resolve().is_relative_to((ROOT/'planning').resolve()):
            raise ValueError('receipt must be inside planning')
        out = out.resolve()
        if not args.reconcile and out.exists():
            raise ValueError('receipt already exists; use GET-only reconcile')
        cfg = load_config()
        db = sqlite3.connect(Path(cfg['state_path']).resolve().as_uri()+'?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        recipient, channel = TARGETS[args.target]
        discord = channel is not None
        if not discord and cfg['allowed']['hyeji']['home'] not in ('-1000000002', '-1000000002:1'):
            raise ValueError('Telegram General policy mismatch')
        if args.reconcile:
            record = json.loads(delivery._secure_read(out))
            if (record.get('operator_owner') != OPERATOR_OWNER or record.get('operator_namespace') != OPERATOR_NAMESPACE
                    or record.get('target') != args.target):
                raise ValueError('receipt identity mismatch')
            eid, wid = canonical_uuid(record['event_id']), canonical_uuid(record['work_id'])
        else:
            eid, wid = str(uuid.uuid4()), str(uuid.uuid4())
            if (db.execute('SELECT 1 FROM v2_events WHERE event_id=? OR work_id=?', (eid,wid)).fetchone()
                    or db.execute('SELECT 1 FROM v2_works WHERE work_id=?', (wid,)).fetchone()):
                raise ValueError('fresh IDs required')
        body = dict(version=2, kind='report', event_id=eid, work_id=wid, to=recipient,
                    channelid=channel if discord else cfg['allowed'][recipient]['home'], contents=CONTENTS)
        if discord:
            body['platform'] = 'discord'
        body = normalize_request(body, cfg['allowed'])
        if args.reconcile:
            if record.get('request_sha256') != hashlib.sha256(serialize_receipt(body).encode()).hexdigest():
                raise ValueError('receipt request mismatch')
        else:
            record = dict(event_id=eid, work_id=wid, operator_owner=OPERATOR_OWNER,
                          operator_namespace=OPERATOR_NAMESPACE, target=args.target,
                          proof='PRIVATE OPERATOR only; not client OAuth or human UI proof',
                          authenticated_client_receipt=False, human_ui_verified=False,
                          channelid_omitted=not discord, submitted=False, reconciled=False,
                          request_sha256=hashlib.sha256(serialize_receipt(body).encode()).hexdigest(),
                          retry=False, ok=False)
            save_receipt(out, record, initial=True)
        reserved = True
        if not args.reconcile:
            try:
                fields = {'platform': 'discord'} if discord else {}
                # Telegram exercises the actual public submission adapter's omitted
                # channel/platform defaults. This is still private operator auth.
                accepted = bridge.submit_v2({'_ingress':cfg}, OPERATOR_OWNER, 'report', eid, wid,
                                            CONTENTS, to=recipient, **({'channelid':channel} if discord else {}), **fields)
                record['submitted'] = (accepted.get('status') == 'accepted'
                                       and accepted.get('event_id') == eid and accepted.get('work_id') == wid)
            except Exception as exc:
                record['submission_error_class'] = type(exc).__name__
            # Reconcile the original IDs even if POST committed without a response.
        verify(cfg, db, body, record)
        try:
            save_receipt(out, record)
        except Exception:
            evidence_failed = True
            raise
        print(json.dumps({'ok':True, 'event_id':eid, 'work_id':wid, 'verified_chunks':record['verified_chunks'],
                          'reconciled':record['reconciled'], 'retry':False}))
        return 0
    except Exception as exc:
        if reserved and record is not None and not evidence_failed:
            record.update(ok=False, error_class=type(exc).__name__)
            try:
                save_receipt(out, record)
            except Exception:
                evidence_failed = True
        print(json.dumps(dict(ok=False, error_class=type(exc).__name__, evidence_failed=evidence_failed,
                              event_id=record.get('event_id') if record else None,
                              work_id=record.get('work_id') if record else None, retry=False)))
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
