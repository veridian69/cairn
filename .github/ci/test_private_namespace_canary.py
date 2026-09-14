"""Offline fixtures and ordinary synthetic children; no kernel policy operations."""

import copy
import errno
import hashlib
import importlib.util
import io
import os
import socket
import struct
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

HELPER = Path(__file__).with_name("private-namespace-canary.py")
NS = "cairncanary" + "a" * 32
DIGEST = "b" * 64


def profile(name, namespace=(), attachment="<unknown>", digest="a" * 64):
    root = "/sys/kernel/security/apparmor/policy"
    for component in namespace:
        root += "/namespaces/" + component
    return {
        "namespace": list(namespace),
        "path": root + "/profiles/" + name + ".1",
        "name": name,
        "mode": "unconfined",
        "attach": attachment,
        "sha256": digest,
    }


def snapshots():
    # Deliberately opaque root profiles remain uninterpreted and unchanged.
    baseline = {
        "complete": True,
        "namespaces": [[]],
        "profiles": [profile("opaque1"), profile("opaque2")],
    }
    loaded = copy.deepcopy(baseline)
    loaded["namespaces"].append([NS])
    loaded["profiles"].append(
        profile("cairn-ci-bwrap", (NS,), "/usr/bin/bwrap", DIGEST)
    )
    return baseline, loaded


def compiled_fixture():
    def string(tag, value):
        return bytes([tag]) + struct.pack("<H", len(value) + 1) + value + b"\0"

    return (
        string(4, b"version")
        + b"\x02"
        + struct.pack("<I", 9)
        + string(4, b"profile")
        + b"\x07"
        + string(5, b"cairn-ci-bwrap")
        + b"\x08"
    )


class CanaryContractTests(unittest.TestCase):
    def test_parser_constructor_is_caller_owned_before_any_acquisition(self):
        helper = self.helper()
        with patch.object(
            helper,
            "safe_directory",
            side_effect=AssertionError("acquisition in constructor"),
        ) as opening:
            proof = helper.ParserOwnerProof("0" * 64, time.monotonic() + 1)
            opening.assert_not_called()
            self.assertEqual(proof.fds, [])
            proof.close()

    def test_parser_binding_refusal_survives_temporary_descriptor_close_failure(self):
        with self.parser_fixture() as (helper, root, binary, digest):
            proof = helper.ParserOwnerProof(digest, time.monotonic() + 2)
            proof.acquire()
            wrong = root / "other"
            wrong.mkdir()
            original_close = os.close

            def close_then_error(fd):
                original_close(fd)
                raise OSError(errno.EINTR, "SECRET close")

            with (
                patch.object(
                    helper,
                    "safe_directory",
                    side_effect=lambda path: os.open(
                        wrong, os.O_RDONLY | os.O_DIRECTORY
                    ),
                ),
                patch.object(helper.os, "close", side_effect=close_then_error),
            ):
                with self.assertRaises(helper.CanaryRefusal):
                    proof.recheck()
            with self.assertRaises(RuntimeError):
                proof.close()
            self.assertEqual(proof.fds, [])

    @contextmanager
    def parser_fixture(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            directory = root / "usr/sbin"
            directory.mkdir(parents=True)
            binary = directory / "apparmor_parser"
            binary.write_bytes(b"synthetic parser, never executed")
            binary.chmod(0o755)
            (root / "sbin").symlink_to("usr/sbin")
            original_stat, original_fstat = os.stat, os.fstat

            def root_owned(info):
                values = list(info)
                values[4] = 0
                return os.stat_result(values)

            stack.enter_context(
                patch.object(
                    helper.os,
                    "stat",
                    side_effect=lambda *a, **kw: root_owned(original_stat(*a, **kw)),
                )
            )
            stack.enter_context(
                patch.object(
                    helper.os,
                    "fstat",
                    side_effect=lambda fd: root_owned(original_fstat(fd)),
                )
            )
            stack.enter_context(
                patch.object(
                    helper,
                    "safe_directory",
                    side_effect=lambda path: os.open(
                        root if str(path) == "/" else directory,
                        os.O_RDONLY | os.O_DIRECTORY,
                    ),
                )
            )
            yield helper, root, binary, hashlib.sha256(binary.read_bytes()).hexdigest()

    def test_parser_recorded_owner_exact_only(self):
        helper = self.helper()
        helper.parser_owner_line(b"apparmor: /sbin/apparmor_parser\n")
        for value in (
            b"",
            b"apparmor: /usr/sbin/apparmor_parser\n",
            b"foreign: /sbin/apparmor_parser\n",
            b"apparmor, foreign: /sbin/apparmor_parser\n",
            b"apparmor: /sbin/apparmor_parser\nextra\n",
            b"diversion by apparmor from: /sbin/apparmor_parser\n",
            b"apparmor: /sbin/apparmor_parser\n" * 2,
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                helper.parser_owner_line(value)

    def test_parser_alias_proof_rechecks_swapped_inode_content_and_alias(self):
        for change in (
            None,
            "inode",
            "content",
            "alias",
            "alias_inode",
            "final_symlink",
            "mode",
            "capability",
            "directory",
            "ancestor_mode",
        ):
            with (
                self.subTest(change=change),
                self.parser_fixture() as (helper, root, binary, digest),
            ):
                before = set(os.listdir("/proc/self/fd"))
                proof = helper.ParserOwnerProof(digest, time.monotonic() + 2)
                proof.acquire()
                try:
                    proof.recheck()
                    if change == "inode":
                        data = binary.read_bytes()
                        binary.rename(binary.with_name("old"))
                        binary.write_bytes(data)
                        binary.chmod(0o755)
                    elif change == "content":
                        binary.write_bytes(b"modified")
                    elif change in ("alias", "alias_inode"):
                        (root / "sbin").rename(root / "oldlink")
                        (root / "sbin").symlink_to(
                            "usr/sbin" if change == "alias_inode" else "usr"
                        )
                    elif change == "final_symlink":
                        binary.rename(binary.with_name("old"))
                        binary.symlink_to("old")
                    elif change == "mode":
                        binary.chmod(0o777)
                    elif change == "directory":
                        binary.parent.rename(root / "saved")
                        binary.parent.mkdir()
                    elif change == "ancestor_mode":
                        binary.parent.chmod(0o777)
                    with patch.object(
                        helper.os,
                        "listxattr",
                        return_value=["security.capability"]
                        if change == "capability"
                        else [],
                    ):
                        if change is None:
                            proof.recheck()
                        else:
                            with self.assertRaises((RuntimeError, OSError)):
                                proof.recheck()
                finally:
                    proof.close()
                self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_parser_swap_during_hash_does_not_accept_old_held_bytes(self):
        with self.parser_fixture() as (helper, root, binary, digest):
            proof = helper.ParserOwnerProof(digest, time.monotonic() + 2)
            proof.acquire()
            pread = os.pread
            changed = False

            def swap(fd, size, offset):
                nonlocal changed
                data = pread(fd, size, offset)
                if not changed:
                    changed = True
                    binary.rename(binary.with_name("old"))
                    binary.write_bytes(data)
                    binary.chmod(0o755)
                return data

            try:
                with patch.object(helper.os, "pread", side_effect=swap):
                    with self.assertRaises(RuntimeError):
                        proof.recheck()
            finally:
                proof.close()

    def test_parser_constructor_refuses_other_aliases_and_closes_descriptors(self):
        for target in ("/usr/sbin", "usr", "other", "usr/../usr/sbin"):
            with (
                self.subTest(target=target),
                self.parser_fixture() as (helper, root, binary, digest),
            ):
                (root / "sbin").unlink()
                (root / "sbin").symlink_to(target)
                before = set(os.listdir("/proc/self/fd"))
                proof = helper.ParserOwnerProof(digest, time.monotonic() + 2)
                try:
                    with self.assertRaises(RuntimeError):
                        proof.acquire()
                finally:
                    proof.close()
                self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_parser_refuses_unowned_link_file_and_foreign_device(self):
        for kind in ("link_owner", "file_owner", "device"):
            with (
                self.subTest(kind=kind),
                self.parser_fixture() as (helper, root, binary, digest),
            ):
                proof = helper.ParserOwnerProof(digest, time.monotonic() + 2)
                proof.acquire()
                original_stat, original_fstat = os.stat, os.fstat

                def changed_stat(
                    name, *args, original_stat=original_stat, kind=kind, **kwargs
                ):
                    info = original_stat(name, *args, **kwargs)
                    if kind == "link_owner" and name == "sbin":
                        fields = list(info)
                        fields[4] = 1
                        return os.stat_result(fields)
                    return info

                def changed_fd(
                    fd, original_fstat=original_fstat, kind=kind, proof=proof
                ):
                    info = original_fstat(fd)
                    if fd == proof.alias_file and kind != "link_owner":
                        fields = list(info)
                        fields[2 if kind == "device" else 4] += 1
                        return os.stat_result(fields)
                    return info

                try:
                    with (
                        patch.object(helper.os, "stat", side_effect=changed_stat),
                        patch.object(helper.os, "fstat", side_effect=changed_fd),
                    ):
                        with self.assertRaises(RuntimeError):
                            proof.recheck()
                finally:
                    proof.close()

    def test_diagnostic_nested_first_failure_and_cleanup_are_separate(self):
        helper = self.helper()
        diag = helper.Diagnostics()
        with diag.phase("remaining_work"):
            with diag.phase("parent_label"):
                pass
            self.assertEqual(diag.active, "remaining_work")
        try:
            with diag.phase("remaining_work"):
                with diag.phase("parent_label"):
                    helper.diagnostic_require(False)
        except RuntimeError:
            pass
        diag.cleaning = True
        try:
            with diag.phase("cleanup_private"):
                with diag.phase("parent_level"):
                    raise ValueError("SECRET::error::ignored")
        except ValueError:
            pass
        self.assertEqual(
            diag.fields(),
            {
                "failure_phase": "parent_label",
                "failure_reason": "guard_refused",
                "cleanup_failure_phase": "parent_level",
            },
        )

    def test_diagnostic_cleanup_only_unknown_and_interrupt_are_fixed_codes(self):
        helper = self.helper()
        for code, exception, reason in (
            ("cleanup_children", ValueError("secret"), "operation_failed"),
            ("SECRET", ValueError("secret"), "operation_failed"),
            ("cleanup_children", helper.CanaryInterrupted(), "interrupted"),
        ):
            diag = helper.Diagnostics()
            diag.cleaning = True
            diag.active = code
            diag.capture(exception)
            expected = code if code != "SECRET" else "unclassified"
            self.assertEqual(
                diag.fields(),
                {
                    "failure_phase": expected,
                    "failure_reason": reason,
                    "cleanup_failure_phase": expected,
                },
            )

    def test_main_early_refusals_emit_codes_without_exception_values(self):
        import json
        from unittest.mock import Mock

        helper = self.helper()
        for phase in ("identity", "argv", "caller_ids", "reaper", "cgroup_parent"):
            with self.subTest(phase=phase), ExitStack() as stack:
                identity = stack.enter_context(
                    patch.object(helper.policy, "identity", return_value="fixed\n")
                )
                stack.enter_context(
                    patch.object(
                        helper.sys,
                        "argv",
                        [str(HELPER)] + (["bad"] if phase == "argv" else []),
                    )
                )
                stack.enter_context(
                    patch.dict(
                        helper.os.environ,
                        {
                            "SUDO_UID": "bad" if phase == "caller_ids" else "1000",
                            "SUDO_GID": "1000",
                        },
                    )
                )
                stack.enter_context(
                    patch.object(
                        helper.signal,
                        "getsignal",
                        return_value=helper.signal.SIG_IGN
                        if phase == "reaper"
                        else helper.signal.SIG_DFL,
                    )
                )
                stack.enter_context(patch.object(helper.signal, "signal"))
                stack.enter_context(patch.object(helper.signal, "setitimer"))
                kernel = stack.enter_context(
                    patch.object(helper, "KernelView", return_value=Mock())
                )
                stack.enter_context(
                    patch.object(
                        helper,
                        "safe_directory",
                        side_effect=ValueError("SECRET::error::path"),
                    )
                )
                creation = stack.enter_context(patch.object(helper, "OwnedDirectory"))
                if phase == "identity":
                    identity.side_effect = RuntimeError("SECRET::error::identity")
                output, errors = io.StringIO(), io.StringIO()
                stack.enter_context(patch.object(helper.sys, "stdout", output))
                stack.enter_context(patch.object(helper.sys, "stderr", errors))
                self.assertEqual(helper.main(), 1)
                value = json.loads(output.getvalue())
                self.assertEqual(value["failure_phase"], phase)
                self.assertEqual(
                    value["failure_reason"],
                    "shared_guard_or_operation_failed"
                    if phase == "identity"
                    else "operation_failed"
                    if phase == "cgroup_parent"
                    else "guard_refused",
                )
                self.assertFalse(value["success"])
                self.assertTrue(value["cleanup"])
                self.assertIsNone(value["cleanup_failure_phase"])
                self.assertNotIn("SECRET", output.getvalue() + errors.getvalue())
                self.assertEqual(errors.getvalue(), "")
                creation.assert_not_called()
                if phase != "cgroup_parent":
                    kernel.assert_not_called()

    def test_parent_predicates_report_exact_nested_phase(self):
        helper = self.helper()
        baseline = dict(
            label="unconfined", level=0, stacked="no", ns_stacked="no", ns_name="root"
        )
        for key, bad in (
            ("label", "SECRET"),
            ("level", 1),
            ("stacked", "yes"),
            ("ns_stacked", "yes"),
            ("ns_name", ""),
        ):
            diag = helper.Diagnostics()
            with self.assertRaises(RuntimeError):
                with diag.phase("remaining_work"):
                    helper.verify_parent(baseline | {key: bad}, diag)
            self.assertEqual(diag.fields()["failure_phase"], "parent_" + key)
            self.assertEqual(diag.fields()["failure_reason"], "guard_refused")
            self.assertNotIn("SECRET", helper.encode(diag.fields()))

    def test_kernel_view_failures_report_innermost_phase(self):
        import stat
        from unittest.mock import Mock

        helper = self.helper()
        for phase in (
            "kernel_security_path",
            "kernel_security_fs",
            "kernel_policy_link",
            "kernel_policy_open",
            "kernel_policy_fs",
            "parent_proc",
        ):
            with self.subTest(phase=phase), ExitStack() as stack:
                diag = helper.Diagnostics()
                safe = stack.enter_context(
                    patch.object(helper, "safe_directory", return_value=10)
                )
                fs = stack.enter_context(
                    patch.object(
                        helper.policy,
                        "filesystem_type",
                        side_effect=[0x73636673, 0x5A3C69F0],
                    )
                )
                entry = stack.enter_context(
                    patch.object(
                        helper.os, "stat", return_value=Mock(st_mode=stat.S_IFLNK)
                    )
                )
                opening = stack.enter_context(
                    patch.object(helper.os, "open", return_value=11)
                )
                stack.enter_context(patch.object(helper.os, "close"))
                if phase == "kernel_security_path":
                    safe.side_effect = ValueError("SECRET")
                elif phase == "kernel_security_fs":
                    fs.side_effect = [0]
                elif phase == "kernel_policy_link":
                    entry.return_value.st_mode = stat.S_IFREG
                elif phase == "kernel_policy_open":
                    opening.side_effect = OSError("SECRET")
                elif phase == "kernel_policy_fs":
                    fs.side_effect = [0x73636673, 0]
                else:
                    safe.side_effect = [10, OSError("SECRET")]
                with self.assertRaises((RuntimeError, ValueError, OSError)):
                    helper.KernelView("fixed", diag)
                self.assertEqual(diag.fields()["failure_phase"], phase)
                self.assertNotIn("SECRET", helper.encode(diag.fields()))

    def test_descriptor_cleanup_failure_emits_safe_json_preserving_work_failure(self):
        import json
        from unittest.mock import Mock

        helper = self.helper()
        kernel = Mock()
        kernel.close.side_effect = OSError("SECRET::error::close")
        with (
            patch.object(helper.policy, "identity", return_value="fixed"),
            patch.object(helper.sys, "argv", [str(HELPER)]),
            patch.dict(helper.os.environ, {"SUDO_UID": "1000", "SUDO_GID": "1000"}),
            patch.object(helper.signal, "signal"),
            patch.object(helper.signal, "setitimer"),
            patch.object(helper, "KernelView", return_value=kernel),
            patch.object(
                helper, "safe_directory", side_effect=OSError("SECRET::error::path")
            ),
            patch.object(helper.sys, "stdout", io.StringIO()) as output,
        ):
            self.assertEqual(helper.main(), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["cleanup"])
        self.assertFalse(report["success"])
        self.assertEqual(report["failure_phase"], "cgroup_parent")
        self.assertEqual(report["cleanup_failure_phase"], "cleanup_descriptors")
        self.assertNotIn("SECRET", output.getvalue())

    def test_main_post_cgroup_and_cleanup_only_diagnostics(self):
        for phase in (
            "executable_aa_exec",
            "executable_parser",
            "executable_python",
            "package_version",
            "package_owner_aa_exec",
            "package_owner_parser",
            "package_owner_python",
            "bwrap_identity",
            "cleanup_children",
            "cleanup_private",
            "cleanup_cgroup",
            "cleanup_staging",
            "cleanup_descriptors",
            None,
        ):
            with self.subTest(phase=phase):
                self.check_fake_main_diagnostic(phase)

    def test_parser_post_query_post_verify_and_precompile_failures_stop_load(self):
        for boundary in (1, 2, 3):
            with self.subTest(boundary=boundary):
                self.check_fake_main_diagnostic(None, proof_failure=boundary)
        self.check_fake_main_diagnostic(None, verify_failure=True)

    def test_full_main_parser_acquisition_cleanup_and_handoff_fail_closed(self):
        for failure in ("recheck_close", "open_registration", "registered_handoff"):
            with self.subTest(failure=failure):
                self.check_fake_main_diagnostic(None, proof_lifecycle=failure)

    def check_fake_main_diagnostic(
        self, phase, *, proof_failure=None, verify_failure=False, proof_lifecycle=None
    ):
        import json
        import stat
        from unittest.mock import Mock

        helper = self.helper()
        expected_phase = (
            "package_owner_parser"
            if proof_failure or proof_lifecycle
            else "remaining_work"
            if verify_failure
            else phase
        )
        events = []
        cleanup = phase is not None and phase.startswith("cleanup_")
        objects = [Mock(fd=7), Mock(fd=8), Mock(fd=9)]
        kernel = Mock(root=10)
        kernel.snapshot.return_value = snapshots()[0]
        runner = Mock()
        phase_by_path = {
            "/sbin/apparmor_parser": "parser",
            helper.AA_EXEC: "aa_exec",
            helper.policy.PARSER: "parser",
            helper.PYTHON: "python",
        }

        def executable(path):
            if phase == "executable_" + phase_by_path[path]:
                raise OSError("SECRET::error::executable")
            return "fixed"

        def command(argv, **kwargs):
            events.append(tuple(argv) if not callable(argv) else "bwrap")
            current, value = "remaining_work", b""
            if callable(argv):
                current = "bwrap_identity"
            elif "--show" in argv:
                current, value = "package_version", (helper.PACKAGE + "\n").encode()
            elif "--search" in argv:
                current = "package_owner_" + phase_by_path[argv[-1]]
                package = (
                    "python3.12-minimal" if argv[-1] == helper.PYTHON else "apparmor"
                )
                value = f"{package}: {argv[-1]}\n".encode()
            elif argv[0] == helper.policy.PARSER:
                value = compiled_fixture()
            if verify_failure and argv == ["/usr/bin/dpkg", "--verify", "apparmor"]:
                return b"integrity mismatch"
            if phase == current:
                raise ValueError("SECRET::error::package")
            return value

        runner.run.side_effect = command
        runner.probe.return_value = []
        real_open, real_close = os.open, os.close
        opened, closed, close_calls = [], set(), []
        owned = (
            helper.ParserOwnerProof("fixed", time.monotonic() + 2)
            if proof_lifecycle
            else None
        )

        def close_descriptor(fd):
            if fd not in opened:
                return  # Other full-main descriptors belong to the fake kernel.
            close_calls.append(fd)
            real_close(fd)
            closed.add(fd)
            if proof_lifecycle == "recheck_close" and len(close_calls) == 1:
                raise OSError(errno.EINTR, "SECRET ambiguous close")

        if owned is not None:
            real_keep = owned.keep

            def keep(fd):
                if len(opened) == 3 and proof_lifecycle == "open_registration":
                    raise helper.CanaryInterrupted()
                real_keep(fd)
                if len(opened) == 3 and proof_lifecycle == "registered_handoff":
                    raise helper.CanaryInterrupted()
                return fd

            def opening():
                fd = real_open("/dev/null", os.O_RDONLY)
                opened.append(fd)
                return fd

            def acquire():
                for _ in range(5):
                    owned.acquire_fd(opening)
                helper.diagnostic_require(False)  # Initial binding/recheck refusal.

            owned.keep, owned.acquire = keep, acquire
        with ExitStack() as stack:

            def release_fixture():
                for fd in opened:
                    if fd not in closed:
                        real_close(
                            fd
                        )  # Exact test-created unknown FD, never helper sweep.

            stack.callback(release_fixture)
            factory = stack.enter_context(patch.object(helper, "ParserOwnerProof"))
            if owned is not None:
                factory.return_value = owned
            proof = factory.return_value

            def recheck():
                events.append("proof")
                if proof.recheck.call_count == proof_failure:
                    raise RuntimeError("SECRET swapped proof")

            if owned is None:
                proof.recheck.side_effect = recheck
            for target, name, value in (
                (helper.policy, "identity", "fixed"),
                (helper, "KernelView", kernel),
                (helper, "safe_directory", 6),
                (helper, "directory_at", 6),
                (helper.policy, "filesystem_type", 0x63677270),
                (helper, "populated", False),
                (helper, "Runner", runner),
                (helper, "names", []),
                (helper, "finish_private", snapshots()[0]),
            ):
                stack.enter_context(patch.object(target, name, return_value=value))
            stack.enter_context(
                patch.object(helper, "OwnedDirectory", side_effect=objects)
            )
            stack.enter_context(
                patch.object(helper, "executable", side_effect=executable)
            )
            stack.enter_context(
                patch.object(
                    helper,
                    "read_at",
                    side_effect=lambda fd, name: (
                        "0"
                        if name == "apparmor_restrict_unprivileged_unconfined"
                        else "1"
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    helper.policy,
                    "diagnostic_file",
                    return_value={
                        "status": "ok",
                        "sha256": helper.SOURCE_HASH,
                        "text": "fixed",
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    helper,
                    "stage_bytes",
                    side_effect=lambda obj, name, limit: (
                        b"fixed" if name == "profile" else compiled_fixture()
                    ),
                )
            )
            for name in (
                "save_file",
                "load_private",
                "verify_inventory",
                "verify_private_layout",
                "cleanup_children",
            ):
                stack.enter_context(patch.object(helper, name))
            for name in ("signal", "setitimer"):
                stack.enter_context(patch.object(helper.signal, name))
            stack.enter_context(
                patch.object(
                    helper.os,
                    "close",
                    side_effect=close_descriptor if owned is not None else None,
                )
            )
            stack.enter_context(
                patch.object(helper.os, "stat", return_value=Mock(st_mode=stat.S_IFREG))
            )
            stack.enter_context(patch.object(helper.sys, "argv", [str(HELPER)]))
            stack.enter_context(
                patch.dict(helper.os.environ, {"SUDO_UID": "1000", "SUDO_GID": "1000"})
            )
            if cleanup:
                failing = {
                    "cleanup_children": helper.cleanup_children,
                    "cleanup_private": helper.finish_private,
                    "cleanup_cgroup": objects[0].remove,
                    "cleanup_staging": objects[1].remove,
                    "cleanup_descriptors": kernel.close,
                }[phase]
                failing.side_effect = OSError("SECRET::error::cleanup")
            output, errors = io.StringIO(), io.StringIO()
            stack.enter_context(patch.object(helper.sys, "stdout", output))
            stack.enter_context(patch.object(helper.sys, "stderr", errors))
            code = helper.main()
            value = json.loads(output.getvalue())
            self.assertEqual(code, 0 if expected_phase is None else 1)
            self.assertEqual(value["success"], expected_phase is None)
            self.assertEqual(value["cleanup"], not cleanup and not proof_lifecycle)
            self.assertEqual(value["failure_phase"], expected_phase)
            self.assertEqual(
                value["cleanup_failure_phase"],
                "cleanup_descriptors"
                if proof_lifecycle
                else phase
                if cleanup
                else None,
            )
            self.assertNotIn("SECRET", output.getvalue() + errors.getvalue())
            self.assertEqual(errors.getvalue(), "")
            if expected_phase is not None and not cleanup:
                helper.load_private.assert_not_called()
            if proof_failure or verify_failure:
                self.assertFalse(
                    any(
                        isinstance(e, tuple) and e[0] == helper.policy.PARSER
                        for e in events
                    )
                )
                proof.close.assert_called()
            if owned is not None:
                self.assertEqual(
                    value["failure_reason"],
                    "guard_refused"
                    if proof_lifecycle == "recheck_close"
                    else "interrupted",
                )
                self.assertNotIn(
                    ("/usr/bin/dpkg-query", "--search", "/sbin/apparmor_parser"), events
                )
                self.assertEqual(owned.fds, [])
                self.assertEqual(len(close_calls), len(set(close_calls)))
                self.assertEqual(
                    closed,
                    set(
                        opened[:-1]
                        if proof_lifecycle == "open_registration"
                        else opened
                    ),
                )
                with self.assertRaises(RuntimeError):
                    owned.close()
                self.assertEqual(len(close_calls), len(set(close_calls)))
            if expected_phase is None:
                self.assertIsNone(value["failure_reason"])
                query = ("/usr/bin/dpkg-query", "--search", "/sbin/apparmor_parser")
                self.assertEqual(events.count(query), 1)
                self.assertNotIn(
                    ("/usr/bin/dpkg-query", "--search", helper.policy.PARSER), events
                )
                checks = [i for i, item in enumerate(events) if item == "proof"]
                self.assertEqual(len(checks), 3)
                self.assertLess(events.index(query), checks[0])
                self.assertLess(
                    events.index(("/usr/bin/dpkg", "--verify", "apparmor")), checks[1]
                )
                compilation = next(
                    i
                    for i, item in enumerate(events)
                    if isinstance(item, tuple) and item[0] == helper.policy.PARSER
                )
                self.assertLess(checks[2], compilation)

    def test_direct_entry_refuses_without_traceback_or_kernel_access(self):
        import sys

        result = subprocess.run(
            [sys.executable, "-I", "-B", str(HELPER)],
            env={},
            capture_output=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"")
        import json

        self.assertFalse(json.loads(result.stdout)["success"])

    def test_private_layout_exact_entries_no_symlink_or_foreign_device(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            owned = helper.OwnedDirectory(parent, NS, lambda: None)
            root = Path(tmp) / NS
            try:
                for name in ("profiles", "raw_data", "namespaces"):
                    (root / name).mkdir()
                for name in ("revision", ".load", ".replace", ".remove"):
                    (root / name).touch()
                helper.verify_private_layout(owned)
                (root / "foreign").touch()
                with self.assertRaises(RuntimeError):
                    helper.verify_private_layout(owned)
                (root / "foreign").unlink()
                (root / ".load").unlink()
                (root / ".load").symlink_to(root / ".replace")
                with self.assertRaises(RuntimeError):
                    helper.verify_private_layout(owned)
            finally:
                owned.close()
                os.close(parent)

    def helper(self):
        self.assertTrue(HELPER.is_file(), "private namespace canary not implemented")
        spec = importlib.util.spec_from_file_location("namespace_canary", HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @contextmanager
    def writer_fixture(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            parent = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            owned = helper.OwnedDirectory(parent, NS, lambda: None)
            target = Path(tmp) / NS / ".load"
            target.touch()
            for name in ("profiles", "raw_data", "namespaces"):
                (target.parent / name).mkdir()
            for name in ("revision", ".replace", ".remove"):
                (target.parent / name).touch()
            inode = target.stat().st_ino
            original_stat, original_fstat = os.stat, os.fstat

            def root_target(info):
                if info.st_ino == inode:
                    fields = list(info)
                    fields[4] = 0
                    return os.stat_result(fields)
                return info

            stack.enter_context(
                patch.object(helper.policy, "filesystem_type", return_value=0x5A3C69F0)
            )
            stack.enter_context(
                patch.object(
                    helper.os,
                    "stat",
                    side_effect=lambda *a, **kw: root_target(original_stat(*a, **kw)),
                )
            )
            stack.enter_context(
                patch.object(
                    helper.os,
                    "fstat",
                    side_effect=lambda fd: root_target(original_fstat(fd)),
                )
            )
            stack.enter_context(
                patch.object(
                    helper,
                    "verify_private_layout",
                    side_effect=lambda obj: obj.verify(),
                )
            )
            children = helper.Children(time.monotonic() + 2)
            runner = helper.Runner(children, None)
            stack.enter_context(patch.object(runner, "admit", return_value=None))
            try:
                yield helper, owned, target, children, runner
            finally:
                children.stop(time.monotonic() + 1)
                owned.close()
                os.close(parent)

    def test_exact_private_profile_does_not_waive_opaque_root_profiles(self):
        helper = self.helper()
        baseline, loaded = snapshots()
        self.assertIsNone(
            helper.verify_inventory(baseline, loaded, NS, DIGEST, phase="loaded")
        )

    def test_root_drift_refuses_instead_of_repairing_or_excluding_root(self):
        helper = self.helper()
        for field, value in (
            ("sha256", "c" * 64),
            ("attach", "/usr/bin/bwrap"),
            ("mode", "enforce"),
            ("name", "replacement"),
        ):
            with self.subTest(field=field):
                baseline, loaded = snapshots()
                loaded["profiles"][0][field] = value
                with self.assertRaises(RuntimeError):
                    helper.verify_inventory(
                        baseline, loaded, NS, DIGEST, phase="loaded"
                    )

    def test_owned_opaque_attachment_requires_the_exact_compiled_digest(self):
        helper = self.helper()
        baseline, loaded = snapshots()
        loaded["profiles"][-1]["attach"] = "<unknown>"
        self.assertIsNone(
            helper.verify_inventory(baseline, loaded, NS, DIGEST, phase="loaded")
        )
        loaded["profiles"][-1]["sha256"] = "c" * 64
        with self.assertRaises(RuntimeError):
            helper.verify_inventory(baseline, loaded, NS, DIGEST, phase="loaded")

    def test_private_wrong_attachment_hash_or_mode_refuses(self):
        helper = self.helper()
        for field, value in (
            ("attach", "/bin/bwrap"),
            ("sha256", "c" * 64),
            ("mode", "complain"),
            ("name", "unconfined"),
        ):
            with self.subTest(field=field):
                baseline, loaded = snapshots()
                loaded["profiles"][-1][field] = value
                with self.assertRaises(RuntimeError):
                    helper.verify_inventory(
                        baseline, loaded, NS, DIGEST, phase="loaded"
                    )

    def test_complete_inventory_includes_empty_child_namespaces(self):
        helper = self.helper()
        for child in ([NS, "empty"], ["foreign"]):
            with self.subTest(child=child):
                baseline, loaded = snapshots()
                loaded["namespaces"].append(child)
                with self.assertRaises(RuntimeError):
                    helper.verify_inventory(
                        baseline, loaded, NS, DIGEST, phase="loaded"
                    )

    def test_extra_duplicate_or_missing_private_profile_refuses(self):
        helper = self.helper()
        for change in ("extra", "duplicate", "missing"):
            with self.subTest(change=change):
                baseline, loaded = snapshots()
                if change == "extra":
                    loaded["profiles"].append(profile("other", (NS,)))
                elif change == "duplicate":
                    loaded["profiles"].append(copy.deepcopy(loaded["profiles"][-1]))
                else:
                    loaded["profiles"].pop()
                with self.assertRaises(RuntimeError):
                    helper.verify_inventory(
                        baseline, loaded, NS, DIGEST, phase="loaded"
                    )

    def test_partial_inventory_never_proves_isolation(self):
        helper = self.helper()
        for which in (0, 1):
            with self.subTest(which=which):
                reports = snapshots()
                reports[which]["complete"] = False
                with self.assertRaises(RuntimeError):
                    helper.verify_inventory(*reports, NS, DIGEST, phase="loaded")

    def test_cleanup_requires_full_baseline_restoration(self):
        helper = self.helper()
        baseline, loaded = snapshots()
        self.assertIsNone(
            helper.verify_inventory(baseline, baseline, NS, DIGEST, phase="removed")
        )
        with self.assertRaises(RuntimeError):
            helper.verify_inventory(baseline, loaded, NS, DIGEST, phase="removed")

    def test_parent_must_be_exact_root_unconfined_and_unstacked(self):
        helper = self.helper()
        parent = {
            "label": "unconfined",
            "level": 0,
            "stacked": "no",
            "ns_stacked": "no",
            "ns_name": "root",
        }
        self.assertIsNone(helper.verify_parent(parent))
        for field, value in (
            ("label", "some-profile (unconfined)"),
            ("label", "unconfined//&:foreign:unconfined"),
            ("level", 1),
            ("stacked", "yes"),
            ("ns_stacked", "yes"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(RuntimeError):
                    helper.verify_parent(parent | {field: value})

    def test_cleanup_refuses_live_children_or_changed_ownership(self):
        helper = self.helper()
        owner = ("123:1:host\n", 5, 99)
        self.assertIsNone(
            helper.require_cleanup(
                owner, owner, children_exited=True, private_verified=True
            )
        )
        for actual, exited, verified in (
            (owner, False, True),
            (owner, True, False),
            (("123:2:host\n", 5, 99), True, True),
            (("123:1:host\n", 5, 100), True, True),
        ):
            with self.subTest(actual=actual, exited=exited, verified=verified):
                with self.assertRaises(RuntimeError):
                    helper.require_cleanup(
                        owner, actual, children_exited=exited, private_verified=verified
                    )

    def test_negative_requires_actual_eperm_not_generic_probe_failure(self):
        helper = self.helper()
        evidence = {
            "uid": 1001,
            "gid": 1001,
            "cap_eff": 0,
            "cap_prm": 0,
            "cap_amb": 0,
            "label_before": f":{NS}:unconfined",
            "label_after": f":{NS}:unconfined",
            "userns_before": 123,
            "userns_after": 123,
            "return": -1,
            "errno": errno.EPERM,
        }
        self.assertIsNone(helper.verify_negative(evidence, NS, 1001, 1001))
        for field, value in (
            ("return", 0),
            ("errno", errno.ENOSYS),
            ("errno", errno.EINVAL),
            ("uid", 0),
            ("cap_eff", 1),
            ("cap_prm", 1),
            ("cap_amb", 1),
            ("label_before", f":{NS}:cairn-ci-bwrap (unconfined)"),
            ("label_after", "unconfined"),
            ("userns_after", 124),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(RuntimeError):
                    helper.verify_negative(evidence | {field: value}, NS, 1001, 1001)

    def test_hash_single_profile_extent_and_structural_refusals(self):
        helper = self.helper()

        def string(tag, value):
            return bytes([tag]) + struct.pack("<H", len(value) + 1) + value + b"\0"

        header = string(4, b"version") + b"\x02" + struct.pack("<I", 9)
        body = string(4, b"profile") + b"\x07" + string(5, b"cairn-ci-bwrap")
        # Blob bytes deliberately contain structural-looking tags.
        body += b"\x06\x03\0\0\0\x08\x07\x04\x08"
        binary = header + body
        self.assertEqual(
            helper.compiled_hash(binary), hashlib.sha256(binary[12:]).hexdigest()
        )
        for bad in (
            binary + b"\0",
            binary + body,
            binary[:-1],
            binary[:-2],
            binary.replace(b"cairn-ci-bwrap", b"foreign-bwrap"),
            header + string(4, b"namespace") + string(5, b"foreign") + body,
            binary.replace(b"\x06\x03\0\0\0", b"\x06\xff\xff\xff\xff"),
            b"x" * (helper.MAX_BINARY + 1),
        ):
            with self.subTest(length=len(bad)):
                with self.assertRaises(RuntimeError):
                    helper.compiled_hash(bad)

    def test_startup_gate_eof_short_bad_or_expired_never_execs(self):
        helper = self.helper()
        for value in (b"", b"X", b"GG"):
            with (
                self.subTest(value=value),
                patch.object(helper.os, "read", return_value=value),
            ):
                with self.assertRaises(RuntimeError):
                    helper.read_gate(99, time.monotonic() + 1)
        with self.assertRaises(RuntimeError):
            helper.read_gate(99, time.monotonic() - 1)

    def test_pidfd_and_admission_failures_leave_owned_reapable_child(self):
        helper = self.helper()
        for where in ("pidfd", "admit"):
            with self.subTest(where=where), ExitStack() as stack:
                children = helper.Children(time.monotonic() + 1)
                stack.enter_context(patch.object(helper.os, "fork", return_value=4242))
                stack.enter_context(
                    patch.object(
                        helper.os,
                        "pidfd_open",
                        side_effect=OSError()
                        if where == "pidfd"
                        else lambda pid: os.open("/dev/null", os.O_RDONLY),
                    )
                )
                kill = stack.enter_context(patch.object(helper.os, "kill"))
                stack.enter_context(patch.object(helper.signal, "pidfd_send_signal"))
                stack.enter_context(
                    patch.object(helper.os, "waitpid", return_value=(4242, 9))
                )

                def fail_admission(pid, children=children):
                    self.assertIn(pid, children.registry)
                    raise RuntimeError("injected admission failure")

                with self.assertRaises((OSError, RuntimeError)):
                    children.spawn(lambda: None, (0, 1, 2), admit=fail_admission)
                self.assertIn(4242, children.registry)
                children.stop(time.monotonic() + 1)
                self.assertEqual(children.registry, {})
                if where == "pidfd":
                    kill.assert_called_once_with(4242, helper.signal.SIGKILL)

    def test_socket_credentials_are_per_message_not_creator_identity(self):
        helper = self.helper()
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with parent, child:
            parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            child.sendmsg([b'{"phase":"entry"}'])
            value, creds = helper.receive_packet(parent)
            self.assertEqual(value, {"phase": "entry"})
            self.assertEqual(creds, (os.getpid(), os.getuid(), os.getgid()))
            child.sendmsg([b"x" * (helper.MAX_PROBE + 1)])
            with self.assertRaises(RuntimeError):
                helper.receive_packet(parent)

    def test_namespace_tree_includes_empty_children_and_rejects_symlinks(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "namespaces" / NS / "namespaces" / "empty").mkdir(parents=True)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(helper.namespace_tree(fd), [[], [NS], [NS, "empty"]])
                (root / "namespaces" / "foreign").symlink_to(
                    root, target_is_directory=True
                )
                with self.assertRaises(OSError):
                    helper.namespace_tree(fd)
            finally:
                os.close(fd)

    def test_owned_directory_refuses_replacement_without_deleting_it(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                owned = helper.OwnedDirectory(parent, NS, lambda: None)
                os.rename(Path(tmp) / NS, Path(tmp) / "saved")
                (Path(tmp) / NS).mkdir()
                with self.assertRaises(RuntimeError):
                    owned.remove()
                self.assertTrue((Path(tmp) / NS).is_dir())
                owned.close()
            finally:
                os.close(parent)

    def test_attestation_refuses_monitor_replay_and_identity_mismatches(self):
        helper = self.helper()
        token = {
            "invocation": "one",
            "phase": "outer",
            "challenge": "a" * 64,
            "namespace": NS,
            "uid": 1001,
            "gid": 1001,
        }
        observation = {
            "pid": 1,
            "starttime": 444,
            "ids": [1001] * 8,
            "caps": [0, 0, 0],
            "label": "cairn-ci-bwrap (unconfined)",
            "namespaces": {k: [4, i] for i, k in enumerate(("user", "pid", "mnt"), 10)},
            "uid_map": "1001 1001 1",
            "entry": None,
        }
        packet = {"token": token, "observation": observation, "negative": None}
        host = copy.deepcopy(observation) | {
            "host_pid": 987,
            "label": f":{NS}:cairn-ci-bwrap (unconfined)",
        }
        prior = copy.deepcopy(host)
        prior["host_pid"] = 986
        prior["namespaces"] = {
            k: [4, i] for i, k in enumerate(("user", "pid", "mnt"), 20)
        }
        helper.verify_attestation(packet, host, token, (987, 1001, 1001), prior, set())
        for field, value in (
            ("pid", 2),
            ("starttime", 555),
            ("label", "unconfined"),
            ("caps", [1, 0, 0]),
            ("namespaces", prior["namespaces"]),
        ):
            with self.subTest(field=field):
                with self.assertRaises(RuntimeError):
                    helper.verify_attestation(
                        packet,
                        host | {field: value},
                        token,
                        (987, 1001, 1001),
                        prior,
                        set(),
                    )
        with self.assertRaises(RuntimeError):
            helper.verify_attestation(
                packet, host, token, (987, 1001, 1001), prior, {987}
            )
        with self.assertRaises(RuntimeError):
            helper.verify_attestation(
                packet,
                host,
                token | {"challenge": "b" * 64},
                (987, 1001, 1001),
                prior,
                set(),
            )

    def test_probe_eof_or_wrong_ack_never_advances(self):
        path = HELPER.with_name("private_namespace_probe.py")
        self.assertTrue(path.is_file(), "fixed probe not implemented")
        spec = importlib.util.spec_from_file_location("private_probe", path)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        for wrong in ({"continue": "stale"}, None):
            with self.subTest(wrong=wrong):
                parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                with parent, child, patch.object(probe, "observe", return_value={}):
                    import json

                    token = {
                        "phase": "entry",
                        "invocation": "one",
                        "challenge": "a" * 64,
                    }
                    parent.send(json.dumps(token).encode())
                    if wrong is None:
                        parent.shutdown(socket.SHUT_WR)
                    else:
                        parent.send(json.dumps(wrong).encode())
                    with self.assertRaises(RuntimeError):
                        probe.exchange(child, "entry")

    def test_runner_identity_refuses_before_any_kernel_access(self):
        helper = self.helper()
        with (
            patch.object(
                helper.policy, "identity", side_effect=RuntimeError("runner refusal")
            ),
            patch.object(
                helper, "KernelView", side_effect=AssertionError("kernel reached")
            ),
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(helper.main(), 1)

    def test_ci_split_keeps_policy_offline_and_host_diagnostics_manual(self):
        source = HELPER.parent.parent / "workflows/check.yml"
        text = source.read_text()
        self.assertIn("name: Lightweight policy and wheel checks", text)
        self.assertIn("name: Full repository check (on demand)", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("github.event_name == 'workflow_dispatch'", text)
        self.assertIn(".github/ci/test_private_namespace_canary.py", text)
        self.assertIn(".github/ci/test_bwrap_policy.py", text)
        self.assertIn(".github/ci/test_ci_split.py", text)
        self.assertIn("run: make check", text)
        self.assertNotIn("private-namespace-canary.py", text)
        self.assertNotIn("bwrap-policy.py", text)
        self.assertNotIn("needs:", text)
        manual = source.with_name("host-isolation.yml").read_text()
        self.assertIn("workflow_dispatch:", manual)
        self.assertIn("default: false", manual)
        self.assertIn("-m host_isolation", manual)
        self.assertNotIn("private-namespace-canary.py", manual)
        self.assertNotIn("bwrap-policy.py", manual)
        self.assertNotIn("continue-on-error", manual)

    def test_load_opens_only_owned_namespace_load_descriptor(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            owned = helper.OwnedDirectory(parent, NS, lambda: None)
            try:
                target = Path(tmp) / NS / ".load"
                target.touch()
                binary = compiled_fixture()
                original_stat, original_fstat = os.stat, os.fstat
                target_inode = target.stat().st_ino

                def root_target(info):
                    if info.st_ino == target_inode:
                        fields = list(info)
                        fields[4] = 0
                        return os.stat_result(fields)
                    return info

                children = helper.Children(time.monotonic() + 2)
                runner = helper.Runner(children, None)
                state = {}
                with (
                    patch.object(
                        helper,
                        "verify_private_layout",
                        side_effect=lambda obj: obj.verify(),
                    ),
                    patch.object(
                        helper.policy, "filesystem_type", return_value=0x5A3C69F0
                    ),
                    patch.object(
                        helper.os,
                        "fstat",
                        side_effect=lambda fd: root_target(original_fstat(fd)),
                    ),
                    patch.object(
                        helper.os,
                        "stat",
                        side_effect=lambda *a, **kw: root_target(
                            original_stat(*a, **kw)
                        ),
                    ),
                    patch.object(runner, "admit", return_value=None),
                ):
                    try:
                        helper.load_private(
                            owned, binary, helper.compiled_hash(binary), runner, state
                        )
                    finally:
                        children.stop(time.monotonic() + 1)
                self.assertTrue(state["attempted"])
                self.assertTrue(state["coordinator_closed"])
                self.assertEqual(target.read_bytes(), binary)
                target.unlink()
                foreign = Path(tmp) / "foreign"
                foreign.write_bytes(b"untouched")
                target.symlink_to(foreign)
                with self.assertRaises((OSError, RuntimeError)):
                    helper.load_private(
                        owned, binary, helper.compiled_hash(binary), runner, {}
                    )
                self.assertEqual(foreign.read_bytes(), b"untouched")
            finally:
                owned.close()
                os.close(parent)

    def test_writer_result_full_only_and_never_after_deadline(self):
        helper = self.helper()
        good = b'{"count":123,"errno":0}'
        helper.check_writer_result(good, 123, time.monotonic() + 1)
        for raw in (
            b"",
            b'{"count":0,"errno":0}',
            b'{"count":122,"errno":0}',
            b'{"count":-1,"errno":4}',
            good + good,
            b'{"count":123,"count":123,"errno":0}',
            b'{"count":true,"errno":0}',
        ):
            with self.subTest(raw=raw), self.assertRaises((RuntimeError, ValueError)):
                helper.check_writer_result(raw, 123, time.monotonic() + 1)
        with self.assertRaises(RuntimeError):
            helper.check_writer_result(good, 123, time.monotonic() - 1)

    def test_writer_raw_errors_are_single_attempts_in_child_not_coordinator(self):
        for count, error in ((0, 0), (1, 0), (-1, errno.EINTR), (-1, errno.EPERM)):
            with (
                self.subTest(count=count, error=error),
                self.writer_fixture() as (helper, owned, target, children, runner),
            ):
                coordinator = os.getpid()
                marker = target.parent / "raw-calls"

                def raw(
                    fd,
                    binary,
                    count=count,
                    error=error,
                    coordinator=coordinator,
                    marker=marker,
                ):
                    if os.getpid() == coordinator:
                        raise AssertionError("coordinator executed raw write")
                    with marker.open("ab") as output:
                        output.write(b"one\n")
                    return count, error

                binary = compiled_fixture()
                state = {}
                with patch.object(helper, "raw_write_once", side_effect=raw):
                    with self.assertRaises(RuntimeError):
                        helper.load_private(
                            owned, binary, helper.compiled_hash(binary), runner, state
                        )
                self.assertEqual(marker.read_bytes(), b"one\n")
                self.assertTrue(state["coordinator_closed"])
                self.assertFalse(children.registry)

    def test_blocked_writer_no_result_coordinator_times_out_then_reaps(self):
        with self.writer_fixture() as (helper, owned, target, children, runner):
            children.deadline = time.monotonic() + 0.08
            import select

            def blocked(fd, binary):
                select.select([], [], [], 2)
                return len(binary), 0

            binary, state = compiled_fixture(), {}
            started = time.monotonic()
            with patch.object(helper, "raw_write_once", side_effect=blocked):
                with self.assertRaises(RuntimeError):
                    helper.load_private(
                        owned, binary, helper.compiled_hash(binary), runner, state
                    )
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(state["coordinator_closed"])
            self.assertTrue(children.registry)
            children.stop(time.monotonic() + 1)
            self.assertFalse(children.registry)

    def test_failed_writer_reap_prevents_inventory_even_after_kill(self):
        helper = self.helper()
        from unittest.mock import Mock

        children = helper.Children(time.monotonic() + 1)
        children.registry[4242] = {"pidfd": None, "gate": None, "admitted": True}
        diag = helper.Diagnostics()
        diag.cleaning = True
        with (
            patch.object(helper.os, "kill"),
            patch.object(helper.os, "waitpid", return_value=(0, 0)),
        ):
            with self.assertRaises(RuntimeError), diag.phase("cleanup_children"):
                children.stop(time.monotonic() + 0.01)
        self.assertEqual(diag.fields()["cleanup_failure_phase"], "cleanup_children")
        private, kernel = Mock(), Mock()
        with self.assertRaises(RuntimeError):
            helper.finish_private(
                private,
                kernel,
                {},
                DIGEST,
                children,
                Mock(),
                {"coordinator_closed": True},
            )
        kernel.snapshot.assert_not_called()
        private.remove.assert_not_called()

    def test_load_proof_failures_never_release_writer(self):
        for failure in (
            "binary",
            "filesystem",
            "offset",
            "entry",
            "owner",
            "namespace",
        ):
            with (
                self.subTest(failure=failure),
                self.writer_fixture() as (helper, owned, target, children, runner),
                ExitStack() as stack,
            ):
                binary, state = compiled_fixture(), {}
                digest = helper.compiled_hash(binary)
                raw = stack.enter_context(patch.object(helper, "raw_write_once"))
                if failure == "binary":
                    digest = "0" * 64
                elif failure == "filesystem":
                    stack.enter_context(
                        patch.object(helper.policy, "filesystem_type", return_value=0)
                    )
                elif failure == "offset":
                    stack.enter_context(
                        patch.object(helper.os, "lseek", return_value=1)
                    )
                elif failure in ("entry", "owner"):
                    original = helper.os.stat

                    def altered(name, *args, original=original, failure=failure, **kw):
                        info = original(name, *args, **kw)
                        if name == ".load":
                            fields = list(info)
                            fields[1 if failure == "entry" else 4] += 1
                            return os.stat_result(fields)
                        return info

                    stack.enter_context(
                        patch.object(helper.os, "stat", side_effect=altered)
                    )
                    if failure == "owner":
                        original_fstat = helper.os.fstat
                        inode = target.stat().st_ino

                        def wrong_owner(fd, original_fstat=original_fstat, inode=inode):
                            info = original_fstat(fd)
                            if info.st_ino == inode:
                                fields = list(info)
                                fields[4] = 1
                                return os.stat_result(fields)
                            return info

                        stack.enter_context(
                            patch.object(helper.os, "fstat", side_effect=wrong_owner)
                        )
                else:
                    stack.enter_context(
                        patch.object(
                            owned,
                            "verify",
                            side_effect=RuntimeError("changed namespace"),
                        )
                    )
                with self.assertRaises(RuntimeError):
                    helper.load_private(owned, binary, digest, runner, state)
                raw.assert_not_called()
                self.assertFalse(state["attempted"])
                self.assertTrue(state["coordinator_closed"])
                self.assertFalse(children.registry)

    def test_private_layout_is_checked_before_load_open(self):
        with self.writer_fixture() as (helper, owned, target, children, runner):
            binary = compiled_fixture()
            with (
                patch.object(
                    helper,
                    "verify_private_layout",
                    side_effect=RuntimeError("nonregular target"),
                ),
                patch.object(helper.os, "open") as opening,
            ):
                with self.assertRaises(RuntimeError):
                    helper.load_private(
                        owned, binary, helper.compiled_hash(binary), runner, {}
                    )
                opening.assert_not_called()

    def test_writer_admission_and_release_failure_close_fd_and_reap_without_write(self):
        for phase in ("admit", "release"):
            with (
                self.subTest(phase=phase),
                self.writer_fixture() as (helper, owned, target, children, runner),
                ExitStack() as stack,
            ):
                raw = stack.enter_context(patch.object(helper, "raw_write_once"))
                if phase == "admit":
                    stack.enter_context(
                        patch.object(
                            runner,
                            "admit",
                            side_effect=RuntimeError("admission failed"),
                        )
                    )
                else:
                    stack.enter_context(
                        patch.object(
                            helper,
                            "verify_private_layout",
                            side_effect=[
                                None,
                                None,
                                RuntimeError("namespace changed before release"),
                            ],
                        )
                    )
                state, binary = {}, compiled_fixture()
                with self.assertRaises(RuntimeError):
                    helper.load_private(
                        owned, binary, helper.compiled_hash(binary), runner, state
                    )
                self.assertFalse(state["attempted"])
                self.assertTrue(state["coordinator_closed"])
                self.assertTrue(children.registry)
                children.stop(time.monotonic() + 1)
                self.assertFalse(children.registry)
                raw.assert_not_called()
                self.assertEqual(target.read_bytes(), b"")

    def test_load_coordinator_close_failure_retains_uncertainty_without_retry(self):
        with self.writer_fixture() as (helper, owned, target, children, runner):
            binary, state = compiled_fixture(), {}
            original_close, original_fstat = os.close, os.fstat
            inode, coordinator, calls = target.stat().st_ino, os.getpid(), []

            def close(fd):
                is_load = (
                    os.getpid() == coordinator and original_fstat(fd).st_ino == inode
                )
                original_close(fd)
                if is_load:
                    calls.append(fd)
                    raise OSError(errno.EINTR, "ambiguous close")

            with patch.object(helper.os, "close", side_effect=close):
                with self.assertRaises(OSError):
                    helper.load_private(
                        owned, binary, helper.compiled_hash(binary), runner, state
                    )
            self.assertEqual(len(calls), 1)
            self.assertFalse(state["coordinator_closed"])
            children.stop(time.monotonic() + 1)

    def test_uncertain_load_cleanup_empty_or_exact_owned_only(self):
        from unittest.mock import Mock

        helper = self.helper()
        for variant in ("empty", "loaded", "wronghash", "rootdrift", "unknown", "live"):
            with self.subTest(variant=variant):
                baseline, current = snapshots()
                if variant == "empty":
                    current["profiles"].pop()
                if variant == "wronghash":
                    current["profiles"][-1]["sha256"] = "0" * 64
                if variant == "rootdrift":
                    current["profiles"][0]["name"] = "changed"
                if variant == "unknown":
                    current["profiles"].append(profile("foreign", (NS,)))
                private, kernel, children, group = (Mock() for _ in range(4))
                private.name, private.identity, kernel.owner = NS, (1, 2), "run"
                children.registry = {}
                kernel.snapshot.side_effect = [current, baseline]
                with (
                    patch.object(helper, "verify_private_layout"),
                    patch.object(helper, "populated", return_value=variant == "live"),
                    patch.object(helper.policy, "identity", return_value="run"),
                    patch.object(
                        helper.os, "stat", return_value=Mock(st_dev=1, st_ino=2)
                    ),
                ):
                    if variant in ("empty", "loaded"):
                        self.assertEqual(
                            helper.finish_private(
                                private,
                                kernel,
                                baseline,
                                DIGEST,
                                children,
                                group,
                                {"coordinator_closed": True},
                            ),
                            baseline,
                        )
                        private.remove.assert_called_once()
                    else:
                        with self.assertRaises(RuntimeError):
                            helper.finish_private(
                                private,
                                kernel,
                                baseline,
                                DIGEST,
                                children,
                                group,
                                {"coordinator_closed": True},
                            )
                        private.remove.assert_not_called()

    def test_no_inventory_or_rmdir_before_child_reap_and_load_fd_close(self):
        helper = self.helper()
        from unittest.mock import Mock

        for registry, closed in (({42: {}}, True), ({}, False)):
            private, kernel, children, group = (Mock() for _ in range(4))
            children.registry = registry
            with patch.object(helper, "populated", return_value=False):
                with self.assertRaises(RuntimeError):
                    helper.finish_private(
                        private,
                        kernel,
                        {},
                        DIGEST,
                        children,
                        group,
                        {"coordinator_closed": closed},
                    )
            kernel.snapshot.assert_not_called()
            private.remove.assert_not_called()

    def test_failed_cgroup_kill_still_reaps_unadmitted_children(self):
        helper = self.helper()
        from unittest.mock import Mock

        children = Mock()
        group = Mock()
        group.write.side_effect = RuntimeError("injected kernel failure")
        with self.assertRaises(RuntimeError):
            helper.cleanup_children(children, group, time.monotonic() + 1)
        children.stop.assert_called_once()

    def test_actual_synthetic_command_capture_and_output_overflow(self):
        helper = self.helper()
        import sys

        children = helper.Children(time.monotonic() + 3)
        runner = helper.Runner(children, None)
        with patch.object(runner, "admit", return_value=None):
            try:
                self.assertEqual(
                    runner.run([sys.executable, "-I", "-c", "print('fixture')"]),
                    b"fixture\n",
                )
                self.assertFalse(children.registry)
                with self.assertRaises(RuntimeError):
                    runner.run(
                        [sys.executable, "-I", "-c", "print('X' * 1000)"], limit=16
                    )
            finally:
                children.stop(time.monotonic() + 1)
        self.assertFalse(children.registry)

    def test_fork_failure_closes_startup_pipe_without_creating_registry_entry(self):
        helper = self.helper()
        children = helper.Children(time.monotonic() + 1)
        before = set(os.listdir("/proc/self/fd"))
        with patch.object(
            helper.os, "fork", side_effect=OSError("injected fork failure")
        ):
            with self.assertRaises(OSError):
                children.spawn(lambda: None, (0, 1, 2))
        self.assertEqual(children.registry, {})
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_deadline_signal_masked_until_child_registered(self):
        helper = self.helper()
        children = helper.Children(time.monotonic() + 1)
        with (
            patch.object(helper.signal, "pthread_sigmask", return_value=set()) as mask,
            patch.object(helper.os, "fork", side_effect=OSError()),
        ):
            with self.assertRaises(OSError):
                children.spawn(lambda: None, (0, 1, 2))
        self.assertIn(helper.signal.SIGALRM, mask.call_args_list[0].args[1])

    def test_unexpected_rights_are_closed_on_packet_refusal(self):
        helper = self.helper()
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        fd = os.open("/dev/null", os.O_RDONLY)
        try:
            before = set(os.listdir("/proc/self/fd"))
            child.sendmsg(
                [b"{}"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))]
            )
            with self.assertRaises(RuntimeError):
                helper.receive_packet(parent)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
        finally:
            os.close(fd)
            parent.close()
            child.close()

    def test_uncertain_directory_creation_is_not_claimed_as_clean(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(
                    helper,
                    "directory_at",
                    side_effect=OSError("injected post-mkdir failure"),
                ):
                    with self.assertRaises(helper.CreationUncertain):
                        helper.OwnedDirectory(parent, NS, lambda: None)
                self.assertTrue((Path(tmp) / NS).is_dir())
            finally:
                os.close(parent)

    def test_main_retains_uncertain_creation_in_all_three_roles(self):
        """Real temporary mkdir, fake kernel: exercise syscall and return gaps."""
        import json
        from contextlib import redirect_stdout
        from unittest.mock import Mock

        for role, ordinal in (("cgroup", 1), ("staging", 2), ("private", 3)):
            for window in ("mkdir-return", "constructor-return", "caller-assignment"):
                with (
                    self.subTest(role=role, window=window),
                    tempfile.TemporaryDirectory() as tmp,
                    ExitStack() as stack,
                ):
                    helper = self.helper()
                    root = Path(tmp)
                    (root / "namespaces").mkdir()
                    (root / "cgroup.kill").touch()
                    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                    stack.callback(os.close, parent)
                    original_type, original_mkdir = helper.OwnedDirectory, os.mkdir
                    objects, calls = [], []

                    def mkdir_then_signal(
                        *args, original_mkdir=original_mkdir, **kwargs
                    ):
                        original_mkdir(*args, **kwargs)
                        raise RuntimeError("simulated signal after mkdir")

                    def construct(
                        directory,
                        name,
                        guard,
                        calls=calls,
                        ordinal=ordinal,
                        parent=parent,
                        window=window,
                        helper=helper,
                        original_type=original_type,
                        objects=objects,
                        stack=stack,
                        mkdir_then_signal=mkdir_then_signal,
                    ):
                        calls.append(directory)
                        if len(calls) != ordinal:
                            obj = Mock(fd=os.dup(parent))
                            obj.close.side_effect = lambda: None
                            stack.callback(os.close, obj.fd)
                            return obj
                        if window == "mkdir-return":
                            with patch.object(
                                helper.os, "mkdir", side_effect=mkdir_then_signal
                            ):
                                return original_type(directory, name, guard)
                        obj = original_type(directory, name, guard)
                        objects.append(obj)
                        if window == "caller-assignment":
                            return obj
                        # Constructor returned a pinned object, caller has not assigned it.
                        raise RuntimeError("simulated signal at ownership handoff")

                    def command(argv, helper=helper, **kwargs):
                        if callable(argv):
                            return b""
                        if "--show" in argv:
                            return (helper.PACKAGE + "\n").encode()
                        if "--search" in argv:
                            package = (
                                "python3.12-minimal"
                                if argv[-1] == helper.PYTHON
                                else "apparmor"
                            )
                            return f"{package}: {argv[-1]}\n".encode()
                        return (
                            compiled_fixture()
                            if argv[0] == helper.policy.PARSER
                            else b""
                        )

                    kernel = Mock(root=parent)
                    kernel.snapshot.return_value = snapshots()[0]
                    runner = Mock()
                    runner.run.side_effect = command
                    stack.enter_context(
                        patch.object(
                            helper.policy, "identity", return_value="test-run\n"
                        )
                    )
                    stack.enter_context(
                        patch.dict(
                            helper.os.environ, {"SUDO_UID": "1000", "SUDO_GID": "1000"}
                        )
                    )
                    stack.enter_context(patch.object(helper.sys, "argv", [str(HELPER)]))
                    stack.enter_context(patch.object(helper.signal, "signal"))
                    stack.enter_context(patch.object(helper.signal, "setitimer"))
                    stack.enter_context(
                        patch.object(helper, "KernelView", return_value=kernel)
                    )
                    stack.enter_context(
                        patch.object(
                            helper,
                            "safe_directory",
                            side_effect=lambda path, parent=parent: os.dup(parent),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            helper.policy, "filesystem_type", return_value=0x63677270
                        )
                    )
                    stack.enter_context(
                        patch.object(helper, "OwnedDirectory", side_effect=construct)
                    )
                    stack.enter_context(
                        patch.object(helper, "populated", return_value=False)
                    )
                    stack.enter_context(
                        patch.object(helper, "Runner", return_value=runner)
                    )
                    stack.enter_context(patch.object(helper, "ParserOwnerProof"))
                    stack.enter_context(
                        patch.object(helper, "executable", return_value={})
                    )
                    stack.enter_context(
                        patch.object(
                            helper,
                            "read_at",
                            side_effect=lambda fd, name: (
                                "0"
                                if name == "apparmor_restrict_unprivileged_unconfined"
                                else "1"
                            ),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            helper.policy,
                            "diagnostic_file",
                            return_value={
                                "status": "ok",
                                "sha256": helper.SOURCE_HASH,
                                "text": "fixed",
                            },
                        )
                    )
                    stack.enter_context(patch.object(helper, "save_file"))
                    stack.enter_context(
                        patch.object(
                            helper,
                            "stage_bytes",
                            side_effect=lambda obj, name, limit: (
                                b"fixed" if name == "profile" else compiled_fixture()
                            ),
                        )
                    )
                    stack.enter_context(patch.object(helper, "names", return_value=[]))
                    stack.enter_context(patch.object(helper, "cleanup_children"))
                    output = io.StringIO()
                    previous_trace = helper.sys.gettrace()

                    def interrupt_assignment(
                        frame, event, arg, helper=helper, role=role
                    ):
                        if (
                            event == "line"
                            and frame.f_code is helper.main.__code__
                            and frame.f_locals.get("creation_intent") == role
                            and frame.f_locals.get(role) is not None
                        ):
                            raise RuntimeError(
                                "simulated signal after caller assignment"
                            )
                        return interrupt_assignment

                    try:
                        if window == "caller-assignment":
                            helper.sys.settrace(interrupt_assignment)
                        with redirect_stdout(output):
                            code = helper.main()
                        report = json.loads(output.getvalue())
                        self.assertEqual(len(calls), ordinal)
                        self.assertEqual(code, 1)
                        self.assertFalse(report["success"])
                        self.assertFalse(report["cleanup"])
                        self.assertEqual(
                            report["failure_phase"],
                            "cgroup_create" if role == "cgroup" else "remaining_work",
                        )
                        self.assertEqual(
                            report["failure_reason"], "creation_unverified"
                        )
                        self.assertIsNone(report["cleanup_failure_phase"])
                        targets = list(root.rglob("cairncanary*"))
                        self.assertEqual(len(targets), 1)
                        self.assertTrue(targets[0].is_dir())
                    finally:
                        helper.sys.settrace(previous_trace)
                        for obj in objects:
                            obj.close()


if __name__ == "__main__":
    unittest.main()
