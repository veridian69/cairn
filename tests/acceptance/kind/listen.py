"""A TCP listener that says when it is listening.

The enforcement proof needs a target whose readiness is a fact rather
than a guess: a probe that arrives before the bind gets `refused`, which
is neither of the two answers the proof is allowed to draw a conclusion
from. Announcing the bind on stdout lets the harness wait for it.
"""

import socket
import sys


def hold_until_peer_closes(connection: socket.socket) -> None:
    """Keep the accepted side viable for a CONNECT tunnel handshake."""
    with connection:
        while connection.recv(1024):
            pass


def main(argv: list[str]) -> int:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", int(argv[1])))
    server.listen(8)
    print("listening", flush=True)
    while True:
        connection, _ = server.accept()
        hold_until_peer_closes(connection)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
