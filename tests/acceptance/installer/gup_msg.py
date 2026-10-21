#!/usr/bin/env python3
"""Named Garden messages for the Garden upgrade acceptance trial."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

_rt_path = Path(__file__).with_name("garden_roundtrip.py")
_rt_spec = importlib.util.spec_from_file_location("garden_roundtrip", _rt_path)
if _rt_spec is None or _rt_spec.loader is None:
    raise ImportError("could not load garden_roundtrip.py")
rt = importlib.util.module_from_spec(_rt_spec)
_rt_spec.loader.exec_module(rt)


class Client(rt.Client):  # type: ignore[name-defined,misc]
    """Keep initialise instructions for the disconnected-session diagnostic."""

    instructions = ""

    def request(self, method: str, params: dict[str, object]) -> Any:
        result = super().request(method, params)
        if method == "initialize":
            self.instructions = str(result.get("instructions", ""))
        return result


def valid_name(name: str) -> bool:
    """Accept a single conservative filename component."""
    return (
        bool(name)
        and name not in {".", ".."}
        and all(char.isascii() and (char.isalnum() or char in "_-") for char in name)
    )


def validate_ops(ops: Sequence[str]) -> list[tuple[str, str | None]]:
    parsed: list[tuple[str, str | None]] = []
    for op in ops:
        if op == "generation":
            parsed.append(("generation", None))
            continue
        kind, separator, name = op.partition(":")
        if not separator or kind not in {"send", "has"} or not valid_name(name):
            raise ValueError(f"invalid operation: {op}")
        parsed.append((kind, name))
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--garden", required=True)
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--lease-wait", type=float, default=160.0)
    parser.add_argument("ops", nargs="+")
    args = parser.parse_args(argv)
    if not math.isfinite(args.lease_wait) or args.lease_wait < 0:
        parser.error("--lease-wait must be finite and nonnegative")
    try:
        args.parsed_ops = validate_ops(args.ops)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="gup-") as directory, ExitStack() as stack:
        home = Path(directory)
        binding = rt.seed_home(home, args.bundle_dir, "spike", "val")
        env = {**os.environ, "GARDEN_HOME": str(home)}
        subprocess.run(
            [
                args.garden,
                "home",
                "add-identity",
                "spike",
                "--token-file",
                str(args.bundle_dir / "spike" / "agent.token"),
            ],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        deadline = time.monotonic() + args.lease_wait
        while True:
            client = Client(args.garden, env, "spike", 30)
            try:
                generation = client.start(binding)
            except rt.CheckFailure as error:
                client.close()
                if "capability missing" not in str(error):
                    raise
                print(
                    f"INFO disconnected: {client.instructions[:200]}", file=sys.stderr
                )
                rt.require(time.monotonic() < deadline, "spike lease still held")
                time.sleep(min(10, max(0, deadline - time.monotonic())))
            except BaseException:
                client.close()
                raise
            else:
                stack.callback(client.close)
                break
        for kind, name in args.parsed_ops:
            if kind == "generation":
                print(f"generation={generation}")
            elif kind == "send":
                assert name is not None
                nonce = f"gup-{name}-" + os.urandom(8).hex()
                message = client.call("send_message", content=nonce, recipients=["val"])
                rt.require(
                    message.get("content") == nonce
                    and type(message.get("sequence")) is int,
                    "send failed",
                )
                args.store.mkdir(parents=True, exist_ok=True)
                (args.store / f"{name}.json").write_text(
                    json.dumps(
                        {
                            key: message[key]
                            for key in ("id", "sequence", "content", "binding")
                        }
                    )
                )
                print(f"sent {name} seq={message['sequence']}")
            else:
                assert name is not None
                message = json.loads((args.store / f"{name}.json").read_text())
                present = rt.history_has(client, message, generation)
                print(f"{name} {'present' if present else 'absent'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
