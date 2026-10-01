"""Scrub local paths and refuse publication if any run secret survives (check 9)."""

from __future__ import annotations

import getpass
import json
import re
import shutil
import tempfile
from pathlib import Path


def published(out: Path) -> list[str]:
    """The transcript and every verification report, including later re-verifications."""
    return ["transcript.md", *sorted(p.name for p in out.glob("verification*.json"))]


def scrub(text: str, replacements: dict[str, str]) -> str:
    for old in sorted(replacements, key=len, reverse=True):
        text = text.replace(old, replacements[old])
    return text


def _leaves(value: object) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _leaves(v)]
    if isinstance(value, list):
        return [s for v in value for s in _leaves(v)]
    return [value] if isinstance(value, str) and len(value) >= 16 else []


def _run_tokens(out: Path) -> dict[str, str]:
    tokens: dict[str, str] = json.loads((out / "config" / "tokens.json").read_text())
    return tokens


def collect_secrets(out: Path, credential_files: list[Path]) -> list[bytes]:
    """Every run bearer token and every provider credential, whole and by leaf.

    Includes each turn's retained credential copy (config/<prefix>.*.json): the CLIs
    refresh OAuth in place, so those hold the bytes that were live during the run.
    """
    secrets = [t.encode() for t in _run_tokens(out).values()]
    retained = sorted((out / "config").glob("t[0-9]-*.credentials.json"))
    retained += sorted((out / "config").glob("t[0-9]-*.auth.json"))
    for cred in [*credential_files, *retained]:
        raw = cred.read_bytes()
        secrets.append(raw)
        secrets += [s.encode() for s in _leaves(json.loads(raw))]
    return [s for s in secrets if s]


def secret_hits(paths: list[Path], secrets: list[bytes]) -> list[str]:
    return [str(p) for p in paths if any(s in p.read_bytes() for s in secrets)]


def scan(paths: list[Path], out: Path, credential_files: list[Path]) -> list[str]:
    """Check 9 over files outside the publication set: story.json before rendering
    (a token rendered into pixels cannot be found later) and the companion page."""
    return secret_hits(paths, collect_secrets(out, credential_files))


def scrub_account(text: str, user: str) -> str:
    """The local account name as a word (ls -l owners); never inside an email or domain."""
    return re.sub(rf"(?<![@\w.-]){re.escape(user)}(?![\w.@-])", "<user>", text)


def _slug(path: str) -> str:
    """How Claude Code names a directory after a path: / and . become -."""
    return path.replace("/", "-").replace(".", "-")


def publish(
    out: Path,
    dest: Path,
    credential_files: list[Path],
    extra: dict[str, str] | None = None,
) -> list[Path]:
    meta = json.loads((out / "run-metadata.json").read_text())
    runtime, home = meta["runtime"], str(Path.home())
    replacements = {runtime: "<runtime>", home: "<home>",
                    _slug(runtime): "<runtime-slug>", _slug(home): "<home-slug>"}  # fmt: skip
    replacements |= extra or {}
    # Run tokens die with the disposable instance: redact them. Provider
    # credentials stay fail-closed below.
    replacements |= {
        token: f"<token:{name}>" for name, token in _run_tokens(out).items()
    }
    secrets = collect_secrets(out, credential_files)
    with tempfile.TemporaryDirectory() as raw:
        staging = Path(raw)
        staged = []
        for name in published(out):
            target = staging / name
            text = scrub((out / name).read_text(), replacements)
            target.write_text(scrub_account(text, getpass.getuser()))
            staged.append(target)
        hits = secret_hits(staged, secrets)
        if hits:
            raise RuntimeError(
                f"secret material in: {', '.join(Path(h).name for h in hits)}"
            )
        dest.mkdir(parents=True, exist_ok=False)
        return [Path(shutil.copy2(p, dest / p.name)) for p in staged]
