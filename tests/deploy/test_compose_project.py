"""Slice 8 task 6: the guards the Compose project must satisfy.

`docker compose config -q` in `make check` proves the project parses,
interpolates and validates. It says nothing about whether the project
still has the production posture P-69 pins, which is what this module
holds — the same division of labour as the render gate and
``test_rendered_manifests.py`` beside it.

Four claims carry the weight:

- **I-12's loopback rule.** The published address is not a parameter.
  Publication beyond this host is a reverse proxy an operator puts there
  deliberately, and no `.env` value can turn it on by accident.
- **P-61's single pin.** The images come from ``deploy/images.lock``
  through interpolation, and the committed example sets neither, so the
  pin cannot be changed in one consumer and forgotten in another.
- **I-91's absence.** Without the retrieval overlay the project has no
  index service, no index password and no proxy variable at all.
- **I-08's silence.** No committed file in the project carries a
  credential — the example deliberately leaves the index password unset,
  so a copied example cannot become a running index's password.

The sources are parsed rather than `docker compose config` being run:
the tests must mean the same thing on a host with no Docker, and the
interpolation this module asserts about is precisely what `config`
resolves away.

Module-local helpers for the reason the other test modules record:
``tests`` has no package markers, so no ``conftest.py`` can be shared.
"""

from pathlib import Path
from typing import Any

import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPOSITORY / "deploy" / "compose"
IMAGES_LOCK = REPOSITORY / "deploy" / "images.lock"
BASE_CONFIGMAP = REPOSITORY / "deploy" / "kustomize" / "base" / "configmap.yaml"

CAIRN_DATA_PATH = "/var/lib/cairn"
CONFIG_PATH = "/etc/cairn/config.yaml"
CREDENTIALS_PATH = "/var/run/secrets/cairn"
FALKORDB_DATA_PATH = "/var/lib/falkordb/data"
# The same identity the kind overlay patches, and for the same reason:
# a UID the pinned image's own /etc/passwd does not name, so that the
# primary group can never be resolved out of it, plus an explicit group
# 0. See the rationale in compose.graphiti.yaml.
FALKORDB_UID = "10001:0"


def _project(*names: str) -> dict[str, Any]:
    """One composition, merged the way ``-f a -f b`` merges it.

    Only the keys this project actually overlays are merged, and they are
    merged per service, which is all `compose.graphiti.yaml` needs: it
    adds services and adds one key to an existing one.
    """
    merged: dict[str, Any] = {"services": {}, "volumes": {}}
    for name in names:
        document = yaml.safe_load((COMPOSE_DIR / name).read_text(encoding="utf-8"))
        for service, definition in document.get("services", {}).items():
            merged["services"].setdefault(service, {}).update(definition)
        merged["volumes"].update(document.get("volumes") or {})
    return merged


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key] = value
    return values


def _mount(service: dict[str, Any], target: str) -> dict[str, Any]:
    matches = [mount for mount in service["volumes"] if mount["target"] == target]
    assert len(matches) == 1, f"expected exactly one mount at {target}"
    mount: dict[str, Any] = matches[0]
    return mount


BASE = _project("compose.yaml")
GRAPHITI = _project("compose.yaml", "compose.graphiti.yaml")


def test_the_project_publishes_only_on_loopback() -> None:
    """I-12 made structural rather than documented.

    The host address is a literal and only the port is a parameter, so
    the one thing an operator can get wrong here is which loopback port
    an instance answers on.
    """
    published = BASE["services"]["cairn"]["ports"]
    assert len(published) == 1
    # Not split on ":" — the mandatory-variable syntax carries one of its
    # own, which is exactly the shape being asserted.
    assert published[0].startswith("127.0.0.1:${CAIRN_HOST_PORT:?")
    assert published[0].endswith(":8000")


def test_no_other_service_publishes_anything() -> None:
    """The index is reachable on the project network and nowhere else."""
    for name, service in GRAPHITI["services"].items():
        if name == "cairn":
            continue
        assert "ports" not in service, f"{name} publishes a port"


def test_images_come_from_the_pin_and_the_example_cannot_shadow_it() -> None:
    """P-61: one place a version changes, and the project reads it.

    Both halves are needed. Interpolation alone would still permit a
    second copy in ``.env.example`` that quietly wins, because Compose
    resolves the later ``--env-file`` first.
    """
    pins = _env_file(IMAGES_LOCK)
    example = _env_file(COMPOSE_DIR / ".env.example")
    for service, variable in (
        ("cairn", "CAIRN_IMAGE"),
        ("falkordb", "FALKORDB_IMAGE"),
        ("falkordb-init", "FALKORDB_IMAGE"),
    ):
        image = GRAPHITI["services"][service]["image"]
        assert image.startswith(f"${{{variable}:?"), (
            f"{service} does not take its image from {variable}"
        )
        assert variable in pins, f"{variable} is not pinned in deploy/images.lock"
        assert variable not in example, f".env.example shadows the {variable} pin"


def test_the_base_project_carries_no_index_and_no_proxy_variable() -> None:
    """I-91's absence, in the Compose shape.

    A stray index service would be an unauthenticated database nobody
    asked for; a stray proxy variable would route an instance's outbound
    traffic through a gateway that does not exist on a single host.
    """
    assert set(BASE["services"]) == {"cairn"}
    assert set(BASE["volumes"]) == {"cairn-data"}
    environment = BASE["services"]["cairn"].get("environment", {})
    assert not [name for name in environment if "PROXY" in name.upper()]


def test_no_service_carries_a_credential_in_its_environment() -> None:
    """I-08 and I-19 applied to the project, after Operator's ruling of 13
    August 2026: every credential here is a file, including the index's.

    The index's password used to be interpolated into `REDIS_ARGS` and
    `REDISCLI_AUTH`, because P-63 held that the image offered no
    file-based path. It does — redis reads its first argument as a
    configuration file — so nothing in this project puts a credential in
    an environment variable, in `.env`, or on a command line, and this
    test is what keeps one from creeping back.
    """
    example = _env_file(COMPOSE_DIR / ".env.example")
    assert "FALKORDB_PASSWORD" not in example
    for name, service in GRAPHITI["services"].items():
        for variable, value in service.get("environment", {}).items():
            assert "PASSWORD" not in variable.upper(), (
                f"{name} carries a password in its environment"
            )
            assert "PASSWORD" not in str(value).upper(), (
                f"{name} interpolates a password into {variable}"
            )
    # And the credential's own file is named, mounted read-only, and
    # refused rather than invented when it is missing.
    mount = _mount(GRAPHITI["services"]["falkordb"], "/etc/falkordb/cairn.conf")
    assert mount["type"] == "bind"
    assert mount["read_only"] is True
    assert mount["source"].startswith("${FALKORDB_CONFIG_FILE:?")
    assert mount["bind"] == {"create_host_path": False}


def test_the_cairn_service_keeps_the_production_posture() -> None:
    """P-69's posture, expressed in the project rather than in prose."""
    cairn = BASE["services"]["cairn"]
    assert cairn["read_only"] is True
    assert cairn["cap_drop"] == ["ALL"]
    assert cairn["security_opt"] == ["no-new-privileges:true"]
    # I-20's termination contract, and the base's `restart` posture: a
    # crashed instance returns, a stopped one stays stopped.
    assert cairn["stop_grace_period"] == "60s"
    assert cairn["restart"] == "unless-stopped"
    # Bounded, so a runaway cannot consume the host's memory.
    assert cairn["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=256m"]
    # No `user`: the image declares 65532:0 and owns its data directory,
    # which is also what makes a fresh named volume come out writable.
    assert "user" not in cairn
    assert "environment" not in cairn


def test_the_cairn_service_mounts_configuration_and_credentials_read_only() -> None:
    """I-19: both arrive as read-only files, and the data volume is named."""
    cairn = BASE["services"]["cairn"]
    for target, variable in (
        (CONFIG_PATH, "${CAIRN_CONFIG_FILE:?"),
        (CREDENTIALS_PATH, "${CAIRN_CREDENTIALS_DIR:?"),
    ):
        mount = _mount(cairn, target)
        assert mount["type"] == "bind"
        assert mount["read_only"] is True
        assert mount["source"].startswith(variable)

    data = _mount(cairn, CAIRN_DATA_PATH)
    assert data["type"] == "volume"
    assert data["source"] == "cairn-data"
    assert data.get("read_only") is not True


def test_the_data_volumes_are_project_prefixed() -> None:
    """No explicit ``name``, so two instances cannot share one volume.

    An explicit name would be one more thing to change per instance, and
    forgetting it would put two instances on one catalogue — which the
    lease refuses, but far later than it needed to.
    """
    for volumes in (BASE["volumes"], GRAPHITI["volumes"]):
        for name, definition in volumes.items():
            assert definition in (None, {}), f"{name} names itself explicitly"


def test_the_cairn_healthcheck_uses_the_image_it_already_has() -> None:
    """P-69: the slim image ships no curl and grows none for a probe."""
    healthcheck = BASE["services"]["cairn"]["healthcheck"]
    assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
    assert "/health/ready" in healthcheck["test"][3]


def test_cairn_waits_for_a_healthy_index_when_retrieval_is_enabled() -> None:
    """Composition opens the index connection, so started is not enough."""
    assert GRAPHITI["services"]["cairn"]["depends_on"] == {
        "falkordb": {"condition": "service_healthy"}
    }
    assert GRAPHITI["services"]["falkordb"]["depends_on"] == {
        "falkordb-init": {"condition": "service_completed_successfully"}
    }


def test_the_index_service_keeps_the_component_s_posture() -> None:
    """The FalkorDB component's own posture, carried into Compose."""
    falkordb = GRAPHITI["services"]["falkordb"]
    assert falkordb["user"] == FALKORDB_UID
    assert falkordb["read_only"] is True
    assert falkordb["cap_drop"] == ["ALL"]
    assert "cap_add" not in falkordb
    assert falkordb["security_opt"] == ["no-new-privileges:true"]
    assert falkordb["stop_grace_period"] == "30s"
    # The console the image would otherwise start beside redis, and the
    # TLS mode that generates its own certificates.
    assert falkordb["environment"]["BROWSER"] == "0"
    assert falkordb["environment"]["TLS"] == "0"
    # P-82 gate-5 live evidence (25 August 2026): the retained graph's
    # relationship full-text read reproducibly exceeded the pinned image's
    # 1,000 ms timeout, while the exact replay completed in 1,207.9 ms under
    # a temporary 5,000 ms bound. Keep that measured bound mirrored in the
    # Kustomize component beside the declared queue and result-set controls.
    assert falkordb["environment"]["FALKORDB_ARGS"] == (
        "MAX_QUEUED_QUERIES 200 TIMEOUT 5000 RESULTSET_SIZE 10000"
    )
    # The healthcheck reads the credential out of the file the server
    # itself is configured from and hands it to redis-cli on stdin. Not
    # argv, and not an environment variable either — `REDISCLI_AUTH`
    # would have been one, and P-63 as amended admits no environment
    # variable on either target, transient child process or not.
    test = falkordb["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL"
    assert "AUTH" in test[-1]
    assert "/etc/falkordb/cairn.conf" in test[-1]
    assert "| redis-cli" in test[-1]
    assert "REDISCLI_AUTH" not in test[-1]
    # And the answer is compared, not the exit status. redis-cli exits 0
    # on `NOAUTH Authentication required.` as readily as on `PONG`, so a
    # status-only check would report an index healthy that is refusing
    # the credential it was handed — and Cairn, waiting on that check,
    # would start against an index it cannot use.
    assert test[-1].rstrip().endswith("grep -q '^PONG$$'")
    data = _mount(falkordb, FALKORDB_DATA_PATH)
    assert data["type"] == "volume"
    assert data["source"] == "falkordb-data"


def test_the_volume_one_shot_is_root_for_exactly_one_capability() -> None:
    """The Compose stand-in for an fsGroup, and its blast radius.

    It is the only root container in the project and the only one holding
    a capability. Both facts are asserted here so that neither can spread
    to a service that serves traffic.
    """
    one_shot = GRAPHITI["services"]["falkordb-init"]
    assert one_shot["user"] == "0:0"
    assert one_shot["cap_drop"] == ["ALL"]
    assert one_shot["cap_add"] == ["CHOWN"]
    assert one_shot["read_only"] is True
    # The one container that is root is the one that can reach nothing.
    assert one_shot["network_mode"] == "none"
    # A one-shot that comes back is a loop.
    assert one_shot["restart"] == "no"
    # Ownership only. A `chmod` would need CAP_FOWNER on the second run,
    # when the directory no longer belongs to root, and the one-shot
    # would fail on every `up` after the first.
    assert one_shot["entrypoint"][-1] == f"chown {FALKORDB_UID} {FALKORDB_DATA_PATH}"
    assert [
        name for name in GRAPHITI["services"] if "cap_add" in GRAPHITI["services"][name]
    ] == ["falkordb-init"]
    assert [
        name
        for name, service in GRAPHITI["services"].items()
        if service.get("user") == "0:0"
    ] == ["falkordb-init"]


def test_the_example_configuration_is_the_base_s_configuration() -> None:
    """One instance's configuration, whichever shape deploys it.

    The Compose example and the Kustomize base's ConfigMap are two copies
    of one document, and a divergence between them is a difference in
    what an operator gets from the two production targets — the kind that
    is only discovered when one of them misbehaves.
    """
    configmap = yaml.safe_load(BASE_CONFIGMAP.read_text(encoding="utf-8"))
    from_kubernetes = yaml.safe_load(configmap["data"]["config.yaml"])
    from_compose = yaml.safe_load(
        (COMPOSE_DIR / "config.example.yaml").read_text(encoding="utf-8")
    )
    assert from_compose == from_kubernetes
    # And the paths the project actually mounts, so a change to one of
    # them cannot pass by changing both copies of the document together.
    assert from_compose["paths"] == {
        "data": CAIRN_DATA_PATH,
        "credentials": CREDENTIALS_PATH,
    }
