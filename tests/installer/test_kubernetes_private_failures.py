"""Failure diagnostics for private commands, which every kubectl call is."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cairn_install.core import Context, InstallError, open_context


def create(tmp_path: Path) -> Context:
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "mode": "disposable",
            "port": 18000,
            "semantic": False,
            "source": str(tmp_path),
            "source_fingerprint": "test",
        },
    )


@pytest.mark.parametrize("verbose", [False, True])
def test_private_command_failure_shows_redacted_stderr_tail_but_never_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verbose: bool
) -> None:
    # A script file keeps the payload strings out of the echoed command line.
    script = tmp_path / "failing-tool.py"
    script.write_text(
        "import sys\n"
        "print('stdout-payload')\n"
        "print('error: token hunter2 rejected by server', file=sys.stderr)\n"
        "raise SystemExit(1)\n"
    )
    with create(tmp_path) as ctx:
        ctx.verbose = verbose
        ctx.add_secret("hunter2")
        with pytest.raises(InstallError, match="exit 1"):
            ctx.command([sys.executable, str(script)], private=True)
        out = capsys.readouterr().out
        log = (ctx.directory / "commands.log").read_text()
    assert "Command failed (exit 1)." in out
    assert "error: token [redacted] rejected by server" in out
    for disclosure in ("hunter2", "stdout-payload"):
        assert disclosure not in out
        assert disclosure not in log
