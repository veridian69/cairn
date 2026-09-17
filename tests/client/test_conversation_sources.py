"""Host source admission is strict, immutable and separate from model input."""

import importlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from cairn.catalogue.audit import Classification, Scope, ScopeSegment
from cairn.client.profiles import MemoryProfile


def sources_module() -> Any:
    assert importlib.util.find_spec("cairn.client.conversation_sources") is not None, (
        "host source admission is missing"
    )
    return importlib.import_module("cairn.client.conversation_sources")


def profile() -> MemoryProfile:
    return MemoryProfile(
        "http://127.0.0.1:8123",
        uuid4(),
        Scope("synthetic", (ScopeSegment("job", "source-test"),)),
        Classification.INTERNAL,
        Path("/unused"),
        uuid4(),
    )


def source_document(
    tmp_path: Path, *, body: str = "Host task: decide capacity."
) -> tuple[Any, ...]:
    module = sources_module()
    configured, principal, source_id = profile(), uuid4(), uuid4()
    bundle = module.create_source_bundle(
        configured,
        expected_principal=principal,
        sources=(module.HostSource(source_id, body),),
    )
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(bundle.to_document(), ensure_ascii=False), encoding="utf-8"
    )
    path.chmod(0o600)
    return module, configured, principal, source_id, bundle, path


def test_loaded_source_is_immutable_full_unicode_and_context_bound(
    tmp_path: Path,
) -> None:
    body = "Operator’s task: Grüezi 🐦\nCapacity is eight, not eighteen.\nPropose staffing."
    module, configured, principal, identity, original, path = source_document(
        tmp_path, body=body
    )
    loaded = module.load_sources(path, profile=configured, expected_principal=principal)
    path.write_text("changed after admission")
    payload = json.loads(loaded.evidence_payload(identity))
    assert payload["source"] == {
        "source_id": str(identity),
        "origin": "host_input",
        "body": body,
    }
    assert payload["instance_id"] == str(configured.expected_instance_id)
    assert payload["principal_id"] == str(principal)
    assert payload["session_id"] == str(configured.session_id)
    assert loaded.to_document() == original.to_document()
    with pytest.raises(AttributeError):
        loaded.sources[0].body = "fabricated"


@pytest.mark.parametrize(
    "field", ["instance_id", "principal_id", "scope", "classification", "session_id"]
)
def test_context_mismatch_is_rejected(tmp_path: Path, field: str) -> None:
    module, configured, principal, _, bundle, path = source_document(tmp_path)
    document = bundle.to_document()
    document[field] = {
        "scope": {"realm": "synthetic", "segments": []},
        "classification": "public",
    }.get(field, str(uuid4()))
    path.write_text(json.dumps(document))
    with pytest.raises(module.SourceError):
        module.load_sources(path, profile=configured, expected_principal=principal)


@pytest.mark.parametrize(
    "case", ["empty", "oversize", "too_many", "duplicate", "no_session"]
)
def test_typed_host_construction_enforces_limits(case: str) -> None:
    module, configured, principal = sources_module(), profile(), uuid4()
    identity = uuid4()
    bodies: tuple[Any, ...] = (module.HostSource(identity, "A task."),)
    with pytest.raises(module.SourceError):
        if case == "empty":
            bodies = ()
        elif case == "oversize":
            bodies = (module.HostSource(identity, "ü" * 8193),)
        elif case == "too_many":
            bodies = tuple(module.HostSource(uuid4(), "A task.") for _ in range(5))
        elif case == "duplicate":
            bodies = (bodies[0], bodies[0])
        else:
            configured = replace(configured, session_id=None)
        module.create_source_bundle(
            configured, expected_principal=principal, sources=bodies
        )


@pytest.mark.parametrize(
    "case", ["origin", "extra", "duplicate_key", "symlink", "public_mode", "missing"]
)
def test_source_loader_rejects_ambiguous_or_unprotected_inputs(
    tmp_path: Path, case: str
) -> None:
    module, configured, principal, _, bundle, path = source_document(tmp_path)
    document = bundle.to_document()
    if case == "origin":
        document["sources"][0]["origin"] = "authenticated_user"
        path.write_text(json.dumps(document))
    elif case == "extra":
        document["register_source"] = True
        path.write_text(json.dumps(document))
    elif case == "duplicate_key":
        path.write_text('{"schema":"x",' + json.dumps(document)[1:])
    elif case == "symlink":
        target = path
        path = tmp_path / "link.json"
        path.symlink_to(target)
    elif case == "public_mode":
        path.chmod(0o644)
    else:
        path.unlink()
    with pytest.raises(module.SourceError):
        module.load_sources(path, profile=configured, expected_principal=principal)


def test_unknown_source_is_never_substituted(tmp_path: Path) -> None:
    module, _, _, _, bundle, _ = source_document(tmp_path)
    with pytest.raises(module.SourceError):
        bundle.evidence_payload(UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"))


def test_typed_host_source_rejects_invalid_unicode_with_a_closed_error() -> None:
    module = sources_module()
    with pytest.raises(module.SourceError) as error:
        module.HostSource(uuid4(), "private input\ud800")
    assert "private input" not in str(error.value)


def test_four_sources_at_utf8_byte_limit_are_admitted() -> None:
    module, configured, principal = sources_module(), profile(), uuid4()
    bundle = module.create_source_bundle(
        configured,
        expected_principal=principal,
        sources=tuple(module.HostSource(uuid4(), "ü" * 8192) for _ in range(4)),
    )
    assert len(bundle.sources) == 4
    assert all(len(source.body.encode("utf-8")) == 16384 for source in bundle.sources)


def test_encoded_aggregate_and_raw_file_sizes_are_bounded(tmp_path: Path) -> None:
    module, configured, principal, _, _, path = source_document(tmp_path)
    with pytest.raises(module.SourceError):
        module.create_source_bundle(
            configured,
            expected_principal=principal,
            sources=tuple(module.HostSource(uuid4(), "x\x01" * 8192) for _ in range(4)),
        )
    path.write_bytes(b" " * 131073)
    with pytest.raises(module.SourceError):
        module.load_sources(path, profile=configured, expected_principal=principal)
