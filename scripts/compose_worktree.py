#!/usr/bin/env python3
"""Resolve and run a Compose project isolated to the current Git worktree."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys

PROJECT_PREFIX = "spine"
PORT_BLOCK_BASE = 20_000
PORT_BLOCK_SIZE = 8
PORT_SLOT_COUNT = 4_096
PORT_NAMES = (
    "SPINE_WEB_PORT",
    "SPINE_WEB_FIXTURES_PORT",
    "SPINE_API_PORT",
    "SPINE_POSTGRES_PORT",
    "SPINE_TEMPORAL_PORT",
    "SPINE_TEMPORAL_UI_PORT",
    "SPINE_NEO4J_HTTP_PORT",
    "SPINE_NEO4J_BOLT_PORT",
)


class ComposeContextError(RuntimeError):
    """Raised when the current checkout cannot be resolved safely."""


@dataclass(frozen=True)
class ComposeContext:
    is_primary: bool
    ports: dict[str, int]
    project: str
    worktree: str


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ComposeContextError(detail or "Git worktree metadata is unavailable")
    return completed.stdout.strip()


def _worktrees(cwd: Path) -> list[Path]:
    output = _git("worktree", "list", "--porcelain", cwd=cwd)
    paths = [
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]
    if not paths:
        raise ComposeContextError("Git did not report a primary worktree")
    return paths


def _project_name(worktree: Path) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", worktree.name.lower()).strip("-_")
    slug = slug[:40].rstrip("-_") or "worktree"
    digest = hashlib.sha256(os.fsencode(worktree)).hexdigest()[:8]
    return f"{PROJECT_PREFIX}-{slug}-{digest}"


def _slot_assignments(worktrees: Sequence[Path], primary: Path) -> dict[Path, int]:
    occupied: set[int] = set()
    assignments: dict[Path, int] = {}
    linked = sorted(
        {path.resolve() for path in worktrees if path.resolve() != primary},
        key=os.fspath,
    )
    for worktree in linked:
        candidate = (
            int.from_bytes(hashlib.sha256(os.fsencode(worktree)).digest()[:2], "big")
            % PORT_SLOT_COUNT
        )
        for _ in range(PORT_SLOT_COUNT):
            if candidate not in occupied:
                occupied.add(candidate)
                assignments[worktree] = candidate
                break
            candidate = (candidate + 1) % PORT_SLOT_COUNT
        else:
            raise ComposeContextError("No free local Compose port block remains")
    return assignments


def resolve_context(cwd: Path) -> ComposeContext:
    worktree = Path(_git("rev-parse", "--show-toplevel", cwd=cwd)).resolve()
    registered = _worktrees(worktree)
    primary = registered[0].resolve()
    if worktree == primary:
        return ComposeContext(
            is_primary=True,
            ports={},
            project=PROJECT_PREFIX,
            worktree=str(worktree),
        )

    active = [path for path in registered if path.exists()]
    if worktree not in active:
        active.append(worktree)
    slot = _slot_assignments(active, primary)[worktree]
    first_port = PORT_BLOCK_BASE + slot * PORT_BLOCK_SIZE
    ports = {name: first_port + index for index, name in enumerate(PORT_NAMES)}
    return ComposeContext(
        is_primary=False,
        ports=ports,
        project=_project_name(worktree),
        worktree=str(worktree),
    )


def _effective_context(
    context: ComposeContext,
    *,
    project: str,
    environment: Mapping[str, str],
    env_files: Sequence[str] = (),
) -> ComposeContext:
    ports = dict(context.ports)
    if context.is_primary:
        ports.update(_ports_from_env_files(env_files, Path(context.worktree)))
    for name in PORT_NAMES:
        if name not in environment:
            continue
        try:
            port = int(environment[name])
        except ValueError:
            raise ComposeContextError(f"{name} must be an integer") from None
        if not 1 <= port <= 65_535:
            raise ComposeContextError(f"{name} must be between 1 and 65535")
        ports[name] = port
    return ComposeContext(
        is_primary=context.is_primary,
        ports=ports,
        project=project or context.project,
        worktree=context.worktree,
    )


def _ports_from_env_files(env_files: Sequence[str], cwd: Path) -> dict[str, int]:
    ports: dict[str, int] = {}
    for value in env_files:
        path = Path(value)
        if not path.is_absolute():
            path = cwd / path
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, raw_value = line.split("=", 1)
            name = name.removeprefix("export ").strip()
            if name not in PORT_NAMES:
                continue
            normalized = raw_value.strip().strip("'\"")
            try:
                ports[name] = int(normalized)
            except ValueError:
                raise ComposeContextError(f"{name} must be an integer") from None
    return ports


def _print_context(context: ComposeContext, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(asdict(context), sort_keys=True))
        return

    print(f"COMPOSE_PROJECT={context.project}")
    print(f"SPINE_COMPOSE_WORKTREE={context.worktree}")
    for name, port in context.ports.items():
        print(f"{name}={port}")


def _run_compose(args: argparse.Namespace, context: ComposeContext) -> int:
    command = _compose_command_prefix(args, context)
    compose_args = args.compose_args
    if compose_args[:1] == ["--"]:
        compose_args = compose_args[1:]
    command.extend(compose_args)

    completed = subprocess.run(
        command,
        cwd=context.worktree,
        env=_compose_environment(context),
    )
    return completed.returncode


def _compose_command_prefix(
    args: argparse.Namespace, context: ComposeContext
) -> list[str]:
    compose = shlex.split(args.compose)
    if not compose:
        raise ComposeContextError("COMPOSE must name an executable")
    command = [*compose, "-p", context.project]
    for env_file in args.env_file:
        command.extend(("--env-file", env_file))
    return command


def _compose_environment(context: ComposeContext) -> dict[str, str]:
    environment = os.environ.copy()
    for name, port in context.ports.items():
        environment.setdefault(name, str(port))
    return environment


def _initialize_runtime(args: argparse.Namespace, context: ComposeContext) -> int:
    template = Path(args.template)
    if template.is_absolute() or ".." in template.parts:
        raise ComposeContextError("runtime template must be inside the worktree")
    runtime = Path(context.worktree, args.runtime)
    if runtime.is_symlink():
        raise ComposeContextError(
            f"runtime environment must not be a symlink: {args.runtime}"
        )

    created = False
    if runtime.exists():
        if not runtime.is_file():
            raise ComposeContextError(
                f"runtime environment is not a regular file: {args.runtime}"
            )
        content = runtime.read_text(encoding="utf-8")
    else:
        content = _committed_file(
            template.as_posix(), cwd=Path(context.worktree)
        ).replace(
            "<current-40-character-git-commit>",
            _git("rev-parse", "HEAD", cwd=Path(context.worktree)),
        )
        descriptor = os.open(runtime, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(content)
        created = True

    print(f"runtime environment {'created' if created else 'kept'}: {args.runtime}")
    required = sorted(
        {
            line.split("=", 1)[0].strip()
            for line in content.splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and "=" in line
            and re.search(r"<[^<>\n]+>", line.split("=", 1)[1])
        }
    )
    if required:
        print(
            "required runtime fields remain: " + ", ".join(required),
            file=sys.stderr,
        )
        return 2
    return 0


def _committed_file(path: str, *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ComposeContextError(f"committed runtime template unavailable: {path}")
    return completed.stdout


def _preflight(args: argparse.Namespace, context: ComposeContext) -> int:
    if not context.ports:
        raise ComposeContextError("no published ports were resolved")

    project_ports: set[int] = set()
    if not args.skip_project_check:
        command = _compose_command_prefix(args, context)
        command.extend(("ps", "--format", "json"))
        observed = subprocess.run(
            command,
            cwd=context.worktree,
            env=_compose_environment(context),
            check=False,
            capture_output=True,
            text=True,
        )
        if observed.returncode != 0:
            raise ComposeContextError("could not inspect the selected Compose project")
        project_ports = _published_ports(observed.stdout)

    unavailable: list[tuple[str, int]] = []
    for name, port in context.ports.items():
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            if port not in project_ports:
                unavailable.append((name, port))
        finally:
            probe.close()
    if unavailable:
        joined = ", ".join(f"{name}={port}" for name, port in unavailable)
        raise ComposeContextError(f"selected loopback ports are unavailable: {joined}")
    print(f"Compose ports are available: {context.project}")
    return 0


def _published_ports(output: str) -> set[int]:
    if not output.strip():
        return set()
    try:
        decoded = json.loads(output)
        records = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        try:
            records = [json.loads(line) for line in output.splitlines() if line.strip()]
        except json.JSONDecodeError:
            raise ComposeContextError(
                "Compose returned invalid project metadata"
            ) from None

    published: set[int] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        publishers = record.get("Publishers", [])
        if not isinstance(publishers, list):
            continue
        for publisher in publishers:
            if not isinstance(publisher, dict):
                continue
            value = publisher.get("PublishedPort")
            if isinstance(value, int):
                published.add(value)
    return published


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    context = subparsers.add_parser("context", help="print the resolved context")
    context.add_argument("--format", choices=("text", "json"), default="text")
    context.add_argument("--project", default="")
    context.add_argument("--env-file", action="append", default=[])

    run = subparsers.add_parser("run", help="run Docker Compose in the context")
    run.add_argument("--compose", default="docker compose")
    run.add_argument("--project", default="")
    run.add_argument("--env-file", action="append", default=[])
    run.add_argument("compose_args", nargs=argparse.REMAINDER)

    initialize = subparsers.add_parser(
        "init", help="create a private runtime environment from its template"
    )
    initialize.add_argument("--template", default=".env.example")
    initialize.add_argument("--runtime", default=".env")

    preflight = subparsers.add_parser(
        "preflight", help="verify selected loopback ports before Compose startup"
    )
    preflight.add_argument("--compose", default="docker compose")
    preflight.add_argument("--project", default="")
    preflight.add_argument("--env-file", action="append", default=[])
    preflight.add_argument("--skip-project-check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        context = _effective_context(
            resolve_context(Path.cwd()),
            project=getattr(args, "project", ""),
            environment=os.environ,
            env_files=getattr(args, "env_file", ()),
        )
        if args.command == "context":
            _print_context(context, args.format)
            return 0
        if args.command == "init":
            return _initialize_runtime(args, context)
        if args.command == "preflight":
            return _preflight(args, context)
        return _run_compose(args, context)
    except ComposeContextError as error:
        print(f"compose worktree context: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
