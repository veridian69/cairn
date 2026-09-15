# macOS foreground installation

Cairn can run a local, disposable server in the foreground on macOS without
systemd or a Docker daemon. This mode provides the catalogue-backed
`/memory/v1` API and exact Attic evidence storage on numeric loopback. It does
not enable semantic graph search and needs no model-provider credential.

The foreground implementation passed hosted acceptance on macOS 26.6.1 Intel
and macOS 26.6.2 Apple Silicon, with Python 3.14.7. The measured scope is
described below; macOS 12.7.6 is unverified.

## Requirements

- An ordinary non-root macOS account.
- A trusted Cairn checkout on a local filesystem.
- Host Python 3.12–3.14. Cairn's managed runtime uses Python 3.14.
- `uv 0.12.14` and network access to the locked Python package sources during
  installation.
- An unused numeric-loopback port.

The Intel installation builds locked `cryptography 50.0.0` from source; that
path passed on the hosted Intel runner. It requires Apple Xcode Command Line
Tools, Rust and OpenSSL development libraries. With Homebrew already installed,
the upstream prerequisites are:

```sh
xcode-select --install
brew install openssl@3 rust
```

See [cryptography 50.0.0 macOS build instructions](https://cryptography.io/en/50.0.0/installation/#building-cryptography-on-macos).
Allow several minutes for the Intel source build. Apple Silicon uses the locked
prebuilt package. Dependency versions remain identical on both architectures.

## Verify and keep the server running

From the checkout root:

```sh
./cairn-install install \
  --non-interactive \
  --mode disposable \
  --name local-memory \
  --port 8000 \
  --keep-running
```

The installer prepares its locked private runtime, bootstraps a local realm,
starts Cairn, verifies authenticated identity and exact Attic bytes, restarts
the server, and checks the retained data. Once it records `Status: verified`,
it keeps the owned child process open until Ctrl-C.

Press Ctrl-C once for a normal shutdown. The installer stops and reaps the
server before releasing the instance lock. It retains the verified data,
credential and transcript. The endpoint is then no longer listening.

Resume the same verified instance and hold it open again with:

```sh
./cairn-install resume \
  --non-interactive \
  --name local-memory \
  --keep-running
```

Resume checks the retained ingest receipt and reads before entering the hold;
it does not submit the installer fixture again.

Without `--keep-running`, disposable install keeps its established behaviour:
it runs the verification and restart checks, stops the server, and returns.
For a persistent macOS catalogue/Attic service, use the login-scoped
[macOS native installation](macos-native.md), validated on macOS 26 Intel and
Apple Silicon. Persistent Linux native service mode uses systemd; see the
[Linux native installation](native-installation.md).

## Connect a fresh client

The result shows the endpoint, state, transcript and administrator credential
paths. The installer also creates an owner-private curl configuration carrying
the credential, so the token need not appear in the command line:

```sh
state_root="${HOME}/.local/state/cairn-install/local-memory/instance"
curl --disable --silent --show-error --fail \
  --noproxy '*' --proto '=http' --max-redirs 0 \
  --config "${state_root}/credentials/curl.conf" \
  http://127.0.0.1:8000/v1/instance
```

Use the [`/memory/v1` client contract](../shared-memory-client.md) for remember,
recall, disagreement, history and correction operations. The administrator's
root grant covers child scopes in the local realm. Keep independently changing
facts separate, use a fresh idempotency key for each new mutation, and verify
fact ID/body mappings through history before correcting a fact.

Graph-disabled memory recall remains available from the catalogue. It does not
claim semantic relevance. The legacy `/v1/retrieve` path retains its existing
index requirements. Exact evidence supplied with a memory is read through the
authenticated `/v1/read-evidence` operation.

## Failure and recovery boundary

Normal Ctrl-C is the supported shutdown path. Failed readiness, verification,
restart or cleanup retains private state and a redacted transcript for
inspection.

The child inherits the installer instance lock. If the installer is killed in
a way it cannot catch, that lock prevents another installer from resuming or
deleting the instance while the child remains alive. This release does not
adopt a surviving macOS process from a stored PID. An unexplained active launch
therefore fails closed and requires operator inspection; do not delete its data
to make the refusal disappear.

## Hosted acceptance scope

The recorded acceptance used standard GitHub-hosted macOS 26 Intel and Apple
Silicon runners, read-only repository access, synthetic data and no provider
credentials. It is a measured result from release preparation, not a promise
that this distribution includes or automatically runs a hosted Mac workflow.
The reproducible helper is [`scripts/test_macos_foreground.py`](../../scripts/test_macos_foreground.py).

The acceptance helper performs two real foreground installer cycles. Across a
clean stop and resume it verifies memory creation, disagreement, correction,
fresh-client recall/history, exact Attic bytes and retained instance identity.
The hosted jobs also ran focused process tests against the hosted kernel, including
inherited-lock exclusion, TERM-resistant process-group cleanup and strict Darwin
inventory, plus the catalogue and Attic fullfsync profile tests. Its report
records the installer's source fingerprint and the exact `uv.lock` SHA-256.
It covers graph-disabled disposable operation only. It does not certify
semantic search, launchd integration, physical power-loss durability or macOS
12. The successful run executed behavioural commit
`1f83b2a6660e8234d79caab7ebeab3d0155c88ab`. Later documentation edits preserve
that runtime code; installer fingerprints also include README content.
