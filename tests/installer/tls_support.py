"""Self-signed test certificates that start ten minutes in the past.

Operator, 25 September 2026: a wall clock stepped back by time synchronisation made
freshly generated certificates "not yet valid". OpenSSL 3.0's req and x509
cannot set a start date; `openssl ca -selfsign` can.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

BACKDATE = timedelta(minutes=10)


def self_signed(cert: Path, key: Path, dns_name: str, days: int) -> None:
    """A self-signed server certificate for `dns_name`, usable as its own CA."""
    work = cert.parent / f".{cert.name}.ca"
    work.mkdir()
    (work / "index.txt").write_text("")
    (work / "serial").write_text("01\n")
    config = work / "ca.cnf"
    config.write_text(
        "[ca]\ndefault_ca = test_ca\n\n"
        f"[test_ca]\ndatabase = {work / 'index.txt'}\nserial = {work / 'serial'}\n"
        f"new_certs_dir = {work}\ndefault_md = sha256\npolicy = test_policy\n"
        "unique_subject = no\n\n"
        "[test_policy]\ncommonName = supplied\n\n"
        "[server]\nbasicConstraints = critical, CA:TRUE\n"
        "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n"
        f"subjectAltName = DNS:{dns_name}\n"
    )
    start = datetime.now(UTC).replace(microsecond=0) - BACKDATE
    end = start + timedelta(days=days)
    quiet = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    subprocess.run(
        ["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(work / "request.csr"),
         "-subj", f"/CN={dns_name}"],
        **quiet,  # type: ignore[call-overload]
    )  # fmt: skip
    subprocess.run(
        ["openssl", "ca", "-batch", "-notext", "-selfsign", "-config", str(config),
         "-keyfile", str(key), "-in", str(work / "request.csr"),
         "-startdate", start.strftime("%Y%m%d%H%M%SZ"),
         "-enddate", end.strftime("%Y%m%d%H%M%SZ"),
         "-extensions", "server", "-out", str(cert)],
        **quiet,  # type: ignore[call-overload]
    )  # fmt: skip
    key.chmod(0o600)
