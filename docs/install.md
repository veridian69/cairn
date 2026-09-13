# Install Cairn

Choose one path before installing tools. These instructions target Linux
`x86_64`, including WSL with a native Linux checkout; the persistent service
requires a working systemd user manager. Keep the checkout, Python environment
and data on Linux storage, not `/mnt/c`. Other platforms and architectures
have not completed this installation acceptance exercise.

Passing developer tests is separate from following these instructions as a new
user. Kubernetes installation and cluster repair are outside this guide.

| Purpose | Follow this procedure | What remains afterwards |
| --- | --- | --- |
| Try the native service | [Disposable quickstart](quickstart.md) | Nothing: the script stops the server and deletes its temporary data and credential. |
| Keep a native instance | [Persistent native installation](operations/native-installation.md) | Stable identity, owner-controlled data and credential, a systemd user service and backup procedure. |
| Run Cairn with Attic and optional semantic search | [Docker Compose installation](../deploy/compose/README.md) | Persistent volumes, protected credentials and a managed container stack. |
| Develop Cairn | [Full developer validation](#full-developer-validation) | A development environment; this does not install a persistent service. |

## Obtain a trusted checkout

Use the repository URL supplied by your distributor. In the following block,
replace `REPLACE_WITH_TRUSTED_REPOSITORY_URL` with that URL; it is the only
substitution. Do not put a password or token in the URL. Use your Git client's
credential manager if the distributor requires authentication.

```sh
mkdir -p "$HOME/projects"
cd "$HOME/projects"
repository_url='REPLACE_WITH_TRUSTED_REPOSITORY_URL'
test "$repository_url" != REPLACE_WITH_TRUSTED_REPOSITORY_URL
git clone "$repository_url" cairn
cd cairn
git rev-parse HEAD
```

If you already have this checkout, open a terminal in its root instead. Unless
a guide explicitly changes directory, commands run from the root containing
`pyproject.toml`, `uv.lock` and `deploy/`. Record the revision printed above for
acceptance. Initial source installation needs access to the locked Python
package indexes; Docker additionally needs access to the image registries.

## Get missing prerequisites

Check versions **before** following a procedure. A missing command is a failed
prerequisite, not a reason to improvise an alias. On Ubuntu 24.04 x86_64, an
administrator can install the common shell tools and native Python with:

```sh
sudo apt-get update
sudo apt-get install --yes git bash coreutils curl jq python3.12
```

The Compose and developer paths also need `make` and `sed`:

```sh
sudo apt-get install --yes make sed
```

Install `openssl` only for optional semantic retrieval:

```sh
sudo apt-get install --yes openssl
```

For another Linux distribution, have its administrator install the equivalent
packages through its normal package policy. Native service installation also
requires systemd user services and `loginctl`; installing a package alone does
not enable a user manager on a host without systemd. The native guide checks
that manager before writing configuration.

For native paths, install uv 0.12.0 using the
[uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
with that pinned version. For example, after reviewing the downloaded script:

```sh
uv_installer="$(mktemp)"
curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  https://astral.sh/uv/0.12.0/install.sh -o "$uv_installer"
# Read the downloaded installer before executing it.
cat "$uv_installer"
sh "$uv_installer"
rm -f "$uv_installer"
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

Expect `uv 0.12.0`. Add `$HOME/.local/bin` to your shell's normal startup PATH
if it is not already there. The commands above set it for the current session.
The disposable path can use `uv python install 3.12` when the distribution has
no Python 3.12. For the persistent guide's explicit `python3.12` checks, install
that executable through the host package policy first.

Docker users should follow the official
[Engine installation](https://docs.docker.com/engine/install/) and
[Compose plugin installation](https://docs.docker.com/compose/install/linux/)
for their distribution. Access to the Docker socket is effectively root access.
Have an administrator grant it only to a trusted operator; do not blindly add
users to a privileged group. The Compose guide describes its required `sudo`
file-ownership operations before first boot.

## Disposable native quickstart prerequisites

Required: Linux x86_64, Bash, Git, coreutils (including `mktemp`, `chmod`,
`sha256sum`, `tr` and `rm`), curl 7.76 or newer, jq 1.6 or newer, Python 3.12 and
uv 0.12.0. You need a writable checkout and `/tmp`, an unused loopback port
8000, and network access for locked dependency installation. You do not need
Go, Bubblewrap, kubectl, Docker or administrator access to run the quickstart.

```sh
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
for tool in bash git mktemp chmod sha256sum tr rm curl jq python3 uv; do
  command -v "$tool" || exit 1
done
curl --version
jq --version
python3 --version
uv --version
uv python find 3.12
test -w .
test -w /tmp
python3 - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 8000))
PY
```

Expect an executable path for every tool, curl 7.76 or newer, jq 1.6 or newer,
Python `3.12.x`, uv `0.12.0`, a Python 3.12 path, writable-directory checks with
no output, and successful loopback binding with no output. A bind error means
port 8000 is already in use; stop the conflicting local service or choose the
persistent procedure's configurable port. Resolve missing tools using
[missing prerequisites](#get-missing-prerequisites), then repeat the checklist
before following the [disposable quickstart](quickstart.md). If locked package
installation cannot reach its configured indexes, ask the network or repository
administrator for the approved access or cache. The runtime installation uses
only locked runtime dependencies; developer tools are a separate path.

## Persistent native prerequisites

Use the disposable checklist plus `python3.12`, systemd with user service
support, `systemctl`, `systemd-analyze`, `loginctl`, `awk`, `grep`, `df`, `id`,
`stat`, `install`, `cp`, `mv` and `tar`. These are supplied by the standard
Linux userland/systemd packages. You need an ordinary non-root account, at least
1 GiB free for a small installation, writable owner-controlled home directories,
and separately protected backup storage. Only optional startup before login
requires administrator/polkit authority. The [native guide](operations/native-installation.md#requirements)
provides exact checks and expected results before installation.
Running the optional bounded retrieval verification also requires curl 8.4.0
or newer; check it with `curl --version` before following the client guide.

## Docker Compose installation

Required: Linux x86_64 with systemd, Engine 25.0 or newer, Compose plugin 2.20.2
or newer, Bash, Git, make, `systemctl`, `sed`, GNU coreutils (`sha256sum`,
`install`, `realpath`, `stat` and `tr`), curl 8.4.0+, jq 1.6+ and Python 3.12.x
as `python3` for the verification helper. Curl 8.4.0 is required because its
[`--max-filesize`](https://curl.se/docs/manpage.html#--max-filesize) limit also
guards unknown-length responses during transfer. Semantic retrieval additionally needs
OpenSSL, a provider API key, outbound provider access, the FalkorDB image and
privilege through `sudo` or an existing root session for numeric file ownership.
Provider calls may incur charges; the base Attic-enabled stack does not call a
model provider. Go, uv, Bubblewrap and kubectl are not host prerequisites here.

You need trusted Docker daemon access and administrator authority for the
explicit numeric UID ownership changes. Container data lives in persistent
volumes; configuration and credential files remain on the host. The
[Compose prerequisite checks](../deploy/compose/README.md#compose-prerequisites-and-local-image)
verify daemon access and versions before changing files. Follow that guide
from beginning to end, including its choice of base or semantic lifecycle
commands. `down` and volume deletion have different retention consequences.

## Full developer validation

This path is for changing or validating Cairn, not a prerequisite for using it.
It requires the native tools above, Python 3.12, uv 0.12.0, Go 1.22+, make,
a C compiler for Go race tests, Docker with the Compose plugin, and the pinned
kubectl fetched below. Bubblewrap at `/usr/bin/bwrap` (verified with 0.9.0),
`/usr/bin/python3`, merged-`/usr` and permitted unprivileged user/PID/mount
namespaces are required for host-isolation tests. Do not disable host security
policy to conceal a failed namespace check. Ask the host administrator for a
supported test environment. No Kubernetes cluster access is required.

On Ubuntu, the extra build tools are provided by `build-essential`, `golang-go`
and `bubblewrap`. Check that the packaged Go version meets 1.22; otherwise use
the [Go installation instructions](https://go.dev/doc/install). Docker/Compose
installation is described above. From the repository root:

```sh
for tool in git make cc go uv jq docker; do
  command -v "$tool" || exit 1
done
go version
uv --version
docker compose version
/usr/bin/python3 --version
/usr/bin/bwrap --version
/usr/bin/bwrap --unshare-user --unshare-pid --ro-bind / / /usr/bin/true
uv sync --locked
./scripts/fetch-kubectl
build/tools/kubectl version --client
make check
```

Expect every prerequisite command to exit zero, the stated versions, and a
successful `make check`. The render and Compose configuration gates are
client-side and contact neither cluster nor Docker daemon. Tests, locked package
installation and dependency auditing may need internet access. The default suite
leaves the opt-in real database tests skipped; passing it does not claim those
integrations or beginner installation acceptance. Record the actual summary,
revision and environment rather than treating a historical test count as a gate.
