# Third-party software notices

This inventory covers the third-party Go modules linked into the Linux `a2a`
binary. It was generated from the locked module graph with:

```text
go list -mod=readonly -deps -f '{{with .Module}}{{if .Version}}{{.Path}}|{{.Version}}|{{.Dir}}{{end}}{{end}}' .
```

The review used the licence and notice files in the repository-local Go module
cache for the exact versions below, plus the active locked Go toolchain licence
for standard-library code linked into the binary. Those source files are
reproduced without alteration under
`THIRD_PARTY_LICENSES/<module>@<version>/`. Supplemental files listed after the
inventory preserve notices for linked subpackages or bundled code within a
module.

| Module | Version | Licence identified from the bundled source file |
| --- | --- | --- |
| `github.com/antithesishq/antithesis-sdk-go` | `v0.6.0-default-no-op` | MIT |
| `github.com/atotto/clipboard` | `v0.1.4` | BSD-3-Clause |
| `github.com/aymanbagabas/go-osc52/v2` | `v2.0.1` | MIT |
| `github.com/charmbracelet/bubbles` | `v0.21.0` | MIT |
| `github.com/charmbracelet/bubbletea` | `v1.3.10` | MIT |
| `github.com/charmbracelet/colorprofile` | `v0.2.3-0.20250311203215-f60798e515dc` | MIT |
| `github.com/charmbracelet/lipgloss` | `v1.1.0` | MIT |
| `github.com/charmbracelet/x/ansi` | `v0.10.1` | MIT |
| `github.com/charmbracelet/x/cellbuf` | `v0.0.13-0.20250311204145-2c3ea96c31dd` | MIT |
| `github.com/charmbracelet/x/term` | `v0.2.1` | MIT |
| `github.com/dustin/go-humanize` | `v1.0.1` | MIT |
| `github.com/google/jsonschema-go` | `v0.4.3` | MIT |
| `github.com/google/uuid` | `v1.6.0` | BSD-3-Clause |
| `github.com/klauspost/compress` | `v1.18.5` | BSD-3-Clause |
| `github.com/lucasb-eyer/go-colorful` | `v1.2.0` | MIT |
| `github.com/mattn/go-isatty` | `v0.0.20` | MIT |
| `github.com/mattn/go-runewidth` | `v0.0.16` | MIT |
| `github.com/minio/highwayhash` | `v1.0.4-0.20251030100505-070ab1a87a76` | Apache-2.0 |
| `github.com/modelcontextprotocol/go-sdk` | `v1.7.0` | Apache-2.0 and MIT (file-level transition described in the upstream licence) |
| `github.com/muesli/ansi` | `v0.0.0-20230316100256-276c6243b2f6` | MIT |
| `github.com/muesli/cancelreader` | `v0.2.2` | MIT |
| `github.com/muesli/termenv` | `v0.16.0` | MIT |
| `github.com/nats-io/jwt/v2` | `v2.8.1` | Apache-2.0 |
| `github.com/nats-io/nats-server/v2` | `v2.12.6` | Apache-2.0 |
| `github.com/nats-io/nats.go` | `v1.50.0` | Apache-2.0 |
| `github.com/nats-io/nkeys` | `v0.4.15` | Apache-2.0 |
| `github.com/nats-io/nuid` | `v1.0.1` | Apache-2.0 |
| `github.com/remyoudompheng/bigfft` | `v0.0.0-20230129092748-24d4a6f8daec` | BSD-3-Clause |
| `github.com/rivo/uniseg` | `v0.4.7` | MIT |
| `github.com/segmentio/asm` | `v1.1.3` | MIT |
| `github.com/segmentio/encoding` | `v0.5.4` | MIT |
| `github.com/spf13/cobra` | `v1.10.2` | Apache-2.0 |
| `github.com/spf13/pflag` | `v1.0.9` | BSD-3-Clause |
| `github.com/xo/terminfo` | `v0.0.0-20220910002029-abceb7e1c41e` | MIT |
| `github.com/yosida95/uritemplate/v3` | `v3.0.2` | BSD-3-Clause |
| `golang.org/x/crypto` | `v0.49.0` | BSD-3-Clause |
| `golang.org/x/oauth2` | `v0.35.0` | BSD-3-Clause |
| `golang.org/x/sync` | `v0.20.0` | BSD-3-Clause |
| `golang.org/x/sys` | `v0.42.0` | BSD-3-Clause |
| `golang.org/x/term` | `v0.41.0` | BSD-3-Clause |
| `golang.org/x/time` | `v0.15.0` | BSD-3-Clause |
| Go standard library and runtime reference texts | `go1.25.0` | BSD-3-Clause; the actual compiler version for each binary is recorded with `go version -m` |
| `gopkg.in/yaml.v3` | `v3.0.1` | MIT and Apache-2.0 (file-level split described in `LICENSE` and `NOTICE`) |
| `modernc.org/libc` | `v1.70.0` | BSD-3-Clause, with additional permissive terms in `LICENSE-3RD-PARTY.md` |
| `modernc.org/mathutil` | `v1.7.1` | BSD-3-Clause |
| `modernc.org/memory` | `v1.11.0` | BSD-3-Clause, with bundled BSD-3-Clause notices |
| `modernc.org/sqlite` | `v1.48.0` | BSD-3-Clause, with upstream SQLite code dedicated to the public domain |

Supplemental source files included in the bundle:

- `go.dev/toolchain@go1.25.0/LICENSE` and `PATENTS` are the reviewed reference
  texts for the Go standard library and runtime. The licence check compares the
  active Go 1.25-or-newer toolchain to these exact bytes; the binary's actual
  compiler version remains part of per-artefact `go version -m` evidence.
- `github.com/klauspost/compress@v1.18.5/LICENSE-s2` for the linked `s2`
  package.
- `gopkg.in/yaml.v3@v3.0.1/NOTICE` from the module root.
- `modernc.org/libc@v1.70.0/LICENSE-3RD-PARTY.md` for code incorporated from
  Go, musl libc, go-netdb, and NixOS/nixpkgs.
- `modernc.org/mathutil@v1.7.1/LICENSE-mersenne` for the bundled Mersenne
  implementation.
- `modernc.org/memory@v1.11.0/LICENSE-GO`, `LICENSE-MMAP-GO`, and
  `LICENSE-LOGO` exactly as supplied by that module.
- `modernc.org/sqlite@v1.48.0/SQLITE-LICENSE` for the upstream SQLite code's
  public-domain dedication.

The reviewed material contains permissive MIT, BSD, and Apache terms. No
copyleft or licence-incompatibility blocker was found for redistribution of
the compiled Linux binary or container image. This conclusion applies to the
locked versions above; changing the dependency graph requires a fresh review.
