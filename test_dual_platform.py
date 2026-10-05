"""Offline dual-platform contract/regression tests; no live credentials."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from hyemi_contracts import ContractError, normalize_request
from hyemi_store import WorkflowStore, NotFound

GUILD='100000000000000003'
BOT='100000000000000012'
CHANNEL='100000000000000006'
OTHER='100000000000000010'
ALLOWED={'hyeji':{'home':'-1000000002:1','channels':['-1000000002:5'],
    'discord':{'guild_id':GUILD,'sender_bot_id':BOT,'channels':[CHANNEL,'100000000000000007']}},
    'hyerin':{'home':'-200','channels':[], 'discord':{'guild_id':GUILD,
    'sender_bot_id':BOT,'channels':[OTHER]}}}

def request(platform='discord',kind='report',**kw):
    value=dict(version=2,kind=kind,event_id=str(uuid4()),work_id=str(uuid4()),to='hyeji',
               channelid=CHANNEL if platform=='discord' else ALLOWED['hyeji']['home'],contents=' 보고 **원문** 😀 <@100000000000000005>\n')
    value['platform']=platform
    if kind=='task':value['acceptance_criteria']='확인'
    if kind=='review':value.update(revision=1,criteria='검토')
    value.update(kw);return value

class DedicatedCredentials(unittest.TestCase):
    def test_absent_wrong_key_malformed_duplicate_no_environment_fallback(self):
        import os
        from hyemi_delivery import DiscordTokens, SenderUnavailable
        valid='HYEMI_DISCORD_TOKEN=offline-fixture-token-not-real'
        cases=['', 'DISCORD_BOT_TOKEN=offline-fixture-token-not-real',
               'HYEMI_DISCORD_BOT_TOKEN=offline-fixture-token-not-real',
               'HYEMI_DISCORD_TOKEN=short', 'HYEMI_DISCORD_TOKEN="unclosed',
               'HYEMI_DISCORD_TOKEN=two words',valid+'\n'+valid,
               valid+'\nHYEMI_DISCORD_TOKEN=',valid+'\nHYEMI_DISCORD_TOKEN=two words']
        with patch.dict(os.environ,{'HYEMI_DISCORD_TOKEN':'environment-must-not-be-used',
                                    'DISCORD_BOT_TOKEN':'shared-must-not-be-used'}):
            for value in cases:
                with self.subTest(value=value),patch('hyemi_delivery._secure_read',return_value=value) as read:
                    with self.assertRaises(SenderUnavailable):DiscordTokens().get('hyemi')
                    read.assert_called_once_with(Path('/root/.hermes/.env'), require_root_0600=True)

    def test_secure_synthetic_files_only(self):
        import hyemi_delivery as d
        secure=d._secure_read
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fixture';path.write_text('HYEMI_DISCORD_TOKEN=offline-fixture-token-not-real')
            path.chmod(0o600)
            def read(fixed, **policy):
                self.assertEqual(fixed,Path('/root/.hermes/.env'))
                self.assertEqual(policy, {'require_root_0600': True})
                return secure(path, **policy)
            with patch.object(d,'_secure_read',side_effect=read):
                self.assertEqual(d.DiscordTokens().get('hyemi'),'offline-fixture-token-not-real')
                path.chmod(0o644)
                with self.assertRaises(d.SenderUnavailable):d.DiscordTokens().get('hyemi')
                path.chmod(0o600)
                target=Path(tmp)/'target';path.rename(target);path.symlink_to(target)
                with self.assertRaises(d.SenderUnavailable):d.DiscordTokens().get('hyemi')
                path.unlink()
                with self.assertRaises(d.SenderUnavailable):d.DiscordTokens().get('hyemi')

    def test_both_recipients_policy_and_actual_bot(self):
        from hyemi_delivery import DiscordTransport
        from hyemi_formatting import render_discord
        from unittest.mock import Mock
        for recipient,channel,old_bot in [('hyeji',CHANNEL,'100000000000000004'),
                                          ('hyerin',OTHER,'100000000000000005')]:
            with self.subTest(recipient=recipient):
                tokens=SimpleNamespace(get=Mock(return_value='offline-fixture-token-not-real'))
                transport=DiscordTransport(tokens,ALLOWED)
                plan=render_discord('hello')[0]
                with patch('hyemi_delivery.request_json',return_value=(200,{'id':old_bot,'bot':True})) as http:
                    result=transport.send('hyemi',channel,'hello',plan,recipient=recipient,logical_author='hyemi')
                    self.assertEqual(result.state,'blocked_configuration')
                    self.assertEqual(http.call_count,1);self.assertEqual(http.call_args.args[0].get_method(),'GET')
                    tokens.get.assert_called_once_with('hyemi')
                policy=copy.deepcopy(ALLOWED);policy[recipient]['discord']['sender_bot_id']=old_bot
                with self.assertRaises(ContractError):normalize_request(request(to=recipient,channelid=channel),policy)
                transport.allowed=policy
                with patch('hyemi_delivery.request_json') as http:
                    self.assertEqual(transport.send('hyemi',channel,'hello',plan,recipient=recipient,logical_author='hyemi').state,'failed')
                    http.assert_not_called()

    def test_both_recipients_dedicated_post_author_and_proof(self):
        from hyemi_delivery import DiscordTransport
        from hyemi_formatting import render_discord
        from unittest.mock import Mock
        for recipient,channel in [('hyeji',CHANNEL),('hyerin',OTHER)]:
            for author in (BOT,'100000000000000004','100000000000000005'):
                with self.subTest(recipient=recipient,author=author):
                    tokens=SimpleNamespace(get=Mock(return_value='offline-fixture-token-not-real'))
                    transport=DiscordTransport(tokens,ALLOWED)
                    replies=[(200,{'id':BOT,'bot':True}),(200,{'id':channel,'guild_id':GUILD}),
                             (200,{'id':'123','channel_id':channel,'content':'hello','author':{'id':author,'bot':True}})]
                    with patch('hyemi_delivery.request_json',side_effect=replies):
                        result=transport.send('hyemi',channel,'hello',render_discord('hello')[0],recipient=recipient,logical_author=recipient)
                    tokens.get.assert_called_once_with('hyemi')
                    self.assertEqual(result.state,'delivered' if author==BOT else 'unknown')
                    if author==BOT:
                        self.assertEqual(result.destination['transport_sender'],'hyemi')
                        self.assertEqual(result.destination['logical_author'],recipient)
                    else:self.assertIsNone(result.destination)

    def test_unsupported_transport_sender_and_cross_route_never_read_token(self):
        from hyemi_delivery import DiscordTransport
        from hyemi_formatting import render_discord
        from unittest.mock import Mock
        tokens=SimpleNamespace(get=Mock())
        transport=DiscordTransport(tokens,ALLOWED)
        for sender,channel in [('hyeji',CHANNEL),('hyerin',CHANNEL),('hyemi',OTHER)]:
            with patch('hyemi_delivery.request_json') as http:
                self.assertEqual(transport.send(sender,channel,'hello',render_discord('hello')[0],recipient='hyeji',logical_author='hyemi').state,'failed')
                tokens.get.assert_not_called();http.assert_not_called()

class DedicatedCredentialSpecRegressions(unittest.TestCase):
    """Exercise real descriptor reads; redirect only the fixed credential open."""
    TOKEN = 'offline-fixture-token-not-real'
    KEY = 'HYEMI_DISCORD_TOKEN'

    def setUp(self):
        import hyemi_delivery as d
        self.d = d
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'synthetic-credential'
        self.write(self.KEY + '=' + self.TOKEN)
        real_open = d.os.open
        def fixture_open(path, flags, *args, **kwargs):
            self.assertEqual(path, Path('/root/.hermes/.env'))
            self.assertTrue(flags & d.os.O_NOFOLLOW)
            return real_open(self.path, flags, *args, **kwargs)
        patcher = patch.object(d.os, 'open', side_effect=fixture_open)
        self.open_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text):
        self.path.write_bytes(text.encode('utf-8') if isinstance(text, str) else text)
        self.path.chmod(0o600)

    def get(self):
        return self.d.DiscordTokens().get('hyemi')

    def rejected(self):
        with self.assertRaises(self.d.SenderUnavailable) as caught:
            self.get()
        self.assertIn(str(caught.exception),
                      ('sender_configuration_invalid', 'sender_not_configured'))
        self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))

    def test_root_regular_0600_accepted(self):
        self.assertEqual(self.path.stat().st_uid, 0)
        self.assertEqual(self.get(), self.TOKEN)

    def test_exact_mode_rejects_readonly_execute_shared_and_special_bits(self):
        for mode in (0o400, 0o700, 0o640, 0o644, 0o1600, 0o2600, 0o4600):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                self.rejected()

    def test_nonroot_owner_rejected_even_when_effective_uid_matches(self):
        real_fstat = self.d.os.fstat
        def nonroot(fd):
            actual = real_fstat(fd)
            return SimpleNamespace(st_mode=actual.st_mode, st_uid=1000)
        with patch.object(self.d.os, 'fstat', side_effect=nonroot) as checked, \
                patch.object(self.d.os, 'geteuid', return_value=1000):
            self.rejected()
            checked.assert_called_once()

    def test_root_owner_not_current_effective_uid_policy(self):
        with patch.object(self.d.os, 'geteuid', return_value=1000):
            self.assertEqual(self.get(), self.TOKEN)

    def test_opened_descriptor_metadata_wins_over_path_metadata(self):
        # The pathname fixture is compliant; the opened descriptor's metadata is not.
        import stat
        with patch.object(self.d.os, 'fstat',
                          return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_uid=0)):
            self.rejected()
        with patch.object(self.d.os, 'fstat',
                          return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o600, st_uid=0)):
            self.rejected()

    def test_symlink_race_rejected_by_nofollow(self):
        target = self.path.with_name('target')
        self.path.rename(target)
        self.path.symlink_to(target)
        # Force the preliminary path check to miss a symlink, as in a race.
        with patch.object(Path, 'is_symlink', return_value=False):
            self.rejected()

    def test_oversize_invalid_utf8_missing_file_sanitized(self):
        for body in (b'x' * 262145, b'\xff'):
            with self.subTest(body_type='oversize' if len(body) > 1 else 'invalid_utf8'):
                self.write(body)
                self.rejected()
        self.path.unlink()
        self.rejected()

    def test_single_canonical_and_horizontal_export_assignments(self):
        for prefix in ('', 'export ', 'export\t', 'export \t ', ' \t export\t '):
            with self.subTest(prefix=prefix):
                self.write(prefix + self.KEY + '=' + self.TOKEN)
                self.assertEqual(self.get(), self.TOKEN)

    def test_duplicate_export_horizontal_whitespace_both_orders(self):
        normal = self.KEY + '=' + self.TOKEN
        for prefix in ('export ', 'export\t', 'export \t ', '\texport\t ', '  export \t\t'):
            exported = prefix + self.KEY + '=offline-second-token-not-real'
            for lines in ((normal, exported), (exported, normal)):
                with self.subTest(prefix=prefix, exported_first=lines[0] == exported):
                    self.write('\n'.join(lines))
                    self.rejected()

    def test_malformed_target_assignments_still_count(self):
        normal = self.KEY + '=' + self.TOKEN
        for raw in ('', 'two words', '"unclosed'):
            for prefix in ('export\t', ' \texport \t '):
                malformed = prefix + self.KEY + '=' + raw
                for lines in ((normal, malformed), (malformed, normal), (malformed,)):
                    with self.subTest(raw=raw, prefix=prefix, lines=len(lines)):
                        self.write('\n'.join(lines))
                        self.rejected()

    def test_legacy_reader_retains_effective_owner_and_modes(self):
        real_fstat = self.d.os.fstat
        def nonroot(fd):
            actual = real_fstat(fd)
            return SimpleNamespace(st_mode=actual.st_mode, st_uid=1000)
        for mode in (0o400, 0o600, 0o700, 0o4600):
            with self.subTest(mode=oct(mode)), \
                    patch.object(self.d.os, 'fstat', side_effect=nonroot), \
                    patch.object(self.d.os, 'geteuid', return_value=1000):
                self.path.chmod(mode)
                self.assertEqual(self.d._secure_read(Path('/root/.hermes/.env')),
                                 self.KEY + '=' + self.TOKEN)

    def test_legacy_telegram_parser_semantics_unchanged(self):
        normal = 'TELEGRAM_BOT_TOKEN=123:offline'
        for extra in ('export\tTELEGRAM_BOT_TOKEN=456:ignored',
                      'TELEGRAM_BOT_TOKEN=', 'TELEGRAM_BOT_TOKEN=two words'):
            with self.subTest(extra=extra):
                self.write(normal + '\n' + extra)
                self.assertEqual(self.d.read_profile_token(Path('/root/.hermes/.env')), '123:offline')
        self.write('export\t' + normal)
        with self.assertRaises(self.d.SenderUnavailable):
            self.d.read_profile_token(Path('/root/.hermes/.env'))
        self.write(' \texport ' + normal)
        self.assertEqual(self.d.read_profile_token(Path('/root/.hermes/.env')), '123:offline')

class Contracts(unittest.TestCase):
    def test_telegram_canonical_duplicate(self):
        old=request('telegram');old.pop('platform')
        self.assertEqual(normalize_request(dict(old,platform='telegram'),ALLOWED),old)
        with tempfile.TemporaryDirectory() as tmp:
            s=WorkflowStore(Path(tmp)/'db');s.admit('owner',old)
            self.assertEqual(s.admit('owner',normalize_request(dict(old,platform='telegram'),ALLOWED))['status'],'duplicate')
    def test_discord_all_kinds(self):
        for kind in ('report','task','review'):
            self.assertEqual(normalize_request(request(kind=kind),ALLOWED)['platform'],'discord')
    def test_isolation_and_ids(self):
        for kw in ({'channelid':OTHER},{'channelid':'-'+CHANNEL},{'channelid':CHANNEL+':1'},
                   {'channelid':'0'+CHANNEL},{'channelid':int(CHANNEL)},{'channelid':None},
                   {'channelid':'１２３'},{'channelid':'0'},{'channelid':'18446744073709551616'},
                   {'platform':'slack'},{'platform':None},{'platform':True},
                   {'platform':'telegram','channelid':CHANNEL}):
            with self.subTest(kw=kw), self.assertRaises(ContractError):normalize_request(request(**kw),ALLOWED)
    def test_policy_is_not_telegram(self):
        a=copy.deepcopy(ALLOWED);a['hyeji']['channels'].append(CHANNEL);del a['hyeji']['discord']
        with self.assertRaises(ContractError):normalize_request(request(),a)

class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=WorkflowStore(Path(self.tmp.name)/'db')
    def test_frozen_raw_no_execution_owner_isolation(self):
        r=request(contents=' \n**문서**\t😀<@123><@!456><@&789> '+ '😀'*2200+'\n')
        self.store.admit('owner',r)
        self.assertEqual(self.store.report('owner',r['event_id']),r)
        self.assertEqual(self.store.queued_events(),[])
        with self.assertRaises(NotFound):self.store.report('other',r['event_id'])
        rows=self.store.outbox_pending()
        from hyemi_delivery import utf16_units, split_text
        from hyemi_formatting import discord_text
        pieces=split_text(discord_text(r['contents']),1998)
        self.assertEqual(len(rows),len(pieces))
        recovered=[]
        for row,piece in zip(rows,pieces):
            left,right=piece[:1].isspace(),piece[-1:].isspace()
            self.assertEqual(row['text'],('\u200b' if left else '')+piece+('\u200b' if right else ''))
            recovered.append(row['text'][int(left):len(row['text'])-int(right)])
        self.assertEqual(''.join(recovered),discord_text(r['contents']))
        self.assertTrue(all(utf16_units(x['text'])<=2000 for x in rows))
        self.assertTrue(all(x['sender']=='hyemi' for x in rows))
        self.assertTrue(all(json.loads(x['rendering'])['platform']=='discord' for x in rows))
        self.assertNotIn('<@',''.join(x['text'] for x in rows))
    def test_unknown_blocks_remaining_chunks(self):
        r=request(contents='x'*4500);self.store.admit('o',r)
        rows=self.store.outbox_pending();self.assertTrue(self.store.claim_outbox(rows[0]['id']))
        self.store.reconcile_outbox()
        self.assertFalse(self.store.claim_outbox(rows[1]['id']))
        self.assertFalse(self.store.retry_failed_outbox(rows[0]['id']))

class StoreMessageIdProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=WorkflowStore(Path(self.tmp.name)/'db')

    def finish(self, message_id, platform='discord', state='delivered'):
        r=request(platform);self.store.admit('owner',r)
        row=next(x for x in self.store.outbox_pending() if x['event_id']==r['event_id'])
        self.assertTrue(self.store.claim_outbox(row['id']))
        if platform=='discord':
            proof=dict(platform='discord',channel_id=CHANNEL,guild_id=GUILD,
                       author_id=BOT,logical_author='hyemi',transport_sender='hyemi')
        else:
            proof=dict(chat_id='-1000000002',message_thread_id=None,general=True)
        try:
            self.store.finish_outbox(row['id'],state,message_id=message_id,destination=proof)
        except ContractError:
            rejected=True
        else:
            rejected=False
        # Reopen the durable store, not a transport mock or cached result.
        result=WorkflowStore(self.store.path).result('owner',r['event_id'])['delivery'][0]
        return rejected,result,proof

    def test_discord_rejects_noncanonical_string_ids(self):
        for mid in ('000123','18446744073709551616','0','',' 123','123 ',
                    '123\n','１２３','+123','-123'):
            with self.subTest(message_id=mid):
                rejected,result,_=self.finish(mid)
                self.assertFalse(result['confirmed_destination'])
                self.assertIsNone(result['verified_destination'])
                self.assertEqual(result['state'],'sending' if rejected else 'unknown')

    def test_discord_rejects_nonstring_ids_without_confirming_conversion(self):
        for mid in (123,18446744073709551615,0,True,False,123.0,None,[],{}):
            with self.subTest(message_id=mid):
                rejected,result,_=self.finish(mid)
                self.assertFalse(result['confirmed_destination'])
                self.assertIsNone(result['verified_destination'])
                self.assertEqual(result['state'],'sending' if rejected else 'unknown')

    def test_discord_accepts_canonical_boundary_ids(self):
        for state in ('delivered','sent'):
            for mid in ('1','123','18446744073709551615'):
                with self.subTest(state=state,message_id=mid):
                    rejected,result,proof=self.finish(mid,state=state)
                    self.assertFalse(rejected)
                    self.assertEqual(result['state'],'delivered')
                    self.assertTrue(result['confirmed_destination'])
                    self.assertEqual(result['verified_destination'],proof)
                    self.assertEqual(result['message_id'],mid)

    def test_telegram_preserves_legacy_message_id_acceptance(self):
        for mid in (123,'000123','18446744073709551616'):
            with self.subTest(message_id=mid):
                rejected,result,proof=self.finish(mid,platform='telegram')
                self.assertFalse(rejected)
                self.assertEqual(result['state'],'delivered')
                self.assertTrue(result['confirmed_destination'])
                self.assertEqual(result['verified_destination'],proof)
                self.assertEqual(result['message_id'],str(mid))

    def test_telegram_preserves_legacy_message_id_rejection(self):
        for mid in (True,123.0,None,'0','１２３',' 123','+123'):
            with self.subTest(message_id=mid):
                rejected,result,_=self.finish(mid,platform='telegram')
                self.assertEqual(rejected,isinstance(mid,(bool,float)))
                self.assertEqual(result['state'],'sending' if rejected else 'unknown')
                self.assertFalse(result['confirmed_destination'])
                self.assertIsNone(result['verified_destination'])

class Transport(unittest.TestCase):
    def setUp(self):
        from hyemi_delivery import DiscordTransport
        self.tokens=SimpleNamespace(get=lambda sender:'offline-fixture-token-not-real')
        self.transport=DiscordTransport(self.tokens,ALLOWED)
        self.plan={'platform':'discord','text':'본문', 'allowed_mentions':{'parse':[],'replied_user':False}}
        self.response={'id':'999','channel_id':CHANNEL,'content':'본문','author':{'id':BOT,'bot':True}}
    def send(self):
        return self.transport.send('hyemi',CHANNEL,'본문',self.plan,recipient='hyeji',logical_author='hyemi')
    def replies(self,last):
        return [(200,{'id':BOT,'bot':True}),(200,{'id':CHANNEL,'guild_id':GUILD}),last]
    def test_success_and_exact_payload(self):
        with patch('hyemi_delivery.request_json',side_effect=self.replies((200,self.response))) as call:
            result=self.send()
        self.assertEqual(result.state,'delivered')
        self.assertEqual(result.destination['logical_author'],'hyemi')
        self.assertEqual(result.destination['transport_sender'],'hyemi')
        self.assertEqual(json.loads(call.call_args.args[0].data),{'content':'본문','tts':False,'allowed_mentions':{'parse':[],'replied_user':False}})
    def test_malformed_and_mismatched_response_unknown(self):
        for changes in ({'id':True},{'id':0},{'id':'0'},{'id':'１２３'}, {'channel_id':OTHER},
                        {'content':'rewrite'},{'author':{'id':'wrong','bot':True}},
                        {'author':{'id':BOT,'bot':False}}, {'guild_id':'wrong'}):
            with self.subTest(changes=changes),patch('hyemi_delivery.request_json',side_effect=self.replies((200,dict(self.response,**changes)))):
                self.assertEqual(self.send().state,'unknown')
    def test_timeout_429_5xx(self):
        from urllib.error import HTTPError
        for error,state in ((TimeoutError(),'unknown'),(HTTPError('offline',429,'rate',{},None),'failed'),(HTTPError('offline',500,'server',{},None),'unknown')):
            with self.subTest(state=state),patch('hyemi_delivery.request_json',side_effect=self.replies(error)):
                self.assertEqual(self.send().state,state)
    def test_wrong_bot_or_channel_prevents_post(self):
        for replies in ([(200,{'id':'wrong','bot':True})],[(200,{'id':BOT,'bot':True}),(200,{'id':CHANNEL,'guild_id':'wrong'})]):
            with patch('hyemi_delivery.request_json',side_effect=replies) as call:
                self.assertEqual(self.send().state,'blocked_configuration')
                self.assertTrue(all(x.args[0].get_method()=='GET' for x in call.call_args_list))
    def test_fixed_default_dedicated_token_no_fallback(self):
        from hyemi_delivery import DiscordTokens, SenderUnavailable
        with patch('hyemi_delivery._secure_read',return_value='HYEMI_DISCORD_TOKEN=offline-fixture-token-not-real') as read:
            self.assertEqual(DiscordTokens().get('hyemi'),'offline-fixture-token-not-real')
            read.assert_called_once_with(Path('/root/.hermes/.env'), require_root_0600=True)
            for who in ('hyeji','hyerin','default','unknown',None):
                with self.subTest(sender=who),self.assertRaises(SenderUnavailable):DiscordTokens().get(who)
            self.assertEqual(read.call_count,1)

class WorkerTests(Ledger):
    def workers(self):
        from hyemi_workers import WorkflowWorkers
        w=WorkflowWorkers({},self.store,SimpleNamespace(config={'allowed':copy.deepcopy(ALLOWED)}))
        self.addCleanup(w.close);return w
    def test_task_review_notifications(self):
        from hyemi_workers import failure
        from hyemi_delivery import SendResult, utf16_units
        for kind,recipient,channel in ((kind,recipient,channel) for kind in ('task','review')
                                       for recipient,channel in (('hyeji',CHANNEL),('hyerin',OTHER))):
            r=request(kind=kind,to=recipient,channelid=channel);self.store.admit('o',r);w=self.workers()
            row=self.store.event('o',r['event_id'])
            w._enqueue(row,'progress','확인 <@123> '+ '😀'*2300)
            w._finish(row,failure('검증 <@&123>'))
            rows=[x for x in self.store.outbox_pending() if x['event_id']==r['event_id']]
            self.assertTrue(all('<@' not in x['text'] and utf16_units(x['text'])<=2000 for x in rows))
            self.assertIn('telegram_report',self.store.result('o',r['event_id']))
            self.assertIn('model_telegram_report',self.store.result('o',r['event_id'])['hyemi_result']['notification'])
            def send(sender,channel,text,**kw):
                self.assertEqual(sender,'hyemi');self.assertEqual(kw['logical_author'],recipient)
                self.assertEqual(kw['recipient'],recipient)
                self.assertEqual(kw['rendering']['allowed_mentions'],{'parse':[],'replied_user':False})
                return SendResult('delivered','123',destination=dict(platform='discord',channel_id=channel,guild_id=GUILD,author_id=BOT,logical_author=recipient,transport_sender='hyemi'))
            def ready(sender):
                self.assertEqual(sender,'hyemi');return True
            w.discord_transport=SimpleNamespace(ready=ready,send=send)
            w._deliver()
            result=self.store.result('o',r['event_id'])
            self.assertTrue(result['delivery'])
            self.assertTrue(all(d['state']=='delivered' for d in result['delivery']))
            self.assertEqual(result['logical_author'],recipient)
            self.assertEqual(result['transport_sender'],'hyemi')
            self.assertEqual(result['sender_bot_id'],BOT)
    def test_missing_dedicated_token_blocks_both_routes_without_network(self):
        for recipient,channel in [('hyeji',CHANNEL),('hyerin',OTHER)]:
            r=request(to=recipient,channelid=channel);self.store.admit('o',r);w=self.workers()
            with patch('hyemi_delivery._secure_read',return_value='DISCORD_BOT_TOKEN=shared-fixture-never-use') as read,patch('hyemi_delivery.request_json') as http:
                w._deliver();http.assert_not_called()
                self.assertTrue(read.call_args_list)
                self.assertTrue(all(x.args==(Path('/root/.hermes/.env'),) for x in read.call_args_list))
            result=self.store.result('o',r['event_id'])
            self.assertEqual(result['transport_sender'],'hyemi')
            self.assertEqual(result['delivery'][0]['state'],'blocked_configuration')

    def test_discord_concurrent_claim_exactly_once(self):
        from concurrent.futures import ThreadPoolExecutor
        r=request();self.store.admit('o',r)
        row=self.store.outbox_pending()[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed=list(pool.map(lambda _:self.store.claim_outbox(row['id']),range(8)))
        self.assertEqual(sum(claimed),1)

    def test_delivery_policy_rechecked(self):
        r=request();self.store.admit('o',r);w=self.workers()
        w.legacy_ingress.config['allowed']['hyeji']['discord']['channels']=[]
        with patch.object(w.discord_transport,'send') as send:
            w._deliver();send.assert_not_called()
        self.assertEqual(self.store.result('o',r['event_id'])['delivery'][0]['state'],'blocked_configuration')
    def test_store_rejects_forged_discord_proofs(self):
        base=dict(platform='discord',channel_id=CHANNEL,guild_id=GUILD,author_id=BOT,logical_author='hyemi',transport_sender='hyemi')
        for key,recipient,channel in ((key,recipient,channel) for key in base
                                      for recipient,channel in (('hyeji',CHANNEL),('hyerin',OTHER))):
            base['channel_id']=channel
            r=request(to=recipient,channelid=channel);self.store.admit('o',r)
            row=next(x for x in self.store.outbox_pending() if x['event_id']==r['event_id'])
            self.store.claim_outbox(row['id'])
            self.store.finish_outbox(row['id'],'delivered',message_id='123',destination=dict(base,**{key:'wrong'}))
            self.assertEqual(self.store.result('o',r['event_id'])['delivery'][0]['state'],'unknown')
    def test_partial_discord_timeout_restart_never_replays(self):
        from hyemi_delivery import SendResult
        r=request(contents='x'*5000);self.store.admit('o',r);w=self.workers()
        proof=dict(platform='discord',channel_id=CHANNEL,guild_id=GUILD,author_id=BOT,logical_author='hyemi',transport_sender='hyemi')
        with patch.object(w.discord_transport,'ready',return_value=True),patch.object(w.discord_transport,'send',
                side_effect=[SendResult('delivered','123',destination=proof),SendResult('unknown',error='timeout')]) as send:
            w._deliver();w._deliver();self.assertEqual(send.call_count,2)
        self.assertEqual([d['state'] for d in self.store.result('o',r['event_id'])['delivery']],['delivered','unknown','pending'])
        restarted=self.workers()
        with patch.object(restarted.discord_transport,'ready',return_value=True),patch.object(restarted.discord_transport,'send') as send:
            restarted._deliver();send.assert_not_called()
    def test_legacy_json_plans_all_states_unchanged(self):
        for state in ('pending','blocked_configuration','sending','unknown','failed','delivered'):
            r=request('telegram');r.pop('platform');self.store.admit('o',r)
            with self.store._db() as db:
                db.execute('UPDATE v2_outbox SET state=? WHERE event_id=?',(state,r['event_id']))
                before=[tuple(x) for x in db.execute('SELECT * FROM v2_outbox WHERE event_id=?',(r['event_id'],))]
            self.store.admit('o',dict(r,platform='telegram'))
            WorkflowStore(self.store.path)
            with self.store._db() as db:
                self.assertEqual(before,[tuple(x) for x in db.execute('SELECT * FROM v2_outbox WHERE event_id=?',(r['event_id'],))])
    def test_legacy_plans_all_states_unchanged(self):
        for state in ('pending','blocked_configuration','sending','unknown','failed','delivered'):
            r=request('telegram');r.pop('platform');self.store.admit('o',r)
            with self.store._db() as db:
                db.execute('UPDATE v2_outbox SET state=?,rendering=NULL WHERE event_id=?',(state,r['event_id']))
                before=[tuple(x) for x in db.execute('SELECT * FROM v2_outbox WHERE event_id=?',(r['event_id'],))]
            self.store.admit('o',dict(r,platform='telegram'))
            WorkflowStore(self.store.path)
            with self.store._db() as db:
                self.assertEqual(before,[tuple(x) for x in db.execute('SELECT * FROM v2_outbox WHERE event_id=?',(r['event_id'],))])

class CanaryTests(unittest.TestCase):
    def setUp(self):
        from planning import run_dual_platform_canary as canary
        from hyemi_http import WorkflowIngress
        from hyemi_workers import WorkflowWorkers
        self.canary=canary
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'planning').mkdir()
        self.store=WorkflowStore(self.root/'db')
        allowed=copy.deepcopy(ALLOWED)
        allowed['hyerin']['discord']['channels']=['100000000000000008','100000000000000009',OTHER,'100000000000000011']
        self.cfg={'api_key':'offline-only', 'state_path':self.store.path,'allowed':allowed}
        self.ingress=WorkflowIngress(self.cfg,self.store)
        self.w=WorkflowWorkers({},self.store,SimpleNamespace(config=self.cfg));self.addCleanup(self.w.close)
        self.seed=request('telegram');self.store.admit('a'*64,self.seed)
        self.posts=0;self.discord_gets=0;self.messages={};self.ambiguous=False;self.after_post=None
        self.tamper=None;self.fsynced=[]
        import os
        real_fsync=os.fsync
        def fsync(fd):self.fsynced.append(fd);return real_fsync(fd)
        for item in (patch.object(canary,'ROOT',self.root),patch.object(canary,'load_config',return_value=self.cfg),
                     patch('bridge.LOCAL_OPENER',SimpleNamespace(open=self.private_open)),
                     patch('hyemi_delivery.request_json',side_effect=self.external),
                     patch('hyemi_delivery.DiscordTokens.get',side_effect=self.dedicated_token),
                     patch('hyemi_delivery.SenderTokens.get',return_value='123:offline'),
                     patch.object(canary.os,'fsync',side_effect=fsync)):
            item.start();self.addCleanup(item.stop)
    def dedicated_token(self,sender):
        self.assertEqual(sender,'hyemi')
        return 'offline-discord-token'
    def private_open(self,req,timeout):
        import io
        method=req.get_method();path=req.full_url.split(':8651')[1]
        owner=req.get_header('X-hyemi-owner');self.assertEqual(owner,self.canary.OPERATOR_OWNER)
        if method=='POST':
            self.posts+=1;self.assertEqual(self.posts,1)
            self.assertGreaterEqual(len(self.fsynced),2)
            self.body=json.loads(req.data)
            initial=json.loads(self.output.read_text());self.initial=initial
            self.assertEqual(initial['event_id'],self.body['event_id'])
            self.assertEqual(initial['work_id'],self.body['work_id'])
            self.assertEqual(self.output.stat().st_mode&0o777,0o600)
            result=self.ingress.submit('offline-only',owner,req.data)
            self.w._deliver()
            if self.after_post:self.after_post()
            if self.ambiguous:raise TimeoutError('offline response lost')
        else:
            result=self.ingress.get('offline-only',owner,path)
            if self.tamper:result=self.tamper(path,result)
        return io.BytesIO(json.dumps(result).encode())
    def external(self,req):
        url=req.full_url
        if 'api.telegram.org' in url:
            body=json.loads(req.data)
            return 200,{'ok':True,'result':{'message_id':41,'chat':{'id':int(body['chat_id'])}}}
        if url.endswith('/users/@me'):
            recipient=self.body['to'];return 200,{'id':self.cfg['allowed'][recipient]['discord']['sender_bot_id'],'bot':True}
        channel=url.split('/channels/')[1].split('/')[0]
        if '/messages' not in url:return 200,{'id':channel,'guild_id':GUILD}
        if req.get_method()=='POST':
            body=json.loads(req.data);mid=str(100+len(self.messages))
            value={'id':mid,'channel_id':channel,'content':body['content'],'author':{'id':self.cfg['allowed'][self.body['to']]['discord']['sender_bot_id'],'bot':True}}
            self.messages[mid]=value;return 200,value
        self.discord_gets+=1
        return 200,self.messages[url.rsplit('/',1)[1]]
    def invoke(self,target='Blog',reconcile=False):
        import contextlib,io
        self.output=self.root/'planning/receipt.json'
        argv=['--execute','--target',target,'--output','planning/receipt.json']
        if reconcile:argv.append('--reconcile')
        with contextlib.redirect_stdout(io.StringIO()):return self.canary.main(argv)
    def test_main_success_exact_store_and_discord_get(self):
        self.assertEqual(self.invoke(),0)
        receipt=json.loads(self.output.read_text())
        self.assertTrue(receipt['ok']);self.assertTrue(receipt['no_agent_run'])
        self.assertEqual(self.discord_gets,len(self.messages));self.assertGreater(self.discord_gets,1)
        self.assertEqual(self.posts,1)
        result=self.store.result(self.canary.OPERATOR_OWNER,self.body['event_id'])
        self.assertEqual(result['transport_sender'],'hyemi')
        self.assertEqual(result['logical_author'],'hyemi')
        self.assertEqual(result['sender_bot_id'],BOT)
        self.assertEqual(self.store.report('a'*64,self.seed['event_id']),normalize_request(self.seed,self.cfg['allowed']))
    def test_main_ambiguous_commit_reconciles_get_only(self):
        self.ambiguous=True;self.assertEqual(self.invoke(),0)
        self.assertEqual(self.posts,1);self.assertTrue(json.loads(self.output.read_text())['reconciled'])
        self.assertEqual(self.invoke(reconcile=True),0);self.assertEqual(self.posts,1)
    def test_receipt_exclusive(self):
        self.assertEqual(self.invoke(),0);self.assertEqual(self.invoke(),1);self.assertEqual(self.posts,1)
    def test_omitted_telegram_channel_canonical(self):
        self.assertEqual(self.invoke('HyejiTelegramGeneral'),0)
        self.assertNotIn('platform',self.body)
        self.assertEqual(self.body['channelid'],self.cfg['allowed']['hyeji']['home'])
        self.assertTrue(json.loads(self.output.read_text())['channelid_omitted'])
    def test_developer_jobs_is_hyerin(self):
        self.assertEqual(self.invoke('DeveloperJobs'),0);self.assertEqual(self.body['to'],'hyerin')
    def test_wrong_physical_author_independent_get_both_routes_fails(self):
        original=self.external
        def bad(req):
            code,value=original(req)
            if req.get_method()=='GET' and '/messages/' in req.full_url:
                value=dict(value,author={'id':'100000000000000004','bot':True})
            return code,value
        for target in ('Blog','DeveloperJobs'):
            with self.subTest(target=target),patch('hyemi_delivery.request_json',side_effect=bad):
                self.posts=0;self.messages={}
                self.assertEqual(self.invoke(target),1)
                self.assertEqual(self.posts,1)
                self.output.unlink()
    def test_exact_six_discord_targets_and_legacy_telegram(self):
        self.assertEqual(self.canary.TARGETS,{
            'Blog':('hyeji',CHANNEL), 'SecondBrain':('hyeji','100000000000000007'),
            'Parenting':('hyerin','100000000000000008'),
            'GeneralJobs':('hyerin','100000000000000009'),
            'DeveloperJobs':('hyerin',OTHER), 'Hotdeals':('hyerin','100000000000000011'),
            'HyejiTelegramGeneral':('hyeji',None)})
    def test_bad_get_content_fails(self):
        original=self.external
        def bad(req):
            code,value=original(req)
            if req.get_method()=='GET' and '/messages/' in req.full_url:value=dict(value,content='changed')
            return code,value
        with patch('hyemi_delivery.request_json',side_effect=bad):self.assertEqual(self.invoke(),1)
        self.assertEqual(self.posts,1)
    def test_initial_fsync_failure_never_posts(self):
        with patch.object(self.canary.os,'fsync',side_effect=OSError):
            self.assertEqual(self.invoke(),1)
        self.assertEqual(self.posts,0)
    def test_telegram_general_topic_one_proof(self):
        original=self.external
        def topic(req):
            code,value=original(req)
            if 'api.telegram.org' in req.full_url:value['result']['message_thread_id']=1
            return code,value
        with patch('hyemi_delivery.request_json',side_effect=topic):
            self.assertEqual(self.invoke('HyejiTelegramGeneral'),0)
    def test_receipt_failure_before_post(self):
        with patch.object(self.canary,'serialize_receipt',side_effect=TypeError):
            self.assertEqual(self.invoke(),1)
        self.assertEqual(self.posts,0)
    def test_persistent_post_serialization_failure_preserves_ids(self):
        def fail():
            item=patch.object(self.canary,'serialize_receipt',side_effect=TypeError);item.start();self.addCleanup(item.stop)
        self.after_post=fail
        self.assertEqual(self.invoke(),1);self.assertEqual(self.posts,1)
        self.assertEqual(json.loads(self.output.read_text())['event_id'],self.body['event_id'])
    def test_persistent_post_fsync_failure_preserves_ids(self):
        def fail():
            item=patch.object(self.canary.os,'fsync',side_effect=OSError);item.start();self.addCleanup(item.stop)
        self.after_post=fail
        self.assertEqual(self.invoke(),1);self.assertEqual(self.posts,1)
        self.assertEqual(json.loads(self.output.read_text())['event_id'],self.body['event_id'])
    def test_all_other_targets(self):
        for target in ('SecondBrain','Parenting','GeneralJobs','Hotdeals'):
            with self.subTest(target=target):
                self.posts=0;self.messages={}
                self.assertEqual(self.invoke(target),0)
                self.output.unlink()
    def test_readback_exact_identity_and_no_run(self):
        for field,value in (('owner','b'*64),('work_id',str(uuid4())),('metadata','{"run_id":"unexpected"}')):
            with self.subTest(field=field):
                self.posts=0;self.messages={}
                def mutate(field=field,value=value):
                    with self.store._db() as db:
                        db.execute('UPDATE v2_events SET '+field+'=? WHERE event_id=?',(value,self.body['event_id']))
                self.after_post=mutate
                self.assertEqual(self.invoke(),1);self.assertEqual(self.posts,1)
                self.output.unlink()
    def test_persistent_post_write_and_flush_failure_preserve_ids(self):
        import os
        real_fdopen=os.fdopen
        for stage in ('write','flush'):
            with self.subTest(stage=stage):
                self.posts=0;self.messages={}
                class BrokenStream:
                    def __init__(self,stream):self.stream=stream
                    def __enter__(self):return self
                    def __exit__(self,*args):self.stream.close()
                    def write(self,text):
                        if stage=='write':raise OSError('offline write failure')
                        return self.stream.write(text)
                    def flush(self):raise OSError('offline flush failure')
                    def fileno(self):return self.stream.fileno()
                patcher=patch.object(self.canary.os,'fdopen',side_effect=lambda *a,**kw:BrokenStream(real_fdopen(*a,**kw)))
                self.after_post=patcher.start
                try:
                    self.assertEqual(self.invoke(),1);self.assertEqual(self.posts,1)
                    self.assertEqual(json.loads(self.output.read_text())['event_id'],self.body['event_id'])
                finally:patcher.stop()
                self.output.unlink()
    def test_post_replace_directory_fsync_is_failure(self):
        import stat
        real_fsync=self.canary.os.fsync
        def broken(fd):
            if stat.S_ISDIR(self.canary.os.fstat(fd).st_mode):raise OSError('directory fsync')
            return real_fsync(fd)
        def fail():
            item=patch.object(self.canary.os,'fsync',side_effect=broken);item.start();self.addCleanup(item.stop)
        self.after_post=fail
        self.assertEqual(self.invoke(),1);self.assertEqual(self.posts,1)
        self.assertEqual(json.loads(self.output.read_text())['event_id'],self.body['event_id'])
    def test_persistent_post_replace_failure_preserves_ids(self):
        def fail():
            item=patch.object(self.canary.os,'replace',side_effect=OSError);item.start();self.addCleanup(item.stop)
        self.after_post=fail
        self.assertEqual(self.invoke(),1);self.assertEqual(self.posts,1)
        self.assertEqual(json.loads(self.output.read_text())['work_id'],self.body['work_id'])

class ProtocolTests(unittest.TestCase):
    def test_live_sdk_schema_and_dual_submission(self):
        import anyio
        import test_bridge as legacy
        from bridge import build_app
        from httpx import ASGITransport, AsyncClient
        legacy.BridgeTests.setUpClass()
        fixture=legacy.BridgeTests()
        cfg=dict(legacy.CFG,_ingress=dict(legacy.CFG['_ingress'],allowed=ALLOWED))
        async def run():
            app=build_app(cfg,fixture.verifier())
            async with app.app.router.lifespan_context(app.app):
                async with AsyncClient(transport=ASGITransport(app=app),base_url='http://bridge.example.invalid') as client:
                    headers={'Accept':'application/json, text/event-stream','Cf-Access-Jwt-Assertion':fixture.token()}
                    async def rpc(method,params):
                        return (await client.post('/mcp',headers=headers,json=dict(jsonrpc='2.0',id=1,method=method,params=params))).json()
                    await rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='offline',version='1')))
                    tools=(await rpc('tools/list',{}))['result']['tools']
                    for name,extra in [('deliver_report',{}),('request_task',{'acceptance_criteria':'done'}),('request_review',{'revision':1,'criteria':'check'})]:
                        tool=next(t for t in tools if t['name']==name)
                        self.assertIn('dedicated Hyemi bot',tool['description'])
                        self.assertNotIn('recipient-owned',tool['description'])
                        schema=tool['inputSchema']
                        self.assertEqual(schema['properties']['platform']['enum'],['telegram','discord'])
                        self.assertEqual(schema['properties']['platform']['default'],'telegram')
                        self.assertNotIn('platform',schema.get('required',[]))
                        self.assertEqual(list(schema['properties'])[-1],'platform')
                        for platform in ('telegram','discord'):
                            args=dict(event_id=str(uuid4()),work_id=str(uuid4()),contents='본문',platform=platform,**extra)
                            if platform=='discord':args['channelid']=CHANNEL
                            with patch('bridge.v2_request',return_value={'status':'accepted'}) as sent:
                                result=await rpc('tools/call',dict(name=name,arguments=args))
                                self.assertFalse(result['result']['isError'],result)
                                body=sent.call_args.args[4]
                                if platform=='telegram':self.assertNotIn('platform',body)
                                else:self.assertEqual(body['platform'],'discord')
                        with patch('bridge.v2_request') as sent:
                            args['platform']='discord';args.pop('channelid')
                            result=await rpc('tools/call',dict(name=name,arguments=args))
                            self.assertTrue(result['result']['isError']);sent.assert_not_called()
                    legacy_schema=next(t['inputSchema'] for t in tools if t['name']=='send_handoff')
                    self.assertNotIn('platform',legacy_schema['properties'])
        anyio.run(run)

if __name__=='__main__':unittest.main()
