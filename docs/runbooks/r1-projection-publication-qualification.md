# R1 composed projection publication qualification

This runbook exercises Issue #562 against the retained, operator-approved
Q16-01/R4 target. PostgreSQL remains canonical; Cognee, Neo4j and PGVector are
derived projection state.

## Stop gate before provider work

Verify the committed #560 and #561 reports refer to the same commit, deployment
fingerprint, Workspace/Environment, authorization generation and provider
profile digest. Read projection status through the authenticated API with the
dispatcher stopped. Startup/readiness must make no provider call.

Before any physical execution, require a fresh, attributable #562 authorization
that names the run, exact target/profile digest, scope, secret references,
request/concurrency/token limits, timeouts, retry/deadline limits, currency and
spend cap, and expiry. An authorization recorded for an earlier qualification
is not silently carried forward. If it is absent, expired or mismatched, **do not start the dispatcher**; record `NOT_VERIFIED`.

## Required observation inventory

One coherent run must record all of these bounded scenarios:

- `canonical_uow_separation`: canonical resolution commits before physical I/O,
  and receipt persistence uses a later canonical transaction.
- `exact_revision_membership`: the shadow candidate contains exactly the
  current accepted R4 revisions and their canonical locators.
- `physical_validation_and_sealing`: every partition passes physical validation
  before the candidate is sealed.
- `activation_generation_cas`: PostgreSQL generation/CAS alone publishes the
  sealed candidate.
- `failure_before_validation`: a controlled failure leaves the active
  publication unchanged.
- `failure_before_activation`: a controlled post-validation failure leaves the
  active publication unchanged.
- `stale_activation_cas`: a concurrent stale activation loses CAS and cannot
  replace the winner.
- `changed_source_head`: stale membership cannot be presented as the exact
  current candidate; the current head requires a new canonical request.
- `tombstone_disclosure_fence`: the current tombstone prevents ordinary
  retrieval even if stale physical artifacts remain.
- `rebuild_failure_preserves_active`: a failed rebuild preserves the last valid
  active publication.
- `successor_rollback`: recovery publishes a separately validated successor and
  never rewinds an old pointer.
- `workspace_namespace_isolation`: foreign workspaces and shadow namespaces
  remain excluded during success, failure and recovery.
- `safe_diagnostics`: request, receipt, candidate, snapshot, generation and
  recovery references correlate without content, credentials, endpoints,
  filenames, provider payloads or raw exceptions.

Use only the deployed outbox/worker, validation/sealing and PostgreSQL CAS
boundaries. A failure may be injected only through a repository-owned,
scope-bound qualification mechanism approved for the exact target. Never edit
canonical rows, active pointers or provider storage manually. If a required
boundary cannot be injected safely, leave that scenario `not_verified` and stop
rather than substituting a unit test.

## Acceptance

Validate the resulting metadata-only artifact with
`ProjectionPublicationQualificationReport`. PASS requires exact prerequisite
and provider compatibility plus one unique passed observation for every
scenario above. A confirmed invariant violation is FAIL even when other
evidence is missing. Missing or incompatible provider evidence and unexercised
scenarios remain NOT_VERIFIED.
