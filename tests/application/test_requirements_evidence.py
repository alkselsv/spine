from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from spine.application.content_authorization import (
    AuthorizationDecisionKind,
    AuthorizationEvidence,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
)
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.requirements_evidence import (
    RequirementsEvidenceInspectionStatus,
    RequirementsEvidenceResolution,
    RequirementsEvidenceResolutionStatus,
    PersistedRequirementsEvidenceResolver,
    RequirementsEvidenceValidationService,
)
from spine.domain.artifacts import (
    ArtifactRef,
    ParsedSegment,
    PhysicalLineLocator,
)
from spine.domain.artifacts.models import segment_digest
from spine.domain.common import ContextOrigin
from spine.domain.sources import RevisionKind
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    Requirement,
    RequirementEvidenceRef,
    RequirementEvidenceStatus,
    RequirementEvidenceValidationStatus,
    RequirementsArtifact,
)


WORKSPACE_ID = UUID(int=1)
ENVIRONMENT_ID = UUID(int=2)
SOURCE_OBJECT_ID = UUID(int=3)
SOURCE_REVISION_ID = UUID(int=4)
PROVENANCE_ID = UUID(int=5)
PARSED_ARTIFACT_ID = UUID(int=6)
REQUIREMENT_ID = UUID(int=7)
SUBJECT_ID = UUID(int=8)
DECISION_ID = UUID(int=9)
VALIDATION_ID = UUID(int=10)
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_parsed_content"),
        trace_id=UUID(int=11),
        acting_subject_id=SUBJECT_ID,
        service_principal_id=None,
        provenance=TrustedContextProvenance(issuer_id=UUID(int=12), signature=b"trusted"),
    )


def requester() -> ParsedContentInspectionRequester:
    return ParsedContentInspectionRequester(subject_id=SUBJECT_ID)


def evidence() -> RequirementEvidenceRef:
    source_ref = AcceptedSourceRevisionRef(
        source_object_id=SOURCE_OBJECT_ID,
        source_revision_id=SOURCE_REVISION_ID,
        provenance_id=PROVENANCE_ID,
    )
    locator = PhysicalLineLocator(
        kind="physical_line", line_number=4, start_codepoint=0, end_codepoint=6
    )
    return RequirementEvidenceRef(
        source_ref=source_ref,
        parsed_artifact_ref=ArtifactRef(
            id=PARSED_ARTIFACT_ID,
            schema_name="parsed-document",
            schema_version="parsed-document:v1",
        ),
        segment_id=UUID("4a4c8f48-0d70-5bb6-997a-9e0c2a774836"),
        segment_digest="a" * 64,
        locator=locator,
    )


def segment() -> ParsedSegment:
    locator = PhysicalLineLocator(
        kind="physical_line", line_number=4, start_codepoint=0, end_codepoint=6
    )
    text = "secret"
    text_digest = hashlib.sha256(text.encode()).hexdigest()
    artifact_digest = "b" * 64
    digest = segment_digest(
        artifact_digest=artifact_digest,
        locator=locator,
        text_digest=text_digest,
        heading_path=(),
    )
    return ParsedSegment(
        segment_id=uuid5(NAMESPACE_URL, f"spine:parsed-segment:v1:{digest}"),
        segment_digest=digest,
        artifact_digest=artifact_digest,
        text=text,
        text_digest=text_digest,
        locator=locator,
    )


class Resolver:
    def __init__(self, result: RequirementsEvidenceResolution) -> None:
        self.result = result

    async def resolve(self, *, context, evidence):
        del context, evidence
        return self.result


class Authorizer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    async def decide_read_content(
        self, *, context, requester, source_object_id, source_revision_id
    ) -> ContentAuthorizationDecision:
        del requester
        return ContentAuthorizationDecision(
            allowed=self.allowed,
            decision_kind=AuthorizationDecisionKind.HUMAN_DISCLOSURE,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            source_object_ids=(source_object_id,),
            policy_evidence=(
                (
                    AuthorizationEvidence(
                        source_object_id=source_object_id,
                        source_revision_id=source_revision_id,
                        policy_version_id=UUID(int=13),
                        policy_generation=1,
                    ),
                )
                if self.allowed
                else ()
            ),
            membership_generation=1,
            authority_generation=1,
            authority_snapshot_reference=UUID(int=14),
            purpose="knowledge_control",
            operation="inspect_parsed_content",
            reason_code=(
                AuthorizationReasonCode.ALLOWED
                if self.allowed
                else AuthorizationReasonCode.AUTHORIZATION_DENIED
            ),
            decision_reference=DECISION_ID,
            trace_id=context.trace_id,
            evaluated_at=NOW,
        )


class Audit:
    async def record_decision(self, **_kwargs):
        return UUID(int=20)


def service(*, resolution: RequirementsEvidenceResolution, allowed: bool) -> RequirementsEvidenceValidationService:
    return RequirementsEvidenceValidationService(
        resolver=Resolver(resolution),
        authorizer=Authorizer(allowed),
        decision_audit=Audit(),
        validation_reference_factory=lambda: VALIDATION_ID,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_inspect_returns_exact_text_after_current_authorization() -> None:
    item = evidence()
    parsed_segment = segment()
    item = item.model_copy(
        update={
            "segment_id": parsed_segment.segment_id,
            "segment_digest": parsed_segment.segment_digest,
        }
    )
    result = await service(
        resolution=RequirementsEvidenceResolution(
            RequirementsEvidenceResolutionStatus.VERIFIED, parsed_segment
        ),
        allowed=True,
    ).inspect(context=context(), requester=requester(), evidence=item)

    assert result.status is RequirementsEvidenceInspectionStatus.AVAILABLE
    assert result.evidence is not None
    assert result.evidence.text == "secret"
    assert result.evidence.validation_reference == UUID(int=20)
    assert result.evidence.authorization_decision_reference == DECISION_ID


@pytest.mark.asyncio
async def test_inspect_denied_never_returns_protected_text() -> None:
    item = evidence()
    parsed_segment = segment()
    item = item.model_copy(
        update={
            "segment_id": parsed_segment.segment_id,
            "segment_digest": parsed_segment.segment_digest,
        }
    )
    result = await service(
        resolution=RequirementsEvidenceResolution(
            RequirementsEvidenceResolutionStatus.VERIFIED, parsed_segment
        ),
        allowed=False,
    ).inspect(context=context(), requester=requester(), evidence=item)

    assert result.status is RequirementsEvidenceInspectionStatus.DENIED
    assert result.evidence is None
    assert "secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_inspect_rechecks_authorization_after_artifact_creation() -> None:
    item = evidence()
    parsed_segment = segment()
    item = item.model_copy(
        update={
            "segment_id": parsed_segment.segment_id,
            "segment_digest": parsed_segment.segment_digest,
        }
    )
    created_result = await service(
        resolution=RequirementsEvidenceResolution(
            RequirementsEvidenceResolutionStatus.VERIFIED, parsed_segment
        ),
        allowed=True,
    ).inspect(context=context(), requester=requester(), evidence=item)
    revoked_result = await service(
        resolution=RequirementsEvidenceResolution(
            RequirementsEvidenceResolutionStatus.VERIFIED, parsed_segment
        ),
        allowed=False,
    ).inspect(context=context(), requester=requester(), evidence=item)

    assert created_result.status is RequirementsEvidenceInspectionStatus.AVAILABLE
    assert revoked_result.status is RequirementsEvidenceInspectionStatus.DENIED
    assert revoked_result.evidence is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        RequirementsEvidenceResolutionStatus.MISSING,
        RequirementsEvidenceResolutionStatus.STALE,
        RequirementsEvidenceResolutionStatus.TOMBSTONED,
        RequirementsEvidenceResolutionStatus.LOCATOR_MISMATCH,
        RequirementsEvidenceResolutionStatus.FABRICATED,
        RequirementsEvidenceResolutionStatus.DEGRADED,
    ],
)
async def test_inspect_unavailable_status_never_returns_source_metadata(
    status: RequirementsEvidenceResolutionStatus,
) -> None:
    result = await service(
        resolution=RequirementsEvidenceResolution(status), allowed=True
    ).inspect(context=context(), requester=requester(), evidence=evidence())

    assert result.status.value == status.value
    assert result.evidence is None
    assert "secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_validate_artifact_records_current_validation_lineage() -> None:
    item = evidence()
    parsed_segment = segment()
    item = item.model_copy(
        update={
            "segment_id": parsed_segment.segment_id,
            "segment_digest": parsed_segment.segment_digest,
        }
    )
    requirement = Requirement(
        requirement_id=REQUIREMENT_ID,
        category="functional",
        statement="Deliver by Friday",
        source_refs=(item.source_ref,),
        evidence_refs=(item,),
    )
    artifact = RequirementsArtifact(
        artifact_id=UUID(int=15),
        artifact_version=1,
        production_identity="requirements-extraction-v1",
        production_digest="c" * 64,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        work_item_id=UUID(int=16),
        run_id=UUID(int=17),
        requirements=(requirement,),
    )

    validated = await service(
        resolution=RequirementsEvidenceResolution(
            RequirementsEvidenceResolutionStatus.VERIFIED, parsed_segment
        ),
        allowed=True,
    ).validate_artifact(context=context(), requester=requester(), artifact=artifact)

    updated = validated.requirements[0].evidence_refs[0]
    assert artifact.requirements[0].evidence_refs[0].validation is None
    assert updated.validation is not None
    assert updated.validation.status is RequirementEvidenceValidationStatus.VALID
    assert updated.validation.validation_reference == UUID(int=20)
    assert updated.validation.authorization_decision_reference == DECISION_ID
    assert validated.requirements[0].evidence_status is RequirementEvidenceStatus.REFERENCED


class _PersistedFixture:
    def __init__(
        self,
        *,
        current_revision_id: UUID = SOURCE_REVISION_ID,
        current_revision_kind: RevisionKind = RevisionKind.CONTENT,
        source_workspace_id: UUID = WORKSPACE_ID,
        document_workspace_id: UUID = WORKSPACE_ID,
        document_locator_contract_version: str = "locator:v1",
        include_segment: bool = True,
        locator_mismatch: bool = False,
    ) -> None:
        self.source = SimpleNamespace(
            source_object_id=SOURCE_OBJECT_ID,
            workspace_id=source_workspace_id,
            environment_id=ENVIRONMENT_ID,
        )
        self.revision = SimpleNamespace(
            revision_id=SOURCE_REVISION_ID,
            source_object_id=SOURCE_OBJECT_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            revision_digest="d" * 64,
        )
        self.provenance = SimpleNamespace(
            provenance_id=PROVENANCE_ID,
            source_object_id=SOURCE_OBJECT_ID,
            revision_id=SOURCE_REVISION_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
        self.head = SimpleNamespace(
            current_revision_id=current_revision_id,
            current_revision_kind=current_revision_kind,
        )
        parsed = segment()
        if locator_mismatch:
            parsed = SimpleNamespace(
                segment_id=evidence().segment_id,
                segment_digest=evidence().segment_digest,
                locator=PhysicalLineLocator(
                    kind="physical_line",
                    line_number=5,
                    start_codepoint=0,
                    end_codepoint=6,
                ),
            )
        self.document = SimpleNamespace(
            workspace_id=document_workspace_id,
            environment_id=ENVIRONMENT_ID,
            source_object_id=SOURCE_OBJECT_ID,
            source_revision_id=SOURCE_REVISION_ID,
            source_revision_digest="d" * 64,
            schema_version="parsed-document:v1",
            locator_contract_version=document_locator_contract_version,
            coverage=SimpleNamespace(value="complete"),
            segments=(parsed,) if include_segment else (),
        )

    def uow_factory(self, _context):
        fixture = self

        class UnitOfWork:
            class Sources:
                async def resolve_source(self, _source_object_id):
                    return fixture.source

                async def resolve_revision(self, _source_revision_id):
                    return fixture.revision

                async def list_provenance(self, _revision_id):
                    return (fixture.provenance,)

            class Canonical:
                async def resolve_current(self, _source_object_id):
                    return fixture.head

            sources = Sources()
            canonical = Canonical()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        return UnitOfWork()


class _ParsedArtifacts:
    def __init__(self, fixture: _PersistedFixture) -> None:
        self.fixture = fixture

    async def get(self, **_kwargs):
        return self.fixture.document


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_kwargs", "expected"),
    [
        ({"current_revision_id": UUID(int=18)}, RequirementsEvidenceResolutionStatus.STALE),
        ({"current_revision_kind": RevisionKind.TOMBSTONE}, RequirementsEvidenceResolutionStatus.TOMBSTONED),
        ({"include_segment": False}, RequirementsEvidenceResolutionStatus.MISSING),
        ({"document_workspace_id": UUID(int=19)}, RequirementsEvidenceResolutionStatus.FABRICATED),
        ({"source_workspace_id": UUID(int=19)}, RequirementsEvidenceResolutionStatus.FABRICATED),
        ({"locator_mismatch": True}, RequirementsEvidenceResolutionStatus.LOCATOR_MISMATCH),
        ({"document_locator_contract_version": "locator:v2"}, RequirementsEvidenceResolutionStatus.FABRICATED),
    ],
)
async def test_persisted_resolver_fails_closed_on_lineage_drift(
    fixture_kwargs: dict[str, object],
    expected: RequirementsEvidenceResolutionStatus,
) -> None:
    fixture = _PersistedFixture(**fixture_kwargs)
    result = await PersistedRequirementsEvidenceResolver(
        uow_factory=fixture.uow_factory,
        parsed_artifacts=_ParsedArtifacts(fixture),
    ).resolve(context=context(), evidence=evidence())

    assert result.status is expected
