"""P-49's bound on the pinned MCP SDK's reachable surface.

``mcp==1.29.0`` drags a larger closure into the runtime than the adapter
uses: ``pyjwt[crypto]`` (and so ``cryptography``) and ``python-multipart``
arrive for the SDK's OAuth and authorisation-server support, which I-88
says Cairn deliberately does not implement — there is no
protected-resource metadata document, no discovery endpoint and no
``resource_metadata`` challenge parameter, because I-24 excludes OAuth
outright and the bearer credential is admitted at the HTTP layer.

**P-49's original bound was unsatisfiable, and Operator amended it on
10 August 2026 during Task 2.** The pin said no module reachable from
``build_application`` may import ``mcp.server.auth``. It cannot hold: the
adapter must import ``mcp.server.lowlevel``, importing any submodule
imports its parent package, and ``mcp/server/__init__.py`` eagerly does
``from .fastmcp import FastMCP``, which reaches
``mcp.server.auth.middleware.bearer_auth`` and with it ``provider`` and
``settings``. That is the SDK's own package layout and no import
discipline of Cairn's can avoid it.

What is enforced instead, in three parts:

- **The allow-list.** Every ``mcp`` import in ``src/cairn`` must be under
  one of the original three roots P-49 permits. Only
  ``client/conversation_mcp.py`` may additionally import ``mcp.server.stdio``
  for its host-side HTTP client bridge (12 September 2026). The Cairn
  authority server remains HTTP-only under I-84. This is the primary bound and it
  subsumes a denial list: ``mcp.server.auth`` is refused because it is not
  permitted, not because it is named. Both independent reviewers of the
  first attempt found the same hole — a denial list alone lets a later
  module reach any *other* SDK surface, and one path is live rather than
  theoretical. ``FastMCP`` is bound as ``mcp.server.FastMCP`` the moment
  anything imports ``mcp.server.lowlevel``, so ``from mcp.server import
  FastMCP`` followed by ``FastMCP(auth=AuthSettings(...))`` would mount
  real OAuth middleware while the string ``mcp.server.auth`` never appears
  in that module's source. The allow-list refuses ``mcp.server`` and so
  refuses that.
- **The runtime endpoints.** ``mcp.server.auth.handlers.*`` and
  ``mcp.server.auth.routes`` — the modules implementing the authorization
  specification's endpoints — are never imported, checked in a fresh
  interpreter so the result cannot depend on what the rest of the suite
  imported first.
- **The detectors' own teeth.** Each scan is exercised against a synthetic
  offending file, so a detector that silently stopped detecting fails
  here rather than passing quietly for the rest of the slice.

The residue is stated rather than hidden. ``mcp.server.auth.provider`` and
``.settings`` are Protocols and settings models; ``.middleware.*`` is
**live ASGI middleware**, not inert types — a reviewer read them and
corrected that framing. Cairn instantiates none of them and mounts none of
their routes, and the allow-list is what keeps a later task from reaching
the constructor that would.

*Known limit, stated because a silent one is worse:* the source scan reads
import statements. A dynamic ``importlib.import_module("mcp.server.auth")``
or ``__import__`` call is a function call on a string, not an import node,
and would not be seen. Nothing in Cairn imports dynamically, and the
runtime half would still catch the OAuth endpoints if something did.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[3] / "src" / "cairn"

# P-49: the only SDK modules the adapter may reach. A name is permitted if
# it is one of these or lies beneath one.
PERMITTED_ROOTS = (
    "mcp.types",
    "mcp.server.lowlevel",
    "mcp.server.streamable_http_manager",
)
STDIO_ROOT = "mcp.server.stdio"
RUNTIME_ROOTS = (*PERMITTED_ROOTS, STDIO_ROOT)

# The authorization specification's endpoint implementations — what I-88
# disclaims, and what must never reach ``sys.modules``.
FORBIDDEN_AT_RUNTIME = ("mcp.server.auth.handlers", "mcp.server.auth.routes")


def under(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in roots)


def imported_names(source: Path) -> set[str]:
    """Every module name a file imports, by parsing rather than grepping —
    a comment or a docstring naming a package is not an import, and this
    module's own prose would otherwise trip every check below."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if type(node) is ast.Import:
            names.update(alias.name for alias in node.names)
        elif type(node) is ast.ImportFrom and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def offending_imports(source: Path) -> set[str]:
    """``mcp`` imports in one file that P-49 does not permit."""
    permitted: tuple[str, ...] = PERMITTED_ROOTS
    if source == SOURCE_ROOT / "client" / "conversation_mcp.py":
        permitted = (*permitted, STDIO_ROOT)
    return {
        name
        for name in imported_names(source)
        if under(name, ("mcp",)) and not under(name, permitted)
    }


def test_cairn_imports_no_sdk_module_outside_the_permitted_roots() -> None:
    """P-49's allow-list, the primary bound: ``the adapter imports only
    mcp.types, mcp.server.lowlevel and mcp.server.streamable_http_manager``.
    Only the conversation client entrypoint has the explicit stdio allowance.

    Stated as an allow-list rather than a denial list on both reviewers'
    finding: refusing only ``mcp.server.auth`` would leave
    ``from mcp.server import FastMCP`` — and the OAuth middleware its
    ``auth=`` argument mounts — perfectly legal.
    """
    sources = sorted(SOURCE_ROOT.rglob("*.py"))
    assert sources, f"no sources found under {SOURCE_ROOT}; the scan is vacuous"

    offenders = {
        str(source.relative_to(SOURCE_ROOT)): sorted(found)
        for source in sources
        if (found := offending_imports(source))
    }
    assert offenders == {}


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("import mcp.server.auth.provider", id="denied-auth-submodule"),
        pytest.param("from mcp.server.auth import provider", id="denied-auth-from"),
        pytest.param("from mcp.server import FastMCP", id="denied-fastmcp"),
        pytest.param("import mcp.server.fastmcp", id="denied-fastmcp-module"),
        pytest.param("import mcp.client", id="denied-client"),
        pytest.param("import mcp", id="denied-bare-root"),
        pytest.param("import mcp.server.auth as x", id="denied-aliased"),
    ],
)
def test_the_source_scan_rejects_each_disallowed_import(
    tmp_path: Path, statement: str
) -> None:
    """The scan's teeth, as an artefact rather than a claim in a commit
    message. A detector is only worth its assertion if something proves it
    still detects; each case here is a real way a later task could reach
    past the allow-list."""
    offender = tmp_path / "offender.py"
    offender.write_text(f"{statement}\n", encoding="utf-8")
    assert offending_imports(offender) != set()


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("from mcp.types import CallToolResult", id="types"),
        pytest.param("from mcp.server.lowlevel import Server", id="lowlevel"),
        pytest.param(
            "from mcp.server.streamable_http_manager import "
            "StreamableHTTPSessionManager",
            id="streamable-http",
        ),
        pytest.param("import json", id="unrelated-stdlib"),
    ],
)
def test_the_source_scan_admits_each_permitted_import(
    tmp_path: Path, statement: str
) -> None:
    """The other half of the teeth: a scan that rejected everything would
    also pass the test above while making the allow-list unusable."""
    permitted = tmp_path / "permitted.py"
    permitted.write_text(f"{statement}\n", encoding="utf-8")
    assert offending_imports(permitted) == set()


@pytest.mark.parametrize(
    "relative,permitted",
    [
        ("client/conversation_mcp.py", True),
        ("transports/mcp/server.py", False),
        ("client/another_bridge.py", False),
        ("conversation_mcp.py", False),
    ],
)
def test_stdio_import_is_limited_to_the_conversation_client_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, permitted: bool
) -> None:
    source_root = tmp_path / "src" / "cairn"
    monkeypatch.setattr(sys.modules[__name__], "SOURCE_ROOT", source_root)
    source = source_root / relative
    source.parent.mkdir(parents=True)
    source.write_text("from mcp.server.stdio import stdio_server\n")
    assert (offending_imports(source) == set()) is permitted


@pytest.mark.parametrize("entrypoint", [None, "cairn.client.conversation_mcp"])
def test_the_authorization_endpoints_are_never_imported_at_runtime(
    entrypoint: str | None,
) -> None:
    """Run in a fresh interpreter so the result cannot depend on what the
    rest of the suite imported first — an order-dependent guard is not a
    guard. Exercise both the four permitted SDK modules alone and the
    concrete conversation adapter entrypoint: neither dependency closure
    may reach the OAuth endpoints."""
    programme = (
        "import sys\n"
        f"for name in {RUNTIME_ROOTS!r}:\n"
        "    __import__(name)\n"
        f"entrypoint = {entrypoint!r}\n"
        "if entrypoint is not None:\n"
        "    __import__(entrypoint)\n"
        f"forbidden = {FORBIDDEN_AT_RUNTIME!r}\n"
        "found = sorted(\n"
        "    n for n in sys.modules\n"
        "    if any(n == r or n.startswith(r + '.') for r in forbidden)\n"
        ")\n"
        "print(','.join(found))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", programme],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == ""


def test_the_forbidden_runtime_modules_exist_in_the_pinned_sdk() -> None:
    """The runtime guard is only worth having if it names something real:
    were the handlers renamed by an SDK upgrade it would pass vacuously
    against modules that no longer exist, looking enforced while enforcing
    nothing.

    Checked on the distribution's file listing in a subprocess rather than
    by importing — importing them is precisely what the guard above
    forbids, and doing it in this process would make the two tests
    order-dependent on each other.
    """
    programme = (
        "import mcp, pathlib\n"
        "root = pathlib.Path(mcp.__file__).parent / 'server' / 'auth'\n"
        "print((root / 'routes.py').is_file(), (root / 'handlers').is_dir())\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", programme],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "True True"


@pytest.mark.parametrize("module_name", RUNTIME_ROOTS)
def test_the_permitted_sdk_modules_are_importable(module_name: str) -> None:
    """The positive control: the four modules P-49 permits are present in
    the pinned version, so a later task building against them is building
    against something real."""
    completed = subprocess.run(
        [sys.executable, "-c", f"__import__({module_name!r})"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
