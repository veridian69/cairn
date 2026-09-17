from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cairn_install import native, workflow
from cairn_install.core import InstallError, open_context


@pytest.mark.parametrize("fault", [None, "foreign", "missing", "start_failed"])
def test_verified_native_resume_starts_retained_sidecar_before_cairn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    with open_context(
        tmp_path / "state",
        "demo",
        create={
            "source": str(tmp_path),
            "mode": "native",
            "port": 19000,
            "semantic": True,
        },
    ) as ctx:
        backend = native.Backend(ctx)
        index = backend._index  # noqa: SLF001
        assert index is not None
        ctx.state["resources"]["native_index_port"] = 19001
        ctx.state["resources"]["native_index_container"] = {
            "name": index.container,
            "label": ctx.instance_id,
            "status": "stopped",
        }
        ctx.state["status"] = "verified"
        ctx.state["receipts"]["ingest"] = {
            "outcome": "committed",
            "mutation_receipt": {},
            "audit_receipt": {},
            "result": {
                "evidence_id": str(uuid4()),
                "assertion_id": str(uuid4()),
                "fact_ids": [str(uuid4())],
            },
        }
        ctx.save()
        calls: list[str] = []
        inspected = {
            "Config": {
                "Labels": {
                    native._INDEX_LABEL: "foreign"
                    if fault == "foreign"
                    else ctx.instance_id
                }
            },  # noqa: SLF001
            "HostConfig": {
                "PortBindings": {
                    "6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19001"}]
                }
            },
            "State": {"Running": False},
        }
        monkeypatch.setattr(
            index, "_inspect", lambda *args: None if fault == "missing" else inspected
        )
        monkeypatch.setattr(backend, "preflight", lambda: None)
        monkeypatch.setattr(backend, "validate_ownership", lambda: None)
        monkeypatch.setattr(backend, "_start_unit", lambda: calls.append("cairn"))
        monkeypatch.setattr(workflow, "backend", lambda ctx: backend)
        monkeypatch.setattr(
            workflow, "bootstrap", lambda *args: pytest.fail("no bootstrap")
        )
        monkeypatch.setattr(workflow, "ingest", lambda *args: pytest.fail("no writes"))
        monkeypatch.setattr(index, "prepare", lambda: pytest.fail("no setup"))

        def command(argv: list[str], **kwargs: Any) -> str:
            assert argv == ["docker", "start", index.container]
            assert kwargs.get("allowed", (0,)) == (0,)
            if fault == "start_failed":
                raise InstallError("sidecar start failed")
            calls.append("falkordb")
            return index.container

        monkeypatch.setattr(ctx, "command", command)
        monkeypatch.setattr(workflow, "ready", lambda ctx: calls.append("identity"))
        monkeypatch.setattr(
            workflow, "verify_reads", lambda *args: calls.append("reads")
        )
        if fault:
            with pytest.raises(InstallError):
                workflow.run_install(ctx)
            assert ctx.state["status"] == "failed"
            assert calls == []
        else:
            workflow.run_install(ctx)
            assert calls == ["falkordb", "cairn", "identity", "reads"]
            assert ctx.state["status"] == "verified"
