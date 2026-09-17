import json
from pathlib import Path
from typing import Any

import pytest

from cairn_install.bootstrap import bootstrap
from cairn_install.core import Context, InstallError, open_context


class FakeBackend:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def lifecycle_argv(self, operation: str) -> list[str]:
        return [operation]


def test_completed_capture_is_salvaged_without_second_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        token = "cairn1.12345678-1234-4234-8234-123456789abc." + "a" * 43
        capture = ctx.root / "credentials" / "bootstrap.json"
        ctx.write_file(
            capture,
            json.dumps(
                {
                    "status": "ok",
                    "operation": "bootstrap",
                    "instance_id": ctx.instance_id,
                    "realm_id": "local",
                    "token": token,
                }
            ),
            secret=True,
        )
        calls: list[str] = []

        def command(argv: list[str], **kwargs: Any) -> str:
            calls.append(argv[0])
            return json.dumps(
                {
                    "status": "ok",
                    "operation": argv[0],
                    "instance_id": ctx.instance_id,
                    "realm_count": 1,
                }
            )

        monkeypatch.setattr(ctx, "command", command)
        bootstrap(ctx, FakeBackend(ctx))
        assert ctx.read_secret(ctx.root / "credentials" / "admin.token") == token
        assert "bootstrap" not in calls


def test_uncertain_committed_bootstrap_refuses_to_mint_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        calls: list[str] = []

        def command(argv: list[str], **kwargs: Any) -> str:
            calls.append(argv[0])
            return json.dumps(
                {
                    "status": "ok",
                    "operation": argv[0],
                    "instance_id": ctx.instance_id,
                    "realm_count": 1,
                }
            )

        monkeypatch.setattr(ctx, "command", command)
        with pytest.raises(InstallError) as caught:
            bootstrap(ctx, FakeBackend(ctx))
        assert caught.value.code == "needs_credential_recovery"
        assert "bootstrap" not in calls


def test_wrong_instance_is_never_bootstrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        monkeypatch.setattr(
            ctx,
            "command",
            lambda *args, **kwargs: json.dumps(
                {"status": "ok", "operation": "check-config", "instance_id": "wrong"}
            ),
        )
        with pytest.raises(InstallError, match="identity"):
            bootstrap(ctx, FakeBackend(ctx))
