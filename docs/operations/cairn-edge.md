# Cairn edge mapper runbook

## What it is

`cairn-edge` is a small Go service in the `cairn-mcp` tree, the inbound mirror
of the relay. It validates a Cloudflare Access login assertion and maps each
inbound path to its own Cairn workload principal, so that nothing at the edge
becomes a principal silently. It runs in the same Compose stack as Cairn
(overlay `deploy/compose/compose.edge.yaml`), publishes only a loopback host
port, and is reached solely through cloudflared. It fills in nothing: scope,
classification and keys come from the connector's instructions.

## Decision chain

Every request is decided in this order. The first step that refuses ends the
request. No refusal body carries detail about the credential, identity or key.

| Step | Check | Refusal |
| --- | --- | --- |
| 1 | Path is a configured route (`/healthz` is handled separately, below) | `404` for any other path |
| 2 | Method is `POST` (Cairn's MCP mounts are stateless, so there is no `GET` or `DELETE`) | `405` |
| 3 | The edge has finished starting | `503` `not_ready` |
| 4 | `Cf-Access-Jwt-Assertion` header is present | `401` `unauthorised` |
| 5 | The Access key set is available. This is decided while verifying, before any claim is read | `503` `unavailable` (log decision `keys_unavailable`) |
| 6 | Assertion is valid: structure, algorithm, key id, signature, expiry, issuer, audience | `401` `unauthorised` |
| 7 | Identity is the configured email and the token is a user login, not a service token | `403` `forbidden` |
| 8 | Request body can be read and is at most 4 MiB | `400` `bad_request` on a read error, `413` `payload_too_large` over the cap |
| 9 | Forward to the upstream mount with the route's bearer; Cloudflare headers are stripped | `502` on an upstream redirect or failure, `504` on upstream timeout |

`GET /healthz` answers without an assertion (`503` `not_ready` until the edge
has started; any other method on it is `404`). A successful forward streams the
upstream response back unchanged. Every write to the client has a fixed
30-second stall bound, so a client that stops reading is cut off.

The edge does no payload inspection, tool filtering or argument filling, and
holds no sessions or response cache.

## Configuration

`edge.json` is a single JSON object. The file must be a regular file owned by
the user the container runs as, mode `0600`, at most 16 KiB. Unknown fields
and trailing data after the object are refused, so a typo cannot silently
disable a check.

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `team_domain` | yes | none | Lowercase `<team>.cloudflareaccess.com` host name |
| `audience` | yes | none | The Access application's audience tag |
| `allowed_email` | yes | none | The one identity allowed through |
| `upstream_url` | yes | none | The memory MCP mount the edge forwards to |
| `allow_http_upstream` | no | `false` | Permit a plain `http` upstream (loopback or an internal network only) |
| `routes` | yes | none | Map of inbound path to the absolute in-container path of its credential file |
| `read_timeout_seconds` | no | `300` | Upstream response timeout |
| `connect_timeout_seconds` | no | `5` | Upstream connect timeout |
| `jwks_timeout_seconds` | no | `5` | Timeout for fetching the Access keys |
| `jwks_refresh_seconds` | no | `3600` | Key refresh interval |
| `jwks_max_age_seconds` | no | `86400` | How long the last good key set may be used |

Timeouts are optional; when given they must be positive and at most 86400.
Route paths must be clean absolute paths and cannot be `/healthz`. Route
values are absolute paths inside the container, for example
`/var/run/secrets/cairn-edge/claude-ai`. The overlay mounts the secrets
directory read-only at `/var/run/secrets/cairn-edge`.

Each credential file follows the relay's rules: regular file, owner-only mode
`0600`, at most 4,096 bytes, symlinks refused, one trailing newline removed.
No credential appears in an argument, environment variable, log line or error
body.

Check a configuration without serving:

```bash
cairn-edge check --config /etc/cairn-edge/edge.json
```

`cairn-edge serve` listens on `127.0.0.1:8790` by default (`--bind-host`,
`--port`). Changing `edge.json` needs a container restart; only route
credentials reload without one (see Rotation).

## Keys

The Access public keys are fetched from
`https://<team_domain>/cdn-cgi/access/certs` before the server listens. If
that first fetch fails, the edge does not start. After that:

- The set is refreshed on the configured interval (default 1 hour).
- If a refresh fails, the last good set stays in use up to the maximum age
  (default 24 hours). After that it is refused and requests get `503`
  `unavailable`: the edge fails closed.
- A missing or stale set, or an assertion naming an unknown key id, triggers
  at most one refetch per minute.
- The key client never follows redirects. A `3xx` counts as a failed fetch.

## Logging

One line per request: `method route status_class decision duration_ms`.

- `route` is only the matched configured route, `healthz` or `unmapped`. The
  raw request path is never logged.
- `decision` is a fixed code. It is never an identity, token or key id. The
  codes are:
  - Edge: `allow`, `healthz`, `not_found`, `method_not_allowed`, `not_ready`,
    `missing_assertion`.
  - Assertion (401): `malformed`, `algorithm`, `unknown_key`, `signature`,
    `expired`, `not_yet_valid`, `issuer`, `audience`.
  - Identity (403): `identity`, `service_token`.
  - Key set (503): `keys_unavailable`.

  On cutover day, a 403 from the first real assertion is diagnosed by
  `identity` versus `service_token`.

Start, reload and failure messages are fixed strings with no detail.

## Rotation

Replace the route's credential file in the secrets directory (write the new
file, then move it into place so the mode and owner are right), then reload:

```bash
(cd deploy/compose && docker compose --env-file ../images.lock --env-file .env \
  -f compose.yaml -f compose.edge.yaml kill -s HUP cairn-edge)
```

The edge logs `reloaded route credentials`, or `reload failed; previous
credentials kept` and carries on with the old ones. SIGHUP reloads route
credentials only. Create the replacement credential in Cairn first and revoke
the old one after the reload, so there is always one valid credential. If
`scripts/cutover_principals.py` provisioned the route principal, rerun it from
the repository root with the original `--base-url`, `--realm`,
`--credential-file`, `--output-dir` and `--expires-at`, plus
`--label <route-principal-label>` and `--reissue <next-number>`.

## Cutover evidence checklist

The spec's list, each line with what produces it. Cutover-day results are
recorded by the operator.

- [ ] Mapper refuses a forged JWT, an expired JWT, a wrong audience, a wrong
  issuer, a wrong identity and a service-token login, each with the expected
  status and no detail leaked.
  `go test ./internal/edge/ -run TestEdgeRefusals -v` from `cairn-mcp/`. The
  subtests are `forged`, `expired`, `wrong_audience`, `wrong_issuer`,
  `wrong_identity` and `service_token`; the test also covers missing
  assertion, unknown path and wrong method, and asserts no refused request
  reached the upstream. Related tests in `cairn-mcp/internal/edge/app_test.go`:
  `TestEdgeRefusesOversizedBodyBeforeUpstream`,
  `TestEdgeMapsUpstreamRedirectAndTimeout`,
  `TestEdgeAnswers503WhenKeySetIsUnavailable`,
  `TestEdgeLogsOnlyFixedRouteLabels`,
  `TestEdgeForwardsWithBearerAndStripsClientCredentials`.
- [ ] Each browser path lands on its own principal, visible in the audit
  chain.
  Make one candidate write through each connector, then call
  `read-audit-events` and select the events whose `principal_id` is the
  `claude-ai` or `chatgpt` principal. The ids come from
  `scripts/cutover_principals.py`.
- [ ] Each relay lands on its own principal, visible in the audit chain.
  The same `read-audit-events` check, with the `principal_id` of each host's
  relay principal after one `diagnose` through that relay.
- [ ] A candidate-only principal attempting `promote` is refused.
  A `curl` ingest with
  `requested_trust: validated` using the `claude-ai` credential against a
  disposable instance. The expected result is a refusal naming the missing
  promote authority, not `committed`.
- [ ] The claude.ai connector works end to end through the real Access
  application after the ingress change.
  Cutover-day operator step, per [the browser connector guide](browser-connectors.md).
- [ ] The ChatGPT connector works end to end after its redirect URI is added.
  Cutover-day operator step, per the same guide.
- [ ] Rollback rehearsed once on the target host.
  Cutover-day operator step; record the rehearsal.
- [ ] Independent review of the mapper and the relay change by Val, as the
  relay itself had.

The Cloudflare changes these paths depend on (the Access application and the
tunnel ingress rules) are made by the operator in their own account; see
[the Cloudflare prerequisites](browser-connectors.md#cloudflare-prerequisites).
