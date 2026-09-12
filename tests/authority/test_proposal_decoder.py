"""Canonical stored values must fail closed before lookup or replay."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import test_mutations as h
import test_proposals as p

from cairn.authority import proposal_codec as codec
from cairn.authority.custody import CustodyValueError
from cairn.authority.proposal_codec import decode
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import Committed, Rejected


def inventory(path: Path) -> dict[str, Any]:
    return {
        table: h._rows(path, f"SELECT * FROM {table}")
        for table in (
            "memory_proposals",
            "memory_proposal_decisions",
            "facts",
            "fact_invalidations",
            "evidence_records",
            "idempotency_records",
            "projection_outbox",
        )
    }


def refuse(path: Path, service: Any, method: str, command: Any, key: UUID) -> None:
    before, audit = inventory(path), h._audit_rows(path)
    result = p.invoke(service, method, command, key=key)
    assert isinstance(result, Rejected)
    assert result.failure.code.value in {"authorisation_denied", "idempotency_conflict"}
    assert inventory(path) == before
    after = h._audit_rows(path)
    assert len(after) == len(audit) + 1
    action = "memory-propose" if method == "propose" else f"memory-proposal-{method}"
    assert [r for r in after if r[-2] == "deny"] == [
        (
            "instance",
            "data",
            action,
            "deny",
            "proposal_authorisation_denied"
            if result.failure.code.value == "authorisation_denied"
            else "proposal_conflict",
        )
    ]


@pytest.mark.parametrize("method", ["read", "list", "accept", "reject", "propose"])
@pytest.mark.parametrize("state", ["pending", "accepted", "rejected"])
def test_uppercase_proposal_identity_is_schema_valid_and_refused(
    tmp_path: Path, method: str, state: str
) -> None:
    p.seed(tmp_path)
    proposal = replace(
        p.proposal(), proposal_id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    )
    service, creation_key, decision_key = p.service(tmp_path), uuid4(), uuid4()
    assert isinstance(
        p.invoke(service, "propose", proposal, key=creation_key), Committed
    )
    rejection = p.api().RejectProposal(proposal.scope, proposal.proposal_id, "No")
    if state != "pending":
        assert isinstance(
            p.invoke(
                service,
                "accept" if state == "accepted" else "reject",
                p.accept(proposal) if state == "accepted" else rejection,
                key=decision_key,
            ),
            Committed,
        )
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("BEGIN")
        connection.execute("PRAGMA defer_foreign_keys=ON")
        connection.execute("DROP TRIGGER memory_proposals_no_update")
        connection.execute("DROP TRIGGER memory_proposal_decisions_no_update")
        connection.execute("UPDATE memory_proposals SET proposal_id=upper(proposal_id)")
        connection.execute(
            "UPDATE memory_proposal_decisions SET proposal_id=upper(proposal_id)"
        )
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    command = {
        "read": p.api().ReadProposal(proposal.scope, proposal.proposal_id),
        "list": p.api().ListProposals(proposal.scope),
        "propose": proposal,
        "accept": p.accept(proposal),
        "reject": rejection,
    }[method]
    refuse(
        tmp_path,
        service,
        method,
        command,
        creation_key if method == "propose" else decision_key,
    )


@pytest.mark.parametrize("method", ["read", "list", "accept", "reject", "propose"])
@pytest.mark.parametrize(
    "table,column,value",
    [
        ("memory_proposals", "target_segments", '[ {"kind":"project","id":"alpha"} ]'),
        (
            "memory_proposals",
            "target_segments",
            '[{"id":"alpha","kind":"project","id":"alpha"}]',
        ),
        ("memory_proposals", "reason", ""),
        ("memory_proposals", "idempotency_key", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"),
        ("memory_proposals", "command_digest", b"short"),
        ("memory_proposals", "operation", "memory-proposal-reject"),
        (
            "memory_proposal_decisions",
            "evidence_id",
            "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
        ),
        ("memory_proposal_decisions", "reason", "unexpected acceptance reason"),
        ("memory_proposal_decisions", "target_classification", "unknown"),
        (
            "memory_proposal_decisions",
            "idempotency_key",
            "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
        ),
    ],
)
def test_all_routes_validate_complete_stored_records(
    tmp_path: Path, method: str, table: str, column: str, value: Any
) -> None:
    p.seed(tmp_path)
    proposal, service, creation_key, decision_key = (
        p.proposal(),
        p.service(tmp_path),
        uuid4(),
        uuid4(),
    )
    assert isinstance(
        p.invoke(service, "propose", proposal, key=creation_key), Committed
    )
    assert isinstance(
        p.invoke(service, "accept", p.accept(proposal), key=decision_key), Committed
    )
    with _open_write_connection(tmp_path, create=False) as connection:
        # Deliberately corrupt a disposable catalogue, including schema-invalid
        # fields. The uppercase proposal regression above keeps all checks/FKs.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"DROP TRIGGER {table}_no_update")
        connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    command = {
        "read": p.api().ReadProposal(proposal.scope, proposal.proposal_id),
        "list": p.api().ListProposals(proposal.scope),
        "propose": proposal,
        "accept": p.accept(proposal),
        "reject": p.api().RejectProposal(proposal.scope, proposal.proposal_id, "No"),
    }[method]
    refuse(
        tmp_path,
        service,
        method,
        command,
        creation_key if method == "propose" else decision_key,
    )


@pytest.mark.parametrize("method", ["propose", "accept", "reject"])
@pytest.mark.parametrize(
    "field",
    ["identity", "mutation_id", "command_digest", "extra", "duplicate", "whitespace"],
)
def test_replay_rejects_noncanonical_receipts(
    tmp_path: Path, method: str, field: str
) -> None:
    p.seed(tmp_path)
    proposal, service, key = p.proposal(), p.service(tmp_path), uuid4()
    if method != "propose":
        assert isinstance(p.invoke(service, "propose", proposal), Committed)
    command = {
        "propose": proposal,
        "accept": p.accept(proposal),
        "reject": p.api().RejectProposal(proposal.scope, proposal.proposal_id, "No"),
    }[method]
    first = p.invoke(service, method, command, key=key)
    assert isinstance(first, Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        raw = connection.execute(
            "SELECT result_bytes FROM idempotency_records WHERE mutation_id=?",
            (str(first.mutation_receipt.mutation_id),),
        ).fetchone()[0]
        document = json.loads(raw)
        receipt = document["mutation_receipt"] if method == "accept" else document
        if field == "identity":
            if method == "accept":
                document["result"]["evidence_id"] = document["result"][
                    "evidence_id"
                ].upper()
            else:
                document["proposal_id"] = document["proposal_id"].upper()
        elif field in {"mutation_id", "command_digest"}:
            receipt[field] = receipt[field].upper()
        elif field == "extra":
            receipt["unrecognised"] = True
        raw = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if field == "duplicate":
            raw = b'{"unused":0,"unused":1,' + raw[1:]
        elif field == "whitespace":
            raw = b" " + raw
        connection.execute("DROP TRIGGER trg_idempotency_records_no_update")
        connection.execute(
            "UPDATE idempotency_records SET result_bytes=?, result_digest=? WHERE mutation_id=?",
            (
                raw,
                hashlib.sha256(raw).digest(),
                str(first.mutation_receipt.mutation_id),
            ),
        )
    refuse(tmp_path, service, method, command, key)


@pytest.mark.parametrize(
    "field,value",
    [
        ("proposal_id", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"),
        ("proposal_id", "aaaaaaaabbbb4ccc8dddeeeeeeeeeeee"),
        ("proposal_id", "{aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee}"),
        ("proposal_id", 1),
        ("proposal_id", None),
        ("mutation_id", "urn:uuid:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        ("command_digest", "AB" * 32),
        ("command_digest", "ab " * 32),
        ("command_digest", "ab" * 31),
        ("command_digest", 0),
    ],
)
def test_identity_receipt_decoder_rejects_coercion(field: str, value: Any) -> None:
    document = dict(
        proposal_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        mutation_id="bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        command_digest="ab" * 32,
    )
    document[field] = value
    with pytest.raises(CustodyValueError):
        decode(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())


@pytest.mark.parametrize(
    "kind,column,value",
    [
        (kind, column, value)
        for kind, columns in (
            ("proposal", (0, 1, 7, 9, 11)),
            ("decision", (0, 2, 4, 6, 9, 10)),
        )
        for column in columns
        for value in ("AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE", None, 7)
    ]
    + [
        ("proposal", 2, "INVALID"),
        ("proposal", 3, "[]"),
        ("proposal", 4, '[{"id":7,"kind":"project"}]'),
        ("proposal", 4, '[{"id":"x","kind":"project","other":true}]'),
        ("proposal", 5, "INTERNAL"),
        ("proposal", 6, "é" * 2049),
        ("proposal", 8, "memory-proposal-reject"),
        ("proposal", 10, "ab" * 32),
        ("proposal", 12, "2026-09-10T00:00:00.0Z"),
        ("proposal", 12, "2026-13-10T00:00:00.000000Z"),
        ("decision", 1, "unknown"),
        ("decision", 3, "memory-proposal-reject"),
        ("decision", 5, b""),
        ("decision", 7, "2026-09-10T00:00:00.0Z"),
        ("decision", 7, "2026-13-10T00:00:00.000000Z"),
        ("decision", 8, "not null"),
        ("decision", 11, "INTERNAL"),
    ],
)
def test_complete_row_decoder_rejects_invalid_columns(
    kind: str, column: int, value: Any
) -> None:
    row: list[object] = (
        [
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "acme",
            '[{"id":"x","kind":"project"}]',
            "[]",
            "internal",
            "Reason",
            "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "memory-propose",
            "dddddddd-bbbb-5ccc-8ddd-eeeeeeeeeeee",
            b"a" * 32,
            "eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "2026-09-10T00:00:00.000000Z",
        ]
        if kind == "proposal"
        else [
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "accepted",
            "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "memory-proposal-accept",
            "dddddddd-bbbb-5ccc-8ddd-eeeeeeeeeeee",
            b"a" * 32,
            "eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "2026-09-10T00:00:00.000000Z",
            None,
            "ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "internal",
        ]
    )
    decoder = codec.decode_proposal if kind == "proposal" else codec.decode_decision
    decoder(tuple(row))  # Control: canonical values, including UUIDv5 keys, decode.
    row[column] = value
    # An empty source with a nonempty destination is tested separately; an
    # empty source AND target is legal, so use a descendant here.
    if kind == "proposal" and column == 3:
        row[4] = '[{"id":"x","kind":"project"}]'
    with pytest.raises(CustodyValueError):
        decoder(tuple(row))


@pytest.mark.parametrize("method", ["read", "list", "accept", "reject", "propose"])
def test_noncanonical_source_timestamp_is_refused(tmp_path: Path, method: str) -> None:
    p.seed(tmp_path)
    proposal, service, key = p.proposal(), p.service(tmp_path), uuid4()
    assert isinstance(p.invoke(service, "propose", proposal, key=key), Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("DROP TRIGGER trg_facts_no_update")
        connection.execute("UPDATE facts SET valid_from='2026-01-01T00:00:00.0Z'")
    command = {
        "read": p.api().ReadProposal(proposal.scope, proposal.proposal_id),
        "list": p.api().ListProposals(proposal.scope),
        "propose": proposal,
        "accept": p.accept(proposal),
        "reject": p.api().RejectProposal(proposal.scope, proposal.proposal_id, "No"),
    }[method]
    refuse(tmp_path, service, method, command, key)


def test_noncanonical_lookahead_cannot_supply_pagination_metadata(
    tmp_path: Path,
) -> None:
    p.seed(tmp_path)
    service = p.service(tmp_path)
    first = replace(
        p.proposal(), proposal_id=UUID("11111111-1111-4111-8111-111111111111")
    )
    second = replace(
        p.proposal(), proposal_id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    )
    for proposal in (first, second):
        assert isinstance(p.invoke(service, "propose", proposal), Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("DROP TRIGGER memory_proposals_no_update")
        connection.execute("UPDATE memory_proposals SET proposal_id=upper(proposal_id)")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    refuse(
        tmp_path, service, "list", p.api().ListProposals(first.scope, limit=1), uuid4()
    )


def test_acceptance_rejects_noncanonical_named_evidence_scope(tmp_path: Path) -> None:
    p.seed(tmp_path)
    proposal, service = p.proposal(), p.service(tmp_path)
    assert isinstance(p.invoke(service, "propose", proposal), Committed)
    with _open_write_connection(tmp_path, create=False) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("DROP TRIGGER trg_evidence_records_no_update")
        connection.execute(
            "UPDATE evidence_records SET scope_segments=' ' || scope_segments"
        )
    refuse(tmp_path, service, "accept", p.accept(proposal), uuid4())


def alter_checked(
    path: Path,
    table: str,
    column: str,
    value: object,
    identity_column: str,
    identity: UUID,
) -> None:
    """Corrupt only a disposable row, retaining all SQLite checks and FKs."""
    trigger = (
        f"{table}_no_update"
        if table.startswith("memory_")
        else f"trg_{table}_no_update"
    )
    with _open_write_connection(path, create=False) as connection:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE {identity_column}=?",
            (value, str(identity)),
        )
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA ignore_check_constraints").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("route", ["replay", "read", "list"])
@pytest.mark.parametrize(
    "operation,field",
    [("propose", "reason"), ("propose", "target_segments"), ("reject", "reason")],
)
def test_persisted_command_must_match_its_receipt(
    tmp_path: Path, route: str, operation: str, field: str
) -> None:
    p.seed(tmp_path)
    proposal, service, key = p.proposal(), p.service(tmp_path), uuid4()
    if operation == "reject":
        assert isinstance(p.invoke(service, "propose", proposal), Committed)
    command = (
        proposal
        if operation == "propose"
        else p.api().RejectProposal(proposal.scope, proposal.proposal_id, "Original")
    )
    assert isinstance(p.invoke(service, operation, command, key=key), Committed)
    value = "Canonical replacement reason"
    if field == "target_segments":
        value = json.dumps(
            [{"id": s.identifier, "kind": s.kind} for s in proposal.scope.segments],
            sort_keys=True,
            separators=(",", ":"),
        )
    alter_checked(
        tmp_path,
        "memory_proposals" if operation == "propose" else "memory_proposal_decisions",
        field,
        value,
        "proposal_id",
        proposal.proposal_id,
    )
    method = operation if route == "replay" else route
    if route != "replay":
        command = (
            p.api().ReadProposal(proposal.scope, proposal.proposal_id)
            if route == "read"
            else p.api().ListProposals(proposal.scope)
        )
    refuse(tmp_path, service, method, command, key)


@pytest.mark.parametrize("route", ["read", "list", "lookahead"])
@pytest.mark.parametrize(
    "field",
    [
        "promoted_fact_id",
        "evidence_id",
        "mutation_id",
        "derived_from",
        "promoted_by",
        "trust",
        "classification",
        "scope_segments",
    ],
)
def test_history_checks_actual_decision_and_publication(
    tmp_path: Path, route: str, field: str
) -> None:
    p.seed(tmp_path)
    service = p.service(tmp_path)
    proposal = replace(
        p.proposal(), proposal_id=UUID("eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    )
    created = p.invoke(service, "propose", proposal)
    accepted = p.invoke(service, "accept", p.accept(proposal))
    assert isinstance(created, Committed) and isinstance(accepted, Committed)
    if route == "lookahead":
        earlier = replace(
            p.proposal(), proposal_id=UUID("11111111-1111-4111-8111-111111111111")
        )
        assert isinstance(p.invoke(service, "propose", earlier), Committed)
    table, column, identity = (
        "memory_proposal_decisions",
        "proposal_id",
        proposal.proposal_id,
    )
    if field == "promoted_fact_id":
        value = str(h._SOURCE_FACT_ID)
    elif field == "evidence_id":
        other = uuid4()
        h._insert_evidence_row(tmp_path, other)
        value = str(other)
    elif field == "mutation_id":
        value = str(created.mutation_receipt.mutation_id)
    else:
        table, column, identity = "facts", "fact_id", accepted.value.promotions[0][1]
        if field == "derived_from":
            other = uuid4()
            h._insert_fact_row(tmp_path, other)
            value = str(other)
        elif field == "promoted_by":
            value = str(p.reviewer(tmp_path).principal_id)
        else:
            value = {
                "trust": "candidate",
                "classification": "public",
                "scope_segments": "[]",
            }[field]
    alter_checked(tmp_path, table, field, value, column, identity)
    command = (
        p.api().ReadProposal(proposal.scope, proposal.proposal_id)
        if route == "read"
        else p.api().ListProposals(
            proposal.scope, limit=1 if route == "lookahead" else 50
        )
    )
    refuse(tmp_path, service, "read" if route == "read" else "list", command, uuid4())


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize(
    "raw", ["[null]", "[" * 990 + "0" + "]" * 990], ids=["null", "deep"]
)
def test_locked_evidence_corruption_has_only_opaque_proposal_denial(
    tmp_path: Path, replay: bool, raw: str
) -> None:
    p.seed(tmp_path)
    proposal, service, key = p.proposal(), p.service(tmp_path), uuid4()
    assert isinstance(p.invoke(service, "propose", proposal), Committed)
    if replay:
        assert isinstance(
            p.invoke(service, "accept", p.accept(proposal), key=key), Committed
        )
    before: dict[str, Any] = {}

    def interfere() -> None:
        alter_checked(
            tmp_path,
            "evidence_records",
            "scope_segments",
            raw,
            "evidence_id",
            h._NAMED_EVIDENCE_ID,
        )
        before.update(inventory=inventory(tmp_path), audit=h._audit_rows(tmp_path))

    result = p.invoke(
        p.service(tmp_path, before_lock=interfere),
        "accept",
        p.accept(proposal),
        key=key,
    )
    assert (
        isinstance(result, Rejected)
        and result.failure.code.value == "authorisation_denied"
    )
    assert inventory(tmp_path) == before["inventory"]
    audits = h._audit_rows(tmp_path)
    assert len(audits) == len(before["audit"]) + 1
    assert [r for r in audits if r[3] == "deny"] == [
        (
            "instance",
            "data",
            "memory-proposal-accept",
            "deny",
            "proposal_authorisation_denied",
        )
    ]


@pytest.mark.parametrize(
    "field", ["reason", "target_scope", "target_classification", "fact_ids", "evidence"]
)
def test_acceptance_context_rejects_different_actual_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    from cairn.catalogue.audit import Classification

    p.seed(tmp_path)
    proposal, service = p.proposal(), p.service(tmp_path)
    assert isinstance(p.invoke(service, "propose", proposal), Committed)
    other_fact, other_evidence = uuid4(), uuid4()
    h._insert_fact_row(tmp_path, other_fact)
    h._insert_evidence_row(tmp_path, other_evidence)
    changes = {
        "reason": "Another command",
        "target_scope": proposal.scope,
        "target_classification": Classification.RESTRICTED,
        "fact_ids": (other_fact,),
        "evidence": other_evidence,
    }
    original = service._authority.promote

    def substitute(actor: Any, command: Any, **kwargs: Any) -> Any:
        return original(actor, replace(command, **{field: changes[field]}), **kwargs)

    monkeypatch.setattr(service._authority, "promote", substitute)
    refuse(tmp_path, service, "accept", p.accept(proposal), uuid4())


@pytest.mark.parametrize("field", ["actor", "key", "proposal"])
def test_acceptance_context_rejects_different_actual_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    p.seed(tmp_path)
    proposal, service = p.proposal(), p.service(tmp_path)
    assert isinstance(p.invoke(service, "propose", proposal), Committed)
    other = p.reviewer(tmp_path)
    original = service._authority.promote

    def substitute(actor: Any, command: Any, **kwargs: Any) -> Any:
        if field == "actor":
            actor = other
        elif field == "key":
            kwargs["idempotency_key"] = uuid4()
        else:
            kwargs["acceptance"] = replace(kwargs["acceptance"], proposal_id=uuid4())
        return original(actor, command, **kwargs)

    monkeypatch.setattr(service._authority, "promote", substitute)
    refuse(tmp_path, service, "accept", p.accept(proposal), uuid4())


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_creation_replay_after_decision_and_invalidation_preserves_history(
    tmp_path: Path, decision: str
) -> None:
    from cairn.catalogue.transactions import Replayed

    p.seed(tmp_path)
    proposal, service, key = p.proposal(), p.service(tmp_path), uuid4()
    created = p.invoke(service, "propose", proposal, key=key)
    assert isinstance(created, Committed)
    command = (
        p.accept(proposal)
        if decision == "accept"
        else p.api().RejectProposal(proposal.scope, proposal.proposal_id, "No")
    )
    assert isinstance(p.invoke(service, decision, command), Committed)
    h._invalidate_fact_row(tmp_path)
    before = inventory(tmp_path)
    replay = p.invoke(p.service(tmp_path), "propose", proposal, key=key)
    assert isinstance(replay, Replayed)
    assert (replay.value, replay.mutation_receipt) == (
        created.value,
        created.mutation_receipt,
    )
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("grant", [h._RETRIEVE_GRANT_ID, h._PROMOTE_GRANT_ID])
def test_optional_validation_never_replaces_normal_locked_authority(
    tmp_path: Path, grant: UUID
) -> None:
    import test_promotion_acceptance_guard as seam

    p.seed(tmp_path)
    context = replace(seam._context(uuid4()), validate_stored=lambda *args: None)
    before = inventory(tmp_path)
    result = seam._accept(
        seam._authority(
            tmp_path, before_lock=lambda: h._revoke_grant_row(tmp_path, grant)
        ),
        context,
    )
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "authorisation_denied"
    assert inventory(tmp_path) == before
    assert h._audit_rows(tmp_path) == [
        (
            "realm",
            "data",
            "promote",
            "deny",
            "source_retrieve_denied"
            if grant == h._RETRIEVE_GRANT_ID
            else "target_promote_denied",
        )
    ]


def test_legacy_default_retains_receipt_domain_and_replay(tmp_path: Path) -> None:
    import test_promotion_acceptance_guard as seam

    from cairn.catalogue.transactions import Replayed

    p.seed(tmp_path)
    first = seam._accept(seam._authority(tmp_path), None)
    assert isinstance(first, Committed)
    before = inventory(tmp_path)
    replay = seam._accept(seam._authority(tmp_path), None)
    assert isinstance(replay, Replayed)
    assert (replay.value, replay.mutation_receipt) == (
        first.value,
        first.mutation_receipt,
    )
    assert inventory(tmp_path) == before
    assert h._rows(
        tmp_path, "SELECT operation,result_schema FROM idempotency_records"
    ) == [("promote", "cairn.authority.promotion/v1")]
    assert h._audit_rows(tmp_path) == [
        ("realm", "data", "promote", "allow", "facts_promoted"),
        ("realm", "data", "promote", "allow", "idempotent_replay"),
    ]


def test_normal_authority_uses_fresh_time_after_optional_validation(
    tmp_path: Path,
) -> None:
    import test_promotion_acceptance_guard as seam

    from cairn.catalogue.sqlite import parse_timestamp

    p.seed(tmp_path)
    now = h._NOW

    def validation(*args: Any) -> None:
        nonlocal now
        now = parse_timestamp(h._FUTURE_TS)

    context = replace(seam._context(uuid4()), validate_stored=validation)
    before = inventory(tmp_path)
    result = seam._accept(seam._authority(tmp_path, clock=lambda: now), context)
    assert isinstance(result, Rejected)
    assert result.failure.code.value == "authorisation_denied"
    assert inventory(tmp_path) == before
