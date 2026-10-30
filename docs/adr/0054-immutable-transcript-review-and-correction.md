---
status: accepted
---

# Immutable transcript review and correction

## Context

An ASR transcript is probabilistic evidence and may be incorrect. Reviewers need
to decide against the exact transcript version they inspected, while preserving
the original ASR output and the authority and policy behind the decision.
Rewriting ASR segments in place would invalidate citations and make audit and
replay ambiguous. A review decision also must not silently choose the product's
mandatory-review policy.

## Decisions

1. A review record pins the workspace, environment, source revision, transcript
   artifact ID and digest, reviewer identity and authority, decision, rationale,
   policy version, correlation ID and decision time.
2. Reviews are append-only and tenant-scoped. One decision owns one exact ASR
   artifact version. An exact actor-scoped idempotency replay returns the first
   result; a different decision against that version receives a conflict.
3. A correction creates a `CorrectedTranscriptArtifact` with its own identity,
   digest and segments, linked to both the exact ASR artifact and review record.
   The original ASR artifact and its citations remain unchanged.
4. An injected versioned policy determines publication eligibility. The review
   application service does not encode whether review is mandatory. A rejection
   is always ineligible for publication.
5. PostgreSQL stores review and correction records as immutable tenant-scoped
   rows under RLS. The API derives reviewer identity and authority from protected
   request context; callers cannot claim reviewer roles in the request body.

## Consequences

- Stale artifact digests fail before a review is recorded.
- Competing decisions cannot overwrite each other, and a retry after a lost
  response has one stable outcome.
- Corrected text has explicit lineage and can be considered by publication
  policy as a distinct artifact.
- A production deployment must inject its approved review policy configuration.

## Source

Ticket #681 under the R2 audio transcription specification (#656).
