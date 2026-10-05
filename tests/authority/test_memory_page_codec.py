from datetime import UTC, datetime
from uuid import UUID

from cairn.authority.memory_codec import memory_value
from cairn.authority.memory_page_types import (
    PagedMemoryFact,
    SourceTimeStatus,
    TimeBasis,
)
from cairn.authority.memory_types import MemoryFact, MemoryFactRecord
from cairn.catalogue.audit import Classification, Scope, TrustClass

_AT = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def _fact() -> MemoryFact:
    record = MemoryFactRecord(
        UUID(int=1, version=4),
        "body",
        Scope("acme", ()),
        Classification.INTERNAL,
        TrustClass.CANDIDATE,
        None,
        None,
        None,
        _AT,
        None,
    )
    return MemoryFact(record, None, None, 0.25)


def test_paged_fact_extends_the_legacy_record_without_renaming_fields() -> None:
    legacy = memory_value(_fact())
    paged = memory_value(
        PagedMemoryFact(
            _fact(),
            _AT,
            SourceTimeStatus.AVAILABLE,
            TimeBasis.SOURCE,
            UUID(int=2, version=4),
        )
    )
    assert isinstance(legacy, dict) and isinstance(paged, dict)
    assert {k: paged[k] for k in legacy} == legacy
    assert paged["observed_at"] == "2026-10-05T09:00:00.000000Z"
    assert paged["source_time_status"] == "available"
    assert paged["ordering_time_basis"] == "source"
    assert paged["source_evidence_id"] == str(UUID(int=2, version=4))


def test_unavailable_source_time_is_explicitly_null() -> None:
    paged = memory_value(
        PagedMemoryFact(_fact(), None, SourceTimeStatus.UNAVAILABLE, None, None)
    )
    assert isinstance(paged, dict)
    assert paged["observed_at"] is None
    assert paged["source_time_status"] == "unavailable"
    assert paged["ordering_time_basis"] is None
    assert paged["source_evidence_id"] is None
