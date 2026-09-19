"""§6.6 conformance: exact-evidence custody (EVIDENCE-04) and audit
visibility, completeness, atomicity and hash-chain verification
(AUDIT-01–05).

EVIDENCE-01–03 require retrieval and live in
``test_retrieval_scenarios.py``; EVIDENCE-04 — rejection before any
durable Attic write — is proven here, through the wire.
"""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from conftest import (
    OTHER_REPO,
    REALM,
    REPO,
    Instance,
    ingest_body,
    scenario,
    serve,
)

from cairn.catalogue.sqlite import CATALOGUE_FILENAME
from cairn.catalogue.verification import VerificationError, verify_catalogue
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"  # provenance: tests/transports/v1/test_auth.py
ATTIC_FILENAME = "attic.sqlite3"


@scenario("EVIDENCE-04")
@pytest.mark.anyio
async def test_evidence_04_secret_material_never_reaches_the_attic(
    tmp_path: Path,
    transport: str,
) -> None:
    """Suspected secret material in an exact-evidence payload is rejected
    before any durable Attic write: no assertion, no evidence record, no
    outbox row, no Attic payload, no secret bytes on disk."""
    instance = Instance(tmp_path, "EVIDENCE-04", attic=True)
    token = instance.add_actor(segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(
            ingest_body(
                evidence_payload=f"deploy log: export AWS_KEY={AWS_EXAMPLE_KEY}"
            ),
            credential=token,
        )

    assert outcome.failure_code == "secret_rejected"
    assert outcome.detail is not None
    assert outcome.detail["field_path"] == "evidence_payload"
    assert instance.count("assertions") == 0
    assert instance.count("evidence_records") == 0
    assert instance.count("evidence_outbox") == 0
    assert AWS_EXAMPLE_KEY.encode() not in instance.catalogue_bytes()
    attic_path = instance.data_path / ATTIC_FILENAME
    if attic_path.exists():
        with closing(sqlite3.connect(attic_path)) as connection, connection:
            rows = connection.execute("SELECT COUNT(*) FROM payloads").fetchone()
        assert rows[0] == 0
        assert AWS_EXAMPLE_KEY.encode() not in attic_path.read_bytes()


@scenario("AUDIT-01")
@pytest.mark.anyio
async def test_audit_01_allowed_denied_and_failed_attempts_all_record(
    tmp_path: Path,
    transport: str,
) -> None:
    """An allowed request, an authorisation denial and a failed
    (unauthenticated) attempt each create their required audit event,
    durable by the time the response has returned."""
    instance = Instance(tmp_path, "AUDIT-01")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        client = running.client
        allowed = await client.ingest(ingest_body(), credential=token)
        denied = await client.ingest(
            ingest_body(segments=[OTHER_REPO]), credential=token
        )
        unauthenticated = await client.ingest({}, credential=None)

    assert allowed.outcome == "committed"
    assert denied.failure_code == "authorisation_denied"
    assert unauthenticated.failure_code == "authentication_failed"
    realm_outcomes = [event["outcome"] for event in instance.events("realm")]
    assert realm_outcomes == ["allow"]
    instance_events = instance.events("instance")
    assert [event["outcome"] for event in instance_events] == ["deny", "deny"]
    assert instance_events[0]["reason_code"] == "ingest_grant_not_held"


@scenario("AUDIT-02")
@pytest.mark.anyio
async def test_audit_02_audit_read_is_prefix_confined(
    tmp_path: Path, transport: str
) -> None:
    """audit-read exposes only events at its prefix and descendants:
    traffic in a sibling scope is invisible to a prefix-scoped reader."""
    instance = Instance(tmp_path, "AUDIT-02")
    principal_id = instance.add_principal()
    writer = instance.add_credential(principal_id)
    instance.add_grant(principal_id, segments=[], operations=["ingest"])
    reader = instance.add_actor(segments=[REPO], operations=["audit-read"])

    async with serve(instance, transport) as running:
        client = running.client
        in_prefix = await client.ingest(ingest_body(segments=[REPO]), credential=writer)
        sibling = await client.ingest(
            ingest_body(segments=[OTHER_REPO]), credential=writer
        )
        page = await client.read_audit_events(
            {"realm_id": REALM, "scope_prefix": [REPO]}, credential=reader
        )

    assert in_prefix.outcome == "committed"
    assert sibling.outcome == "committed"
    assert page.result is not None, page.text
    events = page.result["events"]
    assert isinstance(events, list)
    assert events, "the prefix-scoped ingest must be visible"
    scoped = [event for event in events if event["requested_scope"] is not None]
    assert scoped, "at least the prefix-scoped ingest carries its scope"
    for event in scoped:
        assert event["requested_scope"]["segments"][0] == {
            "kind": "repository",
            "id": "acme-repo",
        }
    identifiers = {
        segment["id"]
        for event in scoped
        for segment in event["requested_scope"]["segments"]
    }
    assert "other-repo" not in identifiers


@scenario("AUDIT-03")
@pytest.mark.anyio
async def test_audit_03_events_omit_all_prohibited_content(
    tmp_path: Path,
    transport: str,
) -> None:
    """Audit events omit prohibited content: fact bodies, bearer tokens
    and screened secret material never appear in any stored event."""
    instance = Instance(tmp_path, "AUDIT-03")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])
    fact_body = "The deploy pipeline uses kaniko."

    async with serve(instance, transport) as running:
        client = running.client
        allowed = await client.ingest(
            ingest_body(facts=[{"body": fact_body}]), credential=token
        )
        secret_attempt = await client.ingest(
            ingest_body(facts=[{"body": f"key {AWS_EXAMPLE_KEY}"}]), credential=token
        )

    assert allowed.outcome == "committed"
    assert secret_attempt.failure_code == "secret_rejected"
    with (
        closing(sqlite3.connect(instance.data_path / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        stored_events = [
            bytes(row[0]).decode("utf-8")
            for row in connection.execute(
                "SELECT canonical_event FROM audit_events"
            ).fetchall()
        ]
    assert stored_events
    for canonical in stored_events:
        assert fact_body not in canonical
        assert token not in canonical
        assert AWS_EXAMPLE_KEY not in canonical


@scenario("AUDIT-04")
@pytest.mark.anyio
async def test_audit_04_failed_mutation_leaves_no_orphan(
    tmp_path: Path, transport: str
) -> None:
    """A failed authorised mutation leaves neither a mutation nor an
    orphan audit event: the refusal's denial event is appended, nothing
    else is written, and the chain still verifies."""
    instance = Instance(tmp_path, "AUDIT-04")
    token = instance.add_actor(
        segments=[REPO], operations=["ingest", "retrieve", "promote"]
    )
    unknown_fact = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

    async with serve(instance, transport) as running:
        outcome = await running.client.promote(
            {
                "fact_ids": [unknown_fact],
                "evidence": {
                    "external_uri": "https://ci.example.org/run/9",
                    "payload_digest": "0" * 64,
                },
                "target_scope": {"realm": REALM, "segments": [REPO]},
                "reason": "promotion of a fact that does not exist",
            },
            credential=token,
        )

    # An unknown fact is answered with the same coarse denial as a
    # forbidden one — existence-hiding per I-67 — while the durable
    # denial event records the precise reason.
    assert outcome.failure_code == "authorisation_denied"
    assert instance.count("assertions") == 0
    assert instance.count("facts") == 0
    assert instance.count("idempotency_records") == 0
    denials = [
        event
        for chain in ("realm", "instance")
        for event in instance.events(chain)
        if event["outcome"] == "deny"
    ]
    assert len(denials) == 1
    assert denials[0]["reason_code"] == "fact_unknown"
    report = verify_catalogue(instance.config)
    assert report is not None


def _tampered_copy(instance: Instance, destination: Path) -> CairnConfig:
    """A byte-for-byte copy of the instance's data directory, with the
    catalogue's append-only triggers dropped so a test can forge history
    the way an attacker with file access would."""
    shutil.copytree(instance.data_path, destination / "data")
    (destination / "credentials").mkdir(exist_ok=True)
    with (
        closing(
            sqlite3.connect(destination / "data" / CATALOGUE_FILENAME)
        ) as connection,
        connection,
    ):
        connection.execute("DROP TRIGGER trg_audit_events_no_update")
        connection.execute("DROP TRIGGER trg_audit_events_no_delete")
        connection.commit()
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=instance.config.instance_id,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(
            data=destination / "data", credentials=destination / "credentials"
        ),
    )


@scenario("AUDIT-05")
@pytest.mark.anyio
async def test_audit_05_chain_verifies_and_tampering_is_detected(
    tmp_path: Path,
    transport: str,
) -> None:
    """The intact hash chain verifies; a modified, a removed and a
    reordered event are each detected."""
    instance = Instance(tmp_path, "AUDIT-05")
    token = instance.add_actor(segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        for index in range(3):
            body = ingest_body(facts=[{"body": f"observation number {index}"}])
            committed = await running.client.ingest(body, credential=token)
            assert committed.outcome == "committed"

    # Intact: the full offline verification passes.
    verify_catalogue(instance.config)

    # Modified: flip a recorded outcome from allow to deny. The fact
    # body itself is deliberately absent from events (AUDIT-03), so the
    # forgery targets a field every event does carry.
    modified = _tampered_copy(instance, tmp_path / "modified")
    with (
        closing(
            sqlite3.connect(modified.paths.data / CATALOGUE_FILENAME)
        ) as connection,
        connection,
    ):
        row = connection.execute(
            "SELECT rowid, canonical_event FROM audit_events "
            "WHERE chain_kind = 'realm' AND sequence = 1"
        ).fetchone()
        original = bytes(row[1])
        assert b'"outcome":"allow"' in original
        connection.execute(
            "UPDATE audit_events SET canonical_event = ? WHERE rowid = ?",
            (original.replace(b'"outcome":"allow"', b'"outcome":"deny"'), row[0]),
        )
        connection.commit()
    with pytest.raises(VerificationError):
        verify_catalogue(modified)

    removed = _tampered_copy(instance, tmp_path / "removed")
    with (
        closing(sqlite3.connect(removed.paths.data / CATALOGUE_FILENAME)) as connection,
        connection,
    ):
        connection.execute(
            "DELETE FROM audit_events WHERE chain_kind = 'realm' AND sequence = 2"
        )
        connection.commit()
    with pytest.raises(VerificationError):
        verify_catalogue(removed)

    reordered = _tampered_copy(instance, tmp_path / "reordered")
    with (
        closing(
            sqlite3.connect(reordered.paths.data / CATALOGUE_FILENAME)
        ) as connection,
        connection,
    ):
        first = connection.execute(
            "SELECT canonical_event FROM audit_events "
            "WHERE chain_kind = 'realm' AND sequence = 1"
        ).fetchone()[0]
        second = connection.execute(
            "SELECT canonical_event FROM audit_events "
            "WHERE chain_kind = 'realm' AND sequence = 2"
        ).fetchone()[0]
        connection.execute(
            "UPDATE audit_events SET canonical_event = ? "
            "WHERE chain_kind = 'realm' AND sequence = 1",
            (second,),
        )
        connection.execute(
            "UPDATE audit_events SET canonical_event = ? "
            "WHERE chain_kind = 'realm' AND sequence = 2",
            (first,),
        )
        connection.commit()
    with pytest.raises(VerificationError):
        verify_catalogue(reordered)
