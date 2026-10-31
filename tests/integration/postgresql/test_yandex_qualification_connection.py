from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.application.connections.provider import (
    ProviderReadCommand,
    ProviderReadDeniedError,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.yandex_qualification_connection import (
    YANDEX_QUALIFICATION_APPROVAL_ID,
    YANDEX_QUALIFICATION_CONNECTION_ID,
    YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
    YANDEX_QUALIFICATION_GRANT_REFERENCE,
    YANDEX_QUALIFICATION_REFERENCE,
    YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
    YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
    YandexQualificationConnectionBootstrap,
    YandexQualificationConnectionConfig,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderConnectionHealth,
    ProviderConnectionStatus,
    ProviderDiagnosticCode,
    ProviderKind,
)
from spine.infrastructure.connections.postgresql_provider_management import (
    PostgreSQLProviderConnectionRepository,
    provider_connection_approvals,
    provider_connection_versions,
)
from spine.infrastructure.connections.postgresql_yandex_qualification import (
    PostgreSQLYandexQualificationConnectionRepository,
    YandexQualificationBootstrapConflictError,
)
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
DISPATCHER_PASSWORD = "dispatcher-test-secret"
WORKSPACE_ID = UUID("76200000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("76200000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("76200000-0000-4000-8000-000000000003")
OTHER_ENVIRONMENT_ID = UUID("76200000-0000-4000-8000-000000000004")
OCCURRED_AT = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class YandexDatabase:
    migration_engine: AsyncEngine
    runtime_engine: AsyncEngine


def _id(value: int) -> UUID:
    return UUID(f"76200000-0000-4000-8000-{value:012d}")


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role, password=password
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(loop_scope="session")
async def yandex_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[YandexDatabase]:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
        dispatcher_role=DISPATCHER_ROLE,
        dispatcher_password=DISPATCHER_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration_url = _role_url(postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD)
    await asyncio.to_thread(
        upgrade_database,
        MigrationDatabaseSettings(
            url=migration_url,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            dispatcher_role=DISPATCHER_ROLE,
        ),
    )
    migration_engine = create_async_engine(
        migration_url.get_secret_value(), hide_parameters=True
    )
    runtime_engine = create_async_engine(
        _role_url(
            postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD
        ).get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) "
                "VALUES "
                "(:workspace_id, 'yandex-qualification', 'Yandex Qualification'), "
                "(:other_workspace_id, 'other-yandex-qualification', "
                "'Other Yandex Qualification')"
            ),
            {
                "workspace_id": WORKSPACE_ID,
                "other_workspace_id": OTHER_WORKSPACE_ID,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments "
                "(id, workspace_id, kind, display_name) "
                "VALUES "
                "(:environment_id, :workspace_id, 'staging', 'Yandex'), "
                "(:other_environment_id, :other_workspace_id, 'staging', "
                "'Other Yandex')"
            ),
            {
                "workspace_id": WORKSPACE_ID,
                "environment_id": ENVIRONMENT_ID,
                "other_workspace_id": OTHER_WORKSPACE_ID,
                "other_environment_id": OTHER_ENVIRONMENT_ID,
            },
        )
    try:
        yield YandexDatabase(migration_engine, runtime_engine)
    finally:
        await runtime_engine.dispose()
        async with migration_engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS spine CASCADE"))
        await migration_engine.dispose()


def _config() -> YandexQualificationConnectionConfig:
    return YandexQualificationConnectionConfig(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        operator_identity_id=_id(10),
        approval_id=YANDEX_QUALIFICATION_APPROVAL_ID,
        connection_id=YANDEX_QUALIFICATION_CONNECTION_ID,
        service_principal_id=YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
        credential_reference=YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
        grant_reference=YANDEX_QUALIFICATION_GRANT_REFERENCE,
        resource_scope_reference=YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
        qualification_reference=YANDEX_QUALIFICATION_REFERENCE,
        authorization_generation=2,
        configuration_generation=3,
        approved_at=OCCURRED_AT,
        security_approval_reference="issue-701-comment-6539550358",
        data_approval_reference="issue-702-comment-6539550684",
        fixture_manifest_version="spine-yandex-qualification-fixtures-v1",
        scope_version="spine-yandex-qualification-scope:v1",
        approved_root="/Spine-Qualification-678-v1",
    )


def _service(
    database: YandexDatabase,
    *,
    config: YandexQualificationConnectionConfig,
    workspace_id: UUID | None = None,
    environment_id: UUID | None = None,
):
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(20), secret=b"issue-762-yandex-qualification-bootstrap"
    )
    context = boundary.interactive(
        scope=EnvironmentScope(
            workspace_id=workspace_id or config.workspace_id,
            environment_id=environment_id or config.environment_id,
        ),
        acting_subject_id=config.operator_identity_id,
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation("provider_connection_configure"),
        trace_id=_id(21),
    )

    class ExistingAuthority:
        async def verify(self, candidate):
            assert candidate == config

    service = YandexQualificationConnectionBootstrap(
        authority=ExistingAuthority(),
        repository=PostgreSQLYandexQualificationConnectionRepository(
            session_factory=async_sessionmaker(
                database.migration_engine, expire_on_commit=False
            ),
            context_verifier=boundary,
        ),
        artifact_writer=FilePrivateQualificationArtifactWriter(),
        context=context,
    )
    return service, boundary


async def test_bootstrap_records_audited_authority_and_replays_exact_connection(
    yandex_database: YandexDatabase,
    tmp_path: Path,
) -> None:
    config = _config()
    service, boundary = _service(yandex_database, config=config)

    created = await service.run(
        config, report_path=(tmp_path / "created.json").resolve()
    )
    replay = await service.run(config, report_path=(tmp_path / "replay.json").resolve())

    assert created.connection_replay is False
    assert replay.connection_replay is True
    assert replay.connection_version_id == created.connection_version_id
    async with yandex_database.migration_engine.connect() as connection:
        audit_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT event_type, outcome, payload FROM spine.audit_events "
                        "WHERE (event_type = 'command.outcome' "
                        "AND target_id = :connection_version_id) OR "
                        "(event_type = 'yandex.qualification_authority' "
                        "AND target_id = :approval_id) ORDER BY event_type"
                    ),
                    {
                        "approval_id": config.approval_id,
                        "connection_version_id": created.connection_version_id,
                    },
                )
            )
            .mappings()
            .all()
        )
        assert [row.event_type for row in audit_rows] == [
            "command.outcome",
            "yandex.qualification_authority",
        ]
    lineage = audit_rows[1].payload
    assert lineage["security_approval_reference"] == config.security_approval_reference
    assert lineage["data_approval_reference"] == config.data_approval_reference
    assert lineage["resource_scope_reference_id"] == str(
        config.resource_scope_reference
    )

    read_repository = PostgreSQLProviderConnectionRepository(
        session_factory=async_sessionmaker(
            yandex_database.runtime_engine, expire_on_commit=False
        ),
        context_verifier=boundary,
    )
    command = ProviderReadCommand(
        workspace_id=config.workspace_id,
        environment_id=config.environment_id,
        connection_id=created.connection_id,
        connection_version_id=created.connection_version_id,
        resource_scope_reference=OpaqueProviderReference(
            value=config.resource_scope_reference
        ),
        authorization_generation=config.authorization_generation,
        configuration_generation=config.configuration_generation,
        required_capabilities=(
            ProviderCapabilityKind.ENUMERATION,
            ProviderCapabilityKind.METADATA_READ,
            ProviderCapabilityKind.FULL_RECONCILIATION,
        ),
    )

    def read_context(
        service_principal_id=YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
        *,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    ):
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=service_principal_id,
            purpose=PersistencePurpose("yandex_qualification"),
            operation=PersistenceOperation("provider_connection_read"),
            trace_id=_id(22),
        )

    async with read_repository.admit_read(command, read_context()) as admitted:
        assert admitted.connection_version_id == created.connection_version_id
    for denied_command, denied_context in (
        (replace(command, authorization_generation=1), read_context()),
        (command, read_context(_id(23))),
        (
            command,
            read_context(
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
            ),
        ),
    ):
        with pytest.raises(ProviderReadDeniedError):
            async with read_repository.admit_read(denied_command, denied_context):
                pytest.fail("stale or mismatched Yandex authority was admitted")

    foreign_service, _ = _service(
        yandex_database,
        config=config,
        workspace_id=OTHER_WORKSPACE_ID,
        environment_id=OTHER_ENVIRONMENT_ID,
    )
    with pytest.raises(YandexQualificationBootstrapConflictError) as denied:
        await foreign_service.run(
            config, report_path=(tmp_path / "foreign.json").resolve()
        )
    assert str(denied.value) == (
        "Yandex qualification bootstrap conflicts with canonical state"
    )
    assert str(config.approval_id) not in str(denied.value)


async def test_conflicting_connection_fails_without_recording_approval_or_audit(
    yandex_database: YandexDatabase,
    tmp_path: Path,
) -> None:
    config = _config()
    conflicting_approval_id = _id(30)
    async with yandex_database.migration_engine.begin() as connection:
        await connection.execute(
            insert(provider_connection_approvals).values(
                approval_id=conflicting_approval_id,
                workspace_id=config.workspace_id,
                environment_id=config.environment_id,
                service_principal_id=_id(31),
                provider_kind=ProviderKind.YANDEX_DISK.value,
                credential_reference=_id(32),
                grant_reference=_id(33),
                approved_resource_scope_references=[_id(34)],
                authorization_generation=1,
                configuration_generation=1,
                status=ProviderConnectionStatus.ACTIVE.value,
                recorded_at=OCCURRED_AT,
            )
        )
        await connection.execute(
            insert(provider_connection_versions).values(
                connection_version_id=_id(35),
                connection_id=config.connection_id,
                approval_id=conflicting_approval_id,
                version=1,
                predecessor_version_id=None,
                workspace_id=config.workspace_id,
                environment_id=config.environment_id,
                service_principal_id=_id(31),
                provider_kind=ProviderKind.YANDEX_DISK.value,
                credential_reference=_id(32),
                grant_reference=_id(33),
                approved_resource_scope_references=[_id(34)],
                authorization_generation=1,
                configuration_generation=1,
                status=ProviderConnectionStatus.ACTIVE.value,
                capabilities=[],
                health=ProviderConnectionHealth.HEALTHY.value,
                diagnostic_codes=[ProviderDiagnosticCode.CONNECTION_READY.value],
                observed_at=OCCURRED_AT,
                cursor_lag_seconds=None,
                last_successful_reconciliation_at=OCCURRED_AT,
                receipt_id=_id(36),
                idempotency_key="preexisting-conflict",
                command_digest="a" * 64,
                recorded_at=OCCURRED_AT,
            )
        )
    service, _ = _service(yandex_database, config=config)

    with pytest.raises(YandexQualificationBootstrapConflictError):
        await service.run(config, report_path=(tmp_path / "report.json").resolve())

    async with yandex_database.migration_engine.connect() as connection:
        fixed_approval_count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.provider_connection_approvals "
                "WHERE approval_id = :approval_id"
            ),
            {"approval_id": config.approval_id},
        )
        audit_count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.audit_events "
                "WHERE target_id = :approval_id"
            ),
            {"approval_id": config.approval_id},
        )
    assert fixed_approval_count == 0
    assert audit_count == 0
