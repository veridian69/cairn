"""Bounded request fingerprints for identified diagnostic wire refusals only."""

import hashlib
import json

from starlette.requests import Request
from starlette.types import Message

from cairn.transports.v1.parsing import MAX_REQUEST_BYTES


class DiagnosticFingerprint:
    """Hash a bounded request prefix without retaining it.

    Six times the admitted byte cap allows canonical ASCII JSON escaping in
    MCP arguments; 4096 bytes allow framing metadata. For REST admission that
    stops before reading a body, the digest covers the observed headers only.
    No extra body is consumed just to fingerprint a rejected request.
    """

    def __init__(self) -> None:
        self._hash = hashlib.sha256(b"cairn.memory/diagnose/request/v1\0")
        self._remaining = 6 * MAX_REQUEST_BYTES + 4096

    def update(self, content: bytes) -> None:
        count = min(len(content), self._remaining)
        self._hash.update(content[:count])
        self._remaining -= count

    def digest(self) -> bytes:
        return self._hash.digest()


def fingerprinted_request(request: Request) -> tuple[Request, DiagnosticFingerprint]:
    fingerprint = DiagnosticFingerprint()
    fingerprint.update(b"rest\0")
    # Only admission metadata, never Authorization or unrelated headers.
    for name, value in request.headers.raw:
        if name.lower() in {b"content-type", b"content-length", b"idempotency-key"}:
            fingerprint.update(name.lower())
            fingerprint.update(b":")
            fingerprint.update(value)
            fingerprint.update(b"\n")
    fingerprint.update(b"\0body\0")

    async def receive() -> Message:
        message = await request.receive()
        if message["type"] == "http.request":
            fingerprint.update(message.get("body", b""))
        return message

    return Request(request.scope, receive=receive), fingerprint


def argument_fingerprint(arguments: dict[str, object]) -> bytes:
    fingerprint = DiagnosticFingerprint()
    fingerprint.update(b"mcp\0")
    # The frame was already admitted under the whole-request cap. Incremental
    # encoding avoids retaining a second complete canonical request body.
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    for piece in encoder.iterencode(arguments):
        fingerprint.update(piece.encode("ascii"))
    return fingerprint.digest()
