"""Optional Garden configuration, capture-once enrolment and TLS checks.

Only public administration APIs are used. State holds references and receipts;
private keys and credentials remain in owned protected files.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cairn_install.core import TOKEN, Context, InstallError, read_owned
from cairn_install.verification import request, validate_instance

FIELDS = {
    "endpoint",
    "port",
    "tls_cert_file",
    "tls_key_file",
    "tls_ca_file",
    "scope",
    "classification",
    "participants",
    "expires_at",
    "image",
    "kubernetes_service_type",
    "allowed_cidrs",
}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
KIND = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:/@+%-]{0,254}")
MAX_RESPONSE = 128 * 1024
READY_SECONDS = 120


def require_listener_available(port: int, *, wildcard: bool) -> None:
    """Fail before enrolment if Garden cannot claim its documented host port."""
    host = "0.0.0.0" if wildcard else "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
    except OSError as error:
        raise InstallError(
            f"Garden listener port {port} is unavailable: {error}"
        ) from error


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _document(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, UnicodeError) as error:
        raise InstallError("Garden returned invalid or ambiguous JSON") from error


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC timestamp ending in Z required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("UUID string required")
    parsed = UUID(value)
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("canonical UUIDv4 required")
    return value


def load_options(path: Path | str, mode: str) -> dict[str, Any]:
    """Validate strict input and snapshot paths and PEM digests."""
    if mode not in {"native", "docker", "kubernetes"}:
        raise InstallError("Managed Garden requires native, docker or kubernetes mode")
    value = _document(read_owned(Path(path).absolute(), 128 * 1024))
    try:
        if set(value) - FIELDS:
            raise ValueError("unknown option")
        port = value.setdefault("port", 8443)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Garden listener port must be between 1024 and 65535")
        endpoint = urlsplit(value["endpoint"])
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path != "/mcp"
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("HTTPS public endpoint must end in /mcp")
        if endpoint.port is not None and endpoint.port < 1:
            raise ValueError("invalid public endpoint port")
        host = endpoint.hostname
        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            raise ValueError("public endpoint must not be loopback")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                raise ValueError("invalid endpoint hostname") from None
        else:
            if address.is_loopback or address.is_unspecified or address.is_link_local:
                raise ValueError("public endpoint must be externally reachable")
        scope = value["scope"]
        if (
            not isinstance(scope, dict)
            or set(scope) != {"realm", "segments"}
            or scope["realm"] != "local"
            or not isinstance(scope["segments"], list)
            or len(scope["segments"]) > 16
        ):
            raise ValueError("explicit local scope required")
        for segment in scope["segments"]:
            if (
                not isinstance(segment, dict)
                or set(segment) != {"kind", "identifier"}
                or not isinstance(segment["kind"], str)
                or not KIND.fullmatch(segment["kind"])
                or not isinstance(segment["identifier"], str)
                or not IDENTIFIER.fullmatch(segment["identifier"])
            ):
                raise ValueError("invalid scope segment")
        classification = value.setdefault("classification", "internal")
        if classification not in ("public", "internal", "restricted"):
            raise ValueError("invalid classification")
        participants = value["participants"]
        if not isinstance(participants, dict) or not 1 <= len(participants) <= 1000:
            raise ValueError("participants must contain 1..1000 entries")
        for name, adapter in participants.items():
            if not NAME.fullmatch(name) or adapter not in (
                "claude",
                "codex",
                "opencode",
            ):
                raise ValueError("invalid participant or adapter")
        expiry = _timestamp(value["expires_at"])
        if expiry <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        value["expires_at"] = expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        image = value.get("image")
        if image is not None and mode != "kubernetes":
            raise ValueError(
                "Native and Docker Garden use the included source; image is Kubernetes-only"
            )
        if image is not None and (
            not isinstance(image, str)
            or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image)
        ):
            raise ValueError("image must use an immutable sha256 digest")
        if value.setdefault("kubernetes_service_type", "ClusterIP") not in (
            "ClusterIP",
            "LoadBalancer",
        ):
            raise ValueError("invalid Kubernetes Service type")
        cidrs = value.get("allowed_cidrs", [])
        if not isinstance(cidrs, list) or any(
            not isinstance(cidr, str) for cidr in cidrs
        ):
            raise ValueError("allowed_cidrs must be a list")
        value["allowed_cidrs"] = [str(ipaddress.ip_network(cidr)) for cidr in cidrs]
        if mode == "kubernetes" and (not image or not cidrs):
            raise ValueError(
                "Kubernetes requires a pinned image and explicit allowed_cidrs"
            )
        hashes = {}
        for field in ("tls_cert_file", "tls_key_file", "tls_ca_file"):
            if field == "tls_ca_file" and field not in value:
                continue
            filename = value[field]
            if not isinstance(filename, str) or not Path(filename).is_absolute():
                raise ValueError("TLS paths must be absolute")
            raw = read_owned(Path(filename), 128 * 1024)
            if field == "tls_key_file" and stat.S_IMODE(
                Path(filename).stat().st_mode
            ) not in (0o400, 0o600):
                raise ValueError("TLS private key must have mode 0400 or 0600")
            hashes[field] = hashlib.sha256(raw).hexdigest()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(
            value["tls_cert_file"], value["tls_key_file"], password=lambda: ""
        )
        if "tls_ca_file" in value:
            ssl.create_default_context(cafile=value["tls_ca_file"])
        value["tls_hashes"] = hashes
        return value
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise InstallError("Invalid Garden options: " + str(error)) from None


def _state(ctx: Context) -> dict[str, Any]:
    garden: dict[str, Any] = ctx.state["garden"]
    digest = hashlib.sha256(_json(garden["options"]).encode()).hexdigest()
    if garden.get("options_digest", digest) != digest:
        raise InstallError(
            "Garden options changed; restore the retained deployment binding"
        )
    return garden


def prepare(ctx: Context) -> None:
    """Copy immutable TLS material into the instance's owned private directory."""
    garden = _state(ctx)
    options = garden["options"]
    garden.setdefault(
        "options_digest", hashlib.sha256(_json(options).encode()).hexdigest()
    )
    ctx.save()
    paths = {}
    for source, target, field in (
        ("tls_cert_file", "server.crt", "cert_file"),
        ("tls_key_file", "server.key", "key_file"),
        ("tls_ca_file", "ca.crt", "ca_file"),
    ):
        if source not in options:
            continue
        dest = ctx.root / "garden" / "tls" / target
        if dest.exists() or dest.is_symlink():
            ctx.check_file(dest)
            raw = read_owned(dest, 128 * 1024)
        else:
            raw = read_owned(Path(options[source]), 128 * 1024)
        if hashlib.sha256(raw).hexdigest() != options["tls_hashes"][source]:
            raise InstallError("Garden TLS material changed; restore the retained file")
        try:
            content = raw.decode("ascii")
        except UnicodeError:
            raise InstallError("TLS material must be PEM") from None
        ctx.write_file(dest, content, secret=source == "tls_key_file")
        paths[field] = str(dest)
    garden["tls"] = paths
    ctx.save()


def _receipt(value: dict[str, Any]) -> dict[str, Any]:
    try:
        if value["outcome"] not in ("committed", "replayed"):
            raise ValueError("not committed")
        _uuid(value["mutation_receipt"]["mutation_id"])
        _uuid(value["audit_receipt"]["event_id"])
        result: dict[str, Any] = value["result"]
        if not isinstance(result, dict):
            raise ValueError("missing result")
        return result
    except (KeyError, TypeError, ValueError):
        raise InstallError(
            "Garden enrolment response did not prove a committed operation"
        ) from None


def _mutation(
    ctx: Context,
    agent: dict[str, Any],
    operation: str,
    body: dict[str, Any],
    admin: str,
    capture: Path | None = None,
) -> dict[str, Any]:
    operations = agent.setdefault("operations", {})
    if operation not in operations:
        operations[operation] = {"key": str(uuid4()), "body": body}
        ctx.save()
    saved = operations[operation]
    if saved["body"] != body:
        raise InstallError(
            "Garden enrolment request changed; retained key cannot be reused"
        )
    if "receipt" in saved:
        _receipt(saved["receipt"])
        if capture is None:
            retained: dict[str, Any] = saved["receipt"]
            return retained
    if capture is not None and (capture.exists() or capture.is_symlink()):
        ctx.check_file(capture)
        response = _document(ctx.read_secret(capture).encode())
    else:
        response = request(
            ctx, "/v1/" + operation, admin, data=_json(body).encode(), key=saved["key"]
        )
        result = _receipt(response)
        if capture is not None:
            token = result.get("plaintext")
            if (
                response["outcome"] != "committed"
                or not isinstance(token, str)
                or not TOKEN.fullmatch(token)
            ):
                raise InstallError(
                    "Credential plaintext was not captured; explicit credential recovery is required",
                    "needs_credential_recovery",
                )
            ctx.add_secret(token)
            ctx.write_file(capture, _json(response) + "\n", secret=True)
    _receipt(response)
    safe = json.loads(_json(response))
    safe["result"].pop("plaintext", None)
    saved["receipt"] = safe
    ctx.save()
    return response


def _diagnose(ctx: Context, name: str, agent: dict[str, Any], token: str) -> None:
    options = _state(ctx)["options"]
    response = request(
        ctx,
        "/memory/v1/diagnose",
        token,
        data=_json(
            {"scope": options["scope"], "classification": options["classification"]}
        ).encode(),
    )
    permissions = response.get("permissions", {})
    fields = {
        "instance_id",
        "product_version",
        "contract_identity",
        "contract_digest",
        "mcp_contract_digest",
        "principal_id",
        "principal_kind",
        "scope",
        "classification",
        "permissions",
        "evaluated_at",
        "permission_basis",
    }
    if (
        set(response) != fields
        or response.get("contract_identity") != "cairn.memory/v1"
        or response.get("instance_id") != ctx.instance_id
        or response.get("principal_id") != agent["principal_id"]
        or response.get("principal_kind") != "workload"
        or response.get("scope") != options["scope"]
        or response.get("classification") != options["classification"]
        or response.get("permission_basis") != "current_grants_only"
        or not isinstance(permissions, dict)
        or set(permissions) != {"retrieve", "ingest", "promote", "invalidate"}
        or permissions.get("retrieve") is not True
        or permissions.get("ingest") is not True
        or permissions.get("promote") is not False
        or permissions.get("invalidate") is not False
    ):
        raise InstallError(
            f"Garden authority differs for {name}; inspect grants or explicit recovery"
        )
    try:
        _timestamp(response["evaluated_at"])
        for field in ("contract_digest", "mcp_contract_digest"):
            if not isinstance(response[field], str) or not re.fullmatch(
                r"[0-9a-f]{64}", response[field]
            ):
                raise ValueError("invalid digest")
        if (
            not isinstance(response["product_version"], str)
            or not response["product_version"]
        ):
            raise ValueError("missing product version")
    except (TypeError, ValueError):
        raise InstallError("Garden diagnostic response metadata is invalid") from None
    identity = ctx.state.get("receipts", {}).get("instance", {})
    if (
        "product_version" in identity
        and response["product_version"] != identity["product_version"]
    ):
        raise InstallError(
            "Garden diagnostic product version differs from authenticated Cairn identity"
        )
    for field, filename in (
        ("contract_digest", "cairn-memory-openapi-v1.json"),
        ("mcp_contract_digest", "cairn-memory-mcp-tools-v1.json"),
    ):
        expected = hashlib.sha256(
            read_owned(ctx.source / "src" / "cairn" / "contracts" / filename)
        ).hexdigest()
        if response[field] != expected:
            raise InstallError(
                "Garden diagnostic contract differs from this source distribution"
            )
    agent["diagnostic"] = response
    ctx.save()


def enrol(ctx: Context, admin_token: str) -> None:
    """Create one workload principal, fixed credential and exact grant per agent."""
    garden = _state(ctx)
    options = garden["options"]
    if _timestamp(options["expires_at"]) <= datetime.now(UTC):
        raise InstallError(
            "Garden authority has expired; explicit recovery is required"
        )
    ctx.add_secret(admin_token)
    validate_instance(request(ctx, "/v1/instance", admin_token), ctx.instance_id)
    agents = garden.setdefault("agents", {})
    for name in sorted(options["participants"]):
        agent = agents.setdefault(name, {})
        principal = _receipt(
            _mutation(
                ctx,
                agent,
                "create-principal",
                {"realm_id": "local", "kind": "workload", "label": "garden-" + name},
                admin_token,
            )
        )
        try:
            principal_id = _uuid(principal["principal_id"])
            if (
                principal["kind"] != "workload"
                or principal["label"] != "garden-" + name
            ):
                raise ValueError("principal mismatch")
        except (KeyError, TypeError, ValueError):
            raise InstallError(
                "Garden principal response differs from enrolment"
            ) from None
        agent["principal_id"] = principal_id
        directory = ctx.root / "credentials" / "garden" / name
        capture = directory / "issue.json"
        issued = _receipt(
            _mutation(
                ctx,
                agent,
                "issue-credential",
                {
                    "realm_id": "local",
                    "principal_id": principal_id,
                    "expires_at": options["expires_at"],
                },
                admin_token,
                capture,
            )
        )
        try:
            credential_id = _uuid(issued["credential_id"])
            token = issued["plaintext"]
            if (
                issued["principal_id"] != principal_id
                or issued["expires_at"] != options["expires_at"]
                or not isinstance(token, str)
                or not TOKEN.fullmatch(token)
                or token.split(".")[1] != credential_id
            ):
                raise ValueError("credential mismatch")
        except (KeyError, TypeError, ValueError):
            raise InstallError(
                "Garden credential capture differs from enrolment",
                "needs_credential_recovery",
            ) from None
        token_file = directory / "agent.token"
        ctx.add_secret(token)
        ctx.write_file(token_file, token + "\n", secret=True)
        agent.update(credential_id=credential_id, token_file=str(token_file))
        granted = _receipt(
            _mutation(
                ctx,
                agent,
                "create-grant",
                {
                    "realm_id": "local",
                    "grant": {
                        "principal_id": principal_id,
                        "realm_id": "local",
                        "segments": options["scope"]["segments"],
                        "operations": ["retrieve", "ingest"],
                        "read_clearance": options["classification"],
                        "write_classifications": [options["classification"]],
                        "expires_at": options["expires_at"],
                    },
                },
                admin_token,
            )
        )
        try:
            agent["grant_id"] = _uuid(granted["grant_id"])
        except (KeyError, TypeError, ValueError):
            raise InstallError("Garden grant response is invalid") from None
        _diagnose(ctx, name, agent, token)
    ctx.save()


def write_json_config(
    ctx: Context, path: Path, value: dict[str, Any], *, mode: int = 0o600
) -> None:
    """Preserve owned equivalent JSON across state reloads and older formatting.

    Ownership remains byte-exact: existing contents are checked against the
    journal before comparing documents. Different configuration, altered files,
    ambiguous JSON and changed permissions still fail closed.
    """
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists() or path.is_symlink():
        ctx.check_file(path)
        raw = read_owned(path)
        if _json(_document(raw)) != _json(value):
            raise InstallError(
                f"Garden configuration changed; preserve and inspect: {path}"
            )
        content = raw.decode("utf-8")
    ctx.write_file(path, content, mode=mode)


def gateway_config(
    ctx: Context,
    *,
    data_dir: str,
    cert_file: str,
    key_file: str,
    daemon_url_file: str,
    listen: str,
    cairn_url: str,
) -> dict[str, Any]:
    """Return the Go gateway schema; secrets are file references only."""
    garden = _state(ctx)
    options = garden["options"]
    endpoint = cairn_url.rstrip("/")
    if not endpoint.endswith("/memory/v1/diagnose"):
        endpoint += "/memory/v1/diagnose"
    return {
        "listen": listen,
        "data_dir": data_dir,
        "daemon_url_file": daemon_url_file,
        "tls_cert_file": cert_file,
        "tls_key_file": key_file,
        "auth": {
            "endpoint": endpoint,
            "instance_id": ctx.instance_id,
            "scope": options["scope"],
            "classification": options["classification"],
        },
        "principals": {
            garden["agents"][name]["principal_id"]: name
            for name in options["participants"]
        },
    }


def profiles(ctx: Context) -> None:
    """Write per-participant bundles with explicit host-session placeholders."""
    garden = _state(ctx)
    options = garden["options"]
    exports = {}
    for name, adapter in options["participants"].items():
        directory = ctx.root / "garden" / "profiles" / name
        credential = Path(garden["agents"][name]["token_file"])
        ctx.check_file(credential)
        ctx.write_file(
            directory / "agent.token", ctx.read_secret(credential) + "\n", secret=True
        )
        profile: dict[str, Any] = {
            "garden_endpoint": options["endpoint"],
            "credential_file": "agent.token",
            "instance_id": ctx.instance_id,
            "scope": options["scope"],
            "classification": options["classification"],
            "participant": name,
            "adapter": adapter,
        }
        if garden["tls"].get("ca_file"):
            ca = Path(garden["tls"]["ca_file"])
            ctx.check_file(ca)
            ctx.write_file(directory / "ca.crt", read_owned(ca).decode("ascii"))
            profile["garden_tls_ca_file"] = "ca.crt"
        if adapter == "codex":
            profile.update(
                session_id="REPLACE_WITH_EXISTING_TASK_ID",
                codex_socket="/REPLACE/WITH/EXISTING/CODEX/CONTROL.sock",
                codex_binary="codex",
            )
        elif adapter == "opencode":
            profile.update(
                session_id="REPLACE_WITH_EXISTING_SESSION_ID",
                host_endpoint="http://127.0.0.1:4096",
                host_username="opencode",
                host_credential_file="REPLACE_WITH_HOST_PASSWORD_FILE",
            )
        filename = adapter + ".profile.example.json"
        write_json_config(ctx, directory / filename, profile)
        command = "/REPLACE/WITH/INSTALLED/a2a"
        arguments = ["connect", "--profile", "/REPLACE/WITH/BUNDLE/profile.json"]
        if adapter == "codex":
            ctx.write_file(
                directory / "codex.mcp.example.toml",
                "[mcp_servers.garden]\ncommand = "
                + json.dumps(command)
                + "\nargs = "
                + json.dumps(arguments)
                + "\n",
            )
        else:
            mcp: dict[str, Any]
            if adapter == "claude":
                mcp = {
                    "mcpServers": {"garden": {"command": command, "args": arguments}}
                }
            else:
                mcp = {
                    "mcp": {
                        "garden": {
                            "type": "local",
                            "command": [command, *arguments],
                            "enabled": True,
                        }
                    }
                }
            write_json_config(ctx, directory / (adapter + ".mcp.example.json"), mcp)
        if adapter == "claude":
            setup = (
                "Copy claude.profile.example.json to profile.json; it needs no edits. "
                "In claude.mcp.example.json, replace the REPLACE paths with the installed "
                "a2a binary and this bundle's profile.json. "
            )
        elif adapter == "codex":
            setup = (
                "Copy codex.profile.example.json to profile.json and replace its REPLACE "
                "task and control-socket values. In codex.mcp.example.toml, replace the "
                "REPLACE paths with the installed a2a binary and this bundle's profile.json. "
            )
        else:
            setup = (
                "Copy opencode.profile.example.json to profile.json and replace its REPLACE "
                "host-session values. In opencode.mcp.example.json, replace the REPLACE paths "
                "with the installed a2a binary and this bundle's profile.json. Explicitly configure "
                "the local server endpoint and protected password file. "
            )
        ctx.write_file(
            directory / "README.md",
            f"# Garden participant {name}\n\n"
            "Copy only this participant's directory to its agent machine, using a secure channel.\n"
            "Keep agent.token mode 0600 and this directory mode 0700. Do not distribute installer "
            "administrator credentials, issuance captures or other participants' directories.\n\n"
            "Install a2a. " + setup + "No host session is created.\n\n"
            "Run `a2a doctor --profile /absolute/path/profile.json` to verify the Garden "
            "binding, then "
            "`a2a connect --profile /absolute/path/profile.json` for the MCP stdio bridge. "
            "Configure the agent's MCP command to this a2a connect invocation; no bearer belongs in argv.\n\n"
            "For Claude use a2a connect as its MCP server. For Codex/OpenCode run "
            "`a2a attend --profile /absolute/path/profile.json` alongside the MCP bridge. "
            "Only one attention consumer may own this participant. The profile pins the deployment "
            "identity and scope; tokens remain subject to Cairn revocation and expiry.\n\n"
            "For Claude's unlisted development channel, use the MCP server name garden and "
            "`claude --dangerously-load-development-channels server:garden` only after reviewing "
            "the adapter and completing host channel consent. Organisation policy still applies. "
            "Claude must call acknowledge_delivery on receipt of each channel message.\n\n"
            "Codex requires an existing app-server control socket supporting "
            "`codex app-server proxy --sock` (inspected protocol 0.154.0). An arbitrary desktop "
            "or CLI session without that endpoint cannot be attached.\n",
        )
        exports[name] = str(directory)
    garden["profiles"] = exports
    ctx.save()


class _LocalTLS(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        public_port: int,
        connect_host: str,
        connect_port: int,
        context: ssl.SSLContext,
        timeout: float,
    ) -> None:
        super().__init__(hostname, public_port, context=context, timeout=timeout)
        self.connect_host = connect_host
        self.connect_port = connect_port
        self.tls_context = context

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.connect_host, self.connect_port), self.timeout
        )
        try:
            self.sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _rpc(
    connection: _LocalTLS, token: str, ident: int, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    # Preserve SDK localhost rebinding protection while TLS still verifies the
    # configured public hostname. The installer connects directly/over a local
    # port-forward, rather than asking a public virtual host to route the probe.
    host = connection.connect_host
    if ":" in host:
        host = "[" + host + "]"
    connection.request(
        "POST",
        "/mcp",
        body=_json({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}),
        headers={
            "Host": f"{host}:{connection.connect_port}",
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "identity",
            "MCP-Protocol-Version": "2025-11-25",
        },
    )
    response = connection.getresponse()
    if (
        response.status != 200
        or response.getheader("Content-Encoding", "identity") != "identity"
        or response.getheader("Content-Type", "").split(";")[0] != "application/json"
    ):
        raise InstallError(
            f"Garden authenticated TLS check returned HTTP {response.status}, "
            f"content type {response.getheader('Content-Type', '')[:100]!r}"
        )
    raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise InstallError("Garden returned an oversized MCP response")
    value = _document(raw)
    if (
        value.get("jsonrpc") != "2.0"
        or value.get("id") != ident
        or "error" in value
        or not isinstance(value.get("result"), dict)
    ):
        raise InstallError("Garden returned an invalid MCP response")
    result: dict[str, Any] = value["result"]
    return result


def verify(
    ctx: Context, connect_host: str = "127.0.0.1", connect_port: int | None = None
) -> None:
    """Bounded TLS readiness and per-agent MCP status; validate public-name SNI."""
    garden = _state(ctx)
    options = garden["options"]
    endpoint = urlsplit(options["endpoint"])
    if garden["tls"].get("ca_file"):
        ctx.check_file(Path(garden["tls"]["ca_file"]))
    tls = ssl.create_default_context(cafile=garden["tls"].get("ca_file"))
    deadline = time.monotonic() + READY_SECONDS
    verified = {}
    for name in sorted(options["participants"]):
        token_file = Path(garden["agents"][name]["token_file"])
        ctx.check_file(token_file)
        token = ctx.read_secret(token_file)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstallError("Garden authenticated readiness deadline exceeded")
            connection = _LocalTLS(
                endpoint.hostname or "",
                endpoint.port or 443,
                connect_host,
                connect_port or options["port"],
                tls,
                min(10, remaining),
            )
            try:
                initialized = _rpc(
                    connection,
                    token,
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "cairn-install", "version": "1"},
                    },
                )
                if initialized.get("protocolVersion") != "2025-11-25":
                    raise InstallError("Garden MCP protocol version differs")
                result = _rpc(
                    connection,
                    token,
                    2,
                    "tools/call",
                    {"name": "status", "arguments": {}},
                )
                status = result.get("structuredContent", {})
                if (
                    result.get("isError", False) is not False
                    or not isinstance(status, dict)
                    or status.get("binding")
                    != {
                        "instance_id": ctx.instance_id,
                        "scope": options["scope"],
                        "classification": options["classification"],
                    }
                    or status.get("participant") != name
                    or not isinstance(status.get("generation"), str)
                    or not status["generation"]
                    or status.get("participants") != sorted(options["participants"])
                ):
                    raise InstallError(
                        "Garden MCP status differs from the retained deployment binding"
                    )
                verified[name] = status
                break
            except ssl.SSLError as error:
                reason = " ".join(str(error).split())[:500]
                raise InstallError(
                    "Garden TLS certificate validation failed for "
                    + (endpoint.hostname or "configured endpoint")
                    + ": "
                    + (reason or error.__class__.__name__)
                ) from None
            except (OSError, http.client.HTTPException):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InstallError(
                        "Garden authenticated readiness deadline exceeded"
                    ) from None
                time.sleep(min(2, remaining))
            finally:
                connection.close()
    garden["verification"] = verified
    ctx.save()
