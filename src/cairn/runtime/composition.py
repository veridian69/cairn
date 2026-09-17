import hashlib
import os
import sys
import threading
from _thread import LockType
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import anyio
import graphiti_core.helpers as graphiti_helpers
from fastapi import FastAPI
from starlette.routing import Route

from cairn import __version__
from cairn.administration.commands import CairnAdministration
from cairn.authority.credentials import CredentialAuthenticator
from cairn.authority.diagnostics import CairnDiagnostics
from cairn.authority.memory import CairnMemory
from cairn.authority.mutations import CairnAuthority
from cairn.authority.proposals import CairnProposals
from cairn.authority.sessions import CairnSessions
from cairn.catalogue.extraction_cache import ExtractionCacheStore
from cairn.catalogue.sqlite import (
    CatalogueStorageError,
    parse_timestamp,
    read_connection,
)
from cairn.catalogue.transactions import CatalogueTransactions
from cairn.catalogue.verification import (
    VerificationError,
    VerificationReport,
    _verify_catalogue_locked,
)
from cairn.evidence.adapter import AtticAdapter
from cairn.evidence.attic import SqliteAttic
from cairn.operations.metrics import Metrics, OutboxQueue
from cairn.projection.adapter import IndexAdapter
from cairn.projection.graphiti import GraphitiIndex
from cairn.projection.memory import build_memory_index
from cairn.runtime.config import CairnConfig
from cairn.runtime.delivery import run_delivery_loop
from cairn.runtime.lease import DataDirectoryLease, LeaseError
from cairn.runtime.logging import (
    Dependency,
    LogEvent,
    RuntimeFailureCode,
    SafeLogger,
    configure_logging,
)
from cairn.runtime.status import RuntimeStatus
from cairn.screening import SecretScreen
from cairn.transports.mcp.framing import bearer_authentication
from cairn.transports.mcp.manifest import packaged_manifest_bytes
from cairn.transports.mcp.mount import (
    MOUNT_PATH,
    MOUNT_ROUTE_NAME,
    McpTransport,
    build_mcp_server,
    build_session_manager,
    running_session_manager,
)
from cairn.transports.memory.contracts import (
    MANIFEST_NAME,
    OPENAPI_NAME,
    packaged_bytes,
)
from cairn.transports.memory.dispatch import MemoryDispatch
from cairn.transports.memory.operations import TOOL_NAMES as MEMORY_TOOL_NAMES
from cairn.transports.memory.routes import refuse_mcp_slash
from cairn.transports.memory.routes import register_routes as register_memory_routes
from cairn.transports.memory.server import MOUNT_PATH as MEMORY_MOUNT_PATH
from cairn.transports.memory.server import build_server as build_memory_server
from cairn.transports.rest.app import register_foundation_routes
from cairn.transports.rest.middleware import FoundationMiddleware
from cairn.transports.rest.v1.errors import register_boundary_handlers
from cairn.transports.rest.v1.openapi import packaged_contract_bytes
from cairn.transports.rest.v1.routes import register_v1_routes


@dataclass(slots=True)
class _CatalogueState:
    report: VerificationReport
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True, slots=True)
class ContractState:
    """P-32's readiness requirement: the packaged contract artefact.

    An instance that cannot read its own contract has a broken build, not
    a runtime condition, so ``_contract_state`` refuses to construct the
    application at all rather than serving with a null digest (Operator,
    7 August 2026) — which leaves ``loaded`` always true here and the
    readiness arm reachable only by a test replacing this function. The
    arm stays because P-32 puts the requirement on readiness, and because
    a future contract source that can legitimately be absent would
    otherwise have to reintroduce it.
    """

    loaded: bool
    digest: str
    mcp_digest: str


def _contract_state() -> ContractState:
    """I-76: the digest is the SHA-256 of the packaged bytes, computed
    once at startup and reported unchanged by ``GET /v1/instance``.

    I-89 adds the MCP manifest's digest, computed here by the same two
    lines and under the same rule: an instance that cannot read either
    packaged artefact has a broken build, so both are read in the one
    place whose ``OSError`` refuses the start."""
    return ContractState(
        loaded=True,
        digest=hashlib.sha256(packaged_contract_bytes()).hexdigest(),
        mcp_digest=hashlib.sha256(packaged_manifest_bytes()).hexdigest(),
    )


# I-92/P-67: ``paths.credentials`` is the adapter-credential directory,
# one secret per convention-named file. Kubernetes mounts a per-instance
# Secret there read-only and Compose bind-mounts it read-only; these three
# names are the whole vocabulary, and nothing else in the directory is read.
FALKORDB_USERNAME_FILE = "falkordb-username"
FALKORDB_PASSWORD_FILE = "falkordb-password"
OPENAI_API_KEY_FILE = "openai-api-key"


class AdapterCredentialError(Exception):
    """A required adapter credential is absent or unreadable.

    ``credential`` is the convention file name and never its contents:
    I-32 applied to the one class of value that must not appear in a
    failure, a log line or a traceback. The refusal exists so that a
    production instance with a broken Secret mount stops, rather than
    opening the unauthenticated FalkorDB connection I-92 closes.
    """

    def __init__(self, code: str, credential: str) -> None:
        self.code = code
        self.credential = credential
        super().__init__(f"adapter credential error: {code} ({credential})")


@dataclass(frozen=True, slots=True)
class _AdapterCredentials:
    falkordb_username: str | None
    falkordb_password: str
    openai_api_key: str


def _read_credential(directory: Path, name: str) -> str | None:
    """One credential file's value, or ``None`` when it is absent.

    An empty file is absence, not an empty secret (P-67) — a Secret key
    projected with no value would otherwise become a blank password and
    an authenticated-looking connection that is nothing of the sort.
    Exactly one trailing newline is stripped, because every tool that
    writes these files adds one; anything else in the file is the value,
    byte for byte.
    """
    try:
        raw = (directory / name).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        # Chained for the operator's traceback: an ``OSError`` carries the
        # path and the errno, never the file's contents.
        raise AdapterCredentialError("credential_unreadable", name) from error
    value = raw.removesuffix(b"\n")
    if not value:
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        # Deliberately unchained: ``UnicodeDecodeError`` quotes the
        # offending bytes, which are part of the secret.
        raise AdapterCredentialError("credential_unreadable", name) from None
    if "\x00" in text:
        # A NUL cannot survive the environment bridge — ``os.environ``
        # assignment raises ``ValueError`` on one — and it is corruption in
        # a file that holds a single credential. Refused here, by the one
        # rule that governs all three files, so it arrives as P-67's typed
        # refusal rather than as an untyped crash past every handler that
        # knows what a credential failure is.
        raise AdapterCredentialError("credential_unreadable", name)
    return text


def _require_credential(directory: Path, name: str) -> str:
    value = _read_credential(directory, name)
    if value is None:
        raise AdapterCredentialError("credential_missing", name)
    return value


def _read_adapter_credentials(directory: Path) -> _AdapterCredentials:
    return _AdapterCredentials(
        falkordb_username=_read_credential(directory, FALKORDB_USERNAME_FILE),
        falkordb_password=_require_credential(directory, FALKORDB_PASSWORD_FILE),
        openai_api_key=_require_credential(directory, OPENAI_API_KEY_FILE),
    )


def _export_semaphore_limit(value: int) -> None:
    """P-82, Operator's ruling of 25 August 2026: ``graphiti.semaphore_limit``
    is the configured form of graphiti-core's ``SEMAPHORE_LIMIT`` dial,
    so configuration stays the single authoritative layer (I-19) and any
    ambient environment value is overridden, never layered under.

    Setting the environment variable alone would be dead configuration:
    the library bound it at import time, long before composition runs.
    Every ``semaphore_gather`` resolves the constant from ``helpers``'
    module globals at call time — nothing imports it by value — so
    rebinding the attribute is the half that takes effect; the
    environment write keeps the process description consistent for
    anything that consults it afresh."""
    os.environ["SEMAPHORE_LIMIT"] = str(value)
    graphiti_helpers.SEMAPHORE_LIMIT = value


def _default_index(
    config: CairnConfig,
    *,
    writer_gate: LockType,
    safe_logger: SafeLogger | None = None,
) -> tuple[IndexAdapter | None, GraphitiIndex | None]:
    """The retrieval index this configuration asks for, and whichever
    part of it composition must close.

    Disabled is absence, not a null object (P-14). In test mode the
    in-memory adapter stands in for Graphiti so a real instance can be
    exercised end to end without FalkorDB or model providers; in
    production ``graphiti.enabled`` means the real adapter and nothing
    else, and ``build_memory_index`` refuses to be the substitute.

    The second element is the adapter's *owner* half. Only the real
    Graphiti adapter holds a resource — a FalkorDB driver and a dedicated
    event-loop thread — and only the caller that constructed it may close
    it. Returning it concretely rather than as an ``IndexAdapter`` is what
    lets the lifespan close it without widening P-38's closed
    four-operation protocol with a lifecycle method every other adapter
    would implement as a no-op: ``close()`` is composition's business,
    exactly as its own docstring says, so composition is where the
    knowledge of what needs closing belongs.
    """
    if not config.graphiti.enabled:
        return None, None
    if config.mode == "test":
        return build_memory_index(config.mode), None
    credentials = _read_adapter_credentials(config.paths.credentials)
    # I-92's environment bridge, and the reason it is drawn here rather
    # than in the adapter: graphiti-core reads provider credentials from
    # the environment and nowhere else, so the value has to reach
    # ``os.environ`` before the first ``Graphiti`` is constructed. I-19
    # stays intact either way — the Secret is a file, the manifest carries
    # no secret environment variable, and only this process's own memory
    # holds the value.
    os.environ["OPENAI_API_KEY"] = credentials.openai_api_key
    _export_semaphore_limit(config.graphiti.semaphore_limit)
    store = ExtractionCacheStore(
        config.paths.data, writer_gate=writer_gate, logger=safe_logger
    )
    owned = GraphitiIndex(
        host=config.graphiti.host,
        port=config.graphiti.port,
        username=credentials.falkordb_username,
        password=credentials.falkordb_password,
        index_concurrency_limit=config.graphiti.index_concurrency_limit,
        edge_batch_size=config.graphiti.edge_batch_size,
        edge_batch_linger_ms=config.graphiti.edge_batch_linger_ms,
        edge_batch_max_facts=config.graphiti.edge_batch_max_facts,
        extraction_cache=store,
        safe_logger=safe_logger,
    )
    return owned, owned


def build_application(
    config: CairnConfig,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    index_adapter: IndexAdapter | None = None,
) -> FastAPI:
    """``index_adapter`` is the P-38 composition seam, the same device as
    ``clock``: conformance injects its deterministic and hostile adapters
    through it. Omitted, the configuration decides."""
    lease = DataDirectoryLease(config.paths.data, config.instance_id)
    status = RuntimeStatus()
    logger = configure_logging(sys.stderr)
    metrics = Metrics()
    try:
        contract = _contract_state()
        memory_digest = hashlib.sha256(packaged_bytes(OPENAPI_NAME)).hexdigest()
        memory_mcp_digest = hashlib.sha256(packaged_bytes(MANIFEST_NAME)).hexdigest()
    except OSError:
        # A package built without its own contract artefact. Refused here
        # rather than at first request, so the failure is a start failure
        # like the lease and catalogue ones beside it.
        logger.emit(
            LogEvent.RUNTIME_START_FAILED,
            transport=None,
            instance_id=config.instance_id,
            failure_code=RuntimeFailureCode.CONTRACT_UNAVAILABLE,
        )
        raise

    # One writer gate embodied twice: the threading lock inside the shared
    # CatalogueTransactions serialises every catalogue write wherever it
    # originates, and the anyio lock in register_v1_routes serialises the
    # request-level write paths so handlers queue on the event loop instead
    # of stacking worker threads against the inner lock (P-29). Outbox
    # delivery, when Task 12 wires its loop, shares this same transactions
    # instance and therefore the same inner gate.
    uuid_factory: Callable[[], UUID] = uuid4
    writer_gate = threading.Lock()
    transactions = CatalogueTransactions(
        config.paths.data,
        writer_gate=writer_gate,
        clock=clock,
        uuid_factory=uuid_factory,
    )
    screen = SecretScreen()
    # An injected adapter belongs to whoever injected it — conformance
    # builds its deterministic and hostile adapters and expects them to
    # outlive the application — so composition closes only what it built
    # itself. ``owned_index`` is that and nothing else.
    owned_index: GraphitiIndex | None = None
    if index_adapter is not None:
        index: IndexAdapter | None = index_adapter
    else:
        try:
            index, owned_index = _default_index(
                config, writer_gate=writer_gate, safe_logger=logger
            )
        except AdapterCredentialError:
            # A production instance whose Secret mount is broken. Refused
            # here for the same reason as the missing contract artefact
            # above: a start failure belongs at start, and the alternative
            # is an unauthenticated index connection. The log line carries
            # the code; the file name reaches the operator through the
            # refusal itself, and neither carries the value.
            logger.emit(
                LogEvent.RUNTIME_START_FAILED,
                transport=None,
                instance_id=config.instance_id,
                failure_code=RuntimeFailureCode.CREDENTIALS_UNAVAILABLE,
            )
            raise
    # One Attic instance shared by the delivery loop's writes and
    # retrieval's reads: SqliteAttic holds no connection between calls, so
    # sharing costs nothing and keeps a single object answering for the
    # store the way a single CatalogueTransactions answers for the
    # catalogue.
    attic: AtticAdapter | None = (
        SqliteAttic(config.paths.data) if config.attic.enabled else None
    )
    authority = CairnAuthority(
        config.paths.data,
        transactions,
        clock,
        uuid_factory,
        # Attic is the exact-evidence store, so its toggle is the payload
        # path's toggle; no other configuration field governs it.
        exact_evidence_enabled=config.attic.enabled,
        screen=screen,
        # P-39: with no index there is no deliverer, so projection work is
        # not queued at all rather than accumulating undrainably.
        retrieval_index_enabled=index is not None,
        index=index,
        attic=attic,
        metrics=metrics,
        logger=logger,
    )
    administration = CairnAdministration(
        config.paths.data,
        transactions,
        clock,
        uuid_factory,
        entropy=os.urandom,
        screen=screen,
    )
    authenticator = CredentialAuthenticator(config.paths.data, clock)

    # P-29's single writer gate, held by both transports. Hoisted out of
    # the ``register_v1_routes`` call it used to be constructed in, so the
    # MCP mutation handlers serialise against the REST ones rather than
    # against a second lock of their own.
    write_gate = anyio.Lock()
    memory = CairnMemory(
        config.paths.data,
        transactions,
        clock,
        uuid_factory,
        screen,
        index=index,
        semantic_evidence=(
            index.memory_evidence_source() if isinstance(index, GraphitiIndex) else None
        ),
    )
    memory_dispatch = MemoryDispatch(
        authority=authority,
        proposals=CairnProposals(
            config.paths.data, transactions, clock, screen, authority
        ),
        sessions=CairnSessions(
            config.paths.data, transactions, clock, uuid_factory, screen, authority
        ),
        memory=memory,
        diagnostics=CairnDiagnostics(config.paths.data, transactions, clock),
        product_version=__version__,
        contract_digest=memory_digest,
        mcp_contract_digest=memory_mcp_digest,
        transactions=transactions,
        screen=screen,
        data_path=config.paths.data,
        write_gate=write_gate,
    )
    memory_manager = build_session_manager(build_memory_server(memory_dispatch))

    # I-84's second transport over the same contract. Built per
    # application because the SDK permits ``run()`` once per manager
    # instance, so a shared one could not survive a second build.
    mcp_manager = build_session_manager(
        build_mcp_server(
            authority=authority,
            administration=administration,
            transactions=transactions,
            screen=screen,
            data_path=config.paths.data,
            write_gate=write_gate,
            clock=clock,
            instance_id=config.instance_id,
            product_version=__version__,
            contract_digest=contract.digest,
            mcp_contract_digest=contract.mcp_digest,
        )
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            lease.acquire()
        except LeaseError as error:
            failure_code = (
                RuntimeFailureCode.ALREADY_LOCKED
                if error.code == "already_locked"
                else RuntimeFailureCode.DATA_UNAVAILABLE
            )
            logger.emit(
                LogEvent.RUNTIME_START_FAILED,
                transport=None,
                instance_id=config.instance_id,
                failure_code=failure_code,
            )
            raise
        catalogue_state: _CatalogueState | None = None
        try:
            limiter = anyio.CapacityLimiter(1)
            try:
                report = await anyio.to_thread.run_sync(
                    _verify_catalogue_locked,
                    config,
                    limiter=limiter,
                )
            except VerificationError as error:
                logger.emit(
                    LogEvent.RUNTIME_START_FAILED,
                    transport=None,
                    instance_id=config.instance_id,
                    dependency=Dependency.CATALOGUE,
                    failure_code=_verification_failure_code(error),
                )
                raise
            catalogue_state = _CatalogueState(report=report)
            application.state.catalogue_state = catalogue_state
            try:
                outbox_state = await anyio.to_thread.run_sync(
                    _sample_outbox_state,
                    config,
                    clock,
                    limiter=limiter,
                )
            except CatalogueStorageError:
                logger.emit(
                    LogEvent.RUNTIME_START_FAILED,
                    transport=None,
                    instance_id=config.instance_id,
                    dependency=Dependency.CATALOGUE,
                    failure_code=RuntimeFailureCode.CATALOGUE_INVALID,
                )
                raise
            for queue, depth, oldest_age_seconds in outbox_state:
                metrics.set_outbox_state(
                    queue,
                    depth=depth,
                    oldest_age_seconds=oldest_age_seconds,
                )
            await status.mark_started()
            # P-32: readiness additionally requires the packaged contract
            # to have loaded; the seam is honest even while the value is a
            # placeholder.
            if contract.loaded:
                await status.mark_ready()
            logger.emit(
                LogEvent.CATALOGUE_VERIFIED,
                transport=None,
                instance_id=config.instance_id,
                dependency=Dependency.CATALOGUE,
            )
            logger.emit(
                LogEvent.RUNTIME_STARTED,
                transport=None,
                instance_id=config.instance_id,
            )
            # P-41: the loop starts only here — after the lease is held and
            # the catalogue has verified — so nothing drains an outbox an
            # instance has not proved it owns and can read. Cancelling the
            # scope on the way out interrupts the loop between passes; a
            # pass already on a worker thread runs to completion, so the
            # confirming transaction is never torn mid-flight (I-25).
            async with anyio.create_task_group() as delivery_scope:
                delivery_scope.start_soon(
                    partial(
                        run_delivery_loop,
                        transactions,
                        attic=attic,
                        index=index,
                        interval_seconds=float(config.delivery.interval_seconds),
                        chunk_size=config.delivery.chunk_size,
                        clock=clock,
                        metrics=metrics,
                        logger=logger,
                    )
                )
                try:
                    # The MCP session manager's own task group, held open
                    # for the serving lifetime. Inside the delivery scope
                    # so it is torn down first: the transport must stop
                    # accepting requests before the loop that drains what
                    # they wrote is cancelled.
                    async with (
                        running_session_manager(mcp_manager),
                        running_session_manager(memory_manager),
                    ):
                        yield
                finally:
                    # I-20: mark shutdown on the adapter *before* waiting
                    # for the delivery scope — an in-flight index call
                    # cancels within one poll slice, so the wait below is
                    # bounded by that slice plus the pass's confirming
                    # transactions, never by a call deadline.
                    if owned_index is not None:
                        owned_index.request_close()
                    delivery_scope.cancel_scope.cancel()
        finally:
            if catalogue_state is not None:
                await status.mark_stopping()
                catalogue_state.close()
            # After the delivery task group has exited, never before: the
            # loop projects through this adapter, and a pass already on a
            # worker thread runs to completion. Closing it underneath a
            # live pass would fail that pass rather than end it cleanly.
            # Off the event loop because ``close()`` is synchronous and
            # joins the adapter's own thread.
            if owned_index is not None:
                await anyio.to_thread.run_sync(owned_index.close)
            lease.release()
            if catalogue_state is not None:
                logger.emit(
                    LogEvent.RUNTIME_STOPPED,
                    transport=None,
                    instance_id=config.instance_id,
                )

    application = FastAPI(
        title="Cairn",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(
        FoundationMiddleware,
        logger=logger,
        metrics=metrics,
    )
    register_foundation_routes(application, status=status, metrics=metrics)
    # I-70's out-of-route boundary: an unknown path and a wrong method
    # answer in the I-26 envelope rather than Starlette's default body.
    register_boundary_handlers(application)
    register_v1_routes(
        application,
        authenticator=authenticator,
        authority=authority,
        administration=administration,
        transactions=transactions,
        screen=screen,
        data_path=config.paths.data,
        write_gate=write_gate,
        instance_id=config.instance_id,
        product_version=__version__,
        contract_digest=contract.digest,
        mcp_contract_digest=contract.mcp_digest,
        clock=clock,
    )
    # I-84: registered after the verb-named routes, inside `/v1`, as a
    # second transport over the same contract rather than a second
    # contract. A Route rather than a Mount so the path matches exactly —
    # see McpTransport for why the prefix form is wrong here.
    application.router.routes.append(
        Route(
            MOUNT_PATH,
            McpTransport(
                mcp_manager,
                # P-54: the same ``authenticate_request`` the verb-named
                # routes call, over the same catalogue, bound once here
                # rather than reached for per request.
                authenticate=bearer_authentication(
                    authenticator=authenticator,
                    transactions=transactions,
                    data_path=config.paths.data,
                ),
            ),
            name=MOUNT_ROUTE_NAME,
            methods=None,
        )
    )
    register_memory_routes(
        application, dispatch=memory_dispatch, authenticator=authenticator
    )
    application.router.routes.append(
        Route(
            MEMORY_MOUNT_PATH,
            McpTransport(
                memory_manager,
                tool_names=MEMORY_TOOL_NAMES,
                authenticate=bearer_authentication(
                    authenticator=authenticator,
                    transactions=transactions,
                    data_path=config.paths.data,
                ),
            ),
            name="memory_mcp",
            methods=None,
        )
    )
    application.router.routes.append(
        Route(
            MEMORY_MOUNT_PATH + "/",
            refuse_mcp_slash,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
        )
    )
    return application


def _verification_failure_code(error: VerificationError) -> RuntimeFailureCode:
    if error.code == "catalogue_unavailable":
        return RuntimeFailureCode.DATA_UNAVAILABLE
    if error.code == "instance_mismatch":
        return RuntimeFailureCode.INSTANCE_MISMATCH
    return RuntimeFailureCode.CATALOGUE_INVALID


def _sample_outbox_state(
    config: CairnConfig,
    clock: Callable[[], datetime],
) -> tuple[tuple[OutboxQueue, int, float], ...]:
    """Reads both outbox queues' depth and oldest-row age in one read
    connection (P-18), once at startup.

    Both tables' ``created_at`` CHECK (migration 0003,
    ``ck_evidence_outbox_created_at`` / ``ck_projection_outbox_created_at``)
    pins length and the dash/colon/dot/``T``/``Z`` positions and restricts
    every character to the class ``[0-9TZ:.-]`` — but that class also
    permits a separator character at a digit position, so e.g.
    ``9999-99-99T99:99:99.999999Z`` passes the CHECK while being no real
    calendar timestamp: shape-only, not meaning-constraining, the same gap
    documented for the outbox identity GLOB CHECKs in
    ``cairn.evidence.delivery``. Unlike delivery's ``ORDER BY``, which
    never parses ``created_at`` and so needs no guard, this function turns
    the oldest row's value into a ``datetime`` to compute an age, so it
    goes through ``parse_timestamp`` — the established reading guard — and
    a malformed value surfaces as ``CatalogueStorageError`` rather than an
    unguarded ``ValueError``. The caller in ``build_application`` treats
    that as a startup failure: full outbox content validation is Task 12's
    remit, not this one's.
    """
    with read_connection(config.paths.data) as connection:
        evidence_row = connection.execute(
            "SELECT COUNT(*), MIN(created_at) FROM evidence_outbox"
        ).fetchone()
        projection_row = connection.execute(
            "SELECT COUNT(*), MIN(created_at) FROM projection_outbox"
        ).fetchone()
    now = clock()
    return (
        (OutboxQueue.EVIDENCE, *_depth_and_age(evidence_row, now)),
        (OutboxQueue.PROJECTION, *_depth_and_age(projection_row, now)),
    )


def _depth_and_age(row: object, now: datetime) -> tuple[int, float]:
    depth, oldest_created_at = cast(tuple[int, str | None], row)
    if oldest_created_at is None:
        return depth, 0.0
    oldest = parse_timestamp(oldest_created_at)
    # Clamped for the same reason as delivery's _oldest_age_seconds: a
    # future-dated row is negative, set_outbox_state refuses that with a
    # TypeError, and here that TypeError would abort startup over a gauge
    # rather than over anything wrong with the catalogue.
    return depth, max(0.0, (now - oldest).total_seconds())
