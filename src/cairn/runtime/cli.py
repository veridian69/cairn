import argparse
import json
import os
import secrets
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID, uuid4

import uvicorn

from cairn.bootstrap.procedures import (
    BootstrapError,
    BootstrapResult,
    RecoveryResult,
    bootstrap_realm,
    recover_realm,
)
from cairn.catalogue.migration import (
    Advanced,
    Created,
    Current,
    MigrationError,
    migrate_catalogue,
)
from cairn.catalogue.sqlite import CatalogueStorageError
from cairn.catalogue.transactions import (
    CatalogueContention,
    CatalogueTransactionError,
    CatalogueTransactions,
)
from cairn.catalogue.verification import (
    VerificationError,
    VerificationReport,
    verify_catalogue,
)
from cairn.operations.backup import BackupError, BackupResult, create_backup
from cairn.operations.restore import RestoreError, RestoreResult, restore_bundle
from cairn.projection.rebuild import rebuild_index
from cairn.runtime.composition import (
    AdapterCredentialError,
    _default_index,
    build_application,
)
from cairn.runtime.config import (
    CairnConfig,
    ConfigError,
    load_config,
    resolve_config_path,
)
from cairn.runtime.lease import DataDirectoryLease, LeaseError
from cairn.runtime.logging import configure_logging

UVICORN_LOGGING_CONFIG: dict[str, object] = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "discard": {
            "class": "logging.NullHandler",
        },
    },
    "loggers": {
        "uvicorn": {
            "handlers": ["discard"],
            "level": "CRITICAL",
            "propagate": False,
        },
        "uvicorn.error": {
            "handlers": ["discard"],
            "level": "CRITICAL",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["discard"],
            "level": "CRITICAL",
            "propagate": False,
        },
    },
}


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(
            json.dumps(
                {"status": "error", "code": "invalid_arguments"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="cairn", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check-config", "migrate", "verify", "serve"):
        command_parser = commands.add_parser(command, allow_abbrev=False)
        command_parser.add_argument("--config", type=Path)

    rebuild_parser = commands.add_parser("rebuild-index", allow_abbrev=False)
    rebuild_parser.add_argument("--config", type=Path)

    backup_parser = commands.add_parser("backup", allow_abbrev=False)
    backup_parser.add_argument("--config", type=Path)
    backup_parser.add_argument("--output", type=Path, required=True)

    restore_parser = commands.add_parser("restore", allow_abbrev=False)
    restore_parser.add_argument("--config", type=Path)
    restore_parser.add_argument("--bundle", type=Path, required=True)

    bootstrap_parser = commands.add_parser("bootstrap", allow_abbrev=False)
    bootstrap_parser.add_argument("--config", type=Path)
    bootstrap_parser.add_argument("--realm", required=True)
    bootstrap_parser.add_argument("--label", required=True)

    recover_parser = commands.add_parser("recover", allow_abbrev=False)
    recover_parser.add_argument("--config", type=Path)
    recover_parser.add_argument("--realm", required=True)
    selector = recover_parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--principal", type=UUID)
    selector.add_argument("--label")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    config_path = resolve_config_path(
        cast(Path | None, arguments.config),
        os.environ,
    )
    try:
        config = load_config(config_path)
    except ConfigError as error:
        payload = {"status": "error", "code": error.code}
        if error.field is not None:
            payload["field"] = error.field
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2

    command = cast(str, arguments.command)
    if command in {"migrate", "verify"}:
        return _run_catalogue_command(command, config)
    if command == "rebuild-index":
        return _run_rebuild_index(config)
    if command == "backup":
        return _run_backup(config, cast(Path, arguments.output))
    if command == "restore":
        return _run_restore(config, cast(Path, arguments.bundle))
    if command in {"bootstrap", "recover"}:
        return _run_bootstrap_command(command, config, arguments)
    if command == "serve":
        try:
            application = build_application(config)
        except AdapterCredentialError as error:
            return _emit_credential_error(error)
        uvicorn.run(
            application,
            host=config.http.host,
            port=config.http.port,
            workers=1,
            access_log=False,
            lifespan="on",
            log_config=UVICORN_LOGGING_CONFIG,
            proxy_headers=False,
        )
        return 0

    print(
        json.dumps(
            {
                "status": "ok",
                "instance_id": str(config.instance_id),
                "mode": config.mode,
                "schema_version": config.schema_version,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_catalogue_command(command: str, config: CairnConfig) -> int:
    try:
        if command == "migrate":
            result = migrate_catalogue(config, lambda: datetime.now(UTC))
            payload = _migration_payload(result, config.instance_id)
        else:
            report = verify_catalogue(config)
            payload = _verification_payload(report)
    except VerificationError as error:
        exit_code = 3 if error.code == "catalogue_unavailable" else 4
        return _emit_error(error.code, exit_code)
    except MigrationError as error:
        return _emit_error(error.code, 4)
    except (CatalogueStorageError, LeaseError):
        return _emit_error("catalogue_unavailable", 3)
    except Exception:
        return _emit_error("internal_error", 3)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _run_rebuild_index(config: CairnConfig) -> int:
    """P-45: clear the retrieval index and re-project every fact from the
    catalogue, under the instance lease so it cannot run beneath a serving
    instance.

    Exits non-zero on any projection failure. A partial rebuild that
    reported success would be the worst outcome available: the index would
    be quietly missing facts with nothing to say so, and retrieval's
    silent-discard discipline means a caller could never tell.
    """
    if not config.graphiti.enabled:
        return _emit_error("retrieval_index_disabled", 4)

    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    try:
        lease.acquire()
    except LeaseError as error:
        return _emit_error(error.code, 3 if error.code == "data_unavailable" else 4)
    # Graphiti may query FalkorDB while constructing the adapter. Install the
    # dependency-log quarantine before that boundary so a failed setup query
    # cannot emit its full parameters (I-32).
    logger = configure_logging(sys.stderr)
    owned_index = None
    # R5/I-25: one in-process writer gate for the whole rebuild — the
    # extraction-cache store inside ``_default_index`` and
    # ``CatalogueTransactions`` must serialise on the same lock, not two.
    writer_gate = threading.Lock()
    try:
        index, owned_index = _default_index(
            config, writer_gate=writer_gate, safe_logger=logger
        )
        if index is None:
            return _emit_error("retrieval_index_disabled", 4)
        report = rebuild_index(
            config.paths.data,
            CatalogueTransactions(
                config.paths.data,
                writer_gate=writer_gate,
                clock=lambda: datetime.now(UTC),
                uuid_factory=uuid4,
            ),
            index,
            uuid_factory=uuid4,
            chunk_size=config.delivery.chunk_size,
            # Safe events to stderr, the JSON report to stdout: a chunked
            # rebuild that demotes must say so (P-82 gate-4 ruling,
            # 25 August 2026), and only ``projection_bulk_demoted`` and
            # its fellow safe events travel this way.
            logger=logger,
        )
    except AdapterCredentialError as error:
        # Caught ahead of the blanket handler below, which would otherwise
        # turn a nameable credential refusal into "internal_error". This
        # command builds the real adapter too, so I-92 governs it as much
        # as it governs ``serve``.
        return _emit_credential_error(error)
    except (CatalogueStorageError, CatalogueTransactionError):
        return _emit_error("catalogue_unavailable", 3)
    except Exception:
        return _emit_error("internal_error", 3)
    finally:
        # This command builds the real adapter too, so it owns the same
        # driver and event-loop thread the server does and closes it on
        # every exit path it can reach. The process is about to end either
        # way, but a command that leaves a daemon thread and an open
        # connection for interpreter shutdown to deal with is relying on
        # luck rather than releasing what it took.
        #
        # The one path this cannot cover: if ``_default_index`` raises
        # after constructing the adapter internally — the compatibility
        # guards in ``_construct_graphiti`` do exactly that on a
        # graphiti-core version drift — the assignment above never
        # completed, so there is no handle here to close and the driver
        # and its daemon loop thread are left to interpreter shutdown.
        # Bounded, because that raise ends the command (and the process)
        # rather than being retried; closing it properly would mean
        # giving the constructor its own cleanup, not widening this.
        if owned_index is not None:
            owned_index.close()
        lease.release()

    complete = report.failed == 0 and report.unreadable == 0
    # "ok" beside exit 4 told the operator the opposite of what the exit
    # code did, on the one path where the index is left incomplete and
    # believing the wrong one of the two matters most. The counts stay on
    # stdout either way — a partial rebuild is a reportable outcome, not an
    # error with nothing to say.
    print(
        json.dumps(
            {
                "status": "ok" if complete else "partial",
                "instance_id": str(config.instance_id),
                "projected": report.projected,
                "failed": report.failed,
                "unreadable": report.unreadable,
                "superseded_rows": report.superseded_rows,
            },
            sort_keys=True,
        )
    )
    return 0 if complete else 4


def _run_backup(config: CairnConfig, output: Path) -> int:
    """P-65: the coordinated backup bundle, as a local command that never
    becomes an HTTP route (I-35). Runs beside a live ``serve`` without the
    data-directory lease; the ``BEGIN IMMEDIATE`` barrier inside
    ``create_backup`` is what fixes the audit boundary."""
    try:
        result = create_backup(config, output, clock=lambda: datetime.now(UTC))
    except BackupError as error:
        return _emit_error(error.code, 4)
    except CatalogueContention:
        # Another writer outlasted the busy timeout while the barrier was
        # being taken — the I-49 retryable failure, surfaced by its code.
        return _emit_error("dependency_unavailable", 3)
    except CatalogueStorageError:
        return _emit_error("catalogue_unavailable", 3)
    except Exception:
        return _emit_error("internal_error", 3)
    print(json.dumps(_backup_payload(result), sort_keys=True))
    return 0


def _backup_payload(result: BackupResult) -> dict[str, object]:
    return {
        "status": "ok",
        "operation": "backup",
        "instance_id": str(result.instance_id),
        "bundle": str(result.bundle_path),
        # The measured barrier window P-65 requires the acceptance run to
        # record; the command is where the measurement exists.
        "barrier_ms": round(result.barrier_ms, 3),
        "members": [
            {
                "name": member.name,
                "bytes": member.byte_count,
                "sha256": member.sha256,
            }
            for member in result.members
        ],
    }


# Every restore refusal raised after the lease is taken leaves the target
# dirty and so carries P-66's recovery instruction. Four are post-install
# and leave the members in place for diagnosis — including a boundary
# read the installed catalogue refuses, and a lease release that fails
# after a restore that otherwise succeeded; ``install_failed`` can leave
# a partly written member, and even ``digest_mismatch`` — which installs
# nothing — leaves the lease file, which is enough to make the next
# attempt refuse the directory as non-empty. The target is a disposable
# empty volume, so the answer is the same in every case: delete it and
# retry (P-66's failure posture). Refusals before the lease leave the
# directory untouched and carry no recovery.
_DIRTY_TARGET_RESTORE_CODES = frozenset(
    {
        "digest_mismatch",
        "install_failed",
        "attic_integrity_failed",
        "audit_boundary_mismatch",
        "audit_boundary_unreadable",
        "lease_release_failed",
    }
)


def _run_restore(config: CairnConfig, bundle: Path) -> int:
    """P-66: the inverse local command (I-35), targeting a replacement
    deployment. Refusals before install leave the data directory holding
    at most the lease file; failures after install leave the members for
    diagnosis."""
    try:
        result = restore_bundle(config, bundle)
    except RestoreError as error:
        payload: dict[str, object] = {"status": "error", "code": error.code}
        if error.member is not None:
            payload["member"] = error.member
        if error.recovery_required or error.code in _DIRTY_TARGET_RESTORE_CODES:
            payload["recovery"] = "delete_data_directory_and_retry"
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 4
    except VerificationError as error:
        # Reachable only after the members are installed; the files stay.
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": error.code,
                    "recovery": "delete_data_directory_and_retry",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3 if error.code == "catalogue_unavailable" else 4
    except LeaseError as error:
        return _emit_error(error.code, 3 if error.code == "data_unavailable" else 4)
    except CatalogueStorageError:
        return _emit_error("catalogue_unavailable", 3)
    except Exception:
        return _emit_error("internal_error", 3)
    print(json.dumps(_restore_payload(result), sort_keys=True))
    return 0


def _restore_payload(result: RestoreResult) -> dict[str, object]:
    return {
        "status": "ok",
        "operation": "restore",
        "instance_id": str(result.instance_id),
        "bundle": str(result.bundle_path),
        "members": [
            {
                "name": member.name,
                "bytes": member.byte_count,
                "sha256": member.sha256,
            }
            for member in result.members
        ],
        # The verified boundary (P-66 step 7): the installed catalogue's
        # heads, proven equal to the manifest's declaration.
        "audit_boundary": [
            {
                "chain_kind": head.chain_kind,
                "chain_identity": head.chain_identity,
                "sequence": head.sequence,
                "head_digest": head.head_digest,
            }
            for head in result.audit_boundary
        ],
    }


def _run_bootstrap_command(
    command: str,
    config: CairnConfig,
    arguments: argparse.Namespace,
) -> int:
    try:
        if command == "bootstrap":
            bootstrap_result = bootstrap_realm(
                config,
                realm_id=cast(str, arguments.realm),
                label=cast(str, arguments.label),
                clock=lambda: datetime.now(UTC),
                uuid_factory=uuid4,
                entropy=secrets.token_bytes,
            )
            payload = _bootstrap_payload(bootstrap_result, config.instance_id)
        else:
            recovery_result = recover_realm(
                config,
                realm_id=cast(str, arguments.realm),
                principal_id=cast(UUID | None, arguments.principal),
                label=cast(str | None, arguments.label),
                clock=lambda: datetime.now(UTC),
                uuid_factory=uuid4,
                entropy=secrets.token_bytes,
            )
            payload = _recovery_payload(recovery_result, config.instance_id)
    except BootstrapError as error:
        return _emit_error(error.code, 4)
    except CatalogueTransactionError as error:
        return _emit_error(error.code, 3)
    except (CatalogueStorageError, LeaseError):
        return _emit_error("catalogue_unavailable", 3)
    except Exception:
        return _emit_error("internal_error", 3)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _bootstrap_payload(
    result: BootstrapResult,
    instance_id: UUID,
) -> dict[str, object]:
    return {
        "status": "ok",
        "operation": "bootstrap",
        "instance_id": str(instance_id),
        "realm_id": result.realm_id,
        "principal_id": str(result.principal_id),
        "credential_id": str(result.credential_id),
        "grant_ids": [str(grant_id) for grant_id in result.grant_ids],
        "token": result.token,
    }


def _recovery_payload(
    result: RecoveryResult,
    instance_id: UUID,
) -> dict[str, object]:
    return {
        "status": "ok",
        "operation": "recover",
        "instance_id": str(instance_id),
        "realm_id": result.realm_id,
        "principal_id": str(result.principal_id),
        "grant_id": str(result.grant_id),
        "credential_id": (
            None if result.credential_id is None else str(result.credential_id)
        ),
        "token": result.token,
    }


def _migration_payload(
    result: Created | Advanced | Current,
    instance_id: UUID,
) -> dict[str, object]:
    if isinstance(result, Created):
        starting_version = 0
    elif isinstance(result, Advanced):
        starting_version = result.previous_version
    else:
        starting_version = result.version
    return {
        "status": "ok",
        "operation": "migrate",
        "instance_id": str(instance_id),
        "starting_version": starting_version,
        "ending_version": result.version,
        "applied_versions": list(range(starting_version + 1, result.version + 1)),
    }


def _verification_payload(report: VerificationReport) -> dict[str, object]:
    return {
        "status": "ok",
        "operation": "verify",
        "schema_version": report.schema_version,
        "instance_id": str(report.instance_id),
        "realm_count": report.realm_count,
        "event_count": report.event_count,
        "idempotency_count": report.idempotency_count,
        "principal_count": report.principal_count,
        "credential_count": report.credential_count,
        "grant_count": report.grant_count,
        "assertion_count": report.assertion_count,
        "fact_count": report.fact_count,
        "invalidation_count": report.invalidation_count,
        "evidence_count": report.evidence_count,
        "evidence_outbox_depth": report.evidence_outbox_depth,
        "projection_outbox_depth": report.projection_outbox_depth,
    }


def _emit_credential_error(error: AdapterCredentialError) -> int:
    """I-92's startup refusal, as the operator sees it: the code, the
    convention file name that is wrong, and nothing read out of the file.
    Exit 3 — the dependency-unavailable class the catalogue and data
    directory already use, because a broken Secret mount is an absent
    dependency and not a malformed request."""
    print(
        json.dumps(
            {"status": "error", "code": error.code, "credential": error.credential},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 3


def _emit_error(code: str, exit_code: int) -> int:
    print(
        json.dumps({"status": "error", "code": code}, sort_keys=True),
        file=sys.stderr,
    )
    return exit_code


def run() -> NoReturn:
    raise SystemExit(main())
