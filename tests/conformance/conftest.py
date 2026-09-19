"""The conformance harness (P-31, spec §6.6).

One shared harness for the 54 applicable scenarios — a deliberate,
Operator-approved departure from the repository's no-conftest convention,
confined to this package: 54 scenarios share one fresh-instance and
seeding shape, and duplicating it per file would bury the scenarios
under their own plumbing.

Each scenario runs against a dedicated fresh instance: its own tmp
directory, its own migrated catalogue, its own deterministic instance
identity derived from the scenario ID. Seeding uses direct catalogue
rows — bootstrap is CLI-only and structurally unreachable through any
transport per I-63, so the first principal, credential and grant cannot
arrive over any surface; the recipe is
``tests/transports/rest/v1/test_routes.py``'s.

P-59: the corpus is transport-parameterised rather than copied. The
``transport`` fixture runs each scenario once per entry in
``TRANSPORTS``, against its own fresh instance, and ``transport.py``
owns everything that differs between them. Nothing here and nothing in
a scenario body may name a path, a header or a status code.

The report emitter also lives here (P-23): a ``pytest_runtest_makereport``
hook records each scenario's outcome under the transport it ran on, and
``pytest_sessionfinish`` writes ``build/conformance/report-<transport>.json``
— build, contract and corpus hashes, transport, per-scenario outcome,
instance identity and evidence pointer, and the inapplicable IDs with
P-31's reasons — for each transport whose 54 applicable scenarios all
ran. A partial selection emits nothing: a report that silently omitted
scenarios would claim a coverage it does not have.

Nothing here may ever mark a scenario skipped: P-31 admits passed,
failed, or inapplicable-with-reason only.
"""

import hashlib
import json
import sqlite3
import subprocess
import threading
from collections.abc import AsyncGenerator, Generator, Iterable
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from transport import TRANSPORTS, OperationOutcome, TransportClient, build_client

from cairn import __version__
from cairn.authority.credentials import mint_token
from cairn.catalogue.migration import migrate_catalogue
from cairn.catalogue.sqlite import (
    CATALOGUE_FILENAME,
    _open_write_connection,
    canonical_timestamp,
)
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.projection.adapter import IndexAdapter
from cairn.projection.delivery import deliver_projection_outbox
from cairn.runtime.config import (
    AtticConfig,
    CairnConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)
from cairn.transports.mcp.manifest import packaged_manifest_bytes
from cairn.transports.rest.v1.openapi import packaged_contract_bytes
from cairn.transports.v1.wire import CONTRACT_IDENTITY

# --- The P-31 applicability partition -----------------------------------

APPLICABLE: tuple[str, ...] = (
    *(f"AUTH-{n:02d}" for n in range(1, 8)),
    *(f"SCOPE-{n:02d}" for n in range(1, 9)),
    *(f"TRUST-{n:02d}" for n in range(1, 7)),
    *(f"MUT-{n:02d}" for n in range(1, 11)),
    *(f"GRANT-{n:02d}" for n in range(1, 11)),
    *(f"INDEX-{n:02d}" for n in range(1, 5)),
    *(f"EVIDENCE-{n:02d}" for n in range(1, 5)),
    *(f"AUDIT-{n:02d}" for n in range(1, 6)),
)

_BOOT_REASON = (
    "remains the slice 3 procedural proof, re-referenced; bootstrap is "
    "CLI-only and deliberately non-networked per I-63"
)

INAPPLICABLE: tuple[tuple[str, str], ...] = (
    *((f"BOOT-{n:02d}", _BOOT_REASON) for n in range(1, 5)),
)

# P-46: slice 6 moves the fifteen retrieval-shaped scenarios from
# INAPPLICABLE to APPLICABLE. Only the four BOOT scenarios remain, for the
# reason they always had.
assert len(APPLICABLE) == 54
assert len(INAPPLICABLE) == 4

# --- Shared constants ---------------------------------------------------

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
TS = canonical_timestamp(NOW)
FUTURE = datetime(2027, 1, 1, tzinfo=UTC)
FUTURE_TS = canonical_timestamp(FUTURE)
REALM = "acme"
OTHER_REALM = "umbra"
REPO = {"kind": "repository", "identifier": "acme-repo"}
OTHER_REPO = {"kind": "repository", "identifier": "other-repo"}
COMPONENT = {"kind": "component", "identifier": "ingestion"}

_REPO_ROOT = Path(__file__).parents[2]
CORPUS_PATH = _REPO_ROOT / "tests" / "screening" / "secret-corpus.json"
REPORT_DIR = _REPO_ROOT / "build" / "conformance"


def report_path(transport: str) -> Path:
    """One report per transport: the document names a single transport,
    so two transports are two documents rather than one that averages
    them."""
    return REPORT_DIR / f"report-{transport}.json"


def instance_uuid(scenario_id: str) -> UUID:
    """A deterministic, RFC 4122 version-4-shaped instance identity per
    scenario, so the emitted report is stable across runs."""
    digest = hashlib.sha256(scenario_id.encode("utf-8")).digest()[:16]
    return UUID(bytes=digest, version=4)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(params=TRANSPORTS)
def transport(request: pytest.FixtureRequest) -> str:
    """P-59's parameterisation: every scenario runs once per transport,
    each against its own fresh instance."""
    transport_id = request.param
    assert isinstance(transport_id, str)
    return transport_id


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Clock:
    """An injectable clock the harness can advance — AUTH-04 and AUTH-05
    need artefacts that expire mid-scenario."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Instance:
    """One dedicated fresh Cairn instance: config, migrated catalogue,
    seeded realms, and direct-row seeding helpers returning bearer
    tokens."""

    def __init__(
        self,
        tmp_path: Path,
        scenario_id: str,
        *,
        attic: bool = False,
        index: IndexAdapter | None = None,
    ) -> None:
        data = tmp_path / "data"
        credentials = tmp_path / "credentials"
        data.mkdir(exist_ok=True)
        credentials.mkdir(exist_ok=True)
        self.clock = Clock()
        self.config = CairnConfig(
            schema_version="cairn.config/v1",
            instance_id=instance_uuid(scenario_id),
            mode="test",
            http=HttpConfig(host="127.0.0.1", port=8000),
            paths=PathConfig(data=data, credentials=credentials),
            attic=AtticConfig(enabled=attic),
            graphiti=GraphitiConfig(enabled=index is not None),
        )
        # The P-38 seam: positive scenarios take the deterministic
        # in-memory adapter, INDEX-01-04 take the hostile one, and a
        # scenario with no retrieval takes none at all.
        self.index = index
        migrate_catalogue(self.config, self.clock)
        self._counter = 0
        with _open_write_connection(self.data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for realm in (REALM, OTHER_REALM):
                connection.execute(
                    "INSERT INTO realms (realm_id, created_at) VALUES (?, ?)",
                    (realm, TS),
                )
                connection.execute(
                    "INSERT INTO audit_heads "
                    "(chain_kind, chain_identity, last_sequence, last_hash) "
                    "VALUES ('realm', ?, 0, ?)",
                    (realm, bytes(32)),
                )
            connection.commit()

    @property
    def data_path(self) -> Path:
        return self.config.paths.data

    def _next_uuid(self) -> UUID:
        self._counter += 1
        return UUID(f"{self._counter:08x}-0000-4000-8000-00000000abcd")

    def add_principal(self, *, label: str | None = None) -> UUID:
        principal_id = self._next_uuid()
        if label is None:
            label = f"conformance-actor-{self._counter}"
        with _open_write_connection(self.data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO principals (principal_id, kind, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (str(principal_id), "human", label, TS),
            )
            connection.commit()
        return principal_id

    def add_credential(
        self,
        principal_id: UUID,
        *,
        expires_at: str | None = None,
    ) -> str:
        credential_id = self._next_uuid()
        minted = mint_token(
            credential_id,
            lambda count: bytes((self._counter + i) % 256 for i in range(count)),
        )
        with _open_write_connection(self.data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO credentials "
                "(credential_id, principal_id, verifier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(credential_id),
                    str(principal_id),
                    minted.verifier,
                    TS,
                    expires_at,
                ),
            )
            connection.commit()
        return minted.text

    def add_grant(
        self,
        principal_id: UUID,
        *,
        segments: list[dict[str, str]],
        operations: list[str],
        realm: str = REALM,
        read_clearance: str = "restricted",
        write_classifications: list[str] | None = None,
        delegable_operations: list[str] | None = None,
        expires_at: str | None = FUTURE_TS,
    ) -> UUID:
        grant_id = self._next_uuid()
        classifications = (
            write_classifications
            if write_classifications is not None
            else ["internal", "public", "restricted"]
        )
        with _open_write_connection(self.data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO grants (grant_id, principal_id, realm_id, "
                "scope_segments, operations, read_clearance, "
                "write_classifications, delegable_operations, issued_by, "
                "expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(grant_id),
                    str(principal_id),
                    realm,
                    _canonical(
                        [{"id": s["identifier"], "kind": s["kind"]} for s in segments]
                    ),
                    _canonical(sorted(operations)),
                    read_clearance,
                    _canonical(sorted(classifications)),
                    None
                    if delegable_operations is None
                    else _canonical(sorted(delegable_operations)),
                    None,
                    expires_at,
                    TS,
                ),
            )
            connection.commit()
        return grant_id

    def add_actor(
        self,
        *,
        segments: list[dict[str, str]],
        operations: list[str],
        realm: str = REALM,
        read_clearance: str = "restricted",
        write_classifications: list[str] | None = None,
        grant_expires_at: str | None = FUTURE_TS,
    ) -> str:
        """Principal + credential + one grant in one call — the common
        scenario preamble — returning the bearer token."""
        principal_id = self.add_principal()
        token = self.add_credential(principal_id)
        self.add_grant(
            principal_id,
            segments=segments,
            operations=operations,
            realm=realm,
            read_clearance=read_clearance,
            write_classifications=write_classifications,
            expires_at=grant_expires_at,
        )
        return token

    def revoke_grant_row(self, grant_id: UUID) -> None:
        with _open_write_connection(self.data_path, create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO grant_revocations "
                "(grant_id, revoked_at, revoked_by, reason_code) "
                "VALUES (?, ?, NULL, ?)",
                (str(grant_id), TS, "conformance_setup"),
            )
            connection.commit()

    def application(self) -> FastAPI:
        from cairn.runtime.composition import build_application

        return build_application(
            self.config, clock=self.clock, index_adapter=self.index
        )

    def drain_projection(self) -> None:
        """Run the projection deliverer to completion, synchronously.

        The background loop does this in a served instance, but on its own
        cadence; a scenario asserting on retrieval needs the index current
        at a known point, and waiting on a timer would make every
        retrieval scenario a race. The loop has its own lifecycle tests.
        """
        assert self.index is not None
        transactions = CatalogueTransactions(
            self.data_path,
            writer_gate=threading.Lock(),
            clock=self.clock,
            uuid_factory=lambda: self._next_uuid(),
        )
        while True:
            report = deliver_projection_outbox(
                transactions, self.index, clock=self.clock, limit=500
            )
            if report.remaining == 0:
                return
            assert report.delivered > 0, "projection delivery made no progress"

    # --- Catalogue inspection ------------------------------------------

    def catalogue_bytes(self) -> bytes:
        return (self.data_path / CATALOGUE_FILENAME).read_bytes()

    def events(self, chain_kind: str) -> list[dict[str, object]]:
        with (
            closing(sqlite3.connect(self.data_path / CATALOGUE_FILENAME)) as connection,
            connection,
        ):
            rows = connection.execute(
                "SELECT canonical_event FROM audit_events "
                "WHERE chain_kind = ? ORDER BY sequence",
                (chain_kind,),
            ).fetchall()
        documents: list[dict[str, object]] = []
        for row in rows:
            document = json.loads(row[0])
            assert type(document) is dict
            documents.append(document)
        return documents

    def count(self, table: str) -> int:
        assert table.isidentifier()
        with (
            closing(sqlite3.connect(self.data_path / CATALOGUE_FILENAME)) as connection,
            connection,
        ):
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])


class Running:
    """A started instance: ``client`` drives the eleven operations over
    the run's transport, ``http`` reaches the foundation surface.

    ``http`` exists for ``/metrics`` alone, which is not an operation and
    is served identically whichever transport a scenario is running on.
    No scenario may reach an operation through it: that is the seam's
    whole point.
    """

    def __init__(self, http: AsyncClient, client: TransportClient) -> None:
        self.http = http
        self.client = client


@asynccontextmanager
async def serve(instance: Instance, transport: str) -> AsyncGenerator[Running]:
    """``async with serve(instance, transport) as running:`` — lifespan,
    ASGI client and the transport's own operation client."""
    application = instance.application()
    async with LifespanManager(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://conformance"
        ) as http:
            yield Running(http, build_client(transport, http))


INGEST_BODY: dict[str, object] = {
    "scope": {"realm": REALM, "segments": [REPO]},
    "classification": "internal",
    "source_type": "agent-claim",
    "facts": [{"body": "The deploy pipeline uses kaniko."}],
}


def hits(outcome: OperationOutcome) -> list[Any]:
    """A retrieval's hits, in the order the surface returned them."""
    assert outcome.result is not None, outcome.text
    found = outcome.result["hits"]
    assert isinstance(found, list)
    return found


def bodies(outcome: OperationOutcome) -> set[str]:
    """The hit bodies, asserted to be text rather than coerced to it: a
    wrongly typed value must fail the scenario, not stringify into
    agreement with what the scenario expected."""
    found = set()
    for hit in hits(outcome):
        body = hit["body"]
        assert isinstance(body, str), hit
        found.add(body)
    return found


def ingest_body(
    *,
    realm: str = REALM,
    segments: list[dict[str, str]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    body = dict(INGEST_BODY)
    body["scope"] = {
        "realm": realm,
        "segments": segments if segments is not None else [REPO],
    }
    body.update(overrides)
    return body


# --- Outcome collection and the report emitter --------------------------

# Keyed by (transport, scenario ID): the same scenario runs once per
# transport, and one report per transport states what that transport
# established.
_outcomes: dict[tuple[str, str], dict[str, str]] = {}


def _git_commit() -> str:
    """The Makefile's REVISION convention: ``git rev-parse HEAD``, with
    ``unknown`` outside a git checkout."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError:
        return "unknown"
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and revision else "unknown"


def render_report(
    outcomes: dict[str, dict[str, str]],
    *,
    transport: str,
    git_commit: str,
    product_version: str,
) -> str:
    """The §6.6 machine-readable report, rendered deterministically —
    sorted keys, two-space indent, trailing newline, the ``openapi.py``
    discipline — so two runs of the same build over the same outcomes are
    byte-identical."""
    scenarios = [
        {
            "id": scenario_id,
            "instance_id": str(instance_uuid(scenario_id)),
            "outcome": outcomes[scenario_id]["outcome"],
            "evidence": outcomes[scenario_id]["evidence"],
        }
        for scenario_id in APPLICABLE
    ]
    failed = sum(1 for entry in scenarios if entry["outcome"] == "failed")
    document = {
        "report": "cairn.conformance/v1",
        "transport": transport,
        "build": {"git_commit": git_commit, "product_version": product_version},
        # Both I-89 digests, on every report: a run states which published
        # contract *and* which published tool manifest it was measured
        # against, whichever surface it drove.
        "contract": {
            "identity": CONTRACT_IDENTITY,
            "digest": hashlib.sha256(packaged_contract_bytes()).hexdigest(),
            "mcp_contract_digest": hashlib.sha256(
                packaged_manifest_bytes()
            ).hexdigest(),
        },
        "corpus": {
            "path": "tests/screening/secret-corpus.json",
            "digest": hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest(),
        },
        "scenarios": scenarios,
        "inapplicable": [
            {"id": scenario_id, "reason": reason}
            for scenario_id, reason in INAPPLICABLE
        ],
        "summary": {
            "applicable": len(APPLICABLE),
            "passed": len(scenarios) - failed,
            "failed": failed,
            "inapplicable": len(INAPPLICABLE),
        },
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Records a scenario's outcome from every phase, not just the call.

    A scenario is ``passed`` only when its call phase passed outright.
    Anything else is ``failed``, and that includes three cases the
    obvious version of this hook would launder into a pass: an error in
    setup or teardown, where the scenario never ran to completion but its
    call phase may report nothing; a skip, which pytest reports as
    neither failed nor passed; and an expectation of failure, where a
    non-strict ``xfail`` that unexpectedly passes arrives as a plain pass
    carrying ``wasxfail``. P-31 and spec §6.6 admit passed, failed or
    inapplicable-with-reason, and I-90 admits no expected failure and no
    waiver, so the emitter refuses to represent one rather than trusting
    nobody will write one. A failure already recorded is never
    overwritten by a later pass.
    """
    report = yield
    marker = item.get_closest_marker("scenario")
    if marker is None:
        return report
    scenario_id = str(marker.args[0])
    if report.failed or report.skipped or hasattr(report, "wasxfail"):
        outcome = "failed"
    elif report.when == "call":
        outcome = "passed"
    else:
        return report
    key = (transport_of(item), scenario_id)
    if _outcomes.get(key, {}).get("outcome") == "failed":
        outcome = "failed"
    _outcomes[key] = {"outcome": outcome, "evidence": report.nodeid}
    return report


def transport_of(item: pytest.Item) -> str:
    """The transport a scenario ran on, from its parameterisation.

    Asserted rather than defaulted: a scenario that reached the emitter
    without the ``transport`` fixture ran against no declared transport,
    and recording it under a guessed one would put an unproven row in a
    report that exists to state exactly what was proven.
    """
    callspec = getattr(item, "callspec", None)
    assert callspec is not None, item.nodeid
    transport = callspec.params.get("transport")
    assert isinstance(transport, str), item.nodeid
    return transport


def pytest_testnodedown(node: object, error: object) -> None:
    """xdist controller: fold a finished worker's outcomes into ours.

    Under ``-n auto`` each worker records only the scenarios it ran, so
    no single process sees a complete corpus and the emitter below would
    write nothing — and then prune the reports a previous run left,
    silently. Workers therefore ship their ``_outcomes`` through
    ``workeroutput`` and the controller merges them here, preserving the
    failed-is-sticky rule, before its own ``pytest_sessionfinish`` emits.
    Serial runs never call this hook and are unchanged.
    """
    workeroutput = getattr(node, "workeroutput", None)
    if not workeroutput:
        return
    for transport, scenario_id, outcome, evidence in workeroutput.get(
        "conformance_outcomes", []
    ):
        key = (transport, scenario_id)
        if _outcomes.get(key, {}).get("outcome") == "failed":
            outcome = "failed"
        _outcomes[key] = {"outcome": outcome, "evidence": evidence}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    workeroutput = getattr(getattr(session, "config", None), "workeroutput", None)
    if workeroutput is not None:
        # xdist worker: report emission belongs to the controller, which
        # alone sees every worker's outcomes. Ship ours and write nothing
        # — a worker must never prune reports either.
        workeroutput["conformance_outcomes"] = [
            [transport, scenario_id, record["outcome"], record["evidence"]]
            for (transport, scenario_id), record in sorted(_outcomes.items())
        ]
        return
    written: set[Path] = set()
    for transport in TRANSPORTS:
        ran = {
            scenario_id for recorded, scenario_id in _outcomes if recorded == transport
        }
        if ran != set(APPLICABLE):
            continue
        outcomes = {
            scenario_id: _outcomes[(transport, scenario_id)]
            for scenario_id in APPLICABLE
        }
        path = report_path(transport)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_report(
                outcomes,
                transport=transport,
                git_commit=_git_commit(),
                product_version=__version__,
            ),
            encoding="utf-8",
        )
        written.add(path)
    # The directory holds what this run established and nothing else.
    # A report left from an earlier run — a transport this build no
    # longer declares, or one whose corpus did not complete this time —
    # is indistinguishable from evidence this run produced, and would sit
    # beside a current report reading as current. Declining to overwrite
    # is not enough; the stale claim has to go.
    if REPORT_DIR.is_dir():
        for stale in sorted(REPORT_DIR.glob("report-*.json")):
            if stale not in written:
                stale.unlink()


def scenario(scenario_id: str) -> pytest.MarkDecorator:
    """``@scenario("AUTH-01")`` — asserts the ID is applicable and tags
    the test for outcome collection."""
    assert scenario_id in APPLICABLE, scenario_id
    return pytest.mark.scenario(scenario_id)


def positives() -> Iterable[dict[str, str]]:
    """The corpus positives, for the leak-negative sweep."""
    document = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    for entry in document["entries"]:
        if entry["verdict"] == "positive":
            yield {"rule": str(entry["rule"]), "content": str(entry["content"])}
