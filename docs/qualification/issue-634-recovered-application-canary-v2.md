# Issue #634 recovered application canary v2

Status: **BLOCKED / NOT_VERIFIED**.

The separately authorized synthetic run restored `recovery:issue509-005` into
the fresh isolated `spine634canary002` target at the authorized commit. Archive
integrity, application ownership and ACLs, forced-RLS posture, PostgreSQL TLS
and normal runtime readiness passed.

Before any provider or Context Broker call, the exact production profile
repository resolved the server-approved stored reference. The preflight
recorded no profile identity, digest, query, source text, credential or raw
exception, and made zero provider and zero Broker requests. This run also
confirmed that the remediated Cognee binding lifecycle selected the fresh
relational, migration, vector and graph configuration in the complete runner.

The restored initial projection intent was then passed to the production
projection consumer. A first local-only invocation exposed that the disposable
Cognee database had not yet been initialized; it stopped before an external
request. After the normal local Cognee schema setup, one provider-backed
invocation reached the approved profile and failed closed in the `add` stage
with a bounded `TimeoutError`. The public safe outcome was
`projection.request.physical_retryable`. No additional provider-backed
invocation was made.

No projection receipt, snapshot, activation, active publication, source
projection reference or reconciliation result was committed. Because no active
publication existed, the Context Broker request was not issued and no temporary
qualification authority was created. Context Bundle count remained zero.

Cleanup revoked runtime and public connection access, removed the temporary
Cognee database and role, rotated the operator, migration, runtime and
dispatcher authentication material, and stopped the PostgreSQL target. The
physical pipeline did not reach graph provisioning, so no task-owned graph
container or volume existed. No serving, cutover or customer data was involved.

The remaining blocker is provider connectivity within the authorized timeout
and retry boundary, followed by normal publication, reconciliation and the
single authorized Context Broker canary with isolation, current-policy,
publication-pin and source-revision-lineage checks. Independent final evidence
review remains required before any qualification claim.
