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

from spine.auth import AuthorizationDeniedError
from spine.auth.records import (
    AuthenticationAliasBinding,
    AuthorizationDirectoryRecords,
    AuthorizationRecordStatus,
    CanonicalHumanIdentity,
)
from spine.infrastructure.db.authorization_seed import (
    PostgreSQLAuthorizationSeedHelper,
)
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.persistence.postgresql_authorization import (
    PostgreSQLAuthorizationDirectory,
)
from spine.infrastructure.persistence.postgresql_authorization_mappings import (
    workspace_memberships,
)
from tests.contracts.auth.adapter import AuthorizationDirectoryFactory
from tests.contracts.auth.test_authorization_directory_contract import (
    DENIED_RECORD_OVERRIDES,
    ENVIRONMENT_ID,
    IDENTITY_ID,
    OTHER_IDENTITY_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    authenticated_alias,
    provenance,
    records,
    route_policy,
    scope,
    test_current_records_resolve_exact_authorization_contract as contract_current,
    test_inactive_or_incomplete_records_deny_without_disclosure as contract_denied,
    test_latest_history_version_is_the_only_authority as contract_history,
    test_same_identity_has_strictly_workspace_local_authority as contract_isolation,
)


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.asyncio(loop_scope="session"),
]
MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
MIGRATION_PASSWORD = "migration-test-secret"
RUNTIME_PASSWORD = "runtime-test-secret"
AUTH_TABLES = (
    "authentication_alias_bindings",
    "canonical_human_identity_states",
    "workspace_memberships",
    "environment_memberships",
    "environment_role_bindings",
    "canonical_human_identities",
)


@dataclass(frozen=True, slots=True)
class AuthorizationDatabase:
    migration_engine: AsyncEngine
    runtime_engine: AsyncEngine


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def authorization_database(
    postgresql_provision: TestDatabaseProvision,
) -> AsyncIterator[AuthorizationDatabase]:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration_url = _role_url(
        postgresql_provision, MIGRATION_ROLE, MIGRATION_PASSWORD
    )
    await asyncio.to_thread(
        upgrade_database,
        MigrationDatabaseSettings(
            url=migration_url,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
        ),
    )
    migration_engine = create_async_engine(
        migration_url.get_secret_value(),
        hide_parameters=True,
    )
    runtime_engine = create_async_engine(
        _role_url(
            postgresql_provision, RUNTIME_ROLE, RUNTIME_PASSWORD
        ).get_secret_value(),
        pool_size=1,
        max_overflow=0,
        pool_reset_on_return="rollback",
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    async with migration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO spine.workspaces (id, slug, display_name) VALUES "
                "(:workspace_a, 'auth-contract-a', 'Auth Contract A'), "
                "(:workspace_b, 'auth-contract-b', 'Auth Contract B') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"workspace_a": WORKSPACE_ID, "workspace_b": OTHER_WORKSPACE_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO spine.environments "
                "(id, workspace_id, kind, display_name) VALUES "
                "(:environment_a, :workspace_a, 'development', 'Auth A'), "
                "(:environment_b, :workspace_b, 'development', 'Auth B') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "environment_a": ENVIRONMENT_ID,
                "workspace_a": WORKSPACE_ID,
                "environment_b": OTHER_ENVIRONMENT_ID,
                "workspace_b": OTHER_WORKSPACE_ID,
            },
        )
    try:
        yield AuthorizationDatabase(
            migration_engine=migration_engine,
            runtime_engine=runtime_engine,
        )
    finally:
        await runtime_engine.dispose()
        async with migration_engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS spine CASCADE"))
        await migration_engine.dispose()


async def _reset_authorization_records(
    database: AuthorizationDatabase,
    seed_records: AuthorizationDirectoryRecords,
) -> None:
    async with database.migration_engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE " + ", ".join(f"spine.{table}" for table in AUTH_TABLES) + " CASCADE")
        )
        await connection.execute(text("TRUNCATE TABLE spine.authorization_generations"))
        for owner in seed_records.environment_owners:
            await connection.execute(
                text(
                    "UPDATE spine.environments SET workspace_id = :workspace_id "
                    "WHERE id = :environment_id"
                ),
                {
                    "workspace_id": owner.workspace_id,
                    "environment_id": owner.environment_id,
                },
            )
        await connection.execute(
            text(
                "INSERT INTO spine.authorization_generations "
                "(workspace_id, environment_id, generation) "
                "SELECT workspace_id, id, 1 FROM spine.environments"
            )
        )


@pytest_asyncio.fixture(loop_scope="session")
async def authorization_directory_factory(
    authorization_database: AuthorizationDatabase,
) -> AuthorizationDirectoryFactory:
    migration_sessions = async_sessionmaker(
        authorization_database.migration_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    runtime_sessions = async_sessionmaker(
        authorization_database.runtime_engine,
        expire_on_commit=False,
        autoflush=False,
    )

    async def create_directory(
        seed_records: AuthorizationDirectoryRecords,
    ) -> PostgreSQLAuthorizationDirectory:
        await _reset_authorization_records(authorization_database, seed_records)
        owners = {
            owner.environment_id: owner.workspace_id
            for owner in seed_records.environment_owners
        }
        physical_records = seed_records.model_copy(
            update={
                "environment_memberships": tuple(
                    membership.model_copy(
                        update={
                            "workspace_id": owners.get(
                                membership.environment_id,
                                membership.workspace_id,
                            )
                        }
                    )
                    for membership in seed_records.environment_memberships
                ),
                "role_bindings": tuple(
                    binding.model_copy(
                        update={
                            "workspace_id": owners.get(
                                binding.environment_id,
                                binding.workspace_id,
                            )
                        }
                    )
                    for binding in seed_records.role_bindings
                ),
            }
        )
        helper = PostgreSQLAuthorizationSeedHelper(migration_sessions)
        await helper.append(physical_records)
        generation_scopes = {
            (generation.workspace_id, generation.environment_id)
            for generation in seed_records.generations
        }
        async with authorization_database.migration_engine.begin() as connection:
            for owner in seed_records.environment_owners:
                if (owner.workspace_id, owner.environment_id) not in generation_scopes:
                    await connection.execute(
                        text(
                            "DELETE FROM spine.authorization_generations "
                            "WHERE workspace_id = :workspace_id "
                            "AND environment_id = :environment_id"
                        ),
                        {
                            "workspace_id": owner.workspace_id,
                            "environment_id": owner.environment_id,
                        },
                    )
        return PostgreSQLAuthorizationDirectory(runtime_sessions)

    return create_directory


async def test_postgresql_current_records_resolve_exact_contract(
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    await contract_current(authorization_directory_factory)


@pytest.mark.parametrize(
    ("case", "record_overrides"),
    DENIED_RECORD_OVERRIDES,
    ids=[case for case, _ in DENIED_RECORD_OVERRIDES],
)
async def test_postgresql_inactive_records_share_disclosure_safe_denial(
    authorization_directory_factory: AuthorizationDirectoryFactory,
    case: str,
    record_overrides: dict[str, object],
) -> None:
    await contract_denied(authorization_directory_factory, case, record_overrides)


async def test_postgresql_latest_history_is_only_current_authority(
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    await contract_history(authorization_directory_factory)


async def test_postgresql_same_identity_is_strictly_tenant_local(
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    await contract_isolation(authorization_directory_factory)


async def test_runtime_cannot_enumerate_or_mutate_authorization_tables(
    authorization_database: AuthorizationDatabase,
) -> None:
    for table in (
        "canonical_human_identities",
        "canonical_human_identity_states",
        "authentication_alias_bindings",
        "workspace_memberships",
        "environment_memberships",
        "environment_role_bindings",
        "authorization_generations",
    ):
        with pytest.raises(ProgrammingError):
            async with authorization_database.runtime_engine.connect() as connection:
                await connection.execute(text(f"SELECT * FROM spine.{table}"))


async def test_runtime_role_cannot_use_migration_owner_seed_helper(
    authorization_database: AuthorizationDatabase,
) -> None:
    helper = PostgreSQLAuthorizationSeedHelper(
        async_sessionmaker(authorization_database.runtime_engine)
    )

    with pytest.raises(RuntimeError, match="migration owner"):
        await helper.append(AuthorizationDirectoryRecords())


async def test_history_rows_are_immutable_through_trusted_internal_path(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    await authorization_directory_factory(records())

    with pytest.raises(IntegrityError) as captured:
        async with authorization_database.migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE spine.workspace_memberships SET status = 'revoked' "
                    "WHERE membership_id = :membership_id"
                ),
                {"membership_id": records().workspace_memberships[0].membership_id},
            )

    assert (
        captured.value.orig.diag.constraint_name
        == "ck_authorization_history_immutable"
    )


async def test_database_constraint_accepts_only_exact_administrator_role(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    await authorization_directory_factory(records())

    with pytest.raises(IntegrityError) as captured:
        async with authorization_database.migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO spine.environment_role_bindings "
                    "(binding_id, workspace_id, environment_id, "
                    "canonical_human_identity_id, role, version, status, "
                    "change_id, recorded_by) VALUES "
                    "(:binding_id, :workspace_id, :environment_id, :identity_id, "
                    "'owner', 1, 'active', :change_id, 'synthetic-test-bootstrap')"
                ),
                {
                    "binding_id": UUID("14000000-0000-0000-0000-000000000098"),
                    "workspace_id": WORKSPACE_ID,
                    "environment_id": ENVIRONMENT_ID,
                    "identity_id": IDENTITY_ID,
                    "change_id": UUID("90000000-0000-0000-0000-000000000098"),
                },
            )

    assert (
        captured.value.orig.diag.constraint_name
        == "ck_environment_role_bindings_role"
    )


async def test_effective_change_advances_generation_and_retains_history(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    directory = await authorization_directory_factory(records())
    before = await directory.resolve_request_authority(
        authenticated_alias(), scope(), route_policy()
    )
    active = records().workspace_memberships[0]
    revoked = active.model_copy(
        update={
            "membership_id": UUID("12000000-0000-0000-0000-000000000099"),
            "version": 2,
            "status": AuthorizationRecordStatus.REVOKED,
            "provenance": provenance(99),
        }
    )
    helper = PostgreSQLAuthorizationSeedHelper(
        async_sessionmaker(authorization_database.migration_engine)
    )
    await helper.append(
        AuthorizationDirectoryRecords(workspace_memberships=(revoked,))
    )

    with pytest.raises(AuthorizationDeniedError):
        await directory.resolve_request_authority(
            authenticated_alias(), scope(), route_policy()
        )
    async with authorization_database.migration_engine.connect() as connection:
        generation = await connection.scalar(
            text(
                "SELECT generation FROM spine.authorization_generations "
                "WHERE workspace_id = :workspace_id AND environment_id = :environment_id"
            ),
            {"workspace_id": WORKSPACE_ID, "environment_id": ENVIRONMENT_ID},
        )
        history_count = await connection.scalar(
            text(
                "SELECT count(*) FROM spine.workspace_memberships "
                "WHERE workspace_id = :workspace_id "
                "AND canonical_human_identity_id = :identity_id"
            ),
            {"workspace_id": WORKSPACE_ID, "identity_id": IDENTITY_ID},
        )

    assert generation == before.authorization_generation + 1
    assert history_count == 2


async def test_active_successor_makes_prior_resolution_generation_stale(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    directory = await authorization_directory_factory(records())
    prior = await directory.resolve_request_authority(
        authenticated_alias(), scope(), route_policy()
    )
    active = records().workspace_memberships[0]
    successor = active.model_copy(
        update={
            "membership_id": UUID("12000000-0000-0000-0000-000000000089"),
            "version": 2,
            "provenance": provenance(89),
        }
    )
    helper = PostgreSQLAuthorizationSeedHelper(
        async_sessionmaker(authorization_database.migration_engine)
    )

    await helper.append(
        AuthorizationDirectoryRecords(workspace_memberships=(successor,))
    )
    current = await directory.resolve_request_authority(
        authenticated_alias(), scope(), route_policy()
    )

    assert current.authorization_generation == prior.authorization_generation + 1
    assert prior.authorization_generation < current.authorization_generation


async def test_concurrent_duplicate_alias_version_has_one_logical_effect(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    baseline = records()
    other_identity = CanonicalHumanIdentity(
        identity_id=OTHER_IDENTITY_ID,
        version=1,
        status=AuthorizationRecordStatus.ACTIVE,
        provenance=provenance(93),
    )
    await authorization_directory_factory(
        baseline.model_copy(
            update={"identities": baseline.identities + (other_identity,)}
        )
    )
    helper = PostgreSQLAuthorizationSeedHelper(
        async_sessionmaker(authorization_database.migration_engine)
    )
    first = AuthenticationAliasBinding(
        binding_id=UUID("11000000-0000-0000-0000-000000000091"),
        alias=authenticated_alias().alias,
        canonical_human_identity_id=IDENTITY_ID,
        version=2,
        status=AuthorizationRecordStatus.ACTIVE,
        provenance=provenance(91),
    )
    second = first.model_copy(
        update={
            "binding_id": UUID("11000000-0000-0000-0000-000000000092"),
            "canonical_human_identity_id": OTHER_IDENTITY_ID,
            "provenance": provenance(92),
        }
    )

    outcomes = await asyncio.gather(
        helper.append(AuthorizationDirectoryRecords(alias_bindings=(first,))),
        helper.append(AuthorizationDirectoryRecords(alias_bindings=(second,))),
        return_exceptions=True,
    )

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, IntegrityError) for outcome in outcomes) == 1
    async with authorization_database.migration_engine.connect() as connection:
        generation = await connection.scalar(
            text(
                "SELECT generation FROM spine.authorization_generations "
                "WHERE workspace_id = :workspace_id AND environment_id = :environment_id"
            ),
            {"workspace_id": WORKSPACE_ID, "environment_id": ENVIRONMENT_ID},
        )
    assert generation == 8


async def test_concurrent_effective_changes_advance_generation_once_each(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    baseline = records()
    await authorization_directory_factory(baseline)
    workspace_successor = baseline.workspace_memberships[0].model_copy(
        update={
            "membership_id": UUID("12000000-0000-0000-0000-000000000088"),
            "version": 2,
            "provenance": provenance(88),
        }
    )
    role_successor = baseline.role_bindings[0].model_copy(
        update={
            "binding_id": UUID("14000000-0000-0000-0000-000000000087"),
            "version": 2,
            "provenance": provenance(87),
        }
    )
    helper = PostgreSQLAuthorizationSeedHelper(
        async_sessionmaker(authorization_database.migration_engine)
    )

    outcomes = await asyncio.gather(
        helper.append(
            AuthorizationDirectoryRecords(
                workspace_memberships=(workspace_successor,)
            )
        ),
        helper.append(
            AuthorizationDirectoryRecords(role_bindings=(role_successor,))
        ),
        return_exceptions=True,
    )

    assert outcomes == [None, None]
    async with authorization_database.migration_engine.connect() as connection:
        generation = await connection.scalar(
            text(
                "SELECT generation FROM spine.authorization_generations "
                "WHERE workspace_id = :workspace_id AND environment_id = :environment_id"
            ),
            {"workspace_id": WORKSPACE_ID, "environment_id": ENVIRONMENT_ID},
        )
    assert generation == 9


async def test_resolution_never_combines_uncommitted_change_with_new_generation(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    directory = await authorization_directory_factory(records())
    revoked = records().workspace_memberships[0].model_copy(
        update={
            "membership_id": UUID("12000000-0000-0000-0000-000000000090"),
            "version": 2,
            "status": AuthorizationRecordStatus.REVOKED,
            "provenance": provenance(90),
        }
    )
    session_factory = async_sessionmaker(authorization_database.migration_engine)
    session = session_factory()
    transaction = await session.begin()
    try:
        await session.execute(
            insert(workspace_memberships).values(
                membership_id=revoked.membership_id,
                workspace_id=revoked.workspace_id,
                canonical_human_identity_id=revoked.canonical_human_identity_id,
                version=revoked.version,
                status=revoked.status.value,
                change_id=revoked.provenance.change_id,
                recorded_by=revoked.provenance.recorded_by,
            )
        )
        before_commit = await directory.resolve_request_authority(
            authenticated_alias(), scope(), route_policy()
        )
        assert before_commit.authorization_generation == 7
        await transaction.commit()
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await session.close()

    with pytest.raises(AuthorizationDeniedError):
        await directory.resolve_request_authority(
            authenticated_alias(), scope(), route_policy()
        )


async def test_transaction_local_scope_does_not_leak_on_pool_reuse(
    authorization_database: AuthorizationDatabase,
    authorization_directory_factory: AuthorizationDirectoryFactory,
) -> None:
    directory = await authorization_directory_factory(records())
    await directory.resolve_request_authority(
        authenticated_alias(), scope(), route_policy()
    )

    async with authorization_database.runtime_engine.connect() as connection:
        workspace_setting = await connection.scalar(
            text("SELECT current_setting('spine.workspace_id', true)")
        )
        environment_setting = await connection.scalar(
            text("SELECT current_setting('spine.environment_id', true)")
        )

    assert workspace_setting in (None, "")
    assert environment_setting in (None, "")


async def test_authorization_catalog_is_classified_forced_rls_and_least_privilege(
    authorization_database: AuthorizationDatabase,
) -> None:
    async with authorization_database.migration_engine.connect() as connection:
        comments = {
            row.table_name: row.comment
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, "
                        "obj_description(relation.oid, 'pg_class') AS comment "
                        "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname = ANY(:tables)"
                    ),
                    {
                        "tables": [
                            "canonical_human_identities",
                            "canonical_human_identity_states",
                            "authentication_alias_bindings",
                            "workspace_memberships",
                            "environment_memberships",
                            "environment_role_bindings",
                            "authorization_generations",
                        ]
                    },
                )
            ).mappings()
        }
        rls = {
            row.table_name: (row.enabled, row.forced)
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, "
                        "relation.relrowsecurity AS enabled, "
                        "relation.relforcerowsecurity AS forced "
                        "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' "
                        "AND relation.relname = ANY(:tables)"
                    ),
                    {
                        "tables": [
                            "workspace_memberships",
                            "environment_memberships",
                            "environment_role_bindings",
                            "authorization_generations",
                        ]
                    },
                )
            ).mappings()
        }
        table_privileges = {
            row.table_name: row.has_any
            for row in (
                await connection.execute(
                    text(
                        "SELECT relation.relname AS table_name, "
                        "has_table_privilege(:runtime, relation.oid, "
                        "'SELECT,INSERT,UPDATE,DELETE') AS has_any "
                        "FROM pg_class AS relation JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "WHERE namespace.nspname = 'spine' AND relation.relname = ANY(:tables)"
                    ),
                    {
                        "runtime": RUNTIME_ROLE,
                        "tables": list(comments),
                    },
                )
            ).mappings()
        }
        resolver = (
            await connection.execute(
                text(
                    "SELECT procedure.prosecdef AS security_definer, "
                    "procedure.proconfig AS config, "
                    "has_function_privilege(:runtime, procedure.oid, 'EXECUTE') "
                    "AS runtime_execute, "
                    "has_function_privilege('public', procedure.oid, 'EXECUTE') "
                    "AS public_execute "
                    "FROM pg_proc AS procedure JOIN pg_namespace AS namespace "
                    "ON namespace.oid = procedure.pronamespace "
                    "WHERE namespace.nspname = 'spine' "
                    "AND procedure.proname = 'resolve_current_authorization_snapshot'"
                ),
                {"runtime": RUNTIME_ROLE},
            )
        ).mappings().one()

    assert comments == {
        "canonical_human_identities": (
            "classification=platform_identity; runtime_table_access=none"
        ),
        "canonical_human_identity_states": (
            "classification=platform_identity; runtime_table_access=none"
        ),
        "authentication_alias_bindings": (
            "classification=platform_identity; runtime_table_access=none"
        ),
        "workspace_memberships": (
            "classification=tenant_authorization; runtime_table_access=none"
        ),
        "environment_memberships": (
            "classification=tenant_authorization; runtime_table_access=none"
        ),
        "environment_role_bindings": (
            "classification=tenant_authorization; runtime_table_access=none"
        ),
        "authorization_generations": (
            "classification=tenant_authorization; runtime_table_access=none"
        ),
    }
    assert rls == {
        "workspace_memberships": (True, True),
        "environment_memberships": (True, True),
        "environment_role_bindings": (True, True),
        "authorization_generations": (True, True),
    }
    assert table_privileges == {table: False for table in comments}
    assert resolver.security_definer is True
    assert tuple(resolver.config) == ("search_path=pg_catalog, spine",)
    assert resolver.runtime_execute is True
    assert resolver.public_execute is False


async def test_schema_contains_no_raw_token_or_arbitrary_claim_storage(
    authorization_database: AuthorizationDatabase,
) -> None:
    async with authorization_database.migration_engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'spine' AND table_name = ANY(:tables)"
                    ),
                    {
                        "tables": [
                            "canonical_human_identities",
                            "canonical_human_identity_states",
                            "authentication_alias_bindings",
                            "workspace_memberships",
                            "environment_memberships",
                            "environment_role_bindings",
                            "authorization_generations",
                        ]
                    },
                )
            ).scalars()
        )

    assert columns.isdisjoint(
        {"access_token", "id_token", "refresh_token", "claims", "groups"}
    )
