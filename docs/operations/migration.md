# Legacy migration

The `cairn_migrate` tool moves supported offline legacy snapshots into a Cairn
v0.1 instance. Its five commands are `export`, `map`, `subset`, `apply` and
`verify`.

Migration artefacts contain source record bodies. Keep snapshots, bundles,
plans, enumerations and receipts in an access-controlled directory outside the
repository on a native Linux filesystem. Commit only aggregate, screened
reports when your information policy permits it.

## Safety boundary

- `export` reads offline snapshots. It does not fetch data from a remote legacy
  service.
- The restored FalkorDB endpoint must be an unauthenticated numeric loopback
  address. Remote hosts, DNS names and embedded credentials are refused.
- The legacy stores remain read-only. Graph reads use `GRAPH.RO_QUERY` against
  a disposable restore container.
- The migration client uses Cairn's `/v1` API. Server-side scope,
  classification, secret screening, audit and idempotency therefore apply.
- `apply` is the only write step. Run and inspect `export` and `map` before
  granting a migration principal access to a target.
- Pin the source revision, snapshot digests, restore-image digest, target
  `instance_id` and Cairn image digest in the operator's migration record. A
  local `cairn:v0.7.8` tag is not a published release image.

## Prepare offline snapshots

The exporter accepts this source set:

| Source | Input |
| --- | --- |
| FalkorDB graph | A dump restored into a disposable FalkorDB container |
| Attic transcripts | A read-only SQLite file |
| Ingest journal | A copy of the `journal/YYYY/MM/DD/<id>.json` tree |
| Thought files | Markdown files with JSON frontmatter |
| Openbrain export | One JSON record per line |

Record source counts and SHA-256 digests before export. Verify that the
snapshot procedure captured a consistent point in time.

### Restore FalkorDB

Use a FalkorDB image compatible with the image that wrote the dump and pin its
repository digest:

```sh
docker pull <image:tag>
docker inspect --format '{{index .RepoDigests 0}}' <image:tag>
```

The `falkordb/falkordb` image reads data from
`/var/lib/falkordb/data`. Mounting the dump at `/data` leaves the database
empty.

```sh
restore_dir=/tmp/cairn-restore
mkdir -p "$restore_dir"
tar xzf <falkor-dump>.tar.gz -C "$restore_dir"
docker run --rm --detach --name cairn-legacy-restore \
  --publish 127.0.0.1:6399:6379 \
  --volume "$restore_dir:/var/lib/falkordb/data" \
  <restore-image>@<restore-image-digest>

docker logs cairn-legacy-restore 2>&1 | grep -i -E 'loaded|error'
docker exec cairn-legacy-restore redis-cli GRAPH.LIST
```

Require the expected graph name before continuing.

## Export

```sh
uv run --locked python -m cairn_migrate export \
  --snapshot-label <label> \
  --graph-dump <falkor-dump>.tar.gz \
  --restore-image <restore-image> \
  --restore-image-digest <sha256:...> \
  --falkor-url redis://127.0.0.1:6399 \
  --graph-name cairn \
  --attic <secure>/transcripts.sqlite \
  --journal <secure>/journal \
  --thoughts <secure>/thoughts \
  --openbrain <secure>/openbrain-dump.jsonl \
  --output <secure>/bundles
```

The label must be one 1–128 character component containing ASCII letters,
digits, dots, underscores or hyphens. The exporter refuses output inside the
checkout and writes the manifest last. A directory without a manifest is an
incomplete export and must be inspected before removal and retry.

The bundle contains canonical JSONL per source store plus `manifest.json`.
Every permanent identity must be present, well-formed and unique. The manifest
binds source paths, counts, file digests and the image that read the graph
dump. Derived graph nodes and edges are counted rather than exported; Cairn
rebuilds its projection from migrated facts.

Re-exporting the same snapshots is byte-identical. A record is either mapped,
matched, reconciled or rejected; it is not silently filtered at read time.

After a successful export, stop the disposable container and dispose of the
restore directory according to the operator's retention policy:

```sh
docker rm --force cairn-legacy-restore
```

## Map and review

```sh
uv run --locked python -m cairn_migrate map \
  --bundle <secure>/bundles/cairn-legacy-export-<label> \
  --output <secure>/plans
```

`map` is offline and deterministic. It verifies every bundle file against the
manifest before producing:

```text
cairn-migration-plan-<label>/
├── operations.jsonl
├── rejections.jsonl
├── reconciliations.jsonl
├── enumerations.json
├── manifest.json
└── report.json
```

`operations.jsonl` and `enumerations.json` contain legacy values and must stay
with the protected migration material. `report.json` contains counts, digests,
rule tallies and legacy identifiers suitable for operator review after
screening.

Before any write, verify:

- `balance.balanced` is true and every source record is accounted for;
- the observed actor and source enumerations are understood as provenance;
- every reconciliation is understood;
- rejection counts and rules are acceptable;
- any invalid or expired derived relationship has an explicit carry-over
  decision; and
- projected workload volume is acceptable for the configured provider.

The mapping profile is specific to the supported legacy data shape. Inspect
its fixed rules and generated operations rather than assuming they fit another
system that happens to use FalkorDB.

Free-form legacy `actor` values never establish human authorship or validated
trust. A graph episode maps to `human`/`validated` only when an approved
repository marker provides explicit capture evidence. All other graph
episodes map to `agent-claim`/`candidate` and require an explicit later
promotion if an operator validates them.

### Generate a projection subset

Use `subset` when projection-enabled validation should operate on the defined
sample rather than the full plan:

```sh
uv run --locked python -m cairn_migrate subset \
  --source-plan <secure>/plans/cairn-migration-plan-<label> \
  --output <secure>/plans \
  --expected-source-manifest-sha256 <manifest-sha256>
```

The output is a materialised, digest-bound plan. Do not hand-edit a plan; its
manifest would no longer match. `apply` and `verify` consume a subset exactly
as they consume a full plan.

## Create short-lived migration authority

Use the normal `/v1` authority API to create a workload principal and issue a
credential. Create two expiring realm-root grants:

1. a data grant permitting `retrieve`, `ingest`, `promote` and `invalidate`,
   with the read clearance and write classification required by the plan; and
2. an audit-read grant held by the workload principal, so `verify` can inspect
   the audit chain.

Validated ingest requires both an evidence payload and `promote` authority.
Revoke both grants and the credential when verification and operator
acceptance are complete.

## Apply

`apply` writes the plan in order:

```sh
uv run --locked python -m cairn_migrate apply \
  --plan <secure>/plans/cairn-migration-plan-<label> \
  --receipts <secure>/receipts/<name>.jsonl \
  --instance https://cairn.example \
  --expected-instance <instance-uuidv4> \
  --credential-file <secure>/credential
```

The target, expected identity and credential file are mandatory. Remote
targets require HTTPS; only a numeric loopback target may use HTTP. URLs with
credentials, paths or queries are refused. The tool reads `/v1/instance`
before its first write and stops if the UUID does not match.

The plan is verified against its manifest before use. After each authoritative
response, `apply` appends an identity-only, instance-bound receipt before
sending the next operation. To resume an interruption, rerun the same command
with the same plan and receipt file. Existing receipts are skipped and Cairn's
idempotency keys turn a duplicate send into a replay.

## Verify

```sh
uv run --locked python -m cairn_migrate verify \
  --plan <secure>/plans/cairn-migration-plan-<label> \
  --receipts <secure>/receipts/<name>.jsonl \
  --instance https://cairn.example \
  --expected-instance <instance-uuidv4> \
  --credential-file <secure>/credential \
  --sample-size 25
```

Verification checks:

- a deterministic sample through `/v1/retrieve` against the plan;
- replays under the original idempotency keys, requiring byte-identical
  receipts and `outcome: replayed`; and
- the audit chain for every receipt, including the replay events.

Use `--skip-retrieval` only for a deliberate projection-disabled custody run;
replay and audit checks still run. A missing receipt, target mismatch,
retrieval mismatch, failed replay or audit mismatch is a failed migration.

For projection-enabled migration, validate on a fresh instance. Enabling
projection after records were ingested with projection disabled does not
queue the old records; use the supported index-rebuild procedure if the full
corpus must be projected later.

## Rollback

Keep the legacy system unchanged until the migrated instance has passed
verification and the operator has accepted the cut-over. Before migration,
take and export a Cairn backup as described in
[backup and restore](backup-restore.md).

Rollback restores that backup into a fresh target. Do not downgrade a
catalogue or overwrite a serving data volume. Preserve the migration plan,
receipts and screened report until the retention period ends, then revoke the
migration credential and dispose of sensitive artefacts securely.
