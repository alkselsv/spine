"""PostgreSQL-backed current source-use guards for projection transactions."""

from __future__ import annotations

from spine.application.processing_disposition import (
    CanonicalCurrentProcessingDispositionAuthority,
)
from spine.application.source_usage_guard import (
    CurrentSourceUsageDecision,
    SourceUsageReason,
    SourceUsageKind,
    SourceUsageRequest,
)
from spine.infrastructure.current_source_constraints import (
    current_lifecycle_allows,
    current_retention_allows,
    lock_source_usage_currentness,
    projection_hold_allows,
)


class TransactionCurrentSourceUsageAuthority:
    """Evaluate all required authorities using the caller's tenant transaction."""

    def __init__(self, uow: object) -> None:
        self._uow = uow

    async def evaluate(
        self,
        request: SourceUsageRequest,
    ) -> CurrentSourceUsageDecision:
        required = len(request.dependencies)
        uow = self._uow
        if uow is None or not hasattr(uow, "canonical") or not hasattr(uow, "_execute"):
            return self._deny(SourceUsageReason.AUTHORITY_UNAVAILABLE, required, ())
        for dependency in sorted(
            request.dependencies,
            key=lambda item: (item.source_object_id.int, item.source_revision_id.int),
        ):
            if not await lock_source_usage_currentness(
                uow,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                source_object_id=dependency.source_object_id,
            ):
                return self._deny(SourceUsageReason.AUTHORITY_UNAVAILABLE, required, ())
        checked = []
        for dependency in request.dependencies:
            processing = await CanonicalCurrentProcessingDispositionAuthority(
                uow.canonical
            ).resolve(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                source_object_id=dependency.source_object_id,
                source_revision_id=dependency.source_revision_id,
            )
            if not processing.eligible:
                return self._deny(
                    SourceUsageReason.PROCESSING_INELIGIBLE,
                    required,
                    tuple(checked),
                )
            if not await current_lifecycle_allows(
                uow,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                source_object_id=dependency.source_object_id,
                expected_decision_id=processing.lifecycle_eligibility_reference,
                expected_generation=processing.lifecycle_eligibility_generation,
            ):
                return self._deny(
                    SourceUsageReason.LIFECYCLE_INELIGIBLE,
                    required,
                    tuple(checked),
                )
            if not await current_retention_allows(
                uow,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                source_object_id=dependency.source_object_id,
                source_revision_id=dependency.source_revision_id,
                artifact_class=request.artifact.artifact_class,
                evaluated_at=request.evaluated_at,
                expected_applicability_id=processing.retention_applicability_reference,
                expected_generation=processing.retention_applicability_generation,
            ):
                return self._deny(
                    SourceUsageReason.RETENTION_INELIGIBLE,
                    required,
                    tuple(checked),
                )
            if dependency.dependency_id is None or not await projection_hold_allows(
                uow,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                source_object_id=dependency.source_object_id,
                source_revision_id=dependency.source_revision_id,
                dependency_id=dependency.dependency_id,
                artifact_id=(
                    dependency.artifact_member_id or request.artifact.artifact_id
                ),
                evaluated_at=request.evaluated_at,
                prohibited_effects=self._hold_effects(request.usage),
            ):
                return self._deny(
                    SourceUsageReason.DISCLOSURE_HELD,
                    required,
                    tuple(checked),
                )
            checked.append(dependency)
        return CurrentSourceUsageDecision(
            allowed=True,
            reason=SourceUsageReason.ALLOWED,
            required_dependency_count=required,
            checked_dependencies=tuple(checked),
        )

    @staticmethod
    def _deny(reason, required, checked):
        return CurrentSourceUsageDecision(
            allowed=False,
            reason=reason,
            required_dependency_count=required,
            checked_dependencies=checked,
        )

    @staticmethod
    def _hold_effects(usage: SourceUsageKind) -> tuple[str, ...]:
        if usage is SourceUsageKind.RETRIEVAL:
            return ("retrieval", "model_use")
        if usage is SourceUsageKind.FINAL_DISCLOSURE:
            return ("final_disclosure", "derived_artifact_disclosure")
        return ("model_use", "derived_artifact_disclosure")


__all__ = ["TransactionCurrentSourceUsageAuthority"]
