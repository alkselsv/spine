---
status: accepted
---

# Immutable governed transcription artifacts

## Context

Audio is an immutable source original, while speech recognition is a
reproducible but replaceable derived processing result. Treating transcription
as another proposal run, a mutable source revision, or an unversioned document
would mix operational truth with model output and make replay or later
retranscription ambiguous.

## Decisions

1. A current, injected processing-authority decision is required before model
   disclosure. An allowed tenant-scoped `TranscriptionJob` pins that decision,
   the exact audio Source Revision, original digest, processing profile, STT
   adapter, model configuration and idempotency identity. It is source/artifact
   infrastructure and is never a WorkItem, WorkflowRun or Q&A Session.
2. PostgreSQL owns the durable job and its typed completed, failed or cancelled
   terminal outcome. Exact replay returns the existing logical job. A bounded
   execution lease gives one worker ownership at a time and lets a later replay
   reclaim an expired pending attempt; a stale worker cannot finalize it.
   Completion persists the artifact and all segments atomically.
3. A completed job creates one immutable, digest-addressed
   `TranscriptionArtifact`. Each `TranscriptSegment` has integer millisecond
   bounds on the original recording timeline and retains only optional signals
   emitted by the pinned adapter.
4. The audio Source Revision, ASR artifact and any later requirements,
   summaries or other business interpretations remain distinct records with
   explicit lineage. Transcription does not publish a projection, call Cognee
   or grant evidence eligibility.
5. The provider-neutral Speech-to-Text port receives only an opaque immutable
   recording handle and pinned configuration. Production provider selection,
   media decoding/chunking, publication and retrieval remain separate policy
   and adapter decisions.
6. A `TranscriptionTimeRangeLocator` pins millisecond offsets to the exact
   Source Revision identity and immutable transcription artifact identity and
   digest. Resolution is tenant-authorized, revalidates both lineages and
   returns an opaque navigation target on the original audio; it does not make
   the transcript eligible for publication by itself.

## Consequences

- Retranscription under another profile or model creates another immutable
  artifact rather than changing prior evidence.
- A pending job can be resumed after its execution lease expires; repeated or
  stale completion cannot create a second logical artifact.
- Publication, current authorization and time-based evidence locators are
  downstream contracts and do not weaken the canonical processing boundary.

## Source

Ticket #663 under the R2 audio transcription specification (#656).
