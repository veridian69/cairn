"""The fresh-session correction walkthrough runs verbatim and its check holds.

Every shell block in ``docs/fresh-session-correction-demo.md`` after the
prerequisites runs unchanged except for the port and the cleanup step, which
keeps a copy of the saved outputs. The instance is disposable and synthetic;
no provider, semantic backend or live service is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
GUIDE = ROOT / "docs" / "fresh-session-correction-demo.md"
CHECK = ROOT / "scripts" / "check_fresh_session_correction.py"
_spec = importlib.util.spec_from_file_location(
    "doc_blocks", ROOT / "scripts" / "doc_blocks.py"
)
assert _spec is not None and _spec.loader is not None
_doc_blocks = importlib.util.module_from_spec(_spec)
sys.modules["doc_blocks"] = _doc_blocks
_spec.loader.exec_module(_doc_blocks)

EXCUSED = {"Prerequisites (not timed)": "installs the environment and opens a shell"}
EXECUTED = [
    "1. Start a disposable instance",
    "2. Session A remembers a claim",
    "3. Session B starts fresh and recalls",
    "4. Session B corrects the belief",
    "5. Recall and history after the correction",
    "6. Run the check",
    "7. Clean up",
]
CLEANUP = 'rm -rf -- "$demo"'


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def walkthrough(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    blocks = _doc_blocks.shell_blocks(GUIDE.read_text(encoding="utf-8"))
    headings = [block.heading for block in blocks]
    assert headings == [*EXCUSED, *EXECUTED], "every block is executed or excused"
    body = "\n".join(block.body for block in blocks if block.heading in EXECUTED)
    assert body.count("port=8123\n") == 1 and body.count(CLEANUP) == 1
    root = tmp_path_factory.mktemp("correction-demo")
    kept = root / "kept"
    body = body.replace("port=8123\n", f"port={_free_port()}\n").replace(
        CLEANUP, f'cp -R -- "$demo/out" "{kept}" || true; {CLEANUP}'
    )
    script = root / "walkthrough.sh"
    script.write_text(body + "\n", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CAIRN_", "OPENAI_"))
    }
    environment["PATH"] = (
        f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    )
    environment["TMPDIR"] = str(root)
    completed = subprocess.run(
        ["bash", "-euo", "pipefail", str(script)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    leftovers = [p.name for p in root.iterdir() if p.name.startswith("cairn-")]
    assert leftovers == [], "cleanup must remove the disposable instance"
    return completed.stdout, kept


def _check(directory: Path) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, str(CHECK), str(directory)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.returncode, json.loads(completed.stdout)


def test_walkthrough_passes_its_check(walkthrough: tuple[str, Path]) -> None:
    stdout, kept = walkthrough
    assert '"status": "passed"' in stdout
    assert '"previous_visit_unknown"' in stdout
    code, result = _check(kept)
    assert code == 0 and result["status"] == "passed"
    assert len(result["checks"]) == 5


def _load(directory: Path, name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((directory / f"{name}.json").read_text())
    return document


def _old_fact(directory: Path) -> dict[str, Any]:
    history = _load(directory, "b-history")["result"]["data"]
    return next(fact for fact in history["facts"] if fact["invalidated_at"])


def _reuse_session(result: dict[str, Any], kept: Path) -> None:
    session = _load(kept, "a-remember")["result"]["session_id"]
    result["visit"]["snapshot"]["session_id"] = session


def _recall_old(result: dict[str, Any], kept: Path) -> None:
    result["data"]["hits"].append(_old_fact(kept))


def _drop_link(result: dict[str, Any], kept: Path) -> None:
    result["data"]["corrections"] = []


def _drop_old(result: dict[str, Any], kept: Path) -> None:
    old = _old_fact(kept)["fact_id"]
    result["data"]["facts"] = [
        f for f in result["data"]["facts"] if f["fact_id"] != old
    ]


def _uncommitted(result: dict[str, Any], kept: Path) -> None:
    result["persistence"]["status"] = "prepared"


def _second_mutation(result: dict[str, Any], kept: Path) -> None:
    result["status"] = "committed"


@pytest.mark.parametrize(
    ("name", "tamper", "reason"),
    [
        ("a-remember", _uncommitted, "custody not committed"),
        ("b-arrive", _reuse_session, "session B reused session A"),
        ("b-recall", _recall_old, "still returns the invalidated belief"),
        ("b-history", _drop_link, "correction link"),
        ("b-history", _drop_old, "lacks the old or new belief"),
        ("b-correct-replay", _second_mutation, "not a replay"),
    ],
)
def test_check_rejects_broken_evidence(
    walkthrough: tuple[str, Path],
    tmp_path: Path,
    name: str,
    tamper: Callable[[dict[str, Any], Path], None],
    reason: str,
) -> None:
    _, kept = walkthrough
    copy = tmp_path / "out"
    shutil.copytree(kept, copy)
    document = _load(copy, name)
    tamper(document["result"], kept)
    (copy / f"{name}.json").write_text(json.dumps(document))
    code, result = _check(copy)
    assert code == 1 and result["status"] == "failed"
    assert reason in result["reason"]
