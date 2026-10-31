from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.trusted_content import TrustedContentEvidenceConflict
from spine.domain.sources import (
    SourceRevisionReceiptAssociation,
    TrustedContentFormat,
    TrustedContentVerificationEvidence,
    TrustedContentVerificationOutcome,
    VerificationCleanupOutcome,
    content_revision,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.persistence.trusted_content import (
    InMemoryTrustedContentEvidenceRepository,
)


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")


def _evidence(
    *, workspace_id: UUID = WORKSPACE_ID, reason_codes: tuple[str, ...] = ("verified",)
):
    reference = ObjectReference(
        schema_version=1,
        object_id=UUID("30000000-0000-4000-8000-000000000001"),
        storage_generation=UUID("40000000-0000-4000-8000-000000000001"),
        digest_algorithm="sha256",
        digest_hex="a" * 64,
        byte_length=4,
    )
    revision = content_revision(
        revision_id=UUID("50000000-0000-4000-8000-000000000001"),
        source_object_id=UUID("60000000-0000-4000-8000-000000000001"),
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT_ID,
        kind="content",
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata={"embedded_title": "Synthetic"},
        original_reference=reference,
        original_sha256=reference.digest_hex,
        byte_length=4,
        media_type="text/plain",
        observed_at=NOW,
    )
    association = SourceRevisionReceiptAssociation(
        association_id=UUID("70000000-0000-4000-8000-000000000001"),
        source_object_id=revision.source_object_id,
        revision_id=revision.revision_id,
        provenance_id=UUID("80000000-0000-4000-8000-000000000001"),
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT_ID,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=UUID("90000000-0000-4000-8000-000000000001"),
        original_command_result_id=UUID("a0000000-0000-4000-8000-000000000001"),
        original_command_result_schema_version=1,
        original_upload_id=UUID("b0000000-0000-4000-8000-000000000001"),
        original_reference=reference,
        original_sha256=reference.digest_hex,
        byte_length=4,
        finalized_at=NOW,
        associated_at=NOW,
    )
    evidence = TrustedContentVerificationEvidence.issue(
        evidence_id=UUID("c0000000-0000-4000-8000-000000000001"),
        revision=revision,
        association=association,
        recognized_format=TrustedContentFormat.TEXT,
        format_profile_version="text:v1",
        verifier_profile_version="verifier:v1",
        bounds_profile_version="bounds:v1",
        outcome=TrustedContentVerificationOutcome.VERIFIED,
        reason_codes=reason_codes,
        cleanup_outcome=VerificationCleanupOutcome.COMPLETED,
        verified_at=NOW,
    )
    return association, evidence


async def test_evidence_repository_is_append_only_and_association_scoped() -> None:
    association, evidence = _evidence()
    repository = InMemoryTrustedContentEvidenceRepository(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        )
    )

    assert await repository.append(evidence) == evidence
    assert await repository.append(evidence) == evidence
    assert await repository.resolve(evidence.evidence_id) == evidence
    assert await repository.list_for_association(association.association_id) == (evidence,)

    _, changed = _evidence(reason_codes=("changed",))
    with pytest.raises(TrustedContentEvidenceConflict, match="immutable"):
        await repository.append(changed)


async def test_evidence_repository_denies_cross_workspace_write_and_read() -> None:
    _, evidence = _evidence()
    other_scope = EnvironmentScope(
        workspace_id=UUID("d0000000-0000-4000-8000-000000000001"),
        environment_id=ENVIRONMENT_ID,
    )
    records = {}
    owner_repository = InMemoryTrustedContentEvidenceRepository(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        records=records,
    )
    repository = InMemoryTrustedContentEvidenceRepository(
        scope=other_scope, records=records
    )

    await owner_repository.append(evidence)

    with pytest.raises(TrustedContentEvidenceConflict, match="scope"):
        await repository.append(evidence)
    assert await repository.resolve(evidence.evidence_id) is None
