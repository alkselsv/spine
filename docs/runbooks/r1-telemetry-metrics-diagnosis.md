# R1 telemetry and metrics diagnosis without server-log access

**Contracts:** O01 safe `OperationalObservation:v1`, O02 metrics, and O07
operational correlation.

**Production qualification: not approved.** R1's external telemetry exporter is
disabled; no vendor trace UI or backend is approved by this runbook.

## Prerequisites

- An authenticated administrator session scoped to the workspace/environment,
  a UTC half-open query window `[start, end)`, and an incident reference.
- The approved metrics reader and correlation reader are composed for this
  deployment. Deployment refresh interval, 90-day metrics retention, and
  administrator access enforcement are verified by their owner.
- A trusted opaque trace, delivery, or run reference from an authorized source.
  Do not accept arbitrary cross-tenant identifiers as evidence that a record
  exists.

If a reader is absent, unavailable, stale, or degraded, report that state; do
not fall back to application logs, database exports, or a provider dashboard.

## Permissions

Use the administrator role and trusted workspace/environment headers through
the authenticated Control Plane client. Metrics and correlation reads are
tenant-scoped and audited. Trace/run/message IDs are correlation references
only, never metric labels or query aggregation dimensions.

## Procedure

1. Query `GET /api/v1/metrics/operational` with `window_start` and `window_end`
   in UTC. Request only needed enumerated measurements. The window is
   half-open; preserve exact currency per cost measurement.
2. Read measurement values with coverage and freshness. `unavailable` is not
   zero; `stale` means refresh has exceeded five minutes; `degraded` means
   partial/known incomplete coverage. Do not infer missing observations,
   answer quality, or product thresholds.
3. When a trusted reference connects a failure to a run, call
   `GET /api/v1/diagnostics/operational-correlation` with its `reference_kind`
   and `reference_id`. Review source/version IDs and coverage only. #302 remains
   run authority and #298 remains the only usefulness provenance.
4. Compare source identities, schema versions, UTC timestamps, retries versus
   logical outcomes, and missing source categories. Trace IDs and references
   must not be introduced as metric dimensions.
5. If telemetry is unavailable, record the safe status and stop. Do not open
   raw logs, spans, payloads, prompts, answers, comments, source excerpts, or
   exception details to complete the diagnosis.

Use the authenticated Control Plane client; it supplies the short-lived
credential and trusted `X-Workspace-ID` / `X-Environment-ID` context. Do not
put credential values in commands, tickets, or evidence.

## Evidence

Record query window, schema versions, measurement names, coverage/freshness,
source/version identities, safe correlation reference kind/ID, aggregate
result, authorization/audit reference, UTC read time, and incident/change ID.
Keep records immutable and content-free. Never record credentials, raw
provider errors, exception messages, stack traces, metric labels outside the
closed vocabulary, protected content, comments, or copied log/span payloads.

## Stop and escalate

Stop on 401/403, cross-scope reference, missing/untrusted lineage, schema
mismatch, unavailable reader, stale/degraded data beyond the approved
interpretation, or conflict between canonical source and projection. Preserve
the safe status and evidence IDs; escalate to telemetry, metrics, or security
owners. Never change retention, sampling, audit, or exporter configuration to
silence the result.

## Recovery boundary

Observations and read models are diagnostic, not canonical audit, run, feedback,
or source evidence. Telemetry sink failure cannot make a successful business
operation fail; required Audit Event failure remains fail-closed. R1 has no
approved exporter/backend, and this procedure cannot establish production
retention or observability readiness by itself.

## Exercise

Run the deterministic metrics/correlation success, denied, stale, degraded,
unavailable, and API response exercises:

```bash
uv run pytest -q \
  tests/application/telemetry/test_operational_metrics.py \
  tests/application/test_operational_correlation.py \
  tests/api/test_operational_metrics.py \
  tests/api/test_operational_correlation.py \
  tests/application/diagnostics/test_events.py
```

These fixtures contain synthetic operational facts only. They prove safe
contract behavior, not access to a production telemetry backend or server logs.
