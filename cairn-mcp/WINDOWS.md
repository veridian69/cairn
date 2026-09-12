# Cairn MCP for Windows

The native Windows x64 relay lets Codex launch `cairn-mcp` over STDIO. It needs
no WSL, Python, Go installation or background service. This Windows delivery is
an MCP transport; it does not provide the Linux/WSL `cairn-memory` command or
automatic conversation capture.

## Install

Obtain `cairn-mcp-windows-amd64.zip` from a release source you trust, verify its
published checksum, extract it to a local directory and run these commands in a
normal PowerShell terminal:

```powershell
.\cairn-mcp.exe --help
powershell -NoProfile -ExecutionPolicy Bypass -File .\test-windows.ps1 -BuildDirectory .
powershell -NoProfile -ExecutionPolicy Bypass -File .\test-setup-windows.ps1 -BinaryPath .\cairn-mcp.exe
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1
```

The setup script installs the binary, prompts without echo for missing
Cloudflare Access credentials and restricts the credential and installation
paths. Existing credential values are preserved and validated locally. The
test/setup validation uses a disposable loopback upstream and sends no MCP
request to a live Cairn service.

The credential files are:

```text
%USERPROFILE%\.config\cairn\cf-access-client-id
%USERPROFILE%\.config\cairn\cf-access-client-secret
```

No credentials are included. Use values and an exact proxy-facing MCP URL from
the operator of your Cairn deployment. The reserved example below does not
resolve:

```powershell
codex mcp add cairn -- "$env:LOCALAPPDATA\Programs\Cairn\cairn-mcp.exe" stdio --upstream-url https://cairn.example.invalid/mcp
```

If the `cairn` entry already exists, update `%USERPROFILE%\.codex\config.toml`
and substitute your actual Windows profile path and trusted upstream:

```toml
[mcp_servers.cairn]
command = 'C:\Users\YOUR_USERNAME\AppData\Local\Programs\Cairn\cairn-mcp.exe'
args = ['stdio', '--upstream-url', 'https://cairn.example.invalid/mcp']
```

Restart Codex, then inspect `/mcp`. STDIO does not need a local relay token.
Do not rely on the binary's compiled upstream default.

The binary is unsigned. Compare its SHA-256 with the separately published
release checksum before installation:

```powershell
Get-FileHash .\cairn-mcp.exe -Algorithm SHA256
Get-Content .\SHA256SUMS
```

## Windows permission boundary

Credential validation checks the current process SID, owner and DACL on the
opened file handle. It permits access-granting entries only for the current
user, SYSTEM and built-in Administrators. Null DACLs, unsupported grant types,
final-component reparse points, hard links and non-regular files are rejected.

Setup accepts install paths only on a local Windows volume. It rejects reparse
components, hard-linked destinations, unsafe owners, null DACLs and grants that
allow another non-administrative principal to replace a path component. Use the
default `%LOCALAPPDATA%\Programs\Cairn` directory unless a custom local path has
equivalent protected ancestry. Local administrators and SYSTEM remain able to
replace the relay.

Restart Codex after credential rotation. Unix `SIGHUP` reload and systemd
installation do not apply to STDIO on Windows.

## Build from source

From a native Linux checkout:

```sh
cd cairn-mcp
./scripts/build-windows
```

The package builder records `git rev-parse HEAD` and whether `cairn-mcp/` has
uncommitted changes in `BUILD-INFO.json`. Run it from a Git checkout with an
initial commit; a source export without Git history cannot supply that
provenance. Building from a dirty relay tree is recorded rather than hidden.

The package contains the Windows binary, native validation scripts, test
executables, licences, source hashes and checksums. Building does not install or
register the relay. Native Windows tests use disposable sentinel credentials
and a loopback endpoint; Linux tests alone are not Windows runtime evidence.
