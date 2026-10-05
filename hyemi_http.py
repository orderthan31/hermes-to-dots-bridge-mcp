"""Private v2 adapter. Mount only on the authenticated local ingress listener."""
import hmac
import json
import re
from hyemi_contracts import canonical_uuid, normalize_request, integer

MAX_BODY = 262144
OWNER_RE = re.compile(r'[0-9a-f]{64}')


def decode_body(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
        raise ValueError('invalid request body')
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj: raise ValueError('duplicate field')
            obj[key] = value
        return obj
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError('invalid JSON')))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('invalid request body') from None


class WorkflowIngress:
    def __init__(self, config, store, workers=None):
        self.config, self.store, self.workers = config, store, workers

    def authenticate(self, key, owner):
        expected = self.config.get('api_key')
        try:
            valid_key = (isinstance(expected, str) and bool(expected) and isinstance(key, str)
                         and hmac.compare_digest(key.encode('utf-8'), expected.encode('utf-8')))
        except UnicodeError:
            valid_key = False
        if not valid_key or not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
            raise PermissionError('unauthorized')

    def submit(self, key, owner, raw):
        self.authenticate(key, owner)
        request = normalize_request(decode_body(raw), self.config['allowed'])
        admitted = self.store.admit(owner, request)
        # Store admits the raw ledger and frozen whole-report outbox atomically.
        # Workers consume the durable ledger/outbox, never run agents inline.
        return {k: admitted[k] for k in ('status', 'event_id', 'work_id')}

    def get(self, key, owner, path):
        self.authenticate(key, owner)
        if not isinstance(path, str) or len(path) > 512: raise ValueError('invalid route')
        match = re.fullmatch(r'/v2/(works|events)/([0-9a-f-]+)(/updates|/result|/report)?(?:\?([^#]*))?', path)
        if not match: raise ValueError('invalid route')
        group, identifier, suffix, query = match.groups()
        canonical_uuid(identifier)
        if group == 'works' and suffix in (None, '/updates'):
            if suffix is None:
                if query is not None: raise ValueError('unexpected query')
                value = self.store.work_status(owner, identifier)
                operation, cursor = 'work_status', None
            else:
                args = {}
                if query is not None:
                    if not query: raise ValueError('invalid query')
                    for item in query.split('&'):
                        part = re.fullmatch(r'(after_seq|limit)=(0|[1-9][0-9]{0,18})', item)
                        if not part or part[1] in args: raise ValueError('invalid query')
                        args[part[1]] = int(part[2])
                cursor = integer(args.get('after_seq', 0), 'after_seq')
                limit = integer(args.get('limit', 50), 'limit', 1, 100)
                value = self.store.updates(owner, identifier, cursor, limit)
                operation = 'updates'
            self.store.record_query(owner, identifier, operation, cursor)
            return value
        if group == 'events' and suffix in ('/result', '/report') and query is None:
            event = self.store.event(owner, identifier)
            operation = suffix[1:]
            value = getattr(self.store, operation)(owner, identifier)
            self.store.record_query(owner, event['work_id'], operation)
            return value
        raise ValueError('invalid route')
