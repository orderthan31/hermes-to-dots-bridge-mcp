import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from hyemi_delivery import (TelegramTransport, SenderTokens, SenderUnavailable,
                            split_text, utf16_units, request_json)


class Response(BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode()); self.status = status
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class DeliveryTests(unittest.TestCase):
    def test_lossless_split_counts_utf16(self):
        original = '보고\n' + '😀한글\n' * 3000
        pieces = split_text(original)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(''.join(pieces), original)
        self.assertTrue(all(0 < utf16_units(p) <= 4000 for p in pieces))

    def test_split_no_artificial_header_or_summary(self):
        text = '  **원문 그대로**\n\n끝  '
        self.assertEqual(split_text(text), [text])
        self.assertEqual(split_text('a' * 4001), ['a' * 4000, 'a'])
        self.assertEqual(split_text('😀' * 2001), ['😀' * 2000, '😀'])

    def test_empty_invalid_unicode_and_limit_rejected(self):
        for text in ('', '\ud800', None):
            with self.assertRaises(ValueError): split_text(text)
        with self.assertRaises(ValueError): split_text('a', 1)

    def test_missing_hyemi_token_fails_closed(self):
        with patch('hyemi_delivery.request_json') as http:
            result = TelegramTransport(SenderTokens({})).send('hyemi', '-1000000002', '원문')
        self.assertEqual(result.state, 'blocked_configuration')
        http.assert_not_called()

    def test_sender_cannot_fall_back_to_profile_or_arbitrary_file(self):
        tokens = SenderTokens({'hyemi_token_file': '/root/.hermes/.env'})
        with self.assertRaises(SenderUnavailable): tokens.get('hyemi')
        with self.assertRaises(SenderUnavailable): tokens.get('someone')

    def test_profile_token_loader_only_approved_files(self):
        with patch('hyemi_delivery.read_profile_token', return_value='123:FAKE') as load:
            self.assertEqual(SenderTokens({}).get('hyeji'), '123:FAKE')
            self.assertEqual(SenderTokens({}).get('hyerin'), '123:FAKE')
        self.assertEqual([str(c.args[0]) for c in load.call_args_list],
            ['/root/.hermes/.env', '/root/.hermes/profiles/hyerin/.env'])

    def test_raw_send_topic_and_no_parse_mode(self):
        tokens = unittest.mock.Mock(); tokens.get.return_value = '123:FAKE'
        with patch('hyemi_delivery.request_json', return_value=(200, {'ok': True, 'result': {'message_id': 42, 'chat': {'id': -1000000002}, 'message_thread_id': 5}})) as http:
            result = TelegramTransport(tokens).send('hyemi', '-1000000002:5', '**원문**')
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.telegram.org/bot123:FAKE/sendMessage')
        payload = json.loads(request.data)
        self.assertEqual(payload['text'], '**원문**')
        self.assertEqual(payload['message_thread_id'], 5)
        self.assertNotIn('parse_mode', payload)
        self.assertFalse(payload['disable_notification'])
        self.assertEqual((result.state, result.message_id), ('delivered', '42'))

    def test_destination_proof_required(self):
        tokens = unittest.mock.Mock(); tokens.get.return_value = '123:FAKE'
        cases = [({}, '-100:2'), ({'chat': {'id': -999}, 'message_thread_id': 99}, '-100:2'),
                 ({'chat': {'id': '-100'}, 'message_thread_id': 2}, '-100:2'),
                 ({'chat': {'id': -100}}, '-100:2'),
                 ({'chat': {'id': True}, 'message_thread_id': 2}, '1:2'),
                 ({'chat': {'id': -100}, 'message_thread_id': None}, '-100'),
                 ({'chat': {'id': -100}, 'message_thread_id': '2'}, '-100:2'),
                 ({'chat': {'id': -100}, 'message_thread_id': True}, '-100:1'),
                 ({'chat': {'id': -100}, 'message_thread_id': 99}, '-100')]
        for extra, channel in cases:
            with self.subTest(extra=extra), patch('hyemi_delivery.request_json', return_value=(200, {'ok': True, 'result': dict(message_id=42, **extra)})):
                result = TelegramTransport(tokens).send('hyeji', channel, 'hello')
                self.assertEqual((result.state, result.error), ('unknown', 'telegram_destination_unconfirmed'))
                self.assertIsNone(result.destination)
        for channel, topic in [('-100:2', 2), ('-100:1', 1), ('-100', None)]:
            answer = {'message_id': 42, 'chat': {'id': -100}}
            if topic is not None: answer['message_thread_id'] = topic
            with patch('hyemi_delivery.request_json', return_value=(200, {'ok': True, 'result': answer})):
                result = TelegramTransport(tokens).send('hyeji', channel, 'hello')
                self.assertEqual(result.state, 'delivered')
                self.assertEqual(result.destination, dict(chat_id='-100', message_thread_id=topic, general=topic in (None, 1)))

    def test_general_topic_omits_thread(self):
        tokens = unittest.mock.Mock(); tokens.get.return_value = '123:FAKE'
        with patch('hyemi_delivery.request_json', return_value=(200, {'ok': True, 'result': {'message_id': 1, 'chat': {'id': -1000000002}}})) as http:
            TelegramTransport(tokens).send('hyeji', '-1000000002:1', '진행')
        self.assertNotIn('message_thread_id', json.loads(http.call_args.args[0].data))

    def test_invalid_destination_and_oversize_no_network(self):
        tokens = unittest.mock.Mock(); tokens.get.return_value = '123:FAKE'
        with patch('hyemi_delivery.request_json') as http:
            for channel, text in [('https://example.test', 'x'), ('-1:0', 'x'), ('-1', 'a' * 4001)]:
                result = TelegramTransport(tokens).send('hyemi', channel, text)
                self.assertEqual(result.state, 'failed')
        http.assert_not_called()

    def test_explicit_rejection_vs_unknown_are_separate(self):
        tokens = unittest.mock.Mock(); tokens.get.return_value = '123:FAKE'
        for answer, expected in [((200, {'ok': False, 'error_code': 403, 'description': 'private details'}), 'failed'),
                                 ((200, {'ok': True, 'result': {}}), 'unknown'),
                                 ((500, {'ok': False}), 'unknown')]:
            with self.subTest(answer=answer), patch('hyemi_delivery.request_json', return_value=answer):
                result = TelegramTransport(tokens).send('hyeji', '-1', 'hello')
                self.assertEqual(result.state, expected)
                self.assertNotIn('private details', str(result))
        with patch('hyemi_delivery.request_json', side_effect=URLError('SECRET_TOKEN')):
            result = TelegramTransport(tokens).send('hyemi', '-1', 'hello')
            self.assertEqual(result.state, 'unknown')
            self.assertNotIn('SECRET_TOKEN', str(result))

    def test_http_4xx_is_known_rejection(self):
        tokens = unittest.mock.Mock(); tokens.get.return_value = '123:FAKE'
        with patch('hyemi_delivery.request_json', side_effect=HTTPError('secret-url', 429, 'private', {}, None)):
            result = TelegramTransport(tokens).send('hyemi', '-1', 'hello')
        self.assertEqual((result.state, result.error), ('failed', 'telegram_http_429'))

    def test_no_proxy_no_redirect_bounded_response(self):
        with patch('hyemi_delivery.build_opener') as factory:
            factory.return_value.open.return_value = Response({'ok': True})
            request_json(unittest.mock.Mock())
        args = factory.call_args.args
        self.assertEqual(args[0].proxies, {})
        self.assertIsNone(args[1].redirect_request(None, None, 302, 'redirect', {}, 'https://evil.test'))
        with patch('hyemi_delivery.build_opener') as factory:
            huge = unittest.mock.MagicMock()
            huge.__enter__.return_value = huge; huge.status = 200
            huge.read.return_value = b'x' * 65537
            factory.return_value.open.return_value = huge
            with self.assertRaises(ValueError): request_json(unittest.mock.Mock())


if __name__ == '__main__': unittest.main()
