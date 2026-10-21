#!/usr/bin/env python3
"""Exercise a running Garden using two exported participant bundles.

Run with stdlib only, for example:
  python3 -I garden_roundtrip.py --garden /absolute/path/garden \
      --bundle-dir /absolute/path/garden/profiles

The bundle directory contains spike/ and val/ by default. Override their names
with --sender and --recipient. Only a temporary GARDEN_HOME is written. Messages
are posted to the supplied deployment, so use acceptance participants. Checks
fail fast; each completed check prints PASS, the failing check prints FAIL.
History fallback proves visibility, not attention acknowledgement: only an
actual channel notification can be acknowledged (stdio.go's pending ID rule).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = "2025-11-25"
CHANNEL = "notifications/claude/channel"
MAX_FRAME = 256 * 1024
BINDING_FIELDS = ("instance_id", "scope", "classification")


class CheckFailure(Exception):
    """Safe diagnostic, containing no credential or remote message contents."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def encode_frame(frame: dict[str, Any]) -> bytes:
    """Garden Stdio.Run scans one JSON object per line, without LSP headers."""
    raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
    require(len(raw) < MAX_FRAME, "outgoing JSON-RPC frame is too large")
    return raw + b"\n"


class FrameDecoder:
    """Incrementally decode bounded lines, including split UTF-8 characters."""

    def __init__(self) -> None:
        self.pending = b""

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self.pending += data
        frames = []
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            require(len(line) < MAX_FRAME, "incoming JSON-RPC frame is too large")
            try:
                frame = json.loads(line)
            except (ValueError, UnicodeError) as error:
                raise CheckFailure("invalid JSON-RPC line") from error
            require(
                isinstance(frame, dict) and frame.get("jsonrpc") == "2.0",
                "invalid JSON-RPC envelope",
            )
            frames.append(frame)
        require(len(self.pending) < MAX_FRAME, "incoming JSON-RPC frame is too large")
        return frames

    def finish(self) -> None:
        require(not self.pending, "truncated JSON-RPC line at EOF")


def tool_value(result: dict[str, Any]) -> dict[str, Any]:
    """Decode the real stdio.go structuredContent/text result envelope."""
    require(not result.get("isError"), "Garden tool returned isError")
    if "structuredContent" in result:
        value = result["structuredContent"]
    else:
        try:
            value = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise CheckFailure("Garden tool result has no JSON content") from error
    if not isinstance(value, dict):
        raise CheckFailure("Garden tool result is not an object")
    return value


def channel_event(frame: dict[str, Any]) -> dict[str, Any] | None:
    if frame.get("method") != CHANNEL:
        return None
    params = frame.get("params", {})
    require(isinstance(params, dict), "invalid channel parameters")
    meta = params.get("meta", {})
    require(
        isinstance(meta, dict)
        and all(
            isinstance(meta.get(key), str) and meta[key]
            for key in ("message_id", "sender", "recipient")
        )
        and isinstance(params.get("content"), str),
        "invalid channel content or metadata",
    )
    return {**meta, "content": params["content"]}


class Client:
    def __init__(
        self, binary: str, env: dict[str, str], name: str, timeout: float
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.process = subprocess.Popen(
            [binary, "connect", "--harness", "claude"],
            env={**env, "GARDEN_IDENTITY": name},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert self.process.stdout is not None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.decoder = FrameDecoder()
        self.frames: deque[dict[str, Any]] = deque()
        self.events: deque[dict[str, Any]] = deque()
        self.serial = 0
        # A gateway without heartbeat (before 0.8) polls directly: no startup probe.
        self.direct = False

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.selector.close()
        if self.process.stdout is not None:
            self.process.stdout.close()

    def send(self, frame: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(encode_frame(frame))
        self.process.stdin.flush()

    def next_frame(self, deadline: float) -> dict[str, Any]:
        while not self.frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Garden response timed out")
            assert self.process.stdout is not None
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                self.decoder.finish()
                raise CheckFailure("Garden child closed stdout")
            self.frames.extend(self.decoder.feed(data))
        return self.frames.popleft()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.serial += 1
        self.send(
            {"jsonrpc": "2.0", "id": self.serial, "method": method, "params": params}
        )
        deadline = time.monotonic() + self.timeout
        while True:
            frame = self.next_frame(deadline)
            if "id" not in frame:
                event = channel_event(frame)
                if event is not None:
                    self.events.append(event)
                continue
            require(frame.get("id") == self.serial, "unexpected JSON-RPC response ID")
            require("error" not in frame, "Garden returned a JSON-RPC error")
            result = frame.get("result")
            if not isinstance(result, dict):
                raise CheckFailure("missing JSON-RPC result")
            return result

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        return tool_value(
            self.request("tools/call", {"name": name, "arguments": arguments})
        )

    def event(self, deadline: float) -> dict[str, Any]:
        while not self.events:
            frame = self.next_frame(deadline)
            require("id" not in frame, "unexpected response while awaiting channel")
            event = channel_event(frame)
            if event is not None:
                self.events.append(event)
        return self.events.popleft()

    def start(self, binding: dict[str, Any]) -> str:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "garden-roundtrip", "version": "1"},
            },
        )
        require(
            result.get("protocolVersion") == PROTOCOL_VERSION,
            "wrong negotiated protocol version",
        )
        require(
            "claude/channel" in result.get("capabilities", {}).get("experimental", {}),
            "Claude channel capability missing",
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        status = self.call("status")
        require(status.get("participant") == self.name, "status participant mismatch")
        require(status.get("binding") == binding, "status binding mismatch")
        generation = status.get("generation")
        if not isinstance(generation, str) or not generation:
            raise CheckFailure("status generation missing")
        print(f"PASS {self.name} initialize/status (binding, participant, generation)")
        if status.get("delivery") == "direct":
            self.direct = True
            print(f"PASS {self.name} direct delivery (gateway predates heartbeat)")
            return generation
        deadline = time.monotonic() + self.timeout
        deferred = []
        while True:
            event = self.event(deadline)
            if event["sender"] == "garden" and event["recipient"] == self.name:
                ack = self.call("acknowledge_delivery", message_id=event["message_id"])
                require(
                    ack.get("acknowledged") is True,
                    "startup probe was not acknowledged",
                )
                break
            deferred.append(event)
        self.events.extendleft(reversed(deferred))
        print(f"PASS {self.name} startup acknowledge_delivery")
        return generation


def seed_home(root: Path, bundles: Path, sender: str, recipient: str) -> dict[str, Any]:
    first = json.loads((bundles / sender / "garden.json").read_text())
    second = json.loads((bundles / recipient / "garden.json").read_text())
    require(
        all(
            first.get(key) == second.get(key)
            for key in (
                "garden_endpoint",
                "garden_tls_ca_file",
                "garden_tls_server_name",
                *BINDING_FIELDS,
            )
        ),
        "participant bundles have different Garden bindings",
    )
    ca = first.get("garden_tls_ca_file")
    require(ca in (None, "", "tls/ca.crt"), "bundle CA path must be tls/ca.crt")
    if ca:
        require(
            (bundles / sender / ca).read_bytes()
            == (bundles / recipient / ca).read_bytes(),
            "participant CAs differ",
        )
        (root / "tls").mkdir(mode=0o700)
        shutil.copyfile(bundles / sender / ca, root / ca)
        (root / ca).chmod(0o600)
    (root / "garden.json").write_text(json.dumps(first) + "\n")
    (root / "garden.json").chmod(0o600)
    return {key: first[key] for key in BINDING_FIELDS}


def history_has(client: Client, message: dict[str, Any], generation: str) -> bool:
    result = client.call(
        "read_messages",
        after_seq=message["sequence"] - 1,
        generation=generation,
        limit=100,
    )
    require(result.get("generation") == generation, "history generation changed")
    return any(
        item.get("id") == message["id"]
        and item.get("content") == message["content"]
        and item.get("binding") == message["binding"]
        for item in result.get("messages", [])
    )


def receive(
    client: Client, message: dict[str, Any], generation: str, label: str
) -> None:
    if client.direct:
        # A direct session has no channel push; the harness reads its history.
        require(
            history_has(client, message, generation),
            "recipient read_messages lacks the nonce",
        )
        print(f"PASS {label} receive (read_messages; direct gateway)")
        return
    deadline = time.monotonic() + client.timeout
    while True:
        try:
            event = client.event(deadline)
        except TimeoutError:
            # History shows whether the message arrived at all; delivery
            # acceptance still needs the channel notification.
            visible = history_has(client, message, generation)
            print(f"INFO {label} nonce in read_messages: {'yes' if visible else 'no'}")
            raise CheckFailure("channel delivery missed the nonce") from None
        if event["message_id"] != message["id"]:
            # A repeated startup probe or older queued message must not block this run.
            require(event["recipient"] == client.name, "channel recipient mismatch")
            ack = client.call("acknowledge_delivery", message_id=event["message_id"])
            require(
                ack.get("acknowledged") is True, "queued channel acknowledgement failed"
            )
            continue
        require(
            event["recipient"] == client.name
            and event["sender"] == message["author_name"]
            and message["content"] in event["content"],
            "channel nonce or metadata mismatch",
        )
        print(f"PASS {label} receive (Claude channel nonce)")
        ack = client.call("acknowledge_delivery", message_id=event["message_id"])
        require(ack.get("acknowledged") is True, "delivery acknowledgement failed")
        print(f"PASS {label} acknowledge_delivery")
        return


def send_message(
    client: Client, recipient: str, binding: dict[str, Any], label: str
) -> dict[str, Any]:
    nonce = "garden-roundtrip-" + label + "-" + str(uuid4())
    message = client.call("send_message", content=nonce, recipients=[recipient])
    require(
        message.get("content") == nonce
        and message.get("binding") == binding
        and message.get("author_name") == client.name
        and message.get("id")
        and type(message.get("sequence")) is int
        and message["sequence"] > 0,
        "send_message result does not identify the nonce and binding",
    )
    outcomes = message.get("deliveries")
    if client.direct and outcomes is None:
        # Before heartbeat, the gateway reports no attention outcomes; history is
        # shared, so the message itself must still name the recipient.
        recipients = message.get("recipients")
        require(
            isinstance(recipients, list) and recipient in recipients,
            "send_message is not addressed to the recipient",
        )
        print(f"PASS {label} send_message (unique nonce; direct gateway)")
        return message
    if not isinstance(outcomes, list):
        raise CheckFailure("send_message has no delivery outcomes")
    require(
        any(
            item.get("recipient") == recipient
            and item.get("delivery")
            in ({"queued"} if label == "offline" else {"notified", "queued"})
            for item in outcomes
        ),
        "attention was suppressed or not addressed to recipient",
    )
    print(f"PASS {label} send_message (unique nonce, addressed recipient)")
    return message


def run(args: argparse.Namespace) -> None:
    with (
        tempfile.TemporaryDirectory(prefix="garden-roundtrip-") as directory,
        ExitStack() as stack,
    ):
        home = Path(directory)
        binding = seed_home(home, args.bundle_dir, args.sender, args.recipient)
        env = {**os.environ, "GARDEN_HOME": str(home)}
        print("PASS temporary Garden home from matching bundles")
        for name in (args.sender, args.recipient):
            result = subprocess.run(
                [
                    args.garden,
                    "home",
                    "add-identity",
                    name,
                    "--token-file",
                    str(args.bundle_dir / name / "agent.token"),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=args.timeout,
                check=False,
            )
            require(result.returncode == 0, "home add-identity failed")
            print(f"PASS add-identity {name}")
        sender = Client(args.garden, env, args.sender, args.timeout)
        stack.callback(sender.close)
        generation = sender.start(binding)
        recipient = Client(args.garden, env, args.recipient, args.timeout)
        stack.callback(recipient.close)
        require(
            recipient.start(binding) == generation,
            "participants have different generations",
        )
        message = send_message(sender, args.recipient, binding, "online")
        receive(recipient, message, generation, "online")
        require(
            history_has(sender, message, generation),
            "sender history lacks online nonce",
        )
        print("PASS online sender read_messages")
        if sender.direct or recipient.direct:
            # The offline variant observes the presence lease through who.
            print("SKIP offline variant (gateway predates who and heartbeat)")
            return
        recipient.close()
        # Closing the bridge does not release the server's 120-second lease.
        deadline = time.monotonic() + args.offline_timeout
        while True:
            roster = sender.call("who")
            if any(
                item.get("name") == args.recipient and item.get("online") is False
                for item in roster.get("participants", [])
            ):
                break
            require(time.monotonic() < deadline, "recipient did not become offline")
            time.sleep(1)
        print("PASS offline recipient disconnected (who)")
        message = send_message(sender, args.recipient, binding, "offline")
        recipient = Client(args.garden, env, args.recipient, args.timeout)
        stack.callback(recipient.close)
        require(
            recipient.start(binding) == generation, "generation changed on reconnect"
        )
        receive(recipient, message, generation, "offline")
        require(
            history_has(sender, message, generation),
            "sender history lacks offline nonce",
        )
        print("PASS offline sender read_messages")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--garden", required=True, help="Garden binary built from this worktree"
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        required=True,
        help="directory containing the per-participant bundles",
    )
    parser.add_argument("--sender", default="spike")
    parser.add_argument("--recipient", default="val")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="per-operation/channel deadline in seconds (default: 30)",
    )
    parser.add_argument(
        "--offline-timeout",
        type=float,
        default=150,
        help="wait for the server's 120-second presence lease to expire (default: 150)",
    )
    args = parser.parse_args()
    if (
        not math.isfinite(args.timeout)
        or args.timeout <= 0
        or args.timeout > 30
        or not math.isfinite(args.offline_timeout)
        or args.offline_timeout <= 0
        or args.sender == args.recipient
        or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)
            for name in (args.sender, args.recipient)
        )
    ):
        parser.error(
            "use distinct valid participant names, 0 < timeout <= 30 and positive offline-timeout"
        )
    args.bundle_dir = args.bundle_dir.resolve()
    args.garden = str(Path(args.garden).resolve())
    try:
        run(args)
    except Exception as error:
        # Do not print remote payloads, token paths or command output on failure.
        detail = str(error) if isinstance(error, CheckFailure) else type(error).__name__
        print(f"FAIL round trip: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
