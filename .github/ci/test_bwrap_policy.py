"""Offline lifecycle fixtures: no sudo, kernel loads, or host policy writes."""

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "bwrap_policy", Path(__file__).with_name("bwrap-policy.py")
)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class InventoryTests(unittest.TestCase):
    def fixture(self, stack, root):
        security = root / "security"
        security.mkdir()
        tree = root / "tree"
        tree.mkdir()
        (security / "policy").symlink_to(tree, target_is_directory=True)
        stack.enter_context(patch.object(policy, "SECURITY", security))
        stack.enter_context(patch.object(policy, "identity", return_value="1:1:host\n"))
        stack.enter_context(
            patch.object(
                policy,
                "filesystem_type",
                side_effect=lambda fd: (
                    0x73636673
                    if os.fstat(fd).st_ino == security.stat().st_ino
                    else 0x5A3C69F0
                ),
            )
        )
        stack.enter_context(
            patch.object(
                policy.subprocess, "run", side_effect=AssertionError("no commands")
            )
        )
        stack.enter_context(
            patch.object(
                policy, "diagnostic_file", side_effect=AssertionError("no source reads")
            )
        )
        self.add_profile(tree / "profiles/wike.120", "wike")
        return tree

    def add_profile(self, directory, name):
        directory.mkdir(parents=True)
        for field, value in {
            "name": name,
            "mode": "unconfined",
            "attach": "<unknown>",
            "sha256": "a" * 64,
        }.items():
            (directory / field).write_text(value + "\n")

    def invoke(self):
        output = io.StringIO()
        with (
            patch("sys.stdout", output),
            patch.object(policy.sys, "argv", ["policy", "inventory"]),
        ):
            policy.main()
        return json.loads(output.getvalue())

    def test_complete_root_child_and_namespace_inventory(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            tree = self.fixture(stack, Path(tmp))
            self.add_profile(tree / "profiles/wike.120/profiles/child.1", "wike//child")
            self.add_profile(tree / "namespaces/test/profiles/other.2", "other")
            report = self.invoke()
            self.assertTrue(report["complete"])
            self.assertEqual(len(report["profiles"]), 3)
            rows = {row["name"]: row for row in report["profiles"]}
            self.assertEqual(rows["other"]["namespace"], ["test"])
            self.assertEqual(rows["wike//child"]["namespace"], [])
            self.assertEqual(rows["wike"]["attach"], "<unknown>")
            self.assertEqual(rows["wike"]["sha256"], "a" * 64)
            self.assertTrue(rows["wike"]["path"].endswith("/policy/profiles/wike.120"))

    def test_incomplete_inputs_never_succeed(self):
        for bad in (
            "missing",
            "oversize",
            "hash",
            "symlink-field",
            "symlink-profile",
            "count",
            "output",
            "filesystem",
            "empty",
            "namespace-symlink",
            "entries",
            "depth",
            "field-directory",
            "invalid-utf8",
        ):
            with (
                self.subTest(bad=bad),
                tempfile.TemporaryDirectory() as tmp,
                ExitStack() as stack,
            ):
                tree = self.fixture(stack, Path(tmp))
                field = tree / "profiles/wike.120/sha256"
                if bad == "missing":
                    field.unlink()
                elif bad == "oversize":
                    field.write_text("x" * 4096)
                elif bad == "hash":
                    field.write_text("not a hash")
                elif bad == "symlink-field":
                    field.unlink()
                    field.symlink_to(tree / "outside")
                elif bad == "symlink-profile":
                    (tree / "profiles/link").symlink_to(tree, target_is_directory=True)
                elif bad == "count":
                    stack.enter_context(
                        patch.object(policy, "INVENTORY_MAX_PROFILES", 0, create=True)
                    )
                elif bad == "output":
                    stack.enter_context(
                        patch.object(policy, "INVENTORY_MAX_OUTPUT", 128, create=True)
                    )
                elif bad == "filesystem":
                    stack.enter_context(
                        patch.object(policy, "filesystem_type", return_value=0)
                    )
                elif bad == "empty":
                    field.parent.rename(tree / "hidden")
                elif bad == "namespace-symlink":
                    (tree / "namespaces").symlink_to(tree, target_is_directory=True)
                elif bad == "entries":
                    stack.enter_context(
                        patch.object(policy, "INVENTORY_MAX_ENTRIES", 0)
                    )
                elif bad == "depth":
                    self.add_profile(
                        tree / "profiles/wike.120/profiles/child.1", "child"
                    )
                    stack.enter_context(patch.object(policy, "INVENTORY_MAX_DEPTH", 0))
                elif bad == "field-directory":
                    field.unlink()
                    field.mkdir()
                elif bad == "invalid-utf8":
                    field.write_bytes(b"\xff")
                output = io.StringIO()
                with (
                    patch("sys.stdout", output),
                    patch.object(policy.sys, "argv", ["policy", "inventory"]),
                    self.assertRaises(RuntimeError),
                ):
                    policy.main()
                self.assertFalse(json.loads(output.getvalue())["complete"])

    def test_inventory_identity_failure_precedes_kernel_reads(self):
        with (
            patch.object(policy.sys, "argv", ["policy", "inventory"]),
            patch.object(
                policy, "identity", side_effect=RuntimeError("foreign runner")
            ),
            patch.object(policy.os, "open") as opened,
        ):
            with self.assertRaisesRegex(RuntimeError, "foreign runner"):
                policy.main()
            opened.assert_not_called()

    def test_short_reads_are_completed_not_reported_as_full_fields(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            self.fixture(stack, Path(tmp))
            original = os.read
            stack.enter_context(
                patch.object(
                    policy.os, "read", lambda fd, count: original(fd, min(count, 7))
                )
            )
            report = self.invoke()
            self.assertEqual(report["profiles"][0]["attach"], "<unknown>")
            self.assertEqual(report["profiles"][0]["sha256"], "a" * 64)

    def test_inventory_json_is_bounded_and_workflow_safe(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            tree = self.fixture(stack, Path(tmp))
            value = "##[warning]bad\n::error::bad\r\x1b"
            (tree / "profiles/wike.120/attach").write_text(value + "\n")
            output = io.StringIO()
            with (
                patch("sys.stdout", output),
                patch.object(policy.sys, "argv", ["policy", "inventory"]),
            ):
                policy.main()
            raw = output.getvalue()
            self.assertNotIn("#", raw)
            self.assertEqual(len(raw.splitlines()), 1)
            self.assertLessEqual(len(raw.encode()), policy.INVENTORY_MAX_OUTPUT)
            self.assertEqual(json.loads(raw)["profiles"][0]["attach"], value)


class WpcomTests(unittest.TestCase):
    # Independently specified wire header: named u32 version, then profile struct.
    binary = (
        b"\x04\x08\x00version\x00\x02\x09\x00\x00\x00"
        b"\x04\x08\x00profile\x00\x07\x05\x06\x00wpcom\x00fixture"
    )
    digest = hashlib.sha256(binary[12:]).hexdigest()

    def test_hash_matches_kernel_version_plus_profile_extent(self):
        self.assertEqual(policy.profile_hash(self.binary), self.digest)
        for value in (
            b"",
            self.binary[:16],
            self.binary.replace(b"wpcom", b"other"),
            self.binary.replace(b"version", b"garbage"),
            self.binary[:12] + b"\x0a\x00\x00\x00" + self.binary[16:],
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                policy.profile_hash(value)

    def fixture(self, stack, root):
        state = root / "state"
        security = root / "security"
        target = security / "policy/profiles/wpcom.121/attach"
        target.parent.mkdir(parents=True)
        target.write_text("<unknown>\n")
        fields = {
            "attach": "<unknown>\n",
            "name": "wpcom\n",
            "mode": "unconfined\n",
            "sha256": self.digest + "\n",
        }
        labels = ["wpcom (unconfined)"]
        calls = []
        package = {
            "version": "apparmor\t4.0.1really4.0.1-0ubuntu0.24.04.7\n",
            "owner": "apparmor: /etc/apparmor.d/wpcom\n",
        }
        source = {
            "status": "ok",
            "sha256": "59b795822094c6721eb95883cb9dfe2cdc2babaa34483a4541dc24113d1c181a",
        }
        local = {"status": "missing"}

        def command(*args, **kwargs):
            calls.append(args)
            if "--show" in args:
                return package["version"]
            if "--search" in args:
                return package["owner"]
            if "--stdout" in args:
                return self.binary
            if "--names" in args:
                return "wpcom" if args[-1] == "/etc/apparmor.d/wpcom" else policy.NAME
            if "--remove" in args:
                if args[-1] == str(state / "wpcom.remove"):
                    labels.remove("wpcom (unconfined)")
                    target.unlink()
                else:
                    labels.remove(policy.NAME + " (unconfined)")
            if "--add" in args:
                if "--binary" in args:
                    labels.append("wpcom (unconfined)")
                    target.write_text("<unknown>\n")
                else:
                    labels.append(policy.NAME + " (unconfined)")
            if args[0] == "/usr/sbin/sysctl":
                return "0" if args[-1].endswith("unconfined") else "1"
            return ""

        stack.enter_context(
            patch.multiple(
                policy,
                STATE=state,
                PROFILE=state / "profile",
                OWNER=state / "owner",
                SECURITY=security,
            )
        )
        stack.enter_context(patch.object(policy, "identity", return_value="1:1:host\n"))
        stack.enter_context(patch.object(policy, "executable_identity"))
        stack.enter_context(patch.object(policy, "run", command))
        stack.enter_context(patch.object(policy, "loaded", lambda: labels.copy()))
        stack.enter_context(
            patch.object(
                policy,
                "kernel_diagnostic_file",
                lambda path: {"status": "ok", "text": fields[path.name]},
            )
        )
        stack.enter_context(
            patch.object(
                policy,
                "diagnostic_file",
                lambda path: local if str(path).endswith("local/wpcom") else source,
            )
        )
        stack.enter_context(patch.object(policy, "attachment_diagnostic"))
        # Ownership alone is mocked: the fixture never needs root.
        stack.enter_context(patch.object(policy.os, "geteuid", return_value=0))
        return state, target, fields, labels, calls, package, source, local

    def test_verified_removal_rescan_and_binary_restoration(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            state, target, _, labels, calls, *_ = self.fixture(stack, Path(directory))
            # Retain a readable nonconflicting attachment for the post-removal scan.
            other = target.parents[1] / "other.1/attach"
            other.parent.mkdir()
            other.write_text("/opt/other")
            reader = policy.kernel_diagnostic_file
            stack.enter_context(
                patch.object(
                    policy,
                    "kernel_diagnostic_file",
                    lambda path: (
                        {"status": "ok", "text": "/opt/other"}
                        if path == other
                        else reader(path)
                    ),
                )
            )
            policy.install("1:1:host\n")
            self.assertEqual(labels, ["cairn-ci-bwrap (unconfined)"])
            self.assertEqual((state / "wpcom.bin").read_bytes(), self.binary)
            # The real ownership validator is covered separately for foreign state.
            with patch.object(policy, "owned"):
                policy.cleanup("1:1:host\n")
            self.assertEqual(labels, ["wpcom (unconfined)"])
            self.assertFalse(state.exists())
            mutations = [c for c in calls if "--remove" in c or "--add" in c]
            self.assertEqual(
                ["--binary" in c for c in mutations], [False, False, False, True]
            )
            self.assertTrue(all("--replace" not in c for c in calls))

    def test_mismatched_identity_source_package_mode_hash_and_local_rules_refuse(self):
        for bad in (
            "name",
            "mode",
            "sha256",
            "version",
            "owner",
            "source",
            "local",
            "scope",
            "hash-mismatch",
            "duplicate-label",
            "missing-hash",
            "symlink-source",
            "unreadable-local",
            "local-comment-include",
        ):
            with (
                self.subTest(bad=bad),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                state, target, fields, labels, calls, package, source, local = (
                    self.fixture(stack, Path(directory))
                )
                if bad in fields:
                    fields[bad] = "wrong\n"
                elif bad in package:
                    package[bad] = "wrong\n"
                elif bad == "source":
                    source["sha256"] = "0" * 64
                elif bad == "local":
                    local.update(status="ok", text="/bin/bwrap ux,\n")
                elif bad == "scope":
                    stack.enter_context(
                        patch.object(
                            policy, "identity", side_effect=RuntimeError("scope")
                        )
                    )
                elif bad == "hash-mismatch":
                    fields["sha256"] = "0" * 64
                elif bad == "duplicate-label":
                    labels.append("wpcom//child (unconfined)")
                elif bad == "missing-hash":
                    fields["sha256"] = ""
                elif bad == "symlink-source":
                    source["status"] = "unreadable"
                elif bad == "unreadable-local":
                    local["status"] = "unreadable"
                elif bad == "local-comment-include":
                    local.update(status="ok", text="#include <local/other>\n")
                with self.assertRaises(RuntimeError):
                    policy.install("1:1:host\n")
                self.assertTrue(target.exists())
                self.assertFalse(state.exists())
                self.assertFalse(any("--remove" in c or "--add" in c for c in calls))

    def test_post_removal_inventory_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            state, target, _, labels, calls, *_ = self.fixture(stack, Path(directory))
            # An empty post-removal inventory is still a failure, never waived.
            with (
                patch.object(policy, "owned"),
                self.assertRaisesRegex(RuntimeError, "inventory"),
            ):
                policy.install("1:1:host\n")
            self.assertEqual(labels, ["wpcom (unconfined)"])
            self.assertTrue(target.exists())
            self.assertFalse(state.exists())
            self.assertFalse(any("--add" in c and "--binary" not in c for c in calls))

    def test_other_unknown_prevents_any_removal(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            state, target, _, labels, calls, *_ = self.fixture(stack, Path(directory))
            other = target.parents[1] / "other.1/attach"
            other.parent.mkdir()
            other.write_text("<unknown>")
            with self.assertRaisesRegex(RuntimeError, "existing or ambiguous"):
                policy.install("1:1:host\n")
            self.assertEqual(labels, ["wpcom (unconfined)"])
            self.assertFalse(state.exists())
            self.assertFalse(any("--add" in c or "--remove" in c for c in calls))

    def test_readable_nonconflicting_wpcom_needs_no_exception(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            state, _, fields, labels, calls, *_ = self.fixture(stack, Path(directory))
            fields["attach"] = "/opt/WordPress.com/wpcom\n"
            policy.install("1:1:host\n")
            self.assertFalse((state / "wpcom.bin").exists())
            with patch.object(policy, "owned"):
                policy.cleanup("1:1:host\n")
            self.assertEqual(labels, ["wpcom (unconfined)"])
            self.assertFalse(any("--stdout" in c or "--binary" in c for c in calls))

    def test_staging_failures_leave_original_loaded(self):
        for failed_file in ("wpcom.bin", "wpcom.sha256", "wpcom.remove"):
            with (
                self.subTest(file=failed_file),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                state, _, _, labels, calls, *_ = self.fixture(stack, Path(directory))
                original_open = Path.open

                def fail(
                    path,
                    mode="r",
                    *args,
                    failed_file=failed_file,
                    original_open=original_open,
                    **kwargs,
                ):
                    if path.name == failed_file and mode == "xb":
                        with original_open(path, mode) as stream:
                            stream.write(b"partial")
                        raise OSError("staging failure")
                    return original_open(path, mode, *args, **kwargs)

                # rollback_unloaded checks the real current uid, not root's uid.
                stack.enter_context(
                    patch.object(policy.os, "geteuid", return_value=os.getuid())
                )
                stack.enter_context(patch.object(Path, "open", fail))
                with self.assertRaisesRegex(OSError, "staging failure"):
                    policy.install("1:1:host\n")
                self.assertEqual(labels, ["wpcom (unconfined)"])
                self.assertFalse(state.exists())
                self.assertFalse(any("--add" in c or "--remove" in c for c in calls))

    def test_mutation_failure_restores_or_retains_recoverable_state(self):
        for failure in ("before-remove", "after-remove", "restore"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                state, _, _, labels, calls, *_ = self.fixture(stack, Path(directory))
                original = policy.run

                def fail(*args, failure=failure, original=original, **kwargs):
                    removing = "--remove" in args
                    if removing and failure == "before-remove":
                        raise RuntimeError("remove failed")
                    if "--binary" in args and failure == "restore":
                        raise RuntimeError("restore failed")
                    result = original(*args, **kwargs)
                    if removing and failure == "after-remove":
                        raise RuntimeError("remove failed")
                    return result

                stack.enter_context(patch.object(policy, "owned"))
                with patch.object(policy, "run", fail), self.assertRaises(RuntimeError):
                    policy.install("1:1:host\n")
                if failure == "restore":
                    self.assertTrue((state / "wpcom.bin").exists())
                    self.assertEqual(labels, [])
                    policy.cleanup("1:1:host\n")
                self.assertEqual(labels, ["wpcom (unconfined)"])
                self.assertFalse(state.exists())
                self.assertFalse(any("--replace" in c for c in calls))

    def test_cleanup_refuses_changed_reappearing_profile_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            state, target, fields, labels, calls, *_ = self.fixture(
                stack, Path(directory)
            )
            state.mkdir(mode=0o700)
            (state / "wpcom.bin").write_bytes(self.binary)
            (state / "wpcom.sha256").write_text(self.digest)
            fields["sha256"] = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                policy.restore_wpcom("1:1:host\n")
            self.assertTrue(target.exists())
            self.assertEqual(labels, ["wpcom (unconfined)"])
            self.assertTrue((state / "wpcom.bin").exists())
            self.assertEqual(calls, [])


class PolicyTests(unittest.TestCase):
    def test_kernel_reader_verified_jump_and_wrong_filesystems(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            security = root / "apparmor"
            security.mkdir()
            tree = root / "kernel-tree"
            field = tree / "profiles/example.1/attach"
            field.parent.mkdir(parents=True)
            field.write_text("<unknown>\n")
            (security / "policy").symlink_to(tree, target_is_directory=True)
            target = security / "policy/profiles/example.1/attach"
            with patch.object(policy, "SECURITY", security):
                for kinds, expected in (
                    ([0, 0x5A3C69F0], "unreadable"),
                    ([0x73636673, 0], "unreadable"),
                    ([0x73636673, 0x5A3C69F0], "ok"),
                ):
                    with patch.object(policy, "filesystem_type", side_effect=kinds):
                        self.assertEqual(
                            policy.kernel_diagnostic_file(target)["status"], expected
                        )
                with patch.object(
                    policy, "filesystem_type", side_effect=[0x73636673, 0x5A3C69F0]
                ):
                    self.assertEqual(
                        policy.kernel_diagnostic_file(target)["text"], "<unknown>\n"
                    )
                field.unlink()
                field.symlink_to(root / "outside")
                with patch.object(
                    policy, "filesystem_type", side_effect=[0x73636673, 0x5A3C69F0]
                ):
                    self.assertEqual(
                        policy.kernel_diagnostic_file(target), {"status": "unreadable"}
                    )
                field.unlink()
                field.parent.rmdir()
                field.parent.symlink_to(root, target_is_directory=True)
                with patch.object(
                    policy, "filesystem_type", side_effect=[0x73636673, 0x5A3C69F0]
                ):
                    self.assertEqual(
                        policy.kernel_diagnostic_file(target), {"status": "unreadable"}
                    )

    def test_kernel_reader_refuses_unapproved_paths_before_open(self):
        for suffix in (
            "profiles/x/raw_data",
            "profiles/x/../name",
            "namespaces/x/name",
        ):
            with patch.object(policy.os, "open") as opened:
                self.assertEqual(
                    policy.kernel_diagnostic_file(policy.SECURITY / "policy" / suffix),
                    {"status": "unreadable"},
                )
                opened.assert_not_called()

    def test_diagnostic_json_neutralises_workflow_commands_with_exact_roundtrip(self):
        payload = (
            "##[warning]legacy ##[error]legacy ##[add-mask]secret "
            "::warning::modern\n::error::modern\r\n::add-mask::secret "
            "\x00\x1b[31m\t\\u0023 # unicode:\u2603"
        )
        value = {"status": "ok", "text": payload}
        output = io.StringIO()
        with (
            patch.object(policy, "diagnostic_file", return_value=value),
            patch.object(policy, "kernel_diagnostic_file", return_value=value),
            patch.object(policy, "diagnostic_command", return_value=value),
            patch("sys.stdout", output),
        ):
            policy.attachment_diagnostic(Path("/fixture/##[warning]/attach"), value)
        emitted = output.getvalue()
        self.assertNotIn("#", emitted)
        self.assertEqual(len(emitted.splitlines()), 1)
        self.assertTrue(emitted.startswith("{"))  # Modern commands require a :: prefix.
        for control in ("\r", "\x00", "\x1b", "\t"):
            self.assertNotIn(control, emitted)
        decoded = json.loads(emitted)
        self.assertEqual(decoded["path"], "/fixture/##[warning]/attach")
        for key in ("attach", "name", "mode", "sha256", "package", "package_owner"):
            self.assertEqual(decoded[key], value)
        self.assertTrue(all(item == value for item in decoded["sources"].values()))

    def test_diagnostic_file_bounds_and_safe_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = root / "value"
            value.write_bytes(b'<unknown>\n"quoted"\n')
            result = policy.diagnostic_file(value)
            self.assertEqual(result["text"], '<unknown>\n"quoted"\n')
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["sha256"]), 64)
            value.write_bytes(b"x" * 4097)
            self.assertEqual(policy.diagnostic_file(value), {"status": "truncated"})
            self.assertEqual(
                policy.diagnostic_file(root / "missing"), {"status": "missing"}
            )
            link = root / "link"
            link.symlink_to(value)
            self.assertEqual(policy.diagnostic_file(link), {"status": "unreadable"})
            parent_link = root / "parent-link"
            parent_link.symlink_to(root, target_is_directory=True)
            self.assertEqual(
                policy.diagnostic_file(parent_link / "value"), {"status": "unreadable"}
            )
            fifo = root / "fifo"
            os.mkfifo(fifo)
            self.assertEqual(policy.diagnostic_file(fifo), {"status": "unreadable"})

    def test_rejected_attachment_json_is_fixed_scope_and_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            attach = Path(directory) / "attach"
            attach.write_text("<unknown>\n")
            output = io.StringIO()
            reads = []

            def read(path):
                reads.append(path)
                return {"status": "missing"}

            with (
                patch.object(policy, "diagnostic_file", read),
                patch.object(policy, "kernel_diagnostic_file", read),
                patch.object(
                    policy, "diagnostic_command", return_value={"status": "unreadable"}
                ),
                patch("sys.stdout", output),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "existing or ambiguous attachment"
                ):
                    policy.check_attachment(attach)
            record = json.loads(output.getvalue())
            self.assertEqual(record["attach"], {"status": "missing"})
            self.assertEqual(
                reads,
                [
                    attach,
                    attach.with_name("name"),
                    attach.with_name("mode"),
                    attach.with_name("sha256"),
                    Path("/etc/apparmor.d/wpcom"),
                    Path("/etc/apparmor.d/local/wpcom"),
                ],
            )

    def test_unknown_attachment_still_refuses_without_load(self):
        with tempfile.TemporaryDirectory() as directory:
            attach = Path(directory) / "attach"
            attach.write_text("<unknown>\n")
            with (
                patch.object(policy, "attachment_diagnostic") as diagnostic,
                patch.object(
                    policy,
                    "kernel_diagnostic_file",
                    return_value={"status": "ok", "text": "<unknown>\n"},
                ),
                patch.object(policy, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "existing or ambiguous"):
                    policy.check_attachment(attach)
                self.assertEqual(diagnostic.call_args.args[1]["text"], "<unknown>\n")
                run.assert_not_called()

    def test_local_and_self_hosted_refused(self):
        for environment in ("", "self-hosted"):
            with patch.dict(
                os.environ, {"CAIRN_RUNNER_ENVIRONMENT": environment}, clear=True
            ):
                with self.assertRaises(RuntimeError):
                    policy.identity()

    def test_attachment_conflicts(self):
        for value in (
            "/usr/bin/bwrap",
            "/bin/bwrap",
            "/usr/bin/*",
            "/{usr/,}bin/bwrap",
            "<unknown>",
            "/**",
        ):
            self.assertTrue(policy.possible_attachment(value), value)
        for value in ("/usr/bin/other", "/opt/browser/**", ""):
            self.assertFalse(policy.possible_attachment(value), value)

    def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            calls = []
            labels = []

            def command(*args):
                calls.append(args)
                if "--add" in args:
                    labels.append(policy.NAME + " (unconfined)")
                if "--remove" in args:
                    labels.clear()
                return ""

            with (
                patch.multiple(
                    policy,
                    STATE=state,
                    PROFILE=state / "profile",
                    OWNER=state / "owner",
                ),
                patch.object(policy, "run", command),
                patch.object(policy, "loaded", lambda: labels),
            ):
                policy.cleanup("1:1:host\n")  # Absent state is a no-op.
                self.assertEqual(calls, [])
                state.mkdir(mode=0o700)
                policy.PROFILE.write_bytes(policy.SOURCE.read_bytes())
                policy.OWNER.write_text("1:1:host\n")
                # Fixtures run as the current user, never as root.
                with patch.object(policy, "owned") as ownership:
                    labels.append(policy.NAME + " (unconfined)")
                    policy.cleanup("1:1:host\n")
                    ownership.assert_called_once_with("1:1:host\n")
                self.assertFalse(state.exists())
                self.assertEqual(len(calls), 1)
                self.assertIn("--remove", calls[0])
                self.assertIn("--skip-cache", calls[0])

    def test_existing_destination_refused_without_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(policy, "STATE", Path(directory)),
                patch.object(policy, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "destination already exists"):
                    policy.install("1:1:host\n")
                run.assert_not_called()

    def test_install_and_failed_load_rollback(self):
        for failure in (
            None,
            "load",
            "profile-open",
            "profile-write",
            "owner-open",
            "owner-write",
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / "state"
                security = root / "security"
                attachment = security / "policy/profiles/other/attach"
                attachment.parent.mkdir(parents=True)
                attachment.write_text("/opt/other")
                labels = []
                calls = []

                def command(*args, calls=calls, failure=failure, labels=labels):
                    calls.append(args)
                    if args[0] == "/usr/sbin/sysctl":
                        return "0" if args[-1].endswith("unconfined") else "1"
                    if "--names" in args:
                        return policy.NAME
                    if "--add" in args:
                        if failure == "load":
                            raise RuntimeError("fixture load failure")
                        labels.append(policy.NAME + " (unconfined)")
                    if "--remove" in args:
                        labels.clear()
                    return ""

                original_open = Path.open

                def failing_open(
                    path,
                    mode="r",
                    *args,
                    failure=failure,
                    original_open=original_open,
                    **kwargs,
                ):
                    if failure and failure.startswith(path.name + "-") and "x" in mode:
                        if failure.endswith("write"):
                            with original_open(path, mode, *args, **kwargs) as stream:
                                stream.write(b"partial" if "b" in mode else "partial")
                        raise OSError("fixture state failure")
                    return original_open(path, mode, *args, **kwargs)

                with (
                    patch.multiple(
                        policy,
                        STATE=state,
                        PROFILE=state / "profile",
                        OWNER=state / "owner",
                        SECURITY=security,
                    ),
                    patch.object(policy, "run", command),
                    patch.object(policy, "loaded", lambda labels=labels: labels),
                    patch.object(policy, "owned"),
                    patch.object(policy, "executable_identity"),
                    patch.object(
                        policy,
                        "kernel_diagnostic_file",
                        return_value={"status": "ok", "text": "/opt/other"},
                    ),
                    patch.object(Path, "open", failing_open),
                ):
                    if failure == "load":
                        with self.assertRaisesRegex(
                            RuntimeError, "fixture load failure"
                        ):
                            policy.install("1:1:host\n")
                    elif failure:
                        with self.assertRaisesRegex(OSError, "fixture state failure"):
                            policy.install("1:1:host\n")
                        self.assertFalse(
                            any("--add" in call or "--remove" in call for call in calls)
                        )
                    else:
                        policy.install("1:1:host\n")
                        self.assertEqual(
                            policy.PROFILE.read_bytes(), policy.SOURCE.read_bytes()
                        )
                        policy.cleanup("1:1:host\n")
                    self.assertFalse(state.exists())
                    self.assertFalse(any("--replace" in call for call in calls))

    def test_partial_rollback_refuses_replaced_directory_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            metadata = state.stat()
            created = (metadata.st_dev, metadata.st_ino)
            with patch.multiple(
                policy, STATE=state, PROFILE=state / "profile", OWNER=state / "owner"
            ):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    policy.rollback_unloaded((created[0], created[1] + 1))
                victim = Path(directory) / "victim"
                victim.write_text("preserve")
                policy.PROFILE.symlink_to(victim)
                with self.assertRaisesRegex(RuntimeError, "unexpected partial-state"):
                    policy.rollback_unloaded(created)
                self.assertEqual(victim.read_text(), "preserve")
                self.assertTrue(policy.PROFILE.is_symlink())

    def test_executable_symlink_refused_before_package_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("not executed")
            link = Path(directory) / "bwrap"
            link.symlink_to(target)
            with (
                patch.object(policy, "BWRAP", link),
                patch.object(policy, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "canonical"):
                    policy.executable_identity()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
