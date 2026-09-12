"""Explicit everyday Cairn commands; all content is inert bounded stdin JSON."""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import BinaryIO, NoReturn
from uuid import UUID

import httpx

from cairn.client import (
    DurableMemorySession,
    DurableSessionFailure,
    DurableTurnResult,
    MemoryClient,
    MemoryOperationFailure,
)
from cairn.client.cli_input import (
    FIELDS,
    PROPOSAL_MUTATIONS,
    SESSION_COMMANDS,
    command_key,
    parse,
    preparation_size,
)
from cairn.client.command_io import CommandInputError
from cairn.client.profiles import ProfileError, load_credential, load_profile
from cairn.client.rendering import CommandOutputError, render_result
from cairn.client.session_validation import require_preparation_binding
from cairn.client.types import ConnectionStatus

_COMMON = """Content-bearing commands read one UTF-8 stdin JSON object (maximum 1048576 bytes).
Unknown/duplicate keys, nulls, non-finite numbers and authority overrides fail.
Only replaces_turn_id, superseded_by, observation timestamps and proposal-list after may be null.
Proposal reference IDs and cursors accept every canonical lowercase UUID version/variant.
Other IDs are RFC UUID strings; non-proposal fact IDs must be UUIDv4.
Exit: 0 completed; 2 invalid input/confirmed refusal or abandoned resume;
3 incomplete recovery/unconfirmed content mutation; 4 operational/output failure.
Status reads return 0 for every state. No automatic retries or model callbacks.
JSON output is cairn.memory-command/v1; --human selects pretty JSON.
"""
_QUERY = (
    "query: string, 1..8192 UTF-8 bytes. budget: integer 1..1048576 (default 16384)."
)
_TURN = '"turn_id":"11111111-1111-4111-8111-111111111111"'
_HELP = {
    "check": "No stdin read. Check explicit expected instance and current grants; ready is not a write permission guarantee.",
    "arrive": _QUERY
    + '\nhistory_fact_ids: optional distinct UUIDv4 array, 0..8. Opens/replays the configured session and issues a fresh visit. Never acknowledges. Example: {"query":"current work","history_fact_ids":[]}',
    "recall": _QUERY
    + '\nrelevant_only: optional boolean (default false). No session, visit or checkpoint mutation. Example: {"query":"new topic","relevant_only":true}',
    "acknowledge-visit": 'visit_id: required server-issued visit UUID. Explicitly acknowledge only after consuming arrival output. Example: {"visit_id":"11111111-1111-4111-8111-111111111111"}',
    "remember": "turn_id, attempt_id: required UUIDs; replaces_turn_id: optional UUID or null referencing an abandoned predecessor. response: required string, 0..32768 UTF-8 bytes. observations: required array, 0..8 objects, each body: string 1..4096 bytes; optional valid_from/valid_to/observed_at: canonical UTC timestamp or null, e.g. 2026-09-10T12:00:00.000000Z. valid_from must precede valid_to; all observed_at values must agree. Complete canonical preparation <=73728 bytes. Imports already completed output, always checks preparation even on replay. Lost unprepared output needs IDENTICAL checkpoint resubmission under the same identities; resume never regenerates it. Example: {"
    + _TURN
    + ',"attempt_id":"22222222-2222-4222-8222-222222222222","response":"Done","observations":[{"body":"The build uses port 8123."}]}',
    "status": "turn_id: optional UUID. Empty/whitespace stdin or terminal stdin means {}. Reads only; does not open a session. Example: {}",
    "resume": "turn_id: required UUID. No callback or replacement generation. Committed/skipped -> 0; interrupted/prepared -> 3; abandoned -> 2. Example: {"
    + _TURN
    + "}",
    "abandon": "turn_id: required UUID; reason: required string, 1..4096 UTF-8 bytes. Fences only an unprepared started turn, never a visit or prepared custody. Example: {"
    + _TURN
    + ',"reason":"Output was lost"}',
    "history": 'fact_id: required UUIDv4. budget: optional integer 1..1048576 (default 16384). Example: {"fact_id":"11111111-1111-4111-8111-111111111111"}',
    "correct": 'fact_ids: required distinct UUIDv4 array, 1..100; reason: required string, 1..4096 UTF-8 bytes; idempotency_key: required explicit UUID; superseded_by: optional UUIDv4 or null. Fixed profile scope; preserves history. No session_id required. If acknowledgement is lost or internal_error leaves the mutation unconfirmed (exit 3), resubmit the IDENTICAL correction with the SAME explicit idempotency_key and all original fields. Session status/resume cannot reconcile corrections. Example: {"fact_ids":["11111111-1111-4111-8111-111111111111"],"reason":"Corrected measurement","idempotency_key":"22222222-2222-4222-8222-222222222222"}',
    "suggest": 'Exactly one of observation: string 1..4096 UTF-8 bytes OR fact_ids: distinct UUIDv4 array 1..8. Optional budget: integer 1..65536 (default 16384); limit: integer 1..16 (default 8). No idempotency_key or authority overrides. Read only, no follow-up mutation. Complete attributed evidence, omissions and degradation remain untrusted data; empty results do not prove no duplicates. Network failures exit 4. Examples: {"observation":"The build uses port 8123.","budget":16384,"limit":8} or {"fact_ids":["11111111-1111-4111-8111-111111111111"]}',
}
_PROPOSAL_RECOVERY = (
    "No session required. Fixed profile source scope and expected instance. "
    "Mutation keys require canonical lowercase hyphenated RFC 4122 variant UUIDs "
    "(version unrestricted, including UUIDv5). "
    "Mutation keys are explicit and never generated. On exit 3, resubmit the IDENTICAL "
    "named proposal operation with the SAME idempotency_key and all original fields. "
    "Session status/resume cannot reconcile proposals. ProposalRecorded records a "
    "proposal or decision, not publication; only FactsPromoted proves publication."
)
_HELP.update(
    {
        "disagree": "left_fact_id, right_fact_id: required distinct canonical UUIDv4 facts. reason: required string, 1..4096 UTF-8 bytes. idempotency_key: required explicit canonical RFC-variant UUID, any version including v5. Scope and classification are fixed by the profile; no session required. Record a disagreement, not a correction, invalidation, trust change or resolution. On dispatched mutation uncertainty (exit 3), resubmit the IDENTICAL disagree operation with the SAME idempotency_key and all original fields. Never use session status/resume. Preflight/read failures exit 4; confirmed refusal or invalid input exits 2. No automatic retry or suggestion-triggered mutation.",
        "propose": "proposal_id, source_fact_id, idempotency_key: required canonical UUIDs. reason: 1..4096 UTF-8 bytes. target_scope: {realm, segments: [{kind, identifier}]}, same scope or ancestor in the same realm. Canonical ASCII realm/kind <=63, identifier <=255, at most 16 segments. "
        + _PROPOSAL_RECOVERY,
        "proposal-list": "Optional limit: integer 1..100 (default 50); after: canonical UUID or null (default null). Read only; no idempotency_key or session required. Hidden decision publication/evidence references remain null. Large pages exceeding the 1048576-byte CLI output bound fail without truncation; request a smaller page.",
        "proposal-read": "proposal_id: required canonical UUID. Read only; no idempotency_key or session required. Hidden decision publication/evidence references remain null.",
        "proposal-accept": "proposal_id, evidence_id, idempotency_key: required canonical UUIDs. target_classification: public, internal or restricted. Explicitly publish the proposal; no automatic acceptance. "
        + _PROPOSAL_RECOVERY,
        "proposal-reject": "proposal_id, idempotency_key: required canonical UUIDs. reason: 1..4096 UTF-8 bytes. Record rejection without publication. "
        + _PROPOSAL_RECOVERY,
    }
)


def write(stream: BinaryIO, data: bytes) -> None:
    position = 0
    while position < len(data):
        size = stream.write(data[position:])
        if size is None or size <= 0:
            raise OSError("output_unavailable")
        position += size
    stream.flush()


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default includes rejected argv (possibly sensitive data).
        raise CommandInputError("invalid_arguments")


def parser() -> Parser:
    result = Parser(
        prog="cairn-memory",
        allow_abbrev=False,
        description="Explicit scoped Cairn memory. Content belongs in stdin JSON.",
        epilog=_COMMON,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    result.add_argument(
        "--profile",
        required=True,
        metavar="PATH",
        help="explicit connection profile; never discovered",
    )
    result.add_argument("--human", action="store_true", help="pretty JSON output")
    commands = result.add_subparsers(dest="command", required=True)
    for name, description in _HELP.items():
        required, optional = FIELDS[name]
        commands.add_parser(
            name,
            allow_abbrev=False,
            description=description,
            epilog=f"Required keys: {', '.join(sorted(required)) or 'none'}. Optional keys: {', '.join(sorted(optional)) or 'none'}.\n"
            + _COMMON,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
    return result


def _help(argv: list[str], output: BinaryIO) -> bool:
    # argparse normally prints through ambient text stdout. Keep help on the
    # explicitly supplied output stream and before every input/config read.
    if "--help" not in argv and "-h" not in argv:
        return False
    root = parser()
    command = None
    profile_value = False
    for item in argv:
        if profile_value:
            profile_value = False
            continue
        if item == "--profile":
            profile_value = True
        elif item in FIELDS:
            command = item
            break
    if command is None:
        help_text = root.format_help()
    else:
        help_text = Parser(
            prog=f"cairn-memory --profile PATH {command}",
            description=_HELP[command],
            epilog=f"Required keys: {', '.join(sorted(FIELDS[command][0])) or 'none'}. Optional keys: {', '.join(sorted(FIELDS[command][1])) or 'none'}.\n"
            + _COMMON,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        ).format_help()
    write(output, help_text.encode())
    return True


def failure_exit(
    error: MemoryOperationFailure, stage: str, *, proposal_dispatched: bool = False
) -> int:
    if stage == "prepared":
        return 3
    if isinstance(error, DurableSessionFailure):
        if error.last_confirmed_stage == "prepared":
            return 3
        if error.operation == "turn-commit":
            return 3
    code = error.failure.code
    uncertain = code in {
        "transport_error",
        "transport_unavailable",
        "invalid_response",
        "http_error",
        "internal_error",
        "dependency_unavailable",
        "commit_outcome_unknown",
        "session_interrupted",
    }
    if error.operation in {
        "diagnose",
        "session-read",
        "recall",
        "history",
        "suggest",
        "arrive",
    }:
        return 4 if uncertain else 2
    if error.operation == "turn-commit":
        return 3
    if uncertain:
        return (
            3
            if proposal_dispatched or error.operation in {"turn-prepare", "correct"}
            else 4
        )
    return 2


async def execute(
    argv: list[str],
    *,
    stdin: BinaryIO,
    stdout: BinaryIO,
    stderr: BinaryIO,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """One invocation. Transport injection serves isolated ASGI acceptance."""
    command = "check"
    stage = "unconfirmed"
    operation = "input"
    human = False
    error_code = "operational_failure"
    exit_code = 4
    recovery: str | None = None
    proposal_dispatched = False

    async def mark_proposal_dispatch(request: httpx.Request) -> None:
        nonlocal proposal_dispatched
        # Preflight and acceptance's pre-read cannot have dispatched this mutation.
        # Record only dispatch, never content or credentials; do not retry here.
        if (
            command in PROPOSAL_MUTATIONS | {"disagree"}
            and request.url.path == f"/memory/v1/{command}"
        ):
            proposal_dispatched = True

    try:
        if _help(argv, stdout):
            return 0
        args = parser().parse_args(argv)
        command, human = args.command, args.human
        value = parse(command, stdin)
        profile = load_profile(Path(args.profile))
        if value.target_scope is not None and (
            value.target_scope.realm != profile.scope.realm
            or value.target_scope.segments
            != profile.scope.segments[: len(value.target_scope.segments)]
        ):
            raise CommandInputError("invalid_input")
        if command in SESSION_COMMANDS and profile.session_id is None:
            raise CommandInputError("session_id_required")
        if value.turn is not None:
            preparation_size(
                value, profile, UUID("00000000-0000-4000-8000-000000000000")
            )
        operation = "diagnose"
        token = load_credential(profile)
        async with httpx.AsyncClient(
            base_url=profile.endpoint,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [mark_proposal_dispatch]},
        ) as http:
            client = MemoryClient(
                http,
                scope=profile.scope,
                classification=profile.classification,
                expected_instance_id=profile.expected_instance_id,
            )
            diagnostic = await client.diagnose(
                expected_instance_id=profile.expected_instance_id
            )
            if diagnostic.status is not ConnectionStatus.READY:
                if diagnostic.failure is not None:
                    raise MemoryOperationFailure("diagnose", diagnostic.failure)
                raise CommandInputError("connection_refused")
            if value.turn is not None:
                assert diagnostic.principal_id is not None
                preparation_size(value, profile, diagnostic.principal_id)
            operation = command
            session = (
                DurableMemorySession(client, session_id=profile.session_id)
                if command in SESSION_COMMANDS and profile.session_id is not None
                else None
            )
            result: object
            if command == "check":
                result = diagnostic
            elif command == "recall":
                result = await client.recall(
                    value.query, budget=value.budget, relevant_only=value.relevant_only
                )
            elif command == "history":
                assert value.fact_id is not None
                result = await client.history(value.fact_id, budget=value.budget)
            elif command == "disagree":
                assert (
                    value.left_fact_id is not None
                    and value.right_fact_id is not None
                    and value.idempotency_key is not None
                )
                result = await client.disagree(
                    value.left_fact_id,
                    value.right_fact_id,
                    reason=value.reason,
                    idempotency_key=value.idempotency_key,
                )
                stage = "committed"
            elif command == "correct":
                assert value.idempotency_key is not None
                result = await client.correct(
                    value.fact_ids,
                    reason=value.reason,
                    superseded_by=value.superseded_by,
                    idempotency_key=value.idempotency_key,
                )
                stage = "committed"
            elif command == "suggest":
                result = await client.suggest(
                    observation=value.observation,
                    fact_ids=value.fact_ids,
                    budget=value.budget,
                    limit=value.limit,
                )
            elif command == "proposal-list":
                result = await client.proposal_list(
                    limit=value.limit, after=value.after
                )
            elif command == "proposal-read":
                assert value.proposal_id is not None
                result = await client.proposal_read(value.proposal_id)
            elif command in PROPOSAL_MUTATIONS:
                assert (
                    value.proposal_id is not None and value.idempotency_key is not None
                )
                if command == "propose":
                    assert (
                        value.source_fact_id is not None
                        and value.target_scope is not None
                    )
                    result = await client.propose(
                        value.proposal_id,
                        source_fact_id=value.source_fact_id,
                        target_scope=value.target_scope,
                        reason=value.reason,
                        idempotency_key=value.idempotency_key,
                    )
                elif command == "proposal-accept":
                    assert (
                        value.evidence_id is not None
                        and value.target_classification is not None
                    )
                    result = await client.proposal_accept(
                        value.proposal_id,
                        evidence_id=value.evidence_id,
                        target_classification=value.target_classification,
                        idempotency_key=value.idempotency_key,
                    )
                else:
                    result = await client.proposal_reject(
                        value.proposal_id,
                        reason=value.reason,
                        idempotency_key=value.idempotency_key,
                    )
                stage = "committed"
            else:
                assert session is not None and profile.session_id is not None
                if command in {"remember", "arrive"}:
                    operation = "session-open"
                    opened = await session.open()
                    stage = opened.snapshot.state
                if command == "arrive":
                    operation = "arrive"
                    result = await session.arrive(
                        value.query,
                        budget=value.budget,
                        history_fact_ids=value.history_fact_ids,
                    )
                elif command == "status":
                    operation = "session-read"
                    result = await session.status(value.turn_id)
                    stage = result.state
                elif command == "acknowledge-visit":
                    assert value.visit_id is not None
                    operation = "visit-acknowledge"
                    result = await session.acknowledge_visit(value.visit_id)
                    stage = result.snapshot.state
                else:
                    assert value.turn_id is not None
                    if command == "abandon":
                        operation = "turn-abandon"
                        result = await session.abandon(
                            value.turn_id, reason=value.reason
                        )
                        stage = result.snapshot.state
                    else:
                        if command == "remember":
                            assert (
                                value.attempt_id is not None and value.turn is not None
                            )
                            operation = "turn-begin"
                            begun = await client.begin_turn(
                                profile.session_id,
                                value.turn_id,
                                attempt_id=value.attempt_id,
                                replaces_turn_id=value.replaces_turn_id,
                                idempotency_key=command_key(
                                    "begin",
                                    profile.session_id,
                                    value.turn_id,
                                    value.attempt_id,
                                ),
                            )
                            stage = begun.snapshot.state
                            operation = "turn-prepare"
                            prepared = await client.prepare_turn(
                                profile.session_id,
                                value.turn_id,
                                value.turn,
                                attempt_id=value.attempt_id,
                                idempotency_key=command_key(
                                    "prepare",
                                    profile.session_id,
                                    value.turn_id,
                                    value.attempt_id,
                                ),
                            )
                            stage = prepared.snapshot.state
                        operation = "resume"
                        result = await session.resume(value.turn_id)
                        if command == "remember":
                            require_preparation_binding(
                                prepared.snapshot, result.snapshot
                            )
                        stage = result.snapshot.state
            exit_code = 0
            if isinstance(result, DurableTurnResult):
                exit_code = {
                    "committed": 0,
                    "skipped": 0,
                    "interrupted": 3,
                    "prepared": 3,
                    "abandoned": 2,
                }[result.state]
            operation = "output"
            write(stdout, render_result(command, result, human=human))
            return exit_code
    except (CommandInputError, ProfileError) as error:
        error_code = str(error)
        exit_code = 2
    except MemoryOperationFailure as error:
        operation = error.operation
        if (
            isinstance(error, DurableSessionFailure)
            and error.last_confirmed_stage != "unconfirmed"
        ):
            stage = error.last_confirmed_stage
        error_code = error.failure.code
        exit_code = failure_exit(error, stage, proposal_dispatched=proposal_dispatched)
        if (
            command == "remember"
            and stage in {"open", "started"}
            and operation in {"turn-begin", "turn-prepare"}
            and exit_code in {3, 4}
        ):
            recovery = "resubmit_identical_checkpoint_same_identities"
        elif command == "correct" and exit_code == 3:
            recovery = "resubmit_identical_correction_same_idempotency_key_and_fields"
        elif command in PROPOSAL_MUTATIONS | {"disagree"} and exit_code == 3:
            recovery = f"resubmit_identical_{command}_same_idempotency_key_and_fields"
        elif exit_code == 3:
            recovery = "status_then_resume_without_regeneration"
    except (CommandOutputError, OSError, ValueError, TypeError, RuntimeError):
        error_code = (
            "output_failure" if operation == "output" else "operational_failure"
        )
        exit_code = 4
    packet = {
        "error": {"code": error_code, "operation": operation},
        "last_confirmed_stage": stage,
    }
    if recovery is not None:
        packet["recovery"] = recovery
    try:
        write(stderr, render_result(command, packet, human=human))
    except (CommandOutputError, OSError, ValueError):
        pass
    return exit_code


def run() -> None:
    code = asyncio.run(
        execute(
            sys.argv[1:],
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
        )
    )
    if code == 4:
        # A failed buffered stdout must not fail again at interpreter shutdown.
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            descriptor = os.open(os.devnull, os.O_WRONLY)
            os.dup2(descriptor, sys.stdout.fileno())
            os.close(descriptor)
    raise SystemExit(code)


if __name__ == "__main__":
    run()
