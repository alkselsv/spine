from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.crm_relationship_v2_source_match import (
    CanonicalDiskSourceQuery,
    CrmFilesItemIdentity,
    DiskRevisionIdentity,
)
from spine.domain.sources import (
    LifecycleAuthorityKind,
    LifecycleDecision,
    LifecycleDecisionEffect,
)
from spine.infrastructure.persistence.postgresql_crm_relationship_v2_source_match import (
    PostgreSQLCanonicalDiskSourceFinder,
)
from tests.application.test_crm_relationship_v2_source_match import (
    NOW,
    ident,
    source_candidate,
)


class Rows:
    def __init__(self, values) -> None:
        self.values = values

    def mappings(self):
        return self

    def all(self):
        return self.values


class SourcesFake:
    def __init__(self, candidate) -> None:
        self.candidate = candidate

    async def resolve_source(self, source_object_id):
        assert source_object_id == self.candidate.source.source_object_id
        return self.candidate.source

    async def resolve_revision(self, revision_id):
        assert revision_id == self.candidate.revision.revision_id
        return self.candidate.revision

    async def list_provenance(self, revision_id):
        assert revision_id == self.candidate.revision.revision_id
        return (self.candidate.provenance,)

    async def resolve_receipt_associations(self, revision_id):
        assert revision_id == self.candidate.revision.revision_id
        return (self.candidate.receipt,)


class TransactionFake:
    def __init__(self, candidate, decision: LifecycleDecision) -> None:
        self.sources = SourcesFake(candidate)
        self.row = SimpleNamespace(
            source_object_id=candidate.source.source_object_id,
            current_revision_id=candidate.revision.revision_id,
            current_decision_id=decision.decision_id,
            lifecycle_generation=decision.generation,
            lifecycle_record=decision.model_dump(mode="json"),
        )

    async def _execute(self, statement):
        del statement
        return Rows([self.row])


def lifecycle_decision(candidate) -> LifecycleDecision:
    lifecycle = candidate.lifecycle
    return LifecycleDecision(
        decision_id=lifecycle.current_decision_id,
        workspace_id=lifecycle.workspace_id,
        environment_id=lifecycle.environment_id,
        source_object_id=lifecycle.source_object_id,
        observation_id=ident("lifecycle-observation"),
        connection_id=lifecycle.connection_id,
        connection_version_id=lifecycle.connection_version_id,
        scope_reference=lifecycle.scope_reference,
        scope_version_id=lifecycle.scope_version_id,
        scope_digest=lifecycle.scope_digest,
        effect=LifecycleDecisionEffect.ELIGIBLE,
        authority_reference=ident("lifecycle-authority"),
        authority_version_id=ident("lifecycle-authority-version"),
        authority_digest="7" * 64,
        authority_kind=LifecycleAuthorityKind.SOURCE_GOVERNANCE,
        status=lifecycle.status,
        legal_hold_active=lifecycle.legal_hold_active,
        expected_current_decision_id=ident("previous-lifecycle-decision"),
        expected_current_generation=lifecycle.generation - 1,
        generation=lifecycle.generation,
        actor_id=ident("lifecycle-actor"),
        audit_event_id=ident("lifecycle-audit"),
        outbox_event_id=ident("lifecycle-outbox"),
        decided_at=NOW,
    )


@pytest.mark.asyncio
async def test_postgresql_finder_returns_exact_existing_source_records() -> None:
    candidate = source_candidate()
    transaction = TransactionFake(candidate, lifecycle_decision(candidate))
    query = CanonicalDiskSourceQuery(
        workspace_id=candidate.source.workspace_id,
        environment_id=candidate.source.environment_id,
        connection_id=candidate.provenance.connection_id,
        connection_version_id=UUID(int=4),
        relationship_files_item=CrmFilesItemIdentity(
            namespace="bitrix24-crm-files-item",
            value="files-42",
        ),
        mapping_profile_id=UUID(int=5),
        mapping_profile_version=2,
        disk_revision=DiskRevisionIdentity(
            namespace="bitrix24-disk-object",
            object_id="disk-42",
            provider_revision="17",
        ),
    )
    membership = candidate.provenance.bitrix24_disk_membership
    assert membership is not None
    query = query.model_copy(
        update={"connection_version_id": membership.connection_version_id}
    )

    result = await PostgreSQLCanonicalDiskSourceFinder().find_current(
        query, transaction
    )

    assert result == (candidate,)
