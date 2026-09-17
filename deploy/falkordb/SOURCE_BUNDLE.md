# Preparing the FalkorDB corresponding-source bundle

`prepare_source_bundle.py` creates the source companion for the exact
linux/amd64 `v4.20.4-cairn.1` runtime. It is release preparation machinery;
running it does not publish an image or source archive.

The generator fails closed unless it can tie every input to the identities in
`provenance.json`. It includes:

- FalkorDB at patched commit `c3fea9bee0d7d4ad6c24308f18a19fc8c81c2996`,
  including all nine direct and five nested submodules at their gitlink commits;
- the official Redis 8.6.3 release source archive, checked against SHA-256
  `9f54d4458c52be5472cdd1347d737f1d488b520fc3d0911cba47302de8d836e2`;
- exact Debian binary and source package versions from the reviewed runtime,
  every package's copyright file, and the complete `.dsc` source closure
  downloaded by content hash from Debian Snapshot;
- the maintained build recipe, provenance, upstream licence texts and the
  prominent modification notice; and
- an internal `SHA256SUMS`, plus a checksum beside the final source archive.

The Git archive is made from committed objects rather than copied worktree
bytes. Missing, changed or uninitialised submodules, a dirty checkout, the
wrong runtime index, the wrong architecture, a changed module, incomplete
Debian source metadata, or any checksum mismatch stops generation.

## Prepare locally

Requirements are Python 3.12 or newer, Git, Docker with the exact accepted
image already present, and outbound HTTPS access to the official Redis and
Debian source archives. From the Cairn checkout, create a separate source
checkout at a new path and initialise its recursive gitlinks:

```sh
source_checkout=/tmp/cairn-falkordb-v4.20.4-cairn.1-source
test ! -e "$source_checkout"
git clone --filter=blob:none --no-checkout \
  https://github.com/FalkorDB/FalkorDB.git "$source_checkout"
git -C "$source_checkout" fetch --depth=1 origin refs/pull/2838/head
git -C "$source_checkout" checkout --detach \
  c3fea9bee0d7d4ad6c24308f18a19fc8c81c2996
git -C "$source_checkout" submodule sync --recursive
git -C "$source_checkout" submodule update --init --recursive
python3 deploy/falkordb/prepare_source_bundle.py \
  --falkordb-checkout "$source_checkout" \
  --runtime-image \
    cairn-local/falkordb-server@sha256:37a9377eda8a9fd493817869bc2ce4f7d110f054ad74a8fe7d39c3689be3a578 \
  --download-cache build/v05-source-downloads \
  --output-dir build/v05-evidence/falkordb-source
```

The download cache is outside the produced archive. Redis is verified by its
published archive SHA-256. Debian Snapshot returns the source files associated
with each exact package/version pair; the generator verifies each downloaded
file's snapshot SHA-1, then verifies the stronger sizes and SHA-256 values in
the signed `.dsc`. Cached bytes are rechecked before use.

## Verify and inspect

```sh
cd build/v05-evidence/falkordb-source
sha256sum --check cairn-falkordb-v4.20.4-cairn.1-source.tar.gz.sha256
mkdir inspected
tar -xzf cairn-falkordb-v4.20.4-cairn.1-source.tar.gz -C inspected
cd inspected/cairn-falkordb-v4.20.4-cairn.1-source
sha256sum --check SHA256SUMS
```

Review `MODIFICATIONS.md`, `metadata/runtime-image.json`,
`metadata/falkordb-repositories.json`, `metadata/debian-packages.tsv` and
`metadata/debian-source-files.json`. The Debian source files are under
`sources/debian/<source>/<version>/`; the original Redis archive is under
`sources/`. `notices/debian-copyright.tar.gz` contains one resolved copyright
text path per installed binary package.

`build-recipe/build.sh` rebuilds the derivative using the retained upstream
recipe and exact module/base identities. Package repositories are mutable, so
a later rebuild is not expected to be bit-identical and must receive new
provenance, scanning and acceptance. The bundle documents the tested extracted
module; it does not claim that module was reproduced from this source checkout.

## Distribution boundary

Publish this checksummed source archive adjacent to the exact binary archive
and image reference, at no charge, only after the release authority permits
publication. Keep it available for recipients of that derivative even after a
newer official image replaces it.

This inventory and automation reduce omissions; they are not legal advice or
a claim that software-licence obligations are universally identical. Preserve
all component notices and obtain legal review appropriate to the distribution.
