"""Report when one hostname converges on exactly one expected address."""

import socket
import sys
import time
from collections.abc import Iterable, Iterator


def converges(observations: Iterable[bool], required: int) -> bool:
    """Require consecutive exact observations, resetting on stale answers."""
    consecutive = 0
    for matched in observations:
        consecutive = consecutive + 1 if matched else 0
        if consecutive >= required:
            return True
    return False


def resolution_observations(
    host: str, expected: str, port: int, attempts: int, interval: float
) -> Iterator[bool]:
    """Yield bounded exact-answer observations while kube-dns endpoints settle."""
    for attempt in range(attempts):
        try:
            addresses = {
                answer[4][0]
                for answer in socket.getaddrinfo(
                    host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            }
        except socket.gaierror:
            addresses = set()
        yield addresses == {expected}
        if attempt + 1 < attempts:
            time.sleep(interval)


def main(argv: list[str]) -> int:
    host, expected, port = argv[1], argv[2], int(argv[3])
    required = int(argv[4]) if len(argv) > 4 else 1
    attempts = int(argv[5]) if len(argv) > 5 else required
    interval = float(argv[6]) if len(argv) > 6 else 0.0
    matched = converges(
        resolution_observations(host, expected, port, attempts, interval),
        required,
    )
    print("matched" if matched else "mismatched", flush=True)
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
