#!/usr/bin/env python3
"""Cloudflare Access-protected MCP bridge into the existing local Hermes ingress.

The MCP endpoint is deliberately fail-closed until an Access app audience,
issuer, and one exact user email are configured. Never proxy arbitrary URLs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Annotated, Literal
from pydantic import Field
import os
import re
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, ProxyHandler, HTTPRedirectHandler
from hyemi_contracts import canonical_uuid, normalize_request, owner_id, integer
from mcp.server.mcpserver import Context

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from hyemi_ingress import InvalidEnvelope, MAX_CONTENTS, normalize

LOG = logging.getLogger("hermes-bridge-mcp")
MAX_RPC_BODY = 262144
ISSUER_RE = re.compile(r"https://[a-z0-9-]+\.cloudflareaccess\.com")
REPORT_CONTENTS_DESCRIPTION = (
    "Write every report in a concise, messenger-friendly style: lead with the useful outcome, "
    "use short paragraphs and readable lists, and preserve all required detail. "
    "Let each job define its own outline, sections, content, and depth. "
    "Supported report syntax: **bold**, `inline code`, fenced code blocks, "
    "[label](https://original-url) or [label](http://original-url), and raw public URLs. "
    "Preserve original link URLs. Malformed formatting is delivered as safe readable text."
)
REPORT_TOOL_DESCRIPTION = (
    "Deliver the exact report authored by Hyemi, without an agent run. "
    "Telegram is the default. Discord requires an explicit authorized numeric channelid; "
    "the dedicated Hyemi bot delivers reports and delivery metadata identifies both logical authorship and physical sender. "
    "Discord preserves Markdown and makes mention tokens inert in presentation. "
    "Acceptance is not delivery confirmation. " + REPORT_CONTENTS_DESCRIPTION
)


def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text())
    ingress_path = Path(cfg["ingress_config_path"])
    if not ingress_path.is_absolute():
        raise ValueError("ingress config path must be absolute")
    ingress = json.loads(ingress_path.read_text())
    url = cfg.get("ingress_url", "http://172.18.0.1:8651/handoffs")
    if url != "http://172.18.0.1:8651/handoffs":
        raise ValueError("ingress URL must be the fixed local endpoint")
    if not ingress.get("api_key") or not ingress.get("allowed"):
        raise ValueError("missing local ingress configuration")
    cfg["ingress_url"] = url
    cfg["_ingress"] = {key: ingress[key] for key in ("api_key", "allowed")}
    return cfg


class AuthUnavailable(Exception):
    """A retryable dependency/admission failure, never an OAuth challenge."""


class AccessVerifier:
    REQUIRED = ["iss", "aud", "exp", "iat", "nbf", "sub", "email", "type"]

    def __init__(self, auth: dict, jwks_client=None):
        self.issuer = auth.get("issuer")
        self.audience = auth.get("audience")
        self.allowed_emails = {x.casefold() for x in auth.get("allowed_emails", []) if isinstance(x, str)}
        self.allowed_subjects = set(auth.get("allowed_subjects", []))
        self.ready = bool(isinstance(self.issuer, str) and isinstance(self.audience, str) and
                          self.audience and self.allowed_emails and ISSUER_RE.fullmatch(self.issuer))
        self.jwks_client = jwks_client
        if self.ready and self.jwks_client is None:
            self.jwks_client = PyJWKClient(self.issuer + "/cdn-cgi/access/certs", timeout=3, lifespan=3600)

    def _identity(self, claims):
        if any(not isinstance(claims.get(name), str) or not claims[name]
               for name in ("iss", "sub", "email", "type")):
            raise ValueError("not authorized")
        if claims["type"] != "app" or claims["email"].casefold() not in self.allowed_emails:
            raise ValueError("not authorized")
        if self.allowed_subjects and claims["sub"] not in self.allowed_subjects:
            raise ValueError("not authorized")

    def preflight(self, token: str):
        """Reject locally invalid input before key lookup; NEVER grants identity."""
        if not self.ready or not isinstance(token, str) or not 0 < len(token) <= 8192:
            raise ValueError("not authorized")
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
                raise ValueError("not authorized")
            header = jwt.get_unverified_header(token)
            if (header.get("alg") != "RS256" or not isinstance(header.get("kid"), str)
                    or not header["kid"]):
                raise ValueError("not authorized")
            # Preflight validates claims only to reject bad tokens during outages.
            # Only the later signature-verified decode may authenticate a request.
            claims = jwt.decode(token, algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, leeway=30, options={
                                    "require": self.REQUIRED, "verify_signature": False,
                                    "verify_exp": True, "verify_iat": True, "verify_nbf": True,
                                    "verify_iss": True, "verify_aud": True, "verify_sub": True})
            self._identity(claims)
        except (jwt.PyJWTError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
            raise ValueError("not authorized") from exc

    def verify(self, token: str) -> dict:
        self.preflight(token)
        try:
            key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.audience,
                issuer=self.issuer, leeway=30, options={"require": self.REQUIRED},
            )
            self._identity(claims)
            return claims
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthUnavailable("temporarily unavailable") from exc
        except (jwt.PyJWTError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
            raise ValueError("not authorized") from exc

    def warm(self):
        """Use PyJWT's current cache policy, not a sticky readiness boolean."""
        if not self.ready:
            raise AuthUnavailable("temporarily unavailable")
        try:
            keys = self.jwks_client.get_signing_keys()
            if not any(key.algorithm_name == "RS256" and key.key_type == "RSA"
                       and key.key_id and key.key is not None for key in keys):
                raise ValueError("no usable signing keys")
        except (jwt.PyJWTError, ValueError, KeyError, TypeError, OSError) as exc:
            raise AuthUnavailable("temporarily unavailable") from exc


async def completion_bound(awaitable):
    """Run once; defer caller cancellation until non-cancellable work finishes.

    AnyIO shielding alone cannot protect against native Task.cancel(). Keep a
    private asyncio task and shield each wait, including repeated cancellation.
    This is deliberately asyncio-specific, like the bridge's ASGI runtime.
    """
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(awaitable)
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancelled = exc
            except Exception:
                if cancelled is not None:
                    raise cancelled
                raise
        if cancelled is not None:
            raise cancelled
    await anyio.lowlevel.checkpoint()
    return result


async def completion_bound_thread(fn, *args, limiter=None):
    return await completion_bound(anyio.to_thread.run_sync(
        fn, *args, abandon_on_cancel=False, limiter=limiter))


class RequestDrain:
    """One published request, retained by every SDK tool dispatch it starts.

    The SDK may finish/cancel HTTP transport before its separate tool task exits.
    All reference changes occur on the event loop, never in worker threads.
    """
    def __init__(self, guard):
        self.guard = guard
        self.references = 1
        guard.in_flight += 1

    def retain(self):
        if self.references == 0:
            # Do not start side effects after the request has drained.
            raise RuntimeError('request already drained')
        self.references += 1

    def release(self):
        self.references -= 1
        if self.references == 0:
            self.guard.in_flight -= 1


def _audit_auth_reason(exc):
    """Bounded inspection of existing causes only; never decode or stringify."""
    try:
        for _ in range(4):
            for kind, label in ((jwt.ExpiredSignatureError, "expired"),
                                (jwt.InvalidSignatureError, "signature"),
                                (jwt.InvalidAudienceError, "audience"),
                                (jwt.InvalidIssuerError, "issuer")):
                if isinstance(exc, kind):
                    return label
            exc = exc.__cause__
            if exc is None:
                break
    except BaseException:
        # Observability must not replace even cancellation from application code.
        pass
    return "invalid"


class _MCPAudit:
    """One bounded HTTP-lifetime record, not a tool-completion/drain record.

    No request, claims, headers, body, exception or response body is retained.
    null status means no response.start was successfully passed to ASGI send;
    it does not guess a 500 or promise delivery to the remote client.
    """
    def __init__(self, method):
        self.started = time.monotonic()
        self.request_id = uuid.uuid4().hex
        self.method = method if type(method) is str and method in (
            "GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH", "CONNECT", "TRACE") else "OTHER"
        self.status = None
        self.auth_outcome, self.auth_reason = "not_completed", "not_completed"
        self.outcome = "completed"

    def emit(self):
        try:
            record = dict(event="mcp_request",
                          timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                          request_id=self.request_id, method=self.method, path="/mcp",
                          status=self.status,
                          duration_ms=min(999999999999, max(0, round((time.monotonic()-self.started)*1000, 3))),
                          auth_outcome=self.auth_outcome, auth_reason=self.auth_reason,
                          outcome=self.outcome)
            LOG.info(json.dumps(record, separators=(",", ":"), allow_nan=False))
        except BaseException:
            # No fallback log (which could also fail or disclose the exception).
            pass


class GuardedApp:
    """JWT gate and drain accounting for every MCP HTTP request.

    /readyz is an internal listener endpoint; the public proxy must expose only
    /mcp. Its payload intentionally excludes identities and dependency details.
    """
    def __init__(self, app, verifier: AccessVerifier, auth_workers: int = 4):
        if type(auth_workers) is not int or auth_workers < 1:
            raise ValueError("auth_workers must be a positive integer")
        self.app, self.verifier = app, verifier
        self.in_flight = 0
        self._auth_workers = auth_workers
        self._auth_active = 0
        self._auth_limiter = anyio.CapacityLimiter(auth_workers)

    async def _auth_work(self, fn, *args):
        # No await between admission check and increment: no unbounded queue.
        if self._auth_active >= self._auth_workers:
            raise AuthUnavailable("temporarily unavailable")
        self._auth_active += 1
        try:
            return await completion_bound_thread(fn, *args, limiter=self._auth_limiter)
        finally:
            self._auth_active -= 1

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == "/livez":
            return await self.respond(send, 200, {"status": "alive"})
        if scope.get("path") == "/healthz":
            return await self.respond(send, 200 if self.verifier.ready else 503,
                                      {"status": "ready" if self.verifier.ready else "locked"})
        if scope.get("path") == "/readyz":
            try:
                await self._auth_work(self.verifier.warm)
            except AuthUnavailable:
                return await self.respond(send, 503, {"status": "not_ready", "in_flight": self.in_flight})
            return await self.respond(send, 200, {"status": "ready", "in_flight": self.in_flight})
        if scope.get("path") != "/mcp":
            return await self.respond(send, 404, {"status": "not_found"})
        # Audit setup is best effort and performs no authentication work.
        try:
            audit = _MCPAudit(scope.get("method"))
        except BaseException:
            audit = None
        original_send = send

        async def audited_send(message):
            await original_send(message)
            try:
                if audit is not None and audit.status is None and message.get("type") == "http.response.start":
                    status = message.get("status")
                    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                        audit.status = int(status)
            except BaseException:
                pass

        send = audited_send
        drain = RequestDrain(self)
        scope.setdefault("state", {})["request_drain"] = drain
        try:
            if not self.verifier.ready:
                if audit is not None:
                    audit.auth_outcome, audit.auth_reason = "unavailable", "unavailable"
                return await self.respond(send, 503, {"status": "not_configured"})
            headers = dict(scope.get("headers", []))
            assertion = headers.get(b"cf-access-jwt-assertion", b"")
            try:
                token = assertion.decode("ascii")
                self.verifier.preflight(token)
                claims = await self._auth_work(self.verifier.verify, token)
                # Deliver deferred scope cancellation before entering any handler.
                await anyio.lowlevel.checkpoint()
            except AuthUnavailable:
                if audit is not None:
                    audit.auth_outcome, audit.auth_reason = "unavailable", "unavailable"
                return await self.respond(send, 503, {"status": "temporarily_unavailable"})
            except (ValueError, UnicodeDecodeError) as exc:
                if audit is not None:
                    audit.auth_outcome = "rejected"
                    audit.auth_reason = _audit_auth_reason(exc) if assertion else "missing"
                return await self.respond(send, 401, {"status": "unauthorized"})
            if audit is not None:
                audit.auth_outcome, audit.auth_reason = "accepted", "verified"
            scope.setdefault("state", {})["principal_sub"] = claims["sub"]
            scope["state"]["principal_issuer"] = claims["iss"]
            scope["state"]["principal_verified"] = True
            return await self.app(scope, receive, send)
        except asyncio.CancelledError:
            if audit is not None:
                audit.outcome = "cancelled"
            raise
        except BaseException:
            if audit is not None:
                audit.outcome = "failed"
            raise
        finally:
            drain.release()
            if audit is not None:
                audit.emit()

    @staticmethod
    async def respond(send, status, body):
        raw = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
                   (b"cache-control", b"no-store")]
        if status == 503:
            headers.append((b"retry-after", b"3"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": raw})


def dispatch(cfg: dict, event_id: str, intent: str, contents: str,
             to: str = "hyeji", channelid: str | None = None) -> dict:
    """Validate against the existing ingress allowlist; send only to its local URL."""
    if not isinstance(contents, str) or len(contents.encode("utf-8")) > MAX_CONTENTS:
        raise ValueError("contents must be text within the ingress limit")
    obj = {"version": 1, "event_id": event_id, "from": "hyemi", "to": to,
           "intent": intent, "contents": contents}
    if channelid is not None:
        obj["channelid"] = channelid
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        normalized, _, _ = normalize(raw, cfg["_ingress"]["allowed"])
    except InvalidEnvelope as exc:
        raise ValueError(str(exc)) from exc
    # Use the canonical envelope so a repeated event_id has the same digest.
    raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = Request(cfg["ingress_url"], data=raw, method="POST", headers={
        "Content-Type": "application/json", "X-API-Key": cfg["_ingress"]["api_key"],
        "User-Agent": "HermesBridgeMCP/1.0"})
    try:
        with urlopen(req, timeout=17) as response:
            code, result = response.status, json.load(response)
    except HTTPError as exc:
        LOG.warning("ingress rejected event=%s code=%s", event_id, exc.code)
        raise RuntimeError("local handoff rejected") from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        LOG.warning("ingress failed event=%s kind=%s", event_id, type(exc).__name__)
        raise RuntimeError("local handoff unavailable") from None
    if code not in (200, 202) or result.get("status") not in ("accepted", "duplicate"):
        raise RuntimeError("unexpected local handoff response")
    return {"status": result["status"], "event_id": event_id, "to": normalized["to"],
            "intent": normalized["intent"],
            "channelid": normalized["channelid"], "note": "Accepted for Hermes processing; not a final Telegram delivery receipt."}

def get_result(cfg: dict, event_id: str) -> dict:
    """Fetch one previously accepted event's delivered reply; never a generic search."""
    try:
        if not isinstance(event_id, str) or str(uuid.UUID(event_id)) != event_id:
            raise ValueError("noncanonical UUID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("event_id must be canonical UUID") from exc
    url = "http://172.18.0.1:8651/handoffs/" + event_id + "/result"
    req = Request(url, method="GET", headers={
        "X-API-Key": cfg["_ingress"]["api_key"], "User-Agent": "HermesBridgeMCP/1.0"})
    try:
        with urlopen(req, timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return {"status": "not_found", "event_id": event_id}
        LOG.warning("result lookup rejected event=%s code=%s", event_id, exc.code)
        raise RuntimeError("handoff result unavailable") from None
    except (URLError, TimeoutError, OSError, ValueError):
        LOG.warning("result lookup failed event=%s", event_id)
        raise RuntimeError("handoff result unavailable") from None


def principal_owner(ctx: Context) -> str:
    """Use only guard-verified ASGI state, never client headers or tool arguments."""
    try:
        state = ctx.request_context.request.scope['state']
        if state.get('principal_verified') is not True: raise ValueError()
        return owner_id(state['principal_issuer'], state['principal_sub'])
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PermissionError('authenticated principal required') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


LOCAL_OPENER = build_opener(ProxyHandler({}), NoRedirect())
MAX_RESPONSE = 1048576


def v2_request(cfg, owner, method, path, body=None):
    """Fixed-origin, bounded private transport. Identity is supplied by the guard."""
    if not re.fullmatch(r'[0-9a-f]{64}', owner): raise PermissionError('unauthorized')
    if not (path.startswith('/v2/') or re.fullmatch(r'/handoffs/[0-9a-f-]+/result', path)):
        raise ValueError('invalid route')
    raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    req = Request('http://172.18.0.1:8651'+path, data=raw, method=method, headers={
        'Content-Type':'application/json', 'X-API-Key':cfg['_ingress']['api_key'],
        'X-Hyemi-Owner':owner, 'User-Agent':'HermesBridgeMCP/2.0'})
    try:
        with LOCAL_OPENER.open(req, timeout=17) as response:
            data = response.read(MAX_RESPONSE+1)
            if len(data)>MAX_RESPONSE: raise ValueError()
            result = json.loads(data.decode('utf-8'))
            if type(result) is not dict: raise ValueError()
            result.pop('session_id', None)
            return result
    except HTTPError as exc:
        if exc.code == 404: return {'status':'not_found'}
        raise RuntimeError('local workflow rejected') from None
    except (URLError, TimeoutError, OSError, ValueError, RecursionError):
        raise RuntimeError('local workflow unavailable') from None


def submit_v2(cfg, owner, kind, event_id, work_id, contents, to='hyeji', channelid=None, **fields):
    recipient = 'hyerin' if to == 'hyelin' else to
    if channelid is None and fields.get('platform', 'telegram') == 'telegram':
        channelid = cfg['_ingress']['allowed'].get(recipient, {}).get('home')
    body = dict(version=2, kind=kind, event_id=event_id, work_id=work_id,
                contents=contents, to=recipient, channelid=channelid)
    body.update({k:v for k,v in fields.items() if v is not None})
    body = normalize_request(body, cfg['_ingress']['allowed'])
    return v2_request(cfg, owner, 'POST', '/v2/requests', body)


def lookup_v2(cfg, owner, category, identifier, suffix='', after_seq=0, limit=50):
    canonical_uuid(identifier)
    path = '/v2/'+category+'/'+identifier+suffix
    if suffix == '/updates':
        integer(after_seq,'after_seq'); integer(limit,'limit',1,100)
        path += '?after_seq='+str(after_seq)+'&limit='+str(limit)
    result = v2_request(cfg, owner, 'GET', path)
    if category == 'events' and suffix == '/result' and result.get('status') == 'not_found':
        if cfg.get('legacy_owner_id') == owner:
            return v2_request(cfg, owner, 'GET', '/handoffs/'+identifier+'/result')
        return dict(status='not_found', event_id=identifier, legacy_access='unassigned')
    return result


class StrictMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        # MCP 2.0.0 _handle_call_tool constructs Context from the HTTP request
        # and calls self.call_tool before ToolManager dispatches *any* tool.
        # Retain here, not in individual handlers: SDK tasks can outlive HTTP.
        from mcp.types import CallToolResult, TextContent
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            try:
                tool.fn_metadata.arg_model.model_validate(arguments, strict=True, extra='forbid')
            except (ValueError, TypeError):
                return CallToolResult(isError=True, content=[TextContent(type='text', text='invalid tool arguments')])
        try:
            drain = context.request_context.request.scope['state']['request_drain']
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PermissionError('authenticated request required') from None
        if not isinstance(drain, RequestDrain):
            raise PermissionError('authenticated request required')
        # Atomic with dispatch on the event loop; cannot resurrect a zero lease.
        drain.retain()
        try:
            operation = super().call_tool(name, arguments, context)
            if tool is not None and not tool.is_async:
                # The SDK offloads sync tool functions with AnyIO run_sync.
                # Native Task.cancel can otherwise abandon that worker thread.
                return await completion_bound(operation)
            # Async operations retain normal cancellation/timeout semantics.
            # Their finally/aclose runs before our ownership is released.
            return await operation
        finally:
            drain.release()


def build_app(cfg: dict, verifier: AccessVerifier | None = None):
    verifier = verifier or AccessVerifier(cfg.get("auth", {}))
    server = StrictMCPServer("hermes-bridge", description="Authenticated bridge to Hermes agents", version="2.0.0")

    async def invoke(ctx, fn, *args, **kwargs):
        owner = principal_owner(ctx)
        from functools import partial
        try:
            # Shared dispatch owns the lease; this async adapter still waits
            # for its real synchronous backend even under native cancellation.
            return await completion_bound_thread(partial(fn, cfg, owner, *args, **kwargs))
        except ValueError:
            raise ValueError('invalid workflow request') from None
        except Exception as exc:
            LOG.warning('workflow request failed kind=%s', type(exc).__name__)
            raise RuntimeError('workflow unavailable') from None

    @server.tool(description=REPORT_TOOL_DESCRIPTION)
    async def deliver_report(event_id: str, work_id: str,
                             contents: Annotated[str, Field(description=REPORT_CONTENTS_DESCRIPTION)], ctx: Context,
                             to: str = 'hyeji', channelid: str | None = None,
                             title: str | None = None, revision: int | None = None,
                             platform: Literal['telegram', 'discord'] = 'telegram') -> dict:
        return await invoke(ctx, submit_v2, 'report', event_id, work_id, contents, to, channelid, title=title, revision=revision, platform=platform)

    @server.tool(description='Request a criteria-based review of one revision; preserve work_id across review iterations. The recipient performs the review; Discord notifications are sent by the dedicated Hyemi bot.')
    async def request_review(event_id: str, work_id: str, contents: str, revision: int,
                             criteria: str, ctx: Context, to: str = 'hyeji',
                             channelid: str | None = None, title: str | None = None,
                             changes: str | None = None, previous_event_id: str | None = None,
                             platform: Literal['telegram', 'discord'] = 'telegram') -> dict:
        return await invoke(ctx, submit_v2, 'review', event_id, work_id, contents, to, channelid,
                            revision=revision, criteria=criteria, title=title, changes=changes, previous_event_id=previous_event_id, platform=platform)

    @server.tool(description='Delegate a task with explicit acceptance criteria. The recipient performs the task; Discord notifications are sent by the dedicated Hyemi bot. Accepted does not mean completed.')
    async def request_task(event_id: str, work_id: str, contents: str, acceptance_criteria: str,
                           ctx: Context, to: str = 'hyeji', channelid: str | None = None,
                           title: str | None = None,
                           platform: Literal['telegram', 'discord'] = 'telegram') -> dict:
        return await invoke(ctx, submit_v2, 'task', event_id, work_id, contents, to, channelid,
                            acceptance_criteria=acceptance_criteria, title=title, platform=platform)

    @server.tool(description='Read owner-scoped work execution status, availability, and outcome.')
    async def get_work_status(work_id: str, ctx: Context) -> dict:
        return await invoke(ctx, lookup_v2, 'works', work_id)

    @server.tool(description='Read owner-scoped incremental work updates. This records an authenticated lookup, not a human read receipt.')
    async def get_work_updates(work_id: str, ctx: Context, after_seq: int = 0, limit: int = 50) -> dict:
        return await invoke(ctx, lookup_v2, 'works', work_id, '/updates', after_seq, limit)

    @server.tool(description='Read the owner-scoped result and delivery states; inspect outcome separately from execution completion.')
    async def get_handoff_result(event_id: str, ctx: Context) -> dict:
        return await invoke(ctx, lookup_v2, 'events', event_id, '/result')

    @server.tool(description='Read the exact owner-scoped report retained without model processing.')
    async def get_report(event_id: str, ctx: Context) -> dict:
        return await invoke(ctx, lookup_v2, 'events', event_id, '/report')

    @server.tool(description='Legacy task compatibility. For reports migrate to deliver_report; for reviews use request_review.')
    async def send_handoff(event_id: str, intent: str, contents: str, ctx: Context,
                           to: str = 'hyeji', channelid: str | None = None) -> dict:
        principal_owner(ctx)
        canonical_uuid(event_id)
        if intent == 'report':
            return {'status':'migration_required', 'tool':'deliver_report',
                    'note':'Supply event_id and work_id to deliver_report for exact Hyemi delivery.'}
        if intent != 'task': raise ValueError('invalid intent')
        work_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'hyemi-legacy-task:'+event_id))
        return await invoke(ctx, submit_v2, 'task', event_id, work_id, contents, to, channelid,
                            acceptance_criteria='Complete the requested task and report its outcome.')

    from naver_tools import register_naver_tools
    register_naver_tools(server, cfg, principal_owner)

    # Advertise the same closed schema enforced before SDK argument parsing.
    for tool in server._tool_manager.list_tools():
        tool.parameters['additionalProperties'] = False

    hosts = cfg.get("allowed_hosts", ["bridge.example.invalid", "172.18.0.1:8652"])
    if not hosts or any("*" in host or "/" in host for host in hosts):
        raise ValueError("MCP host allowlist must contain exact hostnames")
    inner = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                       json_response=True, max_request_body_size=MAX_RPC_BODY,
                                       transport_security=TransportSecuritySettings(
                                           allowed_hosts=hosts,
                                           allowed_origins=["https://chatgpt.com"]),
                                       host=cfg.get("listen", ["172.18.0.1", 8652])[0])
    return GuardedApp(inner, verifier)


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(os.environ.get("HERMES_BRIDGE_MCP_CONFIG", "/root/.config/hermes-bridge-mcp/config.json"))
    host, port = cfg.get("listen", ["172.18.0.1", 8652])
    if host not in ("172.18.0.1", "127.0.0.1") and not (
        host == "0.0.0.0" and os.environ.get("HERMES_BRIDGE_CONTAINERIZED") == "1"
    ):
        raise ValueError("MCP listener must use a local/bridge address or an isolated container")
    app = build_app(cfg)
    LOG.info("Hermes bridge MCP listening on %s:%s (auth ready=%s)", host, port, app.verifier.ready)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
