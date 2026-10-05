"""Fixed-origin, bounded, secret-safe private utility transport (no NAVER keys)."""
import asyncio
from collections import deque
import json
import math
import os
from pathlib import Path
import re
import time
import httpx

BASE_URL = 'http://utility-fastapi-server:8000'
ROUTES = frozenset(('/naver/search', '/naver/keyword-trend', '/naver/shopping-trend'))
MAX_BYTES = 262144
MAX_REQUEST_BYTES = 32768
MESSAGES = {
    'configuration': 'NAVER tools are not configured.',
    'auth': 'NAVER request is not authorized.',
    'rate': 'NAVER admission or provider rate limit reached.',
    'validation': 'Invalid NAVER request arguments.',
    'timeout': 'NAVER request timed out.',
    'provider': 'NAVER provider is unavailable.',
    'size': 'NAVER response exceeds the size limit.',
    'invalid-response': 'NAVER provider returned an invalid response.',
}

class NaverError(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(MESSAGES[category])

class Admission:
    """Rolling local MCP quotas; not a reservation of shared provider quotas.

    At most 120 live entries/owners; pruning never resets an active owner's quota.
    Synchronous admission is atomic in the server event loop.
    """
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.events = deque()
        self.owners = {}

    def admit(self, owner):
        if not isinstance(owner, str) or not re.fullmatch('[0-9a-f]{64}', owner):
            raise NaverError('auth')
        now = self.clock()
        while self.events and self.events[0][0] <= now-60:
            _, old = self.events.popleft()
            self.owners[old] -= 1
            if not self.owners[old]:
                del self.owners[old]
        if len(self.events) >= 120 or self.owners.get(owner, 0) >= 60:
            raise NaverError('rate')
        self.events.append((now, owner))
        self.owners[owner] = self.owners.get(owner, 0)+1


def load_key():
    # Server-controlled file path only; never accepted from a tool argument/cfg.
    try:
        path = Path(os.environ.get('NAVER_UTILITY_API_KEY_FILE', '/run/secrets/utility_api_key'))
        with path.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError()
        key = raw.decode('ascii').strip()
        if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError()
        return key
    except (OSError, ValueError, UnicodeError):
        raise NaverError('configuration') from None


def check_tree(obj, depth=0):
    if depth > 12:
        raise NaverError('invalid-response')
    if type(obj) is str and len(obj) > 8192:
        raise NaverError('size')
    if type(obj) is float and not math.isfinite(obj):
        raise NaverError('invalid-response')
    if isinstance(obj, dict):
        for key, value in obj.items():
            check_tree(key, depth+1)
            check_tree(value, depth+1)
    elif isinstance(obj, list):
        if len(obj) > 2196:  # 366 periods x 6 shopping age series
            raise NaverError('size')
        for value in obj:
            check_tree(value, depth+1)

def reject_private_echoes(obj, private_values):
    # Inspect decoded strings only, after check_tree has bounded the entire tree.
    if isinstance(obj, str):
        if any(private in obj for private in private_values):
            raise NaverError('invalid-response')
    elif isinstance(obj, dict):
        for key, value in obj.items():
            reject_private_echoes(key, private_values)
            reject_private_echoes(value, private_values)
    elif isinstance(obj, list):
        for value in obj:
            reject_private_echoes(value, private_values)


class NaverClient:
    def __init__(self):
        self.admission = Admission()
        self.semaphore = asyncio.Semaphore(4)
        self.deadline = 20.0

    async def request(self, owner, route, body):
        if route not in ROUTES:
            raise NaverError('validation')
        self.admission.admit(owner)
        expires = time.monotonic() + self.deadline
        try:
            raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(raw) > MAX_REQUEST_BYTES:
                raise NaverError('validation')
            async with asyncio.timeout(max(0.0, expires - time.monotonic())):
                async with self.semaphore:
                    key = load_key()
                    async with httpx.AsyncClient(
                        trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(10.0, connect=3.0),
                    ) as client:
                        async with client.stream('POST', BASE_URL+route, content=raw, headers={
                            'X-API-Key': key, 'Content-Type': 'application/json', 'Accept': 'application/json',
                        }) as response:
                            status = response.status_code
                            if status != 200:
                                category = {401:'auth',403:'auth',429:'rate',400:'validation',422:'validation',503:'configuration'}.get(status, 'provider')
                                raise NaverError(category)
                            data = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=8192):
                                data.extend(chunk)
                                if len(data) > MAX_BYTES:
                                    raise NaverError('size')
            try:
                result = json.loads(data)
                if type(result) is not dict:
                    raise ValueError()
                check_tree(result)
                reject_private_echoes(result, (key, BASE_URL, 'utility-fastapi-server', '/run/secrets/'))
                if time.monotonic() >= expires:
                    raise NaverError('timeout')
                return result
            except (ValueError, UnicodeError, RecursionError):
                raise NaverError('invalid-response') from None
        except NaverError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise NaverError('timeout') from None
        except (TypeError, ValueError):
            raise NaverError('validation') from None
        except Exception:
            # No exception repr/body/header/path can escape the boundary.
            raise NaverError('provider') from None
