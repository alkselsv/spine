from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx

from spine.api.documents import install_document_query_routes
from spine.application.document_queries import (
    DocumentIdentitySnapshot,
    DocumentPublicationStatus,
    DocumentPage,
    DocumentPosition,
    DocumentSnapshot,
    DocumentQueryFilters,
    DocumentQueryRequester,
    OriginalReferenceSnapshot,
    ProcessingReferenceSnapshot,
    ProjectionReferenceSnapshot,
    PublicationSnapshot,
    RevisionContentSnapshot,
    RevisionLifecycle,
    RevisionPosition,
    RevisionSnapshot,
    StableLocatorSnapshot,
)
from spine.domain.ingestion import IngestionItemState
from spine.domain.projections import ProjectionHealth
from spine.domain.sources import RevisionKind, SourceKind
from spine.auth import AuthorizationResolution
from tests.api.test_authorization import (
    CONFIGURATION_VERSION,
    ENVIRONMENT_ID,
    OTHER_ENVIRONMENT_ID,
    OTHER_WORKSPACE_ID,
    TRACE_ID,
    WORKSPACE_ID,
    authorized_app,
)


SOURCE_ID = UUID("51000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("51000000-0000-0000-0000-000000000002")
TEAM_ID = UUID("51000000-0000-0000-0000-000000000004")


class TeamAuthorizationDirectory:
    async def resolve_request_authority(
        self,
        authenticated_alias,
        requested_scope,
        route_policy,
    ) -> AuthorizationResolution:
        del authenticated_alias
        return AuthorizationResolution(
            acting_subject_id=UUID("10000000-0000-0000-0000-000000000073"),
            scope=requested_scope,
            roles=route_policy.required_roles,
            authorization_generation=1,
            authentication_configuration_version=CONFIGURATION_VERSION,
            effective_team_ids=frozenset({TEAM_ID}),
        )


def _current_revision() -> RevisionSnapshot:
    return RevisionSnapshot(
        source_revision_id=REVISION_ID,
        kind=RevisionKind.CONTENT,
        observed_at=datetime(2026, 10, 15, 10, tzinfo=timezone.utc),
        lifecycle=RevisionLifecycle.CURRENT,
        accepted_generation=2,
        accepted_at=datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc),
    )


class FakeDocumentQueries:
    include_content = False
    has_more = False

    def __init__(self) -> None:
        self.last_filters: DocumentQueryFilters | None = None

    async def list_documents(
        self,
        *,
        context,
        requester: DocumentQueryRequester,
        limit: int,
        after: DocumentPosition | None,
        filters: DocumentQueryFilters | None = None,
    ) -> DocumentPage[DocumentSnapshot]:
        assert context.scope.workspace_id == WORKSPACE_ID
        assert context.scope.environment_id == ENVIRONMENT_ID
        assert requester.canonical_human_id.int != 0
        assert limit == 25
        assert after is None
        self.last_filters = filters
        return DocumentPage(
            items=(
                DocumentSnapshot(
                    source_object_id=SOURCE_ID,
                    source_kind="document",
                    created_at=datetime(2026, 10, 15, 9, tzinfo=timezone.utc),
                    current_revision=_current_revision(),
                ),
            ),
            has_more=self.has_more,
        )

    async def get_document(
        self,
        *,
        context,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
    ) -> DocumentSnapshot | None:
        assert context.scope.workspace_id == WORKSPACE_ID
        assert requester.canonical_human_id.int != 0
        if source_object_id != SOURCE_ID:
            return None
        revision = _current_revision()
        identity = None
        if self.include_content:
            identity = DocumentIdentitySnapshot(
                identity_mode="upload",
                upload_identity="proposal.pdf",
            )
            revision = revision.model_copy(
                update={
                    "content": RevisionContentSnapshot(
                        revision_digest="a" * 64,
                        original_sha256="b" * 64,
                        byte_length=42,
                        media_type="application/pdf",
                        original_reference=OriginalReferenceSnapshot(
                            object_id=UUID("51000000-0000-0000-0000-000000000010"),
                            storage_generation=UUID("51000000-0000-0000-0000-000000000011"),
                            digest_algorithm="sha256",
                            digest_hex="b" * 64,
                            byte_length=42,
                        ),
                        embedded_title="Protected proposal",
                        locator=StableLocatorSnapshot(
                            locator_kind="page",
                            locator_value="7",
                            locator_schema="pdf-page:v1",
                        ),
                    )
                }
            )
        return DocumentSnapshot(
            source_object_id=SOURCE_ID,
            source_kind="document",
            created_at=datetime(2026, 10, 15, 9, tzinfo=timezone.utc),
            current_revision=revision,
            identity=identity,
        )

    async def list_revisions(
        self,
        *,
        context,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
        limit: int,
        after: RevisionPosition | None,
    ) -> DocumentPage[RevisionSnapshot] | None:
        if source_object_id != SOURCE_ID:
            return None
        revisions = (
            _current_revision(),
            RevisionSnapshot(
                source_revision_id=UUID("51000000-0000-0000-0000-000000000003"),
                kind=RevisionKind.CONTENT,
                observed_at=datetime(2026, 10, 14, 10, tzinfo=timezone.utc),
                lifecycle=RevisionLifecycle.SUPERSEDED,
                superseded_by_revision_id=REVISION_ID,
                accepted_generation=1,
                accepted_at=datetime(2026, 10, 14, 10, 1, tzinfo=timezone.utc),
            ),
        )
        if after is None:
            return DocumentPage(items=revisions[:limit], has_more=limit < len(revisions))
        assert after.source_revision_id == revisions[0].source_revision_id
        return DocumentPage(items=revisions[1:], has_more=False)


class TeamAwareDocumentQueries(FakeDocumentQueries):
    async def get_document(self, **kwargs):
        requester = kwargs["requester"]
        assert requester.effective_team_ids == frozenset({TEAM_ID})
        return await super().get_document(**kwargs)


async def test_authorized_document_collection_returns_only_safe_operational_metadata() -> None:
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=FakeDocumentQueries(),
        cursor_secret=b"issue-294-document-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/documents",
            params={"limit": 25},
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "workspace_id": str(WORKSPACE_ID),
        "environment_id": str(ENVIRONMENT_ID),
        "trace_id": str(TRACE_ID),
        "items": [
            {
                "schema_version": 1,
                "source_object_id": str(SOURCE_ID),
                "source_kind": "document",
                "created_at": "2026-10-15T09:00:00Z",
                "current_revision": {
                    "schema_version": 1,
                    "source_revision_id": str(REVISION_ID),
                    "kind": "content",
                    "observed_at": "2026-10-15T10:00:00Z",
                    "lifecycle": "current",
                        "admission_status": None,
                        "predecessor_revision_id": None,
                        "superseded_by_revision_id": None,
                        "deletion_reason": None,
                    "accepted_generation": 2,
                    "accepted_at": "2026-10-15T10:01:00Z",
                        "processing_references": [],
                        "projection_references": [],
                },
                    "publication": None,
            }
        ],
        "limit": 25,
        "next_cursor": None,
    }
    assert "digest" not in response.text
    assert "locator" not in response.text
    assert "filename" not in response.text


async def test_document_collection_accepts_only_bounded_operational_filters() -> None:
    service = FakeDocumentQueries()
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=service,
        cursor_secret=b"issue-339-document-cursor-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/documents",
            params={
                "limit": 25,
                "source_kind": "document",
                "lifecycle": "current",
                "processing_state": "processed",
                "publication_status": "published",
                "publication_health": "healthy",
            },
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert service.last_filters == DocumentQueryFilters(
        source_kind=SourceKind.DOCUMENT,
        lifecycle=RevisionLifecycle.CURRENT,
        processing_state=IngestionItemState.PROCESSED,
        publication_status=DocumentPublicationStatus.PUBLISHED,
        publication_health=ProjectionHealth.HEALTHY,
    )


async def test_document_cursor_is_bound_to_exact_filters() -> None:
    service = FakeDocumentQueries()
    service.has_more = True
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=service,
        cursor_secret=b"issue-339-document-cursor-secret",
    )
    headers = {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        first = await client.get(
            "/api/v1/documents",
            params={"limit": 25, "source_kind": "document"},
            headers=headers,
        )
        second = await client.get(
            "/api/v1/documents",
            params={
                "limit": 25,
                "source_kind": "message",
                "cursor": first.json()["next_cursor"],
            },
            headers=headers,
        )

    assert first.status_code == 200
    assert first.json()["next_cursor"] is not None
    assert second.status_code == 400
    assert second.json()["code"] == "spine.request.invalid_cursor"
    assert str(SOURCE_ID) not in second.text


async def test_document_filter_validation_fails_closed_without_filter_details() -> None:
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=FakeDocumentQueries(),
        cursor_secret=b"issue-339-document-cursor-secret",
    )
    headers = {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        unsupported = await client.get(
            "/api/v1/documents",
            params={"source_kind": "secret"},
            headers=headers,
        )
        impossible = await client.get(
            "/api/v1/documents",
            params={
                "publication_status": "unpublished",
                "publication_health": "healthy",
            },
            headers=headers,
        )
        unknown = await client.get(
            "/api/v1/documents",
            params={"title": "protected"},
            headers=headers,
        )

    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "spine.request.validation"
    assert impossible.status_code == 422
    assert impossible.json()["code"] == "spine.request.rejected"
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "spine.request.rejected"
    assert "protected" not in unknown.text


def test_document_filter_vocabulary_is_published_in_openapi() -> None:
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=FakeDocumentQueries(),
        cursor_secret=b"issue-339-document-cursor-secret",
    )

    operation = application.openapi()["paths"]["/api/v1/documents"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert set(
        (
            "source_kind",
            "lifecycle",
            "processing_state",
            "publication_status",
            "publication_health",
        )
    ).issubset(parameters)
    assert application.openapi()["components"]["schemas"]["SourceKind"]["enum"] == [
        "document",
        "message",
        "record",
    ]


async def test_document_detail_omits_protected_fields_without_read_content() -> None:
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=FakeDocumentQueries(),
        cursor_secret=b"issue-294-document-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/documents/{SOURCE_ID}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert "identity" not in response.json()["data"]
    assert "content" not in response.json()["data"]["current_revision"]
    assert "proposal.pdf" not in response.text


async def test_document_detail_releases_exact_revision_fields_to_content_reader() -> None:
    service = FakeDocumentQueries()
    service.include_content = True
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=service,
        cursor_secret=b"issue-294-document-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/documents/{SOURCE_ID}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["identity"] == {
        "identity_mode": "upload",
        "connection_id": None,
        "external_namespace": None,
        "external_generation": None,
        "external_object_id": None,
        "upload_identity": "proposal.pdf",
    }
    assert response.json()["data"]["current_revision"]["content"]["locator"] == {
        "locator_kind": "page",
        "locator_value": "7",
        "locator_schema": "pdf-page:v1",
    }


async def test_document_detail_propagates_trusted_effective_teams() -> None:
    application = authorized_app([], directory=TeamAuthorizationDirectory())
    install_document_query_routes(
        application,
        service=TeamAwareDocumentQueries(),
        cursor_secret=b"issue-294-document-cursor-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/documents/{SOURCE_ID}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200


async def test_revision_history_is_cursor_paginated_and_cursor_tampering_fails_closed() -> None:
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=FakeDocumentQueries(),
        cursor_secret=b"issue-294-document-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)
    headers = {
        "Authorization": "Bearer opaque-provider-token",
        "X-Workspace-ID": str(WORKSPACE_ID),
        "X-Environment-ID": str(ENVIRONMENT_ID),
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            f"/api/v1/documents/{SOURCE_ID}/revisions",
            params={"limit": 1},
            headers=headers,
        )
        cursor = first.json()["next_cursor"]
        second = await client.get(
            f"/api/v1/documents/{SOURCE_ID}/revisions",
            params={"limit": 1, "cursor": cursor},
            headers=headers,
        )
        tampered = await client.get(
            f"/api/v1/documents/{SOURCE_ID}/revisions",
            params={"limit": 1, "cursor": cursor[:-1] + "x"},
            headers=headers,
        )

    assert first.status_code == 200
    assert first.json()["items"][0]["lifecycle"] == "current"
    assert second.status_code == 200
    assert second.json()["items"][0]["lifecycle"] == "superseded"
    assert tampered.status_code == 400
    assert tampered.json()["code"] == "spine.request.invalid_cursor"
    assert str(REVISION_ID) not in tampered.text


async def test_missing_and_foreign_documents_fail_without_enumeration() -> None:
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=FakeDocumentQueries(),
        cursor_secret=b"issue-294-document-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)
    missing_id = UUID("51000000-0000-0000-0000-000000000099")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get(
            f"/api/v1/documents/{missing_id}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )
        foreign = await client.get(
            f"/api/v1/documents/{SOURCE_ID}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(OTHER_WORKSPACE_ID),
                "X-Environment-ID": str(OTHER_ENVIRONMENT_ID),
            },
        )

    assert missing.status_code == 404
    assert missing.json()["code"] == "spine.documents.not_found"
    assert str(missing_id) not in missing.text
    assert foreign.status_code == 403
    assert str(SOURCE_ID) not in foreign.text


class OperationalDocumentQueries(FakeDocumentQueries):
    async def get_document(
        self,
        *,
        context,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
    ) -> DocumentSnapshot | None:
        del context, requester
        if source_object_id != SOURCE_ID:
            return None
        return DocumentSnapshot(
            source_object_id=SOURCE_ID,
            source_kind="document",
            created_at=datetime(2026, 10, 15, 9, tzinfo=timezone.utc),
            current_revision=RevisionSnapshot(
                source_revision_id=REVISION_ID,
                kind=RevisionKind.TOMBSTONE,
                observed_at=datetime(2026, 10, 15, 10, tzinfo=timezone.utc),
                lifecycle=RevisionLifecycle.TOMBSTONED,
                deletion_reason="source_deleted",
                accepted_generation=3,
                accepted_at=datetime(2026, 10, 15, 10, 1, tzinfo=timezone.utc),
                processing_references=(
                    ProcessingReferenceSnapshot(
                        ingestion_run_id=UUID("51000000-0000-0000-0000-000000000020"),
                        ingestion_item_id=UUID("51000000-0000-0000-0000-000000000021"),
                        state="processed",
                        coverage="complete",
                    ),
                ),
                projection_references=(
                    ProjectionReferenceSnapshot(
                        projection_run_id=UUID("51000000-0000-0000-0000-000000000022"),
                        projection_candidate_id=UUID("51000000-0000-0000-0000-000000000023"),
                        projection_kind="qa",
                        state="complete",
                    ),
                ),
            ),
            publication=PublicationSnapshot(
                projection_kind="qa",
                status="published",
                snapshot_id=UUID("51000000-0000-0000-0000-000000000024"),
                freshness="stale",
                health="degraded",
                drift_count=1,
                manifest_complete=True,
                source_covered=False,
            ),
        )


async def test_document_detail_represents_tombstone_processing_and_stale_degraded_publication() -> None:
    application = authorized_app([])
    install_document_query_routes(
        application,
        service=OperationalDocumentQueries(),
        cursor_secret=b"issue-294-document-cursor-secret",
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/documents/{SOURCE_ID}",
            headers={
                "Authorization": "Bearer opaque-provider-token",
                "X-Workspace-ID": str(WORKSPACE_ID),
                "X-Environment-ID": str(ENVIRONMENT_ID),
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["current_revision"]["lifecycle"] == "tombstoned"
    assert data["current_revision"]["deletion_reason"] == "source_deleted"
    assert data["current_revision"]["processing_references"][0]["state"] == "processed"
    assert data["current_revision"]["projection_references"][0]["state"] == "complete"
    assert data["publication"] == {
        "projection_kind": "qa",
        "status": "published",
        "snapshot_id": "51000000-0000-0000-0000-000000000024",
        "freshness": "stale",
        "health": "degraded",
        "drift_count": 1,
        "manifest_complete": True,
        "source_covered": False,
    }
    assert "identity" not in data
    assert "content" not in data["current_revision"]
