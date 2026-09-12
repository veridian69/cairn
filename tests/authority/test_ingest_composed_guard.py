"""Real custody transactions for the internal preparation/commit seams."""

import json
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import test_retrieval as fixture

from cairn.authority import mutations
from cairn.authority.credentials import GrantOperation
from cairn.authority.custody import CustodyValueError, FactDraft, SourceType
from cairn.authority.gate import instance_denial_draft, instance_id
from cairn.authority.grants import find_authorising_grant
from cairn.catalogue.audit import ActionKind, Classification
from cairn.catalogue.sqlite import _open_write_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    Committed,
    FailureCode,
    MutationRejection,
    Rejected,
    Replayed,
    RetryClass,
    StableFailure,
    _GuardedTransaction,
)
from cairn.screening import SecretScreen


def _seed(path: Path) -> None:
    fixture._seed_catalogue(path)
    fixture._insert_principal(path, fixture._AGENT_ID, label="synthetic-agent")
    fixture._insert_credential(path, fixture._AGENT_CREDENTIAL_ID, fixture._AGENT_ID)
    fixture._insert_grant(path, operations=frozenset({GrantOperation.INGEST}))
    fixture._insert_grant(
        path,
        grant_id=fixture._SECOND_GRANT_ID,
        operations=frozenset({GrantOperation.RETRIEVE}),
        expires_at="2026-08-05T10:12:12.123456Z",
    )


def _command() -> mutations.IngestAssertion:
    return mutations.IngestAssertion(
        fixture._SCOPE,
        Classification.INTERNAL,
        SourceType.AGENT_CLAIM,
        (FactDraft("synthetic durable observation", None, None),),
    )


def _inventory(path: Path) -> tuple[int, ...]:
    return tuple(
        len(fixture._rows(path, f"SELECT * FROM {table}"))
        for table in (
            "assertions",
            "facts",
            "evidence_records",
            "evidence_outbox",
            "projection_outbox",
            "idempotency_records",
        )
    )


def _revoke(path: Path, grant: UUID) -> None:
    with _open_write_connection(path, create=False) as connection:
        connection.execute(
            "INSERT INTO grant_revocations VALUES (?, ?, ?, ?)",
            (str(grant), fixture._TS, str(fixture._AGENT_ID), "synthetic_revocation"),
        )
        connection.commit()


def _retrieve_guard(
    now: Callable[[], datetime],
) -> Callable[[_GuardedTransaction], None]:
    def guard(transaction: _GuardedTransaction) -> None:
        grants = mutations._sorted_grants(
            transaction.query, fixture._AGENT_ID, fixture._SCOPE.realm
        )
        grant = find_authorising_grant(
            grants,
            realm_id=fixture._SCOPE.realm,
            segments=fixture._SCOPE.segments,
            operation=GrantOperation.RETRIEVE,
            at=now(),
        )
        if grant is None:
            raise MutationRejection(
                StableFailure(
                    FailureCode.AUTHORISATION_DENIED,
                    "The requested operation is not authorised.",
                    fixture._CORRELATION_ID,
                    RetryClass.NEVER,
                ),
                instance_denial_draft(
                    instance_id(transaction.query),
                    fixture._agent_actor(),
                    "ingest",
                    "session_authorisation_denied",
                    fixture._CORRELATION_ID,
                    action_kind=ActionKind.DATA,
                ),
            )

    return guard


def _authority(
    path: Path,
    clock: Callable[[], datetime],
    before_guard: Callable[[], None] = lambda: None,
) -> mutations.CairnAuthority:
    class BoundaryTransactions(CatalogueTransactions):
        # This is immediately before the real writer gate, after ingest's outer
        # authorisation. All transaction handling and custody remain production code.
        def mutate_idempotent(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            before_guard()
            return super().mutate_idempotent(*args, **kwargs)

    transactions = BoundaryTransactions(
        path,
        writer_gate=threading.Lock(),
        clock=clock,
        uuid_factory=uuid4,
    )
    return mutations.CairnAuthority(
        path, transactions, clock, uuid4, True, SecretScreen()
    )


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("change", ["revoke", "expire"])
def test_retrieve_loss_before_writer_guard_denies_without_custody(
    tmp_path: Path,
    replay: bool,
    change: str,
) -> None:
    _seed(tmp_path)
    now = [fixture._NOW]

    def clock() -> datetime:
        return now[0]

    key = uuid4()
    if replay:
        first = _authority(tmp_path, clock).ingest(
            fixture._agent_actor(),
            _command(),
            idempotency_key=key,
            correlation_id=fixture._CORRELATION_ID,
            commit_guard=_retrieve_guard(clock),
        )
        assert isinstance(first, Committed)
    before = _inventory(tmp_path)

    def change_authority() -> None:
        if change == "revoke":
            _revoke(tmp_path, fixture._SECOND_GRANT_ID)
        else:
            now[0] += timedelta(minutes=2)

    outcome = _authority(tmp_path, clock, change_authority).ingest(
        fixture._agent_actor(),
        _command(),
        idempotency_key=key,
        correlation_id=fixture._CORRELATION_ID,
        commit_guard=_retrieve_guard(clock),
    )
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code == FailureCode.AUTHORISATION_DENIED
    assert _inventory(tmp_path) == before
    assert fixture._rows(
        tmp_path, "SELECT reason_code FROM audit_events WHERE outcome = 'deny'"
    ) == [
        ("session_authorisation_denied",),
    ]


def test_valid_guard_allows_fresh_and_replay_with_real_fact_identity(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    service = _authority(tmp_path, lambda: fixture._NOW)
    key = uuid4()
    outcomes = [
        service.ingest(
            fixture._agent_actor(),
            _command(),
            idempotency_key=key,
            correlation_id=fixture._CORRELATION_ID,
            commit_guard=_retrieve_guard(lambda: fixture._NOW),
        )
        for _ in range(2)
    ]
    first, replay = outcomes
    assert isinstance(first, Committed)
    assert isinstance(replay, Replayed)
    assert first.value == replay.value
    assert fixture._rows(tmp_path, "SELECT fact_id FROM facts") == [
        (str(first.value.fact_ids[0]),)
    ]


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("change", ["revoke", "expire"])
def test_hook_cannot_replace_fresh_normal_ingest_authorisation(
    tmp_path: Path,
    replay: bool,
    change: str,
) -> None:
    _seed(tmp_path)
    now = [fixture._NOW]
    key = uuid4()
    if replay:
        assert isinstance(
            _authority(tmp_path, lambda: now[0]).ingest(
                fixture._agent_actor(),
                _command(),
                idempotency_key=key,
                correlation_id=fixture._CORRELATION_ID,
            ),
            Committed,
        )
    before = _inventory(tmp_path)

    def change_authority() -> None:
        if change == "revoke":
            _revoke(tmp_path, fixture._DATA_GRANT_ID)
        else:
            now[0] += timedelta(days=365)

    def must_not_run(transaction: _GuardedTransaction) -> None:
        pytest.fail("normal ingest authority must precede the extra guard")

    outcome = _authority(tmp_path, lambda: now[0], change_authority).ingest(
        fixture._agent_actor(),
        _command(),
        idempotency_key=key,
        correlation_id=fixture._CORRELATION_ID,
        commit_guard=must_not_run,
    )
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code == FailureCode.AUTHORISATION_DENIED
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "batch",
        "observed",
        "metadata",
        "payload",
        "oversize_payload",
        "body",
        "validity",
        "source",
        "trust",
    ],
)
def test_nonwriting_validation_matches_actual_ingest_invalid_payloads(
    tmp_path: Path,
    case: str,
) -> None:
    _seed(tmp_path)
    command = _command()
    if case == "empty":
        command = replace(command, facts=())
    elif case == "batch":
        command = replace(command, facts=command.facts * 101)
    elif case == "observed":
        command = replace(command, observed_at=datetime(2026, 1, 1))
    elif case == "metadata":
        command = replace(command, metadata="{broken")
    elif case == "payload":
        command = replace(command, evidence_payload=b"")
    elif case == "oversize_payload":
        command = replace(command, evidence_payload=b"x" * 1048577)
    elif case == "body":
        # Model a malformed internal domain value, bypassing its constructor,
        # to prove the full record validation is actually reused here.
        object.__setattr__(command.facts[0], "body", "x" * 65537)
    elif case == "validity":
        object.__setattr__(command.facts[0], "valid_from", fixture._NOW)
        object.__setattr__(command.facts[0], "valid_to", fixture._NOW)
    elif case == "source":
        object.__setattr__(command, "source_type", "invalid")
    else:
        object.__setattr__(command, "requested_trust", "invalid")
    before = _inventory(tmp_path)
    audit_before = fixture._rows(tmp_path, "SELECT event_id FROM audit_events")
    with pytest.raises(CustodyValueError) as failure:
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
        )
    assert _inventory(tmp_path) == before
    assert fixture._rows(tmp_path, "SELECT event_id FROM audit_events") == audit_before
    outcome = _authority(tmp_path, lambda: fixture._NOW).ingest(
        fixture._agent_actor(),
        command,
        idempotency_key=uuid4(),
        correlation_id=fixture._CORRELATION_ID,
    )
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code == FailureCode.INVALID_REQUEST
    assert fixture._rows(
        tmp_path, "SELECT reason_code FROM audit_events WHERE outcome = 'deny'"
    ) == [(failure.value.code,)]
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("field", ["body", "metadata", "evidence"])
def test_nonwriting_screen_matches_custody_denial(tmp_path: Path, field: str) -> None:
    _seed(tmp_path)
    secret = "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----"
    command = _command()
    if field == "body":
        command = replace(command, facts=(FactDraft(secret, None, None),))
    elif field == "metadata":
        command = replace(
            command, metadata=json.dumps({"note": secret}, separators=(",", ":"))
        )
    else:
        command = replace(command, evidence_payload=secret.encode())
    before = _inventory(tmp_path)
    finding = mutations.validate_ingest_payload(
        fixture._agent_actor(),
        command,
        effective_at=fixture._NOW,
        exact_evidence_enabled=True,
        screen=SecretScreen(),
    )
    assert finding is not None
    assert secret not in repr(finding)
    assert _inventory(tmp_path) == before
    outcome = _authority(tmp_path, lambda: fixture._NOW).ingest(
        fixture._agent_actor(),
        command,
        idempotency_key=uuid4(),
        correlation_id=fixture._CORRELATION_ID,
    )
    assert isinstance(outcome, Rejected)
    assert outcome.failure.code == FailureCode.SECRET_REJECTED
    assert outcome.failure.detail is not None
    assert outcome.failure.detail.rule == finding.rule
    assert outcome.failure.detail.field_path == finding.field_path
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("times", [(), (None,), (fixture._NOW, None)])
def test_nonwriting_validation_checks_common_time_and_accepts_valid_payload(
    tmp_path: Path,
    times: tuple[datetime | None, ...],
) -> None:
    _seed(tmp_path)
    command = replace(_command(), observed_at=fixture._NOW)
    before = _inventory(tmp_path)
    assert (
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(fixture._NOW,),
        )
        is None
    )
    with pytest.raises(CustodyValueError, match="invalid_validity"):
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=times,
        )
    assert _inventory(tmp_path) == before


def test_common_observation_time_rejects_distinct_zurich_folds(tmp_path: Path) -> None:
    _seed(tmp_path)
    zone = ZoneInfo("Europe/Zurich")
    first = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    assert first == second  # Python's wall-clock equality hides this distinction.
    assert first.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert second.astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    command = replace(_command(), facts=_command().facts * 2, observed_at=first)
    before = _inventory(tmp_path)
    with pytest.raises(CustodyValueError, match="invalid_validity"):
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(first, second),
        )
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("fold, utc_hour", [(0, 0), (1, 1)])
def test_common_observation_time_accepts_equivalent_instants_across_zones(
    tmp_path: Path,
    fold: int,
    utc_hour: int,
) -> None:
    _seed(tmp_path)
    local = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Zurich"), fold=fold)
    utc = datetime(2026, 10, 25, utc_hour, 30, tzinfo=UTC)
    west = datetime(
        2026, 10, 24, 20 + utc_hour, 30, tzinfo=timezone(timedelta(hours=-4))
    )
    command = replace(_command(), facts=_command().facts * 3, observed_at=local)
    before = _inventory(tmp_path)
    assert (
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(local, utc, west),
        )
        is None
    )
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize(
    "value",
    [
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-1))),
    ],
)
@pytest.mark.parametrize("matching_assertion", [True, False])
def test_common_observation_time_rejects_utc_overflow(
    value: datetime,
    matching_assertion: bool,
) -> None:
    command = replace(
        _command(),
        observed_at=value if matching_assertion else fixture._NOW,
    )
    with pytest.raises(CustodyValueError, match="invalid_validity") as error:
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(value,),
        )
    assert error.value.code == "invalid_validity"


@pytest.mark.parametrize(
    "local, utc",
    [
        (
            datetime(1, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
            datetime(1, 1, 1, tzinfo=UTC),
        ),
        (
            datetime(
                9999, 12, 31, 22, 59, 59, 999999, tzinfo=timezone(timedelta(hours=-1))
            ),
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        ),
    ],
)
def test_common_observation_time_accepts_representable_utc_boundaries(
    local: datetime,
    utc: datetime,
) -> None:
    command = replace(_command(), facts=_command().facts * 2, observed_at=local)
    assert (
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(local, utc),
        )
        is None
    )


class _EqualTimestamp:
    def __eq__(self, other: object) -> bool:
        return True


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 10, 25, 2, 30),
        datetime(2026, 10, 25, 2, 30, tzinfo=_NoOffset()),
        "2026-10-25T00:30:00Z",
        42,
        False,
        _EqualTimestamp(),
    ],
)
def test_common_observation_time_rejects_naive_and_invalid_values(
    value: object,
) -> None:
    command = replace(_command(), observed_at=fixture._NOW)
    with pytest.raises(CustodyValueError, match="invalid_validity"):
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            command,
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(cast(datetime, value),),
        )


def test_common_observation_time_preserves_explicit_none() -> None:
    assert (
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            _command(),
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(None,),
        )
        is None
    )
    with pytest.raises(CustodyValueError, match="invalid_validity"):
        mutations.validate_ingest_payload(
            fixture._agent_actor(),
            _command(),
            effective_at=fixture._NOW,
            exact_evidence_enabled=True,
            screen=SecretScreen(),
            observation_times=(fixture._NOW,),
        )


def test_guard_denial_rolls_back_work_in_the_real_writer_transaction(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _revoke(tmp_path, fixture._SECOND_GRANT_ID)
    before = _inventory(tmp_path)

    def guard(transaction: _GuardedTransaction) -> None:
        transaction.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid4()), "workload", "provisional", fixture._TS),
        )
        _retrieve_guard(lambda: fixture._NOW)(transaction)

    outcome = _authority(tmp_path, lambda: fixture._NOW).ingest(
        fixture._agent_actor(),
        _command(),
        idempotency_key=uuid4(),
        correlation_id=fixture._CORRELATION_ID,
        commit_guard=guard,
    )
    assert isinstance(outcome, Rejected)
    assert _inventory(tmp_path) == before
    assert fixture._rows(tmp_path, "SELECT label FROM principals") == [
        ("synthetic-agent",)
    ]
