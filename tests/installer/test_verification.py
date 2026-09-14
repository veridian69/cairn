from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cairn_install.core import InstallError, open_context
from cairn_install.verification import ingest, validate_instance


def test_instance_identity_must_match() -> None:
    with pytest.raises(InstallError):
        validate_instance(
            {"contract_identity": "cairn/v1", "instance_id": str(uuid4())}, str(uuid4())
        )


def test_ingest_reuses_committed_receipt_without_another_write(
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
        response = {
            "outcome": "committed",
            "audit_receipt": {},
            "mutation_receipt": {},
            "result": {
                "assertion_id": str(uuid4()),
                "evidence_id": str(uuid4()),
                "fact_ids": [str(uuid4())],
            },
        }
        calls: list[str] = []

        def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["key"])
            return response

        monkeypatch.setattr("cairn_install.verification.request", request)
        assert ingest(ctx, "secret") == response
        assert ingest(ctx, "secret") == response
        assert len(calls) == 1


def test_uncertain_ingest_retries_only_same_request_key(
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
        attempts: list[tuple[str, bytes]] = []

        def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            attempts.append((kwargs["key"], kwargs["data"]))
            raise InstallError("response lost")

        monkeypatch.setattr("cairn_install.verification.request", request)
        for _ in range(2):
            with pytest.raises(InstallError):
                ingest(ctx, "secret")
        assert attempts[0] == attempts[1]
