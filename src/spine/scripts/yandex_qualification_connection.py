"""Operator composition for canonical Yandex qualification authority."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.yandex_qualification_connection import (
    YandexQualificationConnectionBootstrap,
    YandexQualificationConnectionConfig,
)
from spine.infrastructure.connections.postgresql_yandex_qualification import (
    PostgreSQLYandexQualificationConnectionRepository,
)
from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    POSTGRESQL_SEARCH_PATH_OPTIONS,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)
from spine.scripts.qualification import QualificationVerificationSettings, verify


class _ExistingQualificationAuthority:
    def __init__(self, settings: QualificationVerificationSettings) -> None:
        self._settings = settings

    async def verify(self, config: YandexQualificationConnectionConfig) -> None:
        if (
            config.workspace_id != self._settings.workspace_a_id
            or config.environment_id != self._settings.environment_a_id
            or config.operator_identity_id != self._settings.identity_id
        ):
            raise RuntimeError("Yandex qualification authority mismatch")
        await verify(self._settings)


def _same_database_target(migration_url: str, runtime_url: str) -> bool:
    """Compare PostgreSQL targets without comparing or disclosing credentials."""

    migration = urlsplit(migration_url)
    runtime = urlsplit(runtime_url)

    def target(parts):
        return (
            parts.hostname,
            parts.port or 5432,
            unquote(parts.path.removeprefix("/")),
        )

    return target(migration) == target(runtime)


async def _run(
    *, config: YandexQualificationConnectionConfig, report_path: Path
) -> int:
    migration_settings = MigrationDatabaseSettings()
    authority_settings = QualificationVerificationSettings()
    if not _same_database_target(
        migration_settings.url.get_secret_value(),
        authority_settings.runtime_database_url,
    ):
        raise RuntimeError("Yandex qualification database target mismatch")
    engine = create_async_engine(
        migration_settings.url.get_secret_value(),
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    boundary = TrustedContextBoundary.create()
    context = boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=config.workspace_id, environment_id=config.environment_id
        ),
        acting_subject_id=config.operator_identity_id,
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation("provider_connection_configure"),
        trace_id=uuid4(),
    )
    try:
        await YandexQualificationConnectionBootstrap(
            authority=_ExistingQualificationAuthority(authority_settings),
            repository=PostgreSQLYandexQualificationConnectionRepository(
                session_factory=async_sessionmaker(
                    engine, expire_on_commit=False, autoflush=False
                ),
                context_verifier=boundary,
            ),
            artifact_writer=FilePrivateQualificationArtifactWriter(),
            context=context,
        ).run(config, report_path=report_path)
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision canonical authority for Yandex qualification #678."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        config = YandexQualificationConnectionConfig.model_validate_json(
            arguments.config.read_text(encoding="utf-8")
        )
        return asyncio.run(_run(config=config, report_path=arguments.report))
    except Exception:
        print(
            "Yandex qualification connection bootstrap refused: prerequisites or canonical authority are invalid.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
