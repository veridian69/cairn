"""The fake provider remains a viable CONNECT target after accept."""

import importlib.util
import socket
import threading
import time
from pathlib import Path
from types import ModuleType

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tests" / "acceptance" / "kind" / "listen.py"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location("listen", SCRIPT)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


listen = _load()


def test_an_accepted_connection_stays_open_until_its_peer_closes() -> None:
    accepted, peer = socket.socketpair()
    serving = threading.Thread(
        target=listen.hold_until_peer_closes,
        args=(accepted,),
        daemon=True,
    )
    serving.start()
    time.sleep(0.05)
    assert serving.is_alive()
    peer.close()
    serving.join(timeout=1.0)
    assert not serving.is_alive()
