"""Deterministic, network-free report presentation, schema 1.

Only nonnested bold, inline/fenced code, named HTTP(S) links and raw URLs.
Ambiguous/malformed supported syntax or oversized indivisible links fall back
for the WHOLE report to exact raw text, with no entities or previews. Unsupported
syntax is literal. No HTML/Markdown parse_mode is sent. Plans are persisted once.
"""
import ipaddress
import re


def discord_text(text):
    """Deterministic presentation only; original ledger contents stay exact."""
    # Break the token itself: allowed_mentions alone cannot protect other bots
    # that search raw message content for their own mention.
    return text.replace('<@', '<\u200b@')


def render_discord(text):
    """Freeze whitespace-preserving presentation; never normalize receipts.

    Reserve two UTF-16 units for optional U+200B edge guards. Only whitespace
    edges receive guards; every source character (after mention safety) stays
    in order. Literal U+200B is never removed. To recover mention-safe text,
    use the immutable source's 1998-unit pieces to identify inserted positions,
    not a blanket strip/replace of U+200B. Existing frozen plans stay unchanged.
    """
    from hyemi_delivery import split_text
    plans = []
    for part in split_text(discord_text(text), 1998):
        guarded = ('\u200b' if part[:1].isspace() else '') + part
        if part[-1:].isspace():
            guarded += '\u200b'
        plans.append(dict(platform='discord', text=guarded,
                          allowed_mentions={'parse': [], 'replied_user': False}))
    return plans

import unicodedata
from urllib.parse import urlsplit, unquote
from hyemi_delivery import split_text, utf16_units, MAX_UNITS

VERSION = 1
MAX_SOURCE = 262144
MAX_ENTITIES = 100
ESCAPES = r'\`*[]()_{}#+-.!>'
RAW_URL = re.compile(r'https?://[^\s<>`]+', re.I)


def valid_url(value):
    """Syntax validation only: never resolve/fetch, never rewrite the href."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return False
    decoded = unquote(value)
    if any(c.isspace() or unicodedata.category(c).startswith('C') or c in '\\<>"' for c in value):
        return False
    if any(unicodedata.category(c).startswith('C') for c in decoded):
        return False
    try:
        p = urlsplit(value)
        if p.scheme.lower() not in {'http', 'https'} or not p.netloc or not p.hostname or p.username is not None or p.password is not None:
            return False
        if p.port is not None and not 1 <= p.port <= 65535:
            return False
        host = p.hostname
        if not re.fullmatch(r'(?:\[[0-9A-Fa-f:.]+\]|[^:]+)(?::[0-9]+)?', p.netloc): return False
        if '%' in host or p.netloc.endswith(':'): return False
        if ':' in host:
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.encode('idna').decode('ascii').rstrip('.')
            if len(ascii_host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x) for x in ascii_host.split('.')):
                return False
        return True
    except (ValueError, UnicodeError):
        return False


def preview_safe(value):
    if not valid_url(value): return False
    host = urlsplit(value).hostname.lower().rstrip('.')
    try:
        address = ipaddress.ip_address(host)
        return address.is_global and not address.is_multicast and not address.is_unspecified
    except ValueError:
        # Avoid alternate IPv4 spellings and local/special-use hostnames.
        if re.fullmatch(r'[0-9.]+', host) or any(x.startswith('0x') for x in host.split('.')): return False
        return '.' in host and not host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid', '.onion', '.home', '.lan'))


def _chunk(text, entities=None, reason=None):
    out = {'version': VERSION, 'text': text, 'entities': entities or [], 'link_preview_options': {'is_disabled': True}}
    if reason: out['fallback'] = reason
    return out


def _spans(source):
    i = 0
    plain = []
    spans = []
    def flush():
        if plain: spans.append((''.join(plain), None)); plain.clear()
    while i < len(source):
        if source[i] == '\\' and i+1 < len(source) and source[i+1] in ESCAPES:
            plain.append(source[i+1]); i += 2; continue
        value = entity = None
        end = i
        if source.startswith('```', i):
            opening = source.find('\n', i+3)
            closing = source.find('```', i+3)
            if opening < 0 or closing <= opening: raise ValueError('ambiguous_fence')
            lang = source[i+3:opening]
            if lang and not re.fullmatch(r'[A-Za-z0-9_+-]{1,32}', lang): raise ValueError('ambiguous_fence')
            if closing > 0 and source[closing-1] != '\n': raise ValueError('ambiguous_fence')
            value = source[opening+1:closing]; entity = {'type': 'pre'}
            if lang: entity['language'] = lang
            end = closing+3
        elif source.startswith('**', i) or source[i] == '`':
            delim = '**' if source.startswith('**', i) else '`'
            closing = source.find(delim, i+len(delim))
            if closing < 0: raise ValueError('unclosed_markup')
            value = source[i+len(delim):closing]
            if not value or '\n' in value and delim == '`': raise ValueError('ambiguous_markup')
            if delim == '**' and any(c in value for c in '*`[\\'): raise ValueError('nested_markup')
            entity = {'type': 'bold' if delim == '**' else 'code'}; end = closing+len(delim)
        elif source[i] == '[':
            if i > 0 and source[i-1] == '!': raise ValueError('unsupported_image')
            close = source.find(']', i+1)
            if close < 0: raise ValueError('unclosed_bracket')
            if '[' in source[i+1:close]: raise ValueError('nested_bracket')
            if not source.startswith('(', close+1):
                # Literal bracket text is one span, avoiding quadratic rescans.
                plain.append(source[i:close+1]); i = close+1; continue
            if source.startswith('(', close+1):
                finish = close+2; depth = 1
                while finish < len(source) and depth:
                    if source[finish] == '(': depth += 1
                    elif source[finish] == ')': depth -= 1
                    finish += 1
                if depth: raise ValueError('unclosed_link')
                url = source[close+2:finish-1]; value = source[i+1:close]
                if not value or any(c in value for c in '[`*\\\n') or not valid_url(url): raise ValueError('ambiguous_link')
                entity = {'type': 'text_link', 'url': url}; end = finish
        elif source[i:i+8].lower().startswith(('http://', 'https://')):
            match = RAW_URL.match(source, i)
            if match:
                value = match[0]
                # Punctuation is retained, never removed from URL targets.
                if not valid_url(value): raise ValueError('invalid_url')
                entity = {'type': 'url'}; end = match.end()
        if entity is not None:
            if not value: raise ValueError('empty_markup')
            flush(); spans.append((value, entity)); i = end
        else:
            plain.append(source[i]); i += 1
    flush()
    return spans


def render_report(source):
    if not isinstance(source, str) or not source or len(source) > MAX_SOURCE: raise ValueError('invalid_report')
    utf16_units(source)
    try:
        spans = _spans(source)
        chunks = []; text = ''; entities = []; size = 0; preview_used = False
        def flush():
            nonlocal text, entities, size, preview_used
            if not text: return
            chunk = _chunk(text, entities)
            if not preview_used:
                raw = text.encode('utf-16-le')
                for e in entities:
                    url = e.get('url') if e['type'] == 'text_link' else raw[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le') if e['type'] == 'url' else None
                    if url and preview_safe(url):
                        chunk['link_preview_options'] = {'is_disabled': False, 'url': url}; preview_used = True; break
            chunks.append(chunk); text = ''; entities = []; size = 0
        for value, entity in spans:
            units = utf16_units(value)
            link = entity and entity['type'] in {'url', 'text_link'}
            if link and units > MAX_UNITS: raise ValueError('oversized_link')
            if entity and (len(entities) >= MAX_ENTITIES or link and size+units > MAX_UNITS): flush()
            start = 0
            while start < len(value):
                if size == MAX_UNITS: flush()
                stop = start; count = 0
                while stop < len(value):
                    width = 2 if ord(value[stop]) > 0xffff else 1
                    if size+count+width > MAX_UNITS: break
                    count += width; stop += 1
                if stop == start: flush(); continue
                if entity: entities.append(dict(entity, offset=size, length=count))
                text += value[start:stop]; size += count; start = stop
                if start < len(value): flush()
        flush()
        return chunks
    except ValueError as exc:
        return [_chunk(c, reason=str(exc)) for c in split_text(source)]


def validate_chunk(text, plan):
    """Fail closed on corrupt stored metadata; no re-render/retry on delivery."""
    if not isinstance(plan, dict) or plan.get('version') != VERSION or plan.get('text') != text: raise ValueError('invalid_rendering')
    raw = text.encode('utf-16-le'); last = 0
    entities = plan.get('entities')
    if not isinstance(entities, list) or len(entities) > MAX_ENTITIES: raise ValueError('invalid_entities')
    clickable = []
    for e in entities:
        if not isinstance(e, dict) or e.get('type') not in {'bold', 'code', 'pre', 'url', 'text_link'}: raise ValueError('invalid_entity')
        offset, length = e.get('offset'), e.get('length')
        if type(offset) is not int or type(length) is not int or offset < last or length <= 0 or (offset+length)*2 > len(raw): raise ValueError('invalid_entity_range')
        part = raw[offset*2:(offset+length)*2].decode('utf-16-le'); last = offset+length
        allowed = {'type','offset','length'} | ({'url'} if e['type']=='text_link' else {'language'} if e['type']=='pre' else set())
        if set(e)-allowed: raise ValueError('invalid_entity_fields')
        if e['type'] in {'text_link', 'url'}:
            url = e.get('url') if e['type']=='text_link' else part
            if not valid_url(url): raise ValueError('invalid_entity_url')
            clickable.append(url)
        if 'language' in e and not isinstance(e['language'], str): raise ValueError('invalid_language')
    preview = plan.get('link_preview_options')
    if preview != {'is_disabled': True}:
        if not isinstance(preview, dict) or set(preview) != {'is_disabled','url'} or preview['is_disabled'] is not False or preview['url'] not in clickable or not preview_safe(preview['url']): raise ValueError('invalid_preview')
    return {'entities': entities, 'link_preview_options': preview}
