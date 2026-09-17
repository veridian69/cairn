"""§6.6 conformance: authentication and grant evaluation (AUTH-01–07).

Each scenario runs through the real surface of the run's transport
against its own fresh instance and asserts the expected outcome, the
stable failure code and the durable audit evidence.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from conftest import (
    FUTURE_TS,
    NOW,
    REPO,
    Instance,
    ingest_body,
    scenario,
    serve,
)

from cairn.catalogue.sqlite import canonical_timestamp


@scenario("AUTH-01")
@pytest.mark.anyio
async def test_auth_01_valid_credential_and_matching_grant_permit(
    tmp_path: Path,
    transport: str,
) -> None:
    """A valid credential and a matching operation grant permit the
    request."""
    instance = Instance(tmp_path, "AUTH-01")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(ingest_body(), credential=token)

    assert outcome.outcome == "committed"
    allow = instance.events("realm")[-1]
    assert allow["outcome"] == "allow"
    assert allow["action_code"] == "ingest"


@scenario("AUTH-02")
@pytest.mark.anyio
async def test_auth_02_absent_credential_is_denied(
    tmp_path: Path, transport: str
) -> None:
    """An absent credential is denied, with a durable instance-chain
    denial.

    The empty body is deliberate: authentication answers before the
    request is ever parsed. REST's ``WWW-Authenticate`` challenge is
    transport furniture and is asserted where I-73's header table is the
    subject, in ``tests/transports/rest/v1/test_errors.py``.
    """
    instance = Instance(tmp_path, "AUTH-02")

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest({}, credential=None)

    assert outcome.failure_code == "authentication_failed"
    deny = instance.events("instance")[-1]
    assert deny["outcome"] == "deny"


@scenario("AUTH-03")
@pytest.mark.anyio
async def test_auth_03_unknown_credential_denied_without_disclosure(
    tmp_path: Path,
    transport: str,
) -> None:
    """An unknown credential is denied without revealing whether a
    principal or grant exists: a well-formed token for a credential that
    was never issued and a wrong-secret token for a real credential fail
    identically apart from the correlation identity."""
    instance = Instance(tmp_path, "AUTH-03")
    real = instance.add_actor(segments=[REPO], operations=["ingest"])
    # A structurally valid token whose credential identity was never
    # issued, and the real credential's identity with the wrong secret.
    never_issued = "cairn1.99999999-9999-4999-8999-999999999999." + "A" * 43
    wrong_secret = real.rsplit(".", 1)[0] + "." + "B" * 43

    async with serve(instance, transport) as running:
        outcomes = [
            await running.client.ingest(ingest_body(), credential=bearer)
            for bearer in (never_issued, wrong_secret)
        ]

    failures = []
    for outcome in outcomes:
        assert outcome.failure_code == "authentication_failed"
        assert outcome.failure is not None
        failure = dict(outcome.failure)
        failure.pop("correlation_id")
        failures.append(failure)
    assert failures[0] == failures[1]


@scenario("AUTH-04")
@pytest.mark.anyio
async def test_auth_04_expired_credential_is_denied(
    tmp_path: Path, transport: str
) -> None:
    """An expired credential is denied: valid before its expiry instant,
    denied after the clock passes it."""
    instance = Instance(tmp_path, "AUTH-04")
    expiry = canonical_timestamp(NOW + timedelta(hours=1))
    principal_id = instance.add_principal()
    token = instance.add_credential(principal_id, expires_at=expiry)
    instance.add_grant(principal_id, segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        before = await running.client.ingest(ingest_body(), credential=token)
        instance.clock.now = NOW + timedelta(hours=2)
        after = await running.client.ingest(ingest_body(), credential=token)

    assert before.outcome == "committed"
    assert after.failure_code == "authentication_failed"


@scenario("AUTH-05")
@pytest.mark.anyio
async def test_auth_05_expired_grant_is_denied(tmp_path: Path, transport: str) -> None:
    """An expired grant is denied: the same request succeeds while the
    grant is live and is refused once the clock passes its expiry."""
    instance = Instance(tmp_path, "AUTH-05")
    principal_id = instance.add_principal()
    token = instance.add_credential(principal_id)
    expiry = canonical_timestamp(NOW + timedelta(hours=1))
    instance.add_grant(
        principal_id, segments=[REPO], operations=["ingest"], expires_at=expiry
    )

    async with serve(instance, transport) as running:
        before = await running.client.ingest(ingest_body(), credential=token)
        instance.clock.now = NOW + timedelta(hours=2)
        after = await running.client.ingest(ingest_body(), credential=token)

    assert before.outcome == "committed"
    assert after.failure_code == "authorisation_denied"
    # I-53: a denial with no authorising grant has no realm attribution
    # and lands durably on the instance fallback chain.
    deny = instance.events("instance")[-1]
    assert deny["outcome"] == "deny"
    assert deny["reason_code"] == "ingest_grant_not_held"


@scenario("AUTH-06")
@pytest.mark.anyio
async def test_auth_06_revoked_grant_is_denied(tmp_path: Path, transport: str) -> None:
    """A revoked grant is denied."""
    instance = Instance(tmp_path, "AUTH-06")
    principal_id = instance.add_principal()
    token = instance.add_credential(principal_id)
    grant_id = instance.add_grant(
        principal_id, segments=[REPO], operations=["ingest"], expires_at=FUTURE_TS
    )
    instance.revoke_grant_row(grant_id)

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(ingest_body(), credential=token)

    assert outcome.failure_code == "authorisation_denied"
    deny = instance.events("instance")[-1]
    assert deny["outcome"] == "deny"
    assert deny["reason_code"] == "ingest_grant_not_held"


@scenario("AUTH-07")
@pytest.mark.anyio
async def test_auth_07_valid_credential_without_the_operation_is_denied(
    tmp_path: Path,
    transport: str,
) -> None:
    """A valid credential whose grant lacks the requested operation is
    denied — a promote-only grant cannot ingest."""
    instance = Instance(tmp_path, "AUTH-07")
    token = instance.add_actor(segments=[REPO], operations=["promote"])

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(ingest_body(), credential=token)

    assert outcome.failure_code == "authorisation_denied"
    deny = instance.events("instance")[-1]
    assert deny["outcome"] == "deny"
    assert deny["action_code"] == "ingest"
    assert deny["reason_code"] == "ingest_grant_not_held"
