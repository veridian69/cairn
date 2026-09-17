"""Synthetic reference-site settings for acceptance harness tests."""

from __future__ import annotations

import os

_SETTINGS = {
    "REFERENCE_EXPECTED_HOST": "reference",
    "REFERENCE_NODE_NAME": "reference",
    "REFERENCE_BASELINE_NAMESPACE": "cairn-target-acceptance",
    "REFERENCE_GATEWAY_NAMESPACE": "cairn-egress",
    "REFERENCE_STORAGE_CLASS": "cairn-local",
    "REFERENCE_BASELINE_REVISION": "0de93bd1492beda56143bc45b155fccf7cd47caf",
    "REFERENCE_BASELINE_RUN_ID": "task11-0de93bd1492b-01",
    "REFERENCE_BASELINE_PRIMARY_SHA256": "e8b489f8b67e038cc00dc4b2cc807f4a16ba21457d1a668b9e36c33d59361606",
    "REFERENCE_BASELINE_CILIUM_SHA256": "eb5c9251fc8e994b030cf7fc8c5da2aa7e3f5224c8f44390fb046c8270480da0",
    "REFERENCE_ACCEPTANCE_RUN_ID": "task11-0123456789ab-01",
    "REFERENCE_ACCEPTANCE_REPORT": "build/reference-acceptance/test-report.json",
    "REFERENCE_EXPECTED_DISTRIBUTION": "Fedora Linux 44",
    "REFERENCE_EXPECTED_SELINUX": "Enforcing",
    "REFERENCE_PROVIDER_SECRET_FILE": "/tmp/example-provider-secret",
    "REFERENCE_ACCEPTANCE_NAMESPACE": "cairn-target-acceptance",
    "REFERENCE_FOREIGN_NAMESPACE": "cairn-acceptance-foreign",
    "REFERENCE_ENFORCEMENT_NAMESPACE": "cairn-acceptance-enforcement",
    "REFERENCE_SHAPES_NAMESPACE": "cairn-acceptance-shapes",
}

_PREVIOUS = {name: os.environ.get(name) for name in _SETTINGS}
os.environ.update(_SETTINGS)


def pytest_unconfigure() -> None:
    """Restore the caller's environment after the synthetic test profile."""

    for name, previous in _PREVIOUS.items():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
