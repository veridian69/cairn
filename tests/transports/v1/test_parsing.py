import pytest
from starlette.requests import Request
from starlette.types import Message, Scope

from cairn.transports.v1.parsing import (
    MAX_REQUEST_BYTES,
    WireRejection,
    admit_body,
)
from cairn.transports.v1.wire import (
    RULE_BODY_NOT_OBJECT,
    RULE_BODY_TOO_LARGE,
    RULE_DUPLICATE_JSON_KEY,
    RULE_INVALID_CONTENT_TYPE,
    RULE_INVALID_ENCODING,
    RULE_MALFORMED_JSON,
)

_JSON_CONTENT_TYPE = [(b"content-type", b"application/json")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_request(
    body: bytes,
    headers: list[tuple[bytes, bytes]],
    *,
    declared_length: int | bytes | None = None,
) -> Request:
    """Builds a real Starlette request from a hand-crafted ASGI scope.

    ``declared_length`` lets a test lie about ``Content-Length`` relative
    to the actual streamed body — numerically as an ``int``, or as raw
    ``bytes`` for a value that is not decimal at all — which no HTTP
    client helper will do.
    """
    length = len(body) if declared_length is None else declared_length
    if type(length) is int:
        length = str(length).encode("ascii")
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/stub",
        "query_string": b"",
        "headers": [*headers, (b"content-length", length)],
    }
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


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


@pytest.mark.anyio
async def test_a_json_object_body_is_admitted_as_a_dict() -> None:
    request = make_request(b'{"scope": {"realm": "r"}}', _JSON_CONTENT_TYPE)
    assert await admit_body(request) == {"scope": {"realm": "r"}}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param(b"application/json; charset=utf-8", id="charset-lower"),
        pytest.param(b"application/json; charset=UTF-8", id="charset-upper"),
        pytest.param(b"Application/JSON", id="media-type-case"),
    ],
)
async def test_the_charset_parameter_and_case_variants_are_accepted(
    content_type: bytes,
) -> None:
    request = make_request(b"{}", [(b"content-type", content_type)])
    assert await admit_body(request) == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="absent"),
        pytest.param([(b"content-type", b"text/plain")], id="other-type"),
        pytest.param(
            [(b"content-type", b"application/json; charset=latin-1")],
            id="other-charset",
        ),
        pytest.param(
            [(b"content-type", b'application/json; charset="utf-8"')],
            id="quoted-charset",
        ),
        pytest.param(
            [(b"content-type", b"application/json; boundary=x")],
            id="other-parameter",
        ),
        pytest.param(
            [(b"content-type", b"application/json;")],
            id="trailing-semicolon",
        ),
        pytest.param(
            [(b"content-type", b"application/json; ")],
            id="semicolon-whitespace",
        ),
        pytest.param(
            [(b"content-type", b"application/json ;")],
            id="space-then-semicolon",
        ),
        pytest.param(
            [
                (b"content-type", b"application/json"),
                (b"content-type", b"application/json"),
            ],
            id="duplicated-header",
        ),
    ],
)
async def test_anything_but_json_content_type_is_415(
    headers: list[tuple[bytes, bytes]],
) -> None:
    request = make_request(b"{}", headers)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value,
        status=415,
        rule=RULE_INVALID_CONTENT_TYPE,
        field_path="Content-Type",
    )


@pytest.mark.anyio
async def test_a_declared_length_over_the_cap_is_413_without_reading() -> None:
    request = make_request(
        b"{}",
        _JSON_CONTENT_TYPE,
        declared_length=MAX_REQUEST_BYTES + 1,
    )
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=413, rule=RULE_BODY_TOO_LARGE, field_path="body"
    )


@pytest.mark.anyio
async def test_a_non_decimal_declared_length_falls_through_to_the_stream() -> None:
    request = make_request(b"{}", _JSON_CONTENT_TYPE, declared_length=b"junk")
    assert await admit_body(request) == {}


@pytest.mark.anyio
async def test_a_streamed_body_over_the_cap_is_413_despite_its_declaration() -> None:
    oversize = b'{"padding": "' + b"a" * MAX_REQUEST_BYTES + b'"}'
    request = make_request(oversize, _JSON_CONTENT_TYPE, declared_length=2)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=413, rule=RULE_BODY_TOO_LARGE, field_path="body"
    )


@pytest.mark.anyio
async def test_a_body_exactly_at_the_cap_is_admitted() -> None:
    padding = b"a" * (MAX_REQUEST_BYTES - len(b'{"padding": ""}'))
    body = b'{"padding": "' + padding + b'"}'
    assert len(body) == MAX_REQUEST_BYTES
    request = make_request(body, _JSON_CONTENT_TYPE)
    admitted = await admit_body(request)
    assert admitted["padding"] == padding.decode("ascii")


@pytest.mark.anyio
async def test_invalid_utf8_is_rejected_before_parsing() -> None:
    request = make_request(b'{"a": "\xff\xfe"}', _JSON_CONTENT_TYPE)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=400, rule=RULE_INVALID_ENCODING, field_path="body"
    )


@pytest.mark.anyio
async def test_malformed_json_is_rejected() -> None:
    request = make_request(b'{"a": ', _JSON_CONTENT_TYPE)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=400, rule=RULE_MALFORMED_JSON, field_path="body"
    )


@pytest.mark.anyio
async def test_a_pathologically_nested_body_is_rejected_not_crashed() -> None:
    request = make_request(b"[" * 100_000, _JSON_CONTENT_TYPE)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=400, rule=RULE_MALFORMED_JSON, field_path="body"
    )


@pytest.mark.anyio
async def test_an_integer_over_the_digit_limit_is_rejected_not_crashed() -> None:
    request = make_request(b'{"n": ' + b"9" * 5000 + b"}", _JSON_CONTENT_TYPE)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=400, rule=RULE_MALFORMED_JSON, field_path="body"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"n": NaN}', id="nan"),
        pytest.param(b'{"n": Infinity}', id="infinity"),
        pytest.param(b'{"n": -Infinity}', id="negative-infinity"),
        pytest.param(b'{"a": {"b": [1, NaN]}}', id="nested-nan"),
    ],
)
async def test_non_finite_json_constants_are_rejected_at_admission(
    body: bytes,
) -> None:
    """RFC 8259 has no ``NaN``/``Infinity``/``-Infinity``; ``json.loads``
    admits them by default, so without ``parse_constant`` a non-finite
    float crossed admission into wire validation (post-acceptance review,
    8 August 2026)."""
    request = make_request(body, _JSON_CONTENT_TYPE)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=400, rule=RULE_MALFORMED_JSON, field_path="body"
    )


@pytest.mark.anyio
async def test_an_echoed_duplicate_key_is_bounded_in_the_field_path() -> None:
    key = b"k" * 4096
    request = make_request(
        b'{"' + key + b'": 1, "' + key + b'": 2}',
        _JSON_CONTENT_TYPE,
    )
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert caught.value.rule == RULE_DUPLICATE_JSON_KEY
    assert caught.value.field_path == "k" * 128


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"scope": 1, "scope": 2}', id="top-level"),
        pytest.param(b'{"outer": {"scope": 1, "scope": 2}}', id="nested"),
        pytest.param(b'{"items": [{"scope": 1, "scope": 2}]}', id="inside-array"),
    ],
)
async def test_duplicate_json_keys_anywhere_are_rejected(body: bytes) -> None:
    request = make_request(body, _JSON_CONTENT_TYPE)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value,
        status=400,
        rule=RULE_DUPLICATE_JSON_KEY,
        field_path="scope",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"[]", id="array"),
        pytest.param(b"42", id="number"),
        pytest.param(b'"text"', id="string"),
        pytest.param(b"null", id="null"),
    ],
)
async def test_a_non_object_top_level_is_rejected(body: bytes) -> None:
    request = make_request(body, _JSON_CONTENT_TYPE)
    with pytest.raises(WireRejection) as caught:
        await admit_body(request)
    assert_rejects(
        caught.value, status=400, rule=RULE_BODY_NOT_OBJECT, field_path="body"
    )


@pytest.mark.anyio
async def test_the_same_key_in_sibling_objects_is_not_a_duplicate() -> None:
    request = make_request(b'{"a": 1, "b": {"a": 2}}', _JSON_CONTENT_TYPE)
    assert await admit_body(request) == {"a": 1, "b": {"a": 2}}
