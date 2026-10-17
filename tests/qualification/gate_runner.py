"""Executable repository-gate runner for Issue #68 qualification."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Callable, Sequence


GATE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uv", "sync", "--extra", "dev"),
    ("uv", "lock", "--check"),
    ("uv", "run", "pytest", "-q"),
    ("uv", "run", "python", "-m", "compileall", "-q", "src", "tests"),
    ("git", "diff", "--cached", "--check"),
)
POSTGRES_BLOCK_MARKERS = (
    "mandatory PostgreSQL gate could not start",
    "mandatory PostgreSQL gate requires SPINE_TEST_DATABASE_URL",
    "mandatory PostgreSQL gate cannot import Testcontainers",
)
GATE_ARTIFACT = Path("docs/qualification/issue-68-gate-results.json")
_ARTIFACT_STATUSES = {"passed", "failed", "blocked-infrastructure"}


@dataclass(frozen=True, slots=True)
class GateResult:
    command: tuple[str, ...]
    returncode: int
    status: str
    safe_summary: str

    def as_json(self) -> dict[str, object]:
        return {
            "command": " ".join(self.command),
            "returncode": self.returncode,
            "status": self.status,
            "summary": self.safe_summary,
        }


def validate_gate_artifact(artifact: object) -> None:
    if not isinstance(artifact, dict) or set(artifact) != {
        "staged_diff_sha256",
        "qualified_tree_sha256",
        "results",
    }:
        raise ValueError("gate artifact schema is invalid")
    staged_hash = artifact["staged_diff_sha256"]
    tree_hash = artifact["qualified_tree_sha256"]
    results = artifact["results"]
    if (
        not isinstance(staged_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", staged_hash) is None
        or not isinstance(tree_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", tree_hash) is None
        or not isinstance(results, list)
        or len(results) != len(GATE_COMMANDS)
    ):
        raise ValueError("gate artifact schema is invalid")
    for expected_command, result in zip(GATE_COMMANDS, results, strict=True):
        if not isinstance(result, dict) or set(result) != {
            "command",
            "returncode",
            "status",
            "summary",
        }:
            raise ValueError("gate artifact schema is invalid")
        if (
            result["command"] != " ".join(expected_command)
            or isinstance(result["returncode"], bool)
            or not isinstance(result["returncode"], int)
            or not isinstance(result["status"], str)
            or result["status"] not in _ARTIFACT_STATUSES
            or not isinstance(result["summary"], str)
            or not result["summary"]
            or (result["status"] == "passed") != (result["returncode"] == 0)
        ):
            raise ValueError("gate artifact schema is invalid")


def _pytest_summary(output: str) -> str:
    match = re.search(r"(\d+ passed(?:, \d+ errors?)?)", output)
    if match:
        return match.group(1)
    return "pytest completed without a parseable summary"


def _is_pure_postgresql_block(output: str) -> bool:
    if "FAILURES" in output or re.search(r"\b\d+\s+failed\b", output):
        return False
    error_counts = re.findall(r"(\d+)\s+passed,\s*(\d+)\s+errors?\b", output)
    if not error_counts:
        return False
    error_count = int(error_counts[-1][1])
    lines = output.splitlines()
    setup_indexes = [
        index for index, line in enumerate(lines) if "ERROR at setup of" in line
    ]
    if len(setup_indexes) != error_count:
        return False
    for index in setup_indexes:
        next_line = index + 1
        while next_line < len(lines) and not lines[next_line].strip():
            next_line += 1
        if (
            next_line == len(lines)
            or not lines[next_line].strip().startswith(POSTGRES_BLOCK_MARKERS)
        ):
            return False
    return True


def classify_result(
    command: Sequence[str],
    *,
    returncode: int,
    output: str,
) -> GateResult:
    command_tuple = tuple(command)
    if returncode == 0:
        summary = _pytest_summary(output) if command_tuple[2:4] == ("pytest", "-q") else "completed successfully"
        return GateResult(command_tuple, returncode, "passed", summary)
    if (
        command_tuple[2:4] == ("pytest", "-q")
        and any(marker in output for marker in POSTGRES_BLOCK_MARKERS)
        and _is_pure_postgresql_block(output)
    ):
        return GateResult(
            command_tuple,
            returncode,
            "blocked-infrastructure",
            "mandatory PostgreSQL setup was unavailable; pytest execution reached the real gate",
        )
    return GateResult(
        command_tuple,
        returncode,
        "failed",
        "command failed; raw command output is intentionally not stored",
    )


def run_gate_commands(
    repository_root: Path,
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[GateResult, ...]:
    results: list[GateResult] = []
    for command in GATE_COMMANDS:
        completed = executor(
            command,
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
        results.append(
            classify_result(
                command,
                returncode=completed.returncode,
                output=completed.stdout + completed.stderr,
            )
        )
    return tuple(results)


def staged_diff_sha256(repository_root: Path) -> str:
    completed = subprocess.run(
        (
            "git",
            "diff",
            "--cached",
            "--binary",
            "--",
            ".",
            ":(exclude)docs/qualification/issue-68-gate-results.json",
        ),
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(completed.stdout).hexdigest()


def qualified_tree_sha256(repository_root: Path) -> str:
    """Hash the staged source tree, excluding the self-referential gate artifact."""

    completed = subprocess.run(
        ("git", "ls-files", "--stage", "-z"),
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    digest = hashlib.sha256()
    excluded_path = GATE_ARTIFACT.as_posix().encode("utf-8")
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", maxsplit=1)
        mode, object_id, stage = metadata.split()
        if path == excluded_path:
            continue
        if stage != b"0":
            raise ValueError("qualified source tree contains an unresolved index entry")
        digest.update(mode + b" " + object_id + b" " + path + b"\0")
    return digest.hexdigest()


def _worktree_has_unstaged_files(repository_root: Path) -> bool:
    unstaged = subprocess.run(
        ("git", "diff", "--quiet"),
        cwd=repository_root,
        check=False,
    )
    untracked = subprocess.run(
        ("git", "ls-files", "--others", "--exclude-standard"),
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return unstaged.returncode != 0 or bool(untracked.stdout.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="emit the staged-diff identity and command manifest without executing gates",
    )
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).parents[2]
    if _worktree_has_unstaged_files(repository_root):
        print(json.dumps({"status": "failed", "summary": "unstaged or untracked files present"}))
        return 1
    if arguments.dry_run:
        print(
            json.dumps(
                {
                    "staged_diff_sha256": staged_diff_sha256(repository_root),
                    "qualified_tree_sha256": qualified_tree_sha256(repository_root),
                    "commands": [" ".join(command) for command in GATE_COMMANDS],
                    "results": [],
                },
                indent=2,
            )
        )
        return 0
    artifact_path = repository_root / GATE_ARTIFACT
    if not artifact_path.is_file():
        print(json.dumps({"status": "failed", "summary": "gate artifact is missing"}))
        return 1
    results = run_gate_commands(repository_root)
    report_results = [result.as_json() for result in results]
    report = {
        "staged_diff_sha256": staged_diff_sha256(repository_root),
        "qualified_tree_sha256": qualified_tree_sha256(repository_root),
        "results": report_results,
    }
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        validate_gate_artifact(artifact)
    except (OSError, json.JSONDecodeError, ValueError):
        print(json.dumps({"status": "failed", "summary": "gate artifact schema is invalid"}))
        return 1
    report["artifact_match"] = (
        artifact.get("staged_diff_sha256") == report["staged_diff_sha256"]
        and artifact.get("qualified_tree_sha256") == report["qualified_tree_sha256"]
        and artifact.get("results") == report_results
    )
    if report["artifact_match"] is not True:
        print(json.dumps(report, indent=2))
        return 1
    print(
        json.dumps(
            report,
            indent=2,
        )
    )
    if any(result.status == "failed" for result in results):
        return 1
    if any(result.status == "blocked-infrastructure" for result in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
