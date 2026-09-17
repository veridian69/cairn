"""CI-only profile lifecycle; never called by normal repository commands."""

import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

STATE = Path("/run/cairn-ci-bwrap-policy")
PROFILE = STATE / "profile"
OWNER = STATE / "owner"
SECURITY = Path("/sys/kernel/security/apparmor")
NAME = "cairn-ci-bwrap"
PARSER = "/usr/sbin/apparmor_parser"
SOURCE = Path(__file__).with_name("cairn-bwrap.apparmor")
BWRAP = Path("/usr/bin/bwrap")
WPCOM_SOURCE = Path("/etc/apparmor.d/wpcom")
WPCOM_SOURCE_SHA256 = "59b795822094c6721eb95883cb9dfe2cdc2babaa34483a4541dc24113d1c181a"
WPCOM_REMOVE = b"profile wpcom {}\n"
MAX_BINARY = 1024 * 1024
INVENTORY_MAX_PROFILES = 512
INVENTORY_MAX_ENTRIES = 8192
INVENTORY_MAX_DEPTH = 32
INVENTORY_MAX_OUTPUT = 256 * 1024


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, binary=False):
    result = subprocess.run(
        args, check=True, text=not binary, capture_output=True, timeout=10
    ).stdout
    require(len(result) <= MAX_BINARY, "command output exceeds lifecycle limit")
    return result


def identity():
    require(
        os.environ.get("CAIRN_RUNNER_ENVIRONMENT") == "github-hosted",
        "requires GitHub runner.environment=github-hosted",
    )
    require(os.environ.get("GITHUB_ACTIONS") == "true", "requires GitHub Actions")
    require(os.environ.get("ImageOS") == "ubuntu24", "requires ubuntu24 image")
    require("microsoft" not in os.uname().release.lower(), "WSL is forbidden")
    require(os.geteuid() == 0, "CI lifecycle requires root")
    parts = [os.environ.get(key, "") for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")]
    require(
        all(re.fullmatch(r"[0-9]+", part) for part in parts), "missing run identity"
    )
    require(os.environ.get("GITHUB_JOB") == "host", "requires host job")
    return ":".join(parts) + ":host\n"


def possible_attachment(value):
    """Fail closed for opaque or potentially matching AAREs; no partial glob parser."""
    if value == "<unknown>":
        return True
    if value in ("/usr/bin/bwrap", "/bin/bwrap"):
        return True
    prefix = re.split(r"[?*\[{@\\]", value, maxsplit=1)[0]
    return prefix != value and any(
        path.startswith(prefix) for path in ("/usr/bin/bwrap", "/bin/bwrap")
    )


def diagnostic_file(path):
    """At most 4 KiB; refuse symlinks in every component and non-regular files."""
    descriptor = None
    try:
        path = path.absolute()
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for component in path.parts[1:-1]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        os.close(descriptor)
        descriptor = file_descriptor
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return {"status": "unreadable"}
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(4096)
        # Conservatively suppress even exactly-4KiB files: never read a sentinel byte.
        if len(data) == 4096:
            return {"status": "truncated"}
        return {
            "status": "ok",
            "text": data.decode("utf-8"),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    except FileNotFoundError:
        return {"status": "missing"}
    except (OSError, UnicodeError):
        return {"status": "unreadable"}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def diagnostic_command(*args):
    # Only fixed dpkg-query identity requests below, never values from profile names.
    try:
        result = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if len(result.stdout) >= 4096:
            return {"status": "truncated"}
        return {"status": "ok", "text": result.stdout.decode("utf-8")}
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return {"status": "unreadable"}


def filesystem_type(descriptor):
    """glibc Linux x86-64 statfs ABI; reject other ABIs before calling libc."""
    if (
        sys.platform != "linux"
        or os.uname().machine != "x86_64"
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(ctypes.c_long) != 8
    ):
        raise OSError("unsupported statfs ABI")

    class Statfs(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_long),
            ("bsize", ctypes.c_long),
            ("counts", ctypes.c_ulong * 5),
            ("fsid", ctypes.c_int * 2),
            ("namelen", ctypes.c_long),
            ("frsize", ctypes.c_long),
            ("flags", ctypes.c_long),
            ("spare", ctypes.c_long * 4),
        ]

    if ctypes.sizeof(Statfs) != 120:
        raise OSError("unexpected statfs layout")
    function = ctypes.CDLL(None, use_errno=True).fstatfs
    function.argtypes = [ctypes.c_int, ctypes.POINTER(Statfs)]
    function.restype = ctypes.c_int
    result = Statfs()
    if function(descriptor, ctypes.byref(result)) != 0:
        raise OSError(ctypes.get_errno(), "fstatfs failed")
    return result.type


def kernel_diagnostic_file(path):
    """Follow only the fixed securityfs policy jump, then pin apparmorfs traversal."""
    descriptor = None
    try:
        parts = path.relative_to(SECURITY / "policy").parts
        if (
            len(parts) < 3
            or len(parts) % 2 != 1
            or parts[-1] not in ("attach", "name", "mode", "sha256")
            or any(part != "profiles" for part in parts[:-1:2])
            or any(part in (".", "..") for part in parts)
        ):
            return {"status": "unreadable"}
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for component in SECURITY.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        if filesystem_type(descriptor) != 0x73636673:
            return {"status": "unreadable"}
        if not stat.S_ISLNK(
            os.stat("policy", dir_fd=descriptor, follow_symlinks=False).st_mode
        ):
            return {"status": "unreadable"}
        # Kernel magic link: do not resolve/readlink it as a userspace pathname.
        next_descriptor = os.open(
            "policy", os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor
        )
        os.close(descriptor)
        descriptor = next_descriptor
        if filesystem_type(descriptor) != 0x5A3C69F0:
            return {"status": "unreadable"}
        device = os.fstat(descriptor).st_dev
        for index, component in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if os.fstat(descriptor).st_dev != device:
                return {"status": "unreadable"}
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return {"status": "unreadable"}
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(4096)
        if len(data) == 4096:
            return {"status": "truncated"}
        return {
            "status": "ok",
            "text": data.decode("utf-8"),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    except FileNotFoundError:
        return {"status": "missing"}
    except (OSError, UnicodeError, ValueError):
        return {"status": "unreadable"}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def attachment_diagnostic(attachment, rejected):
    record = {"path": str(attachment), "attach": rejected}
    for field in ("name", "mode", "sha256"):
        record[field] = kernel_diagnostic_file(attachment.with_name(field))
    record["sources"] = {
        path: diagnostic_file(Path(path))
        for path in ("/etc/apparmor.d/wpcom", "/etc/apparmor.d/local/wpcom")
    }
    record["package"] = diagnostic_command(
        "/usr/bin/dpkg-query",
        "--show",
        "--showformat=${binary:Package}\t${Version}\n",
        "apparmor",
    )
    record["package_owner"] = diagnostic_command(
        "/usr/bin/dpkg-query", "--search", "/etc/apparmor.d/wpcom"
    )
    print(json.dumps(record, ensure_ascii=True).replace("#", "\\u0023"), flush=True)


def check_attachment(attachment):
    value = kernel_diagnostic_file(attachment)
    if value["status"] != "ok" or possible_attachment(value["text"].strip()):
        attachment_diagnostic(attachment, value)
        raise RuntimeError("existing or ambiguous attachment: " + str(attachment))


def loaded():
    return (SECURITY / "profiles").read_text().splitlines()


def profile_hash(binary):
    """Kernel aa_calc_profile_hash: LE32 version followed by the profile extent.

    Only the single, non-namespaced wpcom output of the pinned parser is accepted.
    No attempt to interpret arbitrary binary policy or waive a hash mismatch.
    """
    require(
        37 < len(binary) <= MAX_BINARY
        and binary[:12] == b"\x04\x08\x00version\x00\x02"
        and 5 <= (int.from_bytes(binary[12:16], "little") & 0x3FF) <= 9
        and binary[16:37] == b"\x04\x08\x00profile\x00\x07\x05\x06\x00wpcom\x00",
        "unexpected compiled wpcom format",
    )
    return hashlib.sha256(binary[12:]).hexdigest()


def attachments():
    result = list((SECURITY / "policy/profiles").rglob("attach"))
    require(result, "loaded attachment inventory unavailable")
    return result


def wpcom_identity(attachment, digest=None):
    require(
        attachment.parent.parent == SECURITY / "policy/profiles"
        and re.fullmatch(r"wpcom\.[0-9]+", attachment.parent.name),
        "unexpected wpcom kernel path",
    )
    for field, expected in (
        ("name", "wpcom"),
        ("mode", "unconfined"),
        ("attach", "<unknown>"),
    ):
        value = kernel_diagnostic_file(attachment.with_name(field))
        require(
            value.get("status") == "ok" and value.get("text", "").strip() == expected,
            "unexpected wpcom " + field,
        )
    value = kernel_diagnostic_file(attachment.with_name("sha256"))
    actual = value.get("text", "").strip()
    require(
        value.get("status") == "ok" and re.fullmatch(r"[0-9a-f]{64}", actual),
        "wpcom kernel hash unavailable",
    )
    require(digest is None or actual == digest, "wpcom compiled/kernel hash mismatch")
    require(
        [line for line in loaded() if "wpcom" in line] == ["wpcom (unconfined)"],
        "unexpected wpcom loaded inventory",
    )
    return actual


def prepare_wpcom(attachment, owner):
    require(identity() == owner, "foreign wpcom run")
    digest = wpcom_identity(attachment)
    require(
        run(
            "/usr/bin/dpkg-query",
            "--show",
            "--showformat=${binary:Package}\t${Version}\n",
            "apparmor",
        )
        == "apparmor\t4.0.1really4.0.1-0ubuntu0.24.04.7\n",
        "unexpected AppArmor package version",
    )
    require(
        run("/usr/bin/dpkg-query", "--search", str(WPCOM_SOURCE))
        == "apparmor: /etc/apparmor.d/wpcom\n",
        "unexpected wpcom package owner",
    )
    source = diagnostic_file(WPCOM_SOURCE)
    require(
        source.get("status") == "ok" and source.get("sha256") == WPCOM_SOURCE_SHA256,
        "unexpected wpcom package source",
    )
    local = diagnostic_file(Path("/etc/apparmor.d/local/wpcom"))
    require(
        local.get("status") == "missing"
        or (
            local.get("status") == "ok"
            and all(
                not line.strip()
                or (
                    line.lstrip().startswith("#")
                    and not re.match(r"\s*#\s*include\b", line)
                )
                for line in local.get("text", "").splitlines()
            )
        ),
        "wpcom local policy is not empty",
    )
    require(
        run(
            PARSER,
            "--config-file=/dev/null",
            "--skip-cache",
            "--names",
            str(WPCOM_SOURCE),
        ).strip()
        == "wpcom",
        "unexpected wpcom profile names",
    )
    binary = run(
        PARSER,
        "--config-file=/dev/null",
        "--skip-cache",
        "--skip-kernel-load",
        "--stdout",
        str(WPCOM_SOURCE),
        binary=True,
    )
    require(profile_hash(binary) == digest, "wpcom compiled/kernel hash mismatch")
    return binary


def restore_wpcom(owner):
    snapshot = STATE / "wpcom.bin"
    if not snapshot.exists():
        return
    require(identity() == owner, "foreign wpcom run")
    binary = snapshot.read_bytes()
    digest = profile_hash(binary)
    require((STATE / "wpcom.sha256").read_text() == digest, "saved wpcom hash differs")
    if not any("wpcom" in line for line in loaded()):
        run(
            PARSER,
            "--config-file=/dev/null",
            "--skip-cache",
            "--binary",
            "--add",
            str(snapshot),
        )
    candidates = [
        path
        for path in attachments()
        if re.fullmatch(r"wpcom\.[0-9]+", path.parent.name)
    ]
    require(len(candidates) == 1, "restored wpcom inventory differs")
    wpcom_identity(candidates[0], digest)


def executable_identity():
    require(
        BWRAP.resolve(strict=True) == BWRAP and not BWRAP.is_symlink(),
        "Bubblewrap must be canonical /usr/bin/bwrap, not a symlink",
    )
    metadata = BWRAP.stat()
    require(
        stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0,
        "Bubblewrap must be a root-owned regular file",
    )
    require(
        metadata.st_mode & 0o022 == 0
        and metadata.st_mode & 0o6000 == 0
        and metadata.st_mode & 0o111 != 0,
        "Bubblewrap must be executable, non-setid and not writable by group/others",
    )
    for parent in BWRAP.parents:
        info = parent.stat()
        require(
            info.st_uid == 0 and info.st_mode & 0o022 == 0,
            "unsafe Bubblewrap parent: " + str(parent),
        )
    require(
        "security.capability" not in os.listxattr(BWRAP),
        "file capabilities on Bubblewrap are forbidden",
    )
    require(
        run("/usr/bin/dpkg-query", "--search", str(BWRAP)).strip()
        == "bubblewrap: /usr/bin/bwrap",
        "unexpected executable package owner",
    )
    require(
        not run("/usr/bin/dpkg", "--verify", "bubblewrap").strip(),
        "installed Bubblewrap differs from package metadata",
    )
    print(
        "Bubblewrap canonical path:",
        BWRAP,
        "sha256:",
        hashlib.sha256(BWRAP.read_bytes()).hexdigest(),
        flush=True,
    )


def rollback_unloaded(created):
    """Only this invocation's fresh directory; never infer ownership from partial files."""
    current = STATE.lstat()
    require(
        created is not None and (current.st_dev, current.st_ino) == created,
        "lifecycle directory identity changed",
    )
    require(
        stat.S_ISDIR(current.st_mode)
        and current.st_uid == os.geteuid()
        and stat.S_IMODE(current.st_mode) == 0o700,
        "unsafe partial state",
    )
    entries = list(STATE.iterdir())
    for entry in entries:
        info = entry.lstat()
        require(
            entry
            in (
                PROFILE,
                OWNER,
                STATE / "wpcom.bin",
                STATE / "wpcom.sha256",
                STATE / "wpcom.remove",
            )
            and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1,
            "unexpected partial-state entry",
        )
    # No load was attempted, so neither the owner marker nor profile need be complete.
    for entry in entries:
        entry.unlink()
    STATE.rmdir()


def owned(owner):
    require(not STATE.is_symlink() and STATE.is_dir(), "invalid lifecycle directory")
    require(
        STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o777 == 0o700,
        "invalid lifecycle ownership/mode",
    )
    require(not OWNER.is_symlink() and OWNER.read_text() == owner, "foreign run state")
    require(not PROFILE.is_symlink() and PROFILE.is_file(), "invalid saved profile")
    require(PROFILE.read_bytes() == SOURCE.read_bytes(), "saved profile differs")
    entries = {entry.name for entry in STATE.iterdir()}
    wpcom_files = {"wpcom.bin", "wpcom.sha256", "wpcom.remove"}
    require(
        entries in ({"profile", "owner"}, {"profile", "owner"} | wpcom_files),
        "unexpected lifecycle entries",
    )
    for entry in STATE.iterdir():
        info = entry.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and info.st_mode & 0o022 == 0,
            "unsafe lifecycle file",
        )
    if entries & wpcom_files:
        require(
            (STATE / "wpcom.remove").read_bytes() == WPCOM_REMOVE,
            "saved wpcom removal differs",
        )
        require(
            (STATE / "wpcom.bin").stat().st_size <= MAX_BINARY,
            "saved wpcom binary exceeds limit",
        )
        require(
            profile_hash((STATE / "wpcom.bin").read_bytes())
            == (STATE / "wpcom.sha256").read_text(),
            "saved wpcom hash differs",
        )


def cleanup(owner):
    if not STATE.exists() and not STATE.is_symlink():
        return
    owned(owner)
    if any(line.startswith(NAME + " (") for line in loaded()):
        run(PARSER, "--config-file=/dev/null", "--skip-cache", "--remove", str(PROFILE))
    require(
        not any(line.startswith(NAME + " (") for line in loaded()),
        "profile remains loaded",
    )
    restore_wpcom(owner)
    for name in ("wpcom.remove", "wpcom.sha256", "wpcom.bin"):
        (STATE / name).unlink(missing_ok=True)
    PROFILE.unlink()
    OWNER.unlink()
    STATE.rmdir()


def install(owner):
    require(not STATE.exists() and not STATE.is_symlink(), "destination already exists")
    executable_identity()
    require(Path("/etc/apparmor.d/abi/4.0").is_file(), "ABI 4.0 unavailable")
    for key, expected in (
        ("kernel.apparmor_restrict_unprivileged_userns", "1"),
        ("kernel.apparmor_restrict_unprivileged_unconfined", "0"),
        ("kernel.unprivileged_userns_clone", "1"),
    ):
        require(
            run("/usr/sbin/sysctl", "-n", key).strip() == expected,
            "unexpected sysctl: " + key,
        )
    require(
        not any(re.search(r"bwrap|bubblewrap", line, re.I) for line in loaded()),
        "existing Bubblewrap profile",
    )
    wpcom = None
    binary = None
    for attachment in attachments():
        if (
            attachment.parent.parent == SECURITY / "policy/profiles"
            and re.fullmatch(r"wpcom\.[0-9]+", attachment.parent.name)
            and kernel_diagnostic_file(attachment).get("text", "").strip()
            == "<unknown>"
        ):
            require(wpcom is None, "duplicate wpcom profile")
            binary = prepare_wpcom(attachment, owner)
            wpcom = attachment
        else:
            check_attachment(attachment)
    require(
        run(
            PARSER, "--config-file=/dev/null", "--skip-cache", "--names", str(SOURCE)
        ).strip()
        == NAME,
        "unexpected profile names",
    )
    run(
        PARSER,
        "--config-file=/dev/null",
        "--skip-cache",
        "--skip-kernel-load",
        str(SOURCE),
    )
    STATE.mkdir(mode=0o700)
    created = None
    load_attempted = False
    try:
        metadata = STATE.lstat()
        created = (metadata.st_dev, metadata.st_ino)
        # All file creation, writing and closing is inside the rollback boundary.
        with PROFILE.open("xb") as target:
            target.write(SOURCE.read_bytes())
        with OWNER.open("x") as target:
            target.write(owner)
        if binary is not None:
            for name, data in (
                ("wpcom.bin", binary),
                ("wpcom.sha256", profile_hash(binary).encode()),
                ("wpcom.remove", WPCOM_REMOVE),
            ):
                with (STATE / name).open("xb") as target:
                    target.write(data)
            # Revalidate the exact loaded target after staging, before any mutation.
            wpcom_identity(wpcom, profile_hash(binary))
            load_attempted = True
            run(
                PARSER,
                "--config-file=/dev/null",
                "--skip-cache",
                "--remove",
                str(STATE / "wpcom.remove"),
            )
            require(
                not any("wpcom" in line for line in loaded()), "wpcom remains loaded"
            )
            for attachment in attachments():
                check_attachment(attachment)
        load_attempted = True
        run(PARSER, "--config-file=/dev/null", "--skip-cache", "--add", str(PROFILE))
        require(
            any(line.startswith(NAME + " (") for line in loaded()),
            "profile was not loaded",
        )
    except Exception:
        if load_attempted:
            cleanup(owner)
        else:
            rollback_unloaded(created)
        raise


def inventory(owner):
    """Read only kernel metadata; never infer attachment safety from this report."""
    require(identity() == owner, "foreign inventory run")
    records = []
    entry_count = 0
    output_size = 128  # Reserve the envelope and final newline.

    def encode(value):
        return json.dumps(value, ensure_ascii=True).replace("#", "\\u0023")

    @contextmanager
    def directory(parent, name, device=None, follow=False):
        flags = os.O_RDONLY | os.O_DIRECTORY
        if not follow:
            flags |= os.O_NOFOLLOW
        fd = os.open(name, flags, dir_fd=parent)
        try:
            if device is not None:
                require(os.fstat(fd).st_dev == device, "inventory device changed")
            yield fd
        finally:
            os.close(fd)

    def entries(fd):
        nonlocal entry_count
        result = set()
        with os.scandir(fd) as iterator:
            for entry in iterator:
                entry_count += 1
                require(entry_count <= INVENTORY_MAX_ENTRIES, "inventory entry limit")
                result.add(entry.name)
        return result

    def field(fd, name, device):
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        try:
            info = os.fstat(descriptor)
            require(
                stat.S_ISREG(info.st_mode) and info.st_dev == device,
                "inventory invalid field",
            )
            data = b""
            while len(data) < 4096:
                chunk = os.read(descriptor, 4096 - len(data))
                if not chunk:
                    break
                data += chunk
            require(len(data) < 4096, "inventory field limit")
            text = data.decode("utf-8").removesuffix("\n")
            require(bool(text), "inventory empty field")
            if name == "sha256":
                require(re.fullmatch(r"[0-9a-f]{64}", text), "inventory invalid hash")
            return text
        finally:
            os.close(descriptor)

    def profiles(parent, path, namespace, device, depth):
        nonlocal output_size
        require(depth <= INVENTORY_MAX_DEPTH, "inventory depth limit")
        with directory(parent, "profiles", device) as container:
            for name in sorted(entries(container)):
                require(
                    len(records) < INVENTORY_MAX_PROFILES, "inventory profile limit"
                )
                with directory(container, name, device) as fd:
                    profile_path = path / "profiles" / name
                    record = {"namespace": namespace, "path": str(profile_path)}
                    for key in ("name", "mode", "attach", "sha256"):
                        record[key] = field(fd, key, device)
                    output_size += len(encode(record)) + 2
                    require(
                        output_size <= INVENTORY_MAX_OUTPUT, "inventory output limit"
                    )
                    records.append(record)
                    if "profiles" in entries(fd):
                        profiles(fd, profile_path, namespace, device, depth + 1)

    def namespaces(fd, path, namespace, device, depth):
        require(depth <= INVENTORY_MAX_DEPTH, "inventory depth limit")
        profiles(fd, path, namespace, device, depth)
        if "namespaces" in entries(fd):
            with directory(fd, "namespaces", device) as container:
                for name in sorted(entries(container)):
                    with directory(container, name, device) as child:
                        namespaces(
                            child,
                            path / "namespaces" / name,
                            namespace + [name],
                            device,
                            depth + 1,
                        )

    try:
        with ExitStack() as stack:
            fd = stack.enter_context(directory(None, "/"))
            for component in SECURITY.parts[1:]:
                fd = stack.enter_context(directory(fd, component))
            require(filesystem_type(fd) == 0x73636673, "inventory requires securityfs")
            require(
                stat.S_ISLNK(
                    os.stat("policy", dir_fd=fd, follow_symlinks=False).st_mode
                ),
                "inventory requires policy jump",
            )
            fd = stack.enter_context(directory(fd, "policy", follow=True))
            require(filesystem_type(fd) == 0x5A3C69F0, "inventory requires apparmorfs")
            namespaces(fd, SECURITY / "policy", [], os.fstat(fd).st_dev, 0)
        require(records, "inventory empty")
        report = encode({"complete": True, "profiles": records})
        require(len(report) + 1 <= INVENTORY_MAX_OUTPUT, "inventory output limit")
    except (OSError, UnicodeError, RuntimeError) as error:
        reason = (
            str(error)
            if isinstance(error, RuntimeError)
            else "unreadable kernel metadata"
        )
        print(
            encode(
                {"complete": False, "error": "inventory incomplete", "reason": reason}
            ),
            flush=True,
        )
        raise RuntimeError("inventory incomplete: " + reason) from None
    print(report, flush=True)


def main():
    require(
        len(sys.argv) == 2 and sys.argv[1] in ("install", "cleanup", "inventory"),
        "expected install, cleanup or inventory",
    )
    owner = identity()
    {"install": install, "cleanup": cleanup, "inventory": inventory}[sys.argv[1]](owner)


if __name__ == "__main__":
    main()
