"""Local shell simulations: never run the installer, Docker or host setup."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/pre-blind"


def shell(body: str, work: Path) -> subprocess.CompletedProcess[str]:
    # Load function definitions only, excluding all top-level setup and dispatch.
    source = SCRIPT.read_text().split("# --- configuration table", 1)[1]
    source = "# --- configuration table" + source.split("trap 'forward_stop", 1)[0]
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", source + "\n" + body],
        cwd=ROOT,
        env={**os.environ, "SIM_WORK": str(work)},
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_gops_matrix_listing_simulation(tmp_path: Path) -> None:
    result = shell(
        r"""
PRE_BLIND_STAGES=matrix
PRE_BLIND_CONFIGS="nat-gops dock-gops"
GARDEN_DOMAIN=example.test
run_dir=$SIM_WORK
printf 'SIMULATION stages=%s configs=%s\n' "$PRE_BLIND_STAGES" "$PRE_BLIND_CONFIGS"
for name in $PRE_BLIND_CONFIGS; do
  printf '%s %s %s %s %s %s\n' "$name" "$(config_mode "$name")" "$(config_feature "$name")" "$(config_port "$name")" "$(config_garden_port "$name")" "$(config_garden_host "$name")"
  [[ $(install_args "$name") != *--garden-config* ]]
  [[ $(config_garden_dir "$name") == "$HOME/garden-tls-$name" ]]
  garden_configured "$name"
done
any_garden_configured
for fn in garden_configured any_garden_configured garden_operations_source garden_state garden_container garden_python garden_version garden_data_has garden_messages garden_message_checks garden_refusal garden_backup_config garden_operations_config garden_interruption_config kubernetes_garden_refusals garden_token_status garden_remove_config; do
  declare -F "$fn"
done
installer() { echo 'unexpected installer call' >&2; return 99; }
docker() { echo 'unexpected Docker call' >&2; return 99; }
log() { :; }
install_config() { echo "install $1"; }
verify_config() { echo "verify $1"; }
restart_config() { echo "restart $1"; }
lifecycle_config() { echo "lifecycle $1"; }
garden_operations_config() { echo "Garden operations $1"; }
matrix
""",
        tmp_path,
    )
    assert (
        "nat-gops native attic 8214 8414 garden-nat-gops.example.test" in result.stdout
    )
    assert (
        "dock-gops docker attic 8224 8424 garden-dock-gops.example.test"
        in result.stdout
    )
    for name in ("nat-gops", "dock-gops"):
        assert f"lifecycle {name}\nGarden operations {name}" in result.stdout
    assert "garden_remove_config" in result.stdout


@pytest.mark.parametrize("mutate,expected", [(False, "passed"), (True, "failed")])
def test_kubernetes_refusal_detects_state_mutation(
    tmp_path: Path, mutate: bool, expected: str
) -> None:
    result = shell(
        r"""
run_dir=$SIM_WORK
state_root=$SIM_WORK/state
checks=$SIM_WORK/checks.jsonl
failed=0
mkdir -p "$run_dir/logs" "$state_root/kube-attic"
echo '{"status":"verified"}' >"$state_root/kube-attic/state.json"
installer() {
  echo "$*" >>"$SIM_WORK/calls"
  if [[ MUTATE == yes ]]; then echo ' ' >>"$state_root/kube-attic/state.json"; fi
  echo 'error [invalid_arguments]: Kubernetes operation refused'
  return 2
}
kubernetes_garden_refusals
cat "$checks"
cat "$SIM_WORK/calls"
""".replace("MUTATE == yes", f"{'yes' if mutate else 'no'} == yes"),
        tmp_path,
    )
    expected_observed = "yes" if not mutate else "no"
    assert (
        result.stdout.count(f'"observed":"{expected_observed}","outcome":"{expected}"')
        >= 3
    )
    for operation in ("garden-add", "garden-upgrade", "garden-backup"):
        assert f"{operation} " in result.stdout
    assert "--garden-config" in result.stdout


def test_second_source_changes_version_and_garden_fingerprint(tmp_path: Path) -> None:
    result = shell(
        r"""
run_dir=$SIM_WORK
garden_operations_source
python3 - "$garden_source_b" <<'CHECK'
import sys
import tomllib
from pathlib import Path
from cairn_install.garden import garden_fingerprint
from cairn_install.garden_upgrade import _version
root = Path(sys.argv[1])
version = lambda path: tomllib.loads((path / "pyproject.toml").read_text())["project"]["version"]
assert _version(version(root)) > _version(version(Path.cwd()))
assert garden_fingerprint(root) != garden_fingerprint(Path.cwd())
print("different version and fingerprint")
CHECK
""",
        tmp_path,
    )
    assert "different version and fingerprint" in result.stdout


def test_backup_expects_a_verified_archive(tmp_path: Path) -> None:
    result = shell(
        r"""
run_dir=$SIM_WORK
installer_dir=$PWD
mkdir -p "$run_dir/logs"
garden_state() {
  case $2 in
    .garden.backup.phase) echo complete;;
    .garden.backup.archive) echo "$SIM_WORK/garden-backup-test.tar";;
  esac
}
record() { [[ $2 == "$3" ]]; }
run_step() { echo "$*"; }
garden_backup_config nat-gops
""",
        tmp_path,
    )
    assert "nat-gops-backup installer garden-backup" in result.stdout
    assert "nat-gops-backup-verify python3 -I" in result.stdout


@pytest.mark.parametrize("version,proceeds", [("0.7.20", False), ("0.8.0", True)])
def test_operations_require_a_release_candidate_checkout(
    tmp_path: Path, version: str, proceeds: bool
) -> None:
    result = shell(
        r"""
run_dir=$SIM_WORK
cd "$SIM_WORK"
printf '[project]\nversion = "VERSION"\n' > pyproject.toml
finding() { echo "finding: $1"; }
run_step() { echo "step: $1"; return 1; }
garden_operations_config nat-gops || true
""".replace("VERSION", version),
        tmp_path,
    )
    assert ("step: nat-gops-upgrade-source" in result.stdout) is proceeds
    assert (
        "finding: nat-gops needs a 0.8.0+ checkout" in result.stdout
    ) is not proceeds


@pytest.mark.parametrize("replacement", [False, True])
def test_interruption_requires_the_same_journal(
    tmp_path: Path, replacement: bool
) -> None:
    # This is a fake process and local journal, with the real phase watcher.
    installer = tmp_path / "cairn-install"
    installer.write_text(
        """#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from pathlib import Path
path = Path(os.environ["SIM_WORK"]) / "state/nat-gops/state.json"
state = json.loads(path.read_text())
if state["garden"]["upgrade"]["id"] == "old":
    state["garden"]["upgrade"] = {"id": "interrupted", "direction": "upgrade", "phase": "staged"}
    def stop(*args):
        state["status"] = "interrupted"
        path.write_text(json.dumps(state))
        sys.exit(130)
    signal.signal(signal.SIGTERM, stop)
    path.write_text(json.dumps(state))
    time.sleep(10)
    sys.exit(98)
journal = state["garden"]["upgrade"]
if os.environ.get("SIM_REPLACE") == "yes":
    journal["id"] = "replacement"
journal["phase"] = "complete"
state["status"] = "verified"
state["garden"]["upgrades"].append({"id": journal["id"]})
path.write_text(json.dumps(state))
"""
    )
    installer.chmod(0o700)
    result = shell(
        r"""
run_dir=$SIM_WORK
state_root=$SIM_WORK/state
checks=$SIM_WORK/checks.jsonl
installer_dir=$SIM_WORK
check_dir=$PWD/tests/acceptance/installer
garden_source_b=$SIM_WORK/source
failed=0
export SIM_REPLACE=REPLACE
mkdir -p "$run_dir/logs" "$state_root/nat-gops"
echo '{"garden":{"upgrade":{"id":"old"},"upgrades":[]}}' >"$state_root/nat-gops/state.json"
installer() { "$SIM_WORK/cairn-install" "$@"; }
garden_messages() { :; }
garden_message_checks() { :; }
garden_interruption_config nat-gops G0
cat "$checks"
""".replace("SIM_REPLACE=REPLACE", f"SIM_REPLACE={'yes' if replacement else 'no'}"),
        tmp_path,
    )
    expected = "failed" if replacement else "passed"
    lines = result.stdout.splitlines()
    check = next(line for line in lines if '"name":"nat-gops rerun retains' in line)
    assert f'"outcome":"{expected}"' in check


@pytest.mark.parametrize("content", [None, "{", '{"content": ""}', '{"content": 7}'])
def test_missing_or_malformed_message_never_matches(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "messages/nat-gops").mkdir(parents=True)
        (tmp_path / "messages/nat-gops/M1.json").write_text(content)
    result = shell(
        r"""
run_dir=$SIM_WORK
state_root=$SIM_WORK/state
mkdir -p "$state_root/nat-gops/instance/garden/data"
printf 'any stored bytes\n' >"$state_root/nat-gops/instance/garden/data/state.db"
config_mode() { echo native; }
echo "nonce=[$(message_nonce nat-gops M1)]"
echo "found=$(garden_data_has nat-gops data "$(message_nonce nat-gops M1)")"
""",
        tmp_path,
    )
    assert "nonce=[]" in result.stdout
    assert "found=no" in result.stdout


def test_participant_token_removed_when_garden_remove_fails(tmp_path: Path) -> None:
    result = shell(
        r"""
run_dir=$SIM_WORK
state_root=$SIM_WORK/state
mkdir -p "$state_root/nat-gops/instance/garden/profiles/spike"
printf 'token' >"$state_root/nat-gops/instance/garden/profiles/spike/agent.token"
printf '{"garden":{"options":{}}}' >"$state_root/nat-gops/state.json"
garden_token_status() { stat -c 'mode=%a' "$2"; }
record() { echo "record $1 -> $3"; }
run_step() { [[ $1 != nat-gops-garden-remove ]]; }
status=0; garden_remove_config nat-gops || status=$?
echo "status=$status"
ls "$run_dir" | grep -c old-participant || true
""",
        tmp_path,
    )
    assert "token initially accepted -> mode=600" in result.stdout
    assert "status=1" in result.stdout
    assert "rejected" not in result.stdout
    assert result.stdout.rstrip().endswith("0")


@pytest.mark.parametrize(
    "existing,owned",
    [
        ("bin/garden", False),
        ("bin/garden-config", False),
        ("bin/garden-session", False),
        ("share/garden", False),
        ("dangling", False),
        ("bin/garden-config", True),
    ],
)
def test_foreign_adapter_install_is_never_replaced(
    tmp_path: Path, existing: str, owned: bool
) -> None:
    result = shell(
        r"""
HOME=$SIM_WORK
adapter_marker="$HOME/.local/state/cairn-pre-blind/garden-adapter-owned"
# The script's own list, so removal and the guard cannot drift apart.
eval "$(sed -n '/^adapter_paths=(/,/)$/p' scripts/pre-blind)"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share"
case EXISTING in
  dangling) ln -s "$HOME/missing" "$HOME/.local/bin/garden-session";;
  *) printf 'foreign' >"$HOME/.local/EXISTING";;
esac
[[ OWNED == 0 ]] || { mkdir -p "${adapter_marker%/*}"; : >"$adapter_marker"; }
finding() { echo "finding: $1"; }
run_step() { echo "step: $1"; return 7; }
# Exercise only the guarded adapter block of prereqs, up to the first build.
guarded=$(sed -n '/# Adapter binary for participant verification/,/run_step garden-build/p' scripts/pre-blind)
eval "adapter_block() { $guarded
}"
status=0; adapter_block || status=$?
echo "status=$status"
echo "marker=$([[ -e $adapter_marker ]] && echo yes || echo no)"
""".replace("EXISTING", existing).replace("OWNED", "1" if owned else "0"),
        tmp_path,
    )
    if owned:
        assert "step: garden-build" in result.stdout
        assert "finding" not in result.stdout
    else:
        assert "finding: existing Garden adapter in ~/.local" in result.stdout
        assert "step: garden-build" not in result.stdout
        # A refusal never vouches for the foreign files, so removal keeps them.
        assert "marker=no" in result.stdout
