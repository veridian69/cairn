"""Bound input before decoding; never turn model or user text into shell input."""

import importlib
import io
from types import ModuleType

import pytest


def api() -> ModuleType:
    return importlib.import_module("cairn.client.command_io")


@pytest.mark.parametrize("body", [b"hello\n", "£\n".encode(), b""])
def test_text_preserves_exact_content_at_byte_boundary(body: bytes) -> None:
    assert api().read_text(io.BytesIO(body), limit=max(1, len(body))) == body.decode()


@pytest.mark.parametrize(
    "body,code", [(b"abcd", "input_too_large"), (b"\xff", "invalid_input")]
)
def test_invalid_text_has_content_free_error(body: bytes, code: str) -> None:
    module = api()
    with pytest.raises(module.CommandInputError, match=f"^{code}$"):
        module.read_text(io.BytesIO(body), limit=3)


def test_oversize_reads_at_most_one_extra_byte() -> None:
    stream = io.BytesIO(b"a" * 1000)
    module = api()
    with pytest.raises(module.CommandInputError, match="input_too_large"):
        module.read_text(stream, limit=10)
    assert stream.tell() == 11


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b"true",
        b'{"x":1,"x":2}',
        b'{"a":{"x":1,"x":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":"\\ud800"}',
        b'{"\\ud800":1}',
        b'{"nested":{"\\udfff":1}}',
        b"{",
    ],
)
def test_strict_json_refuses_invalid_or_ambiguous_objects(body: bytes) -> None:
    module = api()
    with pytest.raises(module.CommandInputError, match="^invalid_input$"):
        module.read_object(io.BytesIO(body), limit=1024)


def test_json_preserves_selected_content_without_interpreting_it() -> None:
    body = (
        '{"response":"$(echo nope)\\n","observations":[{"body":"retain £"}]}'.encode()
    )
    assert api().read_object(io.BytesIO(body), limit=1024) == {
        "response": "$(echo nope)\n",
        "observations": [{"body": "retain £"}],
    }


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 1048577])
def test_invalid_limit_is_refused_before_read(limit: object) -> None:
    module = api()
    stream = io.BytesIO(b"secret")
    with pytest.raises(module.CommandInputError, match="^invalid_input_limit$"):
        module.read_text(stream, limit=limit)
    assert stream.tell() == 0


def test_stream_failure_is_safe() -> None:
    class Broken(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("PRIVATE LOCAL DETAILS")

    module = api()
    with pytest.raises(module.CommandInputError, match="^input_unavailable$"):
        module.read_text(Broken(), limit=1024)


def test_short_reads_are_not_mistaken_for_eof() -> None:
    class Chunked(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return super().read(1 if size is None or size < 0 else min(size, 1))

    assert api().read_text(Chunked("£\n".encode()), limit=3) == "£\n"


def test_closed_stream_failure_is_safe() -> None:
    stream = io.BytesIO(b"private")
    stream.close()
    module = api()
    with pytest.raises(module.CommandInputError, match="^input_unavailable$"):
        module.read_text(stream, limit=1024)
