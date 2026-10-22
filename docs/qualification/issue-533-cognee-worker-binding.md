# Issue #533 Cognee projection worker binding

Status: deterministic configuration and integration evidence only. This is not
physical Cognee qualification and does not authorize provider connectivity or
spend.

## Bound composition

The outbox dispatcher loads one strict `cognee-projection-binding:v1` document
from `SPINE_COGNEE_WORKER_BINDING_JSON`. The document contains the approved
`CogneeWorkerProfile`, its independent approval and an observed runtime
identity. Unknown fields, invalid digests, unapproved or mismatched profile
bindings, and runtime identity drift fail startup with bounded reason codes.
Rejected configuration values are not included in the error.

The production root wraps `CogneeContextProjection` with the profile guard
before passing it to
`build_postgresql_cognee_projection_delivery_binding`. The same guarded
projection therefore handles ordinary projection and recovery rebuilds. The
delivery binding reuses the canonical PostgreSQL resolver, parsed-artifact
locator resolver, Cognee receipt builder, result persistence bridge and recovery
lifecycle. Canonical resolution closes its UnitOfWork before the guarded
physical call; result persistence uses a later UnitOfWork.

Profile presence is not invocation authority. Each physical command still
requires an injected external-secret reference resolver and an independently
approved, workspace/environment/run/profile-scoped budget authorization. The
deployment must also inject a provider-call executor that applies the profile's
request, concurrency, token, timeout, deadline and retry limits at the physical
provider seam. If any dependency is missing, invalid or unavailable, the guard
returns only bounded refusal codes and does not call Cognee. Startup and
configuration validation do not resolve secrets or call a provider.

## Evidence scope

Deterministic tests cover strict configuration loading, missing and invalid
settings, runtime mismatch, approved invocation, unavailable budget state,
zero provider calls on refusal, and production-root selection of the combined
projection/recovery route. Existing canonical request, receipt, currentness,
tombstone, isolation, idempotency, rollback and recovery suites remain the
authority for those invariants.

No real secret resolver, paid-run authorization, provider call, deployed #507
R4/R6 result or #509 recovery result is supplied or claimed by this ticket. No
approved provider-specific limit executor exists yet, so the executable default
remains fail-closed with `provider_limits_unavailable`; this is configuration
and route evidence, not deployed provider readiness.
