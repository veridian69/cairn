"""A deterministic OpenAI-compatible provider for the kind evidence tier.

Graphiti's production adapter must run during the restore rehearsal, but an
acceptance transcript must not depend on a public model provider or a secret.
This server implements the two OpenAI endpoints Graphiti can use. Structured
responses contain the smallest value admitted by the requested JSON schema;
for extraction schemas that means empty entity and edge lists. The episode is
still durably written to FalkorDB and remains retrievable through Graphiti's
episode BM25 path, so ``rebuild-index`` exercises the real production adapter
without making model quality part of a packaging test.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_EMBEDDING_DIMENSIONS = 1024


def _resolve_reference(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError("only local JSON Schema references are supported")
    value: Any = root
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or key not in value:
            raise ValueError("unresolved JSON Schema reference")
        value = value[key]
    if not isinstance(value, dict):
        raise ValueError("JSON Schema reference did not resolve to an object")
    return value


def value_for_schema(schema: dict[str, Any]) -> Any:
    """Return the smallest deterministic value admitted by ``schema``."""

    def value(node: dict[str, Any]) -> Any:
        reference = node.get("$ref")
        if isinstance(reference, str):
            return value(_resolve_reference(schema, reference))
        enum = node.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        for keyword in ("anyOf", "oneOf"):
            alternatives = node.get(keyword)
            if isinstance(alternatives, list) and alternatives:
                selected = next(
                    (
                        option
                        for option in alternatives
                        if isinstance(option, dict) and option.get("type") != "null"
                    ),
                    alternatives[0],
                )
                if not isinstance(selected, dict):
                    raise ValueError("invalid JSON Schema alternative")
                return value(selected)
        kind = node.get("type")
        if isinstance(kind, list):
            kind = next((item for item in kind if item != "null"), "null")
        if kind == "object" or "properties" in node:
            properties = node.get("properties", {})
            required = node.get("required", [])
            if not isinstance(properties, dict) or not isinstance(required, list):
                raise ValueError("invalid object JSON Schema")
            return {
                name: value(properties[name])
                for name in required
                if isinstance(name, str) and isinstance(properties.get(name), dict)
            }
        if kind == "array":
            return []
        if kind == "string":
            return ""
        if kind == "integer":
            return 0
        if kind == "number":
            return 0.0
        if kind == "boolean":
            return False
        if kind == "null":
            return None
        raise ValueError("unsupported JSON Schema node")

    return value(schema)


class _Handler(BaseHTTPRequestHandler):
    server_version = "cairn-kind-provider/1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > _MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
            if self.path == "/v1/responses":
                response = self._responses(request)
            elif self.path == "/v1/embeddings":
                response = self._embeddings(request)
            else:
                self._send(404, {"error": {"message": "unknown endpoint"}})
                return
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send(400, {"error": {"message": "invalid request"}})
            return
        self._send(200, response)

    def _responses(self, request: dict[str, Any]) -> dict[str, Any]:
        text = request.get("text")
        output_format = text.get("format") if isinstance(text, dict) else None
        schema = (
            output_format.get("schema") if isinstance(output_format, dict) else None
        )
        if not isinstance(schema, dict):
            raise ValueError("structured response schema is absent")
        output_text = json.dumps(value_for_schema(schema), separators=(",", ":"))
        model = request.get("model")
        if not isinstance(model, str):
            raise ValueError("model is absent")
        return {
            "id": "resp_cairn_kind_acceptance",
            "created_at": int(time.time()),
            "model": model,
            "object": "response",
            "output": [
                {
                    "id": "msg_cairn_kind_acceptance",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": output_text,
                            "annotations": [],
                            "logprobs": [],
                        }
                    ],
                }
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    def _embeddings(self, request: dict[str, Any]) -> dict[str, Any]:
        model = request.get("model")
        values = request.get("input")
        if not isinstance(model, str):
            raise ValueError("model is absent")
        count = len(values) if isinstance(values, list) and values else 1
        return {
            "object": "list",
            "model": model,
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [0.0] * _EMBEDDING_DIMENSIONS,
                }
                for index in range(count)
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }

    def _send(self, status: int, document: dict[str, Any]) -> None:
        body = json.dumps(document, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_arguments: object) -> None:
        return


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv: list[str]) -> int:
    port = int(argv[1])
    server = make_server("0.0.0.0", port)
    print(f"listening on {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
