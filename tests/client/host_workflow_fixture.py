"""Disposable locked wheel runtime for zero-provider sandbox acceptance tests."""

import hashlib
import os
import shutil
import subprocess
import sysconfig
import tempfile
import zipfile
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement


@dataclass(frozen=True)
class RuntimeEvidence:
    wheel_sha256: str
    requirements_sha256: str
    distributions: tuple[str, ...]
    file_count: int


def build_cli_runtime(destination: Path) -> RuntimeEvidence:
    """Copy installed locked production dependencies and a freshly built wheel.

    This is test staging, not a clean-machine dependency installation. Neither
    host account files nor provider credentials belong in this runtime.
    """
    previous = os.umask(0o077)
    try:
        destination.mkdir(mode=0o700)
        return _build(destination)
    finally:
        os.umask(previous)


def _build(destination: Path) -> RuntimeEvidence:
    repository = Path(__file__).resolve().parents[2]
    requirements = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--no-header",
            "--no-annotate",
            "--format",
            "requirements.txt",
            "--offline",
            "--no-config",
        ],
        cwd=repository,
        capture_output=True,
        timeout=60,
        check=True,
    ).stdout
    source_root = Path(sysconfig.get_path("purelib")).resolve()
    packages = destination / "site-packages"
    packages.mkdir(mode=0o700)
    installed: list[str] = []
    for line in requirements.decode().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        package = distribution(requirement.name)
        if not requirement.specifier.contains(package.version):
            raise ValueError("installed dependency differs from lock")
        installed.append(f"{requirement.name}=={package.version}")
        for member in package.files or ():
            relative = PurePosixPath(str(member))
            if ".." in relative.parts or relative.is_absolute():
                continue  # Distribution console scripts are not runtime imports.
            if "__pycache__" in relative.parts or relative.suffix in (".pth", ".pyc"):
                continue
            source = Path(str(package.locate_file(member)))
            if not source.resolve().is_relative_to(source_root):
                raise ValueError("dependency escapes interpreter package directory")
            target = packages / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
            target.chmod(0o700 if source.stat().st_mode & 0o111 else 0o600)
    with tempfile.TemporaryDirectory(
        prefix="cairn-runtime-wheel-", dir="/tmp"
    ) as temporary:
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--offline",
                "--no-config",
                "--out-dir",
                temporary,
            ],
            cwd=repository,
            capture_output=True,
            timeout=120,
            check=True,
        )
        wheels = tuple(Path(temporary).glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("expected exactly one Cairn wheel")
        wheel_sha256 = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
        with zipfile.ZipFile(wheels[0]) as wheel:
            for wheel_member in wheel.infolist():
                relative = PurePosixPath(wheel_member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe wheel member")
                if wheel_member.is_dir():
                    continue
                target = packages / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as output:
                    output.write(wheel.read(wheel_member))
                target.chmod(0o600)
    entry = destination / "entry"
    entry.write_text(
        "#!/usr/bin/python3 -I\n"
        "import sys\n"
        "sys.path.insert(0, '/runtime/cli/site-packages')\n"
        "from importlib.metadata import distribution\n"
        "entry = next(item for item in distribution('drystane-cairn').entry_points\n"
        "             if item.group == 'console_scripts' and item.name == 'cairn-memory')\n"
        "entry.load()()\n"
    )
    entry.chmod(0o700)
    return RuntimeEvidence(
        wheel_sha256,
        hashlib.sha256(requirements).hexdigest(),
        tuple(sorted(installed)),
        sum(1 for _ in destination.rglob("*")),
    )
