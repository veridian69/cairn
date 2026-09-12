# Cairn MCP relay

`cairn-mcp` bridges MCP clients through a Cloudflare Access boundary. It
supports a per-client STDIO transport and an optional persistent HTTP relay on
`127.0.0.1:8765/mcp`.

The direct Cairn service already exposes Streamable HTTP. If a client can reach
that endpoint and hold both its Cairn bearer credential and any edge
credentials safely, it does not need this relay.

## Choose the upstream explicitly

Use the exact proxy-facing MCP URL supplied by the operator of your Cairn
deployment. Public examples deliberately use a non-resolving reserved name:

```text
--upstream-url https://cairn.example.invalid/mcp
```

Do not rely on the binary's compiled default. The URL must be absolute HTTP(S)
and contain no embedded credentials, query or fragment. HTTPS is required by
default. Plain HTTP also needs `--allow-http-upstream`; reserve it for a trusted
numeric-loopback test endpoint such as `http://127.0.0.1:8000/v1/mcp`.

## Build and test

From `cairn-mcp/` with Go installed:

```sh
go build ./cmd/cairn-mcp
go test ./...
```

These commands do not contact an upstream service. Native Windows users can
instead install the packaged build using [Windows setup](WINDOWS.md).

## Credential files

The relay reads Cloudflare Access service credentials from:

```text
$HOME/.config/cairn/cf-access-client-id
$HOME/.config/cairn/cf-access-client-secret
```

Each must be a non-empty regular file owned by the current user, mode `0600`,
no larger than 4,096 bytes and not a symlink. Install values from an approved
secret source without placing them in arguments, environment variables, logs
or Git:

```sh
install -d -m 0700 "$HOME/.config/cairn"
install -m 0600 /path/to/client-id "$HOME/.config/cairn/cf-access-client-id"
install -m 0600 /path/to/client-secret "$HOME/.config/cairn/cf-access-client-secret"
```

## STDIO mode

STDIO is the simplest option for one MCP client. Configure the client to launch
the relay with the intended upstream. For Codex:

```toml
[mcp_servers.cairn]
command = "/absolute/path/to/cairn-mcp"
args = ["stdio", "--upstream-url", "https://cairn.example.invalid/mcp"]
```

STDIO reads the Cloudflare files directly and exits when its standard input
closes. It does not use the HTTP relay token.

## Persistent HTTP mode

The installer builds the binary at `$HOME/.local/share/bin/cairn-mcp`, creates
an owner-only local relay token and installs a systemd user unit:

```sh
./scripts/install-user --no-start
```

Before starting it, create an override with the exact trusted upstream:

```ini
[Service]
ExecStart=
ExecStart=%h/.local/share/bin/cairn-mcp serve --upstream-url https://cairn.example.invalid/mcp
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now cairn-mcp.service
export CAIRN_MCP_LOCAL_TOKEN="$(<"$HOME/.config/cairn/relay-token")"
```

Configure Codex in the same environment:

```toml
[mcp_servers.cairn]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "CAIRN_MCP_LOCAL_TOKEN"
startup_timeout_sec = 2.0
```

The relay token protects the loopback relay; it is not a Cairn bearer
credential.

## Checks and operation

```sh
"$HOME/.local/share/bin/cairn-mcp" check --upstream-url https://cairn.example.invalid/mcp
curl --silent --show-error --fail http://127.0.0.1:8765/healthz
systemctl --user restart cairn-mcp.service
journalctl --user -u cairn-mcp.service --since "15 minutes ago" --no-pager
```

`/healthz` proves only that the local relay is running. An authorised MCP
`initialize` followed by `tools/list` checks the complete configured route and
contacts its upstream.

All timeout flags accept a finite number of seconds greater than zero. Run
`cairn-mcp --help` for their names and defaults. The relay binds only
`127.0.0.1`, forwards only MCP traffic, keeps credentials out of logs and
reloads a complete validated credential snapshot on `SIGHUP`.
