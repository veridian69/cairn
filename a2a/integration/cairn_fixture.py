"""Serve a disposable, real Cairn for the Garden cross-language acceptance test.

The parent receives synthetic credentials over a private pipe. No configuration
or credentials are discovered from the host. Closing stdin shuts the server down.
"""

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "transports" / "memory"))

from memory_support import SCOPE, Instance  # noqa: E402

from cairn.catalogue.sqlite import _open_write_connection  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="garden-cairn-fixture-") as directory:
        instance = Instance(Path(directory), attic=False)
        alice, alice_token = instance.add_actor(operations=["retrieve", "ingest"])
        bob, bob_token = instance.add_actor(operations=["retrieve", "ingest"])
        reader, reader_token = instance.add_actor(operations=["retrieve"])
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(
                uvicorn.Config(
                    instance.application(),
                    log_config=None,
                    log_level="critical",
                    access_log=False,
                )
            )
            worker = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            worker.start()
            try:
                deadline = time.monotonic() + 15
                while not server.started:
                    if not worker.is_alive() or time.monotonic() > deadline:
                        raise RuntimeError("disposable Cairn did not start")
                    time.sleep(0.01)
                print(
                    json.dumps(
                        {
                            "endpoint": (
                                f"http://127.0.0.1:{listener.getsockname()[1]}"
                                "/memory/v1/diagnose"
                            ),
                            "instance_id": str(instance.config.instance_id),
                            "scope": SCOPE,
                            "classification": "internal",
                            "actors": {
                                "alice": {"id": str(alice), "token": alice_token},
                                "bob": {"id": str(bob), "token": bob_token},
                                "reader": {"id": str(reader), "token": reader_token},
                            },
                        }
                    ),
                    flush=True,
                )
                for line in sys.stdin:
                    command = json.loads(line)
                    if command == {"revoke": "bob"}:
                        with _open_write_connection(
                            instance.data_path, create=False
                        ) as connection:
                            connection.execute(
                                "INSERT INTO credential_revocations "
                                "(credential_id, revoked_at, reason_code) "
                                "SELECT credential_id, created_at, 'fixture_revocation' "
                                "FROM credentials WHERE principal_id = ?",
                                (str(bob),),
                            )
                            connection.commit()
                        print('{"revoked":"bob"}', flush=True)
                    else:
                        raise ValueError("unsupported fixture command")
            finally:
                server.should_exit = True
                worker.join(timeout=10)
                if worker.is_alive():
                    raise RuntimeError("disposable Cairn did not stop")


if __name__ == "__main__":
    main()
