from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from cairn_install.core import MAX_OUTPUT, InstallError
from cairn_install.listing import list_instances


def _state(
    root: Path,
    name: str,
    *,
    mode: str = "native",
    status: str = "verified",
    port: int = 18000,
    semantic: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": 1,
        "owner_uid": os.getuid(),
        "name": name,
        "run_id": f"run-{name}",
        "instance_id": {
            "alpha": "11111111-1111-4111-8111-111111111111",
            "zed": "22222222-2222-4222-8222-222222222222",
        }.get(name, "33333333-3333-4333-8333-333333333333"),
        "mode": mode,
        "status": status,
        "port": port,
        "semantic": semantic,
    }
    directory = root / name
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return root


def test_missing_state_root_returns_empty_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "missing"

    assert list_instances(root) == []
    assert not root.exists()


def test_lists_recorded_state_in_name_order_and_ignores_unrelated_entries(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _state(
        root,
        "zed",
        mode="docker",
        status="failed",
        port=19002,
        semantic=True,
    )
    _state(root, "alpha", status="planned", port=19001)
    (root / "shared.json").write_text("not installer state")
    (root / ".cache").mkdir()
    (root / "invalid_NAME").mkdir()

    assert list_instances(root) == [
        {
            "name": "alpha",
            "mode": "native",
            "status": "planned",
            "port": 19001,
            "features": "Attic only",
            "instance_id": "11111111-1111-4111-8111-111111111111",
        },
        {
            "name": "zed",
            "mode": "docker",
            "status": "failed",
            "port": 19002,
            "features": "Attic plus semantic search",
            "instance_id": "22222222-2222-4222-8222-222222222222",
        },
    ]


def test_protected_recovery_journal_is_authoritative_and_deduplicated(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    recovery = _state(root, "demo", status="verified")
    recovery.update(
        mode="docker",
        status="blitzing",
        port=19003,
        semantic=True,
        blitz_phase="resources_removed",
    )
    journal = root / ".demo.blitz.json"
    journal.write_text(json.dumps(recovery))
    journal.chmod(0o600)

    assert list_instances(root) == [
        {
            "name": "demo",
            "mode": "docker",
            "status": "blitzing",
            "port": 19003,
            "features": "Attic plus semantic search",
            "instance_id": "33333333-3333-4333-8333-333333333333",
        }
    ]

    for child in (root / "demo").iterdir():
        child.unlink()
    (root / "demo").rmdir()
    assert list_instances(root)[0]["status"] == "blitzing"


def test_bad_instance_state_is_unavailable_without_hiding_valid_siblings_or_secrets(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _state(root, "good")
    corrupt = root / "corrupt"
    corrupt.mkdir(mode=0o700)
    state = corrupt / "state.json"
    state.write_text('{"credential":"do-not-show"')
    state.chmod(0o600)
    foreign = _state(root, "foreign")
    foreign["owner_uid"] = os.getuid() + 1
    foreign_path = root / "foreign" / "state.json"
    foreign_path.write_text(json.dumps(foreign))
    foreign_path.chmod(0o600)
    linked = root / "linked"
    linked.mkdir(mode=0o700)
    (linked / "state.json").symlink_to(root / "good" / "state.json")

    rows = list_instances(root)

    assert [row["name"] for row in rows] == ["corrupt", "foreign", "good", "linked"]
    assert [row["status"] for row in rows] == [
        "unavailable",
        "unavailable",
        "verified",
        "unavailable",
    ]
    assert "do-not-show" not in repr(rows)


def test_non_string_rendered_fields_cannot_abort_or_inject_into_listing(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    value = _state(root, "broken")
    value["mode"] = ["native"]
    value["status"] = "verified\nforged-row"
    path = root / "broken" / "state.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)

    assert list_instances(root) == [
        {
            "name": "broken",
            "status": "unavailable",
            "error": "invalid installer state",
        }
    ]


def test_invalid_instance_directory_and_oversized_state_are_unavailable(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    loose = root / "loose"
    loose.mkdir(mode=0o755)
    oversized = root / "oversized"
    oversized.mkdir(mode=0o700)
    state = oversized / "state.json"
    state.write_bytes(b"x" * (MAX_OUTPUT + 1))
    state.chmod(0o600)

    assert [row["status"] for row in list_instances(root)] == [
        "unavailable",
        "unavailable",
    ]


def test_valid_name_file_collision_is_unavailable_without_reading_it(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    collision = root / "blocked"
    collision.write_text("do-not-read")

    rows = list_instances(root)

    assert rows == [
        {
            "name": "blocked",
            "status": "unavailable",
            "error": "invalid installer state",
        }
    ]
    assert "do-not-read" not in repr(rows)


@pytest.mark.parametrize(
    "payload",
    [
        "[" * 20_000 + "0" + "]" * 20_000,
        '{"ignored":' + "1" * 5_000 + "}",
    ],
    ids=("deep", "large-integer"),
)
def test_pathological_json_is_unavailable_without_hiding_a_valid_sibling(
    tmp_path: Path, payload: str
) -> None:
    root = _root(tmp_path)
    nested = root / "nested"
    nested.mkdir(mode=0o700)
    state = nested / "state.json"
    state.write_text(payload)
    state.chmod(0o600)
    _state(root, "good")

    assert [row["status"] for row in list_instances(root)] == [
        "verified",
        "unavailable",
    ]


def test_invalid_recovery_journal_overrides_surviving_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _state(root, "demo")
    secret = tmp_path / "secret"
    secret.write_text("do-not-read")
    journal = root / ".demo.blitz.json"
    journal.symlink_to(secret)

    assert list_instances(root) == [
        {
            "name": "demo",
            "status": "unavailable",
            "error": "invalid blitz recovery journal",
        }
    ]


def test_refuses_symlink_or_non_private_state_root(tmp_path: Path) -> None:
    root = _root(tmp_path)
    link = tmp_path / "linked-state"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(InstallError, match="symlink"):
        list_instances(link)

    root.chmod(0o755)
    with pytest.raises(InstallError, match="0700"):
        list_instances(root)


def test_refuses_symlink_hidden_before_dot_dot_in_state_root(tmp_path: Path) -> None:
    _root(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "child").mkdir()
    link = tmp_path / "linked-parent"
    link.symlink_to(elsewhere / "child", target_is_directory=True)

    with pytest.raises(InstallError, match="symlink"):
        list_instances(link / ".." / "state")


def test_shared_root_lock_refuses_concurrent_cleanup(tmp_path: Path) -> None:
    root = _root(tmp_path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallError, match="cleanup is busy"):
            list_instances(root)
    finally:
        os.close(fd)
