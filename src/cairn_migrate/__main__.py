"""The migration tool's command line (P-71).

``export`` and ``map`` are the dry run, and between them they touch no
network at all: ``export`` reads Operator's offline snapshot set into a
``cairn-legacy-export/v1`` bundle, and ``map`` turns that bundle into a
``cairn-migration-plan/v1`` plan and a privacy-safe report. ``apply`` and
``verify`` are the only subcommands that ever speak to an instance, and
both require the operator to name it and supply a credential file.
Nothing here contacts the legacy VPS or ``cairn.example.invalid``.
"""

import argparse
import asyncio
import ipaddress
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Never
from urllib.parse import urlsplit
from uuid import RFC_4122, UUID

from httpx import AsyncClient, RequestError
from redis.exceptions import RedisError

from cairn_migrate.apply import (
    APPLY_ERROR_CODES,
    ApplyError,
    ApplySummary,
    apply_plan,
    verify_instance,
)
from cairn_migrate.export import (
    ExportError,
    SnapshotSet,
    snapshot_label_is_valid,
    write_export_bundle,
)
from cairn_migrate.mapping import (
    MAPPING_ERROR_CODES,
    SUBSET_PLAN_PROFILE,
    SUBSET_PLAN_RANKING_ALGORITHM,
    SUBSET_PLAN_REQUIRED_ADDITIONAL_TURN_COUNT,
    SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT,
    SUBSET_PLAN_REQUIRED_TURN_COUNT,
    MappingError,
    map_bundle,
    read_export_bundle,
    write_plan,
    write_subset_plan,
)
from cairn_migrate.report import write_report
from cairn_migrate.snapshot import GraphQuery, SnapshotError, falkordb_query
from cairn_migrate.verify import (
    VERIFY_ERROR_CODES,
    VerifyError,
    VerifySummary,
    verify_plan,
)

#: The repository this tool ships in. A bundle carries legacy record
#: bodies, so P-72 puts it in a Operator-controlled directory *outside* the
#: checkout and P-78 keeps legacy content out of the repository
#: entirely. A mistyped ``--output`` is the one plausible way that goes
#: wrong, so it is refused rather than trusted.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]

_EXPORT_EPILOGUE = """\
The graph is read from a disposable container restored from the
snapshot, never from the live VPS — the tool holds no productive
credential and never contacts the VPS or cairn.example.invalid.

Restore procedure (P-72), run before this command:

  mkdir -p /tmp/cairn-restore && tar xzf <falkor-dump>.tar.gz \\
      -C /tmp/cairn-restore
  docker run --rm --detach --name cairn-legacy-restore \\
      --publish 127.0.0.1:6399:6379 \\
      --volume /tmp/cairn-restore:/data \\
      <restore-image>@<restore-image-digest>

Take the image and its digest from the frozen legacy
docker-compose.yml and pass both here: they identify what read the
dump, and the export manifest records them. Point --falkor-url at the
published loopback port. Destroy the container and the restore
directory when the export is done.

The bundle contains legacy record bodies. Write it into a private
directory outside this checkout; the tool refuses an --output path
inside it.
"""


_MAP_EPILOGUE = """\
map is pure and offline: the same bundle in gives a byte-identical plan
out, and nothing here opens a socket. The plan is the single input to
apply.

The plan carries legacy record bodies in operations.jsonl and belongs in
the same private directory as the bundle; the tool refuses an --output
path inside this checkout. report.json beside it is the privacy-safe
artefact — counts, digests, rule tallies, store names and legacy
identifiers only — and is the file the dry-run evidence record quotes.

The report enumerates every observed actor and source value. Operator rules
the Operator-actor set, the reconciliation list, any invalid_at carry-over
and Stage B scope on those figures before any apply runs.
"""


_APPLY_EPILOGUE = """\
apply is the deliberate write boundary. It sends only operations.jsonl,
records identity-only, instance-bound receipts durably after successful
responses, and resumes from those receipts after interruption. --instance and
--expected-instance and --credential-file are mandatory; there is no default
target or credential. Remote instances require HTTPS. The plan and receipts
must remain in a private directory outside this checkout.
"""


_SUBSET_EPILOGUE = """subset is deterministic and pure: it reads a full plan,
materialises one fixed-profile slice into a second ``cairn-migration-plan/v1``
directory, and returns the new path.

The subset is deterministic by construction:
- candidate conversations are planned conversations with at least one planned
  turn,
- every candidate conversation and turn is ranked by
  ``sha256("attic-conversation:<id>")`` or ``sha256("attic-turn:<id>")``,
  in sorted lexicographic order,
- every selected conversation brings its highest-ranked turn,
- then top-ranked remaining turns across all selected conversations fill the
  fixed total turn quota.
"""


_VERIFY_EPILOGUE = """\
verify samples retrieval, replays the same idempotency keys, and reads the
root audit chain. The named credential therefore needs both the data grant
and the separate audit-read grant described by P-76. The target identity is
authenticated before verification, and the plan and receipts must remain in a
private directory outside this checkout. --skip-retrieval is reserved for
Stage A, where projection is deliberately disabled; replay and audit checks
still run unchanged.
"""


ClientFactory = Callable[[str], AsyncClient]

CLI_ERROR_CODES = (
    frozenset(
        {
            "arguments_invalid",
            "bundle_exists",
            "bundle_incomplete",
            "credential_unreadable",
            "expected_instance_invalid",
            "falkor_url_not_loopback",
            "graph_unavailable",
            "instance_url_invalid",
            "migration_path_inside_checkout",
            "output_inside_checkout",
            "output_unavailable",
            "record_not_canonical_json",
            "sample_size_invalid",
            "snapshot_label_invalid",
            "snapshot_unreadable",
            "timestamp_malformed",
            "transport_failed",
        }
    )
    | APPLY_ERROR_CODES
    | MAPPING_ERROR_CODES
    | VERIFY_ERROR_CODES
)


class _JsonArgumentParser(argparse.ArgumentParser):
    """Keep command-line refusals inside the documented JSON envelope."""

    def error(self, _message: str) -> Never:
        _refuse("arguments_invalid")
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="cairn_migrate", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export",
        allow_abbrev=False,
        description="Read an offline legacy snapshot set into a "
        "cairn-legacy-export/v1 bundle.",
        epilog=_EXPORT_EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    export.add_argument("--snapshot-label", required=True)
    export.add_argument("--graph-dump", required=True)
    export.add_argument("--restore-image", required=True)
    export.add_argument("--restore-image-digest", required=True)
    export.add_argument("--falkor-url", default="redis://127.0.0.1:6399")
    export.add_argument("--graph-name", default="cairn")
    export.add_argument("--attic", type=Path, required=True)
    export.add_argument("--journal", type=Path, required=True)
    export.add_argument("--thoughts", type=Path, required=True)
    export.add_argument("--openbrain", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    mapper = commands.add_parser(
        "map",
        allow_abbrev=False,
        description="Map a cairn-legacy-export/v1 bundle into a "
        "cairn-migration-plan/v1 plan and its privacy-safe report.",
        epilog=_MAP_EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mapper.add_argument("--bundle", type=Path, required=True)
    mapper.add_argument("--output", type=Path, required=True)
    subsetter = commands.add_parser(
        "subset",
        allow_abbrev=False,
        description="Generate a deterministic subset of an existing migration plan.",
        epilog=_SUBSET_EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subsetter.add_argument("--source-plan", type=Path, required=True)
    subsetter.add_argument("--output", type=Path, required=True)
    subsetter.add_argument(
        "--expected-source-manifest-sha256",
        required=True,
        help=("SHA-256 of the full source plan's ``manifest.json``"),
    )
    apply = commands.add_parser(
        "apply",
        allow_abbrev=False,
        description="Apply a digest-pinned migration plan through /v1.",
        epilog=_APPLY_EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_instance_arguments(apply)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--receipts", type=Path, required=True)
    verify = commands.add_parser(
        "verify",
        allow_abbrev=False,
        description="Verify migration receipts through /v1.",
        epilog=_VERIFY_EPILOGUE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_instance_arguments(verify)
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--receipts", type=Path, required=True)
    verify.add_argument("--sample-size", type=int, default=25)
    verify.add_argument("--skip-retrieval", action="store_true")
    return parser


def _add_instance_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--instance", required=True)
    command.add_argument("--expected-instance", required=True)
    command.add_argument("--credential-file", type=Path, required=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    graph_factory: Callable[[str, str], GraphQuery] = falkordb_query,
    client_factory: ClientFactory = lambda instance: AsyncClient(
        base_url=instance, timeout=30.0
    ),
) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "map":
        return _map(arguments)
    if arguments.command == "subset":
        return _subset(arguments)
    if arguments.command == "apply":
        return _apply(arguments, client_factory)
    if arguments.command == "verify":
        return _verify(arguments, client_factory)
    return _export(arguments, graph_factory)


def _apply(arguments: argparse.Namespace, client_factory: ClientFactory) -> int:
    if not _migration_paths_outside_checkout(arguments):
        return _refuse("migration_path_inside_checkout")
    admitted = _admit_instance(arguments)
    if admitted is None:
        return 2
    instance, expected_instance, credential = admitted
    try:
        summary = asyncio.run(
            _apply_over_http(
                arguments,
                instance,
                expected_instance,
                credential,
                client_factory,
            )
        )
    except ApplyError as error:
        return _refuse(error.code, detail=error.detail)
    except RequestError:
        return _refuse("transport_failed")
    receipts = arguments.receipts.resolve()
    print(
        json.dumps(
            {
                "status": "ok",
                "receipts_path": str(receipts),
                "planned": summary.planned,
                "applied": summary.applied,
                "resumed": summary.resumed,
            },
            sort_keys=True,
        )
    )
    return 0


async def _apply_over_http(
    arguments: argparse.Namespace,
    instance: str,
    expected_instance: str,
    credential: str,
    client_factory: ClientFactory,
) -> ApplySummary:
    async with client_factory(instance) as client:
        await verify_instance(
            client=client,
            credential=credential,
            expected_instance=expected_instance,
        )
        return await apply_plan(
            plan_path=arguments.plan.resolve(),
            receipts_path=arguments.receipts.resolve(),
            expected_instance=expected_instance,
            client=client,
            credential=credential,
        )


def _verify(arguments: argparse.Namespace, client_factory: ClientFactory) -> int:
    if not _migration_paths_outside_checkout(arguments):
        return _refuse("migration_path_inside_checkout")
    admitted = _admit_instance(arguments)
    if admitted is None:
        return 2
    instance, expected_instance, credential = admitted
    if arguments.sample_size < 1:
        return _refuse("sample_size_invalid")
    try:
        summary = asyncio.run(
            _verify_over_http(
                arguments,
                instance,
                expected_instance,
                credential,
                client_factory,
            )
        )
    except (ApplyError, VerifyError) as error:
        return _refuse(error.code, detail=error.detail)
    except RequestError:
        return _refuse("transport_failed")
    print(
        json.dumps(
            {
                "status": "ok",
                "receipts": summary.receipts,
                "retrieved": summary.retrieved,
                "replayed": summary.replayed,
                "audited": summary.audited,
            },
            sort_keys=True,
        )
    )
    return 0


async def _verify_over_http(
    arguments: argparse.Namespace,
    instance: str,
    expected_instance: str,
    credential: str,
    client_factory: ClientFactory,
) -> VerifySummary:
    async with client_factory(instance) as client:
        await verify_instance(
            client=client,
            credential=credential,
            expected_instance=expected_instance,
        )
        return await verify_plan(
            plan_path=arguments.plan.resolve(),
            receipts_path=arguments.receipts.resolve(),
            expected_instance=expected_instance,
            client=client,
            credential=credential,
            sample_size=arguments.sample_size,
            skip_retrieval=arguments.skip_retrieval,
        )


def _admit_instance(arguments: argparse.Namespace) -> tuple[str, str, str] | None:
    instance = _instance_url(arguments.instance)
    if instance is None:
        _refuse("instance_url_invalid")
        return None
    expected_instance = _expected_instance(arguments.expected_instance)
    if expected_instance is None:
        _refuse("expected_instance_invalid")
        return None
    credential = _read_credential(arguments.credential_file)
    if credential is None:
        _refuse("credential_unreadable")
        return None
    return instance, expected_instance, credential


def _instance_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    if parsed.scheme == "http":
        try:
            if not ipaddress.ip_address(parsed.hostname).is_loopback:
                return None
        except ValueError:
            return None
    return value.rstrip("/")


def _expected_instance(value: str) -> str | None:
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    if parsed.variant != RFC_4122 or parsed.version != 4 or str(parsed) != value:
        return None
    return value


def _migration_paths_outside_checkout(arguments: argparse.Namespace) -> bool:
    return all(
        path != _CHECKOUT_ROOT and _CHECKOUT_ROOT not in path.parents
        for path in (arguments.plan.resolve(), arguments.receipts.resolve())
    )


def _read_credential(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > 65_536:
        return None
    raw = raw.removesuffix(b"\n")
    try:
        credential = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if (
        not credential
        or credential.strip() != credential
        or "\x00" in credential
        or "\n" in credential
    ):
        return None
    return credential


def _map(arguments: argparse.Namespace) -> int:
    output = arguments.output.resolve()
    if output == _CHECKOUT_ROOT or _CHECKOUT_ROOT in output.parents:
        return _refuse("output_inside_checkout")
    try:
        bundle = read_export_bundle(arguments.bundle.resolve())
        result = map_bundle(bundle)
        plan_path = write_plan(result, bundle, output, checkout_root=_CHECKOUT_ROOT)
        report_path = write_report(result, bundle, plan_path)
    except MappingError as error:
        return _refuse(error.code, detail=error.detail)
    # I-32 again: identities, counts and digests only. The three totals are
    # the ones that decide whether the dry run is worth reading further.
    print(
        json.dumps(
            {
                "status": "ok",
                "plan": str(plan_path),
                "report": str(report_path),
                "planned": len(result.operations),
                "rejected": len(result.rejections),
                "reconciled": len(result.reconciliations),
            },
            sort_keys=True,
        )
    )
    return 0


def _subset(arguments: argparse.Namespace) -> int:
    try:
        output = write_subset_plan(
            source_plan=arguments.source_plan.resolve(),
            output_path=arguments.output.resolve(),
            checkout_root=_CHECKOUT_ROOT,
            expected_source_manifest_sha256=arguments.expected_source_manifest_sha256,
        )
        plan_path = output[0]
        source_counts = output[1]
        selected_counts = output[2]
    except MappingError as error:
        return _refuse(error.code, detail=error.detail)
    print(
        json.dumps(
            {
                "status": "ok",
                "plan": str(plan_path),
                "source_counts": {
                    "graph_episodes": source_counts[0],
                    "conversations": source_counts[1],
                    "turns": source_counts[2],
                },
                "selected_counts": {
                    "graph_episodes": selected_counts[0],
                    "conversations": selected_counts[1],
                    "turns": selected_counts[2],
                },
                "subset_profile": SUBSET_PLAN_PROFILE,
                "selection_required": {
                    "conversations": SUBSET_PLAN_REQUIRED_CONVERSATION_COUNT,
                    "turns": SUBSET_PLAN_REQUIRED_TURN_COUNT,
                    "additional_turns": SUBSET_PLAN_REQUIRED_ADDITIONAL_TURN_COUNT,
                },
                "ranking_algorithm": SUBSET_PLAN_RANKING_ALGORITHM,
            },
            sort_keys=True,
        )
    )
    return 0


def _export(
    arguments: argparse.Namespace,
    graph_factory: Callable[[str, str], GraphQuery],
) -> int:
    if not _is_uncredentialled_loopback_url(arguments.falkor_url):
        return _refuse("falkor_url_not_loopback")
    if not snapshot_label_is_valid(arguments.snapshot_label):
        return _refuse("snapshot_label_invalid")
    output = arguments.output.resolve()
    if output == _CHECKOUT_ROOT or _CHECKOUT_ROOT in output.parents:
        return _refuse("output_inside_checkout")
    snapshot = SnapshotSet(
        label=arguments.snapshot_label,
        graph_dump=str(Path(arguments.graph_dump).resolve()),
        restore_image=arguments.restore_image,
        restore_image_digest=arguments.restore_image_digest,
        attic_path=arguments.attic.resolve(),
        journal_root=arguments.journal.resolve(),
        thoughts_root=arguments.thoughts.resolve(),
        openbrain_path=arguments.openbrain.resolve(),
    )
    try:
        query = graph_factory(arguments.falkor_url, arguments.graph_name)
    except (RedisError, ValueError):
        return _refuse("graph_unavailable")
    try:
        result = write_export_bundle(snapshot, query, output)
    except SnapshotError as error:
        return _refuse(error.code, detail=error.detail)
    except ExportError as error:
        return _refuse(error.code)
    except RedisError:
        return _refuse("graph_unavailable")
    # I-32 extends to this tool: identities, counts and digests only —
    # never a body, a title or a scope path.
    print(
        json.dumps(
            {
                "status": "ok",
                "bundle": str(result.bundle_path),
                "stores": {store.store: store.record_count for store in result.stores},
                "entity_nodes": result.entity_nodes,
                "entity_edges": result.entity_edges.total,
            },
            sort_keys=True,
        )
    )
    return 0


def _is_uncredentialled_loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "redis"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse(code: str, *, detail: str | None = None) -> int:
    if code not in CLI_ERROR_CODES:
        raise ValueError(f"unknown CLI error code: {code}")
    payload = {"status": "error", "code": code}
    if detail is not None:
        payload["detail"] = detail
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
