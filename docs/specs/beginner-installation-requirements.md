# Beginner installation requirements

Date: 13 September 2026.
Status: BI-1 through BI-4 installation guidance is implemented; engineering
verification and distribution publication are recorded with this change.
**Fresh-user beginner acceptance remains pending.** Start at [Install Cairn](../install.md).

## Purpose and scope

An inexperienced user must be able to install and operate Cairn through native
and Docker Compose paths without an agent filling in missing steps. These
requirements extend the Slice 9 P-79 documentation and stranger-quickstart work
and remain subject to the [Cairn contract](cairn-v0.1-contract.md).

Scope is native and Docker onboarding. Kubernetes installation and cluster
repair are separate work. Existing authentication, authorisation, secret
handling and loopback boundaries must not be weakened for convenience.

## Evidence baseline and its limits

The following is Operator's fresh-user acceptance handoff, recorded here rather than
claimed as a new execution by the author of this document:

- Testing used a fresh Linux user and the Gitea `cairn-dist` distribution.
- The native source quickstart passed.
- Unchanged `make check` passed at distribution revision
  `336e0e68c915fd9f44de4b8a3a6c50955c283dfc`: 6,169 passed and 134 skipped.
  The dependency audit reported no known vulnerabilities. The 134 opt-in real
  database tests were not exercised by that default developer-suite run.
- Docker Compose installation succeeded with Attic enabled and semantic
  retrieval configured. A synthetic fact was committed and subsequently
  retrieved through semantic search.
- The first retrieval after ingest returned HTTP 503; a subsequent retrieval
  returned the saved fact. This establishes an observed readiness delay, not
  a rule that every 503 is retryable or that every failure is indexing delay.
- Documentation corrections were published through distribution revision
  `cc6b86d96cefce809cfd7beebd4bc47ed7666b91`.

This establishes a working path for a technically comfortable operator. It
does not establish beginner usability, persistent native operation across
restart/reboot, or completion of the acceptance work below. The reported
successful semantic round trip advances the earlier handoff in which that
check was still unverified; historical validation records remain historical.

## BI-1 — Prerequisites separated by purpose

Publish four distinct checklists, before their respective procedures:

1. Disposable native source quickstart.
2. Persistent native instance.
3. Docker Compose installation.
4. Full developer validation suite.

Each checklist must state supported platform and architecture assumptions,
required versions, exact commands for checking availability, required access
and permissions, and the expected result of each prerequisite check. Use the
actual executable names the commands require, such as `python3`; do not rely
on aliases or undocumented environment activation. Label optional tools and
their purpose separately from required tools.

Do not imply that Go, Bubblewrap, kubectl or the full developer toolchain is
required merely to run Cairn. If a particular path really needs one, explain
why on that path. Explain Docker daemon access, its privilege implications,
and any privileged directory creation or file-ownership operations before
installation starts. An unavailable prerequisite must lead to a documented
next step, not an agent-supplied workaround.

## BI-2 — Disposable and persistent native paths

Prominently label the existing quickstart as disposable: it stops the server
and removes its temporary data on exit. Put an obvious link to the persistent
path beside that warning; users must not infer persistence from the script.

Provide a complete persistent native procedure covering:

- Creation and preservation of stable instance identity.
- Persistent configuration and data locations, ownership and permissions.
- One-time bootstrap, recognition of an already bootstrapped instance and
  safe credential retention without repeating bootstrap on normal restart.
- Exact start, stop, restart and status commands with expected observations.
- Behaviour across reboot, including whether automatic startup is configured
  and how to enable or disable it for the supported procedure.
- Backup and recovery, with the required state, safe backup conditions,
  credential treatment and restoration checks clearly explained. Preserve
  instance identity and distinguish authoritative data from rebuildable indexes.

The implementation must select and document a supported operating procedure;
this requirements record does not choose a service manager or assert one is
already configured.

## BI-3 — Beginner-complete Docker Compose procedure

Provide one coherent sequence for the supported Compose configuration, with
Attic and optional semantic retrieval explained. Explain the role and storage
of each enabled component, what optional semantic retrieval needs, and what
users can verify when it is not configured. Do not leave prerequisite or
configuration steps scattered across unrelated guides without a clear order.

Distinguish literal commands from operator-supplied values. Give every
substitution a name, purpose and expected form. State the working directory
at each transition, and keep subsequent commands consistent with it.

At the point each matters, explain:

- Numeric UID/GID ownership, which files or directories need it, and which
  commands need privilege.
- Credential-file permissions, one-time bootstrap-token storage and safe
  retention, including how normal restarts reuse the established identity.
- Loopback-only access and the address used for local verification.
- Persistent volumes/bind mounts and the state they hold, including the
  authoritative catalogue, Attic evidence and rebuildable retrieval state.
- What the documented stop and down commands preserve, and precisely what
  volume deletion or directory removal destroys. Clearly distinguish the
  normal restart path from destructive cleanup.

Keep secrets out of command arguments, logs, Git and unsafe environment
handling. Preserve the existing security model. Neither this procedure nor
its acceptance run may depend on an agent silently correcting commands,
creating aliases or supplying undocumented environment setup.

## BI-4 — Retrieval readiness and useful failures

Replace the example's success-only rendering with a bounded verification
flow. Piping every response through
`jq '{hits, budget_consumed, budget_exhausted}'` hides error envelopes and
turns a failure into misleading null fields.

The documented flow must:

1. Explain that committed custody and completed indexing are different
   milestones. Retain and verify the ingest acknowledgement before polling
   retrieval.
2. Capture HTTP status, relevant headers and the response body without losing
   Cairn failure codes or diagnostic fields. Render success only after checking
   status and response shape; an error must remain recognisable as an error.
3. Classify retries using the documented API failure codes/classes, preserving
   and explaining `Retry-After`. Do not treat every 503, transport error or
   empty success response as an indexing delay.
4. Set explicit finite retry/elapsed-time bounds and per-request timeouts.
   Honour `Retry-After` within that overall budget; if the required wait cannot
   fit, report the deadline rather than retrying early or looping indefinitely.
5. Retry retrieval without repeating an already committed ingest. Preserve
   existing same-key/idempotent recovery rules for an uncertain ingest outcome;
   do not create a new mutation key merely because indexing is pending.
6. If indexing does not complete, report an actionable failure: the last HTTP
   status and Cairn code, what remains unverified, and the relevant documented
   status/log/recovery checks. Avoid disclosing credentials or unrelated data.
7. State what success proves: this instance accepted the synthetic fact and
   returned it for the documented scope, query and trust filter through the
   configured retrieval path. It does not prove every fact is indexed, general
   recall quality, backup recovery or production readiness.

Document and exercise the initial indexing-delay case explicitly, rather than
showing only the eventual successful response.

## Delivery and acceptance checklist

Delivery and fresh-user acceptance are separate. The delivery items below can
close after engineering verification and publication; every fresh-user acceptance
item remains pending until the published instructions have been followed literally.

- [x] Deliver BI-1 through BI-4 in the native entry documentation, Compose guide,
  deployment guide, client verification examples and relevant backup/recovery
  guidance, with consistent cross-links and no competing incomplete procedure.
- [x] Publish source/documentation changes through the normal reviewed
  distribution-export workflow. Reconcile source pins and public overlays;
  validate the exported guides rather than only the private source wording.
- [ ] Have a fresh inexperienced evaluator use a fresh supported user/environment
  and the final published revision, following commands literally except for
  explicitly documented substitutions. Record platform, versions, starting
  state, revision, substitutions, commands and outcomes without retaining secrets.
- [ ] Verify the disposable quickstart's advertised cleanup behaviour and ensure
  its warning makes the difference from persistent installation clear.
- [ ] Verify native persistence: restart the instance, confirm stable identity,
  reuse the retained credential and read the previously saved synthetic fact.
  Verify the documented reboot behaviour and backup/recovery procedure.
- [ ] Verify Docker startup, credential handling and synthetic write/read with
  Attic enabled and semantic retrieval configured. Verify identity and data
  retention across the documented restart path, and explain optional semantic
  configuration and the destructive cleanup boundary.
- [ ] Exercise the initial indexing delay and subsequent successful retrieval,
  plus a bounded unsuccessful wait that produces the documented actionable
  failure. Confirm retries do not repeat committed ingest or hide API errors.
- [ ] Record any missing instruction or agent intervention as a documentation
  defect. Fix it in source, republish, and rerun the affected acceptance path
  from a fresh state; do not repair the acceptance checkout to claim success.
- [ ] Obtain final beginner-installation acceptance against the published
  instructions. The prior operator round trip and passing `make check` do not
  close this gate.

## Work placement

This is a requirements supplement to
the documentation and stranger-quickstart plan,
with operational dependencies on
[deployment](../operations/deployment.md),
[Compose](../../deploy/compose/README.md),
[client examples](../clients.md) and
[backup/restore](../operations/backup-restore.md).
The [installation entry guide](../install.md), [persistent native procedure](../operations/native-installation.md)
and [bounded verification flow](../clients.md#bounded-ingest-and-retrieval-verification)
implement this record. Local synthetic checks are engineering evidence only;
they do not close the fresh-user, reboot, provider or recovery acceptance gates.
