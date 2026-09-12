import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    live: bool
    started: bool
    ready: bool
    stopping: bool


class RuntimeStatus:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._started = False
        self._ready = False
        self._stopping = False

    async def mark_started(self) -> None:
        async with self._lock:
            self._started = True

    async def mark_ready(self) -> None:
        async with self._lock:
            if not self._started or self._stopping:
                raise RuntimeError("runtime cannot become ready")
            self._ready = True

    async def mark_stopping(self) -> None:
        async with self._lock:
            self._ready = False
            self._stopping = True

    async def snapshot(self) -> StatusSnapshot:
        async with self._lock:
            return StatusSnapshot(
                live=True,
                started=self._started,
                ready=self._ready,
                stopping=self._stopping,
            )
