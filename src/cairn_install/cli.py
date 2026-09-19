"""Command-line entry point for guided installation and recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import stat
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, Never

from .core import InstallError, atomic_write, open_context, open_read_context
from .output import paint

DEFAULT_PORT = 8000
MODES = ("disposable", "native", "docker", "kubernetes")
_IMAGE_DIGEST = re.compile(r"[^@\s]+@sha256:[0-9a-fA-F]{64}\Z")
_RECEIPT_IMAGE_DIGEST = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
_LOCAL_IMAGE_TAG = re.compile(r"[^@\s]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_KUBERNETES_NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
_KUBERNETES_NODE = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")
FINGERPRINT_FILES = (
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    ".dockerignore",
    "README.md",
    "LICENSE.md",
    "LICENSE-Apache-2.0.txt",
    "NOTICE.md",
    "deploy/images.lock",
    "deploy/kustomize/rendered/kubernetes.yaml",
    "deploy/kustomize/rendered/kubernetes-retrieval.yaml",
    "integrations/codex/cairn-memory/SKILL.md",
    "integrations/claude/cairn-memory/SKILL.md",
    "a2a/go.mod",
    "a2a/go.sum",
    "a2a/main.go",
    "a2a/Dockerfile",
)
FINGERPRINT_TREES = (
    "src/cairn",
    "src/cairn_install",
    "scripts",
    "a2a/cmd",
    "a2a/internal",
    "a2a/scripts",
)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise InstallError(message, "invalid_arguments")


@contextmanager
def _termination_handlers() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    installed: list[tuple[signal.Signals, Any]] = []

    def interrupt(_number: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous = signal.getsignal(number)
            signal.signal(number, interrupt)
            installed.append((number, previous))
        yield
    finally:
        for number, previous in reversed(installed):
            signal.signal(number, previous)


def _absolute_safe_path(value: Path, *, purpose: str) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise InstallError(f"{purpose} path contains a symlink: {current}")
    return candidate.absolute()


def validate_source(value: Path) -> Path:
    source = _absolute_safe_path(value, purpose="Source")
    if str(source) == "/mnt" or str(source).startswith("/mnt/"):
        raise InstallError("Source must be on a native Linux filesystem, not /mnt.")
    if not source.is_dir():
        raise InstallError(f"Source checkout is not a directory: {source}")
    for relative in (Path("pyproject.toml"), Path("src/cairn")):
        target = source / relative
        if not target.exists():
            raise InstallError(f"Source checkout is missing {relative}: {source}")
    return source


def _fingerprint_paths(source: Path) -> list[Path]:
    paths: list[Path] = []
    for relative in FINGERPRINT_FILES:
        candidate = source / relative
        if candidate.exists():
            paths.append(candidate)
    for relative in FINGERPRINT_TREES:
        root = source / relative
        if not root.exists():
            continue
        if root.is_symlink():
            raise InstallError(f"Install source contains a symlink: {root}")
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                raise InstallError(f"Install source contains a symlink: {candidate}")
            if candidate.is_file() and "__pycache__" not in candidate.parts:
                paths.append(candidate)
    return sorted(paths, key=lambda item: item.relative_to(source).as_posix())


def source_fingerprint(source: Path) -> str:
    """Hash stable, installation-relevant source content without invoking Git."""
    source = validate_source(source)
    digest = hashlib.sha256()
    for path in _fingerprint_paths(source):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise InstallError(f"Install source is not a regular file: {path}")
        relative = path.relative_to(source).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    return digest.hexdigest()


def _parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="cairn-install",
        description="Guided Cairn installer with resumable, preserving recovery.",
    )
    parser.add_argument(
        "operation",
        nargs="?",
        choices=("install", "status", "resume", "rollback", "blitz", "ls"),
        default="install",
    )
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--name")
    parser.add_argument("--port", type=int)
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--falkordb-runtime", type=Path)
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="keep a verified disposable instance in the foreground until Ctrl-C",
    )
    parser.add_argument("--provider-key-file", type=Path)
    parser.add_argument(
        "--garden-config",
        type=Path,
        help="Enable managed Garden using an explicit HTTPS/TLS configuration JSON file",
    )
    parser.add_argument("--kube-context")
    parser.add_argument("--kube-namespace")
    parser.add_argument("--kube-storage-class")
    parser.add_argument("--kube-image")
    parser.add_argument("--kube-preloaded-image", action="store_true", default=None)
    parser.add_argument("--kube-falkordb-receipt", type=Path)
    parser.add_argument(
        "--state-root", type=Path, default=Path("~/.local/state/cairn-install")
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the irreversible blitz operation without a prompt",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show detailed commands, command output and the full result",
    )
    return parser


def _required(
    value: str | None, flag: str, prompt: str, *, non_interactive: bool
) -> str:
    if value:
        return value
    if non_interactive:
        raise InstallError(
            f"{flag} is required in non-interactive mode", "invalid_arguments"
        )
    answer = input(prompt).strip()
    if not answer:
        raise InstallError(f"{flag} is required", "invalid_arguments")
    return answer


def _prompt_mode(non_interactive: bool) -> str:
    if non_interactive:
        raise InstallError(
            "--mode is required in non-interactive mode", "invalid_arguments"
        )
    print("Installation modes: disposable, native, docker, kubernetes")
    return input("Mode: ").strip()


def _prompt_port(non_interactive: bool) -> int:
    if non_interactive:
        return DEFAULT_PORT
    answer = input(f"Port [{DEFAULT_PORT}]: ").strip()
    if not answer:
        return DEFAULT_PORT
    try:
        return int(answer)
    except ValueError as error:
        raise InstallError("Port must be an integer", "invalid_arguments") from error


def _prompt_semantic(mode: str, non_interactive: bool) -> bool:
    if mode == "disposable":
        print("Features: Attic only (disposable mode keeps semantic search disabled).")
        return False
    if mode == "native" and platform.system() == "Darwin":
        print("Features: Attic only (macOS native does not enable semantic search).")
        return False
    if non_interactive:
        return False
    print("Features:")
    print("  1) Attic only")
    print("  2) Attic plus semantic search")
    answer = input("Choose 1 or 2 [1]: ").strip() or "1"
    if answer not in {"1", "2"}:
        raise InstallError("Feature choice must be 1 or 2", "invalid_arguments")
    return answer == "2"


def _confirm_blitz(name: str, *, non_interactive: bool, yes: bool) -> None:
    print(
        f"IRREVERSIBLE: blitz permanently deletes everything the installer owns "
        f"for {name!r}, including its data, credentials, configuration, evidence "
        "and private state. Rollback and resume will no longer be possible. "
        "This is not secure erasure."
    )
    if yes:
        return
    if non_interactive:
        raise InstallError(
            "--yes is required for blitz in non-interactive mode",
            "confirmation_required",
        )
    confirmation = input(f"Type {name} to confirm blitz: ").strip()
    if confirmation != name:
        raise InstallError(
            "Confirmation did not match the installation name; nothing was deleted.",
            "confirmation_required",
        )


def _validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise InstallError("Port must be between 1 and 65535", "invalid_arguments")
    return port


def _show_configuration(
    *,
    operation: str,
    name: str,
    mode: str,
    port: int,
    semantic: bool,
    source: Path | None,
    state_root: Path,
) -> None:
    print(paint("Configuration", "stage"))
    print(f"  Operation: {operation}")
    print(f"  Name: {name}")
    print(f"  Mode: {mode}")
    print(f"  Port: {port}")
    print(f"  Features: {'Attic plus semantic search' if semantic else 'Attic only'}")
    if source is not None:
        print(f"  Source: {source}")
    print(f"  Private state: {state_root / name}")
    print(
        "The installer will explain each stage and retain state for resume or rollback."
    )
    sys.stdout.flush()


def _load_falkordb_receipt(path: Path) -> dict[str, object]:
    source = _absolute_safe_path(path, purpose="FalkorDB receipt")
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_size > 64 * 1024
            ):
                raise InstallError(
                    "FalkorDB receipt must be a small owned regular file"
                )
            raw = stream.read(64 * 1024 + 1)
    except OSError as error:
        raise InstallError(f"Cannot safely read FalkorDB receipt: {error}") from error
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InstallError("FalkorDB receipt is malformed JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "image",
        "archive_sha256",
        "nodes",
    }:
        raise InstallError("FalkorDB receipt has an invalid schema")
    image = value.get("image")
    archive = value.get("archive_sha256")
    nodes = value.get("nodes")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(image, str)
        or not _RECEIPT_IMAGE_DIGEST.fullmatch(image)
        or not isinstance(archive, str)
        or re.fullmatch(r"[0-9a-f]{64}", archive) is None
        or not isinstance(nodes, list)
        or not nodes
    ):
        raise InstallError("FalkorDB receipt has an invalid schema")
    normalised_nodes: list[dict[str, str]] = []
    names: set[str] = set()
    uids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"name", "uid"}:
            raise InstallError("FalkorDB receipt has an invalid node record")
        node_name = node.get("name")
        uid = node.get("uid")
        if (
            not isinstance(node_name, str)
            or not _KUBERNETES_NODE.fullmatch(node_name)
            or not isinstance(uid, str)
            or not uid
            or len(uid) > 128
            or any(char.isspace() or ord(char) < 32 for char in uid)
            or node_name in names
            or uid in uids
        ):
            raise InstallError("FalkorDB receipt has invalid or duplicate nodes")
        names.add(node_name)
        uids.add(uid)
        normalised_nodes.append({"name": node_name, "uid": uid})
    return {
        "schema_version": 1,
        "image": image,
        "archive_sha256": archive,
        "nodes": normalised_nodes,
    }


def _load_falkordb_runtime(path: Path) -> dict[str, object]:
    source = _absolute_safe_path(path, purpose="FalkorDB runtime descriptor")
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_size > 64 * 1024
            ):
                raise InstallError(
                    "FalkorDB runtime descriptor must be a small owned regular file"
                )
            raw = stream.read(64 * 1024 + 1)
    except OSError as error:
        raise InstallError(
            f"Cannot safely read FalkorDB runtime descriptor: {error}"
        ) from error
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InstallError("FalkorDB runtime descriptor is malformed JSON") from error
    expected = {
        "schema_version",
        "image",
        "local_tag",
        "platform",
        "archive",
        "archive_sha256",
        "published",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise InstallError("FalkorDB runtime descriptor has an invalid schema")
    image = value.get("image")
    local_tag = value.get("local_tag")
    archive_name = value.get("archive")
    archive_sha256 = value.get("archive_sha256")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(image, str)
        or not _RECEIPT_IMAGE_DIGEST.fullmatch(image)
        or not isinstance(local_tag, str)
        or not _LOCAL_IMAGE_TAG.fullmatch(local_tag)
        or value.get("platform") != "linux/amd64"
        or not isinstance(archive_name, str)
        or Path(archive_name).name != archive_name
        or archive_name in {"", ".", ".."}
        or not isinstance(archive_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
        or value.get("published") is not False
    ):
        raise InstallError("FalkorDB runtime descriptor has an invalid schema")
    archive = _absolute_safe_path(
        source.parent / archive_name, purpose="FalkorDB runtime archive"
    )
    try:
        fd = os.open(archive, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise InstallError(
                    "FalkorDB runtime archive must be an owned regular file"
                )
            actual_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as error:
        raise InstallError(
            f"Cannot safely read FalkorDB runtime archive: {error}"
        ) from error
    if actual_sha256 != archive_sha256:
        raise InstallError("FalkorDB runtime archive digest does not match descriptor")
    return {
        "schema_version": 1,
        "image": image,
        "local_tag": local_tag,
        "platform": "linux/amd64",
        "archive": str(archive),
        "archive_sha256": archive_sha256,
        "published": False,
    }


def _kubernetes_configuration(
    args: argparse.Namespace, *, name: str, semantic: bool
) -> dict[str, object]:
    context = _required(
        args.kube_context,
        "--kube-context",
        "Kubernetes context: ",
        non_interactive=args.non_interactive,
    )
    storage_class = _required(
        args.kube_storage_class,
        "--kube-storage-class",
        "Kubernetes StorageClass: ",
        non_interactive=args.non_interactive,
    )
    image = _required(
        args.kube_image,
        "--kube-image",
        "Kubernetes image (repository@sha256:digest): ",
        non_interactive=args.non_interactive,
    )
    namespace = name if args.kube_namespace is None else args.kube_namespace
    if not all(
        isinstance(value, str) and value and not any(char.isspace() for char in value)
        for value in (context, storage_class)
    ):
        raise InstallError(
            "Kubernetes context and StorageClass must be non-empty single arguments",
            "invalid_arguments",
        )
    if not _KUBERNETES_NAMESPACE.fullmatch(namespace):
        raise InstallError(
            "--kube-namespace must be a 1–63 character lowercase Kubernetes name",
            "invalid_arguments",
        )
    if not _IMAGE_DIGEST.fullmatch(image):
        raise InstallError(
            "--kube-image must use a sha256 digest (REPOSITORY@sha256:DIGEST)",
            "invalid_arguments",
        )
    preloaded = bool(args.kube_preloaded_image)
    options: dict[str, object] = {
        "context": context,
        "namespace": namespace,
        "storage_class": storage_class,
        "image": image,
        "image_policy": "IfNotPresent" if preloaded else "Always",
        "preloaded_image": preloaded,
    }
    receipt_path = args.kube_falkordb_receipt
    if semantic and receipt_path is None:
        if args.non_interactive:
            raise InstallError(
                "--kube-falkordb-receipt is required for semantic Kubernetes installs",
                "invalid_arguments",
            )
        receipt_path = Path(
            _required(
                None,
                "--kube-falkordb-receipt",
                "FalkorDB node-staging receipt: ",
                non_interactive=False,
            )
        )
    if receipt_path is not None:
        if not semantic:
            raise InstallError(
                "--kube-falkordb-receipt requires --semantic", "invalid_arguments"
            )
        options["falkordb_receipt"] = _load_falkordb_receipt(receipt_path)
    return options


def _new_configuration(
    args: argparse.Namespace, default_source: Path | None
) -> tuple[
    str, str, int, bool, Path, str, dict[str, object] | None, dict[str, object] | None
]:
    mode = args.mode or _prompt_mode(args.non_interactive)
    if args.keep_running and mode != "disposable":
        raise InstallError(
            "--keep-running requires disposable mode", "invalid_arguments"
        )
    if mode not in MODES:
        raise InstallError(
            "Mode must be disposable, native, docker or kubernetes", "invalid_arguments"
        )
    name = _required(
        args.name,
        "--name",
        "Installation name: ",
        non_interactive=args.non_interactive,
    )
    port = _validate_port(
        args.port if args.port is not None else _prompt_port(args.non_interactive)
    )
    semantic = bool(args.semantic)
    if args.provider_key_file is not None and not semantic:
        raise InstallError(
            "--provider-key-file requires --semantic", "invalid_arguments"
        )
    if args.semantic and mode == "disposable":
        raise InstallError(
            "Disposable mode supports Attic only; semantic search is unavailable.",
            "invalid_arguments",
        )
    _validate_platform_features(mode, semantic)
    _validate_garden_platform(mode, enabled=args.garden_config is not None)
    if not args.semantic:
        semantic = _prompt_semantic(mode, args.non_interactive)
    runtime = None
    if semantic and mode in {"native", "docker"}:
        runtime_path = args.falkordb_runtime
        if runtime_path is None:
            if args.non_interactive:
                raise InstallError(
                    "--falkordb-runtime is required for semantic native and docker installs",
                    "invalid_arguments",
                )
            runtime_path = Path(
                _required(
                    None,
                    "--falkordb-runtime",
                    "FalkorDB runtime descriptor: ",
                    non_interactive=False,
                )
            )
        runtime = _load_falkordb_runtime(runtime_path)
    elif args.falkordb_runtime is not None:
        raise InstallError(
            "--falkordb-runtime requires semantic native or docker mode",
            "invalid_arguments",
        )
    kubernetes_options = (
        _kubernetes_configuration(args, name=name, semantic=semantic)
        if mode == "kubernetes"
        else None
    )
    if mode != "kubernetes" and any(
        (
            args.kube_context,
            args.kube_namespace,
            args.kube_storage_class,
            args.kube_image,
            args.kube_preloaded_image,
            args.kube_falkordb_receipt,
        )
    ):
        raise InstallError(
            "--kube-* options require --mode kubernetes", "invalid_arguments"
        )
    source_value = args.source or default_source
    if source_value is None:
        raise InstallError(
            "--source is required when using the installed cairn-install command",
            "invalid_arguments",
        )
    source = validate_source(source_value)
    return (
        name,
        mode,
        port,
        semantic,
        source,
        source_fingerprint(source),
        kubernetes_options,
        runtime,
    )


def _validate_platform_features(mode: str, semantic: bool) -> None:
    if mode == "native" and semantic and platform.system() == "Darwin":
        raise InstallError(
            "macOS native supports Attic only; semantic search is unavailable.",
            "invalid_arguments",
        )


def _validate_garden_platform(_mode: str, *, enabled: bool) -> None:
    if enabled and platform.system() == "Darwin":
        raise InstallError(
            "Garden is not supported on macOS; install Garden on Linux.",
            "invalid_arguments",
        )


def _assert_immutable(state: dict[str, Any], requested: dict[str, Any]) -> None:
    changed = [key for key, value in requested.items() if state.get(key) != value]
    if changed:
        names = ", ".join(changed)
        raise InstallError(
            f"Recorded installation options differ ({names}); use its original options."
        )


def _assert_resume_kubernetes_options(
    state: dict[str, Any], args: argparse.Namespace
) -> None:
    supplied: dict[str, object | None] = {
        "--kube-context": args.kube_context,
        "--kube-namespace": args.kube_namespace,
        "--kube-storage-class": args.kube_storage_class,
        "--kube-image": args.kube_image,
        "--kube-preloaded-image": args.kube_preloaded_image,
        "--kube-falkordb-receipt": (
            _load_falkordb_receipt(args.kube_falkordb_receipt)
            if args.kube_falkordb_receipt is not None
            else None
        ),
    }
    requested = {flag: value for flag, value in supplied.items() if value is not None}
    if not requested:
        return
    kubernetes = state.get("kubernetes")
    if not isinstance(kubernetes, dict):
        raise InstallError(
            "Kubernetes options were supplied for an installation without a Kubernetes transport record.",
            "invalid_arguments",
        )
    keys = {
        "--kube-context": "context",
        "--kube-namespace": "namespace",
        "--kube-storage-class": "storage_class",
        "--kube-image": "image",
        "--kube-preloaded-image": "preloaded_image",
        "--kube-falkordb-receipt": "falkordb_receipt",
    }
    changed = [
        flag for flag, value in requested.items() if kubernetes.get(keys[flag]) != value
    ]
    if changed:
        raise InstallError(
            "Recorded Kubernetes options differ ("
            + ", ".join(changed)
            + "); use its original options.",
            "invalid_arguments",
        )


def _assert_resume_core_options(
    state: dict[str, Any], args: argparse.Namespace
) -> None:
    requested: dict[str, object] = {}
    if args.mode is not None:
        requested["mode"] = args.mode
    if args.port is not None:
        requested["port"] = _validate_port(args.port)
    if args.semantic:
        requested["semantic"] = True
    _assert_immutable(state, requested)


def _assert_resume_falkordb_runtime(
    state: dict[str, Any], args: argparse.Namespace
) -> None:
    if args.falkordb_runtime is None:
        return
    if state.get("falkordb_runtime") != _load_falkordb_runtime(args.falkordb_runtime):
        raise InstallError(
            "Recorded FalkorDB runtime differs; use its original configuration.",
            "invalid_arguments",
        )


def _prepare_provider_key(ctx: Any, supplied: Path | None) -> None:
    destination = ctx.root / "credentials" / "openai-api-key"
    recorded = ctx.state.get("provider_key_file")
    if recorded is not None:
        if recorded != str(destination):
            raise InstallError(
                "Recorded provider key path is not this installation's path"
            )
        if supplied is None:
            ctx.check_file(destination)
            ctx.read_secret(destination)
            return
    input_path = supplied or (ctx.directory / "openai-api-key")
    input_path = _absolute_safe_path(input_path, purpose="Provider key")
    if not input_path.exists():
        if supplied is not None:
            raise InstallError(
                f"Provider key file does not exist: {input_path}",
                "provider_key_required",
            )
        atomic_write(input_path, b"", 0o600)
        raise InstallError(
            f"Semantic search needs an OpenAI key. Edit the protected file, then rerun: {input_path}",
            "provider_key_required",
        )
    if input_path.stat().st_size == 0:
        raise InstallError(
            f"Semantic search needs an OpenAI key. Edit the protected file, then rerun: {input_path}",
            "provider_key_required",
        )
    key = ctx.read_secret(input_path)
    ctx.write_file(destination, key + "\n", mode=0o600, secret=True)
    ctx.state["provider_key_file"] = str(destination)
    ctx.save()


def _render_result(result: object, *, operation: str, verbose: bool) -> None:
    if result is None:
        return
    if isinstance(result, str):
        print(result)
        return
    import json

    if operation == "status" or verbose or not isinstance(result, Mapping):
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    heading = {
        "install": "Installation complete",
        "resume": "Resume complete",
        "rollback": "Rollback complete",
        "blitz": "Blitz complete",
    }[operation]
    print(paint(heading, "success"))
    if operation == "blitz":
        for label, key in (("Name", "name"), ("Status", "status")):
            if value := result.get(key):
                print(f"  {label}: {value}")
        return
    fields = (
        ("Status", "status"),
        ("Endpoint", "endpoint"),
        ("State", "state"),
        ("Credential", "credential_file"),
        ("Garden", "garden_endpoint"),
        ("Garden adapters", "garden_profiles"),
    )
    for label, key in fields:
        if value := result.get(key):
            print(f"  {label}: {value}")


def _render_listing(rows: list[dict[str, object]], *, verbose: bool) -> None:
    if verbose:
        import json

        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        print("No recorded installations.")
        return
    columns = (
        ("NAME", "name"),
        ("MODE", "mode"),
        ("RECORDED STATUS", "status"),
        ("PORT", "port"),
        ("FEATURES", "features"),
    )
    table = [[title for title, _ in columns]]
    for record in rows:
        table.append(
            [
                str(record.get(key) if record.get(key) is not None else "-")
                for _, key in columns
            ]
        )
    widths = [max(len(row[index]) for row in table) for index in range(len(columns))]
    for index, row in enumerate(table):
        line = "  ".join(
            value.ljust(width) for value, width in zip(row, widths, strict=True)
        ).rstrip()
        print(paint(line, "stage") if index == 0 else line)


def _transcript_footer(args: argparse.Namespace | None, *, failed: bool) -> None:
    if args is None or args.operation in {"ls", "status"}:
        return
    path = getattr(args, "transcript_path", None)
    if not isinstance(path, Path):
        return
    try:
        info = path.lstat()
    except OSError:
        return
    if (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
    ):
        print(
            f"Transcript: {path}", file=sys.stderr if failed else sys.stdout, flush=True
        )


def _run(args: argparse.Namespace, default_source: Path | None) -> None:
    state_root = _absolute_safe_path(args.state_root, purpose="State root")
    operation = args.operation
    if args.keep_running and operation not in {"install", "resume"}:
        raise InstallError(
            "--keep-running is only valid with install or resume", "invalid_arguments"
        )
    if args.yes and operation != "blitz":
        raise InstallError(
            "--yes is only valid with blitz",
            "invalid_arguments",
        )
    if operation == "ls":
        from .listing import list_instances

        _render_listing(list_instances(state_root), verbose=args.verbose)
        return
    if operation == "blitz":
        name = _required(
            args.name,
            "--name",
            "Installation name: ",
            non_interactive=args.non_interactive,
        )
        _confirm_blitz(name, non_interactive=args.non_interactive, yes=args.yes)
        with open_context(state_root, name) as ctx:
            from . import workflow

            ctx.verbose = args.verbose
            args.transcript_path = ctx.directory / "commands.log"
            try:
                result = workflow.blitz_install(ctx)
            except InstallError as error:
                raise InstallError(
                    f"{error} Recovery information retained; retry blitz with the same name.",
                    error.code,
                ) from error
            _render_result(result, operation=operation, verbose=args.verbose)
        return
    if operation == "status":
        name = _required(
            args.name,
            "--name",
            "Installation name: ",
            non_interactive=args.non_interactive,
        )
        with open_read_context(state_root, name) as ctx:
            from . import workflow

            ctx.verbose = args.verbose
            args.transcript_path = ctx.directory / "commands.log"
            _show_configuration(
                operation=operation,
                name=ctx.name,
                mode=ctx.mode,
                port=ctx.port,
                semantic=ctx.semantic,
                source=None,
                state_root=state_root,
            )
            _render_result(
                workflow.status_install(ctx), operation=operation, verbose=args.verbose
            )
        return
    if operation == "rollback":
        name = _required(
            args.name,
            "--name",
            "Installation name: ",
            non_interactive=args.non_interactive,
        )
        with open_context(state_root, name) as ctx:
            from . import workflow

            ctx.verbose = args.verbose
            args.transcript_path = ctx.directory / "commands.log"
            _show_configuration(
                operation=operation,
                name=ctx.name,
                mode=ctx.mode,
                port=ctx.port,
                semantic=ctx.semantic,
                source=None,
                state_root=state_root,
            )
            _render_result(
                workflow.rollback_install(ctx),
                operation=operation,
                verbose=args.verbose,
            )
        return

    if operation == "resume":
        name = _required(
            args.name,
            "--name",
            "Installation name: ",
            non_interactive=args.non_interactive,
        )
        with open_context(state_root, name) as ctx:
            from . import workflow

            if args.keep_running and ctx.mode != "disposable":
                raise InstallError(
                    "--keep-running requires disposable mode", "invalid_arguments"
                )
            ctx.verbose = args.verbose
            args.transcript_path = ctx.directory / "commands.log"
            source = validate_source(args.source or ctx.source)
            fingerprint = source_fingerprint(source)
            _assert_immutable(
                ctx.state,
                {"source": str(source), "source_fingerprint": fingerprint},
            )
            _assert_resume_core_options(ctx.state, args)
            _assert_resume_kubernetes_options(ctx.state, args)
            _assert_resume_falkordb_runtime(ctx.state, args)
            if args.garden_config is not None:
                from .garden import load_options

                saved = ctx.state.get("garden", {}).get("options")
                if saved != load_options(args.garden_config, ctx.mode):
                    raise InstallError(
                        "Recorded Garden options differ; resume with the original configuration"
                    )
            _show_configuration(
                operation=operation,
                name=ctx.name,
                mode=ctx.mode,
                port=ctx.port,
                semantic=ctx.semantic,
                source=source,
                state_root=state_root,
            )
            _validate_platform_features(ctx.mode, ctx.semantic)
            _validate_garden_platform(ctx.mode, enabled="garden" in ctx.state)
            if ctx.semantic:
                _prepare_provider_key(ctx, args.provider_key_file)
            _render_result(
                (
                    workflow.run_install(ctx, keep_running=True)
                    if args.keep_running
                    else workflow.run_install(ctx)
                ),
                operation=operation,
                verbose=args.verbose,
            )
        return

    name, mode, port, semantic, source, fingerprint, kubernetes_options, runtime = (
        _new_configuration(args, default_source)
    )
    _show_configuration(
        operation="install",
        name=name,
        mode=mode,
        port=port,
        semantic=semantic,
        source=source,
        state_root=state_root,
    )
    create = {
        "mode": mode,
        "port": port,
        "semantic": semantic,
        "source": str(source),
        "source_fingerprint": fingerprint,
    }
    if kubernetes_options is not None:
        create["kubernetes"] = kubernetes_options
    if runtime is not None:
        create["falkordb_runtime"] = runtime
    garden_options = None
    if args.garden_config is not None:
        from .garden import load_options

        garden_options = load_options(args.garden_config, mode)
        if garden_options["port"] == port:
            raise InstallError("Garden and Cairn need different listening ports")
        create["garden"] = {"options": garden_options}
        print(f"  Garden HTTPS endpoint: {garden_options['endpoint']}")
        print("  Garden data: preserved by restart/rollback; deleted by blitz")
    with open_context(state_root, name, create=create) as ctx:
        from . import workflow

        ctx.verbose = args.verbose
        args.transcript_path = ctx.directory / "commands.log"
        # Garden's enrolment and lifecycle receipts accumulate under this key;
        # only its options form the immutable user configuration.
        _assert_immutable(
            ctx.state, {key: value for key, value in create.items() if key != "garden"}
        )
        if ctx.state.get("garden", {}).get("options") != garden_options:
            raise InstallError(
                "Recorded Garden options differ; use the original configuration"
            )
        if semantic:
            _prepare_provider_key(ctx, args.provider_key_file)
        _render_result(
            (
                workflow.run_install(ctx, keep_running=True)
                if args.keep_running
                else workflow.run_install(ctx)
            ),
            operation=operation,
            verbose=args.verbose,
        )


def main(
    argv: Sequence[str] | None = None, *, default_source: Path | None = None
) -> int:
    operation: str | None = None
    args: argparse.Namespace | None = None
    failed = True
    try:
        args = _parser().parse_args(argv)
        operation = args.operation
        with _termination_handlers():
            _run(args, default_source)
        failed = False
        return 0
    except InstallError as error:
        failed = True
        sys.stdout.flush()
        print(
            f"{paint('error', 'error', fd=2)} [{error.code}]: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 2
    except KeyboardInterrupt:
        failed = True
        sys.stdout.flush()
        recovery = (
            "recovery information retained. Retry blitz with the same name."
            if operation == "blitz"
            else "state retained. Use resume."
        )
        print(
            f"{paint('error', 'error', fd=2)} [interrupted]: interrupted; {recovery}",
            file=sys.stderr,
            flush=True,
        )
        return 130
    finally:
        _transcript_footer(args, failed=failed)


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
