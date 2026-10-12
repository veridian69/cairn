import importlib.util
import pathlib
import uuid
from collections.abc import Callable
from typing import Any

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cutover_principals.py"
)
spec = importlib.util.spec_from_file_location("cutover_principals", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_plan_is_deterministic_and_candidate_only() -> None:
    first = module.plan("cairn", ["claude-ai"], "2026-12-31T23:59:59Z")
    second = module.plan("cairn", ["claude-ai"], "2026-12-31T23:59:59Z")
    assert first == second
    steps = {step["operation"]: step for step in first}
    assert set(steps) == {"create-principal", "create-grant", "issue-credential"}
    principal = steps["create-principal"]["body"]
    assert principal == {"realm_id": "cairn", "kind": "workload", "label": "claude-ai"}
    grant = steps["create-grant"]["body"]["grant"]
    assert grant["operations"] == ["ingest", "retrieve"]
    assert grant["read_clearance"] == "internal"
    assert grant["write_classifications"] == ["internal"]
    assert grant["segments"] == []
    assert grant["expires_at"] == "2026-12-31T23:59:59Z"
    assert steps["issue-credential"]["body"]["expires_at"] == "2026-12-31T23:59:59Z"
    for step in first:
        assert uuid.UUID(step["idempotency_key"]).version == 5


def test_plan_orders_labels_and_keys_differ_per_label() -> None:
    steps = module.plan("cairn", ["spike-wsl", "val-wsl"], "2026-12-31T23:59:59Z")
    assert [s["label"] for s in steps] == ["spike-wsl"] * 3 + ["val-wsl"] * 3
    assert len({s["idempotency_key"] for s in steps}) == 6


def test_labels_are_required(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SystemExit):
        run(tmp_path)
    assert not (tmp_path / "out").exists()


# ---- main() behaviour through httpx.MockTransport ----
import json  # noqa: E402
import stat  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

TOKEN = "cairn1.OPERATOR-TOKEN-DISTINCTIVE"
EXPIRES = "2026-12-31T23:59:59Z"


def credential_id(pid: str) -> str:
    return str(uuid.UUID(bytes=uuid.uuid5(uuid.NAMESPACE_OID, pid).bytes, version=4))


def issued(pid: str) -> str:
    return f"cairn1.{credential_id(pid)}.SECRET{'x' * 37}"


def handler(issue_plaintext: bool = True) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/instance":
            accepted = {f"Bearer {issued('p-zen')}"}
            ok = request.headers["authorization"] in accepted
            return httpx.Response(200 if ok else 401, json={})
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        body = json.loads(request.content)
        op = request.url.path.rsplit("/", 1)[1]
        result: dict[str, Any]
        if op == "create-principal":
            result = {"principal_id": f"p-{body['label']}"}
        elif op == "create-grant":
            result = {"grant_id": f"g-{body['grant']['principal_id']}"}
        else:
            pid = body["principal_id"]
            result = {
                "credential_id": credential_id(pid),
                "plaintext": issued(pid) if issue_plaintext else None,
            }
        return httpx.Response(200, json={"outcome": "committed", "result": result})

    return respond


def run(
    tmp_path: pathlib.Path,
    *extra: str,
    issue_plaintext: bool = True,
    base: str = "https://cairn.example",
    cred: pathlib.Path | None = None,
) -> int:
    if cred is None:
        cred = tmp_path / "operator"
        cred.write_text(TOKEN + "\n")
    argv = [
        "--base-url",
        base,
        "--realm",
        "r",
        "--credential-file",
        str(cred),
        "--output-dir",
        str(tmp_path / "out"),
        "--expires-at",
        EXPIRES,
        *extra,
    ]
    result: int = module.main(
        argv, transport=httpx.MockTransport(handler(issue_plaintext))
    )
    return result


def test_fresh_run_writes_private_files_and_prints_no_secret(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = ["claude-ai", "chatgpt"]
    assert run(tmp_path, "--label", labels[0], "--label", labels[1]) == 0
    captured = capsys.readouterr()
    for label in labels:
        path = tmp_path / "out" / label
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text() == issued(f"p-{label}") + "\n"
    for text in (captured.out, captured.err):
        assert TOKEN not in text
        assert "SECRET" not in text


def test_symlink_target_is_refused_and_untouched(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (out / "spike-wsl").symlink_to(victim)
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, "--label", "spike-wsl")
    assert "spike-wsl" in str(exc.value)
    assert victim.read_text() == "keep"


def test_symlink_target_is_refused_before_any_request(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (out / "zen").symlink_to(victim)
    cred = tmp_path / "operator"
    cred.write_text(TOKEN + "\n")
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    argv = [
        "--base-url",
        "https://cairn.example",
        "--realm",
        "r",
        "--credential-file",
        str(cred),
        "--output-dir",
        str(out),
        "--expires-at",
        EXPIRES,
        "--label",
        "spike-wsl",
        "--label",
        "zen",
    ]
    with pytest.raises(SystemExit) as exc:
        module.main(argv, transport=httpx.MockTransport(record))
    assert exc.value.code != 0 and "zen" in str(exc.value)
    assert requests == []
    assert victim.read_text() == "keep"


def test_overwrite_is_reported(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "zen").write_text("old")
    assert run(tmp_path, "--label", "zen") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["overwritten"] is True


def test_failed_write_keeps_the_old_credential(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "zen"
    target.write_text("old\n")

    def fail(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError):
        module.write_credential(target, "zen", "new")
    assert target.read_text() == "old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["zen"]


def test_replay_without_file_asks_for_reissue(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "--label", "zen", issue_plaintext=False) == 1
    assert "--reissue <next number>" in capsys.readouterr().err


def test_replay_with_file_keeps_it(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "zen").write_text(issued("p-zen") + "\n")
    assert run(tmp_path, "--label", "zen", issue_plaintext=False) == 0
    assert (out / "zen").read_text() == issued("p-zen") + "\n"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(issued("p-old").encode(), id="another-credential"),
        pytest.param(issued("p-zen")[:-1].encode() + b"y", id="same-id-wrong-secret"),
        pytest.param(issued("p-zen")[:-5].encode(), id="truncated-secret"),
        pytest.param(b"\xff\xfe", id="not-utf-8"),
        pytest.param(b"", id="empty"),
    ],
)
def test_replay_with_unusable_file_asks_for_reissue(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], content: bytes
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "zen").write_bytes(content)
    assert run(tmp_path, "--label", "zen", issue_plaintext=False) == 1
    assert "--reissue <next number>" in capsys.readouterr().err
    assert (out / "zen").read_bytes() == content


def test_reissue_changes_only_the_credential_key() -> None:
    base = module.plan("r", ["zen"], EXPIRES)
    again = module.plan("r", ["zen"], EXPIRES, reissue=2)
    changed = [
        a["operation"]
        for a, b in zip(base, again, strict=True)
        if a["idempotency_key"] != b["idempotency_key"]
    ]
    assert changed == ["issue-credential"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user@host",
        "https://host/?q=1",
        "https://host/#f",
        "ftp://host",
    ],
)
def test_bad_base_url_is_refused_before_reading_credential(
    tmp_path: pathlib.Path, url: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, "--label", "zen", base=url, cred=tmp_path / "missing")
    assert "--base-url" in str(exc.value)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000", "http://[::1]:8000", "https://cairn.example"]
)
def test_good_base_urls_accepted(url: str) -> None:
    module.check_base_url(url)
