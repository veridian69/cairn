"""Host scope is enforced by Cairn even when its credential has broader rights."""

import threading
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import test_mutations as h

from cairn.authority.credentials import GrantOperation
from cairn.authority.mutations import CairnAuthority
from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.catalogue.transactions import Committed, FailureCode, Rejected, Replayed
from cairn.screening import SecretScreen


def seed(path: Path, *, readable: bool = True) -> None:
    h._seed_catalogue(path)
    h._seed_invalidator(path, segments=())
    h._insert_assertion_row(path)
    h._insert_fact_row(path)
    if readable:
        h._insert_grant(
            path,
            grant_id=h._RETRIEVE_GRANT_ID,
            segments=(),
            operations=frozenset({GrantOperation.RETRIEVE}),
        )


def correct(
    authority: CairnAuthority,
    *,
    scope: Scope | None = h._SOURCE_SCOPE,
    replacement: UUID | None = None,
) -> object:
    return authority.invalidate(
        h._agent_actor(),
        h._invalidate_command(superseded_by=replacement),
        idempotency_key=h._IDEMPOTENCY_KEY,
        correlation_id=h._CORRELATION_ID,
        expected_scope=scope,
    )


@pytest.mark.parametrize(
    "scope",
    [
        Scope(h._REALM, ()),
        Scope(h._REALM, (ScopeSegment("job", "other"),)),
        Scope(h._REALM, h._SOURCE_SCOPE.segments + (ScopeSegment("run", "nested"),)),
    ],
)
def test_broad_credential_cannot_correct_outside_exact_host_scope(
    tmp_path: Path, scope: Scope
) -> None:
    seed(tmp_path)
    result = correct(h._authority(tmp_path), scope=scope)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.AUTHORISATION_DENIED
    assert h._rows(tmp_path, "SELECT * FROM fact_invalidations") == []
    assert h._audit_rows(tmp_path)[-1][0] == "instance"


def test_scoped_correction_requires_retrieve_but_legacy_does_not(
    tmp_path: Path,
) -> None:
    seed(tmp_path, readable=False)
    authority = h._authority(tmp_path)
    assert isinstance(correct(authority), Rejected)
    assert isinstance(correct(authority, scope=None), Committed)


@pytest.mark.parametrize("outside", [False, True])
def test_replacement_requires_same_scope_and_readable_classification(
    tmp_path: Path, outside: bool
) -> None:
    seed(tmp_path, readable=False)
    h._insert_grant(
        tmp_path,
        grant_id=h._RETRIEVE_GRANT_ID,
        segments=(),
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=Classification.INTERNAL,
    )
    scope = (
        Scope(h._REALM, (ScopeSegment("job", "other"),)) if outside else h._SOURCE_SCOPE
    )
    level = Classification.INTERNAL if outside else Classification.RESTRICTED
    assertion, replacement = uuid4(), uuid4()
    h._insert_assertion_row(
        tmp_path, assertion_id=assertion, scope=scope, classification=level
    )
    h._insert_fact_row(
        tmp_path, replacement, assertion_id=assertion, scope=scope, classification=level
    )
    assert isinstance(
        correct(h._authority(tmp_path), replacement=replacement), Rejected
    )
    assert h._rows(tmp_path, "SELECT * FROM fact_invalidations") == []


def test_scoped_success_and_replay_return_real_original_custody(tmp_path: Path) -> None:
    seed(tmp_path)
    authority = h._authority(tmp_path)
    first = correct(authority)
    replay = correct(authority)
    assert isinstance(first, Committed) and isinstance(replay, Replayed)
    assert first.value == replay.value
    assert first.mutation_receipt == replay.mutation_receipt
    assert len(h._rows(tmp_path, "SELECT * FROM fact_invalidations")) == 1
    assert isinstance(correct(authority, scope=Scope(h._REALM, ())), Rejected)


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize(
    "operation", [GrantOperation.RETRIEVE, GrantOperation.INVALIDATE]
)
def test_scoped_current_grants_are_checked_inside_writer_before_replay(
    tmp_path: Path, replay: bool, operation: GrantOperation
) -> None:
    seed(tmp_path)
    if replay:
        assert isinstance(correct(h._authority(tmp_path)), Committed)
    grant = (
        h._RETRIEVE_GRANT_ID
        if operation is GrantOperation.RETRIEVE
        else h._INVALIDATE_GRANT_ID
    )
    transactions = h._GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: h._NOW,
        uuid_factory=uuid4,
        interfere=lambda path: h._revoke_grant_row(path, grant),
    )
    result = correct(h._authority(tmp_path, transactions=transactions))
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.AUTHORISATION_DENIED
    assert len(h._rows(tmp_path, "SELECT * FROM fact_invalidations")) == int(replay)


@pytest.mark.parametrize("replay", [False, True])
def test_scoped_retrieve_expiry_uses_fresh_writer_time(
    tmp_path: Path, replay: bool
) -> None:
    seed(tmp_path, readable=False)
    expires = h._NOW + timedelta(seconds=1)
    h._insert_grant(
        tmp_path,
        grant_id=h._RETRIEVE_GRANT_ID,
        segments=(),
        operations=frozenset({GrantOperation.RETRIEVE}),
        expires_at=expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    if replay:
        assert isinstance(correct(h._authority(tmp_path)), Committed)
    now = [h._NOW]

    def advance(path: Path) -> None:
        now[0] = expires + timedelta(seconds=1)

    transactions = h._GrantRaceTransactions(
        tmp_path,
        writer_gate=threading.Lock(),
        clock=lambda: h._NOW,
        uuid_factory=uuid4,
        interfere=advance,
    )
    authority = CairnAuthority(
        tmp_path, transactions, lambda: now[0], uuid4, True, SecretScreen()
    )
    result = correct(authority)
    assert isinstance(result, Rejected)
    assert result.failure.code is FailureCode.AUTHORISATION_DENIED
    assert len(h._rows(tmp_path, "SELECT * FROM fact_invalidations")) == int(replay)


def test_all_current_covering_read_grants_contribute_clearance(tmp_path: Path) -> None:
    seed(tmp_path)
    h._insert_grant(
        tmp_path,
        grant_id=UUID("00000000-0000-4000-8000-000000000001"),
        segments=h._SOURCE_SCOPE.segments,
        operations=frozenset({GrantOperation.RETRIEVE}),
        read_clearance=Classification.PUBLIC,
    )
    assert isinstance(correct(h._authority(tmp_path)), Committed)


def test_locked_scoped_and_normal_checks_share_one_clock_instant(
    tmp_path: Path,
) -> None:
    seed(tmp_path, readable=False)
    expiry = h._NOW + timedelta(seconds=10)
    h._insert_grant(
        tmp_path,
        grant_id=h._RETRIEVE_GRANT_ID,
        segments=(),
        operations=frozenset({GrantOperation.RETRIEVE}),
        expires_at=expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    instants = iter(
        [h._NOW, expiry - timedelta(seconds=1), expiry + timedelta(seconds=1)]
    )
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return next(instants)

    authority = CairnAuthority(
        tmp_path, h._transactions(tmp_path), clock, uuid4, True, SecretScreen()
    )
    assert isinstance(correct(authority), Committed)
    assert calls == 2


def test_future_recorded_fact_is_not_readable_for_scoped_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path)
    identity = uuid4()
    with monkeypatch.context() as changes:
        changes.setattr(
            h, "_TS", (h._NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        )
        h._insert_fact_row(tmp_path, identity)
    result = h._authority(tmp_path).invalidate(
        h._agent_actor(),
        h._invalidate_command(fact_ids=(identity,)),
        idempotency_key=uuid4(),
        correlation_id=uuid4(),
        expected_scope=h._SOURCE_SCOPE,
    )
    assert isinstance(result, Rejected)
    assert h._rows(tmp_path, "SELECT * FROM fact_invalidations") == []
