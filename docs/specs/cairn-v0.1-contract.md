# Cairn v0.1 contract

This is the repository-local Cairn contract. It was extracted from section 6 of the frozen Drystane specification at `43dcbd5ecfba4b754a6f4d79f466a7de923de201`; the normative wording below includes the explicitly recorded RC2 evidence-read amendment.

## The Cairn contract (load-bearing)

### 6.1 Positioning

Cairn is a standalone open-source product with its own repository, versioned API, and compatibility promise. The entire project — server, client SDKs, protocol definitions, specifications, documentation, tests, and deployment artefacts — is licensed under **Apache-2.0**. Drystane is its flagship consumer, not its owner. Everything in this section must hold for *any* consumer.

Cairn's authoritative fact catalogue owns fact identity, scope, provenance,
trust and temporal history. Extraction and semantic/graph search engines,
including Graphiti, are rebuildable indexes behind a Cairn-owned adapter; they
are never the authorisation or knowledge authority. An immutable assertion
accepted into custody may produce one or more facts. Promotion and invalidation
address those fact identities, not an extraction engine's episode identifiers.

Attic is an optional v0.1 exact-evidence adapter behind the same authority
module, not a separate public service. The catalogue owns each evidence
record's identity, scope, classification and provenance; Attic stores exact
evidence payloads and their FTS representation. Only Cairn may invoke it.
Writes pass authentication, grant evaluation and secret screening before
Attic custody, and every candidate read is reconciled against the catalogue
before return. Attic exposes no direct REST or MCP operation. RC2 adds Cairn's
`read-evidence(scope, evidence_id)` custody operation through REST and MCP;
clients address catalogue identities, never the adapter. It requires a live
`retrieve` grant and discloses only same-realm evidence at the requested scope
or an ancestor within derived read clearance. It returns exact UTF-8 payload,
SHA-256, byte length and `text/plain; charset=utf-8`, after verifying catalogue
metadata. Source inspection is independent of semantic indexing and associated
fact trust or invalidation; it does not assert factual validity.
See `docs/m0/cairn-productisation-gap-analysis.md`.

### 6.2 Scope model

Every read and write carries a generic ordered scope path; the service enforces
it — scoping is not a client convention. Drystane gives its segments
software-delivery meanings, but Cairn does not hard-code those meanings.

```yaml
scope:
  realm: local
  path:
    - kind: repository
      id: <repository-id>
    - kind: change-set
      id: <change-set-id>
    - kind: composite-run
      id: <run-id>
    - kind: job
      id: <job-id>
classification: <data class>           # e.g. public, internal, restricted
```

Rules:
- Realm identifiers use the same lowercase 1–63 character label syntax as
  segment kinds.
- Every principal is an authenticated `human` or `workload`; anonymous access
  does not exist. Operator, planner, worker and verifier are grant or
  provenance roles, not principal types.
- Grants are direct and deny by default; v0.1 has no named permission roles or
  role inheritance. Each grant binds one principal to one realm, one scope
  prefix, explicit operations, `read_clearance` and
  `write_classifications`.
- Data-grant operations are exactly `retrieve`, `ingest`, `promote` and
  `invalidate`. Administrative capabilities are separate and explicit; there
  is no wildcard, catch-all `admin` operation or CRUD shorthand.
- Audit access is the explicit `audit-read` capability, bound to one realm and
  scope prefix. It permits metadata-only inspection of audit events at that
  prefix and its descendants. Either principal type may hold it through an
  explicit grant; no role or principal type implies it. It never exposes fact
  bodies, query text, credential material, suspected secret text or raw model
  output.
- Workload grants require `expires_at`; human grants may omit it but remain
  revocable. Grant expiry is checked on every request and fails closed.
  Credential expiry may be earlier but never extends the grant. Expired and
  revoked grants remain in audit history.
- Grant administration is the separate `grant-manage` capability. It may be
  assigned explicitly to a human or workload; no principal type or role
  implies it.
- Each `grant-manage` grant carries an explicit delegation envelope. A created
  grant must remain in the same realm, target the same scope prefix or a
  descendant, use only permitted delegated operations, carry no higher read
  clearance or broader write classifications, and expire no later than the manager grant.
  `grant-manage` itself is not delegable in v0.1.
- Grants are immutable: changes create a replacement and revoke the old grant.
  An issuer may revoke a grant it created; a realm-root `grant-manage` holder
  may revoke any grant in its realm for recovery or incident response. Every
  revocation records actor, time and reason.
- There is no network bootstrap endpoint. A local administrative command with
  direct catalogue access may create the first human principal and explicit
  realm-root grants only when the realm is empty. The same local path assigns
  or recovers non-delegable `grant-manage`. Every action enters immutable audit
  history. Credential material is emitted once; only its verifier is stored.
- Every request is associated with an authenticated principal. Server-side
  grants constrain that principal's operations, scope roots and permitted
  classifications; a caller-supplied scope is a request, not authority.
- Retrieval returns only facts whose scope is equal to or an ancestor of the request scope, subject to classification and the caller's permission. Sibling scopes are invisible: job A cannot see job B; run N cannot see run N+1. **Filesystem isolation without memory isolation is insufficient** — this rule is the product.
- A `retrieve` grant covering requested scope `S` authorises facts stored
  exactly at `S` and inherited from its ancestors in the same realm; separate
  grants for those ancestors are not required. It never authorises direct
  retrieval at an ancestor scope, or any descendant or sibling.
- Scope ancestry is exact ordered `(kind, id)` prefix ancestry. Segment kinds
  are domain-defined; Cairn compares them but does not assign their meaning.
- Segment kinds require no registry. They are lowercase labels of 1–63
  characters, begin with a letter, end with a letter or digit, and otherwise
  contain only letters, digits and hyphens.
- Segment identifiers are opaque, case-sensitive, URI-safe ASCII values of
  1–255 characters. They begin with a letter or digit and otherwise permit
  letters, digits, `.`, `_`, `~`, `:`, `/`, `@`, `+`, `%` and `-`. They contain
  no whitespace or control characters and are stable identities, not display
  names.
- A scope path contains at most 16 segments.
- The path may be empty, representing the realm root. Writing or promoting a
  fact there requires an explicit root grant; ordinary grants never imply it.
- Writes land at the narrowest applicable scope. Widening (e.g. run-level → repo-level knowledge) happens only through explicit promotion.
- v0.1 has no general cross-scope fact query. No principal may query
  descendants or siblings. Privileged workflows make separately authorised
  retrieval calls at each requested scope. Administrative audit access does
  not reveal fact bodies and requires `audit-read`.
- Agent role belongs to principal policy or fact provenance, not scope
  ancestry.
- Classification is an access label checked against the principal's clearance;
  it is not an ancestry segment.
- v0.1 classifications are fixed and ordered:
  `public < internal < restricted`. A grant's `read_clearance` permits its
  level and lower levels. `write_classifications` explicitly enumerates the
  labels permitted for ingest and promotion targets; read clearance never
  implies write-down authority. Custom labels are unsupported. `public` still requires
  authentication; `secret` is not a classification because secrets are
  rejected at ingest.
- Every assertion must declare its classification; omission is rejected.
  Extracted facts inherit the assertion's classification.

Every attempted data or administrative operation produces an immutable audit
event, whether it is allowed, denied or fails. Later state transitions produce
separate events. Each event records: event identity and sequence; recorded-at
time; principal, grant and credential-verifier identities; operation or
capability; realm and relevant source, requested and target scopes; allow,
deny or error outcome with a stable reason code; affected assertion, fact or
grant identities; classification and trust transition metadata; evidence
reference or hash; and correlation and idempotency identities. Request bodies,
fact bodies, query text, credential material, suspected secret text and raw
model output are not stored in audit events.

Realm-attributable events enter the immutable chain for that existing realm.
Each Cairn instance also owns one reserved local-only instance audit chain for
attempts which cannot be bound safely to an existing realm, including a
malformed or unknown realm request. Such an event stores only safe metadata
and a request fingerprint; it never copies the untrusted realm or scope text.
The instance chain is visible only to local verification and backup evidence
in v0.1. A realm-bound `audit-read` grant cannot access it, and recording an
attempt there never creates or reserves a realm.

An authorised mutation and its audit event commit in the same catalogue
transaction. Read, denied and failed attempts persist their audit event before
Cairn returns a response. Realm chains have a monotonic per-realm sequence;
the reserved instance chain has its own monotonic sequence. Every chain uses
SHA-256 and Cairn provides a chain-verification procedure. The API permits
neither editing nor deletion of audit events. This detects catalogue tampering;
it does not claim protection from an administrator able to rewrite the
database and every external copy. Signing and WORM storage are optional
deployment controls, not v0.1 guarantees.

### 6.3 Fact schema (contract level)

An immutable **assertion** is the source material accepted into custody. It may
contain one or more directly supplied facts or produce facts through extraction.
Every resulting fact has a stable Cairn identity and carries: body; scope path;
**provenance** (assertion identity, source actor, source type — agent claim /
verified check / human, evidence reference, and extractor lineage where
applicable); **trust class** (candidate | validated | failed-approach);
**bitemporal fields** (valid-from/valid-to as statements about the world,
recorded-at as immutable ingest time); and an optional invalidation link (what
superseded it, and why).

### 6.4 API surface (v0.1, versioned)

| Operation | Contract |
|---|---|
| `retrieve(scope, query, budget)` | Scope-filtered semantic, graph and enabled exact-evidence retrieval within a token budget; results carry provenance and trust class. Attic candidates are returned only after catalogue reconciliation. Retrieval defaults to `validated`; `candidate` and `failed-approach` require explicit trust filters. Cairn has no special "own run" bypass — consumers express it through paths and filters. |
| `read-evidence(scope, evidence_id)` | Authenticated exact source custody read under the existing retrieve grant. Same-realm ancestor scope and clearance checks precede Attic access; unknown, inaccessible and external-reference evidence share `not_found`. `evidence_pending` is retryable after delay; `evidence_corrupt` is non-retryable. No semantic index is required. |
| `ingest(scope, facts[], provenance)` | Durable, journaled write; lands as candidate unless the caller holds promotion rights. When configured, exact source evidence is also placed in Attic under the catalogue-owned assertion/evidence identity after the same authority and secret screens. Asynchronous extraction is acceptable; the ACK confirms custody, not searchability. |
| `promote(fact_ids[], evidence_ref, target_scope?, target_classification?)` | Creates a new validated fact at the same scope, or at an ancestor scope for widening. It requires evidence and records `derived_from` plus promoting principal; the source is unchanged. Classification is inherited by default and may be raised only when allowed by `write_classifications`; lowering is forbidden in v0.1. Descendant, sibling and cross-realm targets are forbidden. Verification-gated in Drystane. |
| `invalidate(fact_ids[], reason, superseded_by?)` | Ends validity without deletion; history is preserved. |

Drystane distributes live findings through the controller and persists them
through `ingest`; Cairn does not become a scheduler or message broker merely
because the stored fact originated as a finding.

Promotion authorisation requires `retrieve` covering the source fact's exact
scope, including permission to read its trust class, and `promote` covering
the target scope. No `promote` grant on the source is required. A broad target
grant does not permit scanning descendant sources.

Declassification is not a v0.1 operation. It requires a separately designed
and audited capability before it may be introduced.

### 6.5 Guarantees Cairn makes (and tests in its own suite)

1. No retrieval ever crosses sibling scopes (contamination test — the `@ottogin1` scenario is a named regression test).
2. No fact exists without provenance and a trust class.
3. Validated facts are traceable to evidence; failed approaches remain retrievable *as failures*.
4. Secrets are rejected at ingest (pattern + policy screens); Cairn is not a route around secret policy.
5. Point-in-time queries answer "what did we believe at time T" (bitemporal recall).
6. Every index hit is reconciled against the authoritative catalogue before it
   is returned; a stale or compromised retrieval index cannot widen scope or
   trust.
7. Every enabled Attic hit is reconciled against the authoritative catalogue;
   an exact-evidence lookup cannot widen scope, classification or trust, and
   secret material is rejected before Attic custody.

### 6.6 Scope conformance contract

Cairn maintains one normative, machine-readable scenario corpus. Each scenario
defines its initial principals, grants and facts; the request; the expected
result and stable reason code; and the expected audit evidence. Every supported
transport must run every applicable scenario through its real external
interface against a fresh real catalogue. REST and MCP therefore prove the
same security contract. Transport-specific schema, framing and error-mapping
tests are additional and cannot substitute for the shared corpus.

The corpus has eight mandatory scenario families, each covering allowed and
adversarial paths where meaningful:

1. authentication and grant evaluation;
2. scope syntax, realm root handling, ancestry and realm isolation;
3. trust filters and classification clearance;
4. ingest, promotion and invalidation authority;
5. grant creation, delegation, expiry and revocation;
6. reconciliation of hostile or stale index results against the catalogue;
7. audit visibility, completeness, atomicity and hash-chain verification; and
8. exact-evidence custody and hostile Attic-result reconciliation when the
   Attic adapter is enabled.

The deliberately non-networked bootstrap path has a separate procedural
conformance test.

`INDEX-01`–`INDEX-04` use a controlled hostile-index adapter inside a fully
started Cairn instance. The external transport, authentication, grant
enforcement and real SQLite catalogue remain in use while the adapter returns
deterministic forbidden or stale identities. Each release also runs an
integration smoke test against the supported production index; that smoke test
cannot replace the deterministic attack cases.

Conformance runs use dedicated, explicitly configured Cairn instances, never
the productive Cairn. Each instance has its own catalogue, custody journal,
index adapter or endpoint, credential store, bind endpoints, instance identity
and optional seed dataset. Multiple instances may run concurrently on one
host. An explicit client configuration selects the transport endpoint,
credential reference and expected instance identity; no global configuration
registry or service discovery is required. Fixture loading or reset is allowed
only when the server is explicitly in test mode and its identity matches the
client configuration. A productive endpoint, including a Cloudflare-gated MCP
endpoint, is never an implicit conformance default.

The following named scenarios are the minimum v0.1 matrix:

| ID | Required proof |
|---|---|
| `AUTH-01` | A valid credential and matching operation grant permit the request. |
| `AUTH-02` | An absent credential is denied. |
| `AUTH-03` | An unknown credential is denied without revealing whether a principal or grant exists. |
| `AUTH-04` | An expired credential is denied. |
| `AUTH-05` | An expired grant is denied. |
| `AUTH-06` | A revoked grant is denied. |
| `AUTH-07` | A valid credential without the requested operation is denied. |
| `SCOPE-01` | Exact-scope and inherited-ancestor facts are returned. |
| `SCOPE-02` | A descendant grant cannot query an ancestor scope directly. |
| `SCOPE-03` | Sibling facts are not returned. |
| `SCOPE-04` | Descendant facts are not returned. |
| `SCOPE-05` | Cross-realm facts are not returned or disclosed. |
| `SCOPE-06` | Realm-root writes and promotions require an explicit root grant. |
| `SCOPE-07` | Malformed realm, kind and segment identifiers are rejected. |
| `SCOPE-08` | A 16-segment path is accepted and a 17-segment path is rejected. |
| `TRUST-01` | Default retrieval returns validated facts only. |
| `TRUST-02` | Candidate and failed-approach facts require their explicit filters. |
| `TRUST-03` | Facts above read clearance are not returned or disclosed. |
| `TRUST-04` | An ingest without classification is rejected. |
| `TRUST-05` | A custom classification is rejected. |
| `TRUST-06` | Suspected secret material is rejected rather than classified. |
| `MUT-01` | Authorised ingest and invalidation preserve provenance and history. |
| `MUT-02` | Replaying an idempotency identity creates no duplicate assertion, fact or mutation. |
| `MUT-03` | Same-scope promotion creates a validated derived fact and leaves the source unchanged. |
| `MUT-04` | Ancestor promotion creates a validated derived fact and leaves the source unchanged. |
| `MUT-05` | Promotion without evidence is rejected. |
| `MUT-06` | Promotion without source `retrieve` authority is rejected. |
| `MUT-07` | Promotion without target `promote` authority is rejected. |
| `MUT-08` | Descendant, sibling and cross-realm promotion targets are rejected. |
| `MUT-09` | A classification raise outside `write_classifications` is rejected. |
| `MUT-10` | Every classification-lowering promotion is rejected. |
| `GRANT-01` | A grant created wholly within its delegation envelope succeeds. |
| `GRANT-02` | Scope-prefix delegation beyond the envelope is rejected. |
| `GRANT-03` | Operation delegation beyond the envelope is rejected. |
| `GRANT-04` | Read-clearance delegation beyond the envelope is rejected. |
| `GRANT-05` | Write-classification delegation beyond the envelope is rejected. |
| `GRANT-06` | A delegated expiry later than the manager grant is rejected. |
| `GRANT-07` | Delegation of `grant-manage` is rejected. |
| `GRANT-08` | A grant issuer may revoke its issued grant. |
| `GRANT-09` | A realm-root grant manager may perform recovery revocation. |
| `GRANT-10` | Any other revocation attempt is rejected. |
| `INDEX-01` | Hostile sibling, descendant and cross-realm index hits are discarded. |
| `INDEX-02` | Hostile candidate or failed-approach hits absent explicit filters are discarded. |
| `INDEX-03` | Hostile hits above read clearance are discarded. |
| `INDEX-04` | Stale hits for invalidated or unknown catalogue identities are discarded. |
| `EVIDENCE-01` | An authorised exact-evidence hit at the requested scope or an ancestor is returned with its catalogue provenance. |
| `EVIDENCE-02` | Sibling, descendant and cross-realm Attic hits are discarded without disclosure. |
| `EVIDENCE-03` | Unknown, stale and above-clearance Attic hits are discarded. |
| `EVIDENCE-04` | Suspected secret material is rejected before any durable Attic write. |
| `AUDIT-01` | Allowed, denied and failed attempts all create the required event before response. |
| `AUDIT-02` | `audit-read` exposes only events at its prefix and descendants. |
| `AUDIT-03` | Audit events and results omit all prohibited content. |
| `AUDIT-04` | A failed authorised mutation leaves neither a mutation nor an orphan audit event. |
| `AUDIT-05` | The intact hash chain verifies and a modified, removed or reordered event is detected. |
| `BOOT-01` | Bootstrap succeeds for an empty realm and creates explicit root grants. |
| `BOOT-02` | Bootstrap refuses a non-empty realm. |
| `BOOT-03` | Credential material is emitted once and only its verifier remains stored. |
| `BOOT-04` | Local grant-management recovery is non-delegable and fully audited. |

Acceptance is zero-tolerance. Every applicable named network scenario must
pass through both REST and MCP with the exact expected outcome, stable reason
code, side effects and audit evidence. Skips, expected failures and security
waivers are not permitted. The local bootstrap procedures and
production-index integration smoke test must also pass. Each run emits a
machine-readable report identifying the build, contract and corpus hashes,
non-secret instance identity, transport, scenario outcomes and evidence. Any
failure blocks the Cairn release and M1 exit.
