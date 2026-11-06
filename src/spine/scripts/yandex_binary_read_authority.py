"""Runtime-only operator composition for the approved Yandex binary-read successor."""

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
    YandexQualificationBinaryReadConfig,
    YandexQualificationBinaryReadProvisioner,
)
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.connections.postgresql_yandex_qualification import (
    PostgreSQLYandexQualificationBinaryReadRepository,
)
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)
from spine.scripts.qualification import QualificationVerificationSettings, verify


class _ExistingQualificationAuthority:
    def __init__(self, settings: QualificationVerificationSettings) -> None:
        self._settings = settings

    async def verify(self, config: YandexQualificationBinaryReadConfig) -> None:
        if (
            config.workspace_id != self._settings.workspace_a_id
            or config.environment_id != self._settings.environment_a_id
            or config.operator_identity_id != self._settings.identity_id
        ):
            raise RuntimeError("Yandex qualification authority mismatch")
        await verify(self._settings)


def _same_database_target(left_url: str, right_url: str) -> bool:
    def target(url: str) -> tuple[str | None, int, str]:
        parts = urlsplit(url)
        return (
            parts.hostname,
            parts.port or 5432,
            unquote(parts.path.removeprefix("/")),
        )

    return target(left_url) == target(right_url)


async def _run(
    *, config: YandexQualificationBinaryReadConfig, report_path: Path
) -> int:
    database_settings = RuntimeDatabaseSettings()
    authority_settings = QualificationVerificationSettings()
    database_url = database_settings.url.get_secret_value()
    if not _same_database_target(database_url, authority_settings.runtime_database_url):
        raise RuntimeError("Yandex qualification database target mismatch")
    engine = create_async_engine(
        database_url,
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
        await YandexQualificationBinaryReadProvisioner(
            authority=_ExistingQualificationAuthority(authority_settings),
            repository=PostgreSQLYandexQualificationBinaryReadRepository(
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
        description="Provision the approved Yandex qualification binary-read authority."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        config = YandexQualificationBinaryReadConfig.model_validate_json(
            arguments.config.read_text(encoding="utf-8")
        )
        return asyncio.run(_run(config=config, report_path=arguments.report))
    except Exception:
        print(
            "Yandex binary-read authority provisioning refused: prerequisites or canonical authority are invalid.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
