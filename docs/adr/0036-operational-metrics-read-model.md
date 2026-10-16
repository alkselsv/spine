# ADR-036: Tenant-scoped operational metrics read model

Status: accepted

## Context

Operational observations are diagnostic evidence, not canonical operational
truth. Operators nevertheless need deterministic, tenant-scoped measurements
over approved O01 observations, canonical Q&A run/usage facts, and immutable
#298 feedback. The metrics contract requires explicit provenance, UTC windows,
deduplication, coverage state, 90-day retention, and exact currency handling.

## Decision

Spine stores safe metric source events in a PostgreSQL
`operational_metric_events` read-model table. The table is tenant-scoped with
forced RLS, is insert-only for the runtime role, and has a unique scoped
deduplication key. The application reads it through the `OperationalMetricsStore`
port and performs deterministic aggregation into versioned measurements.

Answer-rate facts are accepted only through the canonical Q&A run contract;
cost facts are projected from canonical `AnswerQuestionUsage`; usefulness is
projected only from immutable feedback records. Currency values remain separate
measurements and are never implicitly converted. Missing data is represented as
`unavailable`, not as zero. A configured refresh interval determines `stale`
after the approved five-minute grace period; partial source coverage is
`degraded`, and authorization denial is a separate safe failure.

## Consequences

- The metrics projection can be rebuilt from retained source facts and replayed
  idempotently by deduplication identity.
- The read model cannot replace canonical runs, usage, feedback, audit events,
  or O01 observations.
- Deployment composition must provide the approved refresh interval and the
  tenant-scoped authorization context.
- Aggregation queries must enforce the 90-day retention cutoff and half-open
  windows.
