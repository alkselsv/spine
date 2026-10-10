# Persistence contracts

The application persistence seam lives in `spine.application.persistence`. It is
async and framework-independent: callers receive purpose-specific Workspace and
Environment repositories from an operation-owned Unit of Work, then explicitly
commit or roll back. Exiting without a successful commit always rolls back.

## Trusted context boundary

`TrustedPersistenceContext` is immutable context data, not authority by itself.
The application package exposes no unrestricted authority issuer. A trusted
server or worker composition root creates one infrastructure
`TrustedContextBoundary`, keeps it away from request parsing and
caller-controlled dependency injection, and supplies that same boundary as the
persistence adapter's verifier.

The boundary binds the scope, origin, acting subject, service principal,
server-selected `PersistencePurpose` and `PersistenceOperation`, trace identity,
and issuer identity into an authenticated proof. Verification reads every field
once into a complete, detached, non-polymorphic snapshot, validates that snapshot,
and calculates and compares the proof over those exact captured values. The
authenticated snapshot is the same object returned to the adapter; the
caller-owned source is never reread afterward.

A Unit of Work verifies this binding both when the factory is called and when the
context is entered. Entry performs its final capture and authentication after
acquiring the adapter's transaction synchronization point, requires it to match
the factory's authenticated binding, and introduces no asynchronous suspension
before retaining it. Repositories use only that snapshot. Contexts constructed
from public fields, mutated after issuance, substituted after factory creation,
or issued by another boundary fail before repositories become usable.
Environment ownership is then checked against canonical state.

This proof protects the application boundary from caller-controlled data and
accidental cross-boundary context reuse. It is not a sandbox or privilege
boundary against arbitrary malicious Python code already executing with full
application-process privileges: such code can inspect process memory and must be
prevented by deployment, dependency, and code-execution controls. Issue #40
defines no time-based context expiry or persisted revocation list. Replacing the
composition-root boundary invalidates contexts issued by the previous boundary;
workers should issue an operation-local context from current validated metadata.
OIDC mapping and Access Policy decisions remain outside this contract.

## Initial Workspace bootstrap

The first Workspace uses the separately named `InitialWorkspaceBootstrap` port.
A trusted composition root creates one opaque bootstrap capability and binds it
to one adapter instance. An uninitialized store rejects ordinary Workspace
creation. One successful bootstrap creates the initial Workspace and permanently
seals that adapter's bootstrap path.

Bootstrap authority is not accepted as a persistence context, is not exposed by
ordinary Units of Work, cannot enumerate or access tenants, and grants no ADR
0011 `read_content`, `process_content`, administrator, or recovery authority.
Later Workspace administration remains owned by its consuming specification.

## Transaction and lifecycle rules

Each call to `UnitOfWorkFactory` returns a new single-owner Unit of Work. It may
be entered once and used only by the task that entered it. Re-entry, concurrent
use, and use after commit, rollback, terminal failure, cancellation, or close
raise `UnitOfWorkLifecycleError`. Rollback is idempotent until context exit.
Repository objects never commit or roll back independently.

Repository persistence failures and cancellation during an awaited transaction
operation clear staged mutations and make the Unit of Work terminal while
preserving the original exception or cancellation. Expected absence remains a
normal `None` result and does not invalidate the transaction.

`UnitOfWork.outbox` accepts only immutable intents built and revalidated through
an explicit `OutboxEventRegistry`. Each registered event type/schema version has
one producer-owned Pydantic payload schema with `extra="forbid"`. The writer
requires those schemas to be frozen and recursively composed only from immutable
field types; mutable containers such as lists and dictionaries are rejected at
registration. The intent's Workspace, optional Environment, and trace identity
must exactly equal the trusted Unit of Work context. It joins the current
transaction and returns the stable event identity; PostgreSQL generates that
identity when the producer omits it. Duplicate event or producer identities
raise `OutboxConflictError`. It never publishes, claims, acknowledges, retries,
or stores workflow state. Registry validation also creates an immutable canonical
JSON snapshot; persistence never reserializes live Pydantic or computed/private
model state.

The in-memory adapter reconstructs fresh canonical Workspace, Environment, and
outbox-intent values at every persistence boundary (staging, snapshots,
publication, and reads) rather than retaining caller-owned mutable data. It
publishes prepared canonical records, receipts, and intents atomically only on
explicit commit. It is intended for deterministic application and shared
adapter-contract tests; it does not emulate PostgreSQL RLS, locks, isolation
levels, pooling, or provider failures.

## Extending the Unit of Work

Later vertical slices add a repository protocol named for their business
purpose, then expose that protocol as an explicit Unit of Work property. They
must classify records as Workspace-only or Environment-scoped, accept and return
domain or versioned application types, enforce the current scope, and join the
same transaction. Adapter-neutral contract tests consume the
`PersistenceAdapter` fixture; future implementations extend its parametrized
factory list without changing the contract test modules.

Do not add a generic CRUD/base repository, ambient session, hidden commit,
provider exception, SQL expression, or infrastructure model to this seam.
Repository methods should describe domain intent and provide only the operations
needed by the consuming roadmap slice.
