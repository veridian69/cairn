"""Cairn legacy migration tooling (P-71).

Repository tooling, not a shipped component: the wheel packages only
``src/cairn`` (``pyproject.toml``), so this package never reaches the
wheel or the image. It runs from a checkout as
``uv run --locked python -m cairn_migrate <subcommand>``.

It reads offline snapshots (P-72) and speaks ``/v1`` over HTTP like any
other client (P-71) — it never touches a catalogue directly, never holds
productive credentials and never contacts the legacy VPS.
"""
