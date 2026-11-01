# Issue #772: governed private-real ASR runner contract

Status: **offline implementation contract; no real-data or runtime qualification**.
The runner uses synthetic in-memory fixtures in tests. It does not authorize
customer-data access, perform real model inference, select a model, or change
the #661 thresholds. #766 and #770 remain independent human gates.

## Execution boundary

The runner is a separate application service for
`asr-candidate-qualification:private-real:v1`. The existing
`asr-candidate-qualification:v1` synthetic-only evaluator remains unchanged.
The private path accepts a validated frozen #771 `PrivateCorpusManifest` and a
split enum. Its resolver can return a `PurposeAdmissionRequest` only for a
record already present in that manifest. It accepts no path, URL, discovery
query, latest-revision selector, or fallback dataset.

The candidate factory is injected. It is called only after every selected
record has passed metadata admission, exact-revision content verification, and
annotation admission, and the governed preparation port has produced and
validated all ephemeral chunk handles for the eligible records. The preparation
port receives only an opaque `AudioRecordingHandle`, its positive stage-two
`ContentVerificationResult`, the frozen processing profile, and trusted context;
it must resolve the verified staged reference, verify decoder output, and clean
partial staging on failure or cancellation. The application has no path, URL,
or filesystem fallback. Its runtime boundary accepts only
`SpeechToTextRequestV2` and `SpeechToTextResultV2`; V1 results are unavailable.
The concrete adapter factory remains injected and can wrap the existing
`SpeechToTextPort`; GigaAM and Whisper modules or model assets are not loaded
until all source, host, and decoded-chunk checks pass.
The selected candidate ID must equal the frozen configuration's candidate ID;
each provider result must echo the configured adapter and model-configuration
references before its metrics are attributed. A mismatch is recorded as an
unavailable case, never as a measurement for another candidate.

## Bounded V2 chunk bridge

The frozen `TranscriptionProcessingProfile` plans deterministic
`AudioChunkBoundary` windows on the original recording timeline. Each
`AudioChunkHandle` is built through the shared `audio_chunk_id` contract from
the verified decoded digest, decoder reference, ordinal and offsets. The
processing profile identity must match the candidate configuration; chunk
windows above 25 seconds are refused. Each V2 request carries the exact
recording handle, chunk handle, Russian language hint, profile, adapter and
model-configuration references.

Only typed chunk-local V2 results with exact candidate identities are consumed.
Segment timestamps must be monotonic and inside the chunk duration. Valid
segments are translated by the chunk's original-timeline start offset; segments
ending wholly inside a later chunk's declared preceding overlap are omitted by
the existing deterministic rule. Invalid bounds, ordering, or identity make
that case unavailable; results are not silently clipped, reordered, or
language-filled from the request hint. The current bridge has no independent
detection-evidence field, and the pinned GigaAM/Whisper configurations do not
perform detection, so a non-null `detected_language` is refused. It does not
become an observation merely because it equals `language_hint="ru"`.
`detected_language=None` remains absence of detection evidence. Result
validation and reassembly happen before any transcript-derived measurement is
attributed.

## Admission and lifecycle order

For every selected record, the orchestrator validates trusted workspace,
environment, actor, purpose and operation; then checks in order: current
evaluation-purpose approval; legal/data evidence; exact immutable source
revision; current AccessPolicy; read grant; process grant; retention; admission
audit; approved environment; dataset/split binding; host/no-egress binding.
After every selected record passes these checks, the runner validates whole-
manifest split isolation and the current #766 Protocol Owner decision, then
verifies host/no-egress binding. Only then does it call #771
`TwoStageSourceAdmission.admit` for each exact record and later
`verify_content`. All selected records pass metadata preflight before the first
content stream. Unknown audio digests remain absent in stage one. Stage two
streams and hashes the exact admitted revision and rechecks source, policy and
authority. The #771 adapter discards partial staging on failure.

The runner rechecks current authority, exact source revision, AccessPolicy and
host/no-egress eligibility before annotation reads, audio preparation, runtime
initialization, every chunk inference, after inference, before private report
acceptance and before aggregate release. Revocation during inference fences the
run and prevents report commit. Cancellation propagates and releases completed
ephemeral preparations; an in-flight synchronous model call may finish before
its cancellation is observed, but its late result is fenced and cannot be
measured or persisted. This in-process runner makes no durable retry or worker
recovery guarantee (#668/#688 remain a separate orchestration handoff).

## Evidence and reporting

The restricted report store receives an authorization callback and must invoke
it after preparing a write and immediately before its atomic commit. The callback
rechecks current authority, exact source revision, AccessPolicy, host/no-egress,
and current report-release authorization; a refusal must leave no report visible.

The frozen private report has separate identities for dataset/manifest,
evaluator, candidate/configuration and report; opaque case references; per-case
dispositions; explicit eligible/admitted/verified/annotated/scorable/measured
denominators; independent lineages; limitations; coverage; retention; and audit
references. It excludes transcript and hypothesis text. It is serialized only
to the injected `PrivateReportStoragePort`. There is no default file path.

All results from the offline/fake-backed runner are `NOT_VERIFIED`; the report
schema rejects a qualification `PASS`. Missing or unverified #766 decision
evidence is refused before any content read; the report sufficiency remains
`NOT_VERIFIED` even when a synthetic decision fixture is admitted. Development and qualification cases are selected
only from their frozen manifest split. Private and synthetic denominators are
never combined here.

A denied, revoked, or unavailable pre-report run raises a content-free
`PrivateEvaluationRefused` with aggregate eligible/admitted/verified/annotated
and terminal outcome counts. This refusal object contains no record references,
content, or model outputs and is not serialized, stored as a report, or exported.
The restriction preserves the zero-report-export rule while retaining the
failure disposition for the trusted caller. Successful reports count denied
and revoked cases as zero because any such refusal aborts that run.

Aggregate release passes through #771 `SanitizedReportBuilder`, which resolves
the current scope-bound approved PrivacyPolicy and enforces its category,
metric, rate and minimum-cell allowlist. The private runner additionally asks
the admission port to reject complementary disclosure risk and to recheck the
current release authorization before its injected exporter is called. If any
gate refuses, the public exporter is not called.

## Remaining runtime and governance gates

The offline bridge and its fake-backed contract tests are implemented. Actual
runtime evaluation remains gated. Any later authorized integration must prove
that each concrete GigaAM/Whisper adapter:

- accepts only the provider-neutral request produced after private admission;
- loads only its pinned, already-present local assets and reports exact runtime,
  model, checkpoint, asset and dependency-lock identities;
- has no network egress and cannot resolve arbitrary paths or source revisions;
- returns the typed `SpeechToTextResultV2` contract and does not turn configured
  Russian transcription into detected-language evidence;
- honors cancellation and does not return an eligible result after revocation;
- remains separate from synthetic and development data and does not leak
  transcript text, paths, identifiers or errors into logs/reports.

Those tests must use fakes or controlled synthetic audio unless a separately
authorized operator gate permits an actual model run. Model assets, real
inference, the approved no-egress host, real-record admission, Protocol Owner
sufficiency, and Legal/Data purpose approval remain unverified. No fake-backed
measurement changes qualification from `NOT_VERIFIED`.
