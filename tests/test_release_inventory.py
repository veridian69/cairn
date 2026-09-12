from __future__ import annotations

import subprocess
import sys
from hashlib import sha256
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
CHECKER = REPOSITORY_ROOT / "scripts" / "check-release-inventory"


def _run_checker(lock: Path, inventory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(lock), str(inventory)],
        check=False,
        capture_output=True,
        text=True,
    )


def _lock_digest(lock: Path) -> str:
    return sha256(lock.read_bytes()).hexdigest()


def test_inventory_checker_accepts_an_exact_classified_lock(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1

[[package]]
name = "alpha"
version = "1.2.3"

[[package]]
name = "workspace-package"
source = { editable = "." }
""".lstrip()
    )
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        f"""
| `uv.lock` | `{_lock_digest(lock)}` |

<!-- python-dependencies:start -->
| Package | Locked version | SPDX licence | Classification |
|---|---:|---|---|
| `alpha` | `1.2.3` | `MIT` | permissive/mixed |
| `workspace-package` | workspace | `Apache-2.0` | permissive/mixed |
<!-- python-dependencies:end -->
""".lstrip()
    )

    result = _run_checker(lock, inventory)

    assert result.returncode == 0, result.stderr


def test_inventory_checker_rejects_lock_drift(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1

[[package]]
name = "alpha"
version = "2.0.0"
""".lstrip()
    )
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        f"""
| `uv.lock` | `{_lock_digest(lock)}` |

<!-- python-dependencies:start -->
| Package | Locked version | SPDX licence | Classification |
|---|---:|---|---|
| `alpha` | `1.2.3` | `MIT` | permissive/mixed |
<!-- python-dependencies:end -->
""".lstrip()
    )

    result = _run_checker(lock, inventory)

    assert result.returncode == 1
    assert "dependency inventory does not match uv.lock" in result.stderr


def test_inventory_checker_rejects_unclassified_licences(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1

[[package]]
name = "alpha"
version = "1.2.3"
""".lstrip()
    )
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        f"""
| `uv.lock` | `{_lock_digest(lock)}` |

<!-- python-dependencies:start -->
| Package | Locked version | SPDX licence | Classification |
|---|---:|---|---|
| `alpha` | `1.2.3` | `NOASSERTION` | unresolved |
<!-- python-dependencies:end -->
""".lstrip()
    )

    result = _run_checker(lock, inventory)

    assert result.returncode == 1
    assert "unclassified licence for alpha" in result.stderr


def test_inventory_checker_rejects_lock_provenance_drift(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1

[[package]]
name = "alpha"
version = "1.2.3"
source = { registry = "https://packages.example/first" }
""".lstrip()
    )
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        f"""
| `uv.lock` | `{_lock_digest(lock)}` |

<!-- python-dependencies:start -->
| Package | Locked version | SPDX licence | Classification |
|---|---:|---|---|
| `alpha` | `1.2.3` | `MIT` | permissive/mixed |
<!-- python-dependencies:end -->
""".lstrip()
    )
    lock.write_text(
        """
version = 1

[[package]]
name = "alpha"
version = "1.2.3"
source = { registry = "https://packages.example/second" }
""".lstrip()
    )

    result = _run_checker(lock, inventory)

    assert result.returncode == 1
    assert "uv.lock digest does not match inventory" in result.stderr


def test_inventory_checker_rejects_blank_classification(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1

[[package]]
name = "alpha"
version = "1.2.3"
""".lstrip()
    )
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        f"""
| `uv.lock` | `{_lock_digest(lock)}` |

<!-- python-dependencies:start -->
| Package | Locked version | SPDX licence | Classification |
|---|---:|---|---|
| `alpha` | `1.2.3` | `MIT` |  |
<!-- python-dependencies:end -->
""".lstrip()
    )

    result = _run_checker(lock, inventory)

    assert result.returncode == 1
    assert "unclassified licence for alpha" in result.stderr


def test_inventory_checker_rejects_unrecognised_classification(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1

[[package]]
name = "alpha"
version = "1.2.3"
""".lstrip()
    )
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        f"""
| `uv.lock` | `{_lock_digest(lock)}` |

<!-- python-dependencies:start -->
| Package | Locked version | SPDX licence | Classification |
|---|---:|---|---|
| `alpha` | `1.2.3` | `MIT` | not-a-classification |
<!-- python-dependencies:end -->
""".lstrip()
    )

    result = _run_checker(lock, inventory)

    assert result.returncode == 1
    assert "unclassified licence for alpha" in result.stderr
