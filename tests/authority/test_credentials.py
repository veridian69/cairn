import base64
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cairn.authority.credentials import (
    CLEARANCE_ORDER,
    DATA_OPERATIONS,
    TOKEN_PATTERN,
    AuthenticatedActor,
    AuthenticationDenied,
    CredentialAuthenticator,
    GrantOperation,
    MintedToken,
    ParsedToken,
    PrincipalKind,
    mint_token,
    parse_token,
)
from cairn.catalogue.audit import Classification
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import _open_write_connection
from cairn.runtime.config import CairnConfig, HttpConfig, PathConfig

_CREDENTIAL_ID = UUID("abcdef12-3456-4789-89ab-cdef01234567")
_UNKNOWN_CREDENTIAL_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_PRINCIPAL_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_TOKEN_LENGTH = 87
_TS = "2026-08-05T10:11:12.123456Z"
_NOW = datetime(2026, 8, 5, 10, 11, 12, 123456, tzinfo=UTC)
# Both pass every CHECK migration 0002 places on their columns and are still
# unreadable: the expiry GLOB pins the shape of an instant without asserting
# the instant exists, and the identity GLOB permits a dash where a hex digit
# belongs.
_UNREADABLE_EXPIRY = "2026-13-45T99:99:99.000000Z"
_MALFORMED_PRINCIPAL_ID = "--------------4----8----------------"


def _fixed_entropy(payload: bytes) -> Callable[[int], bytes]:
    def entropy(size: int) -> bytes:
        assert size == len(payload)
        return payload

    return entropy


def _baseline_token() -> str:
    return mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32)))).text


def test_grant_operation_values_are_exact() -> None:
    assert {operation.value for operation in GrantOperation} == {
        "retrieve",
        "ingest",
        "promote",
        "invalidate",
        "audit-read",
        "grant-manage",
    }


def test_data_operations_are_the_four_data_operations() -> None:
    assert DATA_OPERATIONS == frozenset(
        {
            GrantOperation.RETRIEVE,
            GrantOperation.INGEST,
            GrantOperation.PROMOTE,
            GrantOperation.INVALIDATE,
        }
    )


def test_principal_kind_values_are_exact() -> None:
    assert {kind.value for kind in PrincipalKind} == {"human", "workload"}


def test_clearance_order_ranks_public_below_internal_below_restricted() -> None:
    assert (
        CLEARANCE_ORDER[Classification.PUBLIC]
        < CLEARANCE_ORDER[Classification.INTERNAL]
        < CLEARANCE_ORDER[Classification.RESTRICTED]
    )


def test_minted_token_matches_the_i62_grammar_exactly() -> None:
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))

    assert isinstance(minted, MintedToken)
    assert TOKEN_PATTERN.fullmatch(minted.text) is not None
    assert len(minted.text) == _TOKEN_LENGTH


def test_minted_tokens_all_share_one_length() -> None:
    lengths = {
        len(mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes([value] * 32))).text)
        for value in (0, 1, 255)
    }

    assert lengths == {_TOKEN_LENGTH}


def test_secret_encodes_the_32_entropy_bytes_as_unpadded_base64url() -> None:
    payload = bytes(range(32))
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(payload))

    secret = minted.text.rsplit(".", 1)[1]
    assert "=" not in secret
    padded = secret + "=" * (-len(secret) % 4)
    assert base64.urlsafe_b64decode(padded) == payload


def test_verifier_is_sha256_of_the_secret_component() -> None:
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))

    secret = minted.text.rsplit(".", 1)[1]
    assert minted.verifier == hashlib.sha256(secret.encode()).digest()


def test_parse_token_round_trips_the_minted_credential_id_and_verifier() -> None:
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))

    parsed = parse_token(minted.text)

    assert parsed == ParsedToken(
        credential_id=_CREDENTIAL_ID, presented_verifier=minted.verifier
    )


def _malformed_shapes() -> dict[str, str]:
    baseline = _baseline_token()
    prefix, uuid_text, secret = baseline.split(".")
    return {
        "wrong_prefix": f"cairn2.{uuid_text}.{secret}",
        "uppercase_uuid": f"{prefix}.{uuid_text.upper()}.{secret}",
        "non_v4_uuid": f"{prefix}.{uuid_text[:14]}1{uuid_text[15:]}.{secret}",
        "secret_42_chars": f"{prefix}.{uuid_text}.{secret[:-1]}",
        "secret_44_chars": f"{prefix}.{uuid_text}.{secret}A",
        "padded_secret": f"{prefix}.{uuid_text}.{secret[:-1]}=",
        "non_ascii_secret": f"{prefix}.{uuid_text}.{secret[:-1]}é",
        "extra_separator": f"{prefix}.{uuid_text}.{secret}.",
    }


@pytest.mark.parametrize("shape", sorted(_malformed_shapes()))
def test_malformed_shapes_are_rejected(shape: str) -> None:
    text = _malformed_shapes()[shape]

    assert parse_token(text) is None


@settings(max_examples=50, deadline=None)
@given(
    credential_id=st.uuids(version=4),
    entropy_bytes=st.binary(min_size=32, max_size=32),
)
def test_mint_then_parse_round_trips_for_any_uuid4_and_entropy(
    credential_id: UUID, entropy_bytes: bytes
) -> None:
    minted = mint_token(credential_id, _fixed_entropy(entropy_bytes))

    parsed = parse_token(minted.text)

    assert parsed == ParsedToken(
        credential_id=credential_id, presented_verifier=minted.verifier
    )


@settings(max_examples=200, deadline=None)
@given(
    index=st.integers(min_value=0, max_value=_TOKEN_LENGTH - 1),
    replacement=st.characters(min_codepoint=0, max_codepoint=0x2FFFF),
)
def test_single_character_mutation_never_raises(index: int, replacement: str) -> None:
    baseline = _baseline_token()
    mutated = baseline[:index] + replacement + baseline[index + 1 :]

    result = parse_token(mutated)

    if result is not None:
        assert isinstance(result, ParsedToken)
        assert TOKEN_PATTERN.fullmatch(mutated) is not None


def _catalogue_config(data_path: Path) -> CairnConfig:
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=_INSTANCE_ID,
        mode="test",
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=data_path / "credentials"),
    )


def _seed_catalogue(data_path: Path) -> None:
    migrate_catalogue(_catalogue_config(data_path), lambda: _NOW)
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
            ("acme", _TS),
        )
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(_PRINCIPAL_ID), "human", "operator", _TS),
        )
        connection.commit()


def _insert_credential(
    data_path: Path,
    credential_id: UUID,
    verifier: bytes,
    *,
    expires_at: str | None = None,
    principal_id: str | None = None,
) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credentials "
            "(credential_id, principal_id, verifier, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(credential_id),
                principal_id or str(_PRINCIPAL_ID),
                verifier,
                _TS,
                expires_at,
            ),
        )
        connection.commit()


def _insert_malformed_principal(data_path: Path) -> None:
    """A principals row whose identity passes migration 0002's shape-only
    GLOB and still cannot be parsed — 36 characters of hex digits and dashes
    with the '4' and variant nibbles in place, and the rest of the dashes
    anywhere.

    ``credentials.principal_id`` carries no CHECK of its own, only a foreign
    key to this column, so this row is the whole of what stands between the
    catalogue and an unparseable actor identity.
    """
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO principals (principal_id, kind, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (_MALFORMED_PRINCIPAL_ID, "human", "drifted", _TS),
        )
        connection.commit()


def _revoke_credential(data_path: Path, credential_id: UUID) -> None:
    with _open_write_connection(data_path, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO credential_revocations "
            "(credential_id, revoked_at, revoked_by, reason_code) "
            "VALUES (?, ?, ?, ?)",
            (str(credential_id), _TS, str(_PRINCIPAL_ID), "superseded"),
        )
        connection.commit()


def _counting_compare() -> tuple[
    list[tuple[bytes, bytes]], Callable[[bytes, bytes], bool]
]:
    calls: list[tuple[bytes, bytes]] = []

    def compare(stored: bytes, presented: bytes) -> bool:
        calls.append((stored, presented))
        return stored == presented

    return calls, compare


def test_valid_live_credential_authenticates(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(tmp_path, _CREDENTIAL_ID, minted.verifier)
    calls, compare = _counting_compare()
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW, compare=compare)

    result = authenticator.authenticate(minted.text)

    assert result == AuthenticatedActor(
        principal_id=_PRINCIPAL_ID, credential_id=_CREDENTIAL_ID
    )
    assert len(calls) == 1


def test_malformed_token_denies_via_dummy_path(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    calls, compare = _counting_compare()
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW, compare=compare)

    result = authenticator.authenticate("not-a-token")

    assert result == AuthenticationDenied("malformed_token")
    assert len(calls) == 1


def test_unknown_credential_denies_via_dummy_path(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    minted = mint_token(_UNKNOWN_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    calls, compare = _counting_compare()
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW, compare=compare)

    result = authenticator.authenticate(minted.text)

    assert result == AuthenticationDenied("unknown_credential")
    assert len(calls) == 1


def test_wrong_secret_denies_with_verifier_mismatch(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(tmp_path, _CREDENTIAL_ID, minted.verifier)
    wrong = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes([255] * 32)))
    calls, compare = _counting_compare()
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW, compare=compare)

    result = authenticator.authenticate(wrong.text)

    assert result == AuthenticationDenied("verifier_mismatch")
    assert len(calls) == 1


def test_expired_credential_denies_after_successful_comparison(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(tmp_path, _CREDENTIAL_ID, minted.verifier, expires_at=_TS)
    calls, compare = _counting_compare()
    authenticator = CredentialAuthenticator(
        tmp_path, lambda: _NOW + timedelta(seconds=1), compare=compare
    )

    result = authenticator.authenticate(minted.text)

    assert result == AuthenticationDenied("credential_expired")
    assert len(calls) == 1


def test_an_unreadable_expiry_denies_rather_than_raising(tmp_path: Path) -> None:
    """``ck_credentials_expires_at`` constrains the shape of an instant, not
    that the instant exists, so this row is one SQLite stores happily.
    ``parse_timestamp`` answers it with ``CatalogueStorageError``, which is
    not an ``AuthenticationDenied`` and would escape the authentication path
    of every request as a raw exception. The storage layer's own code becomes
    the reason, as ``gate._stored_grant_timestamp`` does for grants."""
    _seed_catalogue(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(
        tmp_path, _CREDENTIAL_ID, minted.verifier, expires_at=_UNREADABLE_EXPIRY
    )
    calls, compare = _counting_compare()
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW, compare=compare)

    result = authenticator.authenticate(minted.text)

    assert result == AuthenticationDenied("timestamp_malformed")
    # The refusal is still reached only after the constant-time comparison:
    # a caller must not learn from timing that this credential exists.
    assert len(calls) == 1


def test_an_unparseable_principal_identity_denies_rather_than_raising(
    tmp_path: Path,
) -> None:
    """The credential is live, unrevoked and its verifier matches — every
    check passes and the actor still cannot be constructed. Without the
    guard ``UUID()`` raises a bare ``ValueError`` at the last line of the
    authentication path."""
    _seed_catalogue(tmp_path)
    _insert_malformed_principal(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(
        tmp_path,
        _CREDENTIAL_ID,
        minted.verifier,
        principal_id=_MALFORMED_PRINCIPAL_ID,
    )
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW)

    result = authenticator.authenticate(minted.text)

    assert result == AuthenticationDenied("credential_uuid_malformed")


def test_a_readable_principal_identity_still_authenticates(tmp_path: Path) -> None:
    """The baseline the case above is a single-column edit of: same seeding,
    same token, a parseable identity — so the malformed identity is the sole
    reason that one is refused."""
    _seed_catalogue(tmp_path)
    _insert_malformed_principal(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(tmp_path, _CREDENTIAL_ID, minted.verifier)
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW)

    result = authenticator.authenticate(minted.text)

    assert result == AuthenticatedActor(
        principal_id=_PRINCIPAL_ID, credential_id=_CREDENTIAL_ID
    )


def test_revoked_credential_denies_after_successful_comparison(
    tmp_path: Path,
) -> None:
    _seed_catalogue(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(tmp_path, _CREDENTIAL_ID, minted.verifier)
    _revoke_credential(tmp_path, _CREDENTIAL_ID)
    calls, compare = _counting_compare()
    authenticator = CredentialAuthenticator(tmp_path, lambda: _NOW, compare=compare)

    result = authenticator.authenticate(minted.text)

    assert result == AuthenticationDenied("credential_is_revoked")
    assert len(calls) == 1


def test_denials_differ_only_by_reason_code(tmp_path: Path) -> None:
    _seed_catalogue(tmp_path)
    minted = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    _insert_credential(tmp_path, _CREDENTIAL_ID, minted.verifier, expires_at=_TS)
    wrong = mint_token(_CREDENTIAL_ID, _fixed_entropy(bytes([255] * 32)))
    unknown = mint_token(_UNKNOWN_CREDENTIAL_ID, _fixed_entropy(bytes(range(32))))
    authenticator = CredentialAuthenticator(
        tmp_path, lambda: _NOW + timedelta(seconds=1)
    )

    denials = [
        authenticator.authenticate("not-a-token"),
        authenticator.authenticate(unknown.text),
        authenticator.authenticate(wrong.text),
        authenticator.authenticate(minted.text),
    ]

    assert [type(denial) for denial in denials] == [AuthenticationDenied] * 4
    assert {denial.reason_code for denial in denials} == {  # type: ignore[union-attr]
        "malformed_token",
        "unknown_credential",
        "verifier_mismatch",
        "credential_expired",
    }
