# Notices

Cairn
Copyright © 2026 Jon Kowszun

## FalkorDB runtime packaging

`deploy/falkordb/upstream/` retains unmodified FalkorDB v4.20.7 server recipe,
scripts and licence text from the pinned upstream tag. Copyright and licensing
remain with the upstream authors under the included `LICENSE.txt`. Cairn's
local source-build packaging is refreshed on 14 October 2026; see
`deploy/falkordb/MODIFICATIONS.md`. The local build recipe fetches pinned FalkorDB, Redis and supporting
sources and builds an Ubuntu 24.04 runtime. These components retain their
upstream licences; the recipe installs the fetched source licence files in the
runtime under `/usr/share/licenses/cairn-falkordb`. See
`deploy/falkordb/README.md`. New Cairn releases do not distribute the locally
built database image or a corresponding-source bundle.
