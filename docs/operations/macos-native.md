# macOS native installation

Cairn's `native` installer mode creates one persistent, per-user macOS service
on numeric loopback. It uses a user `LaunchAgent`: it survives the terminal
that ran the installer, starts when that user logs in and stops when that user
logs out. It does not expose Cairn to another machine or run while the user is
logged out.

Use the ordinary installer from a trusted checkout:

```sh
./cairn-install --non-interactive --mode native --name notes --port 8123
```

The service provides catalogue-backed memory and exact Attic evidence only.
The installer currently supports semantic graph search on Linux; `--semantic`
is unavailable for this macOS native path. Remote FalkorDB use from a Mac has
not been validated or integrated into the installer. It needs no model-provider credential.

## Requirements and status

Use the same account, trusted local checkout, host Python 3.12–3.14, uv
0.12.14, numeric-loopback port and Intel build prerequisites as the
[macOS foreground guide](macos-foreground.md#requirements). The native mode
also needs a logged-in GUI user session and permission to manage that user's
LaunchAgent; it needs no root access. Keep the checkout and private
installation state on a local filesystem.

Native background acceptance passed on macOS 26.6.1 Intel and macOS 26.6.2
Apple Silicon with Python 3.14.7. It verified service reachability after the
installer exited, memory and exact Attic evidence across restart and
rollback/resume, foreign-service protection, delayed shutdown and clean removal.

An actual logout/login cycle and macOS 12.7.6 have not been tested. The login
behaviour described here follows the LaunchAgent configuration; the hosted
acceptance ran within an existing GUI login session.

## Lifecycle and recovery

After `Status: verified`, closing the terminal leaves the owned LaunchAgent
running on `http://127.0.0.1:PORT`. At the next login it starts in the user's
launchd session; logout stops it. Inspect or resume the retained installation:

```sh
./cairn-install status --name notes
./cairn-install resume --name notes
```

`resume` restores the same owner, instance identity, catalogue, Attic data,
credentials and verification receipts. It does not create a replacement realm
or token.

Rollback removes the owned LaunchAgent while retaining the catalogue, Attic,
credentials, configuration, evidence and installer state:

```sh
./cairn-install rollback --name notes
```

Resume recreates the LaunchAgent for that retained instance. `blitz` is
different: after its required confirmation it deletes every resource and data
path recorded as owned by the named installation, including the LaunchAgent,
catalogue, Attic, credentials and installer state. Take a backup first if the
data matters:

```sh
./cairn-install blitz --name notes
```

See the [guided installer reference](guided-installation.md) for ownership,
failure and recovery rules. For a temporary foreground process, use
[macOS foreground](macos-foreground.md). For persistent Linux systemd and
optional semantic search, use the [Linux native guide](native-installation.md).

## Repeatable hosted acceptance

The public repository includes the manual **macOS native acceptance** workflow
(`.github/workflows/macos-foreground.yml`). Maintainers select an exact candidate
branch and run the standard Intel and Apple Silicon jobs. Each job records its
measured result in the Actions summary. The workflow uses synthetic data and no
model-provider credentials; it does not certify macOS 12, semantic search or an
actual logout/login cycle.
