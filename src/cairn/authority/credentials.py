"""Capability vocabulary and credential token format."""

import base64
import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from cairn.catalogue.audit import Classification
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    parse_timestamp,
    read_connection,
)

_SECRET_ENTROPY_BYTES = 32
_DUMMY_VERIFIER = hashlib.sha256(b"cairn-authority-dummy-verifier").digest()

TOKEN_PATTERN = re.compile(
    r"cairn1\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"\.[A-Za-z0-9_-]{43}"
)


class GrantOperation(StrEnum):
    RETRIEVE = "retrieve"
    INGEST = "ingest"
    PROMOTE = "promote"
    INVALIDATE = "invalidate"
    AUDIT_READ = "audit-read"
    GRANT_MANAGE = "grant-manage"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    WORKLOAD = "workload"


DATA_OPERATIONS: frozenset[GrantOperation] = frozenset(
    {
        GrantOperation.RETRIEVE,
        GrantOperation.INGEST,
        GrantOperation.PROMOTE,
        GrantOperation.INVALIDATE,
    }
)

CLEARANCE_ORDER: dict[Classification, int] = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.RESTRICTED: 2,
}


@dataclass(frozen=True, slots=True)
class MintedToken:
    text: str
    verifier: bytes


@dataclass(frozen=True, slots=True)
class ParsedToken:
    credential_id: UUID
    presented_verifier: bytes


def mint_token(credential_id: UUID, entropy: Callable[[int], bytes]) -> MintedToken:
    secret = (
        base64.urlsafe_b64encode(entropy(_SECRET_ENTROPY_BYTES))
        .decode("ascii")
        .rstrip("=")
    )
    text = f"cairn1.{credential_id}.{secret}"
    verifier = hashlib.sha256(secret.encode()).digest()
    return MintedToken(text=text, verifier=verifier)


def parse_token(text: str) -> ParsedToken | None:
    if TOKEN_PATTERN.fullmatch(text) is None:
        return None
    _, uuid_text, secret = text.split(".")
    verifier = hashlib.sha256(secret.encode()).digest()
    return ParsedToken(credential_id=UUID(uuid_text), presented_verifier=verifier)


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    principal_id: UUID
    credential_id: UUID


@dataclass(frozen=True, slots=True)
class AuthenticationDenied:
    reason_code: str


class CredentialAuthenticator:
    def __init__(
        self,
        data_path: Path,
        clock: Callable[[], datetime],
        compare: Callable[[bytes, bytes], bool] = hmac.compare_digest,
    ) -> None:
        self._data_path = data_path
        self._clock = clock
        self._compare = compare

    def authenticate(
        self, token_text: str
    ) -> AuthenticatedActor | AuthenticationDenied:
        parsed = parse_token(token_text)
        row: tuple[str, bytes, str | None] | None = None
        if parsed is not None:
            with read_connection(self._data_path) as connection:
                row = connection.execute(
                    "SELECT principal_id, verifier, expires_at "
                    "FROM credentials WHERE credential_id = ?",
                    (str(parsed.credential_id),),
                ).fetchone()

        stored_verifier = row[1] if row is not None else _DUMMY_VERIFIER
        presented_verifier = (
            parsed.presented_verifier if parsed is not None else _DUMMY_VERIFIER
        )
        matched = self._compare(stored_verifier, presented_verifier)

        if parsed is None:
            return AuthenticationDenied("malformed_token")
        if row is None:
            return AuthenticationDenied("unknown_credential")
        if not matched:
            return AuthenticationDenied("verifier_mismatch")

        principal_id_text, _, expires_at = row
        if expires_at is not None:
            # ck_credentials_expires_at pins the *shape* — 27 characters
            # matching the canonical GLOB — and nothing checks that the
            # instant exists, so '2026-13-45T99:99:99.000000Z' is a row SQLite
            # accepts. parse_timestamp answers that with CatalogueStorageError,
            # which would escape authenticate as a raw exception on the
            # authentication path of every request rather than as a denial.
            # Its own code is reused as the reason, mirroring
            # cairn.authority.gate._stored_grant_timestamp.
            try:
                expiry = parse_timestamp(expires_at)
            except CatalogueStorageError as error:
                return AuthenticationDenied(error.code)
            if self._clock() >= expiry:
                return AuthenticationDenied("credential_expired")

        with read_connection(self._data_path) as connection:
            revoked = connection.execute(
                "SELECT 1 FROM credential_revocations WHERE credential_id = ?",
                (str(parsed.credential_id),),
            ).fetchone()
        if revoked is not None:
            return AuthenticationDenied("credential_is_revoked")

        # credentials.principal_id carries no CHECK of its own (migration
        # 0002), only a foreign key to principals.principal_id — whose CHECK
        # is the same shape-only GLOB cairn.authority.gate._stored_grant_uuid
        # proved non-total. So a row every constraint admits can still hold
        # '0000000--0000-4000-8000-000000000000', on which UUID() raises a
        # raw ValueError out of the authentication path.
        try:
            principal_id = UUID(principal_id_text)
        except ValueError:
            return AuthenticationDenied("credential_uuid_malformed")

        return AuthenticatedActor(
            principal_id=principal_id,
            credential_id=parsed.credential_id,
        )
