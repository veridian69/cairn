"""Exact namespaced resource identities and durable creation/deletion transitions."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol
from urllib.parse import quote

from cairn_install.core import Context, InstallError

RESOURCE_APIS = {
    "serviceaccount": ("v1", "serviceaccounts"),
    "configmap": ("v1", "configmaps"),
    "service": ("v1", "services"),
    "secret": ("v1", "secrets"),
    "pod": ("v1", "pods"),
    "persistentvolumeclaim": ("v1", "persistentvolumeclaims"),
    "statefulset": ("apps/v1", "statefulsets"),
    "networkpolicy": ("networking.k8s.io/v1", "networkpolicies"),
}


class Command(Protocol):
    def __call__(
        self,
        *args: str,
        cluster: bool = False,
        timeout: float = 120,
        stdin: dict[str, Any] | None = None,
    ) -> str: ...


class ResourceJournal:
    """An old receipt or completed deletion never authorises a new incarnation."""

    def __init__(
        self,
        ctx: Context,
        *,
        record: dict[str, Any],
        expected: set[str],
        namespace: str,
        owner_labels: dict[str, str],
        command: Command,
    ) -> None:
        self.ctx = ctx
        self.objects: dict[str, dict[str, Any]] = record["objects"]
        self.expected = expected
        self.namespace = namespace
        self.labels = owner_labels
        self.command = command
        for key, receipt in self.objects.items():
            if (
                key not in expected
                or not isinstance(receipt, dict)
                or key
                != str(receipt.get("kind", "")).lower()
                + "/"
                + str(receipt.get("name", ""))
                or receipt.get("owner_labels") != owner_labels
                or receipt.get("phase")
                not in {"creating", "live", "deleting", "deleted"}
                or (receipt["phase"] in {"live", "deleting"} and not receipt.get("uid"))
            ):
                raise InstallError("Invalid recorded Kubernetes object ownership")

    def get(self, key: str, *, timeout: float = 120) -> dict[str, Any] | None:
        raw = self.command(
            "get", key, "--ignore-not-found", "-o", "json", timeout=timeout
        )
        if not raw.strip():
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            raise InstallError("kubectl returned malformed JSON") from None
        if not isinstance(value, dict):
            raise InstallError("kubectl returned an unexpected document")
        return value

    def owned(self, key: str, observed: dict[str, Any]) -> dict[str, Any]:
        receipt = self.objects.get(key, {})
        meta = observed.get("metadata", {})
        uid = meta.get("uid")
        phase = receipt.get("phase")
        pending = phase == "creating" and receipt.get("intent") is True
        if (
            not uid
            or phase == "deleted"
            or not (pending or phase in {"live", "deleting"})
            or any(meta.get("labels", {}).get(k) != v for k, v in self.labels.items())
            or (not pending and receipt.get("uid") != uid)
            or (pending and receipt.get("uid", uid) != uid)
            or (pending and receipt.get("previous_uid") == uid)
        ):
            raise InstallError("Kubernetes resource UID or ownership conflict: " + key)
        if pending:
            receipt.update(uid=uid, phase="live", intent=False)
            self.ctx.save()
        return observed

    def _deleted(self, key: str) -> None:
        receipt = self.objects.get(key)
        if receipt:
            # Retain the last UID as a tombstone; only create() may begin a fresh
            # transition after checking that the name is still absent.
            receipt.update(phase="deleted", intent=False)
            self.ctx.save()

    def inventory(self, *, allow_absent: bool = False) -> None:
        for key in self.expected:
            observed = self.get(key)
            receipt = self.objects.get(key, {})
            if observed:
                self.owned(key, observed)
            elif receipt.get("phase") == "deleting" or allow_absent:
                self._deleted(key)
            elif receipt.get("phase") == "live":
                raise InstallError("Recorded Kubernetes object is missing: " + key)

    def intent(self, key: str) -> None:
        if key not in self.expected:
            raise InstallError("Resource is outside this installation inventory")
        kind, name = key.split("/", 1)
        receipt = self.objects.setdefault(
            key,
            {
                "kind": kind,
                "name": name,
                "owner_labels": dict(self.labels),
                "phase": "deleted",
                "intent": False,
            },
        )
        if receipt["phase"] == "deleted":
            previous_uid = receipt.pop("uid", None)
            if previous_uid:
                receipt["previous_uid"] = previous_uid
            receipt.update(phase="creating", intent=True)
        if receipt["phase"] != "creating" or receipt.get("intent") is not True:
            raise InstallError("Resource has no pending creation intent: " + key)
        self.ctx.save()

    def create(self, document: dict[str, Any]) -> None:
        key = str(document["kind"]).lower() + "/" + str(document["metadata"]["name"])
        observed = self.get(key)
        if observed:
            self.owned(key, observed)
            if (
                observed["metadata"].get("deletionTimestamp")
                or self.objects[key]["phase"] == "deleting"
            ):
                raise InstallError("Kubernetes resource is still terminating: " + key)
            return
        receipt = self.objects.get(key, {})
        if receipt.get("phase") == "deleting":
            self._deleted(key)
        elif receipt.get("phase") == "live":
            raise InstallError(
                "Previously created Kubernetes object is missing: " + key
            )
        self.intent(key)
        self.command("create", "-f", "-", stdin=document)
        observed = self.get(key)
        if not observed:
            raise InstallError("Created Kubernetes object was not observable: " + key)
        self.owned(key, observed)

    def delete(self, key: str) -> None:
        observed = self.get(key)
        if not observed:
            self._deleted(key)
            return
        self.owned(key, observed)
        receipt = self.objects[key]
        receipt.update(phase="deleting", intent=False)
        self.ctx.save()  # Must precede server acceptance, including cancellation.
        kind, name = key.split("/", 1)
        version, plural = RESOURCE_APIS[kind]
        prefix = "/api/" if version == "v1" else "/apis/"
        path = (
            prefix
            + version
            + "/namespaces/"
            + quote(self.namespace, safe="")
            + "/"
            + plural
            + "/"
            + quote(name, safe="")
        )
        options = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": observed["metadata"]["uid"]},
            "propagationPolicy": "Foreground",
        }
        try:
            self.command("delete", "--raw", path, "-f", "-", stdin=options)
        except InstallError:
            if self.get(key) is not None:
                raise
        self.wait_deleted(key)
        self._deleted(key)

    def wait_deleted(self, key: str) -> None:
        # kubectl wait performs List/Watch even for an exact deletion target.
        # Poll GET instead, keeping deletion within the preflighted permissions.
        deadline = time.monotonic() + 180
        remaining = 180.0
        while remaining > 0:
            if self.get(key, timeout=min(30, remaining)) is None:
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1, remaining))
            remaining = deadline - time.monotonic()
        raise InstallError("Timed out waiting for Kubernetes resource deletion: " + key)
