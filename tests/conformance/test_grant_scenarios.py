"""§6.6 conformance: grant creation, delegation, expiry and revocation
(GRANT-01–10).

The seeded pair mirrors ``tests/transports/rest/v1/test_admin_routes.py``: a
realm-root grant manager with a wide delegation envelope, and a
repository-scoped manager with a deliberately tight one — internal
clearance, ingest-only delegation, bounded expiry — so each envelope
dimension has an excess to refuse.
"""

from pathlib import Path
from uuid import UUID

import pytest
from conftest import (
    OTHER_REPO,
    REALM,
    REPO,
    Instance,
    Running,
    scenario,
    serve,
)
from transport import OperationOutcome

NEAR_EXPIRY = "2026-12-01T00:00:00+00:00"
BEYOND_MANAGER_EXPIRY = "2027-06-01T00:00:00+00:00"


def seed_managers(instance: Instance) -> tuple[str, str, UUID]:
    """Root manager (wide envelope, no expiry), scoped manager (tight
    envelope), returning both tokens and the scoped manager's principal
    id."""
    root_principal = instance.add_principal(label="root-manager")
    root_token = instance.add_credential(root_principal)
    instance.add_grant(
        root_principal,
        segments=[],
        operations=["grant-manage"],
        read_clearance="restricted",
        write_classifications=["internal", "public", "restricted"],
        delegable_operations=["ingest", "retrieve", "promote", "invalidate"],
        expires_at=None,
    )
    scoped_principal = instance.add_principal(label="scoped-manager")
    scoped_token = instance.add_credential(scoped_principal)
    instance.add_grant(
        scoped_principal,
        segments=[REPO],
        operations=["grant-manage"],
        read_clearance="internal",
        write_classifications=["internal"],
        delegable_operations=["ingest"],
    )
    return root_token, scoped_token, scoped_principal


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


async def create_target_principal(running: Running, root_token: str) -> str:
    created = await running.client.create_principal(
        {"realm_id": REALM, "kind": "human", "label": "delegation-target"},
        credential=root_token,
    )
    assert created.result is not None, created.text
    return str(created.result["principal_id"])


async def create_grant(
    running: Running, token: str, proposal: dict[str, object]
) -> OperationOutcome:
    return await running.client.create_grant(
        {"realm_id": REALM, "grant": proposal}, credential=token
    )


@scenario("GRANT-01")
@pytest.mark.anyio
async def test_grant_01_within_envelope_succeeds(
    tmp_path: Path, transport: str
) -> None:
    """A grant created wholly within its manager's delegation envelope
    succeeds, and the delegated grant is live."""
    instance = Instance(tmp_path, "GRANT-01")
    root_token, scoped_token, _ = seed_managers(instance)

    async with serve(instance, transport) as running:
        principal_id = await create_target_principal(running, root_token)
        outcome = await create_grant(
            running, scoped_token, grant_proposal(principal_id)
        )

    assert outcome.outcome == "committed"
    assert outcome.result is not None
    assert UUID(str(outcome.result["grant_id"]))
    assert instance.events("realm")[-1]["outcome"] == "allow"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scenario_id", "override", "reason_code"),
    [
        pytest.param(
            "GRANT-02",
            {"segments": [OTHER_REPO]},
            "scope_outside_envelope",
            id="GRANT-02",
        ),
        pytest.param(
            "GRANT-03",
            {"operations": ["promote"]},
            "operation_outside_envelope",
            id="GRANT-03",
        ),
        pytest.param(
            "GRANT-04",
            {"read_clearance": "restricted"},
            "clearance_exceeds_envelope",
            id="GRANT-04",
        ),
        pytest.param(
            "GRANT-05",
            {"write_classifications": ["restricted"]},
            "classification_outside_envelope",
            id="GRANT-05",
        ),
        pytest.param(
            "GRANT-06",
            {"expires_at": BEYOND_MANAGER_EXPIRY},
            "expiry_exceeds_envelope",
            id="GRANT-06",
        ),
    ],
)
async def test_grants_02_to_06_delegation_beyond_the_envelope_is_rejected(
    tmp_path: Path,
    transport: str,
    scenario_id: str,
    override: dict[str, object],
    reason_code: str,
    request: pytest.FixtureRequest,
) -> None:
    """GRANT-02 scope, GRANT-03 operation, GRANT-04 read clearance,
    GRANT-05 write classification, GRANT-06 expiry: each excess beyond
    the scoped manager's envelope is refused.

    The caller is told only ``authorisation_denied``, but the durable
    denial names the dimension, and each row asserts its own. Without
    that, one envelope check standing in for another — or an ordering
    change that lets an earlier check answer for a later one — would
    leave all five rows green while proving one thing five times.
    """
    request.node.add_marker(pytest.mark.scenario(scenario_id))
    instance = Instance(tmp_path, scenario_id)
    root_token, scoped_token, _ = seed_managers(instance)

    async with serve(instance, transport) as running:
        principal_id = await create_target_principal(running, root_token)
        proposal = grant_proposal(principal_id)
        proposal.update(override)
        outcome = await create_grant(running, scoped_token, proposal)

    assert outcome.failure_code == "authorisation_denied"
    # The coarse code carries no dimension: the disclosure stays in the
    # audit chain, where only an audit-read grant can reach it.
    assert outcome.detail is None
    deny = instance.events("realm")[-1]
    assert deny["outcome"] == "deny"
    assert deny["reason_code"] == reason_code


@scenario("GRANT-07")
@pytest.mark.anyio
async def test_grant_07_grant_manage_delegation_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """Delegation of grant-manage is rejected even for the realm-root
    manager: the operation is structurally non-delegable."""
    instance = Instance(tmp_path, "GRANT-07")
    root_token, _, _ = seed_managers(instance)

    async with serve(instance, transport) as running:
        principal_id = await create_target_principal(running, root_token)
        outcome = await create_grant(
            running,
            root_token,
            grant_proposal(principal_id, operations=["grant-manage"]),
        )

    assert outcome.failure_code == "authorisation_denied"
    assert instance.events("realm")[-1]["outcome"] == "deny"


@scenario("GRANT-08")
@pytest.mark.anyio
async def test_grant_08_issuer_revokes_its_issued_grant(
    tmp_path: Path, transport: str
) -> None:
    """A grant issuer may revoke the grant it issued."""
    instance = Instance(tmp_path, "GRANT-08")
    root_token, scoped_token, _ = seed_managers(instance)

    async with serve(instance, transport) as running:
        principal_id = await create_target_principal(running, root_token)
        created = await create_grant(
            running, scoped_token, grant_proposal(principal_id)
        )
        assert created.result is not None, created.text
        grant_id = created.result["grant_id"]
        revoked = await running.client.revoke_grant(
            {
                "realm_id": REALM,
                "grant_id": grant_id,
                "reason_code": "rotation_complete",
            },
            credential=scoped_token,
        )

    assert revoked.result is not None, revoked.text
    assert revoked.result["grant_id"] == grant_id
    assert instance.count("grant_revocations") == 1
    assert instance.events("realm")[-1]["outcome"] == "allow"


@scenario("GRANT-09")
@pytest.mark.anyio
async def test_grant_09_realm_root_manager_recovery_revocation(
    tmp_path: Path,
    transport: str,
) -> None:
    """A realm-root grant manager may revoke a grant it did not issue —
    the recovery path."""
    instance = Instance(tmp_path, "GRANT-09")
    root_token, scoped_token, _ = seed_managers(instance)

    async with serve(instance, transport) as running:
        principal_id = await create_target_principal(running, root_token)
        created = await create_grant(
            running, scoped_token, grant_proposal(principal_id)
        )
        assert created.result is not None, created.text
        grant_id = created.result["grant_id"]
        recovery = await running.client.revoke_grant(
            {
                "realm_id": REALM,
                "grant_id": grant_id,
                "reason_code": "recovery_revocation",
            },
            credential=root_token,
        )

    assert recovery.result is not None, recovery.text
    assert recovery.result["grant_id"] == grant_id
    assert instance.count("grant_revocations") == 1
    assert instance.events("realm")[-1]["outcome"] == "allow"


@scenario("GRANT-10")
@pytest.mark.anyio
async def test_grant_10_any_other_revocation_attempt_is_rejected(
    tmp_path: Path,
    transport: str,
) -> None:
    """A manager that neither issued the grant nor holds realm-root
    grant-manage cannot revoke it."""
    instance = Instance(tmp_path, "GRANT-10")
    root_token, scoped_token, _ = seed_managers(instance)

    async with serve(instance, transport) as running:
        principal_id = await create_target_principal(running, root_token)
        created = await create_grant(running, root_token, grant_proposal(principal_id))
        assert created.result is not None, created.text
        attempt = await running.client.revoke_grant(
            {
                "realm_id": REALM,
                "grant_id": created.result["grant_id"],
                "reason_code": "not_mine_to_revoke",
            },
            credential=scoped_token,
        )

    assert attempt.failure_code == "authorisation_denied"
    assert instance.count("grant_revocations") == 0
    assert instance.events("realm")[-1]["outcome"] == "deny"
