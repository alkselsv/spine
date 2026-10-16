from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.citation_inspector import (
    CitationInspectorRequester,
    ProtectedCitationInspector,
)
from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.capabilities import compose_answer_disclosure
from spine.domain.disclosure import (
    CitationExcerpt,
    CitationInspectionMode,
    CitationInspectionStatus,
    DisclosureRevalidationDisposition,
    DisclosureRevalidationResult,
    DisclosureDependencyObservation,
    DisclosurePublicationObservation,
    DisclosureRevalidationObservation,
)
from spine.application.disclosure_revalidation import (
    DisclosureRevalidationService,
    InMemoryDisclosureValidationResultRepository,
)

from tests.domain.capabilities.test_answer_disclosure import _request
from tests.application.test_citation_validation import _bundle, _reference


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SUBJECT = UUID(int=3)


def context(
    *, workspace_id: UUID = WORKSPACE, environment_id: UUID = ENVIRONMENT
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_citations"),
        trace_id=UUID(int=4),
        acting_subject_id=SUBJECT,
        service_principal_id=None,
        provenance=TrustedContextProvenance(issuer_id=UUID(int=5), signature=b"trusted"),
    )


class CitationRepository:
    def __init__(self, accepted):
        self.accepted = accepted

    async def resolve_citation(self, *, workspace_id, environment_id, citation_id):
        citation = self.accepted.answer.citations[0]
        if (
            workspace_id == self.accepted.answer.workspace_id
            and environment_id == self.accepted.answer.environment_id
            and citation_id == citation.citation_id
        ):
            return self.accepted
        return None


class ContextBundleRepository:
    def __init__(self, bundle):
        self.bundle = bundle

    async def resolve(self, *, bundle_id, bundle_version, bundle_digest):
        if self.bundle.reference.model_dump() == {
            "bundle_id": bundle_id,
            "bundle_version": bundle_version,
            "bundle_digest": bundle_digest,
            "publication_pin_id": None,
        }:
            return self.bundle
        raise LookupError("bundle unavailable")


class AllowDisclosure:
    async def revalidate(self, request):
        return DisclosureRevalidationResult(
            request_id=request.request_id,
            request_digest=request.command_digest,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            checkpoint=request.checkpoint,
            disposition=DisclosureRevalidationDisposition.SAFE_REUSE,
            safe_reason="citation_disclosure_allowed",
        )


class ExactExcerptResolver:
    def __init__(self, **updates):
        self.updates = updates

    async def resolve(self, citation, *, historical):
        del historical
        evidence = citation.evidence_reference
        value = CitationExcerpt(
            source_object_id=evidence.source_object_id,
            source_revision_id=evidence.source_revision_id,
            original_sha256=evidence.original_sha256,
            revision_digest=evidence.revision_digest,
            locator_schema=evidence.locator_contract_version,
            locator_reference=evidence.locator.value,
            locator_digest=evidence.locator_digest,
            excerpt="The policy applies to this customer.",
        )
        return value.model_copy(update=self.updates) if self.updates else value


class CurrentState:
    def __init__(self, **updates):
        self.requester_authorized = updates.pop("requester_authorized", True)
        self.updates = updates

    async def resolve(self, request):
        dependency = request.dependencies.dependencies[0]
        values = {
            "dependency_id": dependency.dependency_id,
            "source_object_id": dependency.source_object_id,
            "current_policy_version_id": dependency.policy_version_id,
            "current_policy_generation": dependency.policy_generation,
            "membership_generation": dependency.membership_generation,
            "authority_generation": dependency.authority_generation,
            "service_authority_generation": dependency.service_authority_generation,
            "contributor_authorized": True,
            "current_revision_id": dependency.source_revision_id,
            "current_revision_digest": dependency.source_revision_digest,
            "revision_current": True,
            "retention_eligible": True,
            "historical_revision_available": True,
            "historical_revision_id": dependency.source_revision_id,
            "historical_revision_digest": dependency.source_revision_digest,
        }
        values.update(self.updates)
        return DisclosureRevalidationObservation(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            requester_authorized=self.requester_authorized,
            publication=DisclosurePublicationObservation(
                pinned_snapshot_id=dependency.projection_snapshot_id,
                pinned_activation_generation=dependency.activation_generation,
                pinned_boundary_id=dependency.canonical_boundary_id,
                pinned_as_of_boundary_id=dependency.canonical_as_of_boundary_id,
                pinned_as_of_generation=dependency.canonical_as_of_generation,
            ),
            dependencies=(DisclosureDependencyObservation(**values),),
        )


def inspector_with_state(accepted, bundle, state) -> ProtectedCitationInspector:
    return ProtectedCitationInspector(
        citation_repository=CitationRepository(accepted),
        context_bundle_repository=ContextBundleRepository(bundle),
        disclosure_revalidator=DisclosureRevalidationService(
            state_provider=state,
            result_repository=InMemoryDisclosureValidationResultRepository(),
            clock=lambda: NOW,
        ),
        excerpt_resolver=ExactExcerptResolver(),
    )


@pytest.mark.asyncio
async def test_valid_citation_inspection_releases_exact_authorized_excerpt() -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    bundle = _bundle(_reference())

    result = await ProtectedCitationInspector(
        citation_repository=CitationRepository(accepted),
        context_bundle_repository=ContextBundleRepository(bundle),
        disclosure_revalidator=AllowDisclosure(),
        excerpt_resolver=ExactExcerptResolver(),
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=accepted.answer.citations[0].citation_id,
    )

    assert result.status is CitationInspectionStatus.AVAILABLE
    assert result.evidence is not None
    assert result.evidence.excerpt == "The policy applies to this customer."
    assert result.evidence.evidence_reference == accepted.answer.citations[0].evidence_reference
    assert result.evidence.historical is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("original_sha256", "revision_digest", "locator_digest"))
async def test_fabricated_citation_identity_or_digest_fails_closed_without_excerpt(
    field: str,
) -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    citation = accepted.answer.citations[0]
    forged_evidence = citation.evidence_reference.model_copy(update={field: "f" * 64})
    forged = citation.model_copy(update={"evidence_reference": forged_evidence})

    result = await ProtectedCitationInspector(
        citation_repository=CitationRepository(accepted),
        context_bundle_repository=ContextBundleRepository(_bundle(_reference())),
        disclosure_revalidator=AllowDisclosure(),
        excerpt_resolver=ExactExcerptResolver(),
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=citation.citation_id,
        expected_citation=forged,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE
    assert "The policy applies" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_locator_resolver_mismatch_fails_closed_without_cached_excerpt_fallback() -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    citation = accepted.answer.citations[0]
    result = await ProtectedCitationInspector(
        citation_repository=CitationRepository(accepted),
        context_bundle_repository=ContextBundleRepository(_bundle(_reference())),
        disclosure_revalidator=AllowDisclosure(),
        excerpt_resolver=ExactExcerptResolver(locator_reference="different-locator"),
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=citation.citation_id,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE
    assert "The policy applies" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_citation_identity_isolated_between_workspaces_and_environments() -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    result = await ProtectedCitationInspector(
        citation_repository=CitationRepository(accepted),
        context_bundle_repository=ContextBundleRepository(_bundle(_reference())),
        disclosure_revalidator=AllowDisclosure(),
        excerpt_resolver=ExactExcerptResolver(),
    ).inspect(
        context=context(workspace_id=UUID(int=90), environment_id=UUID(int=91)),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=accepted.answer.citations[0].citation_id,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE
    assert result.evidence is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    (
        {"contributor_authorized": False},
        {"retention_eligible": False},
        {"tombstoned": True},
    ),
)
async def test_current_authority_failure_denies_citation_without_source_details(updates) -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    bundle = _bundle(_reference())
    result = await inspector_with_state(
        accepted, bundle, CurrentState(**updates)
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=accepted.answer.citations[0].citation_id,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE
    assert "The policy applies" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_historical_citation_flow_allows_retained_superseded_revision_only() -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    bundle = _bundle(_reference())
    result = await inspector_with_state(
        accepted,
        bundle,
        CurrentState(superseded=True, revision_current=False, current_revision_id=UUID(int=99)),
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=accepted.answer.citations[0].citation_id,
        mode=CitationInspectionMode.HISTORICAL,
    )

    assert result.status is CitationInspectionStatus.AVAILABLE
    assert result.evidence is not None and result.evidence.historical is True


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", ({"tombstoned": True}, {"retention_eligible": False}))
async def test_historical_citation_flow_still_denies_tombstone_or_retention_failure(
    updates,
) -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    bundle = _bundle(_reference())
    result = await inspector_with_state(
        accepted, bundle, CurrentState(**updates)
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=accepted.answer.citations[0].citation_id,
        mode=CitationInspectionMode.HISTORICAL,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_inspection_rejects_requester_that_differs_from_trusted_context() -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    result = await inspector_with_state(
        accepted, _bundle(_reference()), CurrentState()
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(UUID(int=99)),
        citation_id=accepted.answer.citations[0].citation_id,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_administrator_without_content_grant_cannot_inspect_citation() -> None:
    decision = compose_answer_disclosure(_request(), accepted_at=NOW)
    assert decision.accepted_answer is not None
    accepted = decision.accepted_answer
    bundle = _bundle(_reference())
    result = await inspector_with_state(
        accepted, bundle, CurrentState(requester_authorized=False)
    ).inspect(
        context=context(),
        requester=CitationInspectorRequester(SUBJECT),
        citation_id=accepted.answer.citations[0].citation_id,
    )

    assert result.status is CitationInspectionStatus.UNAVAILABLE
    assert result.evidence is None
