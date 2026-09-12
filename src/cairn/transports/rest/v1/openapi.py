"""Deterministic OpenAPI 3.1 generation for the ``/v1`` contract (I-76,
P-30), and the read side of the packaged artefact.

The document is assembled from the strict wire models rather than from the
running application: every route handler takes a raw ``Request`` and
returns a raw ``Response``, so FastAPI's own generator sees nothing but
ten untyped endpoints. Assembling it here is what makes the artefact a
statement about the models P-33 governs instead of about the framework.

Generation is deterministic in the sense I-76 requires — sorted keys, two
space indentation, a trailing newline, and no timestamp, hostname,
product version or other environment-derived content anywhere in the
document. ``info.version`` is the contract's own version, not the
product's: the product version comes from ``git describe`` and would make
the artefact dirty on every commit, while ``GET /v1/instance`` is where a
caller learns which build is answering.

``make contract`` renders this module to ``contracts/cairn-openapi-v1.json``
and into the package, and ``make check`` fails on a dirty diff, so the
committed document is never hand-maintained.
"""

import json
import sys
from importlib import resources

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from cairn.catalogue.transactions import FailureCode
from cairn.runtime.logging import Operation
from cairn.transports.rest.v1.errors import STATUS_BY_FAILURE_CODE
from cairn.transports.v1.operations import OPERATIONS, OperationEntry
from cairn.transports.v1.responses import AUDIT_EVENT_SCHEMA, AUDIT_EVENT_SCHEMA_NAME
from cairn.transports.v1.wire import CONTRACT_IDENTITY, FailureEnvelope

# The artefact's path inside the installed package. The repository copy at
# ``contracts/`` is generated from the same render in the same ``make
# contract`` run, and a test holds the two byte-identical; this is the one
# an instance actually reads, because a container has no repository.
PACKAGED_ARTEFACT = "contracts/cairn-openapi-v1.json"

OPENAPI_VERSION = "3.1.0"
CONTRACT_VERSION = "v1"

_REF_TEMPLATE = "#/components/schemas/{model}"
_SECURITY_SCHEME = "bearerAuth"

_READ_ROUTE_DESCRIPTION = (
    "This is a read: supplying an Idempotency-Key header is "
    "invalid_request. The header is required on all eight mutations and "
    "refused on both reads."
)

_DESCRIPTION = (
    "The Cairn v0.1 memory authority surface. Every operation "
    "authenticates with an opaque bearer credential; scope isolation is "
    "enforced server-side and sibling scopes are never visible. Route "
    "names are the closed operation vocabulary itself, so this document "
    "is a one-to-one map of the authority model.\n\n"
    "Two boundary behaviours are deliberately absent from the per-"
    "operation responses below, because neither is a response to a valid "
    "call on the operation it would sit under: an unknown path returns "
    "the fixed not_found failure, and a known path with a wrong method "
    "returns invalid_request with status 405."
)


# P-51: the routes are the shared operation table, read straight through.
# This module owns how a route is *documented*, not which routes exist.
_ROUTES = OPERATIONS

# What REST has to add to an operation's shared summary, and nothing more.
# Both are the same point — a read is a POST here because its request
# carries a scope path, and I-32 keeps scope text out of URLs — and it is
# REST's to explain because MCP has no verb to justify.
_REST_SUMMARY_NOTES = {
    Operation.READ_AUDIT_EVENTS: (
        "A POST because scope paths must never appear in a URL."
    ),
    Operation.RETRIEVE: (
        "A POST because the scope path and the query must never appear in a URL."
    ),
}

# The header parameters' own schema. The audit document's field schemas
# moved to ``transports/v1/responses.py`` with the document they describe;
# this one stays because a header is REST's alone.
_UUID: dict[str, object] = {"type": "string", "format": "uuid"}


def render_document() -> str:
    """The artefact's exact text. Two calls return identical bytes."""
    return json.dumps(_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def packaged_contract_bytes() -> bytes:
    """The artefact as it ships in the wheel. Raises if the package is
    built without it, which is a broken build rather than a runtime
    condition — ``build_application`` refuses to start on it."""
    return resources.files("cairn").joinpath(PACKAGED_ARTEFACT).read_bytes()


def _document() -> dict[str, object]:
    schemas = _schemas()
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "Cairn",
            "version": CONTRACT_VERSION,
            "summary": f"The {CONTRACT_IDENTITY} contract.",
            "description": _DESCRIPTION,
            "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
        },
        "security": [{_SECURITY_SCHEME: []}],
        "paths": {route.path: _path_item(route) for route in _ROUTES},
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                _SECURITY_SCHEME: {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "An opaque Cairn credential. TLS is mandatory outside loopback."
                    ),
                }
            },
        },
    }


def _schemas() -> dict[str, object]:
    """Every wire model's schema under a stable name.

    Pydantic mangles a parameterised generic's name — the envelope over
    ``IngestResult`` arrives as ``SuccessEnvelope_IngestResult_`` — which
    would reach a generated client as a type name. The mangled names are
    read back out of the key map rather than predicted, and renamed to
    ``<Result>Envelope``.

    Only the definition keys are renamed, because no schema references an
    envelope: the envelopes are top-level response bodies and reference
    their results, never the other way round. Should that ever change, the
    reference would dangle rather than resolve to a stale name, and
    ``test_every_reference_resolves`` is what says so.
    """
    models: list[tuple[type[BaseModel], JsonSchemaMode]] = [
        (FailureEnvelope, "serialization")
    ]
    for route in _ROUTES:
        if route.request is not None:
            models.append((route.request, "validation"))
        models.append((route.success, "serialization"))

    key_map, definitions = models_json_schema(models, ref_template=_REF_TEMPLATE)
    renames = {
        _schema_name(key_map[(route.success, "serialization")]): _success_name(route)
        for route in _ROUTES
        if route.mutation
    }
    schemas: dict[str, object] = {
        renames.get(name, name): _components_refs(schema)
        for name, schema in definitions["$defs"].items()
    }
    schemas[AUDIT_EVENT_SCHEMA_NAME] = AUDIT_EVENT_SCHEMA
    return schemas


def _components_refs(node: object) -> object:
    """Rewrites a shared model's ``$defs`` reference into this document's
    own namespace.

    Only the hand-authored audit document arrives this way — every
    generated schema already carries ``_REF_TEMPLATE``, because Pydantic
    was told to use it. A model shared by two transports cannot name
    OpenAPI's components section, which is why the model names ``$defs``
    and the translation happens here rather than there.
    """
    if type(node) is dict:
        return {
            key: (
                str(value).replace("#/$defs/", "#/components/schemas/")
                if key == "$ref"
                else _components_refs(value)
            )
            for key, value in node.items()
        }
    if type(node) is list:
        return [_components_refs(item) for item in node]
    return node


def _success_name(route: OperationEntry) -> str:
    if route.mutation:
        return f"{route.result.__name__}Envelope"
    return route.result.__name__


def _schema_name(reference: dict[str, object]) -> str:
    return str(reference["$ref"]).rsplit("/", 1)[1]


def _path_item(route: OperationEntry) -> dict[str, object]:
    operation: dict[str, object] = {
        "operationId": route.operation.value,
        "summary": _summary(route),
        "parameters": _parameters(route),
        "responses": _responses(route),
    }
    if not route.mutation:
        operation["description"] = _READ_ROUTE_DESCRIPTION
    if route.request is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": _REF_TEMPLATE.format(model=route.request.__name__)
                    }
                }
            },
        }
    return {route.method: operation}


def _summary(route: OperationEntry) -> str:
    """The shared sentence, plus REST's own note where it has one."""
    note = _REST_SUMMARY_NOTES.get(route.operation)
    return f"{route.summary} {note}" if note else route.summary


def _parameters(route: OperationEntry) -> list[dict[str, object]]:
    """I-27 and I-32 at the header boundary.

    ``Idempotency-Key`` is required on every mutation and rejected on the
    two reads, so it is simply absent there — a parameter list has no way
    to say "forbidden", and an omitted parameter reads as "not used"
    rather than "refused". ``_READ_ROUTE_DESCRIPTION`` carries the
    prohibition in prose on those two operations, for the same reason
    I-71 has ``limit``'s description carry its pagination consequence.
    """
    parameters: list[dict[str, object]] = [
        {
            "name": "X-Correlation-ID",
            "in": "header",
            "required": False,
            "description": (
                "A caller-supplied UUIDv4 is adopted; any other value is "
                "ignored in favour of a fresh one, never rejected. Echoed "
                "on every response."
            ),
            "schema": dict(_UUID),
        }
    ]
    if route.mutation:
        parameters.append(
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "description": (
                    "A canonical lowercase hyphenated UUID. Replaying one "
                    "returns the original receipt with outcome 'replayed'."
                ),
                "schema": dict(_UUID),
            }
        )
    return parameters


def _responses(route: OperationEntry) -> dict[str, object]:
    responses: dict[str, object] = {
        "200": {
            "description": "The operation's result.",
            "headers": _correlation_header(),
            "content": {
                "application/json": {
                    "schema": {"$ref": _REF_TEMPLATE.format(model=_success_name(route))}
                }
            },
        }
    }
    for status in _failure_statuses(route):
        responses[str(status)] = _failure_response(status)
    return responses


def _failure_statuses(route: OperationEntry) -> tuple[int, ...]:
    """The I-73 table in full, which never varies by operation, plus the
    two admission refinements — and those two only where a body is
    admitted, since admission is what raises them."""
    statuses = set(STATUS_BY_FAILURE_CODE.values())
    if route.request is not None:
        statuses |= {413, 415}
    return tuple(sorted(statuses))


def _failure_response(status: int) -> dict[str, object]:
    headers = _correlation_header()
    if status == 401:
        headers["WWW-Authenticate"] = {
            "description": "The bearer challenge.",
            "schema": {"type": "string"},
        }
    if status == 503:
        headers["Retry-After"] = {
            "description": "Seconds to wait before retrying.",
            "schema": {"type": "integer"},
        }
    return {
        "description": _failure_description(status),
        "headers": headers,
        "content": {
            "application/json": {
                "schema": {"$ref": _REF_TEMPLATE.format(model="FailureEnvelope")}
            }
        },
    }


def _failure_description(status: int) -> str:
    codes = sorted(
        code.value
        for code, mapped in STATUS_BY_FAILURE_CODE.items()
        if mapped == status
    )
    if not codes:
        # 413 and 415: admission refusals keep the invalid_request body
        # under their own refined status (I-73).
        codes = [FailureCode.INVALID_REQUEST.value]
    return f"Failure: {', '.join(codes)}."


def _correlation_header() -> dict[str, object]:
    return {
        "X-Correlation-ID": {
            "description": "The correlation identifier for this request.",
            "schema": dict(_UUID),
        }
    }


if __name__ == "__main__":
    # Bytes, not text: the artefact is UTF-8 regardless of the locale the
    # generator happens to run under.
    sys.stdout.buffer.write(render_document().encode("utf-8"))
