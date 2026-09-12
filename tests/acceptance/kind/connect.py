"""One HTTP CONNECT through the egress gateway, reported as what happened.

I-93's gateway is an allow-list, and an allow-list is worth exactly what
its refusals are worth. That makes the vocabulary here the load-bearing
part: "the proxy opened a tunnel", "the proxy refused this destination"
and "the proxy was not reachable at all" are three different facts, and
a script that printed one word for the last two would let a gateway that
is simply down pass as a gateway enforcing perfectly.

The transport-level words are `probe.py`'s, unchanged and for the same
reason. The proxy-level ones are added because a CONNECT has an answer a
bare socket does not.

Run inside the Cairn image, whose standard library is where this comes
from. Nothing is sent through the tunnel even when one opens: P-68's
gateway entries are connectivity claims, and the live provider round
trip is P-70's.
"""

import errno
import socket
import sys

READ_LIMIT = 8192


def attempt(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    timeout: float,
) -> str:
    connection = socket.socket()
    connection.settimeout(timeout)
    try:
        connection.connect((proxy_host, proxy_port))
    except TimeoutError:
        return "blocked"
    except socket.gaierror:
        return "unresolved"
    except OSError as error:
        if error.errno == errno.ECONNREFUSED:
            return "refused"
        return f"error:{errno.errorcode.get(error.errno or 0, str(error.errno))}"
    try:
        # The ACL squid matches is `dstdomain` against this authority, so
        # it is written once and sent in both places a proxy may read it.
        authority = f"{target_host}:{target_port}"
        request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n"
        connection.sendall(request.encode("ascii"))
        status_line = _status_line(connection)
    except TimeoutError:
        # The proxy took the connection and never answered. Distinct from
        # every word above: those are facts about reaching the gateway,
        # and this is a fact about the gateway itself.
        return "timeout"
    except OSError as error:
        return f"error:{errno.errorcode.get(error.errno or 0, str(error.errno))}"
    finally:
        connection.close()
    if not status_line:
        return "truncated"
    fields = status_line.split()
    if len(fields) < 2 or not fields[1].isdigit():
        return "unparseable"
    status = int(fields[1])
    if status == 200:
        return "established"
    if status == 403:
        return "forbidden"
    return f"status:{status}"


def _status_line(connection: socket.socket) -> str:
    """The first line of the answer, and no more of it than that.

    A tunnel that opens carries whatever the caller sends next, so
    reading past the status line would block on a peer that is waiting
    for us.
    """
    received = bytearray()
    while b"\r\n" not in received and len(received) < READ_LIMIT:
        block = connection.recv(1024)
        if not block:
            break
        received.extend(block)
    line, separator, _ = bytes(received).partition(b"\r\n")
    if not separator:
        return ""
    return line.decode("ascii", "replace")


def main(argv: list[str]) -> int:
    print(attempt(argv[1], int(argv[2]), argv[3], int(argv[4]), float(argv[5])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
