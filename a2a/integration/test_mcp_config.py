"""Offline acceptance tests for the explicit-path MCP configuration helper."""

import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "garden-config"


class MCPConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="garden config ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "bin with spaces"
        self.binary.write_text("#!/bin/sh\necho MUST_NOT_EXECUTE >&2\nexit 99\n")
        self.binary.chmod(0o700)
        self.profile = self.root / "profile with spaces.json"
        self.config = self.root / "host config.json"
        self.data = {
            "garden_endpoint": "https://garden.example.invalid/mcp",
            "credential_file": "deliberately-missing.token",
            "instance_id": "11111111-1111-4111-8111-111111111111",
            "scope": {"realm": "engineering", "segments": []},
            "classification": "internal",
            "participant": "val",
            "adapter": "stdio",
        }
        self.save_profile()

    def save_profile(self):
        self.profile.write_text(json.dumps(self.data))

    def run_helper(self, host="claude", *extra, success=True):
        result = subprocess.run(
            [
                str(HELPER),
                "--host",
                host,
                "--profile",
                str(self.profile),
                "--binary",
                str(self.binary),
                "--config",
                str(self.config),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode == 0, success, result.stderr)
        self.assertNotIn("MUST_NOT_EXECUTE", result.stdout + result.stderr)
        return result

    def owner(self):
        return self.config.with_name(self.config.name + ".garden-config.json")

    def backup(self):
        return self.config.with_name(self.config.name + ".garden-config.backup")

    def test_json_hosts_preserve_repeat_remove_and_private_backup(self):
        for host, key in [("claude", "mcpServers"), ("opencode", "mcp")]:
            with self.subTest(host=host):
                self.config = self.root / (host + ".json")
                original = {
                    "theme": "dark",
                    key: {
                        "cairn": {
                            "command": "/elsewhere",
                            "env": {"TOKEN": "SYNTHETIC_SECRET"},
                        }
                    },
                }
                self.config.write_text(json.dumps(original))
                before = self.config.read_bytes()
                self.run_helper(host)
                self.assertEqual(self.backup().read_bytes(), before)
                updated = json.loads(self.config.read_text())
                expected = [str(self.binary), "connect", "--profile", str(self.profile)]
                actual = updated[key]["garden"]
                self.assertEqual(
                    actual["command"]
                    if host == "opencode"
                    else [actual["command"], *actual["args"]],
                    expected,
                )
                self.assertEqual(updated[key]["cairn"], original[key]["cairn"])
                for path in (self.config, self.owner(), self.backup()):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                after = self.config.read_bytes()
                inode = self.config.stat().st_ino
                repeat = self.run_helper(host)
                self.assertIn("Unchanged", repeat.stdout)
                self.assertEqual(self.config.read_bytes(), after)
                self.assertEqual(self.config.stat().st_ino, inode)
                self.run_helper(host, "--remove")
                self.assertEqual(json.loads(self.config.read_text()), original)
                self.assertFalse(self.owner().exists())
                self.run_helper(host, "--remove")
                self.assertEqual(self.backup().read_bytes(), before)
                self.run_helper(host)
                self.assertEqual(self.backup().read_bytes(), before)

    def test_codex_preserves_bytes_and_accepts_new_unrelated_table(self):
        self.config = self.root / "config.toml"
        before = b'# personal comment\nmodel = "custom"\n\n[mcp_servers.cairn]\ncommand = "/other"'
        self.config.write_bytes(before)
        self.run_helper("codex")
        self.assertTrue(self.config.read_bytes().startswith(before))
        doc = tomllib.loads(self.config.read_text())
        self.assertEqual(
            doc["mcp_servers"]["garden"]["args"],
            ["connect", "--profile", str(self.profile)],
        )
        self.run_helper("codex")
        extra = b"\n[another]\nvalue = 7\n"
        self.config.write_bytes(self.config.read_bytes() + extra)
        self.run_helper("codex", "--remove")
        self.assertEqual(self.config.read_bytes(), before + extra)

    def test_verified_tls_profile_fields_work_for_all_hosts_without_reading_ca(self):
        self.data.update(
            garden_tls_ca_file="relative-missing-ca.pem",
            garden_tls_server_name="garden.example.invalid",
        )
        self.save_profile()
        for host in ("claude", "codex", "opencode"):
            with self.subTest(host=host):
                self.config = self.root / ("tls-" + host)
                self.run_helper(host)

    def test_tls_settings_on_plain_http_are_refused(self):
        self.data.update(
            garden_endpoint="http://127.0.0.1:8443/mcp",
            garden_tls_ca_file="ca.pem",
        )
        self.save_profile()
        self.run_helper(success=False)
        self.assertFalse(self.config.exists())

    def test_rejects_unowned_entries_even_identical(self):
        for host in ("claude", "codex", "opencode"):
            with self.subTest(host=host):
                self.config = self.root / host
                self.run_helper(host)
                self.owner().unlink()
                before = self.config.read_bytes()
                self.run_helper(host, success=False)
                self.run_helper(host, "--remove", success=False)
                self.assertEqual(self.config.read_bytes(), before)

    def test_modified_entry_cannot_be_removed(self):
        self.run_helper()
        content = json.loads(self.config.read_text())
        content["mcpServers"]["garden"]["env"] = {"TOKEN": "SYNTHETIC_SECRET"}
        self.config.write_text(json.dumps(content))
        before = self.config.read_bytes()
        result = self.run_helper("claude", "--remove", success=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertNotIn("SYNTHETIC_SECRET", result.stdout + result.stderr)

    def test_modified_codex_block_cannot_be_removed(self):
        self.run_helper("codex")
        self.config.write_text(self.config.read_text().replace("command =", "command="))
        self.run_helper("codex", "--remove", success=False)

    def test_malformed_duplicate_json_and_toml_refused_without_disclosure(self):
        for host, content in [
            ("claude", "{// SYNTHETIC_SECRET\n}"),
            ("claude", '{"a":1,"a":2}'),
            ("claude", '{"x":NaN}'),
            ("codex", 'secret = "SYNTHETIC_SECRET'),
        ]:
            with self.subTest(host=host, content=content):
                self.config.write_text(content)
                result = self.run_helper(host, success=False)
                self.assertEqual(self.config.read_text(), content)
                self.assertNotIn("SYNTHETIC_SECRET", result.stdout + result.stderr)
                self.assertFalse(self.owner().exists())

    def test_profile_rejects_adapter_mismatch_inline_secret_and_url_credentials(self):
        for delta in (
            {"adapter": "claude"},
            {"bearer": "SYNTHETIC_SECRET"},
            {"garden_endpoint": "https://u:SYNTHETIC_SECRET@example.invalid/mcp"},
            {"garden_endpoint": "https://example.invalid/mcp?token=SYNTHETIC_SECRET"},
        ):
            with self.subTest(delta=delta):
                original = self.data.copy()
                self.data.update(delta)
                self.save_profile()
                result = self.run_helper("opencode", success=False)
                self.assertNotIn("SYNTHETIC_SECRET", result.stdout + result.stderr)
                self.assertFalse(self.config.exists())
                self.data = original

    def test_invalid_endpoint_ports_do_not_create_configuration(self):
        for field in ("garden_endpoint", "host_endpoint"):
            for port in ("invalid", "65536"):
                with self.subTest(field=field, port=port):
                    self.data.update(
                        {
                            "adapter": "opencode",
                            "session_id": "ses_target",
                            "garden_endpoint": "https://garden.example.invalid/mcp",
                            "host_endpoint": "http://127.0.0.1:4096",
                        }
                    )
                    self.data[field] = "https://example.invalid:" + port + "/mcp"
                    self.save_profile()
                    self.run_helper("opencode", success=False)
                    self.assertFalse(self.config.exists())
                    self.assertFalse(self.owner().exists())
                    self.assertFalse(self.backup().exists())

    def test_host_session_identifier_byte_limit_matches_runtime(self):
        for host in ("codex", "opencode"):
            for session, valid in (
                ("a" * 128, True),
                ("a" * 129, False),
                ("é" * 64, True),
                ("é" * 65, False),
            ):
                with self.subTest(host=host, length=len(session.encode("utf-8"))):
                    self.data.update(
                        {
                            "adapter": host,
                            "session_id": session,
                            "codex_socket": "/run/user/1000/codex.sock",
                            "host_endpoint": "http://127.0.0.1:4096",
                        }
                    )
                    self.save_profile()
                    self.run_helper(host, "--dry-run", success=valid)
                    self.assertFalse(self.config.exists())
                    self.assertFalse(self.owner().exists())

    def test_dry_run_creates_nothing_and_does_not_open_credentials(self):
        self.config = self.root / "new" / "nested" / "config.json"
        credential = self.root / self.data["credential_file"]
        os.mkfifo(credential, 0o600)
        self.run_helper("claude", "--dry-run")
        self.assertFalse(self.config.parent.parent.exists())
        self.run_helper()
        self.assertTrue(self.config.exists())
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)

    def test_dry_run_existing_configuration_is_unchanged(self):
        self.config.write_text('{"theme":"dark"}')
        before = self.config.read_bytes()
        self.run_helper("claude", "--dry-run")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.owner().exists())
        self.assertFalse(self.backup().exists())

    def test_symlink_inputs_destination_owner_backup_and_parent_refused(self):
        actual = self.root / "actual"
        actual.write_text("{}")
        for kind in ("config", "owner", "backup", "profile", "binary", "parent"):
            with self.subTest(kind=kind):
                saved_profile, saved_binary = self.profile, self.binary
                self.config = self.root / ("config-" + kind)
                if kind == "parent":
                    folder = self.root / "linked-parent"
                    folder.symlink_to(self.root, target_is_directory=True)
                    self.config = folder / "new.json"
                elif kind in ("profile", "binary"):
                    link = self.root / ("linked-" + kind)
                    link.symlink_to(getattr(self, kind))
                    setattr(self, kind, link)
                else:
                    path = self.config if kind == "config" else getattr(self, kind)()
                    path.symlink_to(actual)
                self.run_helper(success=False)
                self.assertEqual(actual.read_text(), "{}")
                self.profile, self.binary = saved_profile, saved_binary

    def test_unsafe_directory_and_hardlink_refused(self):
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        self.config = unsafe / "config"
        self.run_helper(success=False)
        self.config = self.root / "hardlinked"
        os.link(self.profile, self.config)
        self.run_helper(success=False)

    def test_relative_and_traversal_paths_refused(self):
        for name in ("relative.json", str(self.root / ".." / "escape.json")):
            self.config = Path(name)
            self.run_helper(success=False)

    def test_unicode_codex_paths(self):
        new_binary = self.root / "binary 🌱"
        self.binary.rename(new_binary)
        self.binary = new_binary
        self.run_helper("codex")
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["mcp_servers"]["garden"]["command"], str(self.binary))
        self.run_helper("codex", "--remove")

    def test_double_root_alias_cannot_overwrite_profile(self):
        self.config = Path("/" + str(self.profile))
        before = self.profile.read_bytes()
        self.run_helper(success=False)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_codex_inline_parent_conflict_preserves_original(self):
        self.config.write_text('mcp_servers = { cairn = { command = "other" } }\n')
        before = self.config.read_bytes()
        self.run_helper("codex", success=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.owner().exists())


if __name__ == "__main__":
    unittest.main()
