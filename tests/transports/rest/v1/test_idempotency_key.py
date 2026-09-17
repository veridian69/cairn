from uuid import UUID

import pytest
from starlette.datastructures import Headers

from cairn.transports.rest.v1.parsing import (
    forbid_idempotency_key,
    require_idempotency_key,
)
from cairn.transports.v1.parsing import WireRejection
from cairn.transports.v1.wire import (
    RULE_IDEMPOTENCY_KEY_DUPLICATED,
    RULE_IDEMPOTENCY_KEY_FORBIDDEN,
    RULE_IDEMPOTENCY_KEY_MALFORMED,
    RULE_IDEMPOTENCY_KEY_MISSING,
)


def make_headers(pairs: list[tuple[bytes, bytes]]) -> Headers:
    return Headers(raw=pairs)


def assert_rejects(
    rejection: WireRejection,
    *,
    status: int,
    rule: str,
    field_path: str,
) -> None:
    assert rejection.status == status
    assert rejection.rule == rule
    assert rejection.field_path == field_path
    assert str(rejection) == f"wire rejection: {rule}"


def test_a_canonical_idempotency_key_is_returned_as_a_uuid() -> None:
    submitted = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    headers = make_headers([(b"idempotency-key", submitted.encode("ascii"))])
    assert require_idempotency_key(headers) == UUID(submitted)


def test_a_missing_idempotency_key_is_rejected() -> None:
    with pytest.raises(WireRejection) as caught:
        require_idempotency_key(make_headers([]))
    assert_rejects(
        caught.value,
        status=400,
        rule=RULE_IDEMPOTENCY_KEY_MISSING,
        field_path="Idempotency-Key",
    )


def test_a_duplicated_idempotency_key_is_rejected() -> None:
    headers = make_headers(
        [
            (b"idempotency-key", b"3fa85f64-5717-4562-b3fc-2c963f66afa6"),
            (b"idempotency-key", b"3fa85f64-5717-4562-b3fc-2c963f66afa6"),
        ]
    )
    with pytest.raises(WireRejection) as caught:
        require_idempotency_key(headers)
    assert_rejects(
        caught.value,
        status=400,
        rule=RULE_IDEMPOTENCY_KEY_DUPLICATED,
        field_path="Idempotency-Key",
    )


@pytest.mark.parametrize(
    "submitted",
    [
        pytest.param(b"3FA85F64-5717-4562-B3FC-2C963F66AFA6", id="uppercase"),
        pytest.param(b"{3fa85f64-5717-4562-b3fc-2c963f66afa6}", id="braced"),
        pytest.param(
            b"urn:uuid:3fa85f64-5717-4562-b3fc-2c963f66afa6", id="urn-prefixed"
        ),
        pytest.param(b"3fa85f6457174562b3fc2c963f66afa6", id="unhyphenated"),
        pytest.param(b"not-a-uuid", id="not-a-uuid"),
        pytest.param(b"", id="empty"),
    ],
)
def test_a_non_canonical_idempotency_key_is_rejected(submitted: bytes) -> None:
    headers = make_headers([(b"idempotency-key", submitted)])
    with pytest.raises(WireRejection) as caught:
        require_idempotency_key(headers)
    assert_rejects(
        caught.value,
        status=400,
        rule=RULE_IDEMPOTENCY_KEY_MALFORMED,
        field_path="Idempotency-Key",
    )


def test_an_idempotency_key_on_a_read_route_is_rejected() -> None:
    headers = make_headers(
        [(b"idempotency-key", b"3fa85f64-5717-4562-b3fc-2c963f66afa6")]
    )
    with pytest.raises(WireRejection) as caught:
        forbid_idempotency_key(headers)
    assert_rejects(
        caught.value,
        status=400,
        rule=RULE_IDEMPOTENCY_KEY_FORBIDDEN,
        field_path="Idempotency-Key",
    )


def test_an_absent_idempotency_key_on_a_read_route_passes() -> None:
    forbid_idempotency_key(make_headers([]))
