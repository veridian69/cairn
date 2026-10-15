# Modification notice — Cairn local FalkorDB 4.20.7 recipe

Recipe refreshed on 15 October 2026 from FalkorDB v4.20.7 and Redis 8.10.2,
upstream commit `2811bc690c37558b345c0f8491c05424729e6953`.
A local image was built, scanned and accepted from these pins on 15 October 2026.

Cairn's `build.sh` checks out that immutable commit and its recursive gitlinks.
`Dockerfile.source` compiles the module rather than extracting it from a vendor
image, and separately compiles the Redis 8.10.2 core (`make -C src`) from its
hash-verified release source. It applies no patch to the FalkorDB checkout. The copies under `upstream/` mirror
[the tagged server recipe and scripts](https://github.com/FalkorDB/FalkorDB/tree/v4.20.7/build/docker)
and [licence](https://github.com/FalkorDB/FalkorDB/blob/v4.20.7/LICENSE.txt);
they are reference material. The runtime takes its scripts from the verified checkout.

Cairn uses a digest-pinned Ubuntu 24.04 base, refreshes its APT packages, omits
FalkorDB Browser, runs as UID/GID 10001, and retains the bounded module query
arguments. Redis uses TLS support without bundled modules. Cairn disables
VecSim tests and AVX/AVX512 specialisation for its generic amd64 baseline and
provides an immutable `cpu_features` source checkout for offline compilation.
Cairn's Redis matches upstream's server recipe, 8.10.2. Redis 8.10's default
`make` also builds its bundled modules; Cairn builds the core only. Fresh build
identities, licence inventories, scanning and runtime acceptance must be
recorded before selecting a deployment digest.

The FalkorDB material retains its upstream Server Side Public License.
Redis and all supporting components retain their upstream licence texts,
copied from the fetched sources into `/usr/share/licenses/cairn-falkordb`.
Nothing in this recipe relicenses those components under Cairn's Apache licence.
The historical corresponding-source generator and archive apply only to the
older distributed derivative; see `SOURCE_BUNDLE.md` and immutable release tags.
