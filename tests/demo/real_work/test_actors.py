import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "transports" / "memory"))

from memory_support import Instance  # noqa: E402

from scripts.real_work_demo import actors  # noqa: E402


def test_grants_match_the_spec_table() -> None:
    assert actors.GRANTS == {
        "val": ("ingest", "retrieve"),
        "spike": ("ingest", "retrieve"),
        "verifier": ("ingest", "promote", "retrieve"),
        "spike-cold": ("retrieve",),
    }


def test_catalogue_holds_exactly_those_grants(tmp_path: Path) -> None:
    instance = Instance(tmp_path / "cairn", attic=True)
    made = actors.create_actors(instance, tmp_path)
    with closing(sqlite3.connect(instance.data_path / "catalogue.sqlite3")) as con:
        rows = con.execute(
            "SELECT principal_id, operations, scope_segments FROM grants"
        ).fetchall()
    stored = {p: tuple(sorted(json.loads(ops))) for p, ops, _ in rows}
    assert all(
        json.loads(s) == [{"id": "deepdiff", "kind": "repository"}] for _, _, s in rows
    )
    assert actors.DEMO_SCOPE == {
        "realm": "acme",
        "segments": [{"kind": "repository", "identifier": "deepdiff"}],
    }
    for name, actor in made.items():
        assert stored[str(actor.principal)] == actors.GRANTS[name]
        assert actor.token_path.read_text().strip() == actor.token
        assert actor.token_path.stat().st_mode & 0o777 == 0o600
    assert not any("invalidate" in ops for ops in stored.values())
