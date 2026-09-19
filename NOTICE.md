# Notices

Cairn
Copyright © 2026 Jon Kowszun

## FalkorDB runtime packaging

`deploy/falkordb/upstream/` retains the FalkorDB 4.20.4 server scripts and
licence text, including the package-cleanup failure-handling patch submitted
upstream. Copyright and licensing remain with the upstream authors under the
included `LICENSE.txt`. Cairn packaging modifications are dated 15 September
2026. The local build recipe fetches pinned FalkorDB, Redis and supporting
sources and builds an Ubuntu 24.04 runtime. These components retain their
upstream licences; the recipe installs the fetched source licence files in the
runtime under `/usr/share/licenses/cairn-falkordb`. See
`deploy/falkordb/README.md`. New Cairn releases do not distribute the locally
built database image or a corresponding-source bundle.
