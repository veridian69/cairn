"""Actual wheel CLI inside nested jails; no provider or host account is used."""

import importlib
import json
import shutil
import socket
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_arrival_briefing import memory_support as memory_support
from test_memory_cli import checkpoint, profile
from test_memory_cli_process import server

from cairn.catalogue.sqlite import read_connection


@pytest.mark.host_isolation
def test_actual_wheel_cli_inside_nested_sandbox(
    tmp_path: Path, memory_support: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    builder = importlib.import_module("host_workflow_fixture")
    sandbox = importlib.import_module("scripts.host_workflow_sandbox")
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    for name in ("host-runtime", "host-config", "cli-config", "work"):
        (stage / name).mkdir(mode=0o700)
    evidence = builder.build_cli_runtime(stage / "cli-runtime")
    assert evidence.file_count < 8100
    assert len(evidence.wheel_sha256) == len(evidence.requirements_sha256) == 64
    assert evidence.distributions
    assert not tuple((stage / "cli-runtime").rglob("*.pth"))
    assert not tuple((stage / "cli-runtime").rglob("*.pyc"))
    builder.write_python_entry(
        stage / "host-runtime/entry",
        "import json, sys\n"
        "sys.path.insert(0, '/runtime/host')\n"
        "from sandbox import run_cli\n"
        "results = []\n"
        "for item in json.load(sys.stdin):\n"
        "    result = run_cli(tuple(item['argv']), stdin=item['stdin'].encode())\n"
        "    results.append({'code': result.returncode, 'stdout': result.stdout.decode(),\n"
        "                    'stderr': result.stderr.decode()})\n"
        "print(json.dumps(results))\n",
        sandbox_directory="/runtime/host",
    )
    assert sandbox.__file__ is not None
    shutil.copyfile(Path(sandbox.__file__), stage / "host-runtime/sandbox.py")
    (stage / "host-runtime/sandbox.py").chmod(0o600)
    instance = memory_support.Instance(tmp_path / "instance", attic=False)
    _, token = instance.add_actor()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        supplied = profile(
            tmp_path, instance, token, f"http://127.0.0.1:{listener.getsockname()[1]}"
        )
        configuration = json.loads(supplied.read_bytes())
        configuration["credential_file"] = "credential"
        (stage / "cli-config/profile.json").write_text(json.dumps(configuration))
        (stage / "cli-config/profile.json").chmod(0o600)
        (stage / "cli-config/credential").write_text(token)
        (stage / "cli-config/credential").chmod(0o600)
        value = checkpoint()
        requests: list[dict[str, Any]] = [
            {"argv": ["--help"], "stdin": ""},
            {"argv": ["--profile", "/cli/profile.json", "check"], "stdin": ""},
            {
                "argv": ["--profile", "/cli/profile.json", "remember"],
                "stdin": json.dumps(value),
            },
            {
                "argv": ["--profile", "/cli/profile.json", "resume"],
                "stdin": json.dumps({"turn_id": value["turn_id"]}),
            },
        ]
        sealed = sandbox.seal(stage)
        with server(instance, listener):
            result = sandbox.run_host(
                sealed, stdin=json.dumps(requests).encode(), deadline=60
            )
        assert result.returncode == 0, "sandbox runtime entry failed"
        results = json.loads(result.stdout)
        assert [item["code"] for item in results] == [0, 0, 0, 0], results
        assert b"usage:" in results[0]["stdout"].encode()
        remembered = json.loads(results[2]["stdout"])["result"]
        resumed = json.loads(results[3]["stdout"])["result"]
        assert remembered["state"] == resumed["state"] == "committed"
        assert remembered["completed_turn"] == resumed["completed_turn"]
        assert (
            remembered["persistence"]["result"]["fact_ids"]
            == resumed["persistence"]["result"]["fact_ids"]
        )
    with read_connection(instance.data_path) as connection:
        assert connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 1


def test_runtime_builder_does_not_overwrite_existing_destination(
    tmp_path: Path,
) -> None:
    builder = importlib.import_module("host_workflow_fixture")
    target = tmp_path / "existing"
    target.mkdir()
    original = target / "unrelated"
    original.write_text("keep")
    with pytest.raises(FileExistsError):
        builder.build_cli_runtime(target)
    assert original.read_text() == "keep"


@pytest.mark.host_isolation
def test_cli_runtime_does_not_use_host_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    builder = importlib.import_module("host_workflow_fixture")
    sandbox = importlib.import_module("scripts.host_workflow_sandbox")
    runtime = tmp_path / "runtime"
    builder.build_cli_runtime(runtime)
    wrong_python = tmp_path / "wrong-python"
    wrong_python.write_text("#!/bin/sh\necho wrong-host-python >&2\nexit 86\n")
    wrong_python.chmod(0o700)
    command = sandbox._command(((runtime, "/runtime/cli"),), False, ("--help",))
    command[command.index("--remount-ro") : command.index("--remount-ro")] = [
        "--ro-bind",
        str(wrong_python),
        str(Path("/usr/bin/python3").resolve(strict=True)),
    ]
    boundary = command.index("--") + 1
    control = subprocess.run(
        [*command[:boundary], "/usr/bin/python3", "--version"],
        capture_output=True,
        timeout=30,
    )
    assert control.returncode == 86
    assert b"wrong-host-python" in control.stderr
    result = subprocess.run(command, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    assert b"usage:" in result.stdout
    probe = subprocess.run(
        [
            *command[:boundary],
            "/runtime/cli/python/bin/python3",
            "-I",
            "-S",
            "-c",
            "import sys, sysconfig, json, os, _ssl; "
            "from pathlib import Path; "
            "assert Path(os.__file__).is_relative_to('/runtime/cli/python'); "
            "assert '_ssl' in sys.builtin_module_names or "
            "Path(_ssl.__file__).is_relative_to('/runtime/cli/python'); "
            "sys.path.insert(0, '/runtime/cli/site-packages'); "
            "import pydantic_core._pydantic_core; "
            "print(json.dumps([sys.implementation.cache_tag, sysconfig.get_config_var('SOABI')]))",
        ],
        capture_output=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr.decode()
    assert json.loads(probe.stdout) == [
        sys.implementation.cache_tag,
        sysconfig.get_config_var("SOABI"),
    ]


def test_python_staging_preserves_lib64_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = importlib.import_module("host_workflow_fixture")
    base = tmp_path / "base"
    stdlib = base / "lib64/python3.12"
    native = stdlib / "lib-dynload"
    native.mkdir(parents=True)
    (stdlib / "os.py").write_text("fixture standard library")
    (native / "_ssl.so").write_bytes(b"fixture native module")
    monkeypatch.setattr(builder.sys, "base_prefix", str(base))
    monkeypatch.setattr(builder.sysconfig, "get_path", lambda name: str(stdlib))
    variables = {"DESTSHARED": str(native), "LIBDIR": str(base / "lib64")}
    monkeypatch.setattr(builder.sysconfig, "get_config_var", variables.__getitem__)
    builder._stage_python(tmp_path / "runtime")
    staged = tmp_path / "runtime/python/lib64/python3.12"
    assert (staged / "os.py").read_text() == "fixture standard library"
    assert (staged / "lib-dynload/_ssl.so").read_bytes() == b"fixture native module"
