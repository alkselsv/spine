---
status: accepted
---

# Bounded, partial and cancellable transcription processing

## Context

ADR-0049 established immutable transcription jobs and artifacts, but a single
whole-recording model call cannot safely handle long, malformed or expensive
media. Filename extensions and caller-declared duration are not trustworthy
resource controls. A worker also needs an exact durable outcome when only some
chunks completed or cancellation arrives during processing.

## Decisions

1. A versioned `TranscriptionProcessingProfile` governs inspected media type,
   codec, byte length, duration, decoded bytes, peak memory, working bytes,
   decode time, chunk count, overlap and model-attempt limits. A
   media-preparation adapter inspects and
   decodes the immutable recording behind an opaque handle; the application
   validates its result before any Speech-to-Text invocation. Timed-out or
   failed preparation is aborted through the port before terminalization.
2. Chunk boundaries are a pure deterministic function of the inspected
   duration and pinned profile. Every chunk has a digest-derived stable
   identity, original-timeline bounds and explicit preceding overlap. The
   artifact pins decoder and reassembly identities, inspected facts and a
   receipt for every completed chunk.
3. Speech-to-Text retries reuse the same chunk identity. A retryable failure is
   bounded by the profile; exhaustion records both `retry_exhausted` and the
   originating safe cause code. Completed chunks and their segments become an
   immutable `partial` artifact rather than an implicitly completed result.
4. Cancellation intent is durable on the pending `TranscriptionJob` and is
   checked before each attempt and immediately after each completed chunk. If work already completed, a
   cancelled job retains a partial artifact; otherwise it has a terminal
   cancelled receipt without an artifact. Repository finalization gives an
   already-recorded cancellation atomic precedence over completion. Replays
   return that same outcome.
5. Prepared decoder output is released on every completed, partial, failed or
   cancelled application path. Empty, silent, corrupt, unsupported,
   source-oversized, decoded-oversized and no-speech inputs create no artifact.
   Transcription processing still cannot publish Context Projection or
   requirements evidence.
6. The bounded command, chunked Speech-to-Text request/result, job and artifact
   are explicit v2 successors. Existing whole-recording v1 commands, jobs and
   artifacts retain their original schema and deterministic identities.
7. The existing transcription job is the sole owner of durable chunk recovery.
   After each completed model window it atomically stores a cumulative typed
   checkpoint behind the current execution lease. A reclaimed worker validates
   the pinned source, profile, decoder, decoded bytes and reassembly identity,
   then resumes after the contiguous receipt prefix. Stale workers cannot
   checkpoint or finalize. Completed v2 artifacts require full original-timeline
   chunk coverage; incomplete coverage remains `partial`, never `completed` or
   a new `suspended` terminal state.
8. The processing outcome and #660 governance outcome are separate axes.
   `partial` is an immutable diagnostic processing result with exact completed
   chunk coverage and is never publishable in the first slice. #660 `SUSPEND`
   means current policy, authorization or source eligibility cannot permit an
   operation; it fences processing/publication/retrieval and may later be
   re-evaluated, but it is not persisted as a terminal `TranscriptionJob`
   state. A processing job therefore remains completed, partial, failed or
   cancelled while current use can independently be suspended or denied.

## Consequences

- Long synthetic recordings are reproducible across retries without requiring
  a concrete decoder or ASR provider in the domain layer.
- PostgreSQL persists cancellation intent and complete bounded-processing
  provenance atomically with the terminal job and artifact.
- Cancellation is cooperative between model invocations. A provider call that
  is already executing must return or honour its own cancellation mechanism
  before the worker can observe durable intent.
- A later adapter qualification must prove its actual decoder sandbox, timeout,
  memory, disk and cleanup guarantees against the same profile contract.

## Source

Ticket #668 under the R2 audio transcription specification (#656).
The durable recovery and evidence-navigation completion is ticket #688; it
extends this same orchestration path rather than adding a second workflow.
