# Hermes Bridge MCP

A source-only Python MCP bridge with a separate private workflow executor.
Logical recipient labels `hyeji` and `hyerin` are retained for compatibility;
`hyemi` identifies the dedicated delivery sender. No personal deployment,
credentials, runtime data, private Git history, or production receipts are included.
No software license has been selected; this snapshot does not grant a license.

## Included behavior

- Streamable HTTP MCP endpoint `/mcp`, with strict tool schemas and owner-scoped
  work/report/result/update lookups; task, review, report, and legacy handoff tools.
- Cloudflare Access JWT verification using RS256, issuer/audience/time/identity
  checks and email/optional subject allowlists. Missing configuration fails closed.
  Readiness is distinct from liveness. Reverse proxy routing is not authentication.
- Private ingress, durable SQLite workflow ledger, execution/delivery state,
  retry/reconciliation helpers, messenger formatting, Telegram and Discord delivery.
- Naver search and trend tools through a separately operated utility API, with
  validated evidence envelopes, date buckets and pagination constraints.
- Optional operator canaries and narrowly scoped blue/green Caddy helpers.

A successful submission means accepted, not delivered or completed. A status
lookup is not proof that a human read the result. Discord reports retain logical
authorship separately from the physical dedicated Hyemi bot identity.

## Source layout and prerequisites

`bridge.py` is the public MCP process. `hyemi_server.py` runs the private executor;
`hyemi_ingress.py` is the vendored shared ingress implementation. The other
`hyemi_*.py` modules implement contracts, storage, workers, reviews and delivery.
`naver_*.py` implements the utility API adapter. `planning/` contains reusable
operator helpers, not evidence of a live deployment.

Use Python 3.11 and install `requirements-dev.txt` for the test suite, or
`requirements.txt` for the public bridge. The Docker image contains source modules
and public-bridge dependencies only. Review execution additionally requires a
separately installed, compatible Hermes Agent runtime (`agent.auxiliary_client`,
`hermes_cli`, its runtime-provider integration and dependencies); it is NOT
included in this repository/image. Task execution requires the Hermes local
API run/status implementation. Messenger accounts, bot permissions, the utility
API, Cloudflare Access application/JWKS and reverse proxy are also external.
No blank example establishes real OAuth or live delivery acceptance.

## Configuration and isolation

Copy the templates in `examples/` to operator-owned files outside the checkout,
fill real values privately, set restrictive permissions, and review every policy.
They are deliberately nonfunctional templates. Never commit filled configuration.
The root `.env.example` contains no credentials. `.env` and all `.env.*` other than
that one example are excluded.

The bridge reads `HERMES_BRIDGE_MCP_CONFIG`; private ingress reads
`HYEMI_INGRESS_CONFIG`. Their standard root installation defaults are
`/root/.config/hermes-bridge-mcp/config.json` and
`/root/.config/hyemi-ingress/config.json`. Root Hermes profile defaults are
`/root/.hermes` and `/root/.hermes/profiles/hyerin`; these are product integration
paths, not included host files. The fixed Discord reader requires the exact
`HYEMI_DISCORD_TOKEN` key in `/root/.hermes/.env`, owned by root with mode 0600.
It does not accept arbitrary credential paths. Telegram sender readers and
Hermes API workers have separate credential contracts; inspect `hyemi_delivery.py`
and `hyemi_workers.py`. Do not weaken these checks to make examples run.

Discord guild, sender and channel numbers in this snapshot are synthetic.
`hyemi_contracts.py` deliberately pins guild and dedicated sender identities;
reconfigure those reviewed constants and the private per-recipient policy together
for an installation. Authorization still fails closed on mismatches. Callers must
choose `platform="discord"` explicitly and provide an authorized numeric
`channelid`; there is no inferred Discord route. Telegram is the default platform
and uses its own recipient channel policy. Preserve canonical work/event UUIDs.

The private ingress endpoint remains fixed at `172.18.0.1:8651`; this is an
explicit Docker gateway topology contract, not an arbitrary configurable upstream.
The private service also permits loopback binding. Host Hermes APIs are loopback
ports 8662/8663. Review all fixed endpoints before deployment, and keep the ingress
firewalled from public clients. Public MCP service port is 8652.

## Docker deployment template

`compose.yaml` retains one primary service and opt-in blue/green slots. It uses
an operator-created external `hermes-bridge-edge` network, no published ports,
read-only config mounts under `/opt/hermes-bridge`, dropped capabilities and
no-new-privileges. Install the proxy separately on the shared network and ensure
its gateway matches the private ingress topology. Do not run Compose until the
mounts, network and auth policies have been prepared. Existing image labels are
compatibility labels for the operator helpers, not proof of deployment or freshness;
build and pin your own reviewed image digest before promotion.

```sh
python -m pip install -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -m unittest discover -q
docker compose config --quiet
docker build -t hermes-bridge-mcp:local .
```

Only the bridge starts by default. The private executor belongs in the separately
configured Hermes host environment; never mount entire Hermes profiles into the
public bridge container. The bridge only needs its publication subset of ingress
configuration and a separate utility API key when that adapter is enabled.

## Experimental Discord delivery limitation

Discord delivery remains experimental; live provider acceptance is not established
by this snapshot. New Discord rendering plans protect whitespace at chunk edges
with presentation-only U+200B guards and reserve space for those guards within the
message limit. Raw source/report/result text, Telegram rendering and already-frozen
legacy delivery plans remain unchanged. Literal source U+200B characters are not
removed. Offline tests include an edge-whitespace-normalizing provider model.

Strict exact-content POST response and independent GET readback checks remain
required; guards do not authorize relaxed comparisons. A mismatch can leave a
chunk unknown/unverified with later chunks pending. Inspect and reconcile safely;
do not blindly resend or infer success from a visible message. No complete live
Discord, real OAuth-client, or human-UI acceptance is claimed.

## Verification scope

The default unittest discovery command above runs offline unit groups using only
`requirements-dev.txt` and this public checkout; no installed Hermes runtime is
needed. Leave all integration/canary opt-ins unset for this dependency-only run.
The suite exercises synthetic HTTP/JWT/provider fixtures, not real provider
responses. The six Naver date-bucket envelopes are explicitly synthetic and were
constructed from the local schema; they are not captured API evidence.

The real host import/plugin fence test is a separate integration gate, skipped by
default. It requires a compatible Hermes runtime and its dependencies installed at
the `PYTHON` and `SOURCE` locations defined in `hyemi_review.py`. From this checkout,
run only that test with its explicit opt-in:

```sh
HYEMI_HOST_RUNTIME_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  python -m unittest -v test_review_executor.ReviewTests.test_runtime_import_fence_in_isolated_process
```

This uses the real host interpreter and Hermes plugin module, while testing this
checkout's review entrypoint. It verifies forbidden imports and plugin discovery
are denied; it does not run the backend, read provider/profile configuration or
credentials, or send provider requests. Its opt-in does not enable live canaries.
A passing host integration test is not dependency-only unit-test evidence.

Caddy probes (`AUDIT1_CADDY_PROBE=1`) require the separately installed Docker/Caddy
probe environment; live review (`HYEMI_LIVE_REVIEW_CANARY=1`) requires a configured
Hermes provider environment and separately authorized provider access. These remain
distinct opt-ins, skipped by default; do not enable them for the host fence test.
Neither passing tests nor an image build proves public-client OAuth, actual
messenger transport or human UI delivery. Perform separately authorized external
canaries and independent message readbacks before production acceptance. Do not
run operator canaries against an existing service merely to test this snapshot.

Sanitization changes identifiers, deployment examples and fixtures, so these bytes
must receive their own review; they are not the exact bytes of any prior source
approval or deployed image. Private provenance and audit reports are intentionally
not published in this source tree.
