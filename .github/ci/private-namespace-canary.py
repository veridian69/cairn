"""Finite disposable-CI canary, never a root-policy installer or a sandbox."""

import ctypes
import errno
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import select
import signal
import socket
import stat
import struct
import sys
import time
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "bwrap_policy", Path(__file__).with_name("bwrap-policy.py")
)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
require = policy.require
NAME = "cairn-ci-bwrap"
MAX_BINARY = 1024 * 1024
MAX_PROBE = 16 * 1024
PYTHON = "/usr/bin/python3.12"
AA_EXEC = "/usr/bin/aa-exec"
PACKAGE = "4.0.1really4.0.1-0ubuntu0.24.04.7"
SOURCE_HASH = "38f9db9f955b3f5b4bc1fab6dc8ede9e98c143904c71f7e8d6da9bea01384819"


class CanaryRefusal(RuntimeError):
    pass


class CanaryInterrupted(RuntimeError):
    pass


def diagnostic_require(condition):
    if not condition:
        raise CanaryRefusal()


class Diagnostics:
    """Fixed codes only; first escaping inner failure survives cleanup/unwind."""

    phases = frozenset(
        """identity argv caller_ids reaper
        kernel_security_path kernel_security_fs kernel_policy_link kernel_policy_open kernel_policy_fs
        parent_proc parent_label parent_level parent_stacked parent_ns_stacked parent_ns_name
        cgroup_parent cgroup_fs cgroup_create cgroup_kill_support cgroup_empty
        executable_aa_exec executable_parser executable_python
        package_version package_owner_aa_exec package_owner_parser package_owner_python
        bwrap_identity remaining_work cleanup_children cleanup_private cleanup_cgroup
        cleanup_staging cleanup_descriptors unclassified""".split()
    )

    def __init__(self):
        self.active = "unclassified"
        self.cleaning = False
        self.first = None
        self.cleanup = None

    def capture(self, error, *, creation=False):
        phase = self.active if self.active in self.phases else "unclassified"
        reason = (
            "creation_unverified"
            if creation or isinstance(error, CreationUncertain)
            else "interrupted"
            if isinstance(error, CanaryInterrupted)
            else "guard_refused"
            if isinstance(error, CanaryRefusal)
            else "shared_guard_or_operation_failed"
            if phase in {"identity", "bwrap_identity"}
            else "operation_failed"
        )
        if self.first is None:
            self.first = (phase, reason)
        if self.cleaning and self.cleanup is None:
            self.cleanup = phase

    @contextmanager
    def phase(self, code):
        previous = self.active
        self.active = code if code in self.phases else "unclassified"
        try:
            yield
        except BaseException as error:
            self.capture(error)
            raise
        finally:
            self.active = previous

    def fields(self):
        return {
            "failure_phase": self.first[0] if self.first else None,
            "failure_reason": self.first[1] if self.first else None,
            "cleanup_failure_phase": self.cleanup,
        }


def remaining(deadline):
    value = deadline - time.monotonic()
    require(value > 0, "canary deadline")
    return value


def compiled_hash(binary):
    """Frame exactly one parser v4 stream; opaque DFA blobs are length-delimited.

    Tags are parser_interface.c enum sd_code. This proves framing, not policy
    semantics: trusted verified compiler + fixed source remain required.
    """
    require(16 < len(binary) <= MAX_BINARY, "compiled size")
    require(binary[:12] == b"\x04\x08\0version\0\x02", "compiled header")
    require(5 <= (int.from_bytes(binary[12:16], "little") & 0x3FF) <= 9, "compiled ABI")
    pos = 16

    def take(count):
        nonlocal pos
        require(count <= len(binary) - pos, "compiled extent")
        value = binary[pos : pos + count]
        pos += count
        return value

    def number(count):
        return int.from_bytes(take(count), "little")

    def item(depth=0):
        require(depth <= 32, "compiled nesting")
        name = None
        tag = number(1)
        if tag == 4:
            name = take(number(2))
            require(name.endswith(b"\0") and b"\0" not in name[:-1], "compiled name")
            require(name != b"namespace\0", "compiled namespace forbidden")
            require(depth == 0 or name != b"profile\0", "nested profile forbidden")
            tag = number(1)
        if tag in (0, 1, 2, 3):
            value = take(1 << tag)
        elif tag in (5, 6):
            value = take(number(2 if tag == 5 else 4))
        elif tag in (7, 9, 11):
            if tag == 11:
                number(2)  # Some legacy arrays count groups rather than scalar tags.
            value = []
            end = {7: 8, 9: 10, 11: 12}[tag]
            while pos < len(binary) and binary[pos] != end:
                value.append(item(depth + 1))
            require(number(1) == end, "compiled container end")
        else:
            raise RuntimeError("compiled tag")
        return name, tag, value

    name, tag, contents = item()
    require(
        name == b"profile\0"
        and tag == 7
        and contents
        and contents[0] == (None, 5, NAME.encode() + b"\0")
        and pos == len(binary),
        "compiled single-profile identity",
    )
    return hashlib.sha256(binary[12:]).hexdigest()


def read_gate(fd, deadline):
    remaining(deadline)
    # Set nonblocking before fork; parent admission writes exactly one byte.
    require(os.read(fd, 2) == b"G", "startup gate refused")


class Children:
    """Single waiter; each unreaped PID is owned from fork, including setup errors."""

    def __init__(self, deadline):
        self.deadline = deadline
        self.registry = {}

    def spawn(self, action, fds, *, admit=None, ids=None, keep=(), before_release=None):
        gate_r, gate_w = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGALRM}
        )
        registered = False
        try:
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(gate_w)
                    for target, source in enumerate(fds):
                        os.dup2(source, target, inheritable=True)
                    # Writer only: retain its already-verified namespace .load FD.
                    first = 3
                    for retained in sorted({gate_r, *keep}):
                        require(retained >= 3, "invalid retained descriptor")
                        os.closerange(first, retained)
                        first = retained + 1
                    os.closerange(first, 2147483647)
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                    require(
                        select.select([gate_r], [], [], remaining(self.deadline))[0],
                        "startup timeout",
                    )
                    read_gate(gate_r, self.deadline)
                    os.close(gate_r)
                    if ids is not None:
                        os.setgroups([])
                        os.setresgid(ids[1], ids[1], ids[1])
                        os.setresuid(ids[0], ids[0], ids[0])
                    action()
                    os._exit(0)
                except BaseException:
                    os._exit(125)
            # No fallible admission or pidfd operation precedes registration.
            self.registry[pid] = {"pidfd": None, "gate": gate_w, "admitted": False}
            registered = True
        finally:
            os.close(gate_r)
            if not registered:
                os.close(gate_w)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        record = self.registry[pid]
        record["pidfd"] = os.pidfd_open(pid)
        if admit:
            admit(pid)
            record["admitted"] = True
        remaining(self.deadline)
        if before_release is not None:
            before_release()
        require(os.write(gate_w, b"G") == 1, "startup token failed")
        os.close(gate_w)
        record["gate"] = None
        return pid

    def reap(self, pid):
        found, status = os.waitpid(pid, os.WNOHANG)
        if not found:
            return None
        record = self.registry.pop(pid)
        for key in ("gate", "pidfd"):
            if record[key] is not None:
                os.close(record[key])
        return os.waitstatus_to_exitcode(status)

    def stop(self, deadline):
        # Kill all exact direct children too: a partially admitted child is still ours.
        for pid, record in list(self.registry.items()):
            if record["gate"] is not None:
                os.close(record["gate"])
                record["gate"] = None
            try:
                if record["pidfd"] is not None:
                    signal.pidfd_send_signal(record["pidfd"], signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while self.registry:
            remaining(deadline)
            for pid in list(self.registry):
                self.reap(pid)
            if self.registry:
                select.select([], [], [], min(0.01, remaining(deadline)))


def cleanup_children(children, cgroup, deadline):
    failed = False
    try:
        if cgroup is not None:
            cgroup.write("cgroup.kill", b"1")
    except (Exception, KeyboardInterrupt):
        failed = True
    finally:
        # The namespace/cgroup guard may refuse; never abandon unadmitted children.
        if children is not None:
            children.stop(deadline)
    require(not failed, "owned cgroup shutdown failed")
    if cgroup is not None:
        while populated(cgroup):
            select.select([], [], [], min(0.02, remaining(deadline)))


def receive_packet(channel):
    packet, ancillary, flags, _ = channel.recvmsg(MAX_PROBE + 1, socket.CMSG_SPACE(12))
    # Refuse rights, but first close any descriptors installed by recvmsg.
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            for offset in range(0, len(data) - 3, 4):
                os.close(struct.unpack_from("i", data, offset)[0])
    require(
        packet
        and len(packet) <= MAX_PROBE
        and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC),
        "probe packet limit/EOF",
    )
    require(len(ancillary) == 1, "probe credentials missing/extra")
    level, kind, data = ancillary[0]
    require(
        level == socket.SOL_SOCKET
        and kind == socket.SCM_CREDENTIALS
        and len(data) == 12,
        "probe credentials invalid",
    )

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(packet, object_pairs_hook=unique)
    require(isinstance(value, dict), "probe JSON shape")
    return value, struct.unpack("3i", data)


def read_at(fd, name, limit=4096):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        require(stat.S_ISREG(os.fstat(descriptor).st_mode), "invalid metadata file")
        result = b""
        while len(result) < limit:
            part = os.read(descriptor, limit - len(result))
            if not part:
                break
            result += part
        require(len(result) < limit, "metadata limit")
        return result.decode().removesuffix("\n")
    finally:
        os.close(descriptor)


def directory_at(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if parent is not None:
            require(
                os.fstat(fd).st_dev == os.fstat(parent).st_dev,
                "directory device changed",
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def safe_directory(path):
    """Absolute system directory; reject symlinks or writable/unowned ancestors."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(path).parts[1:]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            require(
                info.st_uid == 0 and not info.st_mode & 0o022, "unsafe system directory"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def names(fd, budget):
    result = []
    with os.scandir(fd) as items:
        for item in items:
            budget[0] += 1
            require(budget[0] <= policy.INVENTORY_MAX_ENTRIES, "namespace entry limit")
            result.append(item.name)
    return sorted(result)


def namespace_tree(root):
    result = []
    budget = [0]

    def walk(fd, path):
        require(len(path) <= policy.INVENTORY_MAX_DEPTH, "namespace depth limit")
        result.append(path)
        if "namespaces" in names(fd, budget):
            container = directory_at(fd, "namespaces")
            try:
                for name in names(container, budget):
                    child = directory_at(container, name)
                    try:
                        walk(child, path + [name])
                    finally:
                        os.close(child)
            finally:
                os.close(container)

    walk(root, [])
    return result


class CreationUncertain(RuntimeError):
    """Creation succeeded but ownership could not be pinned; never adopt/remove it."""


class OwnedDirectory:
    """Never adopt an existing name; remove only the pinned object we created."""

    def __init__(self, parent, name, guard):
        namespace_name(name)
        self.parent, self.name, self.guard = parent, name, guard
        self.fd = None
        guard()
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            self.fd = directory_at(parent, name)
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino)
            require(info.st_uid == os.geteuid(), "owned directory UID")
        except BaseException:
            self.close()
            raise CreationUncertain("owned creation could not be pinned") from None

    def verify(self):
        self.guard()
        require(os.fstat(self.parent).st_nlink > 0, "owned parent was removed")
        info = os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)
        require(
            stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino) == self.identity
            and info.st_uid == os.geteuid()
            and not info.st_mode & 0o022,
            "owned directory replaced",
        )
        require(
            (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino) == self.identity,
            "owned descriptor changed",
        )

    def write(self, name, value):
        self.verify()
        require(name in ("cgroup.procs", "cgroup.kill"), "invalid mutation target")
        fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            require(os.fstat(fd).st_dev == self.identity[0], "write device changed")
            require(os.write(fd, value) == len(value), "short kernel write")
        finally:
            os.close(fd)

    def remove(self):
        self.verify()
        os.rmdir(self.name, dir_fd=self.parent)
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def verify_private_layout(owned):
    """Namespace root entries from Linux apparmorfs __aafs_ns_mkdir_entries."""
    owned.verify()
    directories = {"profiles", "raw_data", "namespaces"}
    expected = directories | {"revision", ".load", ".replace", ".remove"}
    require(set(names(owned.fd, [0])) == expected, "private namespace entries")
    for name in expected:
        info = os.stat(name, dir_fd=owned.fd, follow_symlinks=False)
        kind = stat.S_ISDIR if name in directories else stat.S_ISREG
        require(
            kind(info.st_mode)
            and info.st_dev == owned.identity[0]
            and info.st_uid == os.geteuid()
            and not info.st_mode & 0o022,
            "private namespace entry identity",
        )


class KernelView:
    def __init__(self, owner, diagnostics=None):
        self.owner = owner
        self.diag = diagnostics if diagnostics is not None else Diagnostics()
        with self.diag.phase("kernel_security_path"):
            self.security = safe_directory(policy.SECURITY)
        self.root = None
        try:
            with self.diag.phase("kernel_security_fs"):
                diagnostic_require(policy.filesystem_type(self.security) == 0x73636673)
            with self.diag.phase("kernel_policy_link"):
                diagnostic_require(
                    stat.S_ISLNK(
                        os.stat(
                            "policy", dir_fd=self.security, follow_symlinks=False
                        ).st_mode
                    )
                )
            with self.diag.phase("kernel_policy_open"):
                self.root = os.open(
                    "policy", os.O_RDONLY | os.O_DIRECTORY, dir_fd=self.security
                )
            with self.diag.phase("kernel_policy_fs"):
                diagnostic_require(policy.filesystem_type(self.root) == 0x5A3C69F0)
            self.parent = self.parent_state()
            verify_parent(self.parent, self.diag)
        except BaseException:
            self.close()
            raise

    def parent_state(self):
        with self.diag.phase("parent_proc"):
            proc = safe_directory(f"/proc/{os.getpid()}/attr/apparmor")
        try:
            values = {}
            for key, name, phase in (
                ("label", "current", "parent_label"),
                ("level", ".ns_level", "parent_level"),
                ("stacked", ".stacked", "parent_stacked"),
                ("ns_stacked", ".ns_stacked", "parent_ns_stacked"),
                ("ns_name", ".ns_name", "parent_ns_name"),
            ):
                with self.diag.phase(phase):
                    value = read_at(proc if key == "label" else self.security, name)
                    values[key] = int(value) if key == "level" else value
            return values
        finally:
            os.close(proc)

    def guard(self):
        with self.diag.phase("identity"):
            diagnostic_require(policy.identity() == self.owner)
        current = self.parent_state()
        verify_parent(current, self.diag)
        require(current == self.parent, "coordinator namespace changed")

    def snapshot(self):
        self.guard()
        output = io.StringIO()
        with redirect_stdout(output):
            policy.inventory(self.owner)
        report = json.loads(output.getvalue())
        report["namespaces"] = namespace_tree(self.root)
        return report

    def close(self):
        for fd in (self.root, self.security):
            if fd is not None:
                os.close(fd)


def status_values(text):
    result = {}
    for line in text.splitlines():
        key, value = line.split(":", 1)
        require(key not in result, "duplicate proc field")
        result[key] = value.strip()
    return result


def starttime(text):
    # comm may contain spaces and ')'; field 22 follows the final closing ')'.
    return int(text.rsplit(")", 1)[1].split()[19])


def normal_map(text):
    return " ".join(text.split())


def verify_attestation(packet, host, token, credentials, previous, monitors):
    require(
        set(packet) == {"token", "observation", "negative"}
        and packet["token"] == token,
        "stale/replayed phase",
    )
    observed = packet["observation"]
    keys = {
        "pid",
        "starttime",
        "ids",
        "caps",
        "label",
        "namespaces",
        "uid_map",
        "entry",
    }
    require(
        set(observed) == keys and set(host) == keys | {"host_pid"}, "attestation shape"
    )
    uid, gid = token["uid"], token["gid"]
    require(
        credentials == (host["host_pid"], uid, gid)
        and host["host_pid"] > 0
        and host["host_pid"] != os.getpid()
        and host["host_pid"] not in monitors,
        "wrong payload credentials",
    )
    require(
        host["ids"] == [uid] * 4 + [gid] * 4 and host["caps"] == [0, 0, 0],
        "payload privilege",
    )
    for key in ("pid", "starttime", "ids", "caps", "namespaces", "uid_map"):
        require(observed[key] == host[key], "payload identity mismatch")
    positive = token["phase"] in ("outer", "inner")
    relative = f"{NAME} (unconfined)" if positive else "unconfined"
    require(
        observed["label"] == relative
        and host["label"] == f":{token['namespace']}:{relative}",
        "payload label mismatch",
    )
    require(set(host["namespaces"]) == {"user", "pid", "mnt"}, "namespace proof shape")
    if positive:
        require(
            observed["entry"] is None and host["uid_map"] == f"{uid} {uid} 1",
            "nested UID mapping",
        )
        require(
            previous is not None and host["host_pid"] != previous["host_pid"],
            "monitor substituted for payload",
        )
        require(
            all(
                host["namespaces"][key] != previous["namespaces"][key]
                for key in ("user", "pid", "mnt")
            ),
            "nested namespace unchanged",
        )
    else:
        require(
            observed["entry"]
            == {
                "ns_name": token["namespace"],
                "level": 1,
                "stacked": "no",
                "ns_stacked": "no",
            },
            "private namespace entry failed",
        )
        if previous is not None:
            require(
                host["host_pid"] == previous["host_pid"]
                and host["starttime"] == previous["starttime"]
                and host["namespaces"] == previous["namespaces"],
                "negative payload changed",
            )


class Payload:
    """Hold the actual sender's proc/pidfd/namespace references through its ACK."""

    def __init__(self, pid, cgroup):
        self.fds = []
        self.pid = pid
        self.cgroup = cgroup
        try:
            self.pidfd = os.pidfd_open(pid)
            self.fds.append(self.pidfd)
            root = safe_directory("/proc")
            try:
                self.proc = directory_at(root, str(pid))
                self.fds.append(self.proc)
            finally:
                os.close(root)
            self.namespace_fds = {}
            ns = directory_at(self.proc, "ns")
            try:
                for key in ("user", "pid", "mnt"):
                    fd = os.open(
                        key, os.O_RDONLY, dir_fd=ns
                    )  # Fixed proc namespace magic links.
                    self.fds.append(fd)
                    self.namespace_fds[key] = fd
            finally:
                os.close(ns)
            self.initial = self.read()
        except BaseException:
            self.close()
            raise

    def read(self):
        require(not select.select([self.pidfd], [], [], 0)[0], "payload exited")
        require(
            read_at(self.proc, "cgroup") == f"0::/{self.cgroup.name}",
            "foreign payload cgroup",
        )
        require(
            str(self.pid)
            in read_at(self.cgroup.fd, "cgroup.procs", 65536).splitlines(),
            "payload not in owned cgroup",
        )
        require(
            os.readlink("exe", dir_fd=self.proc) == PYTHON, "monitor/non-Python payload"
        )
        st = status_values(read_at(self.proc, "status", 16384))
        attr = directory_at(self.proc, "attr")
        try:
            aa = directory_at(attr, "apparmor")
            try:
                label = read_at(aa, "current")
            finally:
                os.close(aa)
        finally:
            os.close(attr)
        ns_values = {}
        for key, held in self.namespace_fds.items():
            info = os.stat("ns/" + key, dir_fd=self.proc)
            require(
                (info.st_dev, info.st_ino)
                == (os.fstat(held).st_dev, os.fstat(held).st_ino),
                "payload changed namespace",
            )
            ns_values[key] = [info.st_dev, info.st_ino]
        result = {
            "host_pid": self.pid,
            "pid": int(st["NSpid"].split()[-1]),
            "starttime": starttime(read_at(self.proc, "stat")),
            "ids": [int(n) for key in ("Uid", "Gid") for n in st[key].split()],
            "caps": [int(st[key], 16) for key in ("CapEff", "CapPrm", "CapAmb")],
            "label": label,
            "namespaces": ns_values,
            "uid_map": normal_map(read_at(self.proc, "uid_map")),
            "entry": None,
        }
        require(
            not select.select([self.pidfd], [], [], 0)[0], "payload exited during proof"
        )
        return result

    def recheck(self):
        require(self.read() == self.initial, "payload changed before ACK")

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()


class Runner:
    def __init__(self, children, cgroup):
        self.children, self.cgroup = children, cgroup

    def admit(self, pid):
        before = Path(f"/proc/{pid}/stat").read_text()
        self.cgroup.write("cgroup.procs", str(pid).encode())
        require(
            Path(f"/proc/{pid}/cgroup").read_text() == f"0::/{self.cgroup.name}\n",
            "cgroup admission failed",
        )
        require(
            str(pid) in read_at(self.cgroup.fd, "cgroup.procs", 65536).splitlines(),
            "cgroup admission missing",
        )
        require(
            starttime(before) == starttime(Path(f"/proc/{pid}/stat").read_text()),
            "admission child changed",
        )

    def run(
        self,
        command,
        *,
        limit=MAX_PROBE,
        keep=(),
        before_release=None,
        parent_close=None,
        deadline=None,
    ):
        """Fixed trusted commands/callables, in the owned cgroup from startup."""
        deadline = min(
            self.children.deadline,
            time.monotonic() + 10,
            deadline if deadline is not None else self.children.deadline,
        )
        descriptors = []
        try:
            devnull = os.open("/dev/null", os.O_RDONLY)
            descriptors.append(devnull)
            out_r, out_w = os.pipe2(os.O_CLOEXEC)
            descriptors.extend((out_r, out_w))
            err_r, err_w = os.pipe2(os.O_CLOEXEC)
            descriptors.extend((err_r, err_w))

            def action():
                if callable(command):
                    command()
                    sys.stdout.flush()
                    sys.stderr.flush()
                else:
                    os.execve(command[0], command, {})

            try:
                pid = self.children.spawn(
                    action,
                    (devnull, out_w, err_w),
                    admit=self.admit,
                    keep=keep,
                    before_release=before_release,
                )
            finally:
                if parent_close is not None:
                    parent_close()
            for fd in (out_w, err_w):
                os.close(fd)
                descriptors.remove(fd)
            data = {out_r: bytearray(), err_r: bytearray()}
            active = set(data)
            for fd in active:
                os.set_blocking(fd, False)
            code = None
            while active or code is None:
                for fd in select.select(
                    list(active), [], [], min(0.05, remaining(deadline))
                )[0]:
                    block = os.read(
                        fd,
                        min(
                            65536,
                            (limit if fd == out_r else MAX_PROBE) + 1 - len(data[fd]),
                        ),
                    )
                    if not block:
                        active.remove(fd)
                    data[fd].extend(block)
                    require(
                        len(data[fd]) <= (limit if fd == out_r else MAX_PROBE),
                        "command output limit",
                    )
                if code is None:
                    code = self.children.reap(pid)
            require(code == 0, "fixed command failed")
            return bytes(data[out_r])
        finally:
            for fd in descriptors:
                os.close(fd)

    def probe(self, namespace, uid, gid, scenario, source):
        deadline = min(self.children.deadline, time.monotonic() + 10)
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        err_r, err_w = os.pipe2(os.O_CLOEXEC)
        held = []
        phases = (
            ("entry", "outer", "inner")
            if scenario == "positive"
            else ("entry", "negative")
        )
        evidence, monitors = [], set()
        stderr_size = 0
        output_size = 0
        err_open = True

        def wait_readable():
            nonlocal err_open, stderr_size
            while True:
                ready = select.select(
                    [parent] + ([err_r] if err_open else []),
                    [],
                    [],
                    remaining(deadline),
                )[0]
                if err_r in ready:
                    block = os.read(err_r, MAX_PROBE + 1 - stderr_size)
                    stderr_size += len(block)
                    require(stderr_size <= MAX_PROBE, "probe stderr limit")
                    if not block:
                        err_open = False
                if parent in ready:
                    return

        try:
            command = [
                AA_EXEC,
                "--immediate",
                "--namespace",
                namespace,
                "--profile",
                "unconfined",
                "--",
                PYTHON,
                "-I",
                "-B",
                "-c",
                source,
                source,
                scenario,
                "0",
            ]
            pid = self.children.spawn(
                lambda: os.execve(command[0], command, {}),
                (child.fileno(), child.fileno(), err_w),
                admit=self.admit,
                ids=(uid, gid),
            )
            child.close()
            os.close(err_w)
            err_w = None
            invocation = secrets.token_hex(16)
            previous = None
            for phase in phases:
                token = {
                    "invocation": invocation,
                    "phase": phase,
                    "challenge": secrets.token_hex(32),
                    "namespace": namespace,
                    "uid": uid,
                    "gid": gid,
                }
                parent.settimeout(remaining(deadline))
                require(
                    parent.send(encode(token).encode()) > 0, "challenge send failed"
                )
                wait_readable()
                packet, credentials = receive_packet(parent)
                output_size += len(encode(packet))
                require(output_size <= MAX_PROBE, "probe cumulative output limit")
                payload = Payload(credentials[0], self.cgroup)
                held.append(payload)
                host = payload.initial
                verify_attestation(packet, host, token, credentials, previous, monitors)
                if phase == "negative":
                    negative = packet["negative"]
                    require(
                        isinstance(negative, dict)
                        and set(negative)
                        == {
                            "return",
                            "errno",
                            "userns_before",
                            "userns_after",
                            "label_before",
                            "label_after",
                        },
                        "negative evidence shape",
                    )
                    require(
                        negative["label_before"]
                        == negative["label_after"]
                        == "unconfined"
                        and negative["userns_before"] == previous["namespaces"]["user"]
                        and negative["userns_after"] == host["namespaces"]["user"],
                        "negative self evidence changed",
                    )
                    verify_negative(
                        negative
                        | {
                            "uid": uid,
                            "gid": gid,
                            "cap_eff": host["caps"][0],
                            "cap_prm": host["caps"][1],
                            "cap_amb": host["caps"][2],
                            "label_before": previous["label"],
                            "label_after": host["label"],
                        },
                        namespace,
                        uid,
                        gid,
                    )
                else:
                    require(packet["negative"] is None, "unexpected negative evidence")
                require(
                    not select.select([parent], [], [], 0)[0],
                    "extra/replayed probe packet",
                )
                payload.recheck()
                evidence.append(
                    {"phase": phase, "payload": host, "negative": packet["negative"]}
                )
                parent.sendall(encode({"continue": token}).encode())
                if scenario == "positive":
                    monitors.add(host["host_pid"])
                previous = host
            wait_readable()
            require(parent.recv(1) == b"", "trailing probe output")
            # EOF alone is not exit; drain stderr and reap within the original deadline.
            code = None
            while err_open or code is None:
                if (
                    err_open
                    and select.select([err_r], [], [], min(0.02, remaining(deadline)))[
                        0
                    ]
                ):
                    block = os.read(err_r, MAX_PROBE + 1 - stderr_size)
                    stderr_size += len(block)
                    require(stderr_size <= MAX_PROBE, "probe stderr limit")
                    err_open = bool(block)
                if code is None:
                    code = self.children.reap(pid)
                remaining(deadline)
            require(code == 0, "probe process failed")
            return {
                "scenario": scenario,
                "phases": evidence,
                "stderr_bytes": stderr_size,
            }
        finally:
            for item in held:
                item.close()
            parent.close()
            child.close()
            os.close(err_r)
            if err_w is not None:
                os.close(err_w)


def executable(path):
    """Exact installed executable, no symlink, setid, capability or writable ancestry."""
    path = Path(path)
    parent = safe_directory(path.parent)
    try:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o6022
            and info.st_mode & 0o111
            and "security.capability" not in os.listxattr(path),
            "unsafe executable",
        )
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            require(
                (info.st_dev, info.st_ino)
                == (os.fstat(fd).st_dev, os.fstat(fd).st_ino),
                "executable changed",
            )
            digest = hashlib.sha256()
            total = 0
            while block := os.read(fd, 65536):
                total += len(block)
                require(total <= 32 * 1024 * 1024, "executable limit")
                digest.update(block)
            return digest.hexdigest()
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def parser_owner_line(output):
    diagnostic_require(output == b"apparmor: /sbin/apparmor_parser\n")


class ParserOwnerProof:
    """Only /sbin -> usr/sbin may bind the pinned package path to canonical bytes."""

    def __init__(self, digest, deadline):
        self.fds = []
        self.close_failed = False
        self.acquiring = False
        self.closing = None
        self.digest, self.deadline = digest, deadline

    def acquire(self):
        # Caller owns this state before any operation can acquire a descriptor.
        self.root = self.acquire_fd(lambda: safe_directory("/"))
        self.canonical = self.acquire_fd(lambda: safe_directory("/usr/sbin"))
        self.link = self.link_identity()
        self.alias = self.acquire_fd(
            lambda: os.open("sbin", os.O_RDONLY | os.O_DIRECTORY, dir_fd=self.root)
        )
        self.file = self.acquire_fd(
            lambda: os.open(
                "apparmor_parser", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.canonical
            )
        )
        self.alias_file = self.acquire_fd(
            lambda: os.open(
                "apparmor_parser", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.alias
            )
        )
        self.recheck()

    def acquire_fd(self, opening):
        self.acquiring = True  # Includes open-return, registration and handoff gaps.
        fd = opening()
        self.keep(fd)
        self.acquiring = False
        return fd

    def keep(self, fd):
        self.fds.append(fd)
        return fd

    def release(self, fd):
        self.closing = fd  # Any interruption until cleared is an ambiguous close.
        self.fds.remove(fd)
        os.close(fd)
        self.closing = None

    @contextmanager
    def temporary_fd(self, opening):
        fd = self.acquire_fd(opening)
        try:
            yield fd
        except BaseException:
            try:
                self.release(fd)
            except BaseException:
                self.close_failed = True
            raise  # Keep the first binding failure, not a subsequent close error.
        else:
            self.release(fd)

    @staticmethod
    def identity(info):
        return info.st_dev, info.st_ino

    def link_identity(self):
        info = os.stat("sbin", dir_fd=self.root, follow_symlinks=False)
        diagnostic_require(stat.S_ISLNK(info.st_mode) and info.st_uid == 0)
        diagnostic_require(os.readlink("sbin", dir_fd=self.root) == "usr/sbin")
        return self.identity(info)

    def check_bindings(self):
        remaining(self.deadline)
        for path, held in (("/", self.root), ("/usr/sbin", self.canonical)):
            with self.temporary_fd(lambda path=path: safe_directory(path)) as fresh:
                info = os.fstat(fresh)
                diagnostic_require(
                    stat.S_ISDIR(info.st_mode)
                    and info.st_uid == 0
                    and not info.st_mode & 0o022
                    and self.identity(info) == self.identity(os.fstat(held))
                )
        diagnostic_require(self.link_identity() == self.link)
        with self.temporary_fd(
            lambda: os.open("sbin", os.O_RDONLY | os.O_DIRECTORY, dir_fd=self.root)
        ) as fresh:
            diagnostic_require(
                self.identity(os.fstat(fresh))
                == self.identity(os.fstat(self.alias))
                == self.identity(os.fstat(self.canonical))
            )
        for directory, fd in (
            (self.canonical, self.file),
            (self.alias, self.alias_file),
        ):
            info = os.fstat(fd)
            entry = os.stat("apparmor_parser", dir_fd=directory, follow_symlinks=False)
            diagnostic_require(
                stat.S_ISREG(entry.st_mode)
                and stat.S_ISREG(info.st_mode)
                and self.identity(entry)
                == self.identity(info)
                == self.identity(os.fstat(self.file))
                and info.st_uid == 0
                and not info.st_mode & 0o6022
                and info.st_mode & 0o111
                and "security.capability" not in os.listxattr(fd)
            )

    def recheck(self):
        self.check_bindings()
        digest, offset = hashlib.sha256(), 0
        while True:
            remaining(self.deadline)
            block = os.pread(self.alias_file, 65536, offset)
            if not block:
                break
            offset += len(block)
            diagnostic_require(offset <= 32 * 1024 * 1024)
            digest.update(block)
        diagnostic_require(digest.hexdigest() == self.digest)
        self.check_bindings()

    def close(self):
        if self.acquiring:
            self.close_failed = True

        # Do not retry a number whose close might have completed, even if an
        # interrupt landed before its registry removal. Never sweep unknown FDs.
        def forget_ambiguous():
            if self.closing is not None:
                self.close_failed = True
                if self.closing in self.fds:
                    self.fds.remove(self.closing)
                self.closing = None

        forget_ambiguous()
        for fd in tuple(reversed(self.fds)):
            try:
                self.release(fd)
            except BaseException:
                self.close_failed = True
                forget_ambiguous()
                if fd in self.fds:
                    self.fds.remove(fd)  # Interrupted before release recorded intent.
        diagnostic_require(not self.close_failed)


def populated(cgroup):
    cgroup.verify()
    values = {}
    for line in read_at(cgroup.fd, "cgroup.events").splitlines():
        key, value = line.split()
        require(key not in values, "duplicate cgroup event")
        values[key] = value
    require(values.get("populated") in ("0", "1"), "cgroup state unavailable")
    return values["populated"] == "1"


def save_file(staging, name, data):
    require(name in ("profile", "profile.bin"), "unexpected staged file")
    staging.verify()
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=staging.fd,
    )
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            require(count > 0, "staging write failed")
            view = view[count:]
    finally:
        os.close(fd)


def stage_bytes(staging, name, limit):
    staging.verify()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging.fd)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and not info.st_mode & 0o077,
            "staged file identity",
        )
        value = b""
        while len(value) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(value)))
            if not part:
                break
            value += part
        require(len(value) <= limit, "staged file limit")
        return value
    finally:
        os.close(fd)


def raw_write_once(fd, binary):
    """Called ONLY in the registered writer child, never in the coordinator."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.write.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    libc.write.restype = ctypes.c_ssize_t
    buffer = ctypes.create_string_buffer(binary, len(binary))
    ctypes.set_errno(0)
    count = libc.write(fd, buffer, len(binary))
    return count, ctypes.get_errno()


def check_writer_result(raw, size, deadline):
    remaining(deadline)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate writer field")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    require(
        isinstance(value, dict)
        and set(value) == {"count", "errno"}
        and type(value["count"]) is int
        and type(value["errno"]) is int
        and value["count"] == size
        and value["errno"] == 0,
        "uncertain private write outcome",
    )


def load_private(owned, binary, digest, runner, state):
    """One registered writer, one exact namespace FD, one non-retrying write."""
    state.update(attempted=False, coordinator_closed=True)
    require(
        type(binary) is bytes and compiled_hash(binary) == digest, "load binary proof"
    )
    deadline = min(runner.children.deadline, time.monotonic() + 10)
    verify_private_layout(owned)
    fd = os.open(".load", os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=owned.fd)
    state["coordinator_closed"] = False
    coordinator_fd = fd

    def close_parent():
        nonlocal coordinator_fd
        if coordinator_fd is not None:
            closing, coordinator_fd = coordinator_fd, None
            # Do not retry an uncertain close against a potentially reused number.
            os.close(closing)
            state["coordinator_closed"] = True

    def proof():
        remaining(deadline)
        verify_private_layout(owned)
        info = os.fstat(fd)
        entry = os.stat(".load", dir_fd=owned.fd, follow_symlinks=False)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022
            and (info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino)
            and info.st_dev == owned.identity[0]
            and policy.filesystem_type(fd) == 0x5A3C69F0
            and os.lseek(fd, 0, os.SEEK_CUR) == 0,
            "private load descriptor proof",
        )

    def release():
        proof()
        state["attempted"] = (
            True  # Token delivery may itself have an uncertain outcome.
        )

    def write_child():
        count, error = raw_write_once(fd, binary)
        os.close(fd)  # A close failure prevents any success result and exits 125.
        result = encode({"count": count, "errno": error}).encode()
        require(os.write(1, result) == len(result), "writer result failed")

    try:
        proof()
        raw = runner.run(
            write_child,
            limit=512,
            keep=(fd,),
            before_release=release,
            parent_close=close_parent,
            deadline=deadline,
        )
        require(
            state["coordinator_closed"] and not runner.children.registry,
            "writer not reaped or coordinator FD open",
        )
        check_writer_result(raw, len(binary), deadline)
    finally:
        close_parent()


def finish_private(private, kernel, baseline, digest, children, cgroup, state):
    # This ordering must hold even after an interrupted/uncertain writer outcome.
    require(not children.registry, "writer/children not reaped")
    require(state["coordinator_closed"], "coordinator load FD close unverified")
    require(not populated(cgroup), "owned descendants remain")
    verify_private_layout(private)
    snapshot = kernel.snapshot()
    namespace = private.name
    phase = (
        "loaded"
        if any(r["namespace"] == [namespace] for r in snapshot["profiles"])
        else "empty"
    )
    verify_inventory(baseline, snapshot, namespace, digest, phase=phase)
    actual = os.stat(namespace, dir_fd=private.parent, follow_symlinks=False)
    require_cleanup(
        (kernel.owner, *private.identity),
        (policy.identity(), actual.st_dev, actual.st_ino),
        children_exited=not children.registry and not populated(cgroup),
        private_verified=True,
    )
    private.remove()
    after = kernel.snapshot()
    verify_inventory(baseline, after, namespace, digest, phase="removed")
    return after


def stop_signal(signum, frame):
    raise CanaryInterrupted()


def main():
    report = {"success": False, "cleanup": False, "error": None}
    diag = Diagnostics()
    parser_proof = None
    kernel = cgroup = private = staging = children = None
    cg_parent = ns_parent = run_parent = None
    baseline = digest = None
    signals = {}
    saved = {}
    namespace = ""
    uncertain_creation = False
    # Caller-held intent survives syscall-return and constructor/assignment gaps.
    # Clear only after the pinned object has been assigned to its cleanup owner.
    creation_intent = None
    load_state = {"coordinator_closed": True, "attempted": False}
    try:
        diag.active = "identity"
        owner = policy.identity()  # Must precede any kernel/file access.
        diag.active = "argv"
        diagnostic_require(len(sys.argv) == 1)
        diag.active = "caller_ids"
        raw_ids = [os.environ.get(key, "") for key in ("SUDO_UID", "SUDO_GID")]
        diagnostic_require(
            all(re.fullmatch(r"[0-9]{1,10}", value) for value in raw_ids),
        )
        uid, gid = map(int, raw_ids)
        diagnostic_require(
            1000 <= uid < 2**32 - 1 and 0 < gid < 2**32 - 1,
        )
        diag.active = "reaper"
        diagnostic_require(
            signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,
        )
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            signals[sig] = signal.signal(sig, stop_signal)
        signal.setitimer(signal.ITIMER_REAL, 90)
        children = Children(time.monotonic() + 90)
        kernel = KernelView(owner, diag)
        namespace = "cairncanary" + secrets.token_hex(16)
        diag.active = "cgroup_parent"
        cg_parent = safe_directory("/sys/fs/cgroup")
        diag.active = "cgroup_fs"
        diagnostic_require(policy.filesystem_type(cg_parent) == 0x63677270)
        diag.active = "cgroup_create"
        creation_intent = "cgroup"
        cgroup = OwnedDirectory(cg_parent, namespace, kernel.guard)
        creation_intent = None
        diag.active = "cgroup_kill_support"
        diagnostic_require(
            stat.S_ISREG(
                os.stat("cgroup.kill", dir_fd=cgroup.fd, follow_symlinks=False).st_mode
            ),
        )
        diag.active = "cgroup_empty"
        diagnostic_require(not populated(cgroup))
        runner = Runner(children, cgroup)
        identities = {}
        for path, phase in (
            (AA_EXEC, "executable_aa_exec"),
            (policy.PARSER, "executable_parser"),
            (PYTHON, "executable_python"),
        ):
            diag.active = phase
            identities[path] = executable(path)
        diag.active = "package_version"
        diagnostic_require(
            runner.run(
                [
                    "/usr/bin/dpkg-query",
                    "--show",
                    "--showformat=${Version}\n",
                    "apparmor",
                ]
            ).decode()
            == PACKAGE + "\n",
        )
        for path, package, phase in (
            (AA_EXEC, "apparmor", "package_owner_aa_exec"),
            (policy.PARSER, "apparmor", "package_owner_parser"),
            (PYTHON, "python3.12-minimal", "package_owner_python"),
        ):
            diag.active = phase
            if path == policy.PARSER:
                parser_proof = ParserOwnerProof(identities[path], children.deadline)
                parser_proof.acquire()
                parser_owner_line(
                    runner.run(
                        ["/usr/bin/dpkg-query", "--search", "/sbin/apparmor_parser"]
                    )
                )
                parser_proof.recheck()
            else:
                diagnostic_require(
                    runner.run(["/usr/bin/dpkg-query", "--search", path]).decode()
                    == f"{package}: {path}\n",
                )
        # Verify shared bwrap identity under the same owned lifecycle, without changing its code.
        diag.active = "bwrap_identity"
        report["bwrap_identity"] = runner.run(policy.executable_identity).decode()
        diag.active = "remaining_work"
        for package in ("apparmor", "python3.12-minimal"):
            require(
                not runner.run(["/usr/bin/dpkg", "--verify", package]).strip(),
                "executable package verification",
            )
        with diag.phase("package_owner_parser"):
            parser_proof.recheck()
        for path, value in (
            ("apparmor_restrict_unprivileged_userns", "1"),
            ("apparmor_restrict_unprivileged_unconfined", "0"),
            ("unprivileged_userns_clone", "1"),
        ):
            proc = safe_directory("/proc/sys/kernel")
            try:
                require(read_at(proc, path) == value, "unexpected userns sysctl")
            finally:
                os.close(proc)
        source = policy.diagnostic_file(policy.SOURCE)
        require(
            source.get("status") == "ok" and source.get("sha256") == SOURCE_HASH,
            "fixed policy source changed",
        )
        source_bytes = source["text"].encode()
        probe_path = Path(__file__).with_name("private_namespace_probe.py")
        with probe_path.open("rb") as stream:
            probe_bytes = stream.read(16384)
        require(len(probe_bytes) < 16384, "fixed probe source limit")
        report.update(
            {
                "owner": owner.strip(),
                "namespace": namespace,
                "kernel": os.uname().release,
                "apparmor_package": PACKAGE,
                "executables": identities,
                "source_sha256": SOURCE_HASH,
                "probe_sha256": hashlib.sha256(probe_bytes).hexdigest(),
            }
        )
        run_parent = safe_directory("/run")
        creation_intent = "staging"
        staging = OwnedDirectory(run_parent, namespace, kernel.guard)
        creation_intent = None
        saved["profile"] = source_bytes
        save_file(staging, "profile", source_bytes)
        profile_path = f"/run/{namespace}/profile"
        with diag.phase("package_owner_parser"):
            parser_proof.recheck()
            parser_proof.close()
        binary = runner.run(
            [
                policy.PARSER,
                "--config-file=/dev/null",
                "--skip-cache",
                "--skip-kernel-load",
                "--stdout",
                profile_path,
            ],
            limit=MAX_BINARY,
        )
        digest = compiled_hash(binary)
        saved["profile.bin"] = binary
        save_file(staging, "profile.bin", binary)
        require(
            stage_bytes(staging, "profile", 4096) == source_bytes
            and stage_bytes(staging, "profile.bin", MAX_BINARY) == binary,
            "staged policy changed",
        )
        baseline = kernel.snapshot()
        report["before"] = baseline
        report["before_sha256"] = hashlib.sha256(encode(baseline).encode()).hexdigest()
        ns_parent = directory_at(kernel.root, "namespaces")
        creation_intent = "private"
        private = OwnedDirectory(ns_parent, namespace, kernel.guard)
        creation_intent = None
        verify_private_layout(private)
        verify_inventory(baseline, kernel.snapshot(), namespace, digest, phase="empty")
        kernel.guard()
        private.verify()
        load_private(private, binary, digest, runner, load_state)
        loaded = kernel.snapshot()
        verify_inventory(baseline, loaded, namespace, digest, phase="loaded")
        report["loaded"] = loaded
        report["loaded_sha256"] = hashlib.sha256(encode(loaded).encode()).hexdigest()
        report["compiled_sha256"] = hashlib.sha256(binary).hexdigest()
        report["profile_sha256"] = digest
        report["probes"] = []
        for scenario in ("negative", "positive", "negative"):
            kernel.guard()
            private.verify()
            report["probes"].append(
                runner.probe(namespace, uid, gid, scenario, probe_bytes.decode())
            )
        verify_inventory(baseline, kernel.snapshot(), namespace, digest, phase="loaded")
        report["success"] = True
    except CreationUncertain as error:
        diag.capture(error, creation=True)
        uncertain_creation = True
        report["error"] = "owned creation unverified; retained"
    except (Exception, KeyboardInterrupt) as error:
        diag.capture(error, creation=creation_intent is not None)
        report["error"] = "canary refused or failed"
    finally:
        try:
            diag.cleaning = True
            diag.active = "cleanup_children"
            if creation_intent is not None:
                uncertain_creation = True
                report["error"] = "owned creation unverified; retained"
            if signals:
                signal.setitimer(signal.ITIMER_REAL, 10)
            cleanup_deadline = time.monotonic() + 10
            cleanup_children(
                children,
                cgroup if creation_intent != "cgroup" else None,
                cleanup_deadline,
            )
            if private is not None and creation_intent != "private":
                diag.active = "cleanup_private"
                after = finish_private(
                    private, kernel, baseline, digest, children, cgroup, load_state
                )
                report["after"] = after
                report["after_sha256"] = hashlib.sha256(
                    encode(after).encode()
                ).hexdigest()
                report["root_unchanged"] = True
            if cgroup is not None and creation_intent != "cgroup":
                diag.active = "cleanup_cgroup"
                require(not populated(cgroup), "cleanup cgroup populated")
                cgroup.remove()
            if staging is not None and creation_intent != "staging":
                diag.active = "cleanup_staging"
                staging.verify()
                require(
                    set(names(staging.fd, [0])) <= set(saved), "foreign staging entry"
                )
                for name in names(staging.fd, [0]):
                    require(
                        stage_bytes(staging, name, MAX_BINARY) == saved[name],
                        "staging cleanup identity",
                    )
                    staging.verify()
                    os.unlink(name, dir_fd=staging.fd)
                staging.remove()
            report["cleanup"] = not uncertain_creation
        except (Exception, KeyboardInterrupt) as error:
            diag.capture(error)
            report["success"] = False
            report["error"] = "canary cleanup failed; no foreign-state repair attempted"
        finally:
            diag.active = "cleanup_descriptors"
            try:
                if signals:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                if parser_proof is not None:
                    parser_proof.close()
                for obj in (private, cgroup, staging):
                    if obj is not None:
                        obj.close()
                for fd in (ns_parent, cg_parent, run_parent):
                    if fd is not None:
                        os.close(fd)
                if kernel is not None:
                    kernel.close()
                for sig, previous in signals.items():
                    signal.signal(sig, previous)
            except (Exception, KeyboardInterrupt) as error:
                diag.capture(error)
                report["success"] = report["cleanup"] = False
                report["error"] = (
                    "canary cleanup failed; no foreign-state repair attempted"
                )
    if report["error"] is not None:
        report["success"] = False
    report.update(diag.fields())
    output = encode(report)
    if len(output) + 1 > MAX_BINARY:
        diag.active = "remaining_work"
        diag.cleaning = False
        diag.capture(RuntimeError())
        output = encode(
            {
                "success": False,
                "cleanup": report["cleanup"],
                "error": "evidence limit",
                **diag.fields(),
            }
        )
        report["success"] = False
    print(output, flush=True)
    return 0 if report["success"] and report["cleanup"] else 1


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True).replace("#", "\\u0023")


def namespace_name(value):
    require(
        isinstance(value, str) and re.fullmatch(r"cairncanary[0-9a-f]{32}", value),
        "invalid owned namespace name",
    )


def verify_inventory(baseline, current, namespace, digest, *, phase):
    namespace_name(namespace)
    require(phase in ("empty", "loaded", "removed"), "invalid inventory phase")
    require(re.fullmatch(r"[0-9a-f]{64}", digest), "invalid expected hash")
    for report in (baseline, current):
        require(report.get("complete") is True, "incomplete inventory")
        require(
            set(report) == {"complete", "namespaces", "profiles"}, "inventory shape"
        )
        require(
            len(report["profiles"]) <= policy.INVENTORY_MAX_PROFILES
            and len(report["namespaces"]) <= policy.INVENTORY_MAX_ENTRIES,
            "inventory limit",
        )
        for key in ("namespaces", "profiles"):
            values = [encode(row) for row in report[key]]
            require(len(values) == len(set(values)), "duplicate inventory record")
    require(
        not any(ns[:1] == [namespace] for ns in baseline["namespaces"]),
        "namespace was not fresh",
    )
    private = [r for r in current["profiles"] if r["namespace"][:1] == [namespace]]
    outside = [r for r in current["profiles"] if r not in private]
    require(
        sorted(map(encode, outside)) == sorted(map(encode, baseline["profiles"])),
        "root inventory changed",
    )
    expected_ns = baseline["namespaces"] + ([] if phase == "removed" else [[namespace]])
    require(
        sorted(map(encode, current["namespaces"])) == sorted(map(encode, expected_ns)),
        "namespace inventory changed",
    )
    if phase != "loaded":
        require(not private, "unexpected private profiles")
        return
    require(len(private) == 1, "private profile count")
    row = private[0]
    require(
        row["namespace"] == [namespace]
        and row["name"] == NAME
        and row["mode"] == "unconfined"
        and row["attach"] in ("/usr/bin/bwrap", "<unknown>")
        and row["sha256"] == digest
        and re.fullmatch(
            re.escape(
                str(policy.SECURITY / "policy/namespaces" / namespace / "profiles")
            )
            + r"/cairn-ci-bwrap\.[0-9]+",
            row["path"],
        ),
        "private profile proof failed",
    )


def verify_parent(parent, diagnostics=None):
    diag = diagnostics if diagnostics is not None else Diagnostics()
    with diag.phase("parent_ns_name"):
        diagnostic_require(
            set(parent) == {"label", "level", "stacked", "ns_stacked", "ns_name"}
        )
    for key, phase in (
        ("label", "parent_label"),
        ("level", "parent_level"),
        ("stacked", "parent_stacked"),
        ("ns_stacked", "parent_ns_stacked"),
        ("ns_name", "parent_ns_name"),
    ):
        with diag.phase(phase):
            value = parent[key]
            if key == "level":
                diagnostic_require(type(value) is int and value == 0)
            elif key == "ns_name":
                diagnostic_require(isinstance(value, str) and bool(value))
            else:
                diagnostic_require(value == ("unconfined" if key == "label" else "no"))


def require_cleanup(owner, actual, *, children_exited, private_verified):
    require(owner == actual, "cleanup ownership changed")
    require(children_exited is True, "children not proved exited")
    require(private_verified is True, "private inventory not verified")


def verify_negative(evidence, namespace, uid, gid):
    namespace_name(namespace)
    expected = {
        "uid": uid,
        "gid": gid,
        "cap_eff": 0,
        "cap_prm": 0,
        "cap_amb": 0,
        "label_before": f":{namespace}:unconfined",
        "label_after": f":{namespace}:unconfined",
        "return": -1,
        "errno": errno.EPERM,
    }
    require(uid >= 1000 and gid > 0, "ordinary caller required")
    require(
        set(evidence) == set(expected) | {"userns_before", "userns_after"}
        and all(evidence.get(k) == v for k, v in expected.items())
        and evidence["userns_before"] == evidence["userns_after"],
        "negative userns control failed",
    )


if __name__ == "__main__":
    sys.exit(main())
