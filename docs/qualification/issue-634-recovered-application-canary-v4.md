# Issue #634 recovered application canary v4

Status: **BLOCKED / NOT_VERIFIED**.

The run-specific authorization was recorded locally before the provider request;
publication of that authorization as a GitHub comment was not authorized. The
metadata record is preserved in the companion JSON. It binds commit
`fb80cf52b24d392e9dc557b26ab1eb54149b5351`, target `spine634canary004`,
destination `api.vsellm.ru/v1`, profile `cognee-approved-production`, USD 1.00,
concurrency 1, at most 1000 provider requests, 1,000,000 input tokens and 100,000
output tokens, 120 seconds per request, at most two internal retries, a
3600-second run deadline and hard expiry `2026-10-28T15:30:00Z`. It permits one
projection consumer invocation and at most one Context Broker request using
synthetic data, without serving or cutover.

The separately authorized synthetic run restored the verified Issue #509
recovery set into the fresh isolated `spine634canary004` target at commit
`fb80cf52b24d392e9dc557b26ab1eb54149b5351`. Archive integrity, application
ownership and ACLs, forced-RLS posture, PostgreSQL TLS, migrations and normal
runtime readiness passed.

Before any provider or Context Broker call, the exact production profile
repository resolved the server-approved stored reference. The preflight made
zero provider and zero Broker requests. Cognee relational and vector setup was
completed locally before the production projection consumer was invoked. An
initial runner setup attempt failed closed before entering the consumer or
provider boundary; the setup was corrected before the sole consumer invocation.

Exactly one provider-backed production consumer invocation was made. It entered
the Cognee `add` path and failed closed there with a bounded `TimeoutError`. The
run did not produce an external provider request-count observation or evidence
that an external provider accepted a request. The public safe outcome was
`projection.request.physical_retryable`. This is the exact physical substage
that could not be retained safely by canary v3. No retry beyond the profile's
bounded internal retry policy and no second consumer invocation occurred.

The target retained one open projection run and committed no projection receipt,
snapshot, activation, active publication, source projection reference or
reconciliation result. Because no active publication existed, no Context Broker
request or temporary qualification authority was created. Context Bundle count
remained zero.

Cleanup revoked runtime and public connection access, removed the temporary
Cognee database and role, rotated all temporary authentication material, and
stopped the PostgreSQL target. No graph container or graph volume was created.
Raw provider diagnostics and temporary logs were removed after allowlisted safe
outcome extraction. No serving, cutover or customer data was involved.

The remaining blocker is the reproducible timeout in the Cognee `add` stage
within the approved timeout and retry boundary. Normal publication and
reconciliation, followed by the single authorized Context Broker canary and its
isolation, policy, publication-pin and source-revision-lineage checks, remain
unverified. Independent final evidence review is still required before any
qualification claim.
