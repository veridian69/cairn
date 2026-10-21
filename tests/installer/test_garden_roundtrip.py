"""Pure framing checks for the stdlib-only, opt-in Garden acceptance tool."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "garden_roundtrip",
    Path(__file__).resolve().parents[1] / "acceptance/installer/garden_roundtrip.py",
)
assert _spec is not None and _spec.loader is not None
roundtrip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(roundtrip)


def test_frames_survive_split_utf8_and_coalesced_reads() -> None:
    frames = [
        {"jsonrpc": "2.0", "id": 1, "result": {"text": "Grüezi\nBern"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    raw = b"".join(roundtrip.encode_frame(frame) for frame in frames)
    assert raw.count(b"\n") == 2
    decoder = roundtrip.FrameDecoder()
    assert decoder.feed(raw) == frames
    decoder.finish()
    decoder = roundtrip.FrameDecoder()
    decoded = []
    for byte in raw:
        decoded.extend(decoder.feed(bytes([byte])))
    assert decoded == frames
    decoder.finish()


@pytest.mark.parametrize(
    "raw",
    [
        b"Content-Length: 40\r\n",
        b"[]\n",
        b"{}\n",
        b"null\n",
        b'{"jsonrpc":"1.0"}\n',
        b'{"jsonrpc":"2.0","x":"\xff"}\n',
        b"\n",
    ],
)
def test_decoder_rejects_invalid_framing(raw: bytes) -> None:
    with pytest.raises(roundtrip.CheckFailure):
        roundtrip.FrameDecoder().feed(raw)


def test_decoder_rejects_truncated_and_oversized_frames() -> None:
    decoder = roundtrip.FrameDecoder()
    decoder.feed(b'{"jsonrpc":"2.0"}')
    with pytest.raises(roundtrip.CheckFailure, match="truncated"):
        decoder.finish()
    for suffix in (b"", b"\n"):
        with pytest.raises(roundtrip.CheckFailure, match="too large"):
            roundtrip.FrameDecoder().feed(b"x" * roundtrip.MAX_FRAME + suffix)
    with pytest.raises(roundtrip.CheckFailure, match="too large"):
        roundtrip.encode_frame({"text": "x" * roundtrip.MAX_FRAME})


def test_tool_result_decodes_structured_and_legacy_text_content() -> None:
    assert roundtrip.tool_value({"structuredContent": {"acknowledged": True}}) == {
        "acknowledged": True
    }
    assert roundtrip.tool_value(
        {"content": [{"type": "text", "text": '{"id":"x"}'}]}
    ) == {"id": "x"}
    rejected: list[dict[str, Any]] = [
        {"isError": True, "structuredContent": {}},
        {"content": []},
        {},
        {"structuredContent": []},
    ]
    for result in rejected:
        with pytest.raises(roundtrip.CheckFailure):
            roundtrip.tool_value(result)


def test_channel_event_uses_real_content_and_meta_shape() -> None:
    params = {
        "content": "nonce",
        "meta": {"message_id": "m", "sender": "spike", "recipient": "val"},
    }
    assert roundtrip.channel_event({"method": roundtrip.CHANNEL, "params": params}) == {
        "content": "nonce",
        "message_id": "m",
        "sender": "spike",
        "recipient": "val",
    }
    assert roundtrip.channel_event({"method": "other", "params": params}) is None
    with pytest.raises(roundtrip.CheckFailure):
        roundtrip.channel_event(
            {"method": roundtrip.CHANNEL, "params": {"content": "nonce"}}
        )


def test_channel_miss_fails_even_when_history_has_the_nonce(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = {"id": "m1", "sequence": 7, "content": "nonce", "binding": "b"}

    class Missed:
        name = "val"
        timeout = 0.01
        direct = False

        def event(self, deadline: float) -> dict[str, Any]:
            raise TimeoutError

        def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
            assert tool == "read_messages"
            return {"generation": "g", "messages": [message]}

    with pytest.raises(roundtrip.CheckFailure, match="channel delivery missed"):
        roundtrip.receive(Missed(), message, "g", "online")
    output = capsys.readouterr().out
    assert "PASS" not in output
    assert "nonce in read_messages: yes" in output


def test_direct_delivery_gateway_skips_the_startup_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = {"instance_id": "i", "scope": {}, "classification": "internal"}

    class Direct:
        # Duck-typed: the module is loaded dynamically, so it cannot be subclassed.
        call = roundtrip.Client.call

        def __init__(self) -> None:
            self.name = "spike"
            self.timeout = 0.01
            self.direct = False
            self.sent: list[dict[str, Any]] = []

        def send(self, frame: dict[str, Any]) -> None:
            self.sent.append(frame)

        def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "initialize":
                return {
                    "protocolVersion": roundtrip.PROTOCOL_VERSION,
                    "capabilities": {"experimental": {"claude/channel": {}}},
                }
            assert params == {"name": "status", "arguments": {}}
            status = {
                "participant": "spike",
                "binding": binding,
                "generation": "g",
                "delivery": "direct",
            }
            return {"structuredContent": status}

        def event(self, deadline: float) -> dict[str, Any]:
            raise AssertionError("a direct gateway sends no startup probe")

    client = Direct()
    assert roundtrip.Client.start(client, binding) == "g"
    assert client.direct is True
    assert "direct delivery" in capsys.readouterr().out


@pytest.mark.parametrize("direct", [True, False])
def test_send_message_accepts_missing_outcomes_only_from_a_direct_gateway(
    direct: bool,
) -> None:
    binding = {"instance_id": "i", "scope": {}, "classification": "internal"}

    class Sender:
        name = "spike"

        def __init__(self) -> None:
            self.direct = direct

        def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
            assert tool == "send_message"
            return {
                "content": arguments["content"],
                "binding": binding,
                "author_name": "spike",
                "id": "m1",
                "sequence": 1,
                "deliveries": None,
                "recipients": ["val"],
            }

    if direct:
        assert roundtrip.send_message(Sender(), "val", binding, "online")["id"] == "m1"
    else:
        with pytest.raises(roundtrip.CheckFailure, match="no delivery outcomes"):
            roundtrip.send_message(Sender(), "val", binding, "online")


def test_direct_receive_reads_the_recipient_history(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = {"id": "m1", "sequence": 7, "content": "nonce", "binding": "b"}

    class Direct:
        name = "val"
        direct = True

        def __init__(self, messages: list[dict[str, Any]]) -> None:
            self.messages = messages

        def event(self, deadline: float) -> dict[str, Any]:
            raise AssertionError("a direct session has no channel push")

        def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
            assert tool == "read_messages"
            return {"generation": "g", "messages": self.messages}

    roundtrip.receive(Direct([message]), message, "g", "online")
    assert "read_messages; direct gateway" in capsys.readouterr().out
    with pytest.raises(roundtrip.CheckFailure, match="lacks the nonce"):
        roundtrip.receive(Direct([]), message, "g", "online")


def test_direct_send_must_still_address_the_recipient() -> None:
    binding = {"instance_id": "i", "scope": {}, "classification": "internal"}

    class Sender:
        name = "spike"
        direct = True

        def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
            return {
                "content": arguments["content"],
                "binding": binding,
                "author_name": "spike",
                "id": "m1",
                "sequence": 1,
                "deliveries": None,
                "recipients": ["zen"],
            }

    with pytest.raises(roundtrip.CheckFailure, match="not addressed to the recipient"):
        roundtrip.send_message(Sender(), "val", binding, "online")
