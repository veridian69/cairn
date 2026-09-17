"""Trusted, provider-free session controller around the reviewed stdio API.

This is the synthetic slice of the native-controller design. It builds no host
argv and launches no host. A trusted fixture supplies a sealed stage and owns
the synthetic peer; the controller owns its bridge relay and joins its cleanup.
Terminal evidence must come from that trusted fixture, separately from MCP EOF.
It is not a model tool or native capability/dispatcher acceptance.

The fixed launcher is Invocation.launch: only borrowed pipe descriptors and a
cancellation event enter it. Manifest/deadline are bound once. The private
admission directory lives outside the sealed work slot and must be retained
for the invocation lifetime, including failure. Never reopen it as a new run.
As with the sandbox, a hostile same-UID controller is outside this boundary.
"""

from __future__ import annotations

import fcntl
import os
import select
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import UUID

from scripts.host_workflow_bridge import (
    MAX_MESSAGE_BYTES,
    BridgeFailure,
    decode_json,
    decode_message,
)
from scripts.host_workflow_sandbox import (
    Manifest,
    SandboxFailure,
    StdioResult,
    seal,
    serve_host_stdio,
)

_LIMIT = 8388608
_BUFFER = 65536


class ControllerFailure(RuntimeError):
    """Fixed code only; no peer content or paths in diagnostics."""


@dataclass(frozen=True)
class Terminal:
    """Trusted synthetic peer exit evidence; never parsed from bridge EOF."""

    returncode: int
    result: str
    receipt_ids: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    """Bounded retained result; raw transport and diagnostics are not retained."""

    result: str
    receipt_ids: tuple[str, ...]
    input_bytes: int
    output_bytes: int
    stderr_bytes: int


class _Frames:
    def __init__(self) -> None:
        self.buffers = [bytearray(), bytearray()]
        self.counts = [0, 0]
        self.pending: dict[tuple[type, str | int], dict] = {}
        self.seen: set[tuple[type, str | int]] = set()
        self.receipts: set[str] = set()

    def feed(self, direction: int, raw: bytes) -> None:
        buffer = self.buffers[direction]
        buffer.extend(raw)
        while (end := buffer.find(b"\n")) >= 0:
            frame = bytes(buffer[:end])
            del buffer[: end + 1]
            self.counts[direction] += 1
            if self.counts[direction] > 128:
                raise ControllerFailure("native_message_limit")
            try:
                value = decode_message(frame).model_dump(exclude_none=True)
            except BridgeFailure:
                raise ControllerFailure("native_invalid_frame") from None
            if direction == 0:
                if "method" not in value:
                    raise ControllerFailure("native_invalid_frame")
                if "id" in value:
                    key = (type(value["id"]), value["id"])
                    if key in self.seen or self.pending:
                        raise ControllerFailure("native_request_overlap")
                    self.seen.add(key)
                    self.pending[key] = value
            else:
                if "method" in value and "id" not in value:
                    continue  # Forward SDK notifications unchanged; not receipts.
                if "method" in value or "id" not in value:
                    raise ControllerFailure("native_unexpected_response")
                key = (type(value["id"]), value["id"])
                if key not in self.pending:
                    raise ControllerFailure("native_unexpected_response")
                request = self.pending.pop(key)
                self._receipt(request, value)
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise ControllerFailure("native_invalid_frame")

    def eof(self, direction: int) -> None:
        if self.buffers[direction]:
            raise ControllerFailure("native_partial_frame")
        if self.pending:
            raise ControllerFailure("native_pending_request")

    def _receipt(self, request: dict, response: dict) -> None:
        """Observe actual committed CLI receipts, never infer custody from EOF."""
        try:
            params = request["params"]
            args = params["arguments"]
            argv = args["argv"]
            if (
                request["method"] != "tools/call"
                or params["name"] != "run_daily_cli"
                or argv
                not in (
                    ["--profile", "/cli/profile.json", "remember"],
                    ["--profile", "/cli/profile.json", "resume"],
                )
            ):
                return
            answer = response["result"]
            if answer.get("isError", False):
                return
            packet = decode_json(answer["content"][0]["text"].encode())
            if packet["exit_code"] != 0:
                return
            document = decode_json(packet["stdout"].encode())
            body = document["result"]
            original = decode_json(args["stdin"].encode())
            if (
                document["schema"] != "cairn.memory-command/v1"
                or document["command"] != argv[-1]
                or body["state"] != "committed"
                or body["turn_id"] != original["turn_id"]
            ):
                return
            identities = body["persistence"]["result"]["fact_ids"]
            if (
                not isinstance(identities, list)
                or not identities
                or len(identities) > 256
            ):
                return
            if not all(
                type(identity) is str and str(UUID(identity)) == identity
                for identity in identities
            ):
                return
            self.receipts.update(identities)
            if len(self.receipts) > 256:
                raise ControllerFailure("native_receipt_limit")
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            return  # Missing/invalid evidence can never become a completion receipt.


class Invocation:
    """One controller invocation, one bridge admission, one absolute budget.

    Construct only in a trusted private Linux fixture directory. No endpoint,
    profile, command, environment or filesystem override is accepted at launch.
    Caller owns peer pipes exclusively until launch returns; cancellation joins
    the independent sandbox monitor before staging may be released. Failed or
    concurrent admission never changes the first launch's completion state.
    """

    def __init__(self, manifest: Manifest, admission: Path, *, deadline: float):
        now = time.monotonic()
        if (
            type(deadline) not in (int, float)
            or not now < deadline <= now + 60
            or not isinstance(manifest, Manifest)
            or not admission.is_absolute()
            or admission.resolve() != admission
            or admission.is_relative_to(manifest.root)
        ):
            raise ControllerFailure("native_descriptor_invalid")
        try:
            admission.mkdir(mode=0o700)
        except OSError:
            raise ControllerFailure("native_descriptor_invalid") from None
        self._manifest = manifest
        self._admission = admission
        self._deadline = deadline
        self._frames = _Frames()
        self._transport: StdioResult | None = None
        self._failed = False
        self._finished = False

    def _admit(self) -> None:
        try:
            directory = os.open(self._admission, os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(directory)
                if (
                    metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    raise ControllerFailure("native_descriptor_invalid")
                fd = os.open(
                    "used",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                os.close(fd)
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            raise ControllerFailure("native_launcher_already_used") from None
        except OSError:
            raise ControllerFailure("native_descriptor_invalid") from None

    def _time(self, cancel: Event | None) -> float:
        if cancel is not None and cancel.is_set():
            raise ControllerFailure("native_cancelled")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ControllerFailure("native_deadline")
        return remaining

    def launch(
        self, input_fd: int, output_fd: int, *, cancel: Event | None = None
    ) -> StdioResult:
        self._admit()  # Consume before validation/spawn; even a failed attempt is final.
        try:
            if cancel is not None and not isinstance(cancel, Event):
                raise ControllerFailure("native_cancel_invalid")
            self._time(cancel)
            try:
                identities = []
                for fd, access in ((input_fd, os.O_RDONLY), (output_fd, os.O_WRONLY)):
                    if type(fd) is not int or fd < 0:
                        raise ValueError
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISFIFO(info.st_mode)
                        or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != access
                    ):
                        raise ValueError
                    identities.append((info.st_dev, info.st_ino))
                if identities[0] == identities[1]:
                    raise ValueError
            except (OSError, ValueError, OverflowError):
                raise ControllerFailure("native_pipe_invalid") from None
            transport = self._relay(input_fd, output_fd, cancel)
            self._time(cancel)
            if seal(self._manifest.root) != self._manifest:
                raise ControllerFailure("native_source_changed")
            self._time(cancel)
            self._transport = transport
            return self._transport
        except BaseException:
            self._failed = True
            raise

    def _relay(
        self, input_fd: int, output_fd: int, cancel: Event | None
    ) -> StdioResult:
        # Establish ownership before the first allocation. The bridge endpoints
        # transfer only under the startup lock; peer descriptors never do.
        owned: dict[int, None] = {}
        old_flags: dict[int, bool] = {}
        thread: Thread | None = None
        startup = Lock()
        abandoned = False
        claimed = False
        stop = Event()
        cleanup_failed = Event()
        results: list[StdioResult] = []
        errors: list[BaseException] = []

        def cleanup(action: Callable[[], object]) -> None:
            try:
                action()
            except Exception:
                cleanup_failed.set()

        def close_owned(fd: int) -> None:
            # A failed close may already have released/reused the descriptor.
            # Relinquish it before attempting the close; never retry its number.
            del owned[fd]
            cleanup(lambda: os.close(fd))

        def bridge() -> None:
            nonlocal claimed
            with startup:
                # start() can be interrupted before ident publication. A worker
                # arriving after caller reclamation must not use OR close FDs.
                if abandoned:
                    return
                del owned[bridge_input]
                del owned[bridge_output]
                claimed = True
            try:
                results.append(
                    serve_host_stdio(
                        self._manifest,
                        bridge_input,
                        bridge_output,
                        self._deadline,
                        cancel=stop,
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                cleanup(lambda: os.close(bridge_input))
                cleanup(lambda: os.close(bridge_output))

        try:
            bridge_input, send = os.pipe()
            owned.update({bridge_input: None, send: None})
            receive, bridge_output = os.pipe()
            owned.update({receive: None, bridge_output: None})
            for fd in (input_fd, output_fd):
                old_flags[fd] = os.get_blocking(fd)
            thread = Thread(target=bridge, name="synthetic-cairn-bridge")
            thread.start()
            pending = [bytearray(), bytearray()]
            counts = [0, 0]
            ended = [False, False]
            for fd in (input_fd, output_fd, send, receive):
                os.set_blocking(fd, False)
            while True:
                remaining = self._time(cancel)
                if errors:
                    raise errors[0]
                if ended[0] and not pending[0] and send is not None:
                    close_owned(send)
                    send = None
                    if cleanup_failed.is_set():
                        raise ControllerFailure("native_cleanup_failed")
                if all(ended) and not any(pending):
                    break
                reads = [
                    fd
                    for i, fd in enumerate((input_fd, receive))
                    if not ended[i] and not pending[i]
                ]
                writes = [
                    fd
                    for i, fd in enumerate((send, output_fd))
                    if pending[i] and fd is not None
                ]
                ready_read, ready_write, _ = select.select(
                    reads, writes, [], min(remaining, 0.05)
                )
                for i, fd in enumerate((input_fd, receive)):
                    if fd not in ready_read:
                        continue
                    chunk = os.read(fd, min(_BUFFER, _LIMIT - counts[i] + 1))
                    counts[i] += len(chunk)
                    if counts[i] > _LIMIT:
                        raise ControllerFailure(
                            "native_input_limit" if i == 0 else "native_output_limit"
                        )
                    if chunk:
                        self._frames.feed(i, chunk)
                        pending[i].extend(chunk)
                    else:
                        # Child failure takes precedence over a transport-side EOF.
                        if i == 1:
                            thread.join(min(remaining, 0.1))
                            if errors:
                                raise errors[0]
                            if not ended[0]:
                                raise ControllerFailure("native_premature_eof")
                        self._frames.eof(i)
                        ended[i] = True
                for i, fd in enumerate((send, output_fd)):
                    if fd in ready_write:
                        try:
                            sent = os.write(fd, pending[i])
                        except BrokenPipeError:
                            raise ControllerFailure("native_pipe_closed") from None
                        del pending[i][:sent]
            thread.join(self._time(cancel))
            if thread.is_alive():
                raise ControllerFailure("native_deadline")
            if errors:
                raise errors[0]
            if not results or (
                results[0].input_bytes,
                results[0].output_bytes,
            ) != tuple(counts):
                raise ControllerFailure("native_transport_incomplete")
            return results[0]
        except (ControllerFailure, SandboxFailure):
            raise
        except (OSError, RuntimeError):
            raise ControllerFailure("native_stdio_failed") from None
        finally:
            with startup:
                abandoned = True
                # Ownership is determined only by the synchronised claim. ident
                # is used solely to join an already-published non-owning worker;
                # an unpublished late worker will see abandonment and return.
                joinable = claimed or (thread is not None and thread.ident is not None)
            cleanup(stop.set)
            if joinable and thread is not None:
                cleanup(
                    lambda: thread.join(max(0, self._deadline - time.monotonic()) + 3)
                )
            self._frames.buffers = [bytearray(), bytearray()]
            self._frames.pending.clear()
            for fd in tuple(owned):
                close_owned(fd)
            for fd, blocking in old_flags.items():
                cleanup(lambda fd=fd, blocking=blocking: os.set_blocking(fd, blocking))
            if joinable and thread is not None:
                try:
                    if thread.is_alive():
                        cleanup_failed.set()
                except Exception:
                    cleanup_failed.set()
            if cleanup_failed.is_set():
                raise ControllerFailure("native_cleanup_failed") from None

    def finish(self, terminal: Terminal | None) -> Result:
        """Classify once, after transport drainage/reap and trusted peer exit.

        Supplying terminal evidence cannot repair a failed transport, reset its
        budget or manufacture a receipt. Native terminal parsing remains held.
        """
        if self._finished or self._failed or self._transport is None:
            raise ControllerFailure("native_session_incomplete")
        self._finished = True
        self._time(None)
        if terminal is None:
            raise ControllerFailure("native_terminal_missing")
        if (
            type(terminal) is not Terminal
            or type(terminal.returncode) is not int
            or terminal.returncode != 0
        ):
            raise ControllerFailure("native_peer_failed")
        try:
            if (
                type(terminal.result) is not str
                or not terminal.result
                or len(terminal.result.encode()) > 4096
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ControllerFailure("native_terminal_invalid") from None
        if (
            type(terminal.receipt_ids) is not tuple
            or not terminal.receipt_ids
            or len(terminal.receipt_ids) > 256
            or not all(type(identity) is str for identity in terminal.receipt_ids)
            or len(set(terminal.receipt_ids)) != len(terminal.receipt_ids)
            or set(terminal.receipt_ids) != self._frames.receipts
        ):
            raise ControllerFailure("native_receipts_missing")
        return Result(
            terminal.result,
            terminal.receipt_ids,
            self._transport.input_bytes,
            self._transport.output_bytes,
            self._transport.stderr_bytes,
        )
