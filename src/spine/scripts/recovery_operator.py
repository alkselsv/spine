"""Explicit recovery-operator profile entrypoint.

The CLI validates a versioned profile and never discovers infrastructure from
command-line paths, DSNs or import strings.  A deployment-owned composition
must call ``run_operator_command`` with already-bound recovery ports.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from spine.application.storage.recovery_operator import RecoveryOperatorService
from spine.domain.storage.recovery_operator import RecoveryOperatorProfile, RecoveryOperatorResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an approved Spine recovery-operator profile."
    )
    parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="JSON file containing spine-recovery-operator:v1 references.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the profile without invoking recovery ports.",
    )
    return parser


def load_profile(path: Path) -> RecoveryOperatorProfile:
    """Load only the strict profile schema; errors are redacted by the caller."""

    return RecoveryOperatorProfile.model_validate_json(path.read_bytes())


async def run_operator_command(
    profile: RecoveryOperatorProfile,
    operator: RecoveryOperatorService,
)-> RecoveryOperatorResult:
    """Invoke the application service through the explicit command boundary."""

    return await operator.execute(profile)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = build_parser().parse_args(argv)
    try:
        profile = load_profile(arguments.profile)
    except Exception:
        print(
            json.dumps(
                {
                    "schema_version": "spine-recovery-operator-result:v1",
                    "status": "blocked",
                    "safe_failure_code": "config.invalid",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    if arguments.validate_only:
        print(
            json.dumps(
                {
                    "schema_version": profile.schema_version,
                    "status": "validated",
                }
            )
        )
        raise SystemExit(0)

    print(
        json.dumps(
            {
                "schema_version": "spine-recovery-operator-result:v1",
                "status": "blocked",
                "safe_failure_code": "operator.composition_not_bound",
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(3)


if __name__ == "__main__":
    main()


__all__ = ["build_parser", "load_profile", "main", "run_operator_command"]
