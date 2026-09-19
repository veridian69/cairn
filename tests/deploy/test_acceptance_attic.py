"""The Task 9 direct Attic probe asks the SQLite store itself."""

import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tests" / "acceptance" / "kind" / "attic.py"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "kind_acceptance_attic", SCRIPT
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_the_probe_distinguishes_a_stored_payload_from_an_absent_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "attic.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE payloads(payload BLOB NOT NULL) STRICT")
        connection.execute(
            "INSERT INTO payloads(payload) VALUES (?)", (b"attic evidence fixture",)
        )
    database.chmod(0o440)
    attic = _load()

    assert attic.main(["attic.py", str(database), "attic evidence fixture"]) == 0
    assert capsys.readouterr().out.strip() == "present"
    assert attic.main(["attic.py", str(database), "different payload"]) == 0
    assert capsys.readouterr().out.strip() == "absent"
