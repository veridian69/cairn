#!/usr/bin/env bash
# Rebuild the maintained amd64 runtime without changing the accepted release tag.
# APT security repositories move: a rebuild has a new digest and needs acceptance.
set -euo pipefail

if (( $# != 0 )); then
  printf 'usage: %s\nBuilds cairn-local/falkordb-server:rebuild; does not publish it.\n' "$0" >&2
  exit 2
fi

recipe_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_image=falkordb/falkordb:v4.20.4@sha256:adbddd418916c25618564ff8597a919b08bc76452ebeb74eb985c38d7281df62
source_digest=falkordb/falkordb@sha256:adbddd418916c25618564ff8597a919b08bc76452ebeb74eb985c38d7281df62
redis_image=redis:8.6.3@sha256:4d25e2fe601f7ffaeb4437cb6ced3518bc36edf34ebe98863c80836943d94529
redis_digest=redis@sha256:4d25e2fe601f7ffaeb4437cb6ced3518bc36edf34ebe98863c80836943d94529
module_sha=81ea6b989dc2fd4c9ad905e246018b220b02f0e40c406255f9da4768c1684555
output_image=cairn-local/falkordb-server:rebuild

for command in docker python3 sha256sum mktemp; do
  command -v "$command" >/dev/null || { printf 'required command missing: %s\n' "$command" >&2; exit 1; }
done
(cd "$recipe_dir/upstream" && sha256sum --check SHA256SUMS)

build_dir=$(mktemp -d "${TMPDIR:-/tmp}/cairn-falkordb-build.XXXXXXXX")
source_container=
shim_image="cairn-local/falkordb-compiler-shim:build-${build_dir##*.}"
cleanup() {
  if [[ -n "$source_container" ]]; then docker rm -v "$source_container" >/dev/null 2>&1 || true; fi
  docker image rm "$shim_image" >/dev/null 2>&1 || true
  rm -rf -- "$build_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for image in "$source_image" "$redis_image"; do
  docker pull --platform linux/amd64 "$image"
  [[ $(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image") == linux/amd64 ]] || {
    printf 'image platform differs from linux/amd64\n' >&2; exit 1;
  }
done
docker image inspect --format '{{join .RepoDigests "\n"}}' "$source_image" | grep -Fx -- "$source_digest" >/dev/null
docker image inspect --format '{{join .RepoDigests "\n"}}' "$redis_image" | grep -Fx -- "$redis_digest" >/dev/null

mkdir -p "$build_dir/compiler" "$build_dir/context/build/docker"
source_container=$(docker create --network none --entrypoint /bin/true "$source_image")
docker cp "$source_container:/var/lib/falkordb/bin/falkordb.so" "$build_dir/compiler/falkordb.so"
printf '%s  %s\n' "$module_sha" "$build_dir/compiler/falkordb.so" | sha256sum --check -
docker rm -v "$source_container" >/dev/null
source_container=
printf 'FROM scratch\nCOPY falkordb.so /FalkorDB/bin/linux-x64-release/falkordb.so\n' > "$build_dir/compiler/Dockerfile"
docker build --platform linux/amd64 --progress=plain --tag "$shim_image" "$build_dir/compiler"

cp "$recipe_dir/upstream/run.sh" "$recipe_dir/upstream/gen-certs.sh" "$build_dir/context/build/docker/"
python3 - "$recipe_dir/upstream/Dockerfile.server" "$build_dir/context/Dockerfile" "$redis_image" <<'PY'
import pathlib
import sys

source, target, redis = sys.argv[1:]
dockerfile = pathlib.Path(source).read_bytes()
original = b"FROM redis:8.6.3\n"
if dockerfile.count(original) != 1:
    raise SystemExit("expected exactly one upstream Redis FROM instruction")
pathlib.Path(target).write_bytes(dockerfile.replace(original, f"FROM {redis}\n".encode()))
PY
docker build --no-cache --platform linux/amd64 --progress=plain \
  --build-arg "BASE_IMAGE=$shim_image" --tag "$output_image" "$build_dir/context"
docker image inspect --format '{{.Id}}' "$output_image"
printf 'Built %s. Scan and repeat acceptance before selecting its digest for release.\n' "$output_image"
