# Issue 11 durable execution boundary

Status: prototype-backed decision evidence for issue #11. This is not a production implementation plan.

## Recommendation

Choose **Option B: hybrid execution boundary**.

PostgreSQL, a transactional outbox and explicit workers should own idempotent short work units: canonical source/revision writes, outbox delivery, projection commands, per-file attempts, projection receipts, read-model updates and replayable progress events. Temporal should own composite durable lifecycle: cancellation, operator retry orchestration, retry exhaustion policy, rebuild/rollback, long waits, workflow queries/signals/updates and recovery of end-to-end R1 runs.

Option A, a Temporal-owned lifecycle for every R1 step, is ADR-compliant but heavier than R1 needs for small idempotent ingestion/projection units. Option C, PostgreSQL/outbox/workers own all durable lifecycle responsibilities, conflicts with accepted ADR 0010 and must not be used unless an ADR amendment or superseding ADR is approved first.

## Primary sources read

- `gh issue view 11 --comments` was attempted first, as requested, but failed with GitHub CLI output about deprecated classic Projects GraphQL fields. The same issue data was then read from `gh issue view 11 --json number,title,body,state,author,comments,labels,url`. Issue #11 is open, has no comments, and asks whether PostgreSQL + transactional outbox + explicit workers can satisfy R1 ingestion/projection/rebuild durability or whether any lifecycle requires Temporal now.
- `AGENTS.md` says PostgreSQL is the intended canonical store, Temporal Python SDK owns durable workflows/retries/signals/long waits, ingestion/notifications/external actions are at-least-once and idempotent, and long-running state belongs to the durable workflow layer.
- `docs/adr/0010-capability-based-agent-runtime.md` is accepted. It says the runtime owns lineage, idempotency, events, cooperative cancellation and short technical retries, while durable retries, fallback, approvals and stop decisions remain workflow concerns. It also says Temporal remains the sole owner of durable workflow state, retries, approvals and recovery.
- `docs/adr/0001-canonical-store-and-context-graph.md` is accepted. It says PostgreSQL is the source of truth for Spine-owned state and source revisions, while the Context Graph/Cognee side is a rebuildable projection behind Context Broker.
- `docs/ARCHITECTURE.md` states Postgres is source of truth, Cognee is rebuildable projection, at-least-once plus idempotency is required, durability belongs to Temporal, Temporal persistence is authoritative for workflow execution, and the ingestion sequence writes canonical rows plus outbox in one transaction before consumers update read models, create idempotent projection commands and publish workflow triggers.
- `docs/ROADMAP.md` makes R1 a Q&A slice with reliable document upload, source versions, graph/vector projection update/delete/rebuild, administrator processing status, diagnostic Q&A, citations, run timeline and regression evals.
- `docs/INTERFACE.md` requires per-file upload results, retry without duplicate logical effect, async `accepted -> running -> terminal` progress via SSE with polling fallback, run diagnostics and administrator-localizable ingestion/projection/generation errors.
- `docs/research/issue-11-durable-execution-boundary.md` did not exist before this note.

## Option comparison

| Option | Owner shape | Evidence fit | Testing cost | Decision |
|---|---|---|---|---|
| A. ADR-compliant Temporal-owned lifecycle | Temporal owns every durable lifecycle; Postgres/outbox mostly canonical state and event bridge | Fully respects ADR 0010, but makes small file/projection attempts pay workflow complexity even when idempotent worker recovery is enough | High: Temporal tests for many small cases plus Postgres/outbox tests | Valid fallback, not the recommended R1 granularity |
| B. Hybrid | Postgres/outbox/workers own idempotent short work units; Temporal owns composite lifecycle and durable decisions | Matches ADR 0001, ADR 0010, architecture and interface requirements without adding Temporal to every internal adapter attempt | Medium: deterministic worker/outbox tests plus focused workflow tests for cancellation/rebuild/retry exhaustion/operator retry | **Recommended** |
| C. Postgres/outbox/workers own everything | Postgres rows and workers own durable lifecycle, cancellation, rebuild/rollback and retry exhaustion | Could be made to work technically, but contradicts accepted ADR 0010 and architecture durability rule | High and subtle: custom orchestration recovery, cancellation and long-wait semantics must be rebuilt and defended | ADR amendment path only |

## State-owner matrix

| R1 lifecycle item | Recommended owner(s) | Notes |
|---|---|---|
| document upload / ingestion | PostgreSQL canonical row, transactional outbox, explicit worker/activity, API/read model, UI/SSE stream | API accepts command and idempotency key; Postgres stores upload/source/revision state; outbox schedules parsing/projection work; UI observes read model/progress. |
| per-file partial failure | PostgreSQL canonical row, explicit worker/activity, API/read model, UI/SSE stream | Each file gets independent status and sanitized error. Batch lifecycle may be Temporal-owned if it needs cancellation or operator retry orchestration. |
| SourceRevision creation | PostgreSQL canonical row, transactional outbox | Immutable source revision plus outbox record must be one transaction. |
| projection command | PostgreSQL canonical row, transactional outbox, explicit worker/activity | Command record and idempotency key are canonical; worker/activity may run projection adapter. |
| Cognee adapter/indexing attempt | explicit worker/activity | Cognee remains behind ContextProjection. Attempts are idempotent and produce receipts, warnings and failures. |
| projection validation | explicit worker/activity, Temporal workflow | Deterministic checks can be an activity; the pass/fail decision for activation/retry/rollback is Temporal-owned when part of rebuild or composite projection lifecycle. |
| shadow rebuild | Temporal workflow, explicit worker/activity, PostgreSQL canonical row | Temporal coordinates rebuild, validation and activation; workers execute projection units; Postgres stores projection versions/runs/receipts. |
| activation/rollback | Temporal workflow, PostgreSQL canonical row | Temporal owns durable decision and recovery; Postgres atomically records active ProjectionVersion and rollback audit. |
| Q&A run/progress | Temporal workflow, PostgreSQL canonical row, API/read model, UI/SSE stream | Temporal owns run lifecycle/cancellation/retry exhaustion; Postgres stores run records/context bundles/read models; UI reads progress. |
| cancellation | Temporal workflow | Worker code cooperates and records cancellation checkpoints, but durable cancel authority belongs to workflow for composite operations. |
| operator retry | Temporal workflow, PostgreSQL canonical row | Operator action is a workflow update/signal or a new idempotent command correlated to the failed logical operation. |
| retry exhaustion | Temporal workflow, PostgreSQL canonical row, API/read model, UI/SSE stream | Temporal decides exhaustion for composite lifecycle; Postgres exposes terminal status and safe diagnostics. |
| crash recovery | transactional outbox, PostgreSQL canonical row, Temporal workflow | Outbox recovers undelivered short units; Temporal recovers workflow execution and decisions. |
| SSE/polling progress | API/read model, UI/SSE stream, PostgreSQL canonical row | UI is never owner of durable state; it renders canonical/read-model transitions and can reconnect/poll. |

## Prototype evidence summary

The throwaway prototype lives in `tests/prototypes/test_issue_11_durable_execution_boundary.py`. It is deliberately an in-memory fake model, not production code and not a proposed module interface.

It covers these failure cases:

- at-least-once outbox delivery with duplicate worker delivery;
- duplicate event/idempotency key preserving one logical projection effect;
- retry after partial failure;
- per-file failure in a batch without poisoning other files;
- retry exhaustion as a Temporal-owned terminal decision in the hybrid model;
- crash after canonical write/outbox record and before delivery, recovered by replaying outbox state;
- cancellation of Q&A/composite work as Temporal-owned;
- rebuild using a shadow projection and rollback to the previous active version;
- progress shape `accepted -> running -> terminal`;
- operator retry/idempotency represented by the same logical operation key;
- PostgreSQL-owning cancellation as evidence that Option C is an ADR-amendment path, not an ADR-compliant decision.

The prototype does not verify real Temporal replay, signals, updates, time skipping or SDK behavior. `pyproject.toml` currently does not include the Temporal SDK, so this note intentionally avoids adding it just to scaffold attractive but fake workflow tests.

## Testing complexity comparison

Option A requires many Temporal workflow tests even for operations whose only durable requirement is "canonical row plus outbox record plus idempotent worker attempt." It is straightforward with Temporal SDK installed, but it spreads time-skipping/replay compatibility concerns across small ingestion/projection paths.

Option B keeps low-level ingestion/projection evidence at application/adapter seams: transaction commits outbox, duplicate delivery does not duplicate logical effects, worker attempts are idempotent, projection receipts are stable and read models expose terminal state. Temporal tests stay focused on lifecycle seams that need Temporal's value: cancellation, retry exhaustion, operator retry, shadow rebuild activation/rollback, Q&A run recovery and long waits.

Option C needs a custom orchestration test suite for cancellation, long waits, recovery, retry exhaustion, operator actions and rebuild rollback. That recreates responsibilities already assigned to Temporal by accepted ADR 0010, and dependent specs would need an approved architecture change before relying on it.

## Dependent spec guidance

- Issue #3 should assume `SourceObject`/`SourceRevision`/original storage are canonical PostgreSQL state, with outbox records emitted transactionally for downstream projection/workflow triggers.
- Issue #15 should assume Context Broker and Cognee projection attempts are idempotent worker/activity units that return receipts and do not own durable lifecycle decisions.
- Issue #18 should assume Q&A run lifecycle, cancellation, retry exhaustion and recovery are Temporal-owned, while run records, context bundles, citations and read models are stored in PostgreSQL.
- Issues #23-#27 should specify explicit idempotency keys, outbox delivery semantics, per-file statuses, safe progress read models, operator retry commands and the Temporal-owned lifecycle points for rebuild/rollback and composite runs.
- None of the dependent implementation specs should finalize a Postgres-only durable lifecycle unless an ADR 0010 amendment or superseding ADR is approved first.

## ADR approval gate

No ADR amendment is required for Option B. If the team instead chooses Option C, prepare an ADR amendment or superseding ADR that explicitly transfers durable lifecycle ownership from Temporal to PostgreSQL/outbox/workers for the affected R1 lifecycle responsibilities. Until that ADR is accepted, dependent implementation specs must treat Postgres-only durable lifecycle ownership as unapproved.
