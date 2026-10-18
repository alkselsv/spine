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
specific admitted command/run and the existing Q&A terminal outcome. Protected
answer/citation content is returned only from persisted state after disclosure
is revalidated under current authorization. Mismatched identities, incomplete
persistence, authorization changes, and unavailable readback fail closed; they
do not become fabricated model outcomes. D5 defines the readback contract.

### D4 — same-execution typed capture

The observer's typed capture originates from the same authorized execution and
contains `ContextResponse`, `ProjectionRetrievalResult`, `RetrievalResult`,
`ContextBundle` and lineage, answer/citations, and verified disclosure. It must
not trigger another retrieval or reconstruct missing values from run history
or gold labels. D6 defines the transport and lifecycle contract.

### D5 — bounded canonical terminal readback

The application waiter is read-only and bounded. It polls canonical admission
state through `AskAdmissionApplication.status()` with an injected monotonic
clock, sleeper, and deadline. It distinguishes admitted/pending, running,
terminal success, terminal failure/cancellation, and infrastructure
unavailability. It validates `command_id`, `run_id`, `session_id`, `message_id`,
`workspace_id`, `environment_id`, and requester identity, then reads the exact
message through authorized `QASessionService.get_message()` and current
disclosure revalidation, including the current authorization generation.
Terminal persistence can race capture publication:
neither alone is sufficient. Freeze observation only after terminal read,
matching capture, and current disclosure all succeed. Timeout ends only the
observer wait, does not cancel the workflow, and does not use
`wait_for_cancellation()` for polling. Missing or mismatched data is never
substituted from history; infrastructure read failures remain unavailable,
not model outcomes.

### D6 — authenticated ephemeral typed-capture handoff

The application `CaptureChannel` registers before admission and delivers the
versioned `r1-qa-observer-capture:v1` typed envelope from the authorized worker
to the observer over an authenticated, ephemeral in-memory channel for the
separate-process topology. Strict correlation binds command, run, session,
message, workspace, environment, requester, attempt, and capture-routing
identity. `trace_id` is a valid routing key only when uniqueness per case is
proven; otherwise use an opaque `capture_id`. Authorize each subscription for
its exact expected execution and trusted scope. Bound buffering and backpressure,
apply a TTL, acknowledge once after schema and identity validation, handle
matching duplicate delivery idempotently, and clean up on acknowledgement,
timeout, or expiry.

Worker restart, expired or mismatched delivery, observer disconnect,
backpressure, and capture loss fail observation closed. They do not alter the
canonical Q&A outcome, cancel the workflow, or trigger another retrieval. Do
not put protected capture payloads in Temporal history, logs, benchmark-only
durable storage, or a second copy in canonical Q&A persistence. The runtime
owner supplies and operates deployment topology, workload credentials,
authenticated transport settings, and channel security configuration; this
approval does not assert that they are deployed.

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
contract. Put the non-negative `worker_elapsed_ns` scalar in the ephemeral
execution/capture envelope with its activity-attempt identity; do not transmit
worker timestamps.

## Consequences

- #469 owns the concrete production capability, dependency wiring, durable
  invocation, bounded terminal readback, and safe capture handoff.
- #450 owns the observer adapter and validated observation/execution records;
  it does not own Q&A execution composition.
- #452 owns benchmark orchestration and readiness preflight; it does not own
  Q&A composition or projection writes.
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
