from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from spine.application.canonical_tombstone_selectors import (
    CanonicalTombstoneSelectorError,
)
from spine.application.tombstone_authorization import (
    IssueTombstoneAuthorizationCommand,
    TombstoneAuthorizationOutcome,
    TombstoneAuthorizationService,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.errors import CanonicalEligibilityError
from spine.domain.audit import AuditOutcome, CommandAuditPayload
from spine.domain.sources.canonical_tombstone_selector import (
    CanonicalTombstoneWriterMode,
    TrustedCanonicalTombstoneVersionSelector,
)
from spine.infrastructure.persistence.postgresql_canonical_tombstone_selectors import (
    PostgreSQLCanonicalTombstoneSelectorRepository,
)
from spine.infrastructure.db.migrations import locate_migration_assets, upgrade_database
from spine.infrastructure.db.settings import MigrationDatabaseSettings
from spine.infrastructure.persistence.postgresql_canonical_tombstone import (
    PostgreSQLCurrentTombstoneAuthorizationAuthority,
)
from tests.application.test_tombstone_authorization import NOW
from tests.integration.postgresql.test_postgresql_tombstone_authorization import (
    _admitted_case,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    DISPATCHER_ROLE,
    MIGRATION_ROLE,
    PersistenceAdapter,
    PostgreSQLContractDatabase,
    RUNTIME_ROLE,
    postgresql_adapter,
    postgresql_contract_database,
    synthetic_uuid,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


async def test_v3_reader_first_migration_rollback_retains_read_schema_and_disables_writer(
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    migration_settings = MigrationDatabaseSettings(
        url=postgresql_contract_database.migration_url,
        migration_role=MIGRATION_ROLE,
        runtime_role=RUNTIME_ROLE,
        dispatcher_role=DISPATCHER_ROLE,
    )
    config_path, script_location = locate_migration_assets()
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_location))
    config.attributes["migration_settings"] = migration_settings
    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    migration_failure: BaseException | None = None
    try:
        await asyncio.to_thread(command.downgrade, config, "20270111_110")
        async with engine.connect() as connection:
            columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'spine' "
                            "AND table_name = 'canonical_acceptances'"
                        )
                    )
                ).scalars()
            )
            selector_table = await connection.scalar(
                text(
                    "SELECT to_regclass("
                    "'spine.trusted_canonical_tombstone_selector_versions')"
                )
            )
            disabled_trigger = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger AS trigger "
                    "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' "
                    "AND relation.relname = 'canonical_acceptances' "
                    "AND trigger.tgname = 'trg_canonical_acceptance_v3_disabled' "
                    "AND NOT trigger.tgisinternal"
                )
            )
        assert {
            "revision_kind",
            "tombstone_authorization_id",
            "tombstone_authorization_digest",
            "transition_id",
        } <= columns
        assert selector_table == "spine.trusted_canonical_tombstone_selector_versions"
        assert disabled_trigger == 1

        with pytest.raises(
            DBAPIError,
            match="Canonical acceptance v3 writes are disabled by rollback",
        ):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO spine.canonical_acceptances "
                        "(canonical_acceptance_id, canonical_acceptance_schema_version, "
                        "source_object_id, source_revision_id, revision_kind, workspace_id, "
                        "environment_id, accepted_generation, ordering, provenance_id, "
                        "idempotency_receipt_id, audit_event_id, outbox_event_id, accepted_at) "
                        "VALUES (:id, 3, :id, :id, 'tombstone', :id, :id, 1, "
                        "'expected_current', :id, :id, :id, :id, :accepted_at)"
                    ),
                    {"id": synthetic_uuid(738001), "accepted_at": NOW},
                )
    except BaseException as error:
        migration_failure = error
    finally:
        await engine.dispose()

    try:
        await asyncio.to_thread(upgrade_database, migration_settings)
    except BaseException as cleanup_error:
        if migration_failure is not None:
            raise cleanup_error from migration_failure
        raise
    if migration_failure is not None:
        raise migration_failure

    engine = create_async_engine(
        postgresql_contract_database.migration_url.get_secret_value(),
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            restored_fk = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'fk_canonical_acceptances_tombstone_authorization'"
                )
            )
            disabled_trigger = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'trg_canonical_acceptance_v3_disabled'"
                )
            )
        assert restored_fk == 1
        assert disabled_trigger == 0
    finally:
        await engine.dispose()


async def test_tombstone_selector_publication_is_immutable_and_cas_serialized(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    (
        case,
        ingestion_context,
        _,
        authorization_repository,
        _,
        approval,
        _,
        authorization,
        _,
        _,
    ) = await _admitted_case(postgresql_adapter, postgresql_contract_database, 736200)
    boundary = postgresql_adapter.uow_factory._context_verifier  # type: ignore[attr-defined]

    def context_factory(_operation: str, workspace_id: UUID, environment_id: UUID):
        return boundary.interactive(
            scope=EnvironmentScope(
                workspace_id=workspace_id, environment_id=environment_id
            ),
            acting_subject_id=approval.approver_human_id,
            purpose=PersistencePurpose("canonical_tombstone_selector"),
            operation=PersistenceOperation("canonical_tombstone_selector.publish"),
            trace_id=synthetic_uuid(736201),
        )

    context = context_factory(
        "canonical_tombstone_selector.publish",
        case.source.workspace_id,
        case.source.environment_id,
    )
    repository = PostgreSQLCanonicalTombstoneSelectorRepository(
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=context_factory,
    )
    registry = AuditEventRegistry.with_default_families()
    assert case.source.connection_id is not None

    def selector(
        selector_id: UUID,
        version: int,
        predecessor_id: UUID | None,
        audit_id: UUID,
        key: str,
        mode: CanonicalTombstoneWriterMode = CanonicalTombstoneWriterMode.V3_REQUIRED,
    ) -> TrustedCanonicalTombstoneVersionSelector:
        return TrustedCanonicalTombstoneVersionSelector(
            selector_id=selector_id,
            selector_series_id=synthetic_uuid(736202),
            selector_version=version,
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            source_object_id=case.source.source_object_id,
            source_contract_id=approval.source_contract_id,
            source_contract_version=approval.source_contract_version,
            provider_kind=authorization.provider_kind,
            connection_id=case.source.connection_id,
            resource_scope_reference=approval.resource_scope_reference,
            mode=mode,
            approval_reference=approval.approval_id,
            predecessor_selector_id=predecessor_id,
            audit_reference=audit_id,
            idempotency_identity=key,
            valid_from=NOW,
            valid_until=NOW + timedelta(days=1),
            created_at=NOW,
        )

    def audit(audit_id: UUID):
        return registry.build_event(
            audit_event_id=audit_id,
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(
                command_type="canonical_tombstone_selector.publish"
            ),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=NOW,
            outcome=AuditOutcome.ACCEPTED,
            reason="tombstone_selector_published",
        )

    first = selector(
        synthetic_uuid(736203),
        1,
        None,
        synthetic_uuid(736204),
        "tombstone:first",
        CanonicalTombstoneWriterMode.LEGACY_V1_ALLOWED,
    )
    stored = await repository.publish(
        first,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=audit(first.audit_reference),
    )
    replay = await repository.publish(
        first,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=audit(first.audit_reference),
    )
    assert stored.replayed is False
    assert replay.replayed is True
    legacy_command = SimpleNamespace(
        source=case.source,
        tombstone_authorization=None,
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        await PostgreSQLCurrentTombstoneAuthorizationAuthority(
            clock=lambda: NOW
        ).revalidate(
            legacy_command, uow
        )  # type: ignore[arg-type]
        await uow.rollback()
    unrelated_command = SimpleNamespace(
        source=case.source.model_copy(
            update={"source_object_id": synthetic_uuid(736299)}
        ),
        tombstone_authorization=None,
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await PostgreSQLCurrentTombstoneAuthorizationAuthority(
                clock=lambda: NOW
            ).revalidate(
                unrelated_command, uow
            )  # type: ignore[arg-type]
        await uow.rollback()
    issuance = await TombstoneAuthorizationService(
        authorization_repository,
        authorization_repository,
        clock=lambda: NOW,
    ).issue(IssueTombstoneAuthorizationCommand(authorization), ingestion_context)
    assert issuance.outcome is TombstoneAuthorizationOutcome.ISSUED
    second = selector(
        synthetic_uuid(736205),
        2,
        first.selector_id,
        synthetic_uuid(736206),
        "tombstone:second",
    )
    await repository.publish(
        second,
        expected_current_selector_id=first.selector_id,
        expected_cas_version=1,
        recorded_at=NOW,
        audit_event=audit(second.audit_reference),
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await PostgreSQLCurrentTombstoneAuthorizationAuthority(
                clock=lambda: NOW
            ).revalidate(
                legacy_command, uow
            )  # type: ignore[arg-type]
        await uow.rollback()
    left = selector(
        synthetic_uuid(736207),
        3,
        second.selector_id,
        synthetic_uuid(736208),
        "tombstone:left",
    )
    right = selector(
        synthetic_uuid(736209),
        3,
        second.selector_id,
        synthetic_uuid(736210),
        "tombstone:right",
    )
    outcomes = await asyncio.gather(
        repository.publish(
            left,
            expected_current_selector_id=second.selector_id,
            expected_cas_version=2,
            recorded_at=NOW,
            audit_event=audit(left.audit_reference),
        ),
        repository.publish(
            right,
            expected_current_selector_id=second.selector_id,
            expected_cas_version=2,
            recorded_at=NOW,
            audit_event=audit(right.audit_reference),
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    assert (
        sum(isinstance(value, CanonicalTombstoneSelectorError) for value in outcomes)
        == 1
    )
