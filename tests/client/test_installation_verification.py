"""Installation polling is bounded, read-only and preserves failure meaning."""

import contextlib
import importlib.util
import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verify-retrieval.py"
FACT = "11111111-1111-4111-8111-111111111111"
TOKEN = "cairn1." + FACT + "." + "A" * 43


@pytest.fixture
def verifier() -> Any:
    spec = importlib.util.spec_from_file_location("installation_verifier", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ready(module: Any) -> tuple[int, dict[str, str], bytes]:
    return (
        200,
        {},
        json.dumps(
            {
                "hits": [{"fact_id": FACT, "body": module.BODY}],
                "budget_consumed": 10,
                "budget_exhausted": False,
            }
        ).encode(),
    )


def failure(
    code: str, delay: str = "1", retry: str = "after-delay", status: int = 503
) -> tuple[int, dict[str, str], bytes]:
    return (
        status,
        {"retry-after": delay},
        json.dumps(
            {
                "failure": {
                    "code": code,
                    "retry": retry,
                    "message": "synthetic",
                    "correlation_id": "test",
                }
            }
        ).encode(),
    )


def exercise(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    replies: list[Any],
    limit: float = 5,
    max_attempts: int = 10,
) -> tuple[dict[str, Any], list[Any]]:
    elapsed = [0.0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda n: elapsed.__setitem__(0, elapsed[0] + n)
    )

    def request(*args: Any) -> Any:
        calls.append(args)
        return replies.pop(0)

    monkeypatch.setattr(module, "request", request)
    return (
        module.verify(
            "http://127.0.0.1:8080",
            TOKEN,
            FACT,
            limit,
            2,
            max_attempts,
        ),
        calls,
    )


@contextlib.contextmanager
def local_server(
    replies: list[tuple[int, dict[str, str], bytes]],
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": self.rfile.read(length),
                }
            )
            status, headers, body = replies.pop(0)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _answer
        do_POST = _answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_index_delay_retries_read_only_then_checks_exact_fact(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls = exercise(
        verifier, monkeypatch, [failure("index_pending"), ready(verifier)]
    )
    assert result["fact_id"] == FACT
    assert len(calls) == 2


@pytest.mark.parametrize(
    "reply",
    [
        failure("unknown"),
        failure("index_pending", retry="never"),
        failure("index_pending", status=500),
        failure("authentication_failed", status=401),
        (302, {"location": "http://elsewhere"}, b"redirect"),
    ],
)
def test_terminal_errors_are_not_retried(
    verifier: Any, monkeypatch: pytest.MonkeyPatch, reply: Any
) -> None:
    with pytest.raises(verifier.VerificationError, match="HTTP"):
        exercise(verifier, monkeypatch, [reply])


def test_retry_after_cannot_exceed_deadline(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(verifier.VerificationError, match="deadline"):
        exercise(verifier, monkeypatch, [failure("stale_index", delay="20")])


def test_dependency_unavailable_is_not_mislabelled_as_indexing_delay(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(verifier.VerificationError) as caught:
        exercise(
            verifier,
            monkeypatch,
            [failure("dependency_unavailable", delay="20")],
        )
    assert "indexing" not in str(caught.value)


def test_retry_count_is_bounded_even_when_retry_after_is_zero(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [failure("index_pending", delay="0") for _ in range(4)]
    with pytest.raises(verifier.VerificationError, match="attempt"):
        exercise(verifier, monkeypatch, replies, max_attempts=3)
    assert len(replies) == 1


@pytest.mark.parametrize("delay", ["bad", "-1", "nan"])
def test_invalid_retry_after_is_actionable(
    verifier: Any, monkeypatch: pytest.MonkeyPatch, delay: str
) -> None:
    with pytest.raises(verifier.VerificationError, match="Retry-After"):
        exercise(verifier, monkeypatch, [failure("index_pending", delay=delay)])


def test_missing_retry_after_is_actionable(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, raw = failure("index_pending")
    with pytest.raises(verifier.VerificationError, match="no Retry-After"):
        exercise(verifier, monkeypatch, [(503, {}, raw)])


def test_http_date_retry_after(verifier: Any) -> None:
    assert verifier.retry_delay("Thu, 01 Jan 1970 00:00:12 GMT", now=10) == 2


def test_empty_success_is_not_called_indexing_delay(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(verifier.VerificationError, match="expected fact"):
        exercise(
            verifier,
            monkeypatch,
            [(200, {}, b'{"hits": [], "budget_consumed":0,"budget_exhausted":false}')],
        )


def test_malformed_success_is_not_accepted(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(verifier.VerificationError, match="malformed"):
        exercise(verifier, monkeypatch, [(200, {}, b'{"hits":null}')])


def test_malformed_failure_envelope_is_not_retried(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = (
        503,
        {"retry-after": "1"},
        b'{"failure":{"code":"index_pending","retry":"after-delay"}}',
    )
    with pytest.raises(verifier.VerificationError, match="malformed failure"):
        exercise(verifier, monkeypatch, [malformed, ready(verifier)])


def test_arbitrary_response_body_is_not_copied_to_diagnostics(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "unrelated-private-response"
    with pytest.raises(verifier.VerificationError) as caught:
        exercise(verifier, monkeypatch, [(500, {}, marker.encode())])
    assert marker not in str(caught.value)


def test_failure_diagnostics_redact_the_exact_credential(
    verifier: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = json.dumps(
        {
            "failure": {
                "code": "authentication_failed",
                "retry": "never",
                "message": "echoed " + TOKEN,
                "correlation_id": FACT,
            }
        }
    ).encode()
    with pytest.raises(verifier.VerificationError) as caught:
        exercise(
            verifier,
            monkeypatch,
            [(401, {"X-Correlation-ID": TOKEN}, raw)],
        )
    assert TOKEN not in str(caught.value)
    assert TOKEN not in capsys.readouterr().err
    assert "[redacted]" in str(caught.value)


def test_authentication_failure_preserves_bearer_challenge(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, _, raw = failure("authentication_failed", retry="never", status=401)
    with pytest.raises(verifier.VerificationError) as caught:
        exercise(
            verifier,
            monkeypatch,
            [(status, {"www-authenticate": "Bearer"}, raw)],
        )
    assert '"www_authenticate":"Bearer"' in str(caught.value)


def test_credential_must_be_private_regular_owned_file(
    verifier: Any, tmp_path: Path
) -> None:
    credential = tmp_path / "credential"
    credential.write_text(TOKEN + "\n")
    credential.chmod(0o600)
    assert verifier.read_credential(credential) == TOKEN
    credential.chmod(0o644)
    with pytest.raises(verifier.VerificationError):
        verifier.read_credential(credential)
    credential.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(credential)
    with pytest.raises(verifier.VerificationError):
        verifier.read_credential(alias)


def test_credential_refuses_multiple_trailing_lines(
    verifier: Any, tmp_path: Path
) -> None:
    credential = tmp_path / "credential"
    credential.write_text(TOKEN + "\n\n")
    credential.chmod(0o600)
    with pytest.raises(verifier.VerificationError, match="one Cairn token"):
        verifier.read_credential(credential)


def test_only_numeric_loopback_endpoint_is_allowed(verifier: Any) -> None:
    assert (
        verifier.endpoint("http://127.0.0.1:8080/")
        == "http://127.0.0.1:8080/v1/retrieve"
    )
    for value in [
        "http://example.com",
        "http://user:secret@127.0.0.1",
        " http://127.0.0.1:8080",
        "http://[::1",
        "http://127.0.0.1:0",
        "http://127.0.0.1/elsewhere",
        "http://127.0.0.1?secret=value",
    ]:
        with pytest.raises(verifier.VerificationError):
            verifier.endpoint(value)


def test_real_request_posts_only_retrieval_with_auth_on_stdin(verifier: Any) -> None:
    body = json.dumps({"failure": {"code": "authentication_failed"}}).encode()
    with local_server([(401, {"X-Correlation-ID": FACT}, body)]) as (
        base_url,
        received,
    ):
        status, headers, returned = verifier.request(base_url, TOKEN, 2)

    assert status == 401
    assert headers["x-correlation-id"] == FACT
    assert returned == body
    assert len(received) == 1
    call = received[0]
    assert call["method"] == "POST"
    assert call["path"] == "/v1/retrieve"
    assert call["headers"]["Authorization"] == "Bearer " + TOKEN
    assert "Idempotency-Key" not in call["headers"]
    assert json.loads(call["body"]) == verifier.REQUEST


def test_real_request_does_not_follow_redirect(verifier: Any) -> None:
    with local_server([(200, {}, b"unexpected")]) as (destination, redirected):
        with local_server(
            [(302, {"Location": destination + "/capture"}, b"redirect refused")]
        ) as (origin, received):
            status, _, _ = verifier.request(origin, TOKEN, 2)

    assert status == 302
    assert len(received) == 1
    assert redirected == []


def test_real_request_ignores_environment_proxy(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = json.dumps(
        {"hits": [], "budget_consumed": 0, "budget_exhausted": False}
    ).encode()
    with local_server([(502, {}, b"proxy must not receive this")]) as (proxy, proxied):
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        with local_server([(200, {}, response)]) as (target, received):
            status, _, returned = verifier.request(target, TOKEN, 2)

    assert status == 200
    assert returned == response
    assert len(received) == 1
    assert proxied == []
    assert os.environ["http_proxy"] == proxy


def test_evidence_round_trip_checks_exact_utf8_and_digest(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    payload = "Cairn Attic check: café.\nExact second line.\n".encode()
    seen = []

    def request(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return (
            200,
            {},
            json.dumps(
                {
                    "evidence_id": FACT,
                    "payload": payload.decode(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_length": len(payload),
                    "media_type": "text/plain; charset=utf-8",
                }
            ).encode(),
        )

    monkeypatch.setattr(verifier, "request", request)
    result = verifier.verify(
        "http://127.0.0.1:8080", TOKEN, FACT, 5, 2, 3, evidence_payload=payload
    )
    assert result["status"] == "verified"
    assert result["evidence_id"] == FACT
    assert seen[0]["operation"] == "read-evidence"
    assert seen[0]["request_body"]["evidence_id"] == FACT
    assert "payload" not in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", "changed"),
        ("sha256", "0" * 64),
        ("byte_length", True),
        ("byte_length", 1),
        ("evidence_id", "22222222-2222-4222-8222-222222222222"),
        ("media_type", "text/html"),
    ],
)
def test_evidence_verification_refuses_mismatched_success(
    verifier: Any, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    import hashlib

    payload = b"synthetic evidence\n"
    response = {
        "evidence_id": FACT,
        "payload": payload.decode(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_length": len(payload),
        "media_type": "text/plain; charset=utf-8",
    }
    response[field] = value
    monkeypatch.setattr(
        verifier, "request", lambda *a, **kw: (200, {}, json.dumps(response).encode())
    )
    with pytest.raises(verifier.VerificationError, match="evidence"):
        verifier.verify(
            "http://127.0.0.1:8080", TOKEN, FACT, 5, 2, 3, evidence_payload=payload
        )


def test_evidence_pending_honours_retry_and_does_not_repeat_ingest(
    verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = [0.0]
    monkeypatch.setattr(verifier.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        verifier.time, "sleep", lambda n: elapsed.__setitem__(0, elapsed[0] + n)
    )
    seen = []

    def request(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["operation"])
        return failure("evidence_pending", delay="2")

    monkeypatch.setattr(verifier, "request", request)
    with pytest.raises(verifier.VerificationError, match="attempt limit"):
        verifier.verify(
            "http://127.0.0.1:8080",
            TOKEN,
            FACT,
            10,
            2,
            2,
            evidence_payload=b"synthetic",
        )
    assert elapsed[0] == 2
    assert seen == ["read-evidence", "read-evidence"]


@pytest.mark.parametrize(
    "code,status",
    [("evidence_corrupt", 500), ("index_pending", 503), ("not_found", 404)],
)
def test_evidence_refusals_are_not_indexing_delay(
    verifier: Any, monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    monkeypatch.setattr(
        verifier, "request", lambda *a, **kw: failure(code, status=status)
    )
    with pytest.raises(verifier.VerificationError, match="without retry"):
        verifier.verify(
            "http://127.0.0.1:8080", TOKEN, FACT, 5, 2, 3, evidence_payload=b"synthetic"
        )


@pytest.mark.parametrize(
    "payload",
    [b"a" * 1_048_576, b"\x01" * 1_048_576],
    ids=["ascii-limit", "escaped-limit"],
)
def test_evidence_http_response_allows_json_expansion(
    verifier: Any, payload: bytes
) -> None:
    import hashlib

    body = json.dumps(
        {
            "evidence_id": FACT,
            "payload": payload.decode(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_length": len(payload),
            "media_type": "text/plain; charset=utf-8",
        }
    ).encode()
    with local_server([(200, {}, body)]) as (url, received):
        result = verifier.verify(url, TOKEN, FACT, 10, 5, 1, evidence_payload=payload)
    assert result["status"] == "verified"
    assert received[0]["path"] == "/v1/read-evidence"
    assert json.loads(received[0]["body"]) == {
        "scope": verifier.REQUEST["scope"],
        "evidence_id": FACT,
    }
