"""Fail-closed application seam for inspecting one protected citation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.context_bundles import ContextBundleRepository
from spine.application.persistence.answer_disclosure import AnswerDisclosureRepository
from spine.domain.capabilities import AcceptedGroundedAnswer, Citation
from spine.domain.context_bundles import ContextBundle
from spine.domain.disclosure import (
    CitationExcerpt,
    CitationInspectionEvidence,
    CitationInspectionMode,
    CitationInspectionReason,
    CitationInspectionResult,
    CitationInspectionStatus,
    DisclosureDependencySet,
    DisclosureDependency,
    DisclosureRevalidationCheckpoint,
    DisclosureRevalidationRequest,
    DisclosureRevalidationResult,
)


class CitationInspectorRequesterError(ValueError):
    """The trusted requester identity is not usable for inspection."""


class CitationInspectionUnavailable(LookupError):
    """Safe public failure for every missing or no-longer-eligible citation."""


@dataclass(frozen=True, slots=True)
class CitationInspectorRequester:
    subject_id: UUID
    authorization_generation: int = 1

    def __post_init__(self) -> None:
        if self.subject_id.int == 0 or self.authorization_generation < 1:
            raise CitationInspectorRequesterError("citation inspector requester is invalid")


class CitationDisclosureRevalidator(Protocol):
    async def revalidate(
        self, request: DisclosureRevalidationRequest
    ) -> DisclosureRevalidationResult: ...


class CitationExcerptResolver(Protocol):
    """Resolve a locator only after the application has authorized disclosure."""

    async def resolve(
        self, citation: Citation, *, historical: bool
    ) -> CitationExcerpt | None: ...


class CitationInspectorService(Protocol):
    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: CitationInspectorRequester,
        citation_id: UUID,
        mode: CitationInspectionMode = CitationInspectionMode.CURRENT,
        expected_citation: Citation | None = None,
    ) -> CitationInspectionResult: ...


@dataclass(frozen=True, slots=True)
class ProtectedCitationInspector:
    """Resolve accepted citation lineage and release an exact excerpt safely."""

    citation_repository: AnswerDisclosureRepository
    context_bundle_repository: ContextBundleRepository
    disclosure_revalidator: CitationDisclosureRevalidator
    excerpt_resolver: CitationExcerptResolver

    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: CitationInspectorRequester,
        citation_id: UUID,
        mode: CitationInspectionMode = CitationInspectionMode.CURRENT,
        expected_citation: Citation | None = None,
    ) -> CitationInspectionResult:
        try:
            workspace_id, environment_id = _scope(context)
            if (
                requester.subject_id != context.acting_subject_id
                or context.purpose.value != "knowledge_control"
                or context.operation.value != "inspect_citations"
            ):
                return _unavailable()
        except Exception:
            return _unavailable()
        if not isinstance(citation_id, UUID) or citation_id.int == 0:
            return _unavailable()

        try:
            accepted = await self.citation_repository.resolve_citation(
                workspace_id=workspace_id,
                environment_id=environment_id,
                citation_id=citation_id,
            )
        except Exception:
            return _unavailable()
        citation = _citation_from_answer(accepted, citation_id)
        if citation is None:
            return _unavailable()
        if expected_citation is not None and expected_citation != citation:
            return _unavailable()
        if (
            citation.workspace_id != workspace_id
            or citation.environment_id != environment_id
            or citation.context_bundle.workspace_id != workspace_id
            or citation.context_bundle.environment_id != environment_id
        ):
            return _unavailable()

        try:
            bundle = await self.context_bundle_repository.resolve(
                bundle_id=citation.context_bundle.bundle.bundle_id,
                bundle_version=citation.context_bundle.bundle.bundle_version,
                bundle_digest=citation.context_bundle.bundle.bundle_digest,
            )
        except Exception:
            return _unavailable()
        if not _bundle_matches_citation(bundle, citation):
            return _unavailable()
        try:
            dependencies = DisclosureDependencySet(
                dependencies=(
                    DisclosureDependency.from_context_bundle(
                        bundle,
                        source_object_id=citation.source_object_id,
                    ),
                )
            )
        except Exception:
            return _unavailable()

        revalidation = await self._revalidate(
            requester=requester,
            citation=citation,
            dependencies=dependencies,
            mode=mode,
        )
        if revalidation is None or not revalidation.can_disclose_content:
            return _unavailable()

        try:
            resolved = await self.excerpt_resolver.resolve(
                citation, historical=mode is CitationInspectionMode.HISTORICAL
            )
        except Exception:
            return _unavailable()
        if resolved is None or not _excerpt_matches_citation(resolved, citation):
            return _unavailable()
        try:
            evidence = CitationInspectionEvidence(
                citation_id=citation.citation_id,
                answer_id=citation.answer_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=citation.source_object_id,
                source_revision_id=citation.source_revision_id,
                original_sha256=resolved.original_sha256,
                revision_digest=resolved.revision_digest,
                revision_number=resolved.revision_number,
                locator_schema=resolved.locator_schema,
                locator_reference=resolved.locator_reference,
                locator_digest=resolved.locator_digest,
                context_bundle=citation.context_bundle,
                evidence_reference=citation.evidence_reference,
                excerpt=resolved.excerpt,
                historical=mode is CitationInspectionMode.HISTORICAL,
            )
        except Exception:
            return _unavailable()
        return CitationInspectionResult(
            status=CitationInspectionStatus.AVAILABLE,
            reason=CitationInspectionReason.AVAILABLE,
            evidence=evidence,
        )

    async def _revalidate(
        self,
        *,
        requester: CitationInspectorRequester,
        citation: Citation,
        dependencies: DisclosureDependencySet,
        mode: CitationInspectionMode,
    ) -> DisclosureRevalidationResult | None:
        request = DisclosureRevalidationRequest(
            request_id=uuid5(
                NAMESPACE_URL,
                "spine:citation-inspection:"
                f"{citation.citation_id}:{requester.subject_id}:"
                f"{requester.authorization_generation}:{mode.value}",
            ),
            workspace_id=citation.workspace_id,
            environment_id=citation.environment_id,
            requester_id=requester.subject_id,
            checkpoint=DisclosureRevalidationCheckpoint.BEFORE_CITATION_INSPECTION,
            dependencies=dependencies,
            run_id=citation.run_id,
            attempt_id=citation.attempt_id,
            historical_inspection=mode is CitationInspectionMode.HISTORICAL,
            force_fresh=True,
        )
        try:
            return await self.disclosure_revalidator.revalidate(request)
        except Exception:
            return None


def _citation_from_answer(
    accepted: AcceptedGroundedAnswer | None, citation_id: UUID
) -> Citation | None:
    if accepted is None:
        return None
    return next(
        (citation for citation in accepted.answer.citations if citation.citation_id == citation_id),
        None,
    )


def _bundle_matches_citation(bundle: ContextBundle, citation: Citation) -> bool:
    try:
        evidence = citation.evidence_reference
        return (
            ContextBundle.calculate_digest(bundle) == bundle.bundle_digest
            and bundle.workspace_id == citation.workspace_id
            and bundle.environment_id == citation.environment_id
            and bundle.lineage_reference == citation.context_bundle
            and evidence in bundle.evidence_references
            and evidence.status.value == "valid"
            and evidence.projection_snapshot_id
            == bundle.projection_snapshot.projection_snapshot_id
            and evidence.activation_generation == bundle.activation_generation
        )
    except Exception:
        return False


def _excerpt_matches_citation(resolved: CitationExcerpt, citation: Citation) -> bool:
    try:
        evidence = citation.evidence_reference
        return (
            resolved.source_object_id == evidence.source_object_id
            and resolved.source_revision_id == evidence.source_revision_id
            and resolved.original_sha256 == evidence.original_sha256
            and resolved.revision_digest == evidence.revision_digest
            and resolved.revision_number == evidence.revision_number
            and resolved.locator_schema == evidence.locator_contract_version
            and resolved.locator_reference == evidence.locator.value
            and resolved.locator_digest == evidence.locator_digest
        )
    except Exception:
        return False


def _unavailable() -> CitationInspectionResult:
    return CitationInspectionResult(
        status=CitationInspectionStatus.UNAVAILABLE,
        reason=CitationInspectionReason.CITATION_UNAVAILABLE,
    )


def _scope(context: TrustedPersistenceContext) -> tuple[UUID, UUID]:
    context.validate_shape()
    if not isinstance(context.scope, EnvironmentScope):
        raise CitationInspectorRequesterError(
            "citation inspection requires an environment scope"
        )
    return context.scope.workspace_id, context.scope.environment_id


__all__ = [
    "CitationDisclosureRevalidator",
    "CitationExcerptResolver",
    "CitationInspectorRequester",
    "CitationInspectorRequesterError",
    "CitationInspectionUnavailable",
    "CitationInspectorService",
    "ProtectedCitationInspector",
]
