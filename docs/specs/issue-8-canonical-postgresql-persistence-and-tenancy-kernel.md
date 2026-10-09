# R1 Canonical PostgreSQL Persistence and Tenancy Kernel

Status: draft for maintainer approval; not yet published to the issue tracker.

Originating issue: GitHub Issue #8, "[R0 blocker] Establish PostgreSQL
persistence and migration foundations."

## Problem Statement

Spine has accepted PostgreSQL as the canonical store for Spine-owned state, but
the repository does not yet have an application persistence interface, a
supported PostgreSQL driver, an owned SQLAlchemy adapter, Alembic migrations, or
executable tenant-isolation guarantees. The existing database and outbox areas
are scaffolds, while SQLAlchemy and Alembic are present only as transitive Cognee
dependencies and therefore are not part of Spine's supported application stack.

The next R1 capabilities need to persist workspace-owned and
environment-specific state without importing SQLAlchemy into the domain or
application layers. They also need one dependable transaction boundary for
canonical mutations, idempotency receipts, and outbox intents. Without this
kernel, later source, access-policy, object-storage, audit, and projection work
would each have to invent session ownership, tenant filtering, retry semantics,
and migration behavior independently.

This specification defines the smallest framework-independent persistence
foundation required by the R1 governed document Q&A slice. It establishes the
database lifecycle, application-facing Unit of Work seam, canonical Workspace
and Environment ownership, PostgreSQL Row-Level Security defense in depth,
idempotency and concurrency behavior, forward migrations, and the persistence
half of a transactional outbox.

It does not define the document, policy, source-revision, projection, audit,
authentication, API, or workflow schemas that will consume this foundation.

### Current Repository Assessment

- PostgreSQL is an accepted architectural target but no Spine-owned database
  engine, session factory, mapped tables, migration environment, or database
  integration-test harness exists.
- The infrastructure database and outbox modules contain responsibility
  descriptions only. Application command, query, and policy modules are also
  scaffolds.
- Domain models already define UUID-identified `Workspace` and `Environment`
  concepts and the `development`, `staging`, and `production` environment kinds.
- Existing domain-boundary tests prohibit SQLAlchemy imports from the domain
  layer. That invariant remains mandatory.
- SQLAlchemy and Alembic occur in the lock file through Cognee but are not direct
  project dependencies. There is no declared Psycopg or Testcontainers
  dependency.
- The legacy Q&A prototype calls Cognee directly and has no canonical
  persistence. This specification must not break or silently replace those
  compatibility paths.
- Existing prototype tests for Issues #11, #13, and #28 are decision evidence,
  not production persistence tests or interfaces.

### Existing Decisions and Issue Relationships

- ADR 0001 makes PostgreSQL the source of truth for Spine-owned state and keeps
  the Context Graph rebuildable and replaceable.
- ADR 0004 requires a canonical mutation and its immutable, versioned outbox
  intent to commit atomically in PostgreSQL while deferring delivery behavior and
  preserving Temporal ownership of composite durable lifecycle.
- ADR 0010 keeps composite durable lifecycle, long waits, approvals, recovery,
  and durable workflow retries in Temporal. PostgreSQL, an outbox, and explicit
  workers may own short idempotent work units.
- ADR 0011 makes the current versioned `SourceObject` Access Policy in PostgreSQL
  the sole document-authorization authority. RLS may narrow tenant access but
  never replaces Access Policy evaluation.
- ADR 0018 requires immutable observations, append-only acceptance decisions,
  commit-consistent canonical ordering, PostgreSQL-owned projection activation,
  compare-and-swap transitions, and fail-safe handling of drift and tombstones.
- Completed Issue #2 establishes trusted, fail-closed document authorization and
  confirms that administrators are not content superusers.
- Completed Issue #11 selects a hybrid durability model: PostgreSQL, outbox, and
  workers own short idempotent units; Temporal owns composite durable lifecycle.
- Completed Issues #12 and #28 confirm that Cognee supplies no canonical
  persistence, authorization, activation, or transaction guarantees.
- Completed Issue #13 requires source-revision-scoped stable locators but leaves
  their domain schemas to later ingestion work.
- Completed Issue #14, recorded in ADR 0018, fixes canonical revision,
  tombstone, publication, activation, rollback, and `as_of` semantics.
- Issue #8 has no blocking issues and directly blocks Issues #4, #5, #6, and #7.
  It has no comments or child issues.

## Solution

Introduce a deep persistence module behind one application-facing async Unit of
Work interface. A production SQLAlchemy 2 adapter uses Psycopg 3 and PostgreSQL;
an in-memory adapter supports application contract tests. Callers receive
purpose-specific repositories and explicit commit/rollback behavior without
learning SQLAlchemy sessions, mapped rows, connection-pool state, or database
exceptions.

Persist canonical Workspace and Environment identities first. Every
tenant-owned table carries a `workspace_id`; every environment-specific table
also carries an `environment_id` with a composite foreign key that proves the
environment belongs to that workspace. Trusted server-side components create a
validated persistence context. The database adapter binds that context to each
transaction using transaction-local PostgreSQL settings before any repository
statement executes.

Use RLS `USING` and `WITH CHECK` policies as defense in depth. Runtime
connections use a restricted non-owner role with `NOBYPASSRLS`, while a separate
migration owner applies Alembic revisions. Applicable tenant tables force RLS.
The normal runtime role cannot bypass or disable the policies.

Mutating operations may claim a transactional idempotency receipt. The receipt,
the canonical mutation, and all resulting outbox intents commit in one database
transaction. A repeated equivalent command returns the existing logical result;
a repeated key with changed semantic content fails with a typed conflict.

Alembic owns forward schema evolution. Database and role provisioning is an
explicit deployment prerequisite; application startup checks connectivity and
schema compatibility but never creates tables or applies migrations. Real
PostgreSQL integration tests prove the guarantees that fakes and SQLite cannot.

## User Stories

1. As an application developer, I want one framework-independent Unit of Work
   interface, so that use cases do not depend on SQLAlchemy.
2. As an application developer, I want purpose-specific repositories, so that I
   do not have to assemble domain behavior from generic CRUD operations.
3. As an application developer, I want commit to be explicit, so that a use case
   visibly owns its transaction outcome.
4. As an application developer, I want uncommitted Units of Work to roll back,
   so that exceptions cannot leave partial canonical state.
5. As an application developer, I want database errors translated into stable
   application categories, so that infrastructure details do not leak inward.
6. As an application developer, I want each operation to receive its own
   session, so that concurrent tasks cannot corrupt shared session state.
7. As a security engineer, I want every tenant-owned row scoped to a Workspace,
   so that accidental unscoped access fails closed.
8. As a security engineer, I want environment-specific rows tied to an
   Environment belonging to the same Workspace, so that mismatched tenant keys
   cannot be persisted.
9. As a security engineer, I want tenant context derived by trusted server-side
   components, so that caller-controlled identity fields cannot select database
   authority.
10. As a security engineer, I want RLS on tenant tables, so that a missing
    application filter does not become cross-workspace disclosure.
11. As a security engineer, I want RLS to remain distinct from Access Policy
    authorization, so that tenant isolation cannot be mistaken for permission to
    read protected document content.
12. As a security engineer, I want the runtime database role unable to bypass
    RLS, so that ordinary application credentials cannot silently widen access.
13. As a security engineer, I want pooled connections to lose all transaction
    tenant state before reuse, so that one request cannot inherit another
    request's Workspace or Environment.
14. As a background-worker developer, I want the same trusted persistence
    context rules as HTTP operations, so that asynchronous work does not become
    an authorization bypass.
15. As an operator, I want database connections created at startup rather than
    import time, so that configuration and startup failures are explicit.
16. As an operator, I want bounded connection pools and deterministic cleanup,
    so that application shutdown and database pressure are manageable.
17. As an operator, I want startup to detect an incompatible schema, so that an
    application version cannot run against an unknown database layout.
18. As an operator, I want migrations applied explicitly with separate
    credentials, so that the runtime cannot mutate its own schema.
19. As an operator, I want an empty database to bootstrap reproducibly, so that
    local, CI, and deployment environments start from the same schema history.
20. As an operator, I want committed migration revisions to upgrade to head, so
    that supported deployments have a verified forward path.
21. As an operator, I want failed transactional migrations to leave the prior
    schema authoritative, so that recovery does not depend on guessing which DDL
    statements succeeded.
22. As a command handler, I want a retry with the same idempotency key and
    request content to return one logical result, so that transport retries do
    not duplicate effects.
23. As a command handler, I want changed content under an existing key rejected,
    so that an idempotency key cannot ambiguously identify two operations.
24. As a command handler, I want concurrent duplicate submissions serialized by
    database constraints, so that race conditions cannot create duplicate
    effects.
25. As an authorization developer, I want replayed result references rechecked
    under current authorization, so that idempotency does not preserve revoked
    disclosure.
26. As a worker developer, I want canonical changes and outbox intents committed
    atomically, so that committed work cannot lose its downstream intent.
27. As a future outbox consumer developer, I want stable event identities and
    schema versions, so that consumption can be made idempotent without parsing
    implementation-specific payloads.
28. As a privacy engineer, I want outbox payloads minimized, so that protected
    content and credentials are not copied into an unnecessary durable channel.
29. As a workflow developer, I want the outbox limited to event intent, so that
    it does not compete with Temporal for durable orchestration.
30. As a test author, I want an in-memory Unit of Work adapter, so that
    application policy tests remain fast and deterministic.
31. As a test author, I want the PostgreSQL adapter exercised through the same
    repository contracts, so that fake and production behavior cannot drift
    unnoticed.
32. As a test author, I want real PostgreSQL for RLS, migrations, locking,
    constraints, pooling, and concurrency, so that database guarantees are
    actually proven.
33. As a maintainer, I want database-dependent tests to fail clearly when their
    prerequisite is unavailable, so that a skipped database suite cannot be
    reported as a successful release gate.
34. As a maintainer, I want named constraints and stable error translation, so
    that schema evolution does not make application failures depend on raw
    provider messages.
35. As a maintainer, I want later R1 repositories to reuse this kernel, so that
    source, policy, projection, audit, and object-storage work share transaction
    and tenancy semantics.

## Implementation Decisions

### Scope and Responsibility

The persistence kernel owns:

- direct dependency and configuration contracts for SQLAlchemy 2, Alembic,
  Psycopg 3, and the PostgreSQL test harness;
- process-owned engine creation, bounded connection pooling, readiness checks,
  and shutdown disposal;
- operation-owned async sessions and transaction-scoped Units of Work;
- framework-independent Workspace, Environment, persistence-context,
  idempotency-receipt, and outbox-intent contracts;
- SQLAlchemy mappings for the kernel-owned records;
- tenant-context binding, RLS policies, database grants, and infrastructure error
  translation;
- Alembic metadata, naming conventions, schema revision checks, and initial
  migrations;
- deterministic request digests and bounded retry policy for eligible database
  transactions.

The persistence kernel does not own authentication, Access Policy decisions,
document lifecycle, object bytes, projection content, workflow state, event
delivery, audit processing, HTTP request schemas, or UI behavior.

### Dependency and Configuration Contract

Required behavior:

- SQLAlchemy, Alembic, and Psycopg 3 are direct runtime dependencies. The
  PostgreSQL Testcontainers integration is a direct development dependency.
- The lock file pins the resolved versions. A transitive Cognee dependency is
  never treated as the supported Spine persistence dependency.
- Database settings are typed, reject unknown persistence-specific fields, and
  are immutable after construction.
- Runtime configuration includes a secret database URL, bounded pool size,
  bounded overflow, checkout timeout, connection timeout, pre-ping behavior, and
  transaction retry limit. Logs and validation errors redact credentials.
- Migration credentials are not available through ordinary runtime settings.
  Deployment supplies them only to the explicit migration command.
- Test configuration accepts either an explicit PostgreSQL URL or permission to
  start the pinned Testcontainers image.
- Imports construct neither engines nor connections. Startup constructs the
  engine after configuration validation. Shutdown awaits engine disposal.

Recommended initial implementation:

- Use one SQLAlchemy async engine per application process and one async session
  per Unit of Work.
- Use SQLAlchemy's async-compatible bounded queue pool with pre-ping and
  rollback-on-return enabled.
- Keep pool capacities configurable rather than encoding deployment sizing in
  domain or application code.

`AsyncSession` is not concurrency-safe and must not be shared between tasks.
Parallel work creates independent Units of Work even when the operations share a
trace or higher-level workflow.

### Canonical Workspace and Environment Ownership

`Workspace` is the tenant-isolation root. `Environment` is a canonical identity
inside one Workspace and carries an environment kind. The database representation
uses PostgreSQL UUID columns and enforces:

- a primary key for each Workspace;
- a primary key for each Environment;
- a foreign key from Environment to Workspace;
- a unique pair of Workspace and Environment identifiers suitable as a composite
  foreign-key target;
- a composite foreign key from every environment-specific kernel record to the
  owning Workspace and Environment pair.

Every tenant-owned record includes `workspace_id`. Records whose meaning differs
between development, staging, and production also include `environment_id`.
Later migrations must classify each new tenant table explicitly as
workspace-only or environment-scoped; omission is a review failure.

Primary identities remain stable UUIDs. Timestamps use PostgreSQL
timezone-aware timestamps and UTC semantics. `created_at` is database generated.
Immutable records do not acquire a misleading mutable `updated_at`. Mutable
control records may add an update timestamp and optimistic version only when
their owning specification defines the transition semantics.

Environment naming, lifecycle, and whether one Workspace may have more than one
Environment of the same kind remain domain decisions for their consuming
specification. The kernel enforces ownership and identity without prematurely
freezing those policies.

### Trusted Persistence Context

The application-facing persistence context is immutable and contains one
explicit scope:

- `WorkspaceScope`, containing exactly one Workspace identity; or
- `EnvironmentScope`, containing one Workspace identity and one Environment
  identity that belongs to it.

The context also contains, when applicable:

- acting-subject identity;
- service-principal identity;
- server-selected purpose and operation;
- trace identity.

The kernel accepts this context only from trusted server-side composition roots,
workers, or tests. It does not parse HTTP headers, tokens, request bodies, or
provider claims. Issue #5 owns authentication and construction of the trusted
request context.

Before opening a usable Unit of Work, the factory validates required identifiers
and verifies that the Environment belongs to the Workspace. Missing, malformed,
or inconsistent context produces a typed context error without opening a
tenant-capable repository operation. Error text must not disclose another
Workspace or Environment.

The database adapter begins a transaction and uses parameterized calls to bind
validated values through transaction-local PostgreSQL settings. No repository
statement may execute before binding succeeds. Read operations also use a
transaction because transaction-local settings are part of the isolation
contract.

The initial setting namespace includes the Workspace and Environment identities.
Acting subject, service principal, purpose, operation, and trace may also be
bound for database-side diagnostics or later policy functions, but their
presence does not implement Access Policy authorization.

### RLS and Database Privileges

The deployment role model has at least:

- an operator/bootstrap identity that creates the database and required roles;
- a migration owner that owns the Spine schema and applies Alembic revisions;
- a restricted runtime role that receives only required DML and sequence
  privileges and has `NOBYPASSRLS`.

Runtime processes never use the bootstrap or migration credentials. Runtime
credentials cannot create or alter schemas, tables, policies, functions, or
roles; cannot change role; and cannot disable RLS. The runtime role is not the
owner of tenant tables.

All applicable tenant tables enable and force RLS. Workspace-only policies
compare row `workspace_id` with the transaction-local Workspace identity.
Environment-scoped policies compare both Workspace and Environment identities.
Policies define both read filtering and write `WITH CHECK` behavior. Missing
settings resolve to no authorized tenant row; malformed settings are rejected by
the trusted context binder before repository access.

RLS is defense in depth for tenant and environment isolation only. It does not
decide `read_content`, `process_content`, administrator authority, Security
Domain membership, or any other ADR 0011 Access Policy rule.

Transaction-local custom settings do not defend against arbitrary SQL execution
under the same role. Spine therefore also requires parameterized repository
queries, restricted runtime privileges, no caller-supplied SQL, explicit
transaction ownership, and trusted construction of persistence context.

Ordinary background workers reconstruct a trusted context from their durable,
validated command or event metadata and use the restricted runtime role. This
specification grants no cross-tenant maintenance role and no outbox-scanning
role. A later dispatcher specification may add a narrowly privileged role for
outbox-only access without granting access to canonical protected content.

### Unit of Work Interface and Transaction Ownership

The Unit of Work seam is application-facing and async. Its interface consists of
an async context lifecycle, purpose-specific repositories, explicit `commit`,
and explicit `rollback`. The factory requires a validated persistence context.

The public contract has the following operation shapes. Names may receive minor
language-level adjustments during implementation, but their inputs, outcomes,
ownership, and error behavior are requirements:

| Interface | Operation | Required result and behavior |
| --- | --- | --- |
| Persistence context | Construct a Workspace-scoped or Environment-scoped immutable context | Returns a validated context or an invalid-context error. Caller-controlled identity and authorization fields are not accepted by this constructor. |
| Unit of Work factory | Open with one validated persistence context | Returns a new async context manager owning one session and transaction. It raises invalid-context before repository access and unavailable/pool errors when acquisition fails. |
| Unit of Work | Enter, access kernel repositories, commit, roll back, exit | Enter binds the tenant scope. Commit returns no domain value and completes exactly once. Rollback is idempotent before close. Exit rolls back unless commit succeeded and always closes the session. Re-entry, post-close use, and concurrent use fail deterministically. |
| Workspace repository | Add a Workspace; resolve a Workspace by identity | Add accepts one domain Workspace whose identity equals the `WorkspaceScope`; resolve returns that Workspace or `None` without revealing another tenant. Duplicate identity or another known constraint produces a translated constraint conflict. |
| Environment repository | Add an Environment; resolve an Environment by identity within the current Workspace | Creation uses `WorkspaceScope` and requires the Environment's `workspace_id` to match. Resolution is Workspace-scoped and returns the Environment or `None`. Environment-scoped data repositories introduced later require `EnvironmentScope`. |
| Idempotency repository | Claim an operation name/schema version, caller key, digest algorithm version, and request digest in the current scope | Returns an owned claim with receipt identity when newly inserted, or a replay outcome containing the committed opaque result reference when key and digest match. A key with a different digest raises idempotency conflict. |
| Idempotency repository | Complete an owned claim with one typed opaque result reference | Records the result reference once in the current transaction. Completing a replayed, unknown, already completed, or differently scoped claim raises a translated conflict. Commit is prohibited while a newly owned claim is incomplete. |
| Outbox writer | Append one typed, versioned outbox intent in the current scope | Validates that intent scope equals Unit of Work scope, records the immutable intent, and returns its event identity. Duplicate event or producer deduplication identity returns the defined constraint/idempotency conflict; it never dispatches. |

An opaque result reference contains a stable result type, result identity, and
result schema version. It contains no protected result snapshot. Claim outcomes
are a discriminated union, so application code must handle ownership and replay
explicitly rather than infer them from nullable fields.

Kernel-owned repository interfaces are limited to behavior needed for:

- creating and resolving canonical Workspace records;
- creating and resolving canonical Environment records within a Workspace;
- claiming, completing, and replaying idempotency receipts;
- appending transactional outbox intents.

Later specifications add their own purpose-specific repositories through this
same Unit of Work seam. They do not receive a generic CRUD base repository.

Required transaction semantics:

- Entering a Unit of Work creates a new async session and begins one database
  transaction.
- Tenant context is bound before repositories are usable.
- Repositories may add, query, lock, and flush, but never commit or roll back.
- A successful explicit commit is the only normal completion path.
- Exiting after an exception or without a successful commit rolls back.
- Commit failure triggers rollback where the driver still permits it, translates
  the error, and closes the session.
- Exiting always closes the operation-owned session.
- A Unit of Work cannot be re-entered, shared between concurrent tasks, or nested
  implicitly.
- Transaction propagation is explicit: collaborating application logic receives
  the current Unit of Work or a repository from it. Ambient global sessions and
  hidden context-variable sessions are not part of the interface.
- Savepoints are an adapter-internal technique only when a concrete use case
  proves their need. They are not exposed in the R1 kernel interface.

Default transaction isolation is PostgreSQL `READ COMMITTED`. Operations that
need stronger serialization use named constraints, row locks, compare-and-swap
predicates, or an explicitly stronger transaction isolation level as defined by
their owning specification. The whole application does not pay the retry cost of
blanket serializable isolation.

### Mapping and Repository Conventions

Domain and application contracts use domain types and Pydantic contracts, not
SQLAlchemy declarative bases, mapped rows, sessions, expressions, or database
exceptions. SQLAlchemy mappings and row-to-domain conversion live entirely in
the infrastructure adapter.

All foreign keys, unique constraints, check constraints, and indexes have stable
names. Correctness invariants use database constraints rather than relying only
on pre-query checks. Indexes begin with demonstrated R1 lookup, uniqueness,
foreign-key, RLS, or concurrency needs; the kernel does not add speculative
indexes for later platform entities.

Repository queries include tenant predicates even where RLS would produce the
same filtering. RLS is a second enforcement layer, not a replacement for clear
repository intent.

Immutable and versioned persistence follows these conventions:

- an immutable record is inserted once and is neither updated nor hard-deleted
  through its ordinary repository interface;
- a correction or successor is another record linked to its predecessor when
  the owning domain requires lineage;
- a version identity is explicit and is never inferred from a timestamp, row
  order, or mutable natural key;
- an append-only decision remains distinct from any mutable current pointer or
  derived status;
- a mutable pointer or control row changes only through an explicit expected
  version/predecessor compare-and-swap condition;
- database constraints protect immutable identity and successor uniqueness, while
  later domain specifications define the exact lineage and transition rules.

### Error Translation and Failure Behavior

The SQLAlchemy adapter translates infrastructure failures at the Unit of Work
seam into stable application-facing categories:

- invalid or inconsistent persistence context;
- unavailable database or exhausted connection pool;
- retryable deadlock or serialization conflict;
- optimistic or compare-and-swap conflict;
- idempotency-key content conflict;
- known constraint violation;
- incompatible database schema;
- unexpected persistence failure.

Only deadlock and serialization categories are eligible for automatic database
transaction retry. Known constraint names map to stable conflict meanings.
Unknown provider messages, SQL text, bound values, connection strings, stack
traces, and protected data remain internal and are not suitable as user-facing
messages or outbox payloads. Issue #4 will map these categories into the final
versioned structured-error and audit contracts.

After any statement failure, callers must not continue using the failed Unit of
Work. The adapter rolls it back and closes it. A connection-invalidating failure
causes SQLAlchemy to discard that pooled connection.

### Deterministic Idempotency Receipts

The kernel owns a transactional idempotency receipt with:

- Workspace identity and optional Environment identity;
- stable operation name and operation schema version;
- caller-supplied idempotency key;
- deterministic canonical request digest and digest algorithm version;
- stable result type and opaque result identity;
- database-generated creation time.

Workspace-level and environment-level receipts use separate unique constraints
so that a nullable Environment cannot weaken uniqueness. The unique scope is the
Workspace, optional Environment, operation, and caller key.

The canonical request digest is SHA-256 over an explicitly versioned canonical
byte representation containing the operation name, operation schema version,
and validated semantic command payload. The canonicalizer must:

- serialize only a validated versioned command contract;
- encode UUIDs in lowercase canonical form and enums by their declared values;
- encode timestamps in UTC with one documented ISO 8601 precision;
- encode decimal values as normalized decimal strings, never binary floats;
- reject NaN, infinity, sets, non-string object keys, and unsupported values;
- sort object keys, preserve list order, use UTF-8, and emit no insignificant
  whitespace;
- represent binary inputs by immutable digest/reference metadata rather than
  embedding bytes;
- exclude transport-only fields that do not alter the logical command, while
  keeping Workspace and Environment in the separately persisted uniqueness
  scope.

The canonicalizer has committed cross-version test vectors. Changing its output
requires a new digest algorithm version rather than silently changing old key
meaning.

Atomic command behavior is:

1. Begin the tenant-scoped Unit of Work.
2. Attempt to insert the receipt identity and request digest under its unique
   constraint.
3. If this transaction owns the new receipt, perform the canonical mutation,
   append required outbox intents, store the stable result reference, and commit
   all records together.
4. If a concurrent or previous transaction owns the key, wait for PostgreSQL to
   resolve the uniqueness race, then read the committed receipt.
5. Return the stable result reference when the digest matches; raise the typed
   idempotency conflict when it differs.
6. If the owning transaction fails, its receipt and all logical effects roll
   back, allowing a later retry to become the owner.

Business tables still carry their own natural or domain uniqueness constraints.
An idempotency receipt does not substitute for those invariants.

Replaying a receipt returns an opaque result reference, not a protected response
snapshot. The consuming use case must resolve that reference and repeat all
current authorization, revision, tombstone, retention, and disclosure checks.
A receipt never preserves authority that has since been revoked.

### Bounded Transaction Retries

The initial R1 retry limit is configurable with a default maximum of three total
attempts. The retry wrapper reruns the complete Unit of Work, never a single
failed statement. It may retry only when:

- the translated failure is a PostgreSQL deadlock or serialization failure;
- the operation is declared idempotent and carries a valid idempotency key when
  it can create a logical effect;
- all work inside the transaction is reproducible from the same validated input;
- no irreversible filesystem, object-storage, network, model, message-delivery,
  or other external side effect occurred inside the transaction.

Constraint violations, invalid tenant context, authorization failures,
idempotency digest conflicts, and unknown persistence failures are not retried.
Backoff remains bounded and may include jitter. Retry exhaustion returns the
translated conflict; it does not create a durable retry loop. Composite retry
exhaustion and operator retry remain Temporal/workflow concerns under ADR 0010
and Issue #11.

### Transactional Outbox Foundation

ADR 0004 fixes the architectural authority and atomicity decision. This section
defines only the Issue #8 persistence contract needed to implement that accepted
decision.

The kernel defines an immutable, versioned outbox intent containing:

- opaque globally unique event identity;
- Workspace identity and optional Environment identity;
- stable event type and event schema version;
- optional aggregate type, aggregate identity, and aggregate version;
- minimal versioned JSON payload;
- stable producer-side deduplication identity where the producer can define one;
- database-generated `created_at` record time;
- trace, correlation, and causation references when available.

`created_at` records when PostgreSQL inserted the intent. It is not a domain
event occurrence time and does not prove commit order. A producer-supplied
business occurrence timestamp, when meaningful, belongs to that event type's
later payload contract.

An application use case appends the outbox intent through the current Unit of
Work. The intent and canonical mutation either commit together or both roll
back. Repository code cannot publish, dispatch, or acknowledge an event before
commit.

Event payloads prefer opaque identifiers and safe state-transition metadata.
Protected document content, questions, excerpts, credentials, secrets, raw
provider payloads, and unrestricted error details are excluded unless a later
specification demonstrates that the content is required and defines its access,
encryption, retention, and redaction rules.

Every event type has a typed, versioned producer-owned payload contract. The
kernel validates the envelope and requires the typed payload but does not attempt
heuristic secret or content classification inside arbitrary JSON. The producing
specification is responsible for proving payload minimization with schema tests.

Stable event identity, type, version, and deduplication identity are the only
idempotent-consumption prerequisites defined here. A future consumer must record
its consumption identity atomically with its own logical effect. This
specification does not create an inbox or consumer receipt because no concrete
consumer contract yet owns one.

Outbox claiming, dispatcher roles, locking, delivery state, scheduling, retry
cadence, telemetry, cleanup, consumer execution, audit processing, and Temporal
signaling belong to Issue #4 and later consuming specifications. Outbox rows are
not workflow state and do not model waits, approvals, cancellation, branches, or
retry exhaustion.

### Migration and Bootstrap Architecture

The database uses a dedicated, explicitly qualified Spine schema. Runtime and
migration connections use controlled search paths that include only required
system namespaces and the Spine schema; migrations and SQL do not rely on a
mutable ambient search path for object identity. The Alembic version table is in
the Spine schema.

Deployment has two explicit phases:

1. Operator bootstrap creates the database, bootstrap/migration/runtime roles,
   ownership relationships, and secrets outside application startup.
2. The migration owner runs Alembic to create or upgrade the Spine schema,
   tables, constraints, indexes, grants, and RLS policies.

Role creation is not hidden inside ordinary schema migrations because it needs
cluster-level privileges and deployment-specific secret management. The
migration preflight fails clearly when required roles are absent.

Migration conventions are:

- one linear Alembic head for the R1 modular monolith;
- descriptive revision filenames containing the revision identity and a concise
  purpose;
- explicit `down_revision` ordering and intentional merge revisions only when
  parallel approved work genuinely creates heads;
- stable naming conventions for primary keys, foreign keys, unique constraints,
  checks, and indexes;
- schema-qualified tables, sequences, policies, functions, and grants;
- PostgreSQL-transactional DDL by default;
- an explicit recovery procedure for any future migration that cannot run in a
  transaction;
- data backfills separated into bounded, observable steps when their size makes
  one DDL transaction unsafe;
- no production use of ORM metadata `create_all`.

Supported verification paths are:

- an empty provisioned PostgreSQL database to the current Alembic head;
- every committed Spine migration revision retained as a supported starting
  point to the current head;
- application startup against the expected schema revision;
- application startup rejection against an empty, older, newer, unknown, or
  multiple-head schema state.

R1 does not promise rolling zero-downtime compatibility between old and new
application versions. Deployment coordinates migration and application version.
Migrations are forward-first. Reversible migrations should implement downgrade
when it is truthful and tested, but operational rollback is not guaranteed by
universal downgrade scripts. Backup restoration or a corrective forward
migration is the supported recovery path for irreversible changes.

### Application Startup and Readiness

Application startup:

- validates runtime database configuration;
- creates the process-owned async engine and session factory;
- checks connectivity using the runtime role;
- checks that the database has exactly the supported Alembic head;
- verifies that essential runtime privileges and tenant-context binding work;
- marks readiness failed and disposes the engine when any check fails.

Startup never creates schemas, creates tables, grants roles, changes policies,
or invokes Alembic upgrade. Liveness does not imply database readiness. Shutdown
stops accepting new operations, lets the hosting process apply its bounded
graceful-shutdown policy, rolls back abandoned sessions, and awaits engine
disposal.

### Integration Boundaries

Issue #4 consumes stable persistence error categories, trace/correlation fields,
the transactional outbox writer, and database-generated event identity. It owns
structured external errors, append-only audit-event schemas, dispatcher
behavior, delivery retries, telemetry, and audit processing.

Issue #5 constructs the trusted request context from provider-neutral
authentication and server-owned authorization state. It maps that context into
the persistence context. This kernel neither validates OIDC nor accepts caller
claims as database context.

Issue #6 stores immutable original bytes behind an object-storage port and
persists only governed receipts/references through later repositories. There is
no distributed transaction between PostgreSQL and object storage. Issue #6 must
define staging, finalize, retry, and orphan cleanup around this kernel's
transaction and outbox behavior.

Issue #7 adds the R1 `SourceObject`, immutable `SourceRevision`, Access Policy,
ingestion, ontology/configuration, projection, receipt, reference, and activation
records. Those records use this Unit of Work, tenant keys, RLS conventions,
idempotency receipts, migrations, compare-and-swap techniques, and outbox
writer. This kernel does not predefine their schemas.

Temporal integration consumes committed commands and event intents in later
specifications. Temporal remains the sole durable workflow engine. Cognee and
its relational/vector/graph stores do not import, share, or become authoritative
over this canonical database kernel.

## Testing Decisions

### Test Philosophy and Levels

Tests assert observable behavior through the Unit of Work and repository
interfaces. They do not assert SQLAlchemy call order, mapped-row internals,
private session methods, or exact raw PostgreSQL messages.

The primary contract suite runs against both the in-memory adapter and the
PostgreSQL adapter for behavior the adapters share. The in-memory adapter exists
to test application policy, rejection, and transaction outcomes; it is not
claimed to emulate PostgreSQL locks, isolation, RLS, migrations, or pool state.

Existing prior art is limited. The architecture-scaffold test that rejects
framework imports from the domain layer remains the model for boundary
assertions. The Issue #11 durability prototype supplies failure scenarios for
atomic canonical writes and outbox recovery, but its in-memory model is decision
evidence rather than a production contract suite. The repository has no existing
PostgreSQL, RLS, migration, repository, or Unit of Work tests to copy; this
specification establishes their first shared public seam.

Test levels are reported separately:

- unit tests cover canonical digest serialization, context validation, error
  classification, and pure retry eligibility;
- application contract tests execute use cases through the in-memory Unit of
  Work adapter;
- repository/UoW contract tests run the shared observable behavior suite against
  both adapters;
- PostgreSQL integration tests cover transactions, constraints, concurrency,
  migrations, RLS, roles, connection pooling, and failure recovery.

Testcontainers with a pinned PostgreSQL image is the preferred local and CI
harness. An explicit test PostgreSQL URL may replace it. The configured database
must be disposable and must never be a developer or production database.
PostgreSQL tests fail with a clear prerequisite error when neither a working
container runtime nor the explicit URL is available; they are not silently
skipped or reported as passed.

The full repository gate includes the mandatory PostgreSQL suite. A narrower
unit-only command may exist for development feedback, but its output must state
that PostgreSQL acceptance has not run.

### Executable Acceptance Criteria

1. **Declared stack:** dependency inspection proves SQLAlchemy, Alembic, and
   Psycopg are direct runtime dependencies and Testcontainers is a direct
   development dependency. The locked environment installs on every supported
   Python version used by CI.
2. **Import safety:** importing domain, application, configuration, and
   persistence contract modules opens no socket and creates no engine or
   connection.
3. **Framework independence:** the architecture test proves the domain layer
   imports no SQLAlchemy, Alembic, Psycopg, FastAPI, Cognee, or Temporal types.
   Application persistence contracts expose none of those types.
4. **Engine lifecycle:** startup creates one engine per process, readiness fails
   on unreachable or incompatible databases, and shutdown disposes all pooled
   connections. Startup never changes the Alembic revision.
5. **Session ownership:** two simultaneous Units of Work receive distinct async
   sessions and transactions. Reusing or concurrently entering one Unit of Work
   fails deterministically without executing repository work.
6. **Commit:** a canonical kernel record and its outbox intent become visible
   together only after explicit commit.
7. **Implicit rollback:** leaving a Unit of Work without commit persists neither
   the mutation, idempotency receipt, nor outbox intent.
8. **Failure rollback:** injected failures after the canonical write, after the
   receipt, after the outbox insert, during flush, and before commit leave no
   partial logical effect.
9. **Commit uncertainty recovery:** after simulating a committed transaction
   whose response is lost, retrying the same key and digest returns the one
   committed result and produces no second effect or event intent.
10. **Repository contract parity:** the shared fake/PostgreSQL contract suite
    proves add, resolve, commit, rollback, duplicate replay, and typed-conflict
    behavior through public interfaces.
11. **Workspace isolation:** under the runtime role, a context for Workspace A
    cannot read, update, delete, infer existence from returned values, or insert
    rows owned by Workspace B.
12. **Environment isolation:** two Environments in one Workspace cannot read or
    mutate each other's environment-scoped rows. A Workspace/Environment mismatch
    is rejected by context validation, composite foreign keys, and RLS write
    checks.
13. **Missing context:** absent Workspace or required Environment settings yield
    no tenant rows and reject writes. Errors contain no protected identifiers or
    values.
14. **Invalid context:** malformed identifiers, an Environment from another
    Workspace, and caller-controlled attempts to override trusted context fail
    before repository behavior can succeed.
15. **RLS policy shape:** catalog assertions prove applicable tables have RLS
    enabled and forced and have both read and write checks for their declared
    scope.
16. **Privilege behavior:** the runtime role is a non-owner with `NOBYPASSRLS`,
    cannot alter schema or policy, and cannot assume the migration role. The
    migration owner can migrate but is never used by runtime tests.
17. **Pool leakage:** with a pool constrained to one physical connection, a
    committed, rolled-back, failed, and cancelled Workspace A transaction is
    followed by Workspace B and missing-context operations. None observes
    Workspace A settings or rows.
18. **Worker parity:** a synthetic background operation using a validated trusted
    context receives the same workspace/environment isolation as an interactive
    operation.
19. **Sequential idempotency:** repeating a key with the same canonical command
    returns the same result reference and creates one receipt, logical effect,
    and outbox intent.
20. **Digest conflict:** repeating a key with changed semantic input produces the
    typed idempotency conflict and does not mutate the original receipt or
    logical result.
21. **Canonical digest vectors:** fixed examples cover key ordering, Unicode,
    UUIDs, enums, UTC timestamps, decimals, lists, rejected floats, and algorithm
    version changes.
22. **Concurrent duplicates:** independently connected transactions submit the
    same key concurrently. Exactly one owns the mutation; all successful callers
    observe the same committed result reference and only one event intent exists.
23. **Independent keys:** equal payloads under different keys and different
    environment scopes follow their own domain rules rather than being merged by
    content digest alone.
24. **Replay authorization:** a fake current-authorization check denies a
    previously committed result reference after authority is revoked. The
    receipt does not return protected cached output.
25. **Bounded retry:** injected deadlock and serialization failures retry the
    entire eligible Unit of Work no more than the configured three-attempt
    default. A later success creates one effect.
26. **Retry rejection:** constraint, tenant-context, idempotency-conflict,
    authorization, and unknown failures are attempted once. Operations that
    declare irreversible external work are rejected from automatic transaction
    retry.
27. **Outbox atomicity:** commit persists both mutation and immutable event
    intent; rollback or injected commit failure persists neither.
28. **Outbox minimization:** kernel-owned envelope fields cannot carry arbitrary
    content; each test event uses a typed versioned payload schema whose contract
    rejects undeclared fields. Fixtures prove the payload contains only the
    identifiers and safe transition metadata required by that event.
29. **Migration bootstrap:** a newly provisioned real PostgreSQL database and
    roles upgrade from empty to one Alembic head, after which runtime readiness
    succeeds.
30. **Migration upgrades:** every retained committed migration revision upgrades
    to head against real PostgreSQL and leaves expected constraints, indexes,
    grants, RLS policies, and revision identity.
31. **Migration failure:** an injected transactional migration failure leaves
    the previous revision and schema usable. Non-transactional migration support
    cannot be added without an explicit recovery test.
32. **Schema compatibility:** runtime readiness rejects empty, older, newer,
    unknown, and multiple-head database revision states without attempting an
    upgrade.
33. **Constraint translation:** named foreign-key, uniqueness, check, and
    compare-and-swap failures map to stable application categories without raw
    SQL, values, or provider messages.
34. **Real-database gate:** disabling container access without configuring the
    explicit PostgreSQL URL makes the mandatory integration gate fail clearly.
    The test report cannot count the PostgreSQL suite as passed or silently
    skipped.
35. **Repository gates:** the full test suite, compileall check, domain framework
    import check, and diff check pass with the declared development environment.

## Out of Scope

- `SourceObject`, `SourceRevision`, tombstone, admission, canonical acceptance,
  and source-current schemas.
- Full Access Policy, principal, membership, grant, proposal, approval,
  bootstrap, recovery, or authorization decision logic.
- Projection configuration, snapshot, activation, receipt, drift, rollback, and
  source-to-projection schemas.
- Document parsing, stable locator implementations, ingestion pipelines, and
  connector cursors.
- Object-storage adapters, immutable original writes, backup/restore of object
  bytes, and retention cleanup.
- Cognee adapters, graph/vector schemas, Context Broker, `ContextBundle`, and
  retrieval authorization.
- Grounded Q&A, citations, answer validation, Agent Runtime, and model calls.
- Temporal workflow definitions, Activities, signals, updates, cancellation,
  long waits, and composite retry orchestration.
- Outbox claiming, dispatch, delivery retries, worker scheduling, consumer
  implementation, event telemetry, audit-event schemas, and audit processing.
- HTTP authentication, OIDC, final authorization context construction, REST API,
  SSE, and frontend behavior.
- Production high availability, autoscaling, cloud provisioning, secret-manager
  selection, database proxy selection, replicas, sharding, and microservices.
- A universal persistence repository, ORM entity base with business behavior,
  generic event bus, or second durable workflow engine.
- Production zero-downtime rolling application/schema upgrades.
- Fixed retention durations, backup schedules, or disaster-recovery objectives;
  those require deployment and compliance input.

## Further Notes

### Required Dependencies and Assumptions

- PostgreSQL is provisioned before application startup and supports the RLS,
  transactional DDL, advisory/row-lock, UUID, JSON, and timezone-aware timestamp
  behavior exercised by the integration suite.
- Deployment provides separate bootstrap/migration and runtime credentials.
- The exact supported PostgreSQL major version and Testcontainers image must be
  pinned together by the implementation ticket and CI configuration. Selecting
  that supported major does not change the contracts in this specification.
- Issue #5 will provide trusted identity and authorization context. Until then,
  tests construct that context only through explicit trusted factories.
- Later domain specifications own their tables and migrations while conforming
  to this kernel's tenant, transaction, RLS, idempotency, and naming rules.
- Existing legacy Q&A endpoints, CLI commands, loaders, and Cognee helpers remain
  available until separately tested replacements are delivered.

### Deferred Integration Contracts

- Issue #4 must define dispatcher ownership, outbox claim/lease semantics,
  delivery attempts, audit events, structured external errors, and telemetry.
- Issue #5 must map authenticated OIDC identities and server-selected Workspace,
  Environment, roles, service principal, purpose, and operation into the trusted
  persistence context.
- Issue #6 must define the no-distributed-transaction protocol between immutable
  object storage and PostgreSQL canonical receipts.
- Issue #7 must define document/source/access/projection repositories and their
  specific constraints without weakening this kernel.
- A later Temporal specification must define how committed outbox or command
  identities start or signal durable workflows idempotently.

### Remaining Implementation-Level Choices

The following choices may be made during ticket decomposition or implementation
without reopening this specification, provided their behavior passes the stated
contracts:

- exact compatible dependency lower bounds selected by `uv` for supported
  Python versions;
- exact bounded pool-size, overflow, timeout, and backoff defaults other than the
  approved three-attempt transaction retry default;
- whether SQLAlchemy mappings use declarative classes or imperative mapping
  internally;
- whether the test harness creates one database per test or isolated schemas per
  test, provided role and pool isolation remain real and deterministic;
- the exact names of internal adapter helpers and private mapping modules.

No unresolved architectural or security decision remains for the persistence
kernel. A later requirement that needs cross-tenant runtime access, a different
canonical store, automatic migrations, SQLAlchemy types in application
contracts, or outbox-owned workflow state requires explicit specification and,
where applicable, an ADR amendment.

### Completion Criteria

The implementation produced from this specification is complete only when:

1. The direct dependencies, typed settings, engine lifecycle, Unit of Work,
   purpose-specific kernel repositories, SQLAlchemy adapter, Alembic environment,
   RLS policies, roles/grants, idempotency receipts, and outbox intents are
   implemented.
2. Domain and application contracts contain no SQLAlchemy, Alembic, Psycopg, or
   PostgreSQL-specific types.
3. Every executable acceptance criterion above passes at its declared test level,
   including mandatory real-PostgreSQL tests.
4. Empty bootstrap and all supported migration upgrades pass against the pinned
   PostgreSQL major version.
5. Two-workspace, two-environment, missing-context, invalid-context, privilege,
   and one-connection pool-leakage tests pass under real database roles.
6. Sequential and concurrent idempotency tests prove one logical effect and
   changed-content conflict behavior.
7. Transactional outbox atomicity passes failure injection without implementing
   dispatcher behavior.
8. Domain framework-import protection, the full repository test gate, compileall,
   and diff validation pass.
9. Documentation records the supported PostgreSQL major version, migration
   commands, required role separation, test prerequisites, and exact unverified
   external deployment prerequisites, if any.
10. No production behavior from Issues #4, #5, #6, or #7 is silently implemented
    as part of the kernel.
