"""Composition closes the index adapter it built, and only that one.

The real Graphiti adapter owns a FalkorDB driver and a dedicated
event-loop thread, and until this was wired its ``close()`` was reached
from nowhere: repeated application lifecycles leaked a client and a
thread apiece until the process exited. Steady-state serving never
noticed, which is exactly why nothing caught it — so the regression is
pinned here rather than left to the next reviewer to re-find.

``close()`` stays off the P-38 port deliberately. Lifecycle is
composition's business, and composition is the only place that
constructs the adapter, so it is also the only place that needs to know
what closing means. These tests fix that ownership rule: what
composition built, composition closes; what it was handed, it leaves
alone.
"""

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager

import cairn.runtime.composition as composition
from cairn.authority.memory import CairnMemory
from cairn.catalogue.migration import migrate_catalogue
from cairn.projection.adapter import (
    FactProjected,
    IndexAdapter,
    ProjectedFactState,
    ProjectionFailed,
)
from cairn.projection.graphiti import GraphitiIndex
from cairn.projection.memory import MemoryIndex
from cairn.runtime.composition import _default_index, build_application
from cairn.runtime.config import (
    CairnConfig,
    GraphitiConfig,
    HttpConfig,
    PathConfig,
)

INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("dimension", [1024, 1536, 1])
@pytest.mark.parametrize("injected", [False, True])
def test_selected_graphiti_capability_uses_same_instance_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dimension: int, injected: bool
) -> None:
    class Selected(GraphitiIndex):
        def __init__(self) -> None:
            self._graphiti = SimpleNamespace(  # type: ignore[assignment]
                embedder=SimpleNamespace(
                    config=SimpleNamespace(
                        embedding_dim=dimension,
                        embedding_model="text-embedding-3-small",
                    )
                )
            )

    selected = Selected()
    captured: list[Any] = []
    original = CairnMemory

    def memory(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(composition, "CairnMemory", memory)
    monkeypatch.setattr(
        composition, "_default_index", lambda *a, **k: (selected, selected)
    )
    build_application(
        _config(tmp_path, mode="test"),
        clock=lambda: NOW,
        index_adapter=selected if injected else None,
    )
    assert captured[0]["index"] is selected
    assert captured[0]["semantic_evidence"] is (selected if dimension == 1024 else None)


@pytest.mark.parametrize("kind", ["hostile", "memory", "absent"])
def test_frozen_port_fake_never_has_capability_attributes_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    class Hostile(RecordingIndex):
        @property
        def memory_evidence_source(self) -> object:
            raise AssertionError("composition must not duck-type the frozen port")

    adapters: dict[str, IndexAdapter | None] = {
        "hostile": Hostile(),
        "memory": MemoryIndex(),
        "absent": None,
    }
    selected = adapters[kind]
    captured: list[Any] = []
    original = CairnMemory

    def memory(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(composition, "CairnMemory", memory)
    build_application(
        _config(tmp_path, mode="test", graphiti=False), index_adapter=selected
    )
    assert captured[0]["semantic_evidence"] is None


class RecordingIndex:
    """Stands in for ``GraphitiIndex``: counts the closes it receives."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closes = 0
        self.close_requests = 0

    def request_close(self) -> None:
        self.close_requests += 1

    def project(self, state: ProjectedFactState) -> FactProjected | ProjectionFailed:
        return FactProjected()

    def project_many(
        self, states: tuple[ProjectedFactState, ...]
    ) -> tuple[FactProjected | ProjectionFailed, ...]:
        return tuple(self.project(state) for state in states)

    def search(
        self, query: str, limit: int, partition_keys: tuple[str, ...]
    ) -> tuple[UUID, ...]:
        return ()

    def clear(self, partition_keys: tuple[str, ...] | None) -> None:
        return None

    def close(self) -> None:
        self.closes += 1


def _config(
    data_path: Path, *, mode: str = "production", graphiti: bool = True
) -> CairnConfig:
    credentials = data_path / "credentials"
    if mode == "production" and graphiti:
        # I-92: the real adapter refuses to construct without these, so a
        # production configuration here comes with the Secret mount that
        # makes it startable. Ownership, not credentials, is what this file
        # tests — see test_adapter_credentials.py for the refusals.
        credentials.mkdir(parents=True, exist_ok=True)
        (credentials / "falkordb-password").write_text("secret\n", encoding="utf-8")
        (credentials / "openai-api-key").write_text("key\n", encoding="utf-8")
    return CairnConfig(
        schema_version="cairn.config/v1",
        instance_id=INSTANCE_ID,
        mode=mode,  # type: ignore[arg-type]
        http=HttpConfig(host="127.0.0.1", port=8000),
        paths=PathConfig(data=data_path, credentials=credentials),
        graphiti=GraphitiConfig(enabled=graphiti),
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[RecordingIndex]]:
    """Every adapter ``_default_index`` constructs, in construction order."""
    built: list[RecordingIndex] = []

    class Constructed(RecordingIndex, GraphitiIndex):
        def __init__(self, **kwargs: object) -> None:
            RecordingIndex.__init__(self, **kwargs)
            self._graphiti = SimpleNamespace(  # type: ignore[assignment]
                embedder=SimpleNamespace(
                    config=SimpleNamespace(
                        embedding_dim=1536, embedding_model="text-embedding-3-small"
                    )
                )
            )
            built.append(self)

    monkeypatch.setattr(composition, "GraphitiIndex", Constructed)
    yield built


def test_the_production_adapter_is_returned_as_owned(
    tmp_path: Path, recorded: list[RecordingIndex]
) -> None:
    """The second element is what composition must close. Only the real
    adapter holds a resource, so only it appears there."""
    index, owned = _default_index(_config(tmp_path), writer_gate=threading.Lock())

    assert len(recorded) == 1
    assert index is recorded[0]
    # ``owned`` is typed ``GraphitiIndex | None`` and the fixture patches
    # that name with a stand-in, so the identity is asserted through
    # ``object`` rather than fighting the annotation the fix depends on.
    assert cast(object, owned) is recorded[0]


def test_the_in_memory_adapter_is_owned_by_nobody(tmp_path: Path) -> None:
    """It holds a dictionary and a lock, both of which die with the
    process; there is nothing to close and nothing to leak."""
    index, owned = _default_index(
        _config(tmp_path, mode="test"), writer_gate=threading.Lock()
    )

    assert isinstance(index, MemoryIndex)
    assert owned is None


def test_the_in_memory_adapter_never_sees_an_extraction_cache_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``mode == "test"`` early return precedes the P-90 store
    construction point (P-90 task 6): the memory adapter must never see
    one, so building it must never even try to construct one."""

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "ExtractionCacheStore must not be constructed in test mode"
        )

    monkeypatch.setattr(composition, "ExtractionCacheStore", fail)

    index, owned = _default_index(
        _config(tmp_path, mode="test"), writer_gate=threading.Lock()
    )

    assert isinstance(index, MemoryIndex)
    assert owned is None


def test_a_disabled_index_owns_nothing(tmp_path: Path) -> None:
    index, owned = _default_index(
        _config(tmp_path, graphiti=False), writer_gate=threading.Lock()
    )

    assert index is None
    assert owned is None


def test_the_semaphore_limit_bridge_reaches_env_and_the_bound_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator's ruling, 25 August 2026: ``graphiti.semaphore_limit`` is the
    configured form of graphiti-core's dial (I-19: configuration is the
    single layer). The environment variable alone would be dead — the
    library bound it at import time — so the bridge must also rebind the
    module attribute every ``semaphore_gather`` resolves at call time."""
    import graphiti_core.helpers as graphiti_helpers

    monkeypatch.setattr(graphiti_helpers, "SEMAPHORE_LIMIT", 20)
    monkeypatch.delenv("SEMAPHORE_LIMIT", raising=False)

    composition._export_semaphore_limit(7)

    assert os.environ["SEMAPHORE_LIMIT"] == "7"
    assert graphiti_helpers.SEMAPHORE_LIMIT == 7


@pytest.mark.anyio
async def test_the_lifespan_closes_the_adapter_it_built(
    tmp_path: Path, recorded: list[RecordingIndex]
) -> None:
    """The defect this file exists for: before it, this count stayed at
    zero for the life of the process."""
    config = _config(tmp_path)
    migrate_catalogue(config, lambda: NOW)
    application = build_application(config, clock=lambda: NOW)

    async with LifespanManager(application):
        assert len(recorded) == 1
        assert recorded[0].closes == 0

    assert recorded[0].closes == 1


@pytest.mark.anyio
async def test_repeated_lifecycles_close_every_adapter_they_build(
    tmp_path: Path, recorded: list[RecordingIndex]
) -> None:
    """One leaked thread per lifecycle is what made this invisible in
    steady state and unbounded under repeated start/stop."""
    for cycle in range(3):
        directory = tmp_path / f"cycle{cycle}"
        directory.mkdir()
        config = _config(directory)
        migrate_catalogue(config, lambda: NOW)
        application = build_application(config, clock=lambda: NOW)
        async with LifespanManager(application):
            pass

    assert len(recorded) == 3
    assert [index.closes for index in recorded] == [1, 1, 1]


@pytest.mark.anyio
async def test_an_injected_adapter_is_never_closed(tmp_path: Path) -> None:
    """The seam hands composition someone else's object — conformance
    builds adapters it inspects after the application is gone. Closing
    what it did not build would be composition reaching outside its own
    lifetime."""
    injected = RecordingIndex()
    config = _config(tmp_path)
    migrate_catalogue(config, lambda: NOW)

    application = build_application(config, clock=lambda: NOW, index_adapter=injected)
    async with LifespanManager(application):
        pass

    assert injected.closes == 0
