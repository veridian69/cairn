# Browser connectors

claude.ai and ChatGPT reach Cairn through the edge mapper described in
[the edge runbook](cairn-edge.md). Each connector gets its own Cairn
principal, so its writes are attributable in the audit chain.

In the URLs below, replace `<cairn-host>` with the public hostname your
tunnel serves Cairn on.

## Cloudflare prerequisites

Make these changes in your own Cloudflare account before creating either
connector.

If existing relays still reach Cairn through `/mcp` with service tokens, cover
only `/chatgpt/mcp` in steps 1 and 5 at first. Move every relay to
`/memory/v1/mcp` (see [the relay guide](cairn-mcp-relay.md)), then add `/mcp`
to the application and its ingress row: the browser-path application and the
edge both refuse service tokens.

1. Access application for the browser paths. Create a self-hosted application
   covering `<cairn-host>/mcp` and `<cairn-host>/chatgpt/mcp`. Its only policy
   includes the single email given as `allowed_email` in `edge.json`, with no
   service-token rule.
2. Managed OAuth on that application, with dynamic client registration
   enabled. Leave `allow_any_on_localhost` and `allow_any_on_loopback` off.
3. Callback allow-list: the application's dynamic client registration
   `allowed_uris` holds the claude.ai and claude.com callbacks, and, once
   created, the ChatGPT callback (see [ChatGPT](#chatgpt)). The list is per
   application, so claude.ai cannot log in unless it is on this application.
4. AUD tag: copy the application's AUD tag into `edge.json` as `audience`.
   `team_domain` is the lowercase `<team>.cloudflareaccess.com` host.
5. Tunnel ingress, in this order, before the existing catch-all:

   | Hostname | Path | Service |
   |----------|------|---------|
   | `<cairn-host>` | `^/mcp$` | `http://127.0.0.1:${CAIRN_EDGE_HOST_PORT}` |
   | `<cairn-host>` | `^/chatgpt/mcp$` | `http://127.0.0.1:${CAIRN_EDGE_HOST_PORT}` |
   | `<cairn-host>` | `^/memory/` | `http://127.0.0.1:${CAIRN_HOST_PORT}` |

   `/memory/` is for service-token clients such as the relay. It sits behind
   an Access application with explicit service-token entries, not the browser
   policy. Substitute the real port values; Cloudflare does not expand them.

## claude.ai

Add a custom connector with the URL `https://<cairn-host>/mcp`. Choose OAuth
and leave the client id empty. The connector hands off to Cloudflare Access,
which signs the user in; the edge then maps the `/mcp` path to the `claude-ai`
principal.

## ChatGPT

ChatGPT registers by dynamic client registration, and Access refuses a
registration whose callback is not already allowed. The callback URI only
exists once the connector does, so the order is loosen, create, then pin:

1. In the browser-path application, temporarily loosen the callback allow-list
   (a wildcard prefix or an empty list, whichever Access accepts for ChatGPT's
   registration).
2. Enable developer mode in ChatGPT, add a connector with the URL
   `https://<cairn-host>/chatgpt/mcp`, and choose OAuth.
3. Copy the callback URI ChatGPT generates for the connector. It has the form
   `https://chatgpt.com/connector/oauth/<callback-id>`.
4. Restrict the allow-list to exactly the claude.ai, claude.com and ChatGPT
   callbacks, removing the loosened entry. Retest ChatGPT login, and confirm a
   callback not on the list is refused.

The edge maps `/chatgpt/mcp` to the `chatgpt` principal.

## Connector instructions

Each connector carries this exact text as its instructions, with `<realm>`
replaced by the realm the connector principals were issued under
(`scripts/cutover_principals.py --realm`). The edge fills in nothing, so scope,
classification and idempotency keys come from here.

```text
You are connected to the user's personal memory, Cairn.
Conventions for every memory tool call:
- scope: {"realm": "<realm>", "segments": []} on every memory tool call.
- classification: "internal", and idempotency_key: a fresh random UUID version 4, only on calls that store something (remember). Reuse a key only to retry the same remember, with identical arguments, after an uncertain response; never for a different call. Reads such as recall take neither.
- When the user asks you to remember something, call remember with one short, self-contained fact per item in facts, each as {"body": "..."}. Do not add facts the user did not state.
- When the user asks about something they may have told you before, call recall with a short natural-language query before answering. Answer from what comes back; say so if nothing relevant is stored.
- Memories you store are candidate claims attributed to this connector; the user validates them separately.
```

## What the browser can and cannot do

A browser connector can write candidate facts and read `internal` memory. It
cannot promote, invalidate or read the audit chain, because neither browser
principal holds `promote`, `invalidate`, `audit-read` or `grant-manage`.

Browser statements are candidates on purpose. The chat model writes the memory
in its own words, so it is a claim of the `claude-ai` or `chatgpt` principal,
not the user's validated word. Giving a browser principal `promote` would let a
hijacked chat session mint validated facts in the user's voice, so the user ratifies
facts separately through their own principal.

## Failures

The edge answers with a status code and no detail: `401` for a missing or
invalid Access assertion, `403` for any identity other than the user's, `404` or
`405` for a wrong path or method (only `POST` is accepted), `503` while the
Access keys are unavailable. See the decision chain in
[the edge runbook](cairn-edge.md#decision-chain).
