---
status: accepted
---

# Production Q&A capability and observer handoff

## Context

The R1 production observer needs to execute the same authorized Q&A application
flow used by the product and receive a bounded terminal result plus the typed
records produced by that execution. The repository has the application,
admission, Context Broker, answer execution, persistence, disclosure, outbox,
and Temporal contracts, but it does not yet provide a concrete production
`AnswerQuestionCapability` composition or a complete observer result/capture
handoff.

ADR-0038 assigns admitted execution, cancellation, retries, and terminal
arbitration to the durable workflow owner. ADR-0039 selects the existing
authorized Q&A application boundary, requires observation values to come from
the same in-flight call, and forbids reconstructing missing retrieval lineage
from run history or gold labels. This ADR records the production owner's
approval of composition, bounded terminal readback, and typed-capture handoff.
The application contracts are selected here; concrete channel implementation,
timeout values, and deployment security configuration are specified for #469
and supplied by the runtime owner.

## Approval scope

The initiator explicitly confirmed D1–D4 and subsequently confirmed D5–D6 as
technical architecture decisions on 2026-10-18. This approval does not
authorize use of private corpus data, changes to `AccessPolicy`, creation of
identities or grants, or execution against a production runtime. Those
permissions and operational qualifications remain separate.

## Decisions

### D1 — canonical execution mode

Production Q&A uses the durable path:

`Ask Admission → transactional outbox → Temporal workflow/executor`

The workflow remains the owner of admitted execution, retries, cancellation,
recovery, and terminal arbitration under ADR-0038. A synchronous terminal Ask
is not an alternate production execution path for the observer. The admission
acknowledgement reports acceptance only and is never treated as a terminal Q&A
result.

### D2 — one production composition

Implement and wire one production `AnswerQuestionCapability` using the
existing persisted Ask, Ask Admission, trusted authorization context, authorized
Context Broker, answer execution/generation, canonical persistence, and
disclosure contracts. The API and observer consume this same composition.
Benchmark code must not introduce a second retrieval, generation,
authorization, persistence, or disclosure pipeline.

### D3 — bounded terminal result

The observer obtains a bounded, identity-correlated terminal result for the
specific admitted command/run, including the existing Q&A terminal outcome and
a persisted disclosure that is revalidated under current authorization before
protected answer/citation content is returned. A result for another command,
run, attempt, workspace, or environment is rejected. Timeout, cancellation,
authorization change, incomplete persistence, and unavailable readback fail
closed and do not become fabricated model outcomes.

Use bounded polling of PostgreSQL-canonical state through the existing
`AskAdmissionApplication.status()` and canonical persistence read contracts.
Add a read-only application waiter with an injected monotonic clock and sleeper,
bounded deadline, and strict correlation of `command_id`, `run_id`,
`session_id`, `message_id`, `workspace_id`, `environment_id`, and requester
identity. Once terminal state is observed, obtain protected presentation only
through the existing authorized `QASessionService.get_message()` path and its
current disclosure revalidation. A result is observable only after terminal
state, the exact terminal message, matching typed capture, and current
disclosure have all been validated.

The waiter distinguishes pending/admitted, running, terminal success,
terminal failure/cancellation, and infrastructure unavailability. A missing
or mismatched record is never substituted from history. Observer timeout ends
only the observer's wait and never cancels durable execution; do not use
`wait_for_cancellation()` for polling. Workflow ownership of retries,
cancellation, and terminal arbitration remains unchanged. `COMPLETED` is not
the only execution result eligible for terminal reporting.

### D4 — same-execution typed capture

The observer's typed capture originates from the same authorized execution and
contains the existing observation inputs: `ContextResponse`,
`ProjectionRetrievalResult`, `RetrievalResult`, `ContextBundle` and lineage,
answer/citations, and verified disclosure. It must not trigger a second
retrieval, bypass current authorization or disclosure checks, or synthesize
missing values from run history or gold labels.

Add an application `CaptureChannel` port backed by an authenticated, ephemeral
in-memory transport for separate-process worker/observer topology. The observer
registers a subscription before admission. The authorized worker publishes a
versioned `r1-qa-observer-capture:v1` envelope from the same execution after
terminal persistence and disclosure. It contains strict execution, scope,
requester, attempt, and capture-routing correlation plus typed observation
inputs. The observer accepts it only when identities match terminal readback
and current authorized disclosure.

The channel contract includes workload authentication and per-subscription
authorization; bounded buffering and backpressure; TTL; one-shot acknowledgement;
idempotent duplicate handling; and cleanup. Worker restart, duplicate or
expired delivery, observer disconnect, backpressure, or capture loss fails the
observation closed without changing canonical Q&A outcome or retrying
retrieval. `trace_id` may route a subscription only if uniqueness per case is
proven; otherwise use a separate opaque `capture_id`. Deployment topology,
workload credentials, authenticated transport settings, and channel security
configuration must be supplied and operated by the runtime owner; this ADR
does not assert that they are deployed.

The channel carries no protected retrieval payload in Temporal history, logs,
or benchmark-only durable storage. It does not write a second copy into
canonical Q&A persistence and does not change persisted Q&A outcome if capture
is lost. The existing authorized Context Broker/execution is the sole source
of the typed records; no second retrieval or reconstruction from run history
or gold labels is allowed.

The observer retains the canonical command/run/session/message/scope/requester
identity returned by admission. The canonical attempt is not guessed from
`run_id`: the bounded waiter resolves `attempt_id` and `attempt_number` from
PostgreSQL terminal status, then binds the complete execution identity before
it accepts the capture. The channel may hold one bounded, TTL-limited pre-bind envelope;
the later bind rejects and discards it if any run or attempt field differs.
Unavailable status without complete attempt identity fails closed. This keeps
Temporal Activity retries and later Q&A attempts from being treated as the
same observer attempt.

### D5 — bounded canonical terminal readback

The application waiter is read-only and bounded. It polls canonical admission
state with an injected clock/sleeper, validates the full correlation tuple and
requester, then uses authorized persisted message readback and disclosure
revalidation. Terminal persistence can race capture publication: neither alone
is sufficient. Freeze observation only after terminal read, matching capture,
and current disclosure all succeed. Observer timeout does not send a
cancellation request. Infrastructure read failures remain unavailable, not
model outcomes.

### D6 — authenticated ephemeral typed-capture handoff

The application `CaptureChannel` registers before admission and delivers a
versioned typed envelope from the authorized worker to the observer over an
authenticated, ephemeral in-memory channel. Strict correlation binds command,
run, session, message, workspace, environment, requester, attempt, and capture
routing identity. `trace_id` is acceptable only with proven per-case uniqueness;
otherwise use an opaque `capture_id`. Delivery is bounded, TTL-limited,
one-shot acknowledged, idempotent for duplicates, and cleaned up on completion
or expiry. Observer acknowledgement, timeout, or disconnect also removes its
subscription; this cleanup never cancels the admitted workflow. Capture loss
fails observation closed but never changes the canonical Q&A result or triggers
another retrieval.

### Worker-local latency

Measure elapsed duration with an injected monotonic clock inside the worker
around bounded authorized application execution, from immediately before the
admitted execution invocation through terminal persistence/disclosure. Transmit
scalar duration samples with activity-attempt identity; never compare monotonic
timestamps from different processes. Exclude queue waiting, Temporal
scheduling, terminal readback/capture waiting, corpus preparation, and gold
evaluation. Account for activity attempts/retries separately. Incomplete
samples do not produce fabricated aggregate latency. Preserve
`ProductionContractObservation:v1`; do not silently redefine the
`ProductionContractApplicationFlow.execute` latency boundary in ADR-0039. Map
worker invocation duration into the existing execution-record boundary only
when meanings match; otherwise require a separately versioned execution-record
contract. Include the non-negative `worker_execution_elapsed_ns` scalar and its
canonical `attempt_id` in the ephemeral execution/capture envelope; do not
transmit worker timestamps.

### D7 execution-record compatibility

The selected v2 record is `r1-case-execution-record:v2`. It retains
`observer_elapsed_ms` with the exact v1 boundary: wall time around
`ProductionContractApplicationFlow.execute`, including queue/admission wait,
Temporal scheduling, and terminal readback/capture waits that occur inside that
call. Corpus preparation before the call and evaluation after it are excluded.
V2 adds nullable
`worker_execution_elapsed_ns` and `worker_execution_attempt_id`. Both worker
fields must be null together or present together; a present duration is one
non-negative worker-local monotonic sample and its ID is the canonical Q&A
attempt ID from that execution's typed capture. The sample excludes queue wait,
Temporal scheduling/retry delay, observer readback/capture waiting, corpus
preparation, and evaluation. No cross-process monotonic timestamp or aggregate
across incomplete/retried samples is allowed. V1 serialization and
`ProductionContractObservation:v1` remain unchanged. Downstream consumers
dispatch on `schema_version`; #480 owns v2 report serialization and keeps both
measurements distinct.

### D8 — Versioned observer capture routing

Observer-routed executions may use the existing strictly typed
`ObserverRoutedAskAdmissionWorkflowInput` wrapper, or an equivalent
backward-compatible versioned wrapper, to carry `capture_id`,
`subscription_id`, and the subscription expiry needed by the worker publisher.
These values are opaque routing metadata, not authorization credentials.
The existing application and authenticated `CaptureChannel` boundaries remain
responsible for authorization and full canonical execution identity checks.
Legacy Ask Admission v1 without observer capture remains supported. Protected
retrieval, context bundle, answer, and disclosure values remain in worker
memory and the authenticated ephemeral capture channel; they are not included
in Temporal input, result, history, or durable metadata. This refines #476's
capture-enabled metadata contract, which previously named only `capture_id`.

### D9 — Canonical persisted Q&A attempt identity

The canonical Q&A domain attempt identity and attempt number are allocated and
persisted before authorized retrieval begins. A retry of the same logical
execution, including a Temporal Activity retry, resumes the same persisted
attempt identity. A new Q&A domain attempt is allocated through the canonical
run/attempt persistence lifecycle and receives a distinct identity. Temporal
Activity attempt numbers and Q&A domain attempt numbers remain separate.
Capability input, Context Broker capture, terminal status, `CaptureEnvelope`,
and observer validation all use the persisted Q&A attempt identity. The
observer does not derive an attempt identity from `run_id` or assume an
attempt ordinal. Capture publication failure does not start another retrieval
or change the canonical Q&A result; `CaptureChannel` binding remains one-shot.

### D10 — Preserve observation v1 with safe outer dispositions

Keep `ProductionContractObservation:v1`'s schema and meaning unchanged. Build
an observation only when the actual authorized execution provides the required
typed `ContextResponse`, `ProjectionRetrievalResult`, `RetrievalResult`, any
`ContextBundle` required by that response, `AnswerQuestionOutput`,
`AnswerDisclosureRecord`, and full canonical execution identity. Never invent
typed records, reconstruct them from run history, or perform a second
retrieval. If records are missing or fail validation, return a safe
non-success outer execution disposition and leave the persisted Q&A outcome
unchanged. In-flow denial may remain inside a validated observation when all
required records exist. A pre-retrieval denial maps to outer `denied` only
after canonical authorization denial is confirmed; missing capture by itself
is not denial. An unobserved eligible case does not pass evaluation. This does
not alter v1, metric formulas, or release qualification rules.

### Canonical durable Q&A terminalization (#496)

Durable production Q&A carries an ephemeral typed execution receipt from the
capability to the persistence boundary. It references only the canonical
run/attempt identity and execution pin, the actual persisted ContextBundle,
AgentResult and artifact lineage, the terminal output, and the immutable
disclosure record. The receipt is an in-process handoff; it is not placed in a
public response, Temporal payload/history, logs, or a durable artifact.

The disclosure decision is persisted before terminalization. A single
caller-owned tenant Unit of Work verifies the receipt and disclosure, completes
the attempt, completes the run with that exact final attempt, and completes the
linked Q&A message with the same outcome. The run row is the serialization
fence for concurrent terminal writers; existing terminal compare-and-set
validation selects the winner. A conflict rolls back the entire terminal
transaction. Replay after terminal commit reads the canonical disclosure and
does not repeat retrieval or generation.

Accepted-answer delivery is an idempotent post-commit action keyed by the
persisted disclosure identity. Before delivery, the application re-reads the
message through current authorization and verifies its content-free accepted
presentation against the immutable disclosure. Delivery failure can retry the
same disclosure and cannot replace the canonical outcome. Temporal stores only
a content-free terminal acknowledgement in the new activity-result version;
the v1 decoder remains for already-recorded histories.

## Consequences

- #469 owns the concrete production capability, dependency wiring, durable
  invocation, bounded terminal readback, and safe capture handoff.
- #450 owns the observer adapter and validated observation/execution records;
  it does not own Q&A execution composition.
- #452 owns benchmark orchestration and readiness preflight; it does not own
  Q&A composition or projection writes.
- #478 owns the production observer adapter, real application-contract
  composition checks, canonical identity and terminal/capture/disclosure
  validation, retry/cancellation safety, fail-closed behavior, and deterministic
  composition tests. It does not own final opt-in end-to-end qualification.
- #480 owns connecting the observer to the deterministic runner and adapting
  metadata-only reports to v2; it does not own final production-runtime
  qualification.
- #453 owns the final operator-triggered opt-in end-to-end qualification through
  the integrated #480 runner and configured production composition. Fake tests
  never qualify production runtime.
- #469 specifies the bounded waiter and `CaptureChannel` port, schemas,
  lifecycle, and deterministic failure semantics. Runtime authentication and
  deployment security configuration remain operational prerequisites owned by
  the runtime owner.
- No private-data access, runtime readiness, or real benchmark qualification is
  implied by this architecture approval.

## Related decisions

- ADR-0038 — Atomic Q&A Ask admission.
- ADR-0039 — R1 real benchmark runtime identity and observation boundary.
- ADR-0019 — Grounded answer disclosure contract.
- ADR-0020 — Canonical audit and diagnostic separation.
- ADR-0035 — Resumable Q&A event contract.
