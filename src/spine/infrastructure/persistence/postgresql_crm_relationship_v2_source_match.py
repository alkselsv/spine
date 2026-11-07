"""PostgreSQL exact lookup for CRM v2 Disk-to-source matching."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select

from spine.application.crm_relationship_v2_source_match import (
    BITRIX24_DISK_SOURCE_GENERATION,
    BITRIX24_DISK_SOURCE_NAMESPACE,
    CanonicalDiskSourceCandidate,
    CanonicalDiskSourceQuery,
    CrmRelationshipV2SourceMatcher,
    ExistingCanonicalDiskSourceAuthority,
)
from spine.domain.sources import CurrentLifecycleEligibility, LifecycleDecision
from spine.infrastructure.persistence.postgresql_mappings import (
    current_lifecycle_authorities,
    current_source_heads,
    lifecycle_decisions,
    source_objects,
)
from spine.infrastructure.source_usage_guard import (
    TransactionCurrentSourceUsageAuthority,
)


class PostgreSQLCanonicalDiskSourceFinder:
    """Project existing S2/S3 records inside the caller's tenant transaction."""

    async def find_current(
        self, query: CanonicalDiskSourceQuery, transaction: object
    ) -> tuple[CanonicalDiskSourceCandidate, ...]:
        execute = getattr(transaction, "_execute", None)
        sources = getattr(transaction, "sources", None)
        if execute is None or sources is None:
            return ()
        rows = (
            (
                await execute(
                    select(
                        source_objects.c.source_object_id,
                        current_source_heads.c.current_revision_id,
                        current_lifecycle_authorities.c.current_decision_id,
                        current_lifecycle_authorities.c.generation.label(
                            "lifecycle_generation"
                        ),
                        lifecycle_decisions.c.record.label("lifecycle_record"),
                    )
                    .select_from(
                        source_objects.join(
                            current_source_heads,
                            (
                                current_source_heads.c.workspace_id
                                == source_objects.c.workspace_id
                            )
                            & (
                                current_source_heads.c.environment_id
                                == source_objects.c.environment_id
                            )
                            & (
                                current_source_heads.c.source_object_id
                                == source_objects.c.source_object_id
                            ),
                        )
                        .join(
                            current_lifecycle_authorities,
                            (
                                current_lifecycle_authorities.c.workspace_id
                                == source_objects.c.workspace_id
                            )
                            & (
                                current_lifecycle_authorities.c.environment_id
                                == source_objects.c.environment_id
                            )
                            & (
                                current_lifecycle_authorities.c.source_object_id
                                == source_objects.c.source_object_id
                            ),
                        )
                        .join(
                            lifecycle_decisions,
                            (
                                lifecycle_decisions.c.workspace_id
                                == current_lifecycle_authorities.c.workspace_id
                            )
                            & (
                                lifecycle_decisions.c.environment_id
                                == current_lifecycle_authorities.c.environment_id
                            )
                            & (
                                lifecycle_decisions.c.source_object_id
                                == current_lifecycle_authorities.c.source_object_id
                            )
                            & (
                                lifecycle_decisions.c.decision_id
                                == current_lifecycle_authorities.c.current_decision_id
                            )
                            & (
                                lifecycle_decisions.c.generation
                                == current_lifecycle_authorities.c.generation
                            ),
                        )
                    )
                    .where(
                        source_objects.c.workspace_id == query.workspace_id,
                        source_objects.c.environment_id == query.environment_id,
                        source_objects.c.connection_id == query.connection_id,
                        source_objects.c.identity_mode == "connector",
                        source_objects.c.external_namespace
                        == BITRIX24_DISK_SOURCE_NAMESPACE,
                        source_objects.c.external_generation
                        == BITRIX24_DISK_SOURCE_GENERATION,
                        source_objects.c.external_object_id
                        == query.disk_revision.object_id,
                        current_source_heads.c.current_revision_kind == "content",
                    )
                )
            )
            .mappings()
            .all()
        )
        candidates: list[CanonicalDiskSourceCandidate] = []
        for row in rows:
            try:
                decision = LifecycleDecision.model_validate(row.lifecycle_record)
            except Exception:
                continue
            if (
                decision.decision_id != row.current_decision_id
                or decision.generation != row.lifecycle_generation
            ):
                continue
            lifecycle = CurrentLifecycleEligibility(
                workspace_id=decision.workspace_id,
                environment_id=decision.environment_id,
                source_object_id=decision.source_object_id,
                connection_id=decision.connection_id,
                connection_version_id=decision.connection_version_id,
                scope_reference=decision.scope_reference,
                scope_version_id=decision.scope_version_id,
                scope_digest=decision.scope_digest,
                current_decision_id=decision.decision_id,
                generation=decision.generation,
                status=decision.status,
                legal_hold_active=decision.legal_hold_active,
            )
            source = await sources.resolve_source(row.source_object_id)
            revision = await sources.resolve_revision(row.current_revision_id)
            if source is None or revision is None:
                continue
            provenances = await sources.list_provenance(revision.revision_id)
            receipts = await sources.resolve_receipt_associations(revision.revision_id)
            for provenance in provenances:
                membership = provenance.bitrix24_disk_membership
                if (
                    membership is None
                    or membership.connection_version_id != query.connection_version_id
                    or membership.object_id != query.disk_revision.object_id
                    or membership.provider_revision
                    != query.disk_revision.provider_revision
                ):
                    continue
                for receipt in receipts:
                    if receipt.provenance_id == provenance.provenance_id:
                        candidates.append(
                            CanonicalDiskSourceCandidate(
                                source=source,
                                revision=revision,
                                provenance=provenance,
                                receipt=receipt,
                                lifecycle=lifecycle,
                            )
                        )
        return tuple(candidates)


def build_postgresql_crm_relationship_v2_source_matcher(
    *, uow_factory: Callable[..., object], clock: Callable[[], datetime]
) -> CrmRelationshipV2SourceMatcher:
    """Compose the matcher with the existing PostgreSQL S2/S3 fence."""

    return CrmRelationshipV2SourceMatcher(
        ExistingCanonicalDiskSourceAuthority(
            uow_factory=uow_factory,  # type: ignore[arg-type]
            source_finder=PostgreSQLCanonicalDiskSourceFinder(),
            usage_authority_factory=TransactionCurrentSourceUsageAuthority,
            clock=clock,
        )
    )


__all__ = [
    "PostgreSQLCanonicalDiskSourceFinder",
    "build_postgresql_crm_relationship_v2_source_matcher",
]
