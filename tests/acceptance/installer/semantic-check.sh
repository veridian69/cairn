#!/usr/bin/env bash
# Independent semantic check through documented REST routes only.
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
nonce="$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
body="The acceptance harness recorded a marmalade telescope inventory for run $nonce in the semantic index."
query="marmalade telescope inventory"
seg="$(jq -nc --arg k repository --arg i semantic '.kind = $k | .identifier = $i')"
scope="$(jq -nc --arg r local --argjson s "$seg" '.realm = $r | .segments = [$s]')"
fact="$(jq -nc --arg b "$body" '.body = $b')"
key="$(python3 -c 'import uuid;print(uuid.uuid4())')"
req="$work/ingest.json"
jq -nc --argjson sc "$scope" --arg cl internal --arg st human --argjson f "$fact" '.scope = $sc | .classification = $cl | .source_type = $st | .facts = [$f]' > "$req"
resp="$work/ingest-response.json"
set +e
code="$(curl --disable --silent --show-error --noproxy '*' --max-time 30 --request POST --config "$cc" --header 'Content-Type: application/json' --header "Idempotency-Key: $key" --data-binary "@$req" --output "$resp" --write-out '%{http_code}' "$base_url/v1/ingest")"
set -e
echo "[$label] POST /v1/ingest HTTP $code outcome=$(jq -r .outcome "$resp" 2>/dev/null || echo none)"
if test "$code" != 200 ; then
  head -c 1200 "$resp"
  echo
  exit 1
fi
fact_id="$(jq -r '.result.fact_ids[0]' "$resp")"
echo "[$label] fact_id=$fact_id"
echo "[$label] query=$query scope=local/repository:semantic trust_filters=[candidate]"
rreq="$work/retrieve.json"
jq -nc --argjson sc "$scope" --arg q "$query" --argjson b 65536 --arg tf candidate '.scope = $sc | .query = $q | .budget = $b | .trust_filters = [$tf]' > "$rreq"
rresp="$work/retrieve-response.json"
attempt=0
deadline=$(( $(date +%s) + 180 ))
found=no
while test "$attempt" -lt 30 ; do
  attempt=$((attempt + 1))
  set +e
  code="$(curl --disable --silent --show-error --noproxy '*' --max-time 15 --request POST --config "$cc" --header 'Content-Type: application/json' --data-binary "@$rreq" --output "$rresp" --write-out '%{http_code}' "$base_url/v1/retrieve")"
  set -e
  if test "$code" = 200 ; then
    hits="$(jq -r '[.hits[]?.fact_id] | length' "$rresp" 2>/dev/null || echo 0)"
    if jq -e --arg f "$fact_id" 'any(.hits[]?; .fact_id == $f)' "$rresp" >/dev/null 2>&1 ; then
      found=yes
      break
    fi
    echo "[$label] attempt $attempt HTTP 200 hits=$hits target not yet present"
    if test "$(date +%s)" -ge "$deadline" ; then
      break
    fi
    sleep 5
    continue
  fi
  fcode="$(jq -r '.failure.code // empty' "$rresp" 2>/dev/null || true)"
  fretry="$(jq -r '.failure.retry // empty' "$rresp" 2>/dev/null || true)"
  echo "[$label] attempt $attempt HTTP $code failure=$fcode retry=$fretry"
  case "$code:$fcode:$fretry" in
    503:index_pending:after-delay|503:stale_index:after-delay|503:dependency_unavailable:after-delay)
      if test "$(date +%s)" -ge "$deadline" ; then
        break
      fi
      sleep 5
      ;;
    *)
      break
      ;;
  esac
done
echo "[$label] POST /v1/retrieve final HTTP $code after $attempt attempt(s) found=$found"
if test "$found" != yes ; then
  echo SEMANTIC-RETRIEVAL-FAILED
  head -c 1500 "$rresp"
  echo
  exit 1
fi
got_body="$(jq -r --arg f "$fact_id" '.hits[] | select(.fact_id == $f) | .body' "$rresp")"
echo "[$label] returned body: $got_body"
test "$got_body" = "$body"
echo "[$label] budget_consumed=$(jq -r '.budget_consumed // empty' "$rresp") budget_exhausted=$(jq -r '.budget_exhausted // empty' "$rresp")"
echo "[$label] SEMANTIC-RETRIEVAL-OK fact_id=$fact_id"
