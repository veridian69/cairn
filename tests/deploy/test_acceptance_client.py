"""Slice 8 task 8: the two cross-selection modes the matrix reads.

P-68's cross-checks are the ones that would be *most* comfortable to get
wrong, because both of their honest answers look like success from a
distance: "instance B's token was refused" and "instance A's fixture is
not in instance B". A read that failed for any other reason — a refusal,
a timeout, a wrong shape — must never print either of those words, or
the run would record an isolation claim that nothing established.

That is the whole of what is tested here, against a real HTTP server on
localhost. The `ingest` and `audit` modes task 7 shipped are exercised
in-cluster against a real instance and are untouched.
"""

import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tests" / "acceptance" / "kind" / "client.py"

MUTATION = "3f6c0a1e-0000-4000-8000-000000000001"
OTHER_MUTATION = "3f6c0a1e-0000-4000-8000-000000000002"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location("client", SCRIPT)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


client = _load()


class _Handler(BaseHTTPRequestHandler):
    status: int = 200
    body: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's spelling
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = json.dumps(self.body).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Silent: a pristine test run is part of the evidence."""


@pytest.fixture
def instance_answering() -> Iterator[Any]:
    servers: list[ThreadingHTTPServer] = []

    def make(status: int, body: dict[str, Any]) -> str:
        handler = type("Handler", (_Handler,), {"status": status, "body": body})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("cairn1.token\n", encoding="utf-8")
    return path


def _events(*mutation_ids: str) -> dict[str, Any]:
    return {
        "events": [
            {"mutation_id": mutation_id, "action_code": "ingest"}
            for mutation_id in mutation_ids
        ]
    }


def _run(base_url: str, mode: str, subject: str, token_path: Path) -> int:
    code: Any = client.main(["client.py", mode, base_url, subject, str(token_path)])
    assert isinstance(code, int)
    return code


def test_a_fixture_from_another_instance_is_absent(
    instance_answering: Any, token_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_url = instance_answering(200, _events(OTHER_MUTATION))
    assert _run(base_url, "absent", MUTATION, token_path) == 0
    assert capsys.readouterr().out.strip() == "absent"


def test_a_fixture_that_is_there_is_reported_present(
    instance_answering: Any, token_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control on the check above. Without it, a mode that printed
    `absent` unconditionally would satisfy every cross-check there is."""
    base_url = instance_answering(200, _events(MUTATION))
    assert _run(base_url, "absent", MUTATION, token_path) == 1
    assert capsys.readouterr().out.strip() == "present"


def test_a_read_that_failed_is_never_reported_as_absence(
    instance_answering: Any, token_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The defect this module exists to refuse: a refused read and an
    absent fixture are not the same fact."""
    base_url = instance_answering(500, {})
    assert _run(base_url, "absent", MUTATION, token_path) == 1
    printed = capsys.readouterr().out
    assert printed.startswith("failed ")
    assert "HTTP 500" in printed


def test_a_foreign_credential_is_reported_by_its_status(
    instance_answering: Any, token_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-26's authentication failure is a 401, and the matrix predicts
    that code rather than "it did not work"."""
    base_url = instance_answering(401, {})
    assert _run(base_url, "denied", "-", token_path) == 0
    assert capsys.readouterr().out.strip() == "http-401"


def test_a_credential_that_authenticates_says_so(
    instance_answering: Any, token_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control on the check above, and the wrong answer the matrix is
    watching for: a foreign token that is *accepted* fails the run."""
    base_url = instance_answering(200, _events())
    assert _run(base_url, "denied", "-", token_path) == 0
    assert capsys.readouterr().out.strip() == "accepted"


def test_the_restore_fixture_submits_an_exact_evidence_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def post(
        _base_url: str,
        path: str,
        _token: str,
        payload: dict[str, Any],
        _headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        captured["path"] = path
        captured["payload"] = payload
        return {"mutation_receipt": {"mutation_id": MUTATION}}

    monkeypatch.setattr(client, "_post", post)

    assert (
        client._ingest("http://cairn", "token", "before backup", exact=True) == MUTATION
    )
    assert captured["path"] == "/v1/ingest"
    assert captured["payload"]["evidence_payload"] == "attic evidence before backup"


def test_restore_fixture_mode_requires_exact_evidence_before_success(
    monkeypatch: pytest.MonkeyPatch,
    token_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, bool] | tuple[str, str]] = []

    def ingest(_base_url: str, _token: str, marker: str, *, exact: bool = False) -> str:
        calls.append((marker, exact))
        return MUTATION

    def wait(_base_url: str, _token: str, marker: str) -> None:
        calls.append(("wait", marker))

    monkeypatch.setattr(client, "_ingest", ingest)
    monkeypatch.setattr(client, "_wait_for_fixture_read", wait)
    monkeypatch.setattr(client, "_require_audit_event", lambda *_args: None)

    assert _run("http://cairn", "restore-fixture", "before backup", token_path) == 0
    assert calls == [("before backup", True), ("wait", "before backup")]
    assert capsys.readouterr().out.strip() == f"ok {MUTATION}"


def test_restored_reads_use_the_rest_and_mcp_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def post(
        _base_url: str,
        path: str,
        _token: str,
        payload: dict[str, Any],
        _headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if path == "/v1/retrieve":
            calls.append(("rest", payload["query"]))
            return {"hits": [{"body": "before backup"}]}
        assert path == "/v1/mcp"
        calls.append(("mcp", payload["params"]["arguments"]["query"]))
        return {
            "result": {
                "isError": False,
                "structuredContent": {"hits": [{"body": "before backup"}]},
            }
        }

    monkeypatch.setattr(client, "_post", post)

    client._require_restored_reads("http://cairn", "token", "before backup")

    assert calls == [
        ("rest", '"attic evidence before backup"'),
        ("mcp", "before backup"),
    ]
