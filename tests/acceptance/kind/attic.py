"""Read-only direct payload probe for the Task 9 Attic evidence checks."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def main(argv: list[str]) -> int:
    database, expected = argv[1], argv[2]
    uri = f"{Path(database).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection, connection:
        connection.execute("PRAGMA query_only = ON")
        found = connection.execute(
            "SELECT 1 FROM payloads WHERE payload = ? LIMIT 1",
            (expected.encode(),),
        ).fetchone()
    print("present" if found is not None else "absent", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
