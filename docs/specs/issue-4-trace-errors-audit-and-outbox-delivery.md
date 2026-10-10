# Issue #4 — Trace, structured errors, audit and outbox delivery

Status: implementation-ready; decomposed into GitHub Issues #61–#68.

Originating issue: GitHub Issue #4, "[R0 parallel] Implement trace IDs,
structured errors, and append-only audit events".

## Summary

R1 needs one disclosure-safe diagnostic contract from ingress to asynchronous
work. Today the persistence kernel already carries a UUID `trace_id` and
optional correlation/causation identities in immutable outbox intents, but the
repository has no shared trace context, external error envelope, audit ledger,
safe diagnostic sink or dispatcher for those intents.

This specification defines the smallest cross-cutting foundation pulled by the
governed document Q&A slice:

- one trace identity propagated through HTTP, application commands, Unit of
  Work, outbox delivery, workers and future retrieval/model/SSE/run contracts;
- a versioned structured-error registry whose output is safe by construction;
- immutable, versioned Audit Events for meaningful commands, decisions,
  transitions, delivery retries and feedback;
- typed Diagnostic Events for logs and telemetry that cannot become a second
  audit store;
- recoverable at-least-once dispatch of the existing immutable outbox intents;
- deterministic clocks, identifiers and retry schedules for offline tests.

The module interfaces are deliberately smaller than the implementation. Callers
carry one `DiagnosticContext`, raise application failures, append an Audit Event
through the current Unit of Work, emit a typed Diagnostic Event, or invoke one
bounded `dispatch_batch` operation. Error mapping, redaction, serialization,
leasing, backoff, deduplication and safe provider-failure handling stay behind
those seams.

## Goals

1. Preserve one non-zero trace identity from a trusted ingress or worker entry
   point through every R1 hop without treating it as authorization.
2. Make public failures stable, versioned, localizable and disclosure-safe.
3. Make required audit durable, append-only, tenant-scoped and independently
   queryable by opaque identity.
4. Deliver transactional outbox intents at least once after crashes, lease
   expiry and transient failures without implementing a workflow engine.
5. Ensure logs, metrics, events and tests cannot accidentally serialize raw
   protected content or provider exceptions.
6. Provide reusable conformance fixtures for #3, #15, #25, #26 and #27 so those
   tickets can prove trace propagation when their modules exist.

## Non-goals

- Full OpenTelemetry, Langfuse, dashboards, alerting, SLOs, backup/restore or
  deployment-specific log shipping; those belong to #18.
- OIDC validation, membership/role resolution or Access Policy evaluation;
  those belong to #5 and #7.
- Source, ingestion, projection, retrieval, Q&A run, feedback or SSE business
  schemas; their owning tickets consume this specification.
- Persisting prompts, answers, excerpts, raw payloads, provider failures, stack
  traces or chain-of-thought for debugging.
- Exactly-once delivery, a general message broker, arbitrary fan-out, event
  sourcing, long-running orchestration or Temporal replacement.
- Automatic retry of business commands, model generation or composite workflow
  steps. Only bounded outbox delivery retries are owned here.
- Audit search/API/UI, retention periods, legal hold, physical cleanup or a
  content-superuser audit role. #18 and later policy work own those surfaces.
- Removing or changing the response body of legacy `/ask` and `/ingest` routes.

## Sources and accepted constraints

- `AGENTS.md` requires workspace/environment on tenant records and commands,
  evidence lineage, at-least-once idempotency and non-leaking failures.
- `docs/ARCHITECTURE.md` assigns one trace ID across API, outbox, retrieval and
  generation; PostgreSQL owns audit while telemetry remains diagnostic.
- `docs/INTERFACE.md` requires stable error code, safe message, retryability and
  trace ID, and forbids protected content or raw failures in browser telemetry.
- ADR 0004 fixes immutable transactional outbox intent persistence and leaves
  claiming, delivery, retry and operational state to Issue #4.
- ADR 0010 and the Issue #11 decision keep composite durability, cancellation,
  long waits and retry exhaustion in Temporal.
- ADR 0011 keeps authorization in current PostgreSQL policy; audit history and
  a trace ID never grant access.
- The Issue #8 persistence kernel supplies trusted tenant context, translated
  persistence failures, Unit of Work, typed outbox intents and real PostgreSQL
  integration tests.
- ADR 0020 separates canonical Audit Events, operational delivery state and
  non-canonical Diagnostic Events.

## Domain language

`CONTEXT.md` defines two product-specific terms:

- **Audit Event** is immutable canonical evidence that a meaningful Spine
  command, decision, transition, retry or feedback outcome occurred.
- **Diagnostic Event** is a safe operational observation and is never evidence
  that a business effect occurred.

Trace context and structured errors are general software contracts and do not
belong in the domain glossary.

## Responsibility and module seams

### Diagnostics contracts module

The application diagnostics module owns four public interfaces:

1. `DiagnosticContext` — immutable trace, correlation and causation identities;
2. `StructuredError` plus an explicit error registry/mapper;
3. `AuditWriter` — append one registry-validated Audit Event in the current Unit
   of Work;
4. `DiagnosticSink.emit(event)` — emit one registry-validated safe observation.

The interface does not expose logging, OpenTelemetry, SQLAlchemy, FastAPI or
provider exception types. Production and recording adapters make the sink seam
real; in-memory and PostgreSQL adapters make the audit seam real.

### Outbox dispatch module

The dispatcher exposes one primary application interface:

```text
dispatch_batch(limit, worker_identity) -> DispatchReport
```

The report contains only counts and safe stable codes. The implementation owns
route resolution, lazy delivery-record creation, claims, leases, handler timeout,
acknowledgement, retry scheduling, quarantine and Audit/Diagnostic Events.

Each `(event_type, schema_version)` maps to exactly one R1 consumer name. A
producer that needs two logical effects emits two typed intents. This avoids a
premature generic fan-out bus and makes recovery ownership explicit.

### Composition seams

- HTTP middleware establishes the initial `DiagnosticContext` and exposes the
  trace ID in the response.
- A trusted request-context factory combines diagnostics with identity and scope;
  Issue #5 supplies the authenticated subject later.
- Unit of Work verifies that diagnostic/audit/outbox tenant and trace scope
  cannot diverge.
- A worker reconstructs context only from a validated claimed delivery and its
  immutable outbox intent, never from handler-provided identifiers.
- Future ingestion, projection, retrieval, model, SSE and run modules accept and
  return the same context through their own typed contracts.

## Trace contract

### Identity and ingress

- `trace_id` is a non-zero UUID represented externally as lowercase canonical
  text and stored as PostgreSQL UUID, matching the existing kernel.
- A valid W3C `traceparent` may continue its 128-bit trace identity. Missing,
  malformed or all-zero context causes the server to generate a new ID; it does
  not cause a detailed parsing error.
- Request bodies, query parameters and arbitrary custom trace headers never set
  authority or replace the server-selected trace.
- The response includes `X-Trace-ID`. The value is diagnostic, non-secret and
  cannot be used to retrieve protected data without ordinary authorization.
- Tests inject an ID source; production uses a cryptographically strong UUID
  source. No module reads global randomness in a deterministic test.

### Correlation and causation

- `correlation_id` groups a higher-level run or command when such an identity
  exists; it is optional and never substitutes for `trace_id`.
- `causation_id` identifies the immediate triggering event/command. A worker
  handling an outbox intent uses that intent's `event_id` as causation.
- Nested work preserves `trace_id`, may preserve correlation, and replaces
  causation with the immediate parent identity.
- A retry of one delivery attempt preserves trace, event and correlation IDs and
  receives a new attempt identity.

### Propagation and validation

- `DiagnosticContext` is frozen and validates every identity at construction and
  again at trusted adapter seams.
- Persistence rejects a trace mismatch before repository behavior.
- Outbox records retain the originating trace/correlation/causation identities.
- Dispatcher-created Audit and Diagnostic Events retain the same trace and use
  the outbox event as causation.
- No log, metric or public event derives a new trace ID midway through a logical
  operation.

## Structured-error contract

The first external schema is a frozen, `extra="forbid"` model with:

```text
schema_version = 1
code
safe_message
retryability = never | same_command | after_delay
trace_id
```

- `code` is a bounded namespaced identifier and is the stable machine contract.
- `safe_message` is a conservative fallback, not the sole localization key.
- `retryability` describes caller behavior. It does not authorize automatic
  retries or promise success. `same_command` requires the same idempotency key.
- Transport adapters map registered codes to HTTP/SSE status without putting
  status codes into the application contract.
- A registry owns exception-category to `(code, message, retryability)` mapping.
  Duplicate codes or mappings fail at startup/test construction.
- Known persistence categories map explicitly. Unknown errors map to one generic
  internal code and never use `str(error)`, `repr(error)`, SQL/provider text or
  traceback serialization.
- Validation, denial, conflict, unavailability and unexpected failure remain
  distinct. Expected abstention and clarification are Q&A outcomes, not errors.
- Structured-error serialization is the only public error path under `/api/v1`.
  Legacy response bodies remain compatible until their owning replacement.

## Diagnostic Event contract

A Diagnostic Event is a typed, versioned envelope containing:

- event type and schema version;
- severity from a fixed enum;
- injected occurrence time;
- trace, optional correlation and causation identities;
- workspace and optional environment when known;
- a producer-owned frozen payload registered for that type/version.

Payloads are allowlisted schemas made of safe enums, bounded identifiers,
durations, counts and opaque object references. They cannot represent document
text, filenames when content-bearing, questions, answers, excerpts, prompts,
credentials, provider payloads, exception strings, SQL, URLs with secrets,
stack traces or chain-of-thought.

The standard-library logging adapter emits structured JSON from the validated
snapshot and never sets `exc_info`. A recording adapter supports contract tests.
OpenTelemetry/Langfuse adapters are deferred to #18 and must consume the same
safe envelope rather than accept arbitrary attributes.

## Audit Event contract

### Envelope

Each immutable Audit Event contains:

- opaque non-zero `audit_event_id`;
- event type and schema version;
- Workspace and optional Environment identity;
- acting subject and service principal identities following trusted-context
  semantics, with a service principal required for worker-originated events;
- trace, optional correlation and causation identities;
- injected occurrence time and database-recorded append time;
- one opaque target/reference when applicable;
- outcome/reason as bounded codes;
- producer deduplication identity where the logical event can be retried;
- one registered, frozen producer-owned payload containing only safe fields.

Audit payloads use the same deep-immutability and explicit registration rules as
outbox payloads. A generic `dict[str, Any]` audit metadata field is forbidden.

### Required event families

The registry supports families, not one universal payload:

- command accepted, rejected, replayed and completed;
- canonical state transition committed;
- access allowed and denied, without leaking a protected target to an
  unauthorized public response;
- outbox delivery succeeded, retry scheduled and quarantined;
- feedback recorded;
- policy/approval events supplied later by their owning modules.

### Atomicity and fail-closed behavior

- Audit proving a canonical mutation is appended in the same Unit of Work and
  commits or rolls back with that mutation.
- Audit required before protected disclosure or an externally visible action is
  durably committed before releasing the effect.
- If required audit cannot be committed, protected disclosure/action does not
  proceed and the caller receives a safe structured failure.
- A denied operation remains denied if audit persistence fails; it returns a
  generic safe failure/denial and emits only safe diagnostics.
- Duplicate execution of the same logical audit producer identity yields one
  Audit Event. Separate retry attempts have stable distinct attempt identities.
- Ordinary runtime roles can insert and read only within verified tenant scope;
  they cannot update or delete Audit Events.

Historical audit cannot authorize current access. Audit APIs and content-bearing
inspection are outside this specification.

## PostgreSQL audit persistence

The migration adds an append-only `audit_events` table with:

- named primary, tenant, environment and producer-deduplication constraints;
- explicit Workspace ownership and optional composite Environment ownership;
- forced RLS and runtime grants limited to scoped insert/read required by
  application behavior;
- JSONB only for registry-validated safe payload snapshots;
- indexes for tenant/time, trace and target identity needed by operational
  correlation without introducing a general search API;
- no ordinary update/delete repository methods or runtime grants.

The in-memory and PostgreSQL implementations run the same AuditWriter contract
suite. Migration tests cover empty/current prior heads, RLS, grants, immutability,
two-workspace isolation and rollback with canonical mutation/outbox intent.

## Outbox delivery model

### Separation from immutable intent

The existing `outbox_intents` row remains immutable. Delivery state is a
separate mutable control record keyed by `event_id` and resolved consumer name:

```text
pending | in_flight | retry_scheduled | delivered | quarantined
```

It records attempt count, next-attempt time, lease token/owner/expiry, safe last
error code, delivered/quarantined time and an optimistic version. It never stores
raw exception messages or protected payload copies.

### Claim and recovery

- A dispatcher materializes the single registered consumer route and claims a
  bounded due batch using PostgreSQL locking that permits multiple workers.
- Claim commits before handler I/O. The handler runs outside a database
  transaction under a bounded timeout shorter than the lease.
- A valid lease token is required to acknowledge, reschedule or quarantine.
- Expired in-flight leases become claimable again; stale workers cannot complete
  a newer lease.
- A crash after handler success but before acknowledgement can redeliver. The
  stable outbox `event_id` is the consumer idempotency key.
- Unknown event type/version or consumer binding is quarantined with a safe code
  and Audit Event rather than silently dropped.

### Retry policy

- Retry classification consumes stable application/adapter error categories,
  never provider text.
- Backoff is bounded and deterministic from attempt number and an injected retry
  policy. Tests do not use wall-clock sleep or random jitter.
- Retryable failures schedule another delivery; terminal failures and bounded
  delivery-attempt exhaustion quarantine the delivery.
- Quarantine is an operational delivery outcome, not a business/workflow
  terminal decision. Temporal still owns composite retry exhaustion.
- Each outcome updates delivery state and appends its Audit Event atomically.
  Diagnostic emission may occur after commit and cannot change the outcome.

### Consumer interface

The consumer receives the validated typed intent and reconstructed
`DiagnosticContext`. It returns a typed success or raises a registered terminal
or retryable category. It cannot acknowledge itself, mutate lease state or
receive database sessions. Consumers must use `event_id` idempotently when
performing their own logical effect.

## HTTP and worker integration

- FastAPI middleware establishes context before route/application execution,
  adds `X-Trace-ID` to success and failure responses and binds safe diagnostics.
- `/api/v1` exception handling uses the Structured Error schema. Request
  validation receives a stable code without echoing protected inputs.
- Legacy endpoints gain only the trace response header unless their existing
  compatibility tests permit more.
- Worker entry points accept a claimed delivery, reconstruct trusted scope and
  trace context, invoke one registered consumer, and finalize through the
  dispatcher.
- Structured logging adapters receive only Diagnostic Events. Direct logging of
  request bodies, outbox payloads, exceptions or model/provider objects is
  forbidden by architecture tests and review rules.

## Downstream conformance

Issue #4 cannot directly wire modules that do not yet exist without creating a
dependency cycle. It therefore supplies shared conformance fixtures and a
synthetic composed path:

```text
HTTP ingress -> application command -> Unit of Work/outbox
             -> dispatcher/worker -> audit + structured terminal result
```

The following tickets must reuse those fixtures at their public interfaces:

- #3: ingestion and per-file workers;
- #15: projection commands and receipts;
- #27: retrieval and Context Bundle issuance;
- #25: model generation and persisted Q&A runs;
- #26: `/api/v1`, SSE and terminal rehydration.

Those modules must not invent parallel trace/error envelopes. Closing Issue #4
proves the kernel and synthetic path; each consumer ticket remains responsible
for its real-hop conformance before R1 release.

## Failure behavior

| Failure | Public/worker outcome | Audit | Diagnostic data |
|---|---|---|---|
| malformed inbound trace | new server trace; no detailed parse error | none required | safe invalid-context code |
| known validation/conflict | registered non-retryable structured error | command rejection when meaningful | code and trace only |
| retryable persistence failure | registered retryability; no raw provider data | attempt/retry when committed | safe category/count |
| required audit unavailable | protected effect withheld | no false success event | generic audit-unavailable code |
| handler transient failure | delivery rescheduled | retry event atomic with state | attempt/backoff/code |
| handler terminal failure | delivery quarantined | quarantine event atomic with state | code, never exception text |
| worker crash during handler | lease expires and event may redeliver | later attempt records recovery | safe lease-expired observation |
| stale lease completion | rejected without state change | optional safe rejection event | stable stale-lease code |
| unknown exception | generic internal failure | failure only if safe transaction succeeds | no message/stack/payload |

## Security and disclosure invariants

1. Trace, audit and diagnostic identifiers grant no access.
2. All tenant records and operations preserve Workspace and optional Environment.
3. No protected text, filename when protected, question, answer, excerpt, prompt,
   credential, token, connection string, SQL, provider payload, raw exception,
   stack trace or chain-of-thought crosses public errors, outbox delivery state,
   Audit Events, Diagnostic Events, logs or telemetry.
4. Safe payload schemas are explicit allowlists; heuristic string scanning is
   defense in depth only and never the contract.
5. Unknown errors collapse to one safe category without source-dependent details.
6. Denied and cross-workspace paths do not expose protected target existence in
   response, log or diagnostic payload.
7. Audit append-only status does not imply indefinite retention or authorization.
8. Delivery payload access does not widen the consumer's trusted scope.

## Determinism and injected dependencies

All public behavior is testable with injected:

- clock;
- ID source;
- retry schedule;
- diagnostic recording sink;
- in-memory AuditWriter/Unit of Work;
- fake outbox consumer;
- fake handler timeout/cancellation adapter.

Tests use no wall-clock sleep, random UUID, network provider, developer database
or execution-order assumption. Real PostgreSQL tests use the repository's safe
test harness.

## User stories

US1. As an administrator, I can report a trace ID from any safe failure.

US2. As an operator, I can correlate HTTP, command, outbox, worker and audit
records without reading protected content.

US3. As a frontend developer, I receive a stable code, safe fallback message,
retryability and trace ID for every `/api/v1` failure.

US4. As a security reviewer, I can prove raw exceptions, stack traces,
credentials and protected text are unrepresentable in diagnostic contracts.

US5. As an application developer, I append required audit atomically with a
canonical mutation through the current Unit of Work.

US6. As an authorization developer, I can durably audit allow/deny decisions
without making historical audit an authorization source.

US7. As a worker developer, I receive a validated typed intent and the original
trace context after a recoverable claim.

US8. As an operator, I can distinguish pending, leased, retrying, delivered and
quarantined work using safe codes.

US9. As a consumer developer, I can use stable event identity to make duplicate
delivery idempotent.

US10. As a test author, I can deterministically reproduce IDs, time, backoff,
lease expiry and failures offline.

US11. As a downstream module owner, I can reuse one conformance suite rather
than inventing another trace/error format.

US12. As a maintainer, I can replace the logging or future telemetry adapter
without changing application callers or canonical audit.

## Executable acceptance criteria

AC1. Diagnostic context is frozen, rejects zero/malformed identities and
preserves trace/correlation/causation semantics.

AC2. Valid W3C trace context continues one trace; malformed or absent input
creates a deterministic server trace in tests.

AC3. Trace mismatch between trusted context, Unit of Work, outbox, audit or
delivery fails before a protected/canonical effect.

AC4. Structured Error v1 forbids extra fields and contains stable code, safe
message, retryability and trace ID.

AC5. Every registered persistence/dispatcher category maps deterministically;
unknown exceptions never expose their string, repr or traceback.

AC6. `/api/v1` validation and application failures use Structured Error v1 and
include the same `X-Trace-ID`; legacy bodies remain compatible.

AC7. Diagnostic Event registries accept only frozen `extra="forbid"` payloads
made from approved safe types.

AC8. Recording and logging sinks receive identical validated snapshots; logging
never uses `exc_info` or arbitrary attributes.

AC9. Tests with secret, document, prompt, answer, SQL, URL and stack-shaped
inputs prove they cannot cross structured error or diagnostic schemas.

AC10. Audit Event types are immutable, versioned, tenant/trace-scoped and
registry validated without arbitrary dictionaries.

AC11. Canonical mutation, Audit Event and required outbox intent commit together
or all roll back under injected failures.

AC12. Required audit failure prevents protected disclosure or external effect;
denied access remains denied.

AC13. Duplicate producer identity yields one logical Audit Event; separate retry
attempts retain distinct stable attempt identities.

AC14. PostgreSQL Audit Events have forced RLS, named constraints, least-privilege
grants and no ordinary runtime update/delete path.

AC15. Allowed, denied and cross-workspace audit tests reveal no protected target
data through outputs or errors.

AC16. In-memory and PostgreSQL adapters pass the same AuditWriter suite.

AC17. Outbox intents remain immutable while delivery state changes through its
own explicit repository/interface.

AC18. A bounded dispatcher claim cannot be acquired by two workers; an expired
lease can be reclaimed and a stale token cannot finalize it.

AC19. Handler success followed by acknowledgement loss redelivers the same event
identity and produces one logical consumer effect with an idempotent fake.

AC20. Transient failure schedules deterministic bounded backoff; terminal or
exhausted delivery quarantines with a safe code.

AC21. Delivery state transition and its required Audit Event are atomic.

AC22. Unknown route/version quarantines rather than drops or executes arbitrary
payload code.

AC23. Worker reconstruction preserves tenant, trace, correlation and causation
and refuses mismatched handler-supplied context.

AC24. Dispatcher payloads, delivery rows, errors, Audit Events and Diagnostic
Events contain no raw handler/provider failure.

AC25. The synthetic HTTP-to-worker path preserves one trace ID and produces
only the registered public error/audit/diagnostic contracts.

AC26. Shared downstream fixtures can be applied without FastAPI, SQLAlchemy,
Cognee, Temporal or model-provider types crossing application interfaces.

AC27. All clocks, IDs and schedules are injected; default tests use no wall
clock, random UUID, network or external provider.

AC28. Domain modules retain no FastAPI, SQLAlchemy, Cognee, Temporal, logging or
OpenTelemetry imports.

AC29. Migrations upgrade from every retained revision to one head and verify the
final catalog, RLS and grants.

AC30. Full pytest, compileall, domain-import and clean-diff gates pass.

## Test levels

- **Domain/application unit:** identity validation, registries, error mapping,
  retry schedule, safe schema construction and in-memory audit/dispatch outcomes.
- **Contract:** shared AuditWriter, DiagnosticSink, dispatcher consumer and
  downstream trace/error conformance suites.
- **Infrastructure:** FastAPI middleware/error mapping, structured logging,
  PostgreSQL mappings, failure translation and lease SQL.
- **Integration:** real PostgreSQL migrations, RLS/grants, atomic audit/outbox,
  concurrent claims, crash/lease recovery and two-workspace isolation.
- **Composed offline:** synthetic ASGI ingress through command/outbox/worker to
  terminal error/audit/diagnostic results.
- **Architecture/security:** forbidden imports and negative leak corpus across
  response, event, audit, delivery, log and telemetry representations.

## Ticket decomposition

T1 / #61. Establish diagnostic context and structured-error contracts.

T2 / #62. Emit only registry-validated Diagnostic Events through safe sinks.

T3 / #63. Establish Audit Event contracts and an in-memory append-only ledger.

T4 / #64. Persist the tenant-scoped append-only audit ledger in PostgreSQL.

T5 / #65. Add durable outbox delivery state with claim and lease recovery.

T6 / #66. Dispatch outbox intents with bounded retries, quarantine and audit.

T7 / #67. Propagate diagnostics through FastAPI and worker composition roots.

T8 / #68. Qualify the complete Issue #4 diagnostics path and migration history.

Each ticket owns one independently verifiable interface/outcome, carries its
exact AC subset and is a native sub-issue of #4.

## Dependency plan

```text
#61 ─┬─> #62 ─────────────┐
     ├─> #63 -> #64 ──────┼─> #66 ─> #67 ─> #68
     └─> #65 ─────────────┘       └────────> #68
                           #64 ─────────────> #68
```

T2, T3 and T5 may proceed in parallel after T1. T4 follows T3. T6 composes T2,
T4 and T5. T7 composes the HTTP and worker entry points after T6. T8 performs the
final cross-ticket qualification.

## Compatibility and rollout

- Existing UUID trace fields and outbox intent rows remain compatible.
- New migrations are forward-only and coordinate to one Alembic head.
- Legacy Q&A routes remain available and gain no incompatible error body.
- Diagnostic adapters are introduced behind application interfaces; #18 may add
  OpenTelemetry later without changing callers.
- No roadmap checkbox is changed until the implementation and R1 exit criteria
  are actually proven.

## Deferred decisions

- Audit retention duration, legal hold and deletion procedure.
- Production telemetry backend, sampling and deployment log routing.
- Audit search/read API and administrator interface.
- Multiple destinations/fan-out for one outbox intent.
- Broker-backed dispatch or independent worker scaling beyond measured need.
- Temporal workflow-specific trace adapter and remote agent trace propagation.

## Completion criteria

Issue #4 is implementation-complete when T1–T8 are merged, AC1–AC30 pass, the
full repository gates pass, the synthetic HTTP-to-worker trace is reproducible,
and no protected fixture value appears in any response, error, audit, delivery,
log or diagnostic capture. Real ingestion/projection/retrieval/model/SSE modules
then prove the same shared conformance contract in their owning tickets before
R1 release.
