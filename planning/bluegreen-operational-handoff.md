# Optional blue/green operator workflow

This is generic documentation, not a deployment receipt. The helper only manages
its exact `bridge.example.invalid` Caddy stanza and refuses ambiguous layouts.
Read `python planning/bluegreen_rollout.py --help` before use. Review and adapt the
example domain, Caddyfile path, external Docker network, pinned image references,
and source manifests for your installation. Back up the operator-owned Caddyfile
and capture the running configuration before any promotion. Never substitute an
old review manifest for the hashes of a newly sanitized source tree.

The default Compose service and optional blue/green services publish no host
ports. All use the same read-only config mounts and security settings. Prepare
configuration and a verified candidate image before enabling a slot. Monitor
readiness and auth behavior, preserve the previous slot for rollback, and inspect
redacted audit output rather than logging headers, tokens, bodies, or identities.
The repository does not contain historical manifests, runtime receipts, or an
operational baseline. Optional Caddy tests require a caller-provided Caddyfile;
they are not part of the credential-free offline acceptance claim.
