"""One connection attempt, reported as what actually happened.

P-68's rule is that every isolation claim is an attempted connection with
a predicted outcome, so the vocabulary this prints has to distinguish the
ways a connection can fail. A policy that drops packets and a port with
no listener are both "it did not connect", and treating them as one word
would let a probe aimed at the wrong port pass as proof of isolation.

Run inside the Cairn image, which is where the standard library this uses
comes from — a second image for a socket call would be a second thing to
build, load and pin.
"""

import errno
import socket
import sys


def attempt(host: str, port: int, timeout: float) -> str:
    connection = socket.socket()
    connection.settimeout(timeout)
    try:
        connection.connect((host, port))
    except TimeoutError:
        # Silently dropped. This is what an enforced default-deny looks
        # like from the inside, and it is the only failure that counts as
        # policy having refused the connection.
        return "blocked"
    except socket.gaierror:
        return "unresolved"
    except OSError as error:
        if error.errno == errno.ECONNREFUSED:
            return "refused"
        return f"error:{errno.errorcode.get(error.errno or 0, str(error.errno))}"
    else:
        return "connected"
    finally:
        connection.close()


def main(argv: list[str]) -> int:
    print(attempt(argv[1], int(argv[2]), float(argv[3])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
