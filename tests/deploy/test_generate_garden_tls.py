import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from cairn_install.garden import load_options

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "generate-garden-tls"
JSON_MARKER = "Example garden.json:\n"


def test_no_arguments_prints_usage_and_valid_example_json() -> None:
    assert SCRIPT.exists(), "generate-garden-tls helper is missing"

    result = subprocess.run([str(SCRIPT)], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "Usage: generate-garden-tls DNS_NAME OUTPUT_DIRECTORY" in result.stdout
    assert (
        "scripts/generate-garden-tls reference.example.net /home/cairn/garden-tls"
        in result.stdout
    )
    example = json.loads(result.stdout.split(JSON_MARKER, 1)[1])
    assert example["endpoint"] == "https://reference.example.net:8443/mcp"
    assert example["tls_cert_file"] == "/home/cairn/garden-tls/garden-chain.pem"
    assert example["tls_key_file"] == "/home/cairn/garden-tls/garden-key.pem"
    assert example["tls_ca_file"] == "/home/cairn/garden-tls/internal-ca.pem"
    assert example["participants"] == {"spike": "claude", "val": "codex"}


def test_generates_verified_ca_server_certificate_and_config(tmp_path: Path) -> None:
    assert SCRIPT.exists(), "generate-garden-tls helper is missing"
    output = tmp_path / "tls"

    result = subprocess.run(
        [str(SCRIPT), "garden.example.test", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert re.search(r"^[.+*]+$", result.stderr, re.MULTILINE) is None
    assert set(path.name for path in output.iterdir()) == {
        "garden-cert.pem",
        "garden-chain.pem",
        "garden-key.pem",
        "internal-ca-key.pem",
        "internal-ca.pem",
        "garden.json",
    }
    assert stat.S_IMODE((output / "garden-key.pem").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "internal-ca-key.pem").stat().st_mode) == 0o600

    verify = subprocess.run(
        [
            "openssl",
            "verify",
            "-CAfile",
            str(output / "internal-ca.pem"),
            str(output / "garden-cert.pem"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr

    server = subprocess.check_output(
        ["openssl", "x509", "-in", output / "garden-cert.pem", "-text", "-noout"],
        text=True,
    )
    ca = subprocess.check_output(
        ["openssl", "x509", "-in", output / "internal-ca.pem", "-text", "-noout"],
        text=True,
    )
    assert "DNS:garden.example.test" in server
    assert "TLS Web Server Authentication" in server
    assert "CA:FALSE" in server
    assert "CA:TRUE" in ca
    assert "Certificate Sign" in ca

    generated = json.loads(result.stdout.split("Garden configuration:\n", 1)[1])
    assert generated["endpoint"] == "https://garden.example.test:8443/mcp"
    assert generated["tls_key_file"] == str(output / "garden-key.pem")
    config = output / "garden.json"
    assert json.loads(config.read_text()) == generated
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    options = load_options(config, "docker")
    assert options["scope"] == {
        "realm": "local",
        "segments": [{"kind": "garden", "identifier": "engineering"}],
    }
    assert options["participants"] == {"spike": "claude", "val": "codex"}
    assert options["classification"] == "internal"
    assert options["port"] == 8443
    assert options["expires_at"]


@pytest.mark.parametrize("port", [1024, 65535])
def test_custom_port_is_consistent_in_endpoint_and_config(
    tmp_path: Path, port: int
) -> None:
    output = tmp_path / "tls"

    result = subprocess.run(
        [str(SCRIPT), "garden.example.test", str(output), str(port)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    generated = json.loads((output / "garden.json").read_text())
    assert generated["port"] == port
    assert generated["endpoint"] == f"https://garden.example.test:{port}/mcp"


@pytest.mark.parametrize(
    "port", ["1023", "65536", "0", "18446744073709552640", "eight-four-four-three"]
)
def test_rejects_invalid_ports_before_creating_outputs(
    tmp_path: Path, port: str
) -> None:
    output = tmp_path / "tls"

    result = subprocess.run(
        [str(SCRIPT), "garden.example.test", str(output), port],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "PORT must be an integer from 1024 to 65535" in result.stderr
    assert not output.exists()


def test_key_generation_errors_remain_visible(tmp_path: Path) -> None:
    real_openssl = shutil.which("openssl")
    assert real_openssl is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "openssl"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == genpkey ]]; then echo 'synthetic key generation failure' >&2; exit 41; fi\n"
        'exec "$REAL_OPENSSL" "$@"\n'
    )
    shim.chmod(0o755)
    output = tmp_path / "tls"

    result = subprocess.run(
        [str(SCRIPT), "garden.example.test", str(output)],
        env=os.environ
        | {
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "REAL_OPENSSL": real_openssl,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 41
    assert "synthetic key generation failure" in result.stderr
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    "dns_name",
    [
        "bad..name",
        "bad.-label",
        "bad.label-",
        "10.0.0.1",
        f"{'a' * 64}.example.test",
        f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}",
    ],
)
def test_rejects_invalid_dns_names(tmp_path: Path, dns_name: str) -> None:
    result = subprocess.run(
        [str(SCRIPT), dns_name, str(tmp_path / "tls")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "DNS_NAME is invalid" in result.stderr


@pytest.mark.parametrize(
    "dns_name",
    [
        "localhost",
        "garden.localhost",
        "GARDEN.LOCALHOST",
        "localhost.",
        "garden.localhost.",
        "GARDEN.LOCALHOST.",
    ],
)
def test_rejects_loopback_dns_names_before_creating_outputs(
    tmp_path: Path, dns_name: str
) -> None:
    output = tmp_path / "tls"

    result = subprocess.run(
        [str(SCRIPT), dns_name, str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "public endpoint must not be loopback" in result.stderr
    assert not output.exists()


def test_failed_generation_leaves_no_outputs_and_can_be_retried(tmp_path: Path) -> None:
    real_openssl = shutil.which("openssl")
    assert real_openssl is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "openssl"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == x509 && ${2:-} == -req ]]; then exit 42; fi\n"
        'exec "$REAL_OPENSSL" "$@"\n'
    )
    shim.chmod(0o755)
    output = tmp_path / "tls"
    environment = os.environ | {
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
        "REAL_OPENSSL": real_openssl,
    }

    failed = subprocess.run(
        [str(SCRIPT), "garden.example.test", str(output)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert failed.returncode == 42
    assert list(output.iterdir()) == []

    retried = subprocess.run(
        [str(SCRIPT), "garden.example.test", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert retried.returncode == 0, retried.stderr


def test_existing_configuration_is_not_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "tls"
    output.mkdir()
    config = output / "garden.json"
    config.write_text('{"operator": "configuration"}\n')
    before = config.read_bytes()

    result = subprocess.run(
        [str(SCRIPT), "garden.example.test", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Refusing to overwrite" in result.stderr
    assert config.read_bytes() == before
    assert list(output.iterdir()) == [config]
