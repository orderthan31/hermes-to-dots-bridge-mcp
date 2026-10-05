"""Strict v2 data contracts; no prompts, network calls, or legacy imports.

normalize_request(raw, allowed) checks recipient channel authorization. The store
accepts only normalized requests and rechecks structure with validate_request.
"""
import hashlib
import json
import re
from uuid import UUID


class ContractError(ValueError):
    pass

# Dedicated physical sender for both logical recipients; channel policies stay separate.
DISCORD_GUILD = '100000000000000003'
DISCORD_BOTS = {'hyeji': '100000000000000012', 'hyerin': '100000000000000012'}

def snowflake(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value)
            or int(value) > 2**64 - 1):
        raise ContractError('invalid Discord ID')
    return value

def authorize_destination(request, allowed):
    """Single fail-closed admission/delivery policy; never infer a platform."""
    try:
        platform = request.get('platform', 'telegram')
        if not isinstance(platform, str) or platform not in {'telegram', 'discord'}:
            raise ContractError('invalid platform')
        policy = allowed[request['to']]
        if platform == 'discord':
            policy = policy['discord']
            if (snowflake(policy['guild_id']) != DISCORD_GUILD
                    or snowflake(policy['sender_bot_id']) != DISCORD_BOTS[request['to']]):
                raise ContractError('invalid Discord sender policy')
            channels = policy['channels']
            if not isinstance(channels, (list, tuple, set, dict)):
                raise ContractError('invalid channel policy')
            for channel in channels: snowflake(channel)
            snowflake(request['channelid'])
            authorized = channels
        else:
            channels = policy.get('channels', [])
            if not isinstance(channels, (list, tuple, set, dict)):
                raise ContractError('invalid channel policy')
            authorized = [policy.get('home'), *channels]
        if request['channelid'] not in authorized:
            raise ContractError('channel is not authorized for recipient')
        return policy
    except (KeyError, TypeError, AttributeError):
        raise ContractError('recipient has no channel policy') from None

KINDS = frozenset({'report', 'review', 'task'})
RECIPIENTS = frozenset({'hyeji', 'hyerin'})
MAX_SEQUENCE = 2**63 - 1

def canonical_uuid(value):
    if not isinstance(value, str): raise ContractError('UUID must be a string')
    try: canonical = str(UUID(value))
    except (ValueError, AttributeError): raise ContractError('invalid UUID') from None
    if value != canonical: raise ContractError('UUID must be canonical lowercase hyphenated form')
    return value

def integer(value, name, minimum=0, maximum=MAX_SEQUENCE):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f'{name} must be an integer in [{minimum}, {maximum}]')
    return value

def text(value, name, max_bytes=40000, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ContractError(f'{name} must be a nonempty string')
    try: size = len(value.encode('utf-8'))
    except UnicodeError: raise ContractError(f'{name} must be valid UTF-8') from None
    if size > max_bytes: raise ContractError(f'{name} exceeds byte limit')
    return value

def canonical_json(value):
    try: return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError): raise ContractError('value must be JSON serializable') from None

def _hash(parts):
    return hashlib.sha256(canonical_json(parts).encode('utf-8')).hexdigest()

def owner_id(issuer, subject):
    return _hash([text(issuer,'issuer'), text(subject,'subject')])

def session_key(owner, work, to, kind):
    text(owner, 'owner'); canonical_uuid(work)
    if to not in RECIPIENTS or kind not in KINDS: raise ContractError('invalid session recipient or role')
    return 'hyemi-v2-' + _hash([owner, work, to, kind])

def validate_request(raw):
    if type(raw) is not dict: raise ContractError('request must be an object')
    kind = raw.get('kind')
    if not isinstance(kind,str) or kind not in KINDS: raise ContractError('kind must be report, review, or task')
    required = {'version','kind','event_id','work_id','to','channelid','contents'}
    optional = {'title', 'platform'}
    if kind == 'review':
        required |= {'revision','criteria'}; optional |= {'previous_event_id','changes'}
    elif kind == 'task': required |= {'acceptance_criteria'}
    else: optional |= {'revision'}
    if set(raw) - required - optional: raise ContractError('unknown request fields')
    if required - set(raw): raise ContractError('missing request fields')
    if type(raw['version']) is not int or raw['version'] != 2: raise ContractError('version must be 2')
    r = dict(raw)
    platform = r.get('platform', 'telegram')
    if not isinstance(platform, str) or platform not in {'telegram', 'discord'}:
        raise ContractError('invalid platform')
    if platform == 'telegram': r.pop('platform', None)
    else: snowflake(r['channelid'])
    for field in ('event_id','work_id','previous_event_id'):
        if field in r: canonical_uuid(r[field])
    if not isinstance(r['to'],str) or r['to'] not in RECIPIENTS: raise ContractError('invalid recipient')
    text(r['channelid'],'channelid',200)
    for field in ('contents','criteria','acceptance_criteria','changes'):
        if field in r: text(r[field],field,nonempty=field != 'changes')
    if 'title' in r:
        text(r['title'],'title',800,nonempty=False)
        if len(r['title']) > 200: raise ContractError('title exceeds 200 characters')
    if 'revision' in r: integer(r['revision'],'revision',1)
    return r

def normalize_request(raw, allowed):
    if type(raw) is not dict: raise ContractError('request must be an object')
    r = dict(raw)
    if r.get('to') == 'hyelin': r['to'] = 'hyerin'
    r = validate_request(r)
    authorize_destination(r, allowed)
    return r

RESULT_OUTCOMES = {
    'review': {'changes_required','passed_in_scope','unverified','failed','cancelled'},
    'task': {'completed','failed','blocked','cancelled','interrupted','unverified'},
}

def _meaningful(value):
    if isinstance(value,str): return bool(value.strip())
    if isinstance(value,dict): return any(_meaningful(v) for v in value.values())
    if isinstance(value,list): return any(_meaningful(v) for v in value)
    return False

def validate_result(result, kind):
    """Canonical persistence boundary; metadata inside detail remains extensible."""
    import re
    required={'hyemi_result','telegram_report','outcome','completion_signal'}
    if type(result) is not dict or set(result) != required:
        raise ContractError('result requires exactly four canonical fields')
    if kind not in RESULT_OUTCOMES: raise ContractError('reports have no executor result')
    detail=result['hyemi_result']
    if type(detail) is not dict: raise ContractError('hyemi_result must be a structured object')
    text(detail.get('summary'),'summary')
    report=text(result['telegram_report'],'telegram_report')
    if not re.search('[가-힣]',report): raise ContractError('telegram_report must contain Korean text')
    status=result['outcome']
    if not isinstance(status,str) or status not in RESULT_OUTCOMES[kind]: raise ContractError('invalid result outcome')
    completed=status == ('completed' if kind=='task' else 'passed_in_scope')
    if type(result['completion_signal']) is not bool or result['completion_signal'] != completed:
        raise ContractError('completion_signal disagrees with outcome')
    if kind=='task' and completed and not _meaningful(detail.get('evidence')):
        raise ContractError('completed task requires meaningful evidence')
    canonical_json(result)
    return result

def normalize_executor_result(raw, kind):
    """Losslessly fold executor metadata into detail and derive completion."""
    if type(raw) is not dict or type(raw.get('hyemi_result')) is not dict:
        raise ContractError('executor result must contain structured detail')
    if raw.get('kind',kind)!=kind: raise ContractError('executor kind mismatch')
    detail=dict(raw['hyemi_result'])
    for key,value in raw.items():
        if key in {'hyemi_result','telegram_report','outcome','completion_signal'}: continue
        if key in detail and detail[key]!=value:
            raise ContractError('conflicting executor metadata: '+key)
        detail[key]=value
    status=raw.get('outcome')
    result=dict(hyemi_result=detail,telegram_report=raw.get('telegram_report'),
                outcome=status,completion_signal=status==('completed' if kind=='task' else 'passed_in_scope'))
    return validate_result(result,kind)

# Explicit compatibility name for adapters.
normalize = normalize_request
