# Issue #634 recovered application canary v1

Status: **NOT_VERIFIED**.

The authorized run restored the verified `recovery:issue509-005` archive into a
fresh isolated PostgreSQL target. Archive integrity, application ownership and
ACLs, forced-RLS posture and TLS runtime access passed. A short-lived service
authority was created for the exact `answer_question` / `retrieve_context`
scope.

The offline production-profile preflight resolved the one server-approved
profile through the production repository and proved an exact reference match.
It made no provider or Context Broker requests.

The provider-capable runner then failed closed before Cognee migrations and
before the production projection consumer. The complete runner resolved a stale
database binding while isolated configuration preflights resolved the fresh
target. The final guarded execution stopped with
`cognee_runtime_binding_mismatch`. No external provider was reached, no
projection receipt, snapshot, activation or active publication was created, and
the single Context Broker request was therefore not issued.

Cleanup revoked the temporary authority with terminal version 2, revoked
runtime and public database connection access, removed the temporary Cognee
database and role, and rotated all temporary database authentication material.
No task-owned graph container or volume had been created. The PostgreSQL target
was stopped. No serving, cutover or customer data was involved.

A subsequent run needs a separately authorized fresh target after the full
runner's Cognee configuration lifecycle is made deterministic. It must still
verify reconciliation and runtime readiness; provider-backed projection and
active publication; the single authorized Context Broker result; workspace and
environment isolation; current policy enforcement; the publication pin; and
source-revision lineage. Independent final evidence review of any qualification
claim also remains required.
