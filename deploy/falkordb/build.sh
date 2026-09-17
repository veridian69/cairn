#!/usr/bin/env bash
# Build the maintained linux/amd64 runtime from immutable upstream sources.
set -euo pipefail

if (( $# != 0 )); then
  printf 'usage: %s\nBuilds cairn-local/falkordb-server:rebuild; does not publish it.\n' "$0" >&2
  exit 2
fi
jobs=${CAIRN_BUILD_JOBS:-10}
if [[ ! $jobs =~ ^[0-9]+$ ]] || (( jobs < 1 || jobs > 10 )); then
  printf 'CAIRN_BUILD_JOBS must be an integer from 1 to 10\n' >&2; exit 2
fi

recipe_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
falkor_commit=5ac6db8059013c9d74842c02b6a9f1a4858a6a1b
cpu_features_commit=438a66e41807cd73e0c403966041b358f5eafc68
redis_version=8.6.3
redis_sha=9f54d4458c52be5472cdd1347d737f1d488b520fc3d0911cba47302de8d836e2
output_image=cairn-local/falkordb-server:rebuild
for command in docker git curl sha256sum mktemp; do
  command -v "$command" >/dev/null || { printf 'required command missing: %s\n' "$command" >&2; exit 1; }
done

build_dir=$(mktemp -d "${TMPDIR:-/tmp}/cairn-falkordb-source.XXXXXXXX")
cleanup() { rm -rf -- "$build_dir"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

git clone --quiet --no-checkout https://github.com/FalkorDB/FalkorDB.git "$build_dir/FalkorDB"
git -C "$build_dir/FalkorDB" checkout --quiet --detach "$falkor_commit"
git -C "$build_dir/FalkorDB" submodule update --init --recursive --jobs "$jobs"
head_commit=$(git -C "$build_dir/FalkorDB" rev-parse HEAD)
[[ $head_commit == "$falkor_commit" ]]
submodule_status=$(git -C "$build_dir/FalkorDB" submodule status --recursive)
if grep -Eq '^[+-U]' <<<"$submodule_status"; then
  printf 'FalkorDB recursive submodule checkout differs from the pinned gitlinks\n' >&2; exit 1
fi
source_status=$(git -C "$build_dir/FalkorDB" status --porcelain --untracked-files=all)
if [[ -n $source_status ]] ||
   ! git -C "$build_dir/FalkorDB" submodule foreach --quiet --recursive \
     'status=$(git status --porcelain --untracked-files=all) && test -z "$status"'; then
  printf 'FalkorDB source or a recursive submodule is dirty\n' >&2; exit 1
fi

git clone --quiet --no-checkout https://github.com/google/cpu_features.git "$build_dir/cpu_features"
git -C "$build_dir/cpu_features" checkout --quiet --detach "$cpu_features_commit"
cpu_features_head=$(git -C "$build_dir/cpu_features" rev-parse HEAD)
[[ $cpu_features_head == "$cpu_features_commit" ]]
cpu_features_status=$(git -C "$build_dir/cpu_features" status --porcelain --untracked-files=all)
[[ -z $cpu_features_status ]]
curl --fail --location --retry 3 --output "$build_dir/redis.tar.gz" \
  "https://download.redis.io/releases/redis-${redis_version}.tar.gz"
printf '%s  %s\n' "$redis_sha" "$build_dir/redis.tar.gz" | sha256sum --check -
cp "$recipe_dir/Dockerfile.source" "$build_dir/Dockerfile"
docker build --no-cache --platform linux/amd64 --progress=plain \
  --build-arg "BUILD_JOBS=$jobs" --build-arg "REDIS_VERSION=$redis_version" \
  --tag "$output_image" "$build_dir"
docker image inspect --format '{{.Id}}' "$output_image"
printf 'Built %s. Scan and repeat acceptance before selecting its digest for release.\n' "$output_image"
