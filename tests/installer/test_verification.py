from collections.abc import Iterator
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


TOKEN_VALUE = f"cairn1.{uuid4()}.{'A' * 43}"
OPENAPI = b'{"openapi": "3.1.0"}\n'
MANIFEST = b'{"tools": []}\n'


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    contracts = source / "src" / "cairn" / "contracts"
    contracts.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "cairn"\nversion = "1.2.3"\n'
    )
    (contracts / "cairn-openapi-v1.json").write_bytes(OPENAPI)
    (contracts / "cairn-mcp-tools-v1.json").write_bytes(MANIFEST)
    return source


def _identity(instance_id: str) -> dict[str, Any]:
    import hashlib

    return {
        "contract_identity": "cairn/v1",
        "instance_id": instance_id,
        "product_version": "1.2.3",
        "contract_digest": hashlib.sha256(OPENAPI).hexdigest(),
        "mcp_contract_digest": hashlib.sha256(MANIFEST).hexdigest(),
    }


def _instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str = TOKEN_VALUE
) -> Iterator[Any]:
    """A created installation with an owned credential and no sleeping."""
    monkeypatch.setattr("cairn_install.verification.time.sleep", lambda _s: None)
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(_source_tree(tmp_path)),
            "mode": "disposable",
            "port": 19000,
            "semantic": False,
        },
    ) as ctx:
        (ctx.root / "credentials").mkdir(parents=True, mode=0o700)
        ctx.write_file(
            ctx.root / "credentials" / "admin.token", token + "\n", secret=True
        )
        yield ctx


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    yield from _instance(tmp_path, monkeypatch)


def _responder(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, list[dict[str, Any] | Exception]],
) -> list[tuple[str, str]]:
    """Fake `request`: pops the next answer per endpoint; an exception is raised."""
    calls: list[tuple[str, str]] = []

    def request(ctx: Any, endpoint: str, token: str = "", **_: Any) -> dict[str, Any]:
        calls.append((endpoint, token))
        answer = answers[endpoint].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("cairn_install.verification.request", request)
    return calls


def test_ready_polls_until_ready_then_proves_identity_against_this_source(
    instance: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn_install.verification import ready

    calls = _responder(
        monkeypatch,
        {
            "/health/ready": [
                InstallError("/health/ready connection failed"),
                {"status": "starting"},
                {"status": "ready"},
            ],
            "/v1/instance": [_identity(instance.instance_id)],
        },
    )

    assert ready(instance) == TOKEN_VALUE

    assert calls == [
        ("/health/ready", ""),
        ("/health/ready", ""),
        ("/health/ready", ""),
        ("/v1/instance", TOKEN_VALUE),
    ]
    assert instance.state["receipts"]["instance"] == _identity(instance.instance_id)
    curl_config = instance.root / "credentials" / "curl.conf"
    assert (
        curl_config.read_text() == f'header = "Authorization: Bearer {TOKEN_VALUE}"\n'
    )
    assert curl_config.stat().st_mode & 0o777 == 0o600


def test_ready_refuses_a_malformed_credential_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn_install.verification import ready

    calls = _responder(monkeypatch, {})
    for ctx in _instance(tmp_path, monkeypatch, token="not-a-cairn-token"):
        with pytest.raises(InstallError, match="invalid format"):
            ready(ctx)
        assert "instance" not in ctx.state["receipts"]
    assert calls == []


def test_ready_reports_the_last_readiness_failure_at_the_deadline(
    instance: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn_install.verification import ready

    clock = iter(range(0, 10_000, 61))
    monkeypatch.setattr(
        "cairn_install.verification.time.monotonic", lambda: next(clock)
    )
    calls = _responder(
        monkeypatch,
        {"/health/ready": [InstallError("/health/ready returned HTTP 503")] * 5},
    )

    with pytest.raises(InstallError, match="Readiness deadline exceeded: .*HTTP 503"):
        ready(instance)
    assert all(endpoint == "/health/ready" for endpoint, _ in calls)
    assert "instance" not in instance.state["receipts"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "instance_id",
            "00000000-0000-4000-8000-000000000000",
            "identity/contract differs",
        ),
        ("contract_identity", "cairn/v0", "identity/contract differs"),
        ("product_version", "9.9.9", "product version differs"),
        ("contract_digest", "0" * 64, "contract_digest differs"),
        ("mcp_contract_digest", "0" * 64, "mcp_contract_digest differs"),
    ],
)
def test_ready_refuses_an_identity_that_differs_from_the_source(
    instance: Any,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    from cairn_install.verification import ready

    identity = _identity(instance.instance_id) | {field: value}
    _responder(
        monkeypatch,
        {"/health/ready": [{"status": "ok"}], "/v1/instance": [identity]},
    )

    with pytest.raises(InstallError, match=message):
        ready(instance)
    assert "instance" not in instance.state["receipts"]
