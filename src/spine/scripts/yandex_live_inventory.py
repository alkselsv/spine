"""Canonical live composition for admitted Yandex metadata inventory."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.yandex_disk_qualification import (
    YandexQualificationInventoryConfig,
    YandexQualificationInventoryRunner,
    write_yandex_inventory_report,
    yandex_inventory_not_verified_report,
)
from spine.application.yandex_qualification_execution import (
    YandexQualificationExecutionScope,
)
from spine.connectors.yandex_disk_api import (
    HttpxYandexDiskApiTransport,
    YandexQualificationRequestGovernor,
    YandexQualificationRequestPolicy,
)
from spine.connectors.yandex_disk_qualification import (
    YandexDiskQualificationInventoryProbe,
)
from spine.infrastructure.connections.postgresql_provider_management import (
    PostgreSQLProviderConnectionRepository,
)
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)
from spine.infrastructure.yandex_qualification_credentials import (
    LocalYandexQualificationCredentialResolver,
    load_approved_yandex_user_id,
)
from spine.infrastructure.yandex_qualification_execution import (
    FileYandexQualificationExecutionLease,
)


async def _run(
    *,
    config: YandexQualificationInventoryConfig,
    env_file: Path,
    identity_file: Path,
    candidate_path: Path,
    report_path: Path,
    lease_file: Path,
    environment_opt_in: bool,
) -> int:
    writer = FilePrivateQualificationArtifactWriter()
    if not config.opt_in or environment_opt_in is not True:
        write_yandex_inventory_report(
            report_path,
            yandex_inventory_not_verified_report(config, "prerequisite_denied"),
            artifact_writer=writer,
        )
        return 1
    settings = RuntimeDatabaseSettings()
    engine = create_async_engine(
        settings.url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        pool_timeout=settings.pool_timeout_seconds,
        pool_pre_ping=settings.pool_pre_ping,
        pool_reset_on_return="rollback",
        isolation_level="READ COMMITTED",
        connect_args={
            "connect_timeout": settings.connect_timeout_seconds,
            "options": POSTGRESQL_SEARCH_PATH_OPTIONS,
        },
        hide_parameters=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    boundary = TrustedContextBoundary.create()
    scope = YandexQualificationExecutionScope(
        workspace_id=config.credential_binding.workspace_id,
        environment_id=config.credential_binding.environment_id,
        connection_id=config.connection_id,
        connection_version_id=config.connection_version_id,
        resource_scope_reference=config.resource_scope_reference,
        authorization_generation=config.authorization_generation,
        configuration_generation=config.configuration_generation,
    )
    try:
        resolver = LocalYandexQualificationCredentialResolver(
            env_file=env_file,
            binding=config.credential_binding,
        )
        expected_user_id = load_approved_yandex_user_id(identity_file)
        policy = YandexQualificationRequestPolicy()
        governor = YandexQualificationRequestGovernor(
            policy=policy,
            monotonic=asyncio.get_running_loop().time,
            sleep=asyncio.sleep,
        )
        context = boundary.worker(
            scope=EnvironmentScope(
                workspace_id=config.credential_binding.workspace_id,
                environment_id=config.credential_binding.environment_id,
            ),
            service_principal_id=config.service_principal_id,
            purpose=PersistencePurpose("yandex_qualification"),
            operation=PersistenceOperation("provider_connection_read"),
            trace_id=uuid4(),
        )
        async with httpx.AsyncClient() as client:
            report = await YandexQualificationInventoryRunner(
                config=config,
                expected_user_id=expected_user_id,
                inventory_probe=YandexDiskQualificationInventoryProbe(
                    credential_resolver=resolver,
                    transport=HttpxYandexDiskApiTransport(client=client),
                    request_governor=governor,
                ),
                connection_repository=PostgreSQLProviderConnectionRepository(
                    session_factory=sessions,
                    context_verifier=boundary,
                ),
                execution_lease=FileYandexQualificationExecutionLease(
                    lock_file=lease_file,
                    approved_scope=scope,
                ),
                artifact_writer=writer,
                context=context,
            ).run(
                environment_opt_in=environment_opt_in,
                candidate_path=candidate_path,
            )
        try:
            write_yandex_inventory_report(
                report_path,
                report,
                artifact_writer=writer,
            )
        except OSError:
            if report.manifest_candidate_written:
                try:
                    candidate_path.unlink()
                except OSError:
                    pass
            raise
        return 0 if report.status == "ready_for_manifest_approval" else 1
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the admitted Yandex metadata-only qualification inventory."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--manifest-candidate", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--lease-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = YandexQualificationInventoryConfig.model_validate_json(
            args.config.read_text(encoding="utf-8")
        )
        return asyncio.run(
            _run(
                config=config,
                env_file=args.env_file,
                identity_file=args.identity_file,
                candidate_path=args.manifest_candidate,
                report_path=args.report,
                lease_file=args.lease_file,
                environment_opt_in=(
                    os.environ.get("SPINE_YANDEX_LIVE_INVENTORY_OPT_IN") == "1"
                ),
            )
        )
    except Exception:
        print(
            "Yandex live inventory refused: approved opt-in or prerequisites "
            "are missing.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - entry point wrapper
    raise SystemExit(main())
