"""v0.7.13 adds operations; it must not change one byte of the legacy surface."""

import json
from pathlib import Path

from test_memory import _recall, _seed
from test_retrieval import _ingest_facts

from cairn.authority.memory_codec import memory_value
from cairn.transports.memory.contracts import openapi_document

_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "memory_contract_v0_7_12_legacy.json"
)
_LEGACY_HIT_KEYS = {
    "fact_id",
    "body",
    "scope",
    "classification",
    "trust",
    "valid_from",
    "valid_to",
    "recorded_at",
    "invalidated_at",
    "assertion_id",
    "derived_from",
    "promoted_by",
    "evidence_id",
    "source_principal_id",
    "source_type",
    "relevance_score",
    "has_disagreement",
    "disagreement_context_incomplete",
}


def test_legacy_schemas_and_paths_are_byte_identical() -> None:
    frozen = json.loads(_FIXTURE.read_text())
    current = openapi_document()
    expected_schemas = {
        "RecallRequest",
        "RecallBody",
        "MemoryFactBody",
        "HistoryRequest",
        "HistoryBody",
        "DisagreementBody",
        "ResolutionBody",
        "CorrectionBody",
        "FailureEnvelope",
        "FailureBody",
        "ScopeBody",
        "InvalidRequestDetail",
        "SecretRejectedDetail",
    }
    expected_paths = {"/memory/v1/recall", "/memory/v1/history"}
    assert set(frozen["schemas"]) == expected_schemas
    assert set(frozen["paths"]) == expected_paths
    for name, schema in frozen["schemas"].items():
        assert current["components"]["schemas"][name] == schema, name
    for path, item in frozen["paths"].items():
        assert current["paths"][path] == item, path


def test_legacy_recall_packet_keys_are_unchanged(tmp_path: Path) -> None:
    _seed(tmp_path)
    _ingest_facts(tmp_path, bodies=("frozen memory",))
    document = memory_value(_recall(tmp_path, "frozen"))
    assert isinstance(document, dict)
    assert set(document) == {
        "hits",
        "budget_consumed",
        "budget_exhausted",
        "policy",
        "disagreements",
        "resolutions",
        "semantic_degraded",
    }
    assert len(document["hits"]) > 0
    assert set(document["hits"][0]) == _LEGACY_HIT_KEYS
