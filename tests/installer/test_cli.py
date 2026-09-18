from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn_install import cli
from cairn_install.core import InstallError, open_context, open_read_context


def source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "src" / "cairn").mkdir(parents=True)
    (source / "src" / "cairn" / "__init__.py").write_text("\n")
    (source / "pyproject.toml").write_text("[project]\nname='cairn'\n")
    (source / "uv.lock").write_text("version = 1\n")
    return source


def test_configuration_is_flushed_before_later_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirected automation logs must keep the plan ahead of stderr errors."""

    class BufferedOutput(io.StringIO):
        flushed = False

        def flush(self) -> None:
            self.flushed = True
            super().flush()

    output = BufferedOutput()
    monkeypatch.setattr(sys, "stdout", output)

    cli._show_configuration(  # noqa: SLF001
        operation="install",
        name="demo",
        mode="docker",
        port=8123,
        semantic=True,
        source=tmp_path,
        state_root=tmp_path / "state",
    )

    assert output.flushed


def test_main_flushes_all_stdout_before_rendering_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BufferedOutput(io.StringIO):
        flushed = False

        def flush(self) -> None:
            self.flushed = True
            super().flush()

    output = BufferedOutput()

    class ErrorOutput(io.StringIO):
        def write(self, value: str) -> int:
            assert output.flushed
            return super().write(value)

    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", ErrorOutput())
    monkeypatch.setattr(
        cli,
        "_run",
        lambda args, source: (_ for _ in ()).throw(InstallError("failed")),
    )

    assert cli.main([]) == 2


def falkordb_runtime(tmp_path: Path) -> Path:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"local falkordb image archive")
    descriptor = tmp_path / "runtime.json"
    descriptor.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "image": "cairn.local/falkordb-runtime@sha256:" + "a" * 64,
                "local_tag": "cairn.local/falkordb-runtime:v0.7.0-local",
                "platform": "linux/amd64",
                "archive": archive.name,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "published": False,
            }
        )
    )
    return descriptor


@pytest.mark.parametrize("loader", ["receipt", "runtime-descriptor", "runtime-archive"])
def test_falkordb_metadata_loaders_reject_fifos_without_blocking(
    tmp_path: Path, loader: str
) -> None:
    fifo = tmp_path / "input"
    os.mkfifo(fifo)
    source = fifo
    function = "_load_falkordb_receipt"
    if loader == "runtime-descriptor":
        function = "_load_falkordb_runtime"
    elif loader == "runtime-archive":
        source = tmp_path / "runtime.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "image": "cairn.local/falkordb-runtime@sha256:" + "a" * 64,
                    "local_tag": "cairn.local/falkordb-runtime:test",
                    "platform": "linux/amd64",
                    "archive": fifo.name,
                    "archive_sha256": "b" * 64,
                    "published": False,
                }
            )
        )
        function = "_load_falkordb_runtime"
    code = (
        "from pathlib import Path\n"
        "from cairn_install import cli\n"
        "from cairn_install.core import InstallError\n"
        "try:\n"
        f"    cli.{function}(Path({str(source)!r}))\n"
        "except InstallError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=2, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


def test_falkordb_receipt_rejects_boolean_schema_version(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": True,
                "image": "registry.example/falkordb@sha256:" + "b" * 64,
                "archive_sha256": "c" * 64,
                "nodes": [{"name": "node1", "uid": "uid-node1"}],
            }
        )
    )

    with pytest.raises(InstallError, match="invalid schema"):
        cli._load_falkordb_receipt(receipt)  # noqa: SLF001


def test_garden_options_are_immutable_but_runtime_receipts_can_accumulate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cairn_install import garden

    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Linux"))
    source = source_tree(tmp_path)
    options = {"endpoint": "https://garden.example:8443/mcp", "port": 8443}
    monkeypatch.setattr(garden, "load_options", lambda path, mode: dict(options))

    def install(ctx: object) -> None:
        ctx.state["garden"]["agents"] = {"val": {"principal_id": "retained"}}  # type: ignore[attr-defined]
        ctx.save()  # type: ignore[attr-defined]

    install_workflow(monkeypatch, run_install=install)
    arguments = [
        "--non-interactive",
        "--mode",
        "native",
        "--name",
        "demo",
        "--garden-config",
        str(tmp_path / "garden.json"),
        "--state-root",
        str(tmp_path / "state"),
    ]
    assert cli.main(arguments, default_source=source) == 0
    assert cli.main(arguments, default_source=source) == 0
    options["endpoint"] = "https://replacement.example:8443/mcp"
    assert cli.main(arguments, default_source=source) == 2
    assert "Recorded Garden options differ" in capsys.readouterr().err


def test_garden_port_cannot_collide_with_cairn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn_install import garden

    source = source_tree(tmp_path)
    monkeypatch.setattr(garden, "load_options", lambda path, mode: {"port": 8000})
    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "demo",
                "--garden-config",
                str(tmp_path / "garden.json"),
                "--state-root",
                str(tmp_path / "state"),
            ],
            default_source=source,
        )
        == 2
    )
    assert not (tmp_path / "state" / "demo" / "state.json").exists()


def install_workflow(monkeypatch: pytest.MonkeyPatch, **functions: object) -> None:
    from cairn_install import workflow

    defaults: dict[str, object] = {
        "run_install": lambda ctx: None,
        "status_install": lambda ctx: None,
        "rollback_install": lambda ctx: None,
    }
    defaults.update(functions)
    for name, function in defaults.items():
        monkeypatch.setattr(workflow, name, function)


def test_non_interactive_missing_required_values_never_reads_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "builtins.input", lambda prompt: pytest.fail(f"read stdin: {prompt}")
    )

    result = cli.main(["--non-interactive", "--state-root", str(tmp_path)])

    assert result == 2
    assert "--mode is required in non-interactive mode" in capsys.readouterr().err


def test_interactive_wizard_uses_clear_feature_names_and_shows_plan_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Linux"))
    source = source_tree(tmp_path)
    answers = iter(["native", "demo", "8123", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    def run_install(ctx: object) -> None:
        output = capsys.readouterr().out
        assert "Attic only" in output
        assert "Attic plus semantic search" in output
        assert "Configuration" in output
        assert "Mode: native" in output
        assert "Port: 8123" in output

    install_workflow(monkeypatch, run_install=run_install)

    assert (
        cli.main(["--state-root", str(tmp_path / "state")], default_source=source) == 0
    )


def test_interactive_wizard_collects_semantic_kubernetes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    receipt = tmp_path / "falkordb-receipt.json"
    expected = {
        "schema_version": 1,
        "image": "cairn.local/falkordb-runtime@sha256:" + "b" * 64,
        "archive_sha256": "c" * 64,
        "nodes": [{"name": "node1", "uid": "uid-node1"}],
    }
    monkeypatch.setattr(cli, "_load_falkordb_receipt", lambda path: expected)
    answers = iter(
        [
            "kubernetes",
            "demo",
            "",
            "2",
            "test-context",
            "cairn-rwop",
            "registry.example/cairn@sha256:" + "a" * 64,
            str(receipt),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    args = cli._parser().parse_args([])  # noqa: SLF001
    result = cli._new_configuration(args, source)  # noqa: SLF001

    assert result[1] == "kubernetes"
    assert result[3] is True
    assert result[6]["falkordb_receipt"] == expected  # type: ignore[index]
    assert "kubernetes" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_interactive_wizard_collects_semantic_runtime_descriptor(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    descriptor = tmp_path / "runtime.json"
    expected = {"image": "local-semantic-runtime"}
    monkeypatch.setattr(cli, "_load_falkordb_runtime", lambda path: expected)
    answers = iter([mode, "demo", "", "2", str(descriptor)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    result = cli._new_configuration(  # noqa: SLF001
        cli._parser().parse_args([]),
        source,  # noqa: SLF001
    )

    assert result[1] == mode
    assert result[3] is True
    assert result[7] == expected


def test_disposable_is_always_attic_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    install_workflow(monkeypatch)

    result = cli.main(
        [
            "--non-interactive",
            "--mode",
            "disposable",
            "--name",
            "demo",
            "--semantic",
            "--state-root",
            str(tmp_path / "state"),
        ],
        default_source=source,
    )

    assert result == 2
    assert "Disposable mode supports Attic only" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [
        ("--kube-context", None, "--kube-context is required"),
        ("--kube-storage-class", None, "--kube-storage-class is required"),
        ("--kube-image", None, "--kube-image is required"),
        (
            "--kube-image",
            "registry.example/cairn:latest",
            "--kube-image must use a sha256 digest",
        ),
    ],
)
def test_kubernetes_requires_explicit_transport_inputs(
    flag: str,
    value: str | None,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = source_tree(tmp_path)
    install_workflow(monkeypatch)
    arguments = [
        "--non-interactive",
        "--mode",
        "kubernetes",
        "--name",
        "demo",
        "--kube-context",
        "reference",
        "--kube-storage-class",
        "cairn-rwop",
        "--kube-image",
        "registry.example/cairn@sha256:" + "a" * 64,
        "--state-root",
        str(tmp_path / "state"),
    ]
    index = arguments.index(flag)
    if value is None:
        del arguments[index : index + 2]
    else:
        arguments[index + 1] = value

    assert cli.main(arguments, default_source=source) == 2
    assert message in capsys.readouterr().err


def test_kubernetes_records_default_namespace_and_preloaded_image_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    seen: dict[str, object] = {}

    def run_install(ctx: object) -> None:
        seen.update(ctx.state["kubernetes"])  # type: ignore[attr-defined]

    install_workflow(monkeypatch, run_install=run_install)

    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "kubernetes",
                "--name",
                "demo",
                "--kube-context",
                "reference",
                "--kube-storage-class",
                "cairn-rwop",
                "--kube-image",
                "registry.example/cairn@sha256:" + "a" * 64,
                "--kube-preloaded-image",
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )

    assert seen == {
        "context": "reference",
        "namespace": "demo",
        "storage_class": "cairn-rwop",
        "image": "registry.example/cairn@sha256:" + "a" * 64,
        "image_policy": "IfNotPresent",
        "preloaded_image": True,
    }


def test_semantic_kubernetes_persists_falkordb_receipt_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    provider = tmp_path / "provider-key"
    provider.write_text("test-provider-key\n")
    provider.chmod(0o600)
    receipt_path = tmp_path / "falkordb-receipt.json"
    receipt = {
        "schema_version": 1,
        "image": "registry.example/falkordb@sha256:" + "b" * 64,
        "archive_sha256": "c" * 64,
        "nodes": [
            {"name": "worker-a", "uid": "11111111-1111-4111-8111-111111111111"},
            {"name": "worker-b", "uid": "22222222-2222-4222-8222-222222222222"},
        ],
    }
    receipt_path.write_text(json.dumps(receipt))
    seen: dict[str, object] = {}
    install_workflow(
        monkeypatch,
        run_install=lambda ctx: seen.update(ctx.state["kubernetes"]),
    )

    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "kubernetes",
                "--name",
                "demo",
                "--semantic",
                "--provider-key-file",
                str(provider),
                "--kube-context",
                "reference",
                "--kube-storage-class",
                "cairn-rwop",
                "--kube-image",
                "registry.example/cairn@sha256:" + "a" * 64,
                "--kube-falkordb-receipt",
                str(receipt_path),
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )
    assert seen["falkordb_receipt"] == receipt

    receipt_path.write_text("{}")
    assert cli.main(["resume", "--name", "demo", "--state-root", str(state_root)]) == 0
    assert seen["falkordb_receipt"] == receipt


def test_semantic_native_persists_verified_falkordb_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    provider = tmp_path / "provider-key"
    provider.write_text("test-provider-key\n")
    provider.chmod(0o600)
    descriptor = falkordb_runtime(tmp_path)
    seen: dict[str, object] = {}
    install_workflow(
        monkeypatch,
        run_install=lambda ctx: seen.update(ctx.state["falkordb_runtime"]),
    )
    arguments = [
        "--non-interactive",
        "--mode",
        "native",
        "--name",
        "demo",
        "--semantic",
        "--falkordb-runtime",
        str(descriptor),
        "--provider-key-file",
        str(provider),
        "--state-root",
        str(state_root),
    ]

    assert cli.main(arguments, default_source=source) == 0
    assert seen["image"] == "cairn.local/falkordb-runtime@sha256:" + "a" * 64
    assert seen["archive"] == str((tmp_path / "image.tar").resolve())

    descriptor.write_text("{}")
    (tmp_path / "image.tar").unlink()
    assert cli.main(["resume", "--name", "demo", "--state-root", str(state_root)]) == 0


def test_kubernetes_options_are_immutable_for_an_existing_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    install_workflow(monkeypatch)
    arguments = [
        "--non-interactive",
        "--mode",
        "kubernetes",
        "--name",
        "demo",
        "--kube-context",
        "reference",
        "--kube-storage-class",
        "cairn-rwop",
        "--kube-image",
        "registry.example/cairn@sha256:" + "a" * 64,
        "--state-root",
        str(state_root),
    ]
    assert cli.main(arguments, default_source=source) == 0
    arguments[arguments.index("reference")] = "other-context"

    assert cli.main(arguments, default_source=source) == 2
    assert "kubernetes" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--kube-context", "other-context"),
        ("--kube-namespace", "other-namespace"),
        ("--kube-storage-class", "other-storage-class"),
        ("--kube-image", "registry.example/cairn@sha256:" + "b" * 64),
        ("--kube-preloaded-image", None),
    ],
)
def test_resume_refuses_an_explicit_kubernetes_option_that_differs_from_state(
    flag: str,
    value: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    calls: list[str] = []
    install_workflow(monkeypatch, run_install=lambda ctx: calls.append(ctx.mode))
    install = [
        "--non-interactive",
        "--mode",
        "kubernetes",
        "--name",
        "demo",
        "--kube-context",
        "reference",
        "--kube-storage-class",
        "cairn-rwop",
        "--kube-image",
        "registry.example/cairn@sha256:" + "a" * 64,
        "--state-root",
        str(state_root),
    ]
    assert cli.main(install, default_source=source) == 0
    resume = ["resume", "--name", "demo", "--state-root", str(state_root), flag]
    if value is not None:
        resume.append(value)

    assert cli.main(resume) == 2
    assert "Recorded Kubernetes options differ" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--mode", "docker"), ("--port", "23456"), ("--semantic", None)],
)
def test_resume_refuses_an_explicit_core_option_that_differs_from_state(
    flag: str,
    value: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    calls: list[str] = []
    install_workflow(monkeypatch, run_install=lambda ctx: calls.append(ctx.mode))
    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "demo",
                "--port",
                "19000",
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )
    resume = ["resume", "--name", "demo", "--state-root", str(state_root), flag]
    if value is not None:
        resume.append(value)

    assert cli.main(resume) == 2
    assert "Recorded installation options differ" in capsys.readouterr().err
    assert calls == ["native"]


def test_status_does_not_rewrite_recorded_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    install_workflow(monkeypatch)
    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "demo",
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )
    state = state_root / "demo" / "state.json"
    before = state.stat()

    assert cli.main(["status", "--name", "demo", "--state-root", str(state_root)]) == 0

    after = state.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_status_reads_partial_blitz_after_instance_lock_was_removed(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    with open_context(
        state_root,
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 8000,
            "semantic": False,
        },
    ) as ctx:
        ctx.state.update(status="blitzing", blitz_phase="resources_removed")
        ctx.save()
        journal = state_root / ".demo.blitz.json"
        journal.write_text(json.dumps(ctx.state))
        os.chmod(journal, 0o600)

    (state_root / "demo" / "installer.lock").unlink()
    with open_read_context(state_root, "demo") as recovered:
        assert recovered.state["status"] == "blitzing"
        assert recovered.state["instance_id"] == ctx.state["instance_id"]


def test_status_rejects_a_fifo_instance_lock_without_blocking(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    with open_context(
        state_root,
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 8000,
            "semantic": False,
        },
    ):
        pass
    lock = state_root / "demo" / "installer.lock"
    lock.unlink()
    os.mkfifo(lock, 0o600)

    with pytest.raises(InstallError, match="Installer lock must be"):
        open_read_context(state_root, "demo")


def test_status_respects_the_final_deletion_root_lease(tmp_path: Path) -> None:
    from cairn_install.core import state_root_guard

    state_root = tmp_path / "state"
    with open_context(
        state_root,
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 8000,
            "semantic": False,
        },
    ):
        pass

    with state_root_guard(state_root, exclusive=True):
        with pytest.raises(InstallError, match="cleanup is busy"):
            open_read_context(state_root, "demo")


def test_resume_allows_omitted_kubernetes_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    calls: list[str] = []
    install_workflow(monkeypatch, run_install=lambda ctx: calls.append(ctx.mode))
    install = [
        "--non-interactive",
        "--mode",
        "kubernetes",
        "--name",
        "demo",
        "--kube-context",
        "reference",
        "--kube-storage-class",
        "cairn-rwop",
        "--kube-image",
        "registry.example/cairn@sha256:" + "a" * 64,
        "--state-root",
        str(state_root),
    ]
    assert cli.main(install, default_source=source) == 0

    assert cli.main(["resume", "--name", "demo", "--state-root", str(state_root)]) == 0
    assert calls == ["kubernetes", "kubernetes"]


def test_provider_key_flag_requires_semantic_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    key_file = tmp_path / "unused-key"
    install_workflow(monkeypatch)

    result = cli.main(
        [
            "--non-interactive",
            "--mode",
            "native",
            "--name",
            "demo",
            "--provider-key-file",
            str(key_file),
            "--state-root",
            str(tmp_path / "state"),
        ],
        default_source=source,
    )

    assert result == 2
    assert "--provider-key-file requires --semantic" in capsys.readouterr().err
    assert not key_file.exists()


def test_semantic_setup_creates_protected_key_file_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "platform", SimpleNamespace(system=lambda: "Linux"))
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    install_workflow(
        monkeypatch,
        run_install=lambda ctx: pytest.fail("workflow must wait for the key"),
    )
    arguments = [
        "--non-interactive",
        "--mode",
        "native",
        "--name",
        "demo",
        "--semantic",
        "--falkordb-runtime",
        str(falkordb_runtime(tmp_path)),
        "--state-root",
        str(state_root),
    ]

    assert cli.main(arguments, default_source=source) == 2

    key_file = state_root / "demo" / "openai-api-key"
    state = json.loads((state_root / "demo" / "state.json").read_text())
    assert key_file.read_bytes() == b""
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert state["status"] == "planned"
    first_ids = state["run_id"], state["instance_id"]
    message = capsys.readouterr().err
    assert str(key_file) in message
    assert "edit the protected file, then rerun" in message.lower()

    key_file.write_text("sk-example-super-secret-value\n")
    os.chmod(key_file, 0o600)
    seen: dict[str, object] = {}

    def run_install(ctx: object) -> None:
        seen["ids"] = ctx.run_id, ctx.instance_id  # type: ignore[attr-defined]
        provider = Path(ctx.state["provider_key_file"])  # type: ignore[attr-defined]
        seen["provider"] = provider
        assert provider.read_text() == "sk-example-super-secret-value\n"
        assert stat.S_IMODE(provider.stat().st_mode) == 0o600

    install_workflow(monkeypatch, run_install=run_install)
    assert cli.main(arguments, default_source=source) == 0
    assert seen["ids"] == first_ids
    assert (
        seen["provider"]
        == state_root / "demo" / "instance" / "credentials" / "openai-api-key"
    )
    public = capsys.readouterr()
    logs = (state_root / "demo" / "commands.log").read_text()
    assert "sk-example-super-secret-value" not in public.out + public.err + logs

    key_file.unlink()
    assert cli.main(["resume", "--name", "demo", "--state-root", str(state_root)]) == 0
    assert not key_file.exists()


def test_missing_explicit_provider_key_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = source_tree(tmp_path)
    missing = tmp_path / "keys" / "openai"
    install_workflow(monkeypatch)

    result = cli.main(
        [
            "--non-interactive",
            "--mode",
            "docker",
            "--name",
            "demo",
            "--semantic",
            "--falkordb-runtime",
            str(falkordb_runtime(tmp_path)),
            "--provider-key-file",
            str(missing),
            "--state-root",
            str(tmp_path / "state"),
        ],
        default_source=source,
    )

    assert result == 2
    assert "Provider key file does not exist" in capsys.readouterr().err
    assert not missing.exists()


def test_interrupted_provider_copy_resumes_from_protected_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    create = {
        "mode": "native",
        "port": 8000,
        "semantic": True,
        "source": str(source),
        "source_fingerprint": cli.source_fingerprint(source),
    }
    with open_context(state_root, "demo", create=create) as ctx:
        input_path = ctx.directory / "openai-api-key"
        input_path.write_text("sk-example-interrupted-secret\n")
        os.chmod(input_path, 0o600)

        def interrupt(*args: object, **kwargs: object) -> None:
            raise InstallError("injected interruption", "interrupted")

        monkeypatch.setattr(ctx, "write_file", interrupt)
        with pytest.raises(InstallError, match="injected interruption"):
            cli._prepare_provider_key(ctx, None)
        assert "provider_key_file" not in ctx.state

    with open_context(state_root, "demo") as resumed:
        cli._prepare_provider_key(resumed, None)
        provider = resumed.root / "credentials" / "openai-api-key"
        assert resumed.state["provider_key_file"] == str(provider)
        assert provider.read_text() == "sk-example-interrupted-secret\n"


def test_source_fingerprint_is_content_based_and_ignores_evidence(
    tmp_path: Path,
) -> None:
    source = source_tree(tmp_path)
    (source / "README.md").write_text("Cairn\n")
    before = cli.source_fingerprint(source)
    (source / "docs" / "evidence").mkdir(parents=True)
    (source / "docs" / "evidence" / "run.log").write_text("changing output")

    assert cli.source_fingerprint(source) == before

    (source / "src" / "cairn" / "__init__.py").write_text("VERSION = 2\n")
    assert cli.source_fingerprint(source) != before

    changed_source = source_tree(tmp_path / "other")
    (changed_source / "README.md").write_text("Changed package metadata\n")
    assert cli.source_fingerprint(changed_source) != cli.source_fingerprint(
        source_tree(tmp_path / "baseline")
    )

    (source / "deploy").mkdir()
    (source / "deploy" / "images.lock").write_text("falkordb=first\n")
    image_fingerprint = cli.source_fingerprint(source)
    (source / "deploy" / "images.lock").write_text("falkordb=second\n")
    assert cli.source_fingerprint(source) != image_fingerprint


@pytest.mark.parametrize(
    "manifest_name",
    ("kubernetes.yaml", "kubernetes-retrieval.yaml"),
)
def test_source_fingerprint_includes_rendered_kubernetes_manifests(
    tmp_path: Path, manifest_name: str
) -> None:
    source = source_tree(tmp_path)
    rendered = source / "deploy" / "kustomize" / "rendered"
    rendered.mkdir(parents=True)
    manifest = rendered / manifest_name
    manifest.write_text("kind: Service\nmetadata:\n  name: cairn\n")

    before = cli.source_fingerprint(source)
    manifest.write_text("kind: Service\nmetadata:\n  name: changed-cairn\n")

    assert cli.source_fingerprint(source) != before


def test_source_fingerprint_refuses_symlinked_install_tree(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    real_package = source / "src" / "real-cairn"
    (source / "src" / "cairn").rename(real_package)
    (source / "src" / "cairn").symlink_to(real_package, target_is_directory=True)

    with pytest.raises(InstallError, match="symlink"):
        cli.source_fingerprint(source)


def test_resume_reuses_recorded_options_and_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    captured: list[tuple[str, str, str, int, bool]] = []

    def run_install(ctx: object) -> None:
        captured.append(
            (
                ctx.run_id,  # type: ignore[attr-defined]
                ctx.instance_id,  # type: ignore[attr-defined]
                ctx.mode,  # type: ignore[attr-defined]
                ctx.port,  # type: ignore[attr-defined]
                ctx.semantic,  # type: ignore[attr-defined]
            )
        )

    install_workflow(monkeypatch, run_install=run_install)
    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "docker",
                "--name",
                "demo",
                "--port",
                "8123",
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )
    assert cli.main(["resume", "--name", "demo", "--state-root", str(state_root)]) == 0
    assert captured == [captured[0], captured[0]]
    assert captured[0][2:] == ("docker", 8123, False)


@pytest.mark.parametrize("operation", ["status", "rollback"])
def test_recovery_operations_do_not_require_source(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    called: list[str] = []
    install_workflow(monkeypatch)
    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "demo",
                "--state-root",
                str(state_root),
            ],
            default_source=source,
        )
        == 0
    )
    source.rename(tmp_path / "source-gone")
    install_workflow(
        monkeypatch,
        status_install=lambda ctx: called.append("status"),
        rollback_install=lambda ctx: called.append("rollback"),
    )

    assert cli.main([operation, "--name", "demo", "--state-root", str(state_root)]) == 0
    assert called == [operation]


def test_source_launcher_runs_without_an_installed_package(tmp_path: Path) -> None:
    launcher = Path(__file__).parents[2] / "cairn-install"
    result = subprocess.run(
        [str(launcher), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Guided Cairn installer" in result.stdout


def test_sigterm_stops_active_child_before_releasing_lock(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    state_root = tmp_path / "state"
    child_pid_file = tmp_path / "child.pid"
    child_program = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    program = f"""
import sys
from pathlib import Path
from cairn_install import cli, workflow

def run_install(ctx):
    ctx.command([sys.executable, '-c', {child_program!r}], cwd=ctx.directory)

workflow.run_install = run_install
raise SystemExit(cli.main([
    '--non-interactive', '--mode', 'native', '--name', 'signal-test',
    '--state-root', {str(state_root)!r}
], default_source=Path({str(source)!r})))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=Path(__file__).parents[2],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            if process.poll() is not None:
                break
            time.sleep(0.02)
        assert child_pid_file.exists(), process.communicate(timeout=1)
        child_pid = int(child_pid_file.read_text())

        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=10)

        assert process.returncode == 2
        assert "interrupted" in stderr
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        with open_context(state_root, "signal-test") as ctx:
            assert ctx.state["run_id"]
            assert ctx.state["instance_id"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_main_restores_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    install_workflow(monkeypatch)
    previous = {
        number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)
    }

    assert (
        cli.main(
            [
                "--non-interactive",
                "--mode",
                "native",
                "--name",
                "restore",
                "--state-root",
                str(tmp_path / "state"),
            ],
            default_source=source,
        )
        == 0
    )

    assert {number: signal.getsignal(number) for number in previous} == previous


def test_main_outside_main_thread_does_not_install_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    install_workflow(monkeypatch)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda *args: pytest.fail("signal handlers touched outside main thread"),
    )
    results: list[int] = []
    worker = threading.Thread(
        target=lambda: results.append(
            cli.main(
                [
                    "--non-interactive",
                    "--mode",
                    "native",
                    "--name",
                    "threaded",
                    "--state-root",
                    str(tmp_path / "thread-state"),
                ],
                default_source=source,
            )
        )
    )

    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert results == [0]


@pytest.mark.parametrize("operation", ["status", "rollback", "blitz", "ls"])
def test_keep_running_rejects_non_install_operations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    assert cli.main([operation, "--keep-running", "--state-root", str(tmp_path)]) == 2
    assert "only valid with install or resume" in capsys.readouterr().err


def test_keep_running_install_passes_foreground_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path)
    seen: list[bool] = []

    def run_install(ctx: object, *, keep_running: bool = False) -> None:
        seen.append(keep_running)

    install_workflow(monkeypatch, run_install=run_install)
    assert (
        cli.main(
            [
                "install",
                "--mode",
                "disposable",
                "--name",
                "mac",
                "--port",
                "19234",
                "--source",
                str(source),
                "--state-root",
                str(tmp_path / "state"),
                "--non-interactive",
                "--keep-running",
            ]
        )
        == 0
    )
    assert seen == [True]


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_incompatible_keep_running_does_not_create_state_or_request_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    state = tmp_path / "state"
    assert (
        cli.main(
            [
                "install",
                "--mode",
                mode,
                "--semantic",
                "--keep-running",
                "--name",
                "wrong",
                "--port",
                "19234",
                "--state-root",
                str(state),
                "--non-interactive",
            ]
        )
        == 2
    )
    assert "--keep-running requires disposable mode" in capsys.readouterr().err
    assert not state.exists()
