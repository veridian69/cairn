"""Local realm bootstrap and grant-manage recovery.

Neither procedure is ever reachable through a transport: no REST route or
MCP operation exposes bootstrap or recovery (I-63). Both acquire and
release their own ``DataDirectoryLease`` for the whole procedure, which is
also what keeps them from racing a ``serve`` instance or each other, so a
single realm/principal-existence check taken inside the compound
transaction is sufficient — nothing else can be writing to the data
directory while the lease is held.

Refusals decided here (unknown/existing realm, unknown principal, a
non-current catalogue, an instance-mismatched catalogue) are local command
failures, not administration denials: they carry no audit trail of their
own, and any partial work is rolled back by ``execute_compound`` with no
residue.

Per I-41, every catalogue command compares the catalogue's stored instance
UUID against ``config.instance_id`` and refuses with ``instance_mismatch``
before any mutation; this cannot be folded into the currency pragma check
above since a wrong-instance catalogue can otherwise look perfectly
current.

On ``CommitAmbiguity``, ``execute_compound`` itself fails closed with
``CatalogueTransactionError("commit_outcome_unknown")``, which is left to
propagate unwrapped. The operator re-runs the command: if the realm now
exists, ``bootstrap`` refuses with ``realm_exists``; ``recover`` always
mints a fresh replacement grant (and, on the ``--label`` path, a fresh
credential) rather than trusting the ambiguous prior attempt.
"""

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from cairn.authority.credentials import DATA_OPERATIONS, GrantOperation, PrincipalKind
from cairn.authority.credentials import mint_token as _mint_token
from cairn.catalogue.audit import (
    ZERO_HASH,
    ActionKind,
    AuditDraft,
    ChainKind,
    Classification,
    Outcome,
    Scope,
)
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    canonical_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import CatalogueTransactions, CompoundTransaction
from cairn.runtime.config import CairnConfig
from cairn.runtime.lease import DataDirectoryLease

_MANAGE_DELEGABLE_OPERATIONS: tuple[GrantOperation, ...] = tuple(
    sorted(DATA_OPERATIONS | {GrantOperation.AUDIT_READ}, key=lambda op: op.value)
)


class BootstrapError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"bootstrap error: {code}")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    realm_id: str
    principal_id: UUID
    credential_id: UUID
    grant_ids: tuple[UUID, UUID, UUID]
    token: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    realm_id: str
    principal_id: UUID
    grant_id: UUID
    credential_id: UUID | None
    token: str | None


def bootstrap_realm(
    config: CairnConfig,
    *,
    realm_id: str,
    label: str,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
    entropy: Callable[[int], bytes],
) -> BootstrapResult:
    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    lease.acquire()
    try:
        _require_current_catalogue(config)
        transactions = _transactions(config.paths.data, clock, uuid_factory)

        def work(transaction: CompoundTransaction) -> BootstrapResult:
            if transaction.query(
                "SELECT 1 FROM realms WHERE realm_id = ?", (realm_id,)
            ):
                raise BootstrapError("realm_exists")

            now = clock()
            principal_id = uuid_factory()
            credential_id = uuid_factory()
            grant_ids = (uuid_factory(), uuid_factory(), uuid_factory())
            minted = _mint_token(credential_id, entropy)

            transaction.execute(
                "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
                (realm_id, canonical_timestamp(now)),
            )
            transaction.execute(
                "INSERT INTO audit_heads "
                "(chain_kind, chain_identity, last_sequence, last_hash) "
                "VALUES ('realm', ?, 0, ?)",
                (realm_id, ZERO_HASH),
            )
            transaction.append(
                _local_draft(
                    realm_id,
                    action_kind=ActionKind.SYSTEM,
                    action_code="realm-genesis",
                    reason_code="realm_created",
                    correlation_id=uuid_factory(),
                )
            )
            transaction.execute(
                "INSERT INTO principals (principal_id, kind, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(principal_id),
                    PrincipalKind.HUMAN.value,
                    label,
                    canonical_timestamp(now),
                ),
            )
            transaction.execute(
                "INSERT INTO credentials "
                "(credential_id, principal_id, verifier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (
                    str(credential_id),
                    str(principal_id),
                    minted.verifier,
                    canonical_timestamp(now),
                ),
            )
            _insert_root_grant(
                transaction,
                grant_id=grant_ids[0],
                principal_id=principal_id,
                realm_id=realm_id,
                operations=DATA_OPERATIONS,
                delegable_operations=None,
                now=now,
            )
            _insert_root_grant(
                transaction,
                grant_id=grant_ids[1],
                principal_id=principal_id,
                realm_id=realm_id,
                operations=frozenset({GrantOperation.AUDIT_READ}),
                delegable_operations=None,
                now=now,
            )
            _insert_root_grant(
                transaction,
                grant_id=grant_ids[2],
                principal_id=principal_id,
                realm_id=realm_id,
                operations=frozenset({GrantOperation.GRANT_MANAGE}),
                delegable_operations=_MANAGE_DELEGABLE_OPERATIONS,
                now=now,
            )
            transaction.append(
                _local_draft(
                    realm_id,
                    action_kind=ActionKind.ADMINISTRATION,
                    action_code="realm-bootstrap",
                    reason_code="bootstrap_completed",
                    affected_grant_ids=tuple(sorted(grant_ids, key=str)),
                    correlation_id=uuid_factory(),
                )
            )
            return BootstrapResult(
                realm_id=realm_id,
                principal_id=principal_id,
                credential_id=credential_id,
                grant_ids=grant_ids,
                token=minted.text,
            )

        return transactions.execute_compound(work)
    finally:
        lease.release()


def recover_realm(
    config: CairnConfig,
    *,
    realm_id: str,
    principal_id: UUID | None,
    label: str | None,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
    entropy: Callable[[int], bytes],
) -> RecoveryResult:
    if (principal_id is None) == (label is None):
        raise BootstrapError("invalid_selector")

    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    lease.acquire()
    try:
        _require_current_catalogue(config)
        transactions = _transactions(config.paths.data, clock, uuid_factory)

        def work(transaction: CompoundTransaction) -> RecoveryResult:
            if not transaction.query(
                "SELECT 1 FROM realms WHERE realm_id = ?", (realm_id,)
            ):
                raise BootstrapError("realm_unknown")

            now = clock()
            new_credential_id: UUID | None = None
            token: str | None = None
            if principal_id is not None:
                if not transaction.query(
                    "SELECT 1 FROM principals WHERE principal_id = ?",
                    (str(principal_id),),
                ):
                    raise BootstrapError("principal_unknown")
                target_principal_id = principal_id
            else:
                assert label is not None
                target_principal_id = uuid_factory()
                new_credential_id = uuid_factory()
                minted = _mint_token(new_credential_id, entropy)
                token = minted.text
                transaction.execute(
                    "INSERT INTO principals "
                    "(principal_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
                    (
                        str(target_principal_id),
                        PrincipalKind.HUMAN.value,
                        label,
                        canonical_timestamp(now),
                    ),
                )
                transaction.execute(
                    "INSERT INTO credentials "
                    "(credential_id, principal_id, verifier, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (
                        str(new_credential_id),
                        str(target_principal_id),
                        minted.verifier,
                        canonical_timestamp(now),
                    ),
                )

            grant_id = uuid_factory()
            _insert_root_grant(
                transaction,
                grant_id=grant_id,
                principal_id=target_principal_id,
                realm_id=realm_id,
                operations=frozenset({GrantOperation.GRANT_MANAGE}),
                delegable_operations=_MANAGE_DELEGABLE_OPERATIONS,
                now=now,
            )
            transaction.append(
                _local_draft(
                    realm_id,
                    action_kind=ActionKind.ADMINISTRATION,
                    action_code="realm-recover",
                    reason_code="recovery_grant_issued",
                    affected_grant_ids=(grant_id,),
                    correlation_id=uuid_factory(),
                )
            )
            return RecoveryResult(
                realm_id=realm_id,
                principal_id=target_principal_id,
                grant_id=grant_id,
                credential_id=new_credential_id,
                token=token,
            )

        return transactions.execute_compound(work)
    finally:
        lease.release()


def _require_current_catalogue(config: CairnConfig) -> None:
    try:
        with read_connection(config.paths.data) as connection:
            stored_instance_id = connection.execute(
                "SELECT instance_id FROM catalogue_metadata"
            ).fetchone()[0]
    except CatalogueStorageError as error:
        if error.code == "catalogue_identity_mismatch":
            raise BootstrapError("catalogue_not_current") from error
        raise
    if stored_instance_id != str(config.instance_id):
        raise BootstrapError("instance_mismatch")


def _transactions(
    data_path: Path,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
) -> CatalogueTransactions:
    return CatalogueTransactions(
        data_path,
        writer_gate=threading.Lock(),
        clock=clock,
        uuid_factory=uuid_factory,
    )


def _insert_root_grant(
    transaction: CompoundTransaction,
    *,
    grant_id: UUID,
    principal_id: UUID,
    realm_id: str,
    operations: frozenset[GrantOperation],
    delegable_operations: tuple[GrantOperation, ...] | None,
    now: datetime,
) -> None:
    transaction.execute(
        "INSERT INTO grants (grant_id, principal_id, realm_id, scope_segments, "
        "operations, read_clearance, write_classifications, delegable_operations, "
        "issued_by, expires_at, created_at) "
        "VALUES (?, ?, ?, '[]', ?, ?, ?, ?, NULL, NULL, ?)",
        (
            str(grant_id),
            str(principal_id),
            realm_id,
            _json_sorted_values(operation.value for operation in operations),
            Classification.RESTRICTED.value,
            _json_sorted_values(
                classification.value for classification in Classification
            ),
            None
            if delegable_operations is None
            else _json_sorted_values(
                operation.value for operation in delegable_operations
            ),
            canonical_timestamp(now),
        ),
    )


def _local_draft(
    realm_id: str,
    *,
    action_kind: ActionKind,
    action_code: str,
    reason_code: str,
    correlation_id: UUID,
    affected_grant_ids: tuple[UUID, ...] = (),
) -> AuditDraft:
    return AuditDraft(
        chain_kind=ChainKind.REALM,
        chain_identity=realm_id,
        principal_id=None,
        credential_verifier_id=None,
        grant_id=None,
        action_kind=action_kind,
        action_code=action_code,
        source_scope=None,
        requested_scope=Scope(realm_id, ()),
        target_scope=None,
        outcome=Outcome.ALLOW,
        reason_code=reason_code,
        affected_assertion_ids=(),
        affected_fact_ids=(),
        affected_evidence_ids=(),
        affected_grant_ids=affected_grant_ids,
        classification_transition=None,
        trust_transition=None,
        evidence_reference=None,
        evidence_digest=None,
        correlation_id=correlation_id,
        idempotency_key=None,
        mutation_id=None,
        command_digest=None,
        replay_of_mutation_id=None,
        safe_request_fingerprint=None,
    )


def _json_sorted_values(values: Iterable[str]) -> str:
    return json.dumps(
        sorted(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
