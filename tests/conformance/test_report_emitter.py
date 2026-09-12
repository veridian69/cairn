"""The §6.6 report emitter's own proofs.

The report is a slice deliverable, not a convenience: it is what states
which scenarios ran, against which build, contract and corpus. Nothing
else in the suite exercises it — a conformance run producing a wrong or
over-claiming report would look exactly like a clean one — so its shape,
its determinism and every way it can record an outcome are pinned here.

The outcome hook is driven directly rather than through a nested pytest
run: it is a wrapper hookimpl, so the test advances the generator to its
yield, sends a stubbed report and reads the return value. The stubs are
test-local per the repository's no-test-package convention.
"""

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import conftest as emitter
import pytest
from conftest import (
    APPLICABLE,
    INAPPLICABLE,
    instance_uuid,
    pytest_runtest_makereport,
    pytest_sessionfinish,
    render_report,
)
from transport import REST

from cairn.transports.mcp.manifest import packaged_manifest_bytes
from cairn.transports.v1.wire import CONTRACT_IDENTITY

COMMIT = "0123456789abcdef0123456789abcdef01234567"
VERSION = "0.1.0.dev1+g0123456"


class _Marker:
    def __init__(self, scenario_id: str) -> None:
        self.args = (scenario_id,)


class _CallSpec:
    """The transport parameterisation the emitter reads a run off."""

    def __init__(self, transport: str) -> None:
        self.params = {"transport": transport}


class _Item:
    """Stands in for a pytest item carrying (or lacking) a scenario mark."""

    def __init__(self, scenario_id: str | None, transport: str = REST) -> None:
        self._marker = None if scenario_id is None else _Marker(scenario_id)
        self.callspec = _CallSpec(transport)
        self.nodeid = f"tests/conformance/test_x.py::test_y[{transport}]"

    def get_closest_marker(self, name: str) -> _Marker | None:
        assert name == "scenario"
        return self._marker


class _Report:
    def __init__(
        self,
        *,
        when: str,
        failed: bool = False,
        skipped: bool = False,
        wasxfail: str | None = None,
    ) -> None:
        self.when = when
        self.failed = failed
        self.skipped = skipped
        # Set only when pytest would set it: the emitter reads its
        # presence, so a stub that always carried it would prove nothing.
        if wasxfail is not None:
            self.wasxfail = wasxfail
        self.nodeid = f"tests/conformance/test_x.py::test_y[{when}]"


def drive(item: _Item, report: _Report) -> None:
    """Runs the wrapper hook over one stubbed report."""
    generator = pytest_runtest_makereport(item, None)  # type: ignore[arg-type]
    next(generator)
    try:
        generator.send(report)  # type: ignore[arg-type]
    except StopIteration:
        return
    raise AssertionError("the wrapper hook did not stop after its yield")


def outcomes_for(*, failing: frozenset[str] = frozenset()) -> dict[str, Any]:
    return {
        scenario_id: {
            "outcome": "failed" if scenario_id in failing else "passed",
            "evidence": f"tests/conformance/test_x.py::test_{scenario_id}",
        }
        for scenario_id in APPLICABLE
    }


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Swaps the module-level outcome store and report directory, so a
    test cannot corrupt the outcomes of the real run it is part of."""
    monkeypatch.setattr(emitter, "_outcomes", {})
    monkeypatch.setattr(emitter, "REPORT_DIR", tmp_path / "conformance")
    return emitter.report_path(REST)


def test_the_report_is_byte_identical_across_renders() -> None:
    outcomes = outcomes_for()
    first = render_report(
        outcomes, transport=REST, git_commit=COMMIT, product_version=VERSION
    )
    second = render_report(
        outcomes, transport=REST, git_commit=COMMIT, product_version=VERSION
    )
    assert first == second
    assert first.endswith("}\n")


def test_the_report_carries_every_field_the_contract_requires() -> None:
    """Spec §6.6: build, contract and corpus hashes, non-secret instance
    identity, transport, scenario outcomes and evidence."""
    document = json.loads(
        render_report(
            outcomes_for(), transport=REST, git_commit=COMMIT, product_version=VERSION
        )
    )

    assert document["report"] == "cairn.conformance/v1"
    assert document["transport"] == REST
    assert document["build"] == {"git_commit": COMMIT, "product_version": VERSION}
    assert document["contract"]["identity"] == CONTRACT_IDENTITY
    assert len(document["contract"]["digest"]) == 64
    assert (
        document["contract"]["mcp_contract_digest"]
        == sha256(packaged_manifest_bytes()).hexdigest()
    )
    assert document["corpus"]["path"] == "tests/screening/secret-corpus.json"
    assert len(document["corpus"]["digest"]) == 64
    assert [entry["id"] for entry in document["scenarios"]] == list(APPLICABLE)
    for entry in document["scenarios"]:
        assert entry["outcome"] == "passed"
        assert entry["evidence"].startswith("tests/conformance/")
        # The instance identity is derived from the scenario ID, so it
        # discloses nothing about the host and is stable across runs.
        assert entry["instance_id"] == str(instance_uuid(entry["id"]))
    assert document["inapplicable"] == [
        {"id": scenario_id, "reason": reason} for scenario_id, reason in INAPPLICABLE
    ]
    assert document["summary"] == {
        "applicable": 54,
        "passed": 54,
        "failed": 0,
        "inapplicable": 4,
    }


def test_a_failed_scenario_is_reported_and_counted() -> None:
    document = json.loads(
        render_report(
            outcomes_for(failing=frozenset({"AUTH-03", "MUT-07"})),
            transport=REST,
            git_commit=COMMIT,
            product_version=VERSION,
        )
    )

    failures = {
        entry["id"] for entry in document["scenarios"] if entry["outcome"] == "failed"
    }
    assert failures == {"AUTH-03", "MUT-07"}
    assert document["summary"]["failed"] == 2
    assert document["summary"]["passed"] == 52


def test_the_report_never_claims_an_inapplicable_scenario_ran() -> None:
    document = json.loads(
        render_report(
            outcomes_for(), transport=REST, git_commit=COMMIT, product_version=VERSION
        )
    )

    ran = {entry["id"] for entry in document["scenarios"]}
    recorded_inapplicable = {entry["id"] for entry in document["inapplicable"]}
    assert ran.isdisjoint(recorded_inapplicable)
    assert len(ran | recorded_inapplicable) == 58


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        pytest.param(_Report(when="call"), "passed", id="clean-call"),
        pytest.param(_Report(when="call", failed=True), "failed", id="failing-call"),
        pytest.param(
            _Report(when="call", skipped=True),
            "failed",
            id="skip-is-never-a-pass",
        ),
        pytest.param(_Report(when="setup", failed=True), "failed", id="setup-error"),
        pytest.param(
            _Report(when="teardown", failed=True), "failed", id="teardown-error"
        ),
    ],
)
def test_the_hook_records_the_outcome(
    isolated: Path, report: _Report, expected: str
) -> None:
    """P-31 admits passed, failed or inapplicable-with-reason only. A
    skip and an error in either non-call phase are all failures: none of
    them established the scenario."""
    drive(_Item("AUTH-01"), report)

    assert emitter._outcomes[(REST, "AUTH-01")]["outcome"] == expected


def test_an_unexpected_pass_is_not_a_pass(isolated: Path) -> None:
    """I-90 admits no expected failure and no waiver. A non-strict
    ``xfail`` that unexpectedly passes reaches this hook as a plain pass
    carrying ``wasxfail``, and recording it as passed would put a
    scenario nobody expected to work into a clean conformance report."""
    drive(_Item("AUTH-01"), _Report(when="call", wasxfail="recorded by pytest"))

    assert emitter._outcomes[(REST, "AUTH-01")]["outcome"] == "failed"


def test_a_clean_phase_never_overwrites_a_recorded_failure(isolated: Path) -> None:
    drive(_Item("AUTH-01"), _Report(when="call", failed=True))
    drive(_Item("AUTH-01"), _Report(when="call"))

    assert emitter._outcomes[(REST, "AUTH-01")]["outcome"] == "failed"


def test_a_setup_pass_alone_records_nothing(isolated: Path) -> None:
    """Only the call phase can establish a pass; a passing setup must not
    mark a scenario that has not run."""
    drive(_Item("AUTH-01"), _Report(when="setup"))

    assert emitter._outcomes == {}


def test_an_unmarked_test_is_not_recorded(isolated: Path) -> None:
    drive(_Item(None), _Report(when="call"))

    assert emitter._outcomes == {}


def test_a_partial_run_emits_no_report(isolated: Path) -> None:
    """A report covering some scenarios would read as a clean matrix that
    silently omitted the rest."""
    drive(_Item("AUTH-01"), _Report(when="call"))

    pytest_sessionfinish(None, 0)  # type: ignore[arg-type]

    assert not isolated.exists()


def test_a_run_that_establishes_nothing_removes_a_stale_report(
    isolated: Path,
) -> None:
    """An earlier run's report must not survive a run that did not
    re-establish it, including one naming a transport this build no
    longer declares. Left in place, a clean matrix from a previous build
    sits in the same directory as a current one and is
    indistinguishable from evidence this build produced."""
    stale = emitter.report_path("mcp")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"transport": "mcp"}\n', encoding="utf-8")
    isolated.write_text('{"transport": "rest"}\n', encoding="utf-8")

    drive(_Item("AUTH-01"), _Report(when="call"))
    pytest_sessionfinish(None, 0)  # type: ignore[arg-type]

    assert not stale.exists()
    assert not isolated.exists()


def test_a_complete_run_emits_the_rendered_report(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emitter, "_git_commit", lambda: COMMIT)
    monkeypatch.setattr(emitter, "__version__", VERSION)
    for scenario_id in APPLICABLE:
        drive(_Item(scenario_id), _Report(when="call"))

    pytest_sessionfinish(None, 0)  # type: ignore[arg-type]

    written = isolated.read_text(encoding="utf-8")
    recorded = {
        scenario_id: emitter._outcomes[(REST, scenario_id)]
        for scenario_id in APPLICABLE
    }
    assert written == render_report(
        recorded, transport=REST, git_commit=COMMIT, product_version=VERSION
    )
    assert json.loads(written)["summary"]["passed"] == 54


def test_one_transports_completion_emits_only_its_own_report(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-59's dimension: outcomes are recorded per transport, and a
    transport whose corpus is incomplete gets no report — a run that
    emitted its partner's coverage under its own name would claim a
    surface it never drove."""
    monkeypatch.setattr(emitter, "TRANSPORTS", (REST, "mcp"))
    monkeypatch.setattr(emitter, "_git_commit", lambda: COMMIT)
    monkeypatch.setattr(emitter, "__version__", VERSION)
    stale = emitter.report_path("mcp")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"transport": "mcp"}\n', encoding="utf-8")
    for scenario_id in APPLICABLE:
        drive(_Item(scenario_id), _Report(when="call"))
    drive(_Item("AUTH-01", transport="mcp"), _Report(when="call"))

    pytest_sessionfinish(None, 0)  # type: ignore[arg-type]

    assert json.loads(isolated.read_text(encoding="utf-8"))["transport"] == REST
    # The partner transport ran one scenario of fifty-four, and the
    # report claiming it had run them all is gone rather than left.
    assert not stale.exists()
