# ADR 0048: Revalidate requirements evidence at disclosure time

Status: Accepted

## Context

Requirements extraction produces immutable candidate artifacts. An evidence
reference in such an artifact identifies a source object, exact source revision,
parsed artifact and format-specific locator, but it must not grant permission to
read the referenced content. Source revisions can become stale or tombstoned,
and a user's access can be revoked after the artifact is created.

## Decision

Requirements evidence is resolved through the canonical source, provenance and
parsed-artifact repositories, then authorized through the existing current
human-disclosure policy for the requested workspace, environment, purpose and
operation. The evidence inspector returns protected text only when every exact
identity, digest, locator, current-revision and authorization check succeeds.

Unavailable, denied, stale, tombstoned, fabricated and degraded outcomes are
content-free and do not expose cached excerpts or protected metadata. Candidate
artifacts retain immutable validation status and decision references when an
application validation pass is performed; source facts and artifact lineage are
never rewritten or silently moved to a newer revision.

The web artifact card opens this inspector through the server-authorized API
path and renders unavailable outcomes as safe status rather than falling back to
stored text.

## Consequences

- Evidence references remain useful for review and audit without becoming an
  authorization bypass.
- Every protected disclosure pays the cost of current source and policy checks.
- Rebuilds or policy changes can invalidate a candidate without mutating the
  source record or replacing its revision reference.
