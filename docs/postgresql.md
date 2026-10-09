# PostgreSQL bootstrap, migrations, runtime and test harness

Issue #42 establishes the database runtime and mandatory real-PostgreSQL test
prerequisite. Issue #43 adds the explicit operator bootstrap, the linear Alembic
environment, and the first canonical Workspace and Environment tables. It does
not add application repositories, RLS policies, or runtime tenant-table DML.

## Supported stack

Spine supports PostgreSQL major **17**. Local Testcontainers and CI use the exact
image `postgres:17.6-bookworm`. The resolved Python dependency versions are
recorded in `uv.lock`; SQLAlchemy 2, Alembic, and Psycopg 3 are direct runtime
dependencies, while PostgreSQL Testcontainers is a direct `dev` dependency.

`DatabaseRuntime` creates one bounded async SQLAlchemy engine for its owning
process after `RuntimeDatabaseSettings` validates. It creates an
`async_sessionmaker` for operation-owned sessions and awaits engine disposal at
shutdown. Pool pre-ping, rollback-on-return, pool capacity, checkout timeout,
and Psycopg connection timeout are set explicitly. Startup opens a connection
and rejects an unreachable server or a PostgreSQL major other than 17; any
startup failure disposes the new engine before returning a sanitized error.

Runtime and migration credentials have separate configuration surfaces:

```dotenv
SPINE_DATABASE_URL=postgresql+psycopg://runtime_user:secret@db/spine
SPINE_DATABASE_POOL_SIZE=5
SPINE_DATABASE_MAX_OVERFLOW=5
SPINE_DATABASE_POOL_TIMEOUT_SECONDS=30
SPINE_DATABASE_CONNECT_TIMEOUT_SECONDS=10
SPINE_DATABASE_POOL_PRE_PING=true
SPINE_DATABASE_TRANSACTION_RETRY_LIMIT=3

SPINE_MIGRATION_DATABASE_URL=postgresql+psycopg://migration_owner:secret@db/spine
SPINE_MIGRATION_DATABASE_MIGRATION_ROLE=spine_migration
SPINE_MIGRATION_DATABASE_RUNTIME_ROLE=spine_runtime
```

Importing settings or persistence modules does not load either surface and does
not create an engine, connection, container, or socket. Migration credentials
are consumed only by explicit operator commands.

## Operator bootstrap and migration

Provisioning has three separate authorities:

- the operator identity can create the database and login roles;
- `spine_migration` owns the application database and `spine` schema and applies
  revisions;
- `spine_runtime` is a non-owner login with `NOBYPASSRLS`, no role-creation
  capability, and—until the RLS migration lands—no tenant-table access.

Set the operator-only values in the environment of a trusted deployment step,
not in an application runtime:

```dotenv
SPINE_OPERATOR_DATABASE_URL=postgresql+psycopg://operator:secret@db/postgres
SPINE_OPERATOR_DATABASE_DATABASE_NAME=spine
SPINE_OPERATOR_DATABASE_MIGRATION_ROLE=spine_migration
SPINE_OPERATOR_DATABASE_MIGRATION_PASSWORD=generated-migration-secret
SPINE_OPERATOR_DATABASE_RUNTIME_ROLE=spine_runtime
SPINE_OPERATOR_DATABASE_RUNTIME_PASSWORD=generated-runtime-secret
```

Then run the phases explicitly:

```bash
uv run spine-db-bootstrap
uv run spine-db-migrate
uv run spine-db-bootstrap-workspace \
  --id 10000000-0000-0000-0000-000000000001 \
  --slug initial \
  --display-name "Initial Workspace"
```

`spine-db-bootstrap` creates or reconciles only the database and the two login
roles. Reconciliation removes runtime-role memberships and direct database
authority before granting back `CONNECT` only. `spine-db-migrate` verifies the
current user, database owner, role separation, runtime privileges, memberships,
database/schema/table access, and existing schema owner before it changes the
schema. Runtime and migration connections use the controlled search path
`pg_catalog,spine`. The Alembic version table is `spine.alembic_version`, and the
committed history has one linear head.

`spine-db-bootstrap-workspace` is a one-time migration-authority action. It
atomically inserts the first Workspace and an operator audit record, then seals
itself. It cannot list, update, delete, or impersonate another Workspace and
does not grant document-content authority. Ordinary runtime credentials cannot
invoke it or access its tables.

Application startup never creates roles or schemas, invokes Alembic, or calls
`metadata.create_all`. A missing role, wrong database/schema owner, privileged
runtime role, or unknown migration state is an operator error, not something
startup repairs.

### Failure recovery

Spine migrations use PostgreSQL transactional DDL. If a revision fails, the
revision row and all DDL from that attempt roll back together; fix the cause and
run `uv run spine-db-migrate` again. Verify the surviving revision with:

```sql
SELECT version_num FROM spine.alembic_version;
```

Do not stamp past a failed revision or edit the version table manually. Any
future non-transactional revision must ship its own bounded recovery procedure
and failure test before it can be accepted. Downgrade is implemented only when
truthful; backup restoration or a corrective forward migration remains the
operational recovery path for irreversible changes.

## Mandatory PostgreSQL tests

`uv run pytest -q` always includes the real PostgreSQL suite. With no dedicated
explicit target, the harness starts a unique container, database, user, and
password from `postgres:17.6-bookworm`. If neither a safe explicit URL nor a
working container runtime is available, the suite fails; it does not skip the
PostgreSQL gate.

An explicit target is accepted only through all three test-only settings:

```dotenv
SPINE_TEST_DATABASE_URL=postgresql+psycopg://test_owner:secret@host/spine_test_local
SPINE_TEST_DATABASE_EXPECTED_NAME=spine_test_local
SPINE_TEST_DATABASE_DISPOSABLE_MARKER=spine-test-harness:v1:local_owner_supplied_token
SPINE_TEST_DATABASE_USE_TESTCONTAINERS=false
```

The database must already exist, the URL user must be able to create and drop
the harness's generated schemas and `NOLOGIN` roles, and the exact marker must
already be stored as the database comment:

```sql
COMMENT ON DATABASE spine_test_local
IS 'spine-test-harness:v1:local_owner_supplied_token';
```

The base database name must begin with `spine_test_`; `postgres`, `template0`,
`template1`, broad names, wildcard identifiers, and a marker mismatch are
rejected. The test settings never inspect `SPINE_DATABASE_URL` or
`SPINE_MIGRATION_DATABASE_URL`.

Each explicit run and xdist worker receives generated schema and role names.
Before namespace creation or cleanup, the harness verifies PostgreSQL major 17,
the current database name, and the exact marker used as the expected disposable
server/database identity token, then acquires a database-scoped advisory lock.
Before cleanup it verifies the identity, advisory lock, recorded identifiers,
and catalog ownership again. Cleanup uses only exact quoted identifiers created
and recorded by that run. It never searches by prefix and never drops the
explicit base database. When any proof is missing, the resources remain intact
and the gate reports the failure.

URLs, passwords, marker values, and usernames are excluded from settings
representations and harness errors.
