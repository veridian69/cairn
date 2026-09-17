"""Offline installer acceptance tests; every installation targets a temporary root."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.module = self.base / "source"
        (self.module / "scripts").mkdir(parents=True)
        shutil.copytree(MODULE / "deploy", self.module / "deploy")
        for name in ("install-user", "install-server"):
            shutil.copy2(MODULE / "scripts" / name, self.module / "scripts" / name)
        for name in ("garden-config", "garden-session"):
            script = self.module / "scripts" / name
            script.write_text("#!/usr/bin/env python3\nprint('helper')\n")
            script.chmod(0o755)
        self.binary = self.base / "a2a"
        self.binary.write_text("#!/bin/sh\nprintf 'synthetic a2a\\n'\n")
        self.binary.chmod(0o755)
        self.destination = self.base / "installation"
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home))

    def invoke(self, kind="user", *args, binary=True, success=True):
        command = [str(self.module / "scripts" / f"install-{kind}")]
        if not args or args[0] != "--default-prefix":
            command += [
                "--prefix" if kind == "user" else "--root",
                str(self.destination),
            ]
        else:
            args = args[1:]
        if binary:
            command += ["--binary", str(self.binary)]
        result = subprocess.run(
            command + list(args), env=self.env, text=True, capture_output=True
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_user_payload_and_idempotence(self):
        self.invoke()
        binary = self.destination / "bin/a2a"
        self.assertEqual(
            subprocess.check_output([str(binary)], text=True), "synthetic a2a\n"
        )
        for name in ("a2a", "garden-config", "garden-session"):
            self.assertEqual(
                (self.destination / "bin" / name).stat().st_mode & 0o777, 0o755
            )
        self.assertTrue(
            (
                self.destination / "share/garden/deploy/codex.profile.example.json"
            ).is_file()
        )
        before = {
            p: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in self.destination.rglob("*")
            if p.is_file()
        }
        self.invoke()
        self.assertEqual(
            before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
        )
        self.assertEqual(list(self.home.iterdir()), [])

    def test_default_prefix(self):
        self.invoke("user", "--default-prefix")
        self.assertTrue((self.home / ".local/bin/a2a").is_file())

    def test_dry_run_has_no_filesystem_effects(self):
        self.invoke("user", "--dry-run")
        self.invoke("server", "--dry-run")
        self.assertFalse(self.destination.exists())

    def test_modified_managed_file_blocks_all_updates(self):
        self.invoke()
        managed = self.destination / "bin/garden-session"
        managed.write_text("locally edited\n")
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        original = (self.destination / "bin/a2a").read_bytes()
        self.invoke(success=False)
        self.assertEqual((self.destination / "bin/a2a").read_bytes(), original)
        self.assertEqual(managed.read_text(), "locally edited\n")

    def test_unknown_binary_is_not_overwritten(self):
        target = self.destination / "bin/a2a"
        target.parent.mkdir(parents=True)
        target.write_text("another installation\n")
        self.invoke(success=False)
        self.assertEqual(target.read_text(), "another installation\n")
        self.assertFalse((self.destination / "share").exists())

    def test_owned_binary_can_upgrade(self):
        self.invoke()
        self.binary.write_text("#!/bin/sh\nprintf 'updated\\n'\n")
        self.invoke()
        self.assertEqual(
            (self.destination / "bin/a2a").read_bytes(), self.binary.read_bytes()
        )

    def test_symlinked_destination_or_ancestor_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.destination.mkdir()
        (self.destination / "bin").symlink_to(outside, target_is_directory=True)
        self.invoke(success=False)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.destination / "share").exists())

    def test_server_stages_and_preserves_configuration(self):
        config = self.destination / "etc/garden/garden.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"existing": true}\n')
        config.chmod(0o600)
        self.invoke("server")
        self.assertTrue(
            (self.destination / "etc/systemd/system/garden-mcp.service").is_file()
        )
        self.assertTrue((self.destination / "usr/local/bin/a2a").is_file())
        runtime = self.destination / "var/lib/garden/.a2a"
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
        daemon = runtime / "config.yaml"
        daemon.write_text("existing daemon config\n")
        self.invoke("server")
        self.assertEqual(config.read_text(), '{"existing": true}\n')
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(daemon.read_text(), "existing daemon config\n")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_server_config_symlink_rejected_before_staging(self):
        config = self.destination / "etc/garden/garden.json"
        config.parent.mkdir(parents=True)
        target = self.base / "secret"
        target.write_text("do not touch")
        config.symlink_to(target)
        self.invoke("server", success=False)
        self.assertEqual(target.read_text(), "do not touch")
        self.assertFalse((self.destination / "usr").exists())

    def test_symlinked_manifest_is_rejected(self):
        self.invoke()
        manifest = self.destination / "share/garden/install-manifest.json"
        other = self.base / "manifest"
        manifest.rename(other)
        manifest.symlink_to(other)
        before = other.read_bytes()
        self.invoke(success=False)
        self.assertEqual(other.read_bytes(), before)

    def test_server_refuses_nonprivate_existing_runtime(self):
        runtime = self.destination / "var/lib/garden/.a2a"
        runtime.mkdir(parents=True)
        runtime.chmod(0o755)
        self.invoke("server", success=False)
        self.assertFalse((self.destination / "usr").exists())

    def test_symlinked_binary_source_is_rejected(self):
        actual = self.base / "actual"
        self.binary.rename(actual)
        self.binary.symlink_to(actual)
        self.invoke(success=False)
        self.assertFalse(self.destination.exists())

    def test_server_requires_explicit_root(self):
        self.invoke("server", "--default-prefix", success=False)

    def test_relative_prefix_rejected(self):
        self.invoke("user", "--prefix", "relative", success=False)

    def test_explicit_binary_does_not_build(self):
        fakebin = self.base / "fakebin"
        fakebin.mkdir()
        make = fakebin / "make"
        make.write_text("#!/bin/sh\nexit 99\n")
        make.chmod(0o755)
        self.env["PATH"] = str(fakebin) + os.pathsep + self.env["PATH"]
        self.invoke()

    def test_missing_binary_builds_using_module_make(self):
        fakebin = self.base / "fakebin"
        fakebin.mkdir()
        make = fakebin / "make"
        make.write_text(
            '#!/bin/sh\n[ "$1" = "-C" ] && [ "$3" = "build" ] || exit 91\ncp "$INSTALL_TEST_BINARY" "$2/a2a"\n'
        )
        make.chmod(0o755)
        self.env["PATH"] = str(fakebin) + os.pathsep + self.env["PATH"]
        self.env["INSTALL_TEST_BINARY"] = str(self.binary)
        self.invoke(binary=False)
        self.assertEqual(
            (self.destination / "bin/a2a").read_bytes(), self.binary.read_bytes()
        )

    def test_dry_run_without_binary_does_not_build(self):
        self.invoke("user", "--dry-run", binary=False)
        self.assertFalse((self.module / "a2a").exists())
        self.assertFalse(self.destination.exists())

    def test_non_executable_binary_rejected(self):
        self.binary.chmod(0o600)
        self.invoke(success=False)
        self.assertFalse(self.destination.exists())

    def test_missing_helper_rejected_before_staging(self):
        (self.module / "scripts/garden-session").unlink()
        self.invoke(success=False)
        self.assertFalse(self.destination.exists())

    def test_shipped_helpers_exist_and_are_executable(self):
        for name in ("garden-config", "garden-session"):
            self.assertTrue(os.access(MODULE / "scripts" / name, os.X_OK), name)


if __name__ == "__main__":
    unittest.main()
