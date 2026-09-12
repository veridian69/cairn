"""Task 8: the five administration routes end-to-end.

`GRANT-01`–`GRANT-10` mechanics through REST, and the plaintext-once
proof at the wire: the committed ``issue-credential`` response carries
the token exactly once; the replay carries ``null``; and the token never
appears in the catalogue bytes, the rendered metrics or the captured
safe logs. Module semantics were accepted with slice 3/4; these tests
pin the REST mapping.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from httpx import Response as HTTPXResponse

from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.runtime.composition import build_application
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
ROOT_ID = UUID("22222222-2222-4222-8222-222222222222")
ROOT_CREDENTIAL_ID = UUID("33333333-3333-4333-8333-333333333333")
SCOPED_ID = UUID("44444444-4444-4444-8444-444444444444")
SCOPED_CREDENTIAL_ID = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE_TS = canonical_timestamp(datetime(2027, 1, 1, tzinfo=UTC))
NEAR_EXPIRY = "2026-12-01T00:00:00+00:00"
BEYOND_MANAGER_EXPIRY = "2027-06-01T00:00:00+00:00"
REALM = "acme"

REPO = {"kind": "repository", "identifier": "acme-repo"}
OTHER = {"kind": "repository", "identifier": "other-repo"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_config(data_path: Path) -> CairnConfig:
    data = data_path / "data"
    credentials = data_path / "credentials"
    data.mkdir(exist_ok=True)
    credentials.mkdir(exist_ok=True)
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data, credentials=credentials),
    )


def add_manager_grant(
    config: CairnConfig,
    grant_id: UUID,
    *,
    principal_id: UUID,
    segments: list[dict[str, str]],
    read_clearance: str,
    write_classifications: list[str],
    delegable_operations: list[str],
    expires_at: str | None,
) -> None:
    with _open_write_connection(config.paths.data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grants (grant_id, principal_id, realm_id, "
            "scope_segments, operations, read_clearance, "
            "write_classifications, delegable_operations, issued_by, "
            "expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                str(grant_id),
                str(principal_id),
                REALM,
                _canonical(
                    [{"id": s["identifier"], "kind": s["kind"]} for s in segments]
                ),
                _canonical(["grant-manage"]),
                read_clearance,
                _canonical(sorted(write_classifications)),
                _canonical(sorted(delegable_operations)),
                expires_at,
                TS,
            ),
        )
        connection.commit()


def seed(config: CairnConfig) -> tuple[str, str]:
    """Seeds the realm and two grant managers: a realm-root manager (the
    worker) and a repository-scoped manager with a deliberately tight
    envelope — internal clearance, ingest-only delegation, bounded expiry
    — for the envelope-violation rows. Returns (root token, scoped
    token)."""
    migrate_catalogue(config, lambda: NOW)
    root = mint_token(ROOT_CREDENTIAL_ID, lambda count: bytes(range(count)))
    scoped = mint_token(SCOPED_CREDENTIAL_ID, lambda count: bytes(count))
    with _open_write_connection(config.paths.data, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            (REALM, TS),
        )
        connection.execute(
            "INSERT INTO audit_heads "
            "(chain_kind, chain_identity, last_sequence, last_hash) "
            "VALUES ('realm', ?, 0, ?)",
            (REALM, bytes(32)),
        )
        for principal_id, credential_id, verifier, label in (
            (ROOT_ID, ROOT_CREDENTIAL_ID, root.verifier, "root-manager"),
            (SCOPED_ID, SCOPED_CREDENTIAL_ID, scoped.verifier, "scoped-manager"),
        ):
            connection.execute(
                "INSERT INTO principals (principal_id, kind, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (str(principal_id), "human", label, TS),
            )
            connection.execute(
                "INSERT INTO credentials "
                "(credential_id, principal_id, verifier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(credential_id), str(principal_id), verifier, TS, None),
            )
        connection.commit()
    add_manager_grant(
        config,
        UUID("66666666-6666-4666-8666-666666666666"),
        principal_id=ROOT_ID,
        segments=[],
        read_clearance="restricted",
        write_classifications=["internal", "public", "restricted"],
        delegable_operations=["ingest", "retrieve", "promote", "invalidate"],
        expires_at=None,
    )
    add_manager_grant(
        config,
        UUID("77777777-7777-4777-8777-777777777777"),
        principal_id=SCOPED_ID,
        segments=[REPO],
        read_clearance="internal",
        write_classifications=["internal"],
        delegable_operations=["ingest"],
        expires_at=FUTURE_TS,
    )
    return root.text, scoped.text


class Api:
    def __init__(self, client: AsyncClient, token: str) -> None:
        self._client = client
        self._token = token
        self._counter = 0

    def _key(self) -> str:
        self._counter += 1
        return f"{self._counter:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    async def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        token: str | None = None,
        idempotency_key: str | None = None,
    ) -> HTTPXResponse:
        return await self._client.post(
            path,
            content=json.dumps(body),
            headers={
                "Authorization": f"Bearer {token or self._token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key or self._key(),
            },
        )


def grant_proposal(
    principal_id: str,
    *,
    segments: list[dict[str, str]] | None = None,
    operations: list[str] | None = None,
    read_clearance: str = "internal",
    write_classifications: list[str] | None = None,
    expires_at: str | None = NEAR_EXPIRY,
) -> dict[str, object]:
    proposal: dict[str, object] = {
        "principal_id": principal_id,
        "realm_id": REALM,
        "segments": segments if segments is not None else [REPO],
        "operations": operations if operations is not None else ["ingest"],
        "read_clearance": read_clearance,
        "write_classifications": (
            write_classifications if write_classifications is not None else ["internal"]
        ),
    }
    if expires_at is not None:
        proposal["expires_at"] = expires_at
    return proposal


@pytest.mark.anyio
async def test_the_administration_chain_and_plaintext_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The full chain through REST: create a principal, issue its
    credential (plaintext exactly once, null on replay), delegate it an
    ingest grant within the envelope (`GRANT-01`), ingest with the new
    token, self-revoke the grant (`GRANT-08`), revoke the credential —
    and the token never reaches catalogue bytes, logs or metrics."""
    config = make_config(tmp_path)
    root, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, root)

            created = await api.post(
                "/v1/create-principal",
                {"realm_id": REALM, "kind": "workload", "label": "deploy-bot"},
            )
            assert created.status_code == 200
            principal_id = created.json()["result"]["principal_id"]
            assert created.json()["result"]["kind"] == "workload"

            issued = await api.post(
                "/v1/issue-credential",
                {"realm_id": REALM, "principal_id": principal_id},
                idempotency_key="99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            assert issued.status_code == 200
            token = issued.json()["result"]["plaintext"]
            assert type(token) is str and token.startswith("cairn1.")

            replay = await api.post(
                "/v1/issue-credential",
                {"realm_id": REALM, "principal_id": principal_id},
                idempotency_key="99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            assert replay.json()["outcome"] == "replayed"
            assert replay.json()["result"]["plaintext"] is None

            granted = await api.post(
                "/v1/create-grant",
                {"realm_id": REALM, "grant": grant_proposal(principal_id)},
            )
            assert granted.status_code == 200
            grant_id = granted.json()["result"]["grant_id"]

            ingested = await api.post(
                "/v1/ingest",
                {
                    "scope": {"realm": REALM, "segments": [REPO]},
                    "classification": "internal",
                    "source_type": "agent-claim",
                    "facts": [{"body": "the delegated worker can write"}],
                },
                token=token,
            )
            assert ingested.status_code == 200

            revoked_grant = await api.post(
                "/v1/revoke-grant",
                {
                    "realm_id": REALM,
                    "grant_id": grant_id,
                    "reason_code": "rotation_complete",
                },
            )
            assert revoked_grant.status_code == 200
            denied = await api.post(
                "/v1/ingest",
                {
                    "scope": {"realm": REALM, "segments": [REPO]},
                    "classification": "internal",
                    "source_type": "agent-claim",
                    "facts": [{"body": "after revocation"}],
                },
                token=token,
            )
            assert denied.status_code == 403

            revoked_credential = await api.post(
                "/v1/revoke-credential",
                {
                    "realm_id": REALM,
                    "credential_id": issued.json()["result"]["credential_id"],
                    "reason_code": "superseded",
                },
            )
            assert revoked_credential.status_code == 200
            unauthenticated = await api.post(
                "/v1/ingest",
                {
                    "scope": {"realm": REALM, "segments": [REPO]},
                    "classification": "internal",
                    "source_type": "agent-claim",
                    "facts": [{"body": "after credential revocation"}],
                },
                token=token,
            )
            assert unauthenticated.status_code == 401

            metrics = await client.get("/metrics")

    catalogue_bytes = (config.paths.data / CATALOGUE_FILENAME).read_bytes()
    assert token.encode() not in catalogue_bytes
    assert token not in metrics.text
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("proposal_override", "row"),
    [
        pytest.param({"segments": [OTHER]}, "grant-02", id="grant-02-scope"),
        pytest.param({"operations": ["promote"]}, "grant-03", id="grant-03-operation"),
        pytest.param(
            {"read_clearance": "restricted"}, "grant-04", id="grant-04-clearance"
        ),
        pytest.param(
            {"write_classifications": ["restricted"]},
            "grant-05",
            id="grant-05-classification",
        ),
        pytest.param(
            {"expires_at": BEYOND_MANAGER_EXPIRY}, "grant-06", id="grant-06-expiry"
        ),
        pytest.param(
            {"operations": ["grant-manage"]}, "grant-07", id="grant-07-grant-manage"
        ),
    ],
)
async def test_delegation_beyond_the_envelope_is_rejected(
    tmp_path: Path,
    proposal_override: dict[str, object],
    row: str,
) -> None:
    """`GRANT-02` through `GRANT-07`: the scoped manager's tight envelope
    refuses each excess, coarsely."""
    config = make_config(tmp_path)
    root, scoped = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, scoped)
            created = await api.post(
                "/v1/create-principal",
                {"realm_id": REALM, "kind": "human", "label": f"target-{row}"},
                token=root,
            )
            principal_id = created.json()["result"]["principal_id"]
            proposal = grant_proposal(principal_id)
            proposal.update(proposal_override)
            response = await api.post(
                "/v1/create-grant",
                {"realm_id": REALM, "grant": proposal},
            )

    assert response.status_code == 403
    assert response.json()["failure"]["code"] == "authorisation_denied"


@pytest.mark.anyio
async def test_a_malformed_grant_segment_names_the_field(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    root, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            proposal = grant_proposal(str(ROOT_ID))
            proposal["segments"] = [{"kind": "Repo!", "identifier": "x"}]
            response = await Api(client, root).post(
                "/v1/create-grant",
                {"realm_id": REALM, "grant": proposal},
            )

    assert response.status_code == 400
    assert response.json()["failure"]["detail"] == {
        "field_path": "grant.segments",
        "rule": "invalid_value",
    }


@pytest.mark.anyio
async def test_recovery_and_stranger_revocation_split(
    tmp_path: Path,
) -> None:
    """`GRANT-09` and `GRANT-10`: the realm-root manager may revoke a
    grant it did not issue; a non-issuer, non-root manager may not."""
    config = make_config(tmp_path)
    root, scoped = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, root)
            created = await api.post(
                "/v1/create-principal",
                {"realm_id": REALM, "kind": "human", "label": "target"},
            )
            principal_id = created.json()["result"]["principal_id"]

            issued_by_scoped = await api.post(
                "/v1/create-grant",
                {"realm_id": REALM, "grant": grant_proposal(principal_id)},
                token=scoped,
            )
            assert issued_by_scoped.status_code == 200
            first_grant = issued_by_scoped.json()["result"]["grant_id"]

            issued_by_root = await api.post(
                "/v1/create-grant",
                {"realm_id": REALM, "grant": grant_proposal(principal_id)},
            )
            assert issued_by_root.status_code == 200
            second_grant = issued_by_root.json()["result"]["grant_id"]

            stranger_attempt = await api.post(
                "/v1/revoke-grant",
                {
                    "realm_id": REALM,
                    "grant_id": second_grant,
                    "reason_code": "not_mine_to_revoke",
                },
                token=scoped,
            )
            assert stranger_attempt.status_code == 403
            assert stranger_attempt.json()["failure"]["code"] == "authorisation_denied"

            recovery = await api.post(
                "/v1/revoke-grant",
                {
                    "realm_id": REALM,
                    "grant_id": first_grant,
                    "reason_code": "recovery_revocation",
                },
            )
            assert recovery.status_code == 200


@pytest.mark.anyio
async def test_key_reuse_with_a_different_command_conflicts_without_detail(
    tmp_path: Path,
) -> None:
    """The P-26 idempotency-conflict contract at the wire: 409, the coarse
    code, and no ``detail``."""
    config = make_config(tmp_path)
    root, _ = seed(config)
    application = build_application(config)

    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as client:
            api = Api(client, root)
            key = "88888888-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            first = await api.post(
                "/v1/create-principal",
                {"realm_id": REALM, "kind": "human", "label": "first"},
                idempotency_key=key,
            )
            assert first.status_code == 200
            conflict = await api.post(
                "/v1/create-principal",
                {"realm_id": REALM, "kind": "human", "label": "second"},
                idempotency_key=key,
            )

    assert conflict.status_code == 409
    failure = conflict.json()["failure"]
    assert failure["code"] == "idempotency_conflict"
    assert "detail" not in failure


def test_the_p35_comment_states_the_accepted_reasoning() -> None:
    """P-35's single code obligation, pinned: the corrected comment must
    acknowledge revoke-grant's pre-existing row and cite the acceptance
    rather than claim every cleared identity was freshly minted."""
    # v1 -> rest -> transports -> tests -> root: parents[4] after the P-50
    # Task 1 move added the "rest" directory level (was parents[3] before).
    source = (
        Path(__file__).parents[4] / "src" / "cairn" / "catalogue" / "transactions.py"
    ).read_text(encoding="utf-8")
    assert "P-35" in source
    assert "revoke-grant's names a real,\n" in source
    assert "minted for this request\n" not in source
