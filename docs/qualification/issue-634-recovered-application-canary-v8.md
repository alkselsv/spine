# Issue #634 recovered application canary v8

Status: **BLOCKED / NOT_VERIFIED**.

Run008 used the separately authorized synthetic recovery set on isolated target
`spine634canary008`, commit
`8860651013991f093dc6cd4bccd320cd30214153`, and the approved
`cognee-approved-production` profile. The authorization retained the run007
provider, spend, request, token, timeout, retry, consumer, authority and Broker
limits, with hard expiry `2026-10-29T18:00:00Z`. Authorization publication was
not requested.

The restored target was migrated from revision 69 to current revision 71 before
the run. PostgreSQL TLS, runtime readiness and the exact production profile
resolver passed. Six setup attempts failed closed before the consumer and made
zero provider calls. Content-free diagnostics identified and corrected the
local asyncpg TLS argument and provisioned the pgvector extension through the
operator boundary. The disposable Cognee role remained `NOSUPERUSER`,
`NOCREATEDB` and `NOCREATEROLE`; `pgvector_shared` remained selected.

The sole authorized production projection consumer invocation then failed at
its first request-envelope validation. The diagnostic context carried the
outbox intent's upstream causation identifier, while the consumer contract
requires the current projection event identifier. The consumer returned the
bounded terminal reason `projection.request.invalid` before canonical
resolution, physical projection or any provider call. The invocation budget
was therefore exhausted and no retry was made.

The target still contains its one pre-existing open projection run and no
receipt, snapshot, activation, active publication, source projection reference,
reconciliation result or Context Bundle. No temporary qualification authority
was created and no Context Broker request was made.

Cleanup revoked runtime and public connections, rotated or destroyed six
temporary authentication materials, removed the disposable Cognee database and
role, removed sensitive logs and local Cognee state, and stopped PostgreSQL.
No graph resource was created. No serving, cutover, customer data, direct
canonical edit or active-pointer edit occurred.

The next run must construct the projection consumer diagnostic context with
`causation_id=event_id` before entering the one-shot consumer. It requires a new
run-specific authorization because run008's single consumer invocation was
used. Provider-backed publication, the single Broker canary and its isolation,
policy, publication-pin and lineage checks remain unverified. Independent final
evidence review is still required.
