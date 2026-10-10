# PostgreSQL bootstrap, migrations, runtime and test harness

Issue #42 establishes the database runtime and mandatory real-PostgreSQL test
prerequisite. Issue #43 adds the explicit operator bootstrap, the linear Alembic
environment, and the first canonical Workspace and Environment tables. Issue
#44 adds forced tenant RLS and the minimum ordinary runtime privileges; it does
not add application repositories or trusted context binding. Issue #45 adds the
production SQLAlchemy Unit of Work, trusted transaction-context binding, and
purpose-specific Workspace and Environment repositories. Issue #46 adds
tenant-scoped PostgreSQL idempotency receipts and complete-transaction retry.
Issue #47 adds typed, allowlisted transactional outbox intents that commit with
canonical mutations. Issue #48 adds exact-head runtime readiness, the final
migration/catalog audit, composed receipt-mutation-outbox recovery tests, and
the mandatory persistence-kernel release gate.

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
SPINE_DATABASE_RUNTIME_ROLE=spine_runtime
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

## Runtime Unit of Work

`PostgreSQLPersistence` is assembled from
`DatabaseRuntime.resources.session_factory` and a composition-root-owned
`TrustedContextVerifier`. Its `uow_factory` creates a single-use complete async
Unit of Work for the Workspace, Environment, idempotency, and outbox ports;
`tenant_uow_factory` remains a compatibility alias for the narrower #45 seam.
Entering creates exactly one async session and one explicit `READ COMMITTED`
transaction. Before a repository can execute, the adapter
re-verifies the context proof and binds Workspace, optional Environment, acting
subject, service principal, purpose, operation, and trace as parameterized
transaction-local PostgreSQL settings.

Workspace and Environment repositories use schema-qualified SQLAlchemy Core
mappings and explicit tenant predicates in addition to RLS. An
Environment-scoped context is accepted only after the selected Environment is
resolved under the same bound Workspace and Environment. Workspace-scoped
Environment lifecycle operations temporarily narrow the transaction-local
Environment setting to the exact Environment argument before executing the
RLS-guarded statement.

Repositories never commit or roll back. A successful explicit `commit()` is the
only normal persistence path; leaving the context without it rolls back. A Unit
of Work is single-entry and task-owned, and rejects re-entry, concurrent use,
post-close use, and implicit nesting. Statement, commit, cancellation, and
connection-invalidating failures make the Unit of Work terminal, attempt
rollback, and close the owned session before returning a stable redacted
application error. Deadlock and serialization failures retain distinct retryable
categories; retry orchestration remains outside this adapter.

## Idempotency receipts and transaction retry

Revision `20261010_03` adds immutable receipt identity and digest fields plus a
single completion transition to an opaque result reference. Workspace-scoped
and Environment-scoped commands use separate partial unique indexes, so a
nullable Environment cannot weaken uniqueness. Environment receipts also carry
a composite Workspace/Environment foreign key. Forced RLS applies both scope
identifiers; runtime credentials receive `SELECT` and `INSERT`, plus column-level
`UPDATE` only for the three result-reference fields. A database trigger permits
exactly one `NULL → complete reference` transition and rejects rewriting or
clearing a completed receipt even when SQL bypasses the repository adapter.

`claim()` inserts a pending receipt inside the caller's transaction. PostgreSQL
unique-index arbitration serializes concurrent duplicates: a loser waits for
the owner, then either reads its committed opaque reference or becomes the owner
after rollback. A Unit of Work cannot commit while one of its owned claims is
incomplete. Receipts never store protected response content and replay does not
preserve authorization; the consuming use case resolves the reference under
current policy.

`run_transaction_with_retry()` creates a fresh Unit of Work for every attempt
and commits only after the complete callback succeeds. It retries only translated
deadlock or serialization failures when the operation is idempotent, has an
idempotency key, is reproducible, and declares no irreversible external side
effect. The default remains three total attempts; exhaustion is returned to the
workflow layer rather than becoming a durable loop.

## Transactional outbox intents

Revision `20261010_04` is the single Alembic head and follows the merged
idempotency revision `20261010_03`. It adds immutable outbox intents with a
globally stable event ID, exact tenant scope, versioned event identity, optional
opaque aggregate reference, producer deduplication identity, minimal JSON
payload, and trace/correlation/causation references. Workspace and Environment
producer identities use separate partial unique indexes, and Environment rows
carry the composite Workspace/Environment foreign key. The event identity has a
database default; when a producer omits it, the PostgreSQL writer obtains one
from `gen_random_uuid()` in the same transaction before its insert.

The application composition root supplies an `OutboxEventRegistry`. Producers
register an exact event-type/schema-version pair with a Pydantic payload model
configured with `extra="forbid"` and `frozen=True`; the writer rejects
schemas with mutable nested field types, unregistered events, or mismatched
payloads before persistence. Payload schemas use opaque object references and
safe transition metadata. After strict validation the registry captures one
canonical JSON snapshot, which adapters persist without reserializing live model
state. The kernel does not scan arbitrary JSON for secrets, and it does not
implement claim, dispatch, acknowledgement, retries, cleanup, audit processing,
or workflow state.

`UnitOfWork.outbox.append()` validates exact scope and trusted trace lineage and
inserts through the same transaction as canonical repositories. Runtime
credentials have only `INSERT` on the outbox table. Forced RLS guards the insert,
named constraints translate
duplicate event and producer identities, and a database trigger rejects update
or delete even for migration-authority SQL. Shared fake/PostgreSQL contracts and
real-PostgreSQL failure injection cover rollback after canonical mutation,
after append, during statement flush, before commit, and during commit.

## Operator bootstrap and migration

Provisioning has three separate authorities:

- the operator identity can create the database and login roles;
- `spine_migration` owns the application database and `spine` schema and applies
  revisions;
- `spine_runtime` is a non-owner login with `NOBYPASSRLS`, no role-creation
  capability, schema `USAGE`, and only RLS-guarded DML on the canonical Workspace,
  Environment, idempotency-receipt, and outbox-intent tables.

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
unsafe database/schema/table access, forced RLS protection, and existing schema
owner before it changes the schema. Runtime and migration connections use the
controlled search path `pg_catalog,spine`. The Alembic version table is
`spine.alembic_version`, and the committed history has one linear head:
`20261010_07`. The source-observation migration adds the source ledger tables.
The readiness migration grants the runtime role read-only access
to the revision identity; K0 adds migration-owned, fixed-search-path receipt
tenant validation and receipt-scope immutability without adding child tables or
schema mutation authority.

`spine-db-bootstrap-workspace` is a one-time migration-authority action. It
atomically inserts the first Workspace and an operator audit record, then seals
itself. It cannot list, update, delete, or impersonate another Workspace and
does not grant document-content authority. Ordinary runtime credentials cannot
invoke it or access its tables.

Application startup never creates roles or schemas, invokes Alembic, or calls
`metadata.create_all`. Readiness succeeds only when PostgreSQL major 17 is in
use, the database reports exactly head `20261010_07`, the connected user is the
configured restricted runtime role, required grants and forced RLS are intact,
and transaction-local Workspace/Environment context can be bound. Empty,
older, newer, unknown, and multiple-head states fail closed; the newly created
engine is disposed and the revision is not changed. A missing role, wrong
database/schema owner, privileged runtime role, or incompatible migration state
is an operator error, not something startup repairs.

## Tenant RLS convention

Every migration that creates a tenant-owned table must classify it explicitly:

- a **workspace-only** table carries `workspace_id` (or, for the Workspace root,
  uses `id`) and compares it with transaction-local `spine.workspace_id`;
- an **environment-scoped** table carries both `workspace_id` and
  `environment_id`, enforces their composite ownership, and compares both with
  transaction-local `spine.workspace_id` and `spine.environment_id`.

The initial `workspaces` and sealed bootstrap records are workspace-only. The
canonical `environments` table is environment-scoped. Idempotency receipts use
workspace scope when `environment_id` is null and environment scope otherwise,
with separate uniqueness indexes for the two cases. Outbox intents follow the
same nullable-scope convention and allow runtime `INSERT` only. Missing or malformed
required settings match no rows and fail write checks. Policies must define both
`USING` and `WITH CHECK`, and tenant tables must use both `ENABLE ROW LEVEL
SECURITY` and `FORCE ROW LEVEL SECURITY`. The same revision must install the
policy before granting the runtime role the minimum required DML and sequence
privileges. A table with no sequence grants none; a later migration that creates
a sequence must grant only that exact schema-qualified sequence. Policy, grant,
table, sequence, and function operations remain schema-qualified and
connections retain the controlled search path.

The migration owner has an explicit maintenance policy because it already owns
and evolves the schema and must preserve the sealed initial-Workspace bootstrap.
Application and isolation tests always use `spine_runtime`; that role cannot
assume migration ownership, alter policies or schema, disable RLS, or receive
`TRUNCATE`, `REFERENCES`, or `TRIGGER` privileges.

RLS is tenant/environment defense in depth only. It never grants
`read_content`, `process_content`, administrator authority, or any other
document permission from ADR 0011. Trusted application code must still bind the
validated transaction context, include tenant predicates, evaluate Access
Policy where protected content is involved, and never accept caller-supplied
SQL.

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

Run the release gate with:

```bash
make test-release
```

It runs the complete suite including real PostgreSQL, compilation, and
`git diff --check`. `make verify` additionally synchronizes the declared
development environment first. For a quicker local loop only:

```bash
make test-fast
```

The latter prints that PostgreSQL acceptance has not run and is never sufficient
for release or ticket completion.

## Deployment prerequisites and recovery

The deployment must provide PostgreSQL 17, operator authority for the one-time
database/role bootstrap, separately stored migration and runtime credentials,
and durable backup/restore appropriate to its environment. Spine does not
provision the server, network policy, TLS certificates, secret manager, backup
backend, or container runtime. Production rollout must run operator bootstrap
and `spine-db-migrate` before starting the application with runtime credentials.

If readiness reports an incompatible revision, stop the application and inspect
`spine.alembic_version` with migration authority. Apply the supported forward
migration; do not stamp or edit the version table manually. For a failed
transactional migration, fix the cause and rerun it—the previous revision and
schema remain authoritative. For an irreversible future change, restore the
deployment backup or apply its documented corrective forward migration. The
sealed initial-Workspace bootstrap remains one-time and is not a general tenant
recovery or content-access mechanism.
