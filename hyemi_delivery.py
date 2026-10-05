"""Sender-specific, lossless Telegram transport; credentials stay server-side."""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

MAX_UNITS = 4000
MAX_RESPONSE = 65536
HYEMI_TOKEN_FILE = Path('/root/.config/hyemi-ingress/hyemi-telegram.token')
PROFILE_ENVS = {'hyeji': Path('/root/.hermes/.env'),
                'hyerin': Path('/root/.hermes/profiles/hyerin/.env')}
TOKEN_RE = re.compile(r'[0-9]+:[A-Za-z0-9_-]+\Z')
CHANNEL_RE = re.compile(r'-?[0-9]+(?::[1-9][0-9]*)?\Z')


class SenderUnavailable(ValueError):
    """Safe public error code, never a credential value or path."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(request):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=15) as response:
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError('response_size')
        obj = json.loads(data)
        if not isinstance(obj, dict):
            raise ValueError('response_shape')
        return response.status, obj


def utf16_units(text):
    if not isinstance(text, str):
        raise ValueError('text_type')
    try:
        return len(text.encode('utf-16-le')) // 2
    except UnicodeEncodeError:
        raise ValueError('invalid_unicode') from None


def split_text(text, limit=MAX_UNITS):
    """Split on codepoint boundaries; concatenation is exactly the input."""
    if type(limit) is not int or not 2 <= limit <= MAX_UNITS:
        raise ValueError('invalid_limit')
    if not isinstance(text, str) or not text:
        raise ValueError('empty_text')
    utf16_units(text)
    pieces = []
    start = size = 0
    for index, char in enumerate(text):
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > limit:
            pieces.append(text[start:index]); start = index; size = 0
        size += units
    pieces.append(text[start:])
    return pieces


def _secure_read(path, *, require_root_0600=False):
    if path.is_symlink():
        raise SenderUnavailable('sender_configuration_invalid')
    # O_NOFOLLOW closes the symlink race between metadata and opening.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            meta = os.fstat(fd)
            # Dedicated Discord policy is opt-in; legacy Telegram stays EUID-owned.
            invalid_access = (meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600) if require_root_0600 else (
                meta.st_uid != os.geteuid() or meta.st_mode & 0o077)
            if not stat.S_ISREG(meta.st_mode) or invalid_access:
                raise SenderUnavailable('sender_configuration_invalid')
            with os.fdopen(fd, 'r', encoding='utf-8') as source:
                fd = -1
                data = source.read(262145)
            if len(data) > 262144:
                raise SenderUnavailable('sender_configuration_invalid')
            return data
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, UnicodeError):
        raise SenderUnavailable('sender_not_configured') from None


def read_profile_token(path, name_key='TELEGRAM_BOT_TOKEN', pattern=TOKEN_RE, *, strict=False):
    values = []
    matches = 0
    data = _secure_read(path, require_root_0600=True) if strict else _secure_read(path)
    for line in data.splitlines():
        line = line.strip()
        if strict:
            # Normalize shell horizontal whitespace before counting exact-key matches.
            line = re.sub(r'^export[ \t]+', '', line)
        elif line.startswith('export '):
            line = line[7:]
        name, sep, raw = line.partition('=')
        if sep and name.strip() == name_key:
            matches += 1
            try:
                tokens = shlex.split(raw, comments=True)
            except ValueError:
                raise SenderUnavailable('sender_configuration_invalid') from None
            if len(tokens) == 1:
                values.append(tokens[0])
    if (strict and matches != 1) or len(values) != 1 or not pattern.fullmatch(values[0]):
        raise SenderUnavailable('sender_configuration_invalid')
    return values[0]


class DiscordTokens:
    """Hyemi-only credential from the fixed default profile HYEMI_DISCORD_TOKEN."""
    def get(self, sender):
        if sender != 'hyemi':
            raise SenderUnavailable('sender_configuration_invalid')
        return read_profile_token(Path('/root/.hermes/.env'), 'HYEMI_DISCORD_TOKEN',
                                  re.compile(r'[A-Za-z0-9_.-]{20,200}\Z'), strict=True)


class SenderTokens:
    def __init__(self, config):
        self.config = config

    def get(self, sender):
        if sender in PROFILE_ENVS:
            return read_profile_token(PROFILE_ENVS[sender])
        if sender != 'hyemi':
            raise SenderUnavailable('sender_configuration_invalid')
        configured = self.config.get('hyemi_token_file')
        if not configured:
            raise SenderUnavailable('sender_not_configured')
        if not isinstance(configured, str) or Path(configured) != HYEMI_TOKEN_FILE:
            raise SenderUnavailable('sender_configuration_invalid')
        token = _secure_read(HYEMI_TOKEN_FILE).strip()
        if not TOKEN_RE.fullmatch(token):
            raise SenderUnavailable('sender_configuration_invalid')
        return token


@dataclass(frozen=True)
class SendResult:
    state: str
    message_id: str | None = None
    error: str | None = None
    destination: dict | None = None


def verified_destination(channel, chat_id, topic):
    """Return canonical destination proof, or None; bools are never IDs."""
    if not isinstance(channel, str) or not CHANNEL_RE.fullmatch(channel):
        return None
    chat, _, requested_topic = channel.partition(':')
    general = not requested_topic or int(requested_topic) == 1
    if type(chat_id) is not int or chat_id != int(chat):
        return None
    if topic is not None and (type(topic) is not int or topic < 1):
        return None
    if (general and topic not in (None, 1)) or (not general and topic != int(requested_topic)):
        return None
    return dict(chat_id=str(chat_id), message_thread_id=topic, general=general)


class TelegramTransport:
    def __init__(self, tokens):
        self.tokens = tokens

    def ready(self, sender):
        try:
            self.tokens.get(sender)
            return True
        except SenderUnavailable:
            return False

    def send(self, sender, channel, text, rendering=None):
        try:
            if not isinstance(channel, str) or not CHANNEL_RE.fullmatch(channel):
                return SendResult('failed', error='invalid_destination')
            if not text or utf16_units(text) > MAX_UNITS:
                return SendResult('failed', error='invalid_message')
            token = self.tokens.get(sender)
        except SenderUnavailable:
            return SendResult('blocked_configuration', error='sender_not_configured')
        except (ValueError, TypeError):
            return SendResult('failed', error='invalid_message')
        chat, _, topic = channel.partition(':')
        payload = {'chat_id': chat, 'text': text,
                   'disable_web_page_preview': True, 'disable_notification': False}
        if rendering is not None:
            try:
                from hyemi_formatting import validate_chunk
                if sender != 'hyemi': raise ValueError('report_sender_only')
                payload.update(validate_chunk(text, rendering))
                payload.pop('disable_web_page_preview')
            except (ValueError, TypeError, KeyError):
                return SendResult('failed', error='invalid_report_rendering')
        if topic and topic != '1':
            payload['message_thread_id'] = int(topic)
        request = Request('https://api.telegram.org/bot' + token + '/sendMessage',
                          method='POST', data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        try:
            code, answer = request_json(request)
        except HTTPError as exc:
            return SendResult('failed' if 400 <= exc.code < 500 else 'unknown',
                              error='telegram_http_' + str(exc.code))
        except Exception:
            return SendResult('unknown', error='telegram_outcome_unknown')
        result = answer.get('result')
        if code == 200 and answer.get('ok') is True:
            proof = None
            if isinstance(result, dict) and isinstance(result.get('chat'), dict):
                proof = verified_destination(channel, result['chat'].get('id'), result.get('message_thread_id'))
                if 'message_thread_id' in result and type(result['message_thread_id']) is not int:
                    proof = None
            if (proof is None or type(result.get('message_id')) is not int
                    or result['message_id'] <= 0):
                return SendResult('unknown', error='telegram_destination_unconfirmed')
            return SendResult('delivered', str(result['message_id']), destination=proof)
        error_code = answer.get('error_code')
        if answer.get('ok') is False and type(error_code) is int and 400 <= error_code < 500:
            return SendResult('failed', error='telegram_http_' + str(error_code))
        return SendResult('unknown', error='telegram_outcome_unknown')


def verified_discord_destination(request, destination, logical_author):
    from hyemi_contracts import DISCORD_BOTS, DISCORD_GUILD
    expected = dict(platform='discord', channel_id=request['channelid'],
                    guild_id=DISCORD_GUILD, author_id=DISCORD_BOTS[request['to']],
                    logical_author=logical_author, transport_sender='hyemi')
    return expected if type(destination) is dict and destination == expected else None


class DiscordTransport:
    """Dedicated Hyemi sender; uncertain POSTs are never automatically retried."""
    def __init__(self, tokens, allowed):
        self.tokens, self.allowed = tokens, allowed

    def ready(self, sender):
        try:
            self.tokens.get(sender)
            return True
        except SenderUnavailable:
            return False

    def send(self, sender, channel, text, rendering=None, *, recipient, logical_author):
        from hyemi_contracts import authorize_destination, snowflake
        request = dict(platform='discord', to=recipient, channelid=channel)
        try:
            policy = authorize_destination(request, self.allowed)
            if sender != 'hyemi' or logical_author not in {'hyemi', recipient}:
                raise ValueError('sender_mismatch')
            if not text or utf16_units(text) > 2000 or '<@' in text:
                raise ValueError('invalid_message')
            expected = dict(platform='discord', text=text,
                            allowed_mentions={'parse': [], 'replied_user': False})
            if rendering != expected: raise ValueError('invalid_rendering')
            token = self.tokens.get('hyemi')
        except SenderUnavailable:
            return SendResult('blocked_configuration', error='sender_not_configured')
        except (ValueError, TypeError, KeyError):
            return SendResult('failed', error='discord_invalid_delivery')
        headers = {'Authorization': 'Bot '+token, 'Content-Type': 'application/json',
                   'User-Agent': 'HermesBridgeMCP (private dedicated Hyemi sender)'}
        base = 'https://discord.com/api/v10'
        try:
            code, me = request_json(Request(base+'/users/@me', headers=headers))
            if code != 200 or me.get('id') != policy['sender_bot_id'] or me.get('bot') is not True:
                raise ValueError('sender_mismatch')
            code, target = request_json(Request(base+'/channels/'+channel, headers=headers))
            if code != 200 or target.get('id') != channel or target.get('guild_id') != policy['guild_id']:
                raise ValueError('channel_mismatch')
        except Exception:
            return SendResult('blocked_configuration', error='discord_sender_unverified')
        payload = {'content': text, 'allowed_mentions': {'parse': [], 'replied_user': False}, 'tts': False}
        req = Request(base+'/channels/'+channel+'/messages', headers=headers, method='POST',
                      data=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        try:
            code, answer = request_json(req)
        except HTTPError as exc:
            return SendResult('failed' if 400 <= exc.code < 500 else 'unknown',
                              error='discord_http_'+str(exc.code))
        except Exception:
            return SendResult('unknown', error='discord_outcome_unknown')
        try:
            snowflake(answer.get('id'))
            if (code != 200 or answer.get('channel_id') != channel or answer.get('content') != text
                    or type(answer.get('author')) is not dict
                    or answer['author'].get('id') != policy['sender_bot_id']
                    or answer['author'].get('bot') is not True
                    or answer.get('guild_id', policy['guild_id']) != policy['guild_id']):
                raise ValueError('message_mismatch')
        except (ValueError, TypeError, KeyError):
            return SendResult('unknown', error='discord_destination_unconfirmed')
        proof = dict(platform='discord', channel_id=channel, guild_id=policy['guild_id'],
                     author_id=policy['sender_bot_id'], logical_author=logical_author, transport_sender='hyemi')
        return SendResult('delivered', answer['id'], destination=proof)

