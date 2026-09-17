from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairn_install import docker, native
from cairn_install.core import Context, InstallError, open_context


def _open_semantic_context(tmp_path: Path, mode: str) -> Any:
    source = tmp_path / "source"
    source.mkdir()
    return open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(source),
            "mode": mode,
            "port": 18080,
            "semantic": True,
        },
    )


def _record_provider(ctx: Context) -> None:
    credentials = ctx.root / "credentials"
    credentials.mkdir(mode=0o700)
    provider = credentials / "openai-api-key"
    ctx.write_file(provider, "provider-secret\n", secret=True)
    ctx.state["provider_key_file"] = str(provider)
    ctx.save()


@pytest.mark.parametrize("ledger", ["owned_files", "file_intents"])
def test_native_resume_refuses_lost_recorded_password_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: str
) -> None:
    with _open_semantic_context(tmp_path, "native") as ctx:
        _record_provider(ctx)
        password = ctx.root / "credentials" / "falkordb-password"
        retained_hash = hashlib.sha256(b"lost-password\n").hexdigest()
        ctx.state[ledger][str(password.absolute())] = retained_hash
        ctx.save()
        index = native._NativeIndex(ctx)  # noqa: SLF001
        monkeypatch.setattr(
            secrets, "token_hex", lambda size: pytest.fail(f"generated {size}")
        )

        with pytest.raises(InstallError, match="recorded FalkorDB credential.*missing"):
            index.prepare()

        assert ctx.state[ledger][str(password.absolute())] == retained_hash
        assert not (ctx.root / "credentials" / "falkordb.conf").exists()
        assert ctx.state["resources"] == {}


@pytest.mark.parametrize("ledger", ["owned_files", "file_intents"])
def test_docker_resume_refuses_lost_recorded_password_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: str
) -> None:
    with _open_semantic_context(tmp_path, "docker") as ctx:
        _record_provider(ctx)
        password = ctx.root / "credentials" / "falkordb-password.source"
        retained_hash = hashlib.sha256(b"lost-password\n").hexdigest()
        ctx.state[ledger][str(password.absolute())] = retained_hash
        backend = docker.Backend(ctx)
        retained_volumes = {"falkordb-data": {"name": "retained"}}
        backend._docker["volumes"] = retained_volumes.copy()  # noqa: SLF001
        # Reachable after Context.write_file records ownership but before the
        # Docker semantic metadata assignment and save.
        assert "semantic" not in backend._docker  # noqa: SLF001
        ctx.save()
        state_before = (ctx.directory / "state.json").read_bytes()
        monkeypatch.setattr(
            secrets, "token_hex", lambda size: pytest.fail(f"generated {size}")
        )

        with pytest.raises(InstallError, match="recorded FalkorDB credential.*missing"):
            backend.prepare()

        assert ctx.state[ledger][str(password.absolute())] == retained_hash
        assert backend._docker["volumes"] == retained_volumes  # noqa: SLF001
        assert (ctx.directory / "state.json").read_bytes() == state_before
        assert not (ctx.root / "config.yaml").exists()
        assert not (ctx.root / "compose.yaml").exists()


@pytest.mark.parametrize("backend_kind", ["native", "docker"])
def test_fresh_semantic_prepare_refuses_dangling_password_symlink_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_kind: str
) -> None:
    with _open_semantic_context(tmp_path, backend_kind) as ctx:
        _record_provider(ctx)
        suffix = (
            "falkordb-password"
            if backend_kind == "native"
            else "falkordb-password.source"
        )
        password = ctx.root / "credentials" / suffix
        password.symlink_to(ctx.root / "missing-target")
        monkeypatch.setattr(
            secrets, "token_hex", lambda size: pytest.fail(f"generated {size}")
        )

        action: Callable[[], object]
        if backend_kind == "native":
            action = native._NativeIndex(ctx).prepare  # noqa: SLF001
        else:
            action = docker.Backend(ctx).prepare

        with pytest.raises(InstallError, match="[Uu]nowned"):
            action()
