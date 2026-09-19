# Guided Cairn installation and recovery

`cairn-install` teaches and runs a new Cairn installation from a trusted source
checkout. It supports disposable native, persistent native, Docker and an
existing administrator-prepared Kubernetes cluster on Linux x86_64; macOS
supports foreground and per-user launchd native catalogue/Attic installations.
Each named installation has durable private state, so an interrupted run can
reconcile what already happened before it continues.

**Garden is not supported on macOS.** Run Garden on Linux.

On Linux, persistent modes can also manage a shared Garden server. Add
`--garden-config /absolute/path/garden.json` when creating the installation;
the [managed Garden guide](managed-garden.md) covers TLS, participant credentials,
remote adapters and the same preserving recovery/removal lifecycle.

This installer does not upgrade or adopt an existing installation. Kubernetes
mode installs one new instance; it never provisions or repairs a cluster, and
it does not support OpenShift.

## Requirements

Run as the ordinary user who will own the installation. On Linux, keep the
checkout and state on a native Linux filesystem rather than a WSL-mounted path
such as `/mnt/c`; on macOS, use a trusted local checkout and local state. The
source launcher needs Python 3.12–3.14. All modes need an unused numeric
loopback port and access to the locked dependencies.

Native modes require `uv 0.12.14`. Linux persistent native mode also requires a
working systemd user manager. macOS native mode uses the ordinary user's
launchd session and supports catalogue memory and Attic only. Native acceptance
passed on macOS 26 Intel and Apple Silicon; see [macOS native installation](macos-native.md). Docker mode requires Docker Engine 25.0 or newer,
Compose 2.20.2 or newer and trusted access to the Docker daemon. Semantic
search needs outbound OpenAI access and an OpenAI API key; Attic needs no
external key. Native semantic mode also needs Docker: its FalkorDB index runs
in a dedicated container. The installer checks local prerequisites before
preparing Cairn and proves provider access during semantic verification.
Before enabling semantic mode, [build the FalkorDB runtime locally](../../deploy/falkordb/README.md).
Docker/native installs require `--falkordb-runtime /absolute/path/runtime.json`.
Kubernetes installs require node preparation followed by
`--kube-falkordb-receipt /absolute/path/kubernetes-receipt.json`.

Linux persistent native mode enables a systemd user service. Running before
login and after logout also requires user lingering; follow the manual guide's
[login and reboot behaviour](native-installation.md#login-and-reboot-behaviour).
The installer does not change that account-wide policy. macOS native mode is
login-scoped: it starts at user login and stops at logout.

Kubernetes mode requires `uv 0.12.14` for the locked YAML helper runtime, an
explicit kube context, and a dedicated namespace that an administrator has
created and labelled `cairn.example.invalid/instance: <installation-name>`.
See [namespace and render preparation](deployment.md#namespace-and-render-preparation)
for the exact create, label and verification commands. It also requires Ready
Linux/amd64 capacity, a CSI StorageClass
that supports `ReadWriteOncePod`, an immutable Cairn image digest, and working
CNI and DNS. It needs namespace-scoped mutation permissions for the documented
resources and cluster-scoped read access to Namespace, Nodes, StorageClass and
CSIDriver for preflight. Semantic mode also requires a healthy shared gateway
for provider egress. The installer verifies these inputs but never creates or
deletes the namespace, gateway, StorageClass, CNI, CSI, nodes, registry
credentials or image cache; it never mutates cluster prerequisites. The cluster
administrator remains responsible for those prerequisites and their lifecycle.

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

Add `--semantic` for **Attic plus semantic search** in Linux native or Docker mode:

```sh
falkordb_runtime="$PWD/build/falkordb-local/runtime.json"
test -s "$falkordb_runtime"
./cairn-install --non-interactive --mode docker --name recall \
  --port 8125 --semantic --falkordb-runtime "$falkordb_runtime"
```

The source launcher uses its own checkout. A packaged `cairn-install` command
cannot infer the application source, so give it an explicit trusted checkout:

```sh
cairn-install --non-interactive --mode native --name notes \
  --source "$HOME/projects/cairn"
```

Use `--state-root /absolute/linux/path` only when the default private state root
is unsuitable. It is primarily useful for isolated tests.

## Install to an existing Kubernetes namespace

Kubernetes mode is non-interactive once every immutable input is stated. This
semantic example uses a registry-published, digest-pinned Cairn image and the
separate receipt created after [building FalkorDB locally and staging it on the
eligible nodes](../../deploy/falkordb/README.md#prepare-kubernetes-nodes).
Create the provider-key file outside installer state with mode `0600`; it
contains the key on one line.

```sh
kube_image='REPLACE_WITH_TRUSTED_CAIRN_IMAGE@sha256:REPLACE_WITH_64_HEX_DIGEST'
case "$kube_image" in
  *REPLACE_WITH_*|registry.example/*)
    printf 'Replace kube_image with the distributor-supplied immutable image before running\n' >&2
    exit 2
    ;;
esac
kube_digest="${kube_image##*@sha256:}"
test "${#kube_digest}" -eq 64
case "$kube_digest" in *[!0123456789abcdefABCDEF]*) exit 2 ;; esac
cairn-install --non-interactive --mode kubernetes --name cairn-v05 \
  --port 8126 \
  --kube-context reference --kube-namespace cairn-v05 \
  --kube-storage-class cairn-local \
  --kube-image "$kube_image" \
  --kube-falkordb-receipt /home/operator/build/falkordb-local/kubernetes-receipt.json \
  --semantic --provider-key-file /home/operator/.config/cairn/openai-api-key \
  --state-root /home/operator/.local/state/cairn-install \
  --source /home/operator/projects/cairn
```

The installer copies the provider key into protected installation state and
records the external input by path only. The file supplied with
`--provider-key-file` remains an operator-owned input: the installer does not
modify or delete it, and `blitz` leaves it untouched. Retain it as a durable
credential if that is your policy; if you deliberately created a one-time
staging file, remove it yourself only after confirming the protected copy and
that no later resume depends on the input path. Never place a provider key in
flags or rendered YAML; it is also excluded from command logs and the rendered
manifest.

The receipt is mandatory for a new semantic Kubernetes installation. The
installer retains its contents, checks the recorded node names and UIDs, and
proves the exact local FalkorDB digest on every recorded node without pulling.
It does not transfer the image or obtain host access; node staging remains a
separate cluster-administrator operation.

Cairn uses the normal registry path and the site's existing registry-credential policy.
FalkorDB uses the separate local runtime receipt described above.

`--kube-preloaded-image` is an explicit, narrow exception for the recorded
single-node reference host. Add it only when the same digest is already in that
node's runtime image cache; the installer then uses `IfNotPresent` and refuses
any cluster without exactly one schedulable node. It is not a substitute for a
registry path on a multi-node cluster.

For that single-node exception, stage the locally built Cairn image with the
same reviewed helper used for the FalkorDB runtime. This is a
cluster-administrator operation: it requires explicit SSH host mapping and
non-interactive sudo on the node. It does not create Kubernetes resources or
give the installer host access. Run it from the trusted checkout. The first
commands below read the release tag from `deploy/images.lock` and build it;
do not copy a release number into this procedure. Docker must use its containerd image store,
so `docker image save` preserves the OCI index; the staging helper verifies that
property and refuses the archive before it transfers anything to a node.

```sh
set -eu
source_image="$(awk -F= '$1 == "CAIRN_IMAGE" {print $2}' deploy/images.lock)"
test -n "$source_image"
make image IMAGE="$source_image"
digest="$(docker image inspect "$source_image" --format '{{.Id}}')"
case "$digest" in sha256:[0-9a-f][0-9a-f]*) ;; *) exit 1 ;; esac
local_tag="cairn.local/cairn-runtime:build-${digest#sha256:}"
cairn_image="cairn.local/cairn-runtime@$digest"
mkdir build/cairn-image-stage
docker image tag "$source_image" "$local_tag"
docker image save --output build/cairn-image-stage/image.tar "$local_tag"
python3 scripts/kubernetes_image_stage.py \
  --context reference --archive build/cairn-image-stage/image.tar \
  --image "$cairn_image" --node reference=reference \
  --output build/cairn-image-stage/kubernetes-receipt.json
```

The receipt records staging evidence but is not passed to `cairn-install` for
the Cairn image. The resulting image has the form
`cairn.local/cairn-runtime@sha256:…`. Use
`--kube-image "$cairn_image" --kube-preloaded-image` in
the installation command. The installer proves the exact Cairn digest can
execute with a disposable `IfNotPresent` Pod before it creates the instance.
It does not use `Node.status.images`, because kubelet may cap or disable that
inventory. Garden remains a normally pullable published image. If the exact
probe cannot start, restage the Cairn archive and rerun
`./cairn-install resume --name NAME`; it reuses the recorded installation
inputs. Retain the archive for a later restage. The manual GitOps procedure
remains registry-only; it does not cover this guided, single-node exception.

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
the full result JSON after install, resume, rollback or blitz. A failed
semantic Kubernetes probe reports the shared gateway by name and its documented
installation procedure; probe output is never used to disclose credentials.

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
  --falkordb-runtime "$PWD/build/falkordb-local/runtime.json" \
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

### garden_files — Prepare Garden files and participant credentials

When managed Garden is selected, validates the Garden configuration and writes
the owner-only TLS, server and participant files needed by the selected mode.
Installations without managed Garden skip this stage.

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

### garden_prepare — Prepare Garden runtime and configuration

When Garden is selected, prepares the owned Garden runtime and configuration
after Cairn has passed its own verification. It does not replace or adopt a
pre-existing Garden service.

### garden_start — Start Garden

Starts the Garden service or process recorded for this installation and keeps
its endpoint within the configured TLS and ownership boundary.

### garden_verify — Verify Garden endpoint and participant bindings

Checks the authenticated Garden endpoint and verifies every configured
participant binding and adapter bundle. A Cairn verification does not imply
Garden verification.

### restart — Restart and verify the same saved data

Restarts the owned service and performs read-only checks against the same
identity and saved receipt. A run is reported as `verified` only after these
checks actually pass.

After verification, disposable mode stops its detached process. Unlike the
one-shot [manual disposable procedure](manual-disposable-installation.md), it retains catalogue data,
credentials, state and evidence in the named private directory for inspection
and later recovery.

### stop — Stop the disposable Cairn process and retain its data

After a successful disposable run, stops the owned Cairn process while
retaining its catalogue, credentials, state and evidence for inspection or a
later resume.

### rollback — Stop Cairn and preserve its data

Stops or disables only the proved-owned Cairn resources for an explicit
rollback operation. It retains the catalogue, credentials, configuration,
evidence and persistent storage so `resume` can restore the same instance.

### garden_rollback — Stop Garden and preserve its data

For an explicit rollback of a managed Garden installation, stops the proved-owned
Garden resources and closes its endpoint while retaining Garden data,
credentials and configuration for resume.

## Inspect and resume

List recorded installations:

```sh
./cairn-install ls
```

The table shows name, mode, recorded status, port and features. Features are
`Attic only` or `Attic plus semantic search`; managed Garden is appended as
`; Garden` when the installation owns a Garden configuration. It does not
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

For Kubernetes mode, `status` prints a temporary, explicit port-forward command
instead of a permanent endpoint. For the example above it is:

```sh
kubectl --context reference --namespace cairn-v05 port-forward service/cairn 8126:8000
```

Run it only for a local check, then stop it when that check ends. The installer
opens a loopback-only port-forward for its own verification and always closes
it afterwards.

Resume after correcting the reported problem:

```sh
./cairn-install resume --name notes
```

Context and namespace are immutable on resume, along with the recorded storage
class, image and preloaded-image policy. Kubernetes resume also refuses an API
server or namespace UID change, and reuses its original resources, PVCs,
credentials, UUID and verification receipt.

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

For Kubernetes mode, rollback scales down the proved-owned StatefulSets and
removes its lifecycle holder. It retains the namespace, resources, PVCs,
credentials and state; it does not alter cluster prerequisites or the shared
gateway.

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

Semantic Cairn disables Graphiti's optional third-party telemetry before it
constructs the adapter, regardless of an inherited environment setting. Older
Graphiti runs may already have left a telemetry identifier under
`~/.cache/graphiti`; that user-level cache is not owned by an installation and
`blitz` deliberately does not remove it. An operator who no longer needs that
legacy artefact may inspect and remove it separately.

Kubernetes blitz first proves every surviving namespaced object by its recorded
UID and ownership labels, then deletes only those installer-owned namespaced
resources and PVCs. It tolerates already-absent proved-owned objects on a
retry. It never deletes the administrator-owned namespace, gateway or any
cluster prerequisite.

Deletion follows normal filesystem and provider semantics. It makes no claim
of secure erasure from storage media, snapshots or backups.
