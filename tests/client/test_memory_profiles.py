"""Explicit connection profiles contain configuration, never memory or tokens."""

import importlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest


def api() -> Any:
    assert importlib.util.find_spec("cairn.client.profiles") is not None, (
        "Memory profiles are missing"
    )
    return importlib.import_module("cairn.client.profiles")


def document() -> dict[str, Any]:
    return {
        "schema": "cairn.memory-profile/v1",
        "endpoint": "https://cairn.invalid",
        "expected_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "scope": {
            "realm": "synthetic",
            "segments": [{"kind": "job", "identifier": "quality"}],
        },
        "classification": "internal",
        "credential_file": "credentials/token",
    }


def write_profile(root: Path, value: dict[str, Any]) -> Path:
    path = root / "profile.json"
    path.write_text(json.dumps(value))
    return path


def token() -> str:
    return "cairn1.bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb." + "a" * 43


def test_profile_is_immutable_and_resolves_credentials_relative_to_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = api()
    path = write_profile(tmp_path, document())
    before = path.read_bytes()
    monkeypatch.chdir("/")
    profile = module.load_profile(path)
    assert profile.endpoint == "https://cairn.invalid"
    assert profile.expected_instance_id == UUID(document()["expected_instance_id"])
    assert profile.scope.realm == "synthetic"
    assert profile.scope.segments[0].identifier == "quality"
    assert profile.classification.value == "internal"
    assert profile.credential_file == tmp_path / "credentials/token"
    assert profile.session_id is None
    assert "synthetic" not in repr(profile)
    with pytest.raises(FrozenInstanceError):
        profile.endpoint = "https://changed.invalid"
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]  # Loading never creates a cache.


def test_explicit_absolute_credential_and_uuid5_session_are_configuration(
    tmp_path: Path,
) -> None:
    module = api()
    value = document()
    value["credential_file"] = str(tmp_path / "selected-token")
    value["session_id"] = str(
        uuid5(UUID(value["expected_instance_id"]), "synthetic-session")
    )
    profile = module.load_profile(write_profile(tmp_path, value))
    assert profile.session_id == UUID(value["session_id"])
    assert profile.credential_file == tmp_path / "selected-token"


def test_relative_profile_keeps_its_original_directory_if_cwd_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = api()
    write_profile(tmp_path, document())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    original = module._read_regular

    def read_then_change_directory(*args: Any, **kwargs: Any) -> bytes:
        result = original(*args, **kwargs)
        monkeypatch.chdir(elsewhere)
        assert isinstance(result, bytes)
        return result

    monkeypatch.setattr(module, "_read_regular", read_then_change_directory)
    profile = module.load_profile(Path("profile.json"))
    assert profile.credential_file == tmp_path / "credentials/token"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://cairn.invalid",
        "http://localhost:8000",
        "ftp://127.0.0.1",
        "/relative",
        "https://user:SECRET@cairn.invalid",
        "https://@cairn.invalid",
        "https://cairn.invalid/path",
        "https://cairn.invalid?token=SECRET",
        "https://cairn.invalid#SECRET",
        "https://cairn.invalid\n",
        "",
    ],
)
def test_unsafe_or_ignored_endpoint_parts_are_rejected(
    tmp_path: Path, endpoint: str
) -> None:
    module = api()
    value = document()
    value["endpoint"] = endpoint
    with pytest.raises(module.ProfileError) as caught:
        module.load_profile(write_profile(tmp_path, value))
    assert str(caught.value) == "invalid_profile"
    assert "SECRET" not in repr(caught.value)


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8000", "http://[::1]:8000/", "https://cairn.invalid:8443/"],
)
def test_numeric_loopback_and_https_follow_existing_client_policy(
    tmp_path: Path, endpoint: str
) -> None:
    value = document()
    value["endpoint"] = endpoint
    assert api().load_profile(write_profile(tmp_path, value)).endpoint == endpoint


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema="unknown"),
        lambda d: d.update(token="SECRET"),
        lambda d: d.update(expected_instance_id="not-a-uuid"),
        lambda d: d.update(expected_instance_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        lambda d: d.update(classification="secret"),
        lambda d: d.update(session_id=None),
        lambda d: d.update(credential_file=""),
        lambda d: d.update(credential_file="${SECRET_FILE}"),
        lambda d: d.update(credential_file="~/token"),
        lambda d: d["scope"].update(extra="SECRET"),
        lambda d: d["scope"]["segments"][0].update(identifier=3),
        lambda d: d.pop("scope"),
    ],
)
def test_strict_profile_schema_refuses_implicit_or_malformed_configuration(
    tmp_path: Path, change: Any
) -> None:
    module = api()
    value = document()
    change(value)
    with pytest.raises(module.ProfileError, match="^invalid_profile$"):
        module.load_profile(write_profile(tmp_path, value))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"one","schema":"two"}',
        b"\xff",
        b"[]",
        b'{"secret":NaN}',
        b"[" * 2000,
        b" " * 32769,
    ],
)
def test_profile_decode_is_bounded_strict_and_content_free(
    tmp_path: Path, raw: bytes
) -> None:
    module = api()
    path = tmp_path / "profile.json"
    path.write_bytes(raw)
    with pytest.raises(module.ProfileError) as caught:
        module.load_profile(path)
    assert str(caught.value) in {"invalid_profile", "profile_too_large"}


def test_credential_is_loaded_only_explicitly_and_never_stored_in_profile(
    tmp_path: Path,
) -> None:
    module = api()
    profile = module.load_profile(write_profile(tmp_path, document()))
    profile.credential_file.parent.mkdir()
    profile.credential_file.write_text(token() + "\n")
    assert module.load_credential(profile) == token()
    assert token() not in repr(profile)
    assert not hasattr(profile, "token")


@pytest.mark.parametrize(
    "raw", [b"", b"SECRET", b"x" * 513, b"\xff", b"cairn1.invalid\nheader: injected"]
)
def test_bad_credentials_produce_only_safe_codes(tmp_path: Path, raw: bytes) -> None:
    module = api()
    value = document()
    value["credential_file"] = "token"
    profile = module.load_profile(write_profile(tmp_path, value))
    profile.credential_file.write_bytes(raw)
    with pytest.raises(module.ProfileError) as caught:
        module.load_credential(profile)
    assert str(caught.value) in {"invalid_credential", "credential_too_large"}
    assert "SECRET" not in repr(caught.value)


def test_symlink_profile_and_credential_paths_are_not_followed(tmp_path: Path) -> None:
    module = api()
    path = write_profile(tmp_path, document())
    link = tmp_path / "linked-profile.json"
    link.symlink_to(path)
    with pytest.raises(module.ProfileError, match="profile_unavailable"):
        module.load_profile(link)
    actual = tmp_path / "actual-credentials"
    actual.mkdir()
    (actual / "token").write_text(token())
    (tmp_path / "credentials").symlink_to(actual, target_is_directory=True)
    profile = module.load_profile(path)
    with pytest.raises(module.ProfileError, match="credential_unavailable"):
        module.load_credential(profile)


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_parent_components_do_not_erase_invalid_profile_traversal(
    tmp_path: Path, kind: str
) -> None:
    module = api()
    write_profile(tmp_path, document())
    child = tmp_path / "child"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        child.symlink_to(target, target_is_directory=True)
    else:
        child.write_text("not a directory")
    with pytest.raises(module.ProfileError, match="profile_unavailable"):
        module.load_profile(child / ".." / "profile.json")


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_parent_components_do_not_erase_invalid_credential_traversal(
    tmp_path: Path, kind: str
) -> None:
    module = api()
    value = document()
    value["credential_file"] = "child/../token"
    profile = module.load_profile(write_profile(tmp_path, value))
    (tmp_path / "token").write_text(token())
    child = tmp_path / "child"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        child.symlink_to(target, target_is_directory=True)
    else:
        child.write_text("not a directory")
    with pytest.raises(module.ProfileError, match="credential_unavailable"):
        module.load_credential(profile)


def test_real_parent_directory_reference_remains_supported(tmp_path: Path) -> None:
    module = api()
    nested = tmp_path / "nested"
    nested.mkdir()
    value = document()
    value["credential_file"] = "../token"
    (tmp_path / "token").write_text(token())
    profile = module.load_profile(write_profile(nested, value))
    assert module.load_credential(profile) == token()


def test_missing_profile_is_not_replaced_by_ambient_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = api()
    monkeypatch.setenv("CAIRN_TOKEN", "SECRET")
    with pytest.raises(module.ProfileError, match="profile_unavailable"):
        module.load_profile(tmp_path / "missing.json")
