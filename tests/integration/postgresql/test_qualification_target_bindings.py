from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from spine.auth.qualification_targets import (
    ExternalSecretReference,
    InMemoryQualificationSecretResolver,
    QualificationBindingDeniedError,
    QualificationDatabaseAccess,
    QualificationSecretDeniedError,
    QualificationTarget,
    QualificationTargetBinding,
    QualificationTargetBindingRequest,
    QualificationTargetRole,
    qualification_target_binding_digest,
)
from spine.auth.records import (
    AuthorizationRecordStatus,
    QualificationPrincipalKind,
)
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.postgresql_authorization_mappings import (
    qualification_authority_bindings,
    qualification_target_bindings,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    access_policy_versions,
    source_objects,
)
from spine.infrastructure.persistence.postgresql_qualification_targets import (
    PostgreSQLQualificationTargetBindingResolver,
)
from tests.contracts.auth.qualification_target_binding_contract import (
    QualificationTargetContractCase,
    QualificationTargetResolverScenario,
    assert_qualification_target_binding_resolver_contract,
)


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.asyncio(loop_scope="session"),
]
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
DISPATCHER_PASSWORD = "dispatcher-test-secret"
WORKSPACE_ID = UUID("72000000-0000-0000-0000-000000000001")
SOURCE_ENVIRONMENT_ID = UUID("73000000-0000-0000-0000-000000000001")
RESTORE_ENVIRONMENT_ID = UUID("73000000-0000-0000-0000-000000000002")
SOURCE_POLICY_ID = UUID("77000000-0000-0000-0000-000000000001")
SOURCE_OBJECT_ID = UUID("77000000-0000-0000-0000-000000000002")


@dataclass(frozen=True, slots=True)
class QualificationDatabase:
    migration_engine: AsyncEngine
    runtime_engine: AsyncEngine


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def qualification_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[QualificationDatabase]:
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
        _role_url(postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD).get_secret_value(),
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) VALUES "
                "(:workspace_id, 'qualification-targets', 'Qualification Targets')"
            ),
            {"workspace_id": WORKSPACE_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments (id, workspace_id, kind, display_name) VALUES "
                "(:source_id, :workspace_id, 'staging', 'Source'), "
                "(:restore_id, :workspace_id, 'staging', 'Restore')"
            ),
            {
                "workspace_id": WORKSPACE_ID,
                "source_id": SOURCE_ENVIRONMENT_ID,
                "restore_id": RESTORE_ENVIRONMENT_ID,
            },
        )
        await connection.execute(
            insert(source_objects).values(
                source_object_id=SOURCE_OBJECT_ID,
                workspace_id=WORKSPACE_ID,
                environment_id=SOURCE_ENVIRONMENT_ID,
                source_kind="qualification",
                identity_mode="upload",
                upload_identity="qualification-source-policy",
                created_at=text("transaction_timestamp()"),
            )
        )
        await connection.execute(
            insert(access_policy_versions).values(
                access_policy_version_id=SOURCE_POLICY_ID,
                workspace_id=WORKSPACE_ID,
                environment_id=SOURCE_ENVIRONMENT_ID,
                source_object_id=SOURCE_OBJECT_ID,
                version=1,
                predecessor_policy_version_id=None,
                document_schema_version="access-policy.v1",
                document_hash="1" * 64,
                origin_kind="approved_source_configuration",
                origin_reference=UUID("77000000-0000-0000-0000-000000000003"),
                origin_version=1,
                origin_hash="2" * 64,
                created_by_canonical_human_id=UUID(
                    "77000000-0000-0000-0000-000000000004"
                ),
                created_at=text("transaction_timestamp()"),
            )
        )
    try:
        yield QualificationDatabase(migration_engine, runtime_engine)
    finally:
        await runtime_engine.dispose()
        await migration_engine.dispose()


def source_binding(case: int) -> QualificationTargetBinding:
    suffix = f"{case:012d}"
    return QualificationTargetBinding(
        binding_id=UUID(f"76000000-0000-0000-0000-{suffix}"),
        binding_series_id=UUID(f"76000000-0000-0000-0000-{suffix}"),
        binding_version=1,
        predecessor_binding_id=None,
        authority_binding_id=UUID(f"74000000-0000-0000-0000-{suffix}"),
        authority_binding_version=1,
        qualification_id=UUID(f"78000000-0000-0000-0000-{suffix}"),
        workspace_id=WORKSPACE_ID,
        environment_id=SOURCE_ENVIRONMENT_ID,
        target=QualificationTarget.SOURCE,
        target_reference="target-inventory:source:v1",
        target_scope_reference=f"target-scope:source:case-{case}:v1",
        principal_id=UUID(f"75000000-0000-0000-0000-{suffix}"),
        principal_kind=QualificationPrincipalKind.SERVICE,
        purpose="qualification_recovery",
        operation="source_operator",
        alias_reference=f"principal-alias:source:case-{case}:v1",
        secret=ExternalSecretReference(
            provider_reference="approved-provider-config:v1",
            secret_reference=f"qualification/source/postgres-case-{case}:v1",
        ),
        database_role_reference=f"postgres-role:source-operator-case-{case}:v1",
        target_role=QualificationTargetRole.SOURCE_OPERATOR,
        database_access=QualificationDatabaseAccess.WRITE,
        access_policy_references=(SOURCE_POLICY_ID,),
    )


def binding_request(binding: QualificationTargetBinding) -> QualificationTargetBindingRequest:
    return QualificationTargetBindingRequest(
        binding_id=binding.binding_id,
        binding_series_id=binding.binding_series_id,
        binding_version=binding.binding_version,
        predecessor_binding_id=binding.predecessor_binding_id,
        authority_binding_id=binding.authority_binding_id,
        authority_binding_version=binding.authority_binding_version,
        qualification_id=binding.qualification_id,
        workspace_id=binding.workspace_id,
        environment_id=binding.environment_id,
        target=binding.target,
        target_reference=binding.target_reference,
        target_scope_reference=binding.target_scope_reference,
        principal_id=binding.principal_id,
        principal_kind=binding.principal_kind,
        purpose=binding.purpose,
        operation=binding.operation,
        alias_reference=binding.alias_reference,
        secret=binding.secret,
        database_role_reference=binding.database_role_reference,
        target_role=binding.target_role,
        required_database_access=binding.database_access,
        access_policy_references=binding.access_policy_references,
    )


def target_binding_values(binding: QualificationTargetBinding) -> dict[str, object]:
    return {
        "binding_id": binding.binding_id,
        "binding_series_id": binding.binding_series_id,
        "binding_version": binding.binding_version,
        "predecessor_binding_id": binding.predecessor_binding_id,
        "authority_binding_id": binding.authority_binding_id,
        "authority_binding_version": binding.authority_binding_version,
        "qualification_id": binding.qualification_id,
        "workspace_id": binding.workspace_id,
        "environment_id": binding.environment_id,
        "target": binding.target.value,
        "target_reference": binding.target_reference,
        "target_scope_reference": binding.target_scope_reference,
        "principal_kind": binding.principal_kind.value,
        "principal_id": binding.principal_id,
        "purpose": binding.purpose,
        "operation": binding.operation,
        "alias_reference": binding.alias_reference,
        "secret_provider_reference": binding.secret.provider_reference,
        "secret_reference": binding.secret.secret_reference,
        "database_role_reference": binding.database_role_reference,
        "target_role": binding.target_role.value,
        "database_access": binding.database_access.value,
        "access_policy_references": list(binding.access_policy_references),
        "binding_digest": qualification_target_binding_digest(binding),
    }


async def seed_source_binding(
    database: QualificationDatabase, case: int
) -> QualificationTargetBinding:
    binding = source_binding(case)
    async with database.migration_engine.begin() as connection:
        await connection.execute(
            text(
                "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                "set_config('spine.environment_id', :environment_id, true)"
            ),
            {
                "workspace_id": str(WORKSPACE_ID),
                "environment_id": str(SOURCE_ENVIRONMENT_ID),
            },
        )
        await connection.execute(
            insert(qualification_authority_bindings).values(
                binding_id=binding.authority_binding_id,
                version=1,
                qualification_id=binding.qualification_id,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
                principal_kind=binding.principal_kind.value,
                principal_id=binding.principal_id,
                purpose=binding.purpose,
                operation=binding.operation,
                expires_at=text("transaction_timestamp() + interval '1 day'"),
                status=AuthorizationRecordStatus.ACTIVE.value,
                change_id=UUID(f"79000000-0000-0000-0000-{case:012d}"),
                recorded_by="synthetic-postgresql-test",
            )
        )
        await connection.execute(
            insert(qualification_target_bindings).values(**target_binding_values(binding))
        )
    return binding


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_qualification_target_binding_resolver_contract(
    qualification_database: QualificationDatabase,
) -> None:
    case_numbers = {"exact": 10, "target_mismatch": 11, "revoked": 12}

    async def scenario(
        case: QualificationTargetContractCase,
    ) -> QualificationTargetResolverScenario:
        binding = await seed_source_binding(
            qualification_database, case_numbers[case]
        )
        candidate = binding_request(binding)
        expected: QualificationTargetBinding | None = binding
        if case == "target_mismatch":
            candidate = candidate.model_copy(
                update={
                    "target": QualificationTarget.RESTORE,
                    "target_reference": "target-inventory:restore:v1",
                    "target_scope_reference": "target-scope:restore:v1",
                    "environment_id": RESTORE_ENVIRONMENT_ID,
                }
            )
            expected = None
        elif case == "revoked":
            async with qualification_database.migration_engine.begin() as connection:
                expires_at = await connection.scalar(
                    text(
                        "SELECT expires_at FROM spine.qualification_authority_bindings "
                        "WHERE binding_id = :binding_id AND version = 1"
                    ),
                    {"binding_id": binding.authority_binding_id},
                )
                await connection.execute(
                    insert(qualification_authority_bindings).values(
                        binding_id=binding.authority_binding_id,
                        version=2,
                        qualification_id=binding.qualification_id,
                        workspace_id=binding.workspace_id,
                        environment_id=binding.environment_id,
                        principal_kind=binding.principal_kind.value,
                        principal_id=binding.principal_id,
                        purpose=binding.purpose,
                        operation=binding.operation,
                        expires_at=expires_at,
                        status=AuthorizationRecordStatus.REVOKED.value,
                        change_id=UUID(
                            "79100000-0000-0000-0000-000000000012"
                        ),
                        recorded_by="synthetic-postgresql-contract",
                    )
                )
            expected = None
        return QualificationTargetResolverScenario(
            resolver=PostgreSQLQualificationTargetBindingResolver(
                async_sessionmaker(
                    qualification_database.runtime_engine,
                    expire_on_commit=False,
                )
            ),
            request=candidate,
            expected=expected,
        )

    await assert_qualification_target_binding_resolver_contract(scenario)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_target_resolver_allows_exact_current_binding(
    qualification_database: QualificationDatabase,
) -> None:
    binding = await seed_source_binding(qualification_database, 1)
    resolver = PostgreSQLQualificationTargetBindingResolver(
        async_sessionmaker(qualification_database.runtime_engine, expire_on_commit=False)
    )

    assert await resolver.resolve(binding_request(binding)) == binding


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_target_resolver_denies_target_mismatch(
    qualification_database: QualificationDatabase,
) -> None:
    binding = await seed_source_binding(qualification_database, 2)
    resolver = PostgreSQLQualificationTargetBindingResolver(
        async_sessionmaker(qualification_database.runtime_engine, expire_on_commit=False)
    )

    wrong_target = binding_request(binding).model_copy(
        update={
            "target": QualificationTarget.RESTORE,
            "target_reference": "target-inventory:restore:v1",
            "target_scope_reference": "target-scope:restore:v1",
            "environment_id": RESTORE_ENVIRONMENT_ID,
        }
    )
    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(wrong_target)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_target_resolver_redacts_secret_reference_mismatch(
    qualification_database: QualificationDatabase,
) -> None:
    binding = await seed_source_binding(qualification_database, 3)
    resolver = PostgreSQLQualificationTargetBindingResolver(
        async_sessionmaker(qualification_database.runtime_engine, expire_on_commit=False)
    )

    leaked_reference = "qualification/source/secret-leak-sentinel:v2"
    wrong_secret = binding_request(binding).model_copy(
        update={
            "secret": ExternalSecretReference(
                provider_reference=binding.secret.provider_reference,
                secret_reference=leaked_reference,
            )
        }
    )
    with pytest.raises(QualificationBindingDeniedError) as failure:
        await resolver.resolve(wrong_secret)
    assert leaked_reference not in str(failure.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_target_resolver_denies_after_canonical_revocation(
    qualification_database: QualificationDatabase,
) -> None:
    binding = await seed_source_binding(qualification_database, 4)
    async with qualification_database.migration_engine.begin() as connection:
        expires_at = await connection.scalar(
            text(
                "SELECT expires_at FROM spine.qualification_authority_bindings "
                "WHERE binding_id = :binding_id AND version = 1"
            ),
            {"binding_id": binding.authority_binding_id},
        )
        await connection.execute(
            insert(qualification_authority_bindings).values(
                binding_id=binding.authority_binding_id,
                version=2,
                qualification_id=binding.qualification_id,
                workspace_id=binding.workspace_id,
                environment_id=binding.environment_id,
                principal_kind=binding.principal_kind.value,
                principal_id=binding.principal_id,
                purpose=binding.purpose,
                operation=binding.operation,
                expires_at=expires_at,
                status=AuthorizationRecordStatus.REVOKED.value,
                change_id=UUID("79100000-0000-0000-0000-000000000004"),
                recorded_by="synthetic-postgresql-test",
            )
        )
    resolver = PostgreSQLQualificationTargetBindingResolver(
        async_sessionmaker(qualification_database.runtime_engine, expire_on_commit=False)
    )

    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(binding_request(binding))


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_has_execute_only_resolver_access_and_forced_rls(
    qualification_database: QualificationDatabase,
) -> None:
    async with qualification_database.runtime_engine.connect() as connection:
        privileges = (
            await connection.execute(
                text(
                    "SELECT "
                    "has_table_privilege(current_user, 'spine.qualification_target_bindings', 'SELECT') AS table_select, "
                    "has_function_privilege(current_user, "
                    "'spine.resolve_current_qualification_target_binding(uuid,text,uuid,uuid)', "
                    "'EXECUTE') AS resolver_execute"
                )
            )
        ).mappings().one()
        catalog = (
            await connection.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                    "r.rolsuper, r.rolbypassrls "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_roles r ON r.rolname = current_user "
                    "WHERE n.nspname = 'spine' "
                    "AND c.relname = 'qualification_target_bindings'"
                )
            )
        ).mappings().one()

    assert privileges == {"table_select": False, "resolver_execute": True}
    assert catalog == {
        "relrowsecurity": True,
        "relforcerowsecurity": True,
        "rolsuper": False,
        "rolbypassrls": False,
    }
    async with qualification_database.runtime_engine.connect() as connection:
        with pytest.raises(ProgrammingError):
            await connection.execute(text("SELECT * FROM spine.qualification_target_bindings"))


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_rejects_cross_target_secret_reuse(
    qualification_database: QualificationDatabase,
) -> None:
    source = await seed_source_binding(qualification_database, 5)
    binding_id = UUID("76000000-0000-0000-0000-000000000502")
    async with qualification_database.migration_engine.begin() as connection:
        await connection.execute(
            text(
                "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                "set_config('spine.environment_id', :environment_id, true)"
            ),
            {
                "workspace_id": str(WORKSPACE_ID),
                "environment_id": str(SOURCE_ENVIRONMENT_ID),
            },
        )
        with pytest.raises(IntegrityError) as failure:
            await connection.execute(
                insert(qualification_target_bindings).values(
                    binding_id=binding_id,
                    binding_series_id=binding_id,
                    binding_version=1,
                    predecessor_binding_id=None,
                    authority_binding_id=source.authority_binding_id,
                    authority_binding_version=source.authority_binding_version,
                    qualification_id=source.qualification_id,
                    workspace_id=source.workspace_id,
                    environment_id=source.environment_id,
                    target="restore",
                    target_reference="target-inventory:restore:v1",
                    target_scope_reference="target-scope:restore:v1",
                    principal_kind=source.principal_kind.value,
                    principal_id=UUID("75000000-0000-0000-0000-000000000002"),
                    purpose=source.purpose,
                    operation="restore_operator",
                    alias_reference="principal-alias:restore:v1",
                    secret_provider_reference=source.secret.provider_reference,
                    secret_reference=source.secret.secret_reference,
                    database_role_reference="postgres-role:restore_operator:v1",
                    target_role="restore_operator",
                    database_access="write",
                    access_policy_references=[SOURCE_POLICY_ID],
                    binding_digest="0" * 64,
                )
            )
    assert failure.value.orig.diag.constraint_name == "uq_qualification_target_separation"


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_rejects_verifier_binding_to_source(
    qualification_database: QualificationDatabase,
) -> None:
    source = await seed_source_binding(qualification_database, 6)
    binding_id = UUID("76000000-0000-0000-0000-000000000603")
    values = target_binding_values(source)
    values.update(
        binding_id=binding_id,
        binding_series_id=binding_id,
        principal_id=UUID("75000000-0000-0000-0000-000000000603"),
        alias_reference="principal-alias:source:verifier-case-6:v1",
        secret_reference="qualification/source/verifier-case-6:v1",
        database_role_reference="postgres-role:source-verifier-case-6:v1",
        target_scope_reference="target-scope:source:verifier-case-6:v1",
        target_role="verifier",
        database_access="read_only",
        binding_digest="3" * 64,
    )

    async with qualification_database.migration_engine.begin() as connection:
        with pytest.raises(IntegrityError) as failure:
            await connection.execute(insert(qualification_target_bindings).values(**values))
    assert failure.value.orig.diag.constraint_name == "ck_qualification_target_role_scope"


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_rejects_missing_access_policy_reference(
    qualification_database: QualificationDatabase,
) -> None:
    binding = await seed_source_binding(qualification_database, 7)
    missing_policy = UUID("77000000-0000-0000-0000-000000000099")
    binding_id = UUID("76000000-0000-0000-0000-000000000099")
    values = target_binding_values(binding)
    values.update(
        binding_id=binding_id,
        binding_series_id=binding_id,
        access_policy_references=[missing_policy],
        binding_digest="9" * 64,
    )

    async with qualification_database.migration_engine.begin() as connection:
        with pytest.raises(IntegrityError) as failure:
            await connection.execute(insert(qualification_target_bindings).values(**values))
    assert (
        failure.value.orig.diag.constraint_name
        == "fk_qualification_target_policy_reference"
    )
    assert str(missing_policy) not in str(failure.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_binding_with_missing_secret_denies_without_disclosure(
    qualification_database: QualificationDatabase,
) -> None:
    binding = await seed_source_binding(qualification_database, 8)
    resolver = PostgreSQLQualificationTargetBindingResolver(
        async_sessionmaker(qualification_database.runtime_engine, expire_on_commit=False)
    )
    resolved = await resolver.resolve(binding_request(binding))
    secrets = InMemoryQualificationSecretResolver(())
    with pytest.raises(QualificationSecretDeniedError) as secret_failure:
        await secrets.resolve(
            resolved.secret,
            target=resolved.target,
            target_reference=resolved.target_reference,
            database_role_reference=resolved.database_role_reference,
        )
    assert resolved.secret.secret_reference not in str(secret_failure.value)


@pytest.mark.asyncio(loop_scope="session")
async def test_postgresql_resolver_denies_corrupted_binding_digest(
    qualification_database: QualificationDatabase,
) -> None:
    predecessor = await seed_source_binding(qualification_database, 9)
    successor = QualificationTargetBinding.model_validate(
        {
            **predecessor.model_dump(),
            "binding_id": UUID("76000000-0000-0000-0000-000000000904"),
            "binding_version": 2,
            "predecessor_binding_id": predecessor.binding_id,
            "authority_binding_version": 2,
            "secret": {
                "provider_reference": "approved-provider-config:v1",
                "secret_reference": "qualification/source/postgres:v2",
            },
        }
    )
    async with qualification_database.migration_engine.begin() as connection:
        expires_at = await connection.scalar(
            text(
                "SELECT expires_at FROM spine.qualification_authority_bindings "
                "WHERE binding_id = :binding_id AND version = 1"
            ),
            {"binding_id": predecessor.authority_binding_id},
        )
        await connection.execute(
            insert(qualification_authority_bindings).values(
                binding_id=successor.authority_binding_id,
                version=2,
                qualification_id=successor.qualification_id,
                workspace_id=successor.workspace_id,
                environment_id=successor.environment_id,
                principal_kind=successor.principal_kind.value,
                principal_id=successor.principal_id,
                purpose=successor.purpose,
                operation=successor.operation,
                expires_at=expires_at,
                status=AuthorizationRecordStatus.ACTIVE.value,
                change_id=UUID("79100000-0000-0000-0000-000000000009"),
                recorded_by="synthetic-postgresql-test",
            )
        )
        corrupted = target_binding_values(successor)
        corrupted["alias_reference"] = "principal-alias:corrupted:v1"
        await connection.execute(insert(qualification_target_bindings).values(**corrupted))

    resolver = PostgreSQLQualificationTargetBindingResolver(
        async_sessionmaker(qualification_database.runtime_engine, expire_on_commit=False)
    )
    with pytest.raises(QualificationBindingDeniedError):
        await resolver.resolve(binding_request(successor))
