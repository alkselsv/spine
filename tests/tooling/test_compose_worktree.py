from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[2]
COMPOSE_WORKTREE = ROOT / "scripts" / "compose_worktree.py"
MAKEFILE = ROOT / "Makefile"
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


@pytest.fixture(autouse=True)
def _isolate_compose_port_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PORT_NAMES:
        monkeypatch.delenv(name, raising=False)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _create_repository_with_worktrees(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "spine"
    repository.mkdir()
    _git("init", "-b", "main", cwd=repository)
    _git("config", "user.email", "tests@example.invalid", cwd=repository)
    _git("config", "user.name", "Spine tests", cwd=repository)
    (repository / "README.md").write_text("test repository\n", encoding="utf-8")
    _git("add", "README.md", cwd=repository)
    _git("commit", "-m", "initial", cwd=repository)

    first = tmp_path / "issue-101"
    second = tmp_path / "issue-102"
    _git("worktree", "add", "--detach", str(first), cwd=repository)
    _git("worktree", "add", "--detach", str(second), cwd=repository)
    return repository, first, second


def _context(checkout: Path) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(COMPOSE_WORKTREE), "context", "--format", "json"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _install_tooling(repository: Path, template: str) -> None:
    scripts = repository / "scripts"
    scripts.mkdir(exist_ok=True)
    shutil.copy2(COMPOSE_WORKTREE, scripts / "compose_worktree.py")
    shutil.copy2(MAKEFILE, repository / "Makefile")
    (repository / ".env.example").write_text(template, encoding="utf-8")
    _git(
        "add", "Makefile", "scripts/compose_worktree.py", ".env.example", cwd=repository
    )
    _git("commit", "-m", "install worktree tooling", cwd=repository)


def test_linked_worktrees_receive_stable_disjoint_compose_contexts(
    tmp_path: Path,
) -> None:
    repository, first, second = _create_repository_with_worktrees(tmp_path)

    primary_context = _context(repository)
    first_context = _context(first)
    second_context = _context(second)

    assert primary_context == {
        "is_primary": True,
        "ports": {},
        "project": "spine",
        "worktree": str(repository.resolve()),
    }
    assert first_context == _context(first)
    assert first_context["is_primary"] is False
    assert second_context["is_primary"] is False
    assert first_context["project"] != second_context["project"]
    assert str(first_context["project"]).startswith("spine-issue-101-")
    assert str(second_context["project"]).startswith("spine-issue-102-")

    first_ports = set(dict(first_context["ports"]).values())
    second_ports = set(dict(second_context["ports"]).values())
    assert len(first_ports) == 8
    assert len(second_ports) == 8
    assert first_ports.isdisjoint(second_ports)


def test_hash_collision_still_receives_disjoint_port_blocks(tmp_path: Path) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    by_slot: dict[int, Path] = {}
    collision: tuple[Path, Path] | None = None
    for ordinal in range(10_000):
        candidate = (tmp_path / f"collision-{ordinal}").resolve()
        slot = (
            int.from_bytes(hashlib.sha256(os.fsencode(candidate)).digest()[:2], "big")
            % 4_096
        )
        previous = by_slot.get(slot)
        if previous is not None:
            collision = previous, candidate
            break
        by_slot[slot] = candidate
    assert collision is not None
    first, second = collision
    _git("worktree", "add", "--detach", str(first), cwd=repository)
    _git("worktree", "add", "--detach", str(second), cwd=repository)

    first_ports = set(dict(_context(first)["ports"]).values())
    second_ports = set(dict(_context(second)["ports"]).values())

    assert first_ports.isdisjoint(second_ports)


def test_primary_context_reports_env_file_ports_and_process_override(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    (repository / ".env").write_text(
        "SPINE_WEB_PORT=4175\nSPINE_API_PORT=18080\n", encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["SPINE_API_PORT"] = "19080"

    completed = subprocess.run(
        [
            sys.executable,
            str(COMPOSE_WORKTREE),
            "context",
            "--format",
            "json",
            "--env-file",
            ".env",
        ],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    context = json.loads(completed.stdout)

    assert context["ports"] == {
        "SPINE_API_PORT": 19080,
        "SPINE_WEB_PORT": 4175,
    }


def test_run_preserves_explicit_project_and_port_overrides(tmp_path: Path) -> None:
    _, worktree, _ = _create_repository_with_worktrees(tmp_path)
    fake_compose = tmp_path / "fake_compose.py"
    fake_compose.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

print(json.dumps({
    "args": sys.argv[1:],
    "api_port": os.environ["SPINE_API_PORT"],
    "web_port": os.environ["SPINE_WEB_PORT"],
}))
""",
        encoding="utf-8",
    )
    fake_compose.chmod(0o755)
    environment = os.environ.copy()
    environment["SPINE_API_PORT"] = "65000"

    completed = subprocess.run(
        [
            sys.executable,
            str(COMPOSE_WORKTREE),
            "run",
            "--compose",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_compose))}",
            "--project",
            "manual-project",
            "--env-file",
            ".env",
            "--",
            "ps",
            "--all",
        ],
        cwd=worktree,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    observed = json.loads(completed.stdout)

    assert observed["args"] == [
        "-p",
        "manual-project",
        "--env-file",
        ".env",
        "ps",
        "--all",
    ]
    assert observed["api_port"] == "65000"
    assert observed["web_port"].isdigit()
    assert observed["web_port"] != "65000"

    context = subprocess.run(
        [
            sys.executable,
            str(COMPOSE_WORKTREE),
            "context",
            "--format",
            "json",
            "--project",
            "manual-project",
        ],
        cwd=worktree,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    displayed = json.loads(context.stdout)
    assert displayed["project"] == "manual-project"
    assert displayed["ports"]["SPINE_API_PORT"] == 65000


def test_init_creates_private_runtime_env_and_reports_required_fields(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    template = repository / ".env.example"
    template.write_text(
        "SPINE_COMMIT=<current-40-character-git-commit>\n"
        "SPINE_REQUIRED=<operator-secret>\n",
        encoding="utf-8",
    )
    _git("add", ".env.example", cwd=repository)
    _git("commit", "-m", "add runtime template", cwd=repository)

    completed = subprocess.run(
        [
            sys.executable,
            str(COMPOSE_WORKTREE),
            "init",
            "--template",
            ".env.example",
            "--runtime",
            ".env",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    runtime = repository / ".env"
    assert completed.returncode == 2
    assert runtime.stat().st_mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR
    assert f"SPINE_COMMIT={_git_output('rev-parse', 'HEAD', cwd=repository)}" in (
        runtime.read_text(encoding="utf-8")
    )
    assert "SPINE_REQUIRED" in completed.stderr
    assert "operator-secret" not in completed.stderr


def test_init_never_replaces_existing_runtime_env(tmp_path: Path) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    (repository / ".env.example").write_text(
        "SPINE_REQUIRED=<operator-secret>\n", encoding="utf-8"
    )
    runtime = repository / ".env"
    runtime.write_text("SPINE_EXISTING=keep\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(COMPOSE_WORKTREE),
            "init",
            "--template",
            ".env.example",
            "--runtime",
            ".env",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert runtime.read_text(encoding="utf-8") == "SPINE_EXISTING=keep\n"


def test_make_worktree_init_retries_without_copying_or_replacing_secrets(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    _install_tooling(
        repository,
        "SPINE_COMMIT=<current-40-character-git-commit>\n"
        "SPINE_REQUIRED=<operator-secret>\n",
    )
    (repository / ".env.example").write_text(
        "SPINE_REQUIRED=uncommitted-secret\n", encoding="utf-8"
    )
    fake_uv = tmp_path / "fake_uv.py"
    fake_uv.write_text(
        """#!/usr/bin/env python3
from pathlib import Path

Path("qualification.calls").open("a", encoding="utf-8").write("called\\n")
Path(".env.qualification").write_text("BOUNDED=true\\n", encoding="utf-8")
Path(".env.qualification").chmod(0o600)
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    first = subprocess.run(
        ["make", "worktree-init", f"UV={fake_uv}"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    runtime = repository / ".env"
    assert first.returncode != 0
    assert "uncommitted-secret" not in runtime.read_text(encoding="utf-8")
    assert not (repository / ".env.qualification").exists()

    runtime.write_text("SPINE_REQUIRED=synthetic-secret\n", encoding="utf-8")
    second = subprocess.run(
        ["make", "worktree-init", f"UV={fake_uv}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_snapshot = runtime.read_bytes()
    qualification = repository / ".env.qualification"
    qualification_snapshot = qualification.read_bytes()

    subprocess.run(
        ["make", "worktree-init", f"UV={fake_uv}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert runtime.read_bytes() == runtime_snapshot
    assert qualification.read_bytes() == qualification_snapshot
    assert (repository / "qualification.calls").read_text(encoding="utf-8") == (
        "called\n"
    )


def test_make_preflight_rejects_unrelated_listener_beside_project_listener(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    _install_tooling(repository, "SPINE_REQUIRED=<operator-secret>\n")
    first_port = _free_port_block()
    (repository / ".env").write_text(
        "".join(
            f"{name}={first_port + offset}\n" for offset, name in enumerate(PORT_NAMES)
        ),
        encoding="utf-8",
    )
    (repository / ".env.qualification").write_text("BOUNDED=true\n", encoding="utf-8")
    owned_port = first_port
    unrelated_port = first_port + 2
    fake_compose = tmp_path / "fake_compose.py"
    fake_compose.write_text(
        """#!/usr/bin/env python3
import json

print(json.dumps([{"Publishers": [{"PublishedPort": %d}]}]))
""" % owned_port,
        encoding="utf-8",
    )
    fake_compose.chmod(0o755)
    owned_listener = _listener(owned_port)
    unrelated_listener = _listener(unrelated_port)
    try:
        completed = subprocess.run(
            [
                "make",
                "compose-preflight",
                f"COMPOSE={sys.executable} {fake_compose}",
            ],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
        unrelated_listener.close()
        accepted = subprocess.run(
            [
                "make",
                "compose-preflight",
                f"COMPOSE={sys.executable} {fake_compose}",
            ],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        owned_listener.close()
        unrelated_listener.close()

    assert completed.returncode == 2
    assert f"SPINE_API_PORT={unrelated_port}" in completed.stderr
    assert f"SPINE_WEB_PORT={owned_port}" not in completed.stderr
    assert accepted.returncode == 0


def test_make_compose_context_uses_runtime_env_and_project_resolver(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    _install_tooling(repository, "SPINE_REQUIRED=<operator-secret>\n")
    (repository / ".env").write_text("SPINE_API_PORT=39080\n", encoding="utf-8")
    (repository / ".env.qualification").write_text("BOUNDED=true\n", encoding="utf-8")

    completed = subprocess.run(
        ["make", "compose-context"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "COMPOSE_PROJECT=spine" in completed.stdout
    assert "SPINE_API_PORT=39080" in completed.stdout


@pytest.mark.parametrize(
    ("target", "expected_suffixes"),
    (
        (
            "start",
            (
                "--profile qualification-provisioning run --rm "
                "qualification-authority-provisioner",
                "up --build --detach --wait",
            ),
        ),
        (
            "start-fixtures",
            (
                "--profile qualification-provisioning run --rm "
                "qualification-authority-provisioner",
                "--profile fixtures up --build --detach --wait web-fixtures",
            ),
        ),
        ("status", ("--profile fixtures ps --all",)),
        ("logs", ("logs --tail=200 api qa-worker outbox-dispatcher web",)),
        ("stop", ("--profile fixtures down",)),
        ("destroy", ("--profile fixtures down --volumes --remove-orphans",)),
    ),
)
def test_make_compose_targets_preserve_resolved_project_ports_and_arguments(
    tmp_path: Path,
    target: str,
    expected_suffixes: tuple[str, ...],
) -> None:
    repository, _, _ = _create_repository_with_worktrees(tmp_path)
    _install_tooling(repository, "SPINE_REQUIRED=<operator-secret>\n")
    (repository / ".env").write_text("SPINE_API_PORT=39080\n", encoding="utf-8")
    (repository / ".env.qualification").write_text("BOUNDED=true\n", encoding="utf-8")
    compose_log = tmp_path / "compose.jsonl"
    fake_compose = tmp_path / "fake_compose.py"
    fake_compose.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

record = {"args": sys.argv[1:], "api_port": os.environ.get("SPINE_API_PORT")}
with Path(os.environ["FAKE_COMPOSE_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
if sys.argv[-3:] == ["ps", "--format", "json"]:
    print("[]")
""",
        encoding="utf-8",
    )
    fake_compose.chmod(0o755)
    environment = os.environ.copy()
    environment["FAKE_COMPOSE_LOG"] = str(compose_log)

    subprocess.run(
        ["make", target, f"COMPOSE={sys.executable} {fake_compose}"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    records = [
        json.loads(line)
        for line in compose_log.read_text(encoding="utf-8").splitlines()
    ]
    assert all(
        record["args"][:5]
        == [
            "-p",
            "spine",
            "--env-file",
            ".env",
            "--env-file",
        ]
        for record in records
    )
    assert all(record["args"][5] == ".env.qualification" for record in records)
    assert all(record["api_port"] == "39080" for record in records)
    observed_commands = [" ".join(record["args"][6:]) for record in records]
    for expected_suffix in expected_suffixes:
        assert expected_suffix in observed_commands


def _free_port_block() -> int:
    for first_port in range(30_000, 60_000, 8):
        probes: list[socket.socket] = []
        try:
            for port in range(first_port, first_port + 8):
                probe = socket.socket()
                probe.bind(("127.0.0.1", port))
                probes.append(probe)
            return first_port
        except OSError:
            continue
        finally:
            for probe in probes:
                probe.close()
    raise AssertionError("no free loopback port block")


def _listener(port: int) -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen()
    return listener


def _git_output(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
