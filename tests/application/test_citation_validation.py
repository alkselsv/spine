from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

import pytest

from spine.application.citation_validation import (
    CanonicalCitationContributor,
    CanonicalCitationEvidence,
    CitationValidationReasonCode,
    CitationValidationService,
    RejectedCitation,
    ValidCitation,
)
from spine.application.content_authorization import (
    AuthorizationContextMode,
    ContentAuthorizationDecisionService,
    CurrentContentAuthorizationSnapshot,
    TrustedAuthorizationContext,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.capabilities.answer_question import Citation
from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleEvidenceReference,
    ContextBundleOutcome,
    EvidenceLocator,
    PolicyDecisionGeneration,
)
from spine.domain.context_profiles import ContextProfileRef
from spine.domain.ingestion import ProcessingCoverage, ProcessingReceipt
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionConfigVersion,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionReceipt,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    SourceProjectionReference,
)
from spine.domain.sources import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    CanonicalBoundary,
    CurrentAccessPolicy,
    IdentityMode,
    PolicyOriginKind,
    PrincipalKind,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    content_revision,
)
from spine.domain.sources.original_reference import ObjectReference


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
WORKSPACE, ENVIRONMENT, SOURCE, REVISION = (UUID(int=value) for value in range(1, 5))
SNAPSHOT, BOUNDARY, CONFIG, ACTIVATION = (UUID(int=value) for value in range(5, 9))
RUN, ATTEMPT, HUMAN = UUID(int=9), UUID(int=10), UUID(int=11)


def _digest(schema: str, payload: object) -> str:
    return sha256(
        json.dumps({"schema": schema, "payload": payload}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source(source_id: UUID = SOURCE) -> SourceObject:
    return SourceObject(
        source_object_id=source_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity=f"upload-{source_id.int}",
        created_at=NOW,
    )


def _revision(source_id: UUID = SOURCE, revision_id: UUID = REVISION):
    return content_revision(
        revision_id=revision_id,
        source_object_id=source_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=RevisionMetadata(embedded_title="Synthetic"),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=UUID(int=50 + source_id.int),
            storage_generation=UUID(int=60 + source_id.int),
            digest_algorithm="sha256",
            digest_hex="a" * 64,
            byte_length=4,
        ),
        original_sha256="a" * 64,
        byte_length=4,
        media_type="text/plain",
        observed_at=NOW,
    )


def _config() -> ProjectionConfigVersion:
    return ProjectionConfigVersion(
        projection_config_version_id=CONFIG,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=70),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload={},
        config_digest=_digest("projection-config:r1", {}),
        created_at=NOW,
    )


def _reference(
    *, source_id: UUID = SOURCE, revision_id: UUID = REVISION, evidence_id: str = "evidence-1"
) -> ContextBundleEvidenceReference:
    revision = _revision(source_id, revision_id)
    locator = EvidenceLocator(kind="page", value=f"page:{source_id.int}")
    return ContextBundleEvidenceReference(
        schema_version="context-evidence-reference:v2",
        evidence_id=evidence_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_snapshot_id=SNAPSHOT,
        activation_generation=1,
        source_object_id=source_id,
        source_revision_id=revision_id,
        original_sha256=revision.original_sha256,
        revision_digest=revision.revision_digest,
        processing_receipt_id=UUID(int=500 + source_id.int),
        projected_kind="chunk",
        locator_contract_version="locator:v1",
        locator=locator,
        locator_digest=canonical_projection_locator_digest(
            locator_contract_version="locator:v1", locator=locator
        ),
        evidence_digest="e" * 64,
    )


def _bundle(*references: ContextBundleEvidenceReference) -> ContextBundle:
    return ContextBundle.create(
        bundle_id=UUID(int=80), bundle_version=1,
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        profile_ref=ContextProfileRef(profile_id=UUID(int=81), profile_version=1, profile_digest="1" * 64),
        trusted_request_context_ref=UUID(int=82), authorization_context_ref=UUID(int=83),
        request_id=UUID(int=84), run_id=RUN, attempt_id=ATTEMPT,
        idempotency_key="attempt-1",
        projection_snapshot=ProjectionSnapshot(
            projection_snapshot_id=SNAPSHOT, projection_run_id=UUID(int=85),
            projection_candidate_id=UUID(int=86), workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT, snapshot_state=ProjectionSnapshotState.FINALIZED,
            projection_kind="qa", projection_config_version_id=CONFIG,
            canonical_boundary_id=BOUNDARY, candidate_evidence_digest="2" * 64,
            manifest_digest="3" * 64, member_count=len(references), created_at=NOW,
        ),
        projection_activation=ProjectionActivation(
            projection_activation_id=ACTIVATION, workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT, projection_kind="qa", projection_snapshot_id=SNAPSHOT,
            activation_generation=1, activation_kind=ProjectionActivationKind.PUBLISH,
            command_identity="activate", command_digest="4" * 64,
            idempotency_receipt_id=UUID(int=87), audit_event_id=UUID(int=88), activated_at=NOW,
        ),
        activation_generation=1,
        canonical_boundary=CanonicalBoundary(
            boundary_id=BOUNDARY, workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
            projection_kind="qa", scope_version="boundary:v1", accepted_through_generation=2,
            boundary_time=NOW, manifest_digest="5" * 64, idempotency_receipt_id=UUID(int=89),
        ),
        canonical_as_of=CanonicalAsOf(
            canonical_boundary_id=BOUNDARY, accepted_through_generation=2, boundary_time=NOW,
        ),
        projection_config_version=_config(), freshness=ProjectionFreshness.CURRENT,
        health=ProjectionHealth.HEALTHY, retrieval_strategy="hybrid",
        outcome=ContextBundleOutcome.ALLOWED, reason_code="allowed",
        evidence_references=references,
        policy_decision_generations=tuple(
            PolicyDecisionGeneration(
                source_object_id=reference.source_object_id,
                policy_version_id=UUID(int=100 + reference.source_object_id.int),
                policy_generation=1,
                membership_generation=1,
                authority_generation=1,
                decision_reference=UUID(int=400 + reference.source_object_id.int),
            )
            for reference in references
        ),
        created_at=NOW,
    )


def _citation(bundle: ContextBundle, reference: ContextBundleEvidenceReference) -> Citation:
    return Citation(
        citation_id=UUID(int=90), answer_id=UUID(int=91),
        context_bundle=bundle.lineage_reference, evidence_reference=reference,
    )


def _projection_receipt(
    bundle: ContextBundle, reference: ContextBundleEvidenceReference
) -> ProjectionReceipt:
    return ProjectionReceipt(
        projection_receipt_id=UUID(int=1000 + reference.source_object_id.int),
        projection_run_id=bundle.projection_snapshot.projection_run_id,
        projection_candidate_id=bundle.projection_snapshot.projection_candidate_id,
        projection_partition_id=UUID(int=1100 + reference.source_object_id.int),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity="project",
        command_digest="a" * 64,
        artifact_kind="context_graph",
        artifact_reference="artifact:v1",
        artifact_integrity_digest="b" * 64,
        implementation_schema_version="adapter:v1",
        idempotency_receipt_id=UUID(int=1200 + reference.source_object_id.int),
        completed_at=NOW,
    )


def _record(bundle: ContextBundle, *references: ContextBundleEvidenceReference) -> CanonicalCitationEvidence:
    return CanonicalCitationEvidence(
        evidence_id=references[0].evidence_id, evidence_digest="e" * 64,
        canonical_boundary=bundle.canonical_boundary, canonical_as_of=bundle.canonical_as_of,
        projection_snapshot=bundle.projection_snapshot,
        projection_activation=bundle.projection_activation,
        projection_config_version=bundle.projection_config_version,
        contributors=tuple(
            CanonicalCitationContributor(
                reference=reference,
                source_object=_source(reference.source_object_id),
                source_revision=_revision(reference.source_object_id, reference.source_revision_id),
                processing_receipt=ProcessingReceipt(
                    processing_receipt_id=reference.processing_receipt_id,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    ingestion_run_source_id=UUID(int=600 + reference.source_object_id.int),
                    binding_id=UUID(int=700 + reference.source_object_id.int),
                    source_object_id=reference.source_object_id,
                    source_revision_id=reference.source_revision_id,
                    processor_kind="document_parser",
                    parser_name="synthetic",
                    parser_version="parser:v1",
                    parser_config_reference="parser-config:v1",
                    locator_contract_version=reference.locator_contract_version,
                    original_sha256=reference.original_sha256,
                    output_kind="parsed_document",
                    output_reference=UUID(int=800 + reference.source_object_id.int),
                    output_digest="8" * 64,
                    coverage=ProcessingCoverage.COMPLETE,
                    command_identity="process",
                    command_digest="9" * 64,
                    completed_at=NOW,
                ),
                source_projection_reference=SourceProjectionReference(
                    source_projection_reference_id=UUID(int=900 + reference.source_object_id.int),
                    projection_run_id=bundle.projection_snapshot.projection_run_id,
                    projection_candidate_id=bundle.projection_snapshot.projection_candidate_id,
                    projection_receipt_id=UUID(int=1000 + reference.source_object_id.int),
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    source_object_id=reference.source_object_id,
                    source_revision_id=reference.source_revision_id,
                    processing_receipt_id=reference.processing_receipt_id,
                    segment_id=f"segment-{reference.source_object_id.int}",
                    locator_schema_version=reference.locator_contract_version,
                    locator_reference=reference.locator.value,
                    locator_digest=reference.locator_digest,
                    projected_kind=reference.projected_kind,
                    projected_object_id=reference.evidence_id,
                    projection_config_version_id=CONFIG,
                    security_domain_reference="security-domain:v1",
                    created_at=NOW,
                ),
                projection_receipt=_projection_receipt(bundle, reference),
                projection_snapshot_member=ProjectionSnapshotMember(
                    projection_snapshot_member_id=UUID(int=1300 + reference.source_object_id.int),
                    projection_snapshot_id=SNAPSHOT,
                    canonical_boundary_id=BOUNDARY,
                    canonical_boundary_member_id=reference.source_object_id,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    source_object_id=reference.source_object_id,
                    source_revision_id=reference.source_revision_id,
                    disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
                    projection_partition_id=_projection_receipt(bundle, reference).projection_partition_id,
                    projection_receipt_id=_projection_receipt(bundle, reference).projection_receipt_id,
                ),
            ) for reference in references
        ),
    )


def _context(
    *,
    workspace_id: UUID = WORKSPACE,
    environment_id: UUID = ENVIRONMENT,
    authority_generation: int = 1,
) -> TrustedAuthorizationContext:
    return TrustedAuthorizationContext(
        mode=AuthorizationContextMode.HUMAN_DISCLOSURE,
        workspace_id=workspace_id, environment_id=environment_id,
        canonical_human_id=HUMAN, purpose="answer_question", operation="disclose_answer",
        membership_generation=1, authority_generation=authority_generation,
        authority_snapshot_reference=UUID(int=92), trace_id=UUID(int=93), issued_at=NOW,
    )


class _Bundles:
    def __init__(self, bundle: ContextBundle) -> None: self.bundle = bundle
    async def resolve(self, **_: object) -> ContextBundle: return self.bundle


class _Canonical:
    def __init__(self, records: tuple[CanonicalCitationEvidence, ...]) -> None: self.records = records
    async def resolve_citation_evidence(self, **_: object) -> tuple[CanonicalCitationEvidence, ...]: return self.records


class _Identity:
    def __init__(self, current: TrustedAuthorizationContext) -> None: self.current = current
    async def revalidate(self, _: TrustedAuthorizationContext) -> TrustedAuthorizationContext: return self.current


class _AuthorizationRepository:
    def __init__(
        self,
        revisions: dict[UUID, UUID],
        *,
        denied: set[UUID] = set(),
        retention: bool = True,
        revision_kind: RevisionKind = RevisionKind.CONTENT,
    ) -> None:
        self.revisions, self.denied, self.retention = revisions, denied, retention
        self.revision_kind = revision_kind

    async def resolve_current_content_authorization(self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID):
        if (workspace_id, environment_id) != (WORKSPACE, ENVIRONMENT) or source_object_id not in self.revisions:
            return None
        policy_id = UUID(int=100 + source_object_id.int)
        policy = AccessPolicyVersion(
            access_policy_version_id=policy_id, workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
            source_object_id=source_object_id, version=1, predecessor_policy_version_id=None,
            document_schema_version="access-policy.v1",
            document_hash="6" * 64, origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
            origin_reference=UUID(int=120 + source_object_id.int), origin_version=1,
            origin_hash="7" * 64, created_by_canonical_human_id=HUMAN, created_at=NOW,
            grants=(() if source_object_id in self.denied else (AccessGrant(
                access_grant_id=UUID(int=140 + source_object_id.int), principal_kind=PrincipalKind.HUMAN,
                principal_id=HUMAN, permission=AccessPermission.READ_CONTENT,
            ),)),
        )
        return CurrentContentAuthorizationSnapshot(
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_object_id=source_object_id,
            current_revision_id=self.revisions[source_object_id], current_revision_kind=self.revision_kind,
            retention_eligible=self.retention, provenance_complete=True,
            current_policy=CurrentAccessPolicy(
                workspace_id=WORKSPACE, environment_id=ENVIRONMENT, source_object_id=source_object_id,
                access_policy_version_id=policy_id, policy_activation_id=UUID(int=160 + source_object_id.int),
                policy_generation=1, cas_version=1,
            ), policy=policy,
        )


def _service(bundle: ContextBundle, record: CanonicalCitationEvidence, repository: _AuthorizationRepository, *, context: TrustedAuthorizationContext | None = None) -> CitationValidationService:
    trusted = context or _context()
    authorization = ContentAuthorizationDecisionService(
        repository=repository, identity_authority=_Identity(trusted), clock=lambda: NOW,
        decision_reference_factory=lambda: UUID(int=200),
    )
    return CitationValidationService(
        context_bundles=_Bundles(bundle), canonical_evidence=_Canonical((record,)),
        authorization=authorization, validation_reference_factory=lambda: UUID(int=201),
    )


@pytest.mark.asyncio
async def test_exact_current_authorized_citation_is_valid() -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    result = await _service(bundle, record, _AuthorizationRepository({SOURCE: REVISION})).validate(
        citation=_citation(bundle, reference), expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context()
    )
    assert isinstance(result, ValidCitation)
    assert result.facts.context_bundle_digest == bundle.bundle_digest


@pytest.mark.parametrize("claim_update", [{"evidence_digest": "f" * 64}, {"activation_generation": 2}])
@pytest.mark.asyncio
async def test_fabricated_or_wrong_generation_citation_fails_closed_without_source_details(
    claim_update: dict[str, object],
) -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    fabricated = reference.model_copy(update=claim_update)
    result = await _service(bundle, record, _AuthorizationRepository({SOURCE: REVISION})).validate(
        citation=_citation(bundle, fabricated), expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context()
    )
    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.IDENTITY_INVALID
    serialized = result.model_dump_json()
    assert str(SOURCE) not in serialized and "evidence-1" not in serialized


@pytest.mark.parametrize(
    ("current_revision", "retention", "denied", "revision_kind", "reason"),
    [
        (UUID(int=204), True, set(), RevisionKind.CONTENT, CitationValidationReasonCode.EVIDENCE_INVALIDATED),
        (REVISION, False, set(), RevisionKind.CONTENT, CitationValidationReasonCode.EVIDENCE_INVALIDATED),
        (REVISION, True, set(), RevisionKind.TOMBSTONE, CitationValidationReasonCode.EVIDENCE_INVALIDATED),
    ],
)
@pytest.mark.asyncio
async def test_current_source_lifecycle_fails_closed(
    current_revision: UUID,
    retention: bool,
    denied: set[UUID],
    revision_kind: RevisionKind,
    reason: CitationValidationReasonCode,
) -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    repository = _AuthorizationRepository(
        {SOURCE: current_revision}, denied=denied, retention=retention,
        revision_kind=revision_kind,
    )
    result = await _service(bundle, record, repository).validate(
        citation=_citation(bundle, reference), expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context()
    )
    assert isinstance(result, RejectedCitation)
    assert result.reason_code is reason


@pytest.mark.asyncio
async def test_current_authorization_denial_fails_closed() -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    result = await _service(
        bundle, record,
        _AuthorizationRepository({SOURCE: REVISION}, denied={SOURCE}),
    ).validate(
        citation=_citation(bundle, reference), expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(),
    )
    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.AUTHORIZATION_DENIED


@pytest.mark.asyncio
async def test_inaccessible_derived_contributor_rejects_the_whole_citation() -> None:
    second_source, second_revision = UUID(int=205), UUID(int=206)
    first = _reference(); second = _reference(source_id=second_source, revision_id=second_revision, evidence_id="evidence-2")
    bundle = _bundle(first, second); record = _record(bundle, first, second)
    result = await _service(
        bundle, record,
        _AuthorizationRepository({SOURCE: REVISION, second_source: second_revision}, denied={second_source}),
    ).validate(
        citation=_citation(bundle, first), expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(),
    )
    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.AUTHORIZATION_DENIED


@pytest.mark.asyncio
async def test_cross_workspace_and_environment_fail_safely() -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    service = _service(bundle, record, _AuthorizationRepository({SOURCE: REVISION}), context=_context(authority_generation=2))
    cross_workspace = await service.validate(
        citation=_citation(bundle, reference), expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(workspace_id=UUID(int=999))
    )
    cross_environment = await service.validate(
        citation=_citation(bundle, reference),
        expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(environment_id=UUID(int=998)),
    )
    assert cross_workspace.reason_code is CitationValidationReasonCode.IDENTITY_INVALID
    assert cross_environment.reason_code is CitationValidationReasonCode.IDENTITY_INVALID


@pytest.mark.asyncio
async def test_authority_generation_change_fails_safely() -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    context = _context(authority_generation=2)
    service = _service(
        bundle, record, _AuthorizationRepository({SOURCE: REVISION}), context=context
    )
    stale_authority = await service.validate(
        citation=_citation(bundle, reference), expected_context_bundle=bundle.lineage_reference,
        authorization_context=context,
    )
    assert stale_authority.reason_code is CitationValidationReasonCode.AUTHORITY_STALE


@pytest.mark.parametrize(
    "reference_update",
    [
        {"original_sha256": "0" * 64},
        {"revision_digest": "0" * 64},
        {"locator_digest": "0" * 64},
    ],
)
@pytest.mark.asyncio
async def test_complete_provenance_identity_mismatch_fails_closed(
    reference_update: dict[str, object],
) -> None:
    claimed = _reference().model_copy(update=reference_update)
    bundle = _bundle(claimed)
    record = _record(bundle, claimed)
    result = await _service(
        bundle, record, _AuthorizationRepository({SOURCE: REVISION})
    ).validate(
        citation=_citation(bundle, claimed), expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(),
    )
    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.IDENTITY_INVALID


def test_contributor_with_mismatched_projection_partition_is_invalid() -> None:
    reference = _reference()
    bundle = _bundle(reference)
    contributor = _record(bundle, reference).contributors[0]
    member = contributor.projection_snapshot_member.model_copy(
        update={"projection_partition_id": UUID(int=9999)}
    )

    with pytest.raises(ValueError, match="identity is inconsistent"):
        CanonicalCitationContributor.model_validate(
            {**contributor.model_dump(), "projection_snapshot_member": member}
        )


@pytest.mark.asyncio
async def test_projection_kind_mismatch_fails_closed() -> None:
    reference = _reference()
    bundle = _bundle(reference)
    record = _record(bundle, reference)
    contributor = record.contributors[0]
    projection = contributor.source_projection_reference.model_copy(
        update={"projected_kind": "entity"}
    )
    record = record.model_copy(
        update={
            "contributors": (
                contributor.model_copy(
                    update={"source_projection_reference": projection}
                ),
            )
        }
    )
    result = await _service(
        bundle, record, _AuthorizationRepository({SOURCE: REVISION})
    ).validate(
        citation=_citation(bundle, reference),
        expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(),
    )

    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.IDENTITY_INVALID


@pytest.mark.asyncio
async def test_entity_evidence_with_matching_persisted_kind_is_valid() -> None:
    reference = _reference().model_copy(update={"projected_kind": "entity"})
    bundle = _bundle(reference)
    record = _record(bundle, reference)

    result = await _service(
        bundle, record, _AuthorizationRepository({SOURCE: REVISION})
    ).validate(
        citation=_citation(bundle, reference),
        expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(),
    )

    assert isinstance(result, ValidCitation)


@pytest.mark.asyncio
async def test_snapshot_member_from_another_workspace_fails_closed() -> None:
    reference = _reference()
    bundle = _bundle(reference)
    record = _record(bundle, reference)
    contributor = record.contributors[0]
    member = contributor.projection_snapshot_member.model_copy(
        update={"workspace_id": UUID(int=9999)}
    )
    record = record.model_copy(
        update={
            "contributors": (
                contributor.model_copy(
                    update={"projection_snapshot_member": member}
                ),
            )
        }
    )

    result = await _service(
        bundle, record, _AuthorizationRepository({SOURCE: REVISION})
    ).validate(
        citation=_citation(bundle, reference),
        expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(),
    )

    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.IDENTITY_INVALID


@pytest.mark.asyncio
async def test_independent_citation_ignores_other_bundle_policy_lineage() -> None:
    second_source, second_revision = UUID(int=205), UUID(int=206)
    first = _reference()
    second = _reference(
        source_id=second_source,
        revision_id=second_revision,
        evidence_id="evidence-2",
    )
    bundle = _bundle(first, second)
    record = _record(bundle, first)

    result = await _service(
        bundle, record, _AuthorizationRepository({SOURCE: REVISION})
    ).validate(
        citation=_citation(bundle, first),
        expected_context_bundle=bundle.lineage_reference,
        authorization_context=_context(),
    )

    assert isinstance(result, ValidCitation)
    assert tuple(
        item.source_object_id for item in result.facts.policy_decision_generations
    ) == (SOURCE,)


@pytest.mark.parametrize("record_count", [0, 2])
@pytest.mark.asyncio
async def test_missing_or_ambiguous_canonical_evidence_fails_closed(
    record_count: int,
) -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    context = _context()
    authorization = ContentAuthorizationDecisionService(
        repository=_AuthorizationRepository({SOURCE: REVISION}),
        identity_authority=_Identity(context), clock=lambda: NOW,
    )
    service = CitationValidationService(
        context_bundles=_Bundles(bundle),
        canonical_evidence=_Canonical(tuple(record for _ in range(record_count))),
        authorization=authorization,
        validation_reference_factory=lambda: UUID(int=201),
    )
    result = await service.validate(
        citation=_citation(bundle, reference), expected_context_bundle=bundle.lineage_reference,
        authorization_context=context
    )
    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.IDENTITY_INVALID


@pytest.mark.asyncio
async def test_citation_from_another_attempt_is_rejected_before_resolution() -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    expected = bundle.lineage_reference.model_copy(update={"attempt_id": UUID(int=1200)})
    result = await _service(
        bundle, record, _AuthorizationRepository({SOURCE: REVISION})
    ).validate(
        citation=_citation(bundle, reference), expected_context_bundle=expected,
        authorization_context=_context(),
    )
    assert isinstance(result, RejectedCitation)
    assert result.reason_code is CitationValidationReasonCode.IDENTITY_INVALID


@pytest.mark.asyncio
async def test_default_validation_reference_is_deterministic() -> None:
    reference = _reference(); bundle = _bundle(reference); record = _record(bundle, reference)
    context = _context()
    authorization = ContentAuthorizationDecisionService(
        repository=_AuthorizationRepository({SOURCE: REVISION}),
        identity_authority=_Identity(context), clock=lambda: NOW,
        decision_reference_factory=lambda: UUID(int=200),
    )
    service = CitationValidationService(
        context_bundles=_Bundles(bundle), canonical_evidence=_Canonical((record,)),
        authorization=authorization,
    )
    citation = _citation(bundle, reference)
    first = await service.validate(
        citation=citation, expected_context_bundle=bundle.lineage_reference,
        authorization_context=context,
    )
    second = await service.validate(
        citation=citation, expected_context_bundle=bundle.lineage_reference,
        authorization_context=context,
    )
    assert first == second
