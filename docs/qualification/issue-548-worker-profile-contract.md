# Issue #548 Cognee worker profile contract

Status: configuration/contract evidence only. This profile is not an approval
to run a Cognee worker or make LLM/embedding calls.

## Contract

`CogneeWorkerProfile` is an immutable `cognee-worker-profile:v1` value. Its
canonical digest binds the profile ID, deployment and release compatibility,
approval reference, approved software/server/model identities, external secret
references, generation settings and all operational limits. The approved
Cognee 1.5.4, PostgreSQL 17, Neo4j/APOC 5.26.32, PGVector 0.8.7,
`openai/gpt-5.4-nano`, and `openai/text-embedding-3-small` identities are
validated separately from Neo4j Python 5.28.6 and Python `pgvector` 0.3.6.
Revision fields and generation parameters are explicit inputs, with requiredness
recorded as part of the profile; none are invented when absent. A profile that
marks one as required cannot omit its value.

Secret configuration accepts `secretref://` references only. The provider-
neutral `SecretReferenceResolver` reports reference state and never returns a
secret value. `guarded_provider_call` checks the profile approval binding,
deployment/release, runtime identity, three secret references and a
workspace/environment-scoped run budget bound to the current run before calling
its supplied operation.
Refusals and resolver/provider exceptions produce bounded codes only.

`profile_configuration_status` is a local, bounded readiness summary. It does
not resolve secrets, initialize Cognee, or call a model/provider. No production
composition or startup path is wired by this ticket; callers must explicitly
pass the validated guard before a provider operation.

Untrusted settings should enter through `load_worker_profile`, which reduces
Pydantic validation failures to a bounded status and count instead of returning
error text that may contain rejected input values.

## Verification scope

The deterministic suite exercises valid and invalid profiles, unknown fields
and schema identities, backend/client/model mismatches, digest and release
binding, missing/invalid limits, references-only secrets, resolver statuses,
run budget and tenant scope, provider-call refusal, and safe diagnostics. A
configuration integration test round-trips the JSON shape through validation.
No real credentials, paid provider calls, Cognee adapter, PostgreSQL mutation,
production invocation, or deployment qualification is claimed.
