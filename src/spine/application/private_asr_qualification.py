"""Governed orchestration for private-real ASR evaluation.

The runner accepts only frozen manifest records and injected ports. It has no
filesystem resolver, model import, or public report path.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import unicodedata
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.private_asr_corpus import (
    AdmittedSourceRevision,
    ContentVerificationResult,
    CorpusAdmissionDenied,
    CorpusRecheckUnavailable,
    CorpusVerificationError,
    PrivateReportReceipt,
    PrivateReportStoragePort,
    PrivacySanitizationError,
    PurposeAdmissionRequest,
    SanitizedReportBuilder,
    TwoStageSourceAdmission,
)
from spine.application.transcription import SpeechToTextPort
from spine.domain.artifacts.transcription import (
    AudioRecordingHandle,
    SpeechToTextRequest,
    SpeechToTextResult,
    SpeechToTextResultV2,
)
from spine.domain.evaluations.private_asr_corpus import (
    AggregateCategory,
    AggregateMetric,
    AnnotationStatus,
    CorpusSplit,
    CoverageObservation,
    GroundTruthAnnotation,
    PrivateCorpusManifest,
    PrivateCorpusRecord,
    PrivateReference,
    calculate_coverage,
    qualification_sufficiency,
    validate_split_isolation,
)
from spine.domain.common import DefinitionModel
from spine.domain.evaluations.private_asr_qualification import (
    PRIVATE_REAL_ASR_EVALUATION_SCHEMA,
    PrivateCaseDisposition,
    PrivateCandidateConfiguration,
    PrivateCaseOutcome,
    PrivateEvaluationDenominators,
    PrivateEvaluationFailureKind,
    PrivateEvaluationRefusalOutcome,
    PrivateRealASREvaluationReport,
)


PRIVATE_ASR_NORMALIZER_VERSION = "private-russian-casefold-whitespace:v1"


class PrivateEvaluationUnavailable(Exception):
    """Generic preflight, revocation, runtime, or persistence refusal."""


class PrivateEvaluationRefused(CorpusAdmissionDenied):
    """Content-free in-memory refusal; it is never persisted or exported."""

    def __init__(self, outcome: PrivateEvaluationRefusalOutcome) -> None:
        super().__init__("private evaluation was refused")
        self.outcome = outcome


class ApprovedPrivateAnnotation(DefinitionModel):
    """Current, exact annotation artifact loaded through restricted storage."""

    annotation: GroundTruthAnnotation
    annotation_ref: PrivateReference
    annotation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_receipt_ref: PrivateReference


class EvaluationDataAdmissionPort(Protocol):
    """Resolves opaque frozen references and current governance evidence only."""

    async def purpose_request(
        self, manifest: PrivateCorpusManifest, record: PrivateCorpusRecord,
        *, context: TrustedPersistenceContext,
    ) -> PurposeAdmissionRequest: ...

    async def verify_evaluation_purpose(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_legal_data_evidence(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_exact_source_revision(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_current_access_policy(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_current_read_grant(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_current_process_grant(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_retention_policy(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_admission_audit_receipt(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_approved_environment(
        self, request: PurposeAdmissionRequest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_dataset_and_split(
        self, manifest: PrivateCorpusManifest, record: PrivateCorpusRecord,
        *, requested_split: CorpusSplit, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_host_no_egress(
        self, manifest: PrivateCorpusManifest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_protocol_owner_decision(
        self, manifest: PrivateCorpusManifest, *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def load_approved_annotation(
        self, manifest: PrivateCorpusManifest, record: PrivateCorpusRecord,
        *, admitted: AdmittedSourceRevision, context: TrustedPersistenceContext,
    ) -> ApprovedPrivateAnnotation | None: ...

    async def audio_handle(
        self, record: PrivateCorpusRecord, verified: ContentVerificationResult,
        *, context: TrustedPersistenceContext,
    ) -> AudioRecordingHandle: ...

    async def current_release_authorized(
        self, report: PrivateRealASREvaluationReport,
        *, context: TrustedPersistenceContext,
    ) -> bool: ...

    async def verify_complementary_disclosure_risk(
        self, report: PrivateRealASREvaluationReport, sanitized_report: object,
        *, context: TrustedPersistenceContext,
    ) -> bool: ...


class PrivateCandidateRuntime(Protocol):
    """Candidate boundary backed by the provider-neutral SpeechToTextPort."""

    async def transcribe(
        self, request: SpeechToTextRequest,
    ) -> SpeechToTextResult | SpeechToTextResultV2: ...


class SpeechToTextCandidateRuntime:
    """Adapts the established provider-neutral SpeechToTextPort to this runner."""

    def __init__(self, port: SpeechToTextPort) -> None:
        self._port = port

    async def transcribe(
        self, request: SpeechToTextRequest,
    ) -> SpeechToTextResult | SpeechToTextResultV2:
        result = await self._port.transcribe(request)
        if not isinstance(result, (SpeechToTextResult, SpeechToTextResultV2)):
            raise PrivateEvaluationUnavailable("candidate returned an unsupported result contract")
        if (
            result.adapter_reference != request.adapter_reference
            or result.model_config_reference != request.model_config_reference
        ):
            raise PrivateEvaluationUnavailable("candidate result identity does not match its request")
        return result


class PrivateCandidateRuntimeFactory(Protocol):
    def create(
        self, *, candidate_id: str, configuration: PrivateCandidateConfiguration,
    ) -> PrivateCandidateRuntime: ...


class PrivateReportAuditPort(Protocol):
    async def create_report_receipt(
        self, *, workspace_id: UUID, environment_id: UUID,
        report_id: UUID,
        retention_profile_ref: PrivateReference,
        context: TrustedPersistenceContext,
    ) -> PrivateReference: ...


class PrivateAggregateExportPort(Protocol):
    async def export_sanitized(
        self, payload: bytes, *, context: TrustedPersistenceContext,
    ) -> None: ...


class GovernedPrivateDatasetResolver:
    """Binds records from a validated manifest; it never accepts paths or URLs."""

    def __init__(self, admission: EvaluationDataAdmissionPort) -> None:
        self._admission = admission

    async def request_for(
        self, manifest: PrivateCorpusManifest, record: PrivateCorpusRecord,
        *, context: TrustedPersistenceContext,
    ) -> PurposeAdmissionRequest:
        if record not in manifest.records:
            raise CorpusAdmissionDenied("record is not a member of the frozen dataset")
        request = await self._admission.purpose_request(manifest, record, context=context)
        if (
            request.source_object.source_object_id != record.source_binding.source_object_id
            or request.source_revision.revision_id != record.source_binding.source_revision_id
            or request.source_revision.revision_digest != record.source_binding.source_revision_digest
            or request.workspace_id != manifest.workspace_id
            or request.environment_id != manifest.environment_id
            or request.allowed_environment_id != manifest.environment_id
            or request.purpose_approval_ref != manifest.purpose_approval_ref
            or request.legal_data_evidence_ref != record.admission.legal_data_evidence_ref
            or request.retention_profile_ref != manifest.retention_profile_ref
        ):
            raise CorpusAdmissionDenied("frozen record admission binding is inconsistent")
        return request


class PrivateRealASREvaluationRunner:
    """Execute a selected split after metadata admission and exact-byte verification."""

    def __init__(
        self, *, contexts: TrustedContextVerifier,
        admission: EvaluationDataAdmissionPort,
        source_admission: TwoStageSourceAdmission,
        runtime_factory: PrivateCandidateRuntimeFactory,
        reports: PrivateReportStoragePort,
        report_audit: PrivateReportAuditPort,
        aggregate_builder: SanitizedReportBuilder,
        aggregate_export: PrivateAggregateExportPort,
        clock: Callable[[], datetime],
        candidate_id: str,
        configuration: PrivateCandidateConfiguration,
        evaluator_id: str = "private_asr_evaluator",
        evaluator_version: str = "private_asr_evaluator:v1",
        normalizer_version: str = PRIVATE_ASR_NORMALIZER_VERSION,
        report_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._contexts = contexts
        self._admission = admission
        self._resolver = GovernedPrivateDatasetResolver(admission)
        self._source_admission = source_admission
        self._runtime_factory = runtime_factory
        self._reports = reports
        self._report_audit = report_audit
        self._aggregate_builder = aggregate_builder
        self._aggregate_export = aggregate_export
        self._clock = clock
        self._candidate_id = candidate_id
        self._configuration = configuration
        if configuration.candidate_id != candidate_id:
            raise ValueError("candidate identity does not match its frozen configuration")
        self._evaluator_id = evaluator_id
        self._evaluator_version = evaluator_version
        self._normalizer_version = normalizer_version
        self._report_id_factory = report_id_factory

    async def run(
        self, manifest: PrivateCorpusManifest, *, requested_split: CorpusSplit,
        context: TrustedPersistenceContext,
    ) -> tuple[PrivateRealASREvaluationReport, PrivateReportReceipt]:
        trusted = self._trusted_context(manifest, context)
        self._validate_manifest_scope(manifest, trusted)
        # The source admission service validates approved purpose, legal evidence,
        # current AccessPolicy, grants, actor, retention, audit, expiry and revocation.
        selected = tuple(record for record in manifest.records if record.split is requested_split)

        # Metadata-only preflight for the entire selected split precedes all reads.
        admission_requests: list[tuple[PrivateCorpusRecord, PurposeAdmissionRequest]] = []
        for record in selected:
            try:
                request = await self._resolver.request_for(manifest, record, context=trusted)
                # Ordered authority checks are explicit here even though the frozen
                # #771 admission service repeats them at the content boundary.
                if not await self._admission.verify_evaluation_purpose(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="purpose",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_legal_data_evidence(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="legal_data_evidence",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_exact_source_revision(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE, stage="source_revision",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_current_access_policy(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="access_policy",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_current_read_grant(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="read_grant",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_current_process_grant(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="process_grant",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_retention_policy(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="retention",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_admission_audit_receipt(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="audit",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_approved_environment(request, context=trusted):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="environment",
                        eligible=len(selected), admitted=0)
                if not await self._admission.verify_dataset_and_split(
                    manifest, record, requested_split=requested_split, context=trusted
                ):
                    self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="dataset_split",
                        eligible=len(selected), admitted=0)
            except PrivateEvaluationRefused:
                raise
            except Exception:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE,
                    stage="metadata_preflight", eligible=len(selected), admitted=0)
            admission_requests.append((record, request))

        try:
            validate_split_isolation(manifest.records)
        except ValueError:
            self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="split_isolation",
                eligible=len(selected), admitted=0)
        try:
            protocol_decision = manifest.protocol_decision_ref
            decision_verified = bool(
                protocol_decision is not None
                and await self._admission.verify_protocol_owner_decision(manifest, context=trusted)
            )
            if not decision_verified:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="protocol_owner_decision",
                    eligible=len(selected), admitted=0)
            if not await self._admission.verify_host_no_egress(manifest, context=trusted):
                self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="host_no_egress",
                    eligible=len(selected), admitted=0)
        except PrivateEvaluationRefused:
            raise
        except Exception:
            self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE,
                stage="governance_preflight", eligible=len(selected), admitted=0)

        # Run #771's metadata-only admission after purpose, source, rights,
        # retention, split, Protocol Owner, and host prerequisites all passed.
        admissions: list[tuple[PrivateCorpusRecord, AdmittedSourceRevision]] = []
        for record, request in admission_requests:
            try:
                admitted = await self._source_admission.admit(request, context=trusted)
                self._assert_admitted_record(manifest, record, request, admitted)
            except CorpusAdmissionDenied:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="source_admission",
                    eligible=len(selected), admitted=len(admissions))
            except Exception:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE,
                    stage="source_admission", eligible=len(selected), admitted=len(admissions))
            admissions.append((record, admitted))

        # Stage two is the only operation allowed to open/hash the exact source.
        verified: list[tuple[PrivateCorpusRecord, AdmittedSourceRevision, ContentVerificationResult]] = []
        for record, admitted in admissions:
            try:
                content = await self._source_admission.verify_content(
                    admitted, context=trusted,
                    previously_verified_sha256=(
                        None if record.audio_verification is None
                        else record.audio_verification.audio_sha256
                    ),
                )
            except CorpusAdmissionDenied:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.REVOKED, stage="content_authority",
                    eligible=len(selected), admitted=len(admissions), verified=len(verified))
            except CorpusVerificationError:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE, stage="content_verification",
                    eligible=len(selected), admitted=len(admissions), verified=len(verified))
            except Exception:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE, stage="content_verification",
                    eligible=len(selected), admitted=len(admissions), verified=len(verified))
            verified.append((record, admitted, content))

        annotations: list[tuple[PrivateCorpusRecord, AdmittedSourceRevision, ContentVerificationResult, GroundTruthAnnotation]] = []
        for record, admitted, content in verified:
            await self._recheck(
                admitted, context=trusted, stage="annotation_read",
                eligible=len(selected), admitted_count=len(admissions), verified=len(verified),
                annotated=len(annotations),
            )
            try:
                loaded = await self._admission.load_approved_annotation(
                    manifest, record, admitted=admitted, context=trusted
                )
            except Exception:
                self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE, stage="annotation_read",
                    eligible=len(selected), admitted=len(admissions), verified=len(verified),
                    annotated=len(annotations))
            if loaded is None:
                continue
            annotation = loaded.annotation
            if (
                annotation.source_record_id != record.record_id
                or annotation.status not in (AnnotationStatus.APPROVED, AnnotationStatus.ADJUDICATED)
                or loaded.annotation_ref != record.annotation_ref
                or loaded.annotation_sha256 != record.annotation_sha256
            ):
                self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED, stage="annotation_binding",
                    eligible=len(selected), admitted=len(admissions), verified=len(verified),
                    annotated=len(annotations))
            annotations.append((record, admitted, content, annotation))

        # Fake-backed providers produce observations only. Qualification remains NOT_VERIFIED.
        for _, admitted, _, _ in annotations:
            await self._recheck(
                admitted, context=trusted, stage="before_runtime_initialization",
                eligible=len(selected), admitted_count=len(admissions), verified=len(verified),
                annotated=len(annotations),
            )
        runtime_failure = False
        try:
            if annotations:
                runtime = self._runtime_factory.create(
                    candidate_id=self._candidate_id, configuration=self._configuration
                )
                if runtime is None:
                    raise TypeError("candidate runtime factory returned no runtime")
            else:
                runtime = None
        except Exception:
            runtime = None
            runtime_failure = True
        outcomes: list[PrivateCaseOutcome] = [
            PrivateCaseOutcome(
                record_ref=_record_ref(record), split=record.split,
                disposition=PrivateCaseDisposition.UNAVAILABLE,
            )
            for record, _, _, _ in annotations
        ] if runtime_failure else []
        for record, admitted, content, annotation in annotations:
            if runtime is None:
                continue
            await self._recheck(
                admitted, context=trusted, stage="before_inference",
                eligible=len(selected), admitted_count=len(admissions), verified=len(verified),
                annotated=len(annotations),
            )
            try:
                recording = await self._admission.audio_handle(record, content, context=trusted)
            except Exception:
                outcomes.append(PrivateCaseOutcome(
                    record_ref=_record_ref(record), split=record.split,
                    disposition=PrivateCaseDisposition.UNAVAILABLE,
                ))
                continue
            # Resolving the restricted handle may itself wait on storage or an
            # adapter. Recheck after that await and immediately before inference.
            await self._recheck(
                admitted, context=trusted, stage="before_candidate_invocation",
                eligible=len(selected), admitted_count=len(admissions), verified=len(verified),
                annotated=len(annotations),
            )
            try:
                result = await runtime.transcribe(SpeechToTextRequest(
                    recording=recording,
                    processing_profile_reference=self._configuration.processing_profile_reference,
                    adapter_reference=self._configuration.adapter_reference,
                    model_config_reference=self._configuration.model_config_reference,
                    language_hint="ru",
                ))
            except Exception:
                await self._recheck(
                    admitted, context=trusted, stage="candidate_inference",
                    eligible=len(selected), admitted_count=len(admissions), verified=len(verified),
                    annotated=len(annotations),
                )
                outcomes.append(PrivateCaseOutcome(
                    record_ref=_record_ref(record), split=record.split,
                    disposition=PrivateCaseDisposition.UNAVAILABLE,
                ))
                continue
            await self._recheck(
                admitted, context=trusted, stage="candidate_inference",
                eligible=len(selected), admitted_count=len(admissions), verified=len(verified),
                annotated=len(annotations),
            )
            if not isinstance(result, (SpeechToTextResult, SpeechToTextResultV2)):
                outcomes.append(PrivateCaseOutcome(
                    record_ref=_record_ref(record), split=record.split,
                    disposition=PrivateCaseDisposition.UNAVAILABLE,
                ))
                continue
            if (
                result.adapter_reference != self._configuration.adapter_reference
                or result.model_config_reference != self._configuration.model_config_reference
            ):
                outcomes.append(PrivateCaseOutcome(
                    record_ref=_record_ref(record), split=record.split,
                    disposition=PrivateCaseDisposition.UNAVAILABLE,
                ))
                continue
            reference = _normalize_text(" ".join(
                segment.human_text for segment in annotation.segments if segment.human_text
            ))
            hypothesis = _normalize_text(" ".join(segment.text for segment in result.segments))
            ref_words, hyp_words = reference.split(), hypothesis.split()
            ref_chars, hyp_chars = list(reference), list(hypothesis)
            if not ref_words or not ref_chars:
                outcomes.append(PrivateCaseOutcome(record_ref=_record_ref(record), split=record.split,
                    disposition=PrivateCaseDisposition.UNSCORABLE))
                continue
            outcomes.append(PrivateCaseOutcome(
                record_ref=_record_ref(record), split=record.split,
                disposition=PrivateCaseDisposition.MEASURED,
                word_errors=_edit_distance(ref_words, hyp_words), reference_words=len(ref_words),
                character_errors=_edit_distance(ref_chars, hyp_chars), reference_characters=len(ref_chars),
            ))

        for record, admitted in admissions:
            await self._recheck(
                admitted, context=trusted, stage="before_report_acceptance",
                eligible=len(selected), admitted_count=len(admissions), verified=len(verified),
                annotated=len(annotations),
            )
        try:
            report = await self._build_report(
                manifest, selected, admissions, verified, annotations, outcomes,
                decision_verified=decision_verified, context=trusted,
            )
        except Exception:
            raise PrivateEvaluationUnavailable("private report preparation failed") from None
        receipt = await self._persist_private_report(
            report, admitted_sources=tuple(item[1] for item in admissions), context=trusted,
            eligible=len(selected), verified=len(verified), annotated=len(annotations),
        )
        return report, receipt

    async def export_aggregate(
        self, report: PrivateRealASREvaluationReport, *,
        privacy_policy_version: str, context: TrustedPersistenceContext,
    ) -> None:
        try:
            trusted = self._contexts.verify(context)
            trusted.validate_shape()
        except Exception:
            raise PrivacySanitizationError("aggregate export context is invalid") from None
        if (
            trusted.purpose.value != "asr_model_evaluation"
            or trusted.operation.value != "sanitize_private_asr_report"
        ):
            raise PrivacySanitizationError("aggregate export operation is not authorized")
        if not isinstance(trusted.scope, EnvironmentScope) or (
            trusted.scope.workspace_id, trusted.scope.environment_id
        ) != (report.workspace_id, report.environment_id):
            raise PrivacySanitizationError("aggregate export scope mismatch")
        # Current PrivacyPolicy, allowed categories/metrics, denominators and cell
        # minimum are checked by the frozen #771 SanitizedReportBuilder.
        measurements = _aggregate_measurements(report)
        try:
            sanitized = await self._aggregate_builder.build(
                policy_version=privacy_policy_version, measurements=measurements,
                context=trusted,
            )
        except PrivacySanitizationError:
            raise
        except Exception:
            raise PrivacySanitizationError("aggregate sanitization is unavailable") from None
        try:
            if not await self._admission.verify_complementary_disclosure_risk(
                report, sanitized, context=trusted
            ):
                raise PrivacySanitizationError("aggregate disclosure risk denied")
            if not await self._admission.current_release_authorized(report, context=trusted):
                raise PrivacySanitizationError("aggregate release is not currently authorized")
        except PrivacySanitizationError:
            raise
        except Exception:
            raise PrivacySanitizationError("aggregate release authorization is unavailable") from None
        try:
            await self._aggregate_export.export_sanitized(
                sanitized.model_dump_json(exclude={"privacy_policy_approval", "workspace_id", "environment_id"}).encode(),
                context=trusted,
            )
        except Exception:
            raise PrivacySanitizationError("aggregate export failed") from None

    def _trusted_context(
        self, manifest: PrivateCorpusManifest, context: TrustedPersistenceContext,
    ) -> TrustedPersistenceContext:
        try:
            context.validate_shape()
            trusted = self._contexts.verify(context)
            trusted.validate_shape()
        except Exception:
            raise CorpusAdmissionDenied("trusted evaluation context is invalid") from None
        if not isinstance(trusted.scope, EnvironmentScope):
            raise CorpusAdmissionDenied("workspace and environment scope are required")
        if (trusted.scope.workspace_id, trusted.scope.environment_id) != (
            manifest.workspace_id, manifest.environment_id
        ):
            raise CorpusAdmissionDenied("evaluation context scope mismatch")
        return trusted

    def _validate_manifest_scope(
        self, manifest: PrivateCorpusManifest, context: TrustedPersistenceContext,
    ) -> None:
        if manifest.purpose != "asr_model_evaluation":
            raise CorpusAdmissionDenied("evaluation purpose denied")
        if any(
            (record.workspace_id, record.environment_id) !=
            (manifest.workspace_id, manifest.environment_id)
            for record in manifest.records
        ):
            raise CorpusAdmissionDenied("dataset scope mismatch")
        if context.purpose.value != "asr_model_evaluation":
            raise CorpusAdmissionDenied("evaluation purpose denied")
        if context.operation.value != "run_private_asr_evaluation":
            raise CorpusAdmissionDenied("evaluation operation denied")
        if (
            manifest.evaluator_schema != PRIVATE_REAL_ASR_EVALUATION_SCHEMA
            or manifest.evaluator_version != self._evaluator_version
            or manifest.normalizer_version != self._normalizer_version
        ):
            raise CorpusAdmissionDenied("dataset evaluator or normalizer binding mismatch")

    @staticmethod
    def _raise_refusal(
        *, kind: PrivateEvaluationFailureKind, stage: str, eligible: int,
        admitted: int, verified: int = 0, annotated: int = 0,
    ) -> None:
        raise PrivateEvaluationRefused(PrivateEvaluationRefusalOutcome(
            kind=kind, stage=stage, eligible=eligible, admitted=admitted,
            verified=verified, annotated=annotated,
            denied=int(kind is PrivateEvaluationFailureKind.DENIED),
            revoked=int(kind is PrivateEvaluationFailureKind.REVOKED),
            unavailable=int(kind is PrivateEvaluationFailureKind.UNAVAILABLE),
        ))

    def _assert_admitted_record(
        self, manifest: PrivateCorpusManifest, record: PrivateCorpusRecord,
        request: PurposeAdmissionRequest,
        admitted: AdmittedSourceRevision,
    ) -> None:
        binding = record.source_binding
        authority = admitted.authority
        if (
            admitted.source_object.source_object_id != binding.source_object_id
            or admitted.source_revision.revision_id != binding.source_revision_id
            or admitted.source_revision.revision_digest != binding.source_revision_digest
            or admitted.source_revision_number != binding.source_revision_number
            or admitted.workspace_id != manifest.workspace_id
            or admitted.environment_id != manifest.environment_id
            or admitted.access_policy_version != record.admission.access_policy_version
            or admitted.retention_profile_ref != manifest.retention_profile_ref
            or admitted.purpose_approval_ref != manifest.purpose_approval_ref
            or admitted.legal_data_evidence_ref != record.admission.legal_data_evidence_ref
            or admitted.actor_id != request.authorized_actor_id
            or authority.purpose != "asr_model_evaluation"
            or not authority.current
            or authority.revoked
            or not authority.purpose_approved
            or not authority.legal_data_evidence_confirmed
            or not authority.human_read_granted
            or not authority.process_granted
            or not authority.actor_authorized
            or not authority.retention_current
            or not authority.audit_permitted
            or not authority.environment_allowed
            or authority.expires_at <= self._clock()
        ):
            raise CorpusAdmissionDenied("admission does not match frozen source binding")

    async def _recheck(
        self, admitted: AdmittedSourceRevision, *, context: TrustedPersistenceContext,
        stage: str, eligible: int, admitted_count: int, verified: int, annotated: int,
    ) -> None:
        try:
            current = await self._source_admission.recheck_current(admitted, context=context)
        except CorpusRecheckUnavailable:
            self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE, stage=stage,
                eligible=eligible, admitted=admitted_count, verified=verified, annotated=annotated)
        except Exception:
            self._raise_refusal(kind=PrivateEvaluationFailureKind.UNAVAILABLE, stage=stage,
                eligible=eligible, admitted=admitted_count, verified=verified, annotated=annotated)
        if not current:
            self._raise_refusal(kind=PrivateEvaluationFailureKind.REVOKED, stage=stage,
                eligible=eligible, admitted=admitted_count, verified=verified, annotated=annotated)

    async def _build_report(
        self, manifest, selected, admissions, verified, annotations, outcomes,
        *, decision_verified: bool, context: TrustedPersistenceContext,
    ) -> PrivateRealASREvaluationReport:
        annotations_by_id = {item[0].record_id: item[3] for item in annotations}
        outcomes_by_id = {item.record_ref.reference_id: item for item in outcomes}
        coverage = calculate_coverage(tuple(
            CoverageObservation(
                lineage_id=record.lineage.source_lineage_cluster,
                eligible=True,
                annotated=record.record_id in annotations_by_id,
                scorable=(
                    record.record_id in annotations_by_id
                    and _annotation_is_scorable(annotations_by_id[record.record_id])
                ),
                measured=(
                    outcomes_by_id.get(record.record_id) is not None
                    and outcomes_by_id[record.record_id].disposition is PrivateCaseDisposition.MEASURED
                ),
                annotation_spans=record.coverage.expected_annotation_spans,
                missing_labels=record.coverage.missing_label_count,
                unalignable_segments=record.coverage.unalignable_segment_count,
                insufficient_categories=record.coverage.insufficient_categories,
            )
            for record in selected
        ))
        measured = coverage.measured_observations
        annotated = coverage.annotated_observations
        scorable = coverage.scorable_observations
        verified_count = len(verified)
        admitted_count = len(admissions)
        for record in selected:
            if record.record_id not in outcomes_by_id:
                disposition = (PrivateCaseDisposition.UNANNOTATED if record.record_id not in {
                    item[0].record_id for item in annotations
                } else PrivateCaseDisposition.UNSCORABLE)
                outcomes.append(PrivateCaseOutcome(record_ref=_record_ref(record), split=record.split,
                    disposition=disposition))
        report_id = self._report_id_factory()
        audit_ref = await self._report_audit.create_report_receipt(
            workspace_id=manifest.workspace_id, environment_id=manifest.environment_id,
            report_id=report_id,
            retention_profile_ref=manifest.retention_profile_ref, context=context,
        )
        report = PrivateRealASREvaluationReport(
            report_id=report_id, dataset_id=manifest.dataset_id, manifest_version=manifest.version,
            manifest_sha256=manifest.manifest_sha256, evaluator_id=self._evaluator_id,
            evaluator_version=self._evaluator_version, normalizer_version=self._normalizer_version,
            candidate_id=self._candidate_id,
            candidate_config_digest=self._configuration.configuration_digest,
            workspace_id=manifest.workspace_id, environment_id=manifest.environment_id,
            generated_at=self._clock(), outcomes=tuple(outcomes),
            denominators=PrivateEvaluationDenominators(
                eligible=coverage.eligible_observations, admitted=admitted_count, verified=verified_count,
                annotated=annotated, scorable=scorable, measured=measured,
                independent_lineages=coverage.independent_lineages, denied=0, revoked=sum(
                    item.disposition is PrivateCaseDisposition.REVOKED for item in outcomes
                ), unavailable=sum(
                    item.disposition is PrivateCaseDisposition.UNAVAILABLE for item in outcomes
                ),
                unsupported_metrics=("semantic_spans", "timestamps", "silence", "performance"),
                insufficient_coverage=tuple(sorted({
                    *(category for record in selected for category in record.coverage.insufficient_categories),
                    *(('unmeasured_eligible_cases',) if measured < len(selected) else ()),
                    *(('qualification_split_empty',) if not selected else ()),
                })),
            ),
            measurement_limitations=("fake_or_offline_runtime_cannot_qualify", "no_protocol_minima_inferred"),
            coverage_disposition=(
                "incomplete_measurement"
                if any(item.disposition is PrivateCaseDisposition.UNAVAILABLE for item in outcomes)
                else "insufficient_coverage"
                if not selected or measured < len(selected)
                else "measured_not_qualified"
            ),
            failure_code=(
                "candidate_unavailable"
                if any(item.disposition is PrivateCaseDisposition.UNAVAILABLE for item in outcomes)
                else None
            ),
            sufficiency=qualification_sufficiency(protocol_decision_present=decision_verified),
            protocol_decision_ref=manifest.protocol_decision_ref if decision_verified else None,
            audit_receipt_ref=audit_ref, retention_profile_ref=manifest.retention_profile_ref,
        )
        return report

    async def _persist_private_report(
        self, report: PrivateRealASREvaluationReport, *,
        admitted_sources: tuple[AdmittedSourceRevision, ...],
        context: TrustedPersistenceContext,
        eligible: int, verified: int, annotated: int,
    ) -> PrivateReportReceipt:
        for admitted in admitted_sources:
            await self._recheck(
                admitted, context=context, stage="before_private_report_commit",
                eligible=eligible, admitted_count=len(admitted_sources),
                verified=verified, annotated=annotated,
            )
        try:
            release_authorized = await self._admission.current_release_authorized(
                report, context=context
            )
        except Exception:
            raise PrivateEvaluationUnavailable("private report authorization is unavailable") from None
        if not release_authorized:
            self._raise_refusal(kind=PrivateEvaluationFailureKind.DENIED,
                stage="private_report_release", eligible=eligible,
                admitted=len(admitted_sources), verified=verified, annotated=annotated)
        try:
            receipt = await self._reports.store_private_report(
                report.model_dump_json().encode(),
                retention_profile_ref=report.retention_profile_ref,
                audit_receipt_ref=report.audit_receipt_ref,
            )
            if receipt.retention_profile_ref != report.retention_profile_ref:
                raise ValueError("restricted report receipt scope mismatch")
            return receipt
        except Exception:
            raise PrivateEvaluationUnavailable("private report storage failed") from None


def _record_ref(record: PrivateCorpusRecord) -> PrivateReference:
    return PrivateReference(reference_id=record.record_id, reference_kind="private_asr_record")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _annotation_is_scorable(annotation: GroundTruthAnnotation) -> bool:
    reference = " ".join(
        segment.human_text
        for segment in annotation.segments
        if segment.human_text is not None and not segment.unalignable
    )
    return bool(_normalize_text(reference).split())


def _edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for row, left_item in enumerate(left, start=1):
        current = [row]
        for column, right_item in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (left_item != right_item)))
        previous = current
    return previous[-1]


def _aggregate_measurements(report: PrivateRealASREvaluationReport):
    measured = tuple(item for item in report.outcomes if item.disposition is PrivateCaseDisposition.MEASURED)
    words = sum(item.reference_words or 0 for item in measured)
    errors = sum(item.word_errors or 0 for item in measured)
    if not words:
        raise PrivacySanitizationError("aggregate has no measured denominator")
    from spine.application.private_asr_corpus import AggregateMeasurement

    return (AggregateMeasurement(
        category=AggregateCategory.OVERALL, metric_name=AggregateMetric.WER,
        numerator=errors, denominator=words,
    ),)


__all__ = [
    "ApprovedPrivateAnnotation", "EvaluationDataAdmissionPort", "GovernedPrivateDatasetResolver",
    "PrivateAggregateExportPort", "PrivateCandidateRuntime",
    "PrivateCandidateRuntimeFactory", "PrivateEvaluationUnavailable",
    "PrivateRealASREvaluationRunner",
    "SpeechToTextCandidateRuntime",
]
