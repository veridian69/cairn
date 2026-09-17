# Modification notice — Cairn FalkorDB v4.20.4-cairn.1

Modified on 15 September 2026 from FalkorDB v4.20.4, upstream commit
`5ac6db8059013c9d74842c02b6a9f1a4858a6a1b`.

The maintained source revision is
`c3fea9bee0d7d4ad6c24308f18a19fc8c81c2996`. It changes the Debian runtime
steps in `build/docker/Dockerfile` and `build/docker/Dockerfile.server` so the
optional final `apt-get autoremove` fallback cannot mask a failure in an
earlier package update or removal command. The change was submitted upstream
as [FalkorDB PR #2838](https://github.com/FalkorDB/FalkorDB/pull/2838).

The `v4.20.4-cairn.1` image uses the server-only recipe. It omits FalkorDB
Browser, refreshes the Debian packages available from the pinned Redis 8.6.3
base at build time, and removes packages that the runtime does not require.

The FalkorDB module was not recompiled. It was copied unchanged from
`falkordb/falkordb:v4.20.4@sha256:adbddd418916c25618564ff8597a919b08bc76452ebeb74eb985c38d7281df62`.
The source and resulting runtime module both have SHA-256
`81ea6b989dc2fd4c9ad905e246018b220b02f0e40c406255f9da4768c1684555`.

The modified FalkorDB material remains under its upstream Server Side Public
License. Redis source is supplied under the Server Side Public License option
in Redis's upstream tri-licence file. Other bundled components retain their
own notices and terms. Nothing in this bundle relicenses those components
under Cairn's Apache licence.
