# Issue #634 recovered application canary v5

Status: **BLOCKED / NOT_VERIFIED**.

The run-specific authorization was recorded locally before the provider request;
publication of that authorization as a GitHub comment was not authorized. The
metadata record is preserved in the companion JSON. It binds commit
`f810127529a9beb1dd2fc75431961a30dbc567b0`, target `spine634canary005`,
destination `api.vsellm.ru/v1`, profile `cognee-approved-production`, USD 1.00,
concurrency 1, at most 1000 provider requests, 1,000,000 input tokens and 100,000
output tokens, 120 seconds per request, at most two internal retries, a
3600-second run deadline and hard expiry `2026-10-28T18:00:00Z`. It permits one
projection consumer invocation and at most one Context Broker request using
synthetic data, without serving or cutover.

The separately authorized synthetic run restored the verified Issue #509
recovery set into the fresh isolated `spine634canary005` target. Archive
integrity, PostgreSQL TLS, migrations and normal runtime/profile readiness
passed. Two local bootstrap attempts failed closed before the consumer or
provider boundary and their targets were removed before retry: the first used an
insufficient role for `vector` installation, and the second exposed an unsafe
role posture to the migration preflight. The final target used the repository's
required role posture and typed migration operator API.

Before any provider or Context Broker call, the exact production profile
repository resolved the server-approved stored reference. The preflight made
zero provider and zero Broker requests.

Exactly one provider-backed production consumer invocation was made. The scoped
Cognee connection-test policy no longer failed at the preliminary connection
probe that blocked canary v4: the consumer proceeded into the physical Cognee
`add` operation. The operation nevertheless timed out within the authorized
timeout/retry boundary and returned the bounded public outcome
`projection.request.physical_retryable`. The run did not retain a provider
request-count observation or evidence that an external provider accepted a
request. No second consumer invocation occurred.

The target retained one open projection run and committed no projection receipt,
snapshot, activation, active publication, source projection reference or
reconciliation result. Because no active publication existed, no Context Broker
request or temporary qualification authority was created. Context Bundle count
remained zero.

Cleanup revoked runtime and public connection access, removed the temporary
Cognee database and role, rotated six pieces of temporary authentication
material (including the one-time Cognee database credential printed by Cognee),
and stopped the PostgreSQL target. No task-owned graph container or graph volume
was created. Raw diagnostics, local Cognee state and temporary logs were removed
after allowlisted safe outcome extraction. No serving, cutover or customer data
was involved.

The remaining blocker is now isolated beyond the connection-test lifecycle fix:
the real Cognee `add` operation still times out within the approved production
profile. Normal publication and reconciliation, followed by the single
authorized Context Broker canary and its isolation, policy, publication-pin and
source-revision-lineage checks, remain unverified. Independent final evidence
review is still required before any qualification claim.
