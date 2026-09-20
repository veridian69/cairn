"""The documented build paths stamp the shared release version, never `dev`."""

import re
import subprocess
import tomllib
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
ROOT = MODULE.parent


def release_version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


class BuildVersionTests(unittest.TestCase):
    def test_default_make_build_reports_release_version(self):
        """`make -C a2a build` without VERSION is the documented operator path."""
        subprocess.run(
            ["make", "-C", str(MODULE), "build"], check=True, capture_output=True
        )
        output = subprocess.check_output([str(MODULE / "a2a"), "--version"], text=True)
        self.assertEqual(output.strip(), f"a2a version {release_version()}")

    def test_explicit_version_still_overrides_default(self):
        subprocess.run(
            ["make", "-C", str(MODULE), "VERSION=1.2.3-test", "build"],
            check=True,
            capture_output=True,
        )
        output = subprocess.check_output([str(MODULE / "a2a"), "--version"], text=True)
        self.assertEqual(output.strip(), "a2a version 1.2.3-test")
        # Leave the tree in the documented default state.
        subprocess.run(
            ["make", "-C", str(MODULE), "build"], check=True, capture_output=True
        )

    def test_container_build_requires_an_explicit_version(self):
        """A `docker build` without --build-arg VERSION must fail, not ship `dev`."""
        dockerfile = (MODULE / "Dockerfile").read_text()
        self.assertIsNone(
            re.search(r"^ARG VERSION=", dockerfile, re.MULTILINE),
            "Dockerfile must not default VERSION; the label and binary would say dev",
        )
        self.assertRegex(dockerfile, r'RUN test -n "\$\{VERSION\}"')


if __name__ == "__main__":
    unittest.main()
