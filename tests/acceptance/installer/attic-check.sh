#!/usr/bin/env bash
# Independent attic check through documented REST routes only.
# Adapted from the v0.7.10 blind acceptance harness (reference, 19 September 2026)
# so the pre-blind rehearsal proves what the blind tester proves.
set -eu
umask 077
base_url="$1"
credential_file="$2"
label="$3"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cc="$work/curl.conf"
: > "$cc"
chmod 600 "$cc"
printf 'header = "Authorization: Bearer ' > "$cc"
tr -d '\n' < "$credential_file" >> "$cc"
printf '"\n' >> "$cc"

echo "--- [$label] identity ---"
inst="$work/instance.json"
code="$(curl --disable --silent --show-error --noproxy '*' --max-time 15 --config "$cc" --output "$inst" --write-out '%{http_code}' "$base_url/v1/instance")"
echo "GET /v1/instance HTTP $code"
test "$code" = 200
echo "instance_id=$(jq -r .instance_id "$inst")"
echo "product_version=$(jq -r .product_version "$inst")"
echo "contract_identity=$(jq -r .contract_identity "$inst")"
echo "contract_digest=$(jq -r .contract_digest "$inst")"
echo "mcp_contract_digest=$(jq -r .mcp_contract_digest "$inst")"
test "$(jq -r .contract_identity "$inst")" = cairn/v1
instance_id="$(jq -r .instance_id "$inst")"

echo "--- [$label] attic write ---"
nonce="$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
payload="cairn-acceptance $label $(date -u +%Y%m%dT%H%M%SZ) nonce=$nonce -- mixed punctuation: comma, colon; semicolon (parens) [brackets] 100% done -- end"
expected_sha="$(printf '%s' "$payload" | sha256sum | cut -d' ' -f1)"
expected_len="$(printf '%s' "$payload" | wc -c)"
key="$(python3 -c 'import uuid;print(uuid.uuid4())')"
seg="$(jq -nc --arg k repository --arg i acceptance '.kind = $k | .identifier = $i')"
scope="$(jq -nc --arg r local --argjson s "$seg" '.realm = $r | .segments = [$s]')"
fact="$(jq -nc --arg b "Acceptance marker for $label nonce $nonce: $payload" '.body = $b')"
req="$work/ingest.json"
jq -nc --argjson sc "$scope" --arg cl internal --arg st human --arg p "$payload" --argjson f "$fact" '.scope = $sc | .classification = $cl | .source_type = $st | .evidence_payload = $p | .facts = [$f]' > "$req"
resp="$work/ingest-response.json"
set +e
code="$(curl --disable --silent --show-error --noproxy '*' --max-time 30 --request POST --config "$cc" --header 'Content-Type: application/json' --header "Idempotency-Key: $key" --data-binary "@$req" --output "$resp" --write-out '%{http_code}' "$base_url/v1/ingest")"
set -e
echo "POST /v1/ingest HTTP $code"
if test "$code" != 200 ; then
  echo INGEST-FAILED
  head -c 2000 "$resp"
  echo
  exit 1
fi
echo "outcome=$(jq -r .outcome "$resp")"
echo "mutation_receipt_type=$(jq -r '.mutation_receipt | type' "$resp")"
echo "audit_receipt_type=$(jq -r '.audit_receipt | type' "$resp")"
evidence_id="$(jq -r .result.evidence_id "$resp")"
fact_id="$(jq -r '.result.fact_ids[0]' "$resp")"
echo "evidence_id=$evidence_id fact_id=$fact_id"

echo "--- [$label] attic byte-exact read ---"
rreq="$work/read.json"
jq -nc --argjson sc "$scope" --arg e "$evidence_id" '.scope = $sc | .evidence_id = $e' > "$rreq"
rresp="$work/read-response.json"
attempt=0
code=000
while test "$attempt" -lt 30 ; do
  attempt=$((attempt + 1))
  set +e
  code="$(curl --disable --silent --show-error --noproxy '*' --max-time 30 --request POST --config "$cc" --header 'Content-Type: application/json' --data-binary "@$rreq" --output "$rresp" --write-out '%{http_code}' "$base_url/v1/read-evidence")"
  set -e
  if test "$code" = 200 ; then
    break
  fi
  fcode="$(jq -r '.failure.code // empty' "$rresp" 2>/dev/null || true)"
  fretry="$(jq -r '.failure.retry // empty' "$rresp" 2>/dev/null || true)"
  echo "attempt $attempt HTTP $code failure=$fcode retry=$fretry"
  if test "$code" = 503 -a "$fcode" = evidence_pending -a "$fretry" = after-delay ; then
    sleep 2
    continue
  fi
  break
done
echo "POST /v1/read-evidence HTTP $code after $attempt attempt(s)"
if test "$code" != 200 ; then
  echo READ-EVIDENCE-FAILED
  head -c 2000 "$rresp"
  echo
  exit 1
fi
got_sha="$(jq -r .sha256 "$rresp")"
got_len="$(jq -r .byte_length "$rresp")"
media="$(jq -r .media_type "$rresp")"
jq -j .payload "$rresp" > "$work/got.bin"
printf '%s' "$payload" > "$work/want.bin"
echo "expected sha256=$expected_sha len=$expected_len"
echo "returned sha256=$got_sha len=$got_len media=$media"
test "$got_sha" = "$expected_sha"
test "$got_len" = "$expected_len"
cmp "$work/want.bin" "$work/got.bin"
echo BYTE-EXACT-OK
echo "RESULT label=$label instance_id=$instance_id evidence_id=$evidence_id fact_id=$fact_id sha256=$got_sha nonce=$nonce"
