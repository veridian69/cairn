from cairn.runtime.status import StatusSnapshot

type HealthRendering = tuple[int, dict[str, str]]


def render_live(snapshot: StatusSnapshot) -> HealthRendering:
    return 200, {"status": "live"}


def render_startup(snapshot: StatusSnapshot) -> HealthRendering:
    if snapshot.started:
        return 200, {"status": "started"}
    return 503, {"status": "starting"}


def render_ready(snapshot: StatusSnapshot) -> HealthRendering:
    if snapshot.ready:
        return 200, {"status": "ready"}
    return 503, {"status": "not-ready"}
