"""Fixed synthetic payload; executed only by the disposable canary coordinator.

The coordinator supplies this source via -c so no repository mount is needed.
Never invoke this payload in local checks: its negative control calls unshare.
"""

import ctypes
import json
import os
import socket
import sys


def read(path):
    with open(path, "rb") as stream:
        value = stream.read(16384)
    if len(value) == 16384:
        raise RuntimeError("probe metadata limit")
    return value.decode().removesuffix("\n")


def observe(phase):
    status = dict(line.split(":", 1) for line in read("/proc/self/status").splitlines())
    descriptors = []
    try:
        namespaces = {}
        for key in ("user", "pid", "mnt"):
            fd = os.open("/proc/self/ns/" + key, os.O_RDONLY)
            descriptors.append(fd)
            info = os.fstat(fd)
            namespaces[key] = [info.st_dev, info.st_ino]
        entry = None
        if phase in ("entry", "negative"):
            root = "/sys/kernel/security/apparmor/"
            entry = {
                "ns_name": read(root + ".ns_name"),
                "level": int(read(root + ".ns_level")),
                "stacked": read(root + ".stacked"),
                "ns_stacked": read(root + ".ns_stacked"),
            }
        return {
            "pid": os.getpid(),
            "starttime": int(read("/proc/self/stat").rsplit(")", 1)[1].split()[19]),
            "ids": [int(n) for key in ("Uid", "Gid") for n in status[key].split()],
            "caps": [int(status[key], 16) for key in ("CapEff", "CapPrm", "CapAmb")],
            "label": read("/proc/self/attr/apparmor/current"),
            "namespaces": namespaces,
            "uid_map": " ".join(read("/proc/self/uid_map").split()),
            "entry": entry,
        }
    finally:
        for fd in descriptors:
            os.close(fd)


def packet(channel):
    raw, ancillary, flags, _ = channel.recvmsg(16385, 0)
    if not raw or len(raw) > 16384 or ancillary or flags:
        raise RuntimeError("probe control packet")
    return json.loads(raw)


def exchange(channel, phase, negative=None):
    token = packet(channel)
    if not isinstance(token, dict) or token.get("phase") != phase:
        raise RuntimeError("probe phase mismatch")
    message = {"token": token, "observation": observe(phase), "negative": negative}
    raw = json.dumps(message).encode()
    if len(raw) > 16384 or channel.sendmsg([raw]) != len(raw):
        raise RuntimeError("probe send limit")
    if packet(channel) != {"continue": token}:
        raise RuntimeError("probe acknowledgement mismatch")
    return token


def main():
    if (
        len(sys.argv) != 4
        or sys.argv[2] not in ("positive", "negative")
        or sys.argv[3] not in ("0", "1", "2")
    ):
        raise RuntimeError("fixed probe arguments")
    source, scenario, layer = sys.argv[1], sys.argv[2], int(sys.argv[3])
    channel = socket.socket(fileno=0)
    channel.settimeout(10)
    phase = ("entry", "outer", "inner")[layer]
    exchange(channel, phase)
    if scenario == "negative":
        if layer != 0:
            raise RuntimeError("negative layer")
        before = observe("negative")
        libc = ctypes.CDLL(None, use_errno=True)
        libc.unshare.argtypes = [ctypes.c_int]
        libc.unshare.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = libc.unshare(0x10000000)  # Linux CLONE_NEWUSER.
        error = ctypes.get_errno()
        after = observe("negative")
        exchange(
            channel,
            "negative",
            {
                "return": result,
                "errno": error,
                "userns_before": before["namespaces"]["user"],
                "userns_after": after["namespaces"]["user"],
                "label_before": before["label"],
                "label_after": after["label"],
            },
        )
    elif layer < 2:
        command = [
            "/usr/bin/bwrap",
            "--unshare-all",
            "--share-net",
            "--die-with-parent",
            "--new-session",
            "--as-pid-1",
            "--clearenv",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--chdir",
            "/",
            "--remount-ro",
            "/",
            "/usr/bin/python3.12",
            "-I",
            "-B",
            "-c",
            source,
            source,
            scenario,
            str(layer + 1),
        ]
        channel.detach()  # Keep standard input/output for the next payload.
        os.execve(command[0], command, {})


if __name__ == "__main__":
    main()
