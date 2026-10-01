"""Run the seven-turn demo against disposable Cairn and Garden instances."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def run_turn(
    argv: list[str],
    prompt: str,
    env: dict[str, str],
    cwd: Path,
    out_prefix: Path,
    timeout_s: int,
) -> dict[str, Any]:
    started = time.monotonic()
    with (
        open(f"{out_prefix}.stdout.jsonl", "xb") as stdout,
        open(f"{out_prefix}.stderr.log", "xb") as stderr,
    ):
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        try:
            process.communicate(prompt.encode(), timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            timed_out = True
    return {
        "exit": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "wall_s": round(time.monotonic() - started, 1),
    }


# --- orchestration -----------------------------------------------------------

import argparse  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
A2A = ROOT / "a2a" / "a2a"
# Not /tmp: Codex refuses to create its helper binaries (sandbox, apply_patch)
# under a temporary directory, which would leave agents without apply_patch.
RUNTIME_PARENT = Path.home() / ".cache" / "cairn-real-work-demo"
GARDEN_ACTORS = ("val", "spike", "verifier")
GIT_IDENTITY = {"val": "Val <val@jclk.ch>", "spike": "Spike <spike@jclk.ch>",
                "verifier": "Verifier <verifier@invalid>",
                # Distinct, so a cold-session push can never be taken for the fix branch.
                "spike-cold": "Spike cold session <spike-cold@invalid>"}  # fmt: skip


def _private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def retain_credentials(live: Path, out: Path, prefix: str) -> Path:
    """Keep the credential bytes a turn actually used (local, 0600, never published)."""
    kept = out / "config" / f"{prefix}.{live.name.lstrip('.')}"
    shutil.copyfile(live, kept)
    kept.chmod(0o600)
    return kept


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _cost(stdout: Path) -> dict[str, Any]:
    """Provider cost as each CLI reports it: Claude in USD, Codex in tokens."""
    usd, tokens = 0.0, 0
    for line in stdout.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            usd += float(event.get("total_cost_usd") or 0)
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            tokens += sum(int(v) for v in usage.values() if isinstance(v, int))
    return {"claude_usd": round(usd, 4), "codex_tokens": tokens}


def _serve(instance: object) -> tuple[str, object, threading.Thread]:
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(instance.application(), log_level="critical", access_log=False)  # type: ignore[attr-defined]
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Cairn did not start")
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server, thread


def _garden(
    runtime: Path, endpoint: str, instance_id: str, made: dict[str, Any], out: Path
) -> tuple[subprocess.Popen[bytes], dict[str, Path]]:
    from scripts.real_work_demo.actors import DEMO_SCOPE

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    data = runtime / "garden"
    data.mkdir(mode=0o700)
    config = runtime / "garden-host.json"
    _private(config, json.dumps({
        "gateway": {
            "listen": f"127.0.0.1:{port}",
            "data_dir": str(data / "data"),
            "daemon_url_file": str(data / "daemon.url"),
            "auth": {"endpoint": endpoint + "/memory/v1/diagnose", "instance_id": instance_id,
                     "scope": DEMO_SCOPE, "classification": "internal"},
            "principals": {str(made[n].principal): n for n in GARDEN_ACTORS},
        },
        "stream": {},
    }))  # fmt: skip
    log = (out / "garden-host.log").open("xb")
    env = {"HOME": str(runtime), "PATH": "/usr/bin:/bin"}
    host = subprocess.Popen([str(A2A), "host", "--config", str(config)], stdout=log,
                            stderr=subprocess.STDOUT, env=env)  # fmt: skip
    log.close()  # the child holds its own descriptor
    profiles: dict[str, Path] = {}
    for name in GARDEN_ACTORS:
        profiles[name] = runtime / f"{name}.garden.json"
        _private(profiles[name], json.dumps({
            "garden_endpoint": f"http://127.0.0.1:{port}/mcp",
            "credential_file": str(made[name].token_path),
            "instance_id": instance_id, "scope": DEMO_SCOPE,
            "classification": "internal", "participant": name, "adapter": "stdio",
        }))  # fmt: skip
    deadline = time.monotonic() + 20
    while subprocess.run([str(A2A), "doctor", "--profile", str(profiles["spike"])],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env).returncode:  # fmt: skip
        if host.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("Garden did not start")
        time.sleep(0.1)
    return host, profiles


def main(argv: list[str] | None = None) -> int:
    sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "transports" / "memory")]
    from memory_support import Instance

    from scripts.real_work_demo import (
        actors,
        agents,
        prompts,
        transcript,
        verify,
        workspace,
    )

    parser = argparse.ArgumentParser(prog="real_work_demo.run")
    parser.add_argument("label")
    parser.add_argument("--deepdiff", type=Path, required=True)
    parser.add_argument("--rev", default=workspace.DEEPDIFF_REV)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--max-turns", type=int, default=60)
    # Test-only: replace both provider CLIs with a local shell command.
    parser.add_argument("--fake-agent")
    args = parser.parse_args(argv)
    out: Path = args.out or ROOT / "build" / f"real-work-demo-{args.label}"
    out.mkdir(parents=True, exist_ok=False)
    (out / "config").mkdir(mode=0o700)

    locations: dict[str, Any] = {}
    if args.fake_agent is None:
        from scripts.host_handover import preflight

        locations = preflight(("codex", "claude"))

    RUNTIME_PARENT.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="rwd-", dir=RUNTIME_PARENT))
    runtime.chmod(0o700)
    server = thread = garden = None
    try:
        origin = runtime / "origin.git"
        workspace.make_bare(args.deepdiff, args.rev, origin)
        venv = runtime / "venv"
        # Built from a clone at the pin, never from the caller's checkout at whatever
        # revision it happens to have.
        build = workspace.clone_for(origin, runtime / "build", args.rev)
        lock_hash = workspace.build_venv(build, venv)
        subprocess.run(["chmod", "-R", "a-w", str(venv)], check=True)
        instance = Instance(runtime / "cairn", attic=True)
        made = actors.create_actors(instance, runtime)
        endpoint, server, thread = _serve(instance)
        instance_id = str(instance.config.instance_id)
        garden, profiles = _garden(runtime, endpoint, instance_id, made, out)

        meta: dict[str, Any] = {
            "schema": "cairn.real-work-demo/v1",
            "label": args.label,
            "instance_id": instance_id,
            "scope": actors.DEMO_SCOPE,
            "principals": {n: str(a.principal) for n, a in made.items()},
            "grants": {n: list(g) for n, g in actors.GRANTS.items()},
            "models_requested": {"spike": agents.SPIKE_MODEL, "val": agents.VAL_MODEL},
            "cli_versions": {
                name: subprocess.check_output(
                    [str(loc["binary"]), "--version"], text=True
                ).strip()
                for name, loc in locations.items()
            },
            "deepdiff_rev": args.rev,
            "venv_lock_sha256": lock_hash,
            "source_revision_at_start": _revision(),
            "runtime": str(runtime),
            "token_files": [str(a.token_path) for a in made.values()],
            "tokens_file": str(out / "config" / "tokens.json"),
            "prompts": prompts.PROMPTS,
            "turns": [],
        }
        run_loop = True
        for turn in agents.TURNS:
            if turn.prefix == agents.LOOP[0]:
                # Spec Amendment A: the correction loop runs only if T4 promoted nothing.
                with verify.Catalogue(instance.data_path / "catalogue.sqlite3") as cat:
                    run_loop = not verify.spike_fix_promoted(cat, meta["principals"])
            if turn.prefix in agents.LOOP and not run_loop:
                meta["turns"].append(
                    {"prefix": turn.prefix, "actor": turn.actor, "skipped": True}
                )
                _private(out / "run-metadata.json", json.dumps(meta, indent=2))
                continue
            base = runtime / turn.prefix
            base.mkdir(mode=0o700)
            work = workspace.clone_for(origin, base / "work", args.rev)
            tmp = base / "tmp"
            tmp.mkdir()
            actor = made[turn.actor]
            name, email = GIT_IDENTITY[turn.actor][:-1].split(" <")
            env = {
                "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "HOME": str(base), "TMPDIR": str(tmp), "DEMO_VENV": str(venv),
                "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
            }  # fmt: skip
            garden_profile = profiles.get(turn.actor) if turn.garden else None
            writable = (origin, tmp)
            if turn.provider == "claude":
                config = base / ".claude"
                config.mkdir(mode=0o700)
                mcp = out / "config" / f"{turn.prefix}.mcp.json"
                _private(
                    mcp,
                    json.dumps(
                        agents.claude_mcp(
                            turn, endpoint, actor.token, garden_profile, A2A
                        )
                    ),
                )
                settings = out / "config" / f"{turn.prefix}.settings.json"
                _private(
                    settings,
                    json.dumps(agents.claude_settings(work, writable=writable)),
                )
                env["CLAUDE_CONFIG_DIR"] = str(config)
                command = agents.claude_argv(Path(locations.get("claude", {}).get("binary", "claude")),
                                             mcp, settings, args.max_turns, turn)  # fmt: skip
                auth_name = ".credentials.json"
            else:
                config = base / ".codex"
                config.mkdir(mode=0o700)
                toml = agents.codex_toml(
                    turn, endpoint, garden_profile, A2A, writable=writable
                )
                _private(config / "config.toml", toml)
                shutil.copyfile(
                    config / "config.toml",
                    out / "config" / f"{turn.prefix}.config.toml",
                )
                env["CODEX_HOME"] = str(config)
                env["CAIRN_TOKEN"] = actor.token
                command = agents.codex_argv(Path(locations.get("codex", {}).get("binary", "codex")),
                                            out / f"{turn.prefix}-final.txt")  # fmt: skip
                auth_name = "auth.json"
            if args.fake_agent is None:
                shutil.copyfile(locations[turn.provider]["auth"], config / auth_name)
                (config / auth_name).chmod(0o600)
            else:
                command = ["bash", "-c", args.fake_agent]
            result = run_turn(command, prompts.PROMPTS[turn.prefix], env, work,
                              out / turn.prefix, args.timeout)  # fmt: skip
            if args.fake_agent is None:
                # CLIs refresh OAuth in place; keep what was live for the scrub.
                retain_credentials(config / auth_name, out, turn.prefix)
            meta["turns"].append({"prefix": turn.prefix, "actor": turn.actor, "result": result,
                                  "cost": _cost(out / f"{turn.prefix}.stdout.jsonl")})  # fmt: skip
            # Fail closed: a turn that ran unsandboxed voids the capture (check 8).
            if verify.unsandboxed(out, [turn.prefix]):
                meta["aborted"] = f"sandbox failed open in {turn.prefix}"
            _private(out / "run-metadata.json", json.dumps(meta, indent=2))
            if "aborted" in meta:
                break

        # Snapshot while everything is still up; verification reads these.
        snapshot = out / "snapshot"
        shutil.copytree(instance.data_path, snapshot / "cairn")
        shutil.copytree(
            runtime / "garden", snapshot / "garden", ignore_dangling_symlinks=True
        )
        shutil.copytree(origin, out / "origin.git")
        for turn in meta["turns"]:
            if turn.get("skipped"):
                continue
            shutil.copytree(
                runtime / turn["prefix"] / "work",
                out / "workspaces" / turn["prefix"],
                symlinks=True,
            )
        meta["source_revision_at_end"] = _revision()
        # Written only after every agent has exited: no agent shell can read it.
        # Local only, never published: the scrub (check 9) searches for these.
        _private(
            out / "config" / "tokens.json",
            json.dumps({n: a.token for n, a in made.items()}),
        )
        _private(out / "run-metadata.json", json.dumps(meta, indent=2))
        code = 0
        # Attic payloads are read while Cairn still serves, then persisted, so that
        # verification itself needs nothing that teardown destroys.
        try:
            with verify.Catalogue(snapshot / "cairn" / "catalogue.sqlite3") as cat:
                ids = set(cat.evidence_by_fact().values())
            payloads = verify.read_evidence(
                endpoint, made["verifier"].token, actors.DEMO_SCOPE, ids
            )
            verify.save_evidence(out, payloads)
        except Exception as error:  # recorded; never allowed to skip the rest
            meta["evidence_error"] = f"{type(error).__name__}: {error}"
            code = 1
        try:
            report = verify.verify(out, build_venv=args.fake_agent is None)
            (out / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        except Exception as error:
            meta["verification_error"] = f"{type(error).__name__}: {error}"
            code = 1
        try:
            transcript.write(out)
        except Exception as error:
            meta["transcript_error"] = f"{type(error).__name__}: {error}"
            code = 1
        _private(out / "run-metadata.json", json.dumps(meta, indent=2))
        return code
    finally:
        if garden is not None:
            garden.terminate()
            try:
                garden.wait(timeout=5)
            except subprocess.TimeoutExpired:
                garden.kill()
                garden.wait()
        if server is not None:
            server.should_exit = True  # type: ignore[attr-defined]
            thread.join(timeout=5)  # type: ignore[union-attr]
        subprocess.run(["chmod", "-R", "u+w", str(runtime)], check=False)
        shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
