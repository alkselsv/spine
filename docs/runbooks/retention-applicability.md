# Retention applicability migration and rollback

`RetentionApplicability:v1` is an offline, provider-neutral authority binding.
It records which already approved retention policy applies to an exact source or
approved source class and to which source-bearing artifact classes. It does not
define a duration, authorize cleanup, create a tombstone, or grant disclosure.

## Additive rollout

1. Apply migration `20261221_89` while retention-applicability writers remain
   disabled. The migration adds immutable history and a tenant-scoped current
   pointer; it does not backfill or infer policy bindings.
2. Configure an authority adapter that verifies the exact policy
   ID/version/digest, applicability evidence and approving authority in the same
   PostgreSQL unit of work used by the pointer CAS.
3. Populate bindings only from separately approved Data/Portal or
   Legal/Compliance decisions. Missing bindings stay missing; timestamps,
   diagnostics and existing `retention_eligible` booleans are not authority.
4. Enable readers before any writer or downstream enforcement. A reader must
   return a single current, effective binding for every exact SourceRevision in
   an artifact lineage. Missing, ambiguous, conflicting, not-yet-effective,
   expired or unavailable authority fails closed.
5. Downstream projection, retrieval, citation and final-disclosure integration
   remains a separate guarded rollout. Physical cache cleanup is never the
   authorization boundary.

## Safe failure and recovery

- An applicability write, its safe audit record, outbox intent and current
  pointer CAS commit atomically. Expected-current conflicts are retried only
  after resolving the new current binding; they are never overwritten.
- Immutable originals, parsed content, transcripts, projections, cached answers
  and citations resolve through exact SourceRevision lineage. A mixed artifact
  with one ineligible source is suppressed or rebuilt without that source.
- Legal-hold references are retained separately. A hold constrains a future
  physical-deletion workflow but does not restore retention eligibility or grant
  ordinary disclosure.
- No provider call, live Bitrix24 operation, tombstone or physical cleanup is
  part of this rollout.

## Rollback

Disable applicability writers first, then disable consumers. Keep the v1 reader
and both tables while any audit, outbox or artifact lineage refers to a binding.
Application rollback must tolerate and preserve v1 rows; it must not select an
older binding or recreate the legacy content-kind boolean as authority.

The Alembic downgrade is permitted only for an unactivated/offline deployment
whose retention tables have no required history. It drops the current-pointer
table before immutable history and removes only the v1 trigger functions. For an
activated environment, recover by deploying a reader-compatible build and
issuing a new authorized binding through CAS; never update or delete history.
