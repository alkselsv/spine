# Issue #771: private ASR corpus contract handoff

This document records the offline interfaces delivered for #771. It defines no
recording admission, legal basis, corpus sufficiency, model qualification or
production permission. All test data is synthetic and all ports are fake-backed.

## Contract ownership

- `spine.domain.evaluations.private_asr_corpus` owns the frozen
  `r2-private-asr-evaluation-dataset:v1` manifest, opaque source revision
  bindings, private annotation and lineage contracts, split checks, coverage
  counts and sanitized aggregate schema.
- `spine.application.private_asr_corpus` owns metadata inventory, two-stage
  admission orchestration, current `TrustedPersistenceContext` verification,
  storage lifecycle ports and the fail-closed aggregate sanitizer.
- Existing `SourceObject`, `SourceRevision`, `AccessPolicyVersion`,
  `TrustedPersistenceContext`, `ObjectReference` and audit contracts remain the
  owners of their respective identities, authorization context, source storage
  and audit semantics. This feature adds no independent authorization system or
  storage implementation.
- Domain contracts carry `r2-private-asr-corpus-contract:v1` and application
  request/result contracts carry `r2-private-asr-admission-contract:v1`; the
  manifest and report additionally carry their named schema versions.
- `tests/domain/evaluations/test_private_asr_corpus.py` and
  `tests/application/test_private_asr_corpus.py` use synthetic fixtures and
  in-memory fakes only.

## Frozen interfaces for #772

The runner can consume these public Python contracts:

- `PrivateCorpusManifest`, `PrivateCorpusRecord`, `SourceRevisionBinding`,
  `AdmissionEvidence`, `AudioDigestVerification`, `MediaMetadata`, `PrivateReference`;
- `GroundTruthAnnotation`, `TranscriptSegment`, `SemanticAnnotation`,
  `AnnotationReview`, `AnnotationCorrection`;
- `CorpusSplit`, `ClusterLineage`, `PreprocessingDerivative`,
  `validate_split_isolation`;
- `CoverageObservation`, `CoverageCounts`, `calculate_coverage`,
  `qualification_sufficiency`;
- `PurposeAdmissionRequest`, `AdmittedSourceRevision`,
  `ContentVerificationResult`, `TwoStageSourceAdmission`;
- `PrivacyPolicy`, `AggregateMeasurement`, `SanitizedCorpusReport`,
  `SanitizedReportBuilder`, `PrivacyPolicyApprovalPort`.

The versioned admission service depends on `AccessPolicyPort`,
`AdmissionAuthorityPort`, `TrustedContextVerifier`, `SourceRevisionIdentityPort`,
`AdmittedRevisionContentPort`, `DerivedStagingPort`, `OriginalReferencePort`,
`AuditReceiptPort` and `ClockPort`. Restricted lifecycle/storage contracts are
`PrivateAnnotationStoragePort`, `PrivateReportStoragePort`, `CleanupPort`,
`RevocationFencePort` and `RetentionExpirationPort`. Implementations remain
operator-owned; the contracts do not select a provider or storage backend.

`TwoStageSourceAdmission.admit(...)` uses an injected clock, verifies the exact
source binding, current AccessPolicy, purpose/legal evidence, read/process grants,
actor, retention and allowed environment, then creates an audit receipt without
invoking the content port. Only
`verify_content(admitted, context=..., previously_verified_sha256=...)` may
stream the admitted immutable revision. It hashes during authorized streaming,
checks the original and previously verified digests, rechecks trusted context,
current policy, exact revision and authority, binds the resulting digest to the
revision and audit receipt, and discards partial staging on failure. The content
port exposes no “latest revision” or fallback operation.

`MetadataInventoryPort` returns approved metadata-only entries. It has no
content resolver method. Original, staging, annotation, detailed report,
cleanup, revocation fence, retention expiration and audit receipt are ports;
no concrete customer-data storage backend is included.

## Privacy and qualification status

The private manifest contains opaque references and source identity digests, not
connector names, source URLs, paths or provider payloads. Transcript text lives
only in the private annotation artifact contract. The sanitized report accepts
only allowlisted aggregate categories and metrics, rejects small cells, and has
no per-case IDs, locators, hashes, text, paths or rare-example fields. Sanitizer
errors deny report creation. The report schema enforces denominator, numerator
and complement cell sizes. `SanitizedReportBuilder` resolves a current
scope-bound approved policy and rejects missing, expired, revoked or
scope-mismatched approval. No default small-cell threshold is selected.

Spine's existing immutable `SourceRevision` includes its ingestion SHA-256. That
provenance field may be present in stage-one metadata, but admission does not
read bytes or compute a corpus verification digest. The separate
`AudioDigestVerification` evidence is absent until authorized stage-two
streaming succeeds.

No sufficiency minima are introduced. `qualification_sufficiency(...)` and
sanitized reports return `NOT_VERIFIED`; #766 remains a required human decision.
The implementation does not resolve #770 purpose approval or exact-recording
authorization. No real corpus was populated, no real audio was read or hashed,
and no ASR assets or inference were used.

## Handoff and non-cycles

#772 may depend on the corpus domain/application interfaces above. #771 does not
import a runner, candidate adapter or #683 qualification result. #683 evidence
and #690 model selection remain separate consumers/decisions, so no native
implementation dependency cycle is introduced. Corpus release and qualification
remain `NOT_VERIFIED` until the applicable human gates and operator controls are
fulfilled.
