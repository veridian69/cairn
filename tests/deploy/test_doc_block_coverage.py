"""Every shell block in the guides a blind tester walks is executed or excused.

Blind acceptance executes the public documentation literally, so a fenced
block nobody runs before release is a finding waiting to happen. Each block
in the guides below must be executed verbatim by the pre-blind rehearsal
(``scripts/pre-blind`` extracts it through ``scripts/doc_blocks.py``), be
executed by another repository test, or carry an explicit reason here. A new
block therefore fails this test until someone decides how it is proved.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location(
    "doc_blocks", ROOT / "scripts" / "doc_blocks.py"
)
assert _spec is not None and _spec.loader is not None
_doc_blocks = importlib.util.module_from_spec(_spec)
sys.modules["doc_blocks"] = _doc_blocks
_spec.loader.exec_module(_doc_blocks)
shell_blocks = _doc_blocks.shell_blocks
REHEARSAL = ROOT / "scripts" / "pre-blind"
GUIDES = (
    "docs/install.md",
    "docs/clients.md",
    "docs/operations/guided-installation.md",
    "docs/operations/kubernetes-gateway.md",
    "docs/operations/managed-garden.md",
    "docs/operations/deployment.md",
    "deploy/falkordb/README.md",
)

EXAMPLE = "example command with a placeholder name; the rehearsal runs the same subcommand for every configuration"
SITE_VALUES = "site-value assignments the rehearsal supplies as installer flags"
MANUAL = "manual site-manifest procedure outside the guided installer; renders are gated by make render"
HOST_PROVISIONING = "distribution-specific host provisioning; the reference host is provisioned separately"

# Blocks other tests execute: (guide, heading, index) -> test module.
EXECUTED_ELSEWHERE = {
    ("docs/operations/kubernetes-gateway.md", "Remove the shared gateway safely", 4): (
        "tests/deploy/test_gateway_removal_docs.py"
    ),
}

# Blocks deliberately not executed, each with the reason.
EXCUSED = {
    ("docs/install.md", "Quick install with the guided installer", 1): EXAMPLE,
    ("docs/install.md", "Quick install with the guided installer", 2): EXAMPLE,
    (
        "docs/install.md",
        "Obtain a trusted checkout",
        1,
    ): "clones the public repository; the rehearsal runs inside the checkout",
    ("docs/install.md", "Get missing prerequisites", 1): HOST_PROVISIONING,
    ("docs/install.md", "Get missing prerequisites", 2): HOST_PROVISIONING,
    ("docs/install.md", "Get missing prerequisites", 3): HOST_PROVISIONING,
    ("docs/install.md", "Get missing prerequisites", 4): HOST_PROVISIONING,
    (
        "docs/install.md",
        "Connect locally for verification",
        1,
    ): "foreground port-forward; the rehearsal runs the port-forward command the installer prints",
    (
        "docs/install.md",
        "Full developer validation",
        1,
    ): "runs make check itself; the repository gate executes it",
    (
        "docs/operations/guided-installation.md",
        "Start the interactive wizard",
        1,
    ): "interactive wizard; non-interactive forms are exercised",
    ("docs/operations/guided-installation.md", "Run without prompts", 1): EXAMPLE,
    ("docs/operations/guided-installation.md", "Run without prompts", 2): EXAMPLE,
    (
        "docs/operations/guided-installation.md",
        "Run without prompts",
        3,
    ): "packaged cairn-install form; the rehearsal uses the source launcher",
    (
        "docs/operations/guided-installation.md",
        "Install to an existing Kubernetes namespace",
        1,
    ): "registry-image example; the reference host has no registry, the rehearsal uses the staged image path",
    ("docs/operations/guided-installation.md", "Output and diagnostics", 1): EXAMPLE,
    (
        "docs/operations/guided-installation.md",
        "Supply the OpenAI key without exposing it",
        1,
    ): "opens a local editor; the rehearsal passes a protected key file",
    (
        "docs/operations/guided-installation.md",
        "Supply the OpenAI key without exposing it",
        2,
    ): "opens a local editor; the rehearsal passes a protected key file",
    ("docs/operations/guided-installation.md", "Inspect and resume", 1): EXAMPLE,
    ("docs/operations/guided-installation.md", "Inspect and resume", 2): EXAMPLE,
    (
        "docs/operations/guided-installation.md",
        "Inspect and resume",
        3,
    ): "example port-forward; the rehearsal runs the command the installer prints",
    ("docs/operations/guided-installation.md", "Inspect and resume", 4): EXAMPLE,
    ("docs/operations/guided-installation.md", "Preserving rollback", 1): EXAMPLE,
    (
        "docs/operations/guided-installation.md",
        "Permanently remove a named installation",
        1,
    ): "interactive confirmation; the non-interactive form is exercised",
    (
        "docs/operations/guided-installation.md",
        "Permanently remove a named installation",
        2,
    ): EXAMPLE,
    (
        "docs/operations/managed-garden.md",
        "Configure HTTPS and participants",
        1,
    ): "example invocation; the rehearsal runs the helper for each Garden shape",
    (
        "docs/operations/managed-garden.md",
        "Configure HTTPS and participants",
        2,
    ): EXAMPLE,
    ("docs/operations/managed-garden.md", "Native and Docker", 1): EXAMPLE,
    (
        "docs/operations/managed-garden.md",
        "Complete the configuration and install",
        2,
    ): SITE_VALUES,
    (
        "docs/operations/managed-garden.md",
        "Complete the configuration and install",
        3,
    ): "registry-published Garden image; the reference host has no registry",
    (
        "docs/operations/managed-garden.md",
        "Complete the configuration and install",
        4,
    ): EXAMPLE,
    ("docs/operations/managed-garden.md", "Recovery and removal", 1): EXAMPLE,
    ("docs/operations/managed-garden.md", "Recovery", 1): EXAMPLE,
    ("docs/operations/managed-garden.md", "Recovery", 2): EXAMPLE,
    (
        "docs/operations/deployment.md",
        "Customise instance labels without changing external destinations",
        1,
    ): MANUAL,
    (
        "docs/operations/deployment.md",
        "Customise instance labels without changing external destinations",
        2,
    ): MANUAL,
    ("docs/operations/deployment.md", "Namespace and render preparation", 3): MANUAL,
    (
        "deploy/falkordb/README.md",
        "Install with the prepared nodes",
        1,
    ): "a flag fragment, not a command; the rehearsal passes the receipt flag",
}


def _rehearsal_blocks() -> set[tuple[str, str, int]]:
    """Blocks the rehearsal extracts: `block "$GUIDE_VAR" "Heading" N ...`."""
    text = REHEARSAL.read_text()
    guides = dict(re.findall(r"^([A-Z_]+_GUIDE|GUIDED)=(\S+\.md)$", text, re.MULTILINE))
    calls = re.findall(r'block "\$([A-Z_]+)" "([^"]+)" (\d+)', text)
    assert calls, "the rehearsal extracts no documented blocks"
    return {
        (guides[variable], heading, int(index)) for variable, heading, index in calls
    }


def _guide_blocks() -> set[tuple[str, str, int]]:
    found: set[tuple[str, str, int]] = set()
    for guide in GUIDES:
        for block in shell_blocks((ROOT / guide).read_text()):
            found.add((guide, block.heading, block.index))
    return found


def test_every_guide_shell_block_is_executed_or_excused() -> None:
    documented = _guide_blocks()
    executed = _rehearsal_blocks() | set(EXECUTED_ELSEWHERE)
    excused = set(EXCUSED)

    unaccounted = sorted(documented - executed - excused)
    assert not unaccounted, "guide blocks nobody executes:\n" + "\n".join(
        map(str, unaccounted)
    )

    stale = sorted((executed | excused) - documented)
    assert not stale, "entries for blocks that no longer exist:\n" + "\n".join(
        map(str, stale)
    )

    both = sorted(executed & excused)
    assert not both, "excused blocks the rehearsal executes anyway:\n" + "\n".join(
        map(str, both)
    )


def test_rehearsal_references_resolve_to_existing_blocks() -> None:
    for guide, heading, index in sorted(_rehearsal_blocks()):
        blocks = [
            b for b in shell_blocks((ROOT / guide).read_text()) if b.heading == heading
        ]
        assert blocks, f"{guide}: no shell block under {heading!r}"
        assert index <= len(blocks), (
            f"{guide}: {heading!r} has {len(blocks)} blocks, not {index}"
        )
