"""Keep authenticated mutations flowing while the in-cluster backup runs."""

from __future__ import annotations

import json
import sys
import time

import client


def _is_retryable_contention(error: client.DeniedError) -> bool:
    if error.status != 503:
        return False
    try:
        failure = json.loads(error.detail).get("failure")
    except (AttributeError, json.JSONDecodeError):
        return False
    return (
        isinstance(failure, dict)
        and failure.get("code") == "dependency_unavailable"
        and failure.get("retry") == "after-delay"
    )


def main(argv: list[str]) -> int:
    base_url, token_path, marker = argv[1], argv[2], argv[3]
    with open(token_path, encoding="utf-8") as handle:
        token = handle.read().strip()
    completed = 0
    print("ready", flush=True)
    try:
        while True:
            try:
                client._ingest(base_url, token, f"{marker} {completed}")
            except client.DeniedError as error:
                if not _is_retryable_contention(error):
                    raise
                time.sleep(0.02)
                continue
            completed += 1
            print(f"mutations {completed} {time.time_ns()}", flush=True)
            time.sleep(0.02)
    except (client.RoundTripError, OSError) as error:
        print(f"failed after {completed}: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
