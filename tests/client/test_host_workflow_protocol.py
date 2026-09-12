"""Pure argv/stdin boundary tests; no host, provider or process launch."""

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def protocol() -> Any:
    return importlib.import_module("scripts.host_workflow_protocol")


@pytest.mark.parametrize(
    "command", ["arrive", "recall", "remember", "status", "suggest"]
)
def test_content_is_detached_stdin_never_an_executable(command: str) -> None:
    module = protocol()
    content = json.dumps({"body": "$(touch sentinel); `whoami`\n雪"}) + "\n"
    argv = ["--profile", "/cli/profile.json", command]
    result = module.validate_daily_request(
        {"argv": argv, "stdin": content}, allowed_commands=frozenset({command})
    )
    argv[0] = "python"
    assert result.argv == ("--profile", "/cli/profile.json", command)
    assert result.stdin == content.encode()
    assert result.command == command
    assert result.help is False


@pytest.mark.parametrize("argv", [["--help"], ["status", "--help"]])
def test_help_is_a_distinct_empty_input_request(argv: list[str]) -> None:
    result = protocol().validate_daily_request(
        {"argv": argv, "stdin": ""}, allowed_commands=frozenset({"status"})
    )
    assert result.argv == tuple(argv)
    assert result.stdin == b""
    assert result.help is True


@pytest.mark.parametrize(
    "argv,stdin",
    [
        (["python", "-c", "print('leak')"], ""),
        (["--profile", "/cli/../host/auth.json", "status"], ""),
        (["--profile", "/cli/profile.json", "correct"], "{}"),
        (["correct", "--help"], ""),
        (["--profile", "/cli/profile.json", "status", "--human"], ""),
        (["--profile=/cli/profile.json", "status"], ""),
        (["--help"], "ignored secret"),
        (["--profile", "/cli/profile.json", "check"], "ignored secret"),
        (["--profile", "/cli/profile.json", "status"], "\x00"),
        (["--profile", "/cli/profile.json", "status"], "\ud800"),
        (["--profile", "/cli/profile.json", "status"], "x" * 1048577),
        (["--profile", "/cli/profile.json", "status"], "雪" * 349526),
        (["--profile", "/cli/profile.json", "status"], None),
        (["--profile", "/cli/profile.json", True], ""),
        (["x" * 4097], ""),
        (("--help",), ""),
        ([], ""),
        ("--help", ""),
    ],
    ids=[
        "executable",
        "profile-escape",
        "forbidden-command",
        "forbidden-help",
        "extra-flag",
        "profile-alias",
        "help-input",
        "check-input",
        "nul",
        "surrogate",
        "ascii-overflow",
        "utf8-overflow",
        "null-input",
        "bool-arg",
        "argument-overflow",
        "tuple-argv",
        "empty-argv",
        "string-argv",
    ],
)
def test_rejects_launch_grammar_changes_without_echoing_input(
    argv: object,
    stdin: object,
) -> None:
    module = protocol()
    with pytest.raises(module.WorkflowInputError) as error:
        module.validate_daily_request(
            {"argv": argv, "stdin": stdin},
            allowed_commands=frozenset({"check", "status"}),
        )
    assert str(error.value) == "invalid_workflow_request"


@pytest.mark.parametrize("extra", ["env", "cwd", "executable", "profile", "receipt"])
def test_unknown_fields_cannot_change_process_authority(extra: str) -> None:
    module = protocol()
    with pytest.raises(module.WorkflowInputError):
        module.validate_daily_request(
            {"argv": ["--help"], "stdin": "", extra: "anything"},
            allowed_commands=frozenset({"status"}),
        )


@pytest.mark.parametrize(
    "allowed", [set(), frozenset(), frozenset({"shell"}), {"status"}]
)
def test_controller_command_configuration_is_closed(allowed: object) -> None:
    module = protocol()
    with pytest.raises(
        module.WorkflowInputError, match="invalid_workflow_configuration"
    ):
        module.validate_daily_request(
            {"argv": ["--help"], "stdin": ""}, allowed_commands=allowed
        )


def test_exact_stdin_byte_limit_and_empty_status_are_preserved() -> None:
    module = protocol()
    for content in ("", " \n\t", "x" * 1048576):
        result = module.validate_daily_request(
            {"argv": ["--profile", "/cli/profile.json", "status"], "stdin": content},
            allowed_commands=frozenset({"status"}),
        )
        assert result.stdin == content.encode()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_skill_read_accepts_only_exact_native_installed_path(provider: str) -> None:
    module = protocol()
    folder = ".agents" if provider == "codex" else ".claude"
    path = f"/work/{folder}/skills/cairn-memory/SKILL.md"
    assert module.validate_skill_request({"path": path}, provider=provider) == path
    for invalid in (path + "/..", "/host/auth.json", path.replace(folder, "other")):
        with pytest.raises(module.WorkflowInputError):
            module.validate_skill_request({"path": invalid}, provider=provider)
    with pytest.raises(module.WorkflowInputError):
        module.validate_skill_request({"path": path, "extra": "x"}, provider=provider)


def test_invalid_provider_and_non_object_requests_fail_closed() -> None:
    module = protocol()
    with pytest.raises(
        module.WorkflowInputError, match="invalid_workflow_configuration"
    ):
        module.validate_skill_request({"path": "x"}, provider="unknown")
    invalid_requests: tuple[object, ...] = (None, [], "request")
    for value in invalid_requests:
        with pytest.raises(module.WorkflowInputError):
            module.validate_daily_request(value, allowed_commands=frozenset({"status"}))
        with pytest.raises(module.WorkflowInputError):
            module.validate_skill_request(value, provider="codex")


def test_fresh_import_does_not_read_runtime_files() -> None:
    probe = """
import importlib
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
def audit(event, args):
    if event == 'open':
        path = args[0]
        if isinstance(path, (str, bytes)):
            name = path.decode() if isinstance(path, bytes) else path
            if not name.endswith(('.py', '.pyc', '.so')):
                raise RuntimeError('unexpected_runtime_file_read')
sys.addaudithook(audit)
importlib.import_module('scripts.host_workflow_protocol')
print('pure-import-ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(Path(__file__).resolve().parents[2])],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"pure-import-ok\n"


@pytest.mark.parametrize(
    "command",
    ["propose", "proposal-list", "proposal-read", "proposal-accept", "proposal-reject"],
)
def test_proposal_admission_still_requires_each_callers_allowlist(command: str) -> None:
    module = protocol()
    content = json.dumps({"reason": "$(touch sentinel); `whoami` 雪"})
    request = {"argv": ["--profile", "/cli/profile.json", command], "stdin": content}
    invocation = module.validate_daily_request(
        request, allowed_commands=frozenset({command})
    )
    assert invocation.argv == tuple(request["argv"])
    assert invocation.stdin == content.encode() and invocation.command == command
    help_request = {"argv": [command, "--help"], "stdin": ""}
    assert module.validate_daily_request(
        help_request, allowed_commands=frozenset({command})
    ).help
    for denied in (request, help_request):
        with pytest.raises(
            module.WorkflowInputError, match="^invalid_workflow_request$"
        ):
            module.validate_daily_request(
                denied, allowed_commands=frozenset({"status"})
            )


@pytest.mark.parametrize("allowed", [frozenset({"*"}), frozenset({"proposal-*"})])
def test_proposal_wildcard_admission_is_never_configuration(
    allowed: frozenset[str],
) -> None:
    module = protocol()
    with pytest.raises(
        module.WorkflowInputError, match="^invalid_workflow_configuration$"
    ):
        module.validate_daily_request(
            {"argv": ["--help"], "stdin": ""}, allowed_commands=allowed
        )
