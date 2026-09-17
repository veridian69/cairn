"""Slice 8 task 8: the gateway CONNECT probe's vocabulary.

The isolation matrix's gateway entries are only worth what their
refusals are worth, and every one of them is decided by how this script
reads a proxy's answer. A probe that reported one word for every failure
would report a gateway that is *down* exactly as it reports a gateway
that is *denying* — which is the failure mode P-68 exists to refuse, one
tier up: an isolation claim a broken cluster satisfies for free.

So the parsing is tested here, against a real socket rather than a
mocked one. Nothing in this module needs a cluster, a proxy image or a
network: the fake proxy is a listener on localhost that answers with the
status line the test names, which is the whole of what squid's answer
means to the probe.
"""

import importlib.util
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tests" / "acceptance" / "kind" / "connect.py"


def _load() -> ModuleType:
    """The acceptance scripts are programs, not a package: they are
    mounted into a pod by path and run by `python`. Loading by path is
    how a test reaches one without inventing an import root the cluster
    would not use."""
    specification = importlib.util.spec_from_file_location("connect", SCRIPT)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


connect = _load()


class FakeProxy:
    """One connection, one answer, and a record of what was asked.

    ``answer`` is the raw bytes to send back; ``None`` closes the
    connection without saying anything, which is what a proxy that dies
    mid-request looks like from the client side.
    """

    def __init__(self, answer: bytes | None, hold: float = 0.0) -> None:
        self._answer = answer
        self._hold = hold
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port: int = self._server.getsockname()[1]
        self.request = bytearray()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _ = self._server.accept()
        with connection:
            connection.settimeout(5.0)
            while b"\r\n\r\n" not in self.request:
                received = connection.recv(1024)
                if not received:
                    break
                self.request.extend(received)
            time.sleep(self._hold)
            if self._answer is not None:
                connection.sendall(self._answer)

    def close(self) -> None:
        self._thread.join(timeout=5.0)
        self._server.close()


@pytest.fixture
def proxy_answering() -> Iterator[Callable[[bytes | None], FakeProxy]]:
    proxies: list[FakeProxy] = []

    def make(answer: bytes | None) -> FakeProxy:
        proxy = FakeProxy(answer)
        proxies.append(proxy)
        return proxy

    yield make
    for proxy in proxies:
        proxy.close()


def _attempt(port: int, target: str = "api.openai.com:443") -> str:
    host, _, target_port = target.partition(":")
    outcome: Any = connect.attempt("127.0.0.1", port, host, int(target_port), 5.0)
    assert isinstance(outcome, str)
    return outcome


ProxyFactory = Callable[[bytes | None], FakeProxy]


def test_a_two_hundred_is_an_established_tunnel(proxy_answering: ProxyFactory) -> None:
    """The one positive the allow-list is supposed to produce."""
    proxy = proxy_answering(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert _attempt(proxy.port) == "established"


def test_a_four_oh_three_is_a_refusal_by_the_allow_list(
    proxy_answering: ProxyFactory,
) -> None:
    """squid's answer to `http_access deny all`, and the only refusal the
    matrix may read as the gateway having decided."""
    proxy = proxy_answering(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    assert _attempt(proxy.port, "blocked.example.invalid:443") == "forbidden"


def test_any_other_status_is_reported_as_itself(proxy_answering: ProxyFactory) -> None:
    """A gateway that cannot reach the allow-listed host answers 503, and
    that is not a refusal — reading it as one would let an outage pass as
    enforcement."""
    proxy = proxy_answering(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
    assert _attempt(proxy.port) == "status:503"


def test_a_proxy_that_says_nothing_is_not_a_refusal(
    proxy_answering: ProxyFactory,
) -> None:
    proxy = proxy_answering(None)
    assert _attempt(proxy.port) == "truncated"


def test_the_request_is_a_connect_for_the_named_target(
    proxy_answering: ProxyFactory,
) -> None:
    """The ACL squid matches on is the CONNECT target, so a probe that
    sent a different one would be testing a different rule."""
    proxy = proxy_answering(b"HTTP/1.1 200 Connection established\r\n\r\n")
    _attempt(proxy.port, "api.openai.com:443")
    proxy.close()
    lines = bytes(proxy.request).decode("ascii").split("\r\n")
    assert lines[0] == "CONNECT api.openai.com:443 HTTP/1.1"
    assert "Host: api.openai.com:443" in lines


def test_a_proxy_that_never_answers_is_reported_as_a_timeout() -> None:
    """The failure a gateway under load produces, and the one that must
    not arrive dressed as anything else: a bare `error:` word would send
    a reader looking for a network fault that is not there."""
    proxy = FakeProxy(b"HTTP/1.1 200 Connection established\r\n\r\n", hold=1.0)
    try:
        host, port = "api.openai.com", 443
        outcome: Any = connect.attempt("127.0.0.1", proxy.port, host, port, 0.2)
        assert outcome == "timeout"
    finally:
        proxy.close()


def test_no_proxy_at_all_is_distinguished_from_a_refusal() -> None:
    """`probe.py`'s vocabulary, kept: a gateway that is not there is not a
    gateway that said no."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    assert _attempt(port) == "refused"
