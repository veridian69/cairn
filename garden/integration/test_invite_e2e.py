"""Opt-in end-to-end test: invite, join and the remote CLI against a disposable real Cairn.

The built garden binary (GARDEN_TEST_BINARY) runs against a disposable Cairn that
cairn_fixture.py starts with GARDEN_CAIRN_PYTHON. Everything lives under one temporary
directory; no host configuration, credential or home directory is read. `make integration`
runs this test.

Cairn stays on plain HTTP at 127.0.0.1, which the gateway allows for diagnosis. The gateway
serves HTTPS with a leaf for localhost and 127.0.0.1, chained to a test CA. join and the
remote CLI dial localhost, which resolves to the loopback address on the test host.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

GARDEN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = GARDEN_ROOT.parent
FIXTURE = GARDEN_ROOT / "integration" / "cairn_fixture.py"
# A synthetic passphrase for this test only. It is typed into a pty and never logged.
PASSPHRASE = b"invite e2e passphrase"
TIMEOUT = 60.0


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    """A minimal environment, so nothing from the host reaches the test."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
    env.update(extra)
    return env


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_for(check: Callable[[], bool], what: str, log: Path | None = None) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not check():
        if time.monotonic() > deadline:
            detail = f"\n{tail(log)}" if log else ""
            raise AssertionError(f"timed out waiting for {what}{detail}")
        time.sleep(0.05)


def tail(path: Path | None, limit: int = 2000) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(errors="replace")[-limit:]


def start(argv: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    """Start a long-running process whose output goes to log."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as handle:
        return subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )


def stop(process: subprocess.Popen[bytes], signum: int) -> None:
    """Ask a process to stop, then kill it if it does not. Safe to call twice."""
    if process.poll() is None:
        process.send_signal(signum)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def stop_fixture(process: subprocess.Popen[bytes]) -> None:
    """Closing stdin shuts the disposable Cairn down."""
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def read_line(stream: IO[bytes], timeout: float) -> str:
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        raise AssertionError("timed out waiting for the disposable Cairn to publish")
    line = stream.readline()
    if not line:
        raise AssertionError("the disposable Cairn exited before it published")
    return line.decode()


def run(
    argv: list[str], env: dict[str, str], stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


class Pty:
    """Reads a pseudo-terminal, so a command that asks for a passphrase can be answered."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.output = bytearray()
        self.cursor = 0

    def expect(self, marker: bytes) -> None:
        deadline = time.monotonic() + TIMEOUT
        while True:
            at = self.output.find(marker, self.cursor)
            if at >= 0:
                self.cursor = at + len(marker)
                return
            self._fill(deadline, marker)

    def drain(self) -> None:
        deadline = time.monotonic() + TIMEOUT
        while self._fill(deadline, None):
            pass

    def _fill(self, deadline: float, marker: bytes | None) -> bool:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([self.fd], [], [], max(remaining, 0))
        if not ready:
            raise AssertionError(
                f"timed out on the terminal waiting for {marker!r}: {bytes(self.output[-400:])!r}"
            )
        try:
            chunk = os.read(self.fd, 4096)
        except OSError:
            # Linux reports the closed terminal as EIO once the child has exited.
            chunk = b""
        if not chunk:
            if marker is None:
                return False
            raise AssertionError(
                f"the terminal closed before {marker!r}: {bytes(self.output[-400:])!r}"
            )
        self.output += chunk
        return True


def run_pty(
    argv: list[str],
    env: dict[str, str],
    answers: list[tuple[bytes, bytes]],
    capture: tuple[Path, Path] | None = None,
) -> tuple[int, bytes]:
    """Run argv with the pseudo-terminal as its controlling terminal.

    Each answer waits for its prompt, then types the reply. The return value is the exit code
    and everything the command printed to the terminal. With capture, the command's stdout and
    stderr go to those files instead, so that a code printed on stdout is kept apart from the
    prompts, which still go to the terminal.
    """
    pid, fd = pty.fork()
    if pid == 0:
        try:
            if capture is not None:
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                os.dup2(os.open(capture[0], flags, 0o600), 1)
                os.dup2(os.open(capture[1], flags, 0o600), 2)
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    terminal = Pty(fd)
    try:
        for marker, reply in answers:
            terminal.expect(marker)
            os.write(fd, reply + b"\n")
        terminal.drain()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise
    finally:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), bytes(terminal.output)


def make_tls(directory: Path) -> tuple[Path, Path]:
    """An internal CA and a leaf for localhost and 127.0.0.1.

    Returns the chain file (leaf, then CA) and the leaf key. The certificates are issued from
    the same clock that the gateway and the clients read.
    """

    def openssl(*args: str) -> None:
        subprocess.run(
            ["openssl", *args], cwd=directory, check=True, capture_output=True
        )

    openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-subj", "/CN=Garden invite test CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )  # fmt: skip
    (directory / "leaf.ext").write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
    )
    openssl(
        "req", "-newkey", "rsa:2048", "-nodes",
        "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=localhost",
    )  # fmt: skip
    openssl(
        "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-set_serial", "2", "-days", "2", "-extfile", "leaf.ext", "-out", "leaf.pem",
    )  # fmt: skip
    chain = directory / "chain.pem"
    chain.write_text(
        (directory / "leaf.pem").read_text() + (directory / "ca.pem").read_text()
    )
    os.chmod(directory / "leaf.key", 0o600)
    return chain, directory / "leaf.key"


def pem_body(text: str) -> str:
    """The base64 body of a single PEM certificate, without line breaks."""
    match = re.search(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.S
    )
    assert match is not None, "no PEM certificate"
    return "".join(match.group(1).split())


class InviteJoinRemoteEndToEnd(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("GARDEN_TEST_BINARY"),
        "explicit opt-in: set GARDEN_TEST_BINARY to the built garden binary",
    )
    def test_invite_join_and_remote_cli_against_real_cairn(self) -> None:
        binary = Path(os.environ["GARDEN_TEST_BINARY"])
        python = Path(
            os.environ.get("GARDEN_CAIRN_PYTHON") or REPO_ROOT / ".venv/bin/python"
        )
        self.assertTrue(
            binary.is_absolute() and binary.is_file(),
            "GARDEN_TEST_BINARY must be an absolute file",
        )
        self.assertTrue(
            os.access(python, os.X_OK), "GARDEN_CAIRN_PYTHON must be executable"
        )

        root = Path(tempfile.mkdtemp(prefix="garden-invite-e2e-"))
        self.addCleanup(shutil.rmtree, root, True)
        logs = root / "logs"
        with contextlib.ExitStack() as stack:
            self._run(stack, root, logs, binary, python)

    def _run(
        self,
        stack: contextlib.ExitStack,
        root: Path,
        logs: Path,
        binary: Path,
        python: Path,
    ) -> None:
        # 1. Certificates for the gateway.
        tls = root / "tls"
        tls.mkdir(mode=0o700)
        chain, leaf_key = make_tls(tls)

        # 2. The disposable Cairn, with an administrator (GARDEN_FIXTURE_ADMIN=1).
        fixture_log = logs / "fixture.log"
        fixture_log.parent.mkdir(parents=True, exist_ok=True)
        fixture_handle = fixture_log.open("wb")
        stack.callback(fixture_handle.close)
        fixture = subprocess.Popen(
            [str(python), str(FIXTURE)],
            cwd=REPO_ROOT,
            env=clean_env(
                root / "fixture-home",
                PYTHONPATH=str(REPO_ROOT / "src"),
                PYTHONNOUSERSITE="1",
                PYTHONDONTWRITEBYTECODE="1",
                GARDEN_FIXTURE_ADMIN="1",
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=fixture_handle,
        )
        assert fixture.stdout is not None
        stack.callback(fixture.stdout.close)
        stack.callback(stop_fixture, fixture)
        published: dict[str, Any] = json.loads(read_line(fixture.stdout, TIMEOUT))
        admin = published["admin"]
        alice = published["actors"]["alice"]
        admin_token = admin["token"]
        admin_token_file = root / "admin.token"
        write_private(admin_token_file, admin_token + "\n")
        # Every output and log is checked for the admin credential at the end.
        seen_outputs: list[str] = []

        # 3. The zero-agent daemon, which owns the stream the gateway reads.
        daemon_home = root / "daemon-home"
        (daemon_home / ".garden").mkdir(parents=True)
        (daemon_home / ".garden" / "config.yaml").write_text(
            f"agents: {{}}\nstream:\n  data_dir: {daemon_home / '.garden' / 'data'}\n"
        )
        daemon = start(
            [str(binary), "start"],
            clean_env(daemon_home),
            logs / "daemon.log",
        )
        stack.callback(stop, daemon, signal.SIGINT)
        daemon_url = daemon_home / ".garden" / "daemon.url"
        wait_for(
            lambda: daemon_url.exists() and daemon_url.stat().st_size > 0,
            "the daemon URL",
            logs / "daemon.log",
        )

        # 4. The gateway: invites directory, a single existing principal (alice), HTTPS.
        # The gateway's data_dir must be the daemon's storage directory, as in the Go TLS test.
        gateway_root = root / "gateway"
        gateway_root.mkdir(mode=0o700)
        (gateway_root / "invites").mkdir(mode=0o700)
        gateway_port = free_port()
        gateway_config = root / "gateway.json"
        write_private(
            gateway_config,
            json.dumps(
                {
                    "public_endpoint": f"https://localhost:{gateway_port}/mcp",
                    "listen": f"127.0.0.1:{gateway_port}",
                    "data_dir": str(daemon_home / ".garden" / "data"),
                    "daemon_url_file": str(daemon_url),
                    "tls_cert_file": str(chain),
                    "tls_key_file": str(leaf_key),
                    "auth": {
                        "endpoint": published["endpoint"],
                        "instance_id": published["instance_id"],
                        "scope": published["scope"],
                        "classification": published["classification"],
                    },
                    "principals": {alice["id"]: "alice"},
                    "invites_dir": str(gateway_root / "invites"),
                },
                indent=2,
            )
            + "\n",
        )
        serve_log = logs / "serve.log"
        serve = start(
            [str(binary), "serve", "--config", str(gateway_config)],
            clean_env(root / "serve-home"),
            serve_log,
        )
        stack.callback(stop, serve, signal.SIGTERM)
        wait_for(
            lambda: serve.poll() is not None or port_open(gateway_port),
            "the gateway listener",
            serve_log,
        )
        self.assertIsNone(serve.poll(), tail(serve_log))

        # 5. Two invites from the gateway host, each with an administrator's credential.
        invite_env = clean_env(root / "invite-home")

        def invite(name: str, harness: str) -> tuple[str, str]:
            argv = [
                str(binary), "invite", name, "--harness", harness,
                "--admin-token-file", str(admin_token_file),
                "--gateway-config", str(gateway_config),
                "--reload-unit", "",
            ]  # fmt: skip
            if harness == "cli":
                # A cli invite asks for the passphrase on the terminal, twice, before any
                # admin call. Its stdout and stderr are captured apart from the prompts.
                captured = root / f"invite-{name}"
                captured.mkdir()
                status, _ = run_pty(
                    argv,
                    invite_env,
                    [
                        (f"passphrase for {name}: ".encode(), PASSPHRASE),
                        (b"again: ", PASSPHRASE),
                    ],
                    capture=(captured / "stdout", captured / "stderr"),
                )
                stdout = (captured / "stdout").read_text()
                stderr = (captured / "stderr").read_text()
                returncode = status
            else:
                result = run(argv, invite_env)
                stdout, stderr, returncode = (
                    result.stdout,
                    result.stderr,
                    result.returncode,
                )
            seen_outputs.extend([stdout, stderr])
            self.assertEqual(returncode, 0, stderr)
            code = stdout.strip()
            self.assertTrue(code.startswith("garden1:") and "\n" not in code, stdout)
            match = re.search(r"^principal: (\S+)$", stderr, re.M)
            self.assertIsNotNone(match, stderr)
            assert match is not None
            self.assertIn("gateway not reloaded: send SIGHUP", stderr)
            if harness == "cli":
                self.assertIn("only the passphrase-sealed key", stderr)
            # The invites directory never holds a plain Cairn credential.
            for record in (gateway_root / "invites").glob("*.json"):
                self.assertNotIn("cairn1.", record.read_text())
            return code, match.group(1)

        zen_code, zen_principal = invite("zen", "opencode")
        pat_code, pat_principal = invite("pat", "cli")

        # 6. The gateway reloads on SIGHUP and maps both new principals.
        serve.send_signal(signal.SIGHUP)
        wait_for(
            lambda: "2 principal(s) added" in tail(serve_log, 100000),
            "the gateway reload",
            serve_log,
        )
        self.assertNotIn("refused", tail(serve_log, 100000))
        mapped = json.loads(gateway_config.read_text())["principals"]
        self.assertEqual(
            mapped,
            {alice["id"]: "alice", zen_principal: "zen", pat_principal: "pat"},
        )

        # 7. A client joins with the opencode invite. Its home is separate from the gateway's.
        client = root / "client"
        home = client / "home"
        garden_home = client / "garden-home"
        home.mkdir(parents=True)
        join_env = clean_env(home, GARDEN_HOME=str(garden_home))
        joined = run([str(binary), "join", "-"], join_env, zen_code + "\n")
        seen_outputs.extend([joined.stdout, joined.stderr])
        self.assertEqual(joined.returncode, 0, joined.stderr)
        self.assertIn("joined Garden as zen", joined.stdout)

        garden_json = json.loads((garden_home / "garden.json").read_text())
        self.assertEqual(
            garden_json["garden_endpoint"], f"https://localhost:{gateway_port}/mcp"
        )
        self.assertEqual(garden_json["garden_tls_ca_file"], "tls/internal-ca.pem")
        self.assertEqual(garden_json["instance_id"], published["instance_id"])
        self.assertEqual(garden_json["scope"], published["scope"])
        self.assertEqual(garden_json["classification"], published["classification"])
        self.assertEqual(garden_json["defaults"], {"opencode": "zen"})
        self.assertEqual(
            pem_body((garden_home / "tls" / "internal-ca.pem").read_text()),
            pem_body((tls / "ca.pem").read_text()),
            "the pinned CA is not the gateway's CA",
        )
        zen_token = garden_home / "identities" / "zen.token"
        self.assertEqual(stat.S_IMODE(zen_token.stat().st_mode), 0o600)
        self.assertTrue(zen_token.read_text().startswith("cairn1."))

        # A second redemption of the same code, in an empty home, is refused as used.
        second = client / "second-garden-home"
        replay = run(
            [str(binary), "join", "-"],
            clean_env(client / "second-home", GARDEN_HOME=str(second)),
            zen_code + "\n",
        )
        seen_outputs.extend([replay.stdout, replay.stderr])
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("already been used", replay.stderr)
        self.assertFalse((second / "garden.json").exists())
        self.assertFalse((second / "identities" / "zen.token").exists())

        # 8. The human joins with the cli invite, piped: the sealed key arrives as received, so no
        # passphrase is asked here. Using it is what needs the passphrase, in step 9.
        joined_pat = run([str(binary), "join", "-"], join_env, pat_code + "\n")
        seen_outputs.extend([joined_pat.stdout, joined_pat.stderr])
        self.assertEqual(joined_pat.returncode, 0, joined_pat.stderr)
        self.assertIn("joined Garden as pat", joined_pat.stdout)
        self.assertNotIn("passphrase for", joined_pat.stdout + joined_pat.stderr)
        sealed = garden_home / "identities" / "pat.sealed"
        self.assertTrue(sealed.is_file())
        self.assertEqual(stat.S_IMODE(sealed.stat().st_mode), 0o600)
        self.assertFalse((garden_home / "identities" / "pat.token").exists())
        sealed_record = json.loads(sealed.read_text())
        self.assertEqual(sealed_record["version"], 1)
        self.assertEqual(sealed_record["kdf"], "scrypt")
        self.assertEqual(
            json.loads((garden_home / "garden.json").read_text())["defaults"],
            {"opencode": "zen", "cli": "pat"},
        )

        # 9. say and history through the gateway, as pat, each unlocking the sealed identity.
        status, output = run_pty(
            [str(binary), "say", "--remote", "hello from pat"],
            join_env,
            [(b"passphrase for pat: ", PASSPHRASE)],
        )
        seen_outputs.append(output.decode(errors="replace"))
        self.assertEqual(status, 0, output.decode(errors="replace")[-500:])
        self.assertIn(b"[pat] hello from pat", output)
        self.assertIn(b"stored as", output)

        status, output = run_pty(
            [str(binary), "history", "--remote", "--last", "5"],
            join_env,
            [(b"passphrase for pat: ", PASSPHRASE)],
        )
        seen_outputs.append(output.decode(errors="replace"))
        self.assertEqual(status, 0, output.decode(errors="replace")[-500:])
        lines = [line.strip() for line in output.decode(errors="replace").splitlines()]
        self.assertTrue(
            any(re.search(r"\spat: hello from pat$", line) for line in lines),
            output.decode(errors="replace")[-800:],
        )

        # 10. A harness session cannot select the sealed human identity by name.
        initialize = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e", "version": "0"},
                },
            }
        )
        connect = run(
            [str(binary), "connect", "--harness", "opencode"],
            clean_env(home, GARDEN_HOME=str(garden_home), GARDEN_IDENTITY="pat"),
            initialize + "\n",
        )
        seen_outputs.extend([connect.stdout, connect.stderr])
        self.assertEqual(connect.returncode, 0, connect.stderr)
        self.assertIn(
            "pat is a passphrase-sealed human identity and cannot connect a harness session",
            connect.stdout,
        )
        self.assertNotIn('"pat"', connect.stdout)

        # 11. No output or log carries the admin credential.
        for log in logs.iterdir():
            self.assertNotIn(admin_token, log.read_text(errors="replace"), log.name)
        for text in seen_outputs:
            self.assertNotIn(admin_token, text)


if __name__ == "__main__":
    unittest.main()
