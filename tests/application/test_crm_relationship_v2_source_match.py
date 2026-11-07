from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from spine.application.crm_relationship_v2_source_match import (
    CanonicalDiskSourceCandidate,
    CrmFilesItemIdentity,
    CrmMappedDiskRevisionEvidence,
    CrmRelationshipV2SourceMatchRequest,
    CrmRelationshipV2SourceMatchResult,
    CrmRelationshipV2SourceMatcher,
    DiskRevisionIdentity,
    ExistingCanonicalDiskSourceAuthority,
)
from spine.application.persistence.canonical import CanonicalAcceptanceReadResult
from spine.application.source_usage_guard import (
    CurrentSourceUsageDecision,
    SourceUsageReason,
)
from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskMembershipEvidence,
    Bitrix24DiskStorageClass,
)
from spine.domain.connections.provider import OpaqueProviderReference
from spine.domain.sources import (
    CanonicalAcceptance,
    CanonicalOrdering,
    CurrentLifecycleEligibility,
    CurrentSourceHead,
    ExternalFileRevisionMetadata,
    IdentityMode,
    LifecycleEligibilityStatus,
    ProcessingAdmissionMode,
    QualifiedProcessorReference,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceProcessingAdmission,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
    canonical_revision_metadata_digest,
)
from spine.domain.sources.canonicalization import content_revision
from spine.domain.sources.crm_relationship_v2 import CrmEligibilityOutcome
from spine.infrastructure.object_storage.contracts import ObjectReference

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def ident(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"crm-v2-source-match:{value}")


def files_item(value: str = "files-42") -> CrmFilesItemIdentity:
    return CrmFilesItemIdentity(
        namespace="bitrix24-crm-files-item",
        value=value,
    )


def disk_revision(
    *, object_id: str = "disk-42", revision: str = "17"
) -> DiskRevisionIdentity:
    return DiskRevisionIdentity(
        namespace="bitrix24-disk-object",
        object_id=object_id,
        provider_revision=revision,
    )


def mapping_evidence(
    *,
    item: CrmFilesItemIdentity | None = None,
    disk: DiskRevisionIdentity | None = None,
    strategy: str = "direct_disk_object",
) -> CrmMappedDiskRevisionEvidence:
    values: dict[str, object] = {
        "workspace_id": ident("workspace"),
        "environment_id": ident("environment"),
        "connection_id": ident("connection"),
        "connection_version_id": ident("connection-version"),
        "mapping_strategy": strategy,
        "mapping_profile_id": ident("profile"),
        "mapping_profile_version": 2,
        "files_item": item or files_item(),
        "disk_revision": disk or disk_revision(),
    }
    if strategy == "attached_object":
        values.update(
            ownership_proof_reference="qualification:attached-object:v1",
            ownership_proof_digest="e" * 64,
        )
    return CrmMappedDiskRevisionEvidence(**values)


def request(
    *,
    expected_item: CrmFilesItemIdentity | None = None,
    evidence: CrmMappedDiskRevisionEvidence | None = None,
) -> CrmRelationshipV2SourceMatchRequest:
    return CrmRelationshipV2SourceMatchRequest(
        workspace_id=ident("workspace"),
        environment_id=ident("environment"),
        connection_id=ident("connection"),
        connection_version_id=ident("connection-version"),
        relationship_files_item=expected_item or files_item(),
        mapping_evidence=evidence or mapping_evidence(),
    )


def source_candidate(
    *, object_id: str = "disk-42", revision_value: str = "17"
) -> CanonicalDiskSourceCandidate:
    workspace_id = ident("workspace")
    environment_id = ident("environment")
    connection_id = ident("connection")
    connection_version_id = ident("connection-version")
    source_object_id = ident(f"source:{object_id}")
    revision_id = ident(f"revision:{object_id}:{revision_value}")
    source = SourceObject(
        source_object_id=source_object_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_kind="document",
        identity_mode=IdentityMode.CONNECTOR,
        connection_id=connection_id,
        external_namespace="bitrix24-disk",
        external_generation="bitrix24-disk-object:v1",
        external_object_id=object_id,
        created_at=NOW,
    )
    metadata = RevisionMetadata(
        external_file_revision=ExternalFileRevisionMetadata(
            provider_kind="bitrix24",
            provider_revision=revision_value,
            provider_resource_id=object_id,
            byte_length=4,
            modified_at=NOW,
            identity_assurance="provider_stable",
            assurance_evidence_reference="bitrix24-disk-object:v1",
        )
    )
    original = ObjectReference(
        schema_version=1,
        object_id=ident(f"object:{object_id}:{revision_value}"),
        storage_generation=ident("storage-generation"),
        digest_algorithm="sha256",
        digest_hex="a" * 64,
        byte_length=4,
    )
    revision = content_revision(
        revision_id=revision_id,
        source_object_id=source_object_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=metadata,
        revision_metadata_digest=canonical_revision_metadata_digest(metadata),
        original_reference=original,
        original_sha256="a" * 64,
        byte_length=4,
        media_type="text/plain",
        observed_at=NOW,
    )
    membership = Bitrix24DiskMembershipEvidence(
        workspace_id=workspace_id,
        environment_id=environment_id,
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        resource_scope_reference=OpaqueProviderReference(value=ident("scope")),
        root_policy_id=ident("root-policy"),
        root_policy_version="root:v1",
        root_policy_digest="b" * 64,
        content_policy_id=ident("content-policy"),
        content_policy_version="content:v1",
        content_policy_digest="c" * 64,
        authorization_generation=3,
        configuration_generation=4,
        grant_binding_id=ident("grant"),
        grant_token_epoch=2,
        grant_security_fence=0,
        storage_id="storage-1",
        storage_class=Bitrix24DiskStorageClass.COMMON,
        storage_root_object_id="root-1",
        approved_root_folder_id="folder-1",
        object_id=object_id,
        parent_folder_ids=("folder-1",),
        provider_revision=revision_value,
    )
    provenance = SourceRevisionProvenance(
        provenance_id=ident(f"provenance:{object_id}:{revision_value}"),
        source_object_id=source_object_id,
        revision_id=revision_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        producer_kind="connector",
        producer_reference="bitrix24-disk-reader:v1",
        event_identity=f"disk-{object_id}-{revision_value}",
        event_digest="d" * 64,
        connection_id=connection_id,
        source_contract_id="bitrix24-disk",
        source_contract_version="1",
        resource_scope_reference=ident("scope"),
        observer_service="connector-worker",
        received_at=NOW,
        observed_at=NOW,
        bitrix24_disk_membership=membership,
    )
    receipt = SourceRevisionReceiptAssociation(
        association_id=ident(f"association:{object_id}:{revision_value}"),
        source_object_id=source_object_id,
        revision_id=revision_id,
        provenance_id=provenance.provenance_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=ident(f"receipt:{object_id}:{revision_value}"),
        original_command_result_id=ident(f"result:{object_id}:{revision_value}"),
        original_command_result_schema_version=1,
        original_upload_id=ident(f"upload:{object_id}:{revision_value}"),
        original_reference=original,
        original_sha256="a" * 64,
        byte_length=4,
        finalized_at=NOW,
        associated_at=NOW,
    )
    lifecycle = CurrentLifecycleEligibility(
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_object_id=source_object_id,
        connection_id=connection_id,
        connection_version_id=connection_version_id,
        scope_reference=ident("root-policy"),
        scope_version_id=ident("root-policy-version"),
        scope_digest="b" * 64,
        current_decision_id=ident("lifecycle-decision"),
        generation=7,
        status=LifecycleEligibilityStatus.ELIGIBLE,
        legal_hold_active=False,
    )
    return CanonicalDiskSourceCandidate(
        source=source,
        revision=revision,
        provenance=provenance,
        receipt=receipt,
        lifecycle=lifecycle,
    )


def current_head(candidate: CanonicalDiskSourceCandidate) -> CurrentSourceHead:
    return CurrentSourceHead(
        source_object_id=candidate.source.source_object_id,
        workspace_id=candidate.source.workspace_id,
        environment_id=candidate.source.environment_id,
        current_revision_id=candidate.revision.revision_id,
        current_revision_kind=RevisionKind.CONTENT,
        acceptance_id=ident("acceptance"),
        accepted_generation=5,
        cas_version=6,
        changed_at=NOW,
    )


def processing_admission(
    candidate: CanonicalDiskSourceCandidate,
) -> SourceProcessingAdmission:
    membership = candidate.provenance.bitrix24_disk_membership
    assert membership is not None
    return SourceProcessingAdmission(
        processing_admission_id=ident("processing-admission"),
        workspace_id=candidate.source.workspace_id,
        environment_id=candidate.source.environment_id,
        source_object_id=candidate.source.source_object_id,
        source_revision_id=candidate.revision.revision_id,
        selector_id=ident("selector"),
        provider_connection_id=membership.connection_id,
        provider_connection_version_id=membership.connection_version_id,
        provider_approval_id=ident("provider-approval"),
        resource_scope_reference=ident("scope"),
        authorization_generation=membership.authorization_generation,
        configuration_generation=membership.configuration_generation,
        grant_binding_id=membership.grant_binding_id,
        grant_token_epoch=membership.grant_token_epoch,
        grant_security_fence=membership.grant_security_fence,
        root_policy_id=membership.root_policy_id,
        root_policy_version=membership.root_policy_version,
        root_policy_digest=membership.root_policy_digest,
        content_policy_id=membership.content_policy_id,
        content_policy_version=membership.content_policy_version,
        content_policy_digest=membership.content_policy_digest,
        receipt_association_id=candidate.receipt.association_id,
        verified_sha256="a" * 64,
        verified_byte_length=4,
        verifier_evidence_id=ident("verifier-evidence"),
        verifier_profile_version="verifier:v1",
        verifier_evidence_digest="f" * 64,
        lifecycle_eligibility_reference=ident("lifecycle-decision"),
        lifecycle_eligibility_generation=7,
        retention_applicability_reference=ident("retention"),
        retention_applicability_generation=8,
        mode=ProcessingAdmissionMode.PROCESSOR_REQUIRED,
        processor=QualifiedProcessorReference(
            capability_id="parse_document",
            capability_version="parse-document:v1",
            processor_profile_version="parser:v1",
            qualification_evidence_reference=ident("processor-qualification"),
            qualification_evidence_digest="9" * 64,
        ),
        idempotency_receipt_id=ident("admission-idempotency"),
        audit_event_id=ident("admission-audit"),
        created_at=NOW,
    )


def acceptance_read(
    candidate: CanonicalDiskSourceCandidate,
    *,
    admission: SourceProcessingAdmission | None = None,
    head: CurrentSourceHead | None = None,
) -> CanonicalAcceptanceReadResult:
    authority = admission or processing_admission(candidate)
    current = head or current_head(candidate)
    acceptance = CanonicalAcceptance(
        canonical_acceptance_schema_version=2,
        canonical_acceptance_id=current.acceptance_id,
        source_object_id=candidate.source.source_object_id,
        source_revision_id=candidate.revision.revision_id,
        workspace_id=candidate.source.workspace_id,
        environment_id=candidate.source.environment_id,
        accepted_generation=current.accepted_generation,
        ordering=CanonicalOrdering.EXPECTED_CURRENT,
        admission_decision_id=ident("admission-decision"),
        access_policy_version_id=ident("access-policy"),
        processing_admission_id=authority.processing_admission_id,
        provenance_id=candidate.provenance.provenance_id,
        idempotency_receipt_id=ident("canonical-idempotency"),
        audit_event_id=ident("canonical-audit"),
        outbox_event_id=ident("canonical-outbox"),
        accepted_at=NOW,
    )
    return CanonicalAcceptanceReadResult(acceptance, authority, current)


class SourceFinderFake:
    def __init__(self, candidates: tuple[CanonicalDiskSourceCandidate, ...]) -> None:
        self.candidates = candidates
        self.queries = []
        self.transactions = []

    async def find_current(self, query, transaction):
        self.queries.append(query)
        self.transactions.append(transaction)
        return self.candidates


class CanonicalReaderFake:
    def __init__(
        self,
        candidate: CanonicalDiskSourceCandidate,
        *,
        head: CurrentSourceHead | None = None,
        read: CanonicalAcceptanceReadResult | None = None,
    ) -> None:
        self.head = head or current_head(candidate)
        self.read = read or acceptance_read(candidate, head=self.head)

    async def resolve_current(self, source_object_id):
        del source_object_id
        return self.head

    async def resolve_acceptance_read(self, acceptance_id):
        del acceptance_id
        return self.read


class UnitOfWorkFake:
    def __init__(self, canonical: CanonicalReaderFake) -> None:
        self.canonical = canonical
        self.write_count = 0
        self.active = False

    async def __aenter__(self):
        self.active = True
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback
        self.active = False


class UsageAuthorityFake:
    def __init__(self, reason: SourceUsageReason = SourceUsageReason.ALLOWED) -> None:
        self.reason = reason
        self.requests = []
        self.transaction = None
        self.transaction_was_active = False

    async def evaluate(self, usage):
        self.requests.append(usage)
        self.transaction_was_active = bool(
            self.transaction is not None and self.transaction.active
        )
        return CurrentSourceUsageDecision(
            allowed=self.reason is SourceUsageReason.ALLOWED,
            reason=self.reason,
            required_dependency_count=len(usage.dependencies),
            checked_dependencies=(
                usage.dependencies if self.reason is SourceUsageReason.ALLOWED else ()
            ),
        )


def matcher(
    candidate: CanonicalDiskSourceCandidate,
    *,
    candidates: tuple[CanonicalDiskSourceCandidate, ...] | None = None,
    canonical: CanonicalReaderFake | None = None,
    usage: UsageAuthorityFake | None = None,
):
    finder = SourceFinderFake(candidates if candidates is not None else (candidate,))
    uow = UnitOfWorkFake(canonical or CanonicalReaderFake(candidate))
    usage = usage or UsageAuthorityFake()

    def usage_for(transaction):
        usage.transaction = transaction
        return usage

    authority = ExistingCanonicalDiskSourceAuthority(
        uow_factory=lambda context: uow,
        source_finder=finder,
        usage_authority_factory=usage_for,
        clock=lambda: NOW,
    )
    return CrmRelationshipV2SourceMatcher(authority), finder, uow, usage


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["direct_disk_object", "attached_object"])
async def test_exact_disk_revision_returns_fenced_current_source(strategy: str) -> None:
    candidate = source_candidate()
    subject, finder, uow, usage = matcher(candidate)

    result = await subject.match(
        request(evidence=mapping_evidence(strategy=strategy)), SimpleNamespace()
    )

    assert result.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED
    assert result.source_object_id == candidate.source.source_object_id
    assert result.source_revision_id == candidate.revision.revision_id
    assert result.authority is not None
    assert result.authority.processing_admission == processing_admission(candidate)
    assert finder.queries[0].disk_revision == disk_revision()
    assert finder.transactions == [uow]
    assert (
        usage.requests[0].dependencies[0].source_revision_id
        == result.source_revision_id
    )
    assert usage.transaction_was_active is True
    assert uow.active is False
    assert uow.write_count == 0


def test_sibling_mapping_evidence_cannot_be_used_for_another_files_item() -> None:
    sibling = files_item("files-sibling")

    with pytest.raises(ValidationError, match="exact relationship FILES item"):
        request(expected_item=files_item(), evidence=mapping_evidence(item=sibling))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["workspace", "connection", "source", "receipt", "disk"]
)
async def test_wrong_scope_source_receipt_or_disk_revision_fails_closed(
    changed: str,
) -> None:
    valid = source_candidate()
    candidate = valid
    if changed == "workspace":
        candidate = valid.model_copy(
            update={
                "source": valid.source.model_copy(
                    update={"workspace_id": ident("other-workspace")}
                )
            }
        )
    elif changed == "connection":
        candidate = valid.model_copy(
            update={
                "source": valid.source.model_copy(
                    update={"connection_id": ident("other-connection")}
                )
            }
        )
    elif changed == "source":
        candidate = valid.model_copy(
            update={
                "provenance": valid.provenance.model_copy(
                    update={"source_object_id": ident("other-source")}
                )
            }
        )
    elif changed == "receipt":
        candidate = valid.model_copy(
            update={
                "receipt": valid.receipt.model_copy(
                    update={"revision_id": ident("old-revision")}
                )
            }
        )
    else:
        membership = valid.provenance.bitrix24_disk_membership
        assert membership is not None
        candidate = valid.model_copy(
            update={
                "provenance": valid.provenance.model_copy(
                    update={
                        "bitrix24_disk_membership": membership.model_copy(
                            update={"provider_revision": "18"}
                        )
                    }
                )
            }
        )
    subject, _, _, _ = matcher(valid, candidates=(candidate,))

    result = await subject.match(request(), SimpleNamespace())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert result.source_object_id is None


@pytest.mark.asyncio
async def test_changed_disk_revision_requires_its_own_match() -> None:
    old = source_candidate(revision_value="17")
    subject, _, _, _ = matcher(old)

    result = await subject.match(
        request(evidence=mapping_evidence(disk=disk_revision(revision="18"))),
        SimpleNamespace(),
    )

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT


@pytest.mark.asyncio
async def test_stale_current_head_is_rejected_before_source_use() -> None:
    candidate = source_candidate()
    stale = current_head(candidate).model_copy(
        update={"current_revision_id": ident("new-revision")}
    )
    subject, _, _, usage = matcher(
        candidate, canonical=CanonicalReaderFake(candidate, head=stale)
    )

    result = await subject.match(request(), SimpleNamespace())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT
    assert usage.requests == []


@pytest.mark.asyncio
async def test_mismatched_policy_admission_is_ineligible() -> None:
    candidate = source_candidate()
    mismatched = processing_admission(candidate).model_copy(
        update={"root_policy_digest": "0" * 64}
    )
    read = acceptance_read(candidate, admission=mismatched)
    subject, _, _, usage = matcher(
        candidate, canonical=CanonicalReaderFake(candidate, read=read)
    )

    result = await subject.match(request(), SimpleNamespace())

    assert result.outcome is CrmEligibilityOutcome.SOURCE_INELIGIBLE
    assert usage.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "status", "outcome"),
    [
        (
            SourceUsageReason.PROCESSING_INELIGIBLE,
            LifecycleEligibilityStatus.ELIGIBLE,
            CrmEligibilityOutcome.STALE_CONTEXT,
        ),
        (
            SourceUsageReason.LIFECYCLE_INELIGIBLE,
            LifecycleEligibilityStatus.READ_WITHDRAWN,
            CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN,
        ),
        (
            SourceUsageReason.RETENTION_INELIGIBLE,
            LifecycleEligibilityStatus.ELIGIBLE,
            CrmEligibilityOutcome.SOURCE_INELIGIBLE,
        ),
        (
            SourceUsageReason.AUTHORITY_UNAVAILABLE,
            LifecycleEligibilityStatus.ELIGIBLE,
            CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE,
        ),
    ],
)
async def test_transaction_authority_returns_typed_safe_denial(
    reason: SourceUsageReason,
    status: LifecycleEligibilityStatus,
    outcome: CrmEligibilityOutcome,
) -> None:
    candidate = source_candidate()
    candidate = candidate.model_copy(
        update={"lifecycle": candidate.lifecycle.model_copy(update={"status": status})}
    )
    subject, _, _, _ = matcher(candidate, usage=UsageAuthorityFake(reason))

    result = await subject.match(request(), SimpleNamespace())

    assert result.outcome is outcome
    assert result.source_object_id is None
    assert result.authority is None


@pytest.mark.asyncio
async def test_duplicate_source_match_is_ambiguous() -> None:
    candidate = source_candidate()
    subject, _, _, _ = matcher(candidate, candidates=(candidate, candidate))

    result = await subject.match(request(), SimpleNamespace())

    assert result.outcome is CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE


@pytest.mark.asyncio
async def test_missing_source_match_is_stale_context() -> None:
    candidate = source_candidate()
    subject, _, _, _ = matcher(candidate, candidates=())

    result = await subject.match(request(), SimpleNamespace())

    assert result.outcome is CrmEligibilityOutcome.STALE_CONTEXT


def test_positive_result_requires_complete_exact_source_handoff() -> None:
    with pytest.raises(ValidationError, match="complete handoff"):
        CrmRelationshipV2SourceMatchResult(
            outcome=CrmEligibilityOutcome.ELIGIBLE_VERIFIED
        )


def test_attached_mapping_requires_exact_ownership_proof() -> None:
    values = mapping_evidence(strategy="attached_object").model_dump(
        exclude={"ownership_proof_reference", "ownership_proof_digest"}
    )

    with pytest.raises(ValidationError, match="ownership proof"):
        CrmMappedDiskRevisionEvidence.model_validate(values)
