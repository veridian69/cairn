"""I-90's equivalence rule, asserted head to head.

The 54-scenario corpus proves each transport against the specification,
one transport at a time; two green runs establish that both surfaces
satisfy the corpus, not that they answered the same way where the corpus
did not look. This module closes that gap the only way it can be closed:
one interaction, two fresh instances, and a direct comparison of what
I-90 fixes as equivalent.

I-90's list is exact, and so is this: the stable failure code or success
outcome, the ``invalid_request`` rule identity and field path, the
``secret_rejected`` policy, rule and field path, the
committed-versus-replayed outcome, the audit action code, action kind,
outcome and chain kind of every event the interaction produced in the
order it produced them, and the catalogue side effects including their
absence. Nothing outside that list is compared, because everything
outside it is a per-run value and an equality assertion over one would
be a flake waiting for its first correlation identifier.

Each case also states what it is supposed to provoke, and both runs are
asserted against that statement. Equality alone is not proof: two
interactions that both quietly stopped doing anything would compare
equal, and the case that no longer reaches the behaviour it names is
exactly the one this module would otherwise stop noticing.
"""

import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import OTHER_REPO, REALM, REPO, Instance, ingest_body, serve
from transport import MCP, REST, OperationOutcome, TransportClient

from cairn.catalogue.sqlite import CATALOGUE_FILENAME

# One fixed key, so the replay case replays and the commit cases do not
# depend on the seam's own counter agreeing between two clients.
KEY = "dddddddd-1111-4111-8111-dddddddddddd"

# The detect-secrets example key, as ``tests/screening`` uses it: a
# published sample, never a live credential.
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"

# I-90's audit comparison, member by member. Deliberately not the whole
# event: an event's identifier, sequence, timestamp and hash are per-run
# values I-90 excludes by name.
AUDIT_MEMBERS = ("action_code", "action_kind", "chain_kind", "outcome")

# I-90's amendment of 11 August 2026, found by this module's first run:
# an authentication denial is written before any operation is identified
# on MCP (I-86 authenticates before the frame is parsed) but after the
# URL has named one on REST, so its action code and kind are what each
# transport truthfully knew and structurally cannot match. For exactly
# that event class the comparison exempts those two members and requires
# the granular denial reason code instead — both transports authenticate
# through the one shared ``authenticate_request``, so it must match.
PRE_IDENTIFICATION = "(pre-identification)"

Run = Callable[[TransportClient, str | None], Awaitable[list[OperationOutcome]]]


@dataclass(frozen=True, slots=True)
class Case:
    """One interaction, run identically on both transports.

    ``name`` is also the instance identity both runs derive, so the two
    instances differ in nothing a comparison could trip over. ``expect``
    is the interaction's own claim: one entry per call, naming the
    envelope outcome of a call that succeeded — ``read`` for a read — or
    the stable failure code of one that did not.
    """

    name: str
    run: Run
    expect: tuple[str, ...]


# One grant for every case, so the seeding recipe is shared rather than
# per case: direct catalogue rows, per I-63 and the harness recipe, since
# bootstrap is CLI-only and reaches neither surface.
GRANTED_OPERATIONS = ["ingest", "audit-read"]


async def _commit_then_replay(
    client: TransportClient, token: str | None
) -> list[OperationOutcome]:
    first = await client.ingest(ingest_body(), credential=token, idempotency_key=KEY)
    second = await client.ingest(ingest_body(), credential=token, idempotency_key=KEY)
    return [first, second]


async def _out_of_scope(
    client: TransportClient, token: str | None
) -> list[OperationOutcome]:
    return [
        await client.ingest(
            ingest_body(segments=[OTHER_REPO]), credential=token, idempotency_key=KEY
        )
    ]


async def _unknown_field(
    client: TransportClient, token: str | None
) -> list[OperationOutcome]:
    body = ingest_body()
    body["surplus"] = "not part of the request"
    return [await client.ingest(body, credential=token, idempotency_key=KEY)]


async def _secret(client: TransportClient, token: str | None) -> list[OperationOutcome]:
    return [
        await client.ingest(
            ingest_body(facts=[{"body": f"the key is {AWS_EXAMPLE_KEY}"}]),
            credential=token,
            idempotency_key=KEY,
        )
    ]


async def _unauthenticated(
    client: TransportClient, token: str | None
) -> list[OperationOutcome]:
    return [await client.ingest(ingest_body(), credential=None, idempotency_key=KEY)]


async def _commit_then_read(
    client: TransportClient, token: str | None
) -> list[OperationOutcome]:
    committed = await client.ingest(
        ingest_body(), credential=token, idempotency_key=KEY
    )
    page = await client.read_audit_events(
        {"realm_id": REALM, "scope_prefix": [REPO]}, credential=token
    )
    return [committed, page]


CASES: tuple[Case, ...] = (
    Case("XT-COMMIT-REPLAY", _commit_then_replay, ("committed", "replayed")),
    Case("XT-DENIED", _out_of_scope, ("authorisation_denied",)),
    Case("XT-INVALID", _unknown_field, ("invalid_request",)),
    Case("XT-SECRET", _secret, ("secret_rejected",)),
    Case("XT-UNAUTHENTICATED", _unauthenticated, ("authentication_failed",)),
    Case("XT-READ", _commit_then_read, ("committed", "read")),
)


def _audit_projection(event: dict[str, object]) -> dict[str, object]:
    """One event onto I-90's members, amendment included.

    The predicate is the amendment's own: an instance-chain ``deny``
    carrying no principal identity is a denial issued before any actor —
    and so before any operation — was established. Both sides of a
    comparison satisfy it or neither does, so a masked event can only
    ever be compared against a masked event.
    """
    projection = {member: event[member] for member in AUDIT_MEMBERS}
    if (
        event["chain_kind"] == "instance"
        and event["outcome"] == "deny"
        and event["principal_id"] is None
    ):
        projection["action_code"] = PRE_IDENTIFICATION
        projection["action_kind"] = PRE_IDENTIFICATION
        projection["reason_code"] = event["reason_code"]
    return projection


def comparable(outcome: OperationOutcome) -> dict[str, object]:
    """One answer reduced to what I-90 requires both transports to agree
    on, and to nothing else.

    The mutation receipt contributes its command digest alone. The digest
    is derived from the authority command rather than minted for the run
    (``authority/mutations.py:2451``), so it is the one receipt member
    that says something across transports: equal digests mean both
    surfaces translated their own argument shapes into the same command.
    The mutation identifier beside it is minted, and I-90 excludes it by
    name. The audit receipt contributes its chain identity for the same
    reason and by the same rule.

    ``read-audit-events`` is the one operation whose *result* is audit
    evidence, so it is compared on I-90's audit list rather than whole:
    the events it discloses carry the identifiers, sequences, timestamps
    and hashes I-90 excludes.
    """
    receipt = outcome.mutation_receipt
    audit = outcome.audit_receipt
    result = outcome.result
    disclosed: object = None
    if outcome.succeeded and result is not None and "events" in result:
        events = result["events"]
        assert isinstance(events, list), outcome.text
        disclosed = [_audit_projection(event) for event in events]
    return {
        "succeeded": outcome.succeeded,
        "outcome": outcome.outcome,
        "failure_code": outcome.failure_code,
        "detail": outcome.detail,
        "command_digest": None if receipt is None else receipt["command_digest"],
        "audit_chain": None
        if audit is None
        else {
            "chain_kind": audit["chain_kind"],
            "chain_identity": audit["chain_identity"],
        },
        "disclosed_audit_evidence": disclosed,
    }


def audit_evidence(instance: Instance) -> list[dict[str, object]]:
    """Every event the interaction appended, in the order it appended
    them, projected onto I-90's four members."""
    with sqlite3.connect(instance.data_path / CATALOGUE_FILENAME) as connection:
        rows = connection.execute(
            "SELECT canonical_event FROM audit_events ORDER BY rowid"
        ).fetchall()
    evidence: list[dict[str, object]] = []
    for row in rows:
        event = json.loads(row[0])
        assert type(event) is dict
        evidence.append(_audit_projection(event))
    return evidence


def side_effects(instance: Instance) -> dict[str, int]:
    """Every catalogue table's row count.

    Every table rather than the handful an interaction is expected to
    touch: "the same catalogue side effects, **including their absence**"
    is a statement about the tables nobody thought to look at, and a
    hand-written list of tables would only ever contain the ones somebody
    did.
    """
    with sqlite3.connect(instance.data_path / CATALOGUE_FILENAME) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        counts: dict[str, int] = {}
        for name in names:
            assert name.isidentifier(), name
            counts[name] = int(
                connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            )
    return counts


@dataclass(frozen=True, slots=True)
class Observation:
    answers: list[dict[str, object]]
    evidence: list[dict[str, object]]
    side_effects: dict[str, int]


async def observe(case: Case, tmp_path: Path, transport: str) -> Observation:
    root = tmp_path / transport
    root.mkdir()
    instance = Instance(root, case.name)
    token = instance.add_actor(segments=[REPO], operations=GRANTED_OPERATIONS)
    async with serve(instance, transport) as running:
        outcomes = await case.run(running.client, token)
    return Observation(
        [comparable(outcome) for outcome in outcomes],
        audit_evidence(instance),
        side_effects(instance),
    )


def claimed(observation: Observation) -> list[str]:
    """What a case's ``expect`` entry names, read off the comparable
    answers — which is all a comparison keeps: an answer that succeeded
    names its envelope outcome, or ``read`` where a read has none, and
    one that did not names its stable failure code."""
    named: list[str] = []
    for answer in observation.answers:
        if not answer["succeeded"]:
            code = answer["failure_code"]
            assert isinstance(code, str), answer
            named.append(code)
            continue
        outcome = answer["outcome"]
        named.append(outcome if isinstance(outcome, str) else "read")
    return named


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.anyio
async def test_both_transports_answer_one_interaction_identically(
    case: Case, tmp_path: Path
) -> None:
    rest = await observe(case, tmp_path, REST)
    mcp = await observe(case, tmp_path, MCP)

    for observation in (rest, mcp):
        assert tuple(claimed(observation)) == case.expect, observation.answers

    assert mcp.answers == rest.answers
    assert mcp.evidence == rest.evidence
    assert mcp.side_effects == rest.side_effects
