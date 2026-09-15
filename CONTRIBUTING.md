# Contributing to Cairn

Cairn welcomes focused bug reports, documentation corrections and pull
requests against this public repository. Discuss substantial behaviour or
interface changes in an issue before investing in an implementation.

## Development setup

Cairn's source launcher accepts host Python 3.12–3.14. Its locked managed
application runtime and container use Python 3.14. Development also requires
Linux, uv 0.12.14, Go, Docker with the Compose plugin, and the other tools
listed in the [developer prerequisites](docs/install.md#full-developer-validation).
From a trusted checkout:

```sh
uv sync --locked
./scripts/fetch-kubectl
make check
```

`make check` is the complete local repository gate. It checks formatting,
linting, typing, tests, generated contracts, deployment renders, licence and
dependency policy. Some host and live deployment acceptance remains manual and
environment-specific.

GitHub pull-request checks run the lightweight policy and wheel checks, build
and smoke-test a local image, and scan that image. They do not run the full
repository gate. The full hosted `make check` workflow and host-isolation
diagnostics are separate manual runs.

## Pull requests

Keep each pull request narrow and explain the problem, resulting behaviour and
verification. Add or update tests when behaviour changes, and update generated
contracts or deployment renders when their sources change. Do not weaken
scope, grant, classification, provenance or secret-handling boundaries to make
a test pass.

The public repository is a sanitised export. Maintainers review and merge
accepted pull requests here, then retain contributor attribution while
integrating those changes into the private development repository before the
next sanitised export. Sanitisation can change commit identities and history,
but it does not make the public review disposable.

Never submit private repository history, internal-only material, productive
data, credentials, tokens, private memory contents or generated files that
contain them. Use synthetic fixtures and redact diagnostic output before
posting it publicly. Report suspected vulnerabilities through
[`SECURITY.md`](SECURITY.md), not a public issue.

All project material is licensed under Apache License 2.0; see
[`LICENSE.md`](LICENSE.md). Submit only material you have the right to
contribute.
