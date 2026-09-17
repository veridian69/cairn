"""I-96 and I-97 through the wire, on both transports.

I-96: an ingest whose metadata and evidence payload quote the SHA-256 of
its own fact body — the migration envelope's shape — commits; the digest
is masked on the screening copy only, so the stored record keeps it.

I-97: the decoded evidence payload is screened by the pattern rules only.
Text that trips only the statistical rules commits as a payload and is
refused as a fact body — the priced residual and its boundary, pinned.
The pattern-rule refusal on the payload is EVIDENCE-04's scenario and
stays there.

Neither test carries a scenario ID: the §6.6 vocabulary is unchanged, and
these pin decision behaviour the way the leak sweep does, per I-90's
shared runner.
"""

import hashlib
import json
from pathlib import Path

import pytest
from conftest import REPO, Instance, ingest_body, serve

ENVELOPE_BODY = "DELTA-BODY-attic-turn"
ENVELOPE_DIGEST = hashlib.sha256(ENVELOPE_BODY.encode("utf-8")).hexdigest()

ENTROPY_ONLY_TEXT = (
    "the export token digest is "
    '"876131c8f232b000703776b76f1cfb1aca7fb11a8cc055cd360f413951144b64" '
    'and the marker is "R2c9k7Qw1Zx4Vb8Ln3Jm5Tp0Ys6Ue2Ia9Do4Hf1"'
)


@pytest.mark.anyio
async def test_the_migration_envelope_shape_commits(
    tmp_path: Path,
    transport: str,
) -> None:
    instance = Instance(tmp_path, "SCREEN-I96", attic=True)
    token = instance.add_actor(segments=[REPO], operations=["ingest"])
    payload = json.dumps(
        {"content": ENVELOPE_BODY, "content_sha256": ENVELOPE_DIGEST},
        sort_keys=True,
        separators=(",", ":"),
    )

    async with serve(instance, transport) as running:
        outcome = await running.client.ingest(
            ingest_body(
                facts=[{"body": ENVELOPE_BODY}],
                metadata={"content_sha256": ENVELOPE_DIGEST},
                evidence_payload=payload,
            ),
            credential=token,
        )

    assert outcome.outcome == "committed", outcome.text
    # The mask is screening-copy only: custody keeps the digest it was sent.
    assert ENVELOPE_DIGEST.encode() in instance.catalogue_bytes()


@pytest.mark.anyio
async def test_the_statistical_rules_stop_at_the_evidence_payload_boundary(
    tmp_path: Path,
    transport: str,
) -> None:
    instance = Instance(tmp_path, "SCREEN-I97", attic=True)
    token = instance.add_actor(segments=[REPO], operations=["ingest"])

    async with serve(instance, transport) as running:
        as_payload = await running.client.ingest(
            ingest_body(evidence_payload=ENTROPY_ONLY_TEXT),
            credential=token,
        )
        as_body = await running.client.ingest(
            ingest_body(facts=[{"body": ENTROPY_ONLY_TEXT}]),
            credential=token,
        )

    assert as_payload.outcome == "committed", as_payload.text
    assert as_body.failure_code == "secret_rejected"
    assert as_body.detail is not None
    assert as_body.detail["field_path"] == "facts[0].body"
