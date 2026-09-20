#!/usr/bin/env bash
# Independent persisted check through documented REST routes only.
# Adapted from the v0.7.10 blind acceptance harness (reference, 19 September 2026)
# so the pre-blind rehearsal proves what the blind tester proves.
set -eu
umask 077
base_url="$1"
credential_file="$2"
label="$3"
evidence_id="$4"
expected_sha="$5"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cc="$work/curl.conf"
: > "$cc"
chmod 600 "$cc"
printf 'header = "Authorization: Bearer ' > "$cc"
tr -d '\n' < "$credential_file" >> "$cc"
printf '"\n' >> "$cc"
inst="$work/instance.json"
code="$(curl --disable --silent --show-error --noproxy '*' --max-time 15 --config "$cc" --output "$inst" --write-out '%{http_code}' "$base_url/v1/instance")"
echo "[$label] GET /v1/instance HTTP $code instance_id=$(jq -r .instance_id "$inst" 2>/dev/null || echo none)"
test "$code" = 200
seg="$(jq -nc --arg k repository --arg i acceptance '.kind = $k | .identifier = $i')"
scope="$(jq -nc --arg r local --argjson s "$seg" '.realm = $r | .segments = [$s]')"
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
  if test "$code" = 503 -a "$fcode" = evidence_pending ; then
    sleep 2
    continue
  fi
  break
done
echo "[$label] POST /v1/read-evidence HTTP $code after $attempt attempt(s)"
if test "$code" != 200 ; then
  head -c 1000 "$rresp"
  echo
  exit 1
fi
got_sha="$(jq -r .sha256 "$rresp")"
echo "[$label] expected sha256=$expected_sha"
echo "[$label] returned sha256=$got_sha byte_length=$(jq -r .byte_length "$rresp")"
test "$got_sha" = "$expected_sha"
echo "[$label] PERSISTED-BYTE-EXACT-OK"
