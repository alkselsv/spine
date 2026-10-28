# Issue #634 recovered application canary v6

Status: **BLOCKED / NOT_VERIFIED**.

Run006 used the separately authorized synthetic recovery set on fresh target
`spine634canary006`, commit
`62024024a7d2d74ed2f636469ec302bff083adc7`, and the approved
`cognee-approved-production` profile. The local authorization bounded the
provider destination to `https://api.vsellm.ru/v1`, spend to USD 1.00,
concurrency to one, provider traffic to 1000 requests, and the execution to one
projection consumer invocation. Authorization publication was not requested.

Restore integrity, PostgreSQL TLS, typed migrations, runtime readiness and the
server-approved profile preflight passed. One setup target failed before the
consumer because the read-only recovery archive was not readable by the
container's `postgres` user. That partial target was removed; the archive was
copied to a container-local file with bounded permissions before the final
fresh restore. The offline profile preflight made zero provider and zero Broker
requests.

Exactly one production projection consumer invocation ran with Cognee relational
and vector pools configured as `pool_size=5`, `max_overflow=0`, while provider
concurrency remained one. The prior 280-second pool timeout did not recur.
Cognee completed relational bootstrap and emitted two endpoint connection-test
phases, so the external provider boundary was reached. The run did not include
an independent HTTP counter; it therefore does not claim an exact provider
request count, only that the authorized request limit was not observed to be
exceeded.

The physical `add` stage then failed closed with the public reason
`projection.request.physical_retryable`. PostgreSQL recorded the exact next
boundary: Cognee attempted to create a per-dataset database and its narrow role
did not have `CREATEDB`, producing `permission denied to create database`. This
is a least-privilege provisioning gap, not the run005 pool exhaustion. Granting
`CREATEDB` ad hoc was outside this run's authorization and would broaden runtime
authority, so the consumer was not retried.

The canonical target contains one open projection run and no receipt, snapshot,
activation, active projection, source projection reference or reconciliation
result. No publication existed, so no Context Broker request or temporary
qualification authority was created.

Cleanup revoked runtime and public connections, rotated six credentials,
removed the temporary Cognee database and role, stopped the PostgreSQL target,
and retained only its evidence volume. The one graph container and one graph
volume created during the failed attempt were removed, restoring the pre-run
graph inventory. Raw Cognee state and logs were deleted. No serving, cutover,
customer data or direct canonical/active-pointer edit occurred.

The next implementation step must provide a repository-owned, least-privilege
operator seam for Cognee's deterministic dataset database before runtime `add`,
or configure Cognee to use the already provisioned database without database
creation. Normal provider-backed projection, publication, reconciliation, the
single Broker canary and the policy/isolation/lineage checks remain unverified.
