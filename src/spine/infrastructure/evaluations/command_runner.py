"""Non-shell command execution boundary for qualification adapters."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandExecutionResult:
    """Content captured from one non-shell command execution."""

    return_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Narrow execution port for qualification infrastructure."""

    def run(self, arguments: tuple[str, ...]) -> CommandExecutionResult: ...


class SubprocessCommandRunner:
    """Execute an argument vector without invoking a shell."""

    def run(self, arguments: tuple[str, ...]) -> CommandExecutionResult:
        completed = subprocess.run(  # noqa: S603 - caller supplies fixed argv shapes
            arguments,
            check=False,
            capture_output=True,
            text=True,
        )
        return CommandExecutionResult(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
