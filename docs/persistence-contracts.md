# Persistence contracts

The application persistence seam lives in `spine.application.persistence`. It is
async and framework-independent: callers receive purpose-specific Workspace and
Environment repositories from an operation-owned Unit of Work, then explicitly
commit or roll back. Exiting without a successful commit always rolls back.

## Trusted context boundary

An ordinary Unit of Work accepts only a `TrustedPersistenceContext` issued by a
`TrustedContextAuthority`. The authority is created at a server-side composition
root, worker composition root, or test boundary. HTTP fields, request bodies,
tokens, and provider claims are not persistence contexts and this package offers
no parser that can turn them into one. Their owning authentication boundary must
first authenticate and map them, then select the scope, purpose, and operation
server-side.

`WorkspaceScope` and `EnvironmentScope` are immutable. Environment scope is
validated against canonical Environment ownership before repositories become
usable. Invalid, missing, or inconsistent authority produces a stable typed
error whose message contains no tenant identifiers.

The initial Workspace uses the separately named
`InitialWorkspaceBootstrap` port. Its opaque authority contains no Workspace,
Environment, administrator, or content-access fields. A deployment composition
root supplies the one expected authority directly to the adapter; after one
successful creation the path is sealed. Ordinary Units of Work neither expose
this port nor accept bootstrap authority. This establishes canonical tenancy but
does not grant ADR 0011 `read_content`, `process_content`, administrator, or
cross-tenant authority.

## Transaction and lifecycle rules

Each call to `UnitOfWorkFactory` returns a new single-owner Unit of Work. It may
be entered once and used only by the task that entered it. Re-entry, concurrent
use, and use after commit, rollback, failure, or close raise
`UnitOfWorkLifecycleError`. Rollback is idempotent until context exit. Repository
objects never commit or roll back independently.

The in-memory adapter stages deep copies per Unit of Work and publishes them
atomically only on explicit commit. It is intended for deterministic application
and shared adapter-contract tests; it does not emulate PostgreSQL RLS, locks,
isolation levels, pooling, or provider failures.

## Extending the Unit of Work

Later vertical slices add a repository protocol named for their business
purpose, then expose that protocol as an explicit Unit of Work property. They
must classify records as Workspace-only or Environment-scoped, accept and return
domain or versioned application types, enforce the current scope, and join the
same transaction. An adapter implementation is added to the reusable contract
suite before it is consumed by an application use case.

Do not add a generic CRUD/base repository, ambient session, hidden commit,
provider exception, SQL expression, or infrastructure model to this seam.
Repository methods should describe domain intent and provide only the operations
needed by the consuming roadmap slice.
