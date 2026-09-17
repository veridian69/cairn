from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn.authority.credentials import CLEARANCE_ORDER, GrantOperation, PrincipalKind
from cairn.authority.grants import (
    GrantRecord,
    ProposedGrant,
    delegation_violation,
    find_authorising_grant,
    is_live,
    is_scope_prefix,
)
from cairn.catalogue.audit import Classification, ScopeSegment

_NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
_LATER = datetime(2026, 8, 6, 10, 11, 12, 123456, tzinfo=UTC)

_MANAGER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_WORKLOAD_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

_GRANT_A = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

_REPO = ScopeSegment(kind="repository", identifier="acme-repo")
_CHANGE = ScopeSegment(kind="change-set", identifier="cs-1")
_SIBLING_REPO = ScopeSegment(kind="repository", identifier="other-repo")


def _manager_grant(
    *,
    grant_id: UUID = _GRANT_A,
    principal_id: UUID = _MANAGER_ID,
    realm_id: str = "acme",
    segments: tuple[ScopeSegment, ...] = (_REPO,),
    operations: frozenset[GrantOperation] = frozenset({GrantOperation.GRANT_MANAGE}),
    read_clearance: Classification = Classification.RESTRICTED,
    write_classifications: frozenset[Classification] = frozenset(
        {Classification.PUBLIC, Classification.INTERNAL}
    ),
    delegable_operations: frozenset[GrantOperation] | None = frozenset(
        {GrantOperation.RETRIEVE, GrantOperation.INGEST}
    ),
    issued_by: UUID | None = None,
    expires_at: datetime | None = _LATER,
    created_at: datetime = _NOW,
    revoked: bool = False,
) -> GrantRecord:
    return GrantRecord(
        grant_id=grant_id,
        principal_id=principal_id,
        realm_id=realm_id,
        segments=segments,
        operations=operations,
        read_clearance=read_clearance,
        write_classifications=write_classifications,
        delegable_operations=delegable_operations,
        issued_by=issued_by,
        expires_at=expires_at,
        created_at=created_at,
        revoked=revoked,
    )


def _compliant_proposal(
    *,
    principal_id: UUID = _WORKLOAD_ID,
    realm_id: str = "acme",
    segments: tuple[ScopeSegment, ...] = (_REPO, _CHANGE),
    operations: frozenset[GrantOperation] = frozenset({GrantOperation.RETRIEVE}),
    read_clearance: Classification = Classification.INTERNAL,
    write_classifications: frozenset[Classification] = frozenset(
        {Classification.PUBLIC}
    ),
    delegable_operations: frozenset[GrantOperation] | None = None,
    expires_at: datetime | None = _NOW,
) -> ProposedGrant:
    return ProposedGrant(
        principal_id=principal_id,
        realm_id=realm_id,
        segments=segments,
        operations=operations,
        read_clearance=read_clearance,
        write_classifications=write_classifications,
        delegable_operations=delegable_operations,
        expires_at=expires_at,
    )


# --- is_live --------------------------------------------------------------


def test_is_live_true_for_unexpired_unrevoked_grant() -> None:
    grant = _manager_grant(expires_at=_LATER, revoked=False)
    assert is_live(grant, _NOW) is True


def test_is_live_false_when_expired() -> None:
    grant = _manager_grant(expires_at=_NOW, revoked=False)
    assert is_live(grant, _NOW) is False


def test_is_live_false_when_revoked() -> None:
    grant = _manager_grant(expires_at=_LATER, revoked=True)
    assert is_live(grant, _NOW) is False


def test_is_live_true_when_no_expiry() -> None:
    grant = _manager_grant(expires_at=None, revoked=False)
    assert is_live(grant, _LATER) is True


# --- is_scope_prefix --------------------------------------------------------


def test_root_prefix_covers_everything() -> None:
    assert is_scope_prefix((), ()) is True
    assert is_scope_prefix((), (_REPO,)) is True
    assert is_scope_prefix((), (_REPO, _CHANGE)) is True


def test_prefix_covers_itself() -> None:
    assert is_scope_prefix((_REPO,), (_REPO,)) is True


def test_prefix_covers_descendant() -> None:
    assert is_scope_prefix((_REPO,), (_REPO, _CHANGE)) is True


def test_prefix_never_covers_sibling() -> None:
    assert is_scope_prefix((_REPO,), (_SIBLING_REPO,)) is False


def test_prefix_never_covers_ancestor() -> None:
    assert is_scope_prefix((_REPO, _CHANGE), (_REPO,)) is False


def test_prefix_never_covers_same_length_different_id() -> None:
    assert is_scope_prefix((_REPO, _CHANGE), (_REPO, _SIBLING_REPO)) is False


# --- find_authorising_grant --------------------------------------------------


def test_find_authorising_grant_matches_ancestor_scope_grant() -> None:
    grant = _manager_grant(
        segments=(_REPO,), operations=frozenset({GrantOperation.RETRIEVE})
    )
    found = find_authorising_grant(
        (grant,),
        realm_id="acme",
        segments=(_REPO, _CHANGE),
        operation=GrantOperation.RETRIEVE,
        at=_NOW,
    )
    assert found == grant


def test_find_authorising_grant_none_for_wrong_realm() -> None:
    grant = _manager_grant(realm_id="acme", segments=())
    found = find_authorising_grant(
        (grant,),
        realm_id="other-realm",
        segments=(),
        operation=GrantOperation.GRANT_MANAGE,
        at=_NOW,
    )
    assert found is None


def test_find_authorising_grant_none_for_missing_operation() -> None:
    grant = _manager_grant(operations=frozenset({GrantOperation.RETRIEVE}))
    found = find_authorising_grant(
        (grant,),
        realm_id="acme",
        segments=(_REPO,),
        operation=GrantOperation.INGEST,
        at=_NOW,
    )
    assert found is None


def test_find_authorising_grant_none_when_grant_scope_is_narrower() -> None:
    grant = _manager_grant(segments=(_REPO, _CHANGE))
    found = find_authorising_grant(
        (grant,),
        realm_id="acme",
        segments=(_REPO,),
        operation=GrantOperation.GRANT_MANAGE,
        at=_NOW,
    )
    assert found is None


def test_find_authorising_grant_none_when_expired() -> None:
    grant = _manager_grant(expires_at=_NOW)
    found = find_authorising_grant(
        (grant,),
        realm_id="acme",
        segments=(_REPO,),
        operation=GrantOperation.GRANT_MANAGE,
        at=_NOW,
    )
    assert found is None


def test_find_authorising_grant_none_when_revoked() -> None:
    grant = _manager_grant(revoked=True)
    found = find_authorising_grant(
        (grant,),
        realm_id="acme",
        segments=(_REPO,),
        operation=GrantOperation.GRANT_MANAGE,
        at=_NOW,
    )
    assert found is None


# --- delegation_violation ----------------------------------------------------


def test_grant_01_within_envelope_is_accepted() -> None:
    manager = _manager_grant()
    proposal = _compliant_proposal()
    assert delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW) is None


def test_grant_02_scope_widening_is_rejected() -> None:
    manager = _manager_grant(segments=(_REPO, _CHANGE))
    proposal = _compliant_proposal(segments=(_SIBLING_REPO,))
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "scope_outside_envelope"
    )


def test_proposal_at_exactly_the_managers_prefix_is_accepted() -> None:
    manager = _manager_grant(segments=(_REPO, _CHANGE))
    proposal = _compliant_proposal(segments=(_REPO, _CHANGE))
    assert delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW) is None


def test_realm_widening_is_rejected() -> None:
    manager = _manager_grant(realm_id="acme")
    proposal = _compliant_proposal(realm_id="other-realm")
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "realm_outside_envelope"
    )


def test_grant_03_operation_widening_is_rejected() -> None:
    manager = _manager_grant(delegable_operations=frozenset({GrantOperation.RETRIEVE}))
    proposal = _compliant_proposal(
        operations=frozenset({GrantOperation.RETRIEVE, GrantOperation.INGEST})
    )
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "operation_outside_envelope"
    )


def test_manager_with_no_delegable_operations_delegates_nothing() -> None:
    manager = _manager_grant(delegable_operations=None)
    proposal = _compliant_proposal(operations=frozenset({GrantOperation.RETRIEVE}))
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "operation_outside_envelope"
    )


def test_manager_with_empty_delegable_operations_delegates_nothing() -> None:
    # frozenset() (not None) is the only DB-representable "delegates nothing"
    # shape for a real grant-manage grant: the schema requires
    # delegable_operations to be non-null whenever operations contains
    # grant-manage.
    manager = _manager_grant(delegable_operations=frozenset())
    proposal = _compliant_proposal(operations=frozenset({GrantOperation.RETRIEVE}))
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "operation_outside_envelope"
    )


def test_grant_04_clearance_widening_is_rejected() -> None:
    manager = _manager_grant(read_clearance=Classification.INTERNAL)
    proposal = _compliant_proposal(read_clearance=Classification.RESTRICTED)
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "clearance_exceeds_envelope"
    )


def test_grant_05_write_classification_widening_is_rejected() -> None:
    manager = _manager_grant(write_classifications=frozenset({Classification.PUBLIC}))
    proposal = _compliant_proposal(
        write_classifications=frozenset({Classification.INTERNAL})
    )
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "classification_outside_envelope"
    )


def test_grant_06_later_expiry_is_rejected() -> None:
    manager = _manager_grant(expires_at=_NOW + timedelta(hours=1))
    proposal = _compliant_proposal(expires_at=_LATER)
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "expiry_exceeds_envelope"
    )


def test_manager_without_expiry_imposes_no_ceiling() -> None:
    manager = _manager_grant(expires_at=None)
    proposal = _compliant_proposal(expires_at=_LATER)
    assert delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW) is None


def test_proposal_expiry_exactly_equal_to_managers_is_accepted() -> None:
    # Guards the > vs >= choice at the expiry-ceiling check: equal is within
    # bounds, not a widening.
    manager = _manager_grant(expires_at=_NOW + timedelta(hours=1))
    proposal = _compliant_proposal(expires_at=_NOW + timedelta(hours=1))
    assert delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW) is None


def test_grant_07_grant_manage_delegation_is_rejected() -> None:
    manager = _manager_grant(delegable_operations=frozenset({GrantOperation.RETRIEVE}))
    proposal = _compliant_proposal(operations=frozenset({GrantOperation.GRANT_MANAGE}))
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "grant_manage_not_delegable"
    )


def test_proposed_delegable_operations_is_rejected() -> None:
    # GRANT-07 guarantees an accepted proposal never contains grant-manage,
    # and the schema requires delegable_operations to be null exactly when
    # operations lacks grant-manage — so a non-null delegable_operations
    # here would otherwise die at INSERT with a raw IntegrityError instead
    # of a clean domain rejection.
    manager = _manager_grant()
    proposal = _compliant_proposal(
        delegable_operations=frozenset({GrantOperation.RETRIEVE})
    )
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "delegable_operations_not_permitted"
    )


def test_workload_principal_without_expiry_is_rejected() -> None:
    manager = _manager_grant(expires_at=None)
    proposal = _compliant_proposal(expires_at=None)
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "workload_grant_requires_expiry"
    )


def test_human_principal_without_expiry_is_accepted() -> None:
    manager = _manager_grant(expires_at=None)
    proposal = _compliant_proposal(expires_at=None)
    assert delegation_violation(manager, proposal, PrincipalKind.HUMAN, _NOW) is None


def test_expired_manager_grant_is_not_live_for_delegation() -> None:
    manager = _manager_grant(expires_at=_NOW)
    proposal = _compliant_proposal()
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "manager_grant_not_live"
    )


def test_revoked_manager_grant_is_not_live_for_delegation() -> None:
    manager = _manager_grant(revoked=True)
    proposal = _compliant_proposal()
    assert (
        delegation_violation(manager, proposal, PrincipalKind.WORKLOAD, _NOW)
        == "manager_grant_not_live"
    )


# --- Hypothesis invariants (I-23) --------------------------------------------


_kinds = st.sampled_from(["repository", "change-set", "job"])
_ids = st.sampled_from(["a", "b", "c", "abc-1", "xyz-2"])
_segment_strategy = st.builds(ScopeSegment, kind=_kinds, identifier=_ids)
_segments_strategy = st.lists(_segment_strategy, max_size=4).map(tuple)


@settings(max_examples=200, deadline=None)
@given(prefix=_segments_strategy, candidate=_segments_strategy)
def test_is_scope_prefix_reflexive(
    prefix: tuple[ScopeSegment, ...], candidate: tuple[ScopeSegment, ...]
) -> None:
    assert is_scope_prefix(prefix, prefix) is True
    assert is_scope_prefix(candidate, candidate) is True


@settings(max_examples=200, deadline=None)
@given(left=_segments_strategy, right=_segments_strategy)
def test_is_scope_prefix_antisymmetric_on_unequal_lengths(
    left: tuple[ScopeSegment, ...], right: tuple[ScopeSegment, ...]
) -> None:
    if len(left) != len(right) and is_scope_prefix(left, right):
        assert is_scope_prefix(right, left) is False


@settings(max_examples=200, deadline=None)
@given(a=_segments_strategy, b=_segments_strategy, c=_segments_strategy)
def test_is_scope_prefix_transitive(
    a: tuple[ScopeSegment, ...],
    b: tuple[ScopeSegment, ...],
    c: tuple[ScopeSegment, ...],
) -> None:
    if is_scope_prefix(a, b) and is_scope_prefix(b, c):
        assert is_scope_prefix(a, c) is True


_realm_strategy = st.sampled_from(["acme", "other-realm"])
_operation_strategy = st.sampled_from(list(GrantOperation))
_operations_set_strategy = st.frozensets(_operation_strategy, max_size=3)
_classification_strategy = st.sampled_from(list(Classification))
_classifications_set_strategy = st.frozensets(_classification_strategy, max_size=3)
_expiry_strategy = st.one_of(
    st.none(),
    st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 1, 1),
    ).map(lambda value: value.replace(tzinfo=UTC)),
)
_principal_kind_strategy = st.sampled_from(list(PrincipalKind))
# Bias the general pairs towards the envelope, while retaining invalid inputs.
# Nonvacuity comes from a separate constructive draw in each property example.
_mostly_true = st.sampled_from([True, True, True, True, False])


@st.composite
def _manager_and_proposal(draw: st.DrawFn) -> tuple[GrantRecord, ProposedGrant]:
    delegable = draw(_operations_set_strategy)
    manager_realm = draw(_realm_strategy)
    manager_segments = draw(_segments_strategy)
    manager_clearance = draw(_classification_strategy)
    manager_write = draw(_classifications_set_strategy)
    manager_expiry = draw(_expiry_strategy)
    manager = _manager_grant(
        realm_id=manager_realm,
        segments=manager_segments,
        operations=frozenset(delegable | {GrantOperation.GRANT_MANAGE}),
        read_clearance=manager_clearance,
        write_classifications=manager_write,
        delegable_operations=delegable,
        expires_at=manager_expiry,
    )

    use_manager_realm = draw(_mostly_true)
    realm = manager_realm if use_manager_realm else draw(_realm_strategy)

    use_manager_segments = draw(_mostly_true)
    if use_manager_segments:
        segments = manager_segments + draw(_segments_strategy)
    else:
        segments = draw(_segments_strategy)

    use_delegable_operations = bool(delegable) and draw(_mostly_true)
    operations: frozenset[GrantOperation]
    if use_delegable_operations:
        operations = frozenset(draw(st.sets(st.sampled_from(sorted(delegable)))))
    else:
        operations = draw(_operations_set_strategy)

    use_manager_clearance = draw(_mostly_true)
    if use_manager_clearance:
        permitted = [
            value
            for value in Classification
            if CLEARANCE_ORDER[value] <= CLEARANCE_ORDER[manager_clearance]
        ]
        clearance = draw(st.sampled_from(permitted))
    else:
        clearance = draw(_classification_strategy)

    use_manager_write = bool(manager_write) and draw(_mostly_true)
    write_classifications: frozenset[Classification]
    if use_manager_write:
        write_classifications = frozenset(
            draw(st.sets(st.sampled_from(sorted(manager_write))))
        )
    else:
        write_classifications = draw(_classifications_set_strategy)

    use_manager_expiry = manager_expiry is not None and draw(_mostly_true)
    expires_at: datetime | None
    if use_manager_expiry and manager_expiry is not None:
        naive_expiry = draw(
            st.datetimes(
                min_value=datetime(2020, 1, 1),
                max_value=manager_expiry.replace(tzinfo=None),
            )
        )
        expires_at = naive_expiry.replace(tzinfo=UTC)
    else:
        expires_at = draw(_expiry_strategy)

    omit_delegable_operations = draw(_mostly_true)
    delegable_operations: frozenset[GrantOperation] | None
    if omit_delegable_operations:
        delegable_operations = None
    else:
        delegable_operations = draw(_operations_set_strategy)

    proposal = _compliant_proposal(
        realm_id=realm,
        segments=segments,
        operations=operations,
        read_clearance=clearance,
        write_classifications=write_classifications,
        delegable_operations=delegable_operations,
        expires_at=expires_at,
    )
    return manager, proposal


@st.composite
def _bounded_nonempty_delegation(draw: st.DrawFn) -> tuple[GrantRecord, ProposedGrant]:
    # Construct from the contract, without filtering on the implementation's
    # verdict. Every draw must be accepted, with real operations and an expiry
    # ceiling, so rejection or a broken generator fails the property outright.
    delegable = draw(
        st.frozensets(
            st.sampled_from(
                [op for op in GrantOperation if op is not GrantOperation.GRANT_MANAGE]
            ),
            min_size=1,
        )
    )
    lifetime = draw(st.integers(min_value=1, max_value=86400))
    manager = _manager_grant(
        realm_id=draw(_realm_strategy),
        segments=draw(_segments_strategy),
        read_clearance=draw(_classification_strategy),
        write_classifications=draw(_classifications_set_strategy),
        delegable_operations=delegable,
        expires_at=_NOW + timedelta(seconds=lifetime),
    )
    proposal = _compliant_proposal(
        realm_id=manager.realm_id,
        segments=manager.segments + draw(_segments_strategy),
        operations=draw(st.frozensets(st.sampled_from(sorted(delegable)), min_size=1)),
        read_clearance=draw(
            st.sampled_from(
                [
                    value
                    for value in Classification
                    if CLEARANCE_ORDER[value] <= CLEARANCE_ORDER[manager.read_clearance]
                ]
            )
        ),
        write_classifications=draw(
            st.frozensets(st.sampled_from(sorted(manager.write_classifications)))
            if manager.write_classifications
            else st.just(frozenset())
        ),
        expires_at=_NOW + timedelta(seconds=draw(st.integers(1, lifetime))),
    )
    return manager, proposal


@settings(max_examples=500, deadline=None)
@given(
    pair=_manager_and_proposal(),
    accepted_pair=_bounded_nonempty_delegation(),
    principal_kind=_principal_kind_strategy,
)
def test_delegation_acceptance_implies_every_envelope_dimension_is_within_bounds(
    pair: tuple[GrantRecord, ProposedGrant],
    accepted_pair: tuple[GrantRecord, ProposedGrant],
    principal_kind: PrincipalKind,
) -> None:
    manager, proposal = accepted_pair
    assert manager.expires_at is not None
    assert proposal.operations
    assert delegation_violation(manager, proposal, principal_kind, _NOW) is None
    _assert_envelope_dimensions(manager, proposal, principal_kind)

    # Keep the general implication: rejected pairs need not satisfy the
    # envelope, but any unexpectedly accepted pair must satisfy every bound.
    manager, proposal = pair
    violation = delegation_violation(manager, proposal, principal_kind, _NOW)
    if violation is not None:
        return
    _assert_envelope_dimensions(manager, proposal, principal_kind)


def _assert_envelope_dimensions(
    manager: GrantRecord, proposal: ProposedGrant, principal_kind: PrincipalKind
) -> None:
    assert is_live(manager, _NOW)
    assert proposal.realm_id == manager.realm_id
    assert is_scope_prefix(manager.segments, proposal.segments)
    assert proposal.operations <= (manager.delegable_operations or frozenset())
    assert GrantOperation.GRANT_MANAGE not in proposal.operations
    assert (
        CLEARANCE_ORDER[proposal.read_clearance]
        <= CLEARANCE_ORDER[manager.read_clearance]
    )
    assert proposal.write_classifications <= manager.write_classifications
    assert proposal.delegable_operations is None
    if manager.expires_at is not None:
        assert proposal.expires_at is not None
        assert proposal.expires_at <= manager.expires_at
    if principal_kind is PrincipalKind.WORKLOAD:
        assert proposal.expires_at is not None


def test_delegation_property_fails_when_every_generated_pair_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A separate accepted example cannot detect a vacuous property run.
    monkeypatch.setattr(
        f"{__name__}.delegation_violation", lambda *args: "manager_grant_not_live"
    )
    with pytest.raises(AssertionError):
        test_delegation_acceptance_implies_every_envelope_dimension_is_within_bounds()
