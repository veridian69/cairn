from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import pytest


@pytest.fixture
def acceptance(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    scripts = Path(__file__).parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    sys.modules.pop("test_macos_native", None)
    return importlib.import_module("test_macos_native")


def test_native_commands_keep_each_operation_bounded(
    acceptance: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    state = tmp_path / "state"

    install = acceptance.native_command(source, state, "notes", "install", port=8123)
    resume = acceptance.native_command(source, state, "notes", "resume")
    rollback = acceptance.native_command(source, state, "notes", "rollback")
    blitz = acceptance.native_command(source, state, "notes", "blitz")

    assert install[2:] == [
        "install",
        "--non-interactive",
        "--name",
        "notes",
        "--state-root",
        str(state),
        "--source",
        str(source),
        "--mode",
        "native",
        "--port",
        "8123",
    ]
    assert resume[2:] == [
        "resume",
        "--non-interactive",
        "--name",
        "notes",
        "--state-root",
        str(state),
        "--source",
        str(source),
    ]
    assert rollback[2:] == [
        "rollback",
        "--non-interactive",
        "--name",
        "notes",
        "--state-root",
        str(state),
    ]
    assert blitz[2:] == [
        "blitz",
        "--non-interactive",
        "--name",
        "notes",
        "--state-root",
        str(state),
        "--yes",
    ]
    assert "--keep-running" not in install + resume + rollback + blitz


def test_launch_agent_receipt_binds_instance_plist_and_loaded_bytes(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(acceptance, "account_home", lambda: tmp_path)
    uid = os.getuid()
    instance_id = str(UUID("01234567-89ab-4cde-8f01-23456789abcd"))
    label = f"invalid.example.cairn.{instance_id}"
    plist = tmp_path / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("exact plist\n", encoding="utf-8")
    plist.chmod(0o644)
    receipt = {
        "schema": acceptance.LAUNCH_AGENT_SCHEMA,
        "uid": uid,
        "domain": f"gui/{uid}",
        "label": label,
        "path": str(plist),
        "plist_sha256": acceptance.shared.file_sha256(plist),
        "status": "loaded",
    }
    state = {
        "instance_id": instance_id,
        "resources": {acceptance.LAUNCH_AGENT_RESOURCE: receipt},
    }

    projected = acceptance.launch_agent_receipt(state, expected_status="loaded")

    assert projected == receipt
    acceptance._atomic_replace(plist, b"replacement\n")
    assert plist.stat().st_mode & 0o777 == 0o644
    with pytest.raises(
        acceptance.shared.AcceptanceFailure,
        match="ownership or digest",
    ):
        acceptance.launch_agent_receipt(state, expected_status="loaded")


def test_stopped_receipt_retains_plist_and_removed_receipt_requires_absence(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(acceptance, "account_home", lambda: tmp_path)
    uid = os.getuid()
    instance_id = str(UUID("01234567-89ab-4cde-8f01-23456789abcd"))
    label = f"invalid.example.cairn.{instance_id}"
    plist = tmp_path / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("owned\n", encoding="utf-8")
    plist.chmod(0o644)
    receipt = {
        "schema": acceptance.LAUNCH_AGENT_SCHEMA,
        "uid": uid,
        "domain": f"gui/{uid}",
        "label": label,
        "path": str(plist),
        "plist_sha256": acceptance.shared.file_sha256(plist),
        "status": "stopped",
    }
    state = {
        "instance_id": instance_id,
        "resources": {acceptance.LAUNCH_AGENT_RESOURCE: receipt},
    }

    assert acceptance.launch_agent_receipt(state, expected_status="stopped") == receipt
    plist.unlink()
    receipt["status"] = "removed"
    assert acceptance.launch_agent_receipt(state, expected_status="removed") == receipt
    plist.write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(
        acceptance.shared.AcceptanceFailure,
        match="still has a plist",
    ):
        acceptance.launch_agent_receipt(state, expected_status="removed")


def test_target_absence_requires_exact_profile_and_rechecks_domain(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = os.getuid()
    domain = f"gui/{uid}"
    label = "invalid.example.cairn.01234567-89ab-4cde-8f01-23456789abcd"
    calls: list[list[str]] = []
    results = iter(
        [
            subprocess.CompletedProcess([], 0, b"domain state\n", b""),
            subprocess.CompletedProcess(
                [],
                113,
                b"",
                (
                    f'Bad request.\nCould not find service "{label}" in domain '
                    f"for user gui: {uid}\n"
                ).encode(),
            ),
            subprocess.CompletedProcess([], 0, b"domain state\n", b""),
        ]
    )

    def launchctl(
        arguments: list[str],
        *,
        deadline: float,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[bytes]:
        del deadline, allowed
        calls.append(arguments)
        return next(results)

    monkeypatch.setattr(acceptance, "_launchctl", launchctl)

    assert not acceptance._target_loaded(domain, label, deadline=1.0)
    assert calls == [
        ["print", domain],
        ["print", f"{domain}/{label}"],
        ["print", domain],
    ]


def test_target_absence_rejects_an_arbitrary_113(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = os.getuid()
    domain = f"gui/{uid}"
    label = "invalid.example.cairn.01234567-89ab-4cde-8f01-23456789abcd"
    results = iter(
        [
            subprocess.CompletedProcess([], 0, b"domain state\n", b""),
            subprocess.CompletedProcess([], 113, b"", b"permission denied\n"),
        ]
    )

    def launchctl(
        arguments: list[str],
        *,
        deadline: float,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[bytes]:
        del arguments, deadline, allowed
        return next(results)

    monkeypatch.setattr(acceptance, "_launchctl", launchctl)

    with pytest.raises(
        acceptance.shared.AcceptanceFailure,
        match="absence response",
    ):
        acceptance._target_loaded(domain, label, deadline=1.0)


def test_wait_target_absent_polls_until_launchd_removes_label(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = iter([True, False])
    monkeypatch.setattr(
        acceptance,
        "_target_loaded",
        lambda domain, label, *, deadline: next(observed),
    )
    monkeypatch.setattr(acceptance.time, "sleep", lambda seconds: None)

    acceptance._wait_target_absent(
        "gui/501", "invalid.example.cairn.test", deadline=float("inf"), operation="test"
    )


def test_wait_target_absent_fails_at_its_bound(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([0.0, 0.0, 0.1])
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(acceptance.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        acceptance,
        "_target_loaded",
        lambda domain, label, *, deadline: True,
    )

    with pytest.raises(
        acceptance.shared.AcceptanceFailure,
        match="remained loaded after test",
    ):
        acceptance._wait_target_absent(
            "gui/501", "invalid.example.cairn.test", deadline=0.05, operation="test"
        )


def test_main_records_adapter_failure_without_exposing_a_credential(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "cairn-install").write_text("", encoding="utf-8")
    (source / "uv.lock").write_text("", encoding="utf-8")
    report = tmp_path / "report.json"
    work_root = tmp_path / "work"
    secret = "cairn1.01234567-89ab-4cde-8f01-23456789abcd." + "a" * 43
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        acceptance,
        "parse_args",
        lambda: SimpleNamespace(
            source=source,
            work_root=work_root,
            report=report,
            deadline=420,
            keep_work=False,
        ),
    )

    def fail(source: Path, work_root: Path, *, seconds: float) -> dict[str, object]:
        del source, work_root, seconds
        raise acceptance.InstallError(f"adapter rejected Bearer {secret}")

    monkeypatch.setattr(acceptance, "run_acceptance", fail)

    assert acceptance.main() == 1
    evidence = json.loads(report.read_text())
    assert evidence["status"] == "failed"
    assert secret not in report.read_text()
    assert evidence["failure"] == "adapter rejected Bearer [redacted credential]"


def test_foreign_variants_change_one_identity_dimension_and_use_public_plist(
    acceptance: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "invalid.example.cairn.instance.plist"
    expected = {
        "Program": "/usr/bin/env",
        "ProgramArguments": ["/usr/bin/env", "-i", "/runtime/cairn"],
        "KeepAlive": True,
        "RunAtLoad": True,
    }

    variants = acceptance.foreign_loaded_variants(expected, path)

    assert [name for name, _, _ in variants] == [
        "executable",
        "arguments",
        "environment",
        "restart-policy",
        "path",
    ]
    expected_differences = {
        "executable": {"Program"},
        "arguments": {"ProgramArguments"},
        "environment": {"EnvironmentVariables"},
        "restart-policy": {"KeepAlive"},
        "path": set(),
    }
    for name, profile, source_path in variants:
        keys = set(expected) | set(profile)
        assert {key for key in keys if expected.get(key) != profile.get(key)} == (
            expected_differences[name]
        )
        if name == "path":
            assert source_path.parent.parent == path.parent
            assert source_path != path
            assert not source_path.parent.name.startswith(".")
            assert source_path.name == path.name
        else:
            assert source_path == path


def test_foreign_bootstrap_failure_names_its_variant(
    acceptance: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise acceptance.shared.AcceptanceFailure(
            "launchctl bootstrap returned 5: Input/output error"
        )

    monkeypatch.setattr(acceptance, "_launchctl", fail)

    with pytest.raises(
        acceptance.shared.AcceptanceFailure,
        match="foreign path bootstrap failed.*returned 5",
    ):
        acceptance.bootstrap_foreign_variant(
            "gui/501", tmp_path / "alternate.plist", "path", deadline=1.0
        )
