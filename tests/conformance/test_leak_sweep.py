"""The I-32 leak-negative sweep.

Every corpus positive is driven through every screened field of every
`/v1` operation — the custody fields I-74 names per command and the
boundary addressing fields P-27 walks — on the run's transport, against
one dedicated instance with the Attic enabled. Afterwards the sweep
asserts absence of every positive's content from a full dump of both
SQLite files (catalogue including the outbox tables, and the Attic),
from the captured safe logs, and from the rendered metrics.

No corpus content may appear in a test name, a parametrisation ID or an
assertion message: the sweep therefore runs as one test that accumulates
leak descriptions naming only the rule identity and the sink, and every
request must be refused — a corpus positive that commits anywhere is
itself a failure.

One slot carries a ruled carve-out. I-97 screens the decoded evidence
payload with the pattern rules only, so a positive that trips nothing but
the statistical rules would now commit through the `evidence_payload`
slot — the priced residual, pinned as admitted behaviour by
`test_screen_matrix.py` rather than swept here. Those positives skip that
one slot and every other slot still refuses them; a positive any pattern
rule catches is still driven through the payload slot and must still be
refused there.
"""

import sqlite3
from pathlib import Path

import pytest
from conftest import (
    REALM,
    REPO,
    Instance,
    ingest_body,
    positives,
    serve,
)
from transport import Body, OperationOutcome, TransportClient

from cairn.catalogue.sqlite import CATALOGUE_FILENAME
from cairn.screening import SecretScreen
from cairn.screening.policy import STATISTICAL_RULES

ATTIC_FILENAME = "attic.sqlite3"
UNKNOWN_UUID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

Slot = tuple[str, str]


def _grant_body(
    content_realm: str, segments: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "realm_id": REALM,
        "grant": {
            "principal_id": UNKNOWN_UUID,
            "realm_id": content_realm,
            "segments": segments,
            "operations": ["ingest"],
            "read_clearance": "internal",
            "write_classifications": ["internal"],
            "expires_at": "2026-12-01T00:00:00+00:00",
        },
    }


def build_request(slot: Slot, content: str, live_fact_id: str) -> tuple[str, Body]:
    """(operation, body) with the corpus content injected into the slot's
    field. The 26 slots cover every screened field of every operation:
    the ten I-70 commands' custody text fields and boundary addressing
    fields. ``instance`` takes no caller input and has no slot.

    ``promote`` and ``invalidate`` address a real, live fact rather than
    an unknown identifier, and that is load-bearing rather than tidiness:
    both commands resolve their ``fact_ids`` and deny ``fact_unknown``
    before the transaction callback the custody screen runs inside, so an
    unknown identifier refuses every request for the wrong reason and
    leaves ``promote``'s ``reason`` and ``external_uri`` and
    ``invalidate``'s ``reason`` — three of the twenty-six slots — never
    screened at all. Proven, not assumed: with a real fact the sweep goes
    red when those screen calls are removed, and with an unknown one it
    stays green."""
    route, field = slot
    segment = {"kind": "repository", "identifier": "acme-repo"}
    if route == "ingest":
        if field == "scope.realm":
            return "ingest", ingest_body(realm=content)
        if field == "scope.segments[0].kind":
            return (
                "ingest",
                ingest_body(segments=[{"kind": content, "identifier": "acme-repo"}]),
            )
        if field == "scope.segments[0].identifier":
            return (
                "ingest",
                ingest_body(segments=[{"kind": "repository", "identifier": content}]),
            )
        if field == "facts[0].body":
            return "ingest", ingest_body(facts=[{"body": content}])
        if field == "metadata":
            return "ingest", ingest_body(metadata={"note": content})
        assert field == "evidence_payload"
        return "ingest", ingest_body(evidence_payload=content)
    if route == "promote":
        body: dict[str, object] = {
            "fact_ids": [live_fact_id],
            "evidence": {
                "external_uri": "https://ci.example.org/run/1",
                "payload_digest": "0" * 64,
            },
            "target_scope": {"realm": REALM, "segments": [dict(segment)]},
            "reason": "sweep",
        }
        if field == "target_scope.realm":
            body["target_scope"] = {"realm": content, "segments": [dict(segment)]}
        elif field == "target_scope.segments[0].kind":
            body["target_scope"] = {
                "realm": REALM,
                "segments": [{"kind": content, "identifier": "acme-repo"}],
            }
        elif field == "target_scope.segments[0].identifier":
            body["target_scope"] = {
                "realm": REALM,
                "segments": [{"kind": "repository", "identifier": content}],
            }
        elif field == "reason":
            body["reason"] = content
        else:
            assert field == "evidence.external_uri"
            body["evidence"] = {
                "external_uri": content,
                "payload_digest": "0" * 64,
            }
        return "promote", body
    if route == "invalidate":
        assert field == "reason"
        return (
            "invalidate",
            {"fact_ids": [live_fact_id], "reason": content},
        )
    if route == "create-principal":
        if field == "realm_id":
            return (
                "create-principal",
                {"realm_id": content, "kind": "human", "label": "sweep"},
            )
        assert field == "label"
        return (
            "create-principal",
            {"realm_id": REALM, "kind": "human", "label": content},
        )
    if route == "issue-credential":
        assert field == "realm_id"
        return (
            "issue-credential",
            {"realm_id": content, "principal_id": UNKNOWN_UUID},
        )
    if route == "revoke-credential":
        if field == "realm_id":
            return (
                "revoke-credential",
                {
                    "realm_id": content,
                    "credential_id": UNKNOWN_UUID,
                    "reason_code": "sweep",
                },
            )
        assert field == "reason_code"
        return (
            "revoke-credential",
            {
                "realm_id": REALM,
                "credential_id": UNKNOWN_UUID,
                "reason_code": content,
            },
        )
    if route == "create-grant":
        if field == "realm_id":
            body = _grant_body(REALM, [dict(segment)])
            body["realm_id"] = content
            return "create-grant", body
        if field == "grant.realm_id":
            return "create-grant", _grant_body(content, [dict(segment)])
        if field == "grant.segments[0].kind":
            return (
                "create-grant",
                _grant_body(REALM, [{"kind": content, "identifier": "acme-repo"}]),
            )
        assert field == "grant.segments[0].identifier"
        return (
            "create-grant",
            _grant_body(REALM, [{"kind": "repository", "identifier": content}]),
        )
    if route == "revoke-grant":
        if field == "realm_id":
            return (
                "revoke-grant",
                {
                    "realm_id": content,
                    "grant_id": UNKNOWN_UUID,
                    "reason_code": "sweep",
                },
            )
        assert field == "reason_code"
        return (
            "revoke-grant",
            {"realm_id": REALM, "grant_id": UNKNOWN_UUID, "reason_code": content},
        )
    assert route == "read-audit-events"
    if field == "realm_id":
        return (
            "read-audit-events",
            {"realm_id": content, "scope_prefix": [dict(segment)]},
        )
    if field == "scope_prefix[0].kind":
        return (
            "read-audit-events",
            {
                "realm_id": REALM,
                "scope_prefix": [{"kind": content, "identifier": "acme-repo"}],
            },
        )
    assert field == "scope_prefix[0].identifier"
    return (
        "read-audit-events",
        {
            "realm_id": REALM,
            "scope_prefix": [{"kind": "repository", "identifier": content}],
        },
    )


SLOTS: tuple[Slot, ...] = (
    ("ingest", "scope.realm"),
    ("ingest", "scope.segments[0].kind"),
    ("ingest", "scope.segments[0].identifier"),
    ("ingest", "facts[0].body"),
    ("ingest", "metadata"),
    ("ingest", "evidence_payload"),
    ("promote", "target_scope.realm"),
    ("promote", "target_scope.segments[0].kind"),
    ("promote", "target_scope.segments[0].identifier"),
    ("promote", "reason"),
    ("promote", "evidence.external_uri"),
    ("invalidate", "reason"),
    ("create-principal", "realm_id"),
    ("create-principal", "label"),
    ("issue-credential", "realm_id"),
    ("revoke-credential", "realm_id"),
    ("revoke-credential", "reason_code"),
    ("create-grant", "realm_id"),
    ("create-grant", "grant.realm_id"),
    ("create-grant", "grant.segments[0].kind"),
    ("create-grant", "grant.segments[0].identifier"),
    ("revoke-grant", "realm_id"),
    ("revoke-grant", "reason_code"),
    ("read-audit-events", "realm_id"),
    ("read-audit-events", "scope_prefix[0].kind"),
    ("read-audit-events", "scope_prefix[0].identifier"),
)


async def call(
    client: TransportClient, operation: str, body: Body, credential: str
) -> OperationOutcome:
    """One operation name to one seam method, written out rather than
    reflected: an unknown name is a table error, and ``getattr`` would
    turn it into a missing attribute at the far end of a long sweep."""
    match operation:
        case "ingest":
            return await client.ingest(body, credential=credential)
        case "promote":
            return await client.promote(body, credential=credential)
        case "invalidate":
            return await client.invalidate(body, credential=credential)
        case "create-principal":
            return await client.create_principal(body, credential=credential)
        case "issue-credential":
            return await client.issue_credential(body, credential=credential)
        case "revoke-credential":
            return await client.revoke_credential(body, credential=credential)
        case "create-grant":
            return await client.create_grant(body, credential=credential)
        case "revoke-grant":
            return await client.revoke_grant(body, credential=credential)
        case "read-audit-events":
            return await client.read_audit_events(body, credential=credential)
    raise AssertionError(operation)


def _pattern_refused(content: str) -> bool:
    """Whether the payload slot still refuses this positive under I-97: at
    least one non-statistical rule fires on it. Derived from the rule
    partition over a fully screened field, not from the payload path's own
    behaviour — using the exemption to predict the exemption would prove
    nothing."""
    findings = SecretScreen().screen("facts[0].body", content)
    return any(finding.rule not in STATISTICAL_RULES for finding in findings)


def _tokens(content: str) -> list[str]:
    """The searchable fragments of a positive: whitespace-delimited runs
    of at least 16 characters — ``test_policy.py``'s convention — falling
    back to the longest single line for anything with no such run.

    Never returns an empty list. An entry with nothing to search for
    would make the absence assertion below vacuously true for that entry,
    which is the one way this sweep could report a clean result it never
    established; the caller asserts the invariant so a future corpus
    addition cannot quietly opt itself out of the proof."""
    long_tokens = [token for token in content.split() if len(token) >= 16]
    if long_tokens:
        return long_tokens
    return [max(content.splitlines() or [content], key=len)]


@pytest.mark.anyio
async def test_every_corpus_positive_leaves_no_trace_anywhere(
    tmp_path: Path,
    transport: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = Instance(tmp_path, "LEAK-SWEEP", attic=True)
    principal_id = instance.add_principal(label="sweep-actor")
    token = instance.add_credential(principal_id)
    instance.add_grant(
        principal_id,
        segments=[],
        operations=["ingest", "retrieve", "promote", "invalidate", "audit-read"],
    )
    instance.add_grant(
        principal_id,
        segments=[],
        operations=["grant-manage"],
        delegable_operations=["ingest"],
    )
    entries = list(positives())
    assert len(entries) >= 32, "one positive per rule identity at minimum"

    committed: list[tuple[str, str]] = []
    async with serve(instance, transport) as running:
        client = running.client
        # A live fact for the promote and invalidate slots to address, and
        # the sweep's own positive control: this benign ingest must commit,
        # or every refusal below is a broken harness rather than the screen.
        seeded = await client.ingest(ingest_body(segments=[REPO]), credential=token)
        assert seeded.result is not None, "the sweep's control ingest must commit"
        seeded_ids = seeded.result["fact_ids"]
        assert isinstance(seeded_ids, list)
        live_fact_id = str(seeded_ids[0])
        payload_slot_skips = 0
        for entry in entries:
            for slot in SLOTS:
                if slot == ("ingest", "evidence_payload") and not _pattern_refused(
                    entry["content"]
                ):
                    # I-97: a statistical-only positive now commits through
                    # this one slot; its admission is pinned in
                    # ``test_screen_matrix.py``, not swept here.
                    payload_slot_skips += 1
                    continue
                operation, body = build_request(slot, entry["content"], live_fact_id)
                outcome = await call(client, operation, body, token)
                if outcome.succeeded:
                    committed.append((entry["rule"], f"{slot[0]}:{slot[1]}"))
        assert payload_slot_skips, "the I-97 carve-out must actually be exercised"
        assert payload_slot_skips < len(entries), (
            "some positive must still reach the payload slot"
        )
        metrics_response = await running.http.get("/metrics")

    assert not committed, f"corpus positives were accepted at {sorted(set(committed))}"
    # The fact the promote and invalidate slots addressed is still live and
    # unpromoted: every one of those attempts was refused.
    assert instance.count("facts") == 1
    assert instance.count("fact_invalidations") == 0

    catalogue_blob = instance.catalogue_bytes()
    attic_path = instance.data_path / ATTIC_FILENAME
    attic_blob = attic_path.read_bytes() if attic_path.exists() else b""
    with sqlite3.connect(instance.data_path / CATALOGUE_FILENAME) as connection:
        outbox_rows = connection.execute(
            "SELECT COUNT(*) FROM evidence_outbox"
        ).fetchone()[0]
        projection_rows = connection.execute(
            "SELECT COUNT(*) FROM projection_outbox"
        ).fetchone()[0]
    captured = capsys.readouterr()
    metrics_text = metrics_response.text

    leaks: list[tuple[str, str]] = []
    for entry in entries:
        searched = _tokens(entry["content"])
        assert searched, f"nothing searchable for {entry['rule']}"
        for token_text in searched:
            token_bytes = token_text.encode("utf-8")
            for sink, blob in (
                ("catalogue", catalogue_blob),
                ("attic", attic_blob),
            ):
                if token_bytes in blob:
                    leaks.append((entry["rule"], sink))
            for sink, text in (
                ("stdout-log", captured.out),
                ("stderr-log", captured.err),
                ("metrics", metrics_text),
            ):
                if token_text in text:
                    leaks.append((entry["rule"], sink))
    assert not leaks, f"corpus content found in {sorted(set(leaks))}"
    # The sweep's only committed write is the benign control: the outbox
    # tables hold nothing carrying swept content.
    assert outbox_rows == 0
    assert projection_rows <= 1
