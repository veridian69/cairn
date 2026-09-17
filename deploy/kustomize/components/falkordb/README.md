# The per-instance retrieval index

The FalkorDB workload I-91 makes optional and P-63 pins: one replica, one
PVC, one ClusterIP Service on 6379, authenticated, and reachable only
from its own namespace's Cairn pod.

Include it from an overlay:

```yaml
components:
  - ../../components/falkordb
```

An overlay that includes it must also do three things the component
deliberately does not, because each is a fact about the overlay rather
than about the index. The kind overlay is the worked example of all
three.

1. **Turn the index on.** `graphiti.enabled` lives in the configuration
   document, which the overlay restates:

   ```yaml
   graphiti:
     enabled: true
     host: falkordb
     port: 6379
   ```

2. **Supply a UID the image does not name, and `runAsGroup: 0` with it.**
   The pinned image declares no `USER`, so the component's
   `runAsNonRoot: true` cannot be satisfied by the image alone. OpenShift
   assigns one from the namespace range with gid 0 and needs nothing;
   anywhere else the overlay patches `runAsUser` **and `runAsGroup: 0`**
   (and, where the provisioner does not hand the volume to the pod,
   `fsGroup`) onto the pod.

   Both halves matter, and the UID is the half that is easy to get wrong.
   With `runAsUser` set and `runAsGroup` unset the runtime resolves the
   primary group from the image's own passwd file, and this image names
   two UIDs an operator might plausibly pick: 999 is `redis` in group 999,
   and 1001 is `nextjs` in group 1001. The Secret below is `root:root` at
   0440, so either one leaves the index unable to open its own
   configuration, crash-looping on `Permission denied`. Choose a UID with
   no passwd entry — the kind overlay uses 10001, which resolves to gid 0
   on its own — and state `runAsGroup: 0` anyway, so that a later UID
   change cannot quietly reintroduce the fault. Group 0 is the
   arbitrary-UID convention this repository already keeps everywhere else.

   Without a UID at all the Pod fails admission with
   `container has runAsNonRoot and image will run as root` — a loud
   failure with an obvious remedy, which is why the component prefers it
   to a fixed UID that would break the OpenShift assignment.

3. **Reach the provider.** The index is half of retrieval; the other half
   is the model provider, reached through the cluster's egress gateway
   (`../../overlays/egress-gateway`). The overlay sets `HTTPS_PROXY` on
   the Cairn container and permits the egress.

## The password

Both consumers read it as a file from the `cairn-credentials` Secret, and
neither takes it from an environment variable or a command line. Two keys,
written from one generated secret:

| Key | Read by | Contents |
| --- | --- | --- |
| `falkordb-password` | Cairn (I-92) | the bare token |
| `falkordb.conf` | this index | `requirepass <the same token>` |

`REDIS_ARGS` names the projected path rather than the credential. The
image's entrypoint expands it unquoted ahead of every other argument, and
`redis-server` reads its first argument as a configuration file — so the
file *is* the credential channel, and the amended premise (that the image
offers no file-based path) is simply not true of redis.

```sh
umask 077
token="$(openssl rand -hex 32)"
printf '%s\n' "$token" > falkordb-password
printf 'requirepass %s\n' "$token" > falkordb.conf
$EDITOR openai-api-key
kubectl create secret generic cairn-credentials --namespace <instance> \
  --from-file=falkordb-password \
  --from-file=falkordb.conf \
  --from-file=openai-api-key
```

`--from-file` rather than `--from-literal` throughout, and for this
change's own reason: a literal puts the credential on `kubectl`'s command
line, where the operator's shell history and anyone's `ps` can reach it.

The token no longer has to be shell-safe for redis's sake — nothing
expands it now — but keep it to one line: a configuration file's
directive ends at the newline.

## What the index is worth

It is derived state. The backup bundle excludes it on purpose (P-65),
losing it is a recovery rather than a data loss, and the runbook's
instruction is to delete the claim and run:

```sh
cairn rebuild-index --config /etc/cairn/config.yaml
```
