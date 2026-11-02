"""Collect canonical R2 evidence from one isolated Docker Compose target."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

from spine.domain.common import EnvironmentKind
from spine.domain.evaluations.r2_deployment_qualification import (
    R2CollectionFailureEvidence,
    R2LiveExecution,
    R2PrivacyDeclaration,
)
from spine.infrastructure.evaluations.command_runner import SubprocessCommandRunner
from spine.infrastructure.evaluations.r2_deployment_collector import (
    DockerComposeR2EvidenceCollector,
    R2CollectionError,
    R2CollectorConfiguration,
    UrllibHttpProbe,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision and probe one exact live R2 Compose target."
    )
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--runtime-env", type=Path, required=True)
    parser.add_argument("--qualification-env", type=Path, required=True)
    parser.add_argument("--api-base-url", required=True)
    parser.add_argument("--web-base-url", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--deployment-fingerprint", required=True)
    parser.add_argument(
        "--environment",
        choices=tuple(item.value for item in EnvironmentKind),
        required=True,
    )
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    environment = EnvironmentKind(arguments.environment)
    started_at = datetime.now(timezone.utc)
    execution_id = f"r2-collector:{uuid4()}"

    try:
        collector = DockerComposeR2EvidenceCollector(
            command_runner=SubprocessCommandRunner(),
            http_probe=UrllibHttpProbe(),
            execution_id_factory=lambda: execution_id,
        )
        evidence = collector.collect(
            R2CollectorConfiguration(
                compose_project=arguments.compose_project,
                runtime_env_path=arguments.runtime_env,
                qualification_env_path=arguments.qualification_env,
                api_base_url=arguments.api_base_url,
                web_base_url=arguments.web_base_url,
                head_sha=arguments.head_sha,
                deployment_fingerprint=arguments.deployment_fingerprint,
                environment=environment,
                principal_id=arguments.principal_id,
            )
        )
    except (OSError, R2CollectionError) as error:
        captured_at = datetime.now(timezone.utc)
        evidence = R2CollectionFailureEvidence(
            target_commit=arguments.head_sha,
            deployment_fingerprint=arguments.deployment_fingerprint,
            environment=environment,
            captured_at=captured_at,
            live_execution=R2LiveExecution(
                execution_id=execution_id,
                principal_id=arguments.principal_id,
                environment=environment,
                started_at=started_at,
                completed_at=captured_at,
            ),
            reason_code=(
                str(error)
                if isinstance(error, R2CollectionError)
                else "collector_io_unavailable"
            ),
            privacy=_privacy_declaration(),
        )
    except ValueError:
        print("R2 evidence collection refused the live target.", file=sys.stderr)
        return 2
    try:
        arguments.output.write_text(
            evidence.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        print("R2 evidence collection could not write its artifact.", file=sys.stderr)
        return 2
    return 0


def _privacy_declaration() -> R2PrivacyDeclaration:
    return R2PrivacyDeclaration(
        metadata_only=True,
        credentials_recorded=False,
        network_locations_recorded=False,
        protected_content_recorded=False,
        provider_payloads_recorded=False,
        raw_errors_recorded=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
