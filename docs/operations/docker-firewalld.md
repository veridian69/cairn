# Docker bridge connectivity and firewalld

Use this runbook when the Compose connectivity preflight fails. A successful
DNS lookup alone does not prove that containers can connect to one another.
If two disposable containers fail the same TCP check without Cairn or FalkorDB,
resolve the host networking problem before continuing installation. Preserve
existing configuration, credentials and volumes.

## Inspect the intended daemon

Run these read-only commands on the Docker host, using the same account and
Docker context as the installation. If the daemon's name, context or resources
do not match the intended installation, stop and resolve that mismatch first.

```sh
docker context show
docker info --format 'name={{.Name}} root={{.DockerRootDir}} containers={{.Containers}} images={{.Images}}'
docker ps --format '{{.Names}}\t{{.Status}}'
docker network ls --filter driver=bridge
systemctl is-active docker firewalld
```

If firewalld is active, an administrator uses its normal approved privilege
mechanism to inspect the integration. These commands use `sudo`; they require
permission, not merely the presence of the executable:

```sh
sudo firewall-cmd --state
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --zone=docker --list-all
sudo firewall-cmd --policy=docker-forwarding --list-all
sudo journalctl -u docker -u firewalld -b --no-pager -n 100
```

With Docker's firewall management enabled, its documented firewalld integration
creates a `docker` zone with target `ACCEPT`, assigns Docker-owned bridge
interfaces to that zone, and creates a `docker-forwarding` policy from `ANY`
to `docker`. An empty zone while Docker bridges exist is a discrepancy to
investigate, not a reason to disable the host firewall. See
[Docker's firewalld integration](https://docs.docker.com/engine/network/packet-filtering-firewalls/#integration-with-firewalld).

For a specific Compose network, substitute its name from `docker network ls`:

```sh
network='REPLACE_WITH_COMPOSE_NETWORK_NAME'
test "$network" != REPLACE_WITH_COMPOSE_NETWORK_NAME
test "$(docker network inspect "$network" --format '{{.Driver}}')" = bridge
bridge="$(docker network inspect "$network" --format '{{index .Options "com.docker.network.bridge.name"}}')"
if test -z "$bridge"; then
  network_id="$(docker network inspect "$network" --format '{{.Id}}')"
  bridge="br-$(printf '%s' "$network_id" | cut -c1-12)"
fi
ip link show dev "$bridge"
sudo firewall-cmd --get-zone-of-interface="$bridge"
```

Expect `docker` for a standard Docker-managed bridge. `no zone` or another zone
requires investigation. The default network normally uses `docker0`; the command
above honours Docker's explicit bridge-name option before deriving a bridge name.
An `admin-prohibited` rejection in packet tracing identifies a firewall refusal;
it does not establish why Docker failed to register the interface.

## Restore the managed integration

This is host administration, not a step the Cairn installer performs silently.
Review the Docker daemon's firewall settings and service logs first. Docker's
`iptables`/`ip6tables` settings must permit its normal firewall management; a
host deliberately using another firewall architecture needs its administrator's
supported integration procedure. Do not blindly overwrite `daemon.json`, switch
firewall backends, or discard an existing policy.

Once the intended daemon and configuration are confirmed, arrange a maintenance
window for a Docker daemon restart. It can interrupt every container on that
host, not just Cairn. Retain the output of `docker ps` and follow the owner's
workload restart procedure; do not run `compose down -v` or delete networks or
volumes as a repair shortcut.

With firewalld running, the administrator can ask Docker to reconstruct its
managed rules by restarting Docker:

```sh
set -eu
test "$(sudo firewall-cmd --state)" = running
# Keep firewalld enabled and active.
sudo systemctl restart docker
systemctl is-active docker firewalld
python3 <<'PYTHON'
import json
import subprocess


def output(*arguments):
    return subprocess.check_output(arguments, text=True).strip()


networks = output("docker", "network", "ls", "--filter", "driver=bridge", "-q").split()
if not networks:
    raise SystemExit("No Docker bridge networks to verify; inspect daemon configuration")
expected = set()
for network in json.loads(output("docker", "network", "inspect", *networks)):
    bridge = network.get("Options", {}).get("com.docker.network.bridge.name")
    bridge = bridge or "br-" + network["Id"][:12]
    subprocess.run(["ip", "link", "show", "dev", bridge], check=True, stdout=subprocess.DEVNULL)
    zone = output("sudo", "firewall-cmd", "--get-zone-of-interface=" + bridge)
    if zone != "docker":
        raise SystemExit(f"Docker bridge {bridge} is in zone {zone!r}, not docker")
    expected.add(bridge)
actual = set(output("sudo", "firewall-cmd", "--zone=docker", "--list-interfaces").split())
if actual != expected:
    raise SystemExit(f"docker zone interface mismatch: expected {sorted(expected)}, got {sorted(actual)}")
# --get-target is permanent-only; inspect the active target from --list-all.
for scope in ("--zone=docker", "--policy=docker-forwarding"):
    details = output("sudo", "firewall-cmd", scope, "--list-all")
    targets = [line.strip() for line in details.splitlines() if line.strip().startswith("target:")]
    if targets != ["target: ACCEPT"]:
        raise SystemExit("Unexpected active Docker zone/policy target")
for arguments, expected_value in [
    (("--zone=docker", "--list-sources"), ""),
    (("--policy=docker-forwarding", "--list-ingress-zones"), "ANY"),
    (("--policy=docker-forwarding", "--list-egress-zones"), "docker"),
]:
    if output("sudo", "firewall-cmd", *arguments) != expected_value:
        raise SystemExit("Unexpected Docker-managed zone or forwarding policy; inspect before proceeding")
print("PASS: only Docker bridges occupy the docker zone; forwarding policy matches")
PYTHON
```

Both services must remain active. The gate requires every existing Docker bridge
in the Docker zone, no unrelated interface or source range in that ACCEPT zone,
and the expected forwarding policy. Then rerun the [container-to-container preflight](../../deploy/compose/README.md#container-to-container-connectivity-preflight).
The preflight creates a fresh bridge, so it also checks whether Docker registers
new networks correctly. If integration is still absent, stop and retain the
logs for the Docker/firewalld administrator; repeated restarts are not a diagnosis.

Do not disable firewalld, move physical interfaces into a trusted zone, set a
host-wide forwarding policy to ACCEPT, or add broad forwarding exceptions to
make the probe pass. Restore the Docker-managed integration and verify the
existing host policy remains in force. A passing probe verifies the current
container path; separately verify integration after the next planned reboot or
firewall reload before calling lifecycle persistence accepted.
