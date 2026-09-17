# Cairn MCP relay operations

`cairn-mcp` is a local trust-boundary relay. HTTP mode binds only to
`127.0.0.1:8765`, checks a local bearer token, injects a Cloudflare Access
client ID and secret, and forwards MCP Streamable HTTP traffic to an HTTPS
upstream. STDIO mode starts one process per client and does not use the local
bearer token.

See the [relay README](../../cairn-mcp/README.md) for build and command details
and the [client guide](../clients.md#mcp-clients) for MCP semantics.

## Prepare credentials

Create these files before installation:

```text
$HOME/.config/cairn/cf-access-client-id
$HOME/.config/cairn/cf-access-client-secret
```

Each file must be non-empty, owned by the current user, mode `0600`, at most
4,096 bytes and not a symlink. Keep credential values out of environment
variables, command arguments, unit files, logs and Git.

```sh
client_id_source=/secure/path/client-id
client_secret_source=/secure/path/client-secret
install -d -m 0700 "$HOME/.config/cairn"
install -m 0600 "$client_id_source" \
  "$HOME/.config/cairn/cf-access-client-id"
install -m 0600 "$client_secret_source" \
  "$HOME/.config/cairn/cf-access-client-secret"
```

## Install

From `cairn-mcp/` in a trusted checkout:

```sh
./scripts/install-user --no-start
```

This builds and checks the source, installs the binary at
`$HOME/.local/share/bin/cairn-mcp`, installs the user unit and creates
`$HOME/.config/cairn/relay-token` only when absent. The token has mode `0600`.
`--no-start` neither enables nor starts the service.

## Configure a client

Pass the operator's HTTPS endpoint explicitly. TOML does not expand shell
variables, so use the absolute binary path returned by `realpath`.

### STDIO, recommended

```toml
[mcp_servers.cairn]
command = "/absolute/path/to/cairn-mcp"
args = ["stdio", "--upstream-url", "https://cairn.example/mcp"]
```

STDIO reads the Cloudflare files directly and exits when its client's stdin
closes. Restart the client after replacing either credential so a new process
loads the complete pair.

### HTTP with a user service

The installed unit runs `cairn-mcp serve`. Configure its endpoint with a
systemd user drop-in so it does not rely on a built-in default:

```ini
[Service]
ExecStart=
ExecStart=%h/.local/share/bin/cairn-mcp serve --upstream-url https://cairn.example/mcp
```

Then reload the user manager and start the unit:

```sh
systemctl --user daemon-reload
systemctl --user enable --now cairn-mcp.service
export CAIRN_MCP_LOCAL_TOKEN="$(<"$HOME/.config/cairn/relay-token")"
```

Configure the client:

```toml
[mcp_servers.cairn]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "CAIRN_MCP_LOCAL_TOKEN"
startup_timeout_sec = 2.0
```

Keep the token out of TOML. Launch the client from the shell holding the
environment variable.

## Validate and operate

```sh
"$HOME/.local/share/bin/cairn-mcp" check \
  --upstream-url https://cairn.example/mcp
systemctl --user status cairn-mcp.service --no-pager
curl -fsS http://127.0.0.1:8765/healthz
```

`/healthz` checks only the local process. An authorised MCP `initialize`
followed by `get_status` is the end-to-end check.

The HTTP service reloads all three credential files on `SIGHUP` as one
validated snapshot. Stage replacement files in the same directory, set mode
`0600`, rename both Cloudflare files into place, run `check` with the configured
upstream URL, then reload:

```sh
systemctl --user reload cairn-mcp.service
journalctl --user -u cairn-mcp.service --since "5 minutes ago" --no-pager
```

Require `credentials reloaded`. A rejected reload leaves the previous complete
snapshot active. Rotating `relay-token` also requires a new client process
with the replacement environment value.

Restart after changing the binary, unit or drop-in:

```sh
systemctl --user restart cairn-mcp.service
```

## Troubleshooting

Use observations that do not expose secrets:

```sh
systemctl --user status cairn-mcp.service --no-pager
journalctl --user -u cairn-mcp.service --since "15 minutes ago" --no-pager
ss -ltn '( sport = :8765 )'
curl -fsS http://127.0.0.1:8765/healthz
"$HOME/.local/share/bin/cairn-mcp" check \
  --upstream-url https://cairn.example/mcp
```

| Symptom | Action |
| --- | --- |
| `configuration invalid` | Check path, ownership, regular-file type, exact mode `0600` and non-empty size. |
| `credential reload rejected` | Correct all files and reload again; the old snapshot remains active. |
| `/healthz` fails | Inspect the unit, restart loop and ownership of port 8765. |
| `/healthz` succeeds but MCP fails | Check client-token freshness, Cloudflare Access and the upstream endpoint. |
| HTTP 401 from loopback | Reload the shell token from its file and restart the client. |
| STDIO exits with `stdio error` | Validate both Cloudflare files and upstream reachability. |

Do not use `set -x`, dump process environments, run verbose requests with
bearer headers, or paste logs containing request headers or bodies.

## Uninstall

```sh
systemctl --user disable --now cairn-mcp.service
rm -f "$HOME/.config/systemd/user/cairn-mcp.service"
rm -f "$HOME/.local/share/bin/cairn-mcp"
systemctl --user daemon-reload
systemctl --user reset-failed cairn-mcp.service 2>/dev/null || true
```

This preserves `$HOME/.config/cairn/`. Revoke credentials before securely
deleting their local files, and remove the client configuration separately.
