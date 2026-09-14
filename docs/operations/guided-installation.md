# Guided Cairn installation and recovery

`cairn-install` teaches and runs a new Cairn installation from a trusted source
checkout. It supports disposable native, persistent native and Docker on Linux
`x86_64`. Each named installation has durable private state, so an interrupted
run can reconcile what already happened before it continues.

This installer does not upgrade or adopt an existing installation. It does not
install Kubernetes or OpenShift. Use the [Kubernetes operator procedure](../install.md#kubernetes-installation)
for an existing cluster.

## Requirements

Run as the ordinary user who will own the installation. Keep the checkout and
state on a native Linux filesystem, rather than a WSL-mounted path such as
`/mnt/c`. The source launcher needs Python 3.12–3.14. All modes need an unused
numeric loopback port and access to the locked dependencies.

Native modes require `uv 0.12.0`. Persistent native mode also requires a
working systemd user manager. Docker mode requires Docker Engine 25.0 or newer,
Compose 2.20.2 or newer and trusted access to the Docker daemon. Semantic
search needs outbound OpenAI access and an OpenAI API key; Attic needs no
external key. Native semantic mode also needs Docker: its FalkorDB index runs
in a dedicated container. The installer checks local prerequisites before
preparing Cairn and proves provider access during semantic verification.

Persistent native mode enables a user service. Running before login and after
logout also requires user lingering; follow the manual guide's
[login and reboot behaviour](native-installation.md#login-and-reboot-behaviour).
The installer does not change that account-wide policy.

For detailed host checks and operational trade-offs, see the
[installation prerequisites](../install.md#get-missing-prerequisites),
[manual native procedure](native-installation.md),
[disposable quickstart](../quickstart.md) and
[manual Compose procedure](../../deploy/compose/README.md).

## Start the interactive wizard

From the root of the trusted checkout:

```sh
./cairn-install
```

The wizard asks for the mode, a unique lowercase installation name and a port.
For persistent native and Docker modes it then presents these choices:

```text
1) Attic only
2) Attic plus semantic search
```

Disposable mode is always **Attic only**. The wizard prints the complete
configuration before it creates or starts installation resources. Read that
configuration: the mode, name, port, feature choice and source become fixed for
the named installation.

## Run without prompts

Automation must use `--non-interactive`, `--mode` and `--name`. It never waits
for stdin. Port 8000 and **Attic only** are the defaults for optional values.

```sh
./cairn-install --non-interactive --mode disposable --name trial
./cairn-install --non-interactive --mode native --name notes --port 8123
./cairn-install --non-interactive --mode docker --name notes-docker --port 8124
```

Add `--semantic` for **Attic plus semantic search** in native or Docker mode:

```sh
./cairn-install --non-interactive --mode docker --name recall \
  --port 8125 --semantic
```

The source launcher uses its own checkout. A packaged `cairn-install` command
cannot infer the application source, so give it an explicit trusted checkout:

```sh
cairn-install --non-interactive --mode native --name notes \
  --source "$HOME/projects/cairn"
```

Use `--state-root /absolute/linux/path` only when the default private state root
is unsuitable. It is primarily useful for isolated tests.

## Output and diagnostics

The default display is deliberately quiet: it shows the chosen configuration,
stage progress, failures and a final summary with the endpoint, state and
credential paths. A successful `blitz` instead prints only the deleted
installation's name and status, because its former paths and endpoint are no
longer usable. Detailed prerequisite probes, commands and command output still
go to the owner-only `commands.log`, with credentials redacted.

Install, resume and rollback finish with one copyable `Transcript: /absolute/path`
line pointing to that command transcript. Failed operations also print this line
when a transcript was created. File-generation details appear in the transcript
and verbose output. Successful blitz removes its transcript with the instance;
it does not print a path to a deleted file. Read-only `ls` and `status` do not
create a transcript or append that footer to their output.

Displayed commands share a current directory: `cd` appears on the first visible
command and when that directory changes. Hidden diagnostics do not change the
displayed directory. The log retains the complete working directory for every
command; quiet mode does not repeatedly announce omitted diagnostics.

Add `--verbose` to show those detailed diagnostics in the terminal and print
the full result JSON after install, resume, rollback or blitz:

```sh
./cairn-install resume --name notes --verbose
```

`status` prints a human-readable configuration block followed by its full
recorded result as JSON, without requiring `--verbose`. Its complete output is
not a standalone JSON document. Colour is limited to headings and outcome
labels on a terminal.
Set `NO_COLOR` (even to an empty value), set `TERM=dumb`, or redirect the output
to receive plain text with no ANSI escape sequences.

## Supply the OpenAI key without exposing it

On the first semantic run, if `--provider-key-file` was not supplied, the
installer creates this empty owner-only file and stops:

```text
~/.local/state/cairn-install/NAME/openai-api-key
```

Open that file in a local editor, put the key on one line, save it and retain
mode `0600`. Do not paste the key into chat, command arguments or shell history.

```sh
chmod 600 "$HOME/.local/state/cairn-install/recall/openai-api-key"
${EDITOR:?Set EDITOR to your local editor} \
  "$HOME/.local/state/cairn-install/recall/openai-api-key"
./cairn-install resume --name recall
```

Alternatively, create a protected file yourself and pass its path on the first
run:

```sh
install -m 600 /dev/null "$HOME/.local/state/cairn-openai-key"
${EDITOR:?Set EDITOR to your local editor} "$HOME/.local/state/cairn-openai-key"
./cairn-install --mode docker --name recall --semantic \
  --provider-key-file "$HOME/.local/state/cairn-openai-key"
```

The installer safely reads the file, copies it to the named installation's
protected credentials directory and records only the destination path. Key and
administrator-token contents are omitted from the display and command log.

## Installation stages

The stage names below are the same names stored in `state.json` and shown by
`status`. A completion bit alone is never trusted on resume; the installer
checks the owned resource's real postcondition before another mutation.

### preflight — Check prerequisites and ownership

Checks the platform, runtime or Docker versions, port, source inputs and any
recorded resource ownership. Existing unowned services, projects and files are
refused rather than adopted or overwritten.

### prepare — Prepare runtime and configuration

Creates the owned runtime or image, protected configuration and optional
semantic-search dependency. Docker manifests are materialised in the private
installation directory rather than run from mutable checkout files.

### bootstrap — Verify catalogue and retain administrator credential

Checks and migrates the catalogue offline, then establishes the stable Cairn
identity and captures the administrator credential in a protected file. An
ambiguous post-commit result stops for explicit credential recovery; it does
not silently bootstrap again or issue a replacement.

If the error is `needs_credential_recovery`, first use `rollback` with the same
name to stop owned services. Keep the whole private installation directory.
Its `instance/credentials/bootstrap.json` (or the `bootstrap_intent` path in
`state.json`) is the protected one-time capture; `instance/credentials/admin.token`
is the retained administrator credential. Never paste either file into an issue
or a terminal transcript. A complete capture can be reconciled by `resume`;
an existing realm with no usable capture cannot be safely bootstrapped again.

This exceptional case needs an operator to use Cairn's explicit offline
`recover` procedure, which creates an audited replacement credential. See the
[lost-token recovery instructions](deployment.md#first-boot)
and the [native protected-capture procedure](native-installation.md#bootstrap-exactly-once).
The installer does not adopt a manually recovered credential or rewrite its
ownership record automatically. Preserve the record for diagnosis and use the
manual operator guide for the recovered instance; do not edit UUIDs or delete
the catalogue to make a check pass.

### start — Start Cairn

Starts only the service, process or Compose project whose ownership is recorded
for this installation name.

### verify — Verify identity and exact saved data

Authenticates the recorded Cairn identity and submits one stable synthetic
Attic write, then checks the exact bytes read back. Semantic mode also proves a
bounded candidate retrieval. Submission receipts and idempotency keys are
saved so resume does not ingest a committed check twice.

### restart — Restart and verify the same saved data

Restarts the owned service and performs read-only checks against the same
identity and saved receipt. A run is reported as `verified` only after these
checks actually pass.

After verification, disposable mode stops its detached process. Unlike the
one-shot [manual disposable procedure](manual-disposable-installation.md), it retains catalogue data,
credentials, state and evidence in the named private directory for inspection
and later recovery.

## Inspect and resume

List recorded installations:

```sh
./cairn-install ls
```

The table shows name, mode, recorded status, port and features. It does not
start services or probe live health. An unfinished blitz remains listed even if
only its recovery journal survives. Unreadable or malformed instance records
appear as unavailable while other instances remain visible. A missing state
directory produces an empty list without creating anything. Use `--state-root`
for a different installer state directory, or `--verbose` for JSON rows.

Each name defaults to
`~/.local/state/cairn-install/NAME/`. `state.json` records immutable options,
stable run and instance UUIDs, stage state, ownership receipts and verification
receipts. `commands.log` contains the explained commands and scrubbed output.
Credentials and installation files live below `instance/`.

Inspect the durable record without probing or changing the service:

```sh
./cairn-install status --name notes
```

Resume after correcting the reported problem:

```sh
./cairn-install resume --name notes
```

Resume requires the recorded source path and installation-relevant source
content to remain unchanged. It reuses the same run UUID, Cairn instance UUID,
credential, options and verification receipts. If the source or fixed options
differ, restore the recorded checkout instead of starting over under the same
name.

For a previously verified persistent instance, resume starts the owned service
(and its retained FalkorDB container in native semantic mode) and repeats
authenticated identity and saved-data reads before reporting
success. It does not submit another verification write. A verified disposable
instance stays stopped and returns its recorded result.

A missing retained semantic-index password is lost state, not permission to
rotate it: restore the original protected file before resuming. Retained
bootstrap captures must also agree with the administrator token; contradictory
or missing captures require credential recovery rather than a new bootstrap.

## Preserving rollback

Rollback stops or disables only resources whose recorded ownership still
matches. It refuses modified or foreign services, processes and projects.

```sh
./cairn-install rollback --name notes
```

It does not need the original source checkout. It retains catalogue data,
Attic, bootstrap capture, credentials, identity, configuration, evidence and
Docker volumes. For persistent native mode it removes only the unchanged owned
user-service unit; for Docker it stops the owned project without deleting its
volumes. The command prints the retained paths. A later `resume` restores the
same instance rather than creating a new UUID or token.

## Permanently remove a named installation

`blitz` is the destructive counterpart to preserving rollback. It permanently
removes everything recorded as owned by one named installer instance: its
service or process, dedicated containers and networks, persistent catalogue and
Attic data, semantic index volumes, configuration, credentials, verification
evidence, logs and installer state. Resource-ownership checks refuse foreign
services, processes, containers and volumes, while path checks refuse unsafe
filesystem boundaries. Installer-owned local files are deleted even if their
contents changed after installation.

Review the [backup and restore procedure](backup-restore.md) first. Then run:

```sh
./cairn-install blitz --name notes
```

The command prints an irreversible-deletion notice and requires you to type the
exact installation name. `--yes` bypasses that prompt. Automation must use both
`--non-interactive` and `--yes`; without `--yes`, non-interactive blitz fails
without deleting anything:

```sh
./cairn-install blitz --non-interactive --yes --name notes
```

A successful blitz cannot be resumed or rolled back. It does not need the
original source checkout. If deletion fails or is interrupted, recovery
information records the deletion phase; correct the reported problem and run
`blitz` again with the same name. Do not use `resume` for a partially deleted
instance.

Blitz leaves shared inputs and caches alone: the source checkout, installed
toolchains and pulled images are not owned by one named installation. If
`--provider-key-file` supplied an external key file, that original file is also
untouched; only the installer-owned protected copy is deleted.

Deletion follows normal filesystem and provider semantics. It makes no claim
of secure erasure from storage media, snapshots or backups.
