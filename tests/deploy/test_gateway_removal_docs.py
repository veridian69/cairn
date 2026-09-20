"""Execute the documented namespace inventory guard against removal leftovers."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

GUIDE = Path(__file__).parents[2] / "docs/operations/kubernetes-gateway.md"


def _namespace_removal_block() -> str:
    blocks: list[str] = re.findall(r"```bash\n(.*?)\n```", GUIDE.read_text(), re.DOTALL)
    return next(block for block in blocks if "namespaced_resources=" in block)


def _run_namespace_removal_block(
    tmp_path: Path, *, fail_wait: str = "", include_secret: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    inventory = tmp_path / "inventory"
    inventory.write_text("secret/stale-from-previous-attempt\n")
    calls = tmp_path / "kubectl-calls"
    harness = r"""
set -eu
set -o pipefail
gateway_namespace=cairn-egress
gateway_inventory="$TEST_INVENTORY"
kubectl() {
  printf '%s\n' "$*" >> "$TEST_CALLS"
  if test "$1" = wait; then
    case "$*:$FAIL_WAIT" in
      *' pod '*:pod|*ciliumendpoints.cilium.io*:cilium) return 19 ;;
    esac
    return 0
  fi
  if test "$1" = api-resources; then
    printf '%s\n' pods ciliumendpoints.cilium.io events events.events.k8s.io \
      serviceaccounts configmaps secrets
    return 0
  fi
  if test "$1" = get; then
    if test "$2" = namespace; then
      printf '%s' cairn-egress-gateway
      return 0
    fi
    case "$4" in
      events) printf '%s\n' event/gateway-old ;;
      events.events.k8s.io) printf '%s\n' event.events.k8s.io/gateway-old ;;
      serviceaccounts) printf '%s\n' serviceaccount/default ;;
      configmaps) printf '%s\n' configmap/kube-root-ca.crt ;;
      secrets) test "$INCLUDE_SECRET" != 1 || printf '%s\n' secret/unrelated ;;
    esac
    return 0
  fi
  return 97
}
"""
    environment = os.environ | {
        "TEST_INVENTORY": str(inventory),
        "TEST_CALLS": str(calls),
        "FAIL_WAIT": fail_wait,
        "INCLUDE_SECRET": "1" if include_secret else "0",
    }
    result = subprocess.run(
        ["bash", "-c", harness + "\n" + _namespace_removal_block()],
        cwd=GUIDE.parents[2],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, inventory, calls


@pytest.mark.parametrize(
    ("extra", "allowed"),
    [
        ("event/gateway-old\nevent.events.k8s.io/gateway-old\n", True),
        ("pod/unrelated\n", False),
        ("ciliumendpoint.cilium.io/unrelated\n", False),
        ("secret/unrelated\n", False),
    ],
)
def test_namespace_inventory_guard(tmp_path: Path, extra: str, allowed: bool) -> None:
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY", GUIDE.read_text(), re.DOTALL)
    guard = next(block for block in blocks if "namespace contains unexpected" in block)
    inventory = tmp_path / "inventory"
    inventory.write_text("serviceaccount/default\nconfigmap/kube-root-ca.crt\n" + extra)
    result = subprocess.run(
        [sys.executable, "-", str(inventory)],
        input=guard,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is allowed, result.stderr


def test_namespace_removal_waits_then_rebuilds_inventory_from_scratch(
    tmp_path: Path,
) -> None:
    result, inventory, calls = _run_namespace_removal_block(tmp_path)

    assert result.returncode == 0, result.stderr
    assert inventory.read_text().splitlines() == [
        "configmap/kube-root-ca.crt",
        "event.events.k8s.io/gateway-old",
        "event/gateway-old",
        "serviceaccount/default",
    ]
    recorded = calls.read_text().splitlines()
    assert recorded[1] == (
        "wait --namespace cairn-egress --for=delete pod --selector "
        "app.kubernetes.io/name=cairn-egress-gateway --timeout=120s"
    )
    assert recorded[2] == "api-resources --verbs=list --namespaced -o name"
    assert recorded[3] == (
        "wait --namespace cairn-egress --for=delete "
        "ciliumendpoints.cilium.io --all --timeout=120s"
    )
    assert all(
        line.startswith("get --namespace cairn-egress ") for line in recorded[4:]
    )


@pytest.mark.parametrize("wait", ["pod", "cilium"])
def test_namespace_removal_stops_before_inventory_when_a_wait_fails(
    tmp_path: Path, wait: str
) -> None:
    result, inventory, calls = _run_namespace_removal_block(tmp_path, fail_wait=wait)

    assert result.returncode != 0
    assert inventory.read_text() == "secret/stale-from-previous-attempt\n"
    assert not any(
        line.startswith("get --namespace ") for line in calls.read_text().splitlines()
    )


def test_namespace_removal_shell_block_refuses_an_unrelated_resource(
    tmp_path: Path,
) -> None:
    result, _inventory, _calls = _run_namespace_removal_block(
        tmp_path, include_secret=True
    )

    assert result.returncode != 0
    assert "namespace contains unexpected objects: secret/unrelated" in result.stderr
