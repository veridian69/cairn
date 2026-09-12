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

All shown fields are required. The sole optional field, `session_id`, is an
explicit canonical UUID for a fixed session; the loader never generates one.
Unknown fields, duplicate JSON keys and invalid domain values are rejected.
The expected instance ID must be a canonical UUIDv4.

Credential paths are relative to the profile's directory, not the shell's
working directory. Explicit absolute paths and genuine parent-directory
references are supported. No path component may be a symlink, including a
component followed by `..`. Neither `$VARIABLE` nor `~` is expanded. Profile
and token inputs must be regular files; pipes and device files are refused.
Keep the token file accessible only to the intended host account.

Profiles are limited to 32 KiB and token files to 512 bytes. The token must
have Cairn's credential format; its format alone does not prove authentication.
Profile objects do not hold the token. Errors expose stable codes, not input
paths, endpoints, file contents or credentials.

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
from cairn.client.profiles import ProfileError, load_credential, load_profile


async def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_profile.py PROFILE", file=sys.stderr)
        return 2
    try:
        profile = load_profile(Path(sys.argv[1]))
        token = load_credential(profile)
    except ProfileError as error:
        print(str(error), file=sys.stderr)
        return 2
    async with httpx.AsyncClient(
        base_url=profile.endpoint,
        headers={"Authorization": f"Bearer {token}"},
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
