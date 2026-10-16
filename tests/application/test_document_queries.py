from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.document_queries import (
    CanonicalDocumentQueryService,
    DocumentPublicationStatus,
    DocumentQueryFilters,
    DocumentQueryRequester,
    DocumentSnapshot,
    PublicationSnapshot,
    ProcessingReferenceSnapshot,
    RevisionSnapshot,
    RevisionLifecycle,
)
from spine.application.persistence.context import EnvironmentScope
from spine.domain.ingestion import IngestionItemState
from spine.domain.projections import ProjectionHealth, UnpublishedActivePublication
from spine.domain.sources import RevisionKind, SourceKind
from spine.domain.sources import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    CanonicalAcceptance,
    CanonicalOrdering,
    CurrentAccessPolicy,
    CurrentSourceHead,
    PolicyOriginKind,
    PrincipalKind,
    SourceSecurityDomainBinding,
)
from tests.application.persistence.test_source_observation import (
    provenance,
    source,
    valid_revision,
)


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
HUMAN_ID = UUID("52000000-0000-0000-0000-000000000001")


class FakeSources:
    def __init__(self, source_object, source_revision, source_provenance) -> None:
        self.source = source_object
        self.source_objects = (source_object,)
        self.revision = source_revision
        self.provenance = source_provenance

    async def resolve_source(self, source_object_id):
        return next(
            (
                value
                for value in self.source_objects
                if value.source_object_id == source_object_id
            ),
            None,
        )

    async def resolve_revision(self, revision_id):
        return self.revision if revision_id == self.revision.revision_id else None

    async def list_provenance(self, revision_id):
        return (self.provenance,) if revision_id == self.revision.revision_id else ()

    async def list_sources(self, *, limit, after=None):
        values = sorted(
            (
                value
                for value in self.source_objects
                if after is None or (value.created_at, value.source_object_id) < after
            ),
            key=lambda value: (value.created_at, value.source_object_id),
            reverse=True,
        )
        return tuple(values[:limit])

    async def list_revisions(self, source_object_id, *, limit, after=None):
        del after
        if source_object_id != self.source.source_object_id:
            return ()
        return (self.revision,)[:limit]


class FakeCanonical:
    def __init__(self, head, acceptance) -> None:
        self.head = head
        self.acceptance = acceptance

    async def resolve_current(self, source_object_id):
        return self.head if source_object_id == self.head.source_object_id else None

    async def list_acceptances(self, source_object_id):
        return (self.acceptance,) if source_object_id == self.head.source_object_id else ()


class FakeAdmissions:
    async def resolve_effective(self, revision_id):
        del revision_id
        return None


class FakeIngestion:
    async def list_bindings_for_source_revision(self, source_object_id, source_revision_id):
        del source_object_id, source_revision_id
        return ()


class FakeProjections:
    async def list_runs_for_source_revision(self, source_object_id, source_revision_id):
        del source_object_id, source_revision_id
        return ()

    async def resolve_active_publication(self, projection_kind):
        assert projection_kind == "qa"
        return UnpublishedActivePublication(
            workspace_id=self.workspace_id,
            environment_id=self.environment_id,
            projection_kind="qa",
        )


class FakePolicies:
    def __init__(self, current, policy, binding=None) -> None:
        self.current = current
        self.policy = policy
        self.binding = binding

    async def resolve_current(self, source_object_id, **scope):
        del scope
        return self.current if source_object_id == self.current.source_object_id else None

    async def resolve_policy(self, policy_version_id, **scope):
        del scope
        return self.policy if policy_version_id == self.policy.access_policy_version_id else None

    async def resolve_security_domain_binding(self, binding_id, **scope):
        del scope
        return self.binding if self.binding and binding_id == self.binding.binding_id else None


class FakeUnitOfWork:
    def __init__(self, *, allow_content: bool, team_id: UUID | None = None) -> None:
        source_object = source()
        source_revision = valid_revision(source_object)
        source_provenance = provenance(source_object, source_revision)
        policy_id = UUID("52000000-0000-0000-0000-000000000002")
        grants = (
            AccessGrant(
                access_grant_id=UUID("52000000-0000-0000-0000-000000000003"),
                principal_kind=(PrincipalKind.TEAM if team_id else PrincipalKind.HUMAN),
                principal_id=(team_id or HUMAN_ID),
                permission=AccessPermission.READ_CONTENT,
            ),
        ) if allow_content else ()
        policy = AccessPolicyVersion(
            access_policy_version_id=policy_id,
            workspace_id=source_object.workspace_id,
            environment_id=source_object.environment_id,
            source_object_id=source_object.source_object_id,
            version=1,
            predecessor_policy_version_id=None,
            document_schema_version="access-policy.v1",
            document_hash="a" * 64,
            origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
            origin_reference=UUID("52000000-0000-0000-0000-000000000004"),
            origin_version=1,
            origin_hash="b" * 64,
            created_by_canonical_human_id=HUMAN_ID,
            created_at=NOW,
            grants=grants,
        )
        acceptance = CanonicalAcceptance(
            canonical_acceptance_id=UUID("52000000-0000-0000-0000-000000000005"),
            source_object_id=source_object.source_object_id,
            source_revision_id=source_revision.revision_id,
            workspace_id=source_object.workspace_id,
            environment_id=source_object.environment_id,
            accepted_generation=1,
            ordering=CanonicalOrdering.EXPECTED_CURRENT,
            access_policy_version_id=policy_id,
            provenance_id=source_provenance.provenance_id,
            idempotency_receipt_id=UUID("52000000-0000-0000-0000-000000000006"),
            audit_event_id=UUID("52000000-0000-0000-0000-000000000007"),
            outbox_event_id=UUID("52000000-0000-0000-0000-000000000008"),
            accepted_at=NOW,
        )
        head = CurrentSourceHead(
            source_object_id=source_object.source_object_id,
            workspace_id=source_object.workspace_id,
            environment_id=source_object.environment_id,
            current_revision_id=source_revision.revision_id,
            current_revision_kind=source_revision.kind,
            acceptance_id=acceptance.canonical_acceptance_id,
            accepted_generation=1,
            cas_version=1,
            changed_at=NOW,
        )
        current_policy = CurrentAccessPolicy(
            workspace_id=source_object.workspace_id,
            environment_id=source_object.environment_id,
            source_object_id=source_object.source_object_id,
            access_policy_version_id=policy_id,
            policy_activation_id=UUID("52000000-0000-0000-0000-000000000009"),
            policy_generation=1,
            cas_version=1,
        )
        self.source = source_object
        self.sources = FakeSources(source_object, source_revision, source_provenance)
        self.canonical = FakeCanonical(head, acceptance)
        self.admissions = FakeAdmissions()
        self.ingestion = FakeIngestion()
        self.projections = FakeProjections()
        self.projections.workspace_id = source_object.workspace_id
        self.projections.environment_id = source_object.environment_id
        self.access_policies = FakePolicies(current_policy, policy)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback

    async def commit(self):
        self.committed = True


async def test_current_policy_controls_protected_document_fields() -> None:
    denied_uow = FakeUnitOfWork(allow_content=False)
    allowed_uow = FakeUnitOfWork(allow_content=True)
    requester = DocumentQueryRequester(
        canonical_human_id=HUMAN_ID,
        authorization_generation=1,
    )

    denied = await CanonicalDocumentQueryService(lambda context: denied_uow).get_document(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=denied_uow.source.workspace_id,
                environment_id=denied_uow.source.environment_id,
            )
        ),
        requester=requester,
        source_object_id=denied_uow.source.source_object_id,
    )
    allowed = await CanonicalDocumentQueryService(lambda context: allowed_uow).get_document(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=allowed_uow.source.workspace_id,
                environment_id=allowed_uow.source.environment_id,
            )
        ),
        requester=requester,
        source_object_id=allowed_uow.source.source_object_id,
    )

    assert denied is not None and denied.identity is None
    assert denied.current_revision is not None
    assert denied.current_revision.content is None
    assert denied.current_revision.lifecycle is RevisionLifecycle.CURRENT
    assert allowed is not None and allowed.identity is not None
    assert allowed.current_revision is not None
    assert allowed.current_revision.content is not None
    assert allowed.current_revision.content.embedded_title == "Title"
    assert allowed.publication is not None
    assert allowed.publication.status == "unpublished"


async def test_document_detail_exposes_safe_policy_metadata_without_content_grant() -> None:
    uow = FakeUnitOfWork(allow_content=False)

    snapshot = await CanonicalDocumentQueryService(lambda context: uow).get_document(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
        ),
        source_object_id=uow.source.source_object_id,
    )

    assert snapshot is not None
    assert snapshot.operational_metadata is not None
    metadata = snapshot.operational_metadata
    assert metadata.access_policy is not None
    assert metadata.access_policy.policy_version_id.int != 0
    assert metadata.access_policy.version == 1
    assert metadata.access_policy.generation == 1
    assert metadata.security_domain is None
    assert snapshot.identity is None
    assert snapshot.current_revision is not None
    assert snapshot.current_revision.content is None
    assert "grants" not in metadata.model_dump()


async def test_document_detail_resolves_canonical_security_domain_binding() -> None:
    uow = FakeUnitOfWork(allow_content=False)
    binding = SourceSecurityDomainBinding(
        binding_id=UUID("52000000-0000-0000-0000-000000000040"),
        workspace_id=uow.source.workspace_id,
        environment_id=uow.source.environment_id,
        source_object_id=uow.source.source_object_id,
        security_domain_reference="legal/eu",
        security_domain_version="security-domain.v1",
        binding_version=1,
        predecessor_binding_id=None,
        created_at=NOW,
        policy_activation_id=uow.access_policies.current.policy_activation_id,
    )
    policy = uow.access_policies.policy.model_copy(
        update={
            "document_schema_version": "access-policy.v2",
            "security_domain_binding_id": binding.binding_id,
        }
    )
    uow.access_policies.policy = policy
    uow.access_policies.binding = binding

    snapshot = await CanonicalDocumentQueryService(lambda context: uow).get_document(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
        ),
        source_object_id=uow.source.source_object_id,
    )

    assert snapshot is not None and snapshot.operational_metadata is not None
    metadata = snapshot.operational_metadata
    assert metadata.status.value == "available"
    assert metadata.security_domain is not None
    assert metadata.security_domain.reference == "legal/eu"
    assert metadata.security_domain.version == "security-domain.v1"
    assert metadata.security_domain.binding_version == 1


async def test_missing_canonical_policy_returns_safe_unavailable_metadata() -> None:
    uow = FakeUnitOfWork(allow_content=False)
    uow.access_policies.current = None

    snapshot = await CanonicalDocumentQueryService(lambda context: uow).get_document(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
        ),
        source_object_id=uow.source.source_object_id,
    )

    assert snapshot is not None and snapshot.operational_metadata is not None
    assert snapshot.operational_metadata.status.value == "unavailable"
    assert snapshot.operational_metadata.diagnostic_code.value == "policy_unavailable"
    assert snapshot.operational_metadata.access_policy is None
    assert "provider" not in snapshot.model_dump_json()


async def test_current_policy_accepts_effective_team_grant_for_exact_document_detail() -> None:
    team_id = UUID("52000000-0000-0000-0000-000000000010")
    uow = FakeUnitOfWork(allow_content=True, team_id=team_id)

    snapshot = await CanonicalDocumentQueryService(lambda context: uow).get_document(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
            effective_team_ids=frozenset({team_id}),
        ),
        source_object_id=uow.source.source_object_id,
    )

    assert snapshot is not None and snapshot.identity is not None
    assert snapshot.current_revision is not None
    assert snapshot.current_revision.content is not None


async def test_collection_never_bulk_discloses_protected_current_revision_fields() -> None:
    uow = FakeUnitOfWork(allow_content=True)

    page = await CanonicalDocumentQueryService(lambda context: uow).list_documents(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
        ),
        limit=10,
        after=None,
    )

    assert page.items[0].identity is None
    assert page.items[0].current_revision is not None
    assert page.items[0].current_revision.content is None


async def test_revision_history_never_bulk_discloses_protected_content() -> None:
    uow = FakeUnitOfWork(allow_content=True)

    page = await CanonicalDocumentQueryService(lambda context: uow).list_revisions(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
        ),
        source_object_id=uow.source.source_object_id,
        limit=10,
        after=None,
    )

    assert page is not None
    assert page.items[0].content is None


async def test_document_queries_hide_source_from_another_workspace_and_environment() -> None:
    uow = FakeUnitOfWork(allow_content=True)
    requester = DocumentQueryRequester(
        canonical_human_id=HUMAN_ID,
        authorization_generation=1,
    )
    service = CanonicalDocumentQueryService(lambda context: uow)

    for context in (
        SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=UUID("52000000-0000-0000-0000-000000000020"),
                environment_id=uow.source.environment_id,
            )
        ),
        SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=UUID("52000000-0000-0000-0000-000000000021"),
            )
        ),
    ):
        detail = await service.get_document(
            context=context,
            requester=requester,
            source_object_id=uow.source.source_object_id,
        )
        history = await service.list_revisions(
            context=context,
            requester=requester,
            source_object_id=uow.source.source_object_id,
            limit=10,
            after=None,
        )
        collection = await service.list_documents(
            context=context,
            requester=requester,
            limit=10,
            after=None,
            filters=DocumentQueryFilters(source_kind=SourceKind.DOCUMENT),
        )

        assert detail is None
        assert history is None
        assert collection.items == ()


async def test_document_query_filters_before_forming_the_bounded_page() -> None:
    uow = FakeUnitOfWork(allow_content=False)
    matching_source = uow.source.model_copy(
        update={
            "source_object_id": UUID("52000000-0000-0000-0000-000000000020"),
            "source_kind": "record",
            "created_at": uow.source.created_at + timedelta(minutes=1),
        }
    )
    uow.sources.source_objects = (uow.source, matching_source)
    service = CanonicalDocumentQueryService(lambda context: uow)

    page = await service.list_documents(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
        ),
        limit=1,
        after=None,
        filters=DocumentQueryFilters(source_kind=SourceKind.RECORD),
    )

    assert [item.source_object_id for item in page.items] == [
        matching_source.source_object_id
    ]
    assert page.has_more is False


async def test_document_query_filter_returns_empty_page_without_leaking_other_sources() -> None:
    uow = FakeUnitOfWork(allow_content=True)
    service = CanonicalDocumentQueryService(lambda context: uow)

    page = await service.list_documents(
        context=SimpleNamespace(
            scope=EnvironmentScope(
                workspace_id=uow.source.workspace_id,
                environment_id=uow.source.environment_id,
            )
        ),
        requester=DocumentQueryRequester(
            canonical_human_id=HUMAN_ID,
            authorization_generation=1,
        ),
        limit=10,
        after=None,
        filters=DocumentQueryFilters(source_kind=SourceKind.RECORD),
    )

    assert page.items == ()
    assert page.has_more is False


def test_document_filters_match_only_bounded_operational_metadata() -> None:
    snapshot = DocumentSnapshot(
        source_object_id=UUID("52000000-0000-0000-0000-000000000030"),
        source_kind="document",
        created_at=NOW,
        current_revision=RevisionSnapshot(
            source_revision_id=UUID("52000000-0000-0000-0000-000000000031"),
            kind=RevisionKind.CONTENT,
            observed_at=NOW,
            lifecycle=RevisionLifecycle.CURRENT,
            processing_references=(
                ProcessingReferenceSnapshot(
                    ingestion_run_id=UUID("52000000-0000-0000-0000-000000000032"),
                    ingestion_item_id=UUID("52000000-0000-0000-0000-000000000033"),
                    state=IngestionItemState.PROCESSED,
                ),
            ),
        ),
        publication=PublicationSnapshot(
            projection_kind="qa",
            status=DocumentPublicationStatus.PUBLISHED,
            health=ProjectionHealth.HEALTHY,
        ),
    )

    filters = DocumentQueryFilters(
        source_kind=SourceKind.DOCUMENT,
        lifecycle=RevisionLifecycle.CURRENT,
        processing_state=IngestionItemState.PROCESSED,
        publication_status=DocumentPublicationStatus.PUBLISHED,
        publication_health=ProjectionHealth.HEALTHY,
    )

    assert filters.matches(snapshot)
    assert filters.digest_payload() == {
        "lifecycle": "current",
        "processing_state": "processed",
        "publication_health": "healthy",
        "publication_status": "published",
        "source_kind": "document",
    }


def test_document_filters_reject_health_for_non_published_documents() -> None:
    with pytest.raises(ValueError, match="publication_health"):
        DocumentQueryFilters(
            publication_status=DocumentPublicationStatus.UNPUBLISHED,
            publication_health=ProjectionHealth.HEALTHY,
        )
