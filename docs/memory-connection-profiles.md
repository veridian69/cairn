# Explicit memory connection profiles

A profile is connection configuration, not a memory store. It names one Cairn,
one exact scope and one credential file. Loading it neither contacts the server
nor searches for credentials. There is no environment-variable fallback,
credential cache, transcript archive or automatically updated session file.

The Linux/WSL loader is `cairn.client.profiles.load_profile(Path(...))`.
`load_credential(profile)` reads the designated token separately, immediately
before constructing an HTTP client. Native Windows is not supported by this
file-opening boundary.

## Format

Use strict UTF-8 JSON. This synthetic example names a disposable loopback Cairn;
replace its instance ID and scope with the explicitly designated test instance.
Do not point tests at productive credentials or services.

```json
{
  "schema": "cairn.memory-profile/v1",
  "endpoint": "http://127.0.0.1:8765",
  "expected_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "scope": {
    "realm": "example",
    "segments": [
      {"kind": "project", "identifier": "synthetic"}
    ]
  },
  "classification": "internal",
  "credential_file": "token"
}
```

All shown fields are required. The optional `session_id` is an explicit
canonical UUID for a fixed session; the loader never generates one. The other
two optional fields are described under "Cairn behind an access proxy" below.
Unknown fields, duplicate JSON keys and invalid domain values are rejected.
The expected instance ID must be a canonical UUIDv4.

Credential paths are relative to the profile's directory, not the shell's
working directory. Explicit absolute paths and genuine parent-directory
references are supported. No path component may be a symlink, including a
component followed by `..`. Neither `$VARIABLE` nor `~` is expanded. Profile
and token inputs must be regular files; pipes and device files are refused.
The token file must be owned by the invoking user with no group or other
access (`chmod 600`); anything wider is refused with
`credential_not_protected` before any request is made.

Profiles are limited to 32 KiB and token files to 512 bytes. The token must
have Cairn's credential format; its format alone does not prove authentication.
Profile objects do not hold the token. Errors expose stable codes, not input
paths, endpoints, file contents or credentials.

## Cairn behind an access proxy

A deployment fronted by Cloudflare Access refuses every request that lacks
Access credentials before Cairn sees it. Two optional fields name the files
holding the two components of a Cloudflare Access service token:

```json
{
  "access_client_id_file": "access-client-id",
  "access_client_secret_file": "access-client-secret"
}
```

Give both or neither; one alone is an invalid profile. When present, every
request carries `CF-Access-Client-Id` and `CF-Access-Client-Secret` beside the
Cairn bearer token. `load_access_headers(profile)` reads them, and returns no
headers for a profile that names none. The service token is sent to the
profile's endpoint only and is never logged, printed or held in the profile
object.

The two files follow the credential file's path rules: relative to the
profile's directory, no symlink in any component, no expansion, regular files
of at most 512 bytes, owned by the invoking user with no group or other access
(`chmod 600`). Anything wider is refused with
`access_credential_not_protected` before any request is made. The content is
opaque to Cairn: one line of printable ASCII without spaces. Other failures
are `access_credential_unavailable`, `access_credential_too_large` and
`invalid_access_credential`.

A 401 or 403 that does not carry Cairn's failure envelope is reported as
`upstream_access_denied`, with connection status `upstream_access_denied`, not
as `authentication_failed`: the refusal came from something in front of Cairn,
so the Cairn credential is not the thing to check. The `cairn-memory` error
carries the proxy's status as `http_status`; the response body is never
disclosed. At the preflight or on a read this is a confirmed refusal (exit 2).
On a dispatched mutation it is treated as unconfirmed (exit 3 or 4, with the
usual recovery), because a proxy can forward a request and then replace
Cairn's receipt. A Cairn reached only through a loopback tunnel to its origin needs
none of this.

TLS is required outside numeric loopback. An HTTP `localhost` alias is not
accepted as loopback. Endpoints must be root URLs: embedded credentials,
queries, fragments and path prefixes are refused rather than silently routed
elsewhere. This is consistent with the memory client's root-relative API paths.

## Check before sending conversational content

This example accepts only a profile path on the command line and prints no
credential. Run it in the project's locked Python environment after supplying
an explicit test profile and token file.

```python
import asyncio
import sys
from pathlib import Path

import httpx

from cairn.client import ConnectionStatus, MemoryClient
from cairn.client.profiles import (
    ProfileError,
    load_access_headers,
    load_credential,
    load_profile,
)


async def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_profile.py PROFILE", file=sys.stderr)
        return 2
    try:
        profile = load_profile(Path(sys.argv[1]))
        token = load_credential(profile)
        access = load_access_headers(profile)
    except ProfileError as error:
        print(str(error), file=sys.stderr)
        return 2
    async with httpx.AsyncClient(
        base_url=profile.endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            **access,
        },
        trust_env=False,
        follow_redirects=False,
        timeout=10,
    ) as http:
        client = MemoryClient(
            http, scope=profile.scope, classification=profile.classification
        )
        result = await client.diagnose(
            expected_instance_id=profile.expected_instance_id
        )
    print(result.status.value)
    return 0 if result.status is ConnectionStatus.READY else 1


raise SystemExit(asyncio.run(main()))
```

The diagnostic request necessarily sends authentication and the configured
scope/classification to the configured endpoint. It sends no conversation.
The expected-instance comparison is not a substitute for TLS or choosing a
trusted endpoint. `ready` means the authenticated interface is compatible and
some applicable authority exists; it does not promise ingest permission or
future operation success. Every operation still checks current server grants.

The client requests an uncompressed diagnostic reply and refuses compression
before decoding. It stops when streamed response bytes exceed 16 KiB and
closes the response. Invalid responses never become trusted connection metadata.
