from cairn.operations.health import render_live, render_ready, render_startup
from cairn.runtime.status import StatusSnapshot


def test_live_rendering_contains_only_fixed_status() -> None:
    snapshot = StatusSnapshot(
        live=True,
        started=False,
        ready=False,
        stopping=False,
    )

    assert render_live(snapshot) == (200, {"status": "live"})


def test_startup_rendering_tracks_started_state() -> None:
    starting = StatusSnapshot(
        live=True,
        started=False,
        ready=False,
        stopping=False,
    )
    started = StatusSnapshot(
        live=True,
        started=True,
        ready=False,
        stopping=False,
    )

    assert render_startup(starting) == (503, {"status": "starting"})
    assert render_startup(started) == (200, {"status": "started"})


def test_readiness_rendering_tracks_authority_state() -> None:
    not_ready = StatusSnapshot(
        live=True,
        started=True,
        ready=False,
        stopping=False,
    )
    ready = StatusSnapshot(
        live=True,
        started=True,
        ready=True,
        stopping=False,
    )

    assert render_ready(not_ready) == (503, {"status": "not-ready"})
    assert render_ready(ready) == (200, {"status": "ready"})
