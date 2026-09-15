# Cairn-maintained FalkorDB runtime

Cairn v0.5 selects `v4.20.4-cairn.1`, a Linux amd64 server-only runtime.
The exact tag and OCI index digest are in `release.json` and
`../images.lock`. This candidate has not yet been published to GHCR.

The image retains the exact published FalkorDB 4.20.4 module, uses the pinned
Redis 8.6.3 base and refreshed Debian packages, and includes the shell failure
handling fix submitted in [FalkorDB PR #2838](https://github.com/FalkorDB/FalkorDB/pull/2838).
The browser is absent. `provenance.json` records the upstream commits, module
hash, base and distinct index/platform/config digests. The recorded scan found
zero fixable HIGH/CRITICAL findings and 36 HIGH findings without available fixes.

<a id="install-the-rc3-offline-image"></a>
<a id="install-the-rc4-offline-image"></a>

## Install the maintained offline image

Download the [image archive](https://github.com/veridian69/cairn/releases/download/v0.5.0-rc.4/cairn-falkordb-v4.20.4-cairn.1.tar)
and [matching corresponding-source bundle](https://github.com/veridian69/cairn/releases/download/v0.5.0-rc.4/cairn-falkordb-v4.20.4-cairn.1-source.tar.gz)
from the RC4 release. The source, build inputs and component notices are
available at no charge alongside the binary. Retain the release
[checksums](https://github.com/veridian69/cairn/releases/download/v0.5.0-rc.4/SHA256SUMS).
Before `cairn-install --semantic`, run from the trusted checkout root on the
Docker host:

```sh
python3 scripts/falkordb_release.py load \
  --descriptor deploy/falkordb/release.json \
  --archive /path/to/cairn-falkordb-v4.20.4-cairn.1.tar
./cairn-install --non-interactive --mode docker --name cairn \
  --port 8000 --semantic --provider-key-file /path/to/protected/openai-key
```

Offline loading requires Docker's containerd image store to retain the full
OCI index. It was selected for testing on Docker 29.7.2. The loader verifies the
trusted archive checksum before Docker consumes it, then checks the exact
repository digest. A failed identity check stops installation preparation;
do not substitute a config digest or edit installer state to bypass it.
Normal registry pulls, once published, retain the existing Docker minimum.

## Prepare and rebuild

To prepare an archive from the already validated local image:

```sh
python3 scripts/falkordb_release.py prepare \
  --descriptor deploy/falkordb/release.json \
  --archive build/cairn-falkordb-v4.20.4-cairn.1.tar
```

The archive contains the full index, including its attestation. A platform-only
export is not interchangeable. Release checksums belong to the exact prepared
artefact; do not rewrite them merely because a different archive was produced.

`build.sh` verifies and extracts the vendor module, creates the compiler shim,
and builds the preserved patched upstream recipe with a pinned Redis base.
Run it from the checkout root:

```sh
bash deploy/falkordb/build.sh
```

Package security repositories are mutable. This is a repeatable build procedure,
not a promise of bit-identical future images. A rebuild must receive its own
version/digest, provenance, scan and acceptance evidence before selection.
The module was not recompiled in this release; corresponding source and build
instructions are supplied separately alongside the binary.

See [source-bundle preparation and verification](SOURCE_BUNDLE.md) for the
exact recursive source, component notice and checksum procedure.

## Distribution and replacement

Publish the image and its checksummed corresponding-source bundle together,
with a clear no-charge source download adjacent to the binary download.
Preserve the upstream and component licences and notices; the derivative is
not relicensed under Cairn's Apache licence. Complete the source-bundle checks
before distribution. Registry publication is a separate release action.

When upstream publishes the fix:

1. Select an immutable official release and inspect its runtime and application
   changes; merge any Cairn compatibility changes needed.
2. Repeat vulnerability scanning, `make check`, real database/Graphiti tests,
   and fresh installer semantic search, exact Attic evidence and restart checks.
3. Update all image pins and provenance together, review the committed export,
   then publish a new Cairn release. Never change an existing release's pin.

Until then Cairn owns derivative rebuilds, security review and corresponding
source availability. The upstream PR's merge alone does not replace our image.
