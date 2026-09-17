"""Bearer authentication and the boundary addressing screen for ``/v1``.

P-27's order is the module's shape: authenticate the bearer token first,
then screen every addressing field of the parsed body — realm identifiers
and all scope segment kinds and identifiers — before the application
pipeline is invoked (amended I-74). Authentication failures and boundary
``secret_rejected`` denials append to the always-present instance audit
chain (I-53) with a SHA-256 safe request fingerprint, durably before the
response returns. The public failure stays coarse per I-26 — every
authentication denial is the one ``authentication_failed`` outcome — while
the granular reason travels only on the private chain, which realm
``audit-read`` grants cannot reach.
"""

import hashlib
from pathlib import Path
from uuid import UUID

from starlette.datastructures import Headers

from cairn.authority.credentials import (
    AuthenticationDenied,
    CredentialAuthenticator,
)
from cairn.authority.gate import (
    AUTHENTICATION_FAILED_MESSAGE,
    SECRET_REJECTED_MESSAGE,
    Actor,
    fetch_from,
    instance_denial_draft,
    instance_id,
)
from cairn.catalogue.audit import ActionKind
from cairn.catalogue.sqlite import read_connection
from cairn.catalogue.transactions import (
    CatalogueTransactions,
    FailureCode,
    FailureDetail,
    Rejected,
    RetryClass,
    StableFailure,
)
from cairn.screening import (
    POLICY_VERSION,
    SecretScreen,
    audit_reason_code,
)

_AUTHORIZATION_HEADER = "Authorization"
# Exact scheme match, deliberately case-sensitive: the same fail-closed
# canonical-form discipline P-28 applies to the idempotency key. RFC 7235
# admits case-insensitive schemes; every mainstream client sends the
# canonical spelling.
_BEARER_PREFIX = "Bearer "

# Extraction failures the authenticator never sees, named for the private
# chain only — the public outcome is uniformly ``authentication_failed``
# (I-26), so granularity here discloses nothing.
_REASON_HEADER_MISSING = "authorization_header_missing"
_REASON_HEADER_DUPLICATED = "authorization_header_duplicated"
_REASON_SCHEME_UNSUPPORTED = "authorization_scheme_unsupported"


def authenticate_request(
    headers: Headers,
    *,
    authenticator: CredentialAuthenticator,
    transactions: CatalogueTransactions,
    data_path: Path,
    action_code: str,
    action_kind: ActionKind,
    correlation_id: UUID,
) -> Actor | Rejected:
    """Resolves ``Authorization: Bearer`` to an ``Actor``, or denies.

    Every denial — absent header, duplicated header, wrong scheme, and
    every granular ``AuthenticationDenied`` the authenticator returns — is
    recorded on the instance chain with its precise reason and answered
    publicly as the identical coarse ``authentication_failed``, which is
    what makes `AUTH-03`'s indistinguishability hold at the wire. The
    fingerprint hashes the presented header value (absent header → none:
    there is nothing to fingerprint), so repeated presentations of one bad
    token correlate without the token ever entering the event (Operator,
    7 August 2026).
    """

    def deny(reason_code: str, fingerprint_source: str | None) -> Rejected:
        return _deny_authentication(
            transactions,
            data_path,
            action_code=action_code,
            action_kind=action_kind,
            correlation_id=correlation_id,
            reason_code=reason_code,
            fingerprint_source=fingerprint_source,
        )

    values = headers.getlist(_AUTHORIZATION_HEADER)
    if not values:
        return deny(_REASON_HEADER_MISSING, None)
    if len(values) > 1:
        # Hash the joined values: either header alone would fingerprint a
        # different request than the one that was actually made.
        return deny(_REASON_HEADER_DUPLICATED, "\n".join(values))
    submitted = values[0]
    if not submitted.startswith(_BEARER_PREFIX):
        return deny(_REASON_SCHEME_UNSUPPORTED, submitted)
    result = authenticator.authenticate(submitted[len(_BEARER_PREFIX) :])
    # ``isinstance``, deliberately against the ``type(x) is`` convention:
    # mypy narrows only the positive arm of ``type() is``, and both arms of
    # this closed two-member union are consumed here, so the convention's
    # form cannot type-check. A narrowing aid, not a validation raise.
    if isinstance(result, AuthenticationDenied):
        return deny(result.reason_code, submitted)
    return Actor(
        principal_id=result.principal_id,
        credential_id=result.credential_id,
    )


def screen_addressing(
    body: dict[str, object],
    *,
    screen: SecretScreen,
    actor: Actor,
    transactions: CatalogueTransactions,
    data_path: Path,
    action_code: str,
    action_kind: ActionKind,
    correlation_id: UUID,
) -> Rejected | None:
    """P-27's boundary screen over the parsed body's addressing fields.

    Walks the addressing shapes I-74 names — ``realm_id``, ``scope`` and
    ``target_scope`` (realm and every segment kind and identifier),
    ``grant`` (realm and segments) and ``scope_prefix`` — and stops at the
    first field that yields a finding, the same tier-above rule the
    custody seam follows; within each field every rule still runs. Field
    paths are wire-level names, because this screen *is* the transport's —
    the custody seam keeps its command-level names below.

    A finding denies as ``secret_rejected`` with the I-72 three-field
    ``detail``. The denial event lands on the instance chain, which
    structurally carries no scope fields (I-53, exactly P-27's "no scope
    fields": the scope itself is the suspect content); the fingerprint
    hashes the offending field's text per I-53's untrusted-text rule, so
    repeats of the same hostile value correlate.
    """
    for field_path, text in _addressing_fields(body):
        findings = screen.screen(field_path, text)
        if not findings:
            continue
        finding = findings[0]
        draft = instance_denial_draft(
            _current_instance_id(data_path),
            actor,
            action_code,
            audit_reason_code(finding.rule),
            correlation_id,
            action_kind=action_kind,
            safe_request_fingerprint=hashlib.sha256(text.encode()).digest(),
        )
        failure = StableFailure(
            code=FailureCode.SECRET_REJECTED,
            safe_message=SECRET_REJECTED_MESSAGE,
            correlation_id=correlation_id,
            retry=RetryClass.NEVER,
            detail=FailureDetail(
                policy=POLICY_VERSION,
                rule=finding.rule,
                field_path=finding.field_path,
            ),
        )
        return transactions.reject(draft, failure)
    return None


def _deny_authentication(
    transactions: CatalogueTransactions,
    data_path: Path,
    *,
    action_code: str,
    action_kind: ActionKind,
    correlation_id: UUID,
    reason_code: str,
    fingerprint_source: str | None,
) -> Rejected:
    draft = instance_denial_draft(
        _current_instance_id(data_path),
        None,
        action_code,
        reason_code,
        correlation_id,
        action_kind=action_kind,
        safe_request_fingerprint=(
            None
            if fingerprint_source is None
            else hashlib.sha256(fingerprint_source.encode()).digest()
        ),
    )
    failure = StableFailure(
        code=FailureCode.AUTHENTICATION_FAILED,
        safe_message=AUTHENTICATION_FAILED_MESSAGE,
        correlation_id=correlation_id,
        retry=RetryClass.NEVER,
    )
    return transactions.reject(draft, failure)


def _current_instance_id(data_path: Path) -> str:
    with read_connection(data_path) as connection:
        return instance_id(fetch_from(connection))


def _addressing_fields(body: dict[str, object]) -> list[tuple[str, str]]:
    """The addressing fields of a parsed body, in a fixed documented order.

    Tolerant of shape: only string values are screenable, and anything
    mis-shaped is model validation's refusal to make, not this screen's —
    a wrong-typed field never reaches the application either way.
    """
    fields: list[tuple[str, str]] = []
    realm_id = body.get("realm_id")
    if type(realm_id) is str:
        fields.append(("realm_id", realm_id))
    for scope_key in ("scope", "target_scope"):
        scope = body.get(scope_key)
        if type(scope) is dict:
            realm = scope.get("realm")
            if type(realm) is str:
                fields.append((f"{scope_key}.realm", realm))
            fields.extend(
                _segment_fields(f"{scope_key}.segments", scope.get("segments"))
            )
    grant = body.get("grant")
    if type(grant) is dict:
        grant_realm = grant.get("realm_id")
        if type(grant_realm) is str:
            fields.append(("grant.realm_id", grant_realm))
        fields.extend(_segment_fields("grant.segments", grant.get("segments")))
    fields.extend(_segment_fields("scope_prefix", body.get("scope_prefix")))
    return fields


def _segment_fields(prefix: str, segments: object) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    if type(segments) is not list:
        return fields
    for index, segment in enumerate(segments):
        if type(segment) is not dict:
            continue
        for key in ("kind", "identifier"):
            value = segment.get(key)
            if type(value) is str:
                fields.append((f"{prefix}[{index}].{key}", value))
    return fields
