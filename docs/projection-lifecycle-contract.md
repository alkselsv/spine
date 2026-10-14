# Context projection lifecycle contract

Issue #201 extends the provider-neutral `ContextProjection` contract without
making a physical graph or vector store authoritative.

Each projection command carries an optional monotonic `revision_number` for
each source object. The deterministic adapter records the highest supplied
revision and fences older revisions. A tombstoned revision remains a fence
until a higher-numbered reappearance is supplied. The adapter accepts the
canonical boundary and statuses from the caller; it does not determine
canonical currentness or publication.

Physical work is exposed as immutable `ProjectionEffect` values. A physical
adapter implements `ProjectionEffectStore.apply`, returning the same effect
with an applied, replayed or failed status plus a typed failure code when
needed. `ProjectionResult.effects` retains
the safe effect statuses and `ProjectionResult.lifecycle` distinguishes normal
success, replay, recovery, recoverable partial failure and revision fencing.

Command identity is scoped to workspace, environment and physical namespace.
Repeating the same identity and digest in that scope replays the stored logical
result; a digest or operation conflict fails without another physical effect.
If one effect fails after
earlier effects were applied, the result is `recoverable` and names only the
pending effect IDs. Retrying the same command resumes pending effects and
never reapplies completed effects.

The contract is deterministic under concurrent invocation of the same command:
the adapter serializes its lifecycle decision and produces at most one logical
physical effect per command/effect identity. The deterministic physical store
in `spine.memory.fake` is an offline qualification implementation with
controlled one-shot or persistent failures; it is not a production store.

## Physical namespace routing

`ProjectionNamespaceRouter` derives an opaque physical namespace from the
trusted workspace, environment, projection kind, candidate/snapshot context,
canonical boundary and pinned projection configuration. Lifecycle state is
tracked separately from the identity digest, so a namespace can move through
`shadow`, `rebuilding`, `active`, `failed` and `retired` without changing its
physical address. A different workspace, candidate, snapshot or configuration
therefore always receives a different namespace digest.

The router validates the namespace carried by a command against that trusted
logical context. The deterministic fake rejects mismatches with the typed
`namespace_unavailable` failure and reports only the namespace digest in safe
diagnostics. Ordinary retrieval accepts only `active` namespaces; shadow,
rebuilding, failed and retired artifacts cannot serve retrieval content.

## Cognee chunk write-side adapter

`spine.memory.cognee.indexing.CogneeChunkProjectionStore` implements the
`ProjectionEffectStore` seam for Cognee 1.5.4. It consumes an immutable
`ParsedDocument` catalog keyed by `source_revision_id`, validates workspace,
environment, source identity and revision digest before writing, and sends each
parsed segment as a Cognee `DataItem`. External metadata pins the source
revision, parsed artifact, stable locator, projection configuration, canonical
boundary and routed physical namespace. The adapter derives the dataset name
from the server-routed namespace and uses the namespace digest as the node set;
callers cannot select an arbitrary Cognee dataset.

Project and rebuild effects use public `add` and `cognify` calls. Update effects
use public `update` for shared physical data IDs, add newly introduced segments,
and soft-delete removed segments before re-cognifying. Remove effects soft-delete
all mapped data IDs and retain a tombstone fence. Replayed effect IDs and
revision fences produce status-only outcomes without another physical write;
provider exceptions are also reduced to a failed effect without returning
provider messages or source content. Durable command/receipt persistence and
publication authority remain outside the adapter.

Rebuild qualification uses a new server-routed shadow namespace, runs the same
offline contract/evidence suite against its fake SDK, and promotes it only via
the PostgreSQL-owned publication workflow. A failed rebuild is retired without
changing the active namespace; rollback is a new successor publication of the
last validated canonical boundary, never a reader switch to stale physical
state. Changes to `chunk-artifact:v1` require a new adapter contract version,
rebuild of the target namespace and replay/regression evaluation before
activation.

`ProjectionNamespaceRegistry` is an offline lifecycle catalog for deterministic
qualification. Its active-candidate view excludes failed and retired records;
it does not own PostgreSQL publication or active-pointer authority.

## Shadow rebuild orchestration

Issue #204 adds `ShadowRebuildOrchestrator` as the workflow-facing seam for a
full candidate rebuild. A `ShadowRebuildPlan` pins one run, candidate,
configuration, canonical boundary and server-routed `shadow` (or
`rebuilding`) namespace. Its replay inputs are sorted by source identity,
revision, lane and command identity, so chunk and graph work is replayed in a
stable order regardless of input arrival order.

The orchestrator checkpoints content-free progress before and after each
physical input through `ProjectionRebuildProgressStore`. It resumes only
pending inputs after a retryable physical failure or abandoned run; completed
inputs are not sent to a lane a second time. Chunk effects and graph requests
share the same candidate scope and are coordinated by the same plan, while
their provider-specific adapters remain behind their existing contracts.

Provider exceptions and terminal lane failures become typed, content-free
results. A failed or incomplete run remains in a non-active namespace and can
never serve ordinary retrieval. The orchestrator does not transition an active
pointer or publish a candidate; those decisions remain PostgreSQL-owned and
outside this ticket.

## Physical cleanup and repair

Issue #206 exposes `ProjectionRecoveryCoordinator` as the physical recovery
seam. Cleanup plans are immutable, workspace/environment scoped and may target
only a `failed` or `retired` namespace plus the content-free artifact mappings
listed by the caller. The coordinator revalidates the scope, orders deletion
deterministically, records applied effects and retries only pending effects
after a physical failure. A repeated successful plan returns a replay result;
the coordinator never hard-deletes or changes a canonical record.

The Cognee cleanup adapter uses the public `delete_data(..., mode="soft")`
operation and treats an absent dataset as an idempotent replay. Provider
exceptions become typed safe failures without provider messages or source
content. Cross-workspace, cross-namespace and active-namespace cleanup is
rejected before physical calls. Chunk and graph adapters expose only
content-free mappings, including pending mappings left by an interrupted
operation, so cleanup can remove failed derived state without reopening
payloads.

Repair plans replay the original immutable `ProjectionEffect` or graph request
inputs through `ShadowRebuildOrchestrator`. The coordinator fences every input
whose source revision is stale, tombstoned or otherwise not current, so repair
cannot resurrect deleted content. Source revision identity, revision digest and
stable locator mappings are carried through unchanged. Cleanup and repair are
derived-state maintenance outside the PostgreSQL UnitOfWork; publication and
ordinary retrieval remain governed by PostgreSQL-owned lifecycle state.

## Cognee graph write-side adapter

`spine.memory.cognee.graph.CogneeGraphProjectionWriter` consumes only an
extraction result containing ontology-approved entities and relations plus
content-free source-revision snapshots. It writes one deterministic Cognee
`DataItem` per entity or relation, retaining the stable artifact ID, source
revision, ontology/extraction configuration and evidence locators in external
metadata. Entity and relation data IDs are deterministic within the routed
candidate namespace, so update, remove and retry operations do not create
duplicates.

The writer applies the same command digest and revision fences as the generic
projection lifecycle. Tombstones soft-delete all source artifacts and fence
older revisions; a newer current revision is the only permitted reappearance.
Provider failures return a recoverable, content-free result and leave pending
artifact mappings for retry. Namespace validation happens before any Cognee
call, and provider exceptions never cross the graph result boundary.

## Cognee structured read-side adapter

`spine.memory.cognee.retrieval.CogneeStructuredRetrieval` is the physical
read-side adapter for `RetrieveCommand`. It accepts an already resolved
workspace, environment, candidate/configuration/boundary and routed physical
namespace; it never chooses an active snapshot, candidate or access policy. An
ordinary retrieval is permitted only for an `active` namespace, so shadow,
rebuilding, failed and retired candidates cannot be read through this seam.

The adapter derives the Cognee dataset name from the trusted namespace and calls
only public Cognee 1.5.4 `search` with explicit `CHUNKS` and
`HYBRID_COMPLETION` strategies, verbose structured objects and references. It
maps chunks, entities and relations into provider-neutral contracts and
revalidates every returned reference against the command's SourceRevision,
workspace/environment, revision digest, processing receipt and stable locator.
Completion strings are never used as retrieval answers.

Missing, ambiguous, stale, tombstoned, cross-scope or incomplete physical
evidence returns an empty typed degraded result. Provider exceptions are reduced
to a safe physical failure; neither exception text nor protected content enters
diagnostics. Command identity/digest replay is serialized per tenant and
namespace, and digest conflicts do not issue another physical search. ACL,
active-pointer selection, ContextBundle construction and PostgreSQL UnitOfWork
remain outside this adapter.
