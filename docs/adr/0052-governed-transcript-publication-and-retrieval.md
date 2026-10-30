---
status: accepted
---

# Governed transcript publication and retrieval

## Context

`TranscriptionArtifact` is immutable ASR output, but successful processing must
not itself make transcript text retrievable. Publication needs an explicit,
versioned policy and a derived textual projection that can be rebuilt without
changing the audio revision or ASR artifact.

## Decisions

1. A `TimeRangeLocator` pins the exact audio `SourceRevision`, transcript
   artifact digest/schema version and millisecond range. It does not float to a
   later retranscription.
2. Publication is an immutable, tenant-scoped projection record. Its injected
   policy may require a human approval and may independently allow or reject
   partial artifacts. Missing or mismatched required approval fails closed.
3. Context Broker is the only transcript text read path. Each read reloads the
   current workspace/environment publication, artifact digest/status and
   source authorization. Revoked, superseded, stale, projection-stale and
   otherwise unavailable records return no segments and preserve their history.
4. Existing parsed-document locators remain unchanged; transcript evidence is
   represented by a separate versioned time-range locator.
5. PostgreSQL stores the publication lineage and derived segments under tenant
   RLS. The physical projection is rebuildable and never becomes operational
   source truth.

## Consequences

- Source revocation after publication immediately prevents disclosure without
  deleting the ASR artifact or publication history.
- Rebuild and rollback can change projection state/version without changing the
  source revision or transcript artifact identity.
- A transcript may be consumed by the existing requirements workflow only after
  it crosses the same authorized Context Broker boundary as other evidence.

## Source

Ticket #669 under the R2 audio transcription specification (#656).
