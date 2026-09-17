"""P-74/P-75/P-78: the deterministic mapping and the
``cairn-migration-plan/v1`` artefact.

``map`` is **pure**: the same ``cairn-legacy-export/v1`` bundle in gives a
byte-identical plan out. Nothing here reads a clock, a random source or an
environment variable, because the plan's determinism is what makes the
migration replayable — a re-run derives the same idempotency keys, and I-27
replays them instead of duplicating.

The mapping itself is the gate record's approved §5 made executable, with no
per-record judgement anywhere: a rule reads a field and returns a value.
Where a determination was left open, it is a named constant here rather than
an inference.

**Privacy (P-78).** The plan carries legacy bodies and therefore lives beside
the bundle in the operator's private directory, never in this repository. Only
``operations.jsonl`` holds content; the rejection and reconciliation files
carry rule identities and legacy identifiers alone, so the report built from
them in ``report.py`` is safe to commit.

**Validation is the server's own, not a copy of it.** Every planned request
is admitted through ``IngestRequest`` (the strict ``/v1`` wire model),
``IngestAssertion`` with its ``FactDraft`` and ``Scope`` invariants,
``AssertionRecord``'s metadata rule, and the ``cairn.screening`` secret screen
over the exact field sequence ``/v1`` ingest screens. A record this module
plans is a record the server's admission path has already accepted in every
respect that does not need a live catalogue.
"""

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import RFC_4122, UUID, uuid5

from pydantic import ValidationError

from cairn.authority.custody import (
    AssertionRecord,
    CustodyValueError,
    FactDraft,
    SourceType,
)

# Private, and deliberately so: this is the exact field sequence ``/v1``
# ingest screens (``mutations.py``), including the per-member walk over
# canonical metadata that the Task 11 leak sweep proved necessary. A local
# copy would be a second implementation of a security boundary, free to
# drift from the one that actually guards the server. Importing the real
# one means the dry-run's rejection counts describe what ingest will do.
from cairn.authority.mutations import IngestAssertion, _ingest_screened_fields
from cairn.catalogue.audit import Classification, Scope, TrustClass
from cairn.catalogue.sqlite import CatalogueStorageError, parse_timestamp
from cairn.screening import SecretScreen, first_finding
from cairn.transports.v1.requests import IngestRequest
from cairn_migrate.export import MANIFEST_FILENAME, snapshot_label_is_valid

PLAN_SCHEMA_VERSION = "cairn-migration-plan/v1"
PLAN_MANIFEST_FILENAME = "manifest.json"
OPERATIONS_FILENAME = "operations.jsonl"
REJECTIONS_FILENAME = "rejections.jsonl"
RECONCILIATIONS_FILENAME = "reconciliations.jsonl"
ENUMERATIONS_FILENAME = "enumerations.json"

#: P-74, fixed here once and forever. Every idempotency key the migration
#: ever sends is ``uuid5(MIGRATION_NAMESPACE, f"{store}:{legacy_id}")``, so
#: changing this value would orphan every applied record from its legacy
#: identity. It is a constant, not a setting.
MIGRATION_NAMESPACE = UUID("c4961664-0ded-4ade-aa38-69214bad2678")

#: §5.1-§5.3 target selected by the migration profile.
TARGET_REALM = "cairn"
TARGET_CLASSIFICATION = Classification.INTERNAL

#: §5.8: legacy is one realm's worth of data under a single enforced
#: group. A record carrying any other group is rejected, counted and
#: reported — never silently filtered at read time.
LEGACY_GROUP_ID = "cairn"

#: Actor text is provenance, not authority. Legacy actor fields are free-form
#: and cannot establish that a human authored a record. Only a match through
#: one of ``APPROVED_MATCH_KEYS`` provides the explicit repository evidence
#: needed to map an episode as ``human``/``validated``.

#: The P-73 store list, partitioned by what each store *is* under §5.6-§5.7.
#: Assertion stores plan ``/v1`` calls. Provenance stores enrich them.
#: Repository stores verify counts and supply the explicit capture match; §5.7
#: is explicit that they are not independent assertions.
ASSERTION_STORES = ("graph-episode", "attic-conversation", "attic-turn")
PROVENANCE_STORES = ("journal-event",)
REPOSITORY_STORES = ("thought-file", "openbrain-row")
PLAN_INPUT_STORES = ASSERTION_STORES + PROVENANCE_STORES + REPOSITORY_STORES

# Stage-B deterministic subset profile and ranking.
SUBSET_PLAN_PROFILE = "stage-b-episodes-plus-slice-v1"
SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT = 200
SUBSET_PLAN_REQUIRED_TURN_COUNT = 500
SUBSET_PLAN_REQUIRED_ADDITIONAL_TURN_COUNT = (
    SUBSET_PLAN_REQUIRED_TURN_COUNT - SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT
)
SUBSET_PLAN_RANKING_ALGORITHM = "sha256(canonical_identity)"
SUBSET_CONVERSATION_IDENTITY_PREFIX = "attic-conversation:"
SUBSET_TURN_IDENTITY_PREFIX = "attic-turn:"

# v0.1 size limits, quoted from ``cairn.authority.custody`` rather than
# imported because they are private there. ``tests/migrate/test_mapping.py``
# asserts each against the custody module, so a limit that moves fails a
# test instead of silently planning a record the server will refuse.
BODY_MAX_BYTES = 65536
METADATA_MAX_BYTES = 65536
PAYLOAD_MAX_BYTES = 1048576

#: The closed rejection vocabulary (§5.8), **in the order the rules run**.
#: The order is part of the behaviour, exactly as ``first_finding``'s field
#: order is: a record failing two rules is counted under the first, so the
#: tallies only mean something if the sequence is fixed. Cheap structural
#: rules come first.
#:
#: The remainder is not an arbitrary sequence: it mirrors ``/v1`` ingest's
#: own admission order — wire model, then command construction, then the
#: secret screen, which ``mutations.py`` runs on a command that already
#: exists. Screening earlier would screen a record the server would never
#: reach, and the dry-run's tallies are only worth having if they predict
#: what ingest actually does.
REJECTION_GROUP_ID = "group_id_not_cairn"
REJECTION_BODY_EMPTY = "body_empty"
REJECTION_BODY_TOO_LARGE = "body_too_large"
REJECTION_METADATA_TOO_LARGE = "metadata_too_large"
REJECTION_PAYLOAD_TOO_LARGE = "evidence_payload_too_large"
REJECTION_VALIDATION_FAILED = "mapping_validation_failed"
REJECTION_SECRET_SCREEN = "secret_screen"
REJECTION_RULES = (
    REJECTION_GROUP_ID,
    REJECTION_BODY_EMPTY,
    REJECTION_BODY_TOO_LARGE,
    REJECTION_METADATA_TOO_LARGE,
    REJECTION_PAYLOAD_TOO_LARGE,
    REJECTION_VALIDATION_FAILED,
    REJECTION_SECRET_SCREEN,
)

#: §5.7's reconciliation vocabulary: a repository record with no live-graph
#: counterpart, and a journal event that enriched nothing. Neither is a
#: rejection — nothing was going to be written for them either way — and
#: neither is a silent drop. They are the operator's ruling list.
RECONCILIATION_NO_GRAPH_COUNTERPART = "no_graph_counterpart"
RECONCILIATION_UNMATCHED_JOURNAL_EVENT = "unmatched_journal_event"
RECONCILIATION_RULES = (
    RECONCILIATION_NO_GRAPH_COUNTERPART,
    RECONCILIATION_UNMATCHED_JOURNAL_EVENT,
)

# The markers legacy's ingest harness stamps into an episode's
# ``source_description`` (gate record §4.1/§4.2), confirmed against the
# productive graph on 21 August 2026: 355 of 611 episodes carry both,
# ``cairn_event_id`` always the leading field and ``cairn_episode_uuid``
# always the third, in ``" | "``-separated ``key=value`` fields.
#
# The measured corpus has three description shapes: 355 marker-led
# episodes (fully parseable), 95 ``type=``-led key=value episodes with no
# markers, and 161 free-form prose descriptions that parse to nothing.
# The Task 4 dry-run is therefore expected to report roughly 355 journal
# matches, not zero and not 611 — a zero-match dry run indicates a parse
# bug, not a corpus mismatch.
MARKER_EVENT_ID = "cairn_event_id"
MARKER_EPISODE_UUID = "cairn_episode_uuid"

#: The confirmed field separator — space, pipe, space, never a bare pipe.
MARKER_SEPARATOR = " | "

#: §5.6/§5.7's approved repository match keys, in the fixed order they are
#: tried. The capture match runs over these marker names and **no others**:
#: an unrelated marker such as ``actor``, ``source`` or an event identity
#: colliding with a repository key must never escalate a record to
#: ``human``/``validated`` (review finding P2, 21 August 2026).
APPROVED_MATCH_KEYS = ("openbrain_id", "fingerprint", "content_fingerprint")

# A marker key is a bare identifier. The guard is what keeps the 161
# free-form prose descriptions inert: prose containing a stray ``=`` would
# otherwise mint one garbage pseudo-marker per episode, and a marker map
# polluted by prose is how a wrong journal match would start.
_MARKER_KEY = re.compile(r"[A-Za-z0-9_]+\Z")

# A validation-only assertion identity. ``AssertionRecord`` validates the
# canonical metadata string in ``__post_init__``, and the codebase's own
# convention is to construct a value purely for that side effect (see
# ``custody._validate_scope``). These placeholders are never serialised,
# never sent and never leave this module; the real identities are
# Cairn-assigned at ingest under I-28.
_VALIDATION_UUID = UUID("00000000-0000-4000-8000-000000000000")
_VALIDATION_INSTANT = datetime(2000, 1, 1, tzinfo=UTC)


#: ``MappingError``'s complete code vocabulary. Enforced at construction
#: (re-review finding R4): a documented-but-unchecked closed vocabulary is
#: one typo away from an undocumented open one.
MAPPING_ERROR_CODES = frozenset(
    {
        "bundle_unreadable",
        "bundle_manifest_invalid",
        "bundle_digest_mismatch",
        "bundle_store_missing",
        "record_unparseable",
        "plan_value_invalid",
        "output_inside_checkout",
        "plan_exists",
        "plan_incomplete",
        "output_unavailable",
        "source_plan_manifest_invalid",
        "source_plan_digest_mismatch",
        "source_plan_unknown_operation",
        "source_plan_relationship_invalid",
        "subset_selection_short",
    }
)


class MappingError(Exception):
    """Closed vocabulary of mapping refusal codes — ``MAPPING_ERROR_CODES``.

    An unknown code is a programming error, not a refusal, and raises
    ``ValueError`` rather than constructing a refusal that lies about its
    own vocabulary.
    """

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if code not in MAPPING_ERROR_CODES:
            raise ValueError(f"unknown mapping error code: {code}")
        self.code = code
        self.detail = detail
        suffix = "" if detail is None else f": {detail}"
        super().__init__(f"mapping error: {code}{suffix}")


@dataclass(frozen=True, slots=True)
class _SourcePlanOperation:
    operation: str
    store: str
    legacy_id: str
    idempotency_key: str
    request: Mapping[str, object]
    raw: bytes
    index: int
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SourcePlanManifest:
    label: str
    manifest_sha256: str
    source_bundle: Mapping[str, object]
    target: Mapping[str, object]
    idempotency_namespace: str
    enumerations: bytes


def _source_plan_manifest_sha256(manifest_raw: bytes) -> str:
    return hashlib.sha256(manifest_raw).hexdigest()


def _selection_identity(store: str, legacy_id: str) -> str:
    if store == "attic-conversation":
        return f"{SUBSET_CONVERSATION_IDENTITY_PREFIX}{legacy_id}"
    if store == "attic-turn":
        return f"{SUBSET_TURN_IDENTITY_PREFIX}{legacy_id}"
    return f"{store}:{legacy_id}"


def _selection_digest(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _selection_key(identity: str) -> tuple[str, str]:
    return _selection_digest(identity), identity


def _canonical_uuid(value: object, *, version: int | None = None) -> bool:
    if type(value) is not str or not value.isascii():
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return (
        parsed.variant == RFC_4122
        and (version is None or parsed.version == version)
        and str(parsed) == value
    )


@dataclass(frozen=True, slots=True)
class PlannedOperation:
    """One planned ``/v1`` call, ready for ``apply`` to send unchanged.

    The discriminants are closed vocabularies and are enforced at
    construction, per the repository's closed-value convention: the plan is
    a persistence format, and a value outside its vocabulary must fail here
    rather than surface as a mystery to ``apply``.
    """

    store: str
    legacy_id: str
    idempotency_key: str
    operation: str
    request: dict[str, object]

    def __post_init__(self) -> None:
        if self.operation not in {"ingest", "invalidate"} or self.store not in (
            ASSERTION_STORES
        ):
            raise MappingError("plan_value_invalid")


@dataclass(frozen=True, slots=True)
class Rejection:
    store: str
    legacy_id: str
    rule: str
    detail: str | None

    def __post_init__(self) -> None:
        if self.store not in PLAN_INPUT_STORES or self.rule not in REJECTION_RULES:
            raise MappingError("plan_value_invalid")


@dataclass(frozen=True, slots=True)
class Reconciliation:
    store: str
    legacy_id: str
    rule: str

    def __post_init__(self) -> None:
        if self.store not in PLAN_INPUT_STORES or self.rule not in RECONCILIATION_RULES:
            raise MappingError("plan_value_invalid")


@dataclass(frozen=True, slots=True)
class MappingResult:
    """Everything the plan writer and the report need, and nothing else.

    ``exported_counts`` is carried through from the bundle so the report
    can assert the §5.8 arithmetic — exported equals planned plus rejected
    plus reconciled, per store — without re-reading the bundle. That
    identity is what "zero silent drops" means in practice.
    """

    operations: tuple[PlannedOperation, ...]
    rejections: tuple[Rejection, ...]
    reconciliations: tuple[Reconciliation, ...]
    exported_counts: Mapping[str, int]
    matched_counts: Mapping[str, int]
    enumerations: Mapping[str, Mapping[str, Mapping[str, int]]]
    validated_count: int
    body_bytes: int


def idempotency_key(store: str, legacy_id: str) -> str:
    """P-74: ``uuid5(namespace, "<store>:<legacy id>")``.

    Name-based rather than random because a re-run must derive the *same*
    key: the contract's parser accepts any canonical RFC 4122 UUID, and a
    replayed key returns the original receipt instead of creating a second
    assertion.
    """
    return str(uuid5(MIGRATION_NAMESPACE, f"{store}:{legacy_id}"))


def canonical_json(value: object) -> str:
    """The one JSON encoding this tool writes.

    Identical to the export bundle's, and to the canonical form
    ``custody._validate_metadata`` demands: sorted keys, no insignificant
    whitespace, UTF-8 kept as UTF-8. Canonical by construction is why a
    re-map is byte-identical rather than merely equivalent.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


# --- reading the export bundle ------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportBundle:
    """A verified ``cairn-legacy-export/v1`` bundle, in memory.

    Verified, not merely read: every store file's SHA-256 is checked against
    the manifest before a single record is mapped. A plan derived from a
    bundle that no longer matches its own digests would carry that corruption
    into the catalogue under an idempotency key that makes it permanent, so
    the check is a refusal rather than a warning.
    """

    label: str
    manifest: dict[str, object]
    manifest_sha256: str
    records: Mapping[str, tuple[dict[str, object], ...]]

    def counts(self) -> dict[str, int]:
        return {store: len(self.records[store]) for store in PLAN_INPUT_STORES}


def read_export_bundle(bundle_path: Path) -> ExportBundle:
    manifest_path = bundle_path / MANIFEST_FILENAME
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        raise MappingError("bundle_unreadable", detail=MANIFEST_FILENAME) from error
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as error:
        raise MappingError("bundle_manifest_invalid", detail="not json") from error
    if not isinstance(manifest, dict):
        raise MappingError("bundle_manifest_invalid", detail="not an object")
    if manifest.get("schema_version") != "cairn-legacy-export/v1":
        raise MappingError("bundle_manifest_invalid", detail="schema_version")
    label = _manifest_label(manifest)
    stores = _manifest_stores(manifest)
    records = {
        store: tuple(_read_store(bundle_path, store, stores[store]))
        for store in PLAN_INPUT_STORES
    }
    return ExportBundle(
        label=label,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        records=records,
    )


def _manifest_label(manifest: dict[str, object]) -> str:
    snapshot = manifest.get("snapshot")
    if not isinstance(snapshot, dict):
        raise MappingError("bundle_manifest_invalid", detail="snapshot")
    label = snapshot.get("label")
    if not isinstance(label, str) or not snapshot_label_is_valid(label):
        raise MappingError("bundle_manifest_invalid", detail="snapshot.label")
    return label


def _manifest_stores(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    entries = manifest.get("stores")
    if not isinstance(entries, list):
        raise MappingError("bundle_manifest_invalid", detail="stores")
    stores: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("store"), str):
            raise MappingError("bundle_manifest_invalid", detail="stores[]")
        stores[str(entry["store"])] = entry
    missing = [store for store in PLAN_INPUT_STORES if store not in stores]
    if missing:
        raise MappingError("bundle_store_missing", detail=missing[0])
    return stores


def _read_store(
    bundle_path: Path,
    store: str,
    entry: dict[str, object],
) -> Iterator[dict[str, object]]:
    filename = entry.get("filename")
    if not isinstance(filename, str) or "/" in filename or filename.startswith("."):
        raise MappingError("bundle_manifest_invalid", detail=f"{store}.filename")
    try:
        raw = (bundle_path / filename).read_bytes()
    except OSError as error:
        raise MappingError("bundle_unreadable", detail=filename) from error
    if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
        raise MappingError("bundle_digest_mismatch", detail=store)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise MappingError("bundle_unreadable", detail=filename) from error
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise MappingError(
                "record_unparseable", detail=f"{filename}:{line_number}"
            ) from error
        if not isinstance(record, dict) or not isinstance(record.get("legacy_id"), str):
            raise MappingError("record_unparseable", detail=f"{filename}:{line_number}")
        yield record


# --- the §5.7 match indexes ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepositoryMatch:
    """One repository record an episode's markers reached, and how.

    ``key`` and ``value`` are the P-74 provenance the review's P5 finding
    required persisted: which approved marker matched, and what it carried.
    Both land in the private operation plan's metadata, never in the
    repository-bound report.
    """

    store: str
    legacy_id: str
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class _MatchIndexes:
    """The two lookups §5.7's enrichment needs, built once.

    ``repository_by_key`` maps every declared match key — **domain and
    value together** — onto every record that declared it, in sorted order.
    The domain is the approved marker name the §5.7 relation pairs the
    field with: a thought's ``openbrain_id`` and a row's ``id`` declare
    under ``openbrain_id``, a thought's ``fingerprint`` under
    ``fingerprint``, a row's ``content_fingerprint`` under
    ``content_fingerprint``. Keying by value alone let an ``openbrain_id``
    marker reach an unrelated thought whose *fingerprint* happened to carry
    the same text — a cross-domain collision wrongly granting
    ``human``/``validated`` (re-review finding R1, 21 August 2026). Corresponding ``thought-file`` and ``openbrain-row``
    records share their identity by construction (the thought's
    ``openbrain_id`` is the row's ``id``), so one key reaching two stores is
    the §5.7 complement working as approved, not an ambiguity (review
    finding P1, 21 August 2026): every claimant is matched and enriches,
    deterministically. ``journal`` holds events by their own identity and by
    the ``episode_uuid`` legacy's ingest harness derived, because an episode
    reaches its event through either.
    """

    repository_by_key: Mapping[tuple[str, str], tuple[tuple[str, str], ...]]
    journal_by_id: Mapping[str, dict[str, object]]
    journal_by_episode: Mapping[str, dict[str, object]]


def _build_indexes(
    bundle: ExportBundle,
    admitted_journal: tuple[dict[str, object], ...],
) -> _MatchIndexes:
    """Build the match indexes over admitted records only.

    ``admitted_journal`` is the journal store minus its §5.8 rejections
    (re-review finding R2): a foreign-group event is a counted rejection
    and must be invisible to matching, so it can neither grant trust nor
    appear as an episode's ``journal_event_id`` provenance.
    """
    claimants: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for store in REPOSITORY_STORES:
        for record in bundle.records[store]:
            identity = str(record["legacy_id"])
            for domain, value in _repository_declared_keys(store, record):
                claimants.setdefault((domain, value), set()).add((store, identity))
    journal_by_id: dict[str, dict[str, object]] = {}
    journal_by_episode: dict[str, dict[str, object]] = {}
    for record in admitted_journal:
        journal_by_id[str(record["legacy_id"])] = record
        event = record.get("event")
        if isinstance(event, dict):
            episode_uuid = event.get("episode_uuid")
            if isinstance(episode_uuid, str) and episode_uuid:
                journal_by_episode[episode_uuid] = record
    return _MatchIndexes(
        repository_by_key={
            key: tuple(sorted(value)) for key, value in claimants.items()
        },
        journal_by_id=journal_by_id,
        journal_by_episode=journal_by_episode,
    )


def _repository_declared_keys(
    store: str, record: dict[str, object]
) -> Iterator[tuple[str, str]]:
    """§5.7's declared match keys as ``(domain, value)`` pairs, and only
    those.

    The identity declares under ``openbrain_id``, because that is what it
    is — a thought's ``openbrain_id`` and a row's ``id`` are the same
    namespace by construction. Each fingerprint declares under its own
    field name, so a marker can only ever reach the field the approved
    relation pairs it with.
    """
    identity = record.get("legacy_id")
    if isinstance(identity, str) and identity:
        yield "openbrain_id", identity
    if store == "thought-file":
        frontmatter = record.get("frontmatter")
        if isinstance(frontmatter, dict):
            fingerprint = frontmatter.get("fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                yield "fingerprint", fingerprint
    if store == "openbrain-row":
        fingerprint = record.get("content_fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            yield "content_fingerprint", fingerprint


def parse_markers(source_description: object) -> dict[str, str]:
    """The ``key=value`` markers legacy's ingest harness stamps into an
    episode's ``source_description``.

    Fields are separated by ``" | "`` (confirmed against the productive
    graph, 21 August 2026), each field split on its first ``=``, first
    occurrence of a key winning. Values keep their internal spaces — the
    corpus smuggles whole titles into some ``actor`` fields, and a
    whitespace split would shear those into garbage tokens. A key that is
    not a bare identifier is ignored, which is what keeps free-form prose
    descriptions parsing to nothing rather than to pseudo-markers.

    A description carrying no markers yields nothing and the episode simply
    matches nothing — the absence of a marker is not an error, because
    legacy wrote episodes by more than one route.
    """
    if not isinstance(source_description, str):
        return {}
    markers: dict[str, str] = {}
    for field in source_description.split(MARKER_SEPARATOR):
        key, separator, value = field.partition("=")
        key = key.strip()
        value = value.strip()
        if separator and value and _MARKER_KEY.fullmatch(key) and key not in markers:
            markers[key] = value
    return markers


# --- the per-store unit rules -------------------------------------------------

#: The sentinel for a store that carries no ``group_id`` at all. Attic and
#: the repository stores are files, not graph nodes; ``None`` would be a
#: *present* group of ``None``, which is a different thing from absent.
_NO_GROUP = object()


@dataclass(frozen=True, slots=True)
class _IngestDraft:
    """One record's mapped values, before admission.

    Everything the §5.5 table decides is settled by the time this exists;
    what remains is whether the server would accept it, which
    ``_admit`` answers.
    """

    store: str
    legacy_id: str
    body: object
    valid_from: str | None
    observed_at: str | None
    metadata: dict[str, object]
    source_type: SourceType
    trust: TrustClass
    evidence_payload: str | None
    group_id: object


def conversation_body(record: Mapping[str, object]) -> str:
    """§5.5 leaves an attic conversation without a body of its own — the
    content is in its turns — so it gets a fixed-format header line.

    Fixed-format because it is generated text standing in for an absent
    field: a reader must be able to tell it apart from a legacy body at a
    glance, and a re-map must produce it byte for byte. Absent values render
    empty rather than as ``None``, which would put a Python repr into a
    stored assertion body.
    """
    return (
        f"legacy attic conversation id={record['legacy_id']}"
        f" source={_header_value(record.get('source'))}"
        f" title={_header_value(record.get('title'))}"
    )


def _header_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _metadata(store: str, legacy_id: str, **fields: object) -> dict[str, object]:
    """Provenance metadata, with absent values omitted rather than nulled.

    Omission keeps the canonical string shorter and, more to the point,
    keeps ``metadata[n].key`` screening paths stable: a null-valued key is
    a key the screen still walks for nothing.

    ``legacy_id`` is stored store-prefixed (ruled by the operator, 23 August 2026,
    on the dry-run finding that an attic row id is itself a 64-hex value
    and trips the entropy screen from inside canonical metadata; the
    prefix keeps the value out of the pure-hex shape the detector reads).
    Identity elsewhere stays raw: plan operations, receipts and the
    verbatim-row payload carry the exporter's id unchanged.
    """
    metadata: dict[str, object] = {
        "legacy_store": store,
        "legacy_id": f"{store}:{legacy_id}",
    }
    metadata.update({key: value for key, value in fields.items() if value is not None})
    return metadata


def _episode_draft(
    record: Mapping[str, object],
    indexes: _MatchIndexes,
    markers: Mapping[str, str],
) -> tuple[_IngestDraft, tuple[RepositoryMatch, ...], str | None]:
    """A graph episode — the primary assertion source (§5.7).

    Returns the draft alongside what it matched, because the match is what
    §5.4/§5.5 turn into trust and what §5.7 turns into reconciliation.

    Matching is computed **before** admission and independently of it. A
    repository record whose episode is later rejected still counts as having
    a live-graph counterpart, because it has one; the episode's fate is
    reported under its own rejection rule rather than smeared across a
    second store's reconciliation list.
    """
    legacy_id = str(record["legacy_id"])
    matches = _repository_matches(markers, indexes)
    journal_id = _journal_match(legacy_id, markers, indexes)
    explicitly_captured = bool(matches)
    created_at = record.get("created_at")
    valid_at = record.get("valid_at")
    draft = _IngestDraft(
        store="graph-episode",
        legacy_id=legacy_id,
        body=record.get("episode_body"),
        # §5.5: the explicit ``valid_at`` where legacy recorded one, and the
        # capture time otherwise — never the migration's own clock, which
        # would date every legacy memory to the day it was moved.
        valid_from=_timestamp(valid_at) or _timestamp(created_at),
        observed_at=_timestamp(created_at),
        metadata=_metadata(
            "graph-episode",
            legacy_id,
            legacy_name=record.get("name"),
            legacy_source=record.get("source"),
            legacy_source_description=record.get("source_description"),
            legacy_created_at=_timestamp(created_at),
            legacy_valid_at=_timestamp(valid_at),
            journal_event_id=journal_id,
            repository_matches=(
                [
                    {
                        "store": match.store,
                        "legacy_id": match.legacy_id,
                        "key": match.key,
                        "value": match.value,
                    }
                    for match in matches
                ]
                if matches
                else None
            ),
        ),
        source_type=(
            SourceType.HUMAN if explicitly_captured else SourceType.AGENT_CLAIM
        ),
        trust=(TrustClass.VALIDATED if explicitly_captured else TrustClass.CANDIDATE),
        # P-74 as amended: ingest refuses ``validated`` without a payload
        # (``validated_requires_payload``), and the promote grant does not
        # lift that. The verbatim exported row is the payload, so the
        # evidence is exactly what the bundle's digest already covers.
        evidence_payload=canonical_json(record) if explicitly_captured else None,
        group_id=record.get("group_id"),
    )
    return draft, matches, journal_id


def _repository_matches(
    markers: Mapping[str, str],
    indexes: _MatchIndexes,
) -> tuple[RepositoryMatch, ...]:
    """§5.7's capture match, over the approved keys and no others.

    Only the ``APPROVED_MATCH_KEYS`` marker names are consulted, in their
    fixed order — never the episode's own identity, and never an unrelated
    marker such as ``actor`` or an event id, which could collide with a
    repository key and wrongly escalate a record to ``human``/``validated``
    (review finding P2). Each marker looks up its **own domain** only, so a
    value shared across incompatible fields matches nothing (re-review
    finding R1). Every claimant of a matched key is returned, so
    corresponding ``thought-file`` and ``openbrain-row`` records both
    enrich; a record already matched through an earlier key is not repeated
    under a later one.
    """
    matches: list[RepositoryMatch] = []
    seen: set[tuple[str, str]] = set()
    for key in APPROVED_MATCH_KEYS:
        value = markers.get(key)
        if value is None:
            continue
        for store, legacy_id in indexes.repository_by_key.get((key, value), ()):
            if (store, legacy_id) not in seen:
                seen.add((store, legacy_id))
                matches.append(
                    RepositoryMatch(
                        store=store, legacy_id=legacy_id, key=key, value=value
                    )
                )
    return tuple(matches)


def _journal_match(
    legacy_id: str,
    markers: Mapping[str, str],
    indexes: _MatchIndexes,
) -> str | None:
    """An episode reaches its journal event by either direction of the
    cross-reference legacy maintained: the ``cairn_event_id`` marker it
    carries, or the ``episode_uuid`` the event derived for it."""
    event_id = markers.get(MARKER_EVENT_ID)
    if event_id is not None and event_id in indexes.journal_by_id:
        return event_id
    episode_uuid = markers.get(MARKER_EPISODE_UUID, legacy_id)
    event = indexes.journal_by_episode.get(episode_uuid)
    if event is not None:
        return str(event["legacy_id"])
    return None


def _turn_draft(record: Mapping[str, object]) -> _IngestDraft:
    """An attic turn: one assertion, one fact, the turn's own text.

    Always carries its verbatim row as evidence, per P-74 — attic turns are
    transcript evidence by nature, and the payload is what Task 5 compares
    digests against.
    """
    legacy_id = str(record["legacy_id"])
    created_at = _timestamp(record.get("created_at"))
    return _IngestDraft(
        store="attic-turn",
        legacy_id=legacy_id,
        body=record.get("content"),
        valid_from=created_at,
        observed_at=created_at,
        metadata=_metadata(
            "attic-turn",
            legacy_id,
            conversation_id=record.get("conversation_id"),
            turn_index=record.get("turn_index"),
            role=record.get("role"),
            episode_id=record.get("episode_id"),
            content_sha256=record.get("content_sha256"),
            legacy_created_at=created_at,
        ),
        source_type=SourceType.AGENT_CLAIM,
        trust=TrustClass.CANDIDATE,
        evidence_payload=canonical_json(record),
        group_id=_NO_GROUP,
    )


def _conversation_draft(record: Mapping[str, object]) -> _IngestDraft:
    legacy_id = str(record["legacy_id"])
    created_at = _timestamp(record.get("created_at"))
    started_at = _timestamp(record.get("started_at"))
    return _IngestDraft(
        store="attic-conversation",
        legacy_id=legacy_id,
        body=conversation_body(record),
        valid_from=started_at or created_at,
        observed_at=created_at,
        metadata=_metadata(
            "attic-conversation",
            legacy_id,
            legacy_source=record.get("source"),
            legacy_title=record.get("title"),
            legacy_started_at=started_at,
            legacy_ended_at=_timestamp(record.get("ended_at")),
            legacy_created_at=created_at,
        ),
        source_type=SourceType.AGENT_CLAIM,
        trust=TrustClass.CANDIDATE,
        evidence_payload=canonical_json(record),
        group_id=_NO_GROUP,
    )


def _timestamp(value: object) -> str | None:
    """Timestamps arrive already in I-28 form from the export readers; this
    only distinguishes present from absent."""
    return value if isinstance(value, str) and value else None


# --- admission: the §5.8 rejection rules --------------------------------------


def _admit(
    draft: _IngestDraft,
    screen: SecretScreen,
) -> PlannedOperation | Rejection:
    """The §5.8 rules, in ``REJECTION_RULES`` order, over one draft.

    Every rule that can be answered by the server's own code is answered by
    it. The explicit size checks come first only to give §5.8 its distinct
    rule codes; the constructors below would refuse the same records anyway,
    which is what makes those codes a narrowing of the server's behaviour
    rather than a second opinion about it.
    """
    if draft.group_id is not _NO_GROUP and draft.group_id != LEGACY_GROUP_ID:
        return Rejection(draft.store, draft.legacy_id, REJECTION_GROUP_ID, None)
    body = draft.body
    if not isinstance(body, str) or not body.strip():
        # Whitespace-only counts as empty: it is a byte the custody layer
        # would accept and an assertion no one could ever use.
        return Rejection(draft.store, draft.legacy_id, REJECTION_BODY_EMPTY, None)
    if len(body.encode("utf-8")) > BODY_MAX_BYTES:
        return Rejection(draft.store, draft.legacy_id, REJECTION_BODY_TOO_LARGE, None)
    metadata = canonical_json(draft.metadata)
    if len(metadata.encode("utf-8")) > METADATA_MAX_BYTES:
        return Rejection(
            draft.store, draft.legacy_id, REJECTION_METADATA_TOO_LARGE, None
        )
    payload = draft.evidence_payload
    if payload is not None and len(payload.encode("utf-8")) > PAYLOAD_MAX_BYTES:
        return Rejection(
            draft.store, draft.legacy_id, REJECTION_PAYLOAD_TOO_LARGE, None
        )
    request = _request_body(draft, body, metadata)
    try:
        command = _validated_command(draft, request, body, metadata)
    except (
        ValidationError,
        CustodyValueError,
        CatalogueStorageError,
        ValueError,
    ):
        # ``ValueError`` covers ``AuditValueError``, which ``Scope`` and
        # ``ScopeSegment`` raise; the other three are the wire model, the
        # custody validators and a timestamp the readers let through.
        return Rejection(
            draft.store, draft.legacy_id, REJECTION_VALIDATION_FAILED, None
        )
    finding = first_finding(screen, _ingest_screened_fields(command))
    if finding is not None:
        # The rule identity travels; the secret does not, and neither does
        # the field's content. P-78 admits a rule tally, which is what this
        # is — one row of it.
        return Rejection(
            draft.store, draft.legacy_id, REJECTION_SECRET_SCREEN, finding.rule
        )
    return PlannedOperation(
        store=draft.store,
        legacy_id=draft.legacy_id,
        idempotency_key=idempotency_key(draft.store, draft.legacy_id),
        operation="ingest",
        request=request,
    )


def _request_body(
    draft: _IngestDraft,
    body: str,
    metadata: str,
) -> dict[str, object]:
    """The exact JSON ``apply`` will POST to ``/v1/ingest``.

    Built once and carried in the plan verbatim, so ``apply`` sends what
    ``map`` validated rather than rebuilding it from parts and hoping the
    two agree. ``metadata`` is re-parsed from its canonical string on
    purpose: the wire carries an inline object, and round-tripping the
    canonical form is what guarantees the object the server canonicalises
    is byte-identical to the one this module screened.
    """
    request: dict[str, object] = {
        "scope": {"realm": TARGET_REALM, "segments": []},
        "classification": TARGET_CLASSIFICATION.value,
        "source_type": draft.source_type.value,
        "facts": [
            {"body": body, "valid_from": draft.valid_from, "valid_to": None},
        ],
        "requested_trust": draft.trust.value,
        "observed_at": draft.observed_at,
        "metadata": json.loads(metadata),
    }
    if draft.evidence_payload is not None:
        request["evidence_payload"] = draft.evidence_payload
    return request


def _validated_command(
    draft: _IngestDraft,
    request: dict[str, object],
    body: str,
    metadata: str,
) -> IngestAssertion:
    """Admit the request through every server-side validator that does not
    need a live catalogue, and return the command the screen runs on.

    Three layers, in the server's own order: the strict ``/v1`` wire model,
    the frozen command with its ``FactDraft`` and ``Scope`` invariants, and
    ``AssertionRecord`` for the metadata rule — the last constructed purely
    for its ``__post_init__`` side effect, the convention ``custody`` itself
    uses for ``Scope``. Its placeholder identities are never serialised and
    never leave this function; real identities are Cairn-assigned at ingest
    under I-28.
    """
    IngestRequest.model_validate(request)
    scope = Scope(realm=TARGET_REALM, segments=())
    observed_at = (
        parse_timestamp(draft.observed_at) if draft.observed_at is not None else None
    )
    valid_from = (
        parse_timestamp(draft.valid_from) if draft.valid_from is not None else None
    )
    AssertionRecord(
        assertion_id=_VALIDATION_UUID,
        realm_id=TARGET_REALM,
        segments=(),
        classification=TARGET_CLASSIFICATION,
        source_type=draft.source_type,
        principal_id=_VALIDATION_UUID,
        observed_at=observed_at,
        metadata=metadata,
        recorded_at=_VALIDATION_INSTANT,
    )
    return IngestAssertion(
        scope=scope,
        classification=TARGET_CLASSIFICATION,
        source_type=draft.source_type,
        facts=(FactDraft(body=body, valid_from=valid_from, valid_to=None),),
        requested_trust=draft.trust,
        observed_at=observed_at,
        metadata=metadata,
        evidence_payload=(
            None
            if draft.evidence_payload is None
            else draft.evidence_payload.encode("utf-8")
        ),
    )


# --- the map stage ------------------------------------------------------------


def map_bundle(bundle: ExportBundle) -> MappingResult:
    """The whole pure stage: a verified bundle in, a complete plan out.

    Order is fixed throughout — assertion stores in the P-73 sequence, each
    store in the bundle's own record order — because that order is what makes
    the emitted files byte-identical between runs.
    """
    admitted_journal, journal_rejections = _partition_journal(bundle)
    indexes = _build_indexes(bundle, admitted_journal)
    screen = SecretScreen()
    operations: list[PlannedOperation] = []
    rejections: list[Rejection] = []
    matched_repository: set[tuple[str, str]] = set()
    matched_journal: set[str] = set()
    enumerations = _Enumerations()

    for record in bundle.records["graph-episode"]:
        markers = parse_markers(record.get("source_description"))
        draft, matches, journal_id = _episode_draft(record, indexes, markers)
        for match in matches:
            matched_repository.add((match.store, match.legacy_id))
        if journal_id is not None:
            matched_journal.add(journal_id)
        enumerations.tally("source", "graph-episode", record.get("source"))
        enumerations.tally("actor", "graph-episode", markers.get("actor"))
        _dispatch(_admit(draft, screen), operations, rejections)
    for record in bundle.records["attic-conversation"]:
        enumerations.tally("source", "attic-conversation", record.get("source"))
        _dispatch(_admit(_conversation_draft(record), screen), operations, rejections)
    for record in bundle.records["attic-turn"]:
        _dispatch(_admit(_turn_draft(record), screen), operations, rejections)

    # Every exported journal record enumerates, admitted or rejected
    # (re-review finding P2, second pass, 21 August 2026): a foreign-group
    # event is removed from matching, trust and reconciliation, but
    # rejection is a count, not an erasure — the private enumeration is
    # the inventory of what the snapshot contained, and the operator's ruling input
    # must not lose values the corpus actually carries.
    for record in bundle.records["journal-event"]:
        event = record.get("event")
        if isinstance(event, dict):
            enumerations.tally("actor", "journal-event", event.get("actor"))
            enumerations.tally("source", "journal-event", event.get("source"))
    for record in bundle.records["thought-file"]:
        frontmatter = record.get("frontmatter")
        enumerations.tally(
            "source",
            "thought-file",
            frontmatter.get("source") if isinstance(frontmatter, dict) else None,
        )

    rejections.extend(journal_rejections)
    reconciliations = tuple(
        _reconcile(bundle, admitted_journal, matched_repository, matched_journal)
    )
    return MappingResult(
        operations=tuple(operations),
        rejections=tuple(rejections),
        reconciliations=reconciliations,
        exported_counts=bundle.counts(),
        matched_counts=_matched_counts(bundle, matched_repository, matched_journal),
        enumerations=enumerations.snapshot(),
        validated_count=sum(
            1
            for operation in operations
            if operation.request["requested_trust"] == TrustClass.VALIDATED.value
        ),
        # P-77's Stage B input: projection calls ``add_episode`` per fact, so
        # the cost driver is the number of fact bodies and their size. Both
        # are reported as counts; this tool does not invent a price.
        body_bytes=sum(
            len(str(fact["body"]).encode("utf-8"))
            for operation in operations
            for fact in _facts(operation)
        ),
    )


def _facts(operation: PlannedOperation) -> Sequence[Mapping[str, object]]:
    """The fact list ``_request_body`` built, read back for the P-77 size
    tally. Cast rather than checked: the request is this module's own
    construction, and ``IngestRequest`` has already validated its shape."""
    return cast(Sequence[Mapping[str, object]], operation.request["facts"])


def _partition_journal(
    bundle: ExportBundle,
) -> tuple[tuple[dict[str, object], ...], tuple[Rejection, ...]]:
    """§5.8's group rule over the journal, before anything reads it.

    Enrichment is influence: a foreign-group event that fed the match
    index could grant ``human``/``validated`` to an episode (re-review
    finding R2), so the group test runs first and a rejected event is
    invisible to matching, trust and reconciliation. It occupies exactly
    one accounting column: rejected. It is **not** invisible to the
    private enumeration (re-review finding P2, second pass) — rejection is
    a count, not a removal from the inventory of what the snapshot
    contained, so ``map_bundle`` enumerates every exported journal record
    regardless of this partition.
    An event carrying no group at all is admitted; §5.8 rejects a record
    *carrying* another group, and absence is not carriage.
    """
    admitted: list[dict[str, object]] = []
    rejections: list[Rejection] = []
    for record in bundle.records["journal-event"]:
        event = record.get("event")
        group = event.get("group_id") if isinstance(event, dict) else None
        if group is not None and group != LEGACY_GROUP_ID:
            rejections.append(
                Rejection(
                    store="journal-event",
                    legacy_id=str(record["legacy_id"]),
                    rule=REJECTION_GROUP_ID,
                    detail=None,
                )
            )
        else:
            admitted.append(record)
    return tuple(admitted), tuple(rejections)


def _dispatch(
    outcome: PlannedOperation | Rejection,
    operations: list[PlannedOperation],
    rejections: list[Rejection],
) -> None:
    if isinstance(outcome, Rejection):
        rejections.append(outcome)
    else:
        operations.append(outcome)


class _Enumerations:
    """P-74's actor and source enumeration, over the explicit field matrix.

    The matrix (review findings P3 and R3): ``actor`` is observed on
    journal events and on episode ``actor=`` markers; ``source`` is
    observed on graph episodes, journal events, attic conversations and
    thought-file frontmatter (gate record §4.4). Attic turns carry
    ``role``, which is neither, and openbrain rows declare no source
    field. Rejected journal records remain enumerated as part of the
    snapshot corpus, but are excluded from matching, trust and
    reconciliation (re-review finding P2, second pass) — rejection is a
    count, not a removal from the inventory of what the snapshot
    contained. Every observed value is counted, including the absent case — a corpus
    where most episodes carry no ``source`` is a fact about the corpus, not
    a gap in the report.

    Raw values are P-78-restricted: they land only in the plan directory's
    private ``enumerations.json``; the repository-bound report carries
    distinct/observed tallies and that file's digest.
    """

    def __init__(self) -> None:
        self._counts: dict[str, dict[str, dict[str, int]]] = {
            "actor": {"graph-episode": {}, "journal-event": {}},
            "source": {
                "graph-episode": {},
                "journal-event": {},
                "attic-conversation": {},
                "thought-file": {},
            },
        }

    def tally(self, field: str, store: str, value: object) -> None:
        counts = self._counts[field][store]
        key = value if isinstance(value, str) else ""
        counts[key] = counts.get(key, 0) + 1

    def snapshot(self) -> dict[str, dict[str, dict[str, int]]]:
        return {
            field: {
                store: dict(sorted(values.items())) for store, values in stores.items()
            }
            for field, stores in self._counts.items()
        }


def _reconcile(
    bundle: ExportBundle,
    admitted_journal: tuple[dict[str, object], ...],
    matched_repository: set[tuple[str, str]],
    matched_journal: set[str],
) -> Iterator[Reconciliation]:
    """§5.7: what the enrichment stores did not attach to.

    Neither case is a rejection — no assertion was ever going to be planned
    for these stores — and neither is a drop. They are the list the operator rules on
    in Task 4, and the count arithmetic in the report proves the list is
    complete.
    """
    for store in REPOSITORY_STORES:
        for record in bundle.records[store]:
            identity = str(record["legacy_id"])
            if (store, identity) not in matched_repository:
                yield Reconciliation(
                    store, identity, RECONCILIATION_NO_GRAPH_COUNTERPART
                )
    for record in admitted_journal:
        identity = str(record["legacy_id"])
        if identity not in matched_journal:
            yield Reconciliation(
                "journal-event", identity, RECONCILIATION_UNMATCHED_JOURNAL_EVENT
            )


def _matched_counts(
    bundle: ExportBundle,
    matched_repository: set[tuple[str, str]],
    matched_journal: set[str],
) -> dict[str, int]:
    counts = {
        store: sum(1 for identity in matched_repository if identity[0] == store)
        for store in REPOSITORY_STORES
    }
    counts["journal-event"] = len(matched_journal)
    return counts


# --- writing the plan ---------------------------------------------------------


def write_plan(
    result: MappingResult,
    bundle: ExportBundle,
    output_path: Path,
    *,
    checkout_root: Path,
) -> Path:
    """Write the ``cairn-migration-plan/v1`` directory, manifest last.

    Manifest last for the same reason the export bundle does it: a plan whose
    manifest exists is a plan that was written completely, so an interrupted
    run cannot be mistaken for one ``apply`` may act on.
    """
    resolved = output_path.resolve()
    if resolved == checkout_root or checkout_root in resolved.parents:
        # ``operations.jsonl`` carries legacy bodies. P-78 keeps those out of
        # this repository, and a mistyped ``--output`` is the one plausible
        # way that goes wrong.
        raise MappingError("output_inside_checkout")
    plan_path = _create_plan_directory(resolved, bundle.label)
    files = (
        _write_jsonl(plan_path, OPERATIONS_FILENAME, _operation_lines(result)),
        _write_jsonl(plan_path, REJECTIONS_FILENAME, _rejection_lines(result)),
        _write_jsonl(
            plan_path, RECONCILIATIONS_FILENAME, _reconciliation_lines(result)
        ),
    )
    # The raw actor/source enumerations are the operator's P-74 ruling input and are
    # P-78-restricted, so they are a private plan file, not a report field:
    # the repository-bound report carries their tallies and this file's
    # digest, and the operator rules on the file itself in his private directory.
    enumerations = enumerations_bytes(result)
    try:
        (plan_path / ENUMERATIONS_FILENAME).write_bytes(enumerations)
    except OSError as error:
        raise MappingError(
            "output_unavailable", detail=ENUMERATIONS_FILENAME
        ) from error
    _write_plan_manifest(plan_path, result, bundle, files)
    return plan_path


def enumerations_bytes(result: MappingResult) -> bytes:
    """The private enumerations file, byte-deterministically.

    A function rather than an inline dump because the report quotes this
    exact content's SHA-256: writer and report must derive it from one
    serialisation or the quoted digest describes nothing.
    """
    return (
        json.dumps(result.enumerations, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _operation_lines(result: MappingResult) -> Iterator[dict[str, object]]:
    for operation in result.operations:
        yield {
            "operation": operation.operation,
            "store": operation.store,
            "legacy_id": operation.legacy_id,
            "idempotency_key": operation.idempotency_key,
            "request": operation.request,
        }


def _rejection_lines(result: MappingResult) -> Iterator[dict[str, object]]:
    for rejection in result.rejections:
        yield {
            "store": rejection.store,
            "legacy_id": rejection.legacy_id,
            "rule": rejection.rule,
            "detail": rejection.detail,
        }


def _reconciliation_lines(result: MappingResult) -> Iterator[dict[str, object]]:
    for reconciliation in result.reconciliations:
        yield {
            "store": reconciliation.store,
            "legacy_id": reconciliation.legacy_id,
            "rule": reconciliation.rule,
        }


@dataclass(frozen=True, slots=True)
class _PlanFile:
    filename: str
    record_count: int
    byte_count: int
    sha256: str


def _write_jsonl(
    plan_path: Path,
    filename: str,
    lines: Iterable[dict[str, object]],
) -> _PlanFile:
    digest = hashlib.sha256()
    record_count = 0
    byte_count = 0
    try:
        with (plan_path / filename).open("wb") as target:
            for line in lines:
                encoded = (canonical_json(line) + "\n").encode("utf-8")
                target.write(encoded)
                digest.update(encoded)
                record_count += 1
                byte_count += len(encoded)
    except OSError as error:
        raise MappingError("output_unavailable", detail=filename) from error
    return _PlanFile(
        filename=filename,
        record_count=record_count,
        byte_count=byte_count,
        sha256=digest.hexdigest(),
    )


def _create_plan_directory(
    output_path: Path,
    label: str,
    *,
    suffix: str = "",
) -> Path:
    suffix = f"-{suffix}" if suffix else ""
    plan_path = output_path / f"cairn-migration-plan-{label}{suffix}"
    try:
        plan_path.mkdir(parents=True)
    except FileExistsError as error:
        if plan_path.is_dir() and not (plan_path / PLAN_MANIFEST_FILENAME).exists():
            raise MappingError("plan_incomplete") from error
        raise MappingError("plan_exists") from error
    except OSError as error:
        raise MappingError("output_unavailable") from error
    return plan_path


def _write_plan_manifest(
    plan_path: Path,
    result: MappingResult,
    bundle: ExportBundle,
    files: tuple[_PlanFile, ...],
) -> None:
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_bundle": {
            "label": bundle.label,
            "manifest_sha256": bundle.manifest_sha256,
        },
        "target": {
            "realm": TARGET_REALM,
            "segments": [],
            "classification": TARGET_CLASSIFICATION.value,
        },
        "idempotency_namespace": str(MIGRATION_NAMESPACE),
        "enumerations": {
            "filename": ENUMERATIONS_FILENAME,
            "bytes": len(enumerations_bytes(result)),
            "sha256": hashlib.sha256(enumerations_bytes(result)).hexdigest(),
        },
        "files": [
            {
                "filename": entry.filename,
                "record_count": entry.record_count,
                "bytes": entry.byte_count,
                "sha256": entry.sha256,
            }
            for entry in files
        ],
        "counts": plan_counts(result),
    }
    manifest = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    try:
        (plan_path / PLAN_MANIFEST_FILENAME).write_bytes(manifest)
    except OSError as error:
        raise MappingError(
            "output_unavailable", detail=PLAN_MANIFEST_FILENAME
        ) from error


def read_source_plan(
    source_plan: Path,
    *,
    expected_source_manifest_sha256: str,
) -> tuple[tuple[_SourcePlanOperation, ...], _SourcePlanManifest]:
    """Read and validate a full plan as a subset source.

    The source plan is validated by the same digest pinning rules that
    ``read_plan`` applies, with extra checks for the deterministic subset
    scope: closed store/operation vocabularies, canonical identity parsing,
    and deterministic turn-conversation references.
    """
    manifest_path = source_plan / PLAN_MANIFEST_FILENAME
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as error:
        raise MappingError("source_plan_manifest_invalid") from error
    try:
        manifest = json.loads(manifest_raw)
    except ValueError as error:
        raise MappingError("source_plan_manifest_invalid") from error
    if not isinstance(manifest, dict):
        raise MappingError("source_plan_manifest_invalid")
    if manifest.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise MappingError("source_plan_manifest_invalid", detail="schema_version")
    source_plan_manifest_sha256 = _source_plan_manifest_sha256(manifest_raw)
    if source_plan_manifest_sha256 != expected_source_manifest_sha256:
        raise MappingError("source_plan_digest_mismatch", detail="manifest.json")

    source_bundle = manifest.get("source_bundle")
    if not isinstance(source_bundle, dict):
        raise MappingError("source_plan_manifest_invalid", detail="source_bundle")
    label = source_bundle.get("label")
    if not isinstance(label, str) or not label:
        raise MappingError("source_plan_manifest_invalid", detail="source_bundle.label")
    source_manifest_sha256 = source_bundle.get("manifest_sha256")
    if (
        not isinstance(source_manifest_sha256, str)
        or len(source_manifest_sha256) != 64
        or not re.fullmatch(r"[0-9a-f]{64}", source_manifest_sha256)
    ):
        raise MappingError(
            "source_plan_manifest_invalid", detail="source_bundle.manifest_sha256"
        )
    target = manifest.get("target")
    if not isinstance(target, dict):
        raise MappingError("source_plan_manifest_invalid", detail="target")
    if (
        target.get("realm") != TARGET_REALM
        or target.get("classification") != TARGET_CLASSIFICATION.value
        or target.get("segments") != []
    ):
        raise MappingError("source_plan_manifest_invalid", detail="target")

    idempotency_namespace = manifest.get("idempotency_namespace")
    if not isinstance(idempotency_namespace, str) or idempotency_namespace != str(
        MIGRATION_NAMESPACE
    ):
        raise MappingError(
            "source_plan_manifest_invalid", detail="idempotency_namespace"
        )

    files = manifest.get("files")
    if not isinstance(files, list):
        raise MappingError("source_plan_manifest_invalid", detail="files")
    matches = [
        value
        for value in files
        if isinstance(value, dict) and value.get("filename") == OPERATIONS_FILENAME
    ]
    if len(matches) != 1:
        raise MappingError("source_plan_manifest_invalid", detail="operations entry")
    operation_file = cast(Mapping[str, object], matches[0])

    for filename in (REJECTIONS_FILENAME, RECONCILIATIONS_FILENAME):
        companion_matches = [
            value
            for value in files
            if isinstance(value, dict) and value.get("filename") == filename
        ]
        if len(companion_matches) != 1:
            raise MappingError("source_plan_manifest_invalid", detail=filename)
        companion_entry = cast(Mapping[str, object], companion_matches[0])
        try:
            companion_raw = (source_plan / filename).read_bytes()
        except OSError as error:
            raise MappingError(
                "source_plan_manifest_invalid", detail=filename
            ) from error
        if (
            companion_entry.get("record_count") != _file_line_count(companion_raw)
            or companion_entry.get("bytes") != len(companion_raw)
            or companion_entry.get("sha256")
            != hashlib.sha256(companion_raw).hexdigest()
        ):
            raise MappingError("source_plan_digest_mismatch", detail=filename)
        if _invalid_jsonl_termination(companion_raw):
            raise MappingError("source_plan_manifest_invalid", detail=filename)

    operations_path = source_plan / OPERATIONS_FILENAME
    try:
        operations_raw = operations_path.read_bytes()
    except OSError as error:
        raise MappingError(
            "source_plan_manifest_invalid", detail=OPERATIONS_FILENAME
        ) from error

    raw_operations_count = _file_line_count(operations_raw)
    expected_operations_count = operation_file.get("record_count")
    if not isinstance(expected_operations_count, int) or expected_operations_count < 0:
        raise MappingError(
            "source_plan_manifest_invalid", detail="operations.record_count"
        )
    if expected_operations_count != raw_operations_count:
        raise MappingError(
            "source_plan_digest_mismatch", detail="operations.record_count"
        )

    expected_operations_bytes = operation_file.get("bytes")
    if not isinstance(expected_operations_bytes, int) or expected_operations_bytes < 0:
        raise MappingError("source_plan_manifest_invalid", detail="operations.bytes")
    if expected_operations_bytes != len(operations_raw):
        raise MappingError("source_plan_digest_mismatch", detail="operations.bytes")

    expected_operations_sha = operation_file.get("sha256")
    actual_operations_sha = hashlib.sha256(operations_raw).hexdigest()
    if expected_operations_sha != actual_operations_sha:
        raise MappingError("source_plan_digest_mismatch", detail="operations.sha256")
    if _invalid_jsonl_termination(operations_raw):
        raise MappingError("source_plan_manifest_invalid")

    enumerations_entry = manifest.get("enumerations")
    if not isinstance(enumerations_entry, dict):
        raise MappingError("source_plan_manifest_invalid", detail="enumerations")
    if enumerations_entry.get("filename") != ENUMERATIONS_FILENAME:
        raise MappingError(
            "source_plan_manifest_invalid", detail="enumerations.filename"
        )
    try:
        enumerations_raw = (source_plan / ENUMERATIONS_FILENAME).read_bytes()
    except OSError as error:
        raise MappingError(
            "source_plan_manifest_invalid", detail=ENUMERATIONS_FILENAME
        ) from error
    if (
        enumerations_entry.get("bytes") != len(enumerations_raw)
        or enumerations_entry.get("sha256")
        != hashlib.sha256(enumerations_raw).hexdigest()
    ):
        raise MappingError("source_plan_digest_mismatch", detail=ENUMERATIONS_FILENAME)

    return (
        tuple(
            _source_plan_operation_lines(
                operations_raw,
                expected_store_candidates=(
                    "graph-episode",
                    "attic-turn",
                    "attic-conversation",
                ),
            )
        ),
        _SourcePlanManifest(
            label=label,
            manifest_sha256=source_plan_manifest_sha256,
            source_bundle=source_bundle,
            target=target,
            idempotency_namespace=idempotency_namespace,
            enumerations=enumerations_raw,
        ),
    )


def _invalid_jsonl_termination(content: bytes) -> bool:
    if not content:
        return False
    return not content.endswith(b"\n")


def _file_line_count(content: bytes) -> int:
    if not content:
        return 0
    return content.count(b"\n")


def _source_plan_operation_lines(
    operations_raw: bytes,
    *,
    expected_store_candidates: tuple[str, ...],
) -> Iterator[_SourcePlanOperation]:
    seen: set[tuple[str, str]] = set()
    for index, encoded in enumerate(operations_raw.splitlines(), start=1):
        if not encoded:
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")
        try:
            value = json.loads(encoded)
        except ValueError as error:
            raise MappingError(
                "source_plan_unknown_operation", detail=f"line {index}"
            ) from error
        if not isinstance(value, dict):
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")
        if set(value) != {
            "operation",
            "store",
            "legacy_id",
            "idempotency_key",
            "request",
        }:
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")
        if not all(
            isinstance(value[field], expected_type)
            for field, expected_type in (
                ("operation", str),
                ("store", str),
                ("legacy_id", str),
                ("idempotency_key", str),
                ("request", dict),
            )
        ):
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")
        if value["operation"] != "ingest":
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")
        if value["store"] not in expected_store_candidates:
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")
        if not _canonical_uuid(value["idempotency_key"]):
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")

        request = cast(dict[str, object], value["request"])
        plan_operation = PlannedOperation(
            operation=value["operation"],
            store=value["store"],
            legacy_id=value["legacy_id"],
            idempotency_key=value["idempotency_key"],
            request=request,
        )

        identity = (plan_operation.store, plan_operation.legacy_id)
        if identity in seen:
            raise MappingError("source_plan_unknown_operation", detail=f"line {index}")
        seen.add(identity)

        if plan_operation.store == "attic-turn":
            metadata = request.get("metadata")
            if not isinstance(metadata, dict):
                raise MappingError(
                    "source_plan_relationship_invalid", detail="attic-turn metadata"
                )
            conversation_id = metadata.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise MappingError(
                    "source_plan_relationship_invalid",
                    detail=f"attic-turn:{plan_operation.legacy_id}",
                )
            turn_conversation_id = conversation_id
        else:
            turn_conversation_id = None

        yield _SourcePlanOperation(
            operation=plan_operation.operation,
            store=plan_operation.store,
            legacy_id=plan_operation.legacy_id,
            idempotency_key=plan_operation.idempotency_key,
            request=request,
            raw=(encoded + b"\n"),
            index=index,
            conversation_id=turn_conversation_id,
        )


def _select_subset_operations(
    operations: tuple[_SourcePlanOperation, ...],
) -> tuple[
    tuple[_SourcePlanOperation, ...], tuple[int, int, int], tuple[int, int, int]
]:
    """Apply the fixed deterministic subset profile.

    Returns selected operations plus summary source/selected source counts.
    """
    graph_episodes = [
        operation for operation in operations if operation.store == "graph-episode"
    ]
    source_conversations = [
        operation for operation in operations if operation.store == "attic-conversation"
    ]
    source_turns = [
        operation for operation in operations if operation.store == "attic-turn"
    ]

    turns_by_conversation: dict[
        str, list[tuple[tuple[str, str], _SourcePlanOperation]]
    ] = defaultdict(list)
    for turn in source_turns:
        if turn.conversation_id is None:
            raise MappingError(
                "source_plan_relationship_invalid", detail=f"turn:{turn.legacy_id}"
            )
        turns_by_conversation[turn.conversation_id].append(
            (_selection_key(_selection_identity("attic-turn", turn.legacy_id)), turn)
        )

    conversation_ids = {
        operation.legacy_id
        for operation in source_conversations
        if operation.store == "attic-conversation"
    }
    for turn in source_turns:
        if cast(str, turn.conversation_id) not in conversation_ids:
            raise MappingError(
                "source_plan_relationship_invalid",
                detail=f"turn:{turn.legacy_id}",
            )

    conversation_candidates = [
        operation
        for operation in source_conversations
        if turns_by_conversation.get(operation.legacy_id)
    ]
    ranked_conversations = sorted(
        [
            (
                _selection_key(
                    _selection_identity("attic-conversation", operation.legacy_id)
                ),
                operation,
            )
            for operation in conversation_candidates
        ],
        key=lambda item: item[0],
    )

    if len(ranked_conversations) < SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT:
        raise MappingError("subset_selection_short", detail="conversations")
    selected_conversation_ops = [
        operation
        for _, operation in ranked_conversations[
            :SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT
        ]
    ]

    selected_turns = []
    remaining_turns: list[tuple[tuple[str, str], _SourcePlanOperation]] = []
    for operation in selected_conversation_ops:
        ranked_turns = sorted(
            turns_by_conversation[operation.legacy_id], key=lambda item: item[0]
        )
        selected_turns.append(ranked_turns[0][1])
        remaining_turns.extend(ranked_turns[1:])

    remaining_turns_sorted = sorted(remaining_turns, key=lambda item: item[0])
    additional_turns = remaining_turns_sorted[
        : SUBSET_PLAN_REQUIRED_TURN_COUNT - len(selected_turns)
    ]
    if len(additional_turns) < SUBSET_PLAN_REQUIRED_ADDITIONAL_TURN_COUNT:
        raise MappingError("subset_selection_short", detail="turns")

    selected_turn_operations = selected_turns + [item[1] for item in additional_turns]
    selected_operations = tuple(
        [
            *graph_episodes,
            *selected_conversation_ops,
            *selected_turn_operations,
        ]
    )

    source_counts = (
        len(graph_episodes),
        len(source_conversations),
        len(source_turns),
    )
    selected_counts = (
        len(graph_episodes),
        len(selected_conversation_ops),
        len(selected_turn_operations),
    )
    return selected_operations, source_counts, selected_counts


def write_subset_plan(
    source_plan: Path,
    output_path: Path,
    *,
    checkout_root: Path,
    expected_source_manifest_sha256: str,
) -> tuple[Path, tuple[int, int, int], tuple[int, int, int]]:
    """Write a deterministic, materialised, directly applyable subset plan."""
    resolved = output_path.resolve()
    if resolved == checkout_root or checkout_root in resolved.parents:
        raise MappingError("output_inside_checkout")

    source_operations, source_manifest = read_source_plan(
        source_plan, expected_source_manifest_sha256=expected_source_manifest_sha256
    )
    selected_operations, source_counts, selected_counts = _select_subset_operations(
        source_operations
    )
    selected_set = {
        (operation.store, operation.legacy_id) for operation in selected_operations
    }

    plan_path = _create_plan_directory(
        resolved, source_manifest.label, suffix=SUBSET_PLAN_PROFILE
    )
    subset_payload = b"".join(
        operation.raw
        for operation in source_operations
        if (operation.store, operation.legacy_id) in selected_set
    )
    file_digest = hashlib.sha256(subset_payload).hexdigest()
    operations_file = _PlanFile(
        filename=OPERATIONS_FILENAME,
        record_count=_file_line_count(subset_payload),
        byte_count=len(subset_payload),
        sha256=file_digest,
    )
    try:
        (plan_path / OPERATIONS_FILENAME).write_bytes(subset_payload)
    except OSError as error:
        raise MappingError("output_unavailable", detail=OPERATIONS_FILENAME) from error

    files = (
        operations_file,
        _write_jsonl(plan_path, REJECTIONS_FILENAME, ()),
        _write_jsonl(plan_path, RECONCILIATIONS_FILENAME, ()),
    )
    try:
        (plan_path / ENUMERATIONS_FILENAME).write_bytes(source_manifest.enumerations)
    except OSError as error:
        raise MappingError(
            "output_unavailable", detail=ENUMERATIONS_FILENAME
        ) from error

    _write_subset_manifest(
        plan_path=plan_path,
        source_manifest=source_manifest,
        source_counts=source_counts,
        selected_counts=selected_counts,
        selected_operations=selected_operations,
        files=files,
    )
    return plan_path, source_counts, selected_counts


def _write_subset_manifest(
    *,
    plan_path: Path,
    source_manifest: _SourcePlanManifest,
    source_counts: tuple[int, int, int],
    selected_counts: tuple[int, int, int],
    selected_operations: tuple[_SourcePlanOperation, ...],
    files: tuple[_PlanFile, ...],
) -> None:
    source_graph, source_conversation, source_turn = source_counts
    selected_graph, selected_conversation, selected_turn = selected_counts
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_bundle": source_manifest.source_bundle,
        "source_plan": {
            "schema_version": PLAN_SCHEMA_VERSION,
            "label": source_manifest.label,
            "manifest_sha256": source_manifest.manifest_sha256,
        },
        "target": source_manifest.target,
        "idempotency_namespace": source_manifest.idempotency_namespace,
        "subset": {
            "profile": SUBSET_PLAN_PROFILE,
            "ranking_algorithm": SUBSET_PLAN_RANKING_ALGORITHM,
            "required": {
                "conversations": SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT,
                "turns": SUBSET_PLAN_REQUIRED_TURN_COUNT,
                "additional_turns": SUBSET_PLAN_REQUIRED_ADDITIONAL_TURN_COUNT,
            },
            "source_counts": {
                "graph_episodes": source_graph,
                "conversations": source_conversation,
                "turns": source_turn,
            },
            "selected_counts": {
                "graph_episodes": selected_graph,
                "conversations": selected_conversation,
                "turns": selected_turn,
            },
            "selected_operation_count": len(selected_operations),
        },
        "enumerations": {
            "filename": ENUMERATIONS_FILENAME,
            "bytes": len(source_manifest.enumerations),
            "sha256": hashlib.sha256(source_manifest.enumerations).hexdigest(),
        },
        "files": [
            {
                "filename": entry.filename,
                "record_count": entry.record_count,
                "bytes": entry.byte_count,
                "sha256": entry.sha256,
            }
            for entry in files
        ],
        "counts": _subset_plan_counts(selected_counts),
    }
    manifest = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    try:
        (plan_path / PLAN_MANIFEST_FILENAME).write_bytes(manifest)
    except OSError as error:
        raise MappingError(
            "output_unavailable", detail=PLAN_MANIFEST_FILENAME
        ) from error


def _subset_plan_counts(
    selected_counts: tuple[int, int, int],
) -> dict[str, dict[str, int]]:
    selected_graph, selected_conversation, selected_turn = selected_counts
    planned = {
        "graph-episode": selected_graph,
        "attic-conversation": selected_conversation,
        "attic-turn": selected_turn,
    }
    return {
        store: {
            "exported": planned.get(store, 0),
            "planned": planned.get(store, 0),
            "rejected": 0,
            "matched": 0,
            "reconciled": 0,
        }
        for store in PLAN_INPUT_STORES
    }


def plan_counts(result: MappingResult) -> dict[str, dict[str, int]]:
    """Per-store count arithmetic — the zero-silent-drops assertion itself.

    Every exported record is in exactly one of the four columns: planned
    or rejected for an assertion store, matched, reconciled or — for a
    foreign-group journal event under §5.8 — rejected for an enrichment
    store. §5.7 makes the repository stores provenance and count checks,
    and P-75 keeps the derived graph layer out entirely, so one identity
    covers every store: exported = planned + rejected + matched +
    reconciled. ``report.py`` asserts it rather than asking a reader to
    add it up.
    """
    counts: dict[str, dict[str, int]] = {}
    for store in PLAN_INPUT_STORES:
        counts[store] = {
            "exported": result.exported_counts[store],
            "planned": sum(1 for item in result.operations if item.store == store),
            "rejected": sum(1 for item in result.rejections if item.store == store),
            "matched": result.matched_counts.get(store, 0),
            "reconciled": sum(
                1 for item in result.reconciliations if item.store == store
            ),
        }
    return counts
