# Verify an Attic payload round-trip

Run this from the repository root after enabling Attic, starting Cairn and
checking the authenticated instance identity. Use the installation guide's
`base_url` and `credential_file`, then run the client guide's
[Credentials setup](../clients.md#credentials) in the same Bash shell. That
creates an owner-only `curl_config` without placing the token in arguments.
The prerequisites are Python 3.12–3.14 as `python3`, curl 8.4.0 or newer and jq.
For native installations, Cairn's managed runtime Python can run the helper.
For Kubernetes, keep the documented loopback port-forward running.

This check needs Attic and a live `ingest`/`retrieve` grant at
`local/repository:example` with `internal` write classification and read
clearance. The installation's bootstrap administrator has that authority.
Semantic retrieval, FalkorDB and provider credentials are **not required**.
The synthetic evidence remains in custody; these commands do not delete data.

## Commit one known payload

The paths and synthetic content below are literal. Only the endpoint and
credential location come from your installation. Keep the printed verification
directory: it contains the original bytes, request, key, response and evidence
ID, allowing read verification after a restart without another ingest.

```bash
set -eu
umask 077
install -d -m 0700 "$HOME/.local/state/cairn-checks"
evidence_check_dir="$(mktemp -d "$HOME/.local/state/cairn-checks/evidence.XXXXXXXX")"
printf 'Retained evidence verification files: %s\n' "$evidence_check_dir"
printf 'Cairn Attic check: café.\nExact second line.\n' > "$evidence_check_dir/payload.txt"
python3 -c 'import uuid; print(uuid.uuid4())' > "$evidence_check_dir/idempotency-key"
jq -n --rawfile payload "$evidence_check_dir/payload.txt" '{
  scope: {
    realm: "local",
    segments: [{kind: "repository", identifier: "example"}]
  },
  classification: "internal",
  source_type: "human",
  facts: [{body: "The installation has submitted a synthetic Attic payload."}],
  evidence_payload: $payload
}' > "$evidence_check_dir/ingest.json"
```

Send the retained request once. If the transport fails, its commit status is
uncertain: keep the same directory, payload and key and repeat only this next
block. Do not generate a new key to retry an uncertain write. An HTTP failure
is printed in full and stops the block; diagnose its code before retrying.

```bash
if http_status="$(curl --disable --silent --show-error --noproxy '*' \
  --max-time 30 --request POST --config "$curl_config" \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $(cat "$evidence_check_dir/idempotency-key")" \
  --data-binary "@$evidence_check_dir/ingest.json" \
  --output "$evidence_check_dir/ingest-response.json" \
  --dump-header "$evidence_check_dir/ingest-headers" --write-out '%{http_code}' \
  "$base_url/v1/ingest")"; then
  printf 'Ingest HTTP %s\n' "$http_status"
  cat "$evidence_check_dir/ingest-response.json"
  printf '\n'
else
  printf 'Commit uncertain; retain the same request and key in %s.\n' "$evidence_check_dir" >&2
  exit 1
fi
test "$http_status" = 200
jq -e '
  (.outcome == "committed" or .outcome == "replayed") and
  (.mutation_receipt | type == "object") and
  (.audit_receipt | type == "object") and
  (.result.evidence_id | type == "string")
' "$evidence_check_dir/ingest-response.json" >/dev/null
jq -er '.result.evidence_id' "$evidence_check_dir/ingest-response.json" > "$evidence_check_dir/evidence-id"
```

The acknowledgement proves journaled custody. Attic delivery may still be
pending; a receipt alone does not prove that the payload can be read back.

## Read and compare exact bytes

```bash
python3 scripts/verify-retrieval.py \
  --base-url "$base_url" \
  --credential-file "$credential_file" \
  --evidence-id "$(cat "$evidence_check_dir/evidence-id")" \
  --payload-file "$evidence_check_dir/payload.txt" \
  --deadline 120 --request-timeout 10 --max-attempts 30
```

In evidence mode this helper sends only `POST /v1/read-evidence`, with the
saved evidence ID and `local/repository:example` scope. It sends neither the
expected payload nor another ingest. It requires HTTP 200 with the same ID,
exact UTF-8 bytes (including newlines), matching SHA-256, byte length and the
fixed `text/plain; charset=utf-8` media type. Success prints `status: verified`,
the evidence ID and digest, without echoing the payload.

Only HTTP 503 with `evidence_pending` or `dependency_unavailable` and retry
class `after-delay` is retried. The helper preserves failure details and
honours numeric or HTTP-date `Retry-After`, stopping at 120 seconds or 30
attempts. `evidence_pending` means custody delivery is queued;
`dependency_unavailable` can mean an adapter/storage fault and is not an
indexing delay. Missing/invalid retry headers, `evidence_corrupt` (HTTP 500,
never retry), other refusals, malformed responses and mismatched bytes fail
with a nonzero exit. Inspect service health and storage logs, correct the cause,
then repeat only this read with the same saved input and ID. Do not repeat a
committed ingest. `not_found` does not distinguish missing evidence from evidence
outside the caller's scope or clearance.

## Verify retention across restart

Restart using the native service, Compose or Kubernetes procedure, retaining
its identity, data locations/volumes and credential. Wait for readiness, restart
the Kubernetes port-forward if necessary, and rerun the identical read command.
If using a new shell, explicitly set `evidence_check_dir` to the printed saved
path and restore `base_url` and `credential_file` from the installation guide.
Do not rerun the payload-generation or ingest blocks.

A pass before and after restart proves authenticated custody, Attic delivery,
exact-byte recovery and retention across that restart for this payload. It
proves neither semantic indexing nor factual truth, backups or production
readiness. Run the separate [semantic check](../clients.md#bounded-ingest-and-retrieval-verification)
if semantic retrieval is enabled. Fresh-user installation acceptance is a
separate exercise from the repository test suite.
